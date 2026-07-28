# Transferring the FunPop Dashboard to a new owner

This is a step-by-step checklist for handing the dashboard off completely, so
that **nothing keeps running under your accounts** afterward. Work top to
bottom. Where a step is the new owner's to do, it says so.

## What you're actually transferring

The dashboard is four coupled pieces, not just the code:

| Piece | Lives in | Transfers with the repo? |
|---|---|---|
| **Code** | This GitHub repo | Yes — transfer or re-push |
| **Hosting** | Streamlit Community Cloud app + its Secrets | **No** — tied to your account; new owner redeploys |
| **Data access** | GCP service-account JSON → BigQuery BI Link dataset | **No** — new key, added to two places |
| **Automation** | GitHub Actions (`snapshot.yml`, `refresh.yml`) + `snapshot-data` branch | Code yes, **secrets no** — re-added in new repo |

Two rules that trip people up:

- **GitHub Actions secrets and variables never transfer with a repo.** They
  must be re-created in the destination repo by hand (Steps 4–5).
- **Streamlit Community Cloud apps can't be reassigned** between accounts. The
  new owner creates their own app; it gets a new `*.streamlit.app` URL. Your
  old app keeps running until you delete it (Step 8).

---

## Before you start: pre-flight cleanup

- [ ] **Revoke the old exposed GitHub token.** `README.md` still warns about a
  leaked token (`ghp_dmQH...`). Confirm it's revoked at
  <https://github.com/settings/tokens> before handing anyone the repo.
- [ ] **Decide the data question:** does the new owner get **their own** GCP
  project + BI Link feed, or keep pulling from **yours**?
  - *Their own* (recommended) — they create the service account in their GCP
    project; you owe them nothing after handoff. Use this checklist as written.
  - *Yours* — you'll also need to transfer the GCP project (or keep paying for
    it). See "If they inherit your GCP project" at the bottom.

---

## Step 1 — Move the code (GitHub)

Pick one:

- **Transfer the repo** (keeps history, issues, branches):
  GitHub → repo **Settings → General → Danger Zone → Transfer ownership**.
  The `snapshot-data` branch comes along but will rebuild itself anyway, so it
  doesn't matter if it's stale.
- **Or fresh repo** — new owner creates their own repo and you (or they) push
  the code:
  ```bash
  git clone https://github.com/nathantj123/funpop-dashboard-streamlit.git
  cd funpop-dashboard-streamlit
  git remote set-url origin https://github.com/NEW_OWNER/NEW_REPO.git
  git push -u origin main
  ```
  The `snapshot-data` branch does not need to be pushed — the snapshot job
  recreates it on its first run (Step 6).

> Secrets are **not** in the repo (`.gitignore` excludes `.streamlit/secrets.toml`
> and `*.json`), so nothing sensitive moves with the code. Good — that's why the
> credential steps below exist.

## Step 2 — Create the BigQuery read-only service account (new owner)

In the GCP project that holds the Walmart BI Link `dv_supplier` dataset:

- [ ] IAM & Admin → **Service Accounts → Create service account**
      (e.g. `dashboard-reader`).
- [ ] Grant it these roles on the project — **read only, nothing else**:
  - `BigQuery Data Viewer`
  - `BigQuery Job User`
- [ ] **Keys → Add key → Create new key → JSON.** Download the JSON. This file
      is the credential pasted in Steps 3 and 4. Keep it out of git.

## Step 3 — Deploy the app on Streamlit Community Cloud (new owner)

- [ ] Go to <https://share.streamlit.io>, sign in with the new owner's GitHub.
- [ ] **Create app** → pick the repo, branch `main`, main file `streamlit_app.py`.
- [ ] **Advanced settings → Secrets** → paste a filled-in copy of
      [`.streamlit/secrets.toml.example`](.streamlit/secrets.toml.example):
  - `gcp_service_account_json` — the **entire** JSON from Step 2, between the
    triple quotes. Keep the `private_key` `\n` sequences intact.
  - `[bigquery] dataset` — e.g. `dv_supplier`.
  - `forecast_table` — only if you pin the forecast table; otherwise leave it
    commented out (the app discovers it at runtime).
- [ ] **Deploy.** First build ~2 min. **Write down the new URL** (e.g.
      `https://NEW-NAME.streamlit.app`) — you need it in Step 5.

> Sanity check: open the app, then click **Refresh data** in the sidebar to
> force a live BigQuery pull. If data loads, credentials and dataset are correct.

## Step 4 — Add the GitHub Actions **secrets** (new repo)

The snapshot builder runs in Actions and needs its own copy of the credentials.
In the destination repo: **Settings → Secrets and variables → Actions → Secrets
tab → New repository secret**:

- [ ] `GCP_SERVICE_ACCOUNT_JSON` — the full JSON from Step 2 (same key as Step 3).
- [ ] `BQ_DATASET` — the dataset name, e.g. `dv_supplier`.
- [ ] `FORECAST_TABLE` — **only** if you pinned `forecast_table` in Step 3;
      otherwise skip it (discovery handles it).

## Step 5 — Add the GitHub Actions **variable** for the keep-awake URL (new repo)

`refresh.yml` pings the deployed app to keep it warm. It now reads the URL from
a repo **variable**, falling back to the original app if unset — so set the
variable or you'll be pinging the *old* app.

- [ ] **Settings → Secrets and variables → Actions → Variables tab → New
      repository variable**:
  - `DASHBOARD_URL` = the new app URL from Step 3 (e.g.
    `https://NEW-NAME.streamlit.app/`).

## Step 6 — Build the first snapshot (new repo)

- [ ] **Actions tab → "Build dashboard snapshot" → Run workflow.** This runs
      every query and force-pushes results to the `snapshot-data` branch, so
      cold loads are instant. Confirm the run succeeds (green check).
- [ ] Optional: **Actions → "Dashboard keep-awake" → Run workflow** once to
      confirm it loads the new URL without error.

> After this, both workflows run on their own schedules (mornings, Central
> time). Nothing else to wire up.

## Step 7 — Verify the handoff end to end

- [ ] App opens at the new URL and shows data.
- [ ] Sidebar **Refresh data** returns fresh data (proves live BigQuery access).
- [ ] `snapshot-data` branch has a recent commit (proves the snapshot job works).
- [ ] The "Build dashboard snapshot" and "Dashboard keep-awake" Actions runs are
      green.
- [ ] The **Forecast tab** shows data (not "temporarily unavailable"). If it's
      empty, the dataset may only have a *weekly* forecast — see
      [`README.md`](README.md) "Forecast tab data source".

## Step 8 — Decommission your side (you, after they're verified live)

Only once the new owner confirms everything works:

- [ ] **Delete your Streamlit app** at share.streamlit.io (frees the old URL and
      stops it serving stale data). Do this only if you're fully handing off.
- [ ] **Disable/delete your old service-account key** in GCP so your credential
      no longer reaches the data.
- [ ] If you transferred the repo, you've already lost admin on it — nothing to
      do. If they forked/re-pushed instead, delete or archive your old repo so
      its stale Actions stop running (they'd still ping/point at your app).
- [ ] Remove your local `.streamlit/secrets.toml` if you're done with it.

---

## If they inherit your GCP project (not a fresh one)

If the new owner keeps *your* BigQuery project and BI Link feed rather than
standing up their own:

- Transfer the GCP project separately in the **GCP console → IAM & Admin →
  Settings → Migrate/transfer**, or add the new owner as an **Owner** on the
  project and remove yourself. Billing moves with it — make sure the new owner's
  billing account is attached, or the project (and the dashboard) stops working.
- The service account and its key can stay as-is; just make sure the new owner
  controls the project that owns it, and rotate the key so the credential
  history is theirs.
- Everything else (Streamlit app, Actions secrets/variables) is still per the
  steps above.

## Quick reference — where each secret goes

| Value | Streamlit Secrets (Step 3) | Actions Secret (Step 4) | Actions Variable (Step 5) |
|---|:---:|:---:|:---:|
| Service-account JSON | `gcp_service_account_json` | `GCP_SERVICE_ACCOUNT_JSON` | — |
| Dataset name | `[bigquery].dataset` | `BQ_DATASET` | — |
| Forecast table (optional) | `[bigquery].forecast_table` | `FORECAST_TABLE` | — |
| Deployed app URL | — | — | `DASHBOARD_URL` |
| Snapshot token (private repo only) | `snapshot_remote_token` | — | — |
