"""Self-check for Scraper_backend_*/datasetManager.py's upsert_rows_to_postgres().
Run: python3 database/test_upsert_mapping.py

Monkeypatches database.db.upsert_item so this never touches a real database --
it's checking the row-mapping/gating logic, not Postgres itself.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FIXTURE = [
    {
        "Primary Key": "TND-2026-0001",
        "Tender Title": "Test Tender One",
        "Sector": "health",
        "Country": "India",
        "Opening date": "2026-01-01",
        "Closing date": "2026-02-01",
        "Relevancy Score": 8.5,
        "INR Budget Maximum": "1,234,567.89 INR",
        "Description": "desc one",
        "Organisation name": "Org One",
        "Tender URL": "https://example.com/1",
    },
    {
        "Primary Key": "TND-2026-0002",
        "Tender Title": "Test Tender Two",
        "Sector": "defence",
        "Country": "USA",
        "Opening date": "2026-01-05",
        "Closing date": "2026-02-05",
        "Relevancy Score": 6.0,
        "INR Budget Maximum": "0",
        "Description": "desc two",
        "Organisation name": "Org Two",
        "Tender URL": "https://example.com/2",
    },
]

# database.db calls load_dotenv() at import time, which would repopulate
# DATABASE_URL from a local .env on every import -- import it once up front so
# that one-time load happens now, then pop the var for the gate check below
# (a later `import database.db` is a no-op, module's already cached).
from database import db

calls = []
db.upsert_item = lambda domain, row: calls.append((domain, row))

from Scraper_backend_tenders.datasetManager import upsert_rows_to_postgres

# --- gate check: DATABASE_URL unset -> upsert_item never called ---
os.environ.pop("DATABASE_URL", None)
upsert_rows_to_postgres(FIXTURE, "tenders", "Tender Title", "Tender URL")
assert calls == [], f"expected no calls with DATABASE_URL unset, got {calls}"

# --- DATABASE_URL set -> upsert_item called once per row, correctly mapped ---
os.environ["DATABASE_URL"] = "postgresql://fake:fake@localhost:5432/fake"
upsert_rows_to_postgres(FIXTURE, "tenders", "Tender Title", "Tender URL")

assert len(calls) == 2, f"expected 2 calls, got {len(calls)}"

domain0, row0 = calls[0]
assert domain0 == "tenders"
assert row0["primary_key"] == "TND-2026-0001"
assert row0["title"] == "Test Tender One"
assert row0["url"] == "https://example.com/1"
# Keeps the decimal point (unlike a naive [^\d] strip, which would turn
# "1,234,567.89" into 123456789 -- a ~100x inflation bug fixed in
# datasetManager.py's _budget_to_int).
assert row0["inr_budget_maximum"] == 1234567, row0["inr_budget_maximum"]
assert isinstance(row0["inr_budget_maximum"], int)

domain1, row1 = calls[1]
assert row1["primary_key"] == "TND-2026-0002"
assert row1["inr_budget_maximum"] == 0

# --- grants side: same budget-parsing fix, applied independently in its own
# datasetManager.py (the two packages are separate forks, not shared code) ---
from Scraper_backend_grants.datasetManager import (
    _budget_to_int as grants_budget_to_int,
    get_verbal_scale_multiplier as grants_scale,
)
assert grants_budget_to_int("1,234,567.89 INR") == 1234567
assert grants_budget_to_int("0") == 0
assert grants_budget_to_int("garbage") == 0

# --- K-thousands multiplier: must require a digit before K (mirroring the
# existing M/B checks), not just "K" anywhere -- a bare substring match also
# fired on currency codes containing K (DKK, KRW, PKR, HKD). ---
assert grants_scale("500K") == 1_000.0
assert grants_scale("88.5K") == 1_000.0
assert grants_scale("DKK 500") == 1.0, "DKK falsely triggered the K-thousands multiplier"
assert grants_scale("KRW 500") == 1.0, "KRW falsely triggered the K-thousands multiplier"

from Scraper_backend_tenders.datasetManager import get_verbal_scale_multiplier as tenders_scale
assert tenders_scale("500K") == 1_000.0
assert tenders_scale("HKD 500") == 1.0, "HKD falsely triggered the K-thousands multiplier"

print("[✓] test_upsert_mapping.py: all checks passed")
