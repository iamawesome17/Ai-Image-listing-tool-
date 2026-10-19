#!/usr/bin/env python3
"""
drone_render.py — Phase 2 of the Bana disposition image tool.

Satellite tile → AI "drone" renders (oblique angles, day + golden hour) via the Higgsfield API,
then the true parcel outline is mapped back onto each render where it can be matched reliably.

Usage:
  python drone_render.py --lat 40.6349 --lon -97.5760
  python drone_render.py --apn 300006840 --path /us/ne/fillmore --model grok
  python drone_render.py --address "1214 Road G, Fairmont, NE" --angles N,E --no-golden

Options:
  --model qwen|grok     image model on Higgsfield (default qwen)
  --angles N,NE,E,SE,S  camera positions to render (default N,E,S)
  --no-golden           skip golden-hour versions
  --nadir-only          only the straight-down "enhanced" pair (cheapest, always georeferenced)
  --out DIR             output folder (default out/<apn>/ai)

Outputs: out/<apn>/ai/<model>_<view>_<light>.png (raw render)
         out/<apn>/ai/<model>_<view>_<light>_outline.png (render + outline, when the match passed QA)
         out/<apn>/ai/renders.json (prompts, request ids, match quality per image)
"""
import argparse, json, os, sys, time, uuid, io, urllib.request, urllib.parse
from pathlib import Path

import numpy as np
import cv2
from PIL import Image

sys.path.insert(0, str(Path(__file__).parent))
import listing_shot as ls

API = "https://api.higgsfield.ai"
MODELS = {
    "qwen": {"endpoint": "/alibaba/qwen-image-3/edit", "extra": {"resolution": "1k", "aspect_ratio": "4:3",
                                                                 "prompt_extend": False, "enable_thinking": False}},
    "grok": {"endpoint": "/xai/grok-imagine-image-2.0", "extra": {"resolution": "1k", "aspect_ratio": "4:3", "quality": "medium"}},
}
OPPOSITE = {"N": "south", "NE": "southwest", "E": "west", "SE": "northwest", "S": "north", "SW": "northeast", "W": "east", "NW": "southeast"}
LIGHT = {
    "day": "bright midday sunlight, clear sky, natural colors",
    "golden": "golden hour, low warm sunset light from the west, long soft shadows, warm sky at the horizon",
}
KEEP = ("Keep every road, tree line, field edge, fence line, driveway, building and pond exactly where it is in the source "
        "image; do not add, move or remove any structure or feature. Photorealistic, sharp, 24mm lens, no people, no vehicles, "
        "no text, no labels, no watermark, no map pins.")


def prompt_for(view, light):
    if view == "nadir":
        return (f"Turn this satellite image into a crisp professional drone photograph taken straight down from 400 ft, "
                f"{LIGHT[light]}. Same framing, same scale. {KEEP}")
    return (f"Turn this satellite image into a realistic drone photograph. The drone hovers over the {view_name(view)} edge of "
            f"this scene at about 250 ft, camera tilted 35 degrees down, looking {OPPOSITE[view]} across the same land so the "
            f"horizon is visible at the top of the frame. {LIGHT[light]}. {KEEP}")


def view_name(v):
    return {"N": "north", "NE": "northeast", "E": "east", "SE": "southeast", "S": "south", "SW": "southwest", "W": "west", "NW": "northwest"}[v]


# ---------- Higgsfield client ----------
def hf_headers():
    return {"Authorization": f"Key {os.environ['HIGGSFIELD_KEY']}", "Content-Type": "application/json"}


def hf_post(path, body, idem=None):
    h = hf_headers()
    if idem:
        h["Idempotency-Key"] = idem
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(), headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def hf_get(url):
    req = urllib.request.Request(url, headers=hf_headers())
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def static_map_url(frame):
    q = {"center": f"{frame.lat},{frame.lon}", "zoom": frame.z, "size": f"{frame.w}x{frame.h}",
         "scale": ls.SCALE, "maptype": "satellite", "format": "png", "key": os.environ["GOOGLE_MAPS_KEY"]}
    return "https://maps.googleapis.com/maps/api/staticmap?" + urllib.parse.urlencode(q)


def hf_upload_png(img):
    buf = io.BytesIO(); img.convert("RGB").save(buf, "PNG"); data = buf.getvalue()
    u = hf_post("/files/generate-upload-url", {"content_type": "image/png"})
    hdr = {k: v for k, v in u.get("upload_headers", {}).items()}
    req = urllib.request.Request(u["upload_url"], data=data, headers=hdr, method="PUT")
    with urllib.request.urlopen(req, timeout=120) as r:
        r.read()
    return u["public_url"]


def hf_submit(model, prompt, image_url):
    m = MODELS[model]
    body = {"prompt": prompt, "image_urls": [image_url], **m["extra"]}
    return hf_post(m["endpoint"], body, idem=str(uuid.uuid4()))


def hf_wait_all(jobs, timeout_s=900):
    """jobs: dict key → submission dict. Polls until every job is terminal."""
    t0, delay = time.time(), 4
    results = {}
    while len(results) < len(jobs) and time.time() - t0 < timeout_s:
        for k, j in jobs.items():
            if k in results:
                continue
            s = hf_get(j["status_url"])
            if s["status"] in ("completed", "failed", "nsfw", "canceled"):
                results[k] = s
                print(f"    {k}: {s['status']}", flush=True)
        if len(results) < len(jobs):
            time.sleep(delay); delay = min(delay + 2, 15)
    for k in jobs:
        results.setdefault(k, {"status": "timeout"})
    return results


def download(url):
    return Image.open(io.BytesIO(ls.http_bytes(url))).convert("RGBA")


# ---------- georeference: ortho → render ----------
def match_homography(ortho, render, min_inliers=40, min_ratio=0.25):
    """SIFT + RANSAC homography from the ortho tile to the AI render. Returns (H, quality) or (None, info)."""
    a = cv2.cvtColor(np.array(ortho.convert("RGB")), cv2.COLOR_RGB2GRAY)
    b = cv2.cvtColor(np.array(render.convert("RGB")), cv2.COLOR_RGB2GRAY)
    sift = cv2.SIFT_create(nfeatures=6000)
    ka, da = sift.detectAndCompute(a, None)
    kb, db = sift.detectAndCompute(b, None)
    if da is None or db is None or len(ka) < 20 or len(kb) < 20:
        return None, {"reason": "too few features"}
    m = cv2.BFMatcher(cv2.NORM_L2).knnMatch(da, db, k=2)
    good = [x for x, y in (p for p in m if len(p) == 2) if x.distance < 0.75 * y.distance]
    if len(good) < min_inliers:
        return None, {"reason": f"only {len(good)} good matches"}
    src = np.float32([ka[g.queryIdx].pt for g in good]).reshape(-1, 1, 2)
    dst = np.float32([kb[g.trainIdx].pt for g in good]).reshape(-1, 1, 2)
    H, mask = cv2.findHomography(src, dst, cv2.RANSAC, 6.0)
    if H is None:
        return None, {"reason": "no homography"}
    inl = int(mask.sum()); ratio = inl / len(good)
    # sanity: the homography must keep the image roughly upright and not fold
    corners = np.float32([[0, 0], [a.shape[1], 0], [a.shape[1], a.shape[0]], [0, a.shape[0]]]).reshape(-1, 1, 2)
    w = cv2.perspectiveTransform(corners, H).reshape(-1, 2)
    area = 0.5 * abs(sum(w[i][0] * w[(i + 1) % 4][1] - w[(i + 1) % 4][0] * w[i][1] for i in range(4)))
    if inl < min_inliers or ratio < min_ratio or area < 0.15 * a.shape[0] * a.shape[1] or area > 4 * a.shape[0] * a.shape[1]:
        return None, {"reason": f"weak match: {inl} inliers, ratio {ratio:.2f}", "inliers": inl, "ratio": round(ratio, 2)}
    return H, {"inliers": inl, "ratio": round(ratio, 2), "good": len(good)}


class WarpedFrame:
    """A Frame whose pixel coordinates are pushed through a homography into the render's pixel space."""
    def __init__(self, base, M, W, H):
        self.base, self.M, self.W, self.H = base, M, W, H   # M = homography; W,H = render size (Frame API)

    def px(self, lon, lat):
        x, y = self.base.px(lon, lat)
        p = cv2.perspectiveTransform(np.float32([[[x, y]]]), self.M)[0][0]
        return float(p[0]), float(p[1])

    def inside(self, lon, lat, pad=40):
        x, y = self.px(lon, lat)
        return pad <= x <= self.W - pad and pad <= y <= self.H - pad

    def bbox_px(self, poly):
        xs, ys = zip(*[self.px(x, y) for x, y in poly.exterior.coords])
        return (min(xs), min(ys), max(xs), max(ys))


# ---------- main ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apn"); ap.add_argument("--path"); ap.add_argument("--address")
    ap.add_argument("--lat", type=float); ap.add_argument("--lon", type=float)
    ap.add_argument("--geojson"); ap.add_argument("--arcgis")
    ap.add_argument("--city"); ap.add_argument("--state"); ap.add_argument("--zip"); ap.add_argument("--county")
    ap.add_argument("--model", default="qwen", choices=list(MODELS))
    ap.add_argument("--angles", default="N,E,S"); ap.add_argument("--no-golden", action="store_true")
    ap.add_argument("--nadir-only", action="store_true"); ap.add_argument("--no-pois", action="store_true")
    ap.add_argument("--ref", default="upload", choices=["upload", "static-url"], help="how the model gets the satellite reference")
    ap.add_argument("--submit-only", action="store_true", help="submit jobs and write jobs.json; finish later with --resume")
    ap.add_argument("--resume", help="jobs.json from a --submit-only run")
    ap.add_argument("--out"); ap.add_argument("--env", default=str(Path(__file__).with_name(".env")))
    a = ap.parse_args()
    ls.load_env(a.env)
    if not os.environ.get("HIGGSFIELD_KEY"):
        sys.exit("Missing HIGGSFIELD_KEY in env file")

    print("1/5 parcel …", flush=True)
    defaults = {"city": a.city or "", "state": a.state or "", "zip": a.zip or "", "county": a.county or ""}
    if a.geojson:
        parcel = ls.fetch_parcel_geojson(a.geojson, defaults)
    elif a.arcgis:
        parcel = ls.fetch_parcel_arcgis(a.arcgis, a.lat, a.lon, defaults)
    else:
        parcel = ls.fetch_parcel(a.apn, a.path, a.address, a.lat, a.lon)
    poly = parcel["polygon"]; c = poly.centroid
    print(f"    {parcel['apn']} | {parcel['situs']} {parcel['city']} {parcel['state']} | {parcel['acres']} ac")

    print("2/5 satellite reference …", flush=True)
    z = ls.choose_zoom(poly, 640, 480, margin=0.45)     # extra margin: the AI needs context around the parcel
    frame = ls.Frame(c.y, c.x, z, 640, 480)
    ortho = ls.fetch_satellite(frame)
    roads = ls.road_labels(poly)
    pois = [] if a.no_pois else ls.nearby_pois(c.y, c.x)[:3]

    print("3/5 upload + submit renders …", flush=True)
    ref_url = static_map_url(frame) if a.ref == "static-url" else hf_upload_png(ortho)
    views = ["nadir"] if a.nadir_only else ["nadir"] + [v.strip().upper() for v in a.angles.split(",") if v.strip()]
    lights = ["day"] if a.no_golden else ["day", "golden"]
    jobs, prompts = {}, {}
    for v in views:
        for l in lights:
            key = f"{a.model}_{v}_{l}"
            prompts[key] = prompt_for(v, l)
            jobs[key] = hf_submit(a.model, prompts[key], ref_url)
            print(f"    submitted {key} → {jobs[key]['request_id']}", flush=True)

    out = Path(a.out or f"out/{parcel['apn'] or 'parcel'}/ai"); out.mkdir(parents=True, exist_ok=True)
    (out / "jobs.json").write_text(json.dumps({"jobs": jobs, "prompts": prompts}, indent=2))
    if a.submit_only:
        print(f"submitted {len(jobs)} jobs → {out}/jobs.json"); return

    print("4/5 waiting for renders …", flush=True)
    results = hf_wait_all(jobs)

    print("5/5 georeference + overlay …", flush=True)
    ortho.convert("RGB").save(out / "reference_satellite.png")
    report = {"parcel": {k: v for k, v in parcel.items() if k not in ("polygon", "raw")}, "model": a.model, "zoom": z, "renders": {}}
    for key, s in results.items():
        rec = {"status": s.get("status"), "request_id": jobs[key]["request_id"], "prompt": prompts[key]}
        if s.get("status") != "completed":
            rec["error"] = s.get("error"); report["renders"][key] = rec; continue
        rec["url"] = s["images"][0]["url"]
        local = out / f"{key}.png"
        if local.exists():
            img = Image.open(local).convert("RGBA")
        else:
            try:
                img = download(rec["url"]); img.convert("RGB").save(local)
            except Exception as e:
                rec["download_error"] = str(e); report["renders"][key] = rec
                print(f"    {key}: rendered, download blocked ({rec['url']})"); continue
        view = key.split("_")[1]
        if view == "nadir":
            # same framing as the reference: scale only
            sx, sy = img.width / ortho.width, img.height / ortho.height
            H = np.float32([[sx, 0, 0], [0, sy, 0], [0, 0, 1]]); q = {"method": "identity-scale"}
        else:
            H, q = match_homography(ortho, img)
        rec["match"] = q
        if H is not None:
            wf = WarpedFrame(frame, H, img.width, img.height)
            over = ls.compose(img, wf, parcel, roads, pois)
            over.convert("RGB").save(out / f"{key}_outline.png")
            rec["outline"] = f"{key}_outline.png"
        report["renders"][key] = rec
        print(f"    {key}: {'outline ok' if H is not None else 'no outline (' + q.get('reason', '') + ')'}")
    (out / "renders.json").write_text(json.dumps(report, indent=2))
    print(f"done → {out}/")


if __name__ == "__main__":
    main()
