# Deploying MyPreachingCoach

## Architecture

Two pieces in production:

1. **Flask app on Railway** (project `proactive-vibrancy`, service `mypreachingcoach`) — handles uploads, transcription (Whisper), evaluation (Claude), PDF rendering, email (SendGrid).
2. **Downloader microservice on a Hetzner VM** (`178.104.232.247:8000`) — runs `yt-dlp` + `ffmpeg`. Tries direct from the Hetzner IP first, falls back to a DataImpulse residential proxy on bot-check or SABR failures. Code lives in `downloader-vm/` in this repo and is `scp`'d to the VM manually (no auto-deploy).

Why a separate VM: Railway's data-center IPs get bot-blocked by YouTube. Hetzner's IPs are clean enough to work on the first try most of the time, and DataImpulse picks up the rest at ~$1/GB instead of the previous Webshare $30/mo flat fee.

## Railway service variables

Required:
- `ANTHROPIC_API_KEY`
- `OPENAI_API_KEY`
- `SENDGRID_API_KEY`
- `FROM_EMAIL` (must be a SendGrid-verified sender)
- `FEEDBACK_FORM_URL`
- `DOWNLOADER_URL` — `http://178.104.232.247:8000`
- `DOWNLOADER_SECRET` — shared secret for `X-Auth-Token` header
- `ASSEMBLYAI_API_KEY` — only required if `SERMON_DETECTION=true`

Optional:
- `SERMON_DETECTION=true` — enables the auto-trim-to-sermon flow for recordings >55 min

Persistent volume: `/app/reports` (PDFs + `jobs.json`).

## Hetzner VM

- SSH: `ssh root@178.104.232.247`
- Service: `systemctl status mpc-downloader` — Gunicorn on port 8000
- Logs: `journalctl -u mpc-downloader -f`
- Code: `/opt/mpc-downloader/app.py`, unit at `/etc/systemd/system/mpc-downloader.service`
- Config: `/opt/mpc-downloader/config.env` (contains `DOWNLOADER_SECRET` and `DATAIMPULSE_PROXY`)
- Firewall: UFW allows 22 + 8000 only

To deploy a VM change: edit `downloader-vm/app.py` (or the unit file), `scp` to the VM, `systemctl daemon-reload && systemctl restart mpc-downloader`. Confirm no in-flight downloads first (`ps aux | grep yt-dlp`).

## Local development

```bash
cp .env.example .env  # fill in keys + DOWNLOADER_URL/DOWNLOADER_SECRET
docker-compose up
# App at http://localhost:5050
```

URL submissions hit the live Hetzner VM (or whichever `DOWNLOADER_URL` you point at). File uploads work without it.
