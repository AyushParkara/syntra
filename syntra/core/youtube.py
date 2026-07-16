"""Fetch a YouTube video's metadata + description + full transcript (stdlib only).

"Watch" is a human word — this can't see a video. It pulls the WHOLE transcript (any available
language) plus the title/description so a caller can EXPLAIN what the video teaches, and flag when
the real content is visual (so the transcript alone won't do it justice).

Method (proven working 2026, no pip deps, no PoToken for most videos): call the InnerTube ANDROID
`player` endpoint for metadata + caption-track base URLs, then GET a track's baseUrl and parse the
timedtext XML (`<timedtext format="3"><body><p t=ms d=ms>text</p>`). Falls back to scraping the
watch page's inline `ytInitialPlayerResponse`, and — only if the binary is present — to `yt-dlp`
for PoToken-walled / gated videos. A minority of videos carry `&exp=xpe` and return an empty body
that requires a browser-minted PoToken we cannot make in stdlib; those are reported honestly.
"""

from __future__ import annotations

import html as _html
import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from xml.etree import ElementTree as ET

_INNERTUBE_ENDPOINT = "https://www.youtube.com/youtubei/v1/player?key="
_YOUTUBE_KEY_HELP = (
    "set SYNTRA_YOUTUBE_INNERTUBE_KEY or create ~/.config/syntra/youtube.json "
    "with {\"innertube_key\": \"...\"} to enable the YouTube transcript feature"
)
# ANDROID client: needs no cookies and has the lowest caption-PoToken exposure (yt-dlp flags only
# the WEB client as needing a Subs PoToken). Version rotates — keep it as one editable constant.
_ANDROID_CLIENT = {"clientName": "ANDROID", "clientVersion": "20.10.38", "hl": "en"}
_ANDROID_UA = "com.google.android.youtube/20.10.38 (Linux; U; Android 14) gzip"
_BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
_ID_RE = re.compile(r"(?:v=|/shorts/|/embed/|/v/|/live/|youtu\.be/)([A-Za-z0-9_-]{11})")
_BARE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


@dataclass
class Segment:
    start_s: float
    text: str


@dataclass
class VideoData:
    video_id: str
    ok: bool = False
    status: str = ""              # "ok" | "no_captions" | "potoken_gated" | "unavailable:<reason>" | "error:<msg>"
    title: str = ""
    author: str = ""
    length_s: int = 0
    description: str = ""
    lang: str = ""                # caption language actually used
    kind: str = ""                # "manual" | "asr"
    segments: list = field(default_factory=list)   # list[Segment]
    source: str = ""              # which extractor path succeeded (innertube/scrape/yt-dlp)

    @property
    def transcript(self) -> str:
        return " ".join(s.text for s in self.segments).strip()

    @property
    def word_count(self) -> int:
        return len(self.transcript.split())


# ---------------------------------------------------------------- url parsing
def video_id(url_or_id: str) -> str | None:
    """Extract the 11-char video id from any YouTube URL form (watch/youtu.be/shorts/embed/live/
    music/m/-nocookie, extra params ok), or accept a bare id. Returns None if none found."""
    s = (url_or_id or "").strip()
    if _BARE_ID_RE.match(s):
        return s
    try:
        u = urlparse(s if "://" in s else "https://" + s)
        host = (u.hostname or "").lower()
        if host.endswith("youtu.be"):
            seg = u.path.lstrip("/").split("/")[0]
            if _BARE_ID_RE.match(seg):
                return seg
        if "youtube" in host:
            q = parse_qs(u.query)
            if q.get("v") and _BARE_ID_RE.match(q["v"][0]):
                return q["v"][0]
    except Exception:  # noqa: BLE001
        pass
    m = _ID_RE.search(s)          # catch-all fallback
    return m.group(1) if m else None


# ---------------------------------------------------------------- http helpers
def _opener():
    # A cookie jar lets us satisfy the EU/logged-out consent gate (CONSENT=YES+...).
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))


def _get(url: str, *, ua: str = _BROWSER_UA, timeout: float = 20.0, opener=None) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept-Language": "en-US,en;q=0.9"})
    op = opener.open if opener is not None else urllib.request.urlopen
    return op(req, timeout=timeout).read().decode("utf-8", "replace")


def _post_json(url: str, body: dict, *, ua: str, timeout: float = 20.0) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"User-Agent": ua, "Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def _config_dir() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return (Path(xdg).expanduser() if xdg else Path.home() / ".config") / "syntra"


def innertube_key() -> str:
    """User-supplied YouTube InnerTube key, never bundled.

    YouTube clients use an InnerTube key to call `youtubei/v1/player`. Public examples of such
    keys exist, but Syntra does not ship one so public releases do not look like they contain a
    private Google key. Users who want `/watch`/`youtube_transcript` can provide their own key via
    env or config.
    """
    val = (os.environ.get("SYNTRA_YOUTUBE_INNERTUBE_KEY") or "").strip()
    if val:
        return val
    try:
        data = json.loads((_config_dir() / "youtube.json").read_text(encoding="utf-8"))
        return str(data.get("innertube_key") or data.get("api_key") or "").strip()
    except (OSError, ValueError, TypeError):
        return ""


def innertube_key_help() -> str:
    return _YOUTUBE_KEY_HELP


# ---------------------------------------------------------------- transcript parse
def _parse_timedtext(xml_text: str) -> list:
    """Parse the timedtext format=3 XML (`<body><p t=ms d=ms>text</p>`; text may be split across
    <s> children). Returns list[Segment] (start in seconds). Empty list on empty/garbage body."""
    if not xml_text.strip():
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    segs = []
    for p in root.iter("p"):
        parts = ([p.text] if p.text else []) + [x for s in p for x in (s.text, s.tail) if x]
        txt = _html.unescape("".join(parts)).replace("\n", " ").strip()
        if txt:
            segs.append(Segment(start_s=int(p.get("t", 0)) / 1000.0, text=txt))
    return segs


def _select_track(tracks: list, want_lang: str | None) -> dict | None:
    """Pick a caption track: requested lang → en manual → en asr → any manual → any asr."""
    if not tracks:
        return None
    def is_asr(t):
        return t.get("kind", "") == "asr"
    if want_lang:
        for t in tracks:
            if t.get("languageCode", "").startswith(want_lang):
                return t
    for pred in (lambda t: t.get("languageCode", "").startswith("en") and not is_asr(t),
                 lambda t: t.get("languageCode", "").startswith("en") and is_asr(t),
                 lambda t: not is_asr(t),
                 lambda t: True):
        for t in tracks:
            if pred(t):
                return t
    return None


def _fetch_track(base_url: str) -> list:
    """GET a caption baseUrl and parse it. Returns [] on empty body / PoToken wall / error."""
    if "&exp=xpe" in base_url or "?exp=xpe" in base_url:
        return []   # PoToken-gated: a raw fetch returns an empty body — don't bother.
    try:
        return _parse_timedtext(_get(base_url))
    except Exception:  # noqa: BLE001
        return []


# ---------------------------------------------------------------- caption-track sources
def _tracks_from_player(player: dict) -> list:
    return (player.get("captions", {})
                  .get("playerCaptionsTracklistRenderer", {})
                  .get("captionTracks", []) or [])


def _meta_from_player(player: dict, out: VideoData) -> None:
    vd = player.get("videoDetails", {}) or {}
    out.title = vd.get("title", "") or out.title
    out.author = vd.get("author", "") or out.author
    out.description = vd.get("shortDescription", "") or out.description
    try:
        out.length_s = int(vd.get("lengthSeconds", 0) or 0)
    except (TypeError, ValueError):
        pass


def _scrape_player(vid: str, opener) -> dict | None:
    """Fetch the watch page and pull the inline ytInitialPlayerResponse (captions still inline in
    2026). Handles the consent interstitial via the CONSENT cookie."""
    url = f"https://www.youtube.com/watch?v={vid}&hl=en"
    try:
        html = _get(url, opener=opener)
    except Exception:  # noqa: BLE001
        return None
    if "consent.youtube" in html or 'action="https://consent.youtube.com' in html:
        m = re.search(r'name="v" value="(.*?)"', html)
        if m:
            try:
                opener.open(urllib.request.Request(
                    "https://www.youtube.com/", headers={"User-Agent": _BROWSER_UA,
                    "Cookie": f"CONSENT=YES+{m.group(1)}"}), timeout=15)
                html = _get(url, opener=opener)
            except Exception:  # noqa: BLE001
                pass
    m = re.search(r"ytInitialPlayerResponse\s*=\s*", html)
    if not m:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(html, m.end())
        return obj
    except Exception:  # noqa: BLE001
        return None


def _ytdlp_transcript(vid: str, want_lang: str | None) -> tuple[list, str, str]:
    """Last-resort fallback via yt-dlp IF the binary exists — the only path for PoToken-walled /
    age-gated videos. Returns (segments, lang, kind) or ([], "", "")."""
    if not shutil.which("yt-dlp"):
        return [], "", ""
    import tempfile, os, glob
    lang = (want_lang or "en") + ".*"
    with tempfile.TemporaryDirectory() as td:
        cmd = ["yt-dlp", "--skip-download", "--write-subs", "--write-auto-subs",
               "--sub-langs", lang, "--sub-format", "json3/vtt/best",
               "--extractor-args", "youtube:formats=missing_pot",
               "-o", os.path.join(td, "%(id)s.%(ext)s"),
               f"https://www.youtube.com/watch?v={vid}"]
        try:
            subprocess.run(cmd, capture_output=True, timeout=90)
        except Exception:  # noqa: BLE001
            return [], "", ""
        # prefer json3, then vtt
        for pat, parser in ((f"{td}/*.json3", _parse_ytdlp_json3), (f"{td}/*.vtt", _parse_vtt)):
            files = sorted(glob.glob(pat))
            if files:
                try:
                    with open(files[0], encoding="utf-8", errors="replace") as _fh:
                        txt = _fh.read()
                    segs = parser(txt)
                    if segs:
                        auto = ".auto." in files[0] or files[0].endswith(".asr.json3")
                        return segs, "en", ("asr" if auto else "manual")
                except Exception:  # noqa: BLE001
                    pass
    return [], "", ""


def _parse_ytdlp_json3(txt: str) -> list:
    try:
        d = json.loads(txt)
    except Exception:  # noqa: BLE001
        return []
    segs = []
    for e in d.get("events", []) or []:
        t = "".join(s.get("utf8", "") for s in (e.get("segs") or []))
        t = t.replace("\n", " ").strip()
        if t:
            segs.append(Segment(start_s=int(e.get("tStartMs", 0)) / 1000.0, text=t))
    return segs


def _parse_vtt(txt: str) -> list:
    segs, cur_start, buf = [], None, []
    for line in txt.splitlines():
        m = re.match(r"(\d\d):(\d\d):(\d\d)[.,](\d+)\s*-->", line)
        if m:
            if cur_start is not None and buf:
                segs.append(Segment(cur_start, " ".join(buf).strip()))
            h, mnt, s, _ = m.groups()
            cur_start = int(h) * 3600 + int(mnt) * 60 + int(s)
            buf = []
        elif line.strip() and "-->" not in line and not line.strip().isdigit() \
                and not line.startswith("WEBVTT"):
            buf.append(re.sub(r"<[^>]+>", "", line).strip())
    if cur_start is not None and buf:
        segs.append(Segment(cur_start, " ".join(buf).strip()))
    # de-dupe the rolling-window repetition auto-captions produce
    out = []
    for s in segs:
        if not out or out[-1].text != s.text:
            out.append(s)
    return out


# ---------------------------------------------------------------- public API
def fetch_video(url_or_id: str, *, want_lang: str | None = None, allow_ytdlp: bool = True) -> VideoData:
    """Fetch metadata + description + the full transcript for a YouTube video. Never raises —
    returns a VideoData whose `.status` explains any failure (no_captions / potoken_gated /
    unavailable / error). Fallback ladder: watch-page scrape → InnerTube ANDROID → yt-dlp."""
    vid = video_id(url_or_id)
    if not vid:
        return VideoData(video_id="", status="error: not a recognizable YouTube URL or id")
    out = VideoData(video_id=vid)
    opener = _opener()

    players = []
    scraped = _scrape_player(vid, opener)
    if scraped:
        players.append(("scrape", scraped))
    key = innertube_key()
    if key:
        try:
            players.append(("innertube", _post_json(_INNERTUBE_ENDPOINT + key,
                            {"context": {"client": _ANDROID_CLIENT}, "videoId": vid}, ua=_ANDROID_UA)))
        except Exception as e:  # noqa: BLE001
            if not players:
                out.status = f"error: {type(e).__name__}: {str(e)[:80]}"
                return out
    elif not players and not allow_ytdlp:
        out.status = "missing_innertube_key"
        return out

    # metadata from the first player that has it
    for _, p in players:
        _meta_from_player(p, out)
        if out.title:
            break

    # playability: surface an honest reason (age/bot/region) rather than "no captions"
    for _, p in players:
        st = (p.get("playabilityStatus", {}) or {}).get("status", "")
        if st and st != "OK":
            reason = (p.get("playabilityStatus", {}).get("reason", "") or st)
            out.status = f"unavailable: {reason[:100]}"
            # keep going — captions might still be reachable — but remember the reason

    # Gather candidate tracks from EVERY player. Different players mint different baseUrls for the
    # same track — some carry &exp=xpe (PoToken-walled, empty body), others don't. So try the
    # selected track from each player and keep the first that actually returns text.
    any_tracks = False
    chosen = None
    for _, p in players:
        tracks = _tracks_from_player(p)
        if not tracks:
            continue
        any_tracks = True
        t = _select_track(tracks, want_lang)
        if not t:
            continue
        segs = _fetch_track(t.get("baseUrl", ""))
        if segs:
            chosen = (t, segs)
            break
    if chosen:
        t, segs = chosen
        out.segments = segs
        out.lang = t.get("languageCode", "")
        out.kind = "asr" if t.get("kind", "") == "asr" else "manual"
        out.source = "innertube/scrape"
        out.ok = True
        out.status = "ok"
        return out
    if not any_tracks:
        if not out.status:
            out.status = "missing_innertube_key" if not key else "no_captions"
        return out

    # stdlib chain gave an empty body (PoToken/exp=xpe) — try yt-dlp if allowed + present
    if allow_ytdlp:
        segs, lang, kind = _ytdlp_transcript(vid, want_lang)
        if segs:
            out.segments = segs
            out.lang = lang or (want_lang or "")
            out.kind = kind or "manual"
            out.source = "yt-dlp"
            out.ok = True
            out.status = "ok"
            return out

    # captions exist but couldn't be fetched → almost always the PoToken wall
    out.status = "potoken_gated"
    return out
