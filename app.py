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
    if not pin and (lat is None or lon is None):
        raise gr.Error("Enter a PIN or a lat/lon.")
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


with gr.Blocks(title="Bana Listing Photos") as demo:
    gr.Markdown("# Bana listing photo packet\nParcel in → annotated satellite set + AI drone views with property lines, out.")
    with gr.Row():
        county = gr.Dropdown(list(COUNTIES), value=list(COUNTIES)[0], label="County")
        pin = gr.Textbox(label="Parcel number (PIN / APN)", placeholder="20-0-0636-002-00")
    with gr.Row():
        lat = gr.Number(label="…or latitude", value=None); lon = gr.Number(label="longitude", value=None)
    with gr.Row():
        angles = gr.Textbox(value="S,E,W,N", label="Angles (camera side)")
        lights = gr.Textbox(value="golden,day", label="Lighting")
        render = gr.Checkbox(value=True, label="Generate AI drone views (costs ~$0.50)")
    go = gr.Button("Create packet", variant="primary")
    gallery = gr.Gallery(label="Packet", columns=4, height=600)
    zipf = gr.File(label="Download zip")
    logbox = gr.Textbox(label="Scene scan + log", lines=12)
    go.click(run, [county, pin, lat, lon, angles, lights, render], [gallery, zipf, logbox])

if __name__ == "__main__":
    demo.queue(default_concurrency_limit=2)
    demo.launch(server_name="0.0.0.0", server_port=int(os.environ.get("PORT", 7860)),
                auth=(("bana", PASSWORD) if PASSWORD else None))
