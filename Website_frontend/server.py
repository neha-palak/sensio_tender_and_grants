from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import pandas as pd
import os
import glob
import smtplib
import datetime
import re
import threading
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import sys

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.join(sys._MEIPASS, "Website_frontend")
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Both original tools defaulted to 5001 (Tenders) / 5002 (Grants). This merged
# app replaces both, so it reuses 5001 by default — override with
# SENSIO_DASHBOARD_PORT if that's taken.
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
    try:
        anchor_date = pd.to_datetime(datetime.datetime.now().date())
        closing_date = pd.to_datetime(str(closing_date_str).split(' ')[0])
        return (closing_date - anchor_date).days
    except Exception:
        return 999


def _next_to_app_dir():
    """The folder the packaged app 'lives in' — where data sits when the whole
    thing is kept together (Windows, or a Mac app left next to its data)."""
    exe = os.path.abspath(sys.executable)
    marker = ".app/Contents/MacOS/"
    idx = exe.replace("\\", "/").find(marker)
    if idx != -1:
        return os.path.dirname(exe[: idx + len(".app")])
    return os.path.dirname(os.path.dirname(exe))


def _find_drive_data_dir(excel_basename):
    """Locate the shared folder inside Google Drive for Desktop by looking for the
    weekly Excel. Needed on Mac: a .app can't be launched from inside Drive, so
    people unzip it to their Desktop — from there we still have to find the Drive
    folder to read/write the shared files."""
    home = os.path.expanduser("~")
    roots = []
    roots += glob.glob(os.path.join(home, "Library", "CloudStorage", "GoogleDrive-*", "My Drive"))
    roots += glob.glob(os.path.join(home, "Library", "CloudStorage", "GoogleDrive-*", "Shared drives", "*"))
    roots.append(os.path.join(home, "Google Drive"))
    roots.append(os.path.join(home, "Google Drive", "My Drive"))
    roots.append("/Volumes/GoogleDrive/My Drive")
    for root in roots:
        if not os.path.isdir(root):
            continue
        for depth in ("*", os.path.join("*", "*"), os.path.join("*", "*", "*")):
            hits = glob.glob(os.path.join(root, depth, excel_basename))
            if hits:
                return os.path.dirname(hits[0])
    return None


class Domain:
    """Everything that differs between the Tenders and Grants API surfaces.
    Mirrors the two original standalone server.py files field-for-field —
    same env vars, same Excel column names, same payload keys — so both
    scrapers and both shared Drive folders keep working unmodified."""

    def __init__(self, name, plural, excel_basename, data_dir_env, title_col,
                 url_col, id_key, legacy_saved_name):
        self.name = name                        # "tender" / "grant"
        self.plural = plural                    # "tenders" / "grants"
        self.excel_basename = excel_basename
        self.data_dir_env = data_dir_env
        self.title_col = title_col
        self.url_col = url_col
        self.id_key = id_key                    # payload key: "tenderId" / "grantId"
        self.legacy_saved_name = legacy_saved_name

        env = os.environ.get(data_dir_env, "").strip()
        if env:
            self.data_dir = env
        elif getattr(sys, "frozen", False):
            beside = _next_to_app_dir()
            if os.path.exists(os.path.join(beside, excel_basename)):
                self.data_dir = beside
            else:
                self.data_dir = _find_drive_data_dir(excel_basename) or beside
        else:
            self.data_dir = BASE_DIR

        self.live_excel_path = os.path.join(self.data_dir, excel_basename)
        self.saved_lock = threading.Lock()
        self.notified_alerts = set()
        self.saved_ids_db = set()


DOMAINS = {
    "tenders": Domain(
        name="tender", plural="tenders",
        excel_basename="all_tenders_pipeline.xlsx",
        data_dir_env="TENDER_DATA_DIR",
        title_col="Tender Title", url_col="Tender URL",
        id_key="tenderId", legacy_saved_name="saved_tenders.xlsx",
    ),
    "grants": Domain(
        name="grant", plural="grants",
        excel_basename="all_grants_pipeline.xlsx",
        data_dir_env="GRANT_DATA_DIR",
        title_col="Grant Title", url_col="Grant URL",
        id_key="grantId", legacy_saved_name="saved_grants.xlsx",
    ),
}


# ═══════════════════════════════════════════════════════════════
# SAVED-ITEMS PERSISTENCE LAYER (one file per founder, per domain)
# ═══════════════════════════════════════════════════════════════
# Google Drive has no cross-machine file locking: if two people wrote the SAME
# saved file at once, Drive would silently create a "(conflicted copy)" and drop
# one person's stars. So each founder writes ONLY their own saved_<name>.xlsx and
# never touches anyone else's. Reads MERGE every founder file into one shared
# list, and "Starred By" is derived from which files contain a given item.

def _founder_slug(founder_name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", str(founder_name).strip())
    return slug.strip("_") or "unknown"


def saved_path_for(domain: Domain, founder_name: str) -> str:
    return os.path.join(domain.data_dir, f"saved_{_founder_slug(founder_name)}.xlsx")


def list_saved_files(domain: Domain) -> list:
    return sorted(
        p for p in glob.glob(os.path.join(domain.data_dir, "saved_*.xlsx"))
        if os.path.basename(p) != domain.legacy_saved_name
    )


def _founder_from_path(path: str) -> str:
    base = os.path.basename(path)
    if base.startswith("saved_") and base.endswith(".xlsx"):
        return base[len("saved_"):-len(".xlsx")]
    return base


def load_saved_ids(domain: Domain) -> set:
    ids = set()
    for path in list_saved_files(domain):
        try:
            df = pd.read_excel(path)
            if "Primary Key" in df.columns:
                ids.update(df["Primary Key"].astype(str).tolist())
        except Exception as e:
            print(f"[!] Could not read {os.path.basename(path)}: {e}")
    return ids


def load_saved_ids_for(domain: Domain, founder_name: str) -> set:
    path = saved_path_for(domain, founder_name)
    if not os.path.exists(path):
        return set()
    try:
        df = pd.read_excel(path)
        if "Primary Key" in df.columns:
            return set(df["Primary Key"].astype(str).tolist())
    except Exception as e:
        print(f"[!] Could not read {os.path.basename(path)}: {e}")
    return set()


def save_item_to_excel(domain: Domain, item_id: str, founder_name: str):
    if not os.path.exists(domain.live_excel_path):
        print(f"[!] Live Excel not found, cannot save {domain.name}.")
        return
    path = saved_path_for(domain, founder_name)
    with domain.saved_lock:
        try:
            df_live = pd.read_excel(domain.live_excel_path).fillna("")
            df_live["Primary Key"] = df_live["Primary Key"].astype(str)

            row = df_live[df_live["Primary Key"] == item_id].copy()
            if row.empty:
                print(f"[!] {domain.name.capitalize()} {item_id} not found in live Excel.")
                return

            if os.path.exists(path):
                df_saved = pd.read_excel(path).fillna("")
                df_saved["Primary Key"] = df_saved["Primary Key"].astype(str)
                if item_id in df_saved["Primary Key"].values:
                    return
                df_saved = pd.concat([df_saved, row], ignore_index=True)
            else:
                df_saved = row

            df_saved.to_excel(path, index=False)
            print(f"[✓] {domain.name.capitalize()} {item_id} saved by {founder_name} -> {os.path.basename(path)}")
        except Exception as e:
            print(f"[✕] Failed to save {domain.name} {item_id} for {founder_name}: {e}")


def remove_item_from_excel(domain: Domain, item_id: str, founder_name: str):
    path = saved_path_for(domain, founder_name)
    if not os.path.exists(path):
        return
    with domain.saved_lock:
        try:
            df = pd.read_excel(path).fillna("")
            df["Primary Key"] = df["Primary Key"].astype(str)
            before = len(df)
            df = df[df["Primary Key"] != item_id]
            if len(df) == before:
                return
            df.to_excel(path, index=False)
            print(f"[✓] {domain.name.capitalize()} {item_id} unstarred by {founder_name} -> {os.path.basename(path)}")
        except Exception as e:
            print(f"[✕] Failed to remove {domain.name} {item_id} for {founder_name}: {e}")


def merge_saved_items(domain: Domain) -> list:
    merged = {}
    order = []
    for path in list_saved_files(domain):
        founder = _founder_from_path(path)
        try:
            df = pd.read_excel(path).fillna("")
        except Exception as e:
            print(f"[!] Could not read {os.path.basename(path)}: {e}")
            continue
        if "Primary Key" not in df.columns:
            continue
        df["Primary Key"] = df["Primary Key"].astype(str)
        for _, row in df.iterrows():
            tid = str(row.get("Primary Key"))
            if tid not in merged:
                merged[tid] = {"row": row, "starredBy": []}
                order.append(tid)
            if founder not in merged[tid]["starredBy"]:
                merged[tid]["starredBy"].append(founder)
    return [(tid, merged[tid]["row"], merged[tid]["starredBy"]) for tid in order]


def _migrate_legacy_saved(domain: Domain):
    """One-time, non-destructive split of an old single saved_<plural>.xlsx (with
    a 'Starred By' column) into per-founder files. Skips if any per-founder file
    already exists."""
    legacy = os.path.join(domain.data_dir, domain.legacy_saved_name)
    if not os.path.exists(legacy) or list_saved_files(domain):
        return
    try:
        df = pd.read_excel(legacy).fillna("")
        if "Primary Key" not in df.columns:
            return
        by_founder = {}
        for _, row in df.iterrows():
            names = [n.strip() for n in str(row.get("Starred By", "")).split(",") if n.strip()]
            if not names:
                names = ["unknown"]
            clean = row.drop(labels=["Starred By"], errors="ignore")
            for name in names:
                by_founder.setdefault(name, []).append(clean)
        for name, rows in by_founder.items():
            pd.DataFrame(rows).to_excel(saved_path_for(domain, name), index=False)
        print(f"[✓] Migrated legacy {domain.legacy_saved_name} into {len(by_founder)} per-founder file(s).")
    except Exception as e:
        print(f"[!] Legacy saved migration skipped: {e}")


def register_domain_routes(app, domain: Domain):
    _migrate_legacy_saved(domain)
    domain.saved_ids_db = load_saved_ids(domain)
    print(f"[✓] Loaded {len(domain.saved_ids_db)} saved {domain.plural} across "
          f"{len(list_saved_files(domain))} founder file(s). Data dir: {domain.data_dir}")

    prefix = f"/api/{domain.plural}"

    @app.route(f'{prefix}/sensio-stream', methods=['GET'], endpoint=f'stream_{domain.plural}')
    def stream_excel_data():
        excel_path = domain.live_excel_path
        if not os.path.exists(excel_path):
            return jsonify({"error": f"{excel_path} file not found locally", domain.plural: []}), 200

        saved_ids_now = load_saved_ids(domain)
        domain.saved_ids_db.clear()
        domain.saved_ids_db.update(saved_ids_now)

        try:
            df = pd.read_excel(excel_path).fillna("")
            items_pool = []
            for idx, row in df.iterrows():
                item_id = str(row.get("Primary Key", f"{domain.name.upper()}-{idx}"))
                title = row.get(domain.title_col, f"Untitled {domain.name.capitalize()}")
                category = str(row.get("Sector", "health")).strip().lower()
                country = row.get("Country", "Global")
                opening_date = str(row.get("Opening date", "N/A"))
                closing_date = str(row.get("Closing date", "N/A"))

                try:
                    relevancy_score = float(row.get("Relevancy Score", 0.50))
                except Exception:
                    relevancy_score = 0.50

                try:
                    budget_inr = int(re.sub(r'[^\d]', '', str(row.get("INR Budget Maximum", 0))))
                except Exception:
                    budget_inr = 0

                description = row.get("Description", "")
                eligibility = f"Authority: {row.get('Organisation name', 'Unknown')}"
                link = row.get(domain.url_col, "https://google.com")
                if isinstance(link, dict) and "text" in link:
                    link = link["text"]

                items_pool.append({
                    "id": item_id,
                    "title": title,
                    "category": category,
                    "country": country,
                    "openingDate": opening_date,
                    "closingDate": closing_date,
                    "relevancyScore": relevancy_score / 10 if relevancy_score > 1.0 else relevancy_score,
                    "budgetINR": budget_inr,
                    "description": description,
                    "eligibility": eligibility,
                    "link": link,
                })

                days_left = calculate_days_remaining(closing_date)

                if relevancy_score >= 9.0 or relevancy_score == 1.0:
                    alert_key = f"{item_id}_relevancy"
                    if alert_key not in domain.notified_alerts:
                        display_score = f"{relevancy_score * 100:.0f}" if relevancy_score <= 1.0 else f"{relevancy_score * 10:.0f}"
                        subject = f"🔥 High Relevancy Match Found: {title}"
                        html = f"""
                        <div style="font-family: sans-serif; padding: 20px; border: 1px solid #0d9488; border-radius: 8px; max-width:600px;">
                            <h2 style="color: #0d9488; margin-top:0;">Sensio Target Acquisition</h2>
                            <p>An exceptional {domain.name} matching your operational blueprint has been scanned at <strong>{display_score}% Weight</strong>.</p>
                            <hr style="border:none; border-top:1px solid #cbd5e1; margin:16px 0;"/>
                            <p><strong>{domain.name.capitalize()} ID:</strong> {item_id}</p>
                            <p><strong>Title:</strong> {title}</p>
                            <p><strong>Sector:</strong> {category.upper()}</p>
                            <br/>
                            <a href="{link}" target="_blank" style="background: #0d9488; color: white; padding: 10px 16px; text-decoration: none; border-radius: 6px; font-weight:600; display:inline-block;">Access Portal</a>
                        </div>
                        """
                        if send_gmail_notification(subject, html):
                            domain.notified_alerts.add(alert_key)

                if item_id in domain.saved_ids_db and 0 <= days_left <= 7:
                    alert_key = f"{item_id}_expiry"
                    if alert_key not in domain.notified_alerts:
                        subject = f"⚠️ Critical Timeline Warning: Watchlist {domain.name.capitalize()} closes in {days_left} days!"
                        html = f"""
                        <div style="font-family: sans-serif; padding: 20px; border: 1px solid #dc2626; border-radius: 8px; max-width:600px;">
                            <h2 style="color: #dc2626; margin-top:0;">Sensio Framework Urgent Exception</h2>
                            <p>Action is required. A saved watchlist {domain.name} is approaching its final expiration threshold.</p>
                            <hr style="border:none; border-top:1px solid #cbd5e1; margin:16px 0;"/>
                            <p><strong>{domain.name.capitalize()} ID:</strong> {item_id}</p>
                            <p><strong>Title:</strong> {title}</p>
                            <p><strong>Time Remaining:</strong> <span style="color:#dc2626; font-weight:700;">{days_left} Days Left</span></p>
                        </div>
                        """
                        if send_gmail_notification(subject, html):
                            domain.notified_alerts.add(alert_key)

            return jsonify({
                "sourceFile": f"Local Excel Engine ({domain.excel_basename})",
                domain.plural: items_pool,
            }), 200
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
            save_item_to_excel(domain, item_id, founder_name)
        else:
            remove_item_from_excel(domain, item_id, founder_name)

        fresh = load_saved_ids(domain)
        domain.saved_ids_db.clear()
        domain.saved_ids_db.update(fresh)

        return jsonify({"status": "success", "savedCount": len(domain.saved_ids_db)}), 200

    @app.route(f'{prefix}/saved-{domain.plural}', methods=['GET'], endpoint=f'get_saved_{domain.plural}')
    def get_saved_items():
        try:
            items = []
            for item_id, row, starred_by in merge_saved_items(domain):
                link = row.get(domain.url_col, "https://google.com")
                if isinstance(link, dict) and "text" in link:
                    link = link["text"]
                try:
                    budget_inr = int(re.sub(r'[^\d]', '', str(row.get("INR Budget Maximum", 0))))
                except Exception:
                    budget_inr = 0
                try:
                    relevancy_score = float(row.get("Relevancy Score", 0.50))
                except Exception:
                    relevancy_score = 0.50
                items.append({
                    "id": item_id,
                    "title": row.get(domain.title_col, f"Untitled {domain.name.capitalize()}"),
                    "category": str(row.get("Sector", "health")).strip().lower(),
                    "country": row.get("Country", "Global"),
                    "openingDate": str(row.get("Opening date", "N/A")),
                    "closingDate": str(row.get("Closing date", "N/A")),
                    "relevancyScore": relevancy_score / 10 if relevancy_score > 1.0 else relevancy_score,
                    "budgetINR": budget_inr,
                    "description": row.get("Description", ""),
                    "eligibility": f"Authority: {row.get('Organisation name', 'Unknown')}",
                    "link": link,
                    "starredBy": ", ".join(starred_by),
                    "saved": True,
                })
            return jsonify({domain.plural: items}), 200
        except Exception as e:
            return jsonify({"error": str(e), domain.plural: []}), 500

    @app.route(f'{prefix}/saved-ids', methods=['GET'], endpoint=f'get_saved_ids_{domain.plural}')
    def get_saved_ids():
        founder = request.args.get('founder', '').strip()
        if not founder:
            return jsonify({"savedIds": []}), 200
        return jsonify({"savedIds": list(load_saved_ids_for(domain, founder))}), 200


for domain in DOMAINS.values():
    register_domain_routes(app, domain)


@app.route('/api/shutdown', methods=['POST'])
def shutdown_app():
    # Cleanly stop this local instance from the dashboard's "Quit App" button.
    # We send the HTTP response first, then hard-exit from a background thread a
    # beat later so the reply actually reaches the browser (current Werkzeug has
    # no in-request shutdown hook).
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
