# Serving demo (BUILD 15)

A small FastAPI backend + dashboard that turns declared onboarding details plus a
(mock) open-banking account into a calibrated, model-driven credit score.

## 0. Prerequisites

You need the three model artifacts. They are produced by the calibration script:

```bash
# from the project root, on your machine (needs lightgbm)
python scripts/calibrate_score.py "C:\Users\Josep\Downloads\homecredit" \
    --max-cases 0 --cache-dir "C:\Users\Josep\Downloads\obcache14"
```

That writes, into the project's `artifacts/` folder:

```
artifacts/
  model_lgbm.txt     <- the trained LightGBM booster
  calibrator.pkl     <- the isotonic calibrator (raw score -> PD)   <-- THE PICKLE
  scorecard.json     <- feature order, medians, PDO settings, bands
```

### Where does the pickle go?

**Nowhere manual** if you run the script from the project root — `calibrate_score.py`
already saves `calibrator.pkl` (and the other two) straight into `<project>/artifacts/`,
which is exactly where the server looks by default.

If your artifacts live somewhere else, point the server at them:

```bash
# Windows PowerShell
$env:OBCREDIT_ARTIFACTS="C:\path\to\artifacts"
# macOS / Linux / WSL
export OBCREDIT_ARTIFACTS=/path/to/artifacts
```

The folder must contain all three files: `model_lgbm.txt`, `calibrator.pkl`, `scorecard.json`.

## 1. Install + run

```bash
pip install -r serve/requirements.txt
uvicorn serve.app:app --reload --port 8000     # run from the PROJECT ROOT
```

Open http://127.0.0.1:8000

## 2. Use it

1. Fill in the declared details (employment, income type, housing, education,
   stated monthly income) — these drive the BUILD 14 declared features.
2. Pick a mock bank profile (clean / thin / arrears) — this stands in for a real
   TrueLayer pull and provides the transaction history.
3. Click **Connect bank & get my score**.

The dashboard shows the credit score, risk band, PD, detected income, and the top
factors pushing the score up/down (adverse-action reason codes).

## 3. Live TrueLayer sandbox connection (wired into the dashboard)

The dashboard now has a **Connect with TrueLayer** button that runs the real
hosted-auth OAuth flow against the sandbox and scores the pulled account.

1. Create a sandbox app at https://console.truelayer.com and copy its
   **client_id** and **client_secret**.
2. In the console, add the redirect URI **http://localhost:8000/callback**.
3. Set the credentials in the environment before starting the server:

   ```powershell
   # Windows PowerShell
   $env:TRUELAYER_CLIENT_ID="<your client id>"
   $env:TRUELAYER_CLIENT_SECRET="<your client secret>"
   # optional override:
   # $env:TRUELAYER_REDIRECT_URI="http://localhost:8000/callback"
   ```

4. Start the server and click **Connect with TrueLayer**. Choose the **Mock Bank
   (uk-cs-mock)** provider and log in with TrueLayer's sandbox test credentials.
   You'll be redirected back and scored automatically.

Under the hood: `/connect` stashes your declared details and redirects to
TrueLayer; `/callback` exchanges the auth code, calls `client.fetch_user(...)`,
and hands the payload to the SAME `ScoringService.score_payload(...)` used
everywhere else. The mock profiles remain available as an offline fallback.

## Health check

`GET /api/health` reports whether the artifacts loaded and which BUILD stamp the
scorecard was trained under.
