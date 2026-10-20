"""Read a parcel boundary from a KML/KMZ (Land id, onX, Google Earth), GeoJSON, or zipped Shapefile."""
import json, re, zipfile, io
from pathlib import Path
from shapely.geometry import shape, Polygon, MultiPolygon
from shapely import force_2d
from shapely.ops import unary_union


def _kml_polygons(text):
    polys = []
    for m in re.finditer(r"<Polygon>.*?</Polygon>", text, re.S):
        outer = re.search(r"<outerBoundaryIs>.*?<coordinates>(.*?)</coordinates>", m.group(0), re.S)
        if not outer: continue
        pts = [tuple(map(float, c.split(",")[:2])) for c in outer.group(1).split()]
        if len(pts) >= 4: polys.append(Polygon(pts))
    return polys


def read_boundary(path):
    """Returns (shapely Polygon lon/lat, properties dict). Largest polygon wins when several are present."""
    p = Path(path); data = p.read_bytes(); props = {}
    low = p.suffix.lower()
    polys = []
    if low == ".kmz" or (low == ".zip" and zipfile.is_zipfile(io.BytesIO(data)) and any(n.endswith(".kml") for n in zipfile.ZipFile(io.BytesIO(data)).namelist())):
        z = zipfile.ZipFile(io.BytesIO(data)); kml = next(n for n in z.namelist() if n.endswith(".kml"))
        polys = _kml_polygons(z.read(kml).decode("utf-8", "ignore"))
    elif low == ".kml":
        polys = _kml_polygons(data.decode("utf-8", "ignore"))
    elif low in (".json", ".geojson"):
        d = json.loads(data); feats = d.get("features", [d]) if d.get("type") != "Feature" else [d]
        for f in feats:
            g = force_2d(shape(f["geometry"]))
            if isinstance(g, MultiPolygon): polys += list(g.geoms)
            elif isinstance(g, Polygon): polys.append(g)
            props = props or f.get("properties", {})
    elif low == ".zip":
        import shapefile
        z = zipfile.ZipFile(io.BytesIO(data)); base = next(n[:-4] for n in z.namelist() if n.endswith(".shp"))
        r = shapefile.Reader(shp=io.BytesIO(z.read(base + ".shp")), dbf=io.BytesIO(z.read(base + ".dbf")), shx=io.BytesIO(z.read(base + ".shx")) if base + ".shx" in z.namelist() else None)
        for sr in r.shapeRecords():
            g = force_2d(shape(sr.shape.__geo_interface__))
            if isinstance(g, MultiPolygon): polys += list(g.geoms)
            elif isinstance(g, Polygon): polys.append(g)
            props = props or sr.record.as_dict()
    else:
        raise ValueError("Unsupported boundary file; use KML, KMZ, GeoJSON or a zipped Shapefile.")
    if not polys: raise ValueError("No polygon found in the boundary file.")
    poly = max(polys, key=lambda g: g.area)
    minx, miny, maxx, maxy = poly.bounds
    if not (-180 <= minx <= 180 and -90 <= miny <= 90): raise ValueError("Boundary is not in lon/lat (WGS84); export as KML from Land id or Google Earth.")
    return poly, props
