from dotenv import load_dotenv

load_dotenv()

import time
import json
import threading
import hashlib
import re
import os
import requests
from datetime import datetime, date
from typing import Optional
from urllib.parse import urljoin

from playwright.sync_api import sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright_stealth import Stealth
import redis

from Scraper_backend_grants.datasetManager import json_to_excel
from Scraper_backend_grants.semantic import semantic_filter
from Scraper_backend_grants.llm_filtration import evaluate_and_score_grants
from Scraper_backend_grants.llm_fallback import (
    claude_available,
    extract_with_claude,
    parse_llm_json,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

start_time = time.perf_counter()

# The .py files live in Scraper_backend/scripts/, but the data files
# (keywords.json, all_grants.json, the workbook) live in the Scraper_backend/
# root — so resolve them against the parent of this scripts/ folder.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# The finished workbook is the shared artifact the dashboard reads, so write it to
# the shared data folder — point GRANT_DATA_DIR at the Google Drive folder (the
# same folder the dashboard app reads from). Falls back to the Scraper_backend
# root when unset.
DATA_DIR = os.environ.get("GRANT_DATA_DIR", "").strip() or BASE_DIR

GEMINI_MODEL = "gemini-3.6-flash"

# Supports multiple Gemini API keys (e.g. from different Google accounts/projects)
# so a 429 on one key falls over to the next instead of stalling the whole run.
# Set GEMINI_API_KEYS="key1,key2" in .env, or fall back to a single GEMINI_API_KEY.
_raw_keys = os.environ.get("GEMINI_API_KEYS", "") or os.environ.get("GEMINI_API_KEY", "")
GEMINI_API_KEYS = [k.strip() for k in _raw_keys.split(",") if k.strip()]

if GEMINI_API_KEYS:
    masked = ", ".join(f"{k[:8]}..." for k in GEMINI_API_KEYS)
    print(f"[Config] Gemini keys: SET ({len(GEMINI_API_KEYS)} key(s): {masked})")
else:
    print("[Config] Gemini keys: MISSING")


class GeminiKeyRotator:
    """
    Thread-safe rotation across multiple Gemini API keys.
    All country threads share one rotator so they don't each independently
    guess which key is still usable.

    A 429 is treated as a TEMPORARY, time-based cooldown (RPM limits reset on
    a rolling ~60s window) rather than a permanent block for the rest of the
    run. Without this, one early burst of 429s would permanently disable
    every key for the remainder of a long multi-hour scrape.
    """

    COOLDOWN_SECONDS = 65  # slightly over the typical 60s RPM window

    def __init__(self, keys):
        self._keys = keys
        self._lock = threading.Lock()
        self._index = 0
        self._cooldown_until = {}  # index -> unix timestamp when it's usable again

    def _is_available(self, index):
        until = self._cooldown_until.get(index)
        return until is None or time.time() >= until

    def current(self):
        with self._lock:
            if not self._keys:
                return None, None
            # Prefer the current index if it's out of cooldown; otherwise
            # look for any key that's available right now.
            if self._is_available(self._index):
                return self._index, self._keys[self._index]
            for i in range(len(self._keys)):
                if self._is_available(i):
                    self._index = i
                    return i, self._keys[i]
            # Nothing available yet — return the key with the soonest cooldown
            # expiry so the caller can decide whether to wait or give up.
            soonest = min(self._cooldown_until, key=self._cooldown_until.get)
            return soonest, self._keys[soonest]

    def mark_exhausted(self, index):
        """Put a key on cooldown and advance to the next available key."""
        with self._lock:
            self._cooldown_until[index] = time.time() + self.COOLDOWN_SECONDS
            for i in range(len(self._keys)):
                if self._is_available(i):
                    self._index = i
                    return True
            return False  # every key is currently on cooldown

    def all_exhausted(self):
        with self._lock:
            return not any(self._is_available(i) for i in range(len(self._keys)))

    def seconds_until_next_available(self):
        with self._lock:
            if not self._cooldown_until:
                return 0
            return max(0, min(self._cooldown_until.values()) - time.time())

    def key_count(self):
        return len(self._keys)


gemini_rotator = GeminiKeyRotator(GEMINI_API_KEYS)

CURRENCY_TO_INR = {
    "GBP": 107,
    "USD": 83,
    "EUR": 90,
    "AUD": 54,
    "CAD": 61,
    "SGD": 62,
    "INR": 1,
}

r = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)

print("Flushing previous grant data from Redis...")
for key in r.scan_iter("grant:*"):
    r.delete(key)
print("Redis flushed.")

# ---------------------------------------------------------------------------
# Keywords / sectors
# ---------------------------------------------------------------------------

with open(os.path.join(BASE_DIR, "keywords.json"), "r") as fh:
    keywordsBySector = json.load(fh)  # e.g. {"health": [...], "defence": [...], "corporate": [...]}

# ---------------------------------------------------------------------------
# Adapter config
# ---------------------------------------------------------------------------

adapters = [
    # -----------------------------------------------------------------------
    # CFP / grant portals (India). Most have no per-call search box, so they run
    # in listMode: we scrape whatever calls the page lists right now and let the
    # semantic + LLM layers classify them by sector. extractionMode picks how
    # each call's details are read:
    #   navigate — click the row link into an HTML detail page, extract that page
    #   row      — extract the listing row's own text (link is a PDF / off-site)
    #   page     — the landing page itself is a single call
    # Search-box CFP portals (DBT, ANRF, Invest India — JS apps) are a follow-up.
    # -----------------------------------------------------------------------
    {
        # DST — Department of Science & Technology. Server-rendered Drupal table,
        # a handful of active calls, each linking to an HTML detail page.
        "url": "https://dst.gov.in/call-for-proposals/",
        "iframe": ["False"],
        "refreshMode": "dom",
        "listMode": True,
        "extractionMode": "navigate",
        "IdentifierForGrantList": [[1, "table.views-table"], "tbody tr"],
        "InitialGrantLinks": "td a",
        "ResultsIndicatorText": "___NO_RESULTS___",
        "Country": "India",
        "BackButton": [False],
    },
    {
        # India Science, Technology & Innovation (ISTI) — "Ongoing Call for
        # Proposals" Drupal table. Its visible search box is global site search,
        # not a call filter, so we list-scrape instead.
        "url": "https://www.indiascienceandtechnology.gov.in/latest-updates",
        "iframe": ["False"],
        "refreshMode": "dom",
        "listMode": True,
        "extractionMode": "navigate",
        "IdentifierForGrantList": [[1, "table.views-table"], "tbody tr"],
        "InitialGrantLinks": "td a",
        "ResultsIndicatorText": "___NO_RESULTS___",
        "Country": "India",
        "BackButton": [False],
    },
    {
        # BIRAC — the "#current" table lists active calls (this also covers Grand
        # Challenges India, which is one of these rows). Each links to a
        # cfp_view.php detail page.
        "url": "https://birac.nic.in/cfp.php",
        "iframe": ["False"],
        "refreshMode": "dom",
        "listMode": True,
        "extractionMode": "navigate",
        "IdentifierForGrantList": [[1, "table#current"], "tbody tr"],
        "InitialGrantLinks": "td a",
        "ResultsIndicatorText": "___NO_RESULTS___",
        "Country": "India",
        "BackButton": [False],
    },
    {
        # ICMR — table of calls where each row's link is a PDF ("Call document"),
        # so we can't navigate + read text. Row-mode extracts the row itself
        # (Title + Last Date columns) and keeps the PDF link as the grant URL.
        "url": "https://www.icmr.gov.in/call-for-proposals",
        "iframe": ["False"],
        "refreshMode": "dom",
        "listMode": True,
        "extractionMode": "row",
        "IdentifierForGrantList": [[1, "table.table-bordered"], "tbody tr"],
        "InitialGrantLinks": "td a",
        "ResultsIndicatorText": "___NO_RESULTS___",
        "Country": "India",
        "BackButton": [False],
    },
    {
        # DBT — Department of Biotechnology. JS app (Mantine) whose table lists
        # current calls; each row's link is a PDF, so row-mode extracts the row
        # (Title + Start/End dates) and keeps the PDF as the grant URL. The
        # language pop-up never blocks row-mode since we don't click into cards.
        "url": "https://dbt.gov.in/offerings/call-for-proposals",
        "iframe": ["False"],
        "refreshMode": "dom",
        "listMode": True,
        "extractionMode": "row",
        "IdentifierForGrantList": [[1, "table.mantine-Table-table"], "tbody tr"],
        "InitialGrantLinks": "a",
        "ResultsIndicatorText": "___NO_RESULTS___",
        "Country": "India",
        "BackButton": [False],
    },
    {
        # TDB — Technology Development Board. Renders a single current programme
        # as a detail table (not a list), so the landing page itself is one call.
        # NOTE: best-effort — revisit once verified against a live run.
        "url": "https://tdb.gov.in/call-for-proposal",
        "iframe": ["False"],
        "refreshMode": "dom",
        "listMode": True,
        "extractionMode": "page",
        "IdentifierForGrantList": "body",
        "InitialGrantLinks": "a",
        "ResultsIndicatorText": "___NO_RESULTS___",
        "Country": "India",
        "BackButton": [False],
    },
    {
        # ANRF — successor to SERB. The homepage IS the list (www.anrfonline.in
        # 302s here, so we use the canonical URL): the "PROPOSAL CALLS/
        # ANNOUNCEMENTS → ONGOING" card opens Bootstrap modal #modal3. The modal
        # is display:none until clicked, but its rows are server-rendered at load
        # and innerText falls back to textContent for non-rendered elements, so
        # row-mode reads them without opening it (wait_for uses state="attached").
        # TRAP: four elements share id="schemeTable" across the modals, so a
        # table-level selector grabs 10 rows — blending these 5 ongoing calls with
        # #modal2's 5 CLOSED ones. Scope to #modal3; #modal2 is closed calls and
        # #modal4 is upcoming. Header rows use <th>, so :has(td) drops them.
        # MUST stay "row": every row link carries target="_blank", so navigate
        # mode's wait_for_url_change would never fire on the original page.
        # Note ITS / Seminar-Symposia are year-round and carry no dates, so they
        # land as Undetermined status.
        "url": "https://anrfonline.in/ANRF/HomePage",
        "iframe": ["False"],
        "refreshMode": "dom",
        "listMode": True,
        "extractionMode": "row",
        "IdentifierForGrantList": [[1, "#modal3"], "tbody tr:has(td)"],
        "InitialGrantLinks": "td a",
        "ResultsIndicatorText": "___NO_RESULTS___",
        "Country": "India",
        "BackButton": [False],
    },

    # -----------------------------------------------------------------------
    # Tier-4 — philanthropic funders. These are global (UK/US/Canada), so their
    # calls carry non-INR budgets; CURRENCY_TO_INR already covers GBP/USD/CAD.
    # Most of the tier-4 list turned out to have no open-call list at all
    # (invitation-only funders) and is deliberately absent — see the exclusions
    # note under the adapters list.
    # -----------------------------------------------------------------------
    {
        # Wellcome Trust — server-rendered scheme cards. section.c-section[0] is
        # the 10 main schemes; [1] is "Other funding opportunities" and [2] a
        # newsletter block, so the index matters. The list mixes open and
        # "Closed to applications" schemes and carries that status as row text —
        # closed ones get an Expiry Date and fall out as Closed downstream.
        "url": "https://wellcome.org/research-funding/schemes",
        "iframe": ["False"],
        "refreshMode": "dom",
        "listMode": True,
        "extractionMode": "navigate",
        "IdentifierForGrantList": [[1, "section.c-section"], "article.c-text-card--scheme"],
        "InitialGrantLinks": "a",
        "ResultsIndicatorText": "___NO_RESULTS___",
        "dismissSelectors": [".c-cookie-message__button"],
        "Country": "United Kingdom",
        "BackButton": [False],
    },
    {
        # Gates Foundation — via Global Grand Challenges, which is where Gates
        # actually publishes open calls; gatesfoundation.org itself lists none
        # (it grants to organisations "identified by our staff").
        # Next.js app, so the card list needs networkidle. Its Emotion class
        # hashes (css-lo8kgy …) change on every build, so we anchor on the card
        # <article> nested inside the page-wrapper <article> instead.
        # Only currently-open calls are listed — often exactly one, sometimes
        # none between rounds, so a zero-row run here is expected, not a fault.
        # NOTE: the link selector must use double quotes — main.py wraps it in
        # single quotes when building the JS, so 'a[href*=\'/challenge/\']' would
        # compile to invalid JS and throw ReferenceError.
        "url": "https://gcgh.grandchallenges.org/grant-opportunities",
        "iframe": ["False"],
        "refreshMode": "dom",
        "listMode": True,
        "extractionMode": "navigate",
        "IdentifierForGrantList": [[1, "main"], "article article"],
        "InitialGrantLinks": 'a[href*="/challenge/"]',
        "ResultsIndicatorText": "___NO_RESULTS___",
        "dismissSelectors": ["#onetrust-accept-btn-handler"],
        "Country": "United States",
        "BackButton": [False],
    },
    {
        # Grand Challenges Canada — WordPress; /funding-opportunities/ 301s here
        # so we use the canonical URL. There is no list container: the handful of
        # opportunity cards sit among ~15 unrelated sibling blocks under
        # main#content, and is-style-corner-wrapper is what singles them out.
        # MUST stay extractionMode "navigate" — the cards are lazy-loaded and
        # their innerText is empty until scrolled into view, so "row" mode would
        # extract blank strings. The hrefs are in the DOM from the start.
        "url": "https://www.grandchallenges.ca/apply-for-funding/",
        "iframe": ["False"],
        "refreshMode": "dom",
        "listMode": True,
        "extractionMode": "navigate",
        "IdentifierForGrantList": [[1, "main#content"], "div.wp-block-group.is-style-corner-wrapper"],
        "InitialGrantLinks": "a.wp-block-button__link",
        "ResultsIndicatorText": "___NO_RESULTS___",
        "dismissSelectors": [".cky-btn-accept"],
        "Country": "Canada",
        "BackButton": [False],
    },
    {
        # Clinton Health Access Initiative — Resource Center filtered to the
        # "rfp" facet (the listing isn't linked from the main nav). Rows link to
        # HTML /rfp/ detail pages, so navigate-mode reads each one.
        # These are procurement RFPs/EOIs/tenders for health commodities rather
        # than research grants — kept because a device/commodity bid is a real
        # lead for an ODM, but the relevancy layer does the final call.
        # Match the container on its class: the id carries a Search-&-Filter-Pro
        # instance number (search-filter-results-8994) that changes on rebuild.
        "url": "https://www.clintonhealthaccess.org/resource-center/?_sft_category=rfp&sort_order=date+desc",
        "iframe": ["False"],
        "refreshMode": "dom",
        "listMode": True,
        "extractionMode": "navigate",
        "IdentifierForGrantList": [[1, "div.search-filter-results"], "article.post"],
        "InitialGrantLinks": "h2 a",
        "ResultsIndicatorText": "___NO_RESULTS___",
        "Country": "Global",
        "BackButton": [False],
    },

    # -----------------------------------------------------------------------
    # Tier-5 — corporate foundations. Only HCL publishes an open-call list;
    # Infosys and Reliance are absent for the reasons in the exclusions note.
    # -----------------------------------------------------------------------
    {
        # HCL Foundation — "Open Vacancies/ RFPs/ TORs" Drupal table, narrowed
        # via the site's own exposed filter so job vacancies are excluded.
        # Every row's link is a PDF/DOCX download, so row-mode extracts the row
        # itself (Title + Last Date + Location) and keeps the document as the
        # grant URL — the same shape as ICMR/DBT above.
        # The table is an archive that mixes live and expired RFPs and doesn't
        # self-prune, so expect Closed rows; only page 0 (10 newest) is read.
        "url": "https://www.hclfoundation.org/work-with-us?field_type_value=RFPs%2FTORs",
        "iframe": ["False"],
        "refreshMode": "dom",
        "listMode": True,
        "extractionMode": "row",
        "IdentifierForGrantList": [[1, "table.views-table"], "tbody tr"],
        "InitialGrantLinks": "td a",
        "ResultsIndicatorText": "___NO_RESULTS___",
        "dismissSelectors": ["button.cookiesjsr-btn.allowAll"],
        "Country": "India",
        "BackButton": [False],
    },
]

# Wipro Foundation — one page per programme domain; there is no combined list.
# Cards are server-rendered and link to HTML programme detail pages. The
# container id differs per domain (#programs-container / #health-height /
# #ecology-height / #disaster-height), so we key off the shared class, which is
# the only selector that works across all four.
# Caveat: these are evergreen programme descriptions, not dated calls — Wipro
# announces open windows by email, and typically only one card at a time carries
# an "Apply now" link, so most rows land as Undetermined-status noise.
# The Education page's "Higher Education" tab is AJAX-swapped and isn't captured;
# the default School Education set holds the live call.
adapters += [
    {
        "url": f"https://www.wiprofoundation.org/our-initiatives/{domain}/",
        "iframe": ["False"],
        "refreshMode": "dom",
        "listMode": True,
        "extractionMode": "navigate",
        "IdentifierForGrantList": [[1, ".education-programs-list"], ".education-program.card"],
        "InitialGrantLinks": "p.read_more a",
        "ResultsIndicatorText": "___NO_RESULTS___",
        "Country": "India",
        "BackButton": [False],
    }
    for domain in ("education", "healthcare", "ecology", "disaster-response")
]

# Verified as having NO scrapable open-call list (probed live, all returned 200
# unless noted) — kept here so they aren't re-investigated from the CFP sheet:
#   Hilton, Rockefeller, CIFF, Skoll, Azim Premji, Tata Trusts,
#   Nilekani/EkStep, Reliance — invitation-only or no published calls.
#   Infosys Foundation — Akamai 403s the whole domain to datacenter IPs.
#   Invest India (investindia.gov.in/request-for-proposal) — the page ships an
#     empty Quicktabs container ("quicktabs":{"qt_tenders":{"tabs":[]}}) from the
#     server, so the list renders zero rows for browser and curl alike; the data
#     is only reachable via a POST /views/ajax that the adapter shape can't
#     express. Not worth chasing: the content is Invest India's own IT/marketing
#     procurement, and the "current" block held 6 stale rows (newest deadline
#     14 May 2026, several already carrying Final Result / Cancellation notices)
#     against 20 archived. Its cookie accept button is button.agree-button.
# Hilton's /grants/search/ and Rockefeller's /our-grants/ look like call lists
# but are databases of ALREADY-AWARDED grants — do not point adapters at them.
# Rockefeller's /rfps/ is a real, structurally sound RFP archive (currently 0
# open) but carries vendor/consulting contracts rather than grants; revisit only
# if procurement services come into scope.


EXTRACTION_PROMPT = """You are a specialist in government, research-council, and philanthropic
Call-for-Proposals (CFP) and grant notices (primarily Indian), extracting structured data for Sensio, an Indian health wearables
company operating as a full-stack ODM (smart rings, chest patches, smart bands, and
smart glasses with biosensors for ECG, PPG, SpO2, sleep, and related physiological
monitoring). You will receive the raw HTML (or rendered text) of a single grant notice
page. Extract the fields below and return ONLY a valid JSON object — no markdown fences,
no explanation, nothing else.

Extract the fields factually and completely regardless of how relevant the grant turns
out to be — do not filter, score, or omit a grant based on fit. Relevance scoring happens
in a separate downstream step; your job here is accurate extraction only.

Required fields (use null if not found):
{{
  "Grant Title": "string",
  "Grant Description": "string",
  "Organisation Name": "string",
  "Original Currency": "ISO 4217 code, e.g. GBP, EUR, SGD, AUD, INR",
  "Budget Min": number or null,
  "Budget Max": number or null,
  "Opening Date": "DD Month YYYY or 'not available'",
  "Closing Date": "DD Month YYYY or 'not available'",
  "Grant Status": "Open | Closed | Awarded | Unknown",
  "Award Date": "DD Month YYYY or 'not available'",
  "Country": "string"
}}

Rules:
1. Opening Date
- First look for an explicit grant opening/publication date.
- If an "Opening Date" field is not present, use the date shown next to or after "Published"
  or an equivalent label in the notice's own language/format.
  Examples:
    - Published 16 June 2026
    - Published: 16 June 2026
    - Launch Date: 23 July 2025
    - Date of announcement / advertisement
- The "Published" date should be treated as the Opening Date.
- If neither exists, return "not available".

2. Closing Date
- Extract the grant submission deadline.
- Common labels include (this varies by country/portal, match by meaning, not exact wording):
  - Closing date
  - Closing time
  - Deadline
  - Submission deadline
  - Response deadline
  - Date offers to be received
  - Grant end date / bid end date
  - Last date / Last date for submission
  - Deadline for submission of proposals / applications
- Return only the date in "DD Month YYYY" format.
- If no closing date exists, return "not available".

3. For Budget Min / Budget Max: if only one value is given, set both to that value.
  If a range is given (e.g. lowest lot to highest lot), use those as min and max.

4. For Original Currency: infer from context (currency symbols, country of the notice,
  or explicit currency codes) and always return the ISO 4217 code, not a symbol or name.
  If the notice is from an Indian government body, research council, ministry, or
  foundation and no currency is stated, default Original Currency to "INR" and Country to "India".

5. For Grant Status: derive from whether a submission deadline exists and whether
  it has passed relative to today ({today}).

6. Strip any boilerplate cookie banners, navigation menus, and footer text —
  focus only on the notice content itself.

7. Organisation Name is the agency, department, ministry, research council, or
  foundation issuing the call (e.g. DST, BIRAC, ICMR, ANRF, TDB).

8. Return ONLY the JSON object.

Page content:
{page_content}
"""


def extract_fields_with_gemini(page_html: str) -> dict:
    """
    Send the page content to Gemini and get structured grant fields back.
    Returns a dict with the extracted fields, or an empty dict on failure.

    On a 429 (rate limit) from the currently active key, rotates to the next
    available Gemini key (if configured) and retries the SAME request rather
    than dropping the grant. Only gives up once every configured key has
    been rate-limited.
    """
    today_str = date.today().strftime("%d %B %Y")
    prompt = EXTRACTION_PROMPT.format(
        today=today_str,
        page_content=page_html[:40000],
    )

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        # thinkingConfig (used to force thinkingBudget=0 on gemini-2.5-flash)
        # is rejected outright by gemini-3.6-flash -- 400 INVALID_ARGUMENT,
        # confirmed by direct testing. That model always thinks; dropping the
        # block entirely (verified working) is the only way to call it here.
        "generationConfig": {
            "maxOutputTokens": 2048,
            "temperature": 0,
        },
    }

    attempts = max(gemini_rotator.key_count(), 1) + 1  # +1 for the cooldown-wait retry

    for _ in range(attempts):
        key_index, api_key = gemini_rotator.current()
        if api_key is None:
            print("[Gemini] No API keys configured — falling back to Claude.")
            return extract_with_claude(prompt)

        try:
            resp = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={api_key}",
                headers={"Content-Type": "application/json"},
                json=payload,
                timeout=60,
            )

            if resp.status_code == 429:
                print(f"[Gemini] Key #{key_index} ({api_key[:8]}...) rate-limited (429).")
                still_have_keys = gemini_rotator.mark_exhausted(key_index)
                if still_have_keys:
                    print(f"[Gemini] Switching to next key and retrying same request...")
                    continue  # retry the same page with the next key
                else:
                    # Every key is 429ing. If Claude is available, prefer it over
                    # sleeping — a daily-quota exhaustion never clears within the
                    # cooldown window, so waiting would just stall the whole run.
                    if claude_available():
                        print("[Gemini] All keys rate-limited — falling back to Claude.")
                        return extract_with_claude(prompt)
                    wait_s = min(gemini_rotator.seconds_until_next_available(), 65)
                    if wait_s > 0:
                        print(f"[Gemini] All keys on cooldown — waiting {wait_s:.0f}s "
                              f"for RPM window to reset rather than dropping this grant.")
                        time.sleep(wait_s)
                        continue  # one more attempt now that cooldown should have cleared
                    print("[Gemini] All configured keys are rate-limited.")
                    return {}

            resp.raise_for_status()
            data = resp.json()

            raw_text = ""
            for candidate in data.get("candidates", []):
                for part in candidate.get("content", {}).get("parts", []):
                    if "text" in part:
                        raw_text += part["text"]

            print(f"[Gemini RAW RESPONSE]:\n{repr(raw_text[:500])}")

            return parse_llm_json(raw_text, "Gemini")

        except Exception as exc:
            print(f"[Gemini extraction error] {exc}")
            return extract_with_claude(prompt)

    # Every attempt was consumed by rotation/cooldown without a result.
    return extract_with_claude(prompt)


# SECTION 2 — POST-PROCESSING

def resolve_currency_and_budget(extracted: dict) -> tuple:
    currency = (extracted.get("Original Currency") or "").upper().strip() or None
    budget_min = extracted.get("Budget Min")
    budget_max = extracted.get("Budget Max")
    inr_min = None
    inr_max = None
    if currency and currency in CURRENCY_TO_INR:
        rate = CURRENCY_TO_INR[currency]
        if budget_min is not None:
            inr_min = int(budget_min * rate)
        if budget_max is not None:
            inr_max = int(budget_max * rate)
    return currency, budget_min, budget_max, inr_min, inr_max


def resolve_sector(keyword: str) -> str:
    for sector, words in keywordsBySector.items():
        if keyword in words:
            return sector.capitalize()
    return "Unknown"


def resolve_keywords(title: str, description: str) -> list:
    combined = f"{title or ''} {description or ''}".lower()
    matched = []
    for words in keywordsBySector.values():
        for word in words:
            if word.lower() in combined and word not in matched:
                matched.append(word)
    return matched


def compute_timeline(opening_date_str: Optional[str], closing_date_str: Optional[str]) -> Optional[int]:
    if not opening_date_str or not closing_date_str:
        return None
    for fmt in ("%d %B %Y", "%d %b %Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            o = datetime.strptime(opening_date_str.strip(), fmt)
            c = datetime.strptime(closing_date_str.strip(), fmt)
            return (c - o).days
        except ValueError:
            continue
    return None


# def build_grant_object(extracted: dict, grant_url: str, search_keyword: str, country: str) -> dict:
#     currency, budget_min, budget_max, inr_min, inr_max = resolve_currency_and_budget(extracted)
#     sector = resolve_sector(search_keyword)
#     title = extracted.get("Grant Title") or ""
#     description = extracted.get("Grant Description") or ""
#     matched_keywords = resolve_keywords(title, description)
#     opening_date = extracted.get("Opening Date")
#     closing_date = extracted.get("Closing Date")
#     timeline = compute_timeline(opening_date, closing_date)
#     # Override Grant Status based on closing date — don't rely on Gemini for this
#     computed_status = extracted.get("Grant Status") or "Unknown"
#     if closing_date:
#         for fmt in ("%d %B %Y", "%d %b %Y", "%d/%m/%Y", "%Y-%m-%d"):
#             try:
#                 closing_dt = datetime.strptime(closing_date.strip(), fmt).date()
#                 computed_status = "Open" if closing_dt >= date.today() else "Closed"
#                 break
#             except ValueError:
#                 continue
#     primary_key = hashlib.md5(
#         (title + (extracted.get("Organisation Name") or "")).encode()
#     ).hexdigest()

#     return {
#         "Primary Key": primary_key,
#         "Grant Title": title or None,
#         "Grant Description": description or None,
#         "Organisation Name": extracted.get("Organisation Name"),
#         # "Grant Status": extracted.get("Grant Status"),
#         "Grant Status": computed_status,
#         "Award Date": extracted.get("Award Date"),
#         "Country": extracted.get("Country") or country,
#         "Sector": sector,
#         "Budget Currency": currency,
#         "Budget in Local Currency Minimum": budget_min,
#         "Budget in Local Currency Maximum": budget_max,
#         "Budget in INR Minimum": inr_min,
#         "Budget in INR Maximum": inr_max,
#         "Opening Date": opening_date,
#         "Expiry Date": closing_date,
#         "Timeline": timeline,
#         "Link to the Grant": grant_url,
#         "Keywords": matched_keywords,
#         "Keyword included": search_keyword,
#         "Order Quantity": None,
#         "Special Observation": None,
#         "Eligibility": None,
#         "Application Status": "Not Applied",
#         "Current Applicants": None,
#     }

def build_grant_object(extracted: dict, grant_url: str, search_keyword: str, country: str, category: str = None) -> dict:
    currency, budget_min, budget_max, inr_min, inr_max = resolve_currency_and_budget(extracted)

    # listMode adapters have no search keyword to resolve a sector from
    # (search_keyword is "") -- runMainLogic already knows the right sector
    # (the adapter's configured Sector, or the keyword-search sector for the
    # non-listMode path) and passes it in as `category`. Prefer that; fall
    # back to keyword-derived resolution only when it's not available.
    sector = category.capitalize() if category and category != "Unknown" else resolve_sector(search_keyword)

    title = extracted.get("Grant Title") or ""
    description = extracted.get("Grant Description") or ""

    matched_keywords = resolve_keywords(title, description)

    opening_date = extracted.get("Opening Date")
    closing_date = extracted.get("Closing Date")

    timeline = compute_timeline(opening_date, closing_date)

    # Compute Grant Status ourselves (don't rely on Gemini)
    computed_status = "Undetermined"

    if (
        closing_date
        and str(closing_date).strip()
        and str(closing_date).lower() != "not available"
    ):
        for fmt in ("%d %B %Y", "%d %b %Y", "%d/%m/%Y", "%Y-%m-%d"):
            try:
                closing_dt = datetime.strptime(closing_date.strip(), fmt).date()
                computed_status = (
                    "Open" if closing_dt >= date.today() else "Closed"
                )
                break
            except ValueError:
                continue

    primary_key = hashlib.md5(
        (title + (extracted.get("Organisation Name") or "")).encode()
    ).hexdigest()

    return {
        "Primary Key": primary_key,
        "Grant Title": title or None,
        "Grant Description": description or None,
        "Organisation Name": extracted.get("Organisation Name"),
        "Grant Status": computed_status,
        "Award Date": extracted.get("Award Date"),
        "Country": extracted.get("Country") or country,
        "Sector": sector,
        "Budget Currency": currency,
        "Budget in Local Currency Minimum": budget_min,
        "Budget in Local Currency Maximum": budget_max,
        "Budget in INR Minimum": inr_min,
        "Budget in INR Maximum": inr_max,
        "Opening Date": opening_date,
        "Expiry Date": closing_date,
        "Timeline": timeline,
        "Link to the Grant": grant_url,
        "Keywords": matched_keywords,
        "Keyword included": search_keyword,
        "Order Quantity": None,
        "Special Observation": None,
        "Eligibility": None,
        "Application Status": "Not Applied",
        "Current Applicants": None,
    }


def store_grant(grant_object: dict) -> None:
    primary_key = grant_object["Primary Key"]
    redis_key = f"grant:{primary_key}"
    cached = r.get(redis_key)
    if cached:
        cached_obj = json.loads(cached)
        existing_keywords = set(cached_obj.get("Keywords", []))
        new_keywords = set(grant_object.get("Keywords", []))
        grant_object["Keywords"] = list(existing_keywords | new_keywords)
        r.set(redis_key, json.dumps(grant_object))
        print(f"Updated existing grant -> {primary_key}")
    else:
        r.set(redis_key, json.dumps(grant_object))
        print(json.dumps(grant_object, indent=4))
        print("---------------------------------------")


# ===========================================================================
# SECTION 3 — PLAYWRIGHT NAVIGATION
# ===========================================================================

def get_iframe_query_string(returnContentDocument, adapter):
    selectorStr = "document"
    if adapter["iframe"][0] == "True":
        if isinstance(adapter["iframe"][1], list):
            for level in adapter["iframe"][1]:
                if isinstance(level, list):
                    selectorStr += f"?.querySelectorAll('{level[1]}')[{int(level[0]) - 1}]"
                else:
                    selectorStr += f"?.querySelector('{level}')"
            return f"{selectorStr}?.contentDocument" if returnContentDocument else selectorStr
        else:
            selectorStr += f"?.querySelector('{adapter['iframe']}')"
            return f"{selectorStr}?.contentDocument" if returnContentDocument else selectorStr
    return selectorStr


def getElementQueryString(elementKey, adapter):
    selectorStr = get_iframe_query_string(True, adapter) if adapter["iframe"][0] == "True" else "document"
    if elementKey == "BackButton":
        if isinstance(adapter[elementKey][1], list):
            for level in adapter[elementKey][1]:
                if isinstance(level, list):
                    selectorStr += f"?.querySelectorAll('{level[1]}')[{int(level[0]) - 1}]"
                else:
                    selectorStr += f"?.querySelector('{level}')"
            return selectorStr
        else:
            selectorStr += f"?.querySelector('{adapter[elementKey]}')"
            return selectorStr
    config = adapter.get(elementKey)
    if config is None:
        return "null"
    if isinstance(config, list):
        for level in config:
            if isinstance(level, list):
                selectorStr += f"?.querySelectorAll('{level[1]}')[{int(level[0]) - 1}]"
            else:
                selectorStr += f"?.querySelector('{level}')"
    elif isinstance(config, str):
        selectorStr += f"?.querySelector('{config}')"
    return selectorStr


def getElementQueryStringForListItems(adapter):
    selectorStr = get_iframe_query_string(True, adapter) if adapter["iframe"][0] == "True" else "document"
    if isinstance(adapter["IdentifierForGrantList"], list):
        for level in adapter["IdentifierForGrantList"]:
            if isinstance(level, list):
                selectorStr += f"?.querySelectorAll('{level[1]}')[{int(level[0]) - 1}]"
            else:
                selectorStr += f"?.querySelectorAll('{level}')"
    else:
        selectorStr += f"?.querySelectorAll('{adapter['IdentifierForGrantList']}')"
    return selectorStr


def get_leaf_selector(selector_value):
    if isinstance(selector_value, str):
        return f"?.querySelector('{selector_value}')"
    if isinstance(selector_value, list):
        selectorStr = ""
        for level in selector_value:
            if isinstance(level, list):
                if not isinstance(level[0], int):
                    selectorStr += f"?.querySelectorAll('{level[1]}')"
                    selectorStr += f"[listItem{selectorStr}{level[0]}]"
                else:
                    selectorStr += f"?.querySelectorAll('{level[1]}')[{int(level[0]) - 1}]"
            else:
                selectorStr += f"?.querySelectorAll('{level}')"
        return selectorStr
    return str(selector_value)


def install_custom_selector_loop(page, adapter):
    script = f"""
        () => {{
            window.__assignGrantCustomSelectors = () => {{
                let iterator = 0;
                let parent = document;

                if ("{adapter["iframe"][0]}" == "True") {{
                    parent = {get_iframe_query_string(True, adapter)};
                    if (!parent) return;
                }}

                const grantCards = {getElementQueryStringForListItems(adapter)};

                grantCards?.forEach((listItem) => {{
                    listItem.classList.add(`custom-grantList-${{iterator}}`);

                    const InitialGrantLink = listItem{get_leaf_selector(adapter["InitialGrantLinks"])};
                    if (InitialGrantLink) {{
                        InitialGrantLink.classList.add(`custom-InitialGrantLinks-${{iterator}}-0`);
                    }}

                    iterator += 1;
                }});

                const NextPageButton = {getElementQueryString("NextPageButton", adapter)};
                if (NextPageButton) NextPageButton.classList.add("custom-NextPageButton");

                const keywordSearchElement = {getElementQueryString("keywordSearchBox", adapter)};
                if (keywordSearchElement) keywordSearchElement.classList.add("custom-keywordSearchElement");

                const submitButtonElement = {getElementQueryString("submitButton", adapter)};
                if (submitButtonElement) submitButtonElement.classList.add("custom-submitButtonElement");
            }};

            window.__assignGrantCustomSelectors();

            if (window.__customSelectorInterval) clearInterval(window.__customSelectorInterval);
            window.__customSelectorInterval = setInterval(() => {{ window.__assignGrantCustomSelectors(); }}, 250);
        }}
    """
    page.evaluate(script)


def wait_for_js_visible(page, js_selector, timeout=60000, poll_interval=0.25):
    start = time.time()
    while True:
        try:
            is_visible = page.evaluate(f"""
                () => {{
                    const element = {js_selector};
                    if (!element) return false;
                    const style = window.getComputedStyle(element);
                    const rect = element.getBoundingClientRect();
                    return (
                        style.display !== "none" &&
                        style.visibility !== "hidden" &&
                        style.opacity !== "0" &&
                        rect.width > 0 &&
                        rect.height > 0
                    );
                }}
            """)
        except Exception as e:
            if "Execution context was destroyed" in str(e) or "navigation" in str(e).lower():
                if (time.time() - start) * 1000 > timeout:
                    raise PlaywrightTimeoutError(f"Timeout: {js_selector}")
                time.sleep(poll_interval)
                continue
            raise
        if is_visible:
            return True
        if (time.time() - start) * 1000 > timeout:
            raise PlaywrightTimeoutError(f"Timeout: {js_selector}")
        time.sleep(poll_interval)


def wait_for_url_change(page, old_url, timeout=30000):
    page.wait_for_function("oldUrl => window.location.href !== oldUrl", arg=old_url, timeout=timeout)


def get_results_signature(page, adapter):
    try:
        return page.evaluate(f"""
            () => {{
                let doc = document;
                if ("{adapter["iframe"][0]}" == "True") {{
                    doc = {get_iframe_query_string(True, adapter)};
                    if (!doc || !doc.body) return "NO_DOC";
                }}
                const bodyText = doc.body?.innerText || "";
                const noResultsPresent = bodyText.includes({json.dumps(adapter["ResultsIndicatorText"])});
                const firstCard = {getElementQueryStringForListItems(adapter)};
                const firstCardText = firstCard ? firstCard.innerText.trim() : "";
                return JSON.stringify({{
                    url: window.location.href,
                    noResultsPresent,
                    firstCardText,
                    bodyLength: bodyText.length
                }});
            }}
        """)
    except Exception as e:
        return f"ERROR:{str(e)}"


def wait_for_results_signature_change_or_stability(page, adapter, before_signature, timeout=60000, poll_interval=0.5, stable_for=1.5):
    start = time.time()
    last_signature = None
    stable_since = None
    while True:
        current_signature = get_results_signature(page, adapter)
        if current_signature != before_signature:
            if current_signature == last_signature:
                if stable_since and time.time() - stable_since >= stable_for:
                    return current_signature
            else:
                last_signature = current_signature
                stable_since = time.time()
        if (time.time() - start) * 1000 > timeout:
            return current_signature
        time.sleep(poll_interval)


def wait_for_grant_results_refresh(page, adapter, old_first_card_text=None):
    try:
        return page.evaluate(f"""
            (args) => {{
                let doc = document;
                if ("{adapter["iframe"][0]}" == "True") {{
                    doc = {get_iframe_query_string(True, adapter)};
                    if (!doc || !doc.body) return [false, false, "iframe not ready"];
                }}
                const bodyText = doc.body?.innerText || "";
                if (bodyText.includes(args.noResultsText)) return [true, true, "no results"];
                const firstCard = {getElementQueryStringForListItems(adapter)};
                if (!firstCard) return [false, false, "no first card"];
                return [true, false, "results loaded"];
            }}
        """, {"oldText": old_first_card_text, "noResultsText": adapter["ResultsIndicatorText"]})
    except Exception as e:
        return [False, False, str(e)]


def click_and_wait_for_refresh(page, submit_button, adapter, timeout=60000):
    refresh_mode = adapter.get("refreshMode", "dom")
    if refresh_mode == "navigation":
        old_url = page.url
        navigated = False
        try:
            with page.expect_navigation(wait_until="domcontentloaded", timeout=timeout):
                submit_button.click()
            navigated = True
        except Exception as e:
            print(f"[{adapter.get('Country', '?')}] expect_navigation did not fire cleanly ({e}); checking URL directly...")

        if not navigated:
            # Don't blindly re-click (risk of double submission). Instead, poll
            # whether the URL actually changed on its own within a short window.
            try:
                page.wait_for_function(
                    "oldUrl => window.location.href !== oldUrl",
                    arg=old_url,
                    timeout=15000,
                )
                navigated = True
            except Exception:
                print(f"[{adapter.get('Country', '?')}] URL did not change after submit "
                      f"(still on {page.url}). Search likely did not go through.")

        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass

        if navigated and page.url == old_url:
            print(f"[{adapter.get('Country', '?')}] Warning: navigated but URL is unchanged "
                  f"({page.url}) — search params may not have been applied.")
    else:
        before_signature = get_results_signature(page, adapter)
        submit_button.click()
        wait_for_results_signature_change_or_stability(page, adapter, before_signature=before_signature, timeout=timeout)


# ===========================================================================
# SECTION 4 — MAIN SCRAPE LOOP
# ===========================================================================

def process_grant_page(page, adapter, search_keyword, country, category=None):
    grant_url = page.url
    try:
        page_content = page.evaluate("() => document.body.innerText")
    except Exception:
        page_content = ""

    if not page_content.strip():
        print(f"Empty page content for {grant_url}, skipping.")
        return

    extracted = extract_fields_with_gemini(page_content)

    if not extracted:
        print(f"Gemini returned no data for {grant_url}, skipping.")
        return

    grant_obj = build_grant_object(
        extracted=extracted,
        grant_url=grant_url,
        search_keyword=search_keyword,
        country=country,
        category=category,
    )
    store_grant(grant_obj)


def process_grant_row(card, page, adapter, search_keyword, country, category=None):
    """
    Row-mode extraction: read the listing row's own text (title + deadline etc.)
    and send it to Gemini — no navigation. Used when the row's link points to a
    PDF or an off-site page (e.g. ICMR call documents). The row's link, if any,
    is kept as the grant URL so founders can open the original call.
    """
    try:
        content = card.inner_text()
    except Exception:
        content = ""

    if not content.strip():
        return

    grant_url = page.url
    try:
        href = card.locator('[class*="custom-InitialGrantLinks-"]').first.get_attribute("href")
        if href:
            grant_url = urljoin(page.url, href)
    except Exception:
        pass

    extracted = extract_fields_with_gemini(content)
    if not extracted:
        return

    grant_obj = build_grant_object(
        extracted=extracted,
        grant_url=grant_url,
        search_keyword=search_keyword,
        country=country,
        category=category,
    )
    store_grant(grant_obj)


def process_grant_card_in_place(card, page, adapter, search_keyword, country, category=None):
    """
    For sites like UAE where grants expand inline (no page navigation).
    Clicks the card header to expand it, reads innerText, sends to Gemini,
    then collapses the card again.
    """
    grant_url = page.url  # stays the same — no navigation

    # Click the header to expand the card body
    try:
        header = card.locator(".ex-grant-head").first
        header.click()
        # Wait for the body to appear
        card.locator(".ex-grant-body").wait_for(state="visible", timeout=10000)
    except Exception as e:
        print(f"[UAE] Could not expand card: {e}")
        return

    # Grab full card text (head + body)
    try:
        page_content = card.inner_text()
    except Exception:
        page_content = ""

    if not page_content.strip():
        print("[UAE] Empty card content, skipping.")
        return

    extracted = extract_fields_with_gemini(page_content)

    if not extracted:
        print("[UAE] Gemini returned no data, skipping.")
        return

    # Use the grant's own detail URL if available inside the card
    try:
        detail_link = card.locator("a.button--primary").first.get_attribute("href")
        if detail_link:
            grant_url = detail_link
    except Exception:
        pass

    grant_obj = build_grant_object(
        extracted=extracted,
        grant_url=grant_url,
        search_keyword=search_keyword,
        country=country,
        category=category,
    )
    store_grant(grant_obj)

    # Collapse the card again so the next click works cleanly
    try:
        header = card.locator(".ex-grant-head").first
        header.click()
        card.locator(".ex-grant-body").wait_for(state="hidden", timeout=5000)
    except Exception:
        pass  # collapsing is best-effort


def runMainLogic(page, parent, keyword, category, adapter, timer=1):
    install_custom_selector_loop(page, adapter)

    try:
        lists = parent.locator('[class*="custom-grantList-"]')
        lists.first.wait_for(state="attached", timeout=60000)
    except Exception:
        return

    grant_count = lists.count()
    extraction_mode = adapter.get("extractionMode", "navigate")
    expand_in_place = adapter.get("expandInPlace", False) or extraction_mode == "inplace"

    for listNumber in range(grant_count):
        install_custom_selector_loop(page, adapter)
        card = lists.nth(listNumber)

        if extraction_mode == "row":
            # Row-mode: the listing row itself carries the call info (title,
            # deadline). No navigation — used when the detail link is a PDF or
            # off-site (e.g. ICMR call documents).
            try:
                process_grant_row(
                    card, page, adapter,
                    search_keyword=keyword,
                    country=adapter["Country"],
                    category=category,
                )
            except Exception as e:
                print(f"Error processing row card: {e}")
            continue

        if expand_in_place:
            # ---------------------------------------------------------------
            # UAE-style: expand card inline, extract, collapse — no navigation
            # ---------------------------------------------------------------
            try:
                process_grant_card_in_place(
                    card, page, adapter,
                    search_keyword=keyword,
                    country=adapter["Country"],
                    category=category,
                )
            except Exception as e:
                print(f"Error processing inline card: {e}")
            continue  # no back-navigation needed

        # -------------------------------------------------------------------
        # Standard: navigate into grant page, extract, go back
        # -------------------------------------------------------------------
        element = card.locator('[class*="custom-InitialGrantLinks-"]').first
        old_url = page.evaluate("() => window.location.href")
        before_signature = get_results_signature(page, adapter)

        try:
            element.click()
            wait_for_url_change(page, old_url)
            wait_for_results_signature_change_or_stability(page, adapter, before_signature=before_signature)
            page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception as e:
            print(f"Navigation error: {e}. Skipping.")
            continue

        try:
            process_grant_page(page, adapter, search_keyword=keyword, country=adapter["Country"], category=category)
        except Exception as e:
            print(f"Error processing grant page: {e}")

        # Go back to results
        before_signature = get_results_signature(page, adapter)
        if adapter["BackButton"][0]:
            try:
                back_btn = page.evaluate_handle(
                    f"() => {getElementQueryString('BackButton', adapter)}"
                ).as_element()
                if back_btn:
                    back_btn.click()
            except Exception:
                page.go_back(wait_until="domcontentloaded", timeout=60000)
        else:
            page.go_back(wait_until="domcontentloaded", timeout=60000)

        wait_for_results_signature_change_or_stability(page, adapter, before_signature=before_signature)

        for state in ("domcontentloaded", "networkidle"):
            try:
                page.wait_for_load_state(state, timeout=10000)
            except Exception:
                pass

        if adapter.get("keywordSearchBox"):
            wait_for_js_visible(page, getElementQueryString("keywordSearchBox", adapter))
        install_custom_selector_loop(page, adapter)

    # Pagination — skip for adapters with no Next button (list-mode CFP pages
    # show every current call on a single page).
    if not adapter.get("NextPageButton"):
        return
    before_signature = get_results_signature(page, adapter)
    try:
        nextPageButton = parent.locator(".custom-NextPageButton")
        nextPageButton.first.wait_for(state="attached")
        if nextPageButton.is_disabled():
            return
        old_first_text = parent.locator('[class*="custom-grantList-"]').first.inner_text(timeout=3000)
        nextPageButton.click()
        wait_for_results_signature_change_or_stability(page, adapter, before_signature=before_signature)
        wait_for_grant_results_refresh(page=page, old_first_card_text=old_first_text, adapter=adapter)
        runMainLogic(page, parent, keyword, category, adapter, timer=2)
    except Exception:
        return


# def scrape_site(adapter):
#     with Stealth().use_sync(sync_playwright()) as p:
#         browser = p.chromium.launch(headless=True)
#         page = browser.new_page()
#         page.goto(adapter["url"], timeout=1200000)
#         parent = page

#         for sector, words in keywordsBySector.items():
#             for keyword in words:
#                 for state in ("domcontentloaded", "networkidle"):
#                     try:
#                         page.wait_for_load_state(state, timeout=10000)
#                     except Exception:
#                         pass
def scrape_site(adapter, stop_event):
    with Stealth().use_sync(sync_playwright()) as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(adapter["url"], timeout=1200000)
        parent = page

        # Best-effort dismissal of cookie / language pop-ups before scraping.
        for sel in adapter.get("dismissSelectors", []):
            try:
                page.locator(sel).first.click(timeout=4000)
            except Exception:
                pass

        # ------------------------------------------------------------------
        # LIST MODE — CFP portals with no per-call search box. We don't type
        # keywords; we scrape whatever calls the page lists right now and let
        # the semantic + LLM layers classify them by sector.
        # ------------------------------------------------------------------
        if adapter.get("listMode"):
            if stop_event.is_set():
                browser.close()
                return

            for state in ("domcontentloaded", "networkidle"):
                try:
                    page.wait_for_load_state(state, timeout=10000)
                except Exception:
                    pass

            mode = adapter.get("extractionMode", "navigate")

            if mode == "page":
                # The landing page IS a single call for proposals.
                print(f"[{adapter['Country']}] listMode(page) url={page.url}")
                process_grant_page(
                    page, adapter, search_keyword="", country=adapter["Country"],
                    category=adapter.get("Sector", "Unknown"),
                )
                browser.close()
                return

            install_custom_selector_loop(page, adapter)
            try:
                page.locator('[class*="custom-grantList-"]').first.wait_for(state="attached", timeout=60000)
            except Exception:
                print(f"[{adapter['Country']}] listMode: no calls found at {page.url}")
                browser.close()
                return

            try:
                found = page.locator('[class*="custom-grantList-"]').count()
            except Exception:
                found = "?"
            print(f"[{adapter['Country']}] listMode({mode}) url={page.url} calls_found={found}")

            runMainLogic(
                page, parent,
                keyword="",
                category=adapter.get("Sector", "Unknown"),
                adapter=adapter,
            )
            browser.close()
            return

        for sector, words in keywordsBySector.items():
            for keyword in words:
                if stop_event.is_set():
                    print(f"[{adapter['Country']}] Stop signal received, exiting.")
                    browser.close()
                    return
                # ... rest of keyword loop unchanged
                install_custom_selector_loop(page, adapter)

                try:
                    search_box = parent.locator(".custom-keywordSearchElement")
                    search_box.first.wait_for(state="visible", timeout=60000)
                    search_box.fill(keyword)
                except PlaywrightTimeoutError:
                    page.go_back()
                    search_box = parent.locator(".custom-keywordSearchElement")
                    search_box.first.wait_for(state="visible", timeout=60000)
                    search_box.fill(keyword)

                try:
                    old_first_text = parent.locator('[class*="custom-grantList-"]').first.inner_text(timeout=3000)
                except Exception:
                    old_first_text = None

                submit_btn = parent.locator(".custom-submitButtonElement").first
                submit_btn.wait_for(state="visible", timeout=60000)
                click_and_wait_for_refresh(page, submit_btn, adapter, timeout=60000)

                install_custom_selector_loop(page, adapter)

                result_loaded, got_no_results, reason = wait_for_grant_results_refresh(
                    page, adapter, old_first_card_text=old_first_text
                )

                try:
                    grant_count_found = parent.locator('[class*="custom-grantList-"]').count()
                except Exception:
                    grant_count_found = "?"
                print(f"[{adapter['Country']}] keyword='{keyword}' url={page.url} "
                      f"no_results={got_no_results} reason='{reason}' grants_found={grant_count_found}")

                if got_no_results:
                    continue

                install_custom_selector_loop(page, adapter)
                runMainLogic(
                    page, parent,
                    keyword=keyword,
                    category=sector.capitalize(),
                    adapter=adapter,
                )


# ===========================================================================
# SECTION 5 — THREAD RUNNER + POST-PROCESSING
# ===========================================================================

# threads = []
# for adapter in adapters:
#     thread = threading.Thread(target=scrape_site, args=(adapter,))
#     threads.append(thread)
#     thread.start()

# for thread in threads:
#     thread.join(timeout=600)


stop_event = threading.Event()

threads = []
for adapter in adapters:
    thread = threading.Thread(target=scrape_site, args=(adapter, stop_event), daemon=True)
    threads.append(thread)
    thread.start()

# Bound the TOTAL scrape wall-clock with one shared deadline. Joining each thread
# with its own timeout in sequence means a single hung adapter costs the full
# timeout and the *next* join then waits another full timeout — so N stuck adapters
# ≈ N×timeout. A shared deadline caps the whole wait no matter how many adapters
# stall. A healthy thread finishes on its own (join returns early), so this cap only
# bites when a thread genuinely hangs — set it high enough that normal scraping of
# every keyword completes. Grants are written to Redis as they're found, so the
# final count scales directly with how long the productive threads are allowed to
# run: lower this for a faster/partial run, raise it for a more complete one.
OVERALL_TIMEOUT = 1800  # 30 min safety cap; real runs finish sooner
deadline = time.monotonic() + OVERALL_TIMEOUT
for thread in threads:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        break
    thread.join(timeout=remaining)

# Signal any still-running threads to stop
stop_event.set()
print("Stop signal sent to scraper threads.")

print("Finished waiting for scraper threads")
print("Collecting Redis grants...")

all_grants = []
for key in r.scan_iter("grant:*"):
    if r.type(key) != "string":
        continue
    value = r.get(key)
    if not value:
        continue
    try:
        all_grants.append(json.loads(value))
    except Exception:
        print(f"Skipping non-JSON key: {key}")

print(f"Grants collected: {len(all_grants)}")

# Drop already-closed grants before the expensive Layer 2/3 passes -- nobody
# can act on a closed call, so there's no point spending embedding/Gemini
# calls scoring one. "Undetermined" (unparseable/missing closing date) is
# kept rather than dropped, since we can't actually confirm it's closed.
# A grant scraped while still open keeps showing on the dashboard after it
# closes too -- the dashboard derives status from the stored closing date at
# view time, this filter only affects what gets processed on THIS run.
before_filter = len(all_grants)
all_grants = [g for g in all_grants if g.get("Grant Status") != "Closed"]
dropped = before_filter - len(all_grants)
if dropped:
    print(f"Dropped {dropped} already-closed grant(s) before Layer 2/3.")

if not all_grants:
    print("No grants collected — skipping all downstream steps.")
else:
    print("Running Layer 2: Semantic Embedding Filter...")
    OUTPUT_JSON = os.path.join(BASE_DIR, "all_grants.json")
    semantic_filter(all_grants, output_file=OUTPUT_JSON)

    RUN_LLM_LAYER = True  # Set to False to skip LLM scoring layer
    if RUN_LLM_LAYER:
        print("Running Layer 3: LLM Analytical Scorer Engine...")
        evaluate_and_score_grants(
            input_json_path=OUTPUT_JSON,
            output_json_path=OUTPUT_JSON,
        )
    else:
        print("Skipping Layer 3 (LLM scoring).")

    print("Compiling Final Excel...")
    with open(OUTPUT_JSON) as f:
        print(f"JSON contains {len(json.load(f))} grants")

    json_to_excel(
        json_filename=OUTPUT_JSON,
        excel_filename=os.path.join(DATA_DIR, "all_grants_pipeline.xlsx"),
    )

end_time = time.perf_counter()
print(f"Execution time: {end_time - start_time:.4f} seconds")