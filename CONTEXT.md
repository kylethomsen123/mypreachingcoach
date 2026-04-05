# My Preaching Coach — Session Context
*Paste this at the start of any new Claude.ai chat to resume work instantly.*

## Who I Am
Kyle Thomsen — Associate Pastor, preaching coach, builder. kylet@lifecconline.com
ADHD — I need clear, simple, shippable solutions. Limited dev time.

## What We're Building
A sermon analysis tool called My Preaching Coach. It transcribes sermons and evaluates them for gospel faithfulness, outputting a scored 5-page PDF report. Target users: associate pastors, senior pastors, preaching students.

## Two Tools
1. sermon_analyze.py (CLI — primary, working)
   - ~/Desktop/MyPreachingCoach/sermon_analyze.py
   - Python 3.11, yt-dlp or local file, OpenAI Whisper API (chunked)
   - Acoustics: librosa + scipy
   - Evaluation: Claude API — claude-sonnet-4-6
   - Output: JSON + PDF to ~/
   - Tested on 4 real sermons

2. sermon-analyzer Flask app — not yet saved to Mac, rebuild later

## PDF Report Layout (5 Pages)
P1 Cover: Title, preacher, passage, overall score, Big Idea, Sticky Statement, Encouragement, Top 3 Coaching Priorities
P2 Vocal Delivery: 7 elements with scores, acoustic measurements, narrative notes
P3 Sermon Structure: ME/WE/GOD/YOU/WE2 summaries, strength, growth edge, flags
P4 Gospel Check: G/O/S/P/E/L table, total, gold standard flag, narrative
P5 Summary: All scores + coaching priorities as action items

## GOSPEL Check Framework
Full doc: https://docs.google.com/document/d/1C6Hg8Le95oCtuGbsPno_way1KSZGAZEBB50U69fjsb8
G=Good/God's character (8pts), O=Obstacle/brokenness (8pts), S=Sin/personal complicity (8pts),
P=Perspective/fresh craft (6pts), E=Exalting Jesus (20pts), L=Lordship/Living (10pts). Total=60pts.
E Threshold: if E < 5 flag "Gospel Check: Incomplete". Gospel Gold Standard Flag: Yes/Partially/No.

## Test Results
Kyle Thomsen "Hope & Heartache" — 8/10
Joy Fishler "Joy of the Lord" — 6/10 (Gospel Check flag triggered)
Jessica Gray Jessup — 8/10
Joe Valenzuela "Generation We Need" — 6/10

## Current Model
claude-sonnet-4-6

## Where We Left Off
- Missing dependency: run pip3 install soundfile librosa scipy yt-dlp openai anthropic reportlab
- Verify --help works, then test on a real sermon
- Next: confirm model string, verify 5-page PDF layout, implement Andy Stanley structure on Page 3
- Flask app needs to be rebuilt when ready for web deployment

---
Resume prompt: "Let's continue building My Preaching Coach. Here's the context: [paste this file]"
