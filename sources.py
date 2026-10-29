"""
sources.py — parcel sources: the built-in counties.json plus counties users add from the app ("Counties" tab).

add_source(state, county, url) wires a county from
  • an ArcGIS REST parcel layer URL (…/MapServer/13 or …/FeatureServer/0): fields are detected from the layer schema and
    checked with a live query;
  • a CAMAvision (Vanguard) map URL such as https://maps.camavision.com/cassmo — the pattern many Missouri / Iowa counties use;
  • nothing: an arcgis.com search for "<County> County <ST> parcels" feature services, best public polygon layer wins.
User-added sources live in DATA_DIR/counties_user.json (DATA_DIR = env or /app/data when mounted, else next to the code).
"""
import json, os, re, urllib.parse
from pathlib import Path
import listing_shot as ls

HERE = Path(__file__).parent
DATA_DIR = Path(os.environ.get("DATA_DIR") or ("/app/data" if Path("/app/data").is_dir() else HERE))
USER_FILE = DATA_DIR / "counties_user.json"

STATES = {"AL": "Alabama", "AR": "Arkansas", "FL": "Florida", "GA": "Georgia", "IA": "Iowa", "IL": "Illinois", "IN": "Indiana", "KS": "Kansas", "KY": "Kentucky",
          "LA": "Louisiana", "MI": "Michigan", "MN": "Minnesota", "MO": "Missouri", "MS": "Mississippi", "NC": "North Carolina", "ND": "North Dakota", "NE": "Nebraska",
          "OH": "Ohio", "OK": "Oklahoma", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "VA": "Virginia", "WI": "Wisconsin", "WV": "West Virginia"}

# field-name heuristics, first match wins (lower-case compare)
FIELD_HINTS = {
    "pin": ["pin", "parcelid", "parcel_id", "parcelno", "parcel_no", "parcel_num", "parcelnum", "apn", "parno", "pid", "prop_id", "propid", "gispin", "parcel", "taxid", "tax_id", "acctnum", "account"],
    "situs": ["siteaddress", "site_address", "siteadd", "situs", "situs_addr", "situsaddr", "prop_add", "propaddr", "prop_addr", "phy_addr1", "address", "location", "physaddr", "full_address", "fulladdr"],
    "acres": ["gisacres", "gis_acres", "acres", "deedac", "calc_acres", "calcacres", "deeded_acres", "deedacre", "deed_acres", "shape_area_acres", "acreage", "legalacres", "totacres"],
    "owner": ["ownername", "owner_name", "ownname", "owner", "own_name", "owner1", "deedhold", "taxpayer", "name"],
    "use": ["usecode", "use_code", "landuse", "land_use", "propclass", "prop_class", "class", "parusedesc", "usedesc", "dor_uc", "property_class", "classcode", "luc"],
    "legal": ["legaldesc", "legal_desc", "legal", "s_legal", "legaldescription"],
    "city": ["sitecity", "site_city", "situscity", "prop_city", "phy_city", "city", "scity"],
    "zip": ["sitezip", "site_zip", "situszip", "prop_zip", "phy_zipcd", "zip", "szip", "zipcode"],
    "county": ["county", "cntyname", "county_name", "countyname", "co_name"],
}


def load_sources():
    base = json.loads((HERE / "counties.json").read_text())
    if USER_FILE.exists():
        try:
            base.update(json.loads(USER_FILE.read_text()))
        except Exception:
            pass
    return base


def _save_user(entry_key, cfg):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    d = json.loads(USER_FILE.read_text()) if USER_FILE.exists() else {}
    d[entry_key] = cfg
    USER_FILE.write_text(json.dumps(d, indent=2))


def remove_source(key):
    if not USER_FILE.exists(): return False
    d = json.loads(USER_FILE.read_text())
    if key in d:
        d.pop(key); USER_FILE.write_text(json.dumps(d, indent=2)); return True
    return False


def _key(state, county):
    return re.sub(r"[^a-z0-9]+", "-", f"{county} {state}".lower()).strip("-")


def detect_fields(fields):
    """Map our roles to the layer's field names using the hints; returns (mapping, pin_field)."""
    names = [f["name"] for f in fields]; low = {n.lower(): n for n in names}
    out = {}
    for role, hints in FIELD_HINTS.items():
        for h in hints:
            if h in low: out[role] = low[h]; break
        if role not in out:                                    # substring fallback (e.g. "PARCEL_ID_NO")
            for h in hints[:4]:
                m = next((n for l, n in low.items() if h in l), None)
                if m: out[role] = m; break
    pin = out.pop("pin", None)
    return out, pin


def add_arcgis(state, county, url, test_pin=None):
    url = url.strip().rstrip("/")
    m = re.match(r"(https?://.+?/(?:MapServer|FeatureServer))/(\d+)$", url)
    if not m: raise ValueError("That is not an ArcGIS layer URL — it should end in /MapServer/<n> or /FeatureServer/<n>.")
    info = ls.http_json(url + "?f=json", timeout=30)
    if info.get("error"): raise ValueError(f"Layer refused: {info['error'].get('message')}")
    if info.get("geometryType") != "esriGeometryPolygon": raise ValueError(f"Layer '{info.get('name')}' is {info.get('geometryType', 'unknown')}, not polygons — pick the parcel polygon layer.")
    fields = info.get("fields") or []
    mapping, pin = detect_fields(fields)
    if not pin: raise ValueError("Could not find a parcel-number field in: " + ", ".join(f["name"] for f in fields[:30]))
    # live check: one feature with geometry
    q = urllib.parse.urlencode({"where": "1=1", "outFields": pin, "returnGeometry": "true", "outSR": 4326, "resultRecordCount": 1, "f": "geojson"})
    try:
        d = ls.http_json(f"{url}/query?{q}", timeout=40)
    except Exception as e:
        raise ValueError(f"The layer did not answer a query ({str(e)[:80]}).")
    if not d.get("features"):
        d = ls.http_json(f"{url}/query?" + urllib.parse.urlencode({"where": "1=1", "outFields": pin, "returnGeometry": "true", "outSR": 4326, "resultRecordCount": 1, "f": "json"}), timeout=40)
        if not d.get("features"): raise ValueError("The layer answered but returned no features.")
    sample = (d["features"][0].get("properties") or d["features"][0].get("attributes") or {}).get(pin)
    cfg = {"name": f"{county} County, {state}", "kind": "arcgis", "scan": "federal", "parcels_url": url, "pin_field": pin, "pin_normalize": "loose",
           "fields": mapping, "defaults": {"state": state, "county": county}, "reverse_geocode": True, "added_by": "app", "layer_name": info.get("name"),
           "sample_pin": sample}
    return cfg


def add_camavision(state, county, url):
    m = re.match(r"https?://maps\.camavision\.com/([A-Za-z0-9_]+)", url.strip())
    if not m: raise ValueError("CAMAvision URLs look like https://maps.camavision.com/<county-code>.")
    code = m.group(1); base = f"https://maps.camavision.com/{code}"
    caps = ls.http_bytes(base + "?SERVICE=WFS&VERSION=1.1.0&REQUEST=GetCapabilities").decode("utf-8", "ignore")
    names = set(re.findall(r"<Name>([^<]+)</Name>", caps))
    if "parcels" not in names: raise ValueError("This CAMAvision map has no 'parcels' layer.")
    geom_layer = "search" if "search" in names else next((n for n in ("lot", "glot") if n in names), None)
    if not geom_layer: raise ValueError("No polygon layer found on this CAMAvision map.")
    desc = ls.http_bytes(base + "?SERVICE=WFS&VERSION=1.1.0&REQUEST=DescribeFeatureType&TYPENAME=parcels").decode("utf-8", "ignore")
    fields = [{"name": n} for n in re.findall(r'name="([^"]+)"', desc)]
    mapping, pin = detect_fields(fields)
    cfg = {"name": f"{county} County, {state}", "kind": "camavision", "scan": "federal", "wfs": base, "geom_layer": geom_layer, "pin_field": pin or "parcelid",
           "pin_normalize": "loose", "fields": mapping, "defaults": {"state": state, "county": county}, "reverse_geocode": True, "added_by": "app"}
    return cfg


def search_arcgis_online(state, county):
    """Public parcel feature services on arcgis.com for a county. Returns candidate layer URLs (best first)."""
    st = STATES.get(state, state)
    q = f'({county} County) {st} parcels type:"Feature Service"'
    d = ls.http_json("https://www.arcgis.com/sharing/rest/search?" + urllib.parse.urlencode({"q": q, "num": 25, "f": "json"}), timeout=30)
    cands = []
    for r in d.get("results", []):
        t = (r.get("title") or "").lower(); u = r.get("url") or ""
        if not u or "parcel" not in t or r.get("access") != "public": continue
        score = (county.lower() in t) * 2 + ("parcel" in t) + (st.lower() in t or state.lower() in t)
        cands.append((score, r.get("modified", 0), u, r.get("title")))
    cands.sort(reverse=True)
    out = []
    for _, _, u, title in cands[:6]:
        try:
            info = ls.http_json(u + "?f=json", timeout=20)
            for lyr in info.get("layers", []):
                li = ls.http_json(f"{u}/{lyr['id']}?f=json", timeout=20)
                if li.get("geometryType") == "esriGeometryPolygon":
                    out.append((f"{u}/{lyr['id']}", f"{title} — {lyr.get('name')}"))
        except Exception:
            continue
    return out


def add_source(state, county, url=""):
    """Wire a county. Returns (key, cfg, message)."""
    state = (state or "").strip().upper()[:2]; county = re.sub(r"\s+county$", "", (county or "").strip(), flags=re.I).title()
    if not state or not county: raise ValueError("Give the state (two letters) and the county name.")
    url = (url or "").strip()
    if url and "camavision.com" in url:
        cfg = add_camavision(state, county, url)
    elif url:
        cfg = add_arcgis(state, county, url)
    else:
        cands = search_arcgis_online(state, county)
        if not cands:
            raise ValueError(f"No public parcel service found for {county} County, {state} on arcgis.com. Open the county's GIS map, find the parcel layer's REST URL "
                             "(…/MapServer/<n> or …/FeatureServer/<n>) and paste it here — or, if the county uses maps.camavision.com, paste that link. "
                             "Until then, a Land id KML export works for any parcel.")
        err = None
        for u, label in cands:
            try:
                cfg = add_arcgis(state, county, u); cfg["layer_label"] = label; break
            except Exception as e:
                err = str(e)
        else:
            raise ValueError(f"Found services but none worked ({err}). Candidates: " + "; ".join(l for _, l in cands[:3]))
    key = _key(state, county); _save_user(key, cfg)
    msg = f"Added **{cfg['name']}** ({cfg['kind']}). Parcel number field: `{cfg['pin_field']}`; " + \
          (f"sample parcel number: `{cfg['sample_pin']}`; " if cfg.get("sample_pin") else "") + \
          "fields: " + ", ".join(f"{k}={v}" for k, v in cfg["fields"].items())
    return key, cfg, msg
