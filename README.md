# Sensio Dashboard

Combines the Tender Tool and Grants (CFP) Tool into one Flask app with one
sidebar: Dashboard, Tenders, Grants, Saved, Tender Analytics, Grant Analytics.

Data lives in Supabase Postgres (`database/schema.sql`: `tenders`, `grants`,
`saved_items`) instead of the two tools' old Excel files on a shared Drive
folder — see `database/db.py` for all reads/writes. Deployed on Render
(`Procfile`); same stack as `../Production_Log`.

**Scrapers**: `Scraper_backend_tenders/` and `Scraper_backend_grants/` (ported
from `Project_Tender_Tool`/`Project_CFP_Tool`, scraping/LLM-filtration logic
untouched). Each still writes its Excel workbook as before, and — when
`DATABASE_URL` is set — also upserts every row into `tenders`/`grants`
(`datasetManager.py`'s `upsert_rows_to_postgres`). Runs on a schedule via
`.github/workflows/scrape.yml` (weekly, Monday 03:00 UTC, plus manual
`workflow_dispatch`). Requires two repo secrets before it does anything real:
`DATABASE_URL` (session pooler string) and `GEMINI_API_KEYS`. **The weekly
cron trigger is committed and live as soon as this is pushed** — disable it
in the workflow file first if you're not ready for it to run automatically.

Run a scraper locally: `python -m Scraper_backend_tenders.main` (or
`Scraper_backend_grants.main`) from the repo root, with `redis-server` running
locally and `GEMINI_API_KEYS`/`DATABASE_URL` set in `.env`.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create a Supabase project, run `database/schema.sql` against it (Supabase SQL
editor, or `psql $DATABASE_URL -f database/schema.sql`), then copy
`.env.example` to `.env` and set `DATABASE_URL` to its **session pooler**
connection string (see `.env.example` for why not the direct one).

## Run

```bash
python3 Website_frontend/desktop_app.py   # opens the dashboard in your browser
# or, for dev:
cd Website_frontend && python3 -m flask --app server run --port 5001
```

Default port is 5001 locally (override with `SENSIO_DASHBOARD_PORT`). On
Render, `Procfile` binds to Render's own `$PORT`.

## Self-check

```bash
python3 Website_frontend/test_server.py
python3 database/test_upsert_mapping.py
```

## What was deferred

- **Analytics charts stay per-domain** (`analytics-tenders.html` /
  `analytics-grants.html`) rather than one merged chart set — the underlying
  Chart.js logic wasn't touched, only re-wired to the new endpoints/nav.
- **Packaging** (PyInstaller `.spec`, `windows_app.py`) — the original two
  tools' specs still work for building those separately; this project isn't
  wired for a standalone build yet.
- **Full JS/CSS unification beyond the shared chrome** — `tenders.js` /
  `grants.js` (browse-all filtering) and the CSS ruleset stay as near-verbatim
  per-domain copies. Only the pieces that must coexist on one page (the
  save/founder-identity/modal plumbing loaded on `index.html` and
  `saved.html`) were de-duplicated and namespaced to avoid collisions.
