# My Preaching Coach — Sermon Analyzer
## Claude Code Context File
*Keep this file in the project root. Update it at the end of every session.*

---

## Project Owner
**Kyle Thomsen** — kylet@lifecconline.com
Associate Pastor / Preaching Coach / Builder
- Has ADHD — keep solutions simple, shippable, and well-documented
- Limited dev time; decisions must pass the filter: (1) serves preacher's growth, (2) builds toward paid product, (3) Kyle can actually ship it

---

## What This Project Is
A sermon evaluation tool for associate pastors, senior pastors, and preaching students.
It transcribes a sermon (from YouTube URL or local file), analyzes acoustics, and evaluates gospel faithfulness using the GOSPEL Check framework, outputting a scored 5-page PDF report.

---

## Two Tools Exist

### 1. `sermon_analyze.py` — CLI (PRIMARY WORKING TOOL)
- **Location:** `~/Desktop/MyPreachingCoach/sermon_analyze.py`
- **Python:** 3.11
- **Input:** YouTube URL (via yt-dlp) OR local audio/video file
- **Transcription:** OpenAI Whisper API (chunked — uses `-segment_time 1200` NOT `-segment_size`)
- **Acoustics:** librosa + scipy
- **Evaluation:** Claude API (`claude-sonnet-4-6`)
- **Output:** JSON + PDF to `~/`
- **Dependencies:** `pip3 install soundfile librosa scipy yt-dlp openai anthropic fpdf2`

### 2. `sermon-analyzer` — Flask Web App (SECONDARY)
- Not currently saved on Mac — rebuild later when ready for web deployment

---

## PDF Report Layout (5 Pages) — HYBRID STYLE

**Design principle:** Old clean rendering style (full text, no truncation, proven working) + new GOSPEL scoring table on P4. NO pass/fail checklist anywhere.

| Page | Title | Contents |
|------|-------|----------|
| P1 | Cover | Sermon title, preacher, passage, Overall score badge, Gospel score badge, Big Idea, Sticky Statement, Bottom Line, Encouragement, Top 3 Coaching Priorities |
| P2 | Vocal Delivery | 7 elements — each gets: name + score, bar, measurement, FULL narrative note (no truncation). Must fit cleanly — use auto page break if needed |
| P3 | Sermon Structure | ME/WE/GOD/YOU/WE2 — each gets: label + score + word count + time, bar, summary, Strength, Growth Edge. Flags section at bottom |
| P4 | Gospel Check | Gold Standard flag + narrative. G/O/S/P/E/L scoring TABLE (letter, category, pts/max, bar, note). Total row. Rubric subtotals. Body language / Note reliance blanks |
| P5 | Scorecard | All scores in one clean list (Structure, Vocal, Gospel). Coaching Priorities repeated as action items |

**CRITICAL PDF rules:**
- NEVER truncate text — if it doesn't fit, wrap to next line or allow auto page break
- "Growth ed" truncation bug is KNOWN — label must be "Growth edge:" not a cell that clips
- Vocal notes on P2 were truncating mid-sentence in previous version — rewrite to use multi_cell with full CW width and no fixed row height
- Gospel table rows must use multi_cell for notes column, resetting X/Y after each row
- No orphaned single lines on their own page
- Target: exactly 5 pages for a typical 35-45 min sermon

---

## Evaluation Frameworks

### Primary: GOSPEL Check (Kyle Thomsen)
Full doc: https://docs.google.com/document/d/1C6Hg8Le95oCtuGbsPno_way1KSZGAZEBB50U69fjsb8

| Letter | Dimension | Points |
|--------|-----------|--------|
| G | Good — God's character | 8 |
| O | Obstacle — brokenness | 8 |
| S | Sin — personal complicity | 8 |
| P | Perspective — fresh craft | 6 |
| E | Exalting Jesus | 20 |
| L | Lordship / Living | 10 |
| **Total** | | **60** |

**E Threshold:** E < 5 → flag "Gospel Check: Incomplete"
**Gospel Gold Standard:** Yes / Partially / No

### Secondary: Andy Stanley ME-WE-GOD-YOU-WE Structure (P3)
### Tertiary: BIBL 350 Rubric (P4 subtotals)

---

## Test Results

| Preacher | Sermon | Score | Notes |
|----------|--------|-------|-------|
| Kyle Thomsen | "Hope & Heartache" (Psalm 42) | 7.4/10 | youtube.com/watch?v=gl9_xdYITvk |
| Joy Fishler | "Joy of the Lord" | 6/10 | Gospel Check: Incomplete (E < 5) |
| Jessica Gray Jessup | — | 8/10 | Passed all checks |
| Joe Valenzuela | "Generation We Need" | 6/10 | Lower G and E scores |

---

## Current Model
`claude-sonnet-4-6` — $3/$15 per million tokens input/output

---

## CURRENT TASK — PDF Rewrite Needed
The `SermonPDF` class in `sermon_analyze.py` has rendering bugs:
1. **P2 vocal notes truncate mid-sentence** — text clips at right margin instead of wrapping
2. **"Growth ed" on P3** — label cell is too narrow, clips "Growth edge:"
3. **Orphaned pages** — content spills to P6 or P7 instead of staying in 5 pages
4. **Gospel table rows break badly** — multi_cell inside table row causes misalignment

**The fix:** Full rewrite of the `SermonPDF` class only. Do NOT change anything above line ~449 (the PDF class starts at `class SermonPDF(FPDF):`). Use the hybrid layout spec above. Reference the old working PDF style (clean, full text, no clipping) as the visual target.

**To test:** Run `python3 ~/Desktop/MyPreachingCoach/sermon_analyze.py "https://youtu.be/gl9_xdYITvk?si=YjEKQrbrZMjPkvza" --name "Kyle Thomsen"` and check the output PDF.

---

## Architecture Notes
- Outputs: flat JSON + PDF to `~/`
- ffmpeg chunking: uses `-segment_time 1200` (time-based, NOT `-segment_size` bytes-based)
- Claude prompt receives: full transcript + acoustic summary → GOSPEL scores + narrative
- No database — stateless per-sermon analysis

---

## How to Resume
1. Read this file
2. Run: `pip3 install soundfile librosa scipy yt-dlp openai anthropic fpdf2`
3. Run: `python3 ~/Desktop/MyPreachingCoach/sermon_analyze.py --help`
4. Current task: rewrite SermonPDF class (see CURRENT TASK above)
5. Update "Known Issues" below when done

## Known Issues / Next Priorities
- [ ] **PRIORITY: Rewrite SermonPDF class** — see CURRENT TASK above
- [ ] Implement Andy Stanley ME-WE-GOD-YOU-WE detection (P3 already renders it)
- [ ] Add BIBL 350 rubric scoring option
- [ ] Build toward paid/shareable product (web deployment)

---

## Deployment (Railway)

- **App URL:** https://www.mypreachingcoach.org
- **Platform:** Railway — project `proactive-vibrancy`
- **Services:** `mypreachingcoach` (Flask app) + `docker-warp-socks` (WARP proxy)
- **Volume:** `/app/reports` — persists PDFs and `jobs.json` across redeploys
- **Railway CLI:** `railway service mypreachingcoach` to switch context, then `railway logs`, `railway variables`
- **GitHub:** https://github.com/kylethomsen123/mypreachingcoach — push to `main` triggers auto-deploy

## Admin Tools

| URL | Purpose |
|-----|---------|
| `/health` | Check ffmpeg, yt-dlp, WARP proxy |
| `/admin/status?key=ADMIN_KEY` | Live job dashboard — last 50 jobs with status/duration/errors |
| `/admin/resend?key=ADMIN_KEY` | Manually resend a PDF to an email address |

**ADMIN_KEY** is set in Railway env vars. Current value: `mpc-17ed97f9d952a28e`

## Permissions Granted to Claude

Claude has full authorization to do the following **without asking for confirmation**:

- Read and edit any file in this project directory
- Run `railway logs`, `railway variables`, `railway service` commands
- Set Railway environment variables (`railway variables --set`)
- `git add`, `git commit`, `git push` to `main` to trigger deploys
- Call `curl` against `mypreachingcoach.org` endpoints (health, admin)
- Use the `/admin/resend` endpoint to recover missed email reports
- Pip install packages locally for testing

Claude should still ask before:
- Deleting files or reports from the Railway volume
- Changing the SendGrid API key or other credential env vars
- Making changes to the sermon_analyze.py prompt or scoring logic

---

## Root Cause Log (known past incidents)

| Date | Symptom | Cause | Fix |
|------|---------|-------|-----|
| 2026-04-08 | Emails not sending | `FROM_EMAIL` set to unverified Gmail; SendGrid verified sender is `kylet@lifecconline.com` | Updated Railway `FROM_EMAIL` env var |

---

*Last updated: 2026-04-08*
