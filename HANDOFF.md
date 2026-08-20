# Sensio Tender & Grants — Developer Handoff

For whoever picks this up next. This file is the *why* and the *what's next*.

---

## What it is, in one line

One Flask dashboard (Dashboard, Tenders, Grants, Saved, two Analytics pages)
that merges what used to be two separate desktop apps — the Tender
Intelligence Tool and the Grant Intelligence Tool — backed by Supabase
Postgres and deployed on Render. The two Playwright + Gemini scrapers are
still scheduled on GitHub Actions (Sat/Sun), but the actual recommended way
to run them is now the double-click launchers in `executables/`, run
locally — see "Local execution vs. full CI automation" under Key decisions
for why that ended up being the better default, not just a fallback.

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
- **`tenders`/`grants` are fully replaced every scrape run, not
  accumulated.** Primary keys are date-stamped per run (`TND-DDMMYY-01`,
  `GRN-DDMMYY-01` — see Database schema below), and `db.replace_items()`
  wipes + re-inserts the whole table in one transaction each time
  `datasetManager.py`'s `write_rows_to_postgres()` runs (gated on
  `DATABASE_URL` being set; a local run without it just writes Excel as
  before, byte-identical to v1). The Excel workbook is still produced
  unchanged alongside it, mostly as a manual-QA artifact now.
- **A save is a full snapshot, not a reference** — `saved_items` carries its
  own copy of every field (title, sector, dates, budget, etc.), captured at
  the moment something's starred, specifically so it survives the *next*
  scrape wiping the main table out from under it. See Database schema and
  Key decisions.
- **Scheduled via three GitHub Actions workflows** (still active, but see
  the "Local execution vs. full CI automation" decision below for why local
  runs via `executables/` are the actual recommended path now):
  `scrape-tenders.yml` Saturdays 10:30 UTC (4pm IST), `scrape-grants.yml`
  Sundays 10:30 UTC (4pm IST), `notify-weekly.yml` Thursdays 01:30 UTC
  (7am IST) — a "dashboard is live" email with that week's top tender/grant.
  All three also support manual `workflow_dispatch`. Repo secrets required:
  `DATABASE_URL`, `GEMINI_API_KEYS`, `ANTHROPIC_API_KEY` (Claude fallback in
  CI), `SENDER_EMAIL`/`SENDER_APP_PASSWORD`/`RECIPIENT_EMAIL`/`DASHBOARD_URL`
  (the weekly email).
- **Both scrapers use `gemini-3.6-flash`** (upgraded from `gemini-2.5-flash`
  after confirming it's real and working against the actual API — see
  Landmines for the `thinkingConfig` incompatibility that came with it).
- **Local runs (`executables/run_*.command`/`.bat`) are the recommended way
  to scrape now — free Claude fallback, no Gemini-quota surprises going
  unnoticed for an hour.** CI still works and stays scheduled, but see the
  dedicated "Local execution vs. full CI automation" write-up under Key
  decisions for the real operational reasons this ended up being the better
  default, not just a cost-saving fallback.
- **`GEMINI_API_KEYS` must be in your local `.env` too now**, not just the
  GitHub secret — the scrapers moved into this repo in Phase 2 but
  `.env.example` never documented this var until it was noticed missing.
  Without it, every extraction/scoring call falls straight through to Claude
  instead of using Gemini as primary.
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
requirements.txt          Flask, flask-cors, pandas, openpyxl, psycopg2 (source
                           build, NOT psycopg2-binary — see Landmines),
                           python-dotenv, gunicorn, plus every scraper dependency
                           (playwright, playwright-stealth, redis, google-genai,
                           google-auth, sentence-transformers, httpx stack,
                           pydantic stack, cryptography stack, tenacity) — one
                           shared list for the whole repo
.gitignore                 also covers each scraper's run output now
                           (all_tenders.json/.xlsx, all_grants.json/.xlsx) --
                           these were accidentally committed when the scrapers
                           were first ported into this repo (the source dirs
                           had prior-run output sitting in them at copy time);
                           untracked via `git rm --cached`, left on disk

executables/               double-click launchers, kept out of the repo root
  run_tenders.command,
  run_grants.command,
  run_tenders.bat,
  run_grants.bat            Mac .command / Windows .bat pairs -- `cd .. ` back to
                            repo root first (they live one level down), then
                            create/reuse the shared .venv, install deps +
                            Playwright's Chromium, check/start Redis (brew on Mac;
                            Windows has no native package, so it just detects and
                            points to WSL/Memurai), check/install the `claude`
                            CLI, then run the respective scraper. This is the
                            free-Claude-fallback path (Pro/Max login already on
                            the machine) vs. CI's paid ANTHROPIC_API_KEY path

Website_frontend/         the live dashboard app; imported as Website_frontend.server
  desktop_app.py            local-dev launcher only (`python3
                            Website_frontend/desktop_app.py`) -- opens the
                            dashboard in a browser once Flask is accepting
                            connections; imports as `from server import app, PORT`
                            (same-directory, not package-qualified, since it lives
                            next to server.py now). Not used in production --
                            Render runs gunicorn directly via Procfile
  notify_weekly.py           weekly email: subject "weekly tender&grant dashboard is
                            live!", top tender + top grant by relevancy_score
                            (skipping already-closed ones), a dashboard link. Reads
                            Postgres directly via database.db (two dirname() calls
                            to reach the repo root, same pattern server.py uses) --
                            run standalone via notify-weekly.yml, not part of
                            either scraper
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
                            replace_items(domain, rows) (wipe+insert the whole
                            table in one transaction -- NOT an upsert, see Key
                            decisions), list_saved(domain) (reads saved_items'
                            own snapshot columns, no join against tenders/grants,
                            newest-saved-first), save_item() (snapshots the live
                            row's data at save time), remove_item(),
                            saved_ids_for(), all_saved_ids() — one
                            lazily-created global connection, same
                            single-sync-worker rationale as Production_Log's db.py
  schema.sql               tenders, grants, saved_items (now with its own title/
                            sector/country/dates/budget/description/org/url
                            snapshot columns, added via ALTER TABLE ADD COLUMN
                            IF NOT EXISTS for upgrade safety) + RLS enable
                            statements — run against Supabase, safe to re-run
  test_upsert_mapping.py    self-check for the scraper->Postgres row mapping (gate
                            on/off behaviour + budget-string-to-int parsing), with
                            database.db.replace_items monkeypatched (single
                            batch call now, not one call per row)

Scraper_backend_tenders/  ported from Project_Tender_Tool/Scraper_backend/, scraping
                           logic untouched
  main.py                  Adapters, Playwright driver, Gemini extraction (now
                            gemini-3.6-flash, no thinkingConfig -- see Landmines),
                            Redis dedup, thread runner, pipeline entry point. Drops
                            already-closed tenders before Layer 2/3 (status is
                            computed at scrape time in build_tender_object). Falls
                            back to Claude (llm_fallback.py) when every Gemini key
                            is exhausted, same as grants. No `if __name__` guard,
                            runs top-level on import; NEVER import this module,
                            only run it as a script
  datasetManager.py         Live FX, budget/date parsing, Excel compiler — PLUS
                            write_rows_to_postgres(), called right after
                            excel_rows is built, gated on DATABASE_URL being set,
                            calls db.replace_items() once with the whole batch
                            (not a per-row upsert). Primary Key is date-stamped
                            per run (TND-DDMMYY-01, e.g. TND-200826-01), not
                            year+sequential -- see Key decisions. _budget_to_int
                            keeps the decimal point (a naive [^\d] strip
                            inflated budgets ~100x -- see Landmines)
  llm_filtration.py         10-point rubric scorer + Gemini key rotation
                            (gemini-3.6-flash), falls back to Claude via
                            _score_with_claude when every key is rate-limited
  llm_fallback.py            Claude Code CLI fallback (ported from grants) --
                            shells out to `claude --print`, no code difference
                            from the grants copy beyond "tender"/"grant" wording
  semantic.py                Embedding model (sentence-transformers), cosine
                            similarity per sector — instantiates a real model at
                            import time, so this is also not import-safe for tests
  target_profiles.py, keywords.json, requirements.txt
                            unchanged from v1

Scraper_backend_grants/   ported from Project_CFP_Tool/Scraper_backend/scripts/,
                           same treatment as tenders (Claude fallback, closed-item
                           filter before Layer 2/3, gemini-3.6-flash, decimal-safe
                           budget parsing) plus the BASE_DIR fix described in
                           Landmines. llm_fallback.py is the original; tenders'
                           copy was ported FROM this one

.github/workflows/
  scrape-tenders.yml        Saturday 10:30 UTC cron + workflow_dispatch. Redis
                            service container, Node + `claude` CLI install,
                            `apt-get install libpq-dev` (psycopg2 builds from
                            source now -- see Landmines), Playwright install, runs
                            `python -m Scraper_backend_tenders.main` with
                            ANTHROPIC_API_KEY set (Claude fallback auth in CI --
                            see Key decisions), uploads the Excel workbook as a
                            build artifact
  scrape-grants.yml         Sunday 10:30 UTC cron + workflow_dispatch, identical
                            shape to scrape-tenders.yml
  notify-weekly.yml          Thursday 01:30 UTC cron + workflow_dispatch, installs
                            libpq-dev + psycopg2 directly (doesn't use
                            requirements.txt, it's a much smaller dependency set),
                            runs Website_frontend/notify_weekly.py with the email
                            secrets + DATABASE_URL
```

---

## Database schema — `tenders`, `grants`, `saved_items`

Two near-identical tables (`primary_key` PK, `title`, `sector`, `country`,
`opening_date`, `closing_date`, `relevancy_score`, `inr_budget_maximum`,
`description`, `organisation_name`, `url`, `scraped_at`) — deliberately only
the columns the dashboard actually reads, not every column the Excel workbook
carries (Excel also has Original Currency, Days Remaining, Tender/Grant
Status, Award Date — those stay Excel-only, unneeded by Postgres).

**`primary_key` is date-stamped per scrape run** (`TND-DDMMYY-01`,
`TND-DDMMYY-02`, ... / `GRN-DDMMYY-01`, ...), generated fresh in
`datasetManager.py`'s `json_to_excel` loop from that run's date + row index.
This is deliberate: **`tenders`/`grants` are wiped and re-inserted in full on
every run** (`db.replace_items()`), so the main sheet always reflects only
the current week's scrape, never accumulating stale rows from prior weeks.
There's no meaningful "same row as last week" to upsert against — a tender
scraped again next week gets a new key under that week's date, even if it's
the same real-world opportunity.

`saved_items(id, domain, item_id, founder, title, sector, country,
opening_date, closing_date, relevancy_score, inr_budget_maximum, description,
organisation_name, url, saved_at)`, `UNIQUE(domain, item_id, founder)` —
replaces the entire v1 per-founder-Excel-file design. `domain` is
`'tenders'` or `'grants'`; `item_id` has no FK to `tenders`/`grants` on
purpose. **Given the tables above get fully replaced every run, a save has
to carry its own full snapshot of the item's data, not just a
reference** — `db.save_item()` copies the item's current fields out of the
live table at the moment it's starred, and `db.list_saved()` reads entirely
from `saved_items`' own columns (no join back to `tenders`/`grants` at all)
so a starred item keeps showing correctly even after the next scrape wipes
the row it was copied from. This was verified end to end against the real
database: replaced `tenders` with a fresh batch under new date-stamped keys,
and a previously-saved tender (under the old week's key) still showed up
in the saved list with its original data intact.

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
- **Claude fallback in CI via `ANTHROPIC_API_KEY`, not `USE_CLAUDE_FALLBACK=0`.**
  Originally disabled entirely in CI (no `claude` login exists on a hosted
  runner). Revisited once tenders also needed fallback parity: both workflows
  now install Node + the `claude` CLI and authenticate it via
  `ANTHROPIC_API_KEY` (Anthropic Console, pay-per-token) instead of the
  interactive Pro/Max login `llm_fallback.py` was originally written for.
  `llm_fallback.py` itself needed zero code changes — it just shells out to
  `claude`, and the CLI's own `ANTHROPIC_API_KEY` env-var auth mode is what
  makes this work headlessly. This costs real money per fallback call, unlike
  local runs (see the local launcher scripts) — see "Ideas for future
  versions" for the self-hosted-runner alternative that would make CI free
  too, deliberately not built yet since it trades a small ongoing cost for a
  machine that must be on and reachable on a schedule.
- **Claude fallback ported to tenders for parity with grants.** Originally
  only grants had `llm_fallback.py` (drop-the-item was tenders' only
  behavior on Gemini exhaustion). Ported the exact same helpers, the same
  four fallback points in `main.py`'s extraction retry loop, and the same
  `_score_with_claude`/`_ClaudeResponse` pattern in `llm_filtration.py`.
- **Already-closed tenders/grants are dropped before Layer 2/3, not after.**
  Both `build_tender_object`/`build_grant_object` already computed an
  Open/Closed/Undetermined status at scrape time (closing date vs. today) --
  filtering `all_tenders`/`all_grants` on that right after Redis collection
  means no embedding or Gemini/Claude call is ever spent scoring something
  nobody can act on. "Undetermined" (missing/unparseable date) is kept, not
  dropped, since it can't be confirmed closed. This only affects what a given
  scrape processes — an item that was open when scraped still ages into
  "closed" on the dashboard later, since status there is derived from the
  stored closing_date at view time, not re-checked against this filter.
- **Two separate weekly workflow files, not one shared cron.** Wanted
  tenders on Saturday and grants on Sunday; a single `on.schedule` block
  fires for every job in the workflow, so per-job `if:` conditions on
  `github.event.schedule` would work but are more fragile than just having
  two independent, single-purpose workflow files. `notify-weekly.yml` follows
  the same one-workflow-one-purpose pattern.
- **`tenders`/`grants` fully replaced every run, keyed by scrape date, not
  upserted against a stable identity.** Explicit requirement, not a
  side-effect: the main sheet should always show *only* the current week's
  batch. The alternative (a stable content-hash key, upserting the same
  real-world tender in place across weeks) was considered and rejected —
  it's not what was wanted here. The real design cost of "replace weekly" is
  that saves can no longer reference the main table at all (see next item).
- **`saved_items` became a real snapshot, not a reference, as a direct
  consequence of the item above.** Once `tenders`/`grants` get wiped weekly,
  a `saved_items` row that only stored `item_id` and joined back to the live
  table for its data would lose that data the moment the next scrape ran —
  exactly what "saved tenders/grants preserved per founder" explicitly
  requires *not* to happen. `db.save_item()` now copies the item's full
  current data into `saved_items` at save time; `db.list_saved()` reads
  those columns directly, no join. Caught a real instance of this while
  building it: two pre-existing saves (made under the old reference-only
  design) had gone snapshot-less the moment the schema changed — backfilled
  from the still-live table before that data would have been lost for real
  on the next scrape.

### Local execution vs. full CI automation

The original plan was full automation: both scrapers on a GitHub Actions
schedule, Claude as an automatic fallback whenever Gemini got rate-limited,
zero manual steps. That's still partly true — the workflows exist, are
scheduled, and work — but real operational costs showed up that make
**running the scrapers locally via `executables/run_*.command`/`.bat` the
actual recommended path**, not just a fallback for when CI is inconvenient:

- **Gemini rate limiting in CI wasn't a rare edge case, it was routine.** A
  real scheduled run hit every one of 5 rotated Gemini keys 429-ing on
  nearly every remaining item, cycling through key rotation and cooldown
  waits repeatedly before falling back to a baseline score — consistent with
  a genuine daily quota limit, not the transient per-minute limit the
  rotator's cooldown logic was built to ride out. The run consumed its full
  45-minute `timeout-minutes` budget mostly retrying calls that were never
  going to succeed. Nothing was broken; the quota is just a real, hard
  ceiling that a scheduled, unattended run has no way to see coming or work
  around.
- **The Claude fallback that was supposed to catch that requires a machine
  that's already authenticated and *on*.** `llm_fallback.py`'s whole design
  point (same pattern as `LinkedIn Automation/scripts/generator.py`) is
  shelling out to the local `claude` CLI running on an interactive Pro/Max
  login — free, but that login is tied to a specific logged-in machine.
  GitHub's hosted runners are stateless VMs, fresh every run, with no
  persisted login and no human available to click through a browser OAuth
  flow unattended. The only way to make the fallback work in CI at all was
  swapping to `ANTHROPIC_API_KEY` — the CLI's separate, headless,
  pay-per-token auth mode, billed through the Anthropic Console, entirely
  independent of (and in addition to) the Claude subscription already being
  paid for. The alternative that avoids the extra cost — a self-hosted
  runner, i.e. registering an actual always-on machine to GitHub Actions and
  logging `claude` into it once — just relocates the "needs a machine that's
  on and reachable" constraint rather than removing it, so it wasn't worth
  building for what's fundamentally a once-a-week job.
- **Debugging a scraper problem in CI means push, wait, re-run, read logs,
  repeat** — every fix in this project's history (the `needrestart` hang,
  the psycopg2 segfault, the primary-key bug) that touched something CI-side
  cost several minutes of round-trip per attempt, on top of GitHub's own
  scheduling delays (a cron trigger firing late by 10+ minutes turned out to
  be normal, not a bug — see Landmines). Running locally, a failed run is
  immediately visible in the terminal, with the same Postgres and the same
  code, no push/wait cycle at all.
- **Net effect: local execution is both cheaper (no `ANTHROPIC_API_KEY`
  billing, Gemini's quota is the same either way but at least you see it
  happen live) and faster to iterate on** than the fully-automated version,
  for a job that only runs once a week and takes someone a few minutes to
  kick off by double-clicking a launcher. The CI workflows are left in place
  and scheduled — they're not broken, and still useful as a true "nobody
  remembered to run it" safety net — but they're the fallback now, not the
  primary path.

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
- **`psycopg2-binary` segfaulted the Render worker on almost every request
  that touched Postgres — but it took TWO fixes to actually resolve.**
  Symptom: `/api/grants/sensio-stream` (and intermittently other routes)
  came back as an empty 502 with no error body — nothing in Flask's own
  try/except caught it, because the actual failure was
  `[ERROR] Worker (pid:NN) was sent code 139!` in gunicorn's log, i.e. a
  real `SIGSEGV`, which a Python `except Exception` cannot catch at all.
  First fix: switched `requirements.txt` to source-built `psycopg2==2.9.10`
  (links against the system libpq instead of `-binary`'s bundled
  OpenSSL/libpq, which can conflict with a host's system libraries — the
  psycopg2 project's own docs recommend `-binary` for local dev only).
  Verified buildable and working locally (needs `libpq`/`openssl` dev
  headers on PATH — trivial on Linux/Render, needed explicit
  `LDFLAGS`/`CPPFLAGS` on this Mac). Deploy succeeded, but the identical
  segfault persisted — same `code 139`, confirmed by triggering a fresh
  request and checking the exact log lines right after, not just assumed.
  Second, actual fix: `server.py`'s `calculate_days_remaining()` was the
  only other native/C-extension code in that route's per-row loop, using
  `pandas.to_datetime` — pandas' C-accelerated date parser, compiled
  per-platform, didn't reproduce the crash locally (macOS ARM, Python 3.13)
  against the exact same 51 real rows, while Render runs Linux x86_64 +
  Python 3.12. Rewrote it to plain `datetime.strptime` (no pandas at all,
  and the scraper already normalises `closing_date` to `YYYY-MM-DD` before
  it reaches Postgres, so the flexible parsing pandas offered wasn't even
  needed) and removed the now-dead `import pandas as pd` from `server.py`
  entirely. That's what actually fixed it, confirmed by re-triggering a
  request and re-checking Render's logs. If a future dependency bump ever
  reintroduces `psycopg2-binary`, or pandas gets pulled back into a hot
  per-row path in `server.py`, treat "empty 502, code 139 in logs" as this
  same category of bug and check both.
- **`DATABASE_URL` must be set** for the dashboard to show anything —
  Render env var in prod, local `.env` for dev, GitHub Actions repo secret
  for the scrapers. Needs the **session pooler** connection string in every
  one of those three places, not the direct one.
- **A Supabase password containing a literal `@` broke connection parsing**
  (`postgresql://user:pass@word@host` — the parser reads part of the
  password as the hostname). Symptom: `could not translate host name` errors
  that made no sense until the password was inspected directly. Percent-
  encoding the `@` as `%40` is the general fix, but the password was reset
  entirely instead (Supabase dashboard -> Project Settings -> Database ->
  Reset database password, picking one with no `@`/`:`/`/`/`?`/`#` at all) —
  removes the whole class of bug rather than working around one instance,
  and doubled as rotating a credential that had been visible in chat/edit
  history. Whenever this is reset again, it must be updated in all three
  `DATABASE_URL` locations (`.env`, GitHub secret, Render env var) — a
  mismatch between what's stored and what Supabase actually has produces a
  `password authentication failed` error, easy to mistake for the `@`-escaping
  bug again.
- **A real Supabase password briefly landed in `.env.example` instead of
  `.env` during setup** (committed-template file vs. git-ignored local file —
  easy mix-up). Caught and fixed before it was ever pushed, but worth a
  reminder: `.env.example` is a template with a placeholder, `.env` (never
  committed) holds the real value.
- **`gemini-3.6-flash`'s `generationConfig.thinkingConfig` (used to force
  `thinkingBudget=0` on `gemini-2.5-flash`) is rejected outright** —
  `400 INVALID_ARGUMENT`, confirmed by direct testing against the real API.
  That model always thinks; the fix was dropping the `thinkingConfig` block
  entirely from the raw REST extraction payload in both `main.py` files
  (keeping `temperature`/`maxOutputTokens`), confirmed working the same way.
  Don't add `thinkingConfig` back for this model.
- **GitHub Actions runners can hang forever on `apt-get install` with no
  error** — Ubuntu 24.04 (`ubuntu-latest`) ships `needrestart`, which can
  interactively prompt ("Which services should be restarted?") during
  `playwright install --with-deps` with no terminal attached, silently
  hanging instead of failing. Fixed with `DEBIAN_FRONTEND=noninteractive` +
  `NEEDRESTART_MODE=a` on that step, plus a `timeout-minutes: 45` job-level
  safety net in both scrape workflows so any future hang fails loudly in
  under an hour instead of running toward GitHub's 6-hour default. That
  timeout has since fired for a legitimate reason too (see next item) — a
  cancelled run isn't automatically a bug, check what it was actually doing
  first.
- **A real Gemini quota exhaustion looks identical to a hang from the
  outside.** A `timeout-minutes`-killed run showed all 5 Gemini keys
  429-ing on every remaining item, cycling through key rotation + cooldown
  waits repeatedly before injecting a fallback baseline score each time —
  consistent with a genuine daily quota limit, not a transient rate limit
  (waiting longer wouldn't have helped; the retry logic already tried that).
  Worth checking the actual log for this pattern before assuming a
  timed-out run is a code bug.
- **A code-review pass on both scrapers found several real, silent
  data-corruption bugs**, since fixed: budget values inflated ~100x by a
  naive `[^\d]`-strip that also ate the decimal point (`_budget_to_int` in
  both `datasetManager.py` files); every grant's primary key stamped with
  the tenders prefix `"TND-"` instead of `"GRN-"` (copy-paste leftover);
  a bare `"K"`-in-string check that could 1000x-inflate a budget on any
  currency code containing K (DKK, KRW, PKR, HKD) — now requires a digit
  immediately before the K, mirroring the existing M/B checks; and almost
  every grant getting `Sector: "Unknown"` because `listMode` adapters call
  the scorer with an empty search keyword, which sector resolution keyed off
  — fixed by preferring the adapter's configured `category` when the keyword
  is empty. None of these were scraping-logic bugs; all were in the
  post-processing/data-shaping code around it.
- **A stray typo (`country TEXT,let`) briefly broke `database/schema.sql`**
  after the RLS statements were added and the file was hand-edited in an
  IDE — caught before commit. `schema.sql` should always be valid,
  re-runnable SQL; if a future edit to it fails in the Supabase SQL editor,
  check for exactly this kind of stray keystroke first.
- **The `saved_items` snapshot rewrite broke `list_saved()` with a
  `KeyError: 'primary_key'` in production**, caught immediately by testing
  the actual live endpoints after deploy rather than assuming the local
  self-checks covered it. `server.py`'s `_item_json()` does `row["primary_key"]`
  — fine for `load_items()` (reads `tenders`/`grants`, which has a
  `primary_key` column) but `list_saved()` now reads from `saved_items`,
  whose equivalent column is `item_id`. Fixed by aliasing it in the SQL
  (`latest.item_id AS primary_key`) so every row dict `_item_json()` touches
  has the same shape regardless of which table it came from, rather than
  special-casing the two call sites.
- **Two real pre-existing saves went silently snapshot-less the moment the
  schema changed**, since they were made under the old reference-only
  design before `saved_items` had snapshot columns at all — `list_saved()`
  would have shown them with every field `NULL` (renders as "Untitled
  Grant" with blank everything). Backfilled by reading their still-live
  `grants` row (hadn't been replaced by a new scrape yet) and copying the
  snapshot fields in by hand, one time. If this repo is ever handed a
  Supabase project with saves older than the snapshot rewrite, check for
  this before assuming they're just broken.
- **`all_tenders.json`/`all_tenders_pipeline.xlsx` (and the grants
  equivalents) were accidentally committed to git**, swept up when the
  scrapers were first ported into this repo — the source directories
  (`Project_Tender_Tool/Scraper_backend/`, etc.) had prior-run output
  sitting in them at copy time. Every real local scrape run then showed up
  as modifying tracked files instead of being ignored. Fixed: added them to
  `.gitignore` and `git rm --cached` (untracked, left on disk — they're
  legitimate local output, just shouldn't be version-controlled). Both
  files are cleanly overwritten every run (`json_to_excel` explicitly
  `os.remove()`s the old Excel first; the JSON is opened `"w"`), never
  accumulate or grow multiple sheets.
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

---

## Ideas for future versions

- **Self-hosted GitHub Actions runner**, if CI is ever wanted as more than a
  safety net again — see "Local execution vs. full CI automation" under Key
  decisions for the full reasoning. Register your own always-on machine
  under Settings -> Actions -> Runners, point `scrape-tenders.yml`/
  `scrape-grants.yml`'s `runs-on:` at it, run `claude login` on it once, and
  drop the `ANTHROPIC_API_KEY` env var — `llm_fallback.py` just uses
  whatever auth is already on the machine it runs on. Doesn't fix the
  Gemini-quota problem though, which was the bigger issue.
- **Split `requirements.txt` per deployment target** (`requirements-web.txt`
  / `requirements-scraper.txt`) if Render's build time from compiling the
  full scraper stack (Playwright, ML/crypto libs, psycopg2 from source) ever
  actually becomes annoying — currently just slower, not broken.

---

## Access you'll need

- The Supabase project (dashboard access, to read `DATABASE_URL`, restore a
  paused project, or run SQL by hand for anything destructive) — a separate
  project from `Production_Log`'s.
- Access to the Render service (to update env vars or redeploy) and the
  `neha-palak/sensio_tender_and_grants` GitHub repo, including its Actions
  secrets (`DATABASE_URL`, `GEMINI_API_KEYS`, `ANTHROPIC_API_KEY`,
  `SENDER_EMAIL`, `SENDER_APP_PASSWORD`, `RECIPIENT_EMAIL`, `DASHBOARD_URL`).
- The `Project_Tender_Tool` and `Project_CFP_Tool` repos, kept as v1
  reference/rollback — not deployed anywhere, not touched by this migration.
