"""
drone_photo.py — annotate (and optionally golden-hour relight) a REAL drone photo.

  python drone_photo.py photo.jpg --county adams-il --pin 20-0-0636-002-00 [--golden] [--points "x,y x,y x,y ..."]

Property line placement, in order of preference:
  1. DJI metadata in the photo (GPS, relative altitude, gimbal yaw/pitch, focal length) → the county parcel polygon is
     projected onto the photo automatically (flat-ground pinhole model; good to a few metres at typical altitudes).
  2. --points: pixel corners clicked by the user in the web UI.

Relight (natural, default): the image model re-lights the clean photo, then light_transfer() keeps every real pixel and borrows
only the smoothed light/colour change — golden hour without the AI look. 'dramatic' = the older full AI relight with a magenta
marker that is detected, erased and redrawn as the styled outline.
Magic eraser: magic_erase() removes text/logos already on a photo; only the changed spots are pasted back onto the original.
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
OUT_W = 2400        # drone photo output width (was 1600): sharper lines and room to crop for listings

PROMPT_NATURAL = ("Re-light this exact aerial photograph as a believable late-afternoon golden hour, like an unedited photo a real "
                  "drone pilot took about 45 minutes before sunset: warm low sunlight, gently warmer tones, soft natural shadows, "
                  "clear sky with only a faint warm tint near the horizon. Subtle and realistic: no dramatic orange or pink sky, "
                  "no sun disc, no lens flare, no glow, no haze, no HDR look, no oversaturation. Keep the framing, perspective, "
                  "every road, tree, building and field exactly as they are; do not add, move or remove anything. No text, no watermark.")
PROMPT_DRAMATIC = ("Re-light this exact aerial photograph as golden hour: low warm sunset light from the west, long soft shadows, warm "
                   "glowing sky at the horizon, slightly richer colors. Keep the framing, perspective, every road, tree, building and "
                   "field exactly as they are; do not add, move or remove anything. A thin magenta line is painted on the ground marking "
                   "the property boundary; keep it exactly where it is, same color, unbroken, no glow. No text, no watermark.")
PROMPT_ERASE = ("Remove every piece of text, caption, number, watermark, logo, arrow, outline, pin, sticker or other graphic that was "
                "added on top of this aerial photograph, and fill each of those spots with what is naturally underneath (ground, grass, "
                "trees, sky, road, water). Change nothing else at all: same framing, same lighting, same colors and the same fine detail "
                "everywhere else. Do not add anything. No text, no watermark.")


def grok_edit(img, prompt):
    """One image edit on the Higgsfield grok model at 2k, original aspect ratio. Returns an RGB image the size of img."""
    url = dr.hf_upload_png(img.convert("RGB"))
    saved = dict(dr.MODELS["grok"]["extra"]); dr.MODELS["grok"]["extra"] = {"resolution": "2k", "aspect_ratio": "auto", "quality": "medium"}
    try:
        job = dr.hf_submit("grok", prompt, url); res = dr.hf_wait_all({"edit": job})["edit"]
    finally:
        dr.MODELS["grok"]["extra"] = saved
    if res.get("status") != "completed":
        raise RuntimeError(f"image edit failed: {res.get('error')}")
    out = dr.download(res["images"][0]["url"]).convert("RGB")
    return out.resize(img.size, Image.LANCZOS) if out.size != img.size else out


def align_to(ref, img):
    """Warp img onto ref (the model sometimes nudges the framing a few pixels). Falls back to img unchanged."""
    try:
        a = np.array(ref.convert("L")); b = np.array(img.convert("L"))
        k = 1024 / max(a.shape); sa = cv2.resize(a, None, fx=k, fy=k); sb = cv2.resize(b, None, fx=k, fy=k)
        orb = cv2.ORB_create(4000); ka, da = orb.detectAndCompute(sa, None); kb, db = orb.detectAndCompute(sb, None)
        if da is None or db is None: return img
        m = sorted(cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True).match(db, da), key=lambda x: x.distance)[:600]
        if len(m) < 40: return img
        src = np.float32([kb[x.queryIdx].pt for x in m]) / k; dst = np.float32([ka[x.trainIdx].pt for x in m]) / k
        H, inl = cv2.findHomography(src, dst, cv2.RANSAC, 4.0)
        if H is None or inl.sum() < 30: return img
        if np.abs(H[:2, :2] - np.eye(2)).max() > 0.08 or np.abs(H[:2, 2]).max() > 0.06 * max(a.shape): return img   # not a small nudge: leave it
        w = cv2.warpPerspective(np.array(img.convert("RGB")), H, (a.shape[1], a.shape[0]), flags=cv2.INTER_LANCZOS4, borderMode=cv2.BORDER_REFLECT)
        return Image.fromarray(w)
    except Exception:
        return img


def _guided(I, p, r, eps):
    """Edge-aware smoothing of p guided by I (He et al. guided filter, box-filter form)."""
    box = lambda x: cv2.boxFilter(x, -1, (2 * r + 1, 2 * r + 1))
    mI, mp = box(I), box(p); a = (box(I * p) - mI * mp) / (box(I * I) - mI * mI + eps); b = mp - a * mI
    return box(a) * I + box(b)


def light_transfer(orig, ai, strength=0.85):
    """Keep every real pixel of the photo and borrow only the LIGHT from the AI relight: the per-pixel colour/brightness change
    (AI − original, in Lab) is smoothed edge-aware so the model's invented texture, smearing and over-sharpening are dropped,
    then applied to the original at `strength`. Result: real detail, golden-hour light, no 'AI look'."""
    ai = align_to(orig, ai)
    o = cv2.cvtColor(np.array(orig.convert("RGB")), cv2.COLOR_RGB2LAB).astype(np.float32)
    a = cv2.cvtColor(np.array(ai.convert("RGB")), cv2.COLOR_RGB2LAB).astype(np.float32)
    H, W = o.shape[:2]; k = min(1.0, 1200 / W)                                       # work at ≤1200 px wide, upsample the smooth field
    so = cv2.resize(o, None, fx=k, fy=k, interpolation=cv2.INTER_AREA); sa = cv2.resize(a, None, fx=k, fy=k, interpolation=cv2.INTER_AREA)
    guide = so[..., 0] / 255.0; r = max(4, int(so.shape[1] / 60))
    delta = np.dstack([_guided(guide, (sa[..., c] - so[..., c]) / 255.0, r, 2e-3) * 255.0 for c in range(3)])
    delta = cv2.resize(delta, (W, H), interpolation=cv2.INTER_CUBIC)
    # safety rails: even if the model overdoes it, the photo can only move so far toward warm (Lab units, 255 scale)
    delta[..., 0] = np.clip(delta[..., 0], -16, 12) * strength                       # brightness
    delta[..., 1] = np.clip(delta[..., 1], -6, 8) * strength                         # green ↔ red/magenta
    delta[..., 2] = np.clip(delta[..., 2], -8, 14) * strength                        # blue ↔ yellow (the 'golden')
    out = np.clip(o + delta, 0, 255).astype(np.uint8)
    return Image.fromarray(cv2.cvtColor(out, cv2.COLOR_LAB2RGB))


def relight_golden(img, pts, ref_url=None, style="natural"):
    """Golden-hour version of the photo.
    natural (default): AI relight of the clean photo, then light_transfer → original detail, believable warm light.
    dramatic: the previous full AI relight with the boundary marker (kept for anyone who wants the big sunset)."""
    if style != "dramatic":
        base = img.convert("RGB")
        return light_transfer(base, grok_edit(base, PROMPT_NATURAL)).convert("RGBA")
    marked = img.copy(); d = ImageDraw.Draw(marked)
    d.line(pts + [pts[0]], fill=P.FIDUCIAL + (255,), width=max(4, img.width // 300), joint="curve")
    if ref_url:
        url = ref_url
        saved = dict(dr.MODELS["grok"]["extra"]); dr.MODELS["grok"]["extra"] = {"resolution": "2k", "aspect_ratio": "auto", "quality": "medium"}
        try:
            job = dr.hf_submit("grok", PROMPT_DRAMATIC, url); res = dr.hf_wait_all({"relight": job})["relight"]
        finally:
            dr.MODELS["grok"]["extra"] = saved
        if res.get("status") != "completed": raise RuntimeError(f"relight failed: {res.get('error')}")
        return dr.download(res["images"][0]["url"])
    return grok_edit(marked, PROMPT_DRAMATIC).convert("RGBA")


# ---------- magic eraser ----------
def erase_mask(orig, ai):
    """Where the AI actually removed something. Added text/logos change the LOCAL AVERAGE colour a lot; the model re-drawing tree
    and grass texture does not — so compare blurred images, with a threshold set from the image's own background noise.
    Returns a 0..1 float mask, feathered."""
    o = cv2.cvtColor(np.array(orig.convert("RGB")), cv2.COLOR_RGB2LAB).astype(np.float32)
    a = cv2.cvtColor(np.array(ai.convert("RGB")), cv2.COLOR_RGB2LAB).astype(np.float32)
    a = (a - a.mean((0, 1))) / (a.std((0, 1)) + 1e-6) * o.std((0, 1)) + o.mean((0, 1))   # cancel any global tone shift
    H, W = o.shape[:2]; s = max(1.0, W / 1600)
    d = np.abs(cv2.GaussianBlur(a, (0, 0), 3.0 * s) - cv2.GaussianBlur(o, (0, 0), 3.0 * s))
    diff = np.maximum(d[..., 0], 0.9 * np.maximum(d[..., 1], d[..., 2]))
    med = float(np.median(diff)); mad = float(np.median(np.abs(diff - med))) + 1e-3
    m = (diff > max(12.0, med + 8 * 1.4826 * mad)).astype(np.uint8)
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (int(5 * s) | 1,) * 2))
    n, lab, st, _ = cv2.connectedComponentsWithStats(m)
    keep = np.zeros_like(m)
    for i in range(1, n):
        if st[i, cv2.CC_STAT_AREA] >= 120 * s * s: keep[lab == i] = 1               # drop small blobs from re-drawn texture
    keep = cv2.dilate(keep, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (int(11 * s) | 1,) * 2))
    return np.clip(cv2.GaussianBlur(keep.astype(np.float32), (0, 0), 2.5 * s), 0, 1)


def magic_erase(src_path, out_path, max_w=OUT_W):
    """Remove text / logos / graphics someone already put on a photo. The AI paints the cleaned version; only the spots it changed
    are copied back onto the original, so everything else stays pixel-identical. Returns (out_path, share of image changed)."""
    img = ImageOps.exif_transpose(Image.open(src_path)).convert("RGB")
    if img.width > max_w: img = img.resize((max_w, round(img.height * max_w / img.width)), Image.LANCZOS)
    ai = align_to(img, grok_edit(img, PROMPT_ERASE))
    m = erase_mask(img, ai)[..., None]
    out = (np.array(img, np.float32) * (1 - m) + np.array(ai, np.float32) * m).clip(0, 255).astype(np.uint8)
    Image.fromarray(out).save(out_path, quality=95)
    return str(out_path), float(m.mean())


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


def annotate(photo_path, parcel, scan, pts=None, golden=False, out_dir=None, ref_url=None, meta_path=None, pts_size=None):
    """Annotate a real drone photo: writes an editable run folder (clean images + maps.json + layout.json) and draws the outputs
    through pipeline.finalize, so the web editor can adjust everything afterwards."""
    import layout as L
    img = ImageOps.exif_transpose(Image.open(photo_path)).convert("RGBA")
    meta = read_metadata(meta_path or photo_path)
    source = "manual corners"
    if pts and pts_size and pts_size[0] and pts_size[0] != img.width:                  # corners clicked on the smaller preview copy
        k = img.width / pts_size[0]; pts = [(x * k, y * k) for x, y in pts]
    if pts is None and meta:
        pts = project_polygon(meta, parcel["polygon"], img.width, img.height); source = "DJI metadata projection"
    if img.width > OUT_W:
        k = OUT_W / img.width; img = img.resize((OUT_W, round(img.height * k)), Image.LANCZOS)
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
        style = golden if isinstance(golden, str) else "natural"
        relit = relight_golden(img, pts, ref_url, style)
        if relit.size != img.size: relit = relit.resize(img.size, Image.LANCZOS)
        det, q = P.detect_fiducial(relit) if style == "dramatic" else (None, None)    # natural: same pixels as the original, no marker
        if det:
            clean, _ = P.erase_markers(relit, q.get("_mask"), [], [], dilate=3, radius=3); relit_pts = det   # thin marker: repaint only its own pixels (no smear band)
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
