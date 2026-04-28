"""
MPC Downloader — runs on the Hetzner VM.

Exposes two endpoints for the Railway app to call:
  POST /probe    — returns {title, duration, uploader, used_proxy}
  POST /download — returns the MP3 audio bytes + X-Used-Proxy header

Strategy: try yt-dlp direct from Hetzner first (clean dedicated IP).
If YouTube bot-checks or SABR-blocks, retry once via DataImpulse residential
proxy. Cookies/PO-tokens are intentionally NOT used — testing showed they
trigger SABR "no formats" errors in 2026, making them counterproductive.
"""

import hmac
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_file

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("mpc-downloader")

SECRET = os.environ.get("DOWNLOADER_SECRET", "").strip()
PROXY = os.environ.get("DATAIMPULSE_PROXY", "").strip()
YT_DLP = os.environ.get("YT_DLP_BIN", "/opt/mpc-downloader/venv/bin/yt-dlp")
TIMEOUT_SEC = int(os.environ.get("YT_DLP_TIMEOUT", "1200"))

if not SECRET:
    sys.exit("DOWNLOADER_SECRET not set — refusing to start")

app = Flask(__name__)


def auth_ok(req) -> bool:
    token = req.headers.get("X-Auth-Token", "")
    return bool(token) and hmac.compare_digest(token, SECRET)


def _run(url: str, outdir: str, use_proxy: bool, dump_json: bool) -> subprocess.CompletedProcess:
    args = [YT_DLP, "--no-warnings", "--no-playlist"]
    if use_proxy and PROXY:
        args += ["--proxy", PROXY]
    if dump_json:
        args += ["--dump-json"]
    else:
        args += ["-x", "--audio-format", "mp3", "-o", f"{outdir}/%(id)s.%(ext)s"]
    args.append(url)
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=TIMEOUT_SEC)
    except subprocess.TimeoutExpired as e:
        log.warning("yt-dlp timed out after %ss (proxy=%s): %s", TIMEOUT_SEC, use_proxy, url)
        # Synthesize a failed CompletedProcess so the caller treats it like a normal yt-dlp failure
        partial_stderr = (e.stderr.decode("utf-8", "replace") if isinstance(e.stderr, bytes)
                          else (e.stderr or ""))
        return subprocess.CompletedProcess(
            args=args, returncode=124,
            stdout="", stderr=f"timeout after {TIMEOUT_SEC}s\n{partial_stderr[-300:]}",
        )


def classify_failure(stderr: str) -> str:
    s = (stderr or "").lower()
    if "timeout after" in s:
        return "timeout"
    if "sign in to confirm" in s or "confirm you" in s:
        return "bot-check"
    if "no video formats" in s or "requested format" in s or "sabr" in s:
        return "sabr-or-format"
    if "video unavailable" in s or "private video" in s or "members-only" in s:
        return "unavailable"
    return "unknown"


@app.errorhandler(Exception)
def _json_error(e):
    """Return JSON for unhandled exceptions so the Railway client gets a parseable error
    instead of HTML soup. Werkzeug HTTPExceptions (404/401/etc.) keep their normal codes."""
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return jsonify({"error": e.description, "code": e.code}), e.code
    log.exception("unhandled exception in handler")
    return jsonify({"error": "internal error", "detail": str(e)[:300]}), 500


def _try_both_paths(url: str, outdir: str, dump_json: bool):
    """Try direct first; on failure, retry through the residential proxy.
    Returns (CompletedProcess, used_proxy: bool, first_failure: str|None)."""
    r = _run(url, outdir, use_proxy=False, dump_json=dump_json)
    if r.returncode == 0:
        return r, False, None
    first_fail = classify_failure(r.stderr)
    log.warning("direct failed (%s) — retrying via proxy. url=%s stderr_tail=%r",
                first_fail, url, (r.stderr or "")[-300:])
    if not PROXY:
        return r, False, first_fail
    r2 = _run(url, outdir, use_proxy=True, dump_json=dump_json)
    return r2, True, first_fail


@app.get("/health")
def health():
    v = subprocess.run([YT_DLP, "--version"], capture_output=True, text=True, timeout=10)
    return jsonify({
        "status": "ok",
        "yt_dlp": v.stdout.strip() if v.returncode == 0 else "error",
        "proxy_configured": bool(PROXY),
    })


@app.post("/probe")
def probe():
    if not auth_ok(request):
        abort(401)
    url = ((request.get_json(silent=True) or {}).get("url") or "").strip()
    if not url:
        return jsonify({"error": "missing url"}), 400

    r, used_proxy, first_fail = _try_both_paths(url, "/tmp", dump_json=True)
    if r.returncode != 0 or not r.stdout:
        return jsonify({
            "error": "probe failed",
            "direct_failure": first_fail,
            "final_failure": classify_failure(r.stderr),
            "stderr_tail": (r.stderr or "")[-400:],
        }), 502

    try:
        info = json.loads(r.stdout.splitlines()[0])
    except Exception as e:
        return jsonify({"error": f"could not parse yt-dlp json: {e}"}), 500

    return jsonify({
        "title": info.get("title"),
        "duration": info.get("duration"),
        "uploader": info.get("uploader"),
        "creator": info.get("creator"),
        "artist": info.get("artist"),
        "id": info.get("id"),
        "used_proxy": used_proxy,
        "direct_failure": first_fail,
    })


@app.post("/download")
def download():
    if not auth_ok(request):
        abort(401)
    url = ((request.get_json(silent=True) or {}).get("url") or "").strip()
    if not url:
        return jsonify({"error": "missing url"}), 400

    tmpdir = tempfile.mkdtemp(prefix="mpc-dl-", dir="/tmp")
    r, used_proxy, first_fail = _try_both_paths(url, tmpdir, dump_json=False)

    if r.returncode != 0:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return jsonify({
            "error": "download failed",
            "direct_failure": first_fail,
            "final_failure": classify_failure(r.stderr),
            "stderr_tail": (r.stderr or "")[-500:],
        }), 502

    mp3s = list(Path(tmpdir).glob("*.mp3"))
    if not mp3s:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return jsonify({"error": "yt-dlp produced no mp3", "stderr_tail": (r.stderr or "")[-500:]}), 500

    mp3 = mp3s[0]
    log.info("download ok: %s via %s (%.1f MB)",
             mp3.name, "proxy" if used_proxy else "direct", mp3.stat().st_size / 1e6)

    resp = send_file(mp3, mimetype="audio/mpeg", as_attachment=True, download_name=mp3.name)
    resp.headers["X-Used-Proxy"] = "true" if used_proxy else "false"
    if first_fail:
        resp.headers["X-Direct-Failure"] = first_fail
    return resp


def _cleanup_loop():
    while True:
        try:
            time.sleep(300)
            cutoff = time.time() - 1800
            for p in Path("/tmp").glob("mpc-dl-*"):
                try:
                    if p.is_dir() and p.stat().st_mtime < cutoff:
                        shutil.rmtree(p, ignore_errors=True)
                except Exception:
                    pass
        except Exception as e:
            log.warning("cleanup loop error: %s", e)


threading.Thread(target=_cleanup_loop, daemon=True).start()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
