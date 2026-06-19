# FunPop Dashboard — Streamlit Edition

## What this is

A Streamlit app that pulls live data from BigQuery using your service-account JSON and renders a dashboard with all the calculations from the original script. Hosted free on Streamlit Community Cloud. Anyone with the link sees the dashboard; clicking **Refresh data** in the sidebar forces a fresh BQ pull.

## Files

| File | What it is |
|---|---|
| `streamlit_app.py` | The Streamlit app — auth, BQ queries, UI rendering. |
| `constants.py` | Item IDs, Full/Half/Shelf labels, case-pack sizes (single source of truth, verified against the feed's own item names). |
| `transforms.py` | Dtype-shrinking transforms shared by the app and the snapshot builder. |
| `snapshot.py` / `snapshot_build.py` | Durable query-result snapshot: built on a schedule, published to the `snapshot-data` branch, read by the app on cold start. |
| `store_directory.py` | Builds the static store mailing-address directory for the Store Actions export. |
| `sql/store_query.sql` | The store JOIN query. |
| `sql/dc_query.sql` | The DC query. |
| `sql/forecast_query.sql` | Daily demand-forecast query (powers the Forecast tab). |
| `sql/dc_alignment_query.sql` | Store→DC alignment (rolls store forecast up to DCs). |
| `sql/store_lookahead_query.sql` | Same-period-last-year store inventory (on-hand + in-warehouse + in-transit) for the Inventory tab's week look-ahead. |
| `sql/dc_lookahead_query.sql` | Same-period-last-year DC on-hand for the week look-ahead's DC on-hand / total-network columns. |
| `requirements.txt` | Python deps. |
| `.streamlit/secrets.toml.example` | Template — copy locally to `secrets.toml`, paste into Streamlit Cloud's UI for production. |
| `.gitignore` | Keeps `secrets.toml`, `*.json`, and `snapshot_data/` out of git. |

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
2. **Durable snapshot (so cold loads are instant)** — Streamlit Community Cloud throws away the in-memory cache whenever the app process restarts (sleep, redeploy, recycle). A scheduled GitHub Actions job (`.github/workflows/snapshot.yml` → `snapshot_build.py`) runs every dashboard query and publishes the results as parquet files on the **`snapshot-data` branch** (a single force-pushed commit, so the repo's history never grows and snapshot updates never trigger an app redeploy). On a cold start the app downloads those files from the branch's raw URL (fast, ~3.5 MB total) instead of re-running the heavy joins. Any snapshot miss/staleness/error silently falls back to a live query, so the app is never *worse* than a direct pull. The service-account key only needs **read** access — nothing is written back to BigQuery.
3. **Keep-awake pings** — `.github/workflows/refresh.yml` loads the app in a headless browser in a tight cluster around 8am Central, so the container is awake and warmed from the snapshot when you open it in the morning.
4. **Manual button** — the sidebar "Refresh data" button clears the cache and bypasses the snapshot to force a live pull. Use it to confirm the very latest data.

### Required setup for the snapshot (one-time)

The snapshot builder runs in GitHub Actions, so it needs its own copy of the read-only credentials. In the repo: **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Value |
|---|---|
| `GCP_SERVICE_ACCOUNT_JSON` | The full service-account JSON (the same read-only key pasted into Streamlit Cloud's secrets). |
| `BQ_DATASET` | The dataset name that holds the source tables (the value of `st.secrets["bigquery"]["dataset"]`, e.g. `dv_supplier`). |

That's it — no BigQuery write access or GCP console changes are required. After adding the secrets, open the **Actions** tab → **Build dashboard snapshot** → **Run workflow** to build the first snapshot and confirm it succeeds.

> The job force-pushes a small `snapshot_data` set to the `snapshot-data` branch each time the data changes (≈ once a day, when the feed lands). That branch always holds exactly one commit, so the repo stays small and the app is **not** redeployed by snapshot updates. If you ever want to stop it, disable the **Build dashboard snapshot** workflow.

---

## What's in the dashboard (and what's not)

**Included in this scaffold:**
- **Per-tab fragments** — every tab body runs as an `st.fragment`, so widgets inside a tab (scope radios, threshold sliders, the leaderboard search box) rerun only that tab's computations instead of all eight. Sidebar controls still rerun the whole page.
- **Sidebar state filter** — scope the entire dashboard (Overview through Store Actions, including the dispatch export) to chosen states; DC sections stay network-wide because one DC serves stores in many states.
- Period KPIs (units TY/LY, YOY, last-7-day rollup)
- Last-7-days tracker table
- 4-week daily trend chart
- Store inventory snapshot (on-hand, in-warehouse, in-transit, weeks of supply)
- **Inventory week look-ahead** — extends the daily pipeline view (in store, in warehouse, in transit, DC on-hand, total network — the same columns as the daily flow table) past the latest data day: this year's actuals through that day, then **last year's** levels (shifted forward 52 weeks to the same Walmart fiscal weekday) for the next 7 days as a planning proxy. The table is ordered latest-first (furthest look-ahead day on top) and the future days are shaded in both the chart and table. Last year's levels aren't in the main store/DC frames (they span only the recent lookback, 120 days max), so they come from dedicated same-period-last-year queries (`sql/store_lookahead_query.sql`, `sql/dc_lookahead_query.sql`). Store stages respect the sidebar item + state filters; DC on-hand is network-wide (item-filtered).
- Full store rankings table (sortable, filterable, with status)
- DC analysis section (KPIs, critical/overstock callouts, full table) — appears only if DC query returns data
- **Sales Drivers tab** — answers *why* sales are up or down versus the same period last year for the current filters. It (1) splits the year-over-year **sales-dollar** change into an exact, additive **distribution / velocity / price** bridge (waterfall); (2) reads the **inventory feed against sales** to separate a fixable **availability (stockout)** problem from a true **demand** shift — estimated sales lost to stockouts (each OOS store-day credited the store's own normal velocity), in-stock %, on-hand YoY, weeks of supply, and a plain-English verdict; (3) localises the change to the **items, states and stores** moving it, tagging each store decliner as a *📦 Stockout* or *🛒 Demand* story; and (4) charts the **trajectory** (weekly YoY sales gap vs in-stock rate) over the full range so you can see whether the gap is widening or closing. Everything follows the sidebar item/state filters and the performance window.
- **Store Actions tab** — flags stores with a *physically rep-fixable* problem and lets you **download a CSV dispatch list** (store number, address, city, state, zip + priority/issue) to hand to a field-service company. To react fast without chasing daily noise on low-volume SKUs, it compares each store's **recent 3-day** selling rate against its own **trailing ~21-day run-rate** (a stable baseline), plus a velocity-scaled *"went dark"* check that flags a stocked store the moment its expected lost units (normal rate × silent days) cross a floor — so a brisk seller is caught after ~1 silent day, a slow seller only after a longer gap. Flags: went dark, idle backroom stock, declining vs normal (severity scales with the drop), stuck stock, chronic out-of-stock with supply available, and underperformance vs peers. Stores are ranked by estimated lost units/week. The tab has its own **item scope** control (all items / both bins / shelf only) so it works independently of the sidebar filter, and the on-hand / decline / "went dark" thresholds are adjustable. Addresses come from a **static store directory** built once by the snapshot job (`store_directory.py` introspects `store_dim`'s columns, so it auto-adapts to the dataset's actual address/zip names) and committed as `snapshot_data/store_directory.parquet`; the app reads it straight from disk and only queries `store_dim` live as a fallback. If no address columns exist it degrades to store number + state.

---

## Two ongoing reminders

1. **The old GitHub token** (`ghp_dmQH...`) — revoke at github.com/settings/tokens if you haven't already. It's not used by this Streamlit version, but it's still exposed wherever the old script lives.
2. **DC unit conversion** — the BQ table delivers warehouse packs, not eaches. The app converts packs→eaches using the feed's own per-row `ty_whpk_each_qty`, falling back to `constants.CASE_PACK_UNITS` where the feed has no pack size. If DC on-hand ever looks off by a large factor, check those two sources against each other.
