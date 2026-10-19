# Bana Listing Photo Pipeline — Process Record

One command turns a parcel ID into a listing photo packet: true-boundary satellite shots plus AI "drone" views
from multiple compass angles, each with the property line, pin, acreage card and disclaimer. ~60–90 s, ~$0.50/property.

    python pipeline.py --county adams-il --pin 20-0-0636-002-00
    python pipeline.py --county adams-il --lat 39.91722 --lon -91.2892
    python pipeline.py --regrid --apn 300006840 --path /us/ne/fillmore        # counties covered by your Regrid plan

Output: out/<PIN>/packet_<address>.zip (numbered images + README + scan.json) and out/<PIN>/run.json (every API call).

## Stages

| # | Stage | What happens | Data / API | Output |
|---|-------|--------------|------------|--------|
| 1 | parcel | Look up the parcel polygon, acreage, situs, owner, use code | County ArcGIS REST layer (counties.json) or Regrid v2 | parcel.geojson |
| 2 | scan | Surroundings scan: roads (name, side, adjacent?), named/unnamed streams (crosses parcel? distance), building footprints inside the parcel, FEMA flood zone touching the parcel, neighbor land uses by side, Google Places POIs, land-cover split from satellite pixels | County layers roads/streams/buildings/FIRM/parcels; Google Places Nearby; pixel stats | scan.json + one-paragraph scene description + do-not-invent list |
| 3 | satellite | 640×480 @2× satellite reference, clean and with the magenta boundary drawn server-side (Static Maps `path=`) | Google Maps Static API | ref_clean.png, ref_fiducial.png |
| 4 | phase1 | Annotated satellite set: cover 4:3, tight, wide, clean — outline, pin, acreage card, road labels, POI pins, disclaimer | Pillow compositor (listing_shot.py) | p1_*.png |
| 5 | render | For each view: reference rotated so the camera side is at the bottom (uploaded), prompt = camera + lighting + scene facts + "keep the magenta boundary"; submit, poll, download | Higgsfield API, model `xai/grok-imagine-image-2.0` (1k, 4:3) | ai/<model>_<view>_<light>.jpg, ai/prompts.json |
| 6 | outline | Detect the magenta marker in each render (HSV threshold → largest contour → simplified polygon), inpaint it away, draw the styled outline + pin + card + disclaimer | OpenCV + Pillow | ai/*_outline.jpg, ai/outline_report.json |
| 7 | packet | Number files in Zillow order (golden-hour cover first), README, zip, run log | — | packet_*.zip, run.json |

Default plan: nadir ×2 (day, golden) + S/E/W/N ×2 = 10 renders at 2K; close-ups are crops of the same renders (no extra cost).
`--angles` and `--lights` override. Diagonals (NE/SE/SW/NW) need the upload host (see Hosting/Domains).

## Why it is built this way (decisions)

- **Boundary as a fiducial, not georeferencing.** Feature matching (SIFT) between a satellite tile and an AI render fails on
  obliques because the model re-textures everything. Drawing the boundary in magenta on the reference and telling the model
  it is painted on the ground makes the model carry it through perspective; we then detect and restyle it. 14/14 success on the first run.
- **Rotate the reference, don't ask for an angle.** Grok keeps the reference orientation and treats the bottom edge as the camera
  side. Compass angle is achieved by rotating the satellite image by (bearing − 180°) before sending. Unrotated = view from the south.
- **Model-side rotation when uploads are blocked.** The rotated reference has to be a public URL. If the Higgsfield upload host is
  unreachable, the pipeline asks Grok for an exact 90°/180° rotation of the public Static Maps reference and uses that output's CDN
  URL as the reference (cached in out/<PIN>/ref_rot_urls.json for 7 days). Verified pixel-exact; costs one extra 1K image per angle.
- **Only one marker.** A second colored line for the road made the model drop the boundary. Road labels are placed geometrically
  instead: the road's compass side (from the scan) is rotated into the render's frame and the label goes on the polygon edge facing it.
- **2K with a 1K retry.** Day-lit 2K renders sometimes omit the marker; the pipeline detects the missing marker and re-renders once at 1K.
- **Zoom by cropping.** The model always widens the scene, so framing is done after: standard = polygon bbox + 40% margin,
  close-up = + 10%, both resized to 1152×864 from the 2K render.
- **Reference URL must stay short.** Static Maps URLs over ~1.5k characters fail inside the model's fetch ("Generation failed").
  Road paths are clipped to the frame and simplified; the boundary is simplified to ~20 points.
- **Scene facts in the prompt + do-not-invent list.** Models add ponds and buildings. The scan states what exists (water, buildings,
  roads by side, land cover) so the prompt can forbid the rest. Still review every render.
- **Static Maps `path=` for the fiducial** avoids needing to host a file for the un-rotated reference; rotated references must be uploaded
  (Higgsfield presigned S3 upload).
- **Grok over Qwen.** Qwen Image 3 was unavailable 6/8 jobs and invented a different landscape on the 2 that ran. Grok Image 2.0 held layout 22/22.
- **Google attribution stays on images.** Outputs are requested at their final aspect ratio instead of cropped (Static Maps terms).

## Accounts, keys and allowed domains

Keys live in `.env` (or `keys.env`) next to the scripts — never in the packet or the repo:

    REGRID_TOKEN=...          regrid.com — API trial covers 7 demo counties; nationwide needs the self-serve plan
    GOOGLE_MAPS_KEY=...       Google Cloud project with Maps Static API, Places API (New), Geocoding API enabled
    HIGGSFIELD_KEY=id:secret  console.higgsfield.ai — prepaid balance ($5 min); key shown once

Google Cloud APIs: Maps Static API (required) · Places API (New) (POI pins) · Geocoding API (road labels outside county-GIS areas).
Add each to the key's API restrictions list after enabling.

Domains the workspace must be allowed to reach (claude.ai → Settings → Capabilities):
`app.regrid.com`, `maps.googleapis.com`, `places.googleapis.com`, `api.higgsfield.ai`,
`d3u0tzju9qaucj.cloudfront.net` (render downloads), `fnf-api-input-prod-20250414194641741400000002.s3.amazonaws.com` (reference uploads),
`www.adamscountyarcserver.com` (Adams County GIS). Add a county's GIS host when you add that county to counties.json.

## Adding a county

1. Find the county's public ArcGIS REST endpoint (search "<county> GIS arcgis rest services"; the county GIS page usually lists it).
2. Open `<server>?f=pjson` and note the layer ids for parcels, road centerlines, streams, building footprints, FIRM flood zones.
3. Open the parcels layer and note the field names for PIN, site address, acres, owner, use code.
4. Add a block to counties.json (copy adams-il), map the fields, add use-code meanings if the county publishes them.
5. Allow the host, then run `pipeline.py --county <key> --pin <PIN> --no-render` to check the parcel + scan before paying for renders.
Counties on Regrid skip all of this (`--regrid`), but Regrid has no road/stream/building/flood layers — the scan is thinner.

## Hosting (so the team runs it without anyone's laptop)

`app.py` is a Gradio web form (county + PIN → gallery + zip download) with a shared password; `Dockerfile` + `requirements.txt`
package it. Three ways to put it on the web, cheapest-viable first:

| Option | Monthly | Setup effort | Notes |
|---|---|---|---|
| **Railway** (recommended) | ~$5–10 | 15 min: new project → deploy from GitHub repo → add env vars (keys + APP_PASSWORD) | Always-on URL like bana-listings.up.railway.app; logs; easy redeploys. Run time 1–3 min per packet is fine. |
| Hugging Face Spaces (Docker) | $0 public / $9 private-ish | 10 min: create Space → upload files → secrets | Simplest UI hosting; free tier sleeps and is public, so use a paid private Space. |
| Render / Fly.io | ~$7 | 15 min | Same as Railway; Render's free tier sleeps 50 s on first hit. |

Not recommended: Streamlit Cloud (public only), a desktop app (back on one machine), Zapier/Make (can't run the image math).

Deploy steps (Railway): 1) put this folder in a private GitHub repo (no .env — keys go in Railway → Variables:
REGRID_TOKEN, GOOGLE_MAPS_KEY, HIGGSFIELD_KEY, APP_PASSWORD). 2) Railway → New Project → Deploy from GitHub → it detects the
Dockerfile. 3) Settings → Networking → Generate Domain. 4) Share URL + password with the team. Output zips live in the container's
out/ folder (ephemeral): add a Railway volume at /app/out, or push packets to a Drive/Dropbox folder (next improvement).

## Costs (list prices, Oct 2026)

| Item | Per property |
|---|---|
| County GIS | $0 |
| Static Maps (4–6 requests) + Places (1) | ~$0.07 |
| Grok Image 2.0 via Higgsfield, 10 renders at 2K + 3 rotations + retries | ~$0.50–0.80 (failed jobs are not charged) |
| Regrid lookup (where used) | $0.05–0.10 |

## Known limits / next improvements

- Outline on AI renders is where the model carried the marker — approximate by design; the satellite cover (p1_cover) is the accurate reference.
- Simplified polygon on renders (approxPolyDP) — fine for L-shapes and notches, may round very fine jogs.
- Road labels and POI pins are drawn on satellite images only (positions in AI renders are unknown).
- Rural building footprints are not in the Adams County layer outside the Quincy tri-township; the scan says "0 buildings" there even when sheds exist.
- A lot of under ~0.25 ac on soft (upscaled) satellite imagery gives the model little to hold onto — expect more invention.
- Powerlines: no county layer; OpenStreetMap has them (allow `overpass-api.de`) — the pipeline already draws and labels them when data exists.
  Until then, add known features by hand (next: a `--features` file for pond / driveway / electric poles callouts).
- Diagonal angles (NE/SE/SW/NW) need the upload host `fnf-api-input-prod-…s3.amazonaws.com` allowed; the model-rotation trick only does 90°/180°.
- Not yet done: orbit video (Kling/Seedance image-to-video on Higgsfield), listing-copy generation, push to Notion/Airtable deal record.
