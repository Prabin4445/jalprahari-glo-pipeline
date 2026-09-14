# JalPrahari GLO Lake-Area Pipeline

Near-real-time glacial lake monitoring for the app's 5 GLOF watch lakes.
Everything here is **free tier**: Google Earth Engine + GitHub Actions +
your existing Cloudflare Worker.

## How it works

```
Google Earth Engine (Sentinel-2 optical, Sentinel-1 SAR fallback)
        │  weekly cron, measures current lake area vs 2024 GLO baseline
        ▼
GitHub Actions → commits data/glo-lakes-latest.json
        │
        ▼
Cloudflare Worker  GET /glo-lakes   (new route, 1h cache, CORS *)
        │
        ▼
JalPrahari app — GLOF cards gain a "📡 Now: …" line (already wired;
falls back to the 2024 satellite inventory until the feed is live)
```

Indicators are the app's own monitoring signals (🟢 stable · 🟡 changing ·
🟠 rapid · 🔴 potential concern) — never presented as official warnings.
The JSON and the app both carry that disclaimer.

## Setup checklist (PraBin's steps)

1. **Google Earth Engine** — sign up free at
   https://earthengine.google.com (noncommercial use). Approval is usually quick.
2. **Service account** — in Google Cloud Console create a project +
   service account, download its JSON key, then register that service
   account at https://earthengine.google.com (it must be registered before
   it can call the API headlessly).
3. **GitHub repo** — create a repo (e.g. `jalprahari-glo-pipeline`) and push
   this folder's contents so the workflow file lands at
   `.github/workflows/glo-lake-monitor.yml`.
4. **Secrets** — in the repo: Settings → Secrets → Actions, add
   - `EE_SERVICE_ACCOUNT` = the service account email
   - `EE_KEY_JSON` = the full contents of the JSON key file
5. **First run** — Actions tab → "GLO lake area monitor" → Run workflow.
   After it finishes, `data/glo-lakes-latest.json` should exist in the repo.
6. **Worker** — open `worker-route-glo-lakes.js`, set `GLO_LAKES_JSON_URL`
   to your repo's raw URL
   (`https://raw.githubusercontent.com/<YOU>/<REPO>/main/data/glo-lakes-latest.json`),
   paste the route into `worker.js` (hook it into the fetch handler and the
   `/health` map as the comments show), redeploy the worker.
7. **App** — nothing to do. The Flutter code already fetches `/glo-lakes`
   on every refresh; cards upgrade themselves the moment the feed is live.

## Local test (optional)

```bash
pip install -r requirements.txt
earthengine authenticate   # once, opens a browser
python lake_area_monitor.py
cat data/glo-lakes-latest.json
```

## Files

| File | Purpose |
|---|---|
| `lake_area_monitor.py` | GEE measurement script (S2 NDWI + S1 SAR fallback) |
| `requirements.txt` | `earthengine-api` |
| `.github/workflows/glo-lake-monitor.yml` | Weekly cron + auto-commit |
| `worker-route-glo-lakes.js` | Drop-in `/glo-lakes` route for worker.js |
| `data/glo-lakes-latest.json` | Generated feed (committed by Actions) |

## Method notes

- Sentinel-2 `S2_SR_HARMONIZED`, least-cloudy scene in a 45-day window
  (<30% cloud), SCL cloud/shadow mask, NDWI = (B3−B8)/(B3+B8) > 0.15.
- Sentinel-1 `S1_GRD` IW VV median fallback when optical is blocked;
  water where VV < −16 dB. Labeled as radar fallback in the feed.
- 2.5 km buffer around each real GLO_ID centroid; change is measured
  against the 2024 GLO inventory area for that lake.
