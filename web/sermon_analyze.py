#!/usr/bin/env python3.11
"""
sermon_analyze.py — My Preaching Coach
Usage: python3.11 sermon_analyze.py <audio_or_youtube_url> --name "Speaker Name"
"""
import argparse, json, os, re, subprocess, sys, tempfile, time, urllib.request
from datetime import datetime
from pathlib import Path

# ── Usage logger (optional — delete this block to disable logging) ─────────────
try:
    import usage_logger as _usage_logger
    _LOGGER_AVAILABLE = True
except ImportError:
    _LOGGER_AVAILABLE = False

# ── Deps ──────────────────────────────────────────────────────────────────────
for pkg, imp in [("anthropic","anthropic"),("openai","openai"),
                 ("numpy","numpy"),("soundfile","soundfile"),
                 ("scipy","scipy"),("fpdf","fpdf")]:
    try: __import__(imp)
    except ImportError: sys.exit(f"Missing: pip install {pkg if pkg!='fpdf' else 'fpdf2'}")

import anthropic, openai, numpy as np, soundfile as sf
from scipy.signal import find_peaks, correlate
from fpdf import FPDF

import downloader_client

# ── Constants ─────────────────────────────────────────────────────────────────
OPENAI_API_KEY    = os.environ.get("OPENAI_API_KEY","")
GROQ_API_KEY      = os.environ.get("GROQ_API_KEY","")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY","")
WHISPER_MAX_BYTES = 24 * 1024 * 1024
FEEDBACK_URL      = os.environ.get(
    "FEEDBACK_FORM_URL",
    "https://forms.gle/C2MMAqfsigGcWEhS9"
)

FILLER_WORDS = ["um","uh","like","you know","basically","literally",
                "actually","so","right","okay","kind of","sort of"]

# ── Logging state (populated during a run, read by main() for usage_logger) ────
_last_claude_usage    = {"input_tokens": 0, "output_tokens": 0}
_last_whisper_chunks  = 1
_last_whisper_provider = "groq"   # "groq" or "openai" — set by transcribe()


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
# URL downloads go through the Hetzner download microservice (downloader_client).
# Local file uploads are converted to mp3 via ffmpeg here.

def get_youtube_info(url: str) -> dict:
    """Fetch YouTube metadata via the downloader VM.
    Returns {"uploader": str, "title": str} or {} on failure.
    Prefers creator > artist > uploader for the speaker name."""
    try:
        info = downloader_client.probe(url, timeout=60)
        name = info.get("creator") or info.get("artist") or info.get("uploader") or ""
        return {"uploader": name, "title": info.get("title") or ""}
    except Exception as e:
        print(f"  get_youtube_info: probe failed ({e})")
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
    print("  Downloading via Hetzner downloader VM ...")
    mp3_path, meta = downloader_client.download(source, tmpdir, timeout=900)
    print(f"  Downloaded {os.path.basename(mp3_path)} "
          f"(used_proxy={meta.get('used_proxy')}, direct_failure={meta.get('direct_failure')})")
    return mp3_path


# ── Step 2: Transcribe ────────────────────────────────────────────────────────
def _do_transcribe(client, whisper_model: str, mp3_path: str) -> str:
    """Run transcription with given client/model, handling chunking."""
    global _last_whisper_chunks
    size = os.path.getsize(mp3_path)
    if size <= WHISPER_MAX_BYTES:
        print(f"  Single chunk ({size/1e6:.1f} MB) ...")
        with open(mp3_path,"rb") as f:
            r = client.audio.transcriptions.create(model=whisper_model,file=f,response_format="text")
        text = r if isinstance(r,str) else r.text
        _last_whisper_chunks = 1
        print(f"  Words: {len(text.split()):,}  |  Chunks: 1")
        return text
    cdir = os.path.join(os.path.dirname(mp3_path),"chunks")
    os.makedirs(cdir, exist_ok=True)
    print(f"  {size/1e6:.1f} MB > 24 MB — splitting ...")
    # Re-encode (not stream copy) so ffmpeg can split at proper frame boundaries
    # near the 1200s mark rather than arbitrary byte positions mid-audio-frame.
    subprocess.run(["ffmpeg","-y","-i",mp3_path,"-f","segment",
                    "-segment_time","1200","-c:a","libmp3lame","-q:a","4",
                    os.path.join(cdir,"chunk_%03d.mp3")],
                   check=True, capture_output=True)
    chunks = sorted(Path(cdir).glob("chunk_*.mp3"))
    print(f"  Transcribing {len(chunks)} chunks ...")
    parts = []
    for i,chunk in enumerate(chunks,1):
        print(f"    chunk {i}/{len(chunks)} ...", end="\r")
        with open(chunk,"rb") as f:
            r = client.audio.transcriptions.create(model=whisper_model,file=f,response_format="text")
        parts.append(r if isinstance(r,str) else r.text)
    text = " ".join(parts)
    _last_whisper_chunks = len(chunks)
    print(f"\n  Words: {len(text.split()):,}  |  Chunks: {len(chunks)}")
    return text


def transcribe(mp3_path: str) -> str:
    # Try Groq Whisper first (cheaper), fall back to OpenAI on rate limit
    global _last_whisper_provider
    if GROQ_API_KEY:
        try:
            client = openai.OpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1")
            print("  Using Groq Whisper (whisper-large-v3-turbo)")
            result = _do_transcribe(client, "whisper-large-v3-turbo", mp3_path)
            _last_whisper_provider = "groq"
            return result
        except Exception as e:
            if "rate_limit" in str(e).lower() or "429" in str(e):
                print("  Groq rate limit hit — falling back to OpenAI Whisper ...")
            else:
                raise
    client = openai.OpenAI(api_key=OPENAI_API_KEY)
    print("  Using OpenAI Whisper")
    result = _do_transcribe(client, "whisper-1", mp3_path)
    _last_whisper_provider = "openai"
    return result

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
    # Human speech dynamic range is ~40-60 dB; >70 dB indicates clipping,
    # compression artifacts, or a podcast-feed export — not a real reading.
    dynamic_range_sensor_error = dynamic_range_db > 70

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
        # NOTE: when dynamic_range_sensor_error=True, the rhetorical_variation
        # score in the final analysis JSON will be 0 — but this 0 means
        # "unavailable due to audio fidelity," NOT a real score of 0.
        # Aggregate dashboards over stored JSON must filter on
        # dynamic_range_sensor_error=True to distinguish this from genuine zeros
        # (e.g., pitch_variety=0 when has_audio=False).
        "dynamic_range_sensor_error": dynamic_range_sensor_error,
        "talk_ratio":          round(talk_ratio, 3),
        "pitch_cv":            round(pitch_cv, 3),
        "arc_pattern":         arc_pattern,
        "arc_thirds":          {"start": round(e1,4), "middle": round(e2,4), "end": round(e3,4)},
        "wpm_score":           s_wpm(wpm),
        "filler_score":        s_fpm(filler_per_min),
        "dynamic_range_score": None if dynamic_range_sensor_error else s_dr(dynamic_range_db),
        # TODO: re-enable after metric recalibration — see meta-analysis
        # "pause_score":         s_pause(pause_count, dur_min),
        "vocal_variety_score": s_variety(pitch_cv),
        # TODO: re-enable after metric recalibration — see meta-analysis
        # "talk_silence_score":  s_tts(talk_ratio),
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
DYNAMIC RANGE: {dynamic_range_db} dB  |  PITCH CV: {pitch_cv:.3f}  |  ENERGY ARC: {arc_pattern}
AUDIO AVAILABLE: {has_audio}{has_audio_note}{sensor_error_note}

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
  "five_ps": {{
    "personal_connection": {{"score":0,"label":"<Foundational|Developing|Emerging|Proficient|Exemplary>","narrative":"<2 sentences max>","transcript_reference":"<brief quote or moment from sermon, or null>","suggestion":"<concrete suggestion if score 1-4, else null>","coaching_question":"<self-reflection question for the preacher>"}},
    "problem_naming":      {{"score":0,"label":"...","narrative":"...","transcript_reference":"...","suggestion":"...","coaching_question":"..."}},
    "proclamation":        {{"score":0,"label":"...","narrative":"...","transcript_reference":"...","suggestion":"...","coaching_question":"..."}},
    "practical_step":      {{"score":0,"label":"...","narrative":"...","transcript_reference":"...","suggestion":"...","coaching_question":"..."}},
    "picture_of_change":   {{"score":0,"label":"...","narrative":"...","transcript_reference":"...","suggestion":"...","coaching_question":"..."}},
    "total_score": 0,
    "total_label": "<Foundational|Developing|Emerging|Proficient|Exemplary>"
  }},
  "vocal": {{
    "filler_words":             {{"count":{filler_count},"per_minute":{filler_per_minute},"examples":["<word1>","<word2>","<word3>"],"score":0,"notes":"<1-2 sentence coaching note>"}},
    "pace":                     {{"avg_wpm":{estimated_wpm},"assessment":"fast|ideal|slow","score":0,"notes":"<1-2 sentences>"}},
    "rhetorical_variation":     {{"score":0,"db":{dynamic_range_db},"notes":"<1-2 sentences on volume range and expressiveness>"}},
    "pitch_variety":            {{"score":0,"notes":"<1-2 sentences on pitch variation (cv={pitch_cv:.3f})>"}},
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

5 P's Communication Framework scoring (each dimension 1-8, total max 40):
IMPORTANT: each narrative field must be 2 sentences maximum — no exceptions.
personal_connection: Does the preacher build authentic personal connection before asking the audience to engage?
  1-2=no personal connection, generic or institutional feel
  3-4=brief or surface connection, minimal story or personal reference
  5-6=some personal grounding but not fully developed
  7-8=rich, authentic, specific connection that earns trust and attention before the sermon begins
problem_naming: Is a real, felt human problem clearly named before the biblical text is opened?
  1-2=no problem named, jumps directly to answers or exposition
  3-4=problem vaguely implied or named too late in the sermon
  5-6=problem named but not personalized or emotionally developed
  7-8=problem clearly named and felt, creates urgency and openness for the message
proclamation: Is there a bold, declarative "this is what God has done" theological center?
  1-2=no clear proclamation, text referenced but never declared as truth
  3-4=proclamation present but buried, weak, or muddled with application
  5-6=clear proclamation but not given appropriate weight or placement
  7-8=bold, clear, well-placed proclamation that stands as the sermon's unmistakable center
practical_step: Is there one specific, achievable action the listener can take this week?
  1-2=no practical step offered, vague or missing application
  3-4=step offered but generic, vague, or too many competing steps
  5-6=practical but could be more specific or better connected to the proclamation
  7-8=single, clear, achievable step that flows naturally from the text and proclamation
picture_of_change: Does the preacher paint a vivid vision of what life looks like after obedience?
  1-2=no picture of change, sermon ends with command or explanation only
  3-4=vague or generic picture ("God will bless you") without specific imagery
  5-6=some picture of change but not fully developed or personally resonant
  7-8=vivid, specific, emotionally resonant vision of transformation
total_score = sum of all 5 scores (max 40)
total_label: Foundational(<=10), Developing(<=20), Emerging(<=28), Proficient(<=34), Exemplary(35+)

Gospel Check scoring:
jesus_as_hero:            true if Jesus is clearly the hero — cross/resurrection explicitly central, not assumed
heart_level_application:  true if application addresses heart motivations, not just behavior
behavior_change_present:  true if the sermon relies on behavior change alone (moralism flag — true = problem)
redemptive_history_noted: true if redemptive history or biblical narrative is meaningfully noted
nonchristian_accessible:  true if a non-Christian or skeptic could follow and engage

gold_standard="Yes"      if jesus_as_hero=true AND behavior_change_present=false AND 3+ other checks pass
gold_standard="No"       if jesus_as_hero=false OR (behavior_change_present=true AND 2+ other checks fail)
Otherwise "Partially".   incomplete_flag=true if jesus_as_hero=false.

Vocal score guide (use acoustic measurements above):
filler:       <0.5/min=10, <1.0=8, <2.0=6, <3.5=4, else 2
pace:         130-165wpm=10, 120-180=8, 100-200=6, else lower
rhetorical_variation (dynamic range): >=35dB=10, >=28=8, >=20=6, >=14=4, else 2
pitch_variety:cv>=0.28=10, >=0.20=8, >=0.12=6, >=0.06=4, else 2{pitch_variety_note}
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

CONSTRAINTS ON LANGUAGE (apply to every narrative, note, growth edge, encouragement, suggestion, and coaching_question field — not to numeric scores):
- Do NOT use the phrases: "energy arc is declining", "pausing instead of filling", or "would strengthen the expository integrity". These phrases have become overused across previous reports.
- Write each growth edge in language specific to this sermon. Reference a concrete moment, illustration, or phrase the preacher actually used.
- Avoid stock preaching-coach vocabulary unless it's the most precise word available.
"""


def evaluate_with_claude(transcript: str, speaker: str, acoustic: dict,
                         sermon_type: str = "expository",
                         has_audio: bool = True) -> dict:
    global _last_claude_usage
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
    sensor_error_note = (
        f"\nSENSOR ERROR: the measured dynamic range of {acoustic['dynamic_range_db']} dB "
        "exceeds 70 dB, which is physically impossible for human speech and indicates an "
        "audio-fidelity issue (clipping, extreme compression, or a podcast-feed export). "
        "For the rhetorical_variation field ONLY: set score to 0, and in notes write ONE "
        "sentence stating that audio fidelity prevented reliable measurement of this "
        "dimension and suggesting the user check for clipping, extreme compression, or a "
        "podcast-feed export as the likely cause. Do not narrate a strength or cite the dB "
        "number. All other vocal elements should be scored normally."
        if acoustic.get("dynamic_range_sensor_error") else ""
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
        sensor_error_note=sensor_error_note,
        duration_min=acoustic["duration_min"],
        estimated_wpm=acoustic["estimated_wpm"],
        filler_count=acoustic["filler_count"],
        filler_per_minute=acoustic["filler_per_minute"],
        dynamic_range_db=acoustic["dynamic_range_db"],
        pitch_cv=acoustic["pitch_cv"],
        arc_pattern=acoustic["arc_pattern"],
    )
    msg = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=4096,
        messages=[{"role": "user", "content": prompt}]
    )
    _last_claude_usage = {
        "input_tokens":  msg.usage.input_tokens,
        "output_tokens": msg.usage.output_tokens,
    }
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
    Page 2 -- 5 P's        : Personal Connection, Problem Naming,
                             Proclamation, Practical Step, Picture of Change
                             (colored bands, score bars, narrative,
                             transcript ref, suggestion, coaching question)
    Page 3 -- Vocal        : 6 acoustic/rhetorical elements, score bars,
                             measurement lines, full coaching notes
    Page 4 -- Gospel Check : Gold Standard badge, pass/fail checklist,
                             rubric subtotals, manual blanks
    Page 5 -- Scorecard    : all scores at a glance, coaching priorities
    """

    M  = 15.0    # left/right margin mm
    CW = 185.9   # usable content width (Letter 215.9 - 2*15)

    GOLD   = (175, 130,  25)   # headings, passage, section labels
    ORANGE = (200, 110,  20)   # flags, medium-score bars


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
        five_ps_total = int(ev.get("five_ps", {}).get("total_score", 0))
        five_ps_label = ev.get("five_ps", {}).get("total_label",
                            get_benchmark_label(round(five_ps_total / 40 * 10)))
        overall_t = five_ps_total + rub_t   # 40 + 20 + 25 + 15 = 100
        badges = [
            ("5 P's",        five_ps_total, 40, five_ps_label),
            ("Exegesis",     ex_t,   20,
             get_benchmark_label(round(ex_t / 20 * 10))),
            ("Application",  app_t,  25,
             get_benchmark_label(round(app_t / 25 * 10))),
            ("Presentation", prs_t,  15,
             get_benchmark_label(round(prs_t / 15 * 10))),
        ]
        rub_label = get_benchmark_label(round(overall_t / 100 * 10))

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
        self.cell(bw, 5, safe(f"\xb7 {overall_t}/100"), align="C")

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
            "are derived from acoustic analysis of the audio. Gospel and communication "
            "framework scores reflect AI interpretation of the transcript.")
        self.set_text_color(0, 0, 0)

    # ── Page 2: 5 P's Communication Framework ────────────────────────────────

    # Colors per dimension
    _P_DIM_CLR = {
        "personal_connection": (190,  70,  55),   # coral red
        "problem_naming":      (195, 130,  20),   # amber
        "proclamation":        ( 30,  70, 160),   # navy blue
        "practical_step":      ( 35, 130,  70),   # green
        "picture_of_change":   (110,  45, 145),   # purple
    }

    def page2(self, analysis: dict):
        self.add_page()
        self._top_bar(f"Page {self.page_no()}")
        self._page_title("Communication -- The 5 P's")

        fp = analysis.get("five_ps", {})

        P_DIMS = [
            ("personal_connection", "Personal Connection"),
            ("problem_naming",      "Problem Naming"),
            ("proclamation",        "Proclamation"),
            ("practical_step",      "Practical Step"),
            ("picture_of_change",   "Picture of Change"),
        ]

        for key, title in P_DIMS:
            self._check_page(38)

            dim        = fp.get(key, {})
            score      = int(dim.get("score", 0))
            label      = dim.get("label", get_benchmark_label(score))
            narrative  = dim.get("narrative", "")
            ref        = dim.get("transcript_reference") or ""
            suggestion = dim.get("suggestion") or ""
            coaching_q = dim.get("coaching_question", "")
            color      = self._P_DIM_CLR.get(key, C_NAVY)

            # Colored header band: title (left) | score + label (right)
            self.set_x(self.M)
            self.set_fill_color(*color)
            self.set_text_color(255, 255, 255)
            lw = self.CW * 0.60
            rw = self.CW - lw
            self.set_font("Helvetica", "B", 10)
            self.cell(lw, 8, safe(title), fill=True)
            self.set_font("Helvetica", "", 9)
            self.cell(rw, 8, safe(f"{score}/8  {label}"),
                      fill=True, align="R",
                      new_x="LMARGIN", new_y="NEXT")
            self.set_text_color(0, 0, 0)

            # Narrative
            if narrative:
                self.set_x(self.M)
                self.set_font("Helvetica", "", 9)
                self.multi_cell(self.CW, 5, safe(narrative))

            # Transcript reference — italic, muted
            if ref:
                self.set_x(self.M)
                self.set_font("Helvetica", "I", 9)
                self.set_text_color(*C_DGRAY)
                self.multi_cell(self.CW, 5, safe(f'Reference: "{ref}"'))
                self.set_text_color(0, 0, 0)

            # Suggestion — only when score ≤ 4
            if suggestion:
                self.set_x(self.M)
                self.set_font("Helvetica", "B", 9)
                self.set_text_color(*C_RED)
                self.write(5, "Suggestion: ")
                self.set_font("Helvetica", "", 9)
                self.set_text_color(0, 0, 0)
                self.write(5, safe(suggestion))
                self.ln(6)

            # Coaching question — small italic prompt for self-reflection
            if coaching_q:
                self.set_x(self.M)
                self.set_font("Helvetica", "I", 8)
                self.set_text_color(*C_MGRAY)
                self.multi_cell(self.CW, 4.5,
                                safe(f"Reflect: {coaching_q}"))
                self.set_text_color(0, 0, 0)

            self.ln(3)

        # Total footer
        self._check_page(12)
        self._rule(gap_before=2, gap_after=3)
        total       = int(fp.get("total_score", 0))
        total_label = fp.get("total_label", "")
        self.set_x(self.M)
        self.set_font("Helvetica", "B", 10)
        self.set_text_color(*C_NAVY)
        self.cell(self.CW, 6,
                  safe(f"5 P's Total: {total}/40 -- {total_label}"),
                  new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(0, 0, 0)

    # ── Page 3: Vocal / Rhetorical Delivery ───────────────────────────────────

    def page3(self, acoustic: dict, vocal_analysis: dict):
        has_audio = getattr(self, "has_audio", True)
        page_label = "Vocal Delivery" if has_audio else "Rhetorical Delivery"

        self.add_page()
        self._top_bar(f"Page {self.page_no()}")
        if has_audio:
            self._page_title(
                "Vocal Delivery -- 6 Elements (measured from audio)"
            )
        else:
            self._page_title(
                "Rhetorical Delivery -- 6 Elements (transcript analysis)"
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

        rv_sensor_error = a.get("dynamic_range_sensor_error", False)
        rv_score = (va.get("rhetorical_variation", {}).get("score", 0)
                    if rv_sensor_error else
                    va.get("rhetorical_variation", {}).get(
                        "score", a["dynamic_range_score"] or 0))

        # Each tuple: (display name, score, [italic measurement lines], coaching note, unavailable)
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
                False,
            ),
            (
                "2. Pace",
                va.get("pace", {}).get("score", a["wpm_score"]),
                [f"Measured: {wpm} wpm  ({'in ideal range' if pace_desc == 'ideal' else pace_desc} -- ideal: 130-170 wpm)"
                 "  (High confidence)"],
                va.get("pace", {}).get("notes", ""),
                False,
            ),
            (
                "3. Rhetorical Variation",
                rv_score,
                [f"Measured: {a['dynamic_range_db']} dB variation"
                 + ("  (High confidence)" if has_audio
                    else "  (Not available -- no audio)")],
                va.get("rhetorical_variation", {}).get("notes", ""),
                rv_sensor_error,
            ),
            (
                "4. Pitch Variety",
                va.get("pitch_variety", {}).get("score", a["vocal_variety_score"])
                    if has_audio else 0,
                [] if has_audio else ["Not scored -- no audio file"],
                va.get("pitch_variety", {}).get("notes", "")
                    if has_audio else "Pitch analysis requires audio.",
                False,
            ),
            (
                "5. Rhetorical Arc",
                va.get("rhetorical_arc", {}).get("score", a["energy_arc_score"]),
                [f"Pattern: {a['arc_pattern']}"
                 + ("  (High confidence)" if has_audio
                    else "  (Estimated from transcript)")],
                va.get("rhetorical_arc", {}).get("notes", ""),
                False,
            ),
            (
                "6. Verbal Clarity",
                va.get("verbal_clarity", {}).get("score", 0),
                ["Derived from transcript analysis  (High confidence)"],
                va.get("verbal_clarity", {}).get("notes", ""),
                False,
            ),
        ]

        counted_scores = []
        for name, score, meas_lines, note, unavailable in elements:
            self._check_page(35)   # new page if < 35 mm remain

            if unavailable:
                # Unavailable row: name + status label, single-line explanation, no bar, no score.
                self.set_x(self.M)
                self.set_font("Helvetica", "B", 11)
                self.set_text_color(*C_NAVY)
                self.cell(self.CW - 60, 7, safe(name))
                self.set_text_color(*C_DGRAY)
                self.set_font("Helvetica", "", 9)
                self.cell(60, 7, safe("Unavailable (audio fidelity issue)"),
                          align="R", new_x="LMARGIN", new_y="NEXT")
                self.set_text_color(0, 0, 0)
                # One-line explanation
                self.set_x(self.M)
                self.set_font("Helvetica", "I", 9)
                self.set_text_color(*C_DGRAY)
                self.multi_cell(self.CW, 5, safe(
                    f"Measured {a['dynamic_range_db']} dB -- exceeds the "
                    "human-speech range (~40-60 dB), indicating clipping, "
                    "extreme compression, or a podcast-feed export. "
                    "Score omitted from this report."
                ))
                self.set_text_color(0, 0, 0)
                # Claude's one-sentence note, if present
                if note:
                    self.set_x(self.M)
                    self.set_font("Helvetica", "", 10)
                    self.multi_cell(self.CW, 6, safe(note))
                self.ln(3)
                continue

            # Don't let pitch-no-audio contribute to the average
            if has_audio or "Pitch" not in name:
                counted_scores.append(score)

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

        avg = round(sum(counted_scores) / len(counted_scores), 1) if counted_scores else 0.0
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
                      "  Gospel Check: Christ Not Central",
                      fill=True, new_x="LMARGIN", new_y="NEXT")
            self.set_text_color(0, 0, 0)

        # Gospel narrative paragraph
        self.ln(2)
        self.set_x(self.M)
        self.set_font("Helvetica", "", 9)
        self.multi_cell(self.CW, 5, safe(gc.get("notes", "")))
        self.ln(3)

        # ── Pass/Fail Checkboxes ───────────────────────────────────────────────
        # (flag_when_true=True means True value is a red flag, not a pass)
        checks = [
            ("jesus_as_hero",            "Jesus was the hero of the sermon",         False),
            ("heart_level_application",  "Application addressed heart motivations",  False),
            ("behavior_change_present",  "Application flows from grace, not behavior-change alone", True),
            ("redemptive_history_noted", "Redemptive history / narrative noted",     False),
            ("nonchristian_accessible",  "Accessible to non-Christians / skeptics",  False),
        ]

        for key, label, flag_when_true in checks:
            val    = gc.get(key, False)
            passed = (not val) if flag_when_true else val
            marker = "PASS" if passed else "FAIL"
            col    = C_GREEN if passed else C_RED

            self.set_x(self.M)
            self.set_fill_color(*col)
            self.set_text_color(255, 255, 255)
            self.set_font("Helvetica", "B", 8)
            self.cell(12, 6, marker, fill=True, align="C")
            self.set_fill_color(255, 255, 255)
            self.set_text_color(0, 0, 0)
            self.set_font("Helvetica", "", 9)
            self.cell(self.CW - 12, 6, f"  {label}",
                      new_x="LMARGIN", new_y="NEXT")
        self.ln(3)

        self._rule(gap_before=0, gap_after=2)

        # ── Sermon Evaluation Rubric (individual line items) ───────────────────
        self._page_title("Sermon Evaluation Rubric")

        self.set_x(self.M)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(*C_DGRAY)
        self.multi_cell(self.CW, 4,
            "Note: Body language and note-dependence require in-person"
            " observation -- score manually below.")
        self.set_text_color(0, 0, 0)
        self.ln(2)

        score_col_w = 16
        lbl_w       = self.CW - score_col_w

        rubric_sections = [
            ("Exegesis & Theology", [
                ("context_set",        ex,  "Historical/cultural context set"),
                ("main_point_clear",   ex,  "Main point (thesis) clear"),
                ("preached_jesus",     ex,  "Jesus explicitly central"),
                ("redemptive_history", ex,  "Fits redemptive narrative"),
            ], ex_t, 20),
            ("Application", [
                ("clear_helpful_application", app, "Practical, helpful application"),
                ("gospel_centered",           app, "Flows from grace, not law"),
                ("clear_response",            app, "Listener knows what to do"),
                ("heart_care",                app, "Addresses heart motivations"),
                ("nonchristian_friendly",     app, "Accessible to skeptic"),
            ], app_t, 25),
            ("Presentation (auto-scored)", [
                ("engaging_intro",   pres, "Hook earns attention"),
                ("clear_structure",  pres, "Logical flow / clear structure"),
                ("voice_inflection", pres, "Varied delivery / voice"),
            ], pres_t, 15),
        ]

        for sec_label, items, sub_total, max_total in rubric_sections:
            self.set_x(self.M)
            self.set_fill_color(*C_NAVY)
            self.set_text_color(255, 255, 255)
            self.set_font("Helvetica", "B", 9)
            self.cell(self.CW, 6,
                      f"  {sec_label}   (Total: {sub_total} / {max_total})",
                      fill=True, new_x="LMARGIN", new_y="NEXT")
            self.set_text_color(0, 0, 0)

            for key, data_dict, item_label in items:
                score = data_dict.get(key, 0)
                self.set_x(self.M)
                self.set_font("Helvetica", "", 9)
                self.cell(lbl_w, 5, f"  {item_label}")
                self.set_font("Helvetica", "B", 9)
                self.cell(score_col_w, 5, f"{score} / 5",
                          align="R", new_x="LMARGIN", new_y="NEXT")
            self.ln(2)

        # Manual scoring blanks
        self._rule(gap_before=1, gap_after=1)
        self.set_x(self.M)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(*C_DGRAY)
        self.cell(self.CW, 4, "Manual scoring (in-person observation required):",
                  new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(0, 0, 0)
        self.ln(1)

        lbl_w2 = self.CW - 18
        self.set_font("Helvetica", "", 9)
        self.set_x(self.M)
        self.cell(lbl_w2, 6,
                  "Body language enhanced the sermon and was not distracting")
        self.cell(18, 6, "___ / 5", align="R",
                  new_x="LMARGIN", new_y="NEXT")
        self.set_x(self.M)
        self.cell(lbl_w2, 6, "Preacher did not seem overly reliant on notes")
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

    def page5(self, analysis: dict, gospel_check: dict, acoustic: dict):
        self.add_page()
        self._top_bar(f"Page {self.page_no()}")
        self._page_title("Scorecard -- All Scores at a Glance")

        ev = analysis
        gc = gospel_check
        lw = self.CW - 32   # label column width

        # ── 5 P's Communication ───────────────────────────────────────────────
        self.set_x(self.M)
        self.set_font("Helvetica", "B", 10)
        self.set_text_color(*self.GOLD)
        self.cell(self.CW, 6, "5 P'S COMMUNICATION",
                  new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(0, 0, 0)

        fp = ev.get("five_ps", {})
        P_SCORECARD = [
            ("personal_connection", "Personal Connection"),
            ("problem_naming",      "Problem Naming"),
            ("proclamation",        "Proclamation"),
            ("practical_step",      "Practical Step"),
            ("picture_of_change",   "Picture of Change"),
        ]
        for p_key, p_name in P_SCORECARD:
            dim    = fp.get(p_key, {})
            score  = int(dim.get("score", 0))
            label  = dim.get("label", get_benchmark_label(score))
            sc_col = C_GREEN if score >= 7 else (C_RED if score <= 3 else self.ORANGE)

            self.set_x(self.M)
            self.set_font("Helvetica", "", 9)
            self.cell(lw, 5, safe(p_name))
            self.set_text_color(*sc_col)
            self.set_font("Helvetica", "B", 9)
            self.cell(16, 5, f"{score}/8", align="R")
            self.set_text_color(*C_DGRAY)
            self.set_font("Helvetica", "", 8)
            self.cell(16, 5, safe(label), align="R",
                      new_x="LMARGIN", new_y="NEXT")
            self.set_text_color(0, 0, 0)

        # 5 P's total row
        p_total       = int(fp.get("total_score", 0))
        p_total_label = fp.get("total_label", "")
        self.set_x(self.M)
        self.set_fill_color(*C_LGRAY)
        self.set_font("Helvetica", "B", 9)
        self.cell(lw, 5, "  5 P's Total", fill=True)
        self.cell(32, 5, safe(f"{p_total}/40  {p_total_label}"),
                  align="R", fill=True, new_x="LMARGIN", new_y="NEXT")
        self.set_fill_color(255, 255, 255)
        self.ln(3)

        # ── Vocal / Rhetorical Scores ─────────────────────────────────────────
        self.set_x(self.M)
        self.set_font("Helvetica", "B", 10)
        self.set_text_color(*self.GOLD)
        self.cell(self.CW, 6, "VOCAL / RHETORICAL DELIVERY",
                  new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(0, 0, 0)

        va = ev.get("vocal", {})
        rv_unavailable = acoustic.get("dynamic_range_sensor_error", False)
        vocal_items = [
            ("Filler Words",         va.get("filler_words",         {}).get("score", 0), False),
            ("Pace",                 va.get("pace",                 {}).get("score", 0), False),
            ("Rhetorical Variation", va.get("rhetorical_variation", {}).get("score", 0), rv_unavailable),
            ("Pitch Variety",        va.get("pitch_variety",        {}).get("score", 0), False),
            ("Rhetorical Arc",       va.get("rhetorical_arc",       {}).get("score", 0), False),
            ("Verbal Clarity",       va.get("verbal_clarity",       {}).get("score", 0), False),
        ]

        for vname, vscore, unavailable in vocal_items:
            if unavailable:
                bench  = "Unavailable"
                sc_col = C_DGRAY
            else:
                bench  = get_benchmark_label(vscore)
                sc_col = C_GREEN if vscore >= 8 else (C_RED if vscore <= 4 else self.ORANGE)

            self.set_x(self.M)
            self.set_font("Helvetica", "", 9)
            self.cell(lw, 5, safe(vname))
            self.set_text_color(*sc_col)
            self.set_font("Helvetica", "B", 9)
            self.cell(16, 5, "N/A" if unavailable else f"{vscore}/10", align="R")
            self.set_text_color(*C_DGRAY)
            self.set_font("Helvetica", "", 8)
            self.cell(16, 5, safe(bench), align="R",
                      new_x="LMARGIN", new_y="NEXT")
            self.set_text_color(0, 0, 0)

        self.ln(3)

        # ── Gospel Check ───────────────────────────────────────────────────────
        self.set_x(self.M)
        self.set_font("Helvetica", "B", 10)
        self.set_text_color(*self.GOLD)
        self.cell(self.CW, 6, "GOSPEL CHECK",
                  new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(0, 0, 0)

        # Gold Standard one-liner
        gold_std = gc.get("gold_standard", "Partially")
        gold_col = (C_GREEN if gold_std == "Yes"
                    else (C_RED if gold_std == "No" else self.ORANGE))
        self.set_x(self.M)
        self.set_fill_color(*gold_col)
        self.set_text_color(255, 255, 255)
        self.set_font("Helvetica", "B", 8)
        self.cell(self.CW, 5, safe(f"  Gold Standard: {gold_std}"),
                  fill=True, new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(0, 0, 0)
        self.ln(1)

        # PASS/FAIL checklist (compact)
        checks = [
            ("jesus_as_hero",            "Jesus was the hero of the sermon",                        False),
            ("heart_level_application",  "Application addressed heart motivations",                 False),
            ("behavior_change_present",  "Application flows from grace, not behavior-change alone", True),
            ("redemptive_history_noted", "Redemptive history / narrative noted",                    False),
            ("nonchristian_accessible",  "Accessible to non-Christians / skeptics",                 False),
        ]
        passes = 0
        for key, label, flag_when_true in checks:
            val    = gc.get(key, False)
            passed = (not val) if flag_when_true else val
            if passed:
                passes += 1
            marker = "PASS" if passed else "FAIL"
            col    = C_GREEN if passed else C_RED

            self.set_x(self.M)
            self.set_fill_color(*col)
            self.set_text_color(255, 255, 255)
            self.set_font("Helvetica", "B", 7)
            self.cell(10, 5, marker, fill=True, align="C")
            self.set_fill_color(255, 255, 255)
            self.set_text_color(0, 0, 0)
            self.set_font("Helvetica", "", 8)
            self.cell(self.CW - 10, 5, f"  {label}",
                      new_x="LMARGIN", new_y="NEXT")

        # Total row
        self.set_x(self.M)
        self.set_fill_color(*C_LGRAY)
        self.set_font("Helvetica", "B", 9)
        self.cell(lw, 5, "  Gospel Check Total", fill=True)
        self.cell(32, 5, f"{passes}/5 checks passed", align="R", fill=True,
                  new_x="LMARGIN", new_y="NEXT")
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

        # ── Feedback CTA ──────────────────────────────────────────────────────
        _FEEDBACK_URL = FEEDBACK_URL
        self._rule(gap_before=8, gap_after=5)
        self.set_x(self.M)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(*C_MGRAY)
        self.multi_cell(
            self.CW, 5,
            safe("This tool is built by a preacher, for preachers. "
                 "Your honest feedback shapes what it becomes."),
            align="C",
        )
        self.ln(2)
        self.set_x(self.M)
        self.set_font("Helvetica", "BU", 9)
        self.set_text_color(25, 55, 110)   # C_NAVY
        cw2 = self.CW
        self.cell(cw2, 6, "Submit Feedback", align="C", link=_FEEDBACK_URL,
                  new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(0, 0, 0)


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
    pdf.page5(analysis, gospel_check, acoustic)
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

    print(f"-- 5 P'S COMMUNICATION {'─'*40}")
    fp      = ev.get("five_ps", {})
    P_NAMES = [
        ("personal_connection", "Personal Connection"),
        ("problem_naming",      "Problem Naming"),
        ("proclamation",        "Proclamation"),
        ("practical_step",      "Practical Step"),
        ("picture_of_change",   "Picture of Change"),
    ]
    for p_key, p_name in P_NAMES:
        dim   = fp.get(p_key, {})
        score = int(dim.get("score", 0))
        lbl   = dim.get("label", "")
        b     = terminal_bar(score, 8, 8)
        print(f"  {p_name:<22}  {lbl:<12}  {score}/8  [{b}]")
        if dim.get("suggestion"):
            print(f"       Suggestion: {dim.get('suggestion','')[:70]}")
    p_total = int(fp.get("total_score", 0))
    print(f"  {'TOTAL':<22}  {fp.get('total_label',''):<12}  {p_total}/40")
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
# Use directory relative to this script (works on Railway /app/ and locally)
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPORTS_PERSONAL = os.path.join(_SCRIPT_DIR, "reports", "personal")
REPORTS_BETA = os.path.join(_SCRIPT_DIR, "reports", "beta")


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
    ap.add_argument("--start-sec", type=int, default=None,
                    dest="start_sec",
                    help="Trim audio: start offset in seconds (used for full-service recordings)")
    ap.add_argument("--end-sec", type=int, default=None,
                    dest="end_sec",
                    help="Trim audio: end offset in seconds (used for full-service recordings)")
    ap.add_argument("--email", default="",
                    help="Submitter email address (passed from web app for usage logging)")
    ap.add_argument("--source-type", default=None, dest="source_type",
                    help="Source type for logging: youtube, podcast, file_upload, local_file")
    args = ap.parse_args()

    start_time = time.monotonic()   # used for processing_time_sec in usage log

    if not GROQ_API_KEY and not OPENAI_API_KEY:
        sys.exit("Set GROQ_API_KEY or OPENAI_API_KEY environment variable.")
    if not ANTHROPIC_API_KEY: sys.exit("Set ANTHROPIC_API_KEY environment variable.")

    source       = args.source
    source_label = Path(source).name if Path(source).exists() else source[:80]
    is_url       = not Path(source).exists()

    # ── Derive source_type for logging if not explicitly passed ───────────────
    if args.source_type:
        log_source_type = args.source_type
    elif is_url:
        src_lower = source.lower()
        if "youtube.com" in src_lower or "youtu.be" in src_lower:
            log_source_type = "youtube"
        else:
            log_source_type = "podcast"
    else:
        log_source_type = "local_file"

    # ── Resolve speaker name ───────────────────────────────────────────────────
    # Words that suggest the metadata returned an org/church name, not a person
    _CHURCH_WORDS = {"church", "ministry", "ministries", "community",
                     "fellowship", "chapel", "cathedral", "parish",
                     "diocese", "tabernacle", "assembly", "congregation"}

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

    # ── Base log fields (known before analysis begins) ────────────────────────
    _log_fields = {
        "preacher_name": speaker,
        "email":         args.email,
        "source_type":   log_source_type,
        "source_value":  source_label,
        "sermon_type":   args.sermon_type,
    }

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            print("\n[1/4] Acquiring audio ...")
            mp3 = acquire_audio(source, tmpdir)
            print(f"  Ready: {mp3}")

            # ── Trim to sermon window if --start-sec / --end-sec were provided ────
            if args.start_sec is not None and args.end_sec is not None:
                print(f"  Trimming to sermon window: {args.start_sec}s – {args.end_sec}s ...")
                trimmed = os.path.join(tmpdir, "sermon_trimmed.mp3")
                subprocess.run([
                    "ffmpeg", "-y", "-i", mp3,
                    "-ss", str(args.start_sec), "-to", str(args.end_sec),
                    "-vn", "-c:a", "libmp3lame", "-q:a", "4",
                    trimmed,
                ], check=True, capture_output=True)
                mp3 = trimmed
                print(f"  Trimmed audio ready: {mp3}")
            # ─────────────────────────────────────────────────────────────────────

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

            # ── Log successful run ────────────────────────────────────────────
            if _LOGGER_AVAILABLE:
                gc       = analysis.get("gospel_check", {})
                in_tok   = _last_claude_usage["input_tokens"]
                out_tok  = _last_claude_usage["output_tokens"]
                # Groq Whisper: $0.0014/min  |  OpenAI Whisper: $0.02/min
                _w_rate  = 0.0014 if _last_whisper_provider == "groq" else 0.02
                w_cost   = round(acoustic["duration_min"] * _w_rate, 4)
                c_cost   = round((in_tok * 3 + out_tok * 15) / 1_000_000, 4)
                # Count passing Gospel Check items (behavior_change_present is a flag — True = fail)
                gc_total = sum([
                    bool(gc.get("jesus_as_hero")),
                    bool(gc.get("heart_level_application")),
                    not bool(gc.get("behavior_change_present")),
                    bool(gc.get("redemptive_history_noted")),
                    bool(gc.get("nonchristian_accessible")),
                ])
                _log_fields.update({
                    "duration_min":         acoustic["duration_min"],
                    "word_count":           len(transcript.split()),
                    "whisper_chunks":       _last_whisper_chunks,
                    "whisper_cost_usd":     w_cost,
                    "claude_input_tokens":  in_tok,
                    "claude_output_tokens": out_tok,
                    "claude_cost_usd":      c_cost,
                    "total_cost_usd":       round(w_cost + c_cost, 4),
                    "processing_time_sec":  round(time.monotonic() - start_time, 1),
                    "overall_score":        analysis.get("five_ps", {}).get("total_score", ""),
                    "gospel_check_total":   gc_total,
                    "gold_standard_flag":   gc.get("gold_standard", ""),
                    "incomplete_flag":      gc.get("incomplete_flag", False),
                    "success":              True,
                    "error_message":        "",
                })
                _usage_logger.log_sermon_run(_log_fields)

    except Exception as _exc:
        # ── Log failed run (non-crashing) ─────────────────────────────────────
        if _LOGGER_AVAILABLE:
            _log_fields.update({
                "processing_time_sec": round(time.monotonic() - start_time, 1),
                "success":             False,
                "error_message":       str(_exc)[:500],
            })
            _usage_logger.log_sermon_run(_log_fields)
        raise


if __name__ == "__main__":
    main()
