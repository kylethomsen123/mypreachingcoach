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

# ── Sermon detection feature flag ─────────────────────────────────────────────
# Set SERMON_DETECTION=true in Railway env vars to enable auto sermon detection.
# To disable instantly: delete the SERMON_DETECTION variable in Railway.
# No code change or redeploy needed — just removing the env var turns it off.
SERMON_DETECTION_THRESHOLD_SECS = 55 * 60   # only trigger for uploads > 55 min


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
def send_confirmation_email(to_email: str, preacher_name: str):
    """Send an immediate confirmation email after sermon submission. Non-blocking."""
    import sendgrid as sg_module
    from sendgrid.helpers.mail import Mail

    api_key    = os.getenv("SENDGRID_API_KEY", "")
    from_email = os.getenv("FROM_EMAIL", "")

    if not api_key or not from_email:
        print("[confirm] Skipping — SENDGRID_API_KEY or FROM_EMAIL not set.")
        return

    greeting = f"Hey {preacher_name}," if preacher_name else "Hey there,"

    body = (
        f"{greeting}\n\n"
        "Love that you're investing in your preaching. We've got your sermon "
        "and are putting together your feedback report now — look for it in about 10 minutes.\n\n"
        "Keep growing,\n"
        "MyPreachingCoach\n"
    )

    message = Mail(
        from_email=from_email,
        to_emails=to_email,
        subject="Your feedback is on the way",
        plain_text_content=body,
    )

    try:
        client   = sg_module.SendGridAPIClient(api_key)
        response = client.send(message)
        print(f"[confirm] Sent to {to_email} — HTTP {response.status_code}")
    except Exception as e:
        print(f"[confirm] FAILED sending confirmation to {to_email}: {e}")


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


# ── Sermon detection helpers ──────────────────────────────────────────────────

def get_audio_duration(path: str) -> float:
    """Return file duration in seconds via ffprobe. Returns 0.0 on failure."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=30,
        )
        return float(result.stdout.strip())
    except Exception:
        return 0.0


def detect_sermon_with_diarization(audio_path: str, total_duration: float) -> dict:
    """
    Two-stage sermon detection:
    1. AssemblyAI speaker diarization — builds contiguous blocks for ALL speakers
       using a 7-minute merge gap (accounts for worship/prayer between sermon sections).
    2. Claude sanity check — picks the most likely sermon block using common sense
       about church service structure (duration, position, etc.).
    Falls back gracefully if either stage fails.
    """
    import httpx as _httpx
    import time  as _time

    api_key  = os.getenv("ASSEMBLYAI_API_KEY", "")
    base     = "https://api.assemblyai.com"
    auth     = {"authorization": api_key}

    # ── 1. Upload audio ───────────────────────────────────────────────────────
    file_mb = os.path.getsize(audio_path) / 1e6
    print(f"[assemblyai] Uploading {file_mb:.1f} MB ...")
    with open(audio_path, "rb") as _f:
        resp = _httpx.post(f"{base}/v2/upload", headers=auth, content=_f, timeout=300)
    resp.raise_for_status()
    upload_url = resp.json()["upload_url"]

    # ── 2. Request transcription with speaker diarization ─────────────────────
    resp = _httpx.post(
        f"{base}/v2/transcript",
        headers={**auth, "content-type": "application/json"},
        json={
            "audio_url":      upload_url,
            "speaker_labels": True,
            "speech_models":  ["universal-2"],
        },
        timeout=30,
    )
    resp.raise_for_status()
    transcript_id = resp.json()["id"]
    print(f"[assemblyai] Transcription queued: {transcript_id}")

    # ── 3. Poll until complete (max 40 min) ───────────────────────────────────
    poll_url = f"{base}/v2/transcript/{transcript_id}"
    for attempt in range(120):
        _time.sleep(20)
        resp   = _httpx.get(poll_url, headers=auth, timeout=30)
        resp.raise_for_status()
        result = resp.json()
        status = result["status"]
        if status == "completed":
            break
        if status == "error":
            raise RuntimeError(f"AssemblyAI transcription error: {result.get('error')}")
        if attempt % 3 == 0:
            print(f"[assemblyai] Status: {status} ({attempt * 20}s elapsed)")
    else:
        raise RuntimeError("AssemblyAI timed out after 40 minutes")

    # ── 4. Build contiguous blocks for every speaker ──────────────────────────
    utterances = result.get("utterances") or []
    if not utterances:
        raise RuntimeError("AssemblyAI returned no utterances")

    # Accumulate segments and total time per speaker
    speaker_segs: dict = {}
    speaker_time: dict = {}
    for u in utterances:
        spk   = u["speaker"]
        start = u["start"] / 1000.0
        end   = u["end"]   / 1000.0
        speaker_time[spk] = speaker_time.get(spk, 0) + (end - start)
        speaker_segs.setdefault(spk, []).append([start, end])

    # Merge each speaker's segments with a 7-minute gap.
    # Larger gap than before (was 3 min) so mid-sermon worship/prayer doesn't
    # split the preacher's block into two fragments.
    MAX_GAP_SEC = 420   # 7 minutes
    all_blocks: list = []
    for spk, segs in speaker_segs.items():
        segs = sorted(segs)
        merged: list = []
        for start, end in segs:
            if merged and (start - merged[-1][1]) <= MAX_GAP_SEC:
                merged[-1][1] = end
            else:
                merged.append([start, end])
        for seg in merged:
            dur = seg[1] - seg[0]
            all_blocks.append({
                "speaker":           spk,
                "start":             seg[0],
                "end":               seg[1],
                "duration_min":      dur / 60,
                "start_pct":         seg[0] / total_duration * 100,
                "speaker_total_min": speaker_time[spk] / 60,
            })

    # Sort by block duration descending so index 0 = longest block
    all_blocks.sort(key=lambda x: x["duration_min"], reverse=True)

    for b in all_blocks:
        print(f"[detection] Block: Speaker {b['speaker']}  "
              f"{int(b['start'])//60}:{int(b['start'])%60:02d}–"
              f"{int(b['end'])//60}:{int(b['end'])%60:02d}  "
              f"({b['duration_min']:.0f} min, starts at {b['start_pct']:.0f}%)")

    # ── 5. Claude picks the most likely sermon block ──────────────────────────
    return _claude_pick_sermon(all_blocks, total_duration)


def _claude_pick_sermon(all_blocks: list, total_duration: float) -> dict:
    """
    Asks Claude to identify which diarization block is most likely the sermon.
    Applies common sense about church service structure:
    - Sermons are typically 25–50 min long
    - Sermons start after worship/announcements (usually 20–70% through service)
    - A 12-min block in a 95-min service is almost certainly not the sermon
    Falls back to longest valid block (≥20 min, starts ≥15%) if Claude fails.
    """
    import anthropic as _anthropic

    def _fmt(secs: float) -> str:
        return f"{int(secs)//60}:{int(secs)%60:02d}"

    total_mins = total_duration / 60

    # Summarise top 10 blocks for Claude
    lines = []
    for i, b in enumerate(all_blocks[:10]):
        lines.append(
            f"  [{i}] Speaker {b['speaker']}: {_fmt(b['start'])}–{_fmt(b['end'])}"
            f"  ({b['duration_min']:.0f} min, starts {b['start_pct']:.0f}% through service,"
            f" speaker's total mic time: {b['speaker_total_min']:.0f} min)"
        )

    prompt = (
        f"A church service recording is {total_mins:.0f} minutes long. "
        f"Speaker diarization found these contiguous speaking blocks "
        f"(sorted longest first):\n\n"
        + "\n".join(lines) +
        "\n\nTypical church service patterns:\n"
        "- The sermon is 25–50 minutes of continuous teaching by one person\n"
        "- It starts after worship and announcements — usually 20–70% through the service\n"
        "- Blocks under 20 minutes are almost never the full sermon\n"
        "- The preacher may have the most total mic time, but not always\n\n"
        "Which block index is most likely the sermon? Reply in exactly this format:\n"
        "INDEX: [number]\n"
        "CONFIDENCE: [high/medium/low]\n"
        "REASON: [one sentence explaining your choice]"
    )

    try:
        client   = _anthropic.Anthropic()
        response = client.messages.create(
            model     = "claude-haiku-4-5-20251001",
            max_tokens= 120,
            messages  = [{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        print(f"[claude] Sermon block selection:\n{text}")

        parsed = {}
        for line in text.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                parsed[k.strip().upper()] = v.strip()

        idx = int(parsed.get("INDEX", "0"))
        if 0 <= idx < len(all_blocks):
            b = all_blocks[idx]
            return {
                "start_seconds": int(b["start"]),
                "end_seconds":   int(b["end"]),
                "confidence":    parsed.get("CONFIDENCE", "medium").lower(),
                "reasoning":     parsed.get("REASON", ""),
            }
    except Exception as e:
        print(f"[claude] Sermon selection failed ({e}) — falling back to heuristic")

    # Fallback: longest block ≥20 min that starts at least 15% through the service
    for b in all_blocks:
        if b["duration_min"] >= 20 and b["start_pct"] >= 15:
            return {
                "start_seconds": int(b["start"]),
                "end_seconds":   int(b["end"]),
                "confidence":    "medium",
                "reasoning":     "Longest speaking block meeting minimum sermon duration.",
            }

    # Last resort: longest block overall
    b = all_blocks[0]
    return {
        "start_seconds": int(b["start"]),
        "end_seconds":   int(b["end"]),
        "confidence":    "low",
        "reasoning":     "Could not confidently identify sermon — please adjust times.",
    }


def run_detection_background(pending_id: str) -> None:
    """
    Background thread: downloads audio if needed (URL submissions), runs
    AssemblyAI diarization, updates pending JSON with the detected window.
    Falls back to percentage defaults if anything fails so the user always
    reaches the confirm page.
    """
    pending_path = f"/tmp/pending_{pending_id}.json"
    try:
        with open(pending_path) as _f:
            pending = json.load(_f)
    except Exception as e:
        print(f"[detection] Could not read pending JSON: {e}")
        return

    total_duration = pending["total_duration"]
    audio_path     = pending.get("tmp_path")

    try:
        # ── Download audio for URL submissions ───────────────────────────────
        if pending.get("mode") == "url" and not audio_path:
            import tempfile as _tf
            tmpdir = _tf.mkdtemp(prefix="detection_")
            url    = pending["source_url"]
            print(f"[detection] Downloading for diarization: {url}")
            proxy = os.getenv("YTDLP_PROXY", "")
            cmd   = ["yt-dlp", "-x", "--audio-format", "mp3",
                     "--no-playlist", "--retries", "3",
                     "-o", os.path.join(tmpdir, "%(title)s.%(ext)s"), url]
            if proxy:
                cmd += ["--proxy", proxy]
            subprocess.run(cmd, check=True, capture_output=True, timeout=600)
            mp3s = list(Path(tmpdir).glob("*.mp3"))
            if not mp3s:
                raise RuntimeError("yt-dlp produced no mp3")
            audio_path = str(mp3s[0])
            pending["tmp_path"] = audio_path   # store so confirm POST can use it

        # ── Run AssemblyAI diarization ────────────────────────────────────────
        print(f"[detection] Running AssemblyAI diarization on {audio_path}")
        detected = detect_sermon_with_diarization(audio_path, total_duration)
        pending.update({
            "status":         "ready",
            "detected_start": detected["start_seconds"],
            "detected_end":   detected["end_seconds"],
            "confidence":     detected["confidence"],
            "reasoning":      detected["reasoning"],
            "tmp_path":       audio_path,
        })
        print(f"[detection] Sermon detected: "
              f"{detected['start_seconds']//60}:{detected['start_seconds']%60:02d} – "
              f"{detected['end_seconds']//60}:{detected['end_seconds']%60:02d} "
              f"({detected['confidence']} confidence)")

    except Exception as e:
        print(f"[detection] AssemblyAI failed ({e}) — falling back to percentage defaults")
        pending.update({
            "status":         "ready",
            "detected_start": int(total_duration * 0.40),
            "detected_end":   int(total_duration * 0.90),
            "confidence":     "low",
            "reasoning":      "Auto-detection unavailable — using estimate. Please adjust if needed.",
            "tmp_path":       audio_path,
        })

    with open(pending_path, "w") as _f:
        json.dump(pending, _f)


def trim_audio(input_path: str, start_sec: int, end_sec: int, output_path: str) -> str:
    """Trim audio to [start_sec, end_sec] and save as MP3."""
    subprocess.run([
        "ffmpeg", "-y", "-i", input_path,
        "-ss", str(start_sec), "-to", str(end_sec),
        "-vn", "-c:a", "libmp3lame", "-q:a", "4",
        output_path,
    ], check=True, capture_output=True, timeout=300)
    return output_path


def probe_url_duration(url: str) -> float:
    """
    Uses yt-dlp --dump-json to get video duration in seconds without downloading.
    Returns 0.0 on failure or if the URL isn't a supported yt-dlp source.
    """
    try:
        proxy = os.getenv("YTDLP_PROXY", "")
        cmd   = ["yt-dlp", "--dump-json", "--no-warnings", "--no-playlist",
                 "--socket-timeout", "20"]
        if proxy:
            cmd += ["--proxy", proxy]
        cmd.append(url)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0 and result.stdout.strip():
            info = json.loads(result.stdout.strip())
            return float(info.get("duration") or 0)
    except Exception:
        pass
    return 0.0


# ── Background job ─────────────────────────────────────────────────────────────
def process_sermon(name: str, source: str, email: str,
                   source_type: str, tmp_path: Optional[str] = None,
                   job_id: Optional[str] = None,
                   start_sec: Optional[int] = None,
                   end_sec: Optional[int] = None):
    """
    Runs in a background thread.
    1. Calls sermon_analyze.py as a subprocess (inherits env vars).
    2. Moves the output PDF from reports/personal/ to reports/beta/.
    3. Sends the PDF by email.
    4. Cleans up any uploaded temp file.
    5. Logs status at each stage to jobs.json.
    start_sec / end_sec: optional trim window passed to sermon_analyze.py via
    --start-sec / --end-sec (used when a URL submission was trimmed at confirm).
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
        if start_sec is not None and end_sec is not None:
            cmd += ["--start-sec", str(start_sec), "--end-sec", str(end_sec)]
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

        # ── [SERMON_DETECTION] Probe URL duration, launch async detection ───
        if os.getenv("SERMON_DETECTION", "").lower() == "true":
            try:
                duration = probe_url_duration(source)
                if duration > SERMON_DETECTION_THRESHOLD_SECS:
                    print(f"[detection] URL is {duration/60:.1f} min — launching diarization")
                    pending_id = str(uuid.uuid4())
                    pending    = {
                        "name":           name,
                        "email":          email,
                        "source_url":     source,
                        "tmp_path":       None,
                        "source_type":    "url",
                        "mode":           "url",
                        "total_duration": duration,
                        "status":         "detecting",
                        "detected_start": None,
                        "detected_end":   None,
                        "confidence":     None,
                        "reasoning":      None,
                    }
                    with open(f"/tmp/pending_{pending_id}.json", "w") as _f:
                        json.dump(pending, _f)
                    threading.Thread(
                        target=run_detection_background,
                        args=(pending_id,), daemon=True,
                    ).start()
                    return redirect(url_for("detecting_page", pending_id=pending_id))
            except Exception as _e:
                print(f"[detection] URL duration probe failed ({_e}) — proceeding normally")
        # ── End URL detection ─────────────────────────────────────────────────

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

    # ── [SERMON_DETECTION] Detect sermon segment for long file uploads ────────
    # Gated by SERMON_DETECTION=true env var.
    # To disable: remove that variable in Railway — no redeploy needed.
    if (tmp_path
            and os.getenv("SERMON_DETECTION", "").lower() == "true"
            and source_type != "url"):
        try:
            duration = get_audio_duration(tmp_path)
            if duration > SERMON_DETECTION_THRESHOLD_SECS:
                print(f"[detection] File is {duration/60:.1f} min — launching diarization")
                pending_id = str(uuid.uuid4())
                pending    = {
                    "name":           name,
                    "email":          email,
                    "tmp_path":       tmp_path,
                    "source_type":    source_type,
                    "mode":           "file",
                    "total_duration": duration,
                    "status":         "detecting",
                    "detected_start": None,
                    "detected_end":   None,
                    "confidence":     None,
                    "reasoning":      None,
                }
                with open(f"/tmp/pending_{pending_id}.json", "w") as _f:
                    json.dump(pending, _f)
                threading.Thread(
                    target=run_detection_background,
                    args=(pending_id,), daemon=True,
                ).start()
                return redirect(url_for("detecting_page", pending_id=pending_id))
        except Exception as _det_err:
            print(f"[detection] Failed to start detection ({_det_err}) — proceeding normally")
    # ── End sermon detection ──────────────────────────────────────────────────

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

    # ── Send confirmation email (non-blocking, before analysis begins) ─────────
    if "@" in email:
        send_confirmation_email(email, name)

    # ── Fire background thread and redirect immediately ───────────────────────
    thread = threading.Thread(
        target=process_sermon,
        args=(name, source, email, source_type, tmp_path, job_id),
        daemon=True,
    )
    thread.start()

    return redirect(url_for("submitted"))


@app.route("/confirm/<pending_id>", methods=["GET"])
def confirm_segment_get(pending_id: str):
    """Show the detected sermon segment for user review before running full analysis."""
    pending_path = f"/tmp/pending_{pending_id}.json"
    if not os.path.exists(pending_path):
        return render_template("index.html",
            error="This session expired or was already used. Please re-upload your file.")
    with open(pending_path) as _f:
        pending = json.load(_f)

    def _fmt(secs: float) -> str:
        return f"{int(secs) // 60}:{int(secs) % 60:02d}"

    mode = pending.get("mode", "file")

    return render_template("confirm.html",
        pending_id          = pending_id,
        mode                = mode,
        name                = pending["name"],
        total_duration_mins = int(pending["total_duration"] // 60),
        detected_start      = int(pending["detected_start"]),
        detected_end        = int(pending["detected_end"]),
        detected_start_mmss = _fmt(pending["detected_start"]),
        detected_end_mmss   = _fmt(pending["detected_end"]),
        detected_dur_mins   = int((pending["detected_end"] - pending["detected_start"]) // 60),
        confidence          = pending.get("confidence"),
        reasoning           = pending.get("reasoning"),
    )


@app.route("/detecting/<pending_id>")
def detecting_page(pending_id: str):
    """Waiting page shown while AssemblyAI diarization runs in the background."""
    if not os.path.exists(f"/tmp/pending_{pending_id}.json"):
        return render_template("index.html", error="Session expired. Please resubmit.")
    return render_template("detecting.html", pending_id=pending_id)


@app.route("/detecting-status/<pending_id>")
def detecting_status(pending_id: str):
    """JSON endpoint polled by detecting.html every 3 seconds."""
    pending_path = f"/tmp/pending_{pending_id}.json"
    if not os.path.exists(pending_path):
        return {"status": "expired"}
    try:
        with open(pending_path) as _f:
            pending = json.load(_f)
        return {"status": pending.get("status", "detecting")}
    except Exception:
        return {"status": "detecting"}


@app.route("/confirm/<pending_id>", methods=["POST"])
def confirm_segment_post(pending_id: str):
    """Trim audio to confirmed window and launch full analysis."""
    pending_path = f"/tmp/pending_{pending_id}.json"
    if not os.path.exists(pending_path):
        return render_template("index.html",
            error="This session expired or was already used. Please re-upload your file.")
    with open(pending_path) as _f:
        pending = json.load(_f)
    os.remove(pending_path)

    name        = pending["name"]
    email       = pending["email"]
    source_type = pending["source_type"]
    use_full    = request.form.get("use_full", "false") == "true"
    orig_path   = pending.get("tmp_path")

    # By the time we reach confirm, audio is always on disk:
    # - file uploads: saved during /submit
    # - URL submissions: downloaded during background detection
    # In both cases, trim the file directly here.
    if not orig_path or not os.path.exists(orig_path):
        # Fallback: audio missing (detection download failed) — pass URL with time args
        source    = pending.get("source_url", orig_path)
        tmp_path  = None
        start_sec = None if use_full else int(request.form.get("start_seconds", pending.get("detected_start", 0)))
        end_sec   = None if use_full else int(request.form.get("end_seconds",   pending.get("detected_end", 0)))
        print(f"[confirm] No local file for {name!r} — passing URL with --start-sec/--end-sec")
    elif use_full:
        source    = orig_path
        tmp_path  = orig_path
        start_sec = None
        end_sec   = None
        print(f"[confirm] {name!r} chose full recording")
    else:
        start_sec_req = int(request.form.get("start_seconds", pending["detected_start"]))
        end_sec_req   = int(request.form.get("end_seconds",   pending["detected_end"]))
        trimmed = f"/tmp/trimmed_{pending_id}.mp3"
        try:
            trim_audio(orig_path, start_sec_req, end_sec_req, trimmed)
            if os.path.exists(orig_path):
                os.remove(orig_path)
            source    = trimmed
            tmp_path  = trimmed
            start_sec = None
            end_sec   = None
            print(f"[confirm] Trimmed {name!r}: {start_sec_req}s–{end_sec_req}s → {trimmed}")
        except Exception as _e:
            print(f"[confirm] Trim failed ({_e}) — using full file")
            source    = orig_path
            tmp_path  = orig_path
            start_sec = None
            end_sec   = None

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
    print(f"[confirm] Queued job {job_id[:8]}  preacher={name!r}  email={email!r}")

    if "@" in email:
        send_confirmation_email(email, name)

    thread = threading.Thread(
        target=process_sermon,
        args=(name, source, email, source_type, tmp_path, job_id, start_sec, end_sec),
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
    """Diagnostic endpoint — checks ffmpeg and yt-dlp."""
    import shutil, subprocess as sp
    checks = {}

    checks["ffmpeg"] = shutil.which("ffmpeg") or "NOT FOUND"
    checks["yt-dlp"] = shutil.which("yt-dlp") or "NOT FOUND"
    checks["ASSEMBLYAI_API_KEY"] = "SET" if os.environ.get("ASSEMBLYAI_API_KEY") else "NOT SET"
    checks["SERMON_DETECTION"] = os.environ.get("SERMON_DETECTION", "NOT SET")

    # Test yt-dlp can reach YouTube
    try:
        cmd = ["yt-dlp", "--dump-json", "--no-warnings", "--no-playlist",
               "--socket-timeout", "10", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"]
        r = sp.run(cmd, capture_output=True, text=True, timeout=15)
        if r.returncode == 0:
            import json as j
            info = j.loads(r.stdout.strip())
            checks["yt-dlp_youtube"] = f"OK — {info.get('title', '?')}"
        else:
            checks["yt-dlp_youtube"] = f"FAIL (rc={r.returncode}) — {r.stderr[:200]}"
    except Exception as e:
        checks["yt-dlp_youtube"] = f"ERROR — {e}"

    return checks
