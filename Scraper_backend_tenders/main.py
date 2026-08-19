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

from playwright.sync_api import sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright_stealth import Stealth
import redis

from Scraper_backend_tenders.datasetManager import json_to_excel
from Scraper_backend_tenders.semantic import semantic_filter
from Scraper_backend_tenders.llm_filtration import evaluate_and_score_tenders
from Scraper_backend_tenders.llm_fallback import (
    claude_available,
    extract_with_claude,
    parse_llm_json,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

start_time = time.perf_counter()

BASE_DIR = os.path.dirname(__file__)

# The finished workbook is the shared artifact the dashboard reads, so write it to
# the shared data folder — point TENDER_DATA_DIR at the Google Drive folder (the
# same folder the dashboard app reads from). Falls back to this script's own folder
# when unset.
DATA_DIR = os.environ.get("TENDER_DATA_DIR", "").strip() or BASE_DIR

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

print("Flushing previous tender data from Redis...")
for key in r.scan_iter("tender:*"):
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
    {
        "url": "https://www.find-tender.service.gov.uk/Search/Results",
        "iframe": ["False"],
        "refreshMode": "dom",
        "keywordSearchBox": "input#keywords",
        "submitButton": "button#adv_search_button",
        "IdentifierForTenderList": [[1, "div.notice-search-results"], "div.search-result"],
        "NextPageButton": "a.standard-paginate-next",
        "InitialTenderLinks": "a.search-result-rwh",
        "ResultsIndicatorText": "We've found 0 notices",
        "Country": "UK",
        "BackButton": [False],
    },
    {
        "url": "https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/opportunities/calls-for-tenders?isExactMatch=true&order=DESC&pageNumber=1&pageSize=50&sortBy=startDate",
        "iframe": ["False"],
        "refreshMode": "dom",
        "keywordSearchBox": [[1, "input"]],
        "submitButton": [[3, ".eui-button"]],
        "IdentifierForTenderList": "sedia-result-card-calls-for-tenders",
        "NextPageButton": [[3, ".eui-paginator__page-navigation-item"], [1, "button"]],
        "InitialTenderLinks": ".eui-u-text-link",
        "OrganizationName": "a.ng-star-inserted",
        "TenderTitle": ".eui-common-header__label-text",
        "BidOpeningDate": [[4, "eui-card-content"], [4, ".eui-label"]],
        "BidEndDate": [[4, "eui-card-content"], [6, ".eui-label"]],
        "WorkDescription": [[2, "eui-card"], [7, ".ng-star-inserted"]],
        "TenderValue": [[2, "eui-card"], [3, ".row"], [6, "div"]],
        "StatusIndicator": [[False, "Closed"], "sedia-project-status"],
        "ResultsIndicatorText": "No results found",
        "BackButton": [False],
        "Country": "European Union"
    },
    {
        "url": "https://www.gebiz.gov.sg/ptn/opportunity/BOListing.xhtml",
        "iframe": ["False"],
        # gebiz search is AJAX (JSF mojarra.ab) — the results reload in place and
        # the URL never changes. With "navigation" the submit waited the full 30s
        # for a navigation that never fires, then ~15s polling the URL, for every
        # keyword. "dom" watches the results signature instead and returns as soon
        # as the AJAX result set changes.
        "refreshMode": "dom",
        "keywordSearchBox": [[2, "input.inputText_MAIN"]],
        "submitButton": '.subBannerSearchBar_BUTTON-GO',
        "IdentifierForTenderList": [[1, ".formRepeat_MAIN"], ".formColumns_MAIN"],
        "NextPageButton": [[1, 'input.formRepeatPagination2_NAVIGATION-BUTTON[value="Next"]']],
        "InitialTenderLinks": "a.commandLink_TITLE-BLUE",
        "OrganizationName": [[1, '[id="contentForm:j_idt252"]'], [1, ".formOutputText_VALUE-DIV"]],
        "TenderTitle": ".outputText_TITLE-BLACK",
        "BidOpeningDate": [[4, ".formOutputText_VALUE-DIV"]],
        "BidEndDate": [[7, ".formOutputText_HIDDEN-LABEL"]],
        "TimePeriod": [[9, "div.formOutputText_MAIN"], [1, "div.formOutputText_VALUE-DIV"]],
        "WorkDescription": [[1, "td.table_TABLE-CELL-TD"], [1, "span"]],
        "TenderValue": [[3, "td.table_TABLE-CELL-TD"], [1, "span"]],
        "StatusIndicator": [[False, "Closed"], '.outputText_LABEL-GRAY'],
        "ResultsIndicatorText": "No opportunity found for your search",
        "BackButton": [True, [[1, ".commandButton_BACK-BLUE"], [1, "input"]]],
        "Country": "Singapore"
    },
    {
        "url": "https://www.tenders.gov.au/atm",
        "iframe": ["False"],
        "refreshMode": "navigation",
        "keywordSearchBox": [[1, "input#form-Keyword"]],
        "submitButton": [[1, "button.searchIcon"]],
        "IdentifierForTenderList": [[1, "div.container div.boxEQH"], 'div.row'],
        "NextPageButton": [[1, 'li.next a']],
        "InitialTenderLinks": "a.detail",
        "TenderTitle": [[1, "div.box.boxY p.lead"]],
        "OrganizationName": [[2, "div.list-desc-inner"]],
        "BidEndDate": [[1, 'span:has(label[for="CloseDate"]) + div.list-desc-inner']],
        "BidOpeningDate": [[1, 'span:has(label[for="PublishDate"]) + div.list-desc-inner']],
        "WorkDescription": [[1, 'span:has(label[for="Description"]) + div.list-desc-inner'], [1, "p"]],
        "TimePeriod": [[1, 'span:has(label[for="TimeframeForDelivery"]) + div.list-desc-inner'], [1, "p"]],
        "TenderValue": "Nothing",
        #Closed tenders aren't shown:
        "StatusIndicator": [[True, "Close Date & Time:"], [[2, "div.list-desc"], [1, "span"]]],
        "ResultsIndicatorText": "There are no results that match your selection.",
        "BackButton": [False],
        "Country": "Australia"
    },
    {
        "url": "https://defproc.gov.in/nicgep/app?page=Home&service=page",
        "iframe": ["False"],
        "refreshMode": "dom",
        "keywordSearchBox": [[1, "input#SearchDescription"]],
        "submitButton": [[1, "input#Go"]],
        "IdentifierForTenderList": [[1, "table#table tbody"], "tr.even, tr.odd"],
        "NextPageButton": [[1, "a#linkFwd"]],
        "InitialTenderLinks": "td:nth-child(5) a",
        "TenderTitle": [[6, ".tablebg"], [1, "tbody"], [1, "tr"], [1, ".td_field"]],
        "OrganizationName": [[1, ".tablebg"], [1, "tbody"], [1, "tr"], [1, ".td_field"]],
        "BidEndDate": [[7, ".tablebg"], [1, "tbody"], [4, "tr"], [2, ".td_field"]],
        "BidOpeningDate": [[7, ".tablebg"], [1, "tbody"], [1, "tr"], [2, ".td_field"]],
        "WorkDescription": [[6, ".tablebg"], [1, "tbody"], [2, "tr"], [1, ".td_field"]],
        "TimePeriod": [[6, ".tablebg"], [1, "tbody"], [6, "tr"], [3, ".td_field"]],
        "TenderValue": [[6, ".tablebg"], [1, "tbody"], [5, "tr"], [1, ".td_field"]],
        "BackButton": [True, [[1, 'a[title="Back"]']]],
        # This MoD portal shows "No Tenders found." (not "No Records Found") when
        # a search returns nothing. The old text never matched, so empty-result
        # keywords weren't detected as empty and each one hung ~60s in runMainLogic
        # waiting for tender cards that never appear.
        "ResultsIndicatorText": "No Tenders found",
        "Country": "India"
    },
    # {
    #     "url": "https://www.adgpg.gov.ae/en/For-Suppliers/Public-Tenders",
    #     "iframe": ["False"],
    #     "refreshMode": "dom",
    #     "keywordSearchBox": "#tender-search input",
    #     "submitButton": "#tender-search i.icon-search",
    #     # UAE tenders expand inline — clicking the card header toggles the body.
    #     # No page navigation happens; we read innerText from the expanded card.
    #     "expandInPlace": True,
    #     "IdentifierForTenderList": [[1, "div.ex-tenders--listing"], "div.ex-tender"],
    #     "InitialTenderLinks": ".ex-tender-head",
    #     "ResultsIndicatorText": "No Results Found",
    #     "NextPageButton": [[1, "#load-more-tender"]],
    #     "BackButton": [False],
    #     "Country": "United Arab Emirates"
    # }
    {
        "url": "https://supplier.adgpg.gov.ae/pages/tender-list.html",
        "iframe": ["False"],
        "refreshMode": "dom",
        "keywordSearchBox": "input#searchterm",
        "submitButton": "input#searchterm",  # search filters live as you type — no submit button
        # Must select the grid as a single element ([1, ...]) before querying cards
        # inside it. Two bare strings generate `querySelectorAll(...).querySelectorAll(...)`,
        # and a NodeList has no querySelectorAll — that threw a TypeError inside
        # install_custom_selector_loop, so no cards were ever tagged.
        "IdentifierForTenderList": [[1, "div.tender-grid"], "div.tender-card"],
        "NextPageButton": "button.outlined-btn",  # the "Load more" button
        "InitialTenderLinks": "a.filled-btn",     # the "View details" link
        "ResultsIndicatorText": "No results found",
        "BackButton": [False],
        "Country": "United Arab Emirates",
        "expandInPlace": False,
    }
]


# SECTION 1 — LLM EXTRACTION


EXTRACTION_PROMPT = """You are a specialist in government and public-sector procurement notices
from any country, extracting structured data for Sensio, an Indian health wearables
company operating as a full-stack ODM (smart rings, chest patches, smart bands, and
smart glasses with biosensors for ECG, PPG, SpO2, sleep, and related physiological
monitoring). You will receive the raw HTML (or rendered text) of a single tender notice
page. Extract the fields below and return ONLY a valid JSON object — no markdown fences,
no explanation, nothing else.

Extract the fields factually and completely regardless of how relevant the tender turns
out to be — do not filter, score, or omit a tender based on fit. Relevance scoring happens
in a separate downstream step; your job here is accurate extraction only.

Required fields (use null if not found):
{{
  "Tender Title": "string",
  "Tender Description": "string",
  "Organisation Name": "string",
  "Original Currency": "ISO 4217 code, e.g. GBP, EUR, SGD, AUD, INR",
  "Budget Min": number or null,
  "Budget Max": number or null,
  "Opening Date": "DD Month YYYY or 'not available'",
  "Closing Date": "DD Month YYYY or 'not available'",
  "Tender Status": "Open | Closed | Awarded | Unknown",
  "Award Date": "DD Month YYYY or 'not available'",
  "Country": "string"
}}

Rules:
1. Opening Date
- First look for an explicit tender opening/publication date.
- If an "Opening Date" field is not present, use the date shown next to or after "Published"
  or an equivalent label in the notice's own language/format.
  Examples:
    - Published 16 June 2026
    - Published: 16 June 2026
- The "Published" date should be treated as the Opening Date.
- If neither exists, return "not available".

2. Closing Date
- Extract the tender submission deadline.
- Common labels include (this varies by country/portal, match by meaning, not exact wording):
  - Closing date
  - Closing time
  - Deadline
  - Submission deadline
  - Response deadline
  - Date offers to be received
  - Tender end date / bid end date
- Return only the date in "DD Month YYYY" format.
- If no closing date exists, return "not available".

3. For Budget Min / Budget Max: if only one value is given, set both to that value.
  If a range is given (e.g. lowest lot to highest lot), use those as min and max.

4. For Original Currency: infer from context (currency symbols, country of the notice,
  or explicit currency codes) and always return the ISO 4217 code, not a symbol or name.

5. For Tender Status: derive from whether a submission deadline exists and whether
  it has passed relative to today ({today}).

6. Strip any boilerplate cookie banners, navigation menus, and footer text —
  focus only on the notice content itself.

7. Return ONLY the JSON object.

Page content:
{page_content}
"""


def extract_fields_with_gemini(page_html: str) -> dict:
    """
    Send the page content to Gemini and get structured tender fields back.
    Returns a dict with the extracted fields, or an empty dict on failure.

    On a 429 (rate limit) from the currently active key, rotates to the next
    available Gemini key (if configured) and retries the SAME request rather
    than dropping the tender. Only gives up once every configured key has
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
                              f"for RPM window to reset rather than dropping this tender.")
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


# def build_tender_object(extracted: dict, tender_url: str, search_keyword: str, country: str) -> dict:
#     currency, budget_min, budget_max, inr_min, inr_max = resolve_currency_and_budget(extracted)
#     sector = resolve_sector(search_keyword)
#     title = extracted.get("Tender Title") or ""
#     description = extracted.get("Tender Description") or ""
#     matched_keywords = resolve_keywords(title, description)
#     opening_date = extracted.get("Opening Date")
#     closing_date = extracted.get("Closing Date")
#     timeline = compute_timeline(opening_date, closing_date)
#     # Override Tender Status based on closing date — don't rely on Gemini for this
#     computed_status = extracted.get("Tender Status") or "Unknown"
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
#         "Tender Title": title or None,
#         "Tender Description": description or None,
#         "Organisation Name": extracted.get("Organisation Name"),
#         # "Tender Status": extracted.get("Tender Status"),
#         "Tender Status": computed_status,
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
#         "Link to the Tender": tender_url,
#         "Keywords": matched_keywords,
#         "Keyword included": search_keyword,
#         "Order Quantity": None,
#         "Special Observation": None,
#         "Eligibility": None,
#         "Application Status": "Not Applied",
#         "Current Applicants": None,
#     }

def build_tender_object(extracted: dict, tender_url: str, search_keyword: str, country: str) -> dict:
    currency, budget_min, budget_max, inr_min, inr_max = resolve_currency_and_budget(extracted)

    sector = resolve_sector(search_keyword)

    title = extracted.get("Tender Title") or ""
    description = extracted.get("Tender Description") or ""

    matched_keywords = resolve_keywords(title, description)

    opening_date = extracted.get("Opening Date")
    closing_date = extracted.get("Closing Date")

    timeline = compute_timeline(opening_date, closing_date)

    # Compute Tender Status ourselves (don't rely on Gemini)
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
        "Tender Title": title or None,
        "Tender Description": description or None,
        "Organisation Name": extracted.get("Organisation Name"),
        "Tender Status": computed_status,
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
        "Link to the Tender": tender_url,
        "Keywords": matched_keywords,
        "Keyword included": search_keyword,
        "Order Quantity": None,
        "Special Observation": None,
        "Eligibility": None,
        "Application Status": "Not Applied",
        "Current Applicants": None,
    }


def store_tender(tender_object: dict) -> None:
    primary_key = tender_object["Primary Key"]
    redis_key = f"tender:{primary_key}"
    cached = r.get(redis_key)
    if cached:
        cached_obj = json.loads(cached)
        existing_keywords = set(cached_obj.get("Keywords", []))
        new_keywords = set(tender_object.get("Keywords", []))
        tender_object["Keywords"] = list(existing_keywords | new_keywords)
        r.set(redis_key, json.dumps(tender_object))
        print(f"Updated existing tender -> {primary_key}")
    else:
        r.set(redis_key, json.dumps(tender_object))
        print(json.dumps(tender_object, indent=4))
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
    if isinstance(adapter["IdentifierForTenderList"], list):
        for level in adapter["IdentifierForTenderList"]:
            if isinstance(level, list):
                selectorStr += f"?.querySelectorAll('{level[1]}')[{int(level[0]) - 1}]"
            else:
                selectorStr += f"?.querySelectorAll('{level}')"
    else:
        selectorStr += f"?.querySelectorAll('{adapter['IdentifierForTenderList']}')"
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
            window.__assignTenderCustomSelectors = () => {{
                let iterator = 0;
                let parent = document;

                if ("{adapter["iframe"][0]}" == "True") {{
                    parent = {get_iframe_query_string(True, adapter)};
                    if (!parent) return;
                }}

                const tenderCards = {getElementQueryStringForListItems(adapter)};

                tenderCards?.forEach((listItem) => {{
                    listItem.classList.add(`custom-tenderList-${{iterator}}`);

                    const InitialTenderLink = listItem{get_leaf_selector(adapter["InitialTenderLinks"])};
                    if (InitialTenderLink) {{
                        InitialTenderLink.classList.add(`custom-InitialTenderLinks-${{iterator}}-0`);
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

            window.__assignTenderCustomSelectors();

            if (window.__customSelectorInterval) clearInterval(window.__customSelectorInterval);
            window.__customSelectorInterval = setInterval(() => {{ window.__assignTenderCustomSelectors(); }}, 250);
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


def wait_for_tender_results_refresh(page, adapter, old_first_card_text=None):
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

def process_tender_page(page, adapter, search_keyword, country):
    tender_url = page.url
    try:
        page_content = page.evaluate("() => document.body.innerText")
    except Exception:
        page_content = ""

    if not page_content.strip():
        print(f"Empty page content for {tender_url}, skipping.")
        return

    extracted = extract_fields_with_gemini(page_content)

    if not extracted:
        print(f"Gemini returned no data for {tender_url}, skipping.")
        return

    tender_obj = build_tender_object(
        extracted=extracted,
        tender_url=tender_url,
        search_keyword=search_keyword,
        country=country,
    )
    store_tender(tender_obj)


def process_tender_card_in_place(card, page, adapter, search_keyword, country):
    """
    For sites like UAE where tenders expand inline (no page navigation).
    Clicks the card header to expand it, reads innerText, sends to Gemini,
    then collapses the card again.
    """
    tender_url = page.url  # stays the same — no navigation

    # Click the header to expand the card body
    try:
        header = card.locator(".ex-tender-head").first
        header.click()
        # Wait for the body to appear
        card.locator(".ex-tender-body").wait_for(state="visible", timeout=10000)
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

    # Use the tender's own detail URL if available inside the card
    try:
        detail_link = card.locator("a.button--primary").first.get_attribute("href")
        if detail_link:
            tender_url = detail_link
    except Exception:
        pass

    tender_obj = build_tender_object(
        extracted=extracted,
        tender_url=tender_url,
        search_keyword=search_keyword,
        country=country,
    )
    store_tender(tender_obj)

    # Collapse the card again so the next click works cleanly
    try:
        header = card.locator(".ex-tender-head").first
        header.click()
        card.locator(".ex-tender-body").wait_for(state="hidden", timeout=5000)
    except Exception:
        pass  # collapsing is best-effort


def runMainLogic(page, parent, keyword, category, adapter, timer=1):
    install_custom_selector_loop(page, adapter)

    try:
        lists = parent.locator('[class*="custom-tenderList-"]')
        lists.first.wait_for(state="attached", timeout=60000)
    except Exception:
        return

    tender_count = lists.count()
    expand_in_place = adapter.get("expandInPlace", False)

    for listNumber in range(tender_count):
        install_custom_selector_loop(page, adapter)
        card = lists.nth(listNumber)

        if expand_in_place:
            # ---------------------------------------------------------------
            # UAE-style: expand card inline, extract, collapse — no navigation
            # ---------------------------------------------------------------
            try:
                process_tender_card_in_place(
                    card, page, adapter,
                    search_keyword=keyword,
                    country=adapter["Country"],
                )
            except Exception as e:
                print(f"Error processing inline card: {e}")
            continue  # no back-navigation needed

        # -------------------------------------------------------------------
        # Standard: navigate into tender page, extract, go back
        # -------------------------------------------------------------------
        element = card.locator('[class*="custom-InitialTenderLinks-"]').first
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
            process_tender_page(page, adapter, search_keyword=keyword, country=adapter["Country"])
        except Exception as e:
            print(f"Error processing tender page: {e}")

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

        wait_for_js_visible(page, getElementQueryString("keywordSearchBox", adapter))
        install_custom_selector_loop(page, adapter)

    # Pagination
    before_signature = get_results_signature(page, adapter)
    try:
        nextPageButton = parent.locator(".custom-NextPageButton")
        nextPageButton.first.wait_for(state="attached")
        if nextPageButton.is_disabled():
            return
        old_first_text = parent.locator('[class*="custom-tenderList-"]').first.inner_text(timeout=3000)
        nextPageButton.click()
        wait_for_results_signature_change_or_stability(page, adapter, before_signature=before_signature)
        wait_for_tender_results_refresh(page=page, old_first_card_text=old_first_text, adapter=adapter)
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
                    old_first_text = parent.locator('[class*="custom-tenderList-"]').first.inner_text(timeout=3000)
                except Exception:
                    old_first_text = None

                submit_btn = parent.locator(".custom-submitButtonElement").first
                submit_btn.wait_for(state="visible", timeout=60000)
                click_and_wait_for_refresh(page, submit_btn, adapter, timeout=60000)

                install_custom_selector_loop(page, adapter)

                result_loaded, got_no_results, reason = wait_for_tender_results_refresh(
                    page, adapter, old_first_card_text=old_first_text
                )

                try:
                    tender_count_found = parent.locator('[class*="custom-tenderList-"]').count()
                except Exception:
                    tender_count_found = "?"
                print(f"[{adapter['Country']}] keyword='{keyword}' url={page.url} "
                      f"no_results={got_no_results} reason='{reason}' tenders_found={tender_count_found}")

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
# every keyword completes. Tenders are written to Redis as they're found, so the
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
print("Collecting Redis tenders...")

all_tenders = []
for key in r.scan_iter("tender:*"):
    if r.type(key) != "string":
        continue
    value = r.get(key)
    if not value:
        continue
    try:
        all_tenders.append(json.loads(value))
    except Exception:
        print(f"Skipping non-JSON key: {key}")

print(f"Tenders collected: {len(all_tenders)}")

if not all_tenders:
    print("No tenders collected — skipping all downstream steps.")
else:
    print("Running Layer 2: Semantic Embedding Filter...")
    OUTPUT_JSON = os.path.join(BASE_DIR, "all_tenders.json")
    semantic_filter(all_tenders, output_file=OUTPUT_JSON)

    RUN_LLM_LAYER = True  # Set to False to skip LLM scoring layer
    if RUN_LLM_LAYER:
        print("Running Layer 3: LLM Analytical Scorer Engine...")
        evaluate_and_score_tenders(
            input_json_path=OUTPUT_JSON,
            output_json_path=OUTPUT_JSON,
        )
    else:
        print("Skipping Layer 3 (LLM scoring).")

    print("Compiling Final Excel...")
    with open(OUTPUT_JSON) as f:
        print(f"JSON contains {len(json.load(f))} tenders")

    json_to_excel(
        json_filename=OUTPUT_JSON,
        excel_filename=os.path.join(DATA_DIR, "all_tenders_pipeline.xlsx"),
    )

end_time = time.perf_counter()
print(f"Execution time: {end_time - start_time:.4f} seconds")