# Bana Listing Photos

Parcel in → listing photo packet out: annotated satellite set + AI drone views (S/E/W/N, golden hour + day) with property lines,
road label, acreage card, disclaimer. Web form in `app.py`; pipeline in `pipeline.py`; full process record in `PROCESS.md`.

## Deploy on Railway (≈10 minutes, once)
1. railway.com → Login with GitHub → **New Project → Deploy from GitHub repo** → pick this repo. Railway builds the Dockerfile.
2. Service → **Variables** → add `REGRID_TOKEN`, `GOOGLE_MAPS_KEY`, `HIGGSFIELD_KEY`, `APP_PASSWORD` (the team's shared password).
3. Service → **Settings → Networking → Generate Domain** (port 7860).
4. Optional: **Settings → Volumes → add volume** mounted at `/app/out` so packets persist across redeploys.
5. Open the domain, log in as user `bana` with the password, enter a county + PIN, click **Create packet**.

Any push to `main` redeploys automatically.

## Run locally
    pip install -r requirements.txt
    cp .env.example .env   # fill in keys
    python app.py          # http://localhost:7860
or without the UI: `python pipeline.py --county adams-il --pin 20-0-0636-002-00`

## Adding a county
See PROCESS.md → "Adding a county" (edit `counties.json`).
