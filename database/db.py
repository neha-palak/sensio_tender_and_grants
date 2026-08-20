"""Postgres access for tenders/grants + saved_items (see schema.sql).

One lazily-created global connection: gunicorn's default is one sync worker
handling one request at a time, so a pool buys nothing here.
ponytail: single global connection, fine under gunicorn's default 1 sync
worker -- switch to psycopg2.pool.SimpleConnectionPool if worker/thread count
ever increases.

tenders/grants are fully replaced by each scrape run (see replace_items) --
primary keys are date-stamped per run (TND-DDMMYY-01, see datasetManager.py),
so nothing carries over week to week in those tables by design. saved_items
is therefore a full snapshot of an item's data at save time, not just a
reference -- it's the only place a starred item's data survives past the
week it was saved in.
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

_SNAPSHOT_COLUMNS = (
    "title, sector, country, opening_date, closing_date, relevancy_score, "
    "inr_budget_maximum, description, organisation_name, url"
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


def replace_items(domain, rows):
    """Wipe tenders/grants and insert this run's batch fresh, in one
    transaction -- the main sheet reflects only the current week's scrape,
    never accumulating past runs. Transactional so the table is never left
    empty if an insert fails partway through. saved_items is untouched by
    this (it snapshots its own data, no FK to these tables)."""
    table = _TABLE[domain]
    conn = get_conn()
    conn.autocommit = False
    try:
        cur = conn.cursor()
        cur.execute(f"DELETE FROM {table}")
        for row in rows:
            cur.execute(
                f"""
                INSERT INTO {table} (primary_key, title, sector, country, opening_date,
                    closing_date, relevancy_score, inr_budget_maximum, description,
                    organisation_name, url, scraped_at)
                VALUES (%(primary_key)s, %(title)s, %(sector)s, %(country)s, %(opening_date)s,
                    %(closing_date)s, %(relevancy_score)s, %(inr_budget_maximum)s,
                    %(description)s, %(organisation_name)s, %(url)s, now())
                """,
                row,
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.autocommit = True


def list_saved(domain):
    """Merged saved list for a domain: one row per item, newest-saved-first,
    with starred_by = every founder who saved it. Reads entirely from
    saved_items' own snapshot columns -- no join against tenders/grants,
    since those get wiped weekly and a save must outlive that."""
    cur = _cur()
    cur.execute(
        """
        WITH latest AS (
            SELECT DISTINCT ON (item_id) *
            FROM saved_items
            WHERE domain = %(domain)s
            ORDER BY item_id, saved_at DESC
        ),
        starred AS (
            SELECT item_id, array_agg(founder ORDER BY founder) AS starred_by,
                   MAX(saved_at) AS latest_saved_at
            FROM saved_items
            WHERE domain = %(domain)s
            GROUP BY item_id
        )
        SELECT latest.*, latest.item_id AS primary_key, starred.starred_by
        FROM latest
        JOIN starred USING (item_id)
        ORDER BY starred.latest_saved_at DESC
        """,
        {"domain": domain},
    )
    return cur.fetchall()


def save_item(domain, item_id, founder):
    """Snapshot the item's CURRENT data (from this week's live table) into
    saved_items at the moment it's saved. A no-op if this founder already
    saved it -- re-saving doesn't refresh the snapshot, only a fresh
    save/unsave/save cycle would."""
    table = _TABLE[domain]
    cur = _cur()
    cur.execute(f"SELECT {_ITEM_COLUMNS} FROM {table} WHERE primary_key = %s", (item_id,))
    live_row = cur.fetchone()
    if not live_row:
        return  # item isn't in the current live table -- nothing to snapshot

    write_cur = get_conn().cursor()
    write_cur.execute(
        f"""
        INSERT INTO saved_items (domain, item_id, founder, {_SNAPSHOT_COLUMNS})
        VALUES (%(domain)s, %(item_id)s, %(founder)s, %(title)s, %(sector)s, %(country)s,
            %(opening_date)s, %(closing_date)s, %(relevancy_score)s, %(inr_budget_maximum)s,
            %(description)s, %(organisation_name)s, %(url)s)
        ON CONFLICT (domain, item_id, founder) DO NOTHING
        """,
        {"domain": domain, "item_id": item_id, "founder": founder, **live_row},
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
