# Deploying the FunPop Dashboard

How to change this app and get the change live on **share.streamlit.io**, and what
to check when something looks wrong.

---

## 1. The mental model: three moving parts

They are independent. Most confusion comes from mixing them up.

```mermaid
flowchart LR
    A["Your laptop<br/>edit + streamlit run"] -->|git push main| B["GitHub<br/>main branch"]
    B -->|auto-redeploy ~1 min| C["Streamlit Community Cloud<br/>the live app"]
    B -.->|snapshot.yml, 6-9am Central| D["GitHub Actions<br/>runs the 13 BQ queries"]
    D -->|force-push, 1 commit| E["snapshot-data branch<br/>parquet files"]
    E -->|raw URL download on cold start| C
    F["refresh.yml<br/>keep-awake ping"] -->|headless browser, ~7-8am| C
```

| Part | Lives in | Triggered by | What it controls |
|---|---|---|---|
| **The app** | `main` branch | any push to `main` | The code — layout, calculations, tabs |
| **The snapshot** | [.github/workflows/snapshot.yml](.github/workflows/snapshot.yml) → `snapshot-data` branch | cron 6–9am Central, or manual | The *data* served on cold start |
| **Keep-awake** | [.github/workflows/refresh.yml](.github/workflows/refresh.yml) | cron ~7–8am Central | Whether the app is already warm at 8am |

Key consequence: **the snapshot job does not redeploy the app, and a push does not
rebuild the snapshot.** That separation is deliberate — see the header comment in
[snapshot.py:1-25](snapshot.py#L1-L25) for why (committing parquets to `main` grew
history ~3.5 MB/day and restarted the container right before the morning visit).

---

## 2. One-time local setup

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

mkdir .streamlit
```

Then create `.streamlit/secrets.toml` by hand (the README mentions a
`secrets.toml.example`, but that file isn't actually in the repo):

This file is **TOML**, not JSON. Pasting the raw service-account JSON straight in
gives `Invalid format: please enter valid TOML` — it has to be a *value* assigned to
the `gcp_service_account_json` key, because [streamlit_app.py:228](streamlit_app.py#L228)
calls `json.loads()` on it as a string.

```toml
# NOTE the ''' quotes — single, not double. See the warning below.
gcp_service_account_json = '''
{"type":"service_account","project_id":"your-project","private_key_id":"...","private_key":"-----BEGIN PRIVATE KEY-----\nMIIE...\n-----END PRIVATE KEY-----\n","client_email":"...@your-project.iam.gserviceaccount.com","client_id":"...","token_uri":"https://oauth2.googleapis.com/token"}
'''

[bigquery]
dataset = "dv_supplier"
# Optional: skip runtime forecast-table discovery by pinning the table.
# forecast_table = "..."
```

> **Use `'''`, never `"""`.** TOML's `"""` is a *basic* string that processes escape
> sequences, so each `\n` inside `private_key` becomes a real newline — producing
> literal newlines inside a JSON string value, which is invalid JSON, and
> `json.loads()` dies with `Invalid control character`. TOML's `'''` is a *literal*
> string: it keeps `\n` as the two characters `\` and `n`, which is what's needed.
> Paste the downloaded JSON verbatim (minified onto one line is fine and simplest).

The same content goes in Streamlit Cloud's **Manage app → Settings → Secrets** box —
identical TOML, same quoting rule. Changes there take about a minute to propagate.

`.streamlit/secrets.toml` and `*.json` are already in [.gitignore](.gitignore#L8-L10).
**Never commit them.** Before any push, confirm `git status` doesn't list them.

Run it:

```powershell
streamlit run streamlit_app.py
```

> **Git isn't on this machine's PATH.** `git` fails in PowerShell here, so either
> install [Git for Windows](https://git-scm.com/download/win), or do your commits
> through GitHub Desktop / VS Code's Source Control panel. Everything below works
> the same either way — "push" just means whichever tool you use.

---

## 3. The everyday loop

1. **Edit** the code.
2. **Run locally** — `streamlit run streamlit_app.py`. Fix it here, not in production;
   a bad push takes the live dashboard down until the next push.
3. **Commit and push to `main`.**
4. **Streamlit redeploys automatically**, usually within a minute. Watch it at
   share.streamlit.io → your app → **Manage app** (bottom-right) → the log pane.
5. **Verify** — load the app, then click **Refresh data** in the sidebar to force a
   live pull that bypasses the snapshot.

The header line shows freshness and provenance, e.g.
`Jul 26, 2026 at 07:12 AM CDT (snapshot)` vs `(live pull)`
([streamlit_app.py:1052-1064](streamlit_app.py#L1052-L1064)). `(snapshot)` is
stamped with the *build's* time, not the moment it was read — so if that timestamp
is old, the snapshot job is the thing to look at, not the app.

---

## 4. What each kind of change actually requires

| You changed… | What happens | Anything extra? |
|---|---|---|
| Python/UI code | Push → redeploy ~1 min | No |
| [requirements.txt](requirements.txt) | Push → full rebuild, ~2 min | Watch the logs; a bad pin fails the boot |
| Secrets (key rotation, dataset) | Nothing automatic | Edit in **Manage app → Settings → Secrets**; app reloads itself. Update the matching **Actions** secret too |
| An existing `sql/*.sql` file | Push → redeploy | **Rerun the snapshot workflow**, or the app serves yesterday's parquet built from the *old* SQL until tomorrow's build |
| Query *parameters* in a loader | Push → redeploy | Must mirror the change in `snapshot_build.query_jobs` — see §5 |
| A **new** query | Push → redeploy | Full checklist in §5 |
| [constants.py](constants.py) (`ACTIVE_ITEMS`) | Push → redeploy | It's a query parameter, so the snapshot key changes → rerun the snapshot job |

---

## 5. Adding or changing a BigQuery query — the checklist

This is the one place where a half-done change fails *silently*. The snapshot key is
`sha256(sql_filename + parameter values)` ([snapshot.py:74-84](snapshot.py#L74-L84)),
computed identically by the app and the builder. If they disagree by even one
parameter value, `read_snapshot` returns `None`, the app quietly falls back to a live
BigQuery pull ([streamlit_app.py:308-325](streamlit_app.py#L308-L325)), and everything
still *works* — just slowly, on every cold start, forever. Nothing errors.

1. Add `sql/your_query.sql`, using the `{project}` / `{dataset}` placeholders.
2. Add a loader in [streamlit_app.py](streamlit_app.py) following the existing pattern:
   `@st.cache_data(ttl=86400, max_entries=3, ...)` wrapping
   `_cached_query("your_query.sql", [params...])`. Keep `max_entries` small — the
   cache key includes the hourly refresh slot and the lookback slider, so an
   uncapped loader retains a copy per combination and can blow Community Cloud's
   RAM cap ([streamlit_app.py:329-334](streamlit_app.py#L329-L334)).
3. Add the **same filename and same parameters** to `query_jobs()` in
   [snapshot_build.py:47-68](snapshot_build.py#L47-L68).
4. If the frame is large, add a dtype-shrinking transform to
   `transforms.SNAPSHOT_TRANSFORMS` ([transforms.py:173](transforms.py#L173)) so the
   builder commits an already-shrunk parquet.
5. Push, then **Actions → Build dashboard snapshot → Run workflow** to build it
   immediately instead of waiting for tomorrow morning.
6. Confirm: load the app cold, check the header says `(snapshot)`.

**Consult [docs/data_element_glossary.csv](docs/data_element_glossary.csv) before
using any new column** — it's the authoritative BI Link field reference. Table names
aren't in it; confirm those against the live dataset via `INFORMATION_SCHEMA`.

---

## 6. Secrets: two places, kept in sync

| Where | How to set | Keys |
|---|---|---|
| **Streamlit Cloud** (the app) | Manage app → Settings → Secrets | `gcp_service_account_json`, `[bigquery] dataset`, optional `forecast_table`, optional `snapshot_remote_token` |
| **GitHub Actions** (the snapshot builder) | Repo → Settings → Secrets and variables → Actions | `GCP_SERVICE_ACCOUNT_JSON`, `BQ_DATASET`, optional `FORECAST_TABLE` |

Same read-only service account in both. It needs `BigQuery Data Viewer` +
`BigQuery Job User`. **No write access anywhere** — the snapshot is published to a
git branch, not back into BigQuery.

`snapshot_remote_token` is only needed if you make the repo private: the app fetches
snapshots over the branch's raw URL, which 404s without auth
([snapshot.py:56-60](snapshot.py#L56-L60), [streamlit_app.py:206-209](streamlit_app.py#L206-L209)).

---

## 7. Rolling back

- **Bad code** — revert the commit and push. Redeploy is ~1 min, same as any push.
- **App wedged but code is fine** — Manage app → **Reboot app**. Clears the process
  and its cache; the next load rebuilds from the snapshot.
- **Bad data in the snapshot** — the `snapshot-data` branch holds exactly one
  force-pushed commit, so there's nothing to roll back to. Fix the query, rerun the
  workflow. Meanwhile the sidebar **Refresh data** button bypasses the snapshot
  entirely, and any snapshot older than 30h is ignored automatically
  ([snapshot.py:66](snapshot.py#L66)).

---

## 8. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `ModuleNotFoundError` on boot | dep missing from [requirements.txt](requirements.txt) | Add it, push |
| `google.auth.exceptions.RefreshError` | `private_key` mangled on paste | Re-paste, keeping `\n` sequences intact |
| `404 Not found: Dataset` | wrong `[bigquery] dataset` | Fix in secrets |
| `Forbidden` | service account missing IAM roles | Grant Data Viewer + Job User |
| One tab says *"… is temporarily unavailable"* | That loader raised; secondary loaders are fault-tolerant by design ([streamlit_app.py:214-222](streamlit_app.py#L214-L222)) | The real error is **server-side only** — read it in Manage app → logs. It's hidden because the dashboard is public and BigQuery errors leak project/dataset/table names |
| Forecast tab unavailable | dataset may hold only a *weekly* forecast; the tab needs daily `fcst_dt` | See the Forecast section in [README.md](README.md) and [forecast_source.py](forecast_source.py) |
| Header always says `(live pull)` on cold start | snapshot key mismatch, or the snapshot job is failing | Check §5 step 3; check the Actions tab for red runs |
| Header timestamp is a day+ old | snapshot job succeeded but the feed hasn't advanced, or the job is failing | Actions → Build dashboard snapshot → check recent runs |
| App is slow the first visit each morning | container went to sleep | `refresh.yml` is supposed to prevent this — check it's still enabled and the `DASHBOARD_URL` matches your real app URL |

---

## 9. The repo transfer, and what still points at the old owner

This repo was transferred from `nathantj123` to `kondabalapradeep`. GitHub keeps the
old owner's URLs redirecting indefinitely, so nothing broke — which is exactly why a
stale reference can sit unnoticed. Two kinds of reference:

- **Follows the repo automatically** — [snapshot.yml:103](.github/workflows/snapshot.yml#L103)
  pushes to `${GITHUB_REPOSITORY}`, so the snapshot *publish* side needed no change.
- **Hard-coded** — `SNAPSHOT_REMOTE_URL` in [snapshot.py:54-61](snapshot.py#L54-L61),
  the *read* side. Now updated to `kondabalapradeep`. It can also be overridden at
  runtime with the `SNAPSHOT_REMOTE_URL` env var, with no code change.

Still worth confirming yourself:

1. **Streamlit Cloud's link to the repo.** Community Cloud stores the app's source as
   `owner/repo`; a transfer doesn't update that record. Open share.streamlit.io →
   your app → **Manage app → Settings** and check the repo path reads
   `kondabalapradeep/funpop-dashboard-streamlit`. If it still shows the old owner,
   auto-deploy on push may not fire — see §3. This is the single most likely reason
   a push would silently not deploy.
2. **The keep-awake URL** is hard-coded to `https://funpop-dashboard.streamlit.app/`
   ([refresh.yml:63](.github/workflows/refresh.yml#L63)). Unrelated to the transfer,
   but if the app's URL ever differs, that workflow pings nothing every morning.
3. **Actions secrets don't transfer with a repo in every case.** Confirm
   `GCP_SERVICE_ACCOUNT_JSON` and `BQ_DATASET` still exist under the new owner
   (Settings → Secrets and variables → Actions). The snapshot built successfully
   today, so they're currently present.

---

## 10. Never commit

- `.streamlit/secrets.toml` or any `*.json` service-account key
- `snapshot_data/` — parquets belong on the `snapshot-data` branch only

Both are already covered by [.gitignore](.gitignore), so this only bites if someone
force-adds them.
