"""Smoke test for the merged Flask app. Run: python3 Website_frontend/test_server.py

Stubs database.db so this runs without a live Supabase connection (CI/local
dev without DATABASE_URL set) -- it's checking the Flask route wiring, not the
database. The scraper->Postgres path and the schema itself aren't covered
here; verify those against a real Supabase database separately.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from database import db

db.load_items = lambda domain: []
db.all_saved_ids = lambda domain: set()
db.list_saved = lambda domain: []
db.saved_ids_for = lambda domain, founder: set()

from server import app

client = app.test_client()

r = client.get("/api/tenders/sensio-stream")
assert r.status_code == 200, r.status_code
assert r.get_json()["tenders"] == []

r = client.get("/api/grants/sensio-stream")
assert r.status_code == 200, r.status_code
assert r.get_json()["grants"] == []

r = client.post("/api/tenders/save-tender", json={})
assert r.status_code == 400, r.status_code

r = client.post("/api/grants/save-grant", json={})
assert r.status_code == 400, r.status_code

r = client.get("/api/tenders/saved-tenders")
assert r.status_code == 200, r.status_code
assert r.get_json()["tenders"] == []

r = client.get("/api/tenders/saved-ids")
assert r.status_code == 200, r.status_code
assert r.get_json()["savedIds"] == []

r = client.get("/")
assert r.status_code == 200, r.status_code

print("[✓] test_server.py: all checks passed")
