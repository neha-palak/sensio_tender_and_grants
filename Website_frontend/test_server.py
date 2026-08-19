"""Smoke test for the merged Flask app. Run: python3 Website_frontend/test_server.py

Points TENDER_DATA_DIR/GRANT_DATA_DIR at an empty temp dir so it exercises the
"no live Excel yet" path deterministically, regardless of what's on this machine.
"""
import os
import tempfile

with tempfile.TemporaryDirectory() as tmp:
    os.environ["TENDER_DATA_DIR"] = tmp
    os.environ["GRANT_DATA_DIR"] = tmp

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

    r = client.get("/")
    assert r.status_code == 200, r.status_code

    print("[✓] test_server.py: all checks passed")
