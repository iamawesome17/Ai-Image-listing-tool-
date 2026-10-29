#!/usr/bin/env python3
"""
pipeline.py — one command → full listing photo packet for a parcel.

  python pipeline.py --county adams-il --pin 20-0-0636-002-00
  python pipeline.py --county adams-il --lat 39.91722 --lon -91.2892
  python pipeline.py --regrid --apn 300006840 --path /us/ne/fillmore      (Regrid-covered counties)

Stages (each cached in out/<pin>/, re-run skips what exists unless --force):
  1 parcel        county ArcGIS (counties.json) or Regrid → parcel.geojson
  2 scan          surroundings: roads, water, buildings, flood zone, neighbors, POIs, land-cover stats → scan.json
  3 satellite     Google Static Maps reference (clean + magenta boundary fiducial) → ref_*.png
  4 phase1        annotated satellite set (listing_shot) → p1_*.png
  5 render        Higgsfield (Grok Image 2.0) obliques + nadir, prompts built from the scan → ai/*.jpg
  6 outline       detect the fiducial in every render → styled outline, pin, card, disclaimer → ai/*_outline.jpg
  7 packet        numbered folder + README + zip, run.json log of every API call

Options:
  --angles N,NE,E,SE,S,SW,W,NW   oblique camera positions (default all 8)
  --lights golden,day            lighting variants (default golden,day for N/E/S/W; golden only for the diagonals)
  --no-render                    stop after phase 1
  --out DIR                      default out/<pin>
"""
import argparse, re, json, math, os, sys, time, uuid, io, urllib.request, urllib.parse, zipfile, shutil
from pathlib import Path

import numpy as np, cv2
from PIL import Image, ImageDraw
from shapely.geometry import shape, Point, LineString, MultiLineString, Polygon
from shapely.ops import unary_union

sys.path.insert(0, str(Path(__file__).parent))
import listing_shot as ls
import drone_render as dr
import layout as L

HERE = Path(__file__).parent
LOG = []                      # every external call, written to run.json
FIDUCIAL = (255, 0, 255)      # magenta boundary drawn on the reference the AI sees
ROAD_COLORS = [((0, 255, 255), "00ffff", (85, 95)), ((0, 64, 255), "0040ff", (108, 122))]   # (rgb, hex, opencv hue band) per road
POWER_COLOR = ((255, 140, 0), "ff8c00", (8, 20))
COMPASS = {"N": 0, "NE": 45, "E": 90, "SE": 135, "S": 180, "SW": 225, "W": 270, "NW": 315}


def log(stage, what, **kw):
    LOG.append({"t": time.strftime("%H:%M:%S"), "stage": stage, "what": what, **kw})
    print(f"  [{stage}] {what}" + (f"  {kw}" if kw else ""), flush=True)


def getjson(url, stage, timeout=40, **kw):
    log(stage, "GET " + url.split("?")[0], **kw)
    return ls.http_json(url, timeout=timeout)


# ---------- 1. parcel ----------
def county_query(cfg, layer, params, stage="gis"):
    base = f"{cfg['server']}/{cfg['layers'][layer]}/query?"
    q = {"f": "geojson", "outSR": 4326, "returnGeometry": "true", "outFields": "*", **params}
    return getjson(base + urllib.parse.urlencode(q), stage, layer=layer)


def layer_query(url, params, stage="gis", fmt="geojson", timeout=40):
    """Query any ArcGIS feature/map layer URL (…/MapServer/13 or …/FeatureServer/0)."""
    q = {"f": fmt, "outSR": 4326, "returnGeometry": "true", "outFields": "*", **params}
    return getjson(url.rstrip("/") + "/query?" + urllib.parse.urlencode(q), stage, timeout=timeout)


def pin_variants(pin, mode):
    raw = pin.strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    tight = "".join(ch for ch in raw.upper() if ch.isalnum())
    if mode == "digits": return [digits]
    out = []
    for v in (raw, raw.upper(), tight, digits, raw.replace("-", "").replace(" ", "")):
        if v and v not in out: out.append(v)
    return out


def tn_parts(pin):
    """Tennessee (Comptroller) parcel number → dict(county_id, map, group, parcel), or None.
    Accepts what people actually type: '001 001.00', '001-001.00', '007C A 001.00', '055 001 00100 000', or the layer's own
    PARCELID '055 001    00100 000 2026'. The same map + parcel exists in every TN county, so the county is matched separately."""
    s = re.sub(r"\s+20\d{2}\s*$", "", (pin or "").upper().strip())          # trailing tax year
    toks = [t for t in re.split(r"[\s\-_/,]+", s) if t]
    if not toks: return None
    pi = next((i for i in range(len(toks) - 1, -1, -1) if re.fullmatch(r"\d{1,3}\.\d{1,2}|\d{5}", toks[i])), None)
    if pi is None:                                                             # e.g. '001 1' — last plain number is the parcel
        pi = next((i for i in range(len(toks) - 1, 0, -1) if re.fullmatch(r"\d{1,3}", toks[i])), None)
        if pi is None: return None
    p = toks[pi]
    if "." in p: a, b = p.split("."); parcel = f"{int(a):03d}.{b.ljust(2, '0')[:2]}"
    elif len(p) == 5: parcel = f"{p[:3]}.{p[3:]}"
    else: parcel = f"{int(p):03d}.00"
    head = toks[:pi]
    group = ""
    if head and re.fullmatch(r"[A-Z]", head[-1]): group = head.pop()
    if not head or not re.fullmatch(r"\d{1,3}[A-Z]?", head[-1]): return None
    m = re.fullmatch(r"(\d{1,3})([A-Z]?)", head.pop()); cmap = f"{int(m.group(1)):03d}{m.group(2)}"
    cid = int(head[-1]) if head and re.fullmatch(r"\d{3}", head[-1]) else None
    return {"county_id": cid, "map": cmap, "group": group, "parcel": parcel}


def _sq(v): return str(v).replace("'", "''")


def tn_address(a):
    """'SMITH LN 660' (assessor order) → '660 Smith Ln'."""
    a = (a or "").strip(); m = re.fullmatch(r"(.+?)\s+(\d+[A-Z]?)", a)
    return (f"{m.group(2)} {m.group(1)}" if m else a).title()


def reverse_geocode(lat, lon):
    """city / county / zip from Google Geocoding (returns {} if the API is not enabled on the key)."""
    try:
        d = getjson("https://maps.googleapis.com/maps/api/geocode/json?" + urllib.parse.urlencode({"latlng": f"{lat},{lon}", "key": os.environ["GOOGLE_MAPS_KEY"]}), "geocode")
        out = {}
        for r in d.get("results", []):
            for c in r.get("address_components", []):
                t = c["types"]
                if "locality" in t and "city" not in out: out["city"] = c["long_name"]
                if "administrative_area_level_2" in t and "county" not in out: out["county"] = c["long_name"].replace(" County", "")
                if "postal_code" in t and "zip" not in out: out["zip"] = c["long_name"]
                if "route" in t and "street" not in out: out["street"] = c["long_name"]
                if "administrative_area_level_1" in t and "state" not in out: out["state"] = c["short_name"]
            if len(out) >= 3: break
        return out
    except Exception:
        return {}


def _merc_to_ll(x, y):
    R = 6378137.0
    return (math.degrees(x / R), math.degrees(2 * math.atan(math.exp(y / R)) - math.pi / 2))


def fetch_parcel_camavision(cfg, pin=None, lat=None, lon=None):
    """CAMAvision (Vanguard) county maps: WFS 'parcels' gives attributes + envelope (no geometry), the 'search' layer gives polygons;
    we match the polygon whose bbox equals the parcel envelope. Coordinates come back in EPSG:3857."""
    base = cfg["wfs"]; F = cfg["fields"]; pinf = cfg.get("pin_field", "parcelid")
    def wfs(params):
        q = {"SERVICE": "WFS", "VERSION": "1.1.0", "REQUEST": "GetFeature", **params}
        log("parcel", "GET " + base + " (WFS " + params.get("TYPENAME", "") + ")")
        return ls.http_bytes(base + "?" + urllib.parse.urlencode(q), timeout=40).decode("utf-8", "ignore")
    feat = None
    if pin:
        for v in pin_variants(pin, cfg.get("pin_normalize", "loose")):
            flt = f'<ogc:Filter xmlns:ogc="http://www.opengis.net/ogc"><ogc:PropertyIsLike wildCard="*" singleChar="." escapeChar="!"><ogc:PropertyName>{pinf}</ogc:PropertyName><ogc:Literal>{v}</ogc:Literal></ogc:PropertyIsLike></ogc:Filter>'
            xml = wfs({"TYPENAME": "parcels", "FILTER": flt, "MAXFEATURES": 5})
            members = xml.split("<gml:featureMember>")[1:]
            if members: feat = members[0]; break
    else:
        R = 6378137.0; x = math.radians(lon) * R; y = math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)) * R
        xml = wfs({"TYPENAME": "parcels", "BBOX": f"{x-1},{y-1},{x+1},{y+1},EPSG:3857", "MAXFEATURES": 5})
        members = xml.split("<gml:featureMember>")[1:]
        if members: feat = members[0]
    if not feat:
        sys.exit(f"No parcel found in {cfg['name']} for that parcel number / point. Check the number as it appears on the county assessor site.")
    attrs = {k: v for k, v in re.findall(r"<ms:([A-Za-z0-9_]+)>([^<]*)</ms:\1>", feat)}
    m = re.search(r"<gml:lowerCorner>([-\d.]+) ([-\d.]+)</gml:lowerCorner>\s*<gml:upperCorner>([-\d.]+) ([-\d.]+)", feat)
    if not m: sys.exit("CAMAvision parcel has no envelope.")
    x0, y0, x1, y1 = map(float, m.groups())
    xml = wfs({"TYPENAME": cfg.get("geom_layer", "search"), "BBOX": f"{x0},{y0},{x1},{y1},EPSG:3857", "MAXFEATURES": 400})
    poly = None
    for mem in xml.split("<gml:featureMember>")[1:]:
        mb = re.search(r"<gml:lowerCorner>([-\d.]+) ([-\d.]+)</gml:lowerCorner>\s*<gml:upperCorner>([-\d.]+) ([-\d.]+)", mem)
        if not mb: continue
        if max(abs(float(a) - b) for a, b in zip(mb.groups(), (x0, y0, x1, y1))) > 0.5: continue
        pl = re.search(r"<gml:posList[^>]*>([^<]+)</gml:posList>", mem)
        if not pl: continue
        nums = list(map(float, pl.group(1).split()))
        ring = [_merc_to_ll(nums[i], nums[i + 1]) for i in range(0, len(nums) - 1, 2)]
        if len(ring) >= 4: poly = Polygon(ring); break
    if poly is None: sys.exit("CAMAvision returned the parcel record but no polygon for it.")
    g = lambda k: attrs.get(F[k]) if k in F and F[k] in attrs else None
    acres = g("acres")
    try: acres = float(acres) if acres not in (None, "") else None
    except ValueError: acres = None
    feature = {"type": "Feature", "geometry": poly.__geo_interface__, "properties": attrs}
    norm = {"PIN": attrs.get(pinf), "SiteAddress": g("situs"), "Acres": acres, "OwnerName": g("owner"), "UseCode": str(g("use") or "").strip(),
            "LegalDesc": g("legal"), "county": cfg.get("defaults", {}).get("county"), "city": g("city"), "zip": str(g("zip") or "")[:5] or None}
    parcel = ls.parcel_from_feature({"geometry": poly.__geo_interface__, "properties": norm}, cfg.get("defaults", {}))
    if cfg.get("reverse_geocode") or not parcel["city"]:
        c = poly.centroid; rg = reverse_geocode(c.y, c.x)
        parcel["city"] = parcel["city"] or rg.get("city", ""); parcel["zip"] = parcel["zip"] or rg.get("zip", "")
        if not parcel["situs"] and rg.get("street"): parcel["situs"] = rg["street"]
    parcel["use_desc"] = cfg.get("use_codes", {}).get(parcel["use"], parcel["use"].lower() if parcel["use"] and not parcel["use"].isdigit() else "land")
    parcel["feature"] = feature
    return parcel


def fetch_parcel_realie(pin=None, state=None, county=None, address=None, lat=None, lon=None):
    """Realie.ai nationwide property API (free tier): parcel by APN + state + county, or by street address + state."""
    key = os.environ.get("REALIE_TOKEN", "").strip()
    if not key: sys.exit("REALIE_TOKEN is not set (Railway → Variables).")
    hdr = {"Authorization": key}
    if pin and state and county:
        q = {"parcelId": pin.strip(), "state": state.upper(), "county": county}
        d = ls.http_json("https://app.realie.ai/api/public/property/parcelId/?" + urllib.parse.urlencode(q), headers=hdr, timeout=40)
        log("parcel", "Realie parcelId lookup", **{k: q[k] for k in ("parcelId", "state", "county")})
        props = [d["property"]] if d.get("property") else []
        if not props:                                          # try the digits-only form; Realie matches the county's own format
            alt = re.sub(r"[^0-9A-Za-z]", "", pin)
            if alt != pin.strip():
                d = ls.http_json("https://app.realie.ai/api/public/property/parcelId/?" + urllib.parse.urlencode({**q, "parcelId": alt}), headers=hdr, timeout=40)
                props = [d["property"]] if d.get("property") else []
    elif address and state:
        q = {"state": state.upper(), "address": address, "limit": 3}
        if county: q["county"] = county
        d = ls.http_json("https://app.realie.ai/api/public/property/search/?" + urllib.parse.urlencode(q), headers=hdr, timeout=40)
        log("parcel", "Realie address search", address=address, state=state)
        props = [x for x in d.get("properties", []) if x.get("geometry")]
    else:
        sys.exit("Realie needs a parcel number with state + county, or a street address with state.")
    if not props: sys.exit(f"Realie found no parcel for that input in {county or ''} {state or ''}.".replace("  ", " "))
    pr = props[0]; geom = pr.get("geometry")
    if not geom: sys.exit("Realie has this parcel but no boundary polygon for it — use the county source or a Land id KML.")
    from shapely import force_2d
    poly = force_2d(shape(geom))
    if poly.geom_type == "MultiPolygon": poly = max(poly.geoms, key=lambda g: g.area)
    loc = pr.get("propertyLocation", {}); ident = pr.get("propertyIdentification", {}); own = ident.get("currentOwner", {})
    acres = (pr.get("landInformation") or {}).get("acres") or pr.get("acres")
    norm = {"PIN": pr.get("parcelId") or ident.get("parcelId"), "SiteAddress": loc.get("address") or pr.get("address"), "Acres": acres,
            "OwnerName": " & ".join(x for x in [own.get("ownerName"), own.get("ownerName2")] if x), "UseCode": str(pr.get("useCode") or ""),
            "LegalDesc": (pr.get("legalInformation") or {}).get("legalDescription"), "county": (loc.get("countyUSPS") or ident.get("county") or county or "").title(),
            "city": loc.get("city") or ident.get("city"), "zip": loc.get("zipCode") or pr.get("zipCode")}
    parcel = ls.parcel_from_feature({"geometry": poly.__geo_interface__, "properties": norm}, {"state": (loc.get("state") or state or "").upper()})
    cl = pr.get("propertyClassification") or {}
    parcel["use_desc"] = (cl.get("countyLandUseCode") or "").strip(" (").lower() or "land"
    parcel["feature"] = {"type": "Feature", "geometry": poly.__geo_interface__, "properties": {k: v for k, v in pr.items() if k not in ("geometry", "location")}}
    parcel["raw"] = {"LegalDesc": norm["LegalDesc"], "source": "realie", "realieParcelId": pr.get("realieParcelId")}
    return parcel


def fetch_parcel_county(cfg, pin=None, lat=None, lon=None, county_name=None):
    """Parcel from a county or statewide ArcGIS parcel layer, normalized to the pipeline's parcel dict."""
    if cfg.get("kind") == "camavision":
        return fetch_parcel_camavision(cfg, pin, lat, lon)
    url = cfg.get("parcels_url") or f"{cfg['server']}/{cfg['layers']['parcels']}"
    F = cfg["fields"]; d = {}
    cn = re.sub(r"\s+county$", "", (county_name or "").strip(), flags=re.I)
    if pin and cfg.get("pin_mode") == "tn":
        tp = tn_parts(pin)
        if not tp:
            sys.exit(f"Could not read '{pin}' as a Tennessee parcel number. Use map + parcel as on the assessor card, e.g. 001 001.00 "
                     "(or 007C A 001.00 with a group letter).")
        w = f"CMAP='{tp['map']}' AND PARCEL='{tp['parcel']}' AND " + (f"GP='{tp['group']}'" if tp["group"] else "(GP=' ' OR GP='' OR GP IS NULL)")
        if cn: w += f" AND UPPER(COUNTY_NAME)='{_sq(cn.upper())}'"
        elif tp["county_id"]: w += f" AND COUNTY_ID={tp['county_id']}"
        d = layer_query(url, {"where": w}, "parcel")
        feats = d.get("features") or []
        counties = sorted({(f["properties"].get("COUNTY_NAME") or "?") for f in feats})
        if len(counties) > 1:
            sys.exit(f"Parcel {tp['map']} {tp['group'] + ' ' if tp['group'] else ''}{tp['parcel']} exists in {len(counties)} Tennessee counties "
                     f"({', '.join(counties[:8])}{'…' if len(counties) > 8 else ''}). Type the county name in the County box.")
        if not feats:
            sys.exit(f"No Tennessee parcel {tp['map']} {tp['group'] + ' ' if tp['group'] else ''}{tp['parcel']}" + (f" in {cn} County" if cn else "") +
                     ". Check map/group/parcel on the assessor card (assessment.cot.tn.gov) — or use coordinates.")
    elif pin:
        vs = list(dict.fromkeys(v for v in pin_variants(pin, cfg.get("pin_normalize", "loose")) if v))
        d = layer_query(url, {"where": f"{cfg['pin_field']} IN ({','.join(repr(v) for v in vs)})"}, "parcel")
        if not d.get("features"):                                 # some servers reject IN on text keys: fall back to one-by-one
            for v in vs:
                d = layer_query(url, {"where": f"{cfg['pin_field']}='{v}'"}, "parcel")
                if d.get("features"): break
    else:
        d = layer_query(url, {"geometry": f"{lon},{lat}", "geometryType": "esriGeometryPoint", "inSR": 4326, "spatialRel": "esriSpatialRelIntersects"}, "parcel")
    if not d.get("features"):
        sys.exit(f"No parcel found in {cfg['name']} for that parcel number / point. Check the number as it appears on the county assessor site.")
    f = d["features"][0]; pr = f["properties"]
    g = lambda k: pr.get(F[k]) if k in F else None
    code = g("county_code"); county = g("county") or (cfg.get("county_codes", {}).get(str(code).strip()) if code is not None else None)
    norm = {"PIN": pr.get(cfg["pin_field"]), "SiteAddress": g("situs"), "Acres": g("acres"), "OwnerName": g("owner"),
            "UseCode": str(g("use") or "").strip(), "LegalDesc": g("legal"), "county": county, "city": g("city"), "zip": str(g("zip") or "")[:5] or None}
    if not norm["Acres"] and g("land_sqft"):
        try: norm["Acres"] = round(float(g("land_sqft")) / 43560, 2)
        except (TypeError, ValueError): pass
    if cfg.get("pin_mode") == "tn":                                  # readable id + address order for Tennessee assessor data
        gp = (pr.get("GP") or "").strip()
        norm["PIN"] = f"{int(pr.get('COUNTY_ID') or 0):03d}-{(pr.get('CMAP') or '').strip()}{'-' + gp if gp else ''}-{(pr.get('PARCEL') or '').strip()}"
        norm["SiteAddress"] = tn_address(norm["SiteAddress"])
        if not norm["Acres"]: norm["Acres"] = None                   # DEEDAC 0 = not recorded → acres from the polygon
    parcel = ls.parcel_from_feature({"geometry": f["geometry"], "properties": norm}, cfg.get("defaults", {}))
    if cfg.get("pin_mode") == "tn" and pr.get("COUNTY_NAME"): parcel["county"] = pr["COUNTY_NAME"]   # keep 'McNairy', not 'Mcnairy'
    if cfg.get("reverse_geocode") or not parcel["county"] or not parcel["situs"]:
        c = parcel["polygon"].centroid; rg = reverse_geocode(c.y, c.x)
        parcel["city"] = parcel["city"] or rg.get("city", ""); parcel["zip"] = parcel["zip"] or rg.get("zip", "")
        parcel["county"] = parcel["county"] or rg.get("county", "")
        if not parcel["situs"] and rg.get("street"): parcel["situs"] = rg["street"]
    parcel["use_desc"] = cfg.get("use_codes", {}).get(parcel["use"], (parcel["use"] if parcel["use"] and not parcel["use"].isdigit() else f"use code {parcel['use']}") if parcel["use"] else "land")
    parcel["feature"] = f
    return parcel


# ---------- 2. surroundings scan ----------
def bearing(c, p):
    dx = (p.x - c.x) * math.cos(math.radians(c.y)); dy = p.y - c.y
    b = (math.degrees(math.atan2(dx, dy)) + 360) % 360
    return ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][int((b + 22.5) // 45) % 8]


def road_title(name):
    """Title-case a road name but keep highway designators (US 421 S, NC 42, SR 1234, CR 12, I-70)."""
    keep = {"US", "NC", "SC", "VA", "FL", "MO", "IL", "SR", "CR", "FM", "RR", "NE", "NW", "SE", "SW", "N", "S", "E", "W"}
    out = []
    for w in name.split():
        u = w.upper()
        out.append(u if u in keep or re.fullmatch(r"I-\d+|[A-Z]{1,2}-?\d+[A-Z]?", u) else w.title())
    return " ".join(out)


def meters(a, b):
    return math.hypot((a.x - b.x) * 111320 * math.cos(math.radians(a.y)), (a.y - b.y) * 110540)


def landcover_stats(sat, frame, poly):
    """Rough land-cover split inside the parcel from the satellite pixels: canopy / open green / bare-dry / built-gray."""
    arr = np.array(sat.convert("RGB")).astype(int)
    mask = Image.new("L", sat.size, 0)
    ImageDraw.Draw(mask).polygon([frame.px(x, y) for x, y in poly.exterior.coords], fill=255)
    m = np.array(mask) > 0
    px = arr[m]
    if len(px) == 0:
        return {}
    r, g, b = px[:, 0], px[:, 1], px[:, 2]
    hsv = cv2.cvtColor(px.reshape(-1, 1, 3).astype(np.uint8), cv2.COLOR_RGB2HSV).reshape(-1, 3)
    h, s, v = hsv[:, 0], hsv[:, 1], hsv[:, 2]
    canopy = (v < 125) & (s > 25)                                   # dark, saturated = tree canopy / shadowed woods
    bare = ~canopy & (h < 25) & (s > 35)                            # tan / brown = bare soil, stubble, gravel
    gray = ~canopy & ~bare & (s < 35)                               # roofs, pavement, water glare
    open_green = ~canopy & ~bare & ~gray                            # everything else = grass, crops, pasture
    n = len(px)
    return {"tree_canopy": round(float(canopy.mean()), 2), "open_green": round(float(open_green.mean()), 2),
            "bare_or_crop_stubble": round(float(bare.mean()), 2), "gray_built_or_road": round(float(gray.mean()), 2), "pixels": int(n)}


def scan_surroundings(cfg, parcel, sat_wide, frame_wide):
    poly = parcel["polygon"]; c = poly.centroid
    minx, miny, maxx, maxy = poly.bounds; pad = 0.004
    env = json.dumps({"xmin": minx - pad, "ymin": miny - pad, "xmax": maxx + pad, "ymax": maxy + pad, "spatialReference": {"wkid": 4326}})
    envq = {"geometry": env, "geometryType": "esriGeometryEnvelope", "inSR": 4326, "spatialRel": "esriSpatialRelIntersects"}
    F = cfg["fields"]; scan = {"roads": [], "water": [], "buildings": {}, "flood": {}, "neighbors": [], "pois": []}

    # roads: name, side of parcel, adjacency
    for f in county_query(cfg, "roads", envq, "scan").get("features", []):
        g = shape(f["geometry"]); name = (f["properties"].get(F["road_name"]) or "").strip()
        if not name: continue
        d = min(meters(Point(p), poly.exterior.interpolate(poly.exterior.project(Point(p)))) for p in (g.coords if isinstance(g, LineString) else [q for l in g.geoms for q in l.coords]))
        near = g.interpolate(g.project(c)) if isinstance(g, LineString) else min((l.interpolate(l.project(c)) for l in g.geoms), key=lambda p: meters(p, c))
        lines = [g] if isinstance(g, LineString) else list(g.geoms)
        scan["roads"].append({"name": road_title(name), "side": bearing(c, near), "distance_m": round(d), "adjacent": d < 25,
                              "label_lon": near.x, "label_lat": near.y,
                              "geometry": [[[round(x, 6), round(y, 6)] for x, y, *_ in l.coords] for l in lines]})
    # dedupe by name keeping the closest
    best = {}
    for r in scan["roads"]:
        if r["name"] not in best: best[r["name"]] = r
        else:
            b = best[r["name"]]; b["geometry"] += r["geometry"]
            if r["distance_m"] < b["distance_m"]: b.update({k: r[k] for k in ("side", "distance_m", "adjacent", "label_lon", "label_lat")})
    scan["roads"] = sorted(best.values(), key=lambda r: r["distance_m"])

    # water
    for key, label in (("streams_named", "named"), ("streams_all", "unnamed")):
        for f in county_query(cfg, key, envq, "scan").get("features", []):
            g = shape(f["geometry"]); name = (f["properties"].get(F.get("stream_name", ""), "") or "").strip() if label == "named" else ""
            crosses = g.intersects(poly)
            d = 0 if crosses else round(min(meters(Point(p), c) for p in (g.coords if isinstance(g, LineString) else [q for l in g.geoms for q in l.coords])))
            if crosses or d < 400:
                scan["water"].append({"name": name.title() or "unnamed drainage", "type": "stream", "crosses_parcel": crosses, "distance_m": d, "side": bearing(c, g.centroid),
                                      **label_point(g, poly)})
    seen = set(); scan["water"] = [w for w in scan["water"] if not (w["name"] in seen or seen.add(w["name"]))]

    # buildings (footprint layer may not cover rural areas)
    bl = county_query(cfg, "buildings", envq, "scan").get("features", [])
    inside = [f for f in bl if shape(f["geometry"]).intersects(poly)]
    scan["buildings"] = {"layer_features_nearby": len(bl), "inside_parcel": len(inside), "coverage_note": "footprint layer covers Quincy tri-township only; 0 may mean no coverage",
                         "inside_points": building_points(inside, poly)}

    # flood
    fz = []
    for f in county_query(cfg, "firm_zones", {**envq, "outFields": f"{F['firm_zone']},{F['firm_sfha']}"}, "scan").get("features", []):
        if shape(f["geometry"]).intersects(poly):
            fz.append({"zone": f["properties"].get(F["firm_zone"]), "sfha": f["properties"].get(F["firm_sfha"])})
    scan["flood"] = {"zones_touching_parcel": fz}

    # neighbors' use
    nb = county_query(cfg, "parcels", {**envq, "f": "json", "outFields": f"{cfg['pin_field']},{F['use']},{F['acres']}", "resultRecordCount": 80}, "scan").get("features", [])
    for f in nb:
        at = f.get("attributes", {})
        if str(at.get(cfg["pin_field"])) == str(parcel["apn"]) or not f.get("geometry", {}).get("rings"): continue
        g = unary_union([Polygon([(x, y) for x, y, *_ in ring]) for ring in f["geometry"]["rings"] if len(ring) >= 4])
        if g.distance(poly) * 111320 < 60:
            u = str(at.get(F["use"]) or "")
            scan["neighbors"].append({"side": bearing(c, g.centroid), "use": cfg.get("use_codes", {}).get(u, f"code {u}"), "acres": at.get(F["acres"]),
                                      "pin": str(at.get(cfg["pin_field"]) or ""), "geometry": neighbor_geom(g)})

    # POIs + land cover
    try:
        scan["pois"] = ls.nearby_pois(c.y, c.x, radius_m=2500)[:5]; log("scan", "Google Places nearby", n=len(scan["pois"]))
    except Exception as e:
        log("scan", "Places failed", err=str(e))
    scan["landcover"] = landcover_stats(sat_wide, frame_wide, poly)
    osm_features(scan, poly)
    return scan


def scan_federal(fed, cfg, parcel, sat_wide, frame_wide):
    """Surroundings scan from nationwide layers: TIGER roads, NHD water, FEMA flood zones, USA Structures, parcel-layer neighbors."""
    poly = parcel["polygon"]; c = poly.centroid
    minx, miny, maxx, maxy = poly.bounds; pad = 0.004
    env = json.dumps({"xmin": minx - pad, "ymin": miny - pad, "xmax": maxx + pad, "ymax": maxy + pad, "spatialReference": {"wkid": 4326}})
    envq = {"geometry": env, "geometryType": "esriGeometryEnvelope", "inSR": 4326, "spatialRel": "esriSpatialRelIntersects"}
    scan = {"roads": [], "water": [], "buildings": {}, "flood": {}, "neighbors": [], "pois": []}
    def safe(fn, what):
        try: return fn()
        except Exception as e: log("scan", f"{what} unavailable", err=str(e)[:80]); return {}
    # roads — TIGERweb splits roads across layers (primary / secondary / local); query each, lean fields, short timeout
    rname = fed["roads"]["name"]
    for url in fed["roads"].get("urls") or [fed["roads"]["url"]]:
        d = safe(lambda: layer_query(url, {**envq, "outFields": rname}, "scan", timeout=25), "TIGER roads")
        for f in d.get("features", []):
            g = shape(f["geometry"]); name = (f["properties"].get(rname) or "").strip()
            if not name or g.is_empty: continue
            lines = [g] if isinstance(g, LineString) else list(getattr(g, "geoms", []))
            near = min((l.interpolate(l.project(c)) for l in lines), key=lambda p: meters(p, c))
            dist = min(meters(Point(q), poly.exterior.interpolate(poly.exterior.project(Point(q)))) for l in lines for q in l.coords)
            scan["roads"].append({"name": road_title(name), "side": bearing(c, near), "distance_m": round(dist), "adjacent": dist < 25,
                                  "label_lon": near.x, "label_lat": near.y, "geometry": [[[round(x, 6), round(y, 6)] for x, y, *_ in l.coords] for l in lines]})
    best = {}
    for r in scan["roads"]:
        if r["name"] not in best: best[r["name"]] = r
        else:
            b = best[r["name"]]; b["geometry"] += r["geometry"]
            if r["distance_m"] < b["distance_m"]: b.update({k: r[k] for k in ("side", "distance_m", "adjacent", "label_lon", "label_lat")})
    scan["roads"] = sorted(best.values(), key=lambda r: r["distance_m"])
    # water: streams + waterbodies
    for key, label in (("streams", "creek"), ("waterbodies", "pond")):
        d = safe(lambda: layer_query(fed[key]["url"], {**envq, "outFields": fed[key]["name"]}, "scan", timeout=25), f"NHD {key}")
        for f in d.get("features", []):
            g = shape(f["geometry"]); name = (f["properties"].get(fed[key]["name"]) or "").strip()
            if g.is_empty: continue
            crosses = g.intersects(poly); dist = 0 if crosses else round(g.distance(poly) * 111320)
            if crosses or dist < 400:
                scan["water"].append({"name": name.title() or f"unnamed {label}", "crosses_parcel": crosses, "distance_m": dist, "side": bearing(c, g.centroid)})
    seen = set(); scan["water"] = [w for w in scan["water"] if not (w["name"] in seen or seen.add(w["name"]))]
    # buildings
    d = safe(lambda: layer_query(fed["buildings"]["url"], {**envq, "outFields": "OBJECTID"}, "scan", timeout=25), "USA Structures")
    bl = d.get("features", []); inside = [f for f in bl if shape(f["geometry"]).intersects(poly)]
    scan["buildings"] = {"layer_features_nearby": len(bl), "inside_parcel": len(inside), "coverage_note": "USA Structures (nationwide footprints)",
                         "inside_points": building_points(inside, poly)}
    # flood
    d = safe(lambda: layer_query(fed["flood"]["url"], {**envq, "outFields": f"{fed['flood']['zone']},{fed['flood']['sfha']}"}, "scan", timeout=25), "FEMA NFHL")
    fz = [{"zone": f["properties"].get(fed["flood"]["zone"]), "sfha": f["properties"].get(fed["flood"]["sfha"])} for f in d.get("features", []) if shape(f["geometry"]).intersects(poly)]
    scan["flood"] = {"zones_touching_parcel": fz}
    # neighbors from the same parcel layer
    if cfg.get("parcels_url"):
        usef = cfg["fields"].get("use")
        d = safe(lambda: layer_query(cfg["parcels_url"], {**envq, "outFields": cfg["pin_field"] + (f",{usef}" if usef else ""), "resultRecordCount": 60}, "scan", timeout=25), "neighbors")
        for f in d.get("features", []):
            if str(f["properties"].get(cfg["pin_field"])) == str(parcel["apn"]): continue
            g = shape(f["geometry"])
            if not g.is_empty and g.distance(poly) * 111320 < 60:
                u = str(f["properties"].get(usef) or "").strip() if usef else ""
                scan["neighbors"].append({"side": bearing(c, g.centroid), "use": cfg.get("use_codes", {}).get(u, u.lower() if not u.isdigit() else f"use code {u}"),
                                          "pin": str(f["properties"].get(cfg["pin_field"]) or ""), "geometry": neighbor_geom(g)})
    try:
        scan["pois"] = ls.nearby_pois(c.y, c.x, radius_m=2500)[:5]; log("scan", "Google Places nearby", n=len(scan["pois"]))
    except Exception as e:
        log("scan", "Places failed", err=str(e))
    scan["landcover"] = landcover_stats(sat_wide, frame_wide, poly)
    osm_features(scan, poly)
    return scan


def label_point(g, poly, max_m=60):
    """Where to put a callout for geometry g relative to the parcel: inside the parcel if it crosses, else the nearest point (within max_m)."""
    try:
        if g.intersects(poly):
            inter = g.intersection(poly); p = inter.representative_point() if not inter.is_empty else g.representative_point()
            return {"label_lon": p.x, "label_lat": p.y}
        p = g.interpolate(g.project(poly.centroid)) if hasattr(g, "project") else g.representative_point()
        if p.distance(poly) * 111320 <= max_m:
            return {"label_lon": p.x, "label_lat": p.y}
    except Exception:
        pass
    return {}


def neighbor_geom(g, tol=0.000005):
    """Outer rings of a neighbor parcel, simplified, for the plat."""
    polys = [g] if g.geom_type == "Polygon" else list(getattr(g, "geoms", []))
    return [[[round(x, 6), round(y, 6)] for x, y, *_ in p.simplify(tol).exterior.coords] for p in polys if p.geom_type == "Polygon"]


def building_points(features, poly):
    pts = []
    for f in features:
        try:
            g = shape(f["geometry"]).intersection(poly)
            if not g.is_empty: p = g.representative_point(); pts.append([p.x, p.y, round(shape(f["geometry"]).area * 111320 * 110540)])
        except Exception:
            pass
    return sorted(pts, key=lambda p: -p[2])            # biggest footprint first


def osm_features(scan, poly, pad=0.004):
    """OpenStreetMap power lines and driveways/tracks around the parcel (Overpass, with mirrors). Fails soft."""
    minx, miny, maxx, maxy = poly.bounds
    bb = f"({miny-pad},{minx-pad},{maxy+pad},{maxx+pad})"
    q = f'[out:json][timeout:15];(way["power"~"line|minor_line"]{bb};way["highway"~"service|track|driveway"]{bb};);out geom;'
    body = urllib.parse.urlencode({"data": q}).encode()
    scan["powerlines"], scan["powerline_points"], scan["driveways"], scan["driveway_points"] = [], [], [], []
    errs = []
    for host in OVERPASS_HOSTS:                                   # the main server is often at capacity (502) → try mirrors
        try:
            d = ls.http_json(f"https://{host}/api/interpreter", data=body, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=8)
            for w in d.get("elements", []):
                line = [[n["lon"], n["lat"]] for n in w.get("geometry", [])]
                if len(line) < 2: continue
                tags = w.get("tags", {}); g = LineString(line)
                if "power" in tags:
                    scan["powerlines"].append(line); lp = label_point(g, poly, 40)
                    if lp: scan["powerline_points"].append([lp["label_lon"], lp["label_lat"]])
                else:
                    if g.intersects(poly) or g.distance(poly) * 111320 < 15:
                        scan["driveways"].append(line); lp = label_point(g, poly, 15)
                        if lp: scan["driveway_points"].append([lp["label_lon"], lp["label_lat"]])
            log("scan", "OSM features", host=host, power=len(scan["powerlines"]), driveways=len(scan["driveways"])); return
        except Exception as e:
            errs.append(f"{host}: {str(e)[:40]}")
    log("scan", "OSM features unavailable", err="; ".join(errs)[:160])


OVERPASS_HOSTS = ["overpass-api.de", "overpass.private.coffee", "overpass.kumi.systems"]
REGION = {**dict.fromkeys("IL IN IA KS MI MN MO NE ND OH SD WI".split(), "rural Midwestern"),
          **dict.fromkeys("NC SC GA FL AL MS TN KY AR LA".split(), "rural Southeastern US"),
          **dict.fromkeys("VA WV MD DE PA NJ NY".split(), "rural Mid-Atlantic"),
          **dict.fromkeys("TX OK NM AZ".split(), "rural Southwestern US"),
          **dict.fromkeys("CO WY MT ID UT NV".split(), "rural Mountain West"),
          **dict.fromkeys("WA OR CA".split(), "rural West Coast"),
          **dict.fromkeys("ME NH VT MA CT RI".split(), "rural New England")}


def scene_description(parcel, scan):
    """Plain-English scene facts for the prompt + a do-not-invent list."""
    lc = scan.get("landcover", {})
    region = REGION.get((parcel.get("state") or "").upper(), "rural US")
    parts = [f"a {parcel['acres']:.1f}-acre parcel, {parcel.get('use_desc', 'land')}, in a {region} setting"]
    if lc:
        dom = max(lc, key=lambda k: lc[k] if k != "pixels" else -1)
        words = {"tree_canopy": "mostly wooded with mature tree canopy", "open_green": "mostly open green pasture or lawn",
                 "bare_or_crop_stubble": "mostly open farmland (bare soil or crop stubble)", "gray_built_or_road": "largely paved or built"}
        parts.append(words[dom] + f" ({int(lc[dom]*100)}% of the parcel)")
        if lc.get("tree_canopy", 0) > 0.2 and dom != "tree_canopy": parts.append(f"with tree cover on about {int(lc['tree_canopy']*100)}% of it")
    adj = [r for r in scan["roads"] if r["adjacent"]]
    if adj: parts.append("bordered by " + ", ".join(f"{r['name']} on the {r['side']} side" for r in adj))
    far = [r for r in scan["roads"] if not r["adjacent"]][:2]
    if far: parts.append("with " + ", ".join(f"{r['name']} about {r['distance_m']} m to the {r['side']}" for r in far))
    w = [x for x in scan["water"] if x["crosses_parcel"]]
    parts.append(("a creek (" + ", ".join(x["name"] for x in w) + ") crosses the parcel") if w else "no ponds, lakes or creeks on the parcel")
    b = scan.get("buildings", {}).get("inside_parcel", 0)
    parts.append(f"{b} small outbuilding{'s' if b != 1 else ''} on the parcel" if b else "no buildings on the parcel")
    if scan.get("driveways"): parts.append("a gravel driveway enters the parcel")
    if scan.get("powerlines"): parts.append("an overhead electric line on wooden poles runs past the parcel")
    nb = {}
    for n in scan["neighbors"]: nb.setdefault(n["use"], set()).add(n["side"])
    nb.pop("", None)
    if nb: parts.append("neighbors: " + "; ".join(f"{u} to the {'/'.join(sorted(s))}" for u, s in nb.items()))
    desc = ". ".join(parts) + "."
    forbid = ["no water bodies unless listed above", "no buildings beyond those listed above, no new roads or driveways", "no vehicles, people, animals or signs",
              "no text, labels, watermarks or map pins"]
    return desc, forbid


# ---------- 3. references ----------
def draw_fiducial(sat, frame, poly, width=7):
    img = sat.copy(); d = ImageDraw.Draw(img)
    pts = [frame.px(c[0], c[1]) for c in poly.exterior.coords]
    d.line(pts + [pts[0]], fill=FIDUCIAL + (255,), width=width, joint="curve")
    return img


def fiducial_static_url(frame, poly, roads=None, power=None, weight=4):
    """Public Static Maps URL with the boundary (magenta) and up to two roads (cyan, blue) drawn server-side (path=)."""
    coords = list(poly.simplify(0.00002).exterior.coords)
    paths = [f"color:0xff00ffff|weight:{weight}|" + "|".join(f"{c[1]:.6f},{c[0]:.6f}" for c in coords)]
    from shapely.geometry import box
    minx, miny, maxx, maxy = frame_bounds(frame)
    clip = box(minx - 0.0005, miny - 0.0005, maxx + 0.0005, maxy + 0.0005)
    for r, (rgb, hx, _) in zip(roads or [], ROAD_COLORS):
        merged = unary_union([LineString(l) for l in r["geometry"] if len(l) >= 2]).intersection(clip)
        parts = [merged] if isinstance(merged, LineString) else list(getattr(merged, "geoms", []))
        parts = sorted((g for g in parts if isinstance(g, LineString)), key=lambda g: g.distance(poly))[:3]   # nearest to the parcel
        for g in parts:
            pts = list(g.simplify(0.00025).coords)[:10]                    # keep the URL short: the model's fetch fails past ~1.5k chars
            if len(pts) >= 2:
                paths.append(f"color:0x{hx}ff|weight:2|" + "|".join(f"{y:.5f},{x:.5f}" for x, y in pts))
    for line in (power or []):
        paths.append(f"color:0x{POWER_COLOR[1]}ff|weight:3|" + "|".join(f"{y:.5f},{x:.5f}" for x, y in line[:60]))
    q = [("center", f"{frame.lat},{frame.lon}"), ("zoom", frame.z), ("size", f"{frame.w}x{frame.h}"), ("scale", ls.SCALE),
         ("maptype", "satellite"), ("format", "png")] + [("path", pth) for pth in paths] + [("key", os.environ["GOOGLE_MAPS_KEY"])]
    return "https://maps.googleapis.com/maps/api/staticmap?" + urllib.parse.urlencode(q)


def frame_bounds(frame):
    """lon/lat bounds of a Frame."""
    inv = lambda px, py: ls_px_to_lonlat(frame, px, py)
    (minx, maxy), (maxx, miny) = inv(0, 0), inv(frame.W, frame.H)
    return minx, miny, maxx, maxy


def ls_px_to_lonlat(frame, px, py):
    n = 256 * (2 ** frame.z)
    wx = frame.cx + (px - frame.W / 2) / ls.SCALE; wy = frame.cy + (py - frame.H / 2) / ls.SCALE
    lon = wx / n * 360 - 180
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * wy / n))))
    return lon, lat


def draw_lines(img, frame, roads=None, power=None, width=3):
    """Draw road / powerline fiducials locally (for rotated references)."""
    d = ImageDraw.Draw(img)
    for r, (rgb, _, _) in zip(roads or [], ROAD_COLORS):
        for line in r["geometry"]:
            pts = [frame.px(x, y) for x, y in line]
            if len(pts) >= 2: d.line(pts, fill=rgb + (255,), width=width)
    for line in (power or []):
        pts = [frame.px(x, y) for x, y in line]
        if len(pts) >= 2: d.line(pts, fill=POWER_COLOR[0] + (255,), width=width)
    return img


def rotated_reference(frame_sq, poly, angle_deg, roads=None, power=None):
    """Square Static Maps fiducial rotated so that the compass direction `angle_deg` (camera position) is at the BOTTOM
    of the frame (image top = far side). Returns a 1280x960 RGBA image. 0 = camera north of parcel looking south."""
    url = fiducial_static_url(frame_sq, poly, roads, power)
    sq = Image.open(io.BytesIO(ls.http_bytes(url))).convert("RGBA")          # 1280x1280, north-up
    # north-up: bottom of frame = south (180). PIL rotates CCW by phi → bottom becomes 180+phi, so phi = camera_bearing - 180.
    rot = sq.rotate(angle_deg - 180, resample=Image.BICUBIC, expand=False)
    W, H = rot.size
    side = int(W / (abs(math.cos(math.radians(angle_deg))) + abs(math.sin(math.radians(angle_deg)))))  # inscribed valid square
    cw, ch = side, int(side * 0.75)
    crop = rot.crop(((W - cw) // 2, (H - ch) // 2, (W + cw) // 2, (H + ch) // 2))
    return crop.resize((1280, 960), Image.LANCZOS), url


# ---------- 5. prompts ----------
def prompt_for(view, light, desc, forbid, roads=None, power=None):
    close = view.endswith("_close"); low = view.endswith("_low"); view = view.split("_")[0]
    lightw = {"day": "bright midday sunlight, clear blue sky, natural colors",
              "golden": ("believable late-afternoon golden hour about 45 minutes before sunset: warm low sun from the west, soft natural "
                         "shadows, gently warm tones, clear sky with only a faint warm tint near the horizon. Subtle and realistic like an "
                         "unedited drone photo: no dramatic orange or pink sky, no sun disc, no lens flare, no glow, no haze, no HDR look, "
                         "no oversaturation")}[light]
    scene = f"Scene facts (keep them true): {desc} " + "; ".join(forbid) + "."
    fid = ("A thin magenta line is painted on the ground marking the property boundary" + (", and a thin cyan line is painted along the "
           "centerline of the nearby road" if roads else "") + (", and a thin orange line follows the overhead power line" if power else "") +
           ". Keep each painted line exactly where it is, following the ground in perspective, same color, thin and unbroken, "
           "no glow, no lights; they are part of the scene.")
    keep = ("Keep every road, field edge, tree line, fence, driveway and building exactly where it is in the source image; "
            "do not add, move or remove anything. Photorealistic, sharp, 24mm lens.")
    if view == "nadir":
        cam = f"Turn this satellite image into a crisp professional drone photograph taken straight down from {'250' if close else '400'} ft, same framing and scale."
    elif low:
        cam = ("Turn this satellite image into a photograph taken from a drone hovering at TREETOP HEIGHT, about 40 ft above the ground, just beyond the BOTTOM edge "
               f"of this image (the {dr.view_name(view)} side of the property), camera nearly level, tilted only 8 degrees below the horizon, looking across the land "
               "toward the top of the image. This is a near-ground view: the horizon is about one third from the top of the frame with a wide sky above it, nearby "
               "trees and grass are large in the foreground, the far side of the property is small and distant, strong perspective compression. "
               "The magenta boundary stays fully inside the frame.")
    else:
        cam = ("Turn this satellite image into a realistic drone photograph. The drone hovers just beyond the BOTTOM edge of this image "
               f"at about 300 ft (that is the {dr.view_name(view)} side of the property), camera tilted 35 degrees down, looking toward the "
               "top of the image so the top of the source becomes the far distance with the horizon visible. "
               "The magenta boundary stays fully inside the frame." + (" The property fills most of the frame; the camera is closer, about 200 ft up." if close else ""))
    return f"{cam} {lightw}. {fid} {keep} {scene}"


# ---------- 6. fiducial detection ----------
def dr_has_marker(img):
    pts, _ = detect_fiducial(img); return pts is not None


def detect_fiducial(img):
    """Find the magenta boundary in a render → polygon (px) or None."""
    bgr = cv2.cvtColor(np.array(img.convert("RGB")), cv2.COLOR_RGB2BGR)
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    m = cv2.inRange(hsv, (135, 80, 90), (175, 255, 255))           # magenta hue band
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    m = cv2.dilate(m, np.ones((5, 5), np.uint8))
    cnts, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return None, {"reason": "no magenta found"}
    c = max(cnts, key=cv2.contourArea)
    area = cv2.contourArea(c); hull = cv2.convexHull(c)
    if area < 0.02 * img.width * img.height:
        return None, {"reason": f"magenta region too small ({int(area)} px)"}
    bx, by, bw, bh = cv2.boundingRect(c)
    if bw < 0.22 * img.width or bh < 0.12 * img.height:
        return None, {"reason": f"magenta region too narrow ({bw}x{bh} px)"}
    approx = cv2.approxPolyDP(c, 0.01 * cv2.arcLength(c, True), True).reshape(-1, 2)
    if len(approx) < 3:
        return None, {"reason": "degenerate polygon"}
    return [(float(x), float(y)) for x, y in approx], {"area_px": int(area), "vertices": int(len(approx)),
                                                        "magenta_px": int((m > 0).sum()), "_mask": m}


def detect_lines(img, hue_band, min_px=400):
    """Find marker lines of one color in a render → list of (centroid_xy, mask) per connected component."""
    bgr = cv2.cvtColor(np.array(img.convert("RGB")), cv2.COLOR_RGB2BGR)
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    m = cv2.inRange(hsv, (hue_band[0], 60, 100), (hue_band[1], 255, 255))
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    n, lab, stats, cents = cv2.connectedComponentsWithStats(m)
    comps = [(tuple(cents[i]), (lab == i).astype(np.uint8) * 255) for i in range(1, n) if stats[i, cv2.CC_STAT_AREA] >= min_px]
    return comps, m


def erase_markers(img, mask, roads=None, power=None, dilate=7, radius=5):
    """Detect road / power markers (labels), erase every marker by inpainting. Returns (clean image, labels[(xy, text)]).
    dilate/radius: how far around the marker to repaint — keep small for thin markers (real photos) or the band shows as a smear."""
    labels = []
    erase = mask.copy() if mask is not None else np.zeros((img.height, img.width), np.uint8)
    for r, (_, _, band) in zip(roads or [], ROAD_COLORS):
        comps, m = detect_lines(img, band); erase = cv2.bitwise_or(erase, m)
        if comps:
            labels.append((max(comps, key=lambda c: int(c[1].sum()))[0], r["name"].upper()))
    if power:
        comps, m = detect_lines(img, POWER_COLOR[2]); erase = cv2.bitwise_or(erase, m)
        if comps:
            labels.append((max(comps, key=lambda c: int(c[1].sum()))[0], "ELECTRIC LINES"))
    out = img.copy()
    if erase.any():
        bgr = cv2.cvtColor(np.array(out.convert("RGB")), cv2.COLOR_RGB2BGR)
        bgr = cv2.inpaint(bgr, cv2.dilate(erase, np.ones((dilate, dilate), np.uint8)), radius, cv2.INPAINT_TELEA)
        out = Image.fromarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)).convert("RGBA")
    return out, labels


def geometric_labels(pts, roads, camera_bearing, offset_px=46):
    """Place a label for each adjacent road at the polygon edge facing the road's compass side.
    In a render, image-up = far side = camera_bearing + 180; a compass bearing b maps to angle b - far."""
    far = (camera_bearing + 180) % 360
    cx = sum(x for x, _ in pts) / len(pts); cy = sum(y for _, y in pts) / len(pts)
    out = []
    for r in roads:
        if r.get("distance_m", 999) > 80: continue
        b = COMPASS[r["side"]]; th = math.radians(b - far)
        dx, dy = math.sin(th), -math.cos(th)
        best = max(pts, key=lambda q: (q[0] - cx) * dx + (q[1] - cy) * dy)
        out.append(((best[0] + dx * offset_px, best[1] + dy * offset_px), r["name"].upper()))
    return out


def draw_graphics(img, pts, parcel, labels=()):
    """Styled outline + fill, road/power labels, pin + acreage card, disclaimer — on an already-clean image."""
    ls.PLACED.clear()
    out = img.copy()
    fill = Image.new("RGBA", out.size, (0, 0, 0, 0)); ImageDraw.Draw(fill).polygon(pts, fill=(255, 255, 255, 34)); out.alpha_composite(fill)
    def fn(d, shadow, off):
        q = [(x + off[0], y + off[1]) for x, y in pts]; d.line(q + [q[0]], fill=shadow or (255, 255, 255, 255), width=5, joint="curve")
    ls.shadowed(out, fn, blur=4, offset=(0, 2), alpha=170)
    pf = PolyFrame(pts, out.width, out.height)
    ls.draw_headline(out, pf, parcel)
    for (x, y), text in labels:
        x = min(max(x, 110), out.width - 110); y = min(max(y, 90), out.height - 110)     # keep the card on-frame
        box, _, _ = ls.measure_box(out, (x, y - 24), [text], [26], (16, 8), "top")
        if ls.overlaps(box, ls.PLACED): y += 120
        ls.draw_label(out, (x, y), text, size=26)
    ls.draw_disclaimer(out)
    return out


def render_qa(img, pts, scan):
    """Catch renders where the model replaced the parcel's land cover (e.g. woods → open pasture). Returns None when fine, else a reason."""
    lc = scan.get("landcover") or {}
    if not lc or not pts: return None
    mask = Image.new("L", img.size, 0); ImageDraw.Draw(mask).polygon(pts, fill=255); m = np.array(mask) > 0
    if m.sum() < 500: return None
    hsv = cv2.cvtColor(np.array(img.convert("RGB")), cv2.COLOR_RGB2HSV)
    h, s, v = (hsv[..., i].astype(int) for i in range(3))
    grass = ((h > 25) & (h < 95) & (s > 60) & (v > 135) & m).sum() / m.sum()
    dark = ((v < 110) & m).sum() / m.sum()
    if lc.get("tree_canopy", 0) >= 0.7 and grass >= 0.5: return f"wooded parcel rendered as open ground ({int(grass*100)}% grass)"
    if lc.get("open_green", 0) + lc.get("bare_or_crop_stubble", 0) >= 0.7 and dark >= 0.6: return f"open parcel rendered as woods ({int(dark*100)}% dark)"
    return None


def crop_to_parcel(img, pts, margin, out_size=(1152, 864)):
    """Crop around the parcel polygon (bbox + margin), keep 4:3, resize. Returns (image, fn mapping old px → new px)."""
    xs, ys = zip(*pts); x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    w, h = (x1 - x0) * (1 + 2 * margin), (y1 - y0) * (1 + 2 * margin)
    if w / h < 4 / 3: w = h * 4 / 3
    else: h = w * 3 / 4
    w, h = min(w, img.width), min(h, img.height)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    left = min(max(0, cx - w / 2), img.width - w); top = min(max(0, cy - h / 2), img.height - h)
    crop = img.crop((int(left), int(top), int(left + w), int(top + h)))
    sx, sy = out_size[0] / crop.width, out_size[1] / crop.height
    return crop.resize(out_size, Image.LANCZOS), (lambda x, y: ((x - left) * sx, (y - top) * sy))


class PolyFrame:
    """Frame stand-in for compose(): the parcel polygon is already known in render pixels."""
    def __init__(self, pts, W, H):
        self.pts, self.W, self.H = pts, W, H
        self.cx = sum(p[0] for p in pts) / len(pts); self.cy = sum(p[1] for p in pts) / len(pts)

    def px(self, lon, lat):   # only ever asked for the centroid by draw_headline
        return (self.cx, self.cy)

    def inside(self, lon, lat, pad=40): return False
    def bbox_px(self, poly):
        xs, ys = zip(*self.pts); return (min(xs), min(ys), max(xs), max(ys))




# ---------- main ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--county"); ap.add_argument("--pin"); ap.add_argument("--lat", type=float); ap.add_argument("--lon", type=float)
    ap.add_argument("--boundary", help="KML/KMZ/GeoJSON/zipped Shapefile of the parcel (e.g. Land id export)"); ap.add_argument("--label", help="address/name for the card")
    ap.add_argument("--regrid", action="store_true"); ap.add_argument("--apn"); ap.add_argument("--path"); ap.add_argument("--address")
    ap.add_argument("--state", help="two-letter state (Realie)"); ap.add_argument("--county-name", help="county name (Realie)")
    ap.add_argument("--angles", default="S,E,W,N"); ap.add_argument("--lights", default="golden,day")
    ap.add_argument("--low", default="auto", help="low horizon views: 'auto' = road side + opposite, 'none', or e.g. S,N")
    ap.add_argument("--model", default="grok"); ap.add_argument("--res", default="2k", choices=["1k", "2k"]); ap.add_argument("--no-render", action="store_true"); ap.add_argument("--force", action="store_true")
    ap.add_argument("--out"); ap.add_argument("--env", default=str(HERE / ".env"))
    ap.add_argument("--finalize", help="re-draw every image from out/<PIN>/layout.json (after edits) and rebuild the packet; no API calls")
    a = ap.parse_args()
    ls.load_env(a.env)
    t0 = time.time()
    if a.finalize:
        zp, n = finalize(Path(a.finalize)); print(f"done in {round(time.time()-t0)}s → {zp}  ({n} images)"); return

    # 1 parcel
    print("1/7 parcel", flush=True)
    import sources as S
    sources = S.load_sources(); fed = sources["_federal"]
    cfg = sources.get(a.county or "regrid")
    if a.boundary:
        import boundary_file
        bpoly, bprops = boundary_file.read_boundary(a.boundary)
        parcel = ls.parcel_from_feature({"geometry": bpoly.__geo_interface__, "properties": {"PIN": Path(a.boundary).stem, "SiteAddress": a.label or ""}}, {})
        c0 = bpoly.centroid; rg = reverse_geocode(c0.y, c0.x)
        parcel["county"] = rg.get("county", ""); parcel["city"] = rg.get("city", ""); parcel["zip"] = rg.get("zip", "")
        parcel["state"] = rg.get("state", "")
        parcel["situs"] = a.label or rg.get("street", ""); parcel["use_desc"] = "land"
        cfg = {"name": "boundary file", "kind": "file", "scan": "federal", "fields": {}, "defaults": {}}
        log("parcel", "boundary file", file=a.boundary, acres=parcel["acres"])
    elif cfg is not None and cfg.get("kind") == "realie":
        parcel = fetch_parcel_realie(a.pin, a.state, a.county_name, a.address, a.lat, a.lon)
        log("parcel", "Realie", apn=parcel["apn"], county=parcel["county"])
    elif a.regrid or cfg is None or cfg.get("kind") == "regrid":
        parcel = ls.fetch_parcel(a.pin or a.apn, a.path, a.address, a.lat, a.lon); cfg = sources["regrid"]
        c0 = parcel["polygon"].centroid; rg = reverse_geocode(c0.y, c0.x)
        parcel["county"] = parcel["county"] or rg.get("county", ""); parcel["city"] = parcel["city"] or rg.get("city", "")
        parcel["use_desc"] = parcel.get("use") or "land"
        log("parcel", "Regrid", apn=parcel["apn"])
    else:
        parcel = fetch_parcel_county(cfg, a.pin, a.lat, a.lon, a.county_name)
    poly = parcel["polygon"]; c = poly.centroid
    out = Path(a.out or f"out/{str(parcel['apn']).replace('/', '_').replace(' ', '_')}"); out.mkdir(parents=True, exist_ok=True); (out / "ai").mkdir(exist_ok=True)
    (out / "parcel.geojson").write_text(json.dumps(parcel.get("feature") or {"type": "Feature", "geometry": poly.__geo_interface__, "properties": parcel["raw"]}))
    print(f"    {parcel['apn']} | {parcel['situs']} {parcel['city']} {parcel['state']} | {parcel['acres']} ac | {parcel.get('use_desc', parcel['use'])}")

    # 3 references (needed by the scan for land cover)
    print("3/7 satellite references", flush=True)
    z = ls.choose_zoom(poly, 640, 480, margin=0.22); frame = ls.Frame(c.y, c.x, z, 640, 480)
    ref = ls.fetch_satellite(frame); log("satellite", "Static Maps 640x480@2", zoom=z)
    ref.convert("RGB").save(out / "ref_clean.png")
    fid_url = None   # built after the scan (needs road geometry)

    # 2 scan
    print("2/7 surroundings scan", flush=True)
    if cfg.get("scan") == "county":
        scan = scan_surroundings(cfg, parcel, ref, frame)
    else:
        scan = scan_federal(fed, cfg, parcel, ref, frame)
    desc, forbid = scene_description(parcel, scan)
    import plat; scan["plss"] = plat.plss_lookup(c.x, c.y); log("scan", "PLSS", **({"str": scan["plss"].get("text")} if scan["plss"] else {"note": "unavailable"}))
    if not parcel.get("acres"): parcel["acres"] = round(ls.acres_from_polygon(poly), 2)
    scan["scene_description"] = desc; scan["forbid"] = forbid
    (out / "scan.json").write_text(json.dumps(scan, indent=2, default=str))
    print("    " + desc)
    fid_roads = [r for r in scan["roads"] if r["distance_m"] < 250][:1]           # roads worth labeling on the renders
    power = scan.get("powerlines", [])
    fid_url = fiducial_static_url(frame, poly, [], [])
    fid = Image.open(io.BytesIO(ls.http_bytes(fid_url))).convert("RGBA"); fid.convert("RGB").save(out / "ref_fiducial.png")
    log("satellite", "Static Maps fiducial (boundary + roads)", roads=[r["name"] for r in fid_roads])

    # 4 layout + phase 1 (annotated satellite)
    print("4/7 layout + satellite set", flush=True)
    ref_map = L.FrameMap(frame)
    layout = L.build_layout(parcel, scan, ref_map, ref)
    (out / "layout.json").write_text(json.dumps(layout, indent=1))
    log("layout", "features", labels=[l["text"] for l in layout["labels"]])
    maps = {"_meta": {"model": a.model, "angles": [], "lights": [], "low": []}}
    zt = ls.choose_zoom(poly); zc = ls.choose_zoom(poly, 640, 480)
    frames = {"cover": ls.Frame(c.y, c.x, zc, 640, 480), "tight": ls.Frame(c.y, c.x, zt), "wide": ls.Frame(c.y, c.x, zt - 1)}
    for k, fr in frames.items():
        sat = ls.fetch_satellite(fr); log("satellite", f"Static Maps {k}", zoom=fr.z)
        sat.convert("RGB").save(out / f"p1_{k}_clean.png")
        maps[f"p1_{k}"] = {"src": f"p1_{k}_clean.png", "map": L.FrameMap(fr).to_json(), "kind": "satellite", "pois": k == "wide"}
    save_state(out, parcel, maps)
    if a.no_render:
        zp, n = finalize(out); (out / "run.json").write_text(json.dumps(LOG, indent=2)); print(f"done (phase 1 only) → {zp}  ({n} images)"); return

    # 5 render
    print("5/7 AI renders", flush=True)
    angles = [v.strip().upper() for v in a.angles.split(",") if v.strip()]
    lights = [l.strip() for l in a.lights.split(",") if l.strip()]
    if a.low == "auto":
        adj = [r for r in scan["roads"] if r.get("distance_m", 999) < 60]
        side = {"NE": "E", "SE": "S", "SW": "W", "NW": "N"}.get(adj[0]["side"], adj[0]["side"]) if adj else "S"
        low = [side, {"N": "S", "S": "N", "E": "W", "W": "E"}[side]]
    else:
        low = [] if a.low == "none" else [v.strip().upper() for v in a.low.split(",") if v.strip()]
    maps["_meta"].update({"angles": angles, "lights": lights, "low": low})
    zs = ls.choose_zoom(poly, 480, 480, margin=0.20); frame_sq = ls.Frame(c.y, c.x, zs, 640, 640)
    ref_urls = {}
    rot_cache_f = out / "ref_rot_urls.json"
    rot_cache = json.loads(rot_cache_f.read_text()) if rot_cache_f.exists() and not a.force else {}
    def rotated_ref_url(base):
        """Public URL of the square reference rotated so `base` is at the bottom. Try a direct upload first; if the upload
        host is blocked, ask the image model for an exact rotation of the public Static Maps reference (cached 7 days)."""
        if base in rot_cache: return rot_cache[base]
        img, sq_url = rotated_reference(frame_sq, poly, COMPASS[base], [], [])
        img.convert("RGB").save(out / f"ref_fiducial_{base}.png")
        try:
            u = dr.hf_upload_png(img); log("render", "uploaded rotated reference", view=base)
        except Exception:
            turns = {"N": "180 degrees", "E": "90 degrees clockwise", "W": "90 degrees counter-clockwise"}
            if base not in turns:
                log("render", "diagonal angle needs the upload host; using north-up reference", view=base); return fid_url
            pr = (f"Rotate this exact image {turns[base]}. Output the identical image content rotated; do not redraw, restyle, "
                  "add or remove anything; keep the thin magenta line exactly as drawn.")
            saved = dict(dr.MODELS[a.model]["extra"]); dr.MODELS[a.model]["extra"] = {"resolution": "1k", "aspect_ratio": "1:1", "quality": "medium"}
            job = dr.hf_submit(a.model, pr, sq_url); res = dr.hf_wait_all({base: job})[base]
            dr.MODELS[a.model]["extra"] = saved
            if res.get("status") != "completed":
                log("render", "model rotation failed; using north-up reference", view=base); return fid_url
            u = res["images"][0]["url"]; log("render", "model-rotated reference", view=base, request_id=job["request_id"])
        rot_cache[base] = u; rot_cache_f.write_text(json.dumps(rot_cache)); return u

    def ref_for(view):
        """Reference URL for a view. North-up reference = camera on the south side (also used for nadir); E / W / N use a rotated copy."""
        base = view.split("_")[0]
        if base in ("nadir", "S"): return fid_url
        if base not in ref_urls: ref_urls[base] = rotated_ref_url(base)
        return ref_urls[base]
    plan = [("nadir", l) for l in lights]
    for v in angles:
        for l in lights:
            if l == "day" and v in ("NE", "SE", "SW", "NW"): continue       # diagonals golden-only
            plan.append((v, l))
    for v in low:
        for l in lights: plan.append((f"{v}_low", l))
    dr.MODELS[a.model]["extra"]["resolution"] = a.res
    jobs, prompts = {}, {}
    for v, l in plan:
        key = f"{a.model}_{v}_{l}"
        if (out / "ai" / f"{key}.jpg").exists() and not a.force: continue
        prompts[key] = prompt_for(v, l, desc, forbid, [], [])
        jobs[key] = dr.hf_submit(a.model, prompts[key], ref_for(v)); log("render", "submitted", key=key, request_id=jobs[key]["request_id"])
    (out / "ai" / "prompts.json").write_text(json.dumps(prompts, indent=2))
    def collect(jobs):
        """Download finished renders; return the keys whose render has no boundary marker (or failed)."""
        bad = []
        for key, st in (dr.hf_wait_all(jobs) if jobs else {}).items():
            if st.get("status") == "completed":
                img = dr.download(st["images"][0]["url"])
                pts_, _ = detect_fiducial(img); why = render_qa(img, pts_, scan) if pts_ else "no boundary marker in render"
                if not why:
                    img.convert("RGB").save(out / "ai" / f"{key}.jpg", quality=95); log("render", "downloaded", key=key)
                else:
                    log("render", "rejected: " + why, key=key); bad.append(key)
            else:
                log("render", "FAILED", key=key, status=st.get("status"), error=st.get("error")); bad.append(key)
        return bad
    bad = collect(jobs)
    if bad:   # retry once at 1k — the marker survives more reliably at the lower resolution
        saved = dict(dr.MODELS[a.model]["extra"]); dr.MODELS[a.model]["extra"]["resolution"] = "1k"
        retry = {k: dr.hf_submit(a.model, prompts[k], ref_for(k.split("_", 1)[1].rsplit("_", 1)[0])) for k in bad}
        for k, j in retry.items(): log("render", "retry at 1k", key=k, request_id=j["request_id"])
        dr.MODELS[a.model]["extra"] = saved
        still = collect(retry)
        for k in still: log("render", "gave up", key=k)

    # 6 marker → ground map on every render
    print("6/7 outline detection + ground fit", flush=True)
    for p in sorted((out / "ai").glob(f"{a.model}_*.jpg")):
        if p.stem.endswith("_clean") or p.stem.endswith("_outline"): continue
        try:   # one bad render is skipped, never fails the whole packet
            img = Image.open(p).convert("RGBA")
            pts, q = detect_fiducial(img)
            parts = p.stem.split("_"); view, light = parts[1], parts[-1]; is_low = "low" in parts
            rec = {"src": f"ai/{p.stem}_clean.jpg", "kind": "render", "view": view, "light": light, "low": is_low, "fiducial": {k: v for k, v in q.items() if k != "_mask"}}
            if pts:
                mask = q.pop("_mask", None)
                clean, _ = erase_markers(img, mask, [], [])
                clean.convert("RGB").save(out / "ai" / f"{p.stem}_clean.jpg", quality=95)
                init = L.FrameMap(frame) if view in ("S", "nadir") else L.RotMap(frame_sq, COMPASS.get(view, 180) - 180)
                pm, err, bpx = L.fit_render_map(init, poly, pts, img.width, img.height)   # same as before: fit to the detected polygon
                rec.update({"map": pm.to_json() if pm else None, "boundary_px": bpx, "fit_err_px": round(err, 1)})
                maps[p.stem] = rec
                print(f"    {p.stem}: marker ok, ground fit {'OK' if pm else 'weak'} ({err:.0f} px)")
            else:
                print(f"    {p.stem}: NO marker {rec['fiducial']}")
        except Exception as e:
            log("fit", "skipped render", key=p.stem, err=f"{type(e).__name__}: {str(e)[:160]}")
            print(f"    {p.stem}: skipped ({type(e).__name__}: {str(e)[:120]})")
    save_state(out, parcel, maps)

    # 7 draw + packet
    print("7/7 draw + packet", flush=True)
    zp, n = finalize(out)
    (out / "run.json").write_text(json.dumps({"parcel": {k: v for k, v in parcel.items() if k not in ("polygon", "raw", "feature")}, "plan": plan, "seconds": round(time.time() - t0), "calls": LOG}, indent=2))
    print(f"done in {round(time.time()-t0)}s → {zp}  ({n} images)")


# ---------- state + finalize (re-drawable from layout.json) ----------
def save_state(out, parcel, maps):
    (out / "maps.json").write_text(json.dumps(maps, indent=1))
    (out / "parcel.json").write_text(json.dumps({k: v for k, v in parcel.items() if k not in ("polygon", "raw", "feature")}, indent=1, default=str))


def load_state(out):
    out = Path(out)
    parcel = json.loads((out / "parcel.json").read_text())
    from shapely import force_2d
    parcel["polygon"] = force_2d(shape(json.loads((out / "parcel.geojson").read_text())["geometry"]))
    if parcel["polygon"].geom_type == "MultiPolygon": parcel["polygon"] = max(parcel["polygon"].geoms, key=lambda g: g.area)
    layout = json.loads((out / "layout.json").read_text())
    maps = json.loads((out / "maps.json").read_text())
    scan = json.loads((out / "scan.json").read_text()) if (out / "scan.json").exists() else {}
    return parcel, layout, maps, scan


def crop_box(img, pts, margin, keep_horizon=False):
    """Crop window (left, top, w, h) around the polygon: bbox + margin, 4:3. keep_horizon anchors the bottom and keeps the sky."""
    xs, ys = zip(*pts); x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    if keep_horizon:
        w = max((x1 - x0) * (1 + 2 * margin), (y1 - y0) * (1 + 2 * margin) * 4 / 3)
        w = min(w, img.width); h = w * 3 / 4
        bottom = min(img.height, y1 + (y1 - y0) * margin); top = max(0, bottom - h); bottom = top + h
        if bottom > img.height: top = img.height - h
        left = min(max(0, (x0 + x1) / 2 - w / 2), img.width - w)
        return int(left), int(top), int(w), int(h)
    w, h = (x1 - x0) * (1 + 2 * margin), (y1 - y0) * (1 + 2 * margin)
    if w / h < 4 / 3: w = h * 4 / 3
    else: h = w * 3 / 4
    w, h = min(w, img.width), min(h, img.height)
    if w / h > 4 / 3: w = h * 4 / 3
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    left = min(max(0, cx - w / 2), img.width - w); top = min(max(0, cy - h / 2), img.height - h)
    return int(left), int(top), int(w), int(h)


def prepare(out, key, rec, layout, variant="", out_size=(1152, 864)):
    """Source image + ground map (+ boundary pixels) for one output image. Returns (img, gmap, boundary_px) or (None, None, None)."""
    img = Image.open(out / rec["src"]).convert("RGBA")
    if rec["kind"] == "satellite":
        gmap = L.map_from_json(rec["map"]); return img, gmap, [gmap.to_px(x, y) for x, y in layout["boundary"]]
    pts = rec.get("boundary_px") or []
    base = L.map_from_json(rec["map"]) if rec.get("map") else None
    if layout.get("boundary_edited") and base: pts = [base.to_px(x, y) for x, y in layout["boundary"]]
    if not pts and base: pts = [base.to_px(x, y) for x, y in layout["boundary"]]
    if not pts: return None, None, None
    if rec["kind"] == "photo":
        return img, base, [tuple(p) for p in pts]
    margin = {"": 0.40 if rec["view"] != "nadir" else 0.25, "close": 0.12 if rec.get("low") else 0.10}[variant]
    left, top, w, h = crop_box(img, pts, margin, keep_horizon=bool(rec.get("low")))
    crop = img.crop((left, top, left + w, top + h)).resize(out_size, Image.LANCZOS)
    sx, sy = out_size[0] / w, out_size[1] / h
    bpx = [((x - left) * sx, (y - top) * sy) for x, y in pts]
    gmap = L.CropMap(base, left, top, sx, sy, *out_size) if base else None
    return crop, gmap, bpx


def load_overrides(out):
    """Per-image layouts (out/overrides.json: {image key: layout}). An image with an override is drawn from it instead of layout.json."""
    f = Path(out) / "overrides.json"
    try: return json.loads(f.read_text()) if f.exists() else {}
    except Exception: return {}


def save_overrides(out, ov):
    f = Path(out) / "overrides.json"
    if ov: f.write_text(json.dumps(ov, indent=1))
    elif f.exists(): f.unlink()


def layout_for(out, key, layout, overrides=None):
    ov = load_overrides(out) if overrides is None else overrides
    return ov.get(key) or layout


def _centroid_map(bpx):
    class _C:
        def to_px(self_, lon, lat): return (sum(p[0] for p in bpx) / len(bpx), sum(p[1] for p in bpx) / len(bpx))
        def to_ll(self_, x, y): return (0.0, 0.0)
    return _C()


def effective(rec, layout, gmap, bpx):
    """(layout, gmap, show_labels) actually used to draw this image — mirrors the special cases below so the editor shows the same thing."""
    if rec["kind"] == "satellite": return layout, gmap, True
    if gmap is None: return layout, _centroid_map(bpx), False            # corners only / weak fit: boundary + card, no callouts
    if rec.get("rough"):                                                  # rough map: only labels the user placed in the editor
        lay2 = dict(layout); lay2["labels"] = [l for l in layout["labels"] if str(l.get("id", "")).startswith("u")]
        return lay2, gmap, True
    return layout, gmap, True


def draw_one(out, key, rec, parcel, layout, scan, variant=""):
    """Render one output image from its clean source + ground map (+ crop variant). Returns PIL image or None."""
    img, gmap, bpx = prepare(out, key, rec, layout, variant)
    if img is None: return None
    lay, gm, show_labels = effective(rec, layout, gmap, bpx)
    if rec["kind"] == "satellite":
        return L.render(img, gm, lay, parcel, pois=scan.get("pois", [])[:3] if rec.get("pois") else [], attribution=True)
    return L.render(img, gm, lay, parcel, boundary_px=bpx, show_labels=show_labels)


def editor_payload(out, key, variant=""):
    """What the web editor needs for one image: the clean image (data URL), its size, and the layout in that image's pixels —
    every box at the exact pixel position the server draws it (a dry-run render records the placement)."""
    import base64
    out = Path(out); parcel, layout, maps, scan = load_state(out)
    ov = load_overrides(out); lay = ov.get(key) or layout
    rec = maps[key]; img, gmap, bpx = prepare(out, key, rec, lay, variant)
    if img is None or gmap is None: return None
    lay_eff, gm, show_labels = effective(rec, lay, gmap, bpx)
    L.render(img, gm, lay_eff, parcel, boundary_px=bpx, show_labels=show_labels, attribution=rec["kind"] == "satellite")   # dry run → L.PLACEMENT
    placement = dict(L.PLACEMENT); scale = L._s(img)
    buf = io.BytesIO(); img.convert("RGB").save(buf, "JPEG", quality=88)
    lp = L.layout_px(lay_eff, gmap, placement, scale); lp["boundary"] = [list(p) for p in bpx]
    lp["reference"] = [list(gmap.to_px(x, y)) for x, y in lay.get("reference", [])]
    mpp = None
    if rec["kind"] == "satellite":                               # metres per pixel at the frame centre → live acreage in the editor
        fr = gmap.frame; mpp = 156543.03392 * math.cos(math.radians(fr.lat)) / (2 ** fr.z) / ls.SCALE
    return {"key": key, "image": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode(), "W": img.width, "H": img.height, "layout": lp, "mpp": mpp,
            "draw": bool(maps.get("_meta", {}).get("draw")), "overridden": key in ov, "show_labels": show_labels}


def _edited_layout(out, key, edits, variant=""):
    """Fold editor pixels into a ground layout for `key` (override if it has one, else the shared layout). Returns (new_layout, refit, parcel, layout, maps, img)."""
    out = Path(out); parcel, layout, maps, scan = load_state(out)
    base = load_overrides(out).get(key) or layout
    rec = maps[key]; img, gmap, bpx = prepare(out, key, rec, base, variant)
    refit = None
    if rec["kind"] == "photo" and edits.get("boundary_edited") and len(edits.get("boundary", [])) == len(base["boundary"]):
        # dragged corners ↔ parcel vertices are exact correspondences → fit the real ground map from the moved ones
        old = rec.get("boundary_px") or []
        moved = [i for i, (q, o) in enumerate(zip(edits["boundary"], old)) if math.hypot(q[0] - o[0], q[1] - o[1]) > 2.5] if len(old) == len(edits["boundary"]) else []
        if len(moved) >= 4:
            ll = [tuple(base["boundary"][i]) for i in moved]; px = [tuple(edits["boundary"][i]) for i in moved]
            pm = L.PlaneMap.fit(ll, px, img.width, img.height, ransac=8.0 if len(moved) >= 6 else 0.0)
            import drone_photo as D
            if pm and D._sane(pm, [tuple(v) for v in base["boundary"]], px): refit = pm
            log("edit", "ground map refit from dragged corners", moved=len(moved), ok=bool(refit), err=round(pm.error, 1) if pm else None)
    gm = refit or gmap or _centroid_map(bpx)
    shown = L.layout_px(base, gm) if gmap else None
    new = L.apply_px_edits(base, edits, gm, shown, scale=L._s(img))
    if rec.get("rough") and "labels" in edits:                           # the editor only showed user-placed labels; keep the automatic ones
        seen = {l["id"] for l in new["labels"]}
        new["labels"] += [l for l in base["labels"] if not str(l.get("id", "")).startswith("u") and l["id"] not in seen]
    if edits.get("boundary_edited"): new["boundary_edited"] = True
    return new, refit, parcel, layout, maps, img, rec, bpx


def preview(out, key, edits, variant=""):
    """Render one image with the editor's current (unsaved) edits. No files change. Returns a PIL image."""
    out = Path(out); new, refit, parcel, layout, maps, img, rec, bpx = _edited_layout(out, key, edits, variant)
    scan = json.loads((out / "scan.json").read_text()) if (out / "scan.json").exists() else {}
    if refit:
        rec = dict(rec); rec["map"] = refit.to_json(); rec["rough"] = False; rec["boundary_px"] = [list(refit.to_px(x, y)) for x, y in layout["boundary"]]
        new["boundary"] = layout["boundary"]; new["boundary_edited"] = False
    return draw_one(out, key, rec, parcel, new, scan, variant)


def output_file(out, key):
    """Path of the finished (drawn) image for `key`, main variant."""
    out = Path(out); maps = json.loads((out / "maps.json").read_text()); rec = maps[key]
    if rec["kind"] == "satellite": return out / f"{key}.png"
    if rec["kind"] == "photo": return out / f"{key}_with_lines.jpg"
    return out / "ai" / f"{key}_outline.jpg"


def apply_edits(out, key, edits, variant="", scope="all"):
    """Fold editor output (pixel space of `key`) into the layout, then rebuild. Returns (zip, n).
    scope="all": update layout.json — every image without its own override follows (overrides keep their own edits).
    scope="one": write this image's own override only; the rest of the packet is untouched.
    Geometry (dragged photo corners → ground-map refit, boundary edits, acreage) is always shared."""
    out = Path(out); new, refit, parcel, layout, maps, img, rec, bpx = _edited_layout(out, key, edits, variant)
    ov = load_overrides(out)
    if scope == "one":
        one = new; new = json.loads(json.dumps(layout))
        for k in ("boundary", "boundary_edited"):
            if k in one: new[k] = one[k]
        one.pop("boundary_edited", None); one["boundary"] = new["boundary"]; ov[key] = one; save_overrides(out, ov)
    if refit:
        for k2, r2 in maps.items():
            if not k2.startswith("_") and r2["kind"] == "photo":
                r2["map"] = refit.to_json(); r2["rough"] = False; r2["fit"] = f"fit from dragged corners, {refit.error:.1f} px"
                r2["boundary_px"] = [list(refit.to_px(x, y)) for x, y in layout["boundary"]]
        new["boundary"] = layout["boundary"]; new["boundary_edited"] = False
        for o in ov.values(): o["boundary"] = layout["boundary"]
        save_overrides(out, ov)
        (out / "maps.json").write_text(json.dumps(maps, indent=1))
    if edits.get("card") and scope == "all":
        t = edits["card"].get("title", "")
        import re as _re
        mm = _re.search(r"([\d.]+)\s*ACRES", t.upper())
        if mm:
            try: parcel["acres"] = float(mm.group(1))
            except ValueError: pass
    if new.get("boundary_edited") and not parcel.get("no_geo"):
        parcel["acres"] = round(ls.acres_from_polygon(Polygon(new["boundary"])), 2) if not edits.get("card") else parcel["acres"]
        new["card"]["title"] = f"{parcel['acres']:.2f} ACRES +/-" if not edits.get("card") else new["card"]["title"]
    if new.get("boundary_edited") and ov:                                 # shared boundary moved: every override follows it
        for o in ov.values(): o["boundary"] = new["boundary"]
        save_overrides(out, ov)
    (out / "layout.json").write_text(json.dumps(new, indent=1))
    (out / "parcel.json").write_text(json.dumps({k: v for k, v in parcel.items() if k not in ("polygon", "raw", "feature")}, indent=1, default=str))
    return finalize(out)


def finalize(out):
    """Draw every image from layout.json + maps.json, write the packet folder + zip. No API calls. Returns (zip path, n images)."""
    out = Path(out); parcel, layout, maps, scan = load_state(out); ov = load_overrides(out)
    meta = maps.get("_meta", {}); model = meta.get("model", "grok"); angles = meta.get("angles", []); lights = meta.get("lights", []); low = meta.get("low", [])
    files = {}
    for key, rec in maps.items():
        if key.startswith("_"): continue
        lay = ov.get(key) or layout
        if rec["kind"] == "satellite":
            g = draw_one(out, key, rec, parcel, lay, scan)
            if g is not None: g.convert("RGB").save(out / f"{key}.png"); files[key] = out / f"{key}.png"
            continue
        if rec["kind"] == "photo":
            g = draw_one(out, key, rec, parcel, lay, scan)
            if g is not None: g.convert("RGB").save(out / f"{key}_with_lines.jpg", quality=94); files[key] = out / f"{key}_with_lines.jpg"
            continue
        for variant in ("", "close"):
            g = draw_one(out, key, rec, parcel, lay, scan, variant)
            if g is None: continue
            name = f"{key}{'_' + variant if variant else ''}_outline.jpg"
            g.convert("RGB").save(out / "ai" / name, quality=94); files[key + (":" + variant if variant else "")] = out / "ai" / name
    # plat (not for photos drawn without any parcel geometry)
    if not parcel.get("no_geo"):
        try:
            import plat
            pf = plat.draw_plat(parcel, scan, layout, out / "plat.png"); files["plat"] = pf
        except Exception as e:
            log("plat", "skipped", err=str(e)[:120])
    # packet in Zillow order: golden cover, low views, angles, close-ups, satellite, day set, plat, raw renders
    name = f"packet_{(parcel.get('situs') or parcel['apn']).replace(' ', '_')}_{parcel.get('city', '')}".replace("__", "_").rstrip("_")
    pk = out / name; shutil.rmtree(pk, ignore_errors=True); pk.mkdir()
    order = []
    def add(k, label):
        if k in files and Path(files[k]).exists(): order.append((files[k], label))
    lab = lambda v: f"drone_from_{v}"
    add("photo_golden", "drone_photo_golden_hour_with_lines"); add("photo_original", "drone_photo_with_lines")
    add(f"{model}_nadir_golden", "overhead_golden_hour_with_lines")
    for v in low: add(f"{model}_{v}_low_golden:close", f"close_low_view_from_{v}_golden_hour_with_lines")
    for v in angles[:2]: add(f"{model}_{v}_golden:close", f"closeup_from_{v}_golden_hour_with_lines")
    for v in low: add(f"{model}_{v}_low_golden", f"low_view_from_{v}_golden_hour_with_lines")
    for v in angles: add(f"{model}_{v}_golden", f"{lab(v)}_golden_hour_with_lines")
    for v in angles[2:]: add(f"{model}_{v}_golden:close", f"closeup_from_{v}_golden_hour_with_lines")
    add("p1_cover", "satellite_cover_with_lines")
    add("plat", "plat_map")
    add(f"{model}_nadir_day", "overhead_day_with_lines")
    for v in low: add(f"{model}_{v}_low_day:close", f"close_low_view_from_{v}_day_with_lines"); add(f"{model}_{v}_low_day", f"low_view_from_{v}_day_with_lines")
    for v in angles: add(f"{model}_{v}_day", f"{lab(v)}_day_with_lines")
    add("p1_wide", "satellite_neighborhood_context"); add("p1_tight", "satellite_closeup_with_lines")
    for k, rec in maps.items():
        if not k.startswith("_") and rec["kind"] == "render" and (out / rec["src"]).exists():
            order.append((out / rec["src"], f"{k.split('_', 1)[1]}_no_lines"))
    if (out / "p1_cover_clean.png").exists(): order.append((out / "p1_cover_clean.png", "satellite_clean"))
    if (out / "photo_golden_clean.jpg").exists(): order.append((out / "photo_golden_clean.jpg", "drone_photo_golden_hour_no_lines"))
    for i, (src, label) in enumerate(order, 1):
        shutil.copy(src, pk / f"{i:02d}_{label}{Path(src).suffix}")
    (pk / "README.txt").write_text(
        f"{parcel.get('situs', '')}, {parcel.get('city', '')}, {parcel.get('state', '')} {parcel.get('zip', '')}  --  listing photo packet\n"
        f"PIN {parcel['apn']}  |  {parcel.get('acres')} acres  |  {parcel.get('use_desc', '')}\n\nScene scan used for the renders:\n  {scan.get('scene_description', '')}\n\n"
        "Files are numbered in the recommended Zillow order (cover first). Lines, pin and acreage come from the county parcel polygon;\n"
        "on AI renders the line is where the model carried the boundary marker, so treat it as approximate (disclaimer is on-image).\n"
        "Callouts (POND, DRIVEWAY, ELECTRIC LINES, BUILDING, TIMBER/PASTURE) come from public GIS/OSM data and the satellite image; the plat is drawn\n"
        "from the GIS parcel polygon and is not a survey. AI renders: Grok Image 2.0 via Higgsfield from Google satellite imagery. Review each image before publishing.\n")
    for extra in ("scan.json", "layout.json"):
        if (out / extra).exists(): shutil.copy(out / extra, pk / extra)
    zp = out / f"{name}.zip"
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(pk.iterdir()): z.write(f, f.name)
    return zp, len(order)


if __name__ == "__main__":
    main()
