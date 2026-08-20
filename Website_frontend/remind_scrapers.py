"""Weekly reminder to run the scrapers locally. Run: python3 Website_frontend/remind_scrapers.py

Local execution (executables/run_tenders.command / run_grants.command) is the
recommended way to scrape -- see HANDOFF.md's "Local execution vs. full CI
automation" for why. That only works if someone actually double-clicks them,
so this is a standing nudge, not a data report -- no Postgres involved here,
unlike notify_weekly.py. Same sender/recipient secrets as that script.
"""
import os
import smtplib
from email.mime.text import MIMEText

SENDER_EMAIL = os.environ["SENDER_EMAIL"]
SENDER_APP_PASSWORD = os.environ["SENDER_APP_PASSWORD"]
RECIPIENT_EMAILS = [e.strip() for e in os.environ["RECIPIENT_EMAILS"].split(",") if e.strip()]

BODY = """It's that time of the week -- run the scrapers so this weekend's tenders and grants are fresh.

Double-click, on whichever machine has Claude Code already logged in:
  executables/run_tenders.command   (or run_tenders.bat on Windows)
  executables/run_grants.command    (or run_grants.bat on Windows)

Each one installs whatever it needs and runs on its own -- no need to babysit it.
"""


def main():
    msg = MIMEText(BODY)
    msg["Subject"] = "reminder: run this week's tender & grant scrapers"
    msg["From"] = SENDER_EMAIL
    msg["To"] = ", ".join(RECIPIENT_EMAILS)

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(SENDER_EMAIL, SENDER_APP_PASSWORD)
        server.sendmail(SENDER_EMAIL, RECIPIENT_EMAILS, msg.as_string())

    print(f"[OK] Sent to {', '.join(RECIPIENT_EMAILS)}")


if __name__ == "__main__":
    main()
