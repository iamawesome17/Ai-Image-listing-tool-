"""
layout.py — one layout, many images.

A *layout* is everything drawn on a listing image, stored in ground coordinates (lon/lat) so the same edits apply to
every view: boundary polygon, headline card, feature callouts (POND, DRIVEWAY, ELECTRIC LINES, BUILDING, PASTURE…),
road pins, disclaimer.  layout.json lives in out/<PIN>/ and is what the editor changes.

A *ground map* turns lon/lat into pixels for one image:
  FrameMap   — Google Static Maps frame (exact Web-Mercator math)
  PlaneMap   — homography from a local metric ground plane to pixels; fitted from ≥4 lon/lat↔pixel pairs
               (AI renders via the magenta marker, real drone photos via metadata projection or clicked corners)
  CropMap    — any map followed by a crop/resize

render(img, gmap, layout, parcel) draws the layout on any image in the same house style.
"""
import json, math
import numpy as np, cv2
from PIL import Image, ImageDraw
from shapely.geometry import Point, LineString, Polygon, shape
import listing_shot as ls

FEATURE_STYLE = {"bg": (10, 10, 10, 150), "fg": (255, 255, 255, 255)}
ROAD_STYLE = {"bg": (255, 255, 255, 236), "fg": (20, 20, 20, 255)}


# ---------- ground maps ----------
def _metric(lon, lat, origin):
    kx, ky = 111320 * math.cos(math.radians(origin[1])), 110540
    return ((lon - origin[0]) * kx, (lat - origin[1]) * ky)


def _unmetric(x, y, origin):
    kx, ky = 111320 * math.cos(math.radians(origin[1])), 110540
    return (origin[0] + x / kx, origin[1] + y / ky)


class FrameMap:
    kind = "frame"
    def __init__(self, frame): self.frame = frame; self.W, self.H = frame.W, frame.H
    def to_px(self, lon, lat): return self.frame.px(lon, lat)
    def to_ll(self, x, y):
        f = self.frame; n = 256 * (2 ** f.z)
        wx = f.cx + (x - f.W / 2) / ls.SCALE; wy = f.cy + (y - f.H / 2) / ls.SCALE
        return (wx / n * 360 - 180, math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * wy / n)))))
    def to_json(self): f = self.frame; return {"kind": "frame", "lat": f.lat, "lon": f.lon, "z": f.z, "w": f.w, "h": f.h}


class PlaneMap:
    kind = "plane"
    def __init__(self, H, origin, W, H_px, error=None):
        self.H = np.asarray(H, float); self.Hi = np.linalg.inv(self.H); self.origin = tuple(origin); self.W, self.H_px = W, H_px; self.error = error
        self.Himg = self.H  # alias
    @classmethod
    def fit(cls, ll_pts, px_pts, W, H_px, ransac=0.0):
        origin = (sum(p[0] for p in ll_pts) / len(ll_pts), sum(p[1] for p in ll_pts) / len(ll_pts))
        src = np.array([_metric(x, y, origin) for x, y in ll_pts], np.float32); dst = np.array(px_pts, np.float32)
        if len(src) < 3 or len(src) != len(dst): return None
        try:
            if len(src) == 3:      # triangle lot: a homography needs 4 points, so use the exact affine through the 3 corners
                H = np.vstack([cv2.getAffineTransform(src, dst), [0, 0, 1]]).astype(np.float64)
            else:
                H, mask = cv2.findHomography(src, dst, cv2.RANSAC if ransac else 0, ransac or 3.0)
        except cv2.error:
            return None
        if H is None or abs(np.linalg.det(H)) < 1e-12: return None
        proj = cv2.perspectiveTransform(src.reshape(-1, 1, 2), H).reshape(-1, 2)
        err = float(np.mean(np.linalg.norm(proj - dst, axis=1)))
        return cls(H, origin, W, H_px, err)
    def to_px(self, lon, lat):
        x, y = _metric(lon, lat, self.origin); v = self.H @ np.array([x, y, 1.0])
        return (float(v[0] / v[2]), float(v[1] / v[2]))
    def to_ll(self, x, y):
        v = self.Hi @ np.array([x, y, 1.0]); return _unmetric(v[0] / v[2], v[1] / v[2], self.origin)
    def to_json(self): return {"kind": "plane", "H": self.H.tolist(), "origin": list(self.origin), "W": self.W, "H_px": self.H_px, "error": self.error}


class CropMap:
    kind = "crop"
    def __init__(self, inner, left, top, sx, sy, W, H):
        self.inner, self.left, self.top, self.sx, self.sy, self.W, self.H = inner, left, top, sx, sy, W, H
    def to_px(self, lon, lat):
        x, y = self.inner.to_px(lon, lat); return ((x - self.left) * self.sx, (y - self.top) * self.sy)
    def to_ll(self, x, y): return self.inner.to_ll(x / self.sx + self.left, y / self.sy + self.top)
    def to_json(self): return {"kind": "crop", "inner": self.inner.to_json(), "left": self.left, "top": self.top, "sx": self.sx, "sy": self.sy, "W": self.W, "H": self.H}


def map_from_json(d):
    if d["kind"] == "frame": return FrameMap(ls.Frame(d["lat"], d["lon"], d["z"], d["w"], d["h"]))
    if d["kind"] == "plane": return PlaneMap(d["H"], d["origin"], d["W"], d["H_px"], d.get("error"))
    if d["kind"] == "crop": return CropMap(map_from_json(d["inner"]), d["left"], d["top"], d["sx"], d["sy"], d["W"], d["H"])
    raise ValueError(d["kind"])


# ---------- fitting a render to the ground plane via the detected marker ----------
def _densify(pts, step=6.0, closed=True):
    pts = [tuple(p) for p in pts]
    if closed: pts = pts + [pts[0]]
    out = []
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        n = max(1, int(math.hypot(x1 - x0, y1 - y0) / step))
        out += [(x0 + (x1 - x0) * i / n, y0 + (y1 - y0) * i / n) for i in range(n)]
    return np.array(out, np.float32)


def marker_centerline(mask, step=4.0):
    """Points along the centre of a thick closed marker: midpoints between the outer contour and the nearest inner-hole contour point."""
    cnts, hier = cv2.findContours(mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_NONE)
    if not cnts: return None
    outer_i = max((i for i in range(len(cnts)) if hier[0][i][3] == -1), key=lambda i: cv2.contourArea(cnts[i]))
    holes = [cnts[i] for i in range(len(cnts)) if hier[0][i][3] == outer_i]
    O = cnts[outer_i].reshape(-1, 2).astype(np.float32)[::max(1, int(step))]
    if not holes: return O
    I = max(holes, key=cv2.contourArea).reshape(-1, 2).astype(np.float32)[::max(1, int(step))]
    d2 = ((O[:, None, :] - I[None, :, :]) ** 2).sum(-1); j = d2.argmin(1)
    return (O + I[j]) / 2


def fit_render_map(ref_map, poly, det_pts, W, H, iters=10, mask=None):
    """Ground → render-pixel homography from the polygon drawn on the reference (exact) and the marker detected in the render.
    ICP: start from a bbox-to-bbox affine, repeatedly pair each boundary point with the nearest detected-marker point and refit.
    Returns (PlaneMap, mean_error_px, boundary_px) or (None, err, det_pts) when the fit is poor."""
    ll = [(x, y) for x, y, *_ in poly.exterior.coords][:-1]
    ref_px = np.array([ref_map.to_px(x, y) for x, y in ll], np.float32)
    P = _densify(ref_px, 4.0)
    D = marker_centerline(mask) if mask is not None else None
    if D is None or len(D) < 20: D = _densify(det_pts, 4.0)
    (px0, py0), (px1, py1) = P.min(0), P.max(0); (dx0, dy0), (dx1, dy1) = D.min(0), D.max(0)
    sx, sy = (dx1 - dx0) / max(px1 - px0, 1), (dy1 - dy0) / max(py1 - py0, 1)
    Hcur = np.array([[sx, 0, dx0 - px0 * sx], [0, sy, dy0 - py0 * sy], [0, 0, 1]], np.float64)
    err = 1e9
    for it in range(iters):
        proj = cv2.perspectiveTransform(P.reshape(-1, 1, 2), Hcur).reshape(-1, 2)
        d2 = ((proj[:, None, :] - D[None, :, :]) ** 2).sum(-1); j = d2.argmin(1)
        err = float(np.sqrt(d2[np.arange(len(P)), j]).mean())
        Hn, _ = cv2.findHomography(P, D[j], cv2.RANSAC, 8.0 if it < 4 else 5.0)
        if Hn is None: break
        Hcur = Hn
    proj = cv2.perspectiveTransform(P.reshape(-1, 1, 2), Hcur).reshape(-1, 2)
    d2 = ((proj[:, None, :] - D[None, :, :]) ** 2).sum(-1); err = float(np.sqrt(d2.min(1)).mean())
    # express as ground-plane map: metric local coords → pixels through (frame px → H)
    corners = cv2.perspectiveTransform(ref_px.reshape(-1, 1, 2), Hcur).reshape(-1, 2)
    pm = PlaneMap.fit(ll, [tuple(p) for p in corners], W, H)
    ok = pm is not None and err < 0.02 * W                      # labels usable
    tight = ok and err < 0.009 * W                               # boundary from the fitted polygon (true shape), else the marker as drawn
    return (pm if ok else None), err, ([tuple(map(float, p)) for p in corners] if tight else [tuple(p) for p in det_pts])


# ---------- building the layout from parcel + scan ----------
def build_layout(parcel, scan, ref_map=None, ref_img=None):
    poly = parcel["polygon"]; c = poly.centroid
    acres = f"{parcel['acres']:.2f} ACRES +/-" if parcel.get("acres") else "ACREAGE TBD"
    addr = ", ".join(s for s in [parcel.get("situs", ""), parcel.get("city", ""), f"{parcel.get('state', '')} {parcel.get('zip', '')}".strip()] if s).upper()
    if parcel.get("county"): addr += f"  ·  {parcel['county'].upper()} COUNTY"
    lay = {"boundary": [[round(x, 7), round(y, 7)] for x, y, *_ in poly.simplify(0.000004).exterior.coords],
           "card": {"title": acres, "sub": addr, "lon": c.x, "lat": c.y},
           "labels": [], "disclaimer": ls.DISCLAIMER}
    n = 0
    def add(kind, text, lon, lat, sub=""):
        nonlocal n; n += 1
        lay["labels"].append({"id": f"{kind[0]}{n}", "kind": kind, "text": text, "sub": sub, "lon": round(lon, 7), "lat": round(lat, 7)})
    # roads: pin on the road at the point nearest the parcel (adjacent or within 150 m)
    for r in scan.get("roads", []):
        if r.get("distance_m", 999) > 150 or not r.get("geometry"): continue
        lines = [LineString(l) for l in r["geometry"] if len(l) >= 2]
        if not lines: continue
        near = min((l.interpolate(l.project(c)) for l in lines), key=lambda p: p.distance(poly))
        add("road", r["name"].upper(), near.x, near.y)
    # features from the scan (each carries a label point when the scan found geometry)
    for w in scan.get("water", []):
        if "label_lon" in w and (w.get("crosses_parcel") or w.get("distance_m", 999) < 40):
            kind = "CREEK" if w.get("type") == "stream" else "POND"
            nm = w.get("name", "")
            text = kind if nm.lower().startswith("unnamed") or not nm else f"{kind} · {nm.upper()}" if kind == "CREEK" else kind
            add("feature", text, w["label_lon"], w["label_lat"])
    bpts = scan.get("buildings", {}).get("inside_points", [])[:3]
    if len(bpts) >= 2 and all(math.hypot(*_metric(b[0], b[1], (bpts[0][0], bpts[0][1]))) < 70 for b in bpts[1:]):
        add("feature", "BUILDINGS", sum(b[0] for b in bpts) / len(bpts), sum(b[1] for b in bpts) / len(bpts))
    else:
        for b in bpts: add("feature", "BUILDING", b[0], b[1])
    for p in scan.get("powerline_points", [])[:1]:
        add("feature", "ELECTRIC POLES & LINES", p[0], p[1])
    for p in scan.get("driveway_points", [])[:2]:
        add("feature", "DRIVEWAY", p[0], p[1])
    # land cover callouts from the satellite pixels (only on mixed parcels, only big blobs)
    if ref_map is not None and ref_img is not None:
        for text, lon, lat in landcover_callouts(ref_img, ref_map, poly):
            add("feature", text, lon, lat)
    return lay


def landcover_callouts(sat, gmap, poly, min_share=0.18):
    arr = np.array(sat.convert("RGB")).astype(int)
    mask = Image.new("L", sat.size, 0)
    ImageDraw.Draw(mask).polygon([gmap.to_px(x, y) for x, y, *_ in poly.exterior.coords], fill=255)
    m = np.array(mask) > 0
    if m.sum() < 2000: return []
    hsv = cv2.cvtColor(arr.astype(np.uint8), cv2.COLOR_RGB2HSV)
    v, s = hsv[..., 2].astype(int), hsv[..., 1].astype(int)
    canopy = (v < 125) & (s > 25) & m
    open_green = (v >= 125) & (s > 25) & m & (hsv[..., 0] > 25) & (hsv[..., 0] < 95)
    out = []
    shares = {"TIMBER": canopy.sum() / m.sum(), "PASTURE": open_green.sum() / m.sum()}
    if max(shares.values()) > 0.85: return []                     # single-cover parcel: nothing to call out
    for text, cls in (("TIMBER", canopy), ("PASTURE", open_green)):
        if shares[text] < min_share: continue
        k = np.ones((9, 9), np.uint8)
        cm = cv2.morphologyEx(cls.astype(np.uint8), cv2.MORPH_OPEN, k)
        n, lab, stats, cents = cv2.connectedComponentsWithStats(cm)
        if n < 2: continue
        i = 1 + stats[1:, cv2.CC_STAT_AREA].argmax()
        if stats[i, cv2.CC_STAT_AREA] < min_share * m.sum() * 0.6: continue
        # put the point inside the blob (distance-transform peak), not the centroid
        dt = cv2.distanceTransform((lab == i).astype(np.uint8), cv2.DIST_L2, 5); yy, xx = np.unravel_index(dt.argmax(), dt.shape)
        out.append((text, *gmap.to_ll(float(xx), float(yy))))
    return out


# ---------- drawing ----------
# Every item carries optional fields the editor controls (all backward compatible — missing = old behaviour):
#   style:      {"shadow": bool (soft drop shadows under lines/boxes; default off = crisp), "line_scale": 1.0, "line_visible": True, "fill": True}
#   card:       {..., "scale": 1.0, "visible": True, "off": [dx, dy]}   off = box bottom-centre relative to the pin, in layout units
#   labels[i]:  {..., "scale": 1.0, "visible": True, "off": [dx, dy]}   (1 unit = 1 px on a 1200 px wide image; mode comes from the sign of dy)
#   disc:       {"scale": 1.0, "visible": True, "x": 0.98, "y": 0.97}  x/y = right/bottom edge of the text as a fraction of the image
# PLACEMENT records where everything actually landed on the last render() call, in that image's pixels (the editor shows exactly that).
PLACEMENT = {}
DEFAULT_STYLE = {"shadow": False, "line_scale": 1.0, "line_visible": True, "fill": True}


def _s(img, base=1200.0): return max(0.6, min(2.2, img.width / base))


def style_of(layout): return {**DEFAULT_STYLE, **(layout.get("style") or {})}


def _crisp(img, pts, width, closed=True, shadow=False):
    """White line with a thin dark edge (no blur). With shadow=True a soft drop shadow is added underneath as well."""
    q = list(pts) + ([pts[0]] if closed else [])
    def fn(d, sh, off):
        d.line([(x + off[0], y + off[1]) for x, y in q], fill=sh or (255, 255, 255, 255), width=width, joint="curve")
    if shadow: ls.shadowed(img, fn, blur=4, offset=(0, 2), alpha=170)
    d = ImageDraw.Draw(img)
    d.line(q, fill=(0, 0, 0, 120), width=width + 2, joint="curve")
    d.line(q, fill=(255, 255, 255, 255), width=width, joint="curve")


def draw_card(img, gmap, layout, parcel, scale):
    card = layout["card"]; cs = float(card.get("scale", 1.0) or 1.0); k = scale * cs
    cx, cy = gmap.to_px(card["lon"], card["lat"])
    lines, sizes, pad = [card["title"], card["sub"]], [int(58 * k), int(24 * k)], (int(22 * k), int(14 * k))
    (bx0, by0, bx1, by1), _, _ = ls.measure_box(img, (cx, 0), lines, sizes, pad, "top"); box_h = by1 - by0; box_w = bx1 - bx0
    pin_r = int(24 * k)
    if card.get("off"):
        bx, by = cx + card["off"][0] * scale, cy + card["off"][1] * scale                     # box bottom-centre, user placed
    else:
        cxc = min(max(cx, 0.12 * img.width), 0.88 * img.width); cyc = min(max(cy, 0.2 * img.height), 0.95 * img.height)
        top_need = 2.6 * pin_r + box_h + 16 * k
        by = max(0.30 * img.height, top_need, cyc - min(260 * k, img.height * 0.22)) - 10 * k
        bx = min(max(cxc, box_w / 2 + 6), img.width - box_w / 2 - 6)
    bx = min(max(bx, box_w / 2 + 2), img.width - box_w / 2 - 2); by = min(max(by, box_h + 2.6 * pin_r), img.height - 2)
    ls.draw_pole(img, (bx, by + 10 * k), (cx, cy), width=max(3, int(5 * k)))
    box = ls.text_box(img, (bx, by), lines, sizes, pad=pad, radius=int(14 * k), anchor="bottom")
    ls.draw_pin(img, (bx, box[1] - 6), r=pin_r)
    PLACEMENT["card"] = {"x": cx, "y": cy, "bx": bx, "by": by, "box": list(box), "scale": cs}


def _place(img, xy, lines, sizes, pad, scale, lead0=58):
    """Pick a label position near the anchor that is on-frame and clear of other cards.
    Returns (box_xy_center_x, y_edge, anchor_mode, box) where anchor_mode is 'bottom' (box above the point) or 'top' (below)."""
    x, y = xy
    cands = []
    for lead in (lead0, lead0 + 46, lead0 + 92):
        cands.append((0, -lead * scale, "bottom"))
    for lead in (lead0 - 10, lead0 + 36):
        cands.append((0, lead * scale, "top"))
    for dx in (-130, 130, -220, 220):
        for lead in (lead0, lead0 + 50):
            cands.append((dx * scale, -lead * scale, "bottom")); cands.append((dx * scale, lead * scale, "top"))
    best = None
    for dx, dy, mode in cands:
        bx, by = x + dx, y + dy
        box, _, _ = ls.measure_box(img, (bx, by), lines, sizes, pad, mode)
        sdx = min(0, img.width - 8 - box[2]) + max(0, 8 - box[0]); box = (box[0] + sdx, box[1], box[2] + sdx, box[3]); bx += sdx
        onframe = box[1] >= 6 and box[3] <= img.height - 60
        if onframe and not ls.overlaps(box, ls.PLACED):
            return bx, by, mode, box
        if best is None and onframe: best = (bx, by, mode, box)
    return best or (x, y - lead0 * scale, "bottom", ls.measure_box(img, (x, y - lead0 * scale), lines, sizes, pad, "bottom")[0])


def _explicit(img, xy, off, scale, lines, sizes, pad):
    """Box position from a stored offset: off = (dx, dy) in layout units from the pin to the box edge nearest the pin."""
    x, y = xy; bx, by = x + off[0] * scale, y + off[1] * scale
    mode = "bottom" if off[1] < 0 else "top"
    box, _, _ = ls.measure_box(img, (bx, by), lines, sizes, pad, mode)
    sdx = min(0, img.width - 4 - box[2]) + max(0, 4 - box[0]); sdy = min(0, img.height - 4 - box[3]) + max(0, 4 - box[1])
    return bx + sdx, by + sdy, mode, (box[0] + sdx, box[1] + sdy, box[2] + sdx, box[3] + sdy)


def draw_feature(img, xy, lb, scale):
    """Black semi-transparent callout: dot on the feature, leader to a dark rounded label."""
    x, y = xy; k = scale * float(lb.get("scale", 1.0) or 1.0); text, sub = lb["text"], lb.get("sub", "")
    lines = [text] + ([sub] if sub else []); sizes = [int(26 * k)] + ([int(19 * k)] if sub else [])
    pad = (int(14 * k), int(8 * k))
    bx, by, mode, box = _explicit(img, xy, lb["off"], scale, lines, sizes, pad) if lb.get("off") else _place(img, xy, lines, sizes, pad, k)
    end = (bx, box[3] + 2) if mode == "bottom" else (bx, box[1] - 2)
    def fn(d, shadow, off):
        d.ellipse((x - 6 * k + off[0], y - 6 * k + off[1], x + 6 * k + off[0], y + 6 * k + off[1]), fill=shadow or (255, 255, 255, 255))
        d.line([(x + off[0], y + off[1]), (end[0] + off[0], end[1] + off[1])], fill=shadow or (255, 255, 255, 235), width=max(2, int(3 * k)))
    ls.shadowed(img, fn, blur=3, offset=(0, 2), alpha=150)
    ls.text_box(img, (bx, by), lines, sizes, pad=pad, radius=int(9 * k), anchor=mode, bg=FEATURE_STYLE["bg"], fg=FEATURE_STYLE["fg"])
    PLACEMENT[lb.get("id", text)] = {"x": x, "y": y, "bx": bx, "by": by, "mode": mode, "box": list(box)}


def draw_road(img, xy, lb, scale):
    """Unobtrusive road marker: small pin on the road, light label near it."""
    x, y = xy; k = scale * float(lb.get("scale", 1.0) or 1.0); text = lb["text"]
    lines, sizes, pad = [text], [int(24 * k)], (int(14 * k), int(8 * k))
    bx, by, mode, box = _explicit(img, xy, lb["off"], scale, lines, sizes, pad) if lb.get("off") else _place(img, xy, lines, sizes, pad, k, lead0=46)
    ls.draw_pin(img, (x, y), r=int(14 * k))
    if mode == "top" or abs(bx - x) > 20 * k:                       # label not directly above the pin: thin leader
        end = (bx, box[3] + 2) if mode == "bottom" else (bx, box[1] - 2)
        ImageDraw.Draw(img).line([(x, y - 14 * k), end], fill=(255, 255, 255, 220), width=max(2, int(2 * k)))
    ls.text_box(img, (bx, by), lines, sizes, pad=pad, radius=int(9 * k), anchor=mode, bg=ROAD_STYLE["bg"], fg=ROAD_STYLE["fg"])
    PLACEMENT[lb.get("id", text)] = {"x": x, "y": y, "bx": bx, "by": by, "mode": mode, "box": list(box)}


def disc_defaults(img, scale, attribution):
    """Default disclaimer anchor (right edge, bottom edge) as fractions of the image."""
    bottom = 70 if attribution else int(26 * scale)
    return {"x": 1 - 28 / img.width, "y": 1 - bottom / img.height}


def draw_disclaimer(img, layout, scale, attribution):
    disc = {**{"scale": 1.0, "visible": True}, **(layout.get("disc") or {})}
    text = layout.get("disclaimer", ls.DISCLAIMER)
    size = int(24 * scale * float(disc.get("scale", 1.0) or 1.0))
    dflt = disc_defaults(img, scale, attribution); fx, fy = disc.get("x", dflt["x"]), disc.get("y", dflt["y"])
    d = ImageDraw.Draw(img); f = ls.font(size); w = d.textlength(text, font=f); h = f.getbbox("Ag")[3]
    x, y = fx * img.width - w, fy * img.height - h
    x = min(max(x, 4), img.width - w - 4); y = min(max(y, 4), img.height - h - 4)
    if disc.get("visible", True) and text:
        for dx, dy in [(-2, -2), (2, -2), (-2, 2), (2, 2), (0, 3)]:
            d.text((x + dx, y + dy), text, font=f, fill=(0, 0, 0, 200))
        d.text((x, y), text, font=f, fill=(255, 255, 255, 255))
    PLACEMENT["disc"] = {"x": fx, "y": fy, "box": [x, y, x + w, y + h], "size": size, "visible": disc.get("visible", True), "scale": disc.get("scale", 1.0)}


def render(img, gmap, layout, parcel, pois=(), boundary_px=None, show_card=True, show_labels=True, attribution=False):
    """Draw the full layout on an RGBA image using its ground map. boundary_px overrides the mapped polygon (e.g. detected marker)."""
    out = img.copy(); scale = _s(out); ls.PLACED.clear(); PLACEMENT.clear()
    st = style_of(layout); ls.SHADOWS = bool(st.get("shadow"))
    try:
        pts = boundary_px or [gmap.to_px(x, y) for x, y in layout["boundary"]]
        PLACEMENT["boundary"] = {"px": [list(p) for p in pts], "visible": bool(st.get("line_visible", True)), "line_scale": st.get("line_scale", 1.0)}
        if st.get("line_visible", True) and len(pts) >= 2:
            if st.get("fill", True) and len(pts) >= 3:
                fill = Image.new("RGBA", out.size, (0, 0, 0, 0)); ImageDraw.Draw(fill).polygon(pts, fill=(255, 255, 255, 34)); out.alpha_composite(fill)
            _crisp(out, pts, max(2, int(round(5 * scale * float(st.get("line_scale", 1.0) or 1.0)))), closed=len(pts) >= 3, shadow=ls.SHADOWS)
        if show_card and layout["card"].get("visible", True): draw_card(out, gmap, layout, parcel, scale)
        if len(pts) >= 2:
            xs, ys = zip(*pts); ls.PLACED.append((min(xs), min(ys), max(xs), max(ys))) if not show_labels else None
        if show_labels:
            inside = lambda x, y, pad: pad <= x <= out.width - pad and pad <= y <= out.height - pad
            for lb in sorted(layout["labels"], key=lambda l: (l["kind"] != "feature", l["text"])):
                if not lb.get("visible", True): continue
                x, y = gmap.to_px(lb["lon"], lb["lat"])
                if not inside(x, y, 24): continue
                if lb["kind"] == "feature": draw_feature(out, (x, y), lb, scale)
                elif lb["kind"] == "road": draw_road(out, (x, y), lb, scale)
            for p in pois:
                x, y = gmap.to_px(p["lon"], p["lat"])
                if inside(x, y, 140) and not Polygon(layout["boundary"]).contains(Point(p["lon"], p["lat"])):
                    ls.draw_poi(out, (x, y), p["name"], p["type"])
        draw_disclaimer(out, layout, scale, attribution)
    finally:
        ls.SHADOWS = True
    return out


def layout_px(layout, gmap, placement=None, scale=1.0):
    """Layout expressed in this image's pixels (for the editor). With `placement` (from a render() dry run) every box carries
    the exact pixel position it was drawn at, so the editor shows the output, not an approximation."""
    pl = placement or {}
    card = layout["card"]; cpx = gmap.to_px(card["lon"], card["lat"]); cpl = pl.get("card", {})
    labs = []
    for l in layout["labels"]:
        x, y = gmap.to_px(l["lon"], l["lat"]); p = pl.get(l.get("id"), {})
        labs.append({**l, "x": x, "y": y, "bx": p.get("bx", x), "by": p.get("by", y - 58 * scale), "mode": p.get("mode", "bottom"), "scale": l.get("scale", 1.0), "visible": l.get("visible", True)})
    disc = {**{"scale": 1.0, "visible": True}, **(layout.get("disc") or {}), **{k: v for k, v in pl.get("disc", {}).items() if k in ("x", "y", "box", "size")}}
    return {"boundary": [list(gmap.to_px(x, y)) for x, y in layout["boundary"]],
            "card": {**card, "x": cpx[0], "y": cpx[1], "bx": cpl.get("bx", cpx[0]), "by": cpl.get("by", cpx[1] - 200 * scale), "scale": card.get("scale", 1.0), "visible": card.get("visible", True)},
            "labels": labs, "disclaimer": layout.get("disclaimer", ls.DISCLAIMER), "disc": disc, "style": style_of(layout), "scale": scale}


def apply_px_edits(layout, edits, gmap, shown=None, tol=1.5, scale=1.0):
    """Fold pixel-space edits from the editor back into the ground layout.
    edits = {boundary:[[x,y]..] | None, card:{title,sub,x,y,bx,by,scale,visible}, labels:[{id,kind,text,sub,x,y,bx,by,scale,visible}],
             disclaimer, disc:{x,y,scale,visible}, style:{shadow,line_scale,line_visible,fill}}
    shown: the pixel layout the editor was given (layout_px); items the user did not move keep their original ground position.
    Box positions (bx, by) are stored as offsets from the pin in layout units so they carry across images."""
    new = json.loads(json.dumps(layout))
    shown_lab = {l["id"]: (l["x"], l["y"]) for l in (shown or {}).get("labels", [])}
    unmoved = lambda a, b: a is not None and math.hypot(a[0] - b[0], a[1] - b[1]) <= tol
    off_of = lambda d: [round((d["bx"] - d["x"]) / scale, 1), round((d["by"] - d["y"]) / scale, 1)] if "bx" in d and "by" in d else None
    if edits.get("boundary"):
        new["boundary"] = [list(gmap.to_ll(x, y)) for x, y in edits["boundary"]]
    if edits.get("card"):
        cd = edits["card"]
        if shown and unmoved((shown["card"]["x"], shown["card"]["y"]), (cd["x"], cd["y"])): lon, lat = layout["card"]["lon"], layout["card"]["lat"]
        else: lon, lat = gmap.to_ll(cd["x"], cd["y"])
        new["card"] = {"title": cd.get("title", layout["card"]["title"]), "sub": cd.get("sub", layout["card"]["sub"]), "lon": lon, "lat": lat,
                       "scale": float(cd.get("scale", layout["card"].get("scale", 1.0)) or 1.0), "visible": bool(cd.get("visible", True))}
        off = off_of(cd)
        if off: new["card"]["off"] = off
    if "labels" in edits:
        orig = {l["id"]: l for l in layout["labels"]}; labs = []
        for l in edits["labels"]:
            lid = l.get("id") or f"u{len(labs)}"
            if lid in orig and unmoved(shown_lab.get(lid), (l["x"], l["y"])): lon, lat = orig[lid]["lon"], orig[lid]["lat"]
            else: lon, lat = gmap.to_ll(l["x"], l["y"])
            rec = {"id": lid, "kind": l.get("kind", "feature"), "text": l.get("text", ""), "sub": l.get("sub", ""), "lon": lon, "lat": lat,
                   "scale": float(l.get("scale", 1.0) or 1.0), "visible": bool(l.get("visible", True))}
            off = off_of(l)
            if off: rec["off"] = off
            labs.append(rec)
        new["labels"] = labs
    if "disclaimer" in edits: new["disclaimer"] = edits["disclaimer"]
    if edits.get("disc"):
        dc = edits["disc"]; new["disc"] = {**(layout.get("disc") or {}), **{k: dc[k] for k in ("x", "y", "scale", "visible") if k in dc}}
    if edits.get("style"):
        new["style"] = {**style_of(layout), **{k: edits["style"][k] for k in DEFAULT_STYLE if k in edits["style"]}}
    return new


class RotMap:
    """A square north-up frame rotated by `angle` (PIL rotate, CCW, about the centre), then centre-cropped to cw×ch and resized
    to out_size — exactly what pipeline.rotated_reference does. Used only to initialise the render fit."""
    kind = "rot"
    def __init__(self, frame, angle, out_size=(1280, 960)):
        self.inner = FrameMap(frame); self.angle = angle; a = math.radians(angle)
        W = frame.W; side = int(W / (abs(math.cos(math.radians(angle + 180))) + abs(math.sin(math.radians(angle + 180)))))
        self.cw, self.ch = side, int(side * 0.75); self.cx = self.cy = W / 2
        self.kx, self.ky = out_size[0] / self.cw, out_size[1] / self.ch; self.W, self.H = out_size
    def to_px(self, lon, lat):
        x, y = self.inner.to_px(lon, lat); dx, dy = x - self.cx, y - self.cy; c, s = math.cos(math.radians(self.angle)), math.sin(math.radians(self.angle))
        rx, ry = self.cx + dx * c + dy * s, self.cy - dx * s + dy * c            # PIL rotate = CCW on screen
        return ((rx - (self.cx - self.cw / 2)) * self.kx, (ry - (self.cy - self.ch / 2)) * self.ky)
