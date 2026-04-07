# Deploying MyPreachingCoach

## Railway (Production)

This app requires two Railway services in one project:

### 1. WARP Proxy Service
- Source: Docker image `caomingjun/warp`
- Internal networking: enabled (hostname: `warp`)
- No public domain needed (internal only)
- Variables: `WARP_SLEEP=2`

### 2. Web App Service
- Source: GitHub repo → `/web` directory
- Root directory: `web`
- Public domain: mypreachingcoach.org
- Variables:
  - `YTDLP_PROXY=socks5://warp:1080`
  - `ANTHROPIC_API_KEY=...`
  - `OPENAI_API_KEY=...`
  - `SENDGRID_API_KEY=...`
  - `FROM_EMAIL=...`
  - `FEEDBACK_FORM_URL=...`

### How it works
Cloudflare WARP gives the app a Cloudflare network IP instead of Railway's data-center IP. YouTube trusts Cloudflare IPs, so yt-dlp downloads work without cookies or authentication.

## Local Development
```bash
docker-compose up
# App at http://localhost:5050
```
