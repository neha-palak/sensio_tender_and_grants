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
- **Scheduled via three GitHub Actions workflows**, not a founder's
  cron/manual run: `scrape-tenders.yml` Saturdays 10:30 UTC (4pm IST),
  `scrape-grants.yml` Sundays 10:30 UTC (4pm IST), `notify-weekly.yml`
  Thursdays 01:30 UTC (7am IST) — a "dashboard is live" email with that
  week's top tender/grant. All three also support manual `workflow_dispatch`.
  Repo secrets required: `DATABASE_URL`, `GEMINI_API_KEYS`,
  `ANTHROPIC_API_KEY` (Claude fallback in CI — see Key decisions),
  `SENDER_EMAIL`/`SENDER_APP_PASSWORD`/`RECIPIENT_EMAIL`/`DASHBOARD_URL` (the
  weekly email).
- **Both scrapers use `gemini-3.6-flash`** (upgraded from `gemini-2.5-flash`
  after confirming it's real and working against the actual API — see
  Landmines for the `thinkingConfig` incompatibility that came with it).
- **Local runs get Claude fallback for free; CI runs pay per-token for it.**
  `run_tenders.command`/`run_grants.command` (Mac) and the matching `.bat`
  files (Windows) set up every dependency and run a scraper locally, where
  `llm_fallback.py` reuses your already-logged-in Claude Code Pro/Max
  session at no extra cost. The GitHub Actions runs authenticate the same
  `claude` CLI via `ANTHROPIC_API_KEY` instead, since hosted runners have no
  persistent login to reuse — see "Ideas for future versions" for the
  self-hosted-runner path that would make CI free too.
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
                            upsert_rows_to_postgres(), called right after
                            excel_rows is built, gated on DATABASE_URL being set.
                            _budget_to_int keeps the decimal point (a naive
                            [^\d] strip inflated budgets ~100x -- see Landmines)
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
  that touched Postgres.** Symptom: `/api/grants/sensio-stream` (and
  intermittently other routes) came back as an empty 502 with no error body
  — nothing in Flask's own try/except caught it, because the actual failure
  was `[ERROR] Worker (pid:NN) was sent code 139!` in gunicorn's log, i.e. a
  real `SIGSEGV`, which a Python `except Exception` cannot catch at all. It
  happened on nearly every request cycle (tenders' endpoint often "worked"
  only because its response got sent before the crash landed), constantly
  respawning the single gunicorn worker. Root cause: `psycopg2-binary`
  bundles its own OpenSSL/libpq, which can conflict with a host's system
  libraries — the psycopg2 project's own docs recommend it for local dev
  only, not production. Fix: switched `requirements.txt` to source-built
  `psycopg2==2.9.10` (links against the system libpq instead). Verified
  buildable and working locally (needs `libpq`/`openssl` dev headers on
  PATH — trivial on Linux/Render, needed explicit `LDFLAGS`/`CPPFLAGS` on
  this Mac) before pushing. If a future dependency bump ever reintroduces
  `-binary`, expect this exact symptom again.
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

## Ideas for future versions

- **Claude fallback in CI currently costs real money (`ANTHROPIC_API_KEY`),
  not your Pro/Max subscription.** `Scraper_backend_grants/llm_fallback.py`
  shells out to the `claude` CLI when every Gemini key is exhausted — same
  pattern as `LinkedIn Automation/scripts/generator.py`, which only ever runs
  locally on a machine with an interactive `claude login` already done
  (Pro/Max subscription, no extra cost). GitHub's hosted runners are fresh,
  stateless VMs every run — there's no persisted login to reuse and no human
  to click through a browser login unattended — so `scrape-grants.yml`
  authenticates via `ANTHROPIC_API_KEY` instead (Anthropic Console, pay-per-
  token, a repo secret) to make the fallback work there at all.
  If the per-token cost ever matters enough to avoid: register a **self-hosted
  runner** (your own machine or a persistent VM, added to this repo under
  Settings -> Actions -> Runners) and point `scrape-grants.yml`'s
  `runs-on:` at it instead of `ubuntu-latest`. Run `claude login` on that
  machine once — after that, drop the `ANTHROPIC_API_KEY` env var from the
  workflow entirely; `llm_fallback.py` just shells out to `claude` and uses
  whatever auth is already configured on the machine it runs on, so the
  fallback reuses the Pro/Max subscription for free from then on. Tradeoff:
  that machine needs to actually be on and reachable whenever the scheduled
  workflow fires (currently Sunday 4pm IST for grants).
- **Tenders has no Claude fallback at all** (`main.py` just drops the item
  when Gemini's exhausted) — porting `llm_fallback.py` over would need the
  same CI setup (Node + the `claude` CLI + auth) added to
  `scrape-tenders.yml` too.

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
