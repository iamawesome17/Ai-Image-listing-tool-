"""
app.py — Bana Listing Photos web app (Gradio).

Tabs:
  1. Generate from parcel   county + PIN → satellite set + AI drone views with property lines → zip
  2. Real drone photo       upload a drone photo → property line (DJI metadata or clicked corners) + optional golden-hour relight

Run locally:  python app.py  → http://localhost:7860      Deploy: see README.md (Railway).
Env: REGRID_TOKEN, GOOGLE_MAPS_KEY, HIGGSFIELD_KEY, APP_PASSWORD (login user is "bana").
"""
import os, subprocess, sys, json, time
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


def apply_and_rebuild(out_dir, key, edits_json, progress=gr.Progress()):
    import pipeline as P
    if not out_dir or not key: raise gr.Error("Nothing to apply — open a run in the editor first.")
    try:
        edits = json.loads(edits_json) if edits_json else None
    except Exception:
        edits = None
    if not edits: raise gr.Error("The editor has nothing loaded yet — pick an image and click Load.")
    progress(0.2, desc="Applying edits to every image…")
    zp, n = P.apply_edits(out_dir, key, edits)
    imgs, z = packet_outputs(out_dir)
    return imgs, z, f"**Packet rebuilt: {n} images.** Edits applied to every view (lines, pin card, callouts)."


def reset_edits(out_dir):
    import pipeline as P
    if not out_dir: raise gr.Error("Nothing to reset.")
    out = Path(out_dir); parcel, layout, maps, scan = P.load_state(out)
    import listing_shot as ls
    lay = P.L.build_layout(parcel, scan)
    (out / "layout.json").write_text(json.dumps(lay, indent=1))
    zp, n = P.finalize(out); imgs, z = packet_outputs(out)
    return imgs, z, "**Edits reset** — labels and lines are back to the automatic placement."


# ---------------- tab 1: generate from parcel ----------------
def run(county_label, pin, lat, lon, boundary, label, angles, lights, low, render, progress=gr.Progress()):
    county = next(k for k, v in COUNTY_LABELS.items() if v == county_label)
    pin = (pin or "").strip()
    if not boundary and not pin and not (lat and lon):
        raise gr.Error("Enter a parcel number, both coordinates, or upload a boundary file.")
    cmd = [sys.executable, str(HERE / "pipeline.py"), "--county", county, "--angles", angles, "--lights", lights, "--low", low]
    if boundary:
        cmd += ["--boundary", boundary] + (["--label", label] if label else [])
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
    scan = json.loads((out_dir / "scan.json").read_text()).get("scene_description", "") if (out_dir / "scan.json").exists() else ""
    summary = f"**{len(images)} images · {round(time.time()-t0)} s** — review them, then use **Review & edit** to fix any label or line before you ship.\n\n{scan}"
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


def run_photo(path, work, county_label, pin, boundary, label, golden, pts, progress=gr.Progress()):
    import drone_photo as D, listing_shot as ls, pipeline as P
    county = next(k for k, v in COUNTY_LABELS.items() if v == county_label)
    pin = (pin or "").strip()
    if not path: raise gr.Error("Upload a drone photo first.")
    if not pin and not boundary: raise gr.Error("Enter the parcel number or upload a boundary file — it supplies the property line and the acreage card.")
    progress(0.1, desc="Parcel lookup…")
    cfg = COUNTIES[county]
    if boundary:
        import boundary_file
        bpoly, _ = boundary_file.read_boundary(boundary)
        parcel = ls.parcel_from_feature({"geometry": bpoly.__geo_interface__, "properties": {"PIN": Path(boundary).stem, "SiteAddress": label or ""}}, {})
        c0 = bpoly.centroid; rg = P.reverse_geocode(c0.y, c0.x)
        parcel["county"], parcel["city"], parcel["state"], parcel["zip"] = rg.get("county", ""), rg.get("city", ""), rg.get("state", ""), rg.get("zip", "")
        parcel["situs"] = label or rg.get("street", ""); parcel["use_desc"] = "land"
        cfg = {"name": "boundary file", "kind": "file", "scan": "federal", "fields": {}, "defaults": {}}
    elif cfg.get("kind") == "regrid":
        parcel = ls.fetch_parcel(pin, None, pin if ("," in pin or " " in pin) else None, None, None)
    else:
        parcel = P.fetch_parcel_county(cfg, pin)
    scan = None
    try:
        progress(0.25, desc="Surroundings scan…")
        poly = parcel["polygon"]; c = poly.centroid
        z = ls.choose_zoom(poly, 640, 480, margin=0.22); fr = ls.Frame(c.y, c.x, z, 640, 480)
        fed = json.loads((HERE / "counties.json").read_text())["_federal"]
        scan = P.scan_surroundings(cfg, parcel, ls.fetch_satellite(fr), fr) if cfg.get("scan") == "county" else P.scan_federal(fed, cfg, parcel, ls.fetch_satellite(fr), fr)
    except Exception as e:
        scan = None; scan_err = str(e)[:200]
    else:
        scan_err = None
    points = [tuple(p) for p in pts] if pts and len(pts) >= 3 else None
    progress(0.4, desc="Golden-hour relight (≈1 min)…" if golden else "Drawing lines…")
    try:
        r = D.annotate(work or path, parcel, scan, points, golden, out_dir=HERE / "out" / parcel["apn"] / "photos", meta_path=path)
    except RuntimeError as e:
        raise gr.Error(str(e))
    note = f"Property line from **{r['source']}**."
    if r["source"] == "placed for dragging":
        note = "**No metadata and no corners** — the true parcel shape was placed in the middle of the photo. Open **Review & edit**, drag at least 4 corners onto the real corners, and Apply: the whole shape and the callouts snap into place."
    if r.get("meta"):
        m = r["meta"]; note += f" Camera: {m['model'] or 'drone'}, {m['alt_m']:.0f} m up, heading {m['yaw']:.0f}°, tilt {m['pitch']:.0f}°."
    note += (" Callouts placed from the ground fit." if r.get("ground_map") else " No ground fit (corners only) — boundary and card drawn, no callouts.")
    if scan_err: note += f" ⚠️ Surroundings scan failed ({scan_err}) — no automatic callouts."
    note += " Check the line against what you know of the property, then use **Review & edit** to adjust."
    return [(f, Path(f).stem.replace("_", " ")) for f in r["files"]], note, r["out_dir"], r.get("zip")


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
"""

EDITOR_HTML = """
<div id="bana-editor" style="border:1px solid #e3e1dc;border-radius:12px;padding:10px;background:#fff">
  <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:8px">
    <button id="bana-add-callout" class="bana-btn">+ Callout</button>
    <button id="bana-add-road" class="bana-btn">+ Road pin</button>
    <button id="bana-corner" class="bana-btn">+ Corner</button>
    <button id="bana-del" class="bana-btn">Delete selected</button>
    <button id="bana-undo" class="bana-btn">Undo</button>
    <span id="bana-status" style="color:#5c5a55;font-size:13px;margin-left:6px"></span>
  </div>
  <div style="display:flex;gap:12px;flex-wrap:wrap">
    <div style="flex:1 1 640px;min-width:320px"><canvas id="bana-canvas" style="width:100%;border-radius:10px;background:#111;cursor:crosshair"></canvas></div>
    <div style="flex:0 0 260px;display:flex;flex-direction:column;gap:10px">
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
    gr.HTML('<div id="hero"><h1>Bana Listing Photos</h1><p>Parcel number in → listing-ready images out: true property lines on satellite and AI drone views, or on your own drone photos.</p></div>')

    run_dir = gr.State(None)
    LOAD_JS = "(p) => { if (p) window.banaEditor.load(JSON.parse(p)); }"
    with gr.Tabs() as tabs:
        with gr.Tab("Generate from parcel", id="gen"):
            with gr.Row():
                with gr.Column(scale=1, min_width=320):
                    gr.Markdown("### Property")
                    county = gr.Dropdown(list(COUNTY_LABELS.values()), value=list(COUNTY_LABELS.values())[0], label="County / state")
                    pin = gr.Textbox(label="Parcel number (PIN / APN)", placeholder="20-0-0636-002-00")
                    gr.Markdown('<div class="help">Parcel number as shown on the county assessor site. …or locate by coordinates:</div>')
                    with gr.Row():
                        lat = gr.Number(label="Latitude", value=None); lon = gr.Number(label="Longitude", value=None)
                    with gr.Accordion("Or upload a boundary file (Land id / Google Earth export) — works anywhere", open=False):
                        boundary = gr.File(label="KML, KMZ, GeoJSON or zipped Shapefile", file_types=[".kml", ".kmz", ".geojson", ".json", ".zip"], type="filepath")
                        blabel = gr.Textbox(label="Address or name for the card", placeholder="7617 State St")
                    render = gr.Checkbox(value=True, label="Generate AI drone views (≈ $0.90, 2–3 min)")
                    with gr.Accordion("Advanced", open=False):
                        angles = gr.CheckboxGroup(["S", "E", "W", "N", "NE", "SE", "SW", "NW"], value=["S", "E", "W", "N"], label="High camera positions")
                        lights = gr.CheckboxGroup(["golden", "day"], value=["golden", "day"], label="Lighting")
                        lowv = gr.Dropdown(["auto (road side + opposite)", "none", "S", "N", "E", "W"], value="auto (road side + opposite)", label="Low horizon views")
                    go = gr.Button("Create packet", variant="primary", size="lg")
                    summary = gr.Markdown()
                    zipf = gr.File(label="Download packet (zip)")
                    edit_btn = gr.Button("Review & edit this packet →", size="sm")
                with gr.Column(scale=2):
                    gallery = gr.Gallery(label="Packet", columns=3, height=640, object_fit="cover", show_label=False)
                    with gr.Accordion("Run log", open=False):
                        logbox = gr.Textbox(lines=14, show_label=False)
            go.click(lambda c, p, la, lo, b, bl, a, l, lw, r: run(c, p, la, lo, b, bl, ",".join(a) or "S", ",".join(l) or "golden", lw.split(" ")[0], r),
                     [county, pin, lat, lon, boundary, blabel, angles, lights, lowv, render], [gallery, zipf, summary, logbox, run_dir])

        with gr.Tab("Real drone photo", id="photo"):
            with gr.Row():
                with gr.Column(scale=1, min_width=320):
                    gr.Markdown("### Photo + property")
                    photo = gr.Image(type="filepath", label="Drone photo", height=360, sources=["upload"])
                    pstatus = gr.Markdown('<div class="help">Upload a photo straight from the drone (SD card) to keep its GPS and gimbal data. Otherwise click the parcel corners in order — 4 or more corners also enables callouts.</div>')
                    pcounty = gr.Dropdown(list(COUNTY_LABELS.values()), value=list(COUNTY_LABELS.values())[0], label="County / state")
                    ppin = gr.Textbox(label="Parcel number (PIN / APN)", placeholder="20-0-0636-002-00")
                    with gr.Accordion("Or upload a boundary file (Land id export)", open=False):
                        pboundary = gr.File(label="KML, KMZ, GeoJSON or zipped Shapefile", file_types=[".kml", ".kmz", ".geojson", ".json", ".zip"], type="filepath")
                        plabel = gr.Textbox(label="Address or name for the card")
                    pgolden = gr.Checkbox(value=True, label="Add a golden-hour version (≈ $0.05)")
                    pclear = gr.Button("Clear corners", size="sm")
                    pgo = gr.Button("Annotate photo", variant="primary", size="lg")
                    pnote = gr.Markdown()
                    pzip = gr.File(label="Download (zip)")
                    pedit_btn = gr.Button("Review & edit these photos →", size="sm")
                with gr.Column(scale=2):
                    pgallery = gr.Gallery(label="Results", columns=2, height=640, object_fit="contain", show_label=False)
            pts_state = gr.State([]); orig_state = gr.State(None); work_state = gr.State(None)
            photo.upload(photo_uploaded, [photo], [orig_state, work_state, pts_state, pstatus, photo])
            photo.select(photo_click, [work_state, pts_state], [pts_state, photo, pstatus])
            pclear.click(photo_clear, [work_state], [pts_state, photo, pstatus])
            pgo.click(run_photo, [orig_state, work_state, pcounty, ppin, pboundary, plabel, pgolden, pts_state], [pgallery, pnote, run_dir, pzip])

        with gr.Tab("Review & edit", id="edit"):
            gr.Markdown('<div class="help">Edit the flagship image once — every other view is redrawn from the same edits. Drag corners, callouts or the acreage card; double-click a callout to rename it; <b>Delete</b> removes the selected item. Add corners, callouts (black) or road pins (white) with the buttons, then <b>Apply</b>.</div>')
            with gr.Row():
                eruns = gr.Dropdown(choices=list_runs(), label="Run (newest first)", scale=3)
                erefresh = gr.Button("↻", scale=0, min_width=48)
            with gr.Row():
                ekey = gr.Dropdown(choices=[], label="Image to edit", scale=3)
                eload = gr.Button("Load", scale=1); eapply = gr.Button("Apply edits & rebuild packet", variant="primary", scale=2); ereset = gr.Button("Reset to automatic", scale=1)
            gr.HTML(EDITOR_HTML)
            epayload = gr.Textbox(visible=False); eedits = gr.Textbox(visible=False)
            esummary = gr.Markdown()
            with gr.Row():
                egallery = gr.Gallery(label="Rebuilt packet", columns=4, height=420, object_fit="cover", show_label=False, scale=3)
                ezip = gr.File(label="Download packet (zip)", scale=1)
            eload.click(editor_payload, [run_dir, ekey], [epayload]).then(None, [epayload], None, js=LOAD_JS)
            ekey.change(editor_payload, [run_dir, ekey], [epayload]).then(None, [epayload], None, js=LOAD_JS)
            eapply.click(apply_and_rebuild, [run_dir, ekey, eedits], [egallery, ezip, esummary], js="(o, k, e) => [o, k, JSON.stringify(window.banaEditor.export())]").then(editor_payload, [run_dir, ekey], [epayload]).then(None, [epayload], None, js=LOAD_JS)
            ereset.click(reset_edits, [run_dir], [egallery, ezip, esummary])
            erefresh.click(lambda: gr.update(choices=list_runs()), None, [eruns])
            eruns.change(pick_run, [eruns], [ekey, run_dir])
        with gr.Tab("Counties", id="counties"):
            gr.Markdown('<div class="help">Statewide sources cover every parcel in that state. For a county that is not listed, add it here: give the state and county, and either leave the URL blank (the app searches arcgis.com for the county\'s public parcel layer) or paste the parcel layer\'s REST URL from the county GIS site (…/MapServer/&lt;n&gt; or …/FeatureServer/&lt;n&gt;), or a CAMAvision map link (maps.camavision.com/…) — the pattern many Missouri and Iowa counties use. The county is checked live before it is saved.</div>')
            with gr.Row():
                cstate = gr.Dropdown([f"{k} — {v}" for k, v in S.STATES.items()], value="MO — Missouri", label="State", scale=1)
                cname = gr.Textbox(label="County", placeholder="Cass", scale=1)
                curl = gr.Textbox(label="Parcel layer URL (optional)", placeholder="https://…/MapServer/13  or  https://maps.camavision.com/…", scale=3)
                cadd = gr.Button("Add county", variant="primary", scale=1)
            cmsg = gr.Markdown()
            ctable = gr.Markdown(sources_markdown())
            cadd.click(lambda st, nm, u: add_county(st.split(" ")[0], nm, u), [cstate, cname, curl], [cmsg, ctable, county, pcounty])
        edit_btn.click(open_editor, [run_dir], [ekey, tabs])
        pedit_btn.click(open_editor, [run_dir], [ekey, tabs])

if __name__ == "__main__":
    demo.queue(default_concurrency_limit=2)
    demo.launch(server_name="0.0.0.0", server_port=int(os.environ.get("PORT", 7860)), theme=THEME, css=CSS, head=f"<script>{EDITOR_JS}</script>",
                auth=(("bana", PASSWORD) if PASSWORD else None))
