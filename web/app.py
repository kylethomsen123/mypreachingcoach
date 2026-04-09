"""
app.py — My Preaching Coach web app
Run: python3 app.py
Then open: http://localhost:5050
"""

import base64
import json
import os
import shutil
import sys
import threading
import subprocess
import time
import uuid
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
BASE_DIR         = Path(__file__).parent                  # same dir as app.py
SCRIPT           = BASE_DIR / "sermon_analyze.py"
REPORTS_PERSONAL = BASE_DIR / "reports" / "personal"
REPORTS_BETA     = BASE_DIR / "reports" / "beta"

REPORTS_BETA.mkdir(parents=True, exist_ok=True)

JOBS_FILE  = Path("/app/reports/jobs.json")   # Railway persistent volume
_jobs_lock = threading.Lock()                  # serialises all reads + writes

# Accepted audio/video extensions
ALLOWED_EXTENSIONS = {
    ".mp3", ".mp4", ".m4a", ".wav", ".flac",
    ".aac", ".ogg", ".webm", ".mov",
}


# ── Job logging ───────────────────────────────────────────────────────────────
def log_job(job_id: str, **fields) -> None:
    """Upsert a job record into JOBS_FILE. Thread-safe. Silent on missing volume."""
    if not JOBS_FILE.parent.exists():
        return
    with _jobs_lock:
        try:
            jobs = json.loads(JOBS_FILE.read_text()) if JOBS_FILE.exists() else []
            if not isinstance(jobs, list):
                jobs = []
        except (json.JSONDecodeError, OSError):
            jobs = []
        for record in jobs:
            if record.get("job_id") == job_id:
                record.update(fields)
                break
        else:
            jobs.append({"job_id": job_id, **fields})
        jobs = jobs[-200:]
        tmp = JOBS_FILE.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(jobs, indent=2, default=str))
            tmp.replace(JOBS_FILE)
        except OSError as e:
            print(f"[jobs] WARNING: could not write jobs.json — {e}")


def _mask_email(email: str) -> str:
    """Return 'k***@example.com' — first char + *** + @domain."""
    if "@" not in email:
        return "***"
    local, domain = email.split("@", 1)
    return local[0] + "***@" + domain


def _mark_interrupted_jobs() -> None:
    """On startup, mark any jobs still in 'queued'/'started'/'analyzing' as interrupted.

    These are jobs whose background thread was killed by a server restart mid-flight.
    Without this, they'd show as 'started' forever with no indication of what happened.
    """
    if not JOBS_FILE.exists():
        return
    with _jobs_lock:
        try:
            jobs = json.loads(JOBS_FILE.read_text())
            if not isinstance(jobs, list):
                return
        except (json.JSONDecodeError, OSError):
            return

        interrupted = [j for j in jobs if j.get("status") in {"queued", "started", "analyzing"}]
        if not interrupted:
            return

        for job in interrupted:
            job["status"]    = "error"
            job["error_msg"] = "Server restarted — job was interrupted before completing"
        try:
            tmp = JOBS_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(jobs, indent=2, default=str))
            tmp.replace(JOBS_FILE)
            for job in interrupted:
                print(f"[startup] Marked job {job.get('job_id','?')[:8]} as interrupted "
                      f"(preacher={job.get('preacher_name','?')!r}  "
                      f"email={job.get('email','?')!r})")
        except OSError as e:
            print(f"[startup] WARNING: could not write interrupted jobs — {e}")


_mark_interrupted_jobs()   # surface any jobs killed by a prior restart

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
    notify_email = os.getenv("NOTIFY_EMAIL", "")        # Kyle gets a BCC of every report
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

    if notify_email and notify_email != to_email:
        from sendgrid.helpers.mail import Bcc
        message.bcc = [Bcc(notify_email)]

    client = sg_module.SendGridAPIClient(api_key)
    try:
        response = client.send(message)
        print(f"[email] Sent to {to_email} — HTTP {response.status_code}")
        if notify_email and notify_email != to_email:
            print(f"[email] BCC'd to {notify_email}")
    except Exception as email_err:
        print(f"[email] FAILED sending to {to_email}")
        print(f"[email] FROM_EMAIL was: {from_email}")
        if hasattr(email_err, 'status_code'):
            print(f"[email] HTTP status: {email_err.status_code}")
        if hasattr(email_err, 'body'):
            print(f"[email] Response body: {email_err.body}")
        raise


# ── Background job ─────────────────────────────────────────────────────────────
def process_sermon(name: str, source: str, email: str,
                   source_type: str, tmp_path: Optional[str] = None,
                   job_id: Optional[str] = None):
    """
    Runs in a background thread.
    1. Calls sermon_analyze.py as a subprocess (inherits env vars).
    2. Moves the output PDF from reports/personal/ to reports/beta/.
    3. Sends the PDF by email.
    4. Cleans up any uploaded temp file.
    5. Logs status at each stage to jobs.json.
    """
    if job_id is None:
        job_id = str(uuid.uuid4())
    start_time = time.monotonic()
    timestamp  = datetime.utcnow().isoformat(timespec="seconds") + "Z"

    log_job(job_id,
        timestamp     = timestamp,
        preacher_name = name,
        email         = _mask_email(email),
        source_type   = source_type,
        status        = "started",
        pdf_name      = None,
        error_msg     = None,
        duration_sec  = None,
    )
    print(f"[job] Starting — id={job_id[:8]}  preacher={name!r}  email={email!r}  type={source_type}")

    existing_pdfs = set(REPORTS_PERSONAL.glob("*.pdf"))

    try:
        log_job(job_id, status="analyzing")
        cmd = [sys.executable, str(SCRIPT), source, "--name", name]
        print(f"[job] Running: {' '.join(cmd)}")

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=1800,   # 30-minute hard limit
            stdin=subprocess.DEVNULL,
        )

        if result.returncode != 0:
            print(f"[job] ERROR: script exited {result.returncode}")
            print(f"[job] stderr:\n{result.stderr[-2000:]}")
            log_job(job_id,
                status       = "error",
                error_msg    = f"script exited {result.returncode}: {result.stderr[-300:]}",
                duration_sec = round(time.monotonic() - start_time, 1),
            )
            return

        print("[job] Analysis complete.")
        if result.stdout:
            print(result.stdout[-1000:])

        # ── Find the newly created PDF ────────────────────────────────────────
        new_pdfs = set(REPORTS_PERSONAL.glob("*.pdf")) - existing_pdfs
        if not new_pdfs:
            all_pdfs = sorted(REPORTS_PERSONAL.glob("*.pdf"),
                              key=lambda p: p.stat().st_mtime)
            if not all_pdfs:
                log_job(job_id,
                    status       = "error",
                    error_msg    = "No PDF found in reports/personal/ after analysis",
                    duration_sec = round(time.monotonic() - start_time, 1),
                )
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

        log_job(job_id, status="pdf_ready", pdf_name=pdf_src.name)

        # ── Email the report ──────────────────────────────────────────────────
        try:
            send_report_email(email, name, str(pdf_dst))
            log_job(job_id,
                status       = "email_sent",
                duration_sec = round(time.monotonic() - start_time, 1),
            )
        except Exception as email_exc:
            log_job(job_id,
                status       = "email_failed",
                error_msg    = str(email_exc)[:400],
                duration_sec = round(time.monotonic() - start_time, 1),
            )
            print(f"[job] email FAILED: {email_exc}")

    except subprocess.TimeoutExpired:
        log_job(job_id,
            status       = "error",
            error_msg    = "Analysis timed out after 30 minutes",
            duration_sec = round(time.monotonic() - start_time, 1),
        )
        print("[job] ERROR: Analysis timed out after 30 minutes.")
    except Exception as exc:
        log_job(job_id,
            status       = "error",
            error_msg    = str(exc)[:400],
            duration_sec = round(time.monotonic() - start_time, 1),
        )
        print(f"[job] ERROR: {exc}")
    finally:
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

    # ── Log the submission before launching the thread ────────────────────────
    # This ensures a record exists in jobs.json even if the server restarts
    # and kills the daemon thread before it can write its own first log entry.
    job_id = str(uuid.uuid4())
    log_job(job_id,
        timestamp     = datetime.utcnow().isoformat(timespec="seconds") + "Z",
        preacher_name = name,
        email         = _mask_email(email),
        source_type   = source_type,
        status        = "queued",
        pdf_name      = None,
        error_msg     = None,
        duration_sec  = None,
    )
    print(f"[submit] Queued job {job_id[:8]}  preacher={name!r}  email={email!r}")

    # ── Fire background thread and redirect immediately ───────────────────────
    thread = threading.Thread(
        target=process_sermon,
        args=(name, source, email, source_type, tmp_path, job_id),
        daemon=True,
    )
    thread.start()

    return redirect(url_for("submitted"))


@app.route("/submitted")
def submitted():
    return render_template("submitted.html")


@app.route("/admin/resend", methods=["GET", "POST"])
def admin_resend():
    """Admin endpoint to resend a PDF report by email (for missed/failed sends)."""
    admin_key = os.getenv("ADMIN_KEY", "")
    if request.args.get("key") != admin_key or not admin_key:
        return "Unauthorized", 403

    if request.method == "POST":
        pdf_name     = request.form.get("pdf_name", "").strip()
        to_email     = request.form.get("email", "").strip()
        preacher     = request.form.get("name", "").strip()
        pdf_path     = str(REPORTS_BETA / pdf_name)

        if not pdf_name or not to_email or not preacher:
            return "Missing pdf_name, email, or name", 400
        if not (REPORTS_BETA / pdf_name).exists():
            return f"PDF not found: {pdf_name}", 404

        try:
            send_report_email(to_email, preacher, pdf_path)
            return f"Sent {pdf_name} to {to_email}", 200
        except Exception as e:
            return f"Failed: {e}", 500

    # GET — list available PDFs
    pdfs = sorted(REPORTS_BETA.glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)
    rows = "".join(
        f"<tr><td>{p.name}</td><td>{p.stat().st_size // 1024}KB</td></tr>"
        for p in pdfs
    )
    form = f"""
    <html><body>
    <h2>Resend Report Email</h2>
    <table border=1>{rows}</table>
    <form method=POST action="/admin/resend?key={admin_key}">
      PDF filename: <input name="pdf_name" size=60><br>
      Preacher name: <input name="name" size=30><br>
      Email: <input name="email" size=40><br>
      <input type=submit value="Send">
    </form>
    </body></html>"""
    return form


@app.route("/admin/status")
def admin_status():
    """Admin job log — shows last 50 jobs in a clean HTML table."""
    admin_key = os.getenv("ADMIN_KEY", "")
    if request.args.get("key") != admin_key or not admin_key:
        return "Unauthorized", 403

    jobs = []
    volume_warning = None
    if not JOBS_FILE.parent.exists():
        volume_warning = "/app/reports volume is not mounted — no job data available."
    elif JOBS_FILE.exists():
        try:
            with _jobs_lock:
                jobs = json.loads(JOBS_FILE.read_text())
            if not isinstance(jobs, list):
                jobs = []
        except (json.JSONDecodeError, OSError) as e:
            volume_warning = f"Could not read jobs.json: {e}"

    jobs = list(reversed(jobs))[:50]
    has_running = any(j.get("status") in {"started", "analyzing"} for j in jobs)

    return render_template("admin_status.html",
        jobs=jobs, has_running=has_running,
        volume_warning=volume_warning, admin_key=admin_key)


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    app.run(host="0.0.0.0", debug=False, port=port)

@app.route("/health")
def health():
    """Diagnostic endpoint — checks ffmpeg, yt-dlp, and WARP proxy."""
    import shutil, subprocess as sp, socket
    checks = {}

    checks["ffmpeg"] = shutil.which("ffmpeg") or "NOT FOUND"
    checks["yt-dlp"] = shutil.which("yt-dlp") or "NOT FOUND"
    checks["YTDLP_PROXY"] = os.environ.get("YTDLP_PROXY", "NOT SET")
    checks["PYTHONUNBUFFERED"] = os.environ.get("PYTHONUNBUFFERED", "NOT SET")

    # Test DNS resolution for warp service
    try:
        result = socket.getaddrinfo("docker-warp-socks.railway.internal", 9091)
        checks["warp_dns"] = f"OK — {result[0][4]}"
    except Exception as e:
        checks["warp_dns"] = f"FAIL — {e}"

    # Test TCP connection to warp proxy
    try:
        s = socket.socket(socket.AF_INET6 if ":" in checks.get("warp_dns","") else socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect(("docker-warp-socks.railway.internal", 9091))
        s.close()
        checks["warp_tcp"] = "OK — connected"
    except Exception as e:
        checks["warp_tcp"] = f"FAIL — {e}"

    # Test yt-dlp WITHOUT proxy (expect bot block)
    try:
        cmd = ["yt-dlp", "--dump-json", "--no-warnings", "--no-playlist",
               "--socket-timeout", "10", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"]
        r = sp.run(cmd, capture_output=True, text=True, timeout=15)
        if r.returncode == 0:
            import json as j
            info = j.loads(r.stdout.strip())
            checks["yt-dlp_no_proxy"] = f"OK — {info.get('title', '?')}"
        else:
            checks["yt-dlp_no_proxy"] = f"FAIL (rc={r.returncode}) — {r.stderr[:200]}"
    except Exception as e:
        checks["yt-dlp_no_proxy"] = f"ERROR — {e}"

    # Test yt-dlp WITH proxy
    proxy = os.environ.get("YTDLP_PROXY", "")
    if proxy:
        try:
            cmd = ["yt-dlp", "--dump-json", "--no-warnings", "--no-playlist",
                   "--socket-timeout", "10", "--proxy", proxy,
                   "https://www.youtube.com/watch?v=dQw4w9WgXcQ"]
            r = sp.run(cmd, capture_output=True, text=True, timeout=20)
            if r.returncode == 0:
                import json as j
                info = j.loads(r.stdout.strip())
                checks["yt-dlp_with_proxy"] = f"OK — {info.get('title', '?')}"
            else:
                checks["yt-dlp_with_proxy"] = f"FAIL (rc={r.returncode}) — {r.stderr[:200]}"
        except Exception as e:
            checks["yt-dlp_with_proxy"] = f"ERROR — {e}"

    return checks
