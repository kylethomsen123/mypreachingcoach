# MyPreachingCoach — Launch Readiness & Growth Report
*Generated: April 5, 2026 | Author: Kyle's AI Assistant*
*Based on deep code review of sermon_analyze.py (1,658 lines) and web/app.py*

---

## 🏁 TL;DR — Your Fastest Path to Launch

1. **Fix the 4 PDF bugs** (SermonPDF rewrite — already scoped in CLAUDE.md)
2. **Deploy to Railway** with a `.env` file and the current Flask app
3. **Add 5 intake questions** to `index.html` (takes 30 min)
4. **Wire your feedback Google Form** into the SendGrid email
5. **Send to 10 pastors personally** — don't open-register yet
6. **Run for 3 weeks**, collect feedback, then open up

Total prep time: ~3 hours of focused work.

---

## 1. INTAKE FORM — What to Ask Before Submission

### Currently Collected
- Name, email, YouTube URL or audio file upload
- Sermon type (expository/topical/narrative/liturgical) ← already in the code!

### Add These 5 Fields

```
1. Passage (text box)
   "What scripture passage did you preach from?" 
   → Pre-populates the report's passage field; reduces Claude guessing wrong
   
2. Sermon Title (text box)  
   "Sermon title (if you have one):"
   → Same — pre-populates cover page accurately

3. Church context (dropdown)
   "My context is..."
   [ ] Solo pastor / church plant (< 100 people)
   [ ] Associate or staff pastor
   [ ] Senior pastor (100–500)
   [ ] Senior pastor (500+)
   [ ] Seminary/Bible college student
   [ ] Other preaching instructor
   → Lets you segment feedback and eventually personalize coaching tone

4. What do you most want feedback on? (checkboxes, pick up to 2)
   [ ] Gospel clarity / Jesus as the hero
   [ ] Structure and flow  
   [ ] Vocal delivery
   [ ] Application / practical next steps
   [ ] All of the above
   → Sets user expectations; you can use this to weight your coaching email

5. Is this a live recording or a manuscript read-through?
   ( ) Live recording (congregation present)
   ( ) Practice run / read-through
   → Live recordings have different vocal benchmarks; this improves score accuracy
```

### Why These 5 Work
- **Passage + Title**: Claude currently guesses both from the transcript. Pre-filling reduces hallucination risk and saves ~500 tokens in the prompt.
- **Context**: Segmentation data for beta analysis. A seminary student and a megachurch pastor need different report tones.
- **Focus areas**: Manages expectations. Someone who only cares about structure won't be upset that their E score is low.
- **Live vs. read-through**: Acoustic benchmarks (pace, filler density, pauses) are calibrated for live delivery. A read-through shouldn't be penalized for slower pace.

### Optional (Phase 2)
- "How long have you been preaching?" (dropdown: <1yr, 1-3yr, 3-10yr, 10+yr)
- "Would you like a follow-up coaching conversation?" (yes/no with calendar link)

---

## 2. FOLLOW-UP PROCESS FOR BETA USERS

### The Core Problem with Pastors
Pastors are the busiest people on earth Thursday–Sunday. They're emotionally spent after preaching. **The best feedback window is Monday morning** (sermon debrief day) or Tuesday.

### The 3-Email Sequence

**Email 1: Report Delivery (Immediate — already built)**
Subject: `Your Preaching Coach Report is ready — [Sermon Title]`
- Attach PDF
- One sentence: "Scores are meant to coach, not judge."
- Link to 3-question Google Form (see below)
- "Reply to this email — I read every one."

**Email 2: Check-In (3 days later — needs to be built)**
Subject: `Did the report land? Quick question about [Sermon Title]`
Body (short):
> Hi [Name], just checking in — did the report give you anything useful to work with?
> 
> One question: **What was the most helpful part?** (Hit reply — one line is enough.)
> 
> If anything felt off or confusing, I want to know that too.
> — Kyle

**Email 3: The Ask (10 days later — if no reply to Email 2)**
Subject: `Still thinking about your preaching?`
Body:
> Hi [Name], two weeks ago you ran a sermon through My Preaching Coach.
> 
> If you got value from it — I'd love a 10-minute Zoom call. Just to hear what worked and what I should build next.
> 
> [Book a time: calendly.com/kyle] — No pitch, just listening.

### Implementation
This requires a simple drip sequence. Options:
- **Simplest**: ConvertKit free tier (1,000 subscribers, automations). Set tag "beta-user" on submit → trigger 3-email sequence.
- **Already have SendGrid**: Add a `send_followup_email()` function that gets triggered via a cron job (or Railway scheduled task) 3 days and 10 days after first email.
- **Easiest to start**: Add email 2 as a manual task to your weekly Monday checklist — just reply personally to last week's users.

---

## 3. FEEDBACK MECHANISMS — What Gets RESPONSES From Busy Pastors

### The 3-Question Google Form (embed link in PDF email)

Keep it **brutally short**. Three questions max:

```
1. On a scale of 1–5, how accurate did the report feel for this sermon?
   ⭐ ⭐ ⭐ ⭐ ⭐

2. What was most helpful? (pick one)
   [ ] Gospel Check scores
   [ ] Structure analysis
   [ ] Vocal delivery metrics
   [ ] The coaching priorities
   [ ] The overall score
   
3. What should I improve or add? (text box — optional)
```

**Why this works:**
- Under 60 seconds to complete
- Star rating + multi-choice reduces friction to near zero
- Optional text box means you still get qualitative data when someone has something to say

### Higher-Response Alternatives
1. **Reply-to-email is the highest-response mechanism for pastors.** The current SendGrid email already invites replies. Make sure those replies route to your real inbox, not a no-reply address.
2. **In the PDF itself**, add a QR code at the bottom of Page 5 linking to the Google Form. Pastors often share the PDF with their elders board — QR code means feedback from people who never got the email.
3. **After 3+ reports**, reach out with: "I've run 3 of your sermons. Want to do a 20-min call? I'll coach you live and it helps me improve the tool." Response rate on that offer will be high.

### What NOT to Do
- Don't send a long survey. Pastors won't complete it.
- Don't ask for feedback the same day as the report. Let it breathe.
- Don't ask "did you like it?" Ask "what was most useful?" — more actionable.

---

## 4. REDUCING TOKEN COSTS & API SPEND

### Current Cost Estimate
Your `CLAUDE_PROMPT` sends up to 40,000 chars of transcript to Claude.
- A 35-min sermon ≈ 5,000–6,000 words ≈ ~8,000 tokens input
- Full prompt (instructions + transcript) ≈ 12,000–15,000 tokens input
- Response (JSON) ≈ 1,500–2,500 tokens output
- **Per sermon: ~$0.05–$0.08 at sonnet-4-6 pricing**
- Also: Whisper API: ~$0.006/minute × 35min ≈ $0.21 per sermon

**Total per free beta run: ~$0.25–$0.35.** That's very reasonable.

### Ways to Reduce Further (if needed at scale)

**Option A: Transcript Truncation (already capped at 40k chars)**
Your code: `transcript[:40000]` — good. A 35-min sermon is usually 25,000–35,000 chars so you're fine. Consider adding a `--summarize-long` flag that pre-summarizes transcripts >50,000 chars using a cheaper model before the main evaluation.

**Option B: Switch GOSPEL Check to claude-haiku-3-5**
The GOSPEL scoring is deterministic-ish (rule-based scoring with rubrics). Haiku is 25× cheaper than Sonnet. You could:
1. Run Haiku for GOSPEL scoring (structured JSON output)
2. Run Sonnet only for narrative notes (the coaching language)
Cost reduction: ~60% per sermon.

**Option C: Cache Transcripts**
If someone submits the same YouTube URL twice, re-transcription costs $0.21 unnecessarily.
Add a simple file cache: `hash(url) → transcript.txt` in `reports/cache/`.
Single-line check before calling Whisper.

**Option D: Reduce Prompt Size**
The scoring rubric in your CLAUDE_PROMPT is ~800 tokens of instructions that repeat on every call. Consider:
- Moving the rubric to a system prompt (reused across calls, billed at cached rate with Anthropic's prompt caching)
- Anthropic's prompt caching: 90% cost reduction on repeated prompt prefixes. Your scoring rubric never changes — this is a perfect use case.

**Estimated savings with prompt caching: ~$0.02–$0.04 per sermon** = ~50% reduction in Claude costs.

### Practical Priority Order
1. **Enable Anthropic prompt caching** (highest ROI, ~2 hours to implement)
2. **Cache YouTube transcripts** (prevents double-billing, 30 min to implement)
3. **Consider Haiku for GOSPEL JSON** (if volume grows — revisit at 100+ sermons/month)

---

## 5. SPEEDING UP REPORT GENERATION

### Where Time Is Currently Spent (Estimated)

| Step | Time Estimate | Bottleneck? |
|------|--------------|-------------|
| yt-dlp download | 30–90 sec | Network + video size |
| ffmpeg convert + chunk | 15–45 sec | CPU |
| Whisper transcription | 60–180 sec | API round-trip |
| Acoustic analysis | 15–30 sec | scipy/numpy processing |
| Claude evaluation | 15–45 sec | API round-trip + JSON parsing |
| PDF generation | 3–8 sec | fpdf2 rendering |
| **Total** | **~3–7 min** | |

**Target: Get under 3 minutes for most sermons.**

### Speed Improvements (ranked by impact)

**1. Parallel Whisper Chunks (High Impact, Medium Effort)**
Your current code transcribes chunks sequentially. For a 45-min sermon (2 chunks), you're adding an extra ~60 sec unnecessarily.

```python
# Current (sequential):
for chunk in chunks:
    r = client.audio.transcriptions.create(...)
    parts.append(r)

# Improved (parallel with ThreadPoolExecutor):
from concurrent.futures import ThreadPoolExecutor
def transcribe_chunk(chunk):
    with open(chunk, "rb") as f:
        r = client.audio.transcriptions.create(model="whisper-1", file=f, response_format="text")
    return r if isinstance(r, str) else r.text

with ThreadPoolExecutor(max_workers=4) as ex:
    parts = list(ex.map(transcribe_chunk, chunks))
```
**Estimated savings: 45–90 sec for multi-chunk sermons**

**2. Audio Quality Reduction for Whisper (Medium Impact, Easy)**
You're sending full-quality MP3 to Whisper. Whisper only needs 16kHz mono audio. Downsample before chunking:

```bash
ffmpeg -i input.mp3 -ar 16000 -ac 1 -b:a 32k whisper_input.mp3
```
This reduces file size ~70%, which means faster uploads to Whisper API.
**Estimated savings: 20–40 sec**

**3. Skip Acoustic Analysis for URL Sources Without Local File**
Your `acoustic_analysis()` function requires downloading and re-processing the full audio. If you've already extracted the transcript, acoustic data can be estimated from transcript word count + Whisper metadata (which includes word-level timestamps on `verbose_json` format).

**4. Progress Feedback to User (Medium Priority)**
Right now users see a "submitted" page and wait in silence for an email. Consider:
- A `/status/<job_id>` polling endpoint that shows progress steps
- Or just better copy on `submitted.html`: "Your report takes 3–5 minutes. You'll get an email when it's ready."

This doesn't speed up generation but drastically reduces perceived wait time and support requests.

**5. Move to Celery + Redis on Railway (Long-term)**
Your current threading approach works for a handful of concurrent users but will fail under load. If you get 10+ simultaneous submissions, threads will compete for CPU on a single Railway instance.

For the beta: threading is fine. When you hit 50+ weekly users, migrate to:
- Celery task queue
- Redis as broker
- Railway's managed Redis add-on (~$5/month)

---

## 6. CODE EVALUATION — Current State & What to Fix Before Launch

### ✅ What's Already Working Well
- **Robust chunked transcription** with correct `-segment_time` (not `-segment_size` — you fixed a known ffmpeg bug)
- **Sermon type routing** with different Claude instructions per type (expository/topical/narrative/liturgical)
- **GOSPEL Check scoring** with proper E threshold flagging
- **SendGrid email** with PDF attachment already implemented
- **Feedback form URL** already wired into the email via `FEEDBACK_FORM_URL` env var

### 🔴 Must Fix Before Launch (Your 4 Known PDF Bugs)
Per your CLAUDE.md, these are already scoped:
1. **P2 vocal notes truncate** — multi_cell width fix
2. **"Growth ed" label clip** — cell width fix
3. **Content spills to P6/P7** — page break management
4. **Gospel table row misalignment** — multi_cell in table rows

### 🟡 Should Fix Before Launch
1. **No job status tracking** — if the background thread crashes, user never knows. Add error email: "Something went wrong with your sermon analysis — try again or reply to this email."
2. **No rate limiting** — a malicious user could submit 50 sermons and cost you $15. Add: one submission per email per 24 hours.
3. **500 MB upload limit is huge** — most sermon audio is 50–150 MB. Drop to 200 MB and add a visible file size note in the UI.
4. **The `.env` file isn't in the repo** — make sure `web/.env.example` documents all required vars before Railway deploy.

### 🟢 Nice to Have (Phase 2)
- Dashboard showing all beta reports (for your own analysis)
- Admin endpoint to view/download all submitted PDFs
- Email validation on form submit
- Webhook or Slack notification when a new sermon is submitted

---

## 7. DEPLOYMENT CHECKLIST — Railway.app

```
[ ] Fix the 4 PDF bugs (SermonPDF rewrite)
[ ] Create web/.env with all required keys:
    ANTHROPIC_API_KEY=
    OPENAI_API_KEY=
    SENDGRID_API_KEY=
    FROM_EMAIL=kyle@mypreachingcoach.com
    FEEDBACK_FORM_URL=https://forms.gle/...
[ ] Add Procfile: web: python web/app.py
[ ] Update web/requirements.txt to include:
    flask, sendgrid, python-dotenv, openai, anthropic, 
    fpdf2, soundfile, scipy, numpy, yt-dlp
[ ] Add ffmpeg buildpack in Railway settings
[ ] Set Railway start command to: python web/app.py
[ ] Verify PORT env var is used (Railway sets $PORT dynamically)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5050)))
[ ] Test with one live submission before sending to beta users
[ ] Add mypreachingcoach.com domain (or subdomain) in Railway
```

---

## 8. BETA LAUNCH STRATEGY

### Who Gets Access First (10 People)
- Michael Hill (Marin Covenant) — already your coaching client, highest trust
- 2–3 pastors from your ECC network
- 2–3 Jessup preaching students (BIBL 350)
- 1–2 pastor friends who will give you blunt feedback
- Yourself (run a test with your own sermon every time you update the tool)

### What to Say When You Invite Them

> "Hey [Name], I'm in the final week of beta testing a tool I've been building — it transcribes sermons and gives preachers an AI-powered coaching report based on the GOSPEL Check framework. I'd love for you to run one of your recent sermons through it. It's free, takes 3–5 minutes after you submit, and I'd love your honest feedback on the report. Here's the link: [URL]. Let me know what you think — I read every reply."

**Don't mass-announce yet.** Personal invites get 3× the engagement and better feedback.

### Open Beta Timeline
- **Week 1–2**: 10 personal invites
- **Week 3**: Share in one Facebook group or Slack community (e.g., The Preachers' Collective, Exponential Network)
- **Week 4–5**: Collect feedback, fix top issues
- **Week 6**: Post on LinkedIn/Twitter as a tool announcement

### Moving to Paid
The product is worth $15–25/month or $5–10/report to the right users. Signal for readiness:
- **3+ repeat users** (someone who submits more than once is a paying customer)
- **1 unsolicited "can I pay for this?" message** (you're already at value)
- **10+ completed feedback responses**

Suggested pricing:
- **Free**: 1 report/month (lead magnet, drives signups)
- **Coach tier** ($15/mo or $99/yr): Unlimited reports, history, 1 coaching email/month
- **Team/Seminary tier** ($49/mo): Up to 10 users, instructor dashboard

---

## 9. THE FEEDBACK FLYWHEEL

```
User submits sermon
        ↓
Report emailed (Immediate)
        ↓
3-question form link (In email + PDF QR code)
        ↓
Check-in email Day 3 (Reply to email = lowest friction feedback)
        ↓
Zoom invite Day 10 (For engaged users)
        ↓
Kyle learns what to improve → Better product → More word-of-mouth
```

**Key metric to track in Week 1**: Did the GOSPEL Check scores match the user's own sense of their sermon? That's the trust-builder. If users say "that's exactly right" — you have product-market fit.

---

## 10. SUMMARY — NEXT STEPS (IN ORDER)

| # | Task | Time | Priority |
|---|------|------|----------|
| 1 | Fix SermonPDF rendering bugs (4 known issues) | 2–3 hrs | 🔴 Must |
| 2 | Add 5 intake form questions to index.html | 30 min | 🔴 Must |
| 3 | Create FEEDBACK_FORM_URL Google Form (3 questions) | 15 min | 🔴 Must |
| 4 | Deploy to Railway with complete .env | 1 hr | 🔴 Must |
| 5 | Test end-to-end with your own sermon | 30 min | 🔴 Must |
| 6 | Send personal invites to 10 beta users | 30 min | 🔴 Must |
| 7 | Add error notification email for failed jobs | 45 min | 🟡 Should |
| 8 | Add rate limiting (1 submission/email/24hr) | 30 min | 🟡 Should |
| 9 | Implement parallel Whisper chunking | 1 hr | 🟡 Should |
| 10 | Enable Anthropic prompt caching | 2 hrs | 🟢 Nice |
| 11 | Add Day-3 follow-up email drip | 1 hr | 🟢 Nice |
| 12 | Build /status/<job_id> progress page | 2 hrs | 🟢 Nice |

**You can launch in one focused Saturday.** Steps 1–6 are all that's required.

---

*Note: The Council sub-agent parallel queries couldn't run (gateway offline at time of writing). This report reflects direct analysis of your codebase + domain expertise across product strategy, LLM cost optimization, and pastor engagement patterns.*
