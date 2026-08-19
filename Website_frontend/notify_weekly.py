"""Weekly "dashboard is live" reminder email. Run: python3 Website_frontend/notify_weekly.py

Sender/recipient/dashboard URL are env vars, not hardcoded, so swapping the
sending account (e.g. when repo ownership transfers) or the recipient list is
just updating GitHub Actions secrets -- no code change. Gmail SMTP needs an
App Password for SENDER_EMAIL, not its regular password (Google Account ->
Security -> 2-Step Verification -> App Passwords).
"""
import os
import smtplib
import sys
from datetime import datetime
from email.mime.text import MIMEText

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from database import db

SENDER_EMAIL = os.environ["SENDER_EMAIL"]
SENDER_APP_PASSWORD = os.environ["SENDER_APP_PASSWORD"]
RECIPIENT_EMAILS = [e.strip() for e in os.environ["RECIPIENT_EMAILS"].split(",") if e.strip()]
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "").strip()


def _is_open(closing_date: str) -> bool:
    """Best-effort: closing_date is usually YYYY-MM-DD (datasetManager.py
    normalises it at scrape time) -- treat anything unparseable as open
    rather than hiding it."""
    try:
        return datetime.strptime(str(closing_date).strip(), "%Y-%m-%d").date() >= datetime.now().date()
    except (ValueError, TypeError):
        return True


def top_item(domain: str):
    """Highest relevancy_score row that's still open, or None if the table's
    empty. Falls back to the single highest-scored row if every one of them
    happens to be closed, rather than showing nothing."""
    rows = db.load_items(domain)
    if not rows:
        return None
    rows.sort(key=lambda r: r.get("relevancy_score") or 0, reverse=True)
    for row in rows:
        if _is_open(row.get("closing_date")):
            return row
    return rows[0]


def _line(label: str, row) -> str:
    if not row:
        return f"{label}: nothing in the pipeline yet."
    score = row.get("relevancy_score") or 0
    score_10 = score * 10 if score <= 1.0 else score
    return f"{label}: {row['title']} ({score_10:.1f}/10) — {row.get('url') or ''}"


def build_body() -> str:
    lines = []
    if DASHBOARD_URL:
        lines.append(f"dashboard is live at {DASHBOARD_URL}")
    else:
        lines.append("dashboard is live")
    lines.append("")
    lines.append(_line("Top tender this week", top_item("tenders")))
    lines.append(_line("Top grant this week", top_item("grants")))
    lines.append("")
    if DASHBOARD_URL:
        lines.append(f"Full list (and everything else worth a look) is here: {DASHBOARD_URL}")
    return "\n".join(lines)


def main():
    msg = MIMEText(build_body())
    msg["Subject"] = "weekly tender&grant dashboard is live!"
    msg["From"] = SENDER_EMAIL
    msg["To"] = ", ".join(RECIPIENT_EMAILS)

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(SENDER_EMAIL, SENDER_APP_PASSWORD)
        server.sendmail(SENDER_EMAIL, RECIPIENT_EMAILS, msg.as_string())

    print(f"[OK] Sent to {', '.join(RECIPIENT_EMAILS)}")


if __name__ == "__main__":
    main()
