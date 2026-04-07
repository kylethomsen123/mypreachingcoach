# Deploying MyPreachingCoach

## Railway (Production)

This app requires two Railway services in one project:

### 1. WARP Proxy Service
- Source: Docker image `ghcr.io/mon-ius/docker-warp-socks:v5`
- No public domain needed (internal only)
- No variables needed (works out of the box)
- Exposes SOCKS5 proxy on port 9091

### 2. Web App Service
- Source: GitHub repo → `/web` directory
- Root directory: `web`
- Public domain: mypreachingcoach.org
- Variables:
  - `YTDLP_PROXY=socks5://warp.railway.internal:9091`
  - `ANTHROPIC_API_KEY=...`
  - `OPENAI_API_KEY=...`
  - `SENDGRID_API_KEY=...`
  - `FROM_EMAIL=...`
  - `FEEDBACK_FORM_URL=...`

### How it works
Cloudflare WARP gives the app a Cloudflare network IP instead of Railway's data-center IP. YouTube trusts Cloudflare IPs, so yt-dlp downloads work without cookies or authentication.

The WARP sidecar (docker-warp-socks) runs without NET_ADMIN privileges, making it compatible with Railway's shared container environment.

## Local Development
```bash
docker-compose up
# App at http://localhost:5050
```
