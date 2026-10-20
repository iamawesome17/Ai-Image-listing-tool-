"""
drone_photo.py — annotate (and optionally golden-hour relight) a REAL drone photo.

  python drone_photo.py photo.jpg --county adams-il --pin 20-0-0636-002-00 [--golden] [--points "x,y x,y x,y ..."]

Property line placement, in order of preference:
  1. DJI metadata in the photo (GPS, relative altitude, gimbal yaw/pitch, focal length) → the county parcel polygon is
     projected onto the photo automatically (flat-ground pinhole model; good to a few metres at typical altitudes).
  2. --points: pixel corners clicked by the user in the web UI.

Relight: the polygon is drawn as a thin magenta marker, the image model re-lights the photo as golden hour keeping the
marker, the marker is detected, erased and redrawn as the styled outline (same approach as the AI renders).
"""
import argparse, json, math, re, sys, os, io
from pathlib import Path
import numpy as np, cv2
from PIL import Image, ImageDraw, ImageOps

sys.path.insert(0, str(Path(__file__).parent))
import listing_shot as ls, drone_render as dr, pipeline as P


# ---------- DJI / EXIF metadata ----------
def read_metadata(path):
    """Camera pose from DJI XMP + EXIF. Returns dict or None when the essentials are missing."""
    raw = Path(path).read_bytes()
    head = raw[:4_000_000].decode("latin-1", errors="ignore")
    def x(tag):
        m = re.search(rf'drone-dji:{tag}="([^"]+)"', head) or re.search(rf'<drone-dji:{tag}>([^<]+)<', head)
        return float(m.group(1).replace("+", "")) if m else None
    lat, lon = x("GpsLatitude"), x("GpsLongitude")
    img = Image.open(path); exif = img.getexif()
    if lat is None or lon is None:                         # fall back to EXIF GPS
        gps = exif.get_ifd(0x8825) if exif else {}
        def dms(v, ref):
            if not v: return None
            d = float(v[0]) + float(v[1]) / 60 + float(v[2]) / 3600
            return -d if ref in ("S", "W") else d
        lat, lon = dms(gps.get(2), gps.get(1)), dms(gps.get(4), gps.get(3))
    alt = x("RelativeAltitude")
    yaw = x("GimbalYawDegree"); pitch = x("GimbalPitchDegree"); roll = x("GimbalRollDegree") or 0.0
    if yaw is None: yaw = x("FlightYawDegree")
    f35 = exif.get(41989) if exif else None               # FocalLengthIn35mmFilm
    focal = exif.get(37386) if exif else None
    model = (exif.get(272) or "").strip() if exif else ""
    if f35 is None and focal is not None:
        f35 = float(focal) * (24.0 / 8.8 if "FC" in model or "DJI" in model else 1.0)   # rough 1"-sensor crop guess
    if None in (lat, lon, alt, yaw, pitch) or f35 is None:
        return None
    return {"lat": lat, "lon": lon, "alt_m": alt, "yaw": yaw, "pitch": pitch, "roll": roll, "f35": float(f35), "model": model}


# ---------- projection ----------
def project_polygon(meta, poly, W, H):
    """Project lon/lat polygon vertices (flat ground) into photo pixels with a pinhole camera.
    Camera at the drone GPS position, alt_m above ground; yaw = compass heading of the view (clockwise from north);
    pitch: 0 = horizon, -90 = straight down. Returns pixel points or None if any vertex is behind the camera."""
    lat0, lon0 = meta["lat"], meta["lon"]
    kx, ky = 111320 * math.cos(math.radians(lat0)), 110540
    f_px = (W / 2) / math.tan(math.radians(hfov_deg(meta["f35"], W, H) / 2))
    psi, th, roll = map(math.radians, (meta["yaw"], meta["pitch"], meta["roll"]))
    pts = []
    for x, y, *_ in poly.exterior.coords:
        e, n, u = (x - lon0) * kx, (y - lat0) * ky, -meta["alt_m"]
        fwd = e * math.sin(psi) + n * math.cos(psi)                 # along the camera heading
        right = e * math.cos(psi) - n * math.sin(psi)
        depth = fwd * math.cos(th) + u * math.sin(th)                # along the optical axis
        ydown = fwd * math.sin(th) - u * math.cos(th)                # image y (down); far points go up for a tilted camera
        if depth <= 1: return None
        px, py = W / 2 + f_px * right / depth, H / 2 + f_px * ydown / depth
        if roll:
            cx, cy = W / 2, H / 2; c, s_ = math.cos(-roll), math.sin(-roll)
            px, py = cx + (px - cx) * c - (py - cy) * s_, cy + (px - cx) * s_ + (py - cy) * c
        pts.append((px, py))
    return pts


def hfov_deg(f35, W, H):
    # 35mm-equivalent horizontal FOV for a 3:2 frame; adjust for the photo's aspect
    base = 2 * math.degrees(math.atan(18.0 / f35))
    return base if W / H >= 1.45 else 2 * math.degrees(math.atan(math.tan(math.radians(base / 2)) * (W / H) / 1.5))


# ---------- relight ----------
def relight_golden(img, pts, ref_url=None):
    """Draw the marker, send to the image model for a golden-hour relight, return the relit image (marker kept).
    ref_url: optional public URL of an already-marked image (skips the upload)."""
    marked = img.copy(); d = ImageDraw.Draw(marked)
    d.line(pts + [pts[0]], fill=P.FIDUCIAL + (255,), width=max(4, img.width // 300), joint="curve")
    url = ref_url or dr.hf_upload_png(marked.convert("RGB"))
    prompt = ("Re-light this exact aerial photograph as golden hour: low warm sunset light from the west, long soft shadows, warm "
              "glowing sky at the horizon, slightly richer colors. Keep the framing, perspective, every road, tree, building and "
              "field exactly as they are; do not add, move or remove anything. A thin magenta line is painted on the ground marking "
              "the property boundary; keep it exactly where it is, same color, unbroken, no glow. No text, no watermark.")
    saved = dict(dr.MODELS["grok"]["extra"]); dr.MODELS["grok"]["extra"] = {"resolution": "2k", "aspect_ratio": "auto", "quality": "medium"}
    job = dr.hf_submit("grok", prompt, url); res = dr.hf_wait_all({"relight": job})["relight"]
    dr.MODELS["grok"]["extra"] = saved
    if res.get("status") != "completed":
        raise RuntimeError(f"relight failed: {res.get('error')}")
    return dr.download(res["images"][0]["url"])


# ---------- main ----------
def metadata_debug(path):
    """Which drone/EXIF tags exist in the file (for the UI message when parsing fails)."""
    raw = Path(path).read_bytes()[:4_000_000].decode("latin-1", errors="ignore")
    found = sorted(set(re.findall(r'drone-dji:([A-Za-z]+)', raw)))[:12]
    try:
        exif = Image.open(path).getexif(); model = exif.get(272); gps = 0x8825 in exif
    except Exception:
        model, gps = None, False
    bits = ([f"camera {model}"] if model else []) + (["EXIF GPS"] if gps else []) + ([f"DJI tags: {', '.join(found)}"] if found else [])
    return "; ".join(bits)


def simplify_to_n(ring, n):
    """Visvalingam: drop the vertex with the smallest triangle area until exactly n remain."""
    pts = list(ring)
    while len(pts) > n:
        m = len(pts); best, bi = None, None
        for i in range(m):
            a, b, c = pts[i - 1], pts[i], pts[(i + 1) % m]
            area = abs((b[0] - a[0]) * (c[1] - a[1]) - (c[0] - a[0]) * (b[1] - a[1]))
            if best is None or area < best: best, bi = area, i
        pts.pop(bi)
    return pts


def ground_map_from_clicks(poly, clicks, W, H):
    """Ground map from corners the user clicked (in order around the boundary). The parcel polygon is reduced to the same
    number of corners, matched by cyclic shift/direction, and a homography is fitted. Falls back to a rough bbox map.
    Returns (PlaneMap, note, rough:bool)."""
    import layout as L
    n = len(clicks)
    ring = [(x, y) for x, y, *_ in poly.exterior.coords][:-1]
    best = None
    if n >= 4 and len(ring) >= n:
        simp = simplify_to_n(ring, n)
        for rev in (False, True):
            seq = simp[::-1] if rev else simp
            for k in range(n):
                cand = seq[k:] + seq[:k]
                pm = L.PlaneMap.fit(cand, clicks, W, H)
                if pm and (best is None or pm.error < best.error): best = pm
    if best is not None and best.error < 0.025 * W and _sane(best, ring, clicks):
        return best, f"corner fit {best.error:.1f} px", False
    # rough fallback: parcel bbox → clicked bbox (keeps the editor and the card usable; auto callouts are hidden)
    xs, ys = zip(*ring); cx, cy = zip(*clicks)
    src = [(min(xs), min(ys)), (max(xs), min(ys)), (max(xs), max(ys)), (min(xs), max(ys))]
    dst = [(min(cx), max(cy)), (max(cx), max(cy)), (max(cx), min(cy)), (min(cx), min(cy))]
    pm = L.PlaneMap.fit(src, dst, W, H)
    return pm, "rough bbox map" + (f" (corner fit {best.error:.0f} px rejected)" if best is not None else ""), True


def _sane(pm, ring, clicks):
    """A homography that puts part of the parcel behind the camera, or wildly rescales it, is not a camera."""
    import numpy as np
    from shapely.geometry import Polygon
    ws = []
    for x, y in ring:
        mx, my = L_metric(pm, x, y); v = pm.H @ np.array([mx, my, 1.0]); ws.append(v[2])
    if min(ws) <= 0 or not np.isfinite(pm.H).all(): return False
    mapped = Polygon([pm.to_px(x, y) for x, y in ring]).buffer(0); clicked = Polygon(clicks).buffer(0)
    if mapped.is_empty or clicked.is_empty: return False
    r = mapped.area / clicked.area
    return 0.6 < r < 1.7


def L_metric(pm, lon, lat):
    import layout as L
    return L._metric(lon, lat, pm.origin)


def annotate(photo_path, parcel, scan, pts=None, golden=False, out_dir=None, ref_url=None, meta_path=None):
    """Annotate a real drone photo: writes an editable run folder (clean images + maps.json + layout.json) and draws the outputs
    through pipeline.finalize, so the web editor can adjust everything afterwards."""
    import layout as L
    img = ImageOps.exif_transpose(Image.open(photo_path)).convert("RGBA")
    meta = read_metadata(meta_path or photo_path)
    source = "manual corners"
    if pts is None and meta:
        pts = project_polygon(meta, parcel["polygon"], img.width, img.height); source = "DJI metadata projection"
    if img.width > 1600:
        k = 1600 / img.width; img = img.resize((1600, round(img.height * k)), Image.LANCZOS)
        if pts: pts = [(x * k, y * k) for x, y in pts]
    stem = Path(photo_path).stem.replace("_work", "")
    out_dir = Path(out_dir or f"out/{parcel['apn']}/photos") / stem; out_dir.mkdir(parents=True, exist_ok=True)
    poly = parcel["polygon"]
    # ground map: exact from metadata (pinhole + flat ground = homography), or fitted from the clicked corners
    rough = False
    ring_full = [(x, y) for x, y, *_ in poly.exterior.coords][:-1]
    ring = simplify_to_n(ring_full, 14) if len(ring_full) > 14 else ring_full          # draggable in the editor
    if not pts:                                                                       # nothing to go on: drop the true shape in the middle, user drags the corners
        source = "placed for dragging"
        import layout as L
        xs, ys = zip(*ring); W, H = img.width, img.height
        src = [(min(xs), min(ys)), (max(xs), min(ys)), (max(xs), max(ys)), (min(xs), max(ys))]
        sw, sh = (max(xs) - min(xs)) * 111320 * math.cos(math.radians(poly.centroid.y)), (max(ys) - min(ys)) * 110540
        k = min(0.6 * W / max(sw, 1), 0.5 * H / max(sh, 1)); bw, bh = sw * k, sh * k; cx, cy = W / 2, H * 0.58
        dst = [(cx - bw / 2, cy + bh / 2), (cx + bw / 2, cy + bh / 2), (cx + bw / 2, cy - bh / 2), (cx - bw / 2, cy - bh / 2)]
        pm = L.PlaneMap.fit(src, dst, W, H); pts = [pm.to_px(x, y) for x, y in ring]; fit_note = "not fitted yet"; rough = True
    elif source.startswith("DJI"):
        ll = [(x, y) for x, y, *_ in poly.exterior.coords][:-1]
        pm = L.PlaneMap.fit(ll, pts[:len(ll)], img.width, img.height); fit_note = "metadata"
    else:
        pm, fit_note, rough = ground_map_from_clicks(poly, [tuple(p) for p in pts], img.width, img.height)
        if pm and not rough:                                   # good fit: draw the true parcel shape (every jog), not just the clicked corners
            pts = [pm.to_px(x, y) for x, y in ring]
    layout = L.build_layout(parcel, scan or {"roads": [], "water": [], "buildings": {}, "neighbors": []})
    layout["boundary"] = [[x, y] for x, y in ring]                                       # same vertices as boundary_px (editor drags map 1:1)
    if source.startswith("DJI"): pts = [pm.to_px(x, y) for x, y in ring]
    maps = {"_meta": {"model": "photo", "angles": [], "lights": [], "low": [], "photo": stem}}
    img.convert("RGB").save(out_dir / "photo_original_clean.jpg", quality=94)
    maps["photo_original"] = {"src": "photo_original_clean.jpg", "kind": "photo", "boundary_px": [list(p) for p in pts], "map": pm.to_json() if pm else None,
                              "source": source, "fit": fit_note, "meta": meta, "rough": rough}
    if golden:
        relit = relight_golden(img, pts, ref_url)
        if relit.size != img.size: relit = relit.resize(img.size, Image.LANCZOS)
        det, q = P.detect_fiducial(relit)
        if det:
            clean, _ = P.erase_markers(relit, q.get("_mask"), [], []); relit_pts = det
            if pm:   # the relit image is the same frame: keep the same map; the marker only confirms the line survived
                relit_pts = pts
        else:
            clean, relit_pts = relit, pts
        clean.convert("RGB").save(out_dir / "photo_golden_clean.jpg", quality=94)
        maps["photo_golden"] = {"src": "photo_golden_clean.jpg", "kind": "photo", "boundary_px": [list(p) for p in relit_pts], "map": pm.to_json() if pm else None, "source": source, "rough": rough}
    (out_dir / "parcel.geojson").write_text(json.dumps({"type": "Feature", "geometry": poly.__geo_interface__, "properties": {}}))
    (out_dir / "scan.json").write_text(json.dumps(scan or {}, indent=1, default=str))
    (out_dir / "layout.json").write_text(json.dumps(layout, indent=1))
    P.save_state(out_dir, parcel, maps)
    zp, n = P.finalize(out_dir)
    files = sorted(str(p) for p in out_dir.glob("photo_*_with_lines.jpg")) + ([str(out_dir / "photo_golden_clean.jpg")] if golden else [])
    return {"source": source, "meta": meta, "points": pts, "files": files, "out_dir": str(out_dir), "zip": str(zp), "fit": fit_note, "ground_map": bool(pm) and not rough}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("photo"); ap.add_argument("--county", default="adams-il"); ap.add_argument("--pin")
    ap.add_argument("--golden", action="store_true"); ap.add_argument("--points"); ap.add_argument("--env", default=str(Path(__file__).with_name(".env")))
    a = ap.parse_args()
    ls.load_env(a.env)
    cfg = json.loads((Path(__file__).with_name("counties.json")).read_text())[a.county]
    parcel = P.fetch_parcel_county(cfg, a.pin)
    pts = [tuple(map(float, p.split(","))) for p in a.points.split()] if a.points else None
    r = annotate(a.photo, parcel, None, pts, a.golden)
    print(json.dumps({k: v for k, v in r.items() if k != "points"}, indent=2, default=str))
