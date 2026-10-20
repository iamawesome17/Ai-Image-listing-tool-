"""
lots.py — draw a lot that is not in the GIS yet (subdivision child lots) and reuse it everywhere.

start_draw(...)  → a run folder with a satellite image, the parent parcel as a dashed reference and an editable starting
                   boundary; the normal editor edits it (drag / add / delete corners) and shows live acreage.
save_lot(run)    → DATA_DIR/lots/<slug>.geojson (name, acres, parent APN); listed in the app so the drone-photo tab and the
                   packet generator can use it like a Land id export.
"""
import json, math, re, time
from pathlib import Path
from shapely.geometry import Polygon, shape
from shapely import force_2d
import listing_shot as ls, layout as L, pipeline as P, sources as S

LOTS_DIR = S.DATA_DIR / "lots"


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-") or f"lot-{int(time.time())}"


def start_draw(name, parent=None, lat=None, lon=None, out_root=None):
    """Create an editable draw run. parent: a parcel dict (polygon + fields) or None; lat/lon: where to open the map when there is no parent."""
    out_root = Path(out_root or "out"); out = out_root / "draw" / _slug(name); out.mkdir(parents=True, exist_ok=True)
    if parent is not None:
        poly = parent["polygon"]; c = poly.centroid
        z = ls.choose_zoom(poly, 640, 480, margin=0.18)
    else:
        if lat is None or lon is None: raise ValueError("Give a parent parcel or a latitude/longitude to open the map.")
        c = Polygon([(lon, lat)] * 4).centroid; z = 17
    frame = ls.Frame(c.y, c.x, z, 640, 480)
    sat = ls.fetch_satellite(frame); sat.convert("RGB").save(out / "p1_cover_clean.png")
    fm = L.FrameMap(frame)
    if parent is not None:
        ring = [(x, y) for x, y, *_ in poly.exterior.coords][:-1]
        from drone_photo import simplify_to_n
        start = simplify_to_n(ring, 16) if len(ring) > 16 else ring
        reference = [[x, y] for x, y in ring]
    else:   # starter rectangle ~ 40% of the frame
        W, H = frame.W, frame.H
        start = [fm.to_ll(x, y) for x, y in ((W * 0.3, H * 0.3), (W * 0.7, H * 0.3), (W * 0.7, H * 0.7), (W * 0.3, H * 0.7))]
        reference = []
    pc = Polygon(start).centroid
    acres = round(ls.acres_from_polygon(Polygon(start)), 2)
    parcel = {"apn": name, "situs": name, "city": (parent or {}).get("city", ""), "state": (parent or {}).get("state", ""), "zip": (parent or {}).get("zip", ""),
              "county": (parent or {}).get("county", ""), "acres": acres, "use_desc": "land", "owner": (parent or {}).get("owner"), "path": None, "raw": {},
              "parent_apn": (parent or {}).get("apn")}
    if not parcel["county"]:
        rg = P.reverse_geocode(c.y, c.x); parcel.update({k: rg.get(k, "") for k in ("city", "zip", "county", "state") if not parcel.get(k)})
    layout = {"boundary": [[x, y] for x, y in start], "reference": reference,
              "card": {"title": f"{acres:.2f} ACRES +/-", "sub": ", ".join(s for s in [name.upper(), parcel["city"].upper(), f"{parcel['state']} {parcel['zip']}".strip()] if s) + (f"  ·  {parcel['county'].upper()} COUNTY" if parcel["county"] else ""), "lon": pc.x, "lat": pc.y},
              "labels": [], "disclaimer": ls.DISCLAIMER, "boundary_edited": True}
    maps = {"_meta": {"model": "draw", "angles": [], "lights": [], "low": [], "draw": True, "name": name},
            "p1_cover": {"src": "p1_cover_clean.png", "map": fm.to_json(), "kind": "satellite", "pois": False}}
    (out / "layout.json").write_text(json.dumps(layout, indent=1)); (out / "scan.json").write_text("{}")
    (out / "parcel.geojson").write_text(json.dumps({"type": "Feature", "geometry": Polygon(start).__geo_interface__, "properties": {"name": name}}))
    P.save_state(out, parcel, maps)
    P.finalize(out)
    return out


def save_lot(run_dir, name=None):
    """Write the drawn boundary as a GeoJSON lot file; returns (path, acres)."""
    out = Path(run_dir); parcel, layout, maps, scan = P.load_state(out)
    name = name or maps.get("_meta", {}).get("name") or parcel.get("apn") or out.name
    poly = Polygon(layout["boundary"]).buffer(0)
    acres = round(ls.acres_from_polygon(poly), 2)
    LOTS_DIR.mkdir(parents=True, exist_ok=True)
    f = LOTS_DIR / f"{_slug(name)}.geojson"
    f.write_text(json.dumps({"type": "Feature", "geometry": poly.__geo_interface__,
                             "properties": {"name": name, "acres": acres, "parent_apn": parcel.get("parent_apn"), "county": parcel.get("county"), "state": parcel.get("state"),
                                            "city": parcel.get("city"), "zip": parcel.get("zip"), "saved": time.strftime("%Y-%m-%d %H:%M")}}, indent=1))
    return f, acres


def list_lots():
    """[(label, path)] newest first."""
    if not LOTS_DIR.exists(): return []
    out = []
    for f in sorted(LOTS_DIR.glob("*.geojson"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            pr = json.loads(f.read_text()).get("properties", {})
            out.append((f"{pr.get('name', f.stem)} — {pr.get('acres', '?')} ac{(' · ' + pr['county'] + ' Co') if pr.get('county') else ''}", str(f)))
        except Exception:
            out.append((f.stem, str(f)))
    return out
