"""Understand a YouTube video from its transcript + description — explain what it teaches, and
HONESTLY flag when the real content is visual (so the transcript alone won't do it justice).

"Watch" is a human word; Syntra can't see the video. Given the transcript (+ description) this:
  1. EXPLAINS the core / teachings / whole point (grounded strictly in the words — no invented facts),
  2. judges the VISUAL GAP — is the speaker narrating self-contained ideas, or talking *about*
     something shown on screen (code/slides/whiteboard/demo/diagrams) that the transcript can't
     convey? — and when the gap is high, tells the user to actually watch it (and offers to help
     with screenshots of the visual parts).

Pure-with-injected-caller (mirrors compaction.summarize_turns): `caller(messages) -> result` with
`result.text`. No I/O, no hard LLM dependency → unit-testable with a fake caller.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Deixis / on-screen-reference phrases: the speaker is pointing at something the words don't carry.
_DEIXIS = [
    "as you can see", "as you see", "you can see here", "see here", "over here", "right here",
    "on the left", "on the right", "at the top", "at the bottom", "this line", "this code",
    "this function", "this diagram", "this graph", "this chart", "this slide", "this image",
    "this picture", "look at this", "watch this", "on screen", "on the screen", "up here",
    "down here", "this part", "this button", "click here", "notice how", "you'll notice",
    "shown here", "highlighted", "the animation", "let me show you", "let me draw",
    "as shown", "pointing to", "this red", "this blue", "this arrow", "the box",
]
# Content-type hints that a video is a demo/tutorial where the doing is visual.
_VISUAL_KIND = [
    "tutorial", "walkthrough", "demo", "demonstration", "screen share", "screencast",
    "let's build", "let me show", "coding", "whiteboard", "hands-on", "step by step",
    "drawing", "sketch", "diagram", "visualiz",
]


@dataclass
class VisualGap:
    level: str = "low"        # "high" | "some" | "low"
    reason: str = ""
    signals: list = field(default_factory=list)


@dataclass
class Understanding:
    explanation: str = ""     # the model's grounded explanation of the video's core/teachings
    visual_gap: VisualGap = field(default_factory=VisualGap)
    used_words: int = 0
    method: str = ""          # "stuff" | "map-reduce"
    watch_advice: str = ""    # non-empty when the user should actually watch the video


# ---------------------------------------------------------------- heuristic pre-signals
def visual_signals(transcript: str, title: str = "", description: str = "",
                   length_s: int = 0, segment_count: int = 0) -> VisualGap:
    """Cheap, LLM-free heuristic pass — counts deixis + content-type hints + speech density.
    The LLM makes the FINAL call (given more context), but these signals seed it and stand alone
    if no model is available. Pure."""
    t = (transcript or "").lower()
    hay = " ".join([title.lower(), description.lower(), t])
    sig = []
    deixis_hits = sum(t.count(p) for p in _DEIXIS)
    if deixis_hits >= 3:
        sig.append(f"{deixis_hits} on-screen references ('as you see', 'this code', …)")
    kind_hits = sorted({k for k in _VISUAL_KIND if k in hay})
    if kind_hits:
        sig.append("demo/tutorial cues: " + ", ".join(kind_hits[:4]))
    words = len(t.split())
    # words-per-second: a talking-head explainer is ~2–3 wps; a lot of showing/silence drops it.
    # Only trust density once there's enough transcript to judge (a tiny snippet skews wps).
    if length_s > 60 and words >= 100:
        wps = words / length_s
        if wps < 1.2:
            sig.append(f"low speech density ({wps:.2f} words/sec — long stretches of showing, not telling)")
    # near-empty transcript for a long video → music/visual, not a talk
    if length_s > 120 and words >= 20 and words < length_s * 0.3:
        sig.append("very sparse transcript for the length (little spoken content)")
    # decide level from the signal weight
    score = (deixis_hits >= 6) * 2 + (deixis_hits >= 3) + bool(kind_hits) + \
            (len(sig) >= 3) + (words and length_s > 60 and words / max(1, length_s) < 1.0)
    level = "high" if score >= 3 else "some" if score >= 1 else "low"
    reason = "; ".join(sig) if sig else "the transcript reads as self-contained narration"
    return VisualGap(level=level, reason=reason, signals=sig)


# ---------------------------------------------------------------- chunking (segment-aware)
def _approx_tokens(s: str) -> int:
    return max(1, len(s) // 4)     # ~4 chars/token rough estimate (stdlib, no tokenizer)


def chunk_segments(segments: list, *, target_tokens: int = 1400, overlap_tokens: int = 100) -> list:
    """Group timestamped segments into ~target_tokens chunks WITHOUT splitting a segment (keeps
    timestamps intact). Small overlap for continuity. Returns list[(start_s, text)]. Pure."""
    chunks, cur, cur_tok, cur_start = [], [], 0, None
    for seg in segments:
        st = getattr(seg, "start_s", 0.0)
        txt = getattr(seg, "text", "") or ""
        if cur_start is None:
            cur_start = st
        cur.append(txt)
        cur_tok += _approx_tokens(txt)
        if cur_tok >= target_tokens:
            chunks.append((cur_start, " ".join(cur)))
            # start next chunk with a small textual overlap (last ~overlap_tokens worth)
            keep, ktok = [], 0
            for w in reversed(" ".join(cur).split()):
                keep.insert(0, w); ktok += 1
                if ktok * 1 >= overlap_tokens:   # ~1 token/word
                    break
            cur, cur_tok, cur_start = ([" ".join(keep)] if keep else []), _approx_tokens(" ".join(keep)), st
    if cur and " ".join(cur).strip():
        chunks.append((cur_start or 0.0, " ".join(cur)))
    return chunks


# ---------------------------------------------------------------- prompts
_EXPLAIN_SYSTEM = (
    "You explain what a YouTube video teaches, using ONLY the transcript and description provided. "
    "Do NOT invent facts, names, numbers, or conclusions not present in the text. If something "
    "isn't covered, say so. First identify the key points, then explain the core idea and the "
    "main takeaways clearly and plainly."
)
_MAP_SYSTEM = (
    "Summarize this ONE section of a video transcript using ONLY its text. List the key points, "
    "no invented facts. Keep it tight."
)
_VISUAL_SYSTEM = (
    "You judge whether a video's core meaning can be understood from its TRANSCRIPT ALONE, or "
    "whether the important content is VISUAL (code/slides/whiteboard/diagrams/on-screen demo that "
    "the words only point at). Given the transcript + description, answer with a first line exactly "
    "'VISUAL_GAP: high' or 'VISUAL_GAP: some' or 'VISUAL_GAP: low', then one sentence why."
)


def _fmt(caller, system, user):
    from ..providers.openai_compat import ChatMessage
    res = caller([ChatMessage("system", system), ChatMessage("user", user)])
    return (getattr(res, "text", "") or "").strip()


# ---------------------------------------------------------------- main entry
def understand_video(video, *, caller, stuff_token_budget: int = 16000) -> Understanding:
    """Explain `video` (a youtube.VideoData) and judge its visual gap.

    Routes by size: a short transcript is explained in ONE grounded call ("stuff"); a long one is
    map-reduced (per-chunk key points → final explanation) with the title/description as a global
    brief. Then a visual-gap judgement (heuristic signals + one LLM call) decides whether to advise
    the user to actually watch it. `caller(messages)->result.text`. Pure but for the injected caller.
    """
    transcript = getattr(video, "transcript", "") or ""
    title = getattr(video, "title", "") or ""
    desc = getattr(video, "description", "") or ""
    segments = getattr(video, "segments", []) or []
    length_s = getattr(video, "length_s", 0) or 0

    out = Understanding(used_words=len(transcript.split()))
    brief = f"TITLE: {title}\nCHANNEL: {getattr(video,'author','')}\nDESCRIPTION:\n{desc[:1500]}"

    if not transcript.strip():
        out.explanation = "(no transcript text to explain)"
        out.method = "none"
    elif _approx_tokens(transcript) <= stuff_token_budget:
        out.method = "stuff"
        out.explanation = _fmt(caller, _EXPLAIN_SYSTEM,
            f"{brief}\n\nFULL TRANSCRIPT:\n{transcript}\n\n"
            "Explain what this video teaches: a short TL;DR, then the key points, then the main "
            "takeaways. Use only the transcript + description above.")
    else:
        out.method = "map-reduce"
        chunks = chunk_segments(segments)
        maps = []
        for i, (st, ctext) in enumerate(chunks):
            m = f"[{int(st//60)}:{int(st%60):02d}]"
            maps.append(f"{m} " + _fmt(caller, _MAP_SYSTEM,
                f"GLOBAL CONTEXT (for reference only):\n{brief}\n\nSECTION {i+1}/{len(chunks)}:\n{ctext}"))
        out.explanation = _fmt(caller, _EXPLAIN_SYSTEM,
            f"{brief}\n\nSECTION SUMMARIES (in order, with start timestamps):\n" +
            "\n\n".join(maps) +
            "\n\nUsing ONLY these section summaries, explain what the video teaches: a TL;DR, the "
            "key points with their timestamps, and the main takeaways. Add no new facts.")

    # visual-gap: heuristic signals first, then let the model confirm/override the level.
    vg = visual_signals(transcript, title, desc, length_s, len(segments))
    try:
        verdict = _fmt(caller, _VISUAL_SYSTEM,
            f"{brief}\n\nHEURISTIC SIGNALS (may help): {vg.reason}\n\n"
            f"TRANSCRIPT (first part):\n{transcript[:6000]}")
        m = re.match(r"\s*VISUAL_GAP:\s*(high|some|low)", verdict, re.I)
        if m:
            vg.level = m.group(1).lower()
            tail = verdict[m.end():].strip(" .:\n-")
            if tail:
                vg.reason = tail[:300]
    except Exception:  # noqa: BLE001 - the heuristic verdict stands if the model call fails
        pass
    out.visual_gap = vg

    if vg.level == "high":
        out.watch_advice = ("The core of this video is VISUAL — the speaker is largely explaining "
                            "things shown on screen (code/slides/whiteboard/demo) that the transcript "
                            "can't convey. I've explained what I can from the words, but you should "
                            "actually WATCH it to get the full point. I can pull screenshots of the "
                            "key moments if you want.")
    elif vg.level == "some":
        out.watch_advice = ("Parts of this video rely on on-screen visuals the transcript doesn't "
                            "capture — my explanation covers the spoken content; watch those parts "
                            "directly for the visual detail.")
    return out
