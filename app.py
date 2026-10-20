"""
app.py — web front end for the listing photo pipeline (Gradio).

Run locally:   python app.py            → http://localhost:7860
Deploy:        see PROCESS.md → "Hosting". Needs the same .env keys as pipeline.py plus APP_PASSWORD.

The form takes a county + PIN (or lat/lon), runs pipeline.py, and shows the packet images with a zip download.
"""
import os, subprocess, sys, json, time, shutil
from pathlib import Path
import gradio as gr

HERE = Path(__file__).parent
COUNTIES = json.loads((HERE / "counties.json").read_text())
PASSWORD = os.environ.get("APP_PASSWORD", "")


def run(county, pin, lat, lon, angles, lights, render):
    pin = (pin or "").strip()
    if not pin and not (lat and lon):          # Gradio shows empty numbers as 0
        raise gr.Error("Enter a PIN, or both a latitude and a longitude.")
    cmd = [sys.executable, str(HERE / "pipeline.py"), "--county", county, "--angles", angles, "--lights", lights]
    cmd += ["--pin", pin] if pin else ["--lat", str(lat), "--lon", str(lon)]
    if not render: cmd.append("--no-render")
    t0 = time.time()
    proc = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True, timeout=1800)
    log = proc.stdout + proc.stderr
    if proc.returncode != 0:
        raise gr.Error("Run failed — see log.\n" + log[-2000:])
    # locate outputs
    m = [l for l in log.splitlines() if l.startswith("done")]
    out_dir = None
    for line in log.splitlines():
        if "done in" in line and "→" in line:
            out_dir = Path(line.split("→")[1].strip().split(" ")[0]).parent
    if out_dir is None:
        for line in log.splitlines():
            if line.startswith("4/7") or line.startswith("done (phase 1"): pass
        out_dir = sorted((HERE / "out").glob("*"), key=lambda p: p.stat().st_mtime)[-1]
    zips = sorted(out_dir.glob("packet_*.zip"), key=lambda p: p.stat().st_mtime)
    pk = sorted([p for p in out_dir.glob("packet_*") if p.is_dir()], key=lambda p: p.stat().st_mtime)
    images = sorted(pk[-1].glob("*.jpg")) + sorted(pk[-1].glob("*.png")) if pk else sorted(out_dir.glob("p1_*.png"))
    scan = json.loads((out_dir / "scan.json").read_text()).get("scene_description", "") if (out_dir / "scan.json").exists() else ""
    return [str(p) for p in images], (str(zips[-1]) if zips else None), f"{scan}\n\nDone in {round(time.time()-t0)}s\n\n" + log[-3000:]


def photo_points_add(img, pts, evt: gr.SelectData):
    pts = list(pts or []); pts.append([float(evt.index[0]), float(evt.index[1])])
    return pts, draw_points(img, pts), f"{len(pts)} corner(s) marked"


def photo_points_clear(img):
    return [], img, "0 corners marked"


def draw_points(img, pts):
    from PIL import Image, ImageDraw, ImageOps
    if img is None: return None
    im = ImageOps.exif_transpose(Image.open(img)).convert("RGB") if isinstance(img, str) else img.convert("RGB").copy()
    d = ImageDraw.Draw(im); r = max(6, im.width // 150)
    if len(pts) >= 2: d.line([tuple(p) for p in pts] + ([tuple(pts[0])] if len(pts) >= 3 else []), fill=(255, 0, 255), width=max(3, im.width // 400))
    for x, y in pts: d.ellipse((x - r, y - r, x + r, y + r), fill=(255, 0, 255), outline="white", width=2)
    return im


def run_photo(photo_path, county, pin, golden, pts):
    import drone_photo as D
    pin = (pin or "").strip()
    if not photo_path: raise gr.Error("Upload a drone photo.")
    if not pin: raise gr.Error("Enter the parcel PIN (for the property line and the acreage card).")
    import listing_shot as ls, pipeline as P
    cfg = COUNTIES[county]; parcel = P.fetch_parcel_county(cfg, pin)
    scan = None
    try:
        from PIL import Image
        poly = parcel["polygon"]; c = poly.centroid
        z = ls.choose_zoom(poly, 640, 480, margin=0.22); fr = ls.Frame(c.y, c.x, z, 640, 480)
        scan = P.scan_surroundings(cfg, parcel, ls.fetch_satellite(fr), fr)
    except Exception:
        scan = None
    points = [tuple(p) for p in pts] if pts and len(pts) >= 3 else None
    try:
        r = D.annotate(photo_path, parcel, scan, points, golden, out_dir=HERE / "out" / parcel["apn"] / "photos")
    except RuntimeError as e:
        raise gr.Error(str(e))
    note = f"Property line from: {r['source']}." + ("" if r.get("meta") is None else f"  Camera: {r['meta']['model']} alt {r['meta']['alt_m']:.0f} m, yaw {r['meta']['yaw']:.0f}°, pitch {r['meta']['pitch']:.0f}°.")
    return r["files"], note


with gr.Blocks(title="Bana Listing Photos") as demo:
    gr.Markdown("# Bana listing photo packet\nParcel in → annotated satellite set + AI drone views with property lines, out.")
    with gr.Tab("Generate from parcel"):
      with gr.Row():
          county = gr.Dropdown(list(COUNTIES), value=list(COUNTIES)[0], label="County")
          pin = gr.Textbox(label="Parcel number (PIN / APN)", placeholder="20-0-0636-002-00")
      with gr.Row():
          lat = gr.Number(label="…or latitude", value=None); lon = gr.Number(label="longitude", value=None)
      with gr.Row():
          angles = gr.Textbox(value="S,E,W,N", label="Angles (camera side; also NE,SE,SW,NW)")
          lights = gr.Textbox(value="golden,day", label="Lighting")
          render = gr.Checkbox(value=True, label="Generate AI drone views (costs ~$0.50)")
      go = gr.Button("Create packet", variant="primary")
      gallery = gr.Gallery(label="Packet", columns=4, height=600)
      zipf = gr.File(label="Download zip")
      logbox = gr.Textbox(label="Scene scan + log", lines=12)
      go.click(run, [county, pin, lat, lon, angles, lights, render], [gallery, zipf, logbox])

    with gr.Tab("Real drone photo"):
      gr.Markdown("Upload a drone photo. If it has DJI metadata the property line is placed automatically; otherwise click the parcel corners on the photo in order. Optional golden-hour relight.")
      with gr.Row():
        pcounty = gr.Dropdown(list(COUNTIES), value=list(COUNTIES)[0], label="County")
        ppin = gr.Textbox(label="Parcel number (PIN / APN)", placeholder="20-0-0636-002-00")
        pgolden = gr.Checkbox(value=True, label="Add golden-hour version (costs ~$0.05)")
      photo = gr.Image(type="filepath", label="Drone photo (click corners here if needed)", height=520)
      pts_state = gr.State([])
      with gr.Row():
        pstatus = gr.Textbox(value="0 corners marked", label="Corners", interactive=False)
        pclear = gr.Button("Clear corners")
      pgo = gr.Button("Annotate photo", variant="primary")
      pgallery = gr.Gallery(label="Results", columns=3, height=520)
      pnote = gr.Textbox(label="Notes", lines=2)
      orig_state = gr.State(None)
      photo.upload(lambda img: (img, [], "0 corners marked"), [photo], [orig_state, pts_state, pstatus])
      photo.select(photo_points_add, [orig_state, pts_state], [pts_state, photo, pstatus])
      pclear.click(lambda img: ([], img, "0 corners marked"), [orig_state], [pts_state, photo, pstatus])
      pgo.click(run_photo, [orig_state, pcounty, ppin, pgolden, pts_state], [pgallery, pnote])

if __name__ == "__main__":
    demo.queue(default_concurrency_limit=2)
    demo.launch(server_name="0.0.0.0", server_port=int(os.environ.get("PORT", 7860)),
                auth=(("bana", PASSWORD) if PASSWORD else None))
