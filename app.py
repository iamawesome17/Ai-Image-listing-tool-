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
COUNTIES = json.loads((HERE / "counties.json").read_text())
COUNTY_LABELS = {k: v.get("name", k) for k, v in COUNTIES.items()}
PASSWORD = os.environ.get("APP_PASSWORD", "")


# ---------------- tab 1: generate from parcel ----------------
def run(county_label, pin, lat, lon, angles, lights, render, progress=gr.Progress()):
    county = next(k for k, v in COUNTY_LABELS.items() if v == county_label)
    pin = (pin or "").strip()
    if not pin and not (lat and lon):
        raise gr.Error("Enter a parcel number, or both a latitude and a longitude.")
    cmd = [sys.executable, str(HERE / "pipeline.py"), "--county", county, "--angles", angles, "--lights", lights]
    cmd += ["--pin", pin] if pin else ["--lat", str(lat), "--lon", str(lon)]
    if not render: cmd.append("--no-render")
    progress(0.05, desc="Parcel + surroundings scan…")
    t0 = time.time()
    proc = subprocess.Popen(cmd, cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    log, stage = [], 0.05
    marks = {"2/7": (0.15, "Scanning surroundings…"), "4/7": (0.25, "Annotated satellite set…"), "5/7": (0.35, "Rendering drone views (1–2 min)…"),
             "6/7": (0.85, "Placing property lines…"), "7/7": (0.95, "Packing zip…")}
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
    pk = sorted([p for p in out_dir.glob("packet_*") if p.is_dir()], key=lambda p: p.stat().st_mtime)
    zips = sorted(out_dir.glob("packet_*.zip"), key=lambda p: p.stat().st_mtime)
    images = sorted(pk[-1].glob("*.jpg")) + sorted(pk[-1].glob("*.png")) if pk else sorted(out_dir.glob("p1_*.png"))
    scan = json.loads((out_dir / "scan.json").read_text()).get("scene_description", "") if (out_dir / "scan.json").exists() else ""
    summary = f"**{len(images)} images · {round(time.time()-t0)} s**\n\n{scan}"
    return [(str(p), p.stem.replace("_", " ")) for p in images], (str(zips[-1]) if zips else None), summary, text[-4000:]


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


def run_photo(path, work, county_label, pin, golden, pts, progress=gr.Progress()):
    import drone_photo as D, listing_shot as ls, pipeline as P
    county = next(k for k, v in COUNTY_LABELS.items() if v == county_label)
    pin = (pin or "").strip()
    if not path: raise gr.Error("Upload a drone photo first.")
    if not pin: raise gr.Error("Enter the parcel number — it supplies the property line and the acreage card.")
    progress(0.1, desc="Parcel lookup…")
    cfg = COUNTIES[county]; parcel = P.fetch_parcel_county(cfg, pin)
    scan = None
    try:
        progress(0.25, desc="Surroundings scan…")
        poly = parcel["polygon"]; c = poly.centroid
        z = ls.choose_zoom(poly, 640, 480, margin=0.22); fr = ls.Frame(c.y, c.x, z, 640, 480)
        scan = P.scan_surroundings(cfg, parcel, ls.fetch_satellite(fr), fr)
    except Exception:
        scan = None
    points = [tuple(p) for p in pts] if pts and len(pts) >= 3 else None
    progress(0.4, desc="Golden-hour relight (≈1 min)…" if golden else "Drawing lines…")
    try:
        r = D.annotate(work or path, parcel, scan, points, golden, out_dir=HERE / "out" / parcel["apn"] / "photos", meta_path=path)
    except RuntimeError as e:
        raise gr.Error(str(e))
    note = f"Property line from **{r['source']}**."
    if r.get("meta"):
        m = r["meta"]; note += f" Camera: {m['model'] or 'drone'}, {m['alt_m']:.0f} m up, heading {m['yaw']:.0f}°, tilt {m['pitch']:.0f}°."
    note += " Check the line against what you know of the property — metadata placement is approximate (a few metres)."
    return [(f, Path(f).stem.replace("_", " ")) for f in r["files"]], note


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
"""

with gr.Blocks(title="Bana Listing Photos") as demo:
    gr.HTML('<div id="hero"><h1>Bana Listing Photos</h1><p>Parcel number in → listing-ready images out: true property lines on satellite and AI drone views, or on your own drone photos.</p></div>')

    with gr.Tab("Generate from parcel"):
        with gr.Row():
            with gr.Column(scale=1, min_width=320):
                gr.Markdown("### Property")
                county = gr.Dropdown(list(COUNTY_LABELS.values()), value=list(COUNTY_LABELS.values())[0], label="County")
                pin = gr.Textbox(label="Parcel number (PIN / APN)", placeholder="20-0-0636-002-00")
                gr.Markdown('<div class="help">…or locate by coordinates</div>')
                with gr.Row():
                    lat = gr.Number(label="Latitude", value=None); lon = gr.Number(label="Longitude", value=None)
                render = gr.Checkbox(value=True, label="Generate AI drone views (≈ $0.60, 1–2 min)")
                with gr.Accordion("Advanced", open=False):
                    angles = gr.CheckboxGroup(["S", "E", "W", "N", "NE", "SE", "SW", "NW"], value=["S", "E", "W", "N"], label="Camera positions")
                    lights = gr.CheckboxGroup(["golden", "day"], value=["golden", "day"], label="Lighting")
                go = gr.Button("Create packet", variant="primary", size="lg")
                summary = gr.Markdown()
                zipf = gr.File(label="Download packet (zip)")
            with gr.Column(scale=2):
                gallery = gr.Gallery(label="Packet", columns=3, height=640, object_fit="cover", show_label=False)
                with gr.Accordion("Run log", open=False):
                    logbox = gr.Textbox(lines=14, show_label=False)
        go.click(lambda c, p, la, lo, a, l, r: run(c, p, la, lo, ",".join(a) or "S", ",".join(l) or "golden", r),
                 [county, pin, lat, lon, angles, lights, render], [gallery, zipf, summary, logbox])

    with gr.Tab("Real drone photo"):
        with gr.Row():
            with gr.Column(scale=1, min_width=320):
                gr.Markdown("### Photo + property")
                photo = gr.Image(type="filepath", label="Drone photo", height=360, sources=["upload"])
                pstatus = gr.Markdown('<div class="help">Upload a photo straight from the drone to keep its GPS and gimbal data.</div>')
                pcounty = gr.Dropdown(list(COUNTY_LABELS.values()), value=list(COUNTY_LABELS.values())[0], label="County")
                ppin = gr.Textbox(label="Parcel number (PIN / APN)", placeholder="20-0-0636-002-00")
                pgolden = gr.Checkbox(value=True, label="Add a golden-hour version (≈ $0.05)")
                pclear = gr.Button("Clear corners", size="sm")
                pgo = gr.Button("Annotate photo", variant="primary", size="lg")
                pnote = gr.Markdown()
            with gr.Column(scale=2):
                pgallery = gr.Gallery(label="Results", columns=2, height=640, object_fit="contain", show_label=False)
        pts_state = gr.State([]); orig_state = gr.State(None); work_state = gr.State(None)
        photo.upload(photo_uploaded, [photo], [orig_state, work_state, pts_state, pstatus, photo])
        photo.select(photo_click, [work_state, pts_state], [pts_state, photo, pstatus])
        pclear.click(photo_clear, [work_state], [pts_state, photo, pstatus])
        pgo.click(run_photo, [orig_state, work_state, pcounty, ppin, pgolden, pts_state], [pgallery, pnote])

if __name__ == "__main__":
    demo.queue(default_concurrency_limit=2)
    demo.launch(server_name="0.0.0.0", server_port=int(os.environ.get("PORT", 7860)), theme=THEME, css=CSS,
                auth=(("bana", PASSWORD) if PASSWORD else None))
