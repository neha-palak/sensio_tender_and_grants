"""Postgres access for tenders/grants + saved_items (see schema.sql).

One lazily-created global connection: gunicorn's default is one sync worker
handling one request at a time, so a pool buys nothing here.
ponytail: single global connection, fine under gunicorn's default 1 sync
worker -- switch to psycopg2.pool.SimpleConnectionPool if worker/thread count
ever increases.
"""
import os

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()  # no-op in prod (Render sets real env vars); loads .env locally

_conn = None

_TABLE = {"tenders": "tenders", "grants": "grants"}

_ITEM_COLUMNS = (
    "primary_key, title, sector, country, opening_date, closing_date, "
    "relevancy_score, inr_budget_maximum, description, organisation_name, url"
)


def get_conn():
    global _conn
    if _conn is None or _conn.closed:
        _conn = psycopg2.connect(os.environ["DATABASE_URL"])
        _conn.autocommit = True
    return _conn


def _cur():
    return get_conn().cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def load_items(domain):
    """All rows in tenders/grants, newest scrape first."""
    table = _TABLE[domain]
    cur = _cur()
    cur.execute(f"SELECT * FROM {table} ORDER BY scraped_at DESC")
    return cur.fetchall()


def upsert_item(domain, row):
    """row: dict with keys matching the tenders/grants columns (see schema.sql),
    keyed by primary_key. Used by the scrapers to write a scraped batch."""
    table = _TABLE[domain]
    cur = get_conn().cursor()
    cur.execute(
        f"""
        INSERT INTO {table} (primary_key, title, sector, country, opening_date,
            closing_date, relevancy_score, inr_budget_maximum, description,
            organisation_name, url, scraped_at)
        VALUES (%(primary_key)s, %(title)s, %(sector)s, %(country)s, %(opening_date)s,
            %(closing_date)s, %(relevancy_score)s, %(inr_budget_maximum)s,
            %(description)s, %(organisation_name)s, %(url)s, now())
        ON CONFLICT (primary_key) DO UPDATE SET
            title = EXCLUDED.title, sector = EXCLUDED.sector, country = EXCLUDED.country,
            opening_date = EXCLUDED.opening_date, closing_date = EXCLUDED.closing_date,
            relevancy_score = EXCLUDED.relevancy_score,
            inr_budget_maximum = EXCLUDED.inr_budget_maximum,
            description = EXCLUDED.description, organisation_name = EXCLUDED.organisation_name,
            url = EXCLUDED.url, scraped_at = now()
        """,
        row,
    )


def list_saved(domain):
    """Merged saved list for a domain: every distinct saved item, joined back to
    its live row, with starred_by = the founders who saved it. DB equivalent of
    the old merge_saved_items() that read every founder's Excel file."""
    table = _TABLE[domain]
    cur = _cur()
    cur.execute(
        f"""
        SELECT t.*, array_agg(s.founder ORDER BY s.founder) AS starred_by
        FROM saved_items s
        JOIN {table} t ON t.primary_key = s.item_id
        WHERE s.domain = %s
        GROUP BY t.primary_key
        """,
        (domain,),
    )
    return cur.fetchall()


def save_item(domain, item_id, founder):
    cur = get_conn().cursor()
    cur.execute(
        "INSERT INTO saved_items (domain, item_id, founder) VALUES (%s, %s, %s) "
        "ON CONFLICT (domain, item_id, founder) DO NOTHING",
        (domain, item_id, founder),
    )


def remove_item(domain, item_id, founder):
    cur = get_conn().cursor()
    cur.execute(
        "DELETE FROM saved_items WHERE domain = %s AND item_id = %s AND founder = %s",
        (domain, item_id, founder),
    )


def saved_ids_for(domain, founder):
    """Saved primary keys for ONE founder -- powers per-user star state."""
    cur = _cur()
    cur.execute(
        "SELECT item_id FROM saved_items WHERE domain = %s AND founder = %s",
        (domain, founder),
    )
    return {r["item_id"] for r in cur.fetchall()}


def all_saved_ids(domain):
    """Union of saved ids across every founder -- used only for the expiry-alert
    emails (team-wide watchlist), not per-user star state."""
    cur = _cur()
    cur.execute("SELECT DISTINCT item_id FROM saved_items WHERE domain = %s", (domain,))
    return {r["item_id"] for r in cur.fetchall()}
