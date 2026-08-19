from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import os
import smtplib
import datetime
import re
import threading
import time
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from database import db

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.join(sys._MEIPASS, "Website_frontend")
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Both original tools defaulted to 5001 (Tenders) / 5002 (Grants). This merged
# app replaces both, so it reuses 5001 by default — override with
# SENSIO_DASHBOARD_PORT if that's taken. Render sets its own $PORT; see
# desktop_app.py / Procfile.
PORT = int(os.environ.get("SENSIO_DASHBOARD_PORT", "5001"))

app = Flask(__name__, static_folder=BASE_DIR, static_url_path="")
CORS(app, resources={r"/api/*": {"origins": "*"}})


@app.after_request
def apply_cors_headers(response):
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
    response.headers.add('Access-Control-Allow-Methods', 'GET,POST,OPTIONS')
    return response


# ═══════════════════════════════════════════════════════════════
# GMAIL CONFIGURATION KEYS (shared across domains)
# ═══════════════════════════════════════════════════════════════
GMAIL_USER = "your-email@gmail.com"          # 👈 Replace with your Gmail address
GMAIL_APP_PASS = "your-app-password-here"    # 👈 Replace with your 16-character Google App Password
RECEIVER_EMAIL = "your-email@gmail.com"       # 👈 Where you want to receive alerts


def send_gmail_notification(subject, html_content):
    if GMAIL_USER == "your-email@gmail.com" or GMAIL_APP_PASS == "your-app-password-here":
        print(f"[!] Notification skipped. Set up credentials to deliver email: {subject}")
        return False
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = GMAIL_USER
        msg['To'] = RECEIVER_EMAIL
        msg.attach(MIMEText(html_content, 'html'))

        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(GMAIL_USER, GMAIL_APP_PASS)
        server.sendmail(GMAIL_USER, RECEIVER_EMAIL, msg.as_string())
        server.quit()
        print(f"[✓] Notification Email dispatched successfully: {subject}")
        return True
    except Exception as e:
        print(f"[✕] SMTP Pipeline Failure encountered: {e}")
        return False


def calculate_days_remaining(closing_date_str):
    # Pure Python, no pandas: datasetManager.py already normalises closing
    # dates to YYYY-MM-DD before they ever reach Postgres, so that's the
    # primary format -- the rest are defensive fallbacks for anything that
    # slipped through unparsed. (pd.to_datetime used to be used here; ruled
    # out as a candidate after a segfault in this request path on Render
    # didn't reproduce locally and wasn't fixed by unrelated changes.)
    date_part = str(closing_date_str).split(' ')[0].strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d %B %Y", "%d %b %Y"):
        try:
            closing_date = datetime.datetime.strptime(date_part, fmt).date()
            return (closing_date - datetime.datetime.now().date()).days
        except ValueError:
            continue
    return 999


class Domain:
    """Everything that differs between the Tenders and Grants API surfaces.
    Both now read/write the same two Postgres tables (tenders/grants) plus the
    shared saved_items table — see database/schema.sql and database/db.py."""

    def __init__(self, name, plural, id_key):
        self.name = name        # "tender" / "grant"
        self.plural = plural    # "tenders" / "grants" — also the `domain` value in saved_items
        self.id_key = id_key    # payload key: "tenderId" / "grantId"
        self.notified_alerts = set()   # per-process expiry/relevancy email dedup


DOMAINS = {
    "tenders": Domain(name="tender", plural="tenders", id_key="tenderId"),
    "grants": Domain(name="grant", plural="grants", id_key="grantId"),
}


def _item_json(row, domain):
    try:
        relevancy_score = float(row.get("relevancy_score") or 0.50)
    except Exception:
        relevancy_score = 0.50
    return {
        "id": row["primary_key"],
        "title": row.get("title") or f"Untitled {domain.name.capitalize()}",
        "category": str(row.get("sector") or "health").strip().lower(),
        "country": row.get("country") or "Global",
        "openingDate": row.get("opening_date") or "N/A",
        "closingDate": row.get("closing_date") or "N/A",
        "relevancyScore": relevancy_score / 10 if relevancy_score > 1.0 else relevancy_score,
        "budgetINR": int(row.get("inr_budget_maximum") or 0),
        "description": row.get("description") or "",
        "eligibility": f"Authority: {row.get('organisation_name') or 'Unknown'}",
        "link": row.get("url") or "https://google.com",
    }


def register_domain_routes(app, domain: Domain):
    prefix = f"/api/{domain.plural}"

    @app.route(f'{prefix}/sensio-stream', methods=['GET'], endpoint=f'stream_{domain.plural}')
    def stream_items():
        try:
            rows = db.load_items(domain.plural)
            saved_ids_now = db.all_saved_ids(domain.plural)

            items_pool = []
            for row in rows:
                item = _item_json(row, domain)
                items_pool.append(item)

                days_left = calculate_days_remaining(row.get("closing_date"))
                relevancy_score = item["relevancyScore"]
                item_id = item["id"]

                if relevancy_score >= 0.9:
                    alert_key = f"{item_id}_relevancy"
                    if alert_key not in domain.notified_alerts:
                        display_score = f"{relevancy_score * 100:.0f}"
                        subject = f"🔥 High Relevancy Match Found: {item['title']}"
                        html = f"""
                        <div style="font-family: sans-serif; padding: 20px; border: 1px solid #0d9488; border-radius: 8px; max-width:600px;">
                            <h2 style="color: #0d9488; margin-top:0;">Sensio Target Acquisition</h2>
                            <p>An exceptional {domain.name} matching your operational blueprint has been scanned at <strong>{display_score}% Weight</strong>.</p>
                            <hr style="border:none; border-top:1px solid #cbd5e1; margin:16px 0;"/>
                            <p><strong>{domain.name.capitalize()} ID:</strong> {item_id}</p>
                            <p><strong>Title:</strong> {item['title']}</p>
                            <p><strong>Sector:</strong> {item['category'].upper()}</p>
                            <br/>
                            <a href="{item['link']}" target="_blank" style="background: #0d9488; color: white; padding: 10px 16px; text-decoration: none; border-radius: 6px; font-weight:600; display:inline-block;">Access Portal</a>
                        </div>
                        """
                        if send_gmail_notification(subject, html):
                            domain.notified_alerts.add(alert_key)

                if item_id in saved_ids_now and 0 <= days_left <= 7:
                    alert_key = f"{item_id}_expiry"
                    if alert_key not in domain.notified_alerts:
                        subject = f"⚠️ Critical Timeline Warning: Watchlist {domain.name.capitalize()} closes in {days_left} days!"
                        html = f"""
                        <div style="font-family: sans-serif; padding: 20px; border: 1px solid #dc2626; border-radius: 8px; max-width:600px;">
                            <h2 style="color: #dc2626; margin-top:0;">Sensio Framework Urgent Exception</h2>
                            <p>Action is required. A saved watchlist {domain.name} is approaching its final expiration threshold.</p>
                            <hr style="border:none; border-top:1px solid #cbd5e1; margin:16px 0;"/>
                            <p><strong>{domain.name.capitalize()} ID:</strong> {item_id}</p>
                            <p><strong>Title:</strong> {item['title']}</p>
                            <p><strong>Time Remaining:</strong> <span style="color:#dc2626; font-weight:700;">{days_left} Days Left</span></p>
                        </div>
                        """
                        if send_gmail_notification(subject, html):
                            domain.notified_alerts.add(alert_key)

            return jsonify({"sourceFile": "Supabase Postgres", domain.plural: items_pool}), 200
        except Exception as e:
            return jsonify({"error": f"Internal mapping failure: {str(e)}", domain.plural: []}), 500

    @app.route(f'{prefix}/save-{domain.name}', methods=['POST'], endpoint=f'sync_saved_{domain.plural}')
    def sync_saved_state():
        data = request.get_json() or {}
        item_id = str(data.get(domain.id_key, ""))
        is_saved = data.get("isSaved", False)
        founder_name = str(data.get("founderName", "")).strip()

        if not item_id:
            return jsonify({"error": f"Missing parameter: {domain.id_key}"}), 400
        if not founder_name:
            return jsonify({"error": "Missing parameter: founderName"}), 400

        if is_saved:
            db.save_item(domain.plural, item_id, founder_name)
        else:
            db.remove_item(domain.plural, item_id, founder_name)

        return jsonify({"status": "success"}), 200

    @app.route(f'{prefix}/saved-{domain.plural}', methods=['GET'], endpoint=f'get_saved_{domain.plural}')
    def get_saved_items():
        try:
            items = []
            for row in db.list_saved(domain.plural):
                item = _item_json(row, domain)
                item["starredBy"] = ", ".join(row.get("starred_by") or [])
                item["saved"] = True
                items.append(item)
            return jsonify({domain.plural: items}), 200
        except Exception as e:
            return jsonify({"error": str(e), domain.plural: []}), 500

    @app.route(f'{prefix}/saved-ids', methods=['GET'], endpoint=f'get_saved_ids_{domain.plural}')
    def get_saved_ids():
        founder = request.args.get('founder', '').strip()
        if not founder:
            return jsonify({"savedIds": []}), 200
        return jsonify({"savedIds": list(db.saved_ids_for(domain.plural, founder))}), 200


for domain in DOMAINS.values():
    register_domain_routes(app, domain)


@app.route('/api/shutdown', methods=['POST'])
def shutdown_app():
    # Cleanly stop a local instance from the dashboard's "Quit App" button
    # (used when running via desktop_app.py; harmless/unused when hosted on
    # Render). Response is sent first, then hard-exit from a background thread
    # a beat later so the reply actually reaches the browser.
    def _stop():
        time.sleep(0.4)
        os._exit(0)
    threading.Thread(target=_stop, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/")
def dashboard():
    return send_from_directory(BASE_DIR, "index.html")


@app.route("/<path:path>")
def static_files(path):
    return send_from_directory(BASE_DIR, path)


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=PORT, debug=False, use_reloader=False)
