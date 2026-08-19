# Sensio Dashboard

Combines the Tender Tool and Grants (CFP) Tool into one Flask app with one
sidebar: Dashboard, Tenders, Grants, Saved, Tender Analytics, Grant Analytics.

Both scrapers are unchanged and keep running independently (see
`Project_Tender_Tool/Scraper_backend` and `Project_CFP_Tool/Scraper_backend`)
— this app only reads the two Excel files they already produce.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and point the two vars at the SAME shared Drive
folders the existing Tender Tool and Grants Tool already use — no data
migration needed:

```
TENDER_DATA_DIR=/path/to/Sensio Tender Tool
GRANT_DATA_DIR=/path/to/Sensio Grants Tool
```

## Run

```bash
python3 desktop_app.py          # opens the dashboard in your browser
# or, for dev:
cd Website_frontend && python3 -m flask --app server run --port 5001
```

Default port is 5001 (override with `SENSIO_DASHBOARD_PORT`).

## Self-check

```bash
python3 Website_frontend/test_server.py
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
