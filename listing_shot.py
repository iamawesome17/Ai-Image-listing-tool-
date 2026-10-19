#!/usr/bin/env python3
"""
listing_shot.py — Phase 1 of the Bana disposition image tool.

One command → annotated satellite listing images for a parcel:
  - property outline (true parcel polygon from Regrid)
  - centroid pin + "X.XX ACRES +/-" headline card with address
  - road labels (reverse-geocoded along the parcel edges)
  - nearby POI pins (Google Places)
  - disclaimer

Usage:
  python listing_shot.py --apn 300006840 --path /us/ne/fillmore
  python listing_shot.py --address "1214 Road G, Fairmont, NE"
  python listing_shot.py --lat 40.62 --lon -97.60
Options:
  --out DIR        output folder (default ./out/<apn>)
  --no-pois        skip POI pins
  --max-pois N     default 3
  --env FILE       path to .env with REGRID_TOKEN and GOOGLE_MAPS_KEY

Outputs (PNG): cover_4x3.png (1280×960), tight.png, wide.png (1280×1280), clean_cover.png (no overlay)
"""
import argparse, json, math, os, sys, io, urllib.parse, urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageFilter
from shapely.geometry import shape, Polygon, MultiPolygon, Point
from shapely.ops import unary_union

# ---------- config ----------
FONT_DIR = "/usr/share/fonts/truetype/google-fonts"
FONT_BOLD = f"{FONT_DIR}/Poppins-Bold.ttf"
FONT_MED = f"{FONT_DIR}/Poppins-Medium.ttf"
DISCLAIMER = "APPROXIMATE PROPERTY LINES - PHOTO FOR ILLUSTRATION ONLY"
TILE = 640          # Google Static Maps max per side (free tier)
SCALE = 2           # → 1280 px output
POI_EXCLUDE = {"route", "street_address", "locality", "political", "premise",
               "plus_code", "postal_code", "administrative_area_level_1"}


def load_env_only(path):
    """Same as load_env but does not require every key (used by drone_render)."""
    if path and Path(path).exists():
        for line in Path(path).read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def load_env(path):
    if path and not Path(path).exists():
        alt = Path(path).with_name("keys.env")
        if alt.exists():
            path = alt
    if path and Path(path).exists():
        for line in Path(path).read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    if not os.environ.get("GOOGLE_MAPS_KEY"):
        sys.exit("Missing GOOGLE_MAPS_KEY (set env var or --env file)")


def http_json(url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read().decode())


def http_bytes(url):
    with urllib.request.urlopen(url, timeout=40) as r:
        return r.read()


# ---------- 1. parcel ----------
def fetch_parcel(apn=None, path=None, address=None, lat=None, lon=None):
    tok = os.environ["REGRID_TOKEN"]
    base = "https://app.regrid.com/api/v2/parcels/"
    if apn:
        q = {"parcelnumb": apn, "token": tok, "limit": 5}
        if path:
            q["path"] = path
        url = base + "apn?" + urllib.parse.urlencode(q)
    elif address:
        url = base + "address?" + urllib.parse.urlencode({"query": address, "token": tok, "limit": 5})
    else:
        url = base + "point?" + urllib.parse.urlencode({"lat": lat, "lon": lon, "radius": 30, "token": tok, "limit": 5})
    d = http_json(url)
    feats = d.get("parcels", {}).get("features", [])
    if not feats:
        sys.exit("Regrid returned no parcel. Check APN/path, or the area may be outside your plan's coverage.")
    f = feats[0]
    geom = shape(f["geometry"])
    if isinstance(geom, MultiPolygon):
        geom = max(geom.geoms, key=lambda g: g.area)
    fl = f["properties"]["fields"]
    acres = fl.get("ll_gisacre") or fl.get("gisacre") or fl.get("deeded_acres")
    situs = fl.get("address") or ""
    city = fl.get("scity") or fl.get("city") or ""
    state = fl.get("state2") or ""
    zipc = fl.get("szip") or fl.get("szip5") or ""
    return {
        "apn": fl.get("parcelnumb"),
        "path": fl.get("path"),
        "acres": float(acres) if acres else None,
        "situs": situs.title() if situs else "",
        "city": city.title(),
        "state": state,
        "zip": zipc,
        "county": (fl.get("county") or "").title(),
        "owner": fl.get("owner"),
        "use": fl.get("usedesc"),
        "zoning": fl.get("zoning"),
        "polygon": geom,
        "raw": fl,
    }


def acres_from_polygon(poly):
    """Planar acreage from a lon/lat polygon (good to <0.5% for parcel-sized areas)."""
    import math as _m
    c = poly.centroid
    kx, ky = 111320 * _m.cos(_m.radians(c.y)), 110540
    coords = [((x - c.x) * kx, (y - c.y) * ky) for x, y in poly.exterior.coords]
    area = 0.5 * abs(sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(coords, coords[1:])))
    return area / 4046.8564


def parcel_from_feature(f, defaults=None):
    """Normalize a GeoJSON feature from a county ArcGIS layer (Adams Co IL field names first, generic fallbacks)."""
    pr = f.get("properties", {}); d = defaults or {}
    g = lambda *ks: next((pr[k] for k in ks if pr.get(k) not in (None, "", 0)), None)
    from shapely import force_2d
    geom = force_2d(shape(f["geometry"]))          # county layers often carry a Z coordinate
    if isinstance(geom, MultiPolygon):
        geom = max(geom.geoms, key=lambda x: x.area)
    acres = g("Acres", "GIS_Acres", "ACRES", "gisacre", "ll_gisacre")
    return {
        "apn": g("PIN", "GISPIN", "APN", "parcelnumb", "PARCEL_ID"),
        "path": None,
        "acres": float(acres) if acres else round(acres_from_polygon(geom), 2),
        "situs": (g("SiteAddress", "SITEADD", "SITUS", "address") or "").strip().title(),
        "city": (g("city") or d.get("city", "")).title(),
        "state": g("state") or d.get("state", ""),
        "zip": g("zip") or d.get("zip", ""),
        "county": (g("county") or d.get("county", "")).title(),
        "owner": g("OwnerName", "OWNR_NM", "owner"),
        "use": g("UseCode", "PROPCLASS", "usedesc"),
        "zoning": g("Zoning", "zoning"),
        "polygon": geom,
        "raw": pr,
    }


def fetch_parcel_geojson(path, defaults=None):
    d = json.loads(Path(path).read_text())
    feats = d["features"] if d.get("type") == "FeatureCollection" else [d]
    return parcel_from_feature(feats[0], defaults)


def fetch_parcel_arcgis(layer_url, lat, lon, defaults=None):
    """Point-in-polygon query against any public ArcGIS parcel layer, e.g.
    https://www.adamscountyarcserver.com/adamscountyarcserver/rest/services/Adams_County_Basemap_Complete/MapServer/13"""
    q = {"geometry": f"{lon},{lat}", "geometryType": "esriGeometryPoint", "inSR": 4326, "spatialRel": "esriSpatialRelIntersects",
         "outFields": "*", "returnGeometry": "true", "outSR": 4326, "f": "geojson"}
    d = http_json(layer_url.rstrip("/") + "/query?" + urllib.parse.urlencode(q))
    if not d.get("features"):
        sys.exit("County GIS returned no parcel at that point.")
    return parcel_from_feature(d["features"][0], defaults)


# ---------- 2. imagery (Web Mercator math) ----------
def world_px(lat, lon, z):
    n = 256 * (2 ** z)
    x = (lon + 180) / 360 * n
    s = math.sin(math.radians(lat))
    y = (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * n
    return x, y


def choose_zoom(poly, w=TILE, h=TILE, margin=0.30):
    """Highest zoom at which the parcel fits in a w×h (unscaled) map with the given margin."""
    minx, miny, maxx, maxy = poly.bounds
    for z in range(21, 10, -1):
        x0, y0 = world_px(maxy, minx, z)
        x1, y1 = world_px(miny, maxx, z)
        pw, ph = abs(x1 - x0), abs(y1 - y0)
        if pw * (1 + 2 * margin) <= w and ph * (1 + 2 * margin) <= h:
            return z
    return 11


class Frame:
    """Maps lon/lat → pixel in the SCALE×(w×h) output image."""
    def __init__(self, center_lat, center_lon, zoom, w=TILE, h=TILE):
        self.lat, self.lon, self.z = center_lat, center_lon, zoom
        self.cx, self.cy = world_px(center_lat, center_lon, zoom)
        self.w, self.h = w, h
        self.W, self.H = w * SCALE, h * SCALE

    def px(self, lon, lat):
        x, y = world_px(lat, lon, self.z)
        return ((x - self.cx) * SCALE + self.W / 2, (y - self.cy) * SCALE + self.H / 2)

    def inside(self, lon, lat, pad=40):
        x, y = self.px(lon, lat)
        return pad <= x <= self.W - pad and pad <= y <= self.H - pad

    def bbox_px(self, poly):
        xs, ys = zip(*[self.px(x, y) for x, y in poly.exterior.coords])
        return (min(xs), min(ys), max(xs), max(ys))


def fetch_satellite(frame):
    q = {"center": f"{frame.lat},{frame.lon}", "zoom": frame.z, "size": f"{frame.w}x{frame.h}",
         "scale": SCALE, "maptype": "satellite", "format": "png", "key": os.environ["GOOGLE_MAPS_KEY"]}
    url = "https://maps.googleapis.com/maps/api/staticmap?" + urllib.parse.urlencode(q)
    return Image.open(io.BytesIO(http_bytes(url))).convert("RGBA")


# ---------- 2b. context: roads + POIs ----------
def offset_point(lon, lat, bearing_deg, meters):
    R = 6378137.0
    b = math.radians(bearing_deg)
    lat1, lon1 = math.radians(lat), math.radians(lon)
    d = meters / R
    lat2 = math.asin(math.sin(lat1) * math.cos(d) + math.cos(lat1) * math.sin(d) * math.cos(b))
    lon2 = lon1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(lat1), math.cos(d) - math.sin(lat1) * math.sin(lat2))
    return math.degrees(lon2), math.degrees(lat2)


def road_labels(poly, max_edges=4):
    """Reverse-geocode a point ~25 m outside the midpoint of the longest edges → road names."""
    key = os.environ["GOOGLE_MAPS_KEY"]
    coords = list(poly.exterior.coords)
    edges = []
    for (x0, y0), (x1, y1) in zip(coords, coords[1:]):
        mx, my = (x0 + x1) / 2, (y0 + y1) / 2
        length = math.hypot((x1 - x0) * math.cos(math.radians(my)), y1 - y0)
        # outward normal: rotate edge direction; pick the side away from centroid
        ang = math.degrees(math.atan2(x1 - x0, y1 - y0))  # bearing of edge
        c = poly.centroid
        cand = [offset_point(mx, my, ang + 90, 25), offset_point(mx, my, ang - 90, 25)]
        out = max(cand, key=lambda p: Point(p).distance(c))
        edges.append((length, out))
    edges.sort(reverse=True)
    seen, labels = set(), []
    for _, (lon, lat) in edges[:max_edges]:
        q = {"latlng": f"{lat},{lon}", "result_type": "route", "key": key}
        try:
            d = http_json("https://maps.googleapis.com/maps/api/geocode/json?" + urllib.parse.urlencode(q))
        except Exception:
            continue
        if d.get("status") == "REQUEST_DENIED":
            print("    ! Geocoding API not enabled on this key → no road labels. Enable 'Geocoding API' in Google Cloud.")
            return []
        for r in d.get("results", [])[:1]:
            name = next((c["long_name"] for c in r["address_components"] if "route" in c["types"]), None)
            loc = r["geometry"]["location"]
            # only accept if the road geocode is within ~60 m of our probe point
            dist = math.hypot((loc["lng"] - lon) * 111320 * math.cos(math.radians(lat)), (loc["lat"] - lat) * 110540)
            if name and name.lower() not in seen and dist < 60 and not name.lower().startswith("unnamed"):
                seen.add(name.lower())
                labels.append({"name": name.upper(), "lon": loc["lng"], "lat": loc["lat"]})
    return labels


def nearby_pois(lat, lon, radius_m=1500, n=8):
    key = os.environ["GOOGLE_MAPS_KEY"]
    body = json.dumps({"locationRestriction": {"circle": {"center": {"latitude": lat, "longitude": lon}, "radius": radius_m}},
                       "maxResultCount": n, "rankPreference": "DISTANCE"}).encode()
    hdr = {"Content-Type": "application/json", "X-Goog-Api-Key": key,
           "X-Goog-FieldMask": "places.displayName,places.primaryTypeDisplayName,places.primaryType,places.location"}
    d = http_json("https://places.googleapis.com/v1/places:searchNearby", data=body, headers=hdr)
    out = []
    for p in d.get("places", []):
        t = p.get("primaryType", "")
        if t in POI_EXCLUDE or "primaryTypeDisplayName" not in p:
            continue
        out.append({"name": p["displayName"]["text"], "type": p["primaryTypeDisplayName"]["text"],
                    "lat": p["location"]["latitude"], "lon": p["location"]["longitude"]})
    return out


# ---------- 3. compositor ----------
def font(size, bold=True):
    return ImageFont.truetype(FONT_BOLD if bold else FONT_MED, size)


def shadowed(img, draw_fn, blur=6, offset=(0, 4), alpha=140):
    """Draw draw_fn onto a shadow layer, blur it, composite, then draw for real."""
    sh = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw_fn(ImageDraw.Draw(sh), (0, 0, 0, alpha), offset)
    sh = sh.filter(ImageFilter.GaussianBlur(blur))
    img.alpha_composite(sh)
    draw_fn(ImageDraw.Draw(img), None, (0, 0))


def draw_outline(img, frame, poly, color=(255, 255, 255, 255), width=5, fill_alpha=34):
    pts = [frame.px(c[0], c[1]) for c in poly.exterior.coords]
    fill = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(fill).polygon(pts, fill=(255, 255, 255, fill_alpha))
    img.alpha_composite(fill)

    def fn(d, shadow, off):
        p = [(x + off[0], y + off[1]) for x, y in pts]
        d.line(p + [p[0]], fill=shadow or color, width=width, joint="curve")
    shadowed(img, fn, blur=5, offset=(0, 3), alpha=170)


PLACED = []  # bboxes of cards already drawn on the current image (collision avoidance)


def measure_box(img, xy, lines, sizes, pad, anchor):
    d = ImageDraw.Draw(img)
    fonts = [font(s, bold=(i == 0)) for i, s in enumerate(sizes)]
    widths = [d.textlength(t, font=f) for t, f in zip(lines, fonts)]
    heights = [f.getbbox("Ag")[3] + 4 for f in fonts]
    w = max(widths) + pad[0] * 2
    h = sum(heights) + pad[1] * 2 + (len(lines) - 1) * 4
    x, y = xy
    x0 = x - w / 2
    y0 = y if anchor == "top" else y - h
    return (x0, y0, x0 + w, y0 + h), fonts, heights


def overlaps(b, others, margin=10):
    return any(b[0] - margin < o[2] and b[2] + margin > o[0] and b[1] - margin < o[3] and b[3] + margin > o[1] for o in others)


def text_box(img, xy, lines, sizes, pad=(22, 14), radius=14, anchor="top", bg=(255, 255, 255, 242), fg=(20, 20, 20, 255)):
    """White rounded card with stacked text lines; xy = (x_center, y_top or y_bottom)."""
    (x0, y0, x1, y1), fonts, heights = measure_box(img, xy, lines, sizes, pad, anchor)
    w, h = x1 - x0, y1 - y0
    x = xy[0]

    def fn(dd, shadow, off):
        dd.rounded_rectangle((x0 + off[0], y0 + off[1], x0 + w + off[0], y0 + h + off[1]), radius=radius, fill=shadow or bg)
    shadowed(img, fn, blur=10, offset=(0, 6), alpha=120)
    d = ImageDraw.Draw(img)
    yy = y0 + pad[1]
    for t, f, hh in zip(lines, fonts, heights):
        d.text((x, yy), t, font=f, fill=fg, anchor="ma")
        yy += hh + 4
    PLACED.append((x0, y0, x0 + w, y0 + h))
    return (x0, y0, x0 + w, y0 + h)


def draw_pin(img, xy, color=(220, 38, 38, 255), r=22):
    x, y = xy

    def fn(d, shadow, off):
        c = shadow or color
        d.ellipse((x - r + off[0], y - 2 * r - r + off[1], x + r + off[0], y - r + off[1]), fill=c)
        d.polygon([(x - r * 0.75 + off[0], y - 1.5 * r + off[1]), (x + r * 0.75 + off[0], y - 1.5 * r + off[1]), (x + off[0], y + off[1])], fill=c)
    shadowed(img, fn, blur=6, offset=(0, 5), alpha=150)
    ImageDraw.Draw(img).ellipse((x - r * 0.4, y - 2.4 * r, x + r * 0.4, y - 1.6 * r), fill=(255, 255, 255, 255))


def draw_pole(img, top, bottom, width=5):
    def fn(d, shadow, off):
        d.line([(top[0] + off[0], top[1] + off[1]), (bottom[0] + off[0], bottom[1] + off[1])], fill=shadow or (255, 255, 255, 255), width=width)
        d.ellipse((bottom[0] - 7 + off[0], bottom[1] - 7 + off[1], bottom[0] + 7 + off[0], bottom[1] + 7 + off[1]), fill=shadow or (255, 255, 255, 255))
    shadowed(img, fn, blur=4, offset=(0, 3), alpha=160)


def draw_headline(img, frame, parcel):
    c = parcel["polygon"].centroid
    cx, cy = frame.px(c.x, c.y)
    pole_top = (cx, max(0.33 * frame.H, cy - min(260, frame.H * 0.22)))
    draw_pole(img, pole_top, (cx, cy))
    acres = f"{parcel['acres']:.2f} ACRES +/-" if parcel["acres"] else "ACREAGE TBD"
    addr = ", ".join(s for s in [parcel["situs"], parcel["city"], f"{parcel['state']} {parcel['zip']}".strip()] if s).upper()
    if parcel.get("county"): addr += f"  ·  {parcel['county'].upper()} COUNTY"
    box = text_box(img, (cx, pole_top[1] - 10), [acres, addr], [58, 24], anchor="bottom")
    draw_pin(img, (cx, box[1] - 6), r=24)


def draw_label(img, xy, text, size=30):
    x, y = xy
    text_box(img, (x, y - 24), [text], [size], pad=(16, 8), radius=10)


def draw_poi(img, xy, name, kind):
    x, y = xy
    box, _, _ = measure_box(img, (x, y - 2.8 * 16 - 8), [name, kind], [26, 20], (14, 8), "bottom")
    if overlaps(box, PLACED) or box[0] < 10 or box[2] > img.width - 10 or box[1] < 10:
        return False
    draw_pin(img, (x, y), r=16)
    text_box(img, (x, y - 2.8 * 16 - 8), [name, kind], [26, 20], pad=(14, 8), radius=10, anchor="bottom")
    return True


def draw_disclaimer(img, text=DISCLAIMER, size=24):
    d = ImageDraw.Draw(img)
    f = font(size)
    w = d.textlength(text, font=f)
    x, y = img.width - 28 - w, img.height - 70 - size - 8   # sits above Google's attribution strip
    for dx, dy in [(-2, -2), (2, -2), (-2, 2), (2, 2), (0, 3)]:
        d.text((x + dx, y + dy), text, font=f, fill=(0, 0, 0, 200))
    d.text((x, y), text, font=f, fill=(255, 255, 255, 255))


def compose(sat, frame, parcel, roads, pois, with_overlay=True):
    img = sat.copy()
    if not with_overlay:
        return img
    PLACED.clear()
    draw_outline(img, frame, parcel["polygon"])
    draw_headline(img, frame, parcel)
    PLACED.append(frame.bbox_px(parcel["polygon"]))  # keep POI cards off the parcel itself
    for r in roads:
        if frame.inside(r["lon"], r["lat"], pad=120):
            draw_label(img, frame.px(r["lon"], r["lat"]), r["name"])
    for p in pois:
        if frame.inside(p["lon"], p["lat"], pad=140) and not parcel["polygon"].contains(Point(p["lon"], p["lat"])):
            draw_poi(img, frame.px(p["lon"], p["lat"]), p["name"], p["type"])
    draw_disclaimer(img)
    return img


# ---------- main ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apn"); ap.add_argument("--path"); ap.add_argument("--address")
    ap.add_argument("--lat", type=float); ap.add_argument("--lon", type=float)
    ap.add_argument("--geojson", help="parcel polygon from a GeoJSON file (county GIS export)")
    ap.add_argument("--arcgis", help="ArcGIS parcel layer URL to query by --lat/--lon (county GIS)")
    ap.add_argument("--city"); ap.add_argument("--state"); ap.add_argument("--zip"); ap.add_argument("--county")
    ap.add_argument("--out"); ap.add_argument("--no-pois", action="store_true"); ap.add_argument("--max-pois", type=int, default=3)
    ap.add_argument("--env", default=str(Path(__file__).with_name(".env")))
    a = ap.parse_args()
    load_env(a.env)
    if not (a.geojson or a.apn or a.address or (a.lat is not None and a.lon is not None)):
        sys.exit("Give --geojson, --apn (+ --path), --address, or --lat/--lon")

    print("1/4 parcel …", flush=True)
    defaults = {"city": a.city or "", "state": a.state or "", "zip": a.zip or "", "county": a.county or ""}
    if a.geojson:
        parcel = fetch_parcel_geojson(a.geojson, defaults)
    elif a.arcgis:
        parcel = fetch_parcel_arcgis(a.arcgis, a.lat, a.lon, defaults)
    else:
        parcel = fetch_parcel(a.apn, a.path, a.address, a.lat, a.lon)
    print(f"    {parcel['apn']} | {parcel['situs']} {parcel['city']} {parcel['state']} | {parcel['acres']} ac | {parcel['use']}")
    poly = parcel["polygon"]
    c = poly.centroid

    print("2/4 imagery + context …", flush=True)
    z = choose_zoom(poly)
    zc = choose_zoom(poly, 640, 480)
    frames = {"cover": Frame(c.y, c.x, zc, 640, 480), "tight": Frame(c.y, c.x, z), "wide": Frame(c.y, c.x, z - 1)}
    sats = {k: fetch_satellite(f) for k, f in frames.items()}
    roads = road_labels(poly)
    pois = [] if a.no_pois else nearby_pois(c.y, c.x)[: a.max_pois]
    print(f"    zoom {z} | roads: {[r['name'] for r in roads]} | pois: {[p['name'] for p in pois]}")

    print("3/4 compose …", flush=True)
    out = Path(a.out or f"out/{parcel['apn']}")
    out.mkdir(parents=True, exist_ok=True)
    cover = compose(sats["cover"], frames["cover"], parcel, roads, pois)
    tight = compose(sats["tight"], frames["tight"], parcel, roads, pois)
    wide = compose(sats["wide"], frames["wide"], parcel, roads, pois)
    clean = compose(sats["cover"], frames["cover"], parcel, roads, pois, with_overlay=False)
    cover.convert("RGB").save(out / "cover_4x3.png")
    tight.convert("RGB").save(out / "tight.png")
    wide.convert("RGB").save(out / "wide.png")
    clean.convert("RGB").save(out / "clean_cover.png")
    (out / "parcel.json").write_text(json.dumps({k: v for k, v in parcel.items() if k not in ("polygon", "raw")} | {"roads": roads, "pois": pois, "zoom": z}, indent=2))

    print(f"4/4 done → {out}/")
    for f in ["cover_4x3.png", "tight.png", "wide.png", "clean_cover.png", "parcel.json"]:
        print("   ", out / f)


if __name__ == "__main__":
    main()
