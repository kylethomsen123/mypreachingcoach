#!/usr/bin/env python3.11
"""
send_followups.py — My Preaching Coach follow-up email system

Runs once daily via cron. Reads the Google Sheets usage log, checks who
needs a follow-up, checks whether they already submitted feedback via the
Google Form, and sends the appropriate email via SendGrid.

Touch 2 — sent on Day 3 after the report was delivered
Touch 3 — sent on Day 7 after the report was delivered

If feedback is received at any point, all future touches are suppressed.

Usage: python3.11 /Users/Kyle_1/Desktop/MyPreachingCoach/send_followups.py
"""

import os
import sys
import traceback
from datetime import datetime, timedelta
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent

# Search for .env in likely locations (Desktop dir → git repo → home)
def _find_env() -> Path:
    candidates = [
        SCRIPT_DIR / "web" / ".env",            # ~/Desktop/MyPreachingCoach/web/.env
        SCRIPT_DIR / ".env",                     # ~/Desktop/MyPreachingCoach/.env
        Path.home() / "MyPreachingCoach" / "web" / ".env",   # git repo
    ]
    for p in candidates:
        if p.exists():
            return p
    return candidates[0]   # return first candidate even if missing (will fail silently)

WEB_ENV_PATH = _find_env()
SA_PATH      = SCRIPT_DIR / "service_account.json"

USAGE_SPREADSHEET_ID = "1ljtP0O8uUZmJKNOAiwhrkdVRdTsp1EIla8doLkNh53s"
USAGE_SHEET_NAME     = "Log"
FORM_SPREADSHEET_ID  = "17fNyFHQLW3qdBsfTzkVuJ_W_MO9xKRjt1Iccu217f88"

FEEDBACK_URL = (
    "https://docs.google.com/forms/d/e/"
    "1FAIpQLScVak1fcv8sgEpYgeWDYjwlAAZyXIDeKYwqvc6lWmk7ndL1Vw/viewform"
)

# Columns added to the usage log for tracking follow-ups
NEW_COLUMNS = ["followup_2_sent", "followup_3_sent", "feedback_received"]

TOUCH_2_DAYS = 2.5   # send Touch 2 ~3 days after report (0.5-day buffer for cron timing)
TOUCH_3_DAYS = 6.5   # send Touch 3 ~7 days after report (0.5-day buffer for cron timing)


# ── Environment loading ────────────────────────────────────────────────────────
def _load_env(path: Path) -> None:
    """Load key=value lines from a .env file into os.environ."""
    try:
        from dotenv import load_dotenv
        load_dotenv(path)
        return
    except ImportError:
        pass
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip())


# ── Time helpers ───────────────────────────────────────────────────────────────
def _now() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Los_Angeles"))
    except Exception:
        return datetime.now()


def _parse_ts(ts_str: str) -> datetime | None:
    """Parse ISO 8601 timestamp string to an aware datetime."""
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str)
    except ValueError:
        return None


def _days_since(ts: datetime, now: datetime) -> float:
    """Return fractional days between ts and now, handling tz-naive fallback."""
    try:
        if ts.tzinfo is None:
            delta = now.replace(tzinfo=None) - ts
        else:
            delta = now - ts
        return delta.total_seconds() / 86400
    except Exception:
        return 0.0


# ── Google Sheets connection ───────────────────────────────────────────────────
def _sheets_client():
    """Return an authorised gspread client."""
    import gspread
    from google.oauth2.service_account import Credentials

    if not SA_PATH.exists():
        raise FileNotFoundError(f"service_account.json not found at {SA_PATH}")

    creds = Credentials.from_service_account_file(
        str(SA_PATH),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return gspread.authorize(creds)


# ── Usage log helpers ──────────────────────────────────────────────────────────
def _ensure_tracking_columns(ws) -> list:
    """
    Ensure NEW_COLUMNS exist in the usage log header row.
    Appends any missing ones and returns the full header list.
    """
    all_values = ws.get_all_values()
    if not all_values:
        return []

    header = all_values[0]
    added  = []

    for col_name in NEW_COLUMNS:
        if col_name not in header:
            header.append(col_name)
            added.append(col_name)

    if added:
        # Write the updated header back (row 1 only)
        ws.update(range_name="1:1", values=[header])
        print(f"[sheets] Added tracking columns: {added}")

    return header


# ── Form responses ─────────────────────────────────────────────────────────────
def _get_responded_emails(gc) -> set:
    """
    Return a set of lowercase email addresses that have submitted the form.
    Searches form responses spreadsheet for a column whose header contains 'email'.
    """
    try:
        sh = gc.open_by_key(FORM_SPREADSHEET_ID)
        ws = sh.get_worksheet(0)   # first sheet, whatever it's called
        rows = ws.get_all_values()
        if not rows:
            return set()

        header = [h.lower().strip() for h in rows[0]]

        # Find email column: look for 'email address' first, then 'email'
        email_col = None
        for i, h in enumerate(header):
            if h in ("email address", "email"):
                email_col = i
                break
        if email_col is None:
            for i, h in enumerate(header):
                if "email" in h:
                    email_col = i
                    break

        if email_col is None:
            print("[forms] WARNING: could not find email column in form responses")
            return set()

        emails = set()
        for row in rows[1:]:
            if len(row) > email_col and row[email_col].strip():
                emails.add(row[email_col].strip().lower())

        print(f"[forms] {len(emails)} form responses found")
        return emails

    except Exception as e:
        print(f"[forms] WARNING: could not read form responses — {e}")
        return set()


# ── SendGrid email ─────────────────────────────────────────────────────────────
def _send_email(to_email: str, subject: str, plain: str, html: str) -> bool:
    """Send a single email via SendGrid. Returns True on success."""
    api_key    = os.getenv("SENDGRID_API_KEY", "")
    from_email = os.getenv("FROM_EMAIL", "")

    if not api_key or not from_email:
        print(f"  [email] SKIP — SENDGRID_API_KEY or FROM_EMAIL not set")
        return False

    try:
        import sendgrid as sg_module
        from sendgrid.helpers.mail import Mail

        message = Mail(
            from_email=from_email,
            to_emails=to_email,
            subject=subject,
            plain_text_content=plain,
            html_content=html,
        )
        client   = sg_module.SendGridAPIClient(api_key)
        response = client.send(message)
        print(f"  [email] Sent to {to_email} — HTTP {response.status_code}")
        return True
    except Exception as e:
        print(f"  [email] FAILED for {to_email}: {e}")
        return False


# ── Email copy ────────────────────────────────────────────────────────────────
def _touch2_email(name: str) -> tuple[str, str, str]:
    greeting = name or "there"
    subject  = "Quick thought on your sermon report"

    plain = f"""\
Hi {greeting},

Hope you\u2019ve had a chance to look through your report. One thing I keep noticing across the sermons I\u2019ve analyzed \u2014 the Gospel Check section tends to surprise people the most. Not because the scores are low, but because it makes visible something that\u2019s easy to miss: how clearly the sermon connects the listener to what Jesus has already done, rather than just what they should go do.

Did anything in the report catch you off guard or make you think differently about the sermon?

I\u2019m building this tool specifically for preachers who want to grow, and your perspective is shaping what it becomes. If you have 2 minutes, your feedback would mean a lot:
\U0001f449 {FEEDBACK_URL}

No pressure either way \u2014 I\u2019m grateful you gave it a try.

Kyle
"""

    html = f"""\
<p>Hi {greeting},</p>

<p>Hope you\u2019ve had a chance to look through your report. One thing I keep noticing across the sermons I\u2019ve analyzed \u2014 the Gospel Check section tends to surprise people the most. Not because the scores are low, but because it makes visible something that\u2019s easy to miss: how clearly the sermon connects the listener to what Jesus has already done, rather than just what they should <em>go do</em>.</p>

<p>Did anything in the report catch you off guard or make you think differently about the sermon?</p>

<p>I\u2019m building this tool specifically for preachers who want to grow, and your perspective is shaping what it becomes. If you have 2 minutes, your feedback would mean a lot:<br>
\U0001f449 <a href="{FEEDBACK_URL}">{FEEDBACK_URL}</a></p>

<p>No pressure either way \u2014 I\u2019m grateful you gave it a try.</p>

<p>Kyle</p>
"""
    return subject, plain, html


def _touch3_email(name: str) -> tuple[str, str, str]:
    greeting = name or "there"
    subject  = "Last ask \u2014 2 minutes to shape this tool"

    plain = f"""\
Hi {greeting},

Last note from me on this \u2014 promise.

If you have 2 minutes, your honest feedback helps me make this tool better for every pastor who uses it after you. What\u2019s helpful, what\u2019s not, what\u2019s missing \u2014 all of it matters.

\U0001f449 {FEEDBACK_URL}

Either way, thank you for being one of the first people to try My Preaching Coach. It means more than you know.

Kyle
"""

    html = f"""\
<p>Hi {greeting},</p>

<p>Last note from me on this \u2014 promise.</p>

<p>If you have 2 minutes, your honest feedback helps me make this tool better for every pastor who uses it after you. What\u2019s helpful, what\u2019s not, what\u2019s missing \u2014 all of it matters.</p>

<p>\U0001f449 <a href="{FEEDBACK_URL}">{FEEDBACK_URL}</a></p>

<p>Either way, thank you for being one of the first people to try My Preaching Coach. It means more than you know.</p>

<p>Kyle</p>
"""
    return subject, plain, html


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    _load_env(WEB_ENV_PATH)

    now = _now()
    print(f"[followups] Running at {now.isoformat(timespec='seconds')}\n")

    # ── Connect to Sheets ──────────────────────────────────────────────────────
    try:
        gc = _sheets_client()
    except Exception as e:
        print(f"[followups] ERROR: Could not connect to Google Sheets — {e}")
        sys.exit(1)

    # ── Usage log ──────────────────────────────────────────────────────────────
    try:
        usage_sh = gc.open_by_key(USAGE_SPREADSHEET_ID)
        usage_ws = usage_sh.worksheet(USAGE_SHEET_NAME)
    except Exception as e:
        print(f"[followups] ERROR: Could not open usage log — {e}")
        sys.exit(1)

    header = _ensure_tracking_columns(usage_ws)
    if not header:
        print("[followups] Usage log is empty — nothing to do.")
        return

    # Build column index lookups (1-based for gspread)
    col = {name: idx + 1 for idx, name in enumerate(header)}

    def need(col_name: str) -> int:
        if col_name not in col:
            raise KeyError(f"Column '{col_name}' not found in usage log header")
        return col[col_name]

    # ── Read all usage log data ────────────────────────────────────────────────
    all_rows = usage_ws.get_all_values()
    data_rows = all_rows[1:]   # skip header

    # ── Form responses (emails that already submitted) ─────────────────────────
    responded_emails = _get_responded_emails(gc)

    # ── Process each row ───────────────────────────────────────────────────────
    # Accumulate sheet updates as (sheet_row_1based, col_1based, value)
    sheet_updates: list[tuple[int, int, str]] = []

    sent_count  = 0
    skip_count  = 0
    error_count = 0

    for row_idx_0, row in enumerate(data_rows):
        sheet_row = row_idx_0 + 2   # 1-based, header is row 1

        def get(col_name: str) -> str:
            idx = col.get(col_name)
            if idx is None:
                return ""
            i = idx - 1   # 0-based
            return row[i].strip() if i < len(row) else ""

        # Only process successful rows with a real email
        if get("success").lower() not in ("true", "1", "yes"):
            continue
        email = get("email").strip()
        if not email or "@" not in email:
            continue

        name                = get("preacher_name")
        ts_str              = get("timestamp")
        feedback_received   = get("feedback_received")
        followup_2_sent     = get("followup_2_sent")
        followup_3_sent     = get("followup_3_sent")

        email_lower = email.lower()

        # ── Check form responses ───────────────────────────────────────────────
        if email_lower in responded_emails and feedback_received != "yes":
            print(f"  [row {sheet_row}] {email} — feedback received, marking sheet")
            sheet_updates.append((sheet_row, need("feedback_received"), "yes"))
            skip_count += 1
            continue

        # ── Skip if feedback already recorded ─────────────────────────────────
        if feedback_received == "yes":
            skip_count += 1
            continue

        # ── Calculate age ──────────────────────────────────────────────────────
        ts = _parse_ts(ts_str)
        if ts is None:
            continue
        age_days = _days_since(ts, now)

        # ── Touch 3 (Day 7) ───────────────────────────────────────────────────
        if age_days >= TOUCH_3_DAYS and not followup_3_sent:
            print(f"  [row {sheet_row}] {email} — sending Touch 3 (day {age_days:.1f})")
            subj, plain, html = _touch3_email(name)
            ok = _send_email(email, subj, plain, html)
            if ok:
                ts_sent = now.isoformat(timespec="seconds")
                sheet_updates.append((sheet_row, need("followup_3_sent"), ts_sent))
                sent_count += 1
            else:
                error_count += 1
            continue   # don't also send Touch 2 if Touch 3 is due

        # ── Touch 2 (Day 3) ───────────────────────────────────────────────────
        if age_days >= TOUCH_2_DAYS and not followup_2_sent:
            print(f"  [row {sheet_row}] {email} — sending Touch 2 (day {age_days:.1f})")
            subj, plain, html = _touch2_email(name)
            ok = _send_email(email, subj, plain, html)
            if ok:
                ts_sent = now.isoformat(timespec="seconds")
                sheet_updates.append((sheet_row, need("followup_2_sent"), ts_sent))
                sent_count += 1
            else:
                error_count += 1
            continue

        # Nothing to do for this row yet
        skip_count += 1

    # ── Apply sheet updates ────────────────────────────────────────────────────
    if sheet_updates:
        from gspread.utils import rowcol_to_a1
        batch = [
            {"range": f"{rowcol_to_a1(r, c)}:{rowcol_to_a1(r, c)}", "values": [[v]]}
            for r, c, v in sheet_updates
        ]
        try:
            usage_ws.batch_update(batch)
            print(f"\n[sheets] Applied {len(sheet_updates)} cell update(s)")
        except Exception as e:
            print(f"\n[sheets] WARNING: batch update failed — {e}")
            traceback.print_exc()

    # ── Summary ────────────────────────────────────────────────────────────────
    print(f"\n{'='*50}")
    print(f"Sent {sent_count} follow-up(s), "
          f"skipped {skip_count} (no action needed / feedback received), "
          f"{error_count} error(s)")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()

# ── Cron setup ────────────────────────────────────────────────────────────────
# To run this script daily at 10:00 AM Pacific time, add this line to your crontab:
#
# 1. Open your crontab:
#      crontab -e
#
# 2. Paste this line (10 AM Pacific — adjust UTC offset for DST: -7 in summer, -8 in winter):
#      0 17 * * * /usr/local/bin/python3.11 /Users/Kyle_1/Desktop/MyPreachingCoach/send_followups.py >> /Users/Kyle_1/Desktop/MyPreachingCoach/followup_log.txt 2>&1
#
#    Note: 17:00 UTC = 10:00 AM PDT (UTC-7). During PST (Nov–Mar), change to 18:00 UTC.
#    Or install the 'tzdata' package and use:
#      0 10 * * * TZ=America/Los_Angeles /usr/local/bin/python3.11 /Users/Kyle_1/Desktop/MyPreachingCoach/send_followups.py >> /Users/Kyle_1/Desktop/MyPreachingCoach/followup_log.txt 2>&1
#
# 3. Save and exit (in nano: Ctrl+O, Enter, Ctrl+X)
#
# Verify cron is running:
#      crontab -l
