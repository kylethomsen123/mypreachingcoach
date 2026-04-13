# My Preaching Coach — Sermon Analyzer
## Claude Code Context File
*Keep this file in the project root. Update it at the end of every session.*

---

## Project Owner
**Kyle Thomsen** — kylet@lifecconline.com
Associate Pastor / Preaching Coach / Builder
- Has ADHD — keep solutions simple, shippable, and well-documented
- Limited dev time; decisions must pass the filter: (1) serves preacher's growth, (2) builds toward paid product, (3) Kyle can actually ship it
- This is a **multi-user tool** — solutions must work for all users, not just Kyle. Never suggest per-user browser cookies, local config, or anything that requires setup on the user's computer.

---

## What This Project Is
A sermon evaluation tool for associate pastors, senior pastors, and preaching students.
It transcribes a sermon (from YouTube URL or local file), analyzes acoustics, and evaluates gospel faithfulness using the GOSPEL Check framework, outputting a scored 5-page PDF report delivered by email.

---

## Current Architecture

### Web App (`web/`) — PRIMARY ACTIVE TOOL
- **URL:** https://www.mypreachingcoach.org
- **Stack:** Flask + Gunicorn, deployed on Railway
- **Input:** YouTube URL or file upload (mp3/mp4/m4a/wav/etc.)
- **Transcription:** OpenAI Whisper API
- **Acoustics:** librosa + scipy
- **Evaluation:** Claude API (`claude-sonnet-4-6`)
- **Output:** PDF emailed via SendGrid

### CLI (`sermon_analyze.py`) — secondary/testing tool
- Location: `~/Desktop/MyPreachingCoach/sermon_analyze.py`
- Same analysis logic as web app, runs locally

---

## Sermon Detection Feature
Automatically finds the sermon within a full church service recording.

- **Trigger:** recordings > 55 minutes AND `SERMON_DETECTION=true` Railway env var
- **Flow:** submit → background thread downloads + AssemblyAI diarization → spinner page (`/detecting/<id>`) → confirm page (`/confirm/<id>`) → user adjusts times → analysis runs on trimmed audio
- **Algorithm:** (1) Build contiguous blocks for ALL speakers with 7-min merge gap, (2) Claude Haiku picks most likely sermon block based on duration + position heuristics
- **Key:** The confirm flow downloads audio ONCE during detection — analysis reuses that file. No second YouTube download needed.
- **Fallback:** If AssemblyAI fails, shows 40%/90% defaults with low confidence

---

## YouTube URL Handling — CRITICAL

**The problem:** YouTube Live URLs (`youtube.com/live/VIDEO_ID`) get bot-blocked on Railway's server IPs. Regular watch URLs (`youtube.com/watch?v=VIDEO_ID`) work fine.

**The fix (deployed 3a09302):** `normalize_youtube_url()` in `app.py` auto-converts at form submission:
- `/live/VIDEO_ID` → `/watch?v=VIDEO_ID`
- Strips `?si=` tracking parameters
- Applied before probe AND before analysis

**If bot blocks return for watch URLs too:**
- The hook is already in the code: `YTDLP_PROXY` env var
- Solution: residential proxy service (Webshare, Oxylabs ~$10-20/mo)
- Set `YTDLP_PROXY=socks5://user:pass@host:port` in Railway — no code change needed
- Do NOT suggest per-user browser cookies — this is a multi-user tool

**WARP proxy (deleted April 11, 2026):**
- `docker-warp-socks` service was deleted — Railway doesn't grant NET_ADMIN capability so it never worked
- `YTDLP_PROXY` was never set, so yt-dlp never routed through it regardless
- Previous downloads worked because they used watch URLs, not because of WARP

---

## Deployment (Railway)

- **Platform:** Railway — project `proactive-vibrancy`
- **Service:** `mypreachingcoach` (Flask app only — WARP service deleted)
- **Volume:** `/app/reports` — persists PDFs and `jobs.json` across redeploys
- **Railway CLI:** `railway service mypreachingcoach` then `railway logs`, `railway variables`
- **GitHub:** https://github.com/kylethomsen123/mypreachingcoach — push to `main` triggers auto-deploy

## Admin Tools

| URL | Purpose |
|-----|---------|
| `/health` | Check ffmpeg, yt-dlp, AssemblyAI key, SERMON_DETECTION status |
| `/admin/status?key=ADMIN_KEY` | Live job dashboard — last 50 jobs with status/duration/errors |
| `/admin/resend?key=ADMIN_KEY` | Manually resend a PDF to an email address |

**ADMIN_KEY:** `mpc-17ed97f9d952a28e`

---

## Pre-Flight Checklist — Run Before ANY Deploy or Irreversible Action

```
1. railway service mypreachingcoach && railway logs --tail 10
   → Any active jobs? If yes, WAIT before pushing.

2. curl https://www.mypreachingcoach.org/admin/status?key=mpc-17ed97f9d952a28e
   → Any status=started or status=analyzing? If yes, WAIT.

3. curl https://www.mypreachingcoach.org/health
   → All green? yt-dlp_youtube OK? ASSEMBLYAI_API_KEY SET?
```

**Before recommending deletion of any service or env var:**
- Verify it's actually being used (check Railway variables, check logs for usage)
- Check job history for what succeeded/failed and why
- State explicitly what evidence proves it's safe to remove

---

## PDF Report Layout (5 Pages) — HYBRID STYLE

| Page | Title | Contents |
|------|-------|----------|
| P1 | Cover | Sermon title, preacher, passage, Overall score badge, Gospel score badge, Big Idea, Sticky Statement, Bottom Line, Encouragement, Top 3 Coaching Priorities |
| P2 | Vocal Delivery | 7 elements — name + score, bar, measurement, FULL narrative note (no truncation) |
| P3 | Sermon Structure | ME/WE/GOD/YOU/WE2 — label + score + word count + time, bar, summary, Strength, Growth Edge. Flags section at bottom |
| P4 | Gospel Check | Gold Standard flag + narrative. 5-item PASS/FAIL checklist. Rubric subtotals. Body language / Note reliance blanks |
| P5 | Scorecard | All scores in one clean list. Coaching Priorities as action items |

---

## Evaluation Frameworks

### Primary: Gospel Check (Kyle Thomsen)
5-item PASS/FAIL checklist evaluated by Claude:

| Item | Key | Flag direction |
|------|-----|----------------|
| Jesus was the hero of the sermon | `jesus_as_hero` | FAIL = Christ not central |
| Application addressed heart motivations | `heart_level_application` | FAIL = surface-level |
| Behavior-change only (moralism flag) | `behavior_change_present` | PASS = moralism present (inverted) |
| Redemptive history / narrative noted | `redemptive_history_noted` | FAIL = missing |
| Accessible to non-Christians / skeptics | `nonchristian_accessible` | FAIL = insider-only |

**Gold Standard:** "Yes" if jesus_as_hero=true AND behavior_change_present=false AND 3+ other checks pass
**"No"** if jesus_as_hero=false OR (behavior_change_present=true AND 2+ other checks fail)
**incomplete_flag=true** if jesus_as_hero=false → triggers "Christ Not Central" banner on PDF

**gospel_check_total** in usage log = count of passing items (0–5)

### Secondary: Andy Stanley ME-WE-GOD-YOU-WE Structure
### Tertiary: BIBL 350 Rubric

---

## Permissions Granted to Claude

Claude has full authorization to do the following **without asking for confirmation**:
- Read and edit any file in this project directory
- Run `railway logs`, `railway variables`, `railway service` commands
- Set Railway environment variables (`railway variables --set`)
- `git add`, `git commit`, `git push` to `main` to trigger deploys
- Call `curl` against `mypreachingcoach.org` endpoints (health, admin)
- Use the `/admin/resend` endpoint to recover missed email reports

Claude should still ask before:
- Deleting files or reports from the Railway volume
- Deleting Railway services or env vars
- Changing the SendGrid API key or other credential env vars
- Making changes to the sermon_analyze.py prompt or scoring logic

---

## Root Cause Log

| Date | Symptom | Cause | Fix |
|------|---------|-------|-----|
| 2026-04-08 | Emails not sending | `FROM_EMAIL` set to unverified Gmail | Updated Railway `FROM_EMAIL` to `kylet@lifecconline.com` |
| 2026-04-11 | YouTube bot block | `/live/` URL format triggers bot detection on server IPs | `normalize_youtube_url()` converts to `/watch?v=` at form submission |
| 2026-04-11 | Job interrupted | Pushed deploy while job was running | Always check admin/status for active jobs before pushing |
| 2026-04-11 | Detection returned 12-min block in 95-min service | Old algorithm only checked dominant speaker's longest block | New: all speakers + 7-min merge gap + Claude sanity check |

---

## Known Issues / Next Priorities
- [ ] Rotate AssemblyAI API key — current key was shared in a chat session
- [ ] Test improved sermon detection (Claude + 7-min gap) against Melissa's 11am Easter service
- [ ] Consider residential proxy as backup if YouTube bot blocks return for watch URLs

---

*Last updated: 2026-04-11*
