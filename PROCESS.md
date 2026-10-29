# Bana Listing Photo Pipeline — Process Record

One command turns a parcel ID into a listing photo packet: true-boundary satellite shots plus AI "drone" views
from multiple compass angles, each with the property line, pin, acreage card and disclaimer. ~60–90 s, ~$0.50/property.

    python pipeline.py --county adams-il --pin 20-0-0636-002-00
    python pipeline.py --county adams-il --lat 39.91722 --lon -91.2892
    python pipeline.py --county nc --pin 0096341                             # statewide layers: nc, fl, va, tn
    python pipeline.py --county tn --pin "002 001.02" --county-name McNairy  # TN needs the county (map+parcel repeats statewide)
    python pipeline.py --boundary parcel.kml --label "Smith 40"               # any parcel: Land id / Google Earth export
    python pipeline.py --regrid --apn 300006840 --path /us/ne/fillmore        # counties covered by your Regrid plan
    python drone_photo.py DJI_0042.JPG --county adams-il --pin 20-0-0636-002-00 --golden   # real drone photo

Live tool: https://ai-image-listing-tool-production.up.railway.app (shared login; see Hosting).

Output: out/<PIN>/packet_<address>.zip (numbered images + README + scan.json) and out/<PIN>/run.json (every API call).

## Stages

| # | Stage | What happens | Data / API | Output |
|---|-------|--------------|------------|--------|
| 1 | parcel | Look up the parcel polygon, acreage, situs, owner, use code | County or statewide ArcGIS REST parcel layer (counties.json), Regrid v2, or a boundary file (KML/KMZ/GeoJSON/zipped SHP); county/city/zip filled by Google Geocoding when the layer lacks them | parcel.geojson |
| 2 | scan | Surroundings scan: roads (name, side, adjacent?, geometry), named/unnamed streams (crosses parcel? distance), ponds, building footprints inside the parcel, FEMA flood zone touching the parcel, neighbor land uses by side, Google Places POIs, OSM power lines, land-cover split from satellite pixels | `scan: county` → county layers; `scan: federal` → TIGERweb roads (layers 2/3/8, field NAME), NHD streams/waterbodies, USA Structures, FEMA NFHL, neighbors from the same parcel layer; Overpass; pixel stats | scan.json + one-paragraph scene description + do-not-invent list |
| 3 | satellite | 640×480 @2× satellite reference, clean and with the magenta boundary drawn server-side (Static Maps `path=`) | Google Maps Static API | ref_clean.png, ref_fiducial.png |
| 4 | phase1 | Annotated satellite set: cover 4:3, tight, wide, clean — outline, pin, acreage card, road labels, POI pins, disclaimer | Pillow compositor (listing_shot.py) | p1_*.png |
| 5 | render | For each view: reference rotated so the camera side is at the bottom (uploaded), prompt = camera + lighting + scene facts + "keep the magenta boundary"; submit, poll, download | Higgsfield API, model `xai/grok-imagine-image-2.0` (1k, 4:3) | ai/<model>_<view>_<light>.jpg, ai/prompts.json |
| 6 | outline | Detect the magenta marker in each render (HSV threshold → largest contour → simplified polygon), inpaint it away, draw the styled outline + pin + card + disclaimer | OpenCV + Pillow | ai/*_outline.jpg, ai/outline_report.json |
| 7 | packet | Number files in Zillow order (golden-hour cover first), README, zip, run log | — | packet_*.zip, run.json |

Default plan: nadir ×2 (day, golden) + S/E/W/N ×2 + 2 low-horizon views ×2 = 14 renders at 2K (+ a 1K retry for any render that loses the marker); close-ups are crops of the same renders (no extra cost). `--res 1k`, `--lights golden`, `--low none` each cut the bill.
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

## Parcel sources (counties.json)

| Key | Coverage | Parcel number | Scan | Notes |
|---|---|---|---|---|
| `adams-il` | Adams County, IL | PIN (digits compared, dashes ignored) | county layers | richest scan (county roads/streams/buildings/FIRM) |
| `nc` | North Carolina statewide (NC OneMap) | `parno` as on the county site | federal | parcel numbers are not unique statewide in a few counties — first match wins |
| `fl` | Florida statewide (FDACS parcels) | `PARCEL_ID` | federal | no situs field → Geocoding fills street/city/zip |
| `va` | Virginia statewide (VGIN) | `PARCELID` (localities format these differently; spacing is normalized) | federal | county from `LOCALITY`; Geocoding fills the rest |
| `tn` | Tennessee statewide (Comptroller "Tennessee Property Boundaries Public Use", all 95 counties) | map + group + parcel as on the assessor card: `002 001.02`, `007C A 002.00`, or the full `055 002 00102 000`; **County box required** (same map+parcel exists in dozens of counties; county code 055 in a full id also works) | federal | `pin_mode: tn` parser in pipeline.tn_parts; address field is street-then-number (`SMITH LN 660`) → flipped to `660 Smith Ln`; DEEDAC 0 → acres from polygon; coordinates work with no county |
| `realie` | **any US county** (Realie.ai, free tier, `REALIE_TOKEN`) | APN + state + county, or street address + state | federal | polygon, acres, situs, owner, use, legal. The nationwide default. |
| `cass-mo` | Cass County, MO (CAMAvision WFS) | parcel id as on the assessor site | federal | pattern for Vanguard CAMAvision counties (MO/IA): attributes from `parcels`, polygon from `search` matched by envelope |
| user-added | any county with a public ArcGIS parcel layer or CAMAvision map | layer's own id field | federal | **Counties tab** in the app: state + county (+ optional layer URL); fields auto-detected, checked live; saved to `DATA_DIR/counties_user.json` (mount a Railway volume at /app/data to keep them across deploys) |
| `regrid` | any US county on a paid Regrid plan ($375/mo — not used) | APN or street address | federal | trial token covers only 7 demo counties |
| boundary file | anywhere | — | federal | Land id → export KML; Google Earth → KML/KMZ; shapefile as .zip. Largest polygon is used; acreage from geometry |

Florida uses the FGIO hosted copy of the DOR cadastral (`services9.arcgis.com/Gh9awoU677aKree0/.../Florida_Statewide_Cadastral/FeatureServer/0`) —
the FDACS layer answers id lookups in 40+ s and times out. Parcel ids are stored as each county appraiser formats them (mostly digits only); the lookup sends both forms in one `IN (...)` query.

Missouri: no free statewide parcel layer (counties publish their own, or none). Options: Regrid self-serve plan (nationwide, ~$0.05–0.10/lookup),
or export the parcel from Land id as KML and use the boundary-file option. onX Hunt does not export parcel boundaries; Pixly adds nothing here.

Federal layers (`_federal` in counties.json) — all free, no key: TIGERweb Transportation layers 2 (primary), 3 (US highways), 8 (local), field `NAME`;
NHD MapServer 6 (flowlines) and 12 (waterbodies), field `gnis_name` — request only that field or the query times out; FEMA NFHL layer 28 (`FLD_ZONE`, `SFHA_TF`);
USA Structures FeatureServer/0. All scan calls have a 25 s timeout and fail soft (the run continues with less context).

## Adding a county

1. Find the county's public ArcGIS REST endpoint (search "<county> GIS arcgis rest services"; the county GIS page usually lists it).
2. Open `<server>?f=pjson` and note the layer ids for parcels, road centerlines, streams, building footprints, FIRM flood zones.
3. Open the parcels layer and note the field names for PIN, site address, acres, owner, use code.
4. Add a block to counties.json (copy adams-il), map the fields, add use-code meanings if the county publishes them.
5. Allow the host, then run `pipeline.py --county <key> --pin <PIN> --no-render` to check the parcel + scan before paying for renders.
Counties on Regrid skip all of this (`--regrid`), but Regrid has no road/stream/building/flood layers — the scan is thinner.

## Layout, callouts and the editor (layout.py, editor.js, "Review & edit" tab)

Everything drawn on an image lives in `out/<PIN>/layout.json` in **ground coordinates** (lon/lat): boundary, acreage card anchor + text,
callouts (POND, CREEK, DRIVEWAY, ELECTRIC POLES & LINES, BUILDING(S), TIMBER/PASTURE), road pins, disclaimer. Each image has a *ground map*
in `maps.json` that turns lon/lat into its pixels:
- satellite images: exact Web-Mercator frame;
- AI renders: a homography fitted to the magenta marker (ICP between the reference polygon and the marker centreline) — a flat-ground
  render is exactly a homography, so road pins land on the road in every angle; fit error is logged (typ. 15–30 px at 2K);
- real drone photos: homography from the metadata projection, or from corners the user drags in the editor (exact vertex correspondence,
  RANSAC when ≥6), or a rough bbox map until then (auto callouts hidden).
`pipeline.py --finalize out/<PIN>` redraws every image from the layout and rebuilds the packet without any API call; the editor's
**Apply** does the same after folding the pixel edits back through that image's ground map. Unmoved items keep their
exact ground position; moved items are re-projected. **Nothing in Review & edit calls an API — edits cost $0.**

Editor fields (all optional in layout.json; old files still render): `style` {shadow (default off = crisp white line with a thin dark
edge; on = soft drop shadows under lines, pins and cards), line_scale, line_visible, fill}; `card` {scale, visible, off: [dx, dy]};
`labels[i]` {scale, visible, off: [dx, dy]} — `off` is the label box position relative to its pin in *layout units* (1 unit = 1 px on a
1200-px-wide image) so a placement carries to every view; `disc` {x, y (right/bottom edge as fractions of the image), scale, visible}.
A label box and its pin move independently (drag the box, or drag the pin to move both); Size ± resizes the selected label / card /
disclaimer (or the line width when a corner is selected); Delete removes a label or hides the card / disclaimer; Hide lines removes the
outline (a polygon always keeps ≥3 corners). **Preview** renders the exact output server-side without saving.

**Apply to: This image only** writes `out/<PIN>/overrides.json` ({image key: layout}) — that image is drawn from its own layout from then
on and the rest of the packet is untouched; **Whole packet** updates `layout.json`, and images with an override keep their own edits.
Shared geometry (boundary, dragged-corner refit on photos, acreage) always applies everywhere. *Reset all to automatic* clears both.
The editor payload comes from a server dry-run render (`layout.PLACEMENT`), so every box is shown at the pixel the server drew it, with
the same fonts, paddings and sizes — the canvas is the output. *Download this image* gives the single finished file. Low horizon views (`--low auto`: road side + opposite) use a near-ground prompt and a
bottom-anchored crop that keeps the sky. The plat exhibit (plat.py) is drawn from the same boundary: bearings/distances per course, PLSS
section-township-range (BLM CadNSDI), adjoiner PINs, roads, north arrow, scale bar, course table — "not a survey" footer.

## Real drone photo (drone_photo.py / "Real drone photo" tab)

Puts the true property line (and the acreage card, road labels, disclaimer) on a photo the drone actually took, with an optional golden-hour relight.
1. Line placement: DJI photos straight from the SD card carry XMP tags (`drone-dji:GpsLatitude/GpsLongitude/RelativeAltitude/GimbalYawDegree/GimbalPitchDegree`)
   plus EXIF focal length. The parcel polygon is projected through a flat-ground pinhole camera model (validated to ~1.5 px on a synthetic frame).
   Photos that went through a phone, chat app, or editor lose those tags → the tool says which tags it found. Then either click the corners on the photo
   (4+ clicks → the parcel shape is matched to the clicks and the true outline with every jog is drawn), or just press Annotate: the true parcel shape is
   placed in the middle and you **drag its corners** onto the real corners in Review & edit — 4 moved corners are enough, the rest snap into place and the
   callouts appear (validated at 1.8 px on a synthetic frame).
2. Golden hour: the polygon is drawn as a thin magenta marker, Grok re-lights the photo keeping the marker, the marker is detected, erased (inpaint with a 3-px dilation — the earlier 7-px band showed as a smear along the line) and redrawn as the styled line.
   Outputs: `<photo>_original_with_lines.jpg`, `<photo>_golden_with_lines.jpg`, `<photo>_golden_no_lines.jpg`.
Camera: DJI Mini 2 (24 mm equiv, 4:3) is the reference; any DJI with the same tags works.

## Hosting (so the team runs it without anyone's laptop)

`app.py` is a Gradio web form (county + PIN → gallery + zip download) with a shared password; `Dockerfile` + `requirements.txt`
package it. Three ways to put it on the web, cheapest-viable first:

| Option | Monthly | Setup effort | Notes |
|---|---|---|---|
| **Railway** (recommended) | ~$5–10 | 15 min: new project → deploy from GitHub repo → add env vars (keys + APP_PASSWORD) | Always-on URL like bana-listings.up.railway.app; logs; easy redeploys. Run time 1–3 min per packet is fine. |
| Hugging Face Spaces (Docker) | $0 public / $9 private-ish | 10 min: create Space → upload files → secrets | Simplest UI hosting; free tier sleeps and is public, so use a paid private Space. |
| Render / Fly.io | ~$7 | 15 min | Same as Railway; Render's free tier sleeps 50 s on first hit. |

Not recommended: Streamlit Cloud (public only), a desktop app (back on one machine), Zapier/Make (can't run the image math).

Current deployment: GitHub `iamawesome17/Ai-Image-listing-tool-` (private, branch `main`) → Railway project, auto-redeploys on every push
(~4 min build). URL: https://ai-image-listing-tool-production.up.railway.app. Login: user `bana`, password = Railway variable `APP_PASSWORD`.
Railway → Variables: REGRID_TOKEN, GOOGLE_MAPS_KEY, HIGGSFIELD_KEY, APP_PASSWORD. Railway has unrestricted egress, so everything that is
blocked from the Claude workspace (reference uploads → diagonal angles, Overpass) works there.

To update: edit files in `C:\Users\Hiwat\Documents\bana-listing-tool`, then `git add -A && git commit -m "..." && git push` (needs a GitHub
token with `repo` scope on that repo). Watch the build in Railway → Deployments.

Adding people: today one shared login. For per-person logins add a `USERS` variable (`alice:pw1,bob:pw2`) and change `auth=` in app.py to read it —
Gradio accepts a list of (user, password) pairs. Output zips live in the container's out/ folder (ephemeral): add a Railway volume at /app/out,
or push packets to a Drive/Dropbox folder (next improvement).

## Costs (list prices, Oct 2026)

| Item | Per property |
|---|---|
| County GIS | $0 |
| Static Maps (4–6 requests) + Places (1) | ~$0.07 |
| Grok Image 2.0 via Higgsfield, 10 renders at 2K + 3 rotations + retries | ~$0.50–0.80 (failed jobs are not charged) |
| Regrid lookup (where used) | $0.05–0.10 |

## Known limits / next improvements

- Oct 29 2026: McNairy Co, TN failed every route (no TN source; Realie ignores coordinates). Fixed: built-in `tn` source, County box shown for statewide sources, Realie + coordinates now gives a clear message instead of a dead end.
- Counties added in the app's Counties tab are lost on every Railway redeploy until a volume is mounted at /app/data (Railway → service → Settings → Volumes → mount path `/app/data`). Built-in sources in counties.json are not affected.

- Outline on AI renders is where the model carried the marker — approximate by design; the satellite cover (p1_cover) is the accurate reference.
- Simplified polygon on renders (approxPolyDP) — fine for L-shapes and notches, may round very fine jogs.
- Road labels and POI pins are drawn on satellite images only (positions in AI renders are unknown).
- Rural building footprints are not in the Adams County layer outside the Quincy tri-township; the scan says "0 buildings" there even when sheds exist.
- A lot of under ~0.25 ac on soft (upscaled) satellite imagery gives the model little to hold onto — expect more invention.
- Powerlines come from OpenStreetMap (Overpass, with mirrors) — coverage is patchy in rural areas, and the main server is often at capacity; the run continues without them.
- Google Geocoding API must be in the key's API-restriction list, or FL/VA/boundary-file cards show no city/zip and road labels are skipped (REQUEST_DENIED in the log).
- Diagonal angles (NE/SE/SW/NW) need the reference upload, which works on Railway but not from the Claude workspace; the model-rotation trick only does 90°/180°.
- Drone-photo auto placement assumes flat ground; on steep sites, click the corners instead (drag-to-adjust is the next improvement).
- Not yet done: orbit video (Kling/Seedance image-to-video on Higgsfield), listing-copy generation, push to Notion/Airtable deal record.
