"""
Client for the Hetzner download microservice.

The VM (code in /downloader-vm) runs yt-dlp + ffmpeg and exposes two endpoints:
  POST /probe    — returns {title, duration, uploader, id, used_proxy, direct_failure}
  POST /download — returns mp3 audio bytes + X-Used-Proxy / X-Direct-Failure headers

The VM tries Hetzner-direct first, then falls back to DataImpulse residential
proxy on YouTube bot-check or SABR failures. Railway never calls yt-dlp itself.

Railway env vars:
  DOWNLOADER_URL     — e.g. http://178.104.232.247:8000
  DOWNLOADER_SECRET  — shared secret sent as X-Auth-Token
"""
import os
import re
import time

import requests


DOWNLOADER_URL = (os.environ.get("DOWNLOADER_URL") or "").rstrip("/")
DOWNLOADER_SECRET = os.environ.get("DOWNLOADER_SECRET") or ""


class DownloaderError(RuntimeError):
    """Raised when the downloader VM returns a 5xx or fails to produce audio."""


def is_configured() -> bool:
    return bool(DOWNLOADER_URL and DOWNLOADER_SECRET)


def _headers() -> dict:
    return {"X-Auth-Token": DOWNLOADER_SECRET}


def probe(url: str, timeout: int = 60, retries: int = 2) -> dict:
    """Return {'title', 'duration', 'uploader', 'id', 'used_proxy', 'direct_failure'}."""
    last_exc = None
    for attempt in range(retries + 1):
        try:
            r = requests.post(
                f"{DOWNLOADER_URL}/probe",
                json={"url": url},
                headers=_headers(),
                timeout=timeout,
            )
            if r.status_code >= 500:
                raise DownloaderError(f"probe {r.status_code}: {r.text[:300]}")
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, DownloaderError) as e:
            last_exc = e
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
    raise DownloaderError(f"probe failed after {retries + 1} attempts: {last_exc}")


def download(url: str, dest_dir: str, timeout: int = 900, retries: int = 1) -> tuple[str, dict]:
    """Download audio to dest_dir. Returns (path, meta_headers).
    meta_headers contains X-Used-Proxy and X-Direct-Failure if present.
    """
    last_exc = None
    for attempt in range(retries + 1):
        try:
            r = requests.post(
                f"{DOWNLOADER_URL}/download",
                json={"url": url},
                headers=_headers(),
                timeout=timeout,
                stream=True,
            )
            if r.status_code >= 500:
                raise DownloaderError(f"download {r.status_code}: {r.text[:300]}")
            r.raise_for_status()

            cd = r.headers.get("Content-Disposition", "")
            m = re.search(r'filename="?([^";]+)"?', cd)
            filename = m.group(1) if m else "audio.mp3"
            if not filename.lower().endswith(".mp3"):
                filename += ".mp3"
            dest_path = os.path.join(dest_dir, filename)

            with open(dest_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)

            meta = {
                "used_proxy": r.headers.get("X-Used-Proxy", "unknown"),
                "direct_failure": r.headers.get("X-Direct-Failure"),
            }
            return dest_path, meta
        except (requests.RequestException, DownloaderError) as e:
            last_exc = e
            if attempt < retries:
                time.sleep(5 * (attempt + 1))
    raise DownloaderError(f"download failed after {retries + 1} attempts: {last_exc}")


def health(timeout: int = 10) -> dict:
    r = requests.get(f"{DOWNLOADER_URL}/health", timeout=timeout)
    r.raise_for_status()
    return r.json()
