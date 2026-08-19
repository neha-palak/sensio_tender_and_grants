# Sensio Tender & Grants — Developer Handoff

For whoever picks this up next. This file is the *why* and the *what's next*.

---

## What it is, in one line

One Flask dashboard (Dashboard, Tenders, Grants, Saved, two Analytics pages)
that merges what used to be two separate desktop apps — the Tender
Intelligence Tool and the Grant Intelligence Tool — backed by Supabase
Postgres and deployed on Render, with the same two Playwright + Gemini
scrapers now running on a weekly GitHub Actions schedule instead of on a
founder's laptop.

This is a v2 rewrite of two working v1 systems (`Project_Tender_Tool`,
`Project_CFP_Tool`, both still intact and untouched as reference/rollback).
v1 shipped as a PyInstaller desktop app per founder, reading a shared Excel
workbook on Google Drive, with per-founder `saved_<name>.xlsx` files to work
around Drive's lack of file locking. v2 replaces all of that with one hosted
app and a real database — the Drive-folder-discovery code and the
per-founder-file locking dance are gone entirely, not adapted.

---

## Current state

- **Working and deployed.** Public GitHub repo
  (`neha-palak/sensio_tender_and_grants`) → Render Web Service, deployed
  straight from the repo (no Docker). Free tier, so first load after idle can
  be slow (cold start) — same tradeoff as `../Production_Log`.
- **Database: Supabase Postgres**, free tier, separate project from
  Production Log's (different data domain, kept isolated). Connection string
  lives in `DATABASE_URL` — a Render environment variable in prod, a
  git-ignored local `.env` for dev, and a GitHub Actions repo secret for the
  scrapers.
- **Render (and GitHub Actions) must use Supabase's session pooler
  connection string, not the direct one** — same IPv6-reachability issue
  already hit and documented in `Production_Log/HANDOFF.md`. Get it from
  Supabase's **Connect** panel → **Session pooler**.
- **Row Level Security is enabled on all three tables, with zero policies.**
  The app connects as the `postgres` role via `DATABASE_URL`, which bypasses
  RLS, so this doesn't affect `database/db.py`. What it blocks is Supabase's
  auto-generated PostgREST API (the `anon`/`authenticated` keys) — this
  project never uses Supabase client SDKs or those keys, so RLS-enabled +
  no policies = deny by default to an attack surface that would otherwise be
  open by accident.
- **Scrapers write to Postgres now, not just Excel.** Both
  `Scraper_backend_tenders/` and `Scraper_backend_grants/` still produce
  their Excel workbook exactly as before (unchanged formatting/coloring
  logic) — the workbook is now just a build artifact for optional manual QA,
  uploaded by the GitHub Action. The write that matters is a new, gated
  Postgres upsert in each `datasetManager.py` (`upsert_rows_to_postgres`,
  only runs when `DATABASE_URL` is set).
- **Scheduled via two GitHub Actions workflows**, not a founder's cron/manual
  run: `scrape-tenders.yml` Saturdays 10:30 UTC (4pm IST), `scrape-grants.yml`
  Sundays 10:30 UTC (4pm IST). Both also support manual `workflow_dispatch`.
  Two repo secrets required: `DATABASE_URL`, `GEMINI_API_KEYS`.
- **No login anywhere** — same call as v1 and as Production Log. Founder
  identity is still just a typed name stored in browser local storage
  (`ensureFounderIdentity()`), now driving rows in the shared `saved_items`
  table instead of a per-founder Excel file. Public tender/grant data, a
  small trusted team — authentication would add friction and protect
  nothing.

---

## Architecture

```
Procfile                  `web: gunicorn --chdir Website_frontend server:app --bind
                           0.0.0.0:$PORT` — Render's start command. Stays at repo
                           root (required by Render); --chdir points into
                           Website_frontend/ the same way local dev already did
requirements.txt          Flask, flask-cors, pandas, openpyxl, psycopg2-binary,
                           python-dotenv, gunicorn, plus every scraper dependency
                           (playwright, playwright-stealth, redis, google-genai,
                           google-auth, httpx stack, pydantic stack, cryptography
                           stack, tenacity) — one shared list for the whole repo

Website_frontend/         the live dashboard app; imported as Website_frontend.server
  server.py                Flask API: Domain class + register_domain_routes() called
                            once per domain (tenders, grants) instead of hand-duplicated
                            route sets — /api/<plural>/sensio-stream, /save-<name>,
                            /saved-<plural>, /saved-ids, plus shared /api/shutdown
                            and static file serving. All reads/writes go through
                            database.db — no pandas Excel I/O left in this file
  index.html, tenders.html, grants.html, saved.html,
  analytics-tenders.html, analytics-grants.html
                            unified sidebar nav (Dashboard/Tenders/Grants/Saved/
                            Tender Analytics/Grant Analytics/Quit) across all pages
  tenders-data.js/ui.js, grants-data.js/ui.js,
  saved-tenders.js, saved-grants.js, shared-ui.js, dashboard.js
                            per-domain JS kept separate and namespaced (not
                            force-deduped) rather than one shared generic engine —
                            see "Key decisions" below for why
  base.css / components.css / pages.css
                            one shared stylesheet; grants pages add a `theme-grants`
                            class on <body> to retint the teal accent to blue
  test_server.py            self-check: Flask test client against every route, with
                            database.db functions monkeypatched (no live DB needed)

database/                 imported as `database.db` (server.py: `from database import db`)
  db.py                    Postgres access: get_conn(), load_items(domain),
                            upsert_item(domain, row), list_saved(domain),
                            save_item()/remove_item(), saved_ids_for(), all_saved_ids()
                            — one lazily-created global connection, same
                            single-sync-worker rationale as Production_Log's db.py
  schema.sql               tenders, grants, saved_items table definitions + RLS
                            enable statements — run against Supabase, safe to re-run
  test_upsert_mapping.py    self-check for the scraper->Postgres row mapping (gate
                            on/off behaviour + budget-string-to-int parsing), with
                            database.db.upsert_item monkeypatched

Scraper_backend_tenders/  ported from Project_Tender_Tool/Scraper_backend/, scraping
                           and LLM-filtration logic untouched
  main.py                  Adapters, Playwright driver, Gemini extraction (now
                            gemini-3.6-flash), Redis dedup, thread runner, pipeline
                            entry point — no `if __name__` guard, runs top-level on
                            import; NEVER import this module, only run it as a script
  datasetManager.py         Live FX, budget/date parsing, Excel compiler — PLUS the
                            new upsert_rows_to_postgres(), called right after
                            excel_rows is built, gated on DATABASE_URL being set
  llm_filtration.py         10-point rubric scorer + Gemini key rotation
                            (gemini-3.6-flash)
  semantic.py                Embedding model (sentence-transformers), cosine
                            similarity per sector — instantiates a real model at
                            import time, so this is also not import-safe for tests
  target_profiles.py, keywords.json, requirements.txt
                            unchanged from v1

Scraper_backend_grants/   ported from Project_CFP_Tool/Scraper_backend/scripts/,
                           same treatment as tenders, with the extra
                           llm_fallback.py (Claude CLI fallback — disabled in CI,
                           see Landmines) and the BASE_DIR fix described below

.github/workflows/
  scrape-tenders.yml        Saturday 10:30 UTC cron + workflow_dispatch. Redis
                            service container, Playwright install, runs
                            `python -m Scraper_backend_tenders.main`, uploads the
                            Excel workbook as a build artifact
  scrape-grants.yml         Sunday 10:30 UTC cron + workflow_dispatch, same shape,
                            plus USE_CLAUDE_FALLBACK=0 (see Landmines)

desktop_app.py             still present for local dev convenience (opens the
                            dashboard in a browser once Flask is accepting
                            connections) — not used in production; Render runs
                            gunicorn directly via Procfile
```

---

## Database schema — `tenders`, `grants`, `saved_items`

Two near-identical tables (`primary_key` PK, `title`, `sector`, `country`,
`opening_date`, `closing_date`, `relevancy_score`, `inr_budget_maximum`,
`description`, `organisation_name`, `url`, `scraped_at`) — deliberately only
the columns the dashboard actually reads, not every column the Excel workbook
carries (Excel also has Original Currency, Days Remaining, Tender/Grant
Status, Award Date — those stay Excel-only, unneeded by Postgres).

`saved_items(id, domain, item_id, founder, saved_at)`, `UNIQUE(domain,
item_id, founder)` — replaces the entire v1 per-founder-Excel-file design.
`domain` is `'tenders'` or `'grants'`; `item_id` has no FK to
`tenders`/`grants` on purpose — a save is a snapshot that should survive the
source row being dropped by the next scrape, same intent as v1's per-founder
snapshot files, just enforced by a real unique constraint instead of "don't
let two machines write the same file."

RLS is enabled on all three tables with zero policies — see "Current state"
above for why that's safe for how this app connects.

---

## Key decisions & why

- **Postgres instead of Excel-on-Drive, mirroring `Production_Log`'s
  already-proven stack.** The single biggest complexity v1 carried was
  entirely about working around Google Drive having no cross-machine file
  locking: per-founder `saved_<name>.xlsx` files, a merge-on-read step, a
  legacy-file migration function, a `threading.Lock()` per domain. All of it
  is gone — `saved_items` with a real `UNIQUE` constraint does the same job
  in one table.
- **The Drive-folder-discovery code is deleted, not kept as a fallback.**
  v1's `server.py` had `_find_drive_data_dir()` / `_next_to_app_dir()` to
  locate a shared Drive folder across machines and OSes — real, working code,
  but entirely pointless once data lives in Postgres. Deleted outright rather
  than left dead, per this repo's general bias toward deletion over
  speculative future use.
- **Backend merge uses one parameterized `Domain` class + route factory,
  not two hand-duplicated route sets.** The two v1 `server.py` files were
  ~95% byte-identical (`s/tender/grant/`). `register_domain_routes()` is
  called once per domain instead.
- **Frontend merge is nav-level, not a full JS/CSS rewrite.** The two v1
  frontends were also near-duplicate forks of each other, but with real
  identifier coupling (`window.SensioData`, `handleSaveToggle`,
  `openTenderModal`, DOM ids like `tenderModalOverlay`) baked into every
  function name. Deeply genericizing all of that into one shared engine
  was judged a much bigger, riskier rewrite than what was asked for ("one
  dashboard, unified nav") — so `tenders-*.js`/`grants-*.js` stay separate,
  namespaced only where they must coexist on one page (`index.html`,
  `saved.html`).
- **Scrapers still write Excel, Postgres write is additive and gated.** The
  Postgres write is one new function, gated on `DATABASE_URL`, called after
  the existing `excel_rows` list is already built — so a local run without
  touching prod Postgres behaves byte-identically to v1. The
  scraping/extraction/scoring logic itself was never touched.
- **Scrapers moved into this repo as separate packages
  (`Scraper_backend_tenders`, `Scraper_backend_grants`), not left in the v1
  repos.** GitHub Actions needs the code in the repo it's triggered from.
  `Project_Tender_Tool`/`Project_CFP_Tool` remain untouched as the v1
  reference/rollback.
- **`USE_CLAUDE_FALLBACK=0` for the grants scraper in CI.** Its
  `llm_fallback.py` shells out to a local `claude` CLI (an interactive Claude
  Code login) when every Gemini key is rate-limited. No such login exists in
  a GitHub Actions runner — disabling it explicitly is cleaner than letting
  it fail-detect the missing binary at runtime.
- **Two separate weekly workflow files, not one shared cron.** Wanted
  tenders on Saturday and grants on Sunday; a single `on.schedule` block
  fires for every job in the workflow, so per-job `if:` conditions on
  `github.event.schedule` would work but are more fragile than just having
  two independent, single-purpose workflow files.

---

## Landmines

- **`Scraper_backend_tenders/main.py` and `Scraper_backend_grants/main.py`
  have no `if __name__ == "__main__":` guard.** Both are top-level scripts —
  importing either one launches real Playwright browsers, hits real
  tender/grant portal websites, and calls the real Gemini API immediately.
  Never `import` them, including for tests — only ever run as
  `python -m Scraper_backend_tenders.main` (or the grants equivalent), and
  only when you actually mean to run a real scrape.
- **`semantic.py` in both packages instantiates a `SentenceTransformer` and
  calls `.encode()` at import time** — a real model download + compute. Also
  not safe to import casually; `py_compile` it instead if you just need a
  syntax check.
- **The grants package's `BASE_DIR` needed a real fix during the move.**
  `Project_CFP_Tool`'s `main.py`/`semantic.py`/`llm_filtration.py` computed
  `BASE_DIR = dirname(dirname(abspath(__file__)))` — correct only because
  `main.py` used to live two levels under `Scraper_backend/scripts/`.
  Flattening that nesting into one `Scraper_backend_grants/` package (to
  match `Scraper_backend_tenders/`'s layout) would have silently resolved
  `BASE_DIR` to the repo root instead of the package directory, breaking
  `keywords.json` loading and misplacing `all_grants.json`/
  `all_grants_pipeline.xlsx` output. Fixed to a single
  `dirname(abspath(__file__))` in all three files, matching the tenders
  package's already-correct pattern. If either package is ever moved or
  restructured again, re-check this.
- **`DATABASE_URL` must be set** for the dashboard to show anything —
  Render env var in prod, local `.env` for dev, GitHub Actions repo secret
  for the scrapers. Needs the **session pooler** connection string in every
  one of those three places, not the direct one.
- **A real Supabase password briefly landed in `.env.example` instead of
  `.env` during setup** (committed-template file vs. git-ignored local file —
  easy mix-up). Caught and fixed before it was ever pushed, but worth a
  reminder: `.env.example` is a template with a placeholder, `.env` (never
  committed) holds the real value.
- **A stray typo (`country TEXT,let`) briefly broke `database/schema.sql`**
  after the RLS statements were added and the file was hand-edited in an
  IDE — caught before commit. `schema.sql` should always be valid,
  re-runnable SQL; if a future edit to it fails in the Supabase SQL editor,
  check for exactly this kind of stray keystroke first.
- **Supabase free-tier project pauses after 7 days of zero activity** — same
  as `Production_Log`. Not data loss, but the dashboard will error until
  someone hits "restore" in the Supabase dashboard.
- **Render's build installs the full scraper dependency stack too**
  (Playwright, google-genai, the ML/crypto stack) even though the web
  service never runs a scraper — `requirements.txt` is shared across the
  whole repo rather than split per-deployment-target. Slower Render builds,
  not a correctness problem; split it into `requirements-web.txt` /
  `requirements-scraper.txt` if build time ever actually matters.
- **Destructive SQL (`DROP TABLE`, `TRUNCATE`, etc.) against the live DB
  gets auto-blocked** if run through an AI coding assistant in auto mode,
  even after explicit chat confirmation — same safety gate documented in
  `Production_Log/HANDOFF.md`. Run anything genuinely destructive by hand in
  Supabase's SQL editor instead.

---

## What was deferred (v1 → v2)

- **Analytics charts stay per-domain** (`analytics-tenders.html` /
  `analytics-grants.html`) rather than one merged chart set — the underlying
  Chart.js logic wasn't touched, only re-wired to the new endpoints/nav.
- **No desktop packaging for v2.** v1's PyInstaller `.spec` /
  `windows_app.py` pattern isn't replicated here — the dashboard is a hosted
  Render service now, not a per-founder double-click app. The v1 repos'
  specs still work if a standalone build is ever needed again.
- **Full JS/CSS unification beyond shared chrome** — see "Key decisions"
  above.
- **A manual test run of both scrapers against the live Supabase project**,
  triggered via `workflow_dispatch`, to confirm end-to-end before trusting
  the first automatic weekend run.

---

## Access you'll need

- The Supabase project (dashboard access, to read `DATABASE_URL`, restore a
  paused project, or run SQL by hand for anything destructive) — a separate
  project from `Production_Log`'s.
- Access to the Render service (to update env vars or redeploy) and the
  `neha-palak/sensio_tender_and_grants` GitHub repo, including its Actions
  secrets (`DATABASE_URL`, `GEMINI_API_KEYS`).
- The `Project_Tender_Tool` and `Project_CFP_Tool` repos, kept as v1
  reference/rollback — not deployed anywhere, not touched by this migration.
