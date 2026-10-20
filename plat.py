"""
plat.py — survey-style plat exhibit drawn from the GIS parcel polygon.

Bearings and distances are computed from the parcel geometry (local plane), so they match the county polygon to the
rounding shown; adjoining parcels (PINs), nearby roads, section/township/range (BLM PLSS), north arrow, scale bar and a
title block complete the sheet. It is an exhibit, not a boundary survey — the footer says so.
"""
import json, math, datetime, urllib.parse
from pathlib import Path
from PIL import Image, ImageDraw
from shapely.geometry import LineString, Polygon
import listing_shot as ls

PLSS = "https://gis.blm.gov/arcgis/rest/services/Cadastral/BLM_Natl_PLSS_CadNSDI/MapServer"
FT = 3.28084


def plss_lookup(lon, lat):
    """Section / Township / Range at a point from the BLM PLSS service (fails soft → {})."""
    try:
        q = urllib.parse.urlencode({"geometry": f"{lon},{lat}", "geometryType": "esriGeometryPoint", "inSR": 4326, "spatialRel": "esriSpatialRelIntersects",
                                    "returnGeometry": "false", "f": "json", "outFields": "*"})
        t = ls.http_json(f"{PLSS}/1/query?{q}", timeout=20).get("features", [])
        s = ls.http_json(f"{PLSS}/2/query?{q}", timeout=20).get("features", [])
        if not t: return {}
        a = t[0]["attributes"]; b = s[0]["attributes"] if s else {}
        twp = f"T{int(a['TWNSHPNO'])}{a['TWNSHPDIR']}"; rng = f"R{int(a['RANGENO'])}{a['RANGEDIR']}"
        return {"section": b.get("FRSTDIVNO"), "township": twp, "range": rng, "meridian": a.get("PRINMER"),
                "text": (f"Section {int(b['FRSTDIVNO'])}, " if b.get("FRSTDIVNO") else "") + f"{twp}, {rng}, {a.get('PRINMER', '')}".strip(", ")}
    except Exception:
        return {}


def bearing_text(dx, dy):
    """Quadrant bearing like N 45°12'30\" E from a metric vector."""
    ang = math.degrees(math.atan2(abs(dx), abs(dy)))
    d = int(ang); m = int((ang - d) * 60); sec = int(round(((ang - d) * 60 - m) * 60))
    if sec == 60: sec = 0; m += 1
    if m == 60: m = 0; d += 1
    ns = "N" if dy >= 0 else "S"; ew = "E" if dx >= 0 else "W"
    if abs(dx) < 1e-9: return f"{ns} 00°00'00\" E"
    if abs(dy) < 1e-9: return f"N 90°00'00\" {ew}"
    return f"{ns} {d:02d}°{m:02d}'{sec:02d}\" {ew}"


def _rot_text(img, xy, text, angle, size, fill=(0, 0, 0, 255), bold=False):
    f = ls.font(size, bold); d0 = ImageDraw.Draw(img); w = int(d0.textlength(text, font=f)) + 8; h = size + 8
    tmp = Image.new("RGBA", (w, h), (255, 255, 255, 0)); ImageDraw.Draw(tmp).text((4, 2), text, font=f, fill=fill)
    tmp = tmp.rotate(angle, expand=True, resample=Image.BICUBIC)
    img.alpha_composite(tmp, (int(xy[0] - tmp.width / 2), int(xy[1] - tmp.height / 2)))


def draw_plat(parcel, scan, layout, path, size=(1600, 1200)):
    poly = Polygon(layout["boundary"]) if layout and layout.get("boundary") else parcel["polygon"]
    c = poly.centroid; kx, ky = 111320 * math.cos(math.radians(c.y)), 110540
    m = lambda lon, lat: ((lon - c.x) * kx, (lat - c.y) * ky)                   # metres, east/north
    W, H = size; img = Image.new("RGBA", size, (255, 255, 255, 255)); d = ImageDraw.Draw(img)
    # sheet frame
    d.rectangle((30, 30, W - 30, H - 30), outline=(0, 0, 0, 255), width=3); d.rectangle((40, 40, W - 40, H - 40), outline=(0, 0, 0, 255), width=1)
    # drawing area (left ~70%) and title block (right)
    tb_x = W - 470
    d.line([(tb_x, 40), (tb_x, H - 40)], fill=(0, 0, 0, 255), width=2)
    ax0, ay0, ax1, ay1 = 70, 70, tb_x - 30, H - 150
    # scale: fit parcel (+ margin for labels) into the drawing area
    pts_m = [m(x, y) for x, y in poly.exterior.coords]
    xs, ys = zip(*pts_m); span_x, span_y = max(xs) - min(xs), max(ys) - min(ys)
    pad = 0.30
    scale = min((ax1 - ax0) / (span_x * (1 + 2 * pad)), (ay1 - ay0) / (span_y * (1 + 2 * pad)))      # px per metre
    ox, oy = (ax0 + ax1) / 2 - (max(xs) + min(xs)) / 2 * scale, (ay0 + ay1) / 2 + (max(ys) + min(ys)) / 2 * scale
    P = lambda lon, lat: (lambda e, n: (ox + e * scale, oy - n * scale))(*m(lon, lat))
    clip = Polygon([(ax0, ay0), (ax1, ay0), (ax1, ay1), (ax0, ay1)])
    # adjoining parcels (light) with PINs
    for nb in scan.get("neighbors", []):
        for ring in nb.get("geometry", []):
            pp = [P(x, y) for x, y in ring]
            if len(pp) < 3: continue
            g = Polygon(pp).intersection(clip)
            if g.is_empty: continue
            for part in ([g] if g.geom_type == "Polygon" else list(getattr(g, "geoms", []))):
                if part.geom_type != "Polygon": continue
                d.polygon(list(part.exterior.coords), outline=(150, 150, 150, 255), width=1)
                lp = part.representative_point()
                if nb.get("pin") and part.area > 9000:
                    f = ls.font(16, False); t = f"PIN {nb['pin']}"
                    d.text((lp.x, lp.y), t, font=f, fill=(110, 110, 110, 255), anchor="mm")
                    if nb.get("use"): d.text((lp.x, lp.y + 18), str(nb["use"]).upper(), font=ls.font(13, False), fill=(130, 130, 130, 255), anchor="mm")
    # roads (dashed) with names
    for r in scan.get("roads", [])[:4]:
        for line in r.get("geometry", []):
            pp = [P(x, y) for x, y in line]
            if len(pp) < 2: continue
            g = LineString(pp).intersection(clip)
            for part in ([g] if g.geom_type == "LineString" else list(getattr(g, "geoms", []))):
                if part.geom_type != "LineString" or part.length < 40: continue
                cs = list(part.coords)
                for (x0, y0), (x1, y1) in zip(cs, cs[1:]):
                    L_ = math.hypot(x1 - x0, y1 - y0); n = max(1, int(L_ / 14))
                    for i in range(0, n, 2):
                        d.line([(x0 + (x1 - x0) * i / n, y0 + (y1 - y0) * i / n), (x0 + (x1 - x0) * (i + 1) / n, y0 + (y1 - y0) * (i + 1) / n)], fill=(90, 90, 90, 255), width=3)
                mid = part.interpolate(0.5, normalized=True); a, b = part.interpolate(0.45, normalized=True), part.interpolate(0.55, normalized=True)
                ang = -math.degrees(math.atan2(b.y - a.y, b.x - a.x)); ang = ang + 180 if ang < -90 or ang > 90 else ang
                _rot_text(img, (mid.x, mid.y - 14), r["name"].upper(), ang, 17, (60, 60, 60, 255), True)
    # the parcel
    pp = [P(x, y) for x, y in poly.exterior.coords]
    fill = Image.new("RGBA", size, (0, 0, 0, 0)); ImageDraw.Draw(fill).polygon(pp, fill=(255, 235, 150, 70)); img.alpha_composite(fill); d = ImageDraw.Draw(img)
    d.line(pp + [pp[0]], fill=(0, 0, 0, 255), width=4, joint="curve")
    # bearings + distances per course, corner markers
    courses = []
    verts = list(poly.exterior.coords)[:-1]
    for i, (a, b) in enumerate(zip(verts, verts[1:] + verts[:1])):
        (ex0, ny0), (ex1, ny1) = m(*a), m(*b); dx, dy = ex1 - ex0, ny1 - ny0; dist_ft = math.hypot(dx, dy) * FT
        if dist_ft < 5: continue
        courses.append((i + 1, bearing_text(dx, dy), dist_ft))
        (x0, y0), (x1, y1) = P(*a), P(*b); mx, my = (x0 + x1) / 2, (y0 + y1) / 2
        ang = -math.degrees(math.atan2(y1 - y0, x1 - x0)); ang = ang + 180 if ang < -90 or ang > 90 else ang
        # offset label outward from the polygon
        nx, ny = -(y1 - y0), (x1 - x0); L_ = math.hypot(nx, ny) or 1; nx, ny = nx / L_, ny / L_
        inside = Polygon(pp).contains(__import__("shapely.geometry", fromlist=["Point"]).Point(mx + nx * 12, my + ny * 12))
        sgn = -1 if inside else 1
        if dist_ft * scale / FT > 90:
            _rot_text(img, (mx + nx * 16 * sgn, my + ny * 16 * sgn), bearing_text(dx, dy), ang, 16, (0, 0, 0, 255), False)
            _rot_text(img, (mx + nx * 36 * sgn, my + ny * 36 * sgn), f"{dist_ft:,.2f}'", ang, 16, (0, 0, 0, 255), True)
        else:
            _rot_text(img, (mx + nx * 20 * sgn, my + ny * 20 * sgn), f"L{len(courses)}", ang, 14, (0, 0, 0, 255), True)
    d = ImageDraw.Draw(img)
    for (x, y) in pp[:-1]:
        d.ellipse((x - 5, y - 5, x + 5, y + 5), fill=(255, 255, 255, 255), outline=(0, 0, 0, 255), width=2)
    # acreage in the middle
    f = ls.font(30, True); d.text((pp and P(c.x, c.y)), f"{parcel['acres']:.2f} AC ±", font=f, fill=(0, 0, 0, 255), anchor="mm")
    d.text((P(c.x, c.y)[0], P(c.x, c.y)[1] + 30), f"PIN {parcel['apn']}", font=ls.font(18, False), fill=(0, 0, 0, 255), anchor="mm")
    # north arrow
    nx_, ny_ = ax1 - 60, ay0 + 90
    d.polygon([(nx_, ny_ - 60), (nx_ - 16, ny_ + 10), (nx_, ny_ - 5)], fill=(0, 0, 0, 255)); d.polygon([(nx_, ny_ - 60), (nx_ + 16, ny_ + 10), (nx_, ny_ - 5)], outline=(0, 0, 0, 255), width=2)
    d.text((nx_, ny_ + 24), "N", font=ls.font(26, True), fill=(0, 0, 0, 255), anchor="mm")
    # scale bar (feet)
    nice = [50, 100, 200, 300, 500, 1000, 2000]; target = 220 / scale * FT; sb = min(nice, key=lambda v: abs(v - target))
    sbw = sb / FT * scale; sx0, sy0 = ax0 + 10, ay1 + 30
    for i in range(4):
        d.rectangle((sx0 + sbw / 4 * i, sy0, sx0 + sbw / 4 * (i + 1), sy0 + 10), fill=(0, 0, 0, 255) if i % 2 == 0 else (255, 255, 255, 255), outline=(0, 0, 0, 255))
    d.text((sx0, sy0 + 14), "0", font=ls.font(14, False), fill=(0, 0, 0, 255), anchor="la"); d.text((sx0 + sbw, sy0 + 14), f"{sb} ft", font=ls.font(14, False), fill=(0, 0, 0, 255), anchor="ra")
    d.text((sx0 + sbw / 2, sy0 + 34), f"SCALE: 1\" = {round(96 / scale * FT):,}'  (at 96 dpi)", font=ls.font(13, False), fill=(0, 0, 0, 255), anchor="ma")
    # course table (small) under the drawing
    tx, ty = ax0 + 300, ay1 + 26
    if any(dist_ft * scale / FT <= 90 for _, _, dist_ft in courses):
        d.text((tx, ty), "SHORT COURSES", font=ls.font(13, True), fill=(0, 0, 0, 255))
        row = ty + 18; i = 0
        for n, bt, dist_ft in courses:
            if dist_ft * scale / FT <= 90 and i < 6:
                d.text((tx, row), f"L{n}  {bt}  {dist_ft:,.2f}'", font=ls.font(12, False), fill=(0, 0, 0, 255)); row += 15; i += 1
    # title block
    x = tb_x + 20; y = 70
    def line(t, sz=16, b=False, gap=None, col=(0, 0, 0, 255)):
        nonlocal y; d.text((x, y), t, font=ls.font(sz, b), fill=col); y += gap or (sz + 10)
    line("PARCEL PLAT EXHIBIT", 30, True, 44)
    line(f"{parcel.get('situs', '') or ''}".upper(), 18, True)
    line(f"{parcel.get('city', '')}, {parcel.get('state', '')} {parcel.get('zip', '')}".strip(", ").upper(), 16, False, 30)
    line(f"PIN / APN: {parcel['apn']}", 16, True)
    line(f"AREA: {parcel['acres']:.2f} ACRES ±  ({parcel['acres'] * 43560:,.0f} SQ FT)", 16, False)
    per = sum(dist for _, _, dist in courses); line(f"PERIMETER: {per:,.0f} FT", 16, False)
    if parcel.get("county"): line(f"COUNTY: {parcel['county'].upper()}, {parcel.get('state', '')}", 16, False)
    plss = scan.get("plss") or {}
    if plss.get("text"): line(f"{plss['text'].upper()}", 15, False)
    if parcel.get("owner"): line(f"OWNER OF RECORD: {str(parcel['owner']).upper()[:34]}", 14, False)
    if parcel.get("use_desc"): line(f"USE: {str(parcel['use_desc']).upper()[:36]}", 14, False)
    legal = (parcel.get("raw") or {}).get("LegalDesc") if isinstance(parcel.get("raw"), dict) else None
    if legal:
        y += 6; line("LEGAL (GIS):", 13, True, 18)
        words = str(legal).split(); row = ""
        for w_ in words:
            if d.textlength(row + " " + w_, font=ls.font(12, False)) > 410: line(row.strip(), 12, False, 15); row = ""
            row += " " + w_
        if row.strip(): line(row.strip(), 12, False, 15)
    y += 10; line("COURSE TABLE", 14, True, 20)
    for n, bt, dist_ft in courses[:18]:
        line(f"L{n:<3} {bt}   {dist_ft:,.2f}'", 12, False, 15)
    if len(courses) > 18: line(f"… {len(courses) - 18} more courses", 12, False, 15)
    y = H - 170
    line(f"PREPARED: {datetime.date.today():%B %d, %Y}", 13, False, 18)
    line("SOURCE: COUNTY / STATE GIS PARCEL POLYGON", 12, False, 16)
    line("BEARINGS & DISTANCES COMPUTED FROM GIS GEOMETRY", 12, False, 16)
    line("(GRID, NAD83 LON/LAT). NOT A BOUNDARY SURVEY.", 12, False, 16)
    line("FOR MARKETING / ILLUSTRATION ONLY.", 12, True, 16)
    img.convert("RGB").save(path)
    return Path(path)
