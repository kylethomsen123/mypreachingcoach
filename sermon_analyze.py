#!/usr/bin/env python3.11
"""
sermon_analyze.py — My Preaching Coach
Usage: python3.11 sermon_analyze.py <audio_or_youtube_url> --name "Speaker Name"
"""
import argparse, json, os, re, subprocess, sys, tempfile
from datetime import datetime
from pathlib import Path

# ── Deps ──────────────────────────────────────────────────────────────────────
for pkg, imp in [("anthropic","anthropic"),("openai","openai"),
                 ("numpy","numpy"),("soundfile","soundfile"),
                 ("scipy","scipy"),("fpdf","fpdf")]:
    try: __import__(imp)
    except ImportError: sys.exit(f"Missing: pip install {pkg if pkg!='fpdf' else 'fpdf2'}")

import anthropic, openai, numpy as np, soundfile as sf
from scipy.signal import find_peaks, correlate
from fpdf import FPDF

# ── Constants ─────────────────────────────────────────────────────────────────
OPENAI_API_KEY    = os.environ.get("OPENAI_API_KEY","")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY","")
WHISPER_MAX_BYTES = 24 * 1024 * 1024

FILLER_WORDS = ["um","uh","like","you know","basically","literally",
                "actually","so","right","okay","kind of","sort of"]


# ── Helpers ───────────────────────────────────────────────────────────────────
def safe(text, n=None):
    """Latin-1 safe string, optionally truncated."""
    if not isinstance(text, str): text = str(text)
    for k,v in {'\u2019':"'",' \u2018':"'",'\u201c':'"','\u201d':'"',
                '\u2014':'--','\u2013':'-','\u2026':'...','\u00e9':'e',
                '\u00e0':'a','\u00e8':'e','\u00ea':'e','\u00f4':'o',
                '\u00e2':'a','\u00ee':'i','\u00fb':'u'}.items():
        text = text.replace(k,v)
    text = text.encode('latin-1','replace').decode('latin-1')
    if n and len(text) > n: text = text[:n-3]+"..."
    return text

def terminal_bar(score, max_pts, width=None):
    if width is None: width = max_pts
    filled = round(score / max_pts * width) if max_pts else 0
    return "\u2588"*filled + "\u2591"*(width-filled)

def get_benchmark_label(score: float) -> str:
    """Map a 0-10 score to a qualitative benchmark tier label."""
    if score <= 2: return "Foundational"
    if score <= 4: return "Developing"
    if score <= 6: return "Emerging"
    if score <= 8: return "Proficient"
    return "Exemplary"

# ── Step 1: Acquire audio ──────────────────────────────────────────────────────
def _ytdlp_bin() -> str:
    return "/usr/local/bin/yt-dlp" if os.path.exists("/usr/local/bin/yt-dlp") else "yt-dlp"


def get_youtube_info(url: str) -> dict:
    """
    Fetch YouTube metadata without downloading the video.
    Returns {"uploader": str, "title": str} or {} on failure.
    yt-dlp --dump-json implies --simulate so no audio is downloaded.
    Checks creator > artist > uploader in that order for the speaker name.
    """
    try:
        result = subprocess.run(
            [_ytdlp_bin(), "--dump-json", "--no-warnings", url],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            info = json.loads(result.stdout.strip())
            # Prefer the most-specific person field over channel/org names
            name = (info.get("creator") or info.get("artist")
                    or info.get("uploader") or "")
            return {
                "uploader": name,
                "title":    info.get("title", ""),
            }
    except Exception:
        pass
    return {}


def acquire_audio(source: str, tmpdir: str) -> str:
    p = Path(source)
    if p.exists() and p.suffix.lower() in {".m4a",".mp3",".wav",".flac",".aac",".ogg"}:
        if p.suffix.lower() == ".mp3":
            return str(p)
        out = os.path.join(tmpdir, "audio.mp3")
        print(f"  Converting {p.suffix} -> mp3 via ffmpeg ...")
        subprocess.run(["ffmpeg","-y","-i",str(p),"-q:a","4",out],
                       check=True, capture_output=True)
        return out
    print("  Downloading via yt-dlp ...")
    subprocess.run([_ytdlp_bin(),"-x","--audio-format","mp3",
                    "-o",os.path.join(tmpdir,"%(title)s.%(ext)s"),source], check=True)
    files = list(Path(tmpdir).glob("*.mp3"))
    if not files: sys.exit("yt-dlp produced no mp3.")
    return str(files[0])

# ── Step 2: Transcribe ────────────────────────────────────────────────────────
def transcribe(mp3_path: str) -> str:
    client = openai.OpenAI(api_key=OPENAI_API_KEY)
    size = os.path.getsize(mp3_path)
    if size <= WHISPER_MAX_BYTES:
        print(f"  Single chunk ({size/1e6:.1f} MB) ...")
        with open(mp3_path,"rb") as f:
            r = client.audio.transcriptions.create(model="whisper-1",file=f,response_format="text")
        text = r if isinstance(r,str) else r.text
        print(f"  Words: {len(text.split()):,}  |  Chunks: 1")
        return text
    cdir = os.path.join(os.path.dirname(mp3_path),"chunks")
    os.makedirs(cdir, exist_ok=True)
    print(f"  {size/1e6:.1f} MB > 24 MB — splitting ...")
    subprocess.run(["ffmpeg","-y","-i",mp3_path,"-f","segment",
                    "-segment_time","1200","-c","copy",
                    os.path.join(cdir,"chunk_%03d.mp3")],
                   check=True, capture_output=True)
    chunks = sorted(Path(cdir).glob("chunk_*.mp3"))
    print(f"  Transcribing {len(chunks)} chunks ...")
    parts = []
    for i,chunk in enumerate(chunks,1):
        print(f"    chunk {i}/{len(chunks)} ...", end="\r")
        with open(chunk,"rb") as f:
            r = client.audio.transcriptions.create(model="whisper-1",file=f,response_format="text")
        parts.append(r if isinstance(r,str) else r.text)
    text = " ".join(parts)
    print(f"\n  Words: {len(text.split()):,}  |  Chunks: {len(chunks)}")
    return text

# ── Step 3: Acoustic analysis ─────────────────────────────────────────────────
def _rms_frames(y: np.ndarray, frame_len: int, hop_len: int) -> np.ndarray:
    """Compute per-frame RMS energy using numpy stride tricks."""
    n_frames = max(1, 1 + (len(y) - frame_len) // hop_len)
    pad = frame_len + (n_frames - 1) * hop_len - len(y)
    if pad > 0:
        y = np.pad(y, (0, pad))
    shape   = (n_frames, frame_len)
    strides = (y.strides[0] * hop_len, y.strides[0])
    frames  = np.lib.stride_tricks.as_strided(y, shape=shape, strides=strides)
    return np.sqrt(np.mean(frames.astype(np.float64)**2, axis=1) + 1e-12).astype(np.float32)


def _pitch_cv(y: np.ndarray, sr: int) -> float:
    """Pitch coefficient-of-variation via autocorrelation (no librosa/numba needed)."""
    frame_len = int(sr * 0.04)
    hop_len   = int(sr * 0.20)
    min_lag   = max(1, int(sr / 400))
    max_lag   = int(sr / 80)

    rms_vals = np.array([
        np.sqrt(np.mean(y[s:s+frame_len].astype(np.float64)**2) + 1e-12)
        for s in range(0, max(1, len(y)-frame_len), hop_len)
    ])
    if len(rms_vals) == 0:
        return 0.15
    energy_thresh = float(np.percentile(rms_vals, 30))

    f0_list = []
    for start in range(0, len(y) - frame_len, hop_len):
        frame = y[start:start+frame_len].astype(np.float64)
        if np.sqrt(np.mean(frame**2)) < energy_thresh:
            continue
        norm = float(np.dot(frame, frame))
        if norm < 1e-10:
            continue
        ac_full = correlate(frame, frame, mode='full')
        ac = ac_full[len(ac_full)//2:] / (norm + 1e-10)
        if max_lag >= len(ac):
            continue
        sub = ac[min_lag:max_lag]
        peaks, props = find_peaks(sub, height=0.3, distance=3)
        if len(peaks):
            best_lag = int(peaks[np.argmax(props['peak_heights'])]) + min_lag
            f0_list.append(sr / best_lag)

    if len(f0_list) >= 20:
        arr = np.array(f0_list)
        return float(np.std(arr) / (np.mean(arr) + 1e-10))
    return 0.15


def acoustic_analysis(mp3_path: str, transcript: str) -> dict:
    wav_path = mp3_path.replace(".mp3", "_analysis.wav")
    print("  Converting to wav for analysis ...")
    subprocess.run(
        ["ffmpeg", "-y", "-i", mp3_path, "-ar", "16000", "-ac", "1", wav_path],
        check=True, capture_output=True,
    )
    print("  Loading audio ...")
    y, sr = sf.read(wav_path, dtype="float32")
    if y.ndim > 1:
        y = y.mean(axis=1)

    dur_sec = len(y) / sr
    dur_min = dur_sec / 60.0
    words   = len(transcript.split())
    wpm     = words / dur_min if dur_min else 0

    lower = transcript.lower()
    filler_counts = {}
    for fw in FILLER_WORDS:
        c = len(re.findall(r'\b' + re.escape(fw) + r'\b', lower))
        if c:
            filler_counts[fw] = c
    filler_total   = sum(filler_counts.values())
    filler_per_min = filler_total / dur_min if dur_min else 0
    top_fillers    = sorted(filler_counts.items(), key=lambda x: -x[1])[:5]

    fl  = int(sr * 0.03)
    hl  = int(sr * 0.01)
    rms = _rms_frames(y, fl, hl)
    thresh = float(np.percentile(rms, 20))

    in_p = False; plen = 0; pause_count = 0
    min_pause_frames = int(0.4 / 0.01)
    for v in rms:
        if v < thresh:
            in_p = True; plen += 1
        else:
            if in_p and plen >= min_pause_frames:
                pause_count += 1
            in_p = False; plen = 0

    active     = int(np.sum(rms > thresh))
    talk_ratio = active / len(rms) if len(rms) else 0.85

    peak = float(np.max(np.abs(y))) + 1e-10
    db   = 20.0 * np.log10(np.abs(y.astype(np.float64)) / peak + 1e-10)
    dynamic_range_db = round(float(np.percentile(db, 95)) - float(np.percentile(db, 5)), 1)

    print("  Estimating pitch variation ...")
    pitch_cv = _pitch_cv(y, sr)

    t  = max(1, len(y) // 3)
    e1 = float(np.sqrt(np.mean(y[:t].astype(np.float64)**2)    + 1e-10))
    e2 = float(np.sqrt(np.mean(y[t:2*t].astype(np.float64)**2) + 1e-10))
    e3 = float(np.sqrt(np.mean(y[2*t:].astype(np.float64)**2)  + 1e-10))
    if   e3 >= e2 >= e1: arc_pattern = "building"
    elif e2 >= e3 >= e1: arc_pattern = "peaks-middle"
    elif e3 >= e1:       arc_pattern = "recovery"
    else:                arc_pattern = "declining"

    def s_wpm(v):
        if 130<=v<=165: return 10
        if 120<=v<130 or 165<v<=180: return 8
        if 100<=v<120 or 180<v<=200: return 6
        if 80<=v<100  or 200<v<=220: return 4
        return 2
    def s_fpm(v):
        if v<0.5: return 10
        if v<1.0: return 8
        if v<2.0: return 6
        if v<3.5: return 4
        return 2
    def s_dr(v):
        if v>=35: return 10
        if v>=28: return 8
        if v>=20: return 6
        if v>=14: return 4
        return 2
    def s_pause(cnt, dm):
        ppm = cnt / dm if dm else 0
        if 2<=ppm<=6:  return 10
        if 1<=ppm<2 or 6<ppm<=9:   return 7
        if 0.3<=ppm<1 or 9<ppm<=13: return 5
        return 3
    def s_variety(cv):
        if cv>=0.28: return 10
        if cv>=0.20: return 8
        if cv>=0.12: return 6
        if cv>=0.06: return 4
        return 2
    def s_tts(r):
        p = r * 100
        if 78<=p<=92: return 10
        if 70<=p<78 or 92<p<=96: return 7
        return 4
    def s_arc(pat):
        return {"building":10,"recovery":8,"peaks-middle":6,"declining":4}.get(pat, 5)

    return {
        "duration_min":        round(dur_min, 1),
        "word_count":          words,
        "estimated_wpm":       round(wpm, 1),
        "filler_count":        filler_total,
        "filler_per_minute":   round(filler_per_min, 2),
        "top_fillers":         top_fillers,
        "pause_count":         pause_count,
        "dynamic_range_db":    dynamic_range_db,
        "talk_ratio":          round(talk_ratio, 3),
        "pitch_cv":            round(pitch_cv, 3),
        "arc_pattern":         arc_pattern,
        "arc_thirds":          {"start": round(e1,4), "middle": round(e2,4), "end": round(e3,4)},
        "wpm_score":           s_wpm(wpm),
        "filler_score":        s_fpm(filler_per_min),
        "dynamic_range_score": s_dr(dynamic_range_db),
        "pause_score":         s_pause(pause_count, dur_min),
        "vocal_variety_score": s_variety(pitch_cv),
        "talk_silence_score":  s_tts(talk_ratio),
        "energy_arc_score":    s_arc(arc_pattern),
    }

SERMON_TYPE_INSTRUCTIONS = {
    "expository": (
        "This is an expository sermon. Evaluate how well the preacher derives the "
        "message directly from the biblical text, working through the passage. "
        "Favor structure that follows the text's own outline and argument."
    ),
    "topical": (
        "This is a topical sermon. Evaluate how well the preacher addresses a "
        "specific theme using multiple scriptures. Look for thematic coherence, "
        "proper exegesis of supporting texts, and a clear central claim."
    ),
    "narrative": (
        "This is a narrative sermon. Evaluate story structure and delayed resolution. "
        "Tension, turning point, and resolution are key structural markers."
    ),
    "liturgical": (
        "This is a liturgical sermon (e.g. baptism, communion, funeral, wedding). "
        "Evaluate how well the sermon serves its liturgical context while remaining "
        "biblically grounded and gospel-centered."
    ),
}

# ── Step 4: Claude evaluation ─────────────────────────────────────────────────
CLAUDE_PROMPT = """\
You are an expert preaching coach. Analyze the sermon below and return ONLY valid JSON -- no markdown, no commentary.

SPEAKER: {speaker}
SERMON TYPE: {sermon_type}
TYPE CONTEXT: {sermon_type_instructions}
DURATION: {duration_min} min  |  WPM: {estimated_wpm}  |  FILLERS: {filler_count} total ({filler_per_minute}/min)
PAUSES: {pause_count}  |  DYNAMIC RANGE: {dynamic_range_db} dB  |  PITCH CV: {pitch_cv:.3f}
TALK RATIO: {talk_ratio_pct}%  |  ENERGY ARC: {arc_pattern}
AUDIO AVAILABLE: {has_audio}{has_audio_note}

TRANSCRIPT:
{transcript}

Return this exact JSON (no extra keys):
{{
  "sermon_title": "<string>",
  "passage": "<scripture ref, e.g. John 3:16-21>",
  "bottom_line": "<one memorable sentence>",
  "central_idea": "<central theological claim of the sermon -- 1 sentence>",
  "sticky_statement": "<assessment of the bottom line as a memorable/portable phrase -- 1 sentence>",
  "encouragement": "<2-3 sentences on what this preacher does well>",
  "growth_edges": ["<growth edge 1>","<growth edge 2>","<growth edge 3>"],
  "structure": {{
    "overall_score": 0,
    "sections": [
      {{"label":"ME",  "title":"Personal Hook",   "summary":"<1 sentence>","start_quote":"<first ~15 words>","word_count":0,"estimated_minutes":0.0,"score":0,"strength":"<1 sentence>","growth":"<1 sentence>"}},
      {{"label":"WE",  "title":"Universal Bridge", "summary":"...","start_quote":"...","word_count":0,"estimated_minutes":0.0,"score":0,"strength":"...","growth":"..."}},
      {{"label":"GOD", "title":"Biblical Text",    "summary":"...","start_quote":"...","word_count":0,"estimated_minutes":0.0,"score":0,"strength":"...","growth":"..."}},
      {{"label":"YOU", "title":"Application",      "summary":"...","start_quote":"...","word_count":0,"estimated_minutes":0.0,"score":0,"strength":"...","growth":"..."}},
      {{"label":"WE2", "title":"Vision / Sendoff", "summary":"...","start_quote":"...","word_count":0,"estimated_minutes":0.0,"score":0,"strength":"...","growth":"..."}}
    ],
    "flags": [{{"severity":"warning","text":"<description>"}}]
  }},
  "vocal": {{
    "filler_words":             {{"count":{filler_count},"per_minute":{filler_per_minute},"examples":["<word1>","<word2>","<word3>"],"score":0,"notes":"<1-2 sentence coaching note>"}},
    "pace":                     {{"avg_wpm":{estimated_wpm},"assessment":"fast|ideal|slow","score":0,"notes":"<1-2 sentences>"}},
    "rhetorical_variation":     {{"score":0,"db":{dynamic_range_db},"notes":"<1-2 sentences on volume range and expressiveness>"}},
    "landing_space":            {{"score":0,"count":{pause_count},"avg_duration_sec":0.5,"notes":"<1-2 sentences on pause quality and placement>"}},
    "pitch_variety":            {{"score":0,"notes":"<1-2 sentences on pitch variation (cv={pitch_cv:.3f})>"}},
    "cognitive_breathing_room": {{"score":0,"talk_pct":{talk_ratio_pct},"notes":"<1-2 sentences on talk-to-silence balance>"}},
    "rhetorical_arc":           {{"score":0,"notes":"<1-2 sentences on arc pattern: {arc_pattern}>"}},
    "verbal_clarity":           {{"score":0,"notes":"<1-2 sentences on word choice, sentence complexity, and clarity of expression>"}}
  }},
  "gospel_check": {{
    "jesus_as_hero":            true,
    "heart_level_application":  true,
    "behavior_change_present":  true,
    "redemptive_history_noted": true,
    "nonchristian_accessible":  true,
    "notes": "<2-3 sentence overall gospel evaluation>",
    "G_score":0,"G_note":"<1 sentence -- God character depiction>",
    "O_score":0,"O_note":"<1 sentence -- obstacle/brokenness clarity>",
    "S_score":0,"S_note":"<1 sentence -- sin/complicity honesty>",
    "P_score":0,"P_note":"<1 sentence -- perspective/craft/illustration>",
    "E_score":0,"E_note":"<1 sentence -- how explicitly Jesus is exalted>",
    "L_score":0,"L_note":"<1 sentence -- lordship/transformed living call>",
    "gold_standard":"Yes|Partially|No",
    "gold_standard_note":"<1 sentence explaining verdict>",
    "incomplete_flag":false
  }},
  "rubric": {{
    "exegesis_theology": {{"context_set":0,"main_point_clear":0,"preached_jesus":0,"redemptive_history":0}},
    "application":       {{"clear_helpful_application":0,"gospel_centered":0,"clear_response":0,"heart_care":0,"nonchristian_friendly":0}},
    "presentation":      {{"engaging_intro":0,"clear_structure":0,"voice_inflection":0}}
  }}
}}

GOSPEL scoring (each 0-10):
G  Does the sermon reveal WHO God IS beyond "God loves you"? Specific attributes?
O  Is the human problem felt, named, real? Does it resonate with life?
S  Is sin named honestly -- implicating the hearer -- without moralism?
P  Story/illustration/craft that earns the right to be heard?
E  Is Jesus the HERO? Cross/resurrection explicitly central, not assumed?
L  Grace-motivated call to concrete, specific transformed living?

gold_standard="Yes"     only if E>=8 AND 4+ other scores >=7
gold_standard="No"      if E<5 OR fewer than 2 scores >=5
Otherwise "Partially".  incomplete_flag=true if E<5.

Vocal score guide (use acoustic measurements above):
filler:       <0.5/min=10, <1.0=8, <2.0=6, <3.5=4, else 2
pace:         130-165wpm=10, 120-180=8, 100-200=6, else lower
rhetorical_variation (dynamic range): >=35dB=10, >=28=8, >=20=6, >=14=4, else 2
landing_space (pauses): 2-6/min=10, 1-9/min=7, else 5
pitch_variety:cv>=0.28=10, >=0.20=8, >=0.12=6, >=0.06=4, else 2{pitch_variety_note}
cognitive_breathing_room (talk ratio): 78-92%=10, 70-96%=7, else 4
rhetorical_arc:   building=10, recovery=8, peaks-middle=6, declining=4
verbal_clarity:   Score 1-10 based on word choice simplicity, sentence length, jargon avoidance,
                  and how accessible the language is to a general audience.
                  10=exceptionally clear, 7=mostly clear with minor jargon, 4=often complex or unclear.

Rubric scoring (each 0-5):
exegesis_theology: context_set=historical/cultural context set; main_point_clear=single clear thesis;
  preached_jesus=Jesus explicitly central; redemptive_history=fits redemptive narrative
application: clear_helpful_application=practical steps; gospel_centered=flows from grace not law;
  clear_response=listener knows what to do; heart_care=addresses motivations; nonchristian_friendly=accessible to skeptic
presentation: engaging_intro=hook earns attention; clear_structure=logical flow; voice_inflection=varied delivery
"""


def evaluate_with_claude(transcript: str, speaker: str, acoustic: dict,
                         sermon_type: str = "expository",
                         has_audio: bool = True) -> dict:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    has_audio_note = (
        "\nNOTE: No audio file -- set pitch_variety score to 0,"
        " notes to 'Not scored -- no audio file.'"
        if not has_audio else ""
    )
    pitch_variety_note = (
        "\n                  (Not available -- no audio file)"
        if not has_audio else ""
    )
    prompt = CLAUDE_PROMPT.format(
        transcript=transcript[:40000],
        speaker=speaker,
        sermon_type=sermon_type,
        sermon_type_instructions=SERMON_TYPE_INSTRUCTIONS.get(
            sermon_type, SERMON_TYPE_INSTRUCTIONS["expository"]
        ),
        has_audio=has_audio,
        has_audio_note=has_audio_note,
        pitch_variety_note=pitch_variety_note,
        duration_min=acoustic["duration_min"],
        estimated_wpm=acoustic["estimated_wpm"],
        filler_count=acoustic["filler_count"],
        filler_per_minute=acoustic["filler_per_minute"],
        dynamic_range_db=acoustic["dynamic_range_db"],
        pause_count=acoustic["pause_count"],
        pitch_cv=acoustic["pitch_cv"],
        talk_ratio_pct=round(acoustic["talk_ratio"] * 100),
        arc_pattern=acoustic["arc_pattern"],
    )
    msg = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=4096,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = msg.content[0].text.strip()
    raw = re.sub(r'^```(?:json)?\s*', '', raw)
    raw = re.sub(r'\s*```$', '', raw)
    return json.loads(raw)


# ── PDF color palette ──────────────────────────────────────────────────────────
C_NAVY  = (25,  55, 110)
C_LGRAY = (235, 235, 235)
C_MGRAY = (150, 150, 150)
C_DGRAY = (80,  80,  80)
C_RED   = (180, 30,  30)
C_GREEN = (30,  140, 60)


# ── SermonPDF ─────────────────────────────────────────────────────────────────
class SermonPDF(FPDF):
    """
    5-page sermon evaluation PDF.

    Page 1 -- Cover        : sermon title, score badges, big idea,
                             bottom line, encouragement, growth edges
    Page 2 -- Structure    : ME / WE / GOD / YOU / WE section cards with
                             colored bands, score bars, quotes, summary,
                             strength, growth edge (never truncated)
    Page 3 -- Vocal        : 8 acoustic/rhetorical elements, score bars,
                             measurement lines, full coaching notes
    Page 4 -- Gospel Check : Gold Standard badge, GOSPEL scoring table
                             (G/O/S/P/E/L), rubric subtotals, manual blanks
    Page 5 -- Scorecard    : all scores at a glance, coaching priorities
    """

    M  = 15.0    # left/right margin mm
    CW = 185.9   # usable content width (Letter 215.9 - 2*15)

    GOLD   = (175, 130,  25)   # headings, passage, section labels
    ORANGE = (200, 110,  20)   # flags, medium-score bars

    # Colored band per Andy Stanley section label
    SEC_CLR = {
        "ME":  ( 50, 100, 180),   # blue
        "WE":  ( 35, 130,  90),   # teal
        "GOD": (185,  90,  25),   # orange-brown
        "YOU": (145,  35,  35),   # dark red
        "WE2": ( 90,  50, 150),   # purple
    }

    # GOSPEL dimensions: (letter, display name, max weighted points)
    GOSPEL_ROWS = [
        ("G", "Good -- God's Character",    8),
        ("O", "Obstacle -- Brokenness",     8),
        ("S", "Sin -- Personal Complicity", 8),
        ("P", "Perspective -- Fresh Craft", 6),
        ("E", "Exalting Jesus",            20),
        ("L", "Lordship / Living",         10),
    ]

    # ── FPDF overrides ────────────────────────────────────────────────────────

    def __init__(self):
        super().__init__(orientation="P", unit="mm", format="Letter")
        self.set_auto_page_break(auto=True, margin=15)
        self.set_margins(self.M, self.M, self.M)
        self._date = datetime.now().strftime("%B %d, %Y")

    def header(self):
        pass   # each page draws its own top bar

    def footer(self):
        self.set_y(-12)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(*C_MGRAY)
        self.cell(0, 5,
                  f"My Preaching Coach  |  AI-Powered Sermon Evaluation"
                  f"  |  Page {self.page_no()}",
                  align="C")
        self.set_text_color(0, 0, 0)

    # ── Shared drawing helpers ─────────────────────────────────────────────────

    def _top_bar(self, right_text: str):
        """Navy header band: branding + date left, right_text right."""
        self.set_fill_color(*C_NAVY)
        self.rect(self.M, self.M, self.CW, 8, "F")
        self.set_xy(self.M + 2, self.M + 1)
        self.set_font("Helvetica", "B", 9)
        self.set_text_color(255, 255, 255)
        # Left side: "My Preaching Coach Report · Date"
        self.cell(self.CW * 0.70, 6,
                  safe(f"My Preaching Coach Report \xb7 {self._date}"))
        # Right side: page label or section name
        self.set_xy(self.M, self.M + 1)
        self.cell(self.CW, 6, safe(right_text), align="R")
        self.set_text_color(0, 0, 0)
        self.set_xy(self.M, self.M + 11)   # content starts 11 mm below top

    def _rule(self, gap_before: float = 2, gap_after: float = 3):
        """Thin horizontal rule with optional vertical padding."""
        if gap_before:
            self.ln(gap_before)
        y = self.get_y()
        self.set_draw_color(*C_MGRAY)
        self.set_line_width(0.25)
        self.line(self.M, y, self.M + self.CW, y)
        self.set_line_width(0.2)
        self.set_draw_color(0, 0, 0)
        if gap_after:
            self.ln(gap_after)

    def _page_title(self, text: str):
        """Bold navy section heading followed by a thin rule."""
        self.set_x(self.M)
        self.set_font("Helvetica", "B", 13)
        self.set_text_color(*C_NAVY)
        self.multi_cell(self.CW, 8, safe(text))
        self.set_text_color(0, 0, 0)
        self._rule(gap_before=1, gap_after=3)

    def _score_bar(self, score: float, max_score: float = 10,
                   height: float = 3):
        """Proportional fill bar: green >=8, red <=4, orange otherwise."""
        col    = C_GREEN if score >= 8 else (C_RED if score <= 4 else self.ORANGE)
        fill_w = self.CW * score / max_score if max_score else 0
        y = self.get_y()
        # Gray background track
        self.set_fill_color(*C_LGRAY)
        self.rect(self.M, y, self.CW, height, "F")
        # Colored fill
        if fill_w > 0:
            self.set_fill_color(*col)
            self.rect(self.M, y, fill_w, height, "F")
        self.set_fill_color(255, 255, 255)
        self.ln(height + 2)

    def _check_page(self, min_space: float = 40):
        """
        Break to a new page if fewer than min_space mm remain before the
        auto-page-break margin.  Redraws the top bar so every continuation
        page is properly headed.
        """
        if self.get_y() > self.h - 15 - min_space:
            self.add_page()
            self._top_bar(f"Page {self.page_no()}")
            self.set_x(self.M)

    def _rubric_block(self, title: str, items: list,
                      subtotal: int, max_pts: int):
        """
        Render one rubric category block.
        title    -- category heading, e.g. "Exegesis and Theology /20"
        items    -- list of (label_str, score_int) tuples, each out of 5
        subtotal -- sum of item scores
        max_pts  -- maximum possible points for the category
        """
        # Gold category heading
        self.set_x(self.M)
        self.set_font("Helvetica", "B", 10)
        self.set_text_color(*self.GOLD)
        self.cell(self.CW, 6, safe(title), new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(0, 0, 0)

        score_col_w = 22   # width for "  X  / 5" column
        lbl_w       = self.CW - score_col_w

        for label, score in items:
            y0 = self.get_y()
            self.set_x(self.M)
            self.set_font("Helvetica", "", 9)
            # Bullet prefix keeps visual alignment with the heading
            self.multi_cell(lbl_w, 5, safe(f"- {label}"))
            new_y = self.get_y()
            # Score aligned to the first line of the label
            self.set_xy(self.M + lbl_w, y0)
            self.set_font("Helvetica", "B", 9)
            self.cell(10, 5, str(score), align="R")
            self.set_font("Helvetica", "", 9)
            self.set_text_color(*C_DGRAY)
            self.cell(12, 5, " / 5")
            self.set_text_color(0, 0, 0)
            self.set_xy(self.M, new_y)

        # Subtotal row — light gray fill
        self.set_x(self.M)
        self.set_fill_color(*C_LGRAY)
        self.set_font("Helvetica", "B", 9)
        self.cell(lbl_w, 6, "  Subtotal", fill=True)
        self.cell(10, 6, str(subtotal), align="R", fill=True)
        self.set_font("Helvetica", "", 9)
        self.set_text_color(*C_DGRAY)
        self.cell(12, 6, f" / {max_pts}", fill=True,
                  new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(0, 0, 0)
        self.ln(2)

    # ── Page 1: Cover ──────────────────────────────────────────────────────────

    def page1(self, speaker: str, source_label: str,
              analysis: dict, acoustic: dict):
        self.add_page()
        self._top_bar(f"Page {self.page_no()}")

        ev  = analysis
        r   = ev.get("rubric", {})
        ex  = r.get("exegesis_theology", {})
        app = r.get("application", {})
        prs = r.get("presentation", {})
        ex_t  = sum(ex.values())  if ex  else 0
        app_t = sum(app.values()) if app else 0
        prs_t = sum(prs.values()) if prs else 0
        rub_t = ex_t + app_t + prs_t

        # Sermon title — large, bold, navy
        self.set_x(self.M)
        self.set_font("Helvetica", "B", 20)
        self.set_text_color(*C_NAVY)
        self.multi_cell(self.CW, 11,
                        safe(ev.get("sermon_title", "Untitled Sermon")))
        self.set_text_color(0, 0, 0)
        self.ln(1)

        # Passage — gold
        self.set_x(self.M)
        self.set_font("Helvetica", "B", 12)
        self.set_text_color(*self.GOLD)
        self.cell(self.CW, 7, safe(ev.get("passage", "")),
                  new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(0, 0, 0)

        # Preacher and source — small dark gray
        self.set_x(self.M)
        self.set_font("Helvetica", "", 9)
        self.set_text_color(*C_DGRAY)
        self.cell(self.CW, 5, f"Preacher: {safe(speaker)}",
                  new_x="LMARGIN", new_y="NEXT")
        self.set_x(self.M)
        self.cell(self.CW, 5, f"Source: {safe(source_label)}",
                  new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(0, 0, 0)

        self._rule(gap_before=4, gap_after=4)

        # BOTTOM LINE box
        self.set_x(self.M)
        self.set_fill_color(*C_LGRAY)
        self.set_font("Helvetica", "B", 8)
        self.set_text_color(*self.GOLD)
        self.cell(self.CW, 6, "  BOTTOM LINE",
                  fill=True, new_x="LMARGIN", new_y="NEXT")
        self.ln(2)
        self.set_x(self.M)
        self.set_font("Helvetica", "I", 11)
        self.set_text_color(*C_DGRAY)
        self.multi_cell(self.CW, 7,
                        safe(f'"{ev.get("bottom_line", "")}"'))
        self.set_text_color(0, 0, 0)
        self.ln(4)

        # Score badges row — Option C Hybrid: benchmark label (primary) + · score/max (secondary)
        # Normalise Structure score: Claude sometimes returns 0-100 instead of 0-10
        raw_struct = ev.get("structure", {}).get("overall_score", 0)
        struct_sc  = round(raw_struct / 10, 1) if raw_struct > 10 else float(raw_struct)
        badges = [
            ("Structure",    struct_sc, 10,
             get_benchmark_label(struct_sc)),
            ("Exegesis",     ex_t,   20,
             get_benchmark_label(round(ex_t / 20 * 10))),
            ("Application",  app_t,  25,
             get_benchmark_label(round(app_t / 25 * 10))),
            ("Presentation", prs_t,  15,
             get_benchmark_label(round(prs_t / 15 * 10))),
        ]
        rub_label = get_benchmark_label(round(rub_t / 60 * 10))

        n   = len(badges) + 1   # +1 for Total badge
        gap = 2.5               # mm between badges
        bw  = (self.CW - gap * (n - 1)) / n   # badge width mm
        bh  = 22                               # badge height mm (slightly taller for 3 rows)
        by  = self.get_y()

        for i, (lbl, sc, mx, bench) in enumerate(badges):
            bx = self.M + i * (bw + gap)
            self.set_fill_color(*C_LGRAY)
            self.rect(bx, by, bw, bh, "F")
            # Row 1: category name — tiny, muted gray
            self.set_xy(bx, by + 1)
            self.set_font("Helvetica", "", 7)
            self.set_text_color(*C_MGRAY)
            self.cell(bw, 4, safe(lbl), align="C")
            # Row 2: benchmark label — primary, bold navy
            self.set_xy(bx, by + 5)
            self.set_font("Helvetica", "B", 10)
            self.set_text_color(*C_NAVY)
            self.cell(bw, 7, safe(bench), align="C")
            # Row 3: score — secondary, smaller gray
            self.set_xy(bx, by + 13)
            self.set_font("Helvetica", "", 8)
            self.set_text_color(*C_DGRAY)
            score_str = (f"\xb7 {sc:.1f}/{mx}" if mx == 10
                         else f"\xb7 {int(sc)}/{mx}")
            self.cell(bw, 5, safe(score_str), align="C")

        # Total badge — navy fill, white/pale text
        tx = self.M + len(badges) * (bw + gap)
        self.set_fill_color(*C_NAVY)
        self.rect(tx, by, bw, bh, "F")
        # Row 1: "Overall" label
        self.set_xy(tx, by + 1)
        self.set_font("Helvetica", "", 7)
        self.set_text_color(160, 180, 210)
        self.cell(bw, 4, "Overall", align="C")
        # Row 2: benchmark label
        self.set_xy(tx, by + 5)
        self.set_font("Helvetica", "B", 10)
        self.set_text_color(255, 255, 255)
        self.cell(bw, 7, safe(rub_label), align="C")
        # Row 3: score
        self.set_xy(tx, by + 13)
        self.set_font("Helvetica", "", 8)
        self.set_text_color(160, 180, 210)
        self.cell(bw, 5, safe(f"\xb7 {rub_t}/60"), align="C")

        self.set_text_color(0, 0, 0)
        self.set_y(by + bh + 5)

        # Encouragement — italic green paragraph
        self.set_x(self.M)
        self.set_font("Helvetica", "I", 10)
        self.set_text_color(*C_GREEN)
        self.multi_cell(self.CW, 6, safe(ev.get("encouragement", "")))
        self.set_text_color(0, 0, 0)
        self.ln(3)

        # Top growth edges
        self.set_x(self.M)
        self.set_font("Helvetica", "B", 9)
        self.set_text_color(*self.GOLD)
        self.cell(self.CW, 6, "TOP GROWTH EDGES",
                  new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(0, 0, 0)

        for i, priority in enumerate(ev.get("growth_edges", [])[:3], 1):
            self.set_x(self.M)
            self.set_font("Helvetica", "", 10)
            # write() handles inline line-wrapping naturally
            self.write(6, f"{i}. {safe(priority)}")
            self.ln(7)

        # Permanent disclaimer
        self.ln(4)
        self._rule(gap_before=0, gap_after=3)
        self.set_x(self.M)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(*C_MGRAY)
        self.multi_cell(self.CW, 4.5,
            "This report is generated by AI and is intended as a coaching tool, "
            "not a definitive evaluation. Scores reflect measurable patterns and "
            "are meant to spark conversation, not render judgment. Vocal metrics "
            "are derived from acoustic analysis of the audio. Gospel and structure "
            "scores reflect AI interpretation of the transcript.")
        self.set_text_color(0, 0, 0)

    # ── Page 2: Sermon Structure ───────────────────────────────────────────────

    def page2(self, analysis: dict):
        self.add_page()
        self._top_bar(f"Page {self.page_no()}")
        self._page_title(
            "Sermon Structure -- ME  *  WE  *  GOD  *  YOU  *  WE"
        )

        # Sermon type note
        if getattr(self, "sermon_type", None):
            self.set_x(self.M)
            self.set_font("Helvetica", "I", 9)
            self.set_text_color(*C_DGRAY)
            self.cell(self.CW, 5,
                      safe(f"Sermon type: {self.sermon_type.capitalize()}"),
                      new_x="LMARGIN", new_y="NEXT")
            self.set_text_color(0, 0, 0)
            self.ln(2)

        structure   = analysis.get("structure", {})
        sections    = structure.get("sections", [])
        flags       = structure.get("flags", [])
        total_words = sum(s.get("word_count", 0) for s in sections) or 1

        for sec in sections:
            self._check_page(42)   # new page if < 42 mm remain

            label    = sec.get("label", "")
            title    = sec.get("title", "")
            raw_sc   = sec.get("score", 0)
            # Normalise: Claude sometimes returns 0-100 instead of 0-10
            score    = round(raw_sc / 10, 1) if raw_sc > 10 else float(raw_sc)
            wc       = sec.get("word_count", 0)
            mins     = sec.get("estimated_minutes", 0.0)
            quote    = sec.get("start_quote", "")
            summary  = sec.get("summary", "")
            strength = sec.get("strength", "")
            growth   = sec.get("growth", "")
            color    = self.SEC_CLR.get(label, C_NAVY)

            # Colored header band: label -- title (left) | score/words/min (right)
            self.set_x(self.M)
            self.set_fill_color(*color)
            self.set_text_color(255, 255, 255)
            lw = self.CW * 0.58
            rw = self.CW - lw
            self.set_font("Helvetica", "B", 10)
            self.cell(lw, 8, safe(f"{label} -- {title}"), fill=True)
            self.set_font("Helvetica", "", 9)
            self.cell(rw, 8,
                      safe(f"{score:.1f}/10  {wc} words  ~{mins:.1f} min"),
                      fill=True, align="R",
                      new_x="LMARGIN", new_y="NEXT")
            self.set_text_color(0, 0, 0)

            # Score bar
            self._score_bar(score, 10, height=2)

            # Opening quote — italic, gray
            if quote:
                self.set_x(self.M)
                self.set_font("Helvetica", "I", 9)
                self.set_text_color(*C_DGRAY)
                self.multi_cell(self.CW, 5, safe(f'"{quote}"'))
                self.set_text_color(0, 0, 0)

            # Summary — one-sentence section description
            if summary:
                self.set_x(self.M)
                self.set_font("Helvetica", "", 9)
                self.multi_cell(self.CW, 5, safe(summary))

            # Strength — bold green label inline; write() wraps, never clips
            if strength:
                self.set_x(self.M)
                self.set_font("Helvetica", "B", 9)
                self.set_text_color(*C_GREEN)
                self.write(5, "Strength: ")
                self.set_font("Helvetica", "", 9)
                self.set_text_color(0, 0, 0)
                self.write(5, safe(strength))
                self.ln(6)

            # Growth edge — write() keeps full label on same line, never clips
            if growth:
                self.set_x(self.M)
                self.set_font("Helvetica", "B", 9)
                self.set_text_color(*C_RED)
                self.write(5, "Growth edge: ")
                self.set_font("Helvetica", "", 9)
                self.set_text_color(0, 0, 0)
                self.write(5, safe(growth))
                self.ln(6)

            self.ln(2)

        # Guard: don't let the flags block start within 25 mm of the bottom
        self._check_page(25)

        # Flag if GOD section exceeds 40% of total words
        god_sec = next((s for s in sections if s.get("label") == "GOD"), None)
        if god_sec and god_sec.get("word_count", 0) / total_words > 0.40:
            pct = god_sec["word_count"] / total_words * 100
            self.set_x(self.M)
            self.set_font("Helvetica", "I", 9)
            self.set_text_color(*self.ORANGE)
            self.multi_cell(self.CW, 5,
                f"! GOD section is {pct:.0f}% of total -- consider rebalancing.")
            self.set_text_color(0, 0, 0)

        # Any additional flags returned by the AI
        for flag in flags:
            self.set_x(self.M)
            self.set_font("Helvetica", "I", 9)
            col = C_RED if flag.get("severity") == "warning" else C_DGRAY
            self.set_text_color(*col)
            self.multi_cell(self.CW, 5, safe("! " + flag.get("text", "")))
            self.set_text_color(0, 0, 0)

    # ── Page 3: Vocal / Rhetorical Delivery ───────────────────────────────────

    def page3(self, acoustic: dict, vocal_analysis: dict):
        has_audio = getattr(self, "has_audio", True)
        page_label = "Vocal Delivery" if has_audio else "Rhetorical Delivery"

        self.add_page()
        self._top_bar(f"Page {self.page_no()}")
        if has_audio:
            self._page_title(
                "Vocal Delivery -- 8 Elements (measured from audio)"
            )
        else:
            self._page_title(
                "Rhetorical Delivery -- 8 Elements (transcript analysis)"
            )

        # Italic disclaimer subheader
        self.set_x(self.M)
        self.set_font("Helvetica", "I", 9)
        self.set_text_color(*C_DGRAY)
        if has_audio:
            self.multi_cell(
                self.CW, 5,
                "Acoustic metrics derived from audio analysis. "
                "Verbal Clarity derived from transcript. "
                "High confidence on all scores."
            )
        else:
            self.multi_cell(
                self.CW, 5,
                "No audio file -- all scores estimated from transcript analysis. "
                "Pitch and dynamic range are not available."
            )
        self.set_text_color(0, 0, 0)
        self.ln(4)

        a    = acoustic
        va   = vocal_analysis
        pct  = round(a["talk_ratio"] * 100)
        tops = ", ".join(w for w, _ in a["top_fillers"][:6]) or "none"
        dur  = a["duration_min"] or 1
        wpm  = a["estimated_wpm"]

        if   wpm < 120:  pace_desc = "slow"
        elif wpm <= 170: pace_desc = "ideal"
        else:            pace_desc = "fast"

        # Each tuple: (display name, score, [italic measurement lines], coaching note)
        elements = [
            (
                "1. Filler Words",
                va.get("filler_words", {}).get("score", a["filler_score"]),
                [
                    f"Measured count: {a['filler_count']}  "
                    f"Per minute: {a['filler_per_minute']}  "
                    "(High confidence)",
                    f"Most frequent: {tops}",
                ],
                va.get("filler_words", {}).get("notes", ""),
            ),
            (
                "2. Words Per Minute (Pace)",
                va.get("pace", {}).get("score", a["wpm_score"]),
                [f"Measured: {wpm} wpm  ({pace_desc} -- ideal: 130-170 wpm)"
                 "  (High confidence)"],
                va.get("pace", {}).get("notes", ""),
            ),
            (
                "3. Rhetorical Variation",
                va.get("rhetorical_variation", {}).get("score",
                    a["dynamic_range_score"]),
                [f"Measured: {a['dynamic_range_db']} dB variation"
                 + ("  (High confidence)" if has_audio
                    else "  (Not available -- no audio)")],
                va.get("rhetorical_variation", {}).get("notes", ""),
            ),
            (
                "4. Landing Space",
                va.get("landing_space", {}).get("score", a["pause_score"]),
                [f"Measured: {a['pause_count']} pauses  "
                 f"({a['pause_count'] / dur:.1f}/min)"
                 + ("  (High confidence)" if has_audio
                    else "  (Estimated from transcript)")],
                va.get("landing_space", {}).get("notes", ""),
            ),
            (
                "5. Vocal Variety / Pitch Range",
                va.get("pitch_variety", {}).get("score", a["vocal_variety_score"])
                    if has_audio else 0,
                [] if has_audio else ["Not scored -- no audio file"],
                va.get("pitch_variety", {}).get("notes", "")
                    if has_audio else "Pitch analysis requires audio.",
            ),
            (
                "6. Cognitive Breathing Room",
                va.get("cognitive_breathing_room", {}).get("score",
                    a["talk_silence_score"]),
                [f"Measured: {pct}% speaking time  (ideal: 82-93%)"
                 + ("  (High confidence)" if has_audio
                    else "  (Estimated from transcript)")],
                va.get("cognitive_breathing_room", {}).get("notes", ""),
            ),
            (
                "7. Rhetorical Arc",
                va.get("rhetorical_arc", {}).get("score", a["energy_arc_score"]),
                [f"Pattern: {a['arc_pattern']}"
                 + ("  (High confidence)" if has_audio
                    else "  (Estimated from transcript)")],
                va.get("rhetorical_arc", {}).get("notes", ""),
            ),
            (
                "8. Verbal Clarity",
                va.get("verbal_clarity", {}).get("score", 0),
                ["Derived from transcript analysis  (High confidence)"],
                va.get("verbal_clarity", {}).get("notes", ""),
            ),
        ]

        scores = []
        for name, score, meas_lines, note in elements:
            self._check_page(35)   # new page if < 35 mm remain

            scores.append(score)
            sc_col    = C_GREEN if score >= 8 else (C_RED if score <= 4 else self.ORANGE)
            benchmark = get_benchmark_label(score)

            # Element name left, benchmark + score right
            self.set_x(self.M)
            self.set_font("Helvetica", "B", 11)
            self.set_text_color(*C_NAVY)
            self.cell(self.CW - 40, 7, safe(name))
            self.set_text_color(*C_DGRAY)
            self.set_font("Helvetica", "", 9)
            self.cell(20, 7, safe(benchmark), align="R")
            self.set_text_color(*sc_col)
            self.set_font("Helvetica", "B", 11)
            self.cell(20, 7, f"{score}/10", align="R",
                      new_x="LMARGIN", new_y="NEXT")
            self.set_text_color(0, 0, 0)

            # Proportional score bar
            self._score_bar(score, 10)

            # Italic measurement lines — full CW width, no truncation
            for meas in meas_lines:
                self.set_x(self.M)
                self.set_font("Helvetica", "I", 9)
                self.set_text_color(*C_DGRAY)
                self.multi_cell(self.CW, 5, safe(meas))
                self.set_text_color(0, 0, 0)

            # Coaching note — full CW width multi_cell, no fixed height cap
            if note:
                self.set_x(self.M)
                self.set_font("Helvetica", "", 10)
                self.multi_cell(self.CW, 6, safe(note))

            self.ln(3)

        # Delivery average (skip pitch score if no audio)
        counted = [s for s, (n, _, _, _) in zip(scores, elements)
                   if has_audio or "Pitch Range" not in n]
        avg = round(sum(counted) / len(counted), 1) if counted else 0.0
        self._rule(gap_before=0, gap_after=3)
        self.set_x(self.M)
        self.set_font("Helvetica", "B", 10)
        self.set_text_color(*C_NAVY)
        self.cell(self.CW, 6,
                  f"{page_label} Average: {avg} / 10  "
                  f"({get_benchmark_label(avg)})",
                  new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(0, 0, 0)

    # ── Page 4: Gospel Check + GOSPEL Table + Rubric Summary ─────────────────

    def page4(self, gospel_check: dict, rubric: dict):
        self.add_page()
        self._top_bar(f"Page {self.page_no()}")

        gc   = gospel_check
        r    = rubric
        ex   = r.get("exegesis_theology", {})
        app  = r.get("application", {})
        pres = r.get("presentation", {})
        ex_t   = sum(ex.values())   if ex   else 0
        app_t  = sum(app.values())  if app  else 0
        pres_t = sum(pres.values()) if pres else 0
        rub_t  = ex_t + app_t + pres_t

        self._page_title("Gospel Check")

        # ── Gold Standard badge ────────────────────────────────────────────────
        gold_std  = gc.get("gold_standard", "Partially")
        gold_note = gc.get("gold_standard_note", "")
        gold_col  = (C_GREEN if gold_std == "Yes"
                     else (C_RED if gold_std == "No" else self.ORANGE))

        self.set_x(self.M)
        self.set_fill_color(*gold_col)
        self.set_text_color(255, 255, 255)
        self.set_font("Helvetica", "B", 10)
        self.cell(self.CW, 7,
                  safe(f"  Gospel Gold Standard: {gold_std}"),
                  fill=True, new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(0, 0, 0)

        if gold_note:
            self.set_x(self.M)
            self.set_font("Helvetica", "I", 9)
            self.set_text_color(*C_DGRAY)
            self.multi_cell(self.CW, 5, safe(gold_note))
            self.set_text_color(0, 0, 0)

        # Christ Not Central banner
        if gc.get("incomplete_flag", False):
            self.ln(1)
            self.set_x(self.M)
            self.set_fill_color(*C_RED)
            self.set_text_color(255, 255, 255)
            self.set_font("Helvetica", "B", 10)
            self.cell(self.CW, 7,
                      "  Gospel Check: Christ Not Central  (E score < 5)",
                      fill=True, new_x="LMARGIN", new_y="NEXT")
            self.set_text_color(0, 0, 0)

        # Gospel narrative paragraph
        self.ln(2)
        self.set_x(self.M)
        self.set_font("Helvetica", "", 9)
        self.multi_cell(self.CW, 5, safe(gc.get("notes", "")))
        self.ln(3)

        # ── GOSPEL Scoring Table ───────────────────────────────────────────────
        # Columns: Letter | Category | Pts/Max | Bar | Note
        ltr_w  = 10
        cat_w  = 48
        sc_w   = 18
        bar_w  = 28
        note_w = self.CW - ltr_w - cat_w - sc_w - bar_w  # ~81.9 mm

        # Header row
        self.set_x(self.M)
        self.set_fill_color(*C_NAVY)
        self.set_text_color(255, 255, 255)
        self.set_font("Helvetica", "B", 8)
        self.cell(ltr_w,  6, "Ltr",           fill=True, align="C")
        self.cell(cat_w,  6, "Category",       fill=True)
        self.cell(sc_w,   6, "Pts/Max",        fill=True, align="C")
        self.cell(bar_w,  6, "Score",          fill=True, align="C")
        self.cell(note_w, 6, "Coaching Note",
                  fill=True, new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(0, 0, 0)

        gospel_total = 0
        for i, (letter, category, max_pts) in enumerate(self.GOSPEL_ROWS):
            score_raw = gc.get(f"{letter}_score", 0)
            note_txt  = gc.get(f"{letter}_note", "")
            pts       = round(score_raw * max_pts / 10)
            gospel_total += pts

            y0      = self.get_y()
            bg_col  = (245, 245, 245) if i % 2 == 0 else (255, 255, 255)
            bar_col = (C_GREEN if score_raw >= 8
                       else (C_RED if score_raw <= 4 else self.ORANGE))
            fill_bw = bar_w * score_raw / 10 if score_raw else 0

            # Fixed-height columns
            self.set_x(self.M)
            self.set_fill_color(*bg_col)
            self.set_font("Helvetica", "B", 9)
            self.cell(ltr_w, 6, letter, fill=True, align="C")
            self.set_font("Helvetica", "", 9)
            self.cell(cat_w, 6, safe(category), fill=True)
            self.set_font("Helvetica", "B", 9)
            self.cell(sc_w, 6, f"{pts}/{max_pts}", fill=True, align="C")

            # Mini progress bar — drawn as rect, does not advance cursor
            bar_x = self.get_x()
            self.set_fill_color(*bg_col)
            self.rect(bar_x, y0, bar_w, 6, "F")
            if fill_bw > 0:
                self.set_fill_color(*bar_col)
                self.rect(bar_x, y0, fill_bw, 6, "F")

            # Note column — set_xy then multi_cell to properly reset X/Y
            self.set_xy(bar_x + bar_w, y0)
            self.set_fill_color(*bg_col)
            self.set_font("Helvetica", "", 8)
            self.multi_cell(note_w, 6, safe(note_txt), fill=True)

            # Ensure Y advances at least one full row height
            if self.get_y() < y0 + 6:
                self.set_y(y0 + 6)

        # Total row
        y0 = self.get_y()
        self.set_x(self.M)
        self.set_fill_color(*C_LGRAY)
        self.set_font("Helvetica", "B", 9)
        self.cell(ltr_w + cat_w, 6, "  Total", fill=True)
        self.cell(sc_w, 6, f"{gospel_total}/60", fill=True, align="C")

        bar_x   = self.get_x()
        tot_col = (C_GREEN if gospel_total >= 48
                   else (C_RED if gospel_total <= 24 else self.ORANGE))
        fill_bw = bar_w * gospel_total / 60 if gospel_total else 0
        self.set_fill_color(*C_LGRAY)
        self.rect(bar_x, y0, bar_w, 6, "F")
        if fill_bw > 0:
            self.set_fill_color(*tot_col)
            self.rect(bar_x, y0, fill_bw, 6, "F")

        self.set_xy(bar_x + bar_w, y0)
        self.set_fill_color(*C_LGRAY)
        self.set_font("Helvetica", "I", 8)
        self.cell(note_w, 6,
                  safe(get_benchmark_label(round(gospel_total / 60 * 10))),
                  fill=True, new_x="LMARGIN", new_y="NEXT")
        self.ln(4)

        self._rule(gap_before=0, gap_after=2)

        # ── Sermon Evaluation Rubric (compact subtotals) ───────────────────────
        self._page_title("Sermon Evaluation Rubric")

        self.set_x(self.M)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(*C_DGRAY)
        self.multi_cell(self.CW, 4,
            "Note: Body language and note-dependence require in-person"
            " observation -- score manually below.")
        self.set_text_color(0, 0, 0)
        self.ln(2)

        # Compact subtotal rows (category + score/max)
        score_col_w = 22
        lbl_w = self.CW - score_col_w
        sub_rows = [
            ("Exegesis and Theology", ex_t,   20),
            ("Application",           app_t,  25),
            ("Presentation (auto)",   pres_t, 15),
        ]
        for cat_lbl, sub, mx in sub_rows:
            self.set_x(self.M)
            self.set_font("Helvetica", "", 9)
            self.cell(lbl_w, 6, safe(cat_lbl))
            self.set_font("Helvetica", "B", 9)
            self.cell(10, 6, str(sub), align="R")
            self.set_font("Helvetica", "", 9)
            self.set_text_color(*C_DGRAY)
            self.cell(12, 6, f" / {mx}",
                      new_x="LMARGIN", new_y="NEXT")
            self.set_text_color(0, 0, 0)

        # Manual scoring blanks
        self._rule(gap_before=2, gap_after=1)
        self.set_x(self.M)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(*C_DGRAY)
        self.cell(self.CW, 4, "Manual scoring (in-person observation required):",
                  new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(0, 0, 0)
        self.ln(1)

        lbl_w = self.CW - 18
        self.set_font("Helvetica", "", 9)
        self.set_x(self.M)
        self.cell(lbl_w, 6,
                  "Body language enhanced the sermon and was not distracting")
        self.cell(18, 6, "___ / 5", align="R",
                  new_x="LMARGIN", new_y="NEXT")
        self.set_x(self.M)
        self.cell(lbl_w, 6, "Preacher did not seem overly reliant on notes")
        self.cell(18, 6, "___ / 5", align="R",
                  new_x="LMARGIN", new_y="NEXT")

        self.ln(2)
        self._rule(gap_before=0, gap_after=2)

        # Footer total line
        pct_val = round(rub_t / 60 * 100) if rub_t else 0
        self.set_x(self.M)
        self.set_font("Helvetica", "B", 9)
        self.set_text_color(*C_NAVY)
        self.multi_cell(self.CW, 6,
            f"Automated Total: {rub_t} / 60 = {pct_val}%"
            f"  +  Manual items (body language + notes): ___ / 10",
            align="C")
        self.set_text_color(0, 0, 0)

    # ── Page 5: Scorecard ─────────────────────────────────────────────────────

    def page5(self, analysis: dict, gospel_check: dict):
        self.add_page()
        self._top_bar(f"Page {self.page_no()}")
        self._page_title("Scorecard -- All Scores at a Glance")

        ev = analysis
        gc = gospel_check
        lw = self.CW - 32   # label column width

        # ── Structure Scores ──────────────────────────────────────────────────
        self.set_x(self.M)
        self.set_font("Helvetica", "B", 10)
        self.set_text_color(*self.GOLD)
        self.cell(self.CW, 6, "SERMON STRUCTURE",
                  new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(0, 0, 0)

        for sec in ev.get("structure", {}).get("sections", []):
            raw_sc = sec.get("score", 0)
            score  = round(raw_sc / 10, 1) if raw_sc > 10 else float(raw_sc)
            label  = sec.get("label", "")
            title  = sec.get("title", "")
            bench  = get_benchmark_label(score)
            sc_col = C_GREEN if score >= 8 else (C_RED if score <= 4 else self.ORANGE)

            self.set_x(self.M)
            self.set_font("Helvetica", "", 9)
            self.cell(lw, 5, safe(f"{label}  {title}"))
            self.set_text_color(*sc_col)
            self.set_font("Helvetica", "B", 9)
            self.cell(16, 5, f"{score}/10", align="R")
            self.set_text_color(*C_DGRAY)
            self.set_font("Helvetica", "", 8)
            self.cell(16, 5, safe(bench), align="R",
                      new_x="LMARGIN", new_y="NEXT")
            self.set_text_color(0, 0, 0)

        self.ln(3)

        # ── Vocal / Rhetorical Scores ─────────────────────────────────────────
        self.set_x(self.M)
        self.set_font("Helvetica", "B", 10)
        self.set_text_color(*self.GOLD)
        self.cell(self.CW, 6, "VOCAL / RHETORICAL DELIVERY",
                  new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(0, 0, 0)

        va = ev.get("vocal", {})
        vocal_items = [
            ("Filler Words",             va.get("filler_words",            {}).get("score", 0)),
            ("Pace (WPM)",               va.get("pace",                    {}).get("score", 0)),
            ("Rhetorical Variation",     va.get("rhetorical_variation",    {}).get("score", 0)),
            ("Landing Space",            va.get("landing_space",           {}).get("score", 0)),
            ("Vocal Variety / Pitch",    va.get("pitch_variety",           {}).get("score", 0)),
            ("Cognitive Breathing Room", va.get("cognitive_breathing_room",{}).get("score", 0)),
            ("Rhetorical Arc",           va.get("rhetorical_arc",          {}).get("score", 0)),
            ("Verbal Clarity",           va.get("verbal_clarity",          {}).get("score", 0)),
        ]

        for vname, vscore in vocal_items:
            bench  = get_benchmark_label(vscore)
            sc_col = C_GREEN if vscore >= 8 else (C_RED if vscore <= 4 else self.ORANGE)

            self.set_x(self.M)
            self.set_font("Helvetica", "", 9)
            self.cell(lw, 5, safe(vname))
            self.set_text_color(*sc_col)
            self.set_font("Helvetica", "B", 9)
            self.cell(16, 5, f"{vscore}/10", align="R")
            self.set_text_color(*C_DGRAY)
            self.set_font("Helvetica", "", 8)
            self.cell(16, 5, safe(bench), align="R",
                      new_x="LMARGIN", new_y="NEXT")
            self.set_text_color(0, 0, 0)

        self.ln(3)

        # ── Gospel Scores ─────────────────────────────────────────────────────
        self.set_x(self.M)
        self.set_font("Helvetica", "B", 10)
        self.set_text_color(*self.GOLD)
        self.cell(self.CW, 6, "GOSPEL CHECK",
                  new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(0, 0, 0)

        gospel_total = 0
        for letter, category, max_pts in self.GOSPEL_ROWS:
            score_raw = gc.get(f"{letter}_score", 0)
            pts       = round(score_raw * max_pts / 10)
            gospel_total += pts
            bench  = get_benchmark_label(score_raw)
            sc_col = C_GREEN if score_raw >= 8 else (C_RED if score_raw <= 4 else self.ORANGE)

            self.set_x(self.M)
            self.set_font("Helvetica", "", 9)
            self.cell(lw, 5, safe(f"{letter}  {category}  ({pts}/{max_pts} pts)"))
            self.set_text_color(*sc_col)
            self.set_font("Helvetica", "B", 9)
            self.cell(16, 5, f"{score_raw}/10", align="R")
            self.set_text_color(*C_DGRAY)
            self.set_font("Helvetica", "", 8)
            self.cell(16, 5, safe(bench), align="R",
                      new_x="LMARGIN", new_y="NEXT")
            self.set_text_color(0, 0, 0)

        # Gospel total row
        self.set_x(self.M)
        self.set_fill_color(*C_LGRAY)
        self.set_font("Helvetica", "B", 9)
        self.cell(lw, 6, "  Gospel Total", fill=True)
        self.cell(16, 6, f"{gospel_total}/60", align="R", fill=True)
        self.cell(16, 6,
                  safe(get_benchmark_label(round(gospel_total / 60 * 10))),
                  align="R", fill=True, new_x="LMARGIN", new_y="NEXT")
        self.ln(4)

        # ── Coaching Priorities — action items ────────────────────────────────
        self._rule(gap_before=0, gap_after=3)
        self.set_x(self.M)
        self.set_font("Helvetica", "B", 10)
        self.set_text_color(*C_NAVY)
        self.cell(self.CW, 6, "COACHING PRIORITIES -- Action Items",
                  new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(0, 0, 0)
        self.ln(1)

        for i, priority in enumerate(ev.get("growth_edges", [])[:3], 1):
            self.set_x(self.M)
            self.set_font("Helvetica", "B", 9)
            self.set_text_color(*C_NAVY)
            self.write(6, f"{i}.  ")
            self.set_font("Helvetica", "", 9)
            self.set_text_color(0, 0, 0)
            self.write(6, safe(priority))
            self.ln(8)


# ── PDF entry point ────────────────────────────────────────────────────────────
def build_pdf(speaker: str, source_label: str, acoustic: dict,
              analysis: dict, gospel_check: dict, out_path: str,
              has_audio: bool = True, sermon_type: str = "expository"):
    """Build and save the 5-page sermon evaluation PDF."""
    pdf = SermonPDF()
    pdf.has_audio   = has_audio
    pdf.sermon_type = sermon_type
    pdf.page1(speaker, source_label, analysis, acoustic)
    pdf.page2(analysis)
    pdf.page3(acoustic, analysis.get("vocal", {}))
    pdf.page4(gospel_check, analysis.get("rubric", {}))
    pdf.page5(analysis, gospel_check)
    pdf.output(out_path)


# ── Terminal output ────────────────────────────────────────────────────────────
def print_terminal(speaker, acoustic, analysis):
    ev = analysis
    w  = 62
    print(f"\n{'='*w}")
    print(f"  {safe(ev.get('sermon_title', 'Untitled'))}")
    print(f"  {speaker}  |  {ev.get('passage', '')}  |  {acoustic['duration_min']} min")
    print(f"{'='*w}")
    print(f"  BOTTOM LINE: {ev.get('bottom_line', '')[:120]}")
    print()

    print(f"-- SERMON STRUCTURE {'─'*42}")
    for sec in ev.get("structure", {}).get("sections", []):
        score = sec.get("score", 0)
        b     = terminal_bar(score, 10, 10)
        lbl   = get_benchmark_label(score)
        print(f"  {sec.get('label',''):<4}  {lbl:<12}  {score:>2}/10  [{b}]  "
              f"{sec.get('estimated_minutes', 0):.1f}min  {sec.get('word_count', 0)}w")
        print(f"         Growth edge: {sec.get('growth', '')[:70]}")
    print()

    r    = ev.get("rubric", {})
    ex   = r.get("exegesis_theology", {})
    app  = r.get("application", {})
    prs  = r.get("presentation", {})
    ex_t  = sum(ex.values())  if ex  else 0
    app_t = sum(app.values()) if app else 0
    prs_t = sum(prs.values()) if prs else 0
    rub_t = ex_t + app_t + prs_t
    pct   = round(rub_t / 60 * 100) if rub_t else 0
    print(f"-- RUBRIC SCORES {'─'*45}")
    print(f"  Exegesis & Theology: {ex_t}/20")
    print(f"  Application:         {app_t}/25")
    print(f"  Presentation:        {prs_t}/15")
    print(f"  TOTAL (automated):   {rub_t}/60  ({pct}%)")
    print()

    print(f"-- TOP GROWTH EDGES {'─'*42}")
    for i, p in enumerate(ev.get("growth_edges", []), 1):
        print(f"  {i}. {p[:100]}")
    print()


# ── Output paths ──────────────────────────────────────────────────────────────
REPORTS_PERSONAL = os.path.expanduser(
    "~/Desktop/MyPreachingCoach/reports/personal"
)
REPORTS_BETA = os.path.expanduser(
    "~/Desktop/MyPreachingCoach/reports/beta"
)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="My Preaching Coach -- AI Sermon Evaluation")
    ap.add_argument("source", help="Local audio file or YouTube URL")
    ap.add_argument("--name", default=None,
                    help="Preacher name (overrides YouTube metadata)")
    ap.add_argument("--type", default="expository",
                    choices=["expository", "topical", "narrative", "liturgical"],
                    dest="sermon_type",
                    help="Sermon type for evaluation context (default: expository)")
    args = ap.parse_args()

    if not OPENAI_API_KEY:    sys.exit("Set OPENAI_API_KEY environment variable.")
    if not ANTHROPIC_API_KEY: sys.exit("Set ANTHROPIC_API_KEY environment variable.")

    source       = args.source
    source_label = Path(source).name if Path(source).exists() else source[:80]
    is_url       = not Path(source).exists()

    # ── Resolve speaker name ───────────────────────────────────────────────────
    # Words that suggest the metadata returned an org/church name, not a person
    _CHURCH_WORDS = {"church", "ministry", "ministries", "community",
                     "fellowship", "chapel"}

    if args.name:
        # Explicit --name flag always wins — no further checks
        speaker = args.name
    elif is_url:
        # Try to pull a person name from YouTube metadata
        print("\nFetching YouTube metadata ...")
        info = get_youtube_info(source)
        raw = info.get("uploader", "").strip()
        # Reject org/church names
        if raw and not any(w in raw.lower() for w in _CHURCH_WORDS):
            speaker = raw
            print(f"  Detected preacher: {speaker}")
        else:
            if raw:
                print(f"  Metadata looks like a church name ({raw!r}) -- skipping.")
            try:
                speaker = input("Preacher name (press Enter to skip): ").strip()
            except (EOFError, KeyboardInterrupt):
                speaker = ""
            if not speaker:
                speaker = "Unknown Speaker"
    else:
        # Local file with no --name
        try:
            speaker = input("Preacher name (press Enter to skip): ").strip()
        except (EOFError, KeyboardInterrupt):
            speaker = ""
        if not speaker:
            speaker = "Unknown Speaker"

    # ── Ensure output directories exist ───────────────────────────────────────
    os.makedirs(REPORTS_PERSONAL, exist_ok=True)
    os.makedirs(REPORTS_BETA,     exist_ok=True)

    with tempfile.TemporaryDirectory() as tmpdir:
        print("\n[1/4] Acquiring audio ...")
        mp3 = acquire_audio(source, tmpdir)
        print(f"  Ready: {mp3}")

        print("\n[2/4] Transcribing with Whisper ...")
        transcript = transcribe(mp3)

        print("\n[3/4] Acoustic analysis ...")
        acoustic = acoustic_analysis(mp3, transcript)
        print(f"  {acoustic['duration_min']}min | {acoustic['estimated_wpm']}wpm | "
              f"fillers:{acoustic['filler_count']}({acoustic['filler_per_minute']}/min) | "
              f"pauses:{acoustic['pause_count']} | dr:{acoustic['dynamic_range_db']}dB | "
              f"arc:{acoustic['arc_pattern']}")

        has_audio = True   # always True for current audio-based workflow

        print("\n[4/4] Evaluating with Claude (claude-sonnet-4-6) ...")
        print(f"  Sermon type: {args.sermon_type}")
        analysis     = evaluate_with_claude(transcript, speaker, acoustic,
                                             sermon_type=args.sermon_type,
                                             has_audio=has_audio)
        gospel_check = analysis["gospel_check"]

        print_terminal(speaker, acoustic, analysis)

        safe_name  = re.sub(r"[^\w]+", "_", speaker)
        title_slug = re.sub(r"[^\w]+", "_", analysis.get("sermon_title", "sermon"))[:35]
        base       = f"sermon_eval_{title_slug}_{safe_name}"
        json_path  = os.path.join(REPORTS_PERSONAL, f"{base}.json")
        pdf_path   = os.path.join(REPORTS_PERSONAL, f"{base}.pdf")

        with open(json_path, "w") as f:
            json.dump({
                "speaker":  speaker,
                "source":   source_label,
                "acoustic": acoustic,
                "analysis": analysis,
            }, f, indent=2)

        print("Generating PDF ...")
        build_pdf(speaker, source_label, acoustic, analysis,
                  gospel_check, pdf_path,
                  has_audio=has_audio, sermon_type=args.sermon_type)

        print(f"\n{'─'*62}")
        print(f"  JSON: {json_path}")
        print(f"  PDF:  {pdf_path}")
        print(f'\n  open "{pdf_path}"')
        print()


if __name__ == "__main__":
    main()
