"""
usage_logger.py — Google Sheets usage logging for My Preaching Coach

Non-critical: any failure prints a warning but does NOT crash the analysis.
Delete this file and remove the import from sermon_analyze.py to disable logging.
"""
import os
import traceback
from datetime import datetime
from pathlib import Path

SPREADSHEET_ID = "1ljtP0O8uUZmJKNOAiwhrkdVRdTsp1EIla8doLkNh53s"
SHEET_NAME     = "Log"

HEADERS = [
    "timestamp", "preacher_name", "email", "source_type", "source_value",
    "sermon_type", "duration_min", "word_count", "whisper_chunks",
    "whisper_cost_usd", "claude_input_tokens", "claude_output_tokens",
    "claude_cost_usd", "total_cost_usd", "processing_time_sec",
    "overall_score", "gospel_check_total", "gold_standard_flag",
    "incomplete_flag", "success", "error_message",
]

# Candidate paths for the service account JSON (tried in order after env var check)
_SA_CANDIDATES = [
    os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", ""),           # explicit file-path env var
    "/app/service_account.json",                                  # Railway volume
    str(Path(__file__).parent / "service_account.json"),         # same dir as this script
    str(Path.home() / "Desktop" / "MyPreachingCoach" / "service_account.json"),  # local CLI
]


def _find_service_account() -> str | None:
    """
    Return a path to a valid service account JSON file, or None.

    Checks GOOGLE_SA_JSON_B64 first: if set, decodes the base64 value,
    writes it to /tmp/service_account.json, and returns that path.
    Falls back to the file-path candidates in _SA_CANDIDATES.
    """
    b64 = os.environ.get("GOOGLE_SA_JSON_B64", "").strip()
    if b64:
        try:
            import base64, json as _json
            decoded = base64.b64decode(b64)
            _json.loads(decoded)          # validate before writing
            tmp_path = "/tmp/service_account.json"
            with open(tmp_path, "wb") as fh:
                fh.write(decoded)
            return tmp_path
        except Exception as e:
            print(f"[usage_logger] WARNING: GOOGLE_SA_JSON_B64 decode failed — {e}")

    for path in _SA_CANDIDATES:
        if path and os.path.isfile(path):
            return path
    return None


def log_sermon_run(fields: dict) -> None:
    """
    Append one row to the Google Sheets usage log.
    Writes a header row first if the sheet is empty.
    Prints a warning on any failure — never raises.
    """
    try:
        # ── Build timestamp in Pacific time ──────────────────────────────────
        try:
            from zoneinfo import ZoneInfo
            ts = datetime.now(ZoneInfo("America/Los_Angeles")).isoformat(timespec="seconds")
        except Exception:
            ts = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        fields.setdefault("timestamp", ts)

        # ── Locate service account ────────────────────────────────────────────
        sa_path = _find_service_account()
        if not sa_path:
            print("[usage_logger] WARNING: service_account.json not found — skipping log")
            return

        # ── Connect to Google Sheets ──────────────────────────────────────────
        import gspread
        from google.oauth2.service_account import Credentials

        creds = Credentials.from_service_account_file(
            sa_path,
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(SPREADSHEET_ID)

        # Get or create the Log worksheet
        try:
            ws = sh.worksheet(SHEET_NAME)
        except gspread.exceptions.WorksheetNotFound:
            ws = sh.add_worksheet(title=SHEET_NAME, rows=5000, cols=len(HEADERS))

        # Write header if sheet is empty or header row is missing/wrong
        existing = ws.get_all_values()
        if not existing or existing[0] != HEADERS:
            ws.insert_row(HEADERS, index=1)

        # ── Build the row in header order ─────────────────────────────────────
        row = []
        for h in HEADERS:
            val = fields.get(h, "")
            if val is None:
                val = ""
            elif isinstance(val, bool):
                val = str(val)
            row.append(str(val))

        # Print what we're logging so it's visible in Railway/terminal output
        summary = {
            "preacher": fields.get("preacher_name", ""),
            "source_type": fields.get("source_type", ""),
            "duration_min": fields.get("duration_min", ""),
            "total_cost_usd": fields.get("total_cost_usd", ""),
            "success": fields.get("success", ""),
        }
        print(f"[usage_logger] Logging row: {summary}")

        ws.append_row(row, value_input_option="USER_ENTERED")
        print("[usage_logger] Row appended to Google Sheet successfully.")

    except Exception:
        print(f"[usage_logger] WARNING: Failed to log to Google Sheets:")
        traceback.print_exc()
        # Do NOT re-raise — logging is non-critical
