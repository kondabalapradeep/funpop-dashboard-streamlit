# FunPop Dashboard — Streamlit Edition

## What this is

A Streamlit app that pulls live data from BigQuery using your service-account JSON and renders a dashboard with all the calculations from the original script. Hosted free on Streamlit Community Cloud. Anyone with the link sees the dashboard; clicking **Refresh data** in the sidebar forces a fresh BQ pull.

## Files

| File | What it is |
|---|---|
| `streamlit_app.py` | The Streamlit app — auth, BQ queries, UI rendering. |
| `funpop_core.py` | Original data-prep logic, reused unchanged (HTML template is in here too but unused). |
| `sql/store_query.sql` | The store JOIN query. |
| `sql/dc_query.sql` | The DC query. |
| `sql/forecast_query.sql` | Daily demand-forecast query (powers the Forecast tab). |
| `sql/dc_alignment_query.sql` | Store→DC alignment (rolls store forecast up to DCs). |
| `requirements.txt` | Python deps. |
| `.streamlit/secrets.toml.example` | Template — copy locally to `secrets.toml`, paste into Streamlit Cloud's UI for production. |
| `.gitignore` | Keeps `secrets.toml` and `*.json` out of git. |

---

## Run locally first (5 minutes)

```bash
# In a fresh folder
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Create the secrets file
mkdir -p .streamlit
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
# Open .streamlit/secrets.toml in a text editor and fill in:
#   - dataset name
#   - every field from your service-account JSON (including the multi-line private_key)

streamlit run streamlit_app.py
```

Browser opens at http://localhost:8501. If the dashboard loads with data, you're good. Common first-run problems:

- **`google.auth.exceptions.RefreshError`** — private_key got mangled. Re-paste it carefully into the triple-quoted block, keeping all `\n` sequences exactly as they appear in the JSON.
- **`google.api_core.exceptions.NotFound: 404 Not found: Dataset ...`** — wrong dataset name in `[bigquery]`, or table names in the SQL files don't match your actual tables. Fix in `sql/store_query.sql` and `sql/dc_query.sql`.
- **`google.api_core.exceptions.Forbidden`** — service account lacks BigQuery roles. Needs `BigQuery Data Viewer` + `BigQuery Job User` on the project.

---

## Deploy to Streamlit Community Cloud (free, public URL)

### 1. Push to GitHub

Create a repo (you can reuse `funpop-dashboard` or make a new one). Push everything **except** `.streamlit/secrets.toml`. The `.gitignore` already excludes it. Verify with `git status` — `secrets.toml` should not appear.

You can use a private repo on the free tier. Streamlit will ask for an extra GitHub permission to access it.

### 2. Deploy

1. Go to **share.streamlit.io**, sign in with GitHub.
2. **Create app** → pick the repo, branch (`main`), and main file (`streamlit_app.py`).
3. **Advanced settings → Secrets** — paste the entire contents of your local `.streamlit/secrets.toml`. This is where the service-account key lives in production; never in the repo.
4. **Deploy**. First build takes ~2 minutes (installing pandas, pyarrow, BQ client).

You get a URL like `https://funpop-dashboard.streamlit.app` — that's what you share.

### 3. After it's live

- Pushing to the GitHub repo auto-redeploys the app within a minute.
- Edit secrets at any time from the app's settings page; the app reloads automatically.
- The app sleeps after ~7 days of inactivity; first visit after a sleep takes ~30s to wake. Regular use keeps it warm.

---

## How refresh works

Several layers, mostly automatic:

1. **In-app cache + hourly auto-refresh** — the BQ loaders are wrapped in `@st.cache_data` keyed by a time slot that advances every hour from 6am Central. Each new hour triggers a fresh pull until the day's data lands; once confirmed fresh, the slot locks to `today_done` and no more pulls happen that day.
2. **Durable snapshot (so cold loads are instant)** — Streamlit Community Cloud throws away the in-memory cache whenever the app process restarts (sleep, redeploy, recycle). A scheduled GitHub Actions job (`.github/workflows/snapshot.yml` → `snapshot_build.py`) runs every dashboard query and stores the results in a `_dashboard_snapshot` BigQuery table. On a cold start the app reads that table (fast) instead of re-running the heavy joins. Any snapshot miss/staleness/error silently falls back to a live query, so the app is never *worse* than a direct pull.
3. **Keep-awake pings** — `.github/workflows/refresh.yml` loads the app in a headless browser in a tight cluster around 8am Central, so the container is awake and warmed from the snapshot when you open it in the morning.
4. **Manual button** — the sidebar "Refresh data" button clears the cache and bypasses the snapshot to force a live pull. Use it to confirm the very latest data.

### Required setup for the snapshot (one-time)

The snapshot builder runs in GitHub Actions, so it needs its own credentials. In the repo: **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Value |
|---|---|
| `GCP_SERVICE_ACCOUNT_JSON` | The full service-account JSON (the same key pasted into Streamlit Cloud's secrets). |
| `BQ_DATASET` | The dataset name that holds the source tables (the value of `st.secrets["bigquery"]["dataset"]`, e.g. `dv_supplier`). |

The service account also needs **write** access so it can create/update the snapshot table — grant it **BigQuery Data Editor** on the project/dataset (it already has Data Viewer + Job User for reads).

After adding those, open the **Actions** tab → **Build dashboard snapshot** → **Run workflow** to build the first snapshot and confirm it succeeds.

> If the source data ever grows too large to fit a snapshot row (BigQuery STRING cells cap at ~10 MiB), move the payload to a GCS bucket — `snapshot.py` is the only file that would change. Until then a too-large query just fails to snapshot and the app falls back to a live pull for it.

---

## What's in the dashboard (and what's not)

**Included in this scaffold:**
- Period KPIs (units TY/LY, YOY, last-7-day rollup)
- Last-7-days tracker table
- 4-week daily trend chart
- Store inventory snapshot (on-hand, in-warehouse, in-transit, weeks of supply)
- Full store rankings table (sortable, filterable, with status)
- DC analysis section (KPIs, critical/overstock callouts, full table) — appears only if DC query returns data

**Not yet included** (the original HTML dashboard had these — easy to add as additional Streamlit sections if you want them):
- U/S/W weekly breakdown
- Price-point migration cards
- Retail performance by date / per-price tracking
- Per-week composition trends (Up/Down/Zero/Flat by week)
- Item filter "All / one item only" toggle pattern from the original (this version uses a multiselect)

To add any of these: the data is already in the `data` dict returned by `compute_dashboard()` — just add a section that reads `data["uspw"]`, `data["summary"]`, etc., and renders it with `st.dataframe` or `st.bar_chart`. Use the existing sections as templates.

---

## Two ongoing reminders

1. **The old GitHub token** (`ghp_dmQH...`) — revoke at github.com/settings/tokens if you haven't already. It's not used by this Streamlit version, but it's still exposed wherever the old script lives.
2. **DC unit conversion** — same caveat as before: if DC on-hand numbers look way too small versus the CSV version, the BQ table is delivering warehouse packs, not eaches. Fix in `sql/dc_query.sql` by multiplying `ty_on_hand_whpk_qty` by `ty_whpk_each_qty`.
