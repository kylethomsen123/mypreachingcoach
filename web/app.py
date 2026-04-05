"""
app.py — My Preaching Coach web app
Run: python3 app.py
Then open: http://localhost:5050
"""

import base64
import os
import shutil
import sys
import threading
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from flask import Flask, redirect, render_template, request, url_for

# Load .env from the same directory as this file
load_dotenv(Path(__file__).parent / ".env")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # 500 MB upload limit

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR         = Path(__file__).parent.parent          # ~/Desktop/MyPreachingCoach/
SCRIPT           = BASE_DIR / "sermon_analyze.py"
REPORTS_PERSONAL = BASE_DIR / "reports" / "personal"
REPORTS_BETA     = BASE_DIR / "reports" / "beta"

REPORTS_BETA.mkdir(parents=True, exist_ok=True)

# Accepted audio/video extensions
ALLOWED_EXTENSIONS = {
    ".mp3", ".mp4", ".m4a", ".wav", ".flac",
    ".aac", ".ogg", ".webm", ".mov",
}


# ── Email ──────────────────────────────────────────────────────────────────────
def send_report_email(to_email: str, preacher_name: str, pdf_path: str):
    """Send the finished PDF report via SendGrid."""
    import sendgrid as sg_module
    from sendgrid.helpers.mail import (
        Attachment, Disposition, FileContent, FileName,
        FileType, Mail,
    )

    api_key      = os.getenv("SENDGRID_API_KEY", "")
    from_email   = os.getenv("FROM_EMAIL", "mypreachingcoach@yourdomain.com")
    feedback_url = os.getenv("FEEDBACK_FORM_URL", "https://your-google-form-link-here")

    if not api_key:
        print("[email] SENDGRID_API_KEY not set — skipping email.")
        return

    # Build a readable sermon title from the filename for the subject line
    stem         = Path(pdf_path).stem                       # sermon_eval_Title_Name
    subject_slug = stem.replace("sermon_eval_", "").replace("_", " ").strip()
    subject      = f"Your Preaching Coach Report is ready — {subject_slug}"

    plain_body = f"""\
Hi {preacher_name},

Your sermon report is attached. Here's what's inside:
- Sermon structure analysis (ME/WE/GOD/YOU/WE)
- Vocal delivery scores (measured from audio)
- Gospel Check
- Full rubric with scores

A few things to know:
- Scores are meant to coach, not judge
- The vocal analysis is measured directly from the audio — not guessed
- Body language and note-reliance require in-person observation — \
those lines are left blank for you or a mentor to fill in

---

This is a free beta and I'd love your honest feedback.
It takes 2 minutes: {feedback_url}

Or just reply to this email — I read every response.

— Kyle Thomsen
My Preaching Coach
"""

    html_body = f"""\
<p>Hi {preacher_name},</p>

<p>Your sermon report is attached. Here's what's inside:</p>
<ul>
  <li>Sermon structure analysis (ME/WE/GOD/YOU/WE)</li>
  <li>Vocal delivery scores (measured from audio)</li>
  <li>Gospel Check</li>
  <li>Full rubric with scores</li>
</ul>

<p><strong>A few things to know:</strong></p>
<ul>
  <li>Scores are meant to coach, not judge</li>
  <li>The vocal analysis is measured directly from the audio — not guessed</li>
  <li>Body language and note-reliance require in-person observation —
      those lines are left blank for you or a mentor to fill in</li>
</ul>

<hr>

<p>This is a free beta and I'd love your honest feedback.<br>
It takes 2 minutes: <a href="{feedback_url}">{feedback_url}</a></p>

<p>Or just reply to this email — I read every response.</p>

<p>— Kyle Thomsen<br><em>My Preaching Coach</em></p>
"""

    message = Mail(
        from_email=from_email,
        to_emails=to_email,
        subject=subject,
        plain_text_content=plain_body,
        html_content=html_body,
    )

    # Attach the PDF
    with open(pdf_path, "rb") as f:
        encoded = base64.b64encode(f.read()).decode()

    message.attachment = Attachment(
        FileContent(encoded),
        FileName(Path(pdf_path).name),
        FileType("application/pdf"),
        Disposition("attachment"),
    )

    client   = sg_module.SendGridAPIClient(api_key)
    response = client.send(message)
    print(f"[email] Sent to {to_email} — HTTP {response.status_code}")


# ── Background job ─────────────────────────────────────────────────────────────
def process_sermon(name: str, source: str, email: str,
                   source_type: str, tmp_path: Optional[str] = None):
    """
    Runs in a background thread.
    1. Calls sermon_analyze.py as a subprocess (inherits env vars).
    2. Moves the output PDF from reports/personal/ to reports/beta/.
    3. Sends the PDF by email.
    4. Cleans up any uploaded temp file.
    """
    print(f"[job] Starting — preacher={name!r}  email={email!r}  type={source_type}")

    # Snapshot existing PDFs in personal/ so we can identify the new one after the run
    existing_pdfs = set(REPORTS_PERSONAL.glob("*.pdf"))

    try:
        cmd = [sys.executable, str(SCRIPT), source, "--name", name]
        print(f"[job] Running: {' '.join(cmd)}")

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=1800,   # 30-minute hard limit
            # stdin is closed so the script never hangs waiting for input
            stdin=subprocess.DEVNULL,
        )

        if result.returncode != 0:
            print(f"[job] ERROR: script exited {result.returncode}")
            # Print the last 2 000 chars of stderr so the problem is visible
            print(f"[job] stderr:\n{result.stderr[-2000:]}")
            return

        print("[job] Analysis complete.")
        if result.stdout:
            print(result.stdout[-1000:])   # show tail of script output

        # ── Find the newly created PDF ────────────────────────────────────────
        new_pdfs = set(REPORTS_PERSONAL.glob("*.pdf")) - existing_pdfs

        if not new_pdfs:
            # Fallback: just take the most recently modified PDF in personal/
            all_pdfs = sorted(REPORTS_PERSONAL.glob("*.pdf"),
                              key=lambda p: p.stat().st_mtime)
            if not all_pdfs:
                print("[job] ERROR: No PDF found in reports/personal/")
                return
            new_pdfs = {all_pdfs[-1]}

        pdf_src = sorted(new_pdfs, key=lambda p: p.stat().st_mtime)[-1]

        # ── Move PDF (and JSON) to reports/beta/ ──────────────────────────────
        pdf_dst = REPORTS_BETA / pdf_src.name
        shutil.move(str(pdf_src), str(pdf_dst))
        print(f"[job] PDF saved to: {pdf_dst}")

        json_src = pdf_src.with_suffix(".json")
        if json_src.exists():
            shutil.move(str(json_src), str(REPORTS_BETA / json_src.name))

        # ── Email the report ──────────────────────────────────────────────────
        send_report_email(email, name, str(pdf_dst))

    except subprocess.TimeoutExpired:
        print("[job] ERROR: Analysis timed out after 30 minutes.")
    except Exception as exc:
        print(f"[job] ERROR: {exc}")
    finally:
        # Always clean up uploaded temp file, even on failure
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)
            print(f"[job] Cleaned up temp file: {tmp_path}")


# ── Routes ─────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/submit", methods=["POST"])
def submit():
    name        = request.form.get("name", "").strip()
    email       = request.form.get("email", "").strip()
    source_type = request.form.get("source_type", "url")

    # ── Validate required fields ──────────────────────────────────────────────
    if not name:
        return render_template("index.html", error="Please enter the preacher's name.")
    if not email:
        return render_template("index.html", error="Please enter an email address.")

    # ── Resolve source (URL or uploaded file) ─────────────────────────────────
    tmp_path = None
    source   = None

    if source_type == "url":
        source = request.form.get("url", "").strip()
        if not source:
            return render_template("index.html", error="Please enter a YouTube or podcast URL.")

    else:
        file = request.files.get("audio_file")
        if not file or not file.filename:
            return render_template("index.html", error="Please select an audio or video file.")

        ext = Path(file.filename).suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            return render_template(
                "index.html",
                error=f"Unsupported file type ({ext}). "
                      f"Accepted formats: mp3, mp4, m4a, wav, flac, aac, ogg.",
            )

        timestamp    = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        tmp_filename = f"upload_{timestamp}{ext}"
        tmp_path     = f"/tmp/{tmp_filename}"
        file.save(tmp_path)
        source = tmp_path
        print(f"[upload] Saved to {tmp_path}")

    # ── Fire background thread and redirect immediately ───────────────────────
    thread = threading.Thread(
        target=process_sermon,
        args=(name, source, email, source_type, tmp_path),
        daemon=True,
    )
    thread.start()

    return redirect(url_for("submitted"))


@app.route("/submitted")
def submitted():
    return render_template("submitted.html")


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(debug=True, port=5050)
