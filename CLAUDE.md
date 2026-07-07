# FunPop Dashboard — working notes for Claude

A Streamlit dashboard (`streamlit_app.py`) that reads Walmart **BI Link** supplier
data from BigQuery and renders ten tabs. Hosted on Streamlit Community Cloud;
cold loads are served from a parquet **snapshot** built on a schedule by GitHub
Actions (`snapshot_build.py` → `snapshot-data` branch) with a live BigQuery query
as the fallback.

## Data source & the data-element glossary

- **Source:** Walmart BI Link, `wmt-dv-bi-link-prod.dv_supplier` (the deployed
  dataset name lives in `st.secrets["bigquery"]["dataset"]`, e.g. `dv_supplier`).
  The user loads BI Link feed tables into their own `{project}.{dataset}`.
- **Glossary (authoritative field reference):** `docs/data_element_glossary.xlsx`
  (original) and `docs/data_element_glossary.csv` (cleaned, greppable). **Consult
  it before adding or renaming any BigQuery column.** Columns:
  `technical_name, business_name, status, products, rb_datasets, api_feeds,
  definition`.
  - `products` lists where a field exists: `5:BI Link`, `3:Cloud Feeds`,
    `9:API Glossary`, `13:Report Builder`, etc. **This app uses BI Link fields.**
  - `rb_datasets` / `api_feeds` are the Report-Builder dataset / API-feed groupings
    a field belongs to — useful for guessing a table's columns, not its physical
    BI Link name.
  - Grep example: `grep -iE '^fcst_|forecast' docs/data_element_glossary.csv`.
- **Table names are not in the glossary.** The glossary names *fields* and logical
  *datasets/feeds*, not the physical BI Link table names. Physical names vary per
  export, so confirm them against the live dataset (every SQL file except the
  forecast one was "confirmed against … dv_supplier"). `INFORMATION_SCHEMA` is the
  reliable way to discover what actually exists.

## Forecast tab — table is discovered at runtime (don't hard-code it)

The Forecast tab needs a **daily** store-item demand forecast:
`forecast_date, store_number, walmart_item_number, forecast_quantity`.

The original `sql/forecast_query.sql` hard-coded `daily_demand_forecast`, which
matched no real table, so the query threw and the tab showed *"Forecast data is
temporarily unavailable."* (that message is the loader's exception path, not an
empty result). The BI Link **columns** were right (`fcst_dt`,
`fcst_tot_dmand_each_qty`, `store_nbr`, `wm_item_nbr`) — only the table name was
wrong.

Fix: `forecast_source.py` discovers the real table from `INFORMATION_SCHEMA` by
its forecast columns and builds the query. Used by both the app
(`load_forecast_data` → `_forecast_spec`) and the builder
(`snapshot_build.forecast_sql`). Notes:
- Daily is required (`fcst_dt`). A weekly-only `store_demand_forecast`
  (`fcst_wm_yr_wk_nbr` + `final_fcst_each_qty`) is detected and reported with a
  precise message rather than blindly converted week→date.
- Pin the table to skip discovery: `[bigquery].forecast_table` in secrets, or the
  `FORECAST_TABLE` env var for the builder.
- `sql/forecast_query.sql` is now a **reference template** of the expected shape;
  it is not executed for the forecast (kept in sync with
  `forecast_source.build_forecast_sql`).

## Repo map (essentials)

- `streamlit_app.py` — the app (auth, loaders, all tab rendering).
- `forecast_source.py` — runtime forecast-table discovery + SQL builder.
- `weather_source.py` — Weather tab: live Open-Meteo grid fetch (free, no API key)
  + NWS-style filled-contour map render. Real-time, so fetched on view (cached ~3h)
  and **never snapshotted**; the US boundary ships in `data/us_states_conus.geojson`.
- `transforms.py` — dtype-shrinking transforms shared by app + builder.
- `snapshot.py` / `snapshot_build.py` — durable snapshot read / build.
- `constants.py` — `ACTIVE_ITEMS` and item labels/pack sizes (single source of truth).
- `sql/*.sql` — one query per source; `{project}`/`{dataset}` are substituted.
- `docs/` — the data-element glossary.

## Conventions

- All date filters anchor on `CURRENT_DATE('America/Chicago')` (BI Link feeds land
  ~7am Central, ~1 day lag).
- Secondary loaders are fault-tolerant: a bad source yields a section-local
  warning via `_section_error`, not a page crash. Raw BigQuery errors are logged
  server-side only (the dashboard is public).
- Keep `snapshot_build.query_jobs` in sync with the app's loaders — the snapshot
  key is `(sql_filename, param values)`.
