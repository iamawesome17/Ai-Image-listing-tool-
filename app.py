"""
app.py — Bana Listing Photos web app (Gradio).

Tabs:
  1. Generate from parcel   county + PIN → satellite set + AI drone views with property lines → zip
  2. Real drone photo       upload a drone photo → property line (DJI metadata or clicked corners) + optional golden-hour relight

Run locally:  python app.py  → http://localhost:7860      Deploy: see README.md (Railway).
Env: REGRID_TOKEN, GOOGLE_MAPS_KEY, HIGGSFIELD_KEY, APP_PASSWORD (login user is "bana").
"""
import os, re, subprocess, sys, json, time
from pathlib import Path
import gradio as gr
from PIL import Image, ImageDraw, ImageOps

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import sources as S


def county_table():
    global COUNTIES, COUNTY_LABELS
    COUNTIES = {k: v for k, v in S.load_sources().items() if not k.startswith("_")}
    COUNTY_LABELS = {k: v.get("name", k) for k, v in COUNTIES.items()}
    return COUNTIES


county_table()
PASSWORD = os.environ.get("APP_PASSWORD", "")
try:
    import listing_shot as _ls; _ls.load_env(str(HERE / ".env"))          # local runs: keys from .env / keys.env; Railway: env vars
except Exception:
    pass
EDITOR_JS = (HERE / "editor.js").read_text()

VIEW_NAMES = {"nadir": "Overhead", "S": "From the south", "N": "From the north", "E": "From the east", "W": "From the west",
              "NE": "From the north-east", "SE": "From the south-east", "SW": "From the south-west", "NW": "From the north-west"}


def editable_images(out_dir):
    """(label, key) pairs for the editor dropdown, flagship first."""
    import pipeline as P
    parcel, layout, maps, scan = P.load_state(out_dir)
    items = []
    for k, rec in maps.items():
        if k.startswith("_"): continue
        if rec["kind"] == "satellite": items.append((f"Satellite — {k.split('_')[1]}", k, 5))
        elif rec["kind"] == "photo": items.append(("Drone photo — golden hour" if "golden" in k else "Drone photo — original", k, 0))
        elif rec.get("map"):
            v = VIEW_NAMES.get(rec["view"], rec["view"]); g = rec["light"] == "golden"
            pri = (1 if rec["view"] == "nadir" else 2 if rec.get("low") else 3) if g else 4
            items.append((f"{'Low view ' if rec.get('low') else ''}{v} — {rec['light']}", k, pri))
    items.sort(key=lambda t: (t[2], t[0]))
    return [(a, b) for a, b, _ in items]


def packet_outputs(out_dir):
    out_dir = Path(out_dir)
    pk = sorted([p for p in out_dir.glob("packet_*") if p.is_dir()], key=lambda p: p.stat().st_mtime)
    zips = sorted(out_dir.glob("packet_*.zip"), key=lambda p: p.stat().st_mtime)
    images = sorted(list(pk[-1].glob("*.jpg")) + list(pk[-1].glob("*.png"))) if pk else []
    return [(str(p), p.stem.replace("_", " ")) for p in images], (str(zips[-1]) if zips else None)


def list_runs():
    """Previous runs (anything under out/ with a maps.json), newest first — lets you reopen a packet later."""
    runs = []
    for mp in (HERE / "out").rglob("maps.json"):
        d = mp.parent
        try:
            pj = json.loads((d / "parcel.json").read_text()); name = f"{pj.get('situs') or pj.get('apn')} · {pj.get('city', '')}".strip(" ·")
        except Exception:
            name = d.name
        kind = "photo" if (d / "photo_original_clean.jpg").exists() else "packet"
        runs.append((mp.stat().st_mtime, f"{name}  ({kind}, {time.strftime('%b %d %H:%M', time.localtime(mp.stat().st_mtime))})", str(d)))
    runs.sort(reverse=True)
    return [(a, b) for _, a, b in runs[:40]]


def pick_run(out_dir):
    if not out_dir: return gr.update(), None
    items = editable_images(out_dir)
    return gr.update(choices=items, value=items[0][1] if items else None), out_dir


def sources_markdown():
    rows = ["| Source | Type | Parcel-number field | Added |", "|---|---|---|---|"]
    for k, v in county_table().items():
        rows.append(f"| {v.get('name', k)} | {v.get('kind')} | `{v.get('pin_field', '—')}` | {'app' if v.get('added_by') == 'app' else 'built-in'} |")
    return "\n".join(rows)


def add_county(state, county, url, progress=gr.Progress()):
    progress(0.2, desc="Looking for the county's parcel service…")
    try:
        key, cfg, msg = S.add_source(state, county, url)
    except Exception as e:
        raise gr.Error(str(e))
    county_table()
    choices = list(COUNTY_LABELS.values())
    return f"✅ {msg}", sources_markdown(), gr.update(choices=choices, value=cfg["name"]), gr.update(choices=choices, value=cfg["name"])


def open_editor(out_dir):
    if not out_dir: raise gr.Error("Run a packet first.")
    items = editable_images(out_dir)
    return gr.update(choices=items, value=items[0][1] if items else None), gr.Tabs(selected="edit")


def editor_payload(out_dir, key):
    import pipeline as P
    if not out_dir or not key: return ""
    pl = P.editor_payload(out_dir, key)
    return json.dumps(pl) if pl else ""


def _edits(edits_json):
    try:
        edits = json.loads(edits_json) if edits_json else None
    except Exception:
        edits = None
    if not edits: raise gr.Error("The editor has nothing loaded yet — pick an image and click Load.")
    return edits


def apply_and_rebuild(out_dir, key, edits_json, scope, progress=gr.Progress()):
    """Apply the editor's edits: to every image (shared layout) or to this image only (its own override). No API calls, nothing is re-rendered."""
    import pipeline as P
    if not out_dir or not key: raise gr.Error("Nothing to apply — open a run in the editor first.")
    edits = _edits(edits_json); one = str(scope).startswith("This")
    progress(0.2, desc="Redrawing this image…" if one else "Redrawing every image…")
    zp, n = P.apply_edits(out_dir, key, edits, scope="one" if one else "all")
    imgs, z = packet_outputs(out_dir); f = P.output_file(out_dir, key)
    msg = (f"**Saved for this image only** — the other {n - 1} images are unchanged. Download it on the right, or the whole packet." if one
           else f"**Packet rebuilt: {n} images.** Edits applied to every view (images with their own edits keep them).")
    return imgs, z, msg, (str(f) if f.exists() else None), (str(f) if f.exists() else None)


def preview_one(out_dir, key, edits_json, progress=gr.Progress()):
    """Render this image with the current edits without saving anything — exactly what Apply would produce."""
    import pipeline as P
    if not out_dir or not key: raise gr.Error("Open a run in the editor first.")
    edits = _edits(edits_json)
    progress(0.3, desc="Rendering preview…")
    im = P.preview(out_dir, key, edits)
    if im is None: raise gr.Error("This image cannot be drawn yet (no ground map).")
    p = Path(out_dir) / "preview.jpg"; im.convert("RGB").save(p, quality=92)
    return str(p)


def reset_edits(out_dir):
    import pipeline as P
    if not out_dir: raise gr.Error("Nothing to reset.")
    out = Path(out_dir); parcel, layout, maps, scan = P.load_state(out)
    lay = P.L.build_layout(parcel, scan)
    for k in ("boundary", "boundary_edited", "reference"):                # keep user-drawn geometry
        if k in layout and (layout.get("boundary_edited") or k == "reference"): lay[k] = layout[k]
    (out / "layout.json").write_text(json.dumps(lay, indent=1)); P.save_overrides(out, {})
    zp, n = P.finalize(out); imgs, z = packet_outputs(out)
    return imgs, z, "**Edits reset** — every image is back to the automatic layout (per-image edits cleared too).", None, None


# ---------------- tab 1: generate from parcel ----------------
def run(county_label, pin, lat, lon, boundary, label, angles, lights, low, render, rstate="", rcounty="", progress=gr.Progress()):
    county = next(k for k, v in COUNTY_LABELS.items() if v == county_label)
    pin = (pin or "").strip()
    if not boundary and not pin and not (lat and lon):
        raise gr.Error("Enter a parcel number, both coordinates, or upload a boundary file.")
    cmd = [sys.executable, str(HERE / "pipeline.py"), "--county", county, "--angles", angles, "--lights", lights, "--low", low]
    if boundary:
        cmd += ["--boundary", boundary] + (["--label", label] if label else [])
    elif COUNTIES[county].get("kind") == "realie":
        st = (rstate or "").split(" ")[0].strip().upper(); cn = (rcounty or "").strip()
        if not st: raise gr.Error("Realie needs the state (and county for a parcel number).")
        looks_addr = bool(re.match(r"^\s*\d+\s+[A-Za-z]", pin))
        cmd += ["--state", st] + (["--county-name", cn] if cn else []) + (["--address", pin] if looks_addr else ["--pin", pin])
        if not looks_addr and not cn: raise gr.Error("Realie needs the county name with a parcel number (or enter a street address instead).")
    elif COUNTIES[county].get("kind") == "regrid" and pin and ("," in pin or " " in pin.strip()):
        cmd += ["--address", pin]                      # Regrid: a street address works best
    else:
        cmd += ["--pin", pin] if pin else ["--lat", str(lat), "--lon", str(lon)]
    if not render: cmd.append("--no-render")
    progress(0.05, desc="Parcel + surroundings scan…")
    t0 = time.time()
    proc = subprocess.Popen(cmd, cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    log, stage = [], 0.05
    marks = {"2/7": (0.15, "Scanning surroundings…"), "4/7": (0.25, "Layout + satellite set…"), "5/7": (0.35, "Rendering drone views (1–3 min)…"),
             "6/7": (0.85, "Fitting property lines…"), "7/7": (0.95, "Drawing + plat + zip…")}
    for line in proc.stdout:
        log.append(line.rstrip())
        for k, (v, d) in marks.items():
            if line.startswith(k): stage = v; progress(stage, desc=d)
    proc.wait()
    text = "\n".join(log)
    if proc.returncode != 0:
        raise gr.Error("Run failed — see the log below.\n" + text[-1500:])
    out_dir = None
    for line in log:
        if line.startswith("done") and "→" in line:
            out_dir = Path(line.split("→")[1].strip().split(" ")[0]).parent
    if out_dir is None:
        out_dir = sorted((HERE / "out").glob("*"), key=lambda p: p.stat().st_mtime)[-1]
    images, z = packet_outputs(out_dir)
    warn = ""
    if "credit balance is too low" in text: warn = "⚠️ **Higgsfield balance is empty — no AI drone views were generated.** Top up at console.higgsfield.ai and run again (existing satellite images are kept; only the renders are re-done).\n\n"
    elif "FAILED" in text and render: warn = "⚠️ Some AI renders failed — see the run log.\n\n"
    scan = json.loads((out_dir / "scan.json").read_text()).get("scene_description", "") if (out_dir / "scan.json").exists() else ""
    summary = warn + f"**{len(images)} images · {round(time.time()-t0)} s** — next step: **Review & edit** to fix any label or line before you ship.\n\n{scan}"
    return images, z, summary, text[-4000:], str(out_dir)


# ---------------- tab 2: real drone photo ----------------
def _load(path):
    return ImageOps.exif_transpose(Image.open(path)).convert("RGB")


def draw_points(path, pts):
    if path is None: return None
    im = _load(path); d = ImageDraw.Draw(im); r = max(6, im.width // 150)
    if len(pts) >= 2:
        d.line([tuple(p) for p in pts] + ([tuple(pts[0])] if len(pts) >= 3 else []), fill=(255, 0, 255), width=max(3, im.width // 400))
    for x, y in pts:
        d.ellipse((x - r, y - r, x + r, y + r), fill=(255, 0, 255), outline="white", width=2)
    return im


WORK_W = 1600   # display / click / output width; the original stays untouched for metadata


def make_work_copy(path):
    """Downscale the uploaded photo to WORK_W wide for fast display and clicking; returns the new path."""
    im = _load(path)
    if im.width > WORK_W:
        im = im.resize((WORK_W, round(im.height * WORK_W / im.width)), Image.LANCZOS)
    out = Path(path).with_name(Path(path).stem + "_work.jpg"); im.save(out, quality=92)
    return str(out)


def photo_uploaded(path):
    import drone_photo as D
    if not path: return None, None, [], "", None
    meta = D.read_metadata(path)
    work = make_work_copy(path)
    if meta:
        msg = f"✅ Drone metadata found ({meta['model'] or 'camera'}, {meta['alt_m']:.0f} m up, heading {meta['yaw']:.0f}°, tilt {meta['pitch']:.0f}°) — the property line will be placed automatically. You can still click corners to override."
    else:
        tags = D.metadata_debug(path)
        msg = "ℹ️ No usable drone metadata in this file — click the parcel corners on the photo, in order around the boundary." + (f"  (found: {tags})" if tags else "  (no EXIF/XMP at all — the file was probably re-saved by a photo app)")
    return path, work, [], msg, work


def photo_click(path, pts, evt: gr.SelectData):
    pts = list(pts or []) + [[float(evt.index[0]), float(evt.index[1])]]
    return pts, draw_points(path, pts), f"{len(pts)} corner{'s' if len(pts) != 1 else ''} marked"


def photo_clear(path):
    return [], (_load(path) if path else None), "Corners cleared"


def resolve_parcel(county_label, pin, boundary=None, label="", rstate="", rcounty="", lat=None, lon=None):
    """Parcel dict from any source: boundary file / saved lot, Realie, Regrid, county or statewide layer, or lat/lon."""
    import listing_shot as ls, pipeline as P
    county = next((k for k, v in COUNTY_LABELS.items() if v == county_label), None)
    pin = (pin or "").strip(); cfg = COUNTIES.get(county, {})
    if boundary:
        import boundary_file
        bpoly, props = boundary_file.read_boundary(boundary)
        name = label or (props or {}).get("name") or Path(boundary).stem
        parcel = ls.parcel_from_feature({"geometry": bpoly.__geo_interface__, "properties": {"PIN": name, "SiteAddress": name}}, {})
        c0 = bpoly.centroid; rg = P.reverse_geocode(c0.y, c0.x)
        parcel["county"], parcel["city"], parcel["state"], parcel["zip"] = (props or {}).get("county") or rg.get("county", ""), rg.get("city", ""), rg.get("state", ""), rg.get("zip", "")
        parcel["situs"] = name; parcel["use_desc"] = "land"
        if (props or {}).get("acres"): parcel["acres"] = props["acres"]
        return parcel, {"name": "boundary file", "kind": "file", "scan": "federal", "fields": {}, "defaults": {}}
    if cfg.get("kind") == "realie":
        st = (rstate or "").split(" ")[0].strip().upper(); cn = (rcounty or "").strip()
        looks_addr = bool(re.match(r"^\s*\d+\s+[A-Za-z]", pin))
        try:
            return P.fetch_parcel_realie(None if looks_addr else pin, st, cn, pin if looks_addr else None), cfg
        except SystemExit as e:
            raise gr.Error(str(e))
    if cfg.get("kind") == "regrid":
        return ls.fetch_parcel(pin, None, pin if ("," in pin or " " in pin) else None, lat, lon), cfg
    if not pin and not (lat and lon): raise gr.Error("Enter a parcel number (or coordinates).")
    try:
        return P.fetch_parcel_county(cfg, pin or None, lat if not pin else None, lon if not pin else None), cfg
    except SystemExit as e:
        raise gr.Error(str(e))


def scan_for(parcel, cfg):
    import listing_shot as ls, pipeline as P
    poly = parcel["polygon"]; c = poly.centroid
    z = ls.choose_zoom(poly, 640, 480, margin=0.22); fr = ls.Frame(c.y, c.x, z, 640, 480)
    fed = json.loads((HERE / "counties.json").read_text())["_federal"]
    return P.scan_surroundings(cfg, parcel, ls.fetch_satellite(fr), fr) if cfg.get("scan") == "county" else P.scan_federal(fed, cfg, parcel, ls.fetch_satellite(fr), fr)


def run_photo(path, work, source, county_label, pin, lot, boundary, label, acres_txt, golden, pts, rstate="", rcounty="", progress=gr.Progress()):
    import drone_photo as D, listing_shot as ls, pipeline as P
    from shapely.geometry import Polygon
    if not path: raise gr.Error("Upload a drone photo first.")
    scan = None; scan_err = None; cfg = {}
    progress(0.1, desc="Parcel lookup…")
    if source.startswith("Draw"):                                   # no parcel at all: a 1-acre (or given) square at a dummy location, dragged into place
        try: acres = float(re.sub(r"[^0-9.]", "", acres_txt or "")) or 1.0
        except ValueError: acres = 1.0
        side = (acres * 4046.8564) ** 0.5; dl = side / 111320 / 2
        poly = Polygon([(-dl, -dl * 1.007), (dl, -dl * 1.007), (dl, dl * 1.007), (-dl, dl * 1.007)])
        name = label or "Property"
        parcel = {"apn": name, "path": None, "acres": acres, "situs": name, "city": "", "state": "", "zip": "", "county": "", "owner": None, "use": None, "zoning": None,
                  "polygon": poly, "raw": {}, "use_desc": "land", "no_geo": True}
    else:
        bfile = lot if source.startswith("Saved") else boundary
        if source.startswith("Saved") and not lot: raise gr.Error("Pick a saved lot (draw one on the Subdivision lot tab first).")
        if source.startswith("Parcel") and not (pin or "").strip(): raise gr.Error("Enter the parcel number.")
        parcel, cfg = resolve_parcel(county_label, pin, bfile, label, rstate, rcounty)
        try:
            progress(0.25, desc="Surroundings scan…"); scan = scan_for(parcel, cfg)
        except Exception as e:
            scan = None; scan_err = str(e)[:200]
    points = [tuple(p) for p in pts] if pts and len(pts) >= 3 else None
    progress(0.4, desc="Golden-hour relight (≈1 min)…" if golden else "Drawing lines…")
    try:
        r = D.annotate(work or path, parcel, scan, points, golden, out_dir=HERE / "out" / re.sub(r"[^A-Za-z0-9_.-]+", "_", str(parcel["apn"])) / "photos", meta_path=path)
    except RuntimeError as e:
        raise gr.Error(str(e))
    note = f"Property line from **{r['source']}**."
    if r["source"] == "placed for dragging":
        note = ("**Shape placed on the photo** — open **Review & edit**, drag the corners onto the real corners" +
                (" (4 are enough; the rest snap in and the callouts appear)" if not parcel.get("no_geo") else ", add corners if needed, set the card text") + ", then Apply.")
    if r.get("meta"):
        m = r["meta"]; note += f" Camera: {m['model'] or 'drone'}, {m['alt_m']:.0f} m up, heading {m['yaw']:.0f}°, tilt {m['pitch']:.0f}°."
    if scan_err: note += f" ⚠️ Surroundings scan failed ({scan_err}) — no automatic callouts."
    return [(f, Path(f).stem.replace("_", " ")) for f in r["files"]], note, r["out_dir"], r.get("zip")


# ---------------- subdivision lot (draw) ----------------
def start_lot(name, county_label, pin, lat, lon, rstate, rcounty, progress=gr.Progress()):
    import lots
    name = (name or "").strip()
    if not name: raise gr.Error("Give the lot a name (e.g. PRE Lot 7) — it goes on the card and the saved file.")
    parent = None
    if (pin or "").strip():
        progress(0.2, desc="Parent parcel lookup…"); parent, _ = resolve_parcel(county_label, pin, None, "", rstate, rcounty)
    elif not (lat and lon):
        raise gr.Error("Enter the parent (mother) parcel number, or a latitude/longitude to open the map there.")
    progress(0.5, desc="Satellite…")
    out = lots.start_draw(name, parent, lat, lon, out_root=HERE / "out")
    msg = ("**Parent lot shown dashed in yellow; a small starter box sits at its centre.** " if parent else "") + "On the Review & edit tab: drag the white corners onto the lot lines (+ Corner then click on a line adds one; Delete removes the selected one), Zoom in/out as needed. Acreage updates live. Then **Save lot** and come back here to generate."
    return msg, str(out)


def save_lot_btn(out_dir, key, edits_json):
    import pipeline as P, lots
    if not out_dir: raise gr.Error("Open a map first.")
    try: edits = json.loads(edits_json) if edits_json else None
    except Exception: edits = None
    if edits: P.apply_edits(out_dir, key or "p1_cover", edits)
    f, acres = lots.save_lot(out_dir)
    choices = lots.list_lots()
    imgs, z = packet_outputs(out_dir)
    up = lambda: gr.update(choices=choices, value=str(f))
    return f"✅ Saved **{Path(f).stem}** — {acres:.2f} acres. It is in the saved-lot lists now (drone photo tab, and *Generate packet from this lot* here).", up(), up(), imgs


def is_draw_run(out_dir):
    try:
        return gr.update(visible=bool(json.loads((Path(out_dir) / "maps.json").read_text()).get("_meta", {}).get("draw")))
    except Exception:
        return gr.update(visible=False)


def zoom_lot(out_dir, key, edits_json, delta):
    """Keep the current edits, then re-render the satellite closer/farther around the lot."""
    import pipeline as P, lots
    if not out_dir: raise gr.Error("Open a map first.")
    try: edits = json.loads(edits_json) if edits_json else None
    except Exception: edits = None
    if edits: P.apply_edits(out_dir, key or "p1_cover", edits)
    try: z = lots.rezoom(out_dir, delta)
    except ValueError as e: raise gr.Error(str(e))
    return f"Zoom {z}"


def generate_from_lot(lot, render, angles, lights, lowv, progress=gr.Progress()):
    if not lot: raise gr.Error("Save the lot first.")
    name = json.loads(Path(lot).read_text()).get("properties", {}).get("name", Path(lot).stem)
    return run(list(COUNTY_LABELS.values())[0], "", None, None, lot, name, ",".join(angles) or "S", ",".join(lights) or "golden", lowv.split(" ")[0], render, "", "", progress)


# ---------------- UI ----------------
THEME = gr.themes.Soft(
    primary_hue=gr.themes.colors.emerald, secondary_hue=gr.themes.colors.stone, neutral_hue=gr.themes.colors.stone,
    font=[gr.themes.GoogleFont("Poppins"), "system-ui", "sans-serif"], radius_size=gr.themes.sizes.radius_md,
).set(body_background_fill="#f6f5f1", block_background_fill="#ffffff", block_border_width="1px", block_shadow="0 1px 2px rgba(0,0,0,.04)",
      button_primary_background_fill="#1f6f4a", button_primary_background_fill_hover="#185a3c", button_primary_text_color="#ffffff",
      block_label_background_fill="#f1f0ec", block_label_text_color="#44423d", block_title_text_color="#44423d",
      checkbox_background_color_selected="#1f6f4a", checkbox_border_color_selected="#1f6f4a")

CSS = """
.gradio-container {max-width: 1180px !important; margin: 0 auto !important;}
#hero {padding: 26px 28px 18px; border-radius: 14px; background: linear-gradient(135deg,#17352a 0%,#1f6f4a 60%,#2f8f5f 100%); color: #fff; margin-bottom: 8px;}
#hero h1 {margin: 0 0 4px; font-size: 26px; font-weight: 600; letter-spacing: -.2px; color:#fff;}
#hero p {margin: 0; color: #e7f1ea !important; font-size: 14px;}
.tab-nav button {font-weight: 600 !important;}
.help {color:#5c5a55; font-size:13px; margin: -4px 0 6px;}
footer {display:none !important;}
.gallery-item img {object-fit: cover;}
.bana-btn {background:#fff;border:1px solid #d9d6cf;border-radius:8px;padding:6px 12px;font-size:13px;font-weight:600;cursor:pointer;color:#2b2a27;}
.bana-btn:hover {background:#f1f0ec;}
.bana-in {width:100%;box-sizing:border-box;border:1px solid #d9d6cf;border-radius:8px;padding:7px 9px;font-size:13px;background:#fff;color:#111;}
.bana-row {display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-top:6px;}
.bana-sm {padding:4px 9px;font-size:12px;}
.bana-chk {font-size:13px;display:flex;align-items:center;gap:6px;margin-top:6px;}
"""

EDITOR_HTML = """
<div id="bana-editor" style="border:1px solid #e3e1dc;border-radius:12px;padding:10px;background:#fff">
  <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:8px">
    <button id="bana-add-callout" class="bana-btn">+ Callout</button>
    <button id="bana-add-road" class="bana-btn">+ Road pin</button>
    <button id="bana-corner" class="bana-btn">+ Corner</button>
    <button id="bana-del" class="bana-btn">Delete selected</button>
    <button id="bana-undo" class="bana-btn">Undo</button>
    <span style="border-left:1px solid #d9d6cf;height:22px"></span>
    <button id="bana-toggle-lines" class="bana-btn">Hide lines</button>
    <button id="bana-toggle-card" class="bana-btn">Hide card</button>
    <button id="bana-toggle-disc" class="bana-btn">Hide disclaimer</button>
    <span id="bana-acres" style="font-weight:700;color:#1f6f4a;font-size:14px;margin-left:8px"></span>
    <span id="bana-overridden" style="color:#b45309;font-size:12px;font-weight:600;margin-left:6px"></span>
  </div>
  <div id="bana-status" style="color:#5c5a55;font-size:13px;margin:-2px 0 8px"></div>
  <div style="display:flex;gap:12px;flex-wrap:wrap">
    <div style="flex:1 1 640px;min-width:320px"><canvas id="bana-canvas" style="width:100%;border-radius:10px;background:#111;cursor:crosshair"></canvas></div>
    <div style="flex:0 0 260px;display:flex;flex-direction:column;gap:10px">
      <div style="background:#f6f5f1;border-radius:10px;padding:10px">
        <div style="font-weight:600;font-size:13px;margin-bottom:2px">Selected: <span id="bana-selname" style="font-weight:500;color:#5c5a55">nothing</span></div>
        <div class="bana-row"><span style="font-size:13px">Size</span><button id="bana-smaller" class="bana-btn bana-sm">−</button><button id="bana-bigger" class="bana-btn bana-sm">+</button></div>
        <div class="bana-row"><span style="font-size:13px">Line width</span><button id="bana-line-smaller" class="bana-btn bana-sm">−</button><button id="bana-line-bigger" class="bana-btn bana-sm">+</button></div>
        <label class="bana-chk"><input type="checkbox" id="bana-shadow"> Soft shadows (lines, pins, cards)</label>
        <label class="bana-chk"><input type="checkbox" id="bana-fill" checked> Light fill inside the lines</label>
      </div>
      <div style="background:#f6f5f1;border-radius:10px;padding:10px">
        <div style="font-weight:600;font-size:13px;margin-bottom:6px">Acreage card</div>
        <input id="bana-title" class="bana-in" placeholder="30.21 ACRES +/-">
        <input id="bana-csub" class="bana-in" placeholder="ADDRESS · COUNTY" style="margin-top:6px">
      </div>
      <div id="bana-labelpanel" style="display:none;background:#f6f5f1;border-radius:10px;padding:10px">
        <div style="font-weight:600;font-size:13px;margin-bottom:6px">Selected label</div>
        <input id="bana-text" class="bana-in" placeholder="TEXT">
        <input id="bana-sub" class="bana-in" placeholder="small second line (optional)" style="margin-top:6px">
        <select id="bana-kind" class="bana-in" style="margin-top:6px"><option value="feature">Callout (black)</option><option value="road">Road pin (white)</option></select>
      </div>
      <div style="background:#f6f5f1;border-radius:10px;padding:10px">
        <div style="font-weight:600;font-size:13px;margin-bottom:6px">Disclaimer line</div>
        <input id="bana-disc" class="bana-in">
      </div>
    </div>
  </div>
</div>
"""

with gr.Blocks(title="Bana Listing Photos") as demo:
    import lots
    gr.HTML('<div id="hero"><h1>Bana Listing Photos</h1><p>Parcel or drawn lot in → listing-ready images out: true property lines on satellite and AI drone views, or on your own drone photos.</p></div>')
    run_dir = gr.State(None)
    LOAD_JS = "(p) => { if (p) window.banaEditor.load(JSON.parse(p)); }"
    EXPORT_JS = "(o, k, e) => [o, k, JSON.stringify(window.banaEditor.export())]"
    STATE_CHOICES = [f"{k} — {v}" for k, v in S.STATES.items()]
    ADV_ANGLES = ["S", "E", "W", "N", "NE", "SE", "SW", "NW"]

    with gr.Tabs() as tabs:
        # ---------- 1. subdivision lot ----------
        with gr.Tab("Subdivision lot", id="lot"):
            gr.Markdown('<div class="help">For lots the GIS does not have yet. Enter the <b>mother parcel</b> (any source) — the satellite opens below with the parent lot dashed; drag the corners onto the new lot, save it, then generate the full packet or use it on drone photos.</div>')
            with gr.Row():
                with gr.Column(scale=1, min_width=320):
                    lname = gr.Textbox(label="Lot name (goes on the card)", placeholder="PRE Lot 7")
                    lcounty = gr.Dropdown(list(COUNTY_LABELS.values()), value=list(COUNTY_LABELS.values())[0], label="County / state of the mother parcel")
                    lpin = gr.Textbox(label="Mother parcel number — or a street address with the Realie option", placeholder="20-0-0636-002-00")
                    with gr.Row(visible=False) as lrealie_row:
                        lrstate = gr.Dropdown(STATE_CHOICES, value="MO — Missouri", label="State (Realie)"); lrcounty = gr.Textbox(label="County (Realie)")
                    with gr.Row():
                        llat = gr.Number(label="…or latitude", value=None); llon = gr.Number(label="longitude", value=None)
                    lopen = gr.Button("Open satellite & draw", variant="primary", size="lg")
                    lmsg = gr.Markdown()
                    gr.Markdown('<div class="help">Step 2 happens on the <b>Review &amp; edit</b> tab (opens automatically): drag the corners, Save lot. Step 3: come back here and generate.</div>')
                with gr.Column(scale=2):
                    gr.Markdown("### Generate the packet from a saved lot")
                    with gr.Row():
                        llot = gr.Dropdown(choices=lots.list_lots(), label="Saved lot", scale=3); lrefresh = gr.Button("↻", scale=0, min_width=48)
                    lrender = gr.Checkbox(value=True, label="Generate AI drone views (≈ $0.90, 2–3 min)")
                    with gr.Accordion("Advanced", open=False):
                        langles = gr.CheckboxGroup(ADV_ANGLES, value=["S", "E", "W", "N"], label="High camera positions")
                        llights = gr.CheckboxGroup(["golden", "day"], value=["golden", "day"], label="Lighting")
                        llow = gr.Dropdown(["auto (road side + opposite)", "none", "S", "N", "E", "W"], value="auto (road side + opposite)", label="Low horizon views")
                    lgo = gr.Button("Generate packet from this lot", variant="primary", size="lg")
                    lsummary = gr.Markdown(); lzip = gr.File(label="Download packet (zip)")
                    edit_btn1 = gr.Button("Open in Review & edit →", size="sm")
                    lgallery = gr.Gallery(label="Packet", columns=3, height=420, object_fit="cover", show_label=False)
                    with gr.Accordion("Run log", open=False):
                        llog = gr.Textbox(lines=10, show_label=False)
            lcounty.change(lambda c: gr.update(visible=("Realie" in (c or ""))), [lcounty], [lrealie_row])

        # ---------- 2. single parcel ----------
        with gr.Tab("Single parcel", id="gen"):
            with gr.Row():
                with gr.Column(scale=1, min_width=320):
                    gr.Markdown("### Property")
                    county = gr.Dropdown(list(COUNTY_LABELS.values()), value=list(COUNTY_LABELS.values())[0], label="County / state")
                    pin = gr.Textbox(label="Parcel number (PIN / APN) — or a street address with the Realie option", placeholder="20-0-0636-002-00")
                    with gr.Row(visible=False) as realie_row:
                        rstate = gr.Dropdown(STATE_CHOICES, value="MO — Missouri", label="State (Realie)"); rcounty = gr.Textbox(label="County (Realie)", placeholder="Cass")
                    gr.Markdown('<div class="help">Parcel number as shown on the county assessor site. …or locate by coordinates:</div>')
                    with gr.Row():
                        lat = gr.Number(label="Latitude", value=None); lon = gr.Number(label="Longitude", value=None)
                    with gr.Accordion("Or upload a boundary file (Land id / Google Earth export) — works anywhere", open=False):
                        boundary = gr.File(label="KML, KMZ, GeoJSON or zipped Shapefile", file_types=[".kml", ".kmz", ".geojson", ".json", ".zip"], type="filepath")
                        blabel = gr.Textbox(label="Address or name for the card", placeholder="7617 State St")
                    render = gr.Checkbox(value=True, label="Generate AI drone views (≈ $0.90, 2–3 min)")
                    with gr.Accordion("Advanced", open=False):
                        angles = gr.CheckboxGroup(ADV_ANGLES, value=["S", "E", "W", "N"], label="High camera positions")
                        lights = gr.CheckboxGroup(["golden", "day"], value=["golden", "day"], label="Lighting")
                        lowv = gr.Dropdown(["auto (road side + opposite)", "none", "S", "N", "E", "W"], value="auto (road side + opposite)", label="Low horizon views")
                    go = gr.Button("Create packet", variant="primary", size="lg")
                    summary = gr.Markdown()
                    zipf = gr.File(label="Download packet (zip)")
                    edit_btn2 = gr.Button("Open in Review & edit →", size="sm")
                with gr.Column(scale=2):
                    gallery = gr.Gallery(label="Packet", columns=3, height=640, object_fit="cover", show_label=False)
                    with gr.Accordion("Run log", open=False):
                        logbox = gr.Textbox(lines=14, show_label=False)
            run_evt = go.click(lambda c, p, la, lo, b, bl, a, l, lw, r, rs, rc: run(c, p, la, lo, b, bl, ",".join(a) or "S", ",".join(l) or "golden", lw.split(" ")[0], r, rs, rc),
                               [county, pin, lat, lon, boundary, blabel, angles, lights, lowv, render, rstate, rcounty], [gallery, zipf, summary, logbox, run_dir])
            county.change(lambda c: gr.update(visible=("Realie" in (c or ""))), [county], [realie_row])

        # ---------- 3. drone photo ----------
        with gr.Tab("Drone photo", id="photo"):
            with gr.Row():
                with gr.Column(scale=1, min_width=320):
                    gr.Markdown("### Photo")
                    photo = gr.Image(type="filepath", label="Drone photo", height=320, sources=["upload"])
                    pstatus = gr.Markdown('<div class="help">Straight from the SD card = the line is placed automatically from the drone GPS. Otherwise press <b>Annotate</b> and drag the corners into place in <b>Review &amp; edit</b> below.</div>')
                    gr.Markdown("### Property line from")
                    psource = gr.Radio(["Parcel number", "Saved lot (subdivision)", "Draw on the photo (no parcel)"], value="Parcel number", show_label=False)
                    with gr.Group(visible=True) as pg_parcel:
                        pcounty = gr.Dropdown(list(COUNTY_LABELS.values()), value=list(COUNTY_LABELS.values())[0], label="County / state")
                        ppin = gr.Textbox(label="Parcel number (PIN / APN) — or a street address with the Realie option", placeholder="20-0-0636-002-00")
                        with gr.Row(visible=False) as prealie_row:
                            prstate = gr.Dropdown(STATE_CHOICES, value="MO — Missouri", label="State (Realie)"); prcounty = gr.Textbox(label="County (Realie)")
                        with gr.Accordion("Or upload a boundary file (Land id export)", open=False):
                            pboundary = gr.File(label="KML, KMZ, GeoJSON or zipped Shapefile", file_types=[".kml", ".kmz", ".geojson", ".json", ".zip"], type="filepath")
                    with gr.Group(visible=False) as pg_lot:
                        with gr.Row():
                            plot = gr.Dropdown(choices=lots.list_lots(), label="Saved lot", scale=3); plrefresh = gr.Button("↻", scale=0, min_width=48)
                    with gr.Group(visible=False) as pg_draw:
                        pacres = gr.Textbox(label="Acreage for the card (optional)", placeholder="10.5")
                    plabel = gr.Textbox(label="Name / address for the card (optional)", placeholder="PRE Lot 7")
                    pgolden = gr.Checkbox(value=True, label="Add a golden-hour version (≈ $0.05)")
                    pgo = gr.Button("Annotate photo", variant="primary", size="lg")
                    pclear = gr.Button("Clear clicked corners", size="sm")
                    pnote = gr.Markdown()
                    pzip = gr.File(label="Download (zip)")
                    edit_btn3 = gr.Button("Open in Review & edit →", size="sm")
                with gr.Column(scale=2):
                    pgallery = gr.Gallery(label="Results", columns=2, height=640, object_fit="contain", show_label=False)
            pts_state = gr.State([]); orig_state = gr.State(None); work_state = gr.State(None)
            photo.upload(photo_uploaded, [photo], [orig_state, work_state, pts_state, pstatus, photo])
            photo.select(photo_click, [work_state, pts_state], [pts_state, photo, pstatus])
            pclear.click(photo_clear, [work_state], [pts_state, photo, pstatus])
            photo_evt = pgo.click(run_photo, [orig_state, work_state, psource, pcounty, ppin, plot, pboundary, plabel, pacres, pgolden, pts_state, prstate, prcounty], [pgallery, pnote, run_dir, pzip])
            pcounty.change(lambda c: gr.update(visible=("Realie" in (c or ""))), [pcounty], [prealie_row])
            psource.change(lambda v: (gr.update(visible=v.startswith("Parcel")), gr.update(visible=v.startswith("Saved")), gr.update(visible=v.startswith("Draw"))), [psource], [pg_parcel, pg_lot, pg_draw])
            plrefresh.click(lambda: gr.update(choices=lots.list_lots()), None, [plot])

        # ---------- 4. review & edit ----------
        with gr.Tab("Review & edit", id="edit"):
            gr.Markdown('<div class="help">Separate step after an asset is created. Pick the run and the image. Drag a <b>label box</b> to move it, drag its <b>pin</b> to move the point; drag corners, the acreage card or the disclaimer; double-click a label to rename it. <b>Size ±</b> resizes the selected item; <b>Delete</b> removes a label or hides the card / disclaimer; <b>Hide lines</b> removes the outline. <b>Preview</b> shows the exact output without saving. Nothing here calls an API or re-renders — edits are free.</div>')
            with gr.Row():
                eruns = gr.Dropdown(choices=list_runs(), label="Run (newest first)", scale=3)
                erefresh = gr.Button("↻", scale=0, min_width=48)
            with gr.Row():
                ekey = gr.Dropdown(choices=[], label="Image to edit", scale=3)
                eload = gr.Button("Load", scale=1)
                escope = gr.Radio(["This image only", "Whole packet"], value="This image only", label="Apply to", scale=2)
                epreview = gr.Button("Preview", scale=1); eapply = gr.Button("Apply", variant="primary", scale=1); ereset = gr.Button("Reset all to automatic", scale=1)
            with gr.Row(visible=False) as lot_tools:
                lzin = gr.Button("Zoom in", size="sm"); lzout = gr.Button("Zoom out", size="sm"); lsave = gr.Button("Save lot", variant="primary", size="sm"); lzmsg = gr.Markdown()
            lsaved = gr.Markdown()
            gr.HTML(EDITOR_HTML)
            epayload = gr.Textbox(visible=False); eedits = gr.Textbox(visible=False)
            esummary = gr.Markdown()
            with gr.Row():
                epreview_img = gr.Image(label="Preview (exact output, not saved)", height=360, scale=2, interactive=False)
                with gr.Column(scale=1):
                    eone = gr.File(label="Download this image")
                    ezip = gr.File(label="Download packet (zip)")
            egallery = gr.Gallery(label="Rebuilt packet", columns=4, height=420, object_fit="cover", show_label=False)
            EXPORT3_JS = "(o, k, e, s) => [o, k, JSON.stringify(window.banaEditor.export()), s]"
            eload.click(editor_payload, [run_dir, ekey], [epayload]).then(None, [epayload], None, js=LOAD_JS)
            ekey.input(editor_payload, [run_dir, ekey], [epayload]).then(None, [epayload], None, js=LOAD_JS)
            epreview.click(preview_one, [run_dir, ekey, eedits], [epreview_img], js=EXPORT_JS)
            eapply.click(apply_and_rebuild, [run_dir, ekey, eedits, escope], [egallery, ezip, esummary, eone, epreview_img], js=EXPORT3_JS).then(editor_payload, [run_dir, ekey], [epayload]).then(None, [epayload], None, js=LOAD_JS)
            ereset.click(reset_edits, [run_dir], [egallery, ezip, esummary, eone, epreview_img]).then(editor_payload, [run_dir, ekey], [epayload]).then(None, [epayload], None, js=LOAD_JS)
            erefresh.click(lambda: gr.update(choices=list_runs()), None, [eruns])
            eruns.input(pick_run, [eruns], [ekey, run_dir]).then(editor_payload, [run_dir, ekey], [epayload]).then(None, [epayload], None, js=LOAD_JS).then(is_draw_run, [run_dir], [lot_tools])

        # ---------- 5. counties ----------
        with gr.Tab("Counties", id="counties"):
            gr.Markdown('<div class="help">Statewide sources cover every parcel in that state; <b>Any US county (Realie)</b> covers the rest. For a county you want on its own GIS: give the state and county, and either leave the URL blank (the app searches arcgis.com for the public parcel layer) or paste the parcel layer REST URL (…/MapServer/&lt;n&gt; or …/FeatureServer/&lt;n&gt;) or a CAMAvision map link (maps.camavision.com/…). Checked live before saving.</div>')
            with gr.Row():
                cstate = gr.Dropdown(STATE_CHOICES, value="MO — Missouri", label="State", scale=1)
                cname = gr.Textbox(label="County", placeholder="Cass", scale=1)
                curl = gr.Textbox(label="Parcel layer URL (optional)", placeholder="https://…/MapServer/13  or  https://maps.camavision.com/…", scale=3)
                cadd = gr.Button("Add county", variant="primary", scale=1)
            cmsg = gr.Markdown()
            ctable = gr.Markdown(sources_markdown())
            cadd.click(lambda st, nm, u: add_county(st.split(" ")[0], nm, u), [cstate, cname, curl], [cmsg, ctable, county, pcounty, lcounty])

    def after_run(out_dir):
        items = editable_images(out_dir) if out_dir else []
        return gr.update(choices=list_runs(), value=out_dir), gr.update(choices=items, value=items[0][1] if items else None)
    load_chain = lambda evt: evt.then(after_run, [run_dir], [eruns, ekey])
    goto_edit = lambda evt: evt.then(lambda: gr.Tabs(selected="edit"), None, [tabs]).then(editor_payload, [run_dir, ekey], [epayload]).then(None, [epayload], None, js=LOAD_JS).then(is_draw_run, [run_dir], [lot_tools])
    load_chain(run_evt); load_chain(photo_evt)
    for b in (edit_btn1, edit_btn2, edit_btn3):
        goto_edit(b.click(lambda: None, None, None))
    # subdivision lot wiring: drawing happens on the Review & edit tab, so open it right away
    goto_edit(load_chain(lopen.click(start_lot, [lname, lcounty, lpin, llat, llon, lrstate, lrcounty], [lmsg, run_dir])))
    lsave.click(save_lot_btn, [run_dir, ekey, eedits], [lsaved, llot, plot, egallery], js=EXPORT_JS).then(editor_payload, [run_dir, ekey], [epayload]).then(None, [epayload], None, js=LOAD_JS)
    lrefresh.click(lambda: gr.update(choices=lots.list_lots()), None, [llot])
    for btn, d in ((lzin, 1), (lzout, -1)):
        btn.click(lambda o, k, e, d=d: zoom_lot(o, k, e, d), [run_dir, ekey, eedits], [lzmsg], js=EXPORT_JS).then(editor_payload, [run_dir, ekey], [epayload]).then(None, [epayload], None, js=LOAD_JS)
    lot_evt = lgo.click(generate_from_lot, [llot, lrender, langles, llights, llow], [lgallery, lzip, lsummary, llog, run_dir])
    load_chain(lot_evt)

if __name__ == "__main__":
    demo.queue(default_concurrency_limit=2)
    demo.launch(server_name="0.0.0.0", server_port=int(os.environ.get("PORT", 7860)), theme=THEME, css=CSS, head=f"<script>{EDITOR_JS}</script>",
                auth=(("bana", PASSWORD) if PASSWORD else None))
