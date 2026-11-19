#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["yt-dlp[default]", "faster-whisper", "pillow", "numpy", "imageio-ffmpeg"]
# ///
"""Ortha Clipper: stream -> chat-spike moments -> captioned 9:16 shorts.

The script does the deterministic work (download, scan, transcribe, render).
Claude Code is the editor: it picks moments, trims, writes hooks, checks frames.

  scan   SRC                 find moments where chat/audio spike above the channel's own baseline
  grab   SRC START END       download only that section, transcribe, export a gridded frame
  prep   DIR                 transcribe + frame for a DIR that already has seg.mp4 (live moments)
  render DIR --in --out ...  cut, reframe to 9:16, burn word-by-word captions, hook, credit
  live   SRC                 watch a live stream; save a moment every time chat spikes
  find   [GAME]              who is live right now (YouTube + Twitch), most viewers first
  dash                       serve the dashboard (auto-started by every other command)
"""
import argparse, json, mimetypes, os, re, shutil, socket, subprocess, sys, threading, time, urllib.request, webbrowser
from collections import Counter
from pathlib import Path
from urllib.parse import quote_plus, unquote, urlencode, urlparse

import numpy as np

FF = shutil.which("ffmpeg") or __import__("imageio_ffmpeg").get_ffmpeg_exe()
ROOT = Path(os.environ.get("ORTHA_DIR", "clips")).resolve()
PORT = int(os.environ.get("ORTHA_PORT", 8790))
W, H, FPS = 1080, 1920, 30
BIN = 5  # seconds per chat/audio bucket
HYPE = re.compile(r"(?i)\b(clip|lma+o+|lo+l+|kekw|omegalul|lul|pog\w*|no way|wtf|holy|ayo+|bruh+|nah+|w+|l+)\b|[?!]{2,}")
YEL = (255, 225, 77, 255)
FONT_URL = "https://github.com/google/fonts/raw/main/ofl/anton/Anton-Regular.ttf"


# ---------- helpers ----------
def log(msg, stage="info", src=""):
    """stdout for Claude + events.jsonl for the dashboard."""
    print(msg, flush=True)
    ROOT.mkdir(parents=True, exist_ok=True)
    with open(ROOT / "events.jsonl", "a") as f:
        f.write(json.dumps({"t": time.time(), "cmd": CMD, "stage": stage, "src": str(src), "msg": msg.strip()[:300]}) + "\n")

CMD = ""
CONFIG = Path(os.environ.get("ORTHA_CONFIG", Path.home() / ".config/ortha-clipper/settings.json"))

def settings(update=None):
    """Global settings shared by every project, e.g. {"clipper": "@myclips"}."""
    s = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
    if update:
        s.update({k: v for k, v in update.items() if k in ("clipper", "profiles", "cookies", "twitch_id", "twitch_secret")})
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        CONFIG.write_text(json.dumps(s, indent=1))
    return s

def config(a):
    if a.key:
        settings({a.key: a.value or ""})
    print(json.dumps({k: "***" if k == "twitch_secret" else v for k, v in settings().items()}, indent=1))

def sh(cmd, **kw):
    return subprocess.run([str(c) for c in cmd], check=True, **kw)

def ff(*args):
    sh([FF, "-y", "-hide_banner", "-loglevel", "error", *args])

def ytdlp(*args):
    b = settings().get("cookies")  # YouTube "confirm you're not a bot": borrow the user's logged-in browser session
    return [sys.executable, "-m", "yt_dlp", "--quiet", "--no-warnings", "--ffmpeg-location", FF,
            *(["--cookies-from-browser", b] if b else []), *args]

def is_local(src):
    return Path(src).exists()

def secs(t):
    """'1:02:03' | '62:03' | '3723.5' -> float seconds"""
    return sum(float(p) * 60 ** i for i, p in enumerate(reversed(str(t).split(":"))))

def hms(s):
    s = int(s)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}"

def slug(src):
    if is_local(src):
        return Path(src).stem
    if m := re.search(r"(?:v=|youtu\.be/|/live/|/shorts/)([\w-]{11})", src):
        return f"youtube-{m[1]}"
    if m := re.search(r"(twitch|kick)\.(?:tv|com)/(?:videos/)?([\w-]+)", src):
        return f"{m[1]}-{m[2]}"
    return re.sub(r"\W+", "-", src.split("//")[-1]).strip("-")[-60:]

# Game detection (idea from PeakCut's GAMING.md): read the game from title/channel so hooks and hashtags fit it.
# Order matters: GTA VI before GTA V. The game is a hint for copy, never a filter.
GAMES = [
    ("GTA VI", r"\bgta\s*(vi|6)\b|grand theft auto vi\b|\bvice city\b|\bgta6\b", "#GTA6 #GTAVI #GTA6Game #ViceCity #RockstarGames"),
    ("GTA V", r"\bgta\s*(v|5)\b|\bgta ?online\b|\bgta ?rp\b|nopixel|grand theft auto v\b", "#GTA5 #GTAOnline #GTARP"),
    ("Fortnite", r"fortnite", "#Fortnite #FortniteClips"),
    ("Minecraft", r"minecraft", "#Minecraft #MinecraftClips"),
    ("Valorant", r"valorant", "#Valorant #ValorantClips"),
    ("Counter-Strike 2", r"\bcs2\b|counter[- ]?strike", "#CS2 #CounterStrike"),
    ("Call of Duty", r"warzone|call of duty|\bcod\b|black ops", "#Warzone #CallOfDuty"),
    ("League of Legends", r"league of legends|\blol\b ranked|\bleague\b", "#LeagueOfLegends #LoL"),
    ("Apex Legends", r"apex legends|\bapex\b", "#ApexLegends"),
    ("Marvel Rivals", r"marvel rivals", "#MarvelRivals"),
    ("Elden Ring", r"elden ring|nightreign", "#EldenRing"),
    ("Rocket League", r"rocket league", "#RocketLeague"),
    ("Rainbow Six Siege", r"\br6\b|rainbow six|siege", "#R6 #RainbowSixSiege"),
    ("Roblox", r"roblox", "#Roblox"),
]

def detect_game(text):
    for name, pat, tags in GAMES:
        if re.search(pat, text or "", re.I):
            return {"game": name, "tags": tags}
    return {"game": "", "tags": ""}

def source_info(d):
    """info.json of the source a clip dir belongs to (works for m<start>/ and live/<time>/)."""
    for p in [Path(d), *Path(d).parents]:
        if (p / "info.json").exists():
            return json.loads((p / "info.json").read_text())
        if p == ROOT:
            break
    return {}

def profile_key(info):
    return (info.get("channel") or info.get("src") or "").strip().lower()

def workdir(src):
    d = ROOT / slug(src)
    d.mkdir(parents=True, exist_ok=True)
    if not (d / "info.json").exists():
        info = {"src": src, "title": Path(src).name, "channel": ""}
        if not is_local(src):
            r = subprocess.run(ytdlp("--skip-download", "--print", "%(title)s\n%(uploader)s", src), capture_output=True, text=True)
            t = r.stdout.strip().splitlines()
            if len(t) >= 2:
                info.update(title=t[0], channel=t[1])
        pinned = settings().get("profiles", {}).get(profile_key(info), {}).get("game")
        info.update(detect_game(pinned or f"{info['title']} {info['channel']}"))
        (d / "info.json").write_text(json.dumps(info))
    return d

def describe(d):
    """One line Claude needs before editing: what, who, which game, saved framing."""
    info = source_info(d)
    prof = settings().get("profiles", {}).get(profile_key(info))
    out = f"source: {info.get('title', '?')} | channel: {info.get('channel') or '?'}"
    if info.get("game"):
        out += f" | game: {info['game']} ({info['tags']})"
    if prof:
        flags = " ".join(f"--{k} {v}" for k, v in prof.items() if k in ("cam", "main", "split", "layout", "box") and v)
        out += f"\nsaved framing for this channel (reuse if the facecam hasn't moved): {flags}"
    return out

def baseline_z(x, window=120, warmup=12):
    """z-score of each bin vs this channel's own trailing baseline (median + MAD of the last `window` bins).
    Relative per channel: 200 msg/min is a spike for a small streamer and noise for a big one."""
    x = np.asarray(x, float)
    z = np.zeros(len(x))
    for i in range(warmup, len(x)):
        past = x[max(0, i - window):i]
        med = np.median(past)
        z[i] = (x[i] - med) / (1.4826 * np.median(np.abs(past - med)) + 1)
    return z

def font(size, cache={}):
    if size not in cache:
        p = Path.home() / ".cache/ortha-clipper/Anton-Regular.ttf"
        if not p.exists():
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
                urllib.request.urlretrieve(FONT_URL, p)
            except OSError:
                pass
        from PIL import ImageFont
        cache[size] = ImageFont.truetype(str(p), size) if p.exists() else ImageFont.load_default(size)
    return cache[size]


# ---------- chat ----------
def yt_chat_line(line):
    """One line of yt-dlp's live_chat json -> [(offset_seconds, text, author)]."""
    try:
        j = json.loads(line)
    except ValueError:
        return []
    act = j.get("replayChatItemAction", j)
    off = int(act.get("videoOffsetTimeMsec", 0)) / 1000
    out = []
    for a in act.get("actions", []):
        r = a.get("addChatItemAction", {}).get("item", {}).get("liveChatTextMessageRenderer")
        if r:
            out.append((off, "".join(x.get("text") or (x.get("emoji", {}).get("shortcuts") or [""])[0]
                                      for x in r.get("message", {}).get("runs", [])),
                        r.get("authorName", {}).get("simpleText", "")))
    return out

def read_chat_file(p):
    """TwitchDownloader JSON | yt-dlp .live_chat.json | jsonl of [seconds, text(, user)] -> [[t, text, user]]."""
    text = Path(p).read_text()
    if text.lstrip().startswith("{\"comments\"") or "\"content_offset_seconds\"" in text[:5000]:
        return [[c["content_offset_seconds"], c.get("message", {}).get("body", ""),
                 (c.get("commenter") or {}).get("name", "")] for c in json.loads(text)["comments"]]
    if "replayChatItemAction" in text[:5000] or "addChatItemAction" in text[:5000]:
        return [list(m) for l in text.splitlines() for m in yt_chat_line(l)]
    return [(r + [""])[:3] for r in (json.loads(l) for l in text.splitlines() if l.strip())]

def load_chat(src, d, chat_file=None):
    if chat_file:
        return read_chat_file(chat_file)
    if "youtu" in src:
        out = d / "chat"
        if not Path(f"{out}.live_chat.json").exists():
            log("downloading YouTube chat replay", "progress", src)
            r = subprocess.run([str(c) for c in ytdlp("--skip-download", "--write-subs", "--sub-langs", "live_chat",
                                                      "-o", out, src)], capture_output=True, text=True)
            if r.returncode:  # chat is a bonus signal: fall back to audio instead of killing the scan
                why = (r.stderr.strip().splitlines() or ["unknown error"])[-1]
                log(f"no chat replay ({why[:160]}), so scoring on audio + transcript", "progress", src)
        f = Path(f"{out}.live_chat.json")
        return read_chat_file(f) if f.exists() else []
    log("no public chat-replay API for this site, so scoring on audio only. For chat scoring, export the chat "
        "(e.g. TwitchDownloaderCLI chatdownload --id <vod> -o chat.json) and pass --chat chat.json", "progress", src)
    return []

def live_chat(src, push, d=None, procs=None):
    """push(text, user) for each live message. Twitch: anonymous IRC. YouTube: yt-dlp writes the live chat to a
    file as it arrives (it can't stream it to stdout), so tail that file. Child processes go in `procs` so the
    caller can stop them."""
    if "twitch.tv" in src:
        ch = urlparse(src).path.strip("/").split("/")[0].lower()
        s = socket.create_connection(("irc.chat.twitch.tv", 6667))
        s.sendall(f"PASS SCHMOOPIIE\r\nNICK justinfan{int(time.time()) % 90000 + 10000}\r\nJOIN #{ch}\r\n".encode())
        buf = b""
        while data := s.recv(65536):
            *lines, buf = (buf + data).split(b"\r\n")
            for l in lines:
                if l.startswith(b"PING"):
                    s.sendall(b"PONG :tmi.twitch.tv\r\n")
                elif b" PRIVMSG " in l:
                    push(l.split(b" PRIVMSG ", 1)[1].split(b" :", 1)[-1].decode(errors="replace"),
                         l[1:].split(b"!", 1)[0].decode(errors="replace"))
    elif "youtu" in src:
        out = Path(d or ROOT) / "livechat"
        for old in Path(out.parent).glob("livechat.live_chat.json*"):
            old.unlink(missing_ok=True)
        p = subprocess.Popen(ytdlp("--skip-download", "--write-subs", "--sub-langs", "live_chat", "-o", out, src),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if procs is not None:
            procs.append(p)
        f, pos, rest = Path(f"{out}.live_chat.json.part"), 0, ""
        while p.poll() is None:
            time.sleep(1)
            if not f.exists():
                continue
            with open(f, errors="replace") as fh:
                fh.seek(pos)
                chunk = fh.read()
                pos = fh.tell()
            *lines, rest = (rest + chunk).split("\n")
            for line in lines:
                for _, msg, user in yt_chat_line(line):
                    push(msg, user)
    else:
        log("live chat is supported for Twitch and YouTube only", "error", src)


# ---------- scan ----------

def audio_path(src, d):
    """Remote: the smallest audio-only track (~20 MB/h; yt-dlp's chunked download dodges the throttling
    a direct ffmpeg read gets), never the video."""
    if is_local(src):
        return Path(src)
    path = next(d.glob("audio.*"), None)
    if not path:
        log("downloading audio track (smallest available)", "progress", src)
        # "lang" first: YouTube now ships AI-dubbed audio tracks, and plain "smallest audio" picks a dub
        sh(ytdlp("-f", "ba/audio_only/b", "-S", "lang,+abr", "-N", "4", "-o", d / "audio.%(ext)s", src))
        path = next(d.glob("audio.*"))
    return path

WHISPER = {}

def whisper(model):
    if model not in WHISPER:
        from faster_whisper import WhisperModel
        WHISPER[model] = WhisperModel(model, device="auto", compute_type="auto", cpu_threads=os.cpu_count() or 4)
    return WHISPER[model]

# what streamers say when something just happened (clippers scan for these by ear)
REACT = re.compile(r"(?i)\b(oh my (god|gosh)|no way|what the|let'?s go+|holy (shit|crap|cow)?|are you (kidding|serious)|"
                   r"i can'?t believe|wait,? wait|no,? no,? no|yes,? yes|clip (it|that)|did you see|ha(ha)+|insane|"
                   r"crazy|bro+|dude|fuck|shit|what\?!|chat,? (look|did))\b")

def stream_lang(path, d, model):
    """Spoken language, from 90 s a quarter of the way in. Intros are often music, which fools Whisper's
    first-30-s guess (a CohhCarnage panel came out in Arabic). Saved in info.json so clips reuse it."""
    info = json.loads((d / "info.json").read_text()) if (d / "info.json").exists() else {}
    if not info.get("lang"):
        raw = subprocess.run([FF, "-v", "error", "-ss", str(duration(path) * 0.25), "-t", "90", "-i", str(path),
                              "-ac", "1", "-ar", "16000", "-f", "f32le", "-"], capture_output=True).stdout
        _, det = whisper(model).transcribe(np.frombuffer(raw, np.float32), vad_filter=True)
        info["lang"] = det.language
        (d / "info.json").write_text(json.dumps(info))
    return info["lang"]

def full_transcript(path, d, src, model):
    """Whole-stream transcript, local and free, so moments can be found from what was said, not only chat.
    Resumable: if it gets interrupted, the next scan continues from the last finished line."""
    out = d / f"transcript_full.{model}.jsonl"
    if not out.exists():
        lang = stream_lang(path, d, model)
        tmp = out.with_suffix(".part")
        start = json.loads(tmp.read_text().splitlines()[-1])[1] if tmp.exists() and tmp.read_text().strip() else 0
        log(f"transcribing the whole stream ({model}, {lang}{', resuming at ' + hms(start) if start else ''}) "
            f"to find stories and takes chat missed", "progress", src)
        segs, _ = whisper(model).transcribe(str(path), language=lang, vad_filter=True, beam_size=1,
                                            condition_on_previous_text=False, clip_timestamps=[start] if start else "0")
        last = start
        with open(tmp, "a") as f:
            for sg in segs:
                f.write(json.dumps([round(sg.start, 1), round(sg.end, 1), sg.text.strip()]) + "\n")
                f.flush()
                if sg.start - last >= 600:
                    last = sg.start
                    log(f"transcript: {hms(sg.start)} done", "progress", src)
        tmp.rename(out)
    return [json.loads(l) for l in open(out)]

def comment_marks(src, d):
    """Timestamps viewers wrote in YouTube comments ("2:13:40 LMAO"): the moments people remember."""
    if "youtu" not in src:
        return []
    f = d / "comments.info.json"
    if not f.exists():
        log("reading timestamps from YouTube comments", "progress", src)
        subprocess.run(ytdlp("--skip-download", "--write-info-json", "--write-comments", "--no-write-playlist-metafiles",
                             "--extractor-args", "youtube:max_comments=400,all,0;comment_sort=top", "-o", d / "comments", src),
                       capture_output=True)
    if not f.exists():
        return []
    marks = []
    for c in json.loads(f.read_text()).get("comments") or []:
        for h, m, sec in re.findall(r"(?<![\d:])(?:(\d{1,2}):)?(\d{1,2}):(\d{2})(?![\d:])", c.get("text", "")):
            marks.append((int(h or 0) * 3600 + int(m) * 60 + int(sec), 1 + min(c.get("like_count") or 0, 50) / 10, c["text"][:120]))
    return marks

def loudness(path, src):
    p = subprocess.Popen([FF, "-v", "error", "-i", path, "-vn", "-ac", "1", "-ar", "8000", "-f", "s16le", "-"], stdout=subprocess.PIPE)
    out, n = [], 8000 * BIN * 2
    while chunk := p.stdout.read(n):
        a = np.frombuffer(chunk[: len(chunk) // 2 * 2], np.int16).astype(np.float32)
        out.append(20 * np.log10(np.sqrt((a * a).mean()) + 1))
        if len(out) % 120 == 0:
            log(f"audio: {hms(len(out) * BIN)} analysed", "progress", src)
    return np.array(out)

def is_live(src):
    """True when the link is a stream that's live right now (a Twitch channel page, or a live YouTube video)."""
    if is_local(src):
        return False
    r = subprocess.run(ytdlp("--skip-download", "--print", "%(is_live)s", src), capture_output=True, text=True)
    return r.stdout.strip().splitlines()[:1] == ["True"]

def twitch_live(game, n, s):
    """Official Helix API with an app token (free app at dev.twitch.tv). Twitch returns streams by viewers, highest first."""
    body = urlencode({"client_id": s["twitch_id"], "client_secret": s["twitch_secret"], "grant_type": "client_credentials"}).encode()
    tok = json.load(urllib.request.urlopen("https://id.twitch.tv/oauth2/token", body, timeout=15))["access_token"]
    def get(path, **q):
        req = urllib.request.Request(f"https://api.twitch.tv/helix/{path}?{urlencode(q)}",
                                     headers={"Client-Id": s["twitch_id"], "Authorization": f"Bearer {tok}"})
        return json.load(urllib.request.urlopen(req, timeout=15))["data"]
    g = get("games", name=game) or get("search/categories", query=game, first=1)
    return [("Twitch", x["viewer_count"], x["user_name"], x["title"], f"https://twitch.tv/{x['user_login']}")
            for x in (get("streams", game_id=g[0]["id"], first=min(n, 100)) if g else [])]

def find(a):
    """Who is streaming a game right now, most viewers first, so the user knows what to clip."""
    rows = []
    try:
        r = sh(ytdlp("-J", "--flat-playlist", "--playlist-end", str(a.top),
                     f"https://www.youtube.com/results?search_query={quote_plus(a.game)}&sp=EgJAAQ%253D%253D"),  # filter: live now
               capture_output=True, text=True)
        rows += [("YouTube", e.get("concurrent_view_count") or 0, e.get("channel") or "", e.get("title") or "", e.get("url"))
                 for e in json.loads(r.stdout).get("entries") or [] if e and e.get("live_status") == "is_live"]
    except subprocess.CalledProcessError:
        print("YouTube search failed. If it's the bot check: config cookies <browser>")
    s = settings()
    if s.get("twitch_id") and s.get("twitch_secret"):
        try:
            rows += twitch_live(a.game, a.top, s)
        except Exception as e:
            print(f"Twitch failed: {e}")
    else:
        print("Twitch skipped: create a free app at dev.twitch.tv/console, then config twitch_id <id> and config twitch_secret <secret>")
    rows.sort(key=lambda r: -r[1])
    for plat, viewers, ch, title, url in rows[:a.top]:
        print(f"{plat:8} {viewers:>7,}  {ch[:22]:22} {title[:58]:58} {url}")
    if not rows:
        print("nobody live found")

def scan(a):
    if is_live(a.src):  # one action for the user: a live link gets watched live instead of scanned
        log(f"{a.src} is live right now, so watching it live instead of scanning", "progress", a.src)
        return live(argparse.Namespace(src=a.src, threshold=3.5, pre=40, post=15))
    d = workdir(a.src)
    log(f"scan started: {a.src}", "start", a.src)
    msgs = load_chat(a.src, d, a.chat) if (a.chat or not is_local(a.src)) else []
    log(f"chat: {len(msgs)} messages", "progress", a.src)
    if msgs:
        (d / "chat_norm.jsonl").write_text("".join(json.dumps(m) + "\n" for m in msgs))
    path = audio_path(a.src, d)
    audio = np.array([]) if a.no_audio else loudness(path, a.src)
    segs = [] if a.no_transcript else full_transcript(path, d, a.src, a.transcript_model)
    marks = comment_marks(a.src, d)
    n = max(len(audio), int(max((m[0] for m in msgs), default=0) // BIN) + 1, int(max((g[1] for g in segs), default=0) // BIN) + 1)
    count, hype = np.zeros(n), np.zeros(n)
    by_bin, users = [[] for _ in range(n)], [set() for _ in range(n)]
    for t, m, u in msgs:
        i = int(t // BIN)
        if 0 <= i < n:
            count[i] += 1
            hype[i] += bool(HYPE.search(m))
            by_bin[i].append(m)
            users[i].add(u)
    # spikes are measured in distinct people, not messages (idea from PeakCut): one spammer or an
    # emote wave from a few accounts isn't a crowd reacting
    named = any(u for t, m, u in msgs)
    people = np.array([len(s) for s in users], float) if named else count
    cz = baseline_z(people)
    cz = np.maximum(cz, np.convolve(cz, [0.5, 0.5], "same"))  # reward sustained spikes
    az = baseline_z(np.pad(audio, (0, n - len(audio)))) if len(audio) else np.zeros(n)
    # chat reacts 5-20s after the event, so look back for the audio peak
    az_back = np.array([az[max(0, i - 4):i + 1].max() for i in range(n)])
    hype_ratio = np.divide(hype, count, out=np.zeros(n), where=count > 0)
    # what was said: speech rate, reaction words, and the silence-then-outburst shape clippers listen for
    words, react, said = np.zeros(n), np.zeros(n), [[] for _ in range(n)]
    for s0, s1, text in segs:
        b0, b1 = int(s0 // BIN), max(int(s0 // BIN), int(s1 // BIN))
        for i in range(b0, min(n, b1 + 1)):
            words[i] += len(text.split()) / (b1 - b0 + 1)
        if b0 < n:
            said[b0].append(text)
            react[b0] += len(REACT.findall(text))
    sz = baseline_z(words) if segs else np.zeros(n)
    au = np.pad(audio, (0, n - len(audio))) if len(audio) else np.zeros(n)
    jump = np.array([au[i] - au[max(0, i - 3):i].min() if i else 0 for i in range(n)])
    jz = baseline_z(jump) if len(audio) else np.zeros(n)
    cm, cm_text = np.zeros(n), [[] for _ in range(n)]
    for t, w, text in marks:  # comment timestamps are approximate: spread over +-10 s
        for i in range(max(0, int(t // BIN) - 2), min(n, int(t // BIN) + 3)):
            cm[i] += w
            cm_text[i].append(text)
    react_back = np.array([react[max(0, i - 6):i + 1].sum() for i in range(n)])
    # balanced blend, each signal capped so no single one (a giant chat wave, one viral comment) decides alone
    score = ((np.minimum(cz, 6) + 0.6 * np.minimum(az_back, 6) + 2 * hype_ratio) if msgs else np.minimum(az, 6)) \
        + 0.35 * np.clip(sz, 0, None) + 0.8 * np.minimum(react_back, 3) + 0.4 * np.clip(jz, 0, 6) + 1.5 * np.minimum(cm, 4)

    picked = []
    for i in np.argsort(-score):
        if score[i] < a.threshold or len(picked) >= a.top:
            break
        if all(abs(i - j) * BIN >= 60 for j in picked):
            picked.append(int(i))

    base = np.median(count) * 60 / BIN
    moments = []
    for i in picked:
        peak = i * BIN
        window = [m for j in range(max(0, i - 7), min(n, i + 3)) for m in by_bin[j]]
        common = Counter(re.sub(r"\s+", " ", m.strip().lower())[:40] for m in window).most_common(6)
        quote = " ".join(t for j in range(max(0, i - 8), min(n, i + 2)) for t in said[j])
        why = [f"chat {people[i]:.0f} people vs {np.median(people):.0f} normal" if msgs and cz[i] >= 2 else "",
               "loud" if az_back[i] >= 2 else "", "silence then outburst" if jz[i] >= 2 else "",
               "talking fast" if sz[i] >= 2 else "",
               "said: " + ", ".join(sorted({m[0].lower() for j in range(max(0, i - 6), i + 1) for m in REACT.findall(" ".join(said[j]))}))
               if react_back[i] else "",
               f"{len(set(cm_text[i]))} YouTube comments point here" if cm[i] else ""]
        moments.append({
            "start": max(0, peak - a.pre), "end": peak + a.post, "peak": peak,
            "score": round(float(score[i]), 1), "chat_z": round(float(cz[i]), 1), "audio_z": round(float(az_back[i]), 1),
            "msgs_per_min": round(count[i] * 60 / BIN), "baseline_per_min": round(base),
            "people": int(people[i]), "people_baseline": round(float(np.median(people)), 1),
            "top_chat": [f"{m} x{c}" if c > 1 else m for m, c in common], "sample": window[-15:],
            "why": [w for w in why if w], "said": quote[-280:], "comments": list(dict.fromkeys(cm_text[i]))[:3],
        })
    (d / "moments.json").write_text(json.dumps(moments, indent=1, ensure_ascii=False))
    trail = [float(np.median(count[max(0, i - 120):i])) if i else 0 for i in range(n)]
    (d / "timeline.json").write_text(json.dumps({
        "src": a.src, "bin": BIN, "per_min": (count * 60 / BIN).round().tolist(),
        "baseline": (np.array(trail) * 60 / BIN).round().tolist(), "score": np.round(score, 1).tolist(),
        "audio": np.round(au - (np.median(au) if len(audio) else 0), 1).tolist(), "words": np.round(words, 1).tolist(),
        "comments": np.round(cm, 1).tolist()}))
    if segs:  # skim: one line per 30 s so Claude can find stories, takes and setups the scores missed
        lines, mark = {}, {int(m["peak"] // 30): k for k, m in enumerate(moments, 1)}
        for s0, _, text in segs:
            lines.setdefault(int(s0 // 30), []).append(text)
        (d / "skim.txt").write_text("".join(
            f"[{hms(b * 30)}]{' *#' + str(mark[b]) if b in mark else ''} {' '.join(' '.join(t).split()[:32])}\n"
            for b, t in sorted(lines.items())))
    log(f"{len(moments)} moments -> {d / 'moments.json'}  (channel baseline ~{round(base)} msg/min)", "done", a.src)
    print(describe(d) + "\n")
    for k, m in enumerate(moments, 1):
        print(f"#{k:<2} {hms(m['start'])}-{hms(m['end'])}  score {m['score']:>5}  why: {'; '.join(m['why']) or 'combined signals'}")
        if m["said"]:
            print(f"     said: \"...{m['said'][-160:]}\"")
        if m["top_chat"]:
            print(f"     chat: {' | '.join(m['top_chat'][:4])}")
        for c in m["comments"][:1]:
            print(f"     comment: {c}")
    if segs:
        print(f"\nskim: {d / 'skim.txt'} ({len((d / 'skim.txt').read_text()) // 4:,} tokens). Read it for moments the scores miss.")


# ---------- grab / prep ----------
def grab(a):
    """One section (START END) or several picked moments at once (--moments 1,3,5): one Whisper load for all."""
    if a.moments:
        ms = json.loads((workdir(a.src) / "moments.json").read_text())
        ranges = [(ms[int(k) - 1]["start"], ms[int(k) - 1]["end"]) for k in a.moments.split(",")]
    elif a.start and a.end:
        ranges = [(secs(a.start), secs(a.end))]
    else:
        raise SystemExit("grab needs START END, or --moments 1,3,5 after a scan")
    for s, e in ranges:
        grab_one(a, s, e)

def grab_one(a, s, e):
    d = workdir(a.src) / f"m{int(s)}"
    d.mkdir(exist_ok=True)
    seg = d / "seg.mp4"
    if not seg.exists():
        log(f"downloading {hms(s)}-{hms(e)}", "start", d)
        if is_local(a.src):
            ff("-ss", s, "-i", a.src, "-t", e - s, "-c", "copy", seg)
        else:
            sh(ytdlp("-f", "bv*[height<=1080]+ba/b[height<=1080]/b", "-S", "lang", "--download-sections", f"*{s}-{e}",
                     "--merge-output-format", "mp4", "-o", seg, a.src))
    (d / "meta.json").write_text(json.dumps({"src": a.src, "start": s, "end": e}))
    write_context(d.parent, d, s, e)
    a.dir = d
    prep(a)

def write_context(src_dir, d, s, e):
    """Everything known about this moment before cutting it: why it scored, what was said around it, the chat in
    that window, and YouTube comments pointing at it. The editor reads context.md first."""
    ms = json.loads((src_dir / "moments.json").read_text()) if (src_dir / "moments.json").exists() else []
    m = next((x for x in ms if x["start"] <= (s + e) / 2 <= x["end"]), {})
    tr = next(iter(sorted(src_dir.glob("transcript_full.*.jsonl"))), None)
    said = [x for x in (json.loads(l) for l in open(tr)) if s - 30 <= x[0] <= e + 10] if tr else []
    chat = [x for x in (json.loads(l) for l in open(src_dir / "chat_norm.jsonl")) if s <= x[0] <= e + 15] \
        if (src_dir / "chat_norm.jsonl").exists() else []
    comments = [c for c in (comment_marks(source_info(d).get("src", ""), src_dir) if (src_dir / "comments.info.json").exists() else [])
                if s - 20 <= c[0] <= e + 20]
    ctx = {"window": [s, e], "why": m.get("why", []), "score": m.get("score"),
           "said": [[round(x[0] - s, 1), x[2]] for x in said],
           "chat": [[round(x[0] - s, 1), x[2], x[1]] for x in chat[:200]], "people": len({x[2] for x in chat}),
           "comments": [[hms(c[0]), round(c[1], 1), c[2]] for c in sorted(comments, key=lambda c: -c[1])[:8]]}
    (d / "context.json").write_text(json.dumps(ctx, ensure_ascii=False))
    lines = [f"# Moment {hms(s)}-{hms(e)} (times below are seconds into seg.mp4; negative = before it)",
             f"why it scored: {'; '.join(ctx['why']) or 'manual pick'}", "",
             "## What was said (whole-stream transcript)", *[f"[{t:6.1f}] {x}" for t, x in ctx["said"]], "",
             f"## Chat in this window: {len(chat)} messages from {ctx['people']} people",
             *[f"[{t:6.1f}] {u}: {x}" for t, u, x in ctx["chat"][:40]], "",
             "## YouTube comments pointing here", *[f"- {t} ({w} weight) {x}" for t, w, x in ctx["comments"]]]
    (d / "context.md").write_text("\n".join(lines) + "\n")

def duration(path):
    err = subprocess.run([FF, "-hide_banner", "-i", str(path)], capture_output=True, text=True).stderr
    h, m, s = re.search(r"Duration: (\d+):(\d+):([\d.]+)", err).groups()
    return int(h) * 3600 + int(m) * 60 + float(s)

def sheet(seg, times, out, width=480):
    """Frames at `times` tiled 4-wide with timestamps and a 0.1 grid: one cheap image showing what's on screen when."""
    from PIL import Image, ImageDraw
    tiles = []
    for t in times:
        f = out.with_name(f".f{t:.1f}.jpg")
        ff("-ss", t, "-i", seg, "-frames:v", "1", "-vf", f"scale={width}:-2", f)
        im = Image.open(f).convert("RGB")
        f.unlink()
        dr = ImageDraw.Draw(im)
        for k in range(1, 10):
            x, y = im.width * k // 10, im.height * k // 10
            dr.line([(x, 0), (x, im.height)], fill=(255, 0, 255), width=1)
            dr.line([(0, y), (im.width, y)], fill=(255, 0, 255), width=1)
        dr.rectangle([0, 0, 92, 30], fill="black")
        dr.text((6, 4), f"{t:.1f}s", fill="white", font=font(22))
        tiles.append(im)
    cols = min(4, len(tiles))
    rows = -(-len(tiles) // cols)
    w, h = tiles[0].size
    grid = Image.new("RGB", (cols * w + (cols - 1) * 6, rows * h + (rows - 1) * 6), "white")
    for i, im in enumerate(tiles):
        grid.paste(im, ((i % cols) * (w + 6), (i // cols) * (h + 6)))
    grid.save(out, quality=82)
    return out

def frames(a):
    d = Path(a.dir).resolve()
    out = sheet(d / "seg.mp4", [secs(t) for t in a.times], d / f"frames_{'-'.join(a.times)}.jpg".replace(":", ""))
    print(out)

def prep(a):
    from PIL import Image, ImageDraw
    d = Path(a.dir).resolve()
    seg = d / "seg.mp4"
    if not (d / "words.json").exists():
        log(f"transcribing ({a.model})", "progress", d)
        segs, _ = whisper(a.model).transcribe(str(seg), word_timestamps=True, vad_filter=True,
                                              language=source_info(d).get("lang") or None)
        words, lines = [], []
        for sg in segs:
            lines.append(f"[{sg.start:6.1f}-{sg.end:6.1f}] {sg.text.strip()}")
            words += [{"w": w.word.strip(), "s": round(w.start, 2), "e": round(w.end, 2)} for w in sg.words]
        (d / "words.json").write_text(json.dumps(words))
        (d / "transcript.txt").write_text("\n".join(lines) + "\n")
    # gridded frame so Claude can read off the facecam box in 0-1 coordinates
    ff("-ss", "3", "-i", seg, "-frames:v", "1", d / "frame.jpg")
    img = Image.open(d / "frame.jpg").convert("RGB")
    dr = ImageDraw.Draw(img)
    for k in range(1, 10):
        x, y = img.width * k // 10, img.height * k // 10
        dr.line([(x, 0), (x, img.height)], fill=(255, 0, 255), width=1)
        dr.line([(0, y), (img.width, y)], fill=(255, 0, 255), width=1)
        dr.text((x + 3, 3), f".{k}", fill=(255, 0, 255), font=font(max(14, img.height // 40)))
        dr.text((3, y + 3), f".{k}", fill=(255, 0, 255), font=font(max(14, img.height // 40)))
    img.save(d / "frame_grid.jpg", quality=85)
    dur = duration(seg)
    step = max(2.0, dur / 12)
    sheet(seg, [round(step * i + 0.5, 1) for i in range(int((dur - 0.5) // step) + 1)][:16], d / "sheet.jpg")
    log(f"ready to edit: {d}", "done", d)
    print(f"  seg.mp4 ({dur:.1f}s)  transcript.txt  words.json  frame_grid.jpg ({img.width}x{img.height})  sheet.jpg (every {step:.1f}s)")
    print("  " + describe(d).replace("\n", "\n  "))


# ---------- render ----------
def pages(ws, maxw):
    out, cur = [], []
    for w in ws:
        if cur and (len(cur) >= maxw or w["s"] - cur[-1]["e"] > 0.5 or re.search(r"[.!?]$", cur[-1]["w"])):
            out.append(cur)
            cur = []
        cur.append(w)
    return out + ([cur] if cur else [])

def caption_png(words, active, path, cap_h):
    from PIL import Image, ImageDraw
    txt = [re.sub(r"^[^\w']+|[^\w'?!]+$", "", w["w"]).upper() for w in words]
    img = Image.new("RGBA", (W, cap_h), (0, 0, 0, 0))
    dr = ImageDraw.Draw(img)
    size = 124
    while True:
        f = font(size)
        sp = dr.textlength(" ", font=f)
        ws = [dr.textlength(t, font=f) for t in txt]
        total = sum(ws) + sp * (len(txt) - 1)
        if total <= W - 140 or size <= 60:
            break
        size -= 6
    x = (W - total) / 2
    for i, (t, wd) in enumerate(zip(txt, ws)):
        dr.text((x, cap_h / 2), t, font=f, anchor="lm", fill=YEL if i == active else "white",
                stroke_width=max(6, size // 10), stroke_fill="black")
        x += wd + sp
    img.save(path)

def caption_track(words, t0, t1, tmp, maxw):
    """Word-by-word captions as a PNG sequence + ffconcat timing. Works with any ffmpeg (no libass needed)."""
    from PIL import Image
    cap_h = 300
    blank = tmp / "blank.png"
    Image.new("RGBA", (W, cap_h), (0, 0, 0, 0)).save(blank)
    ws = [w for w in words if w["e"] > t0 and w["s"] < t1 and w["w"].strip()]
    ev = []
    pg = pages(ws, maxw)
    for pi, p in enumerate(pg):
        nxt = pg[pi + 1][0]["s"] if pi + 1 < len(pg) else t1
        for i, w in enumerate(p):
            end = p[i + 1]["s"] if i + 1 < len(p) else min(w["e"] + 0.3, nxt)
            png = tmp / f"c{len(ev):04d}.png"
            caption_png(p, i, png, cap_h)
            ev.append((png, max(w["s"], t0) - t0, min(end, t1) - t0))
    lines, t = ["ffconcat version 1.0"], 0.0
    for png, s, e in ev:
        if s > t + 0.01:
            lines += [f"file '{blank}'", f"duration {s - t:.3f}"]
            t = s
        if e > t:
            lines += [f"file '{png}'", f"duration {e - t:.3f}"]
            t = e
    if t1 - t0 - t > 0.01:
        lines += [f"file '{blank}'", f"duration {t1 - t0 - t:.3f}"]
    lines += [f"file '{blank}'"]  # concat demuxer needs the last file repeated
    (tmp / "caps.txt").write_text("\n".join(lines) + "\n")
    return tmp / "caps.txt", cap_h

def box_png(text, path, size=64):
    """Hook: black text on a white rounded box, wrapped to 2-3 lines."""
    from PIL import Image, ImageDraw
    f = font(size)
    dr = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    lines, cur = [], ""
    for word in text.upper().split():
        if dr.textlength(f"{cur} {word}".strip(), font=f) > W - 220 and cur:
            lines.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}".strip()
    lines.append(cur)
    lh = int(size * 1.15)
    bw = int(max(dr.textlength(l, font=f) for l in lines)) + 64
    bh = lh * len(lines) + 40
    img = Image.new("RGBA", (W, bh), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([((W - bw) // 2, 0), ((W + bw) // 2, bh)], radius=22, fill="white")
    for k, l in enumerate(lines):
        d.text((W / 2, 20 + lh * k + lh / 2), l, font=f, anchor="mm", fill="black")
    img.save(path)

def text_png(text, path, size=40):
    """Small watermark pill: clipper handle (or optional credit)."""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (W, size * 2), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    tw = d.textlength(text, font=font(size))
    d.rounded_rectangle([((W - tw) / 2 - 22, 6), ((W + tw) / 2 + 22, size * 2 - 6)], radius=size, fill=(0, 0, 0, 120))
    d.text((W / 2, size), text, font=font(size), anchor="mm", fill=(255, 255, 255, 235))
    img.save(path)

def cover(w, h, fx=0.5):
    return f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}:(iw-ow)*{fx}:(ih-oh)/2"

def encoder():
    encs = subprocess.run([FF, "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    if "libx264" in encs:
        return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"]
    return ["-c:v", "h264_videotoolbox" if "videotoolbox" in encs else "mpeg4", "-b:v", "10M"]

def render(a):
    d = Path(a.dir).resolve()
    t0, t1 = secs(a.in_), secs(a.out)
    tmp = d / ".render"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()

    split = round(H * a.split / 2) * 2
    box = lambda b: "crop=iw*{2}:ih*{3}:iw*{0}:ih*{1}".format(*(float(v) for v in (b.split(",") if isinstance(b, str) else b)))

    def chain(s, i):
        """One shot's reframe, from [s{i}] to [o{i}]. stack: cam box over main box. full: one box fills 9:16. fit: whole frame on blur."""
        lay = s.get("layout") or ("stack" if s.get("cam") else "fit")
        f = s.get("focus", a.focus)
        if lay == "stack":
            return (f"[s{i}]split[c{i}][m{i}];[c{i}]{box(s['cam'])},{cover(W, split)}[ct{i}];"
                    f"[m{i}]{box(s.get('main', '0,0,1,1'))},{cover(W, H - split, f)}[mb{i}];[ct{i}][mb{i}]vstack[o{i}]"), lay
        if lay == "full":
            return f"[s{i}]{box(s.get('box', '0,0,1,1'))},{cover(W, H, f)}[o{i}]", lay
        return (f"[s{i}]split[b{i}][f{i}];[b{i}]{cover(W, H)},boxblur=30:2[bg{i}];[f{i}]scale={W}:-2[fg{i}];"
                f"[bg{i}][fg{i}]overlay=(W-w)/2:(H-h)/2[o{i}]"), lay

    # shots: cut between framings over time (speaker, reaction, gameplay). Times are seg time, like the transcript.
    if a.shots:
        shots = json.loads(Path(a.shots).read_text() if Path(a.shots).is_file() else a.shots)
    else:
        shots = [{"t": t0, "layout": a.layout, "cam": a.cam, "main": a.main, "box": a.box}]
    shots = sorted(({k: v for k, v in s.items() if v is not None} for s in shots), key=lambda s: s.get("t", 0))
    shots = [s for j, s in enumerate(shots) if j == 0 or t0 < s.get("t", 0) < t1]
    cuts = [0.0] + [s["t"] - t0 for s in shots[1:]] + [t1 - t0]
    parts, layouts = [], []
    for i, s in enumerate(shots):
        c, lay = chain(s, i)
        layouts.append(lay)
        trim = f"trim=start={cuts[i]:.3f}:end={cuts[i + 1]:.3f},setpts=PTS-STARTPTS," if len(shots) > 1 else ""
        parts.append(c.replace(f"[s{i}]", f"[s{i}]{trim}", 1) if trim else c)
    if len(shots) > 1:
        g = (f"[0:v]split={len(shots)}" + "".join(f"[s{i}]" for i in range(len(shots))) + ";" + ";".join(parts) + ";"
             + "".join(f"[o{i}]" for i in range(len(shots))) + f"concat=n={len(shots)}:v=1:a=0[v0]")
    else:
        g = parts[0].replace("[s0]", "[0:v]", 1).replace("[o0]", "[v0]")
    layout = layouts[0] if len(set(layouts)) == 1 else "+".join(dict.fromkeys(layouts))
    cap_y = {"stack": split, "full": int(H * 0.64)}.get(layouts[0], int(H * 0.68))

    cap_y = int(H * a.cap_y) if a.cap_y is not None else cap_y
    inputs, v, k = ["-ss", t0, "-t", t1 - t0, "-i", d / "seg.mp4"], "v0", 1
    words = json.loads((d / "words.json").read_text()) if (d / "words.json").exists() else []
    if words and not a.no_captions:
        caps, cap_h = caption_track(words, t0, t1, tmp, a.words)
        inputs += ["-f", "concat", "-safe", "0", "-i", caps]
        g += f";[{k}:v]fps={FPS},format=rgba[cap];[{v}][cap]overlay=0:{cap_y - cap_h // 2}:eof_action=pass[v{k}]"
        v, k = f"v{k}", k + 1
    # overlays never collide: anything landing on a taken band slides below it
    taken = [(cap_y - 85, cap_y + 85)] if words and not a.no_captions else []
    def place(y, h):
        for top, bot in sorted(taken):
            if y < bot and y + h > top:
                y = bot + 14
        taken.append((y, y + h))
        return int(y)
    if a.hook:
        box_png(a.hook, tmp / "hook.png")
        from PIL import Image
        hy = place(H * a.hook_y, Image.open(tmp / "hook.png").height)
        inputs += ["-i", tmp / "hook.png"]
        on = f":enable='lt(t,{a.hook_secs})'" if a.hook_secs > 0 else ""
        g += f";[{v}][{k}:v]overlay=0:{hy}{on}[v{k}]"
        v, k = f"v{k}", k + 1
    tag = None if a.no_tag else (a.tag or settings().get("clipper"))
    for name, text in (("tag", tag), ("credit", a.credit)):
        if text:
            text_png(text, tmp / f"{name}.png")
            inputs += ["-i", tmp / f"{name}.png"]
            y = place(split + 100 if layouts[0] == "stack" else H * 0.71, 80)
            g += f";[{v}][{k}:v]overlay=0:{y}[v{k}]"
            v, k = f"v{k}", k + 1

    out = d / f"{a.name or 'final'}.mp4"
    log(f"rendering {out.name} ({layout}, {t1 - t0:.1f}s)", "start", d)
    ff(*inputs, "-filter_complex", g + f";[{v}]fps={FPS},format=yuv420p[vout]",
       "-map", "[vout]", "-map", "0:a?", "-af", "loudnorm=I=-14:TP=-1.5:LRA=11", *encoder(),
       "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-movflags", "+faststart", "-shortest", out)
    # QA frames for Claude to look at: just after the start, middle, near the end
    dur = t1 - t0
    ff("-i", out, "-vf", f"select='eq(n\\,{int(FPS * min(1.5, dur / 4))})+eq(n\\,{int(FPS * dur / 2)})+eq(n\\,{int(FPS * dur * 0.85)})',scale=360:-2,tile=3x1",
       "-frames:v", "1", "-update", "1", d / f"{out.stem}_qa.jpg")
    shutil.rmtree(tmp, ignore_errors=True)
    out.with_suffix(".json").write_text(json.dumps({"in": t0, "out": t1, "dur": round(dur, 1), "layout": layout, "hook": a.hook,
                                                    "tag": tag, "credit": a.credit, "shots": shots}))
    if a.remember and len(shots) == 1:  # per-streamer profile: next stream from this channel starts framed
        info = source_info(d)
        if key := profile_key(info):
            profs = settings().get("profiles", {})
            profs[key] = {**profs.get(key, {}), **{k: v for k, v in shots[0].items() if k in ("layout", "cam", "main", "box")},
                          "split": a.split}
            settings({"profiles": profs})
            log(f"saved framing for {info.get('channel') or key}", "progress", d)
    log(f"rendered {out}  ({dur:.1f}s)", "done", d)
    print(f"QA strip: {d / (out.stem + '_qa.jpg')}")


# ---------- live ----------
def recorder(src, buf):
    """ffmpeg reading the live stream straight into 5 s segments. YouTube live only offers separate video and audio
    HLS streams (yt-dlp can't merge those to stdout), so resolve the URLs and hand ffmpeg both."""
    urls = subprocess.run(ytdlp("-g", "-f", "bv*[height<=1080]+ba/b[height<=1080]/b", "-S", "lang", src),
                          capture_output=True, text=True).stdout.split()
    if not urls:
        return None
    inputs = [x for u in urls for x in ("-i", u)]
    maps = ["-map", "0:v:0", "-map", "1:a:0"] if len(urls) > 1 else []
    return subprocess.Popen([FF, "-hide_banner", "-loglevel", "error", *inputs, *maps, "-c", "copy", "-f", "segment",
                             "-segment_time", "5", "-reset_timestamps", "1", "-strftime", "1", str(buf / "%s.ts")])

def live(a):
    d = workdir(a.src) / "live"
    buf = d / "buf"
    buf.mkdir(parents=True, exist_ok=True)
    rec = recorder(a.src, buf)
    if not rec:
        log(f"live: couldn't open {a.src}. Is it live right now?", "error", a.src)
        raise SystemExit(1)
    rec_started, retries = time.time(), 0
    import signal
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGALRM, signal.SIGQUIT):  # any stop runs `finally`,
        signal.signal(sig, lambda *_: sys.exit(0))                             # so the recorder is never orphaned
    chat, chat_procs = [], []  # (wall time, message, user); chat child processes to stop on exit
    threading.Thread(target=live_chat, args=(a.src, lambda m, u: chat.append((time.time(), m, u)), d, chat_procs),
                     daemon=True).start()

    log(f"live: watching {a.src} -> {d}  (baseline warms up for ~1 min)", "start", a.src)
    counts, people, last, pending = [], [], 0.0, []
    try:
        while True:
            time.sleep(BIN)
            now = time.time()
            if rec.poll() is not None:  # URL expired or the network dropped: reconnect, give up after 5 tries in a row
                if now - rec_started > 120:
                    retries = 0
                if retries >= 5:
                    log("live: the stream ended or can't be reached", "done", a.src)
                    break
                retries += 1
                log(f"live: recorder stopped, reconnecting ({retries}/5)", "progress", a.src)
                rec = recorder(a.src, buf) or rec
                rec_started = now
                continue
            recent = [c for c in chat if c[0] > now - BIN]
            counts.append(len(recent))
            people.append(len({u for _, _, u in recent}))  # distinct chatters, so one spammer can't fake a spike
            z = baseline_z(people)[-1]
            (d / "pulse.json").write_text(json.dumps({"t": now, "per_min": [c * 60 // BIN for c in counts[-120:]], "z": round(float(z), 1)}))
            if z >= a.threshold and now - last > 60:
                last = now
                pending.append(now)
                log(f"SPIKE z={z:.1f} {counts[-1] * 60 // BIN} msg/min from {people[-1]} people, saving in {a.post}s", "progress", a.src)
            for p in [p for p in pending if now >= p + a.post]:
                pending.remove(p)
                save_live(d, buf, p, a.pre, chat)
            for f in buf.glob("*.ts"):  # ring buffer
                if f.stat().st_mtime < now - 240:
                    f.unlink(missing_ok=True)
            del chat[: max(0, len(chat) - 20000)]  # bound memory on long streams
    finally:
        for p in (rec, *chat_procs):
            p.terminate()
        for p in (rec, *chat_procs):
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()
        log("live: stopped", "done", a.src)

def save_live(d, buf, peak, pre, chat):
    segs = sorted(f for f in buf.glob("*.ts") if peak - pre - 5 <= int(f.stem) <= time.time())
    if not segs:
        return
    m = d / time.strftime("%Y%m%d-%H%M%S", time.localtime(peak))
    m.mkdir(exist_ok=True)
    (m / "list.txt").write_text("".join(f"file '{f.resolve()}'\n" for f in segs))
    ff("-f", "concat", "-safe", "0", "-i", m / "list.txt", "-c", "copy", m / "seg.mp4")
    lines = [msg for t, msg, _ in chat if peak - 30 <= t <= peak + 10]
    (m / "chat.txt").write_text("\n".join(lines))
    log(f"MOMENT {m}  ({len(segs) * 5}s, chat: {' | '.join(lines[-5:])[:160]})", "done", m)
    if sys.platform == "darwin":
        subprocess.run(["osascript", "-e", f'display notification "Chat spike saved" with title "Ortha Clipper"'])


# ---------- dashboard ----------
def rd(p):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None

def state():
    rel = lambda p: "/" + str(p.relative_to(ROOT))
    lines = (ROOT / "events.jsonl").read_text().splitlines()[-150:] if (ROOT / "events.jsonl").exists() else []
    sources = []
    for d in sorted((p for p in ROOT.iterdir() if p.is_dir() and not p.name.startswith(".")), key=lambda p: -p.stat().st_mtime) if ROOT.exists() else []:
        clips = []
        for m in sorted([*d.glob("m*/"), *d.glob("live/*/")], key=lambda p: p.stat().st_mtime):
            if not (m / "seg.mp4").exists():
                continue
            tr = (m / "transcript.txt").read_text() if (m / "transcript.txt").exists() else ""
            clips.append({
                "dir": rel(m), "name": m.name, "meta": rd(m / "meta.json"),
                "transcribed": (m / "words.json").exists(),
                "seg": rel(m / "seg.mp4"),
                "transcript": [[float(s), float(e), t] for s, e, t in re.findall(r"\[\s*([\d.]+)-\s*([\d.]+)\] (.*)", tr)],
                "still": rel(m / "frame.jpg") if (m / "frame.jpg").exists() else None,
                "sheet": rel(m / "sheet.jpg") if (m / "sheet.jpg").exists() else None,
                "frame": rel(m / "frame_grid.jpg") if (m / "frame_grid.jpg").exists() else None,
                "renders": [{"mp4": rel(f), "info": rd(f.with_suffix(".json")) or {},
                             "qa": rel(f.with_name(f.stem + "_qa.jpg")) if f.with_name(f.stem + "_qa.jpg").exists() else None}
                            for f in sorted(m.glob("*.mp4"), key=lambda f: f.stat().st_mtime) if f.name != "seg.mp4"],
                "post": (m / "post.md").read_text() if (m / "post.md").exists() else None,
                "chat": (m / "chat.txt").read_text().splitlines()[-8:] if (m / "chat.txt").exists() else None,
                "context": rd(m / "context.json"),
            })
        sources.append({"name": d.name, "info": rd(d / "info.json") or {}, "moments": rd(d / "moments.json") or [], "timeline": rd(d / "timeline.json"),
                        "pulse": rd(d / "live/pulse.json"), "clips": clips})
    short = lambda e: {**e, "msg": e["msg"].replace(str(ROOT) + "/", "")}  # paths relative to the clips folder
    return {"root": str(ROOT), "now": time.time(), "events": [short(json.loads(l)) for l in lines], "sources": sources}

KEEP = {"info.json", "moments.json", "timeline.json"}  # small results the dashboard shows; clips live in m*/ and live/

def clear_cache():
    """Delete what a scan can download or rebuild (audio, chat, comments, transcripts, temp folders, job logs).
    Keeps clips, renders, moment lists and settings. Returns bytes freed."""
    if any(j["proc"].poll() is None for j in JOBS.values()):
        raise ValueError("Stop running jobs first.")
    junk = [*ROOT.glob(".jobs/*.log"), *ROOT.glob("*/live/buf"), *ROOT.glob("*/m*/.render"), *ROOT.glob("*/live/*/.render")]
    for d in (p for p in ROOT.iterdir() if p.is_dir() and not p.name.startswith(".")) if ROOT.exists() else []:
        junk += [f for f in d.iterdir() if f.name not in KEEP and not (f.is_dir() and (f.name == "live" or re.fullmatch(r"m\d+.*", f.name)))]
    freed = 0
    for f in junk:
        freed += sum(x.stat().st_size for x in f.rglob("*") if x.is_file()) if f.is_dir() else f.stat().st_size
        shutil.rmtree(f, ignore_errors=True) if f.is_dir() else f.unlink(missing_ok=True)
    log(f"cache cleared: {freed / 1e6:.0f} MB freed", "done", ROOT)
    return freed

JOBS = {}  # dashboard-started jobs: id -> {proc, action, src, model, started}
URL_OK = re.compile(r"^https?://[\w.-]+(:\d+)?(/[^\s\"'`<>]*)?$")

RENDER_FLAGS = {"in": "--in", "out": "--out", "layout": "--layout", "cam": "--cam", "main": "--main", "box": "--box",
                "split": "--split", "focus": "--focus", "hook": "--hook", "hook_secs": "--hook-secs", "hook_y": "--hook-y",
                "cap_y": "--cap-y", "words": "--words", "tag": "--tag", "name": "--name", "shots": "--shots"}
RENDER_SWITCHES = {"no_captions": "--no-captions", "no_tag": "--no-tag", "remember": "--remember"}
# headless runs: commands must be bare `uv run ...` (a `D=...;` or `cd ..;` prefix doesn't match the allow rule)
HEADLESS = ("Work autonomously and don't ask questions. Edit clips in parallel: spawn one clip-editor subagent per clip, "
            "all in a single message, each with its moment dir, the clipper.py path, the channel and the moment's why/said. "
            "Run scan and grab in the foreground with a 600000 ms "
            "timeout; never poll with Monitor or sleep. If scan times out on a long stream, run it again: it resumes. "
            "Grab all picks in one call with --moments. ")

def check_src(src):
    src = str(src or "").strip()
    if not (URL_OK.match(src) and len(src) < 500) and not (Path(src).is_absolute() and Path(src).is_file()):
        raise ValueError("Paste a full stream or video URL (https://...) or an absolute path to a local video file.")
    return src

def check_dir(d):
    p = (ROOT / str(d or "").lstrip("/")).resolve()
    if ROOT not in p.parents or not (p / "seg.mp4").is_file():
        raise ValueError("That clip folder doesn't exist anymore.")
    return p

def claude_cmd(prompt, model):
    if not shutil.which("claude"):
        raise ValueError("The claude CLI isn't on PATH. Run this from Claude Code instead, or install Claude Code.")
    model = model if model in ("haiku", "sonnet", "opus") else "sonnet"
    allow = ["Bash(uv run:*)", "Read", "Write", "Edit", "Grep", "Glob", "Agent"]
    return ["claude", "-p", prompt, "--model", model, "--plugin-dir", str(Path(__file__).resolve().parents[2]),
            "--permission-mode", "acceptEdits", "--allowedTools", *allow,
            "--settings", json.dumps({"permissions": {"allow": allow}}),
            # measured: ~40k -> ~20k context tokens on every turn (no MCP servers, none of the user's other plugins)
            "--strict-mcp-config", "--setting-sources", "project"], model

def start_job(b):
    """Dashboard actions -> background jobs. Everything is passed as argv (no shell) after validation."""
    action, model = b.get("action"), b.get("model", "sonnet")
    me = [sys.executable, str(Path(__file__).resolve())]
    src = label = None
    if action in ("scan", "live"):
        src = check_src(b.get("src"))
        cmd = [*me, action, src]
        label = {"scan": "Finding moments", "live": "Watching live"}[action]
    elif action == "grab":
        src = check_src(b.get("src"))
        cmd = [*me, "grab", src, str(float(b["start"])), str(float(b["end"]))]
        label = f"Grabbing {hms(float(b['start']))}"
    elif action == "render":
        d = check_dir(b.get("dir"))
        src, cmd, p = str(d), [*me, "render", str(d)], b.get("params", {})
        for k, flag in RENDER_FLAGS.items():
            v = p.get(k)
            if v not in (None, ""):
                v = json.dumps(v) if k == "shots" and not isinstance(v, str) else str(v)
                if len(v) > 4000 or (k == "name" and not re.fullmatch(r"[\w-]{1,60}", v)):
                    raise ValueError(f"Invalid {k}.")
                cmd += [flag, v]
        cmd += [flag for k, flag in RENDER_SWITCHES.items() if p.get(k)]
        label = "Rendering"
    elif action == "auto":
        src = check_src(b.get("src"))
        count = max(1, min(10, int(b.get("count", 3))))
        cmd, model = claude_cmd(f"Use the ortha-clipper clip skill. Source: {src}\nScan it, pick the best {count} moments, and render "
                                f"each one as a finished clip: QA strip checked, post.md written. {HEADLESS}"
                                f"Finish with one line per clip: path and hook.", model)
        label = f"Claude ({model}) is clipping"
    elif action == "edit":
        d = check_dir(b.get("dir"))
        src = str(d)
        note = str(b.get("note", ""))[:500]
        cmd, model = claude_cmd(f"Use the ortha-clipper clip skill. Edit the already-grabbed moment in {d} into a finished short "
                                f"(follow the clip-editor steps: in/out, framing, hook, render, QA, post.md). "
                                f"{('Editor notes from the user: ' + note) if note else ''} {HEADLESS}", model)
        label = f"Claude ({model}) is editing {d.name}"
    else:
        raise ValueError("Unknown action.")
    jid = f"{action}-{int(time.time() * 1000) % 10**9}"
    (ROOT / ".jobs").mkdir(parents=True, exist_ok=True)
    out = open(ROOT / ".jobs" / f"{jid}.log", "w")
    proc = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True,
                            env={**os.environ, "ORTHA_DIR": str(ROOT)})
    ai = action in ("auto", "edit")
    JOBS[jid] = {"proc": proc, "action": action, "label": label, "src": src, "model": model if ai else None, "started": time.time(),
                 "body": b}  # kept so a failed job can be retried as-is
    log(f"{label}: {src}", "start", src)

    def wait():
        code = proc.wait()
        tail = (ROOT / ".jobs" / f"{jid}.log").read_text(errors="replace").strip()[-400:]
        if code == 0:
            log(f"{label} finished. {tail if ai else ''}".strip(), "done", src)
        elif code > 0:
            lines = (ROOT / ".jobs" / f"{jid}.log").read_text(errors="replace").strip().splitlines()  # yt-dlp's own "ERROR: ..." beats our generic "failed: <cmd>" line
            JOBS[jid]["error"] = next((l for l in reversed(lines) if l.startswith("ERROR")), lines[-1] if lines else f"exit {code}")[:200]
            log(f"{label} failed (exit {code}): {tail[-200:]}", "error", src)
    threading.Thread(target=wait, daemon=True).start()
    return jid

def dash(_a=None):
    import secrets, signal
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    page = Path(__file__).with_name("dashboard.html")
    token = secrets.token_urlsafe(18)  # only our own page knows it; blocks drive-by POSTs from other sites
    hosts = {f"localhost:{PORT}", f"127.0.0.1:{PORT}"}  # blocks DNS rebinding

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            if self.headers.get("Host") not in hosts or self.headers.get("X-Ortha-Token") != token:
                return self.send(b'{"error":"forbidden"}', "application/json", 403)
            try:
                body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 10000)) or b"{}")
                path = urlparse(self.path).path
                if path == "/api/run":
                    res = {"id": start_job(body)}
                elif path == "/api/settings":
                    res = settings({"clipper": str(body.get("clipper", ""))[:60]})
                elif path in ("/api/retry", "/api/dismiss"):
                    job = JOBS.get(body.get("id"))
                    if not job or job["proc"].poll() in (None, 0):
                        raise ValueError("No failed job with that id.")
                    del JOBS[body["id"]]
                    res = {"id": start_job(job["body"])} if path == "/api/retry" else {"ok": True}
                elif path == "/api/clear-cache":
                    res = {"freed": clear_cache()}
                elif path == "/api/stop":
                    job = JOBS.get(body.get("id"))
                    if job and job["proc"].poll() is None:
                        os.killpg(job["proc"].pid, signal.SIGTERM)
                        log(f"stopped: {job['src']}", "done", job["src"])
                    res = {"ok": True}
                else:
                    return self.send(b'{"error":"not found"}', "application/json", 404)
                self.send(json.dumps(res).encode(), "application/json")
            except (ValueError, TypeError) as e:
                self.send(json.dumps({"error": str(e)}).encode(), "application/json", 400)

        def send(self, body, ctype, code=200):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = unquote(urlparse(self.path).path)
            if self.headers.get("Host") not in hosts:
                return self.send(b"forbidden", "text/plain", 403)
            if path in ("/", "/index.html"):
                return self.send(page.read_text().replace("{{TOKEN}}", token).encode(), "text/html; charset=utf-8")
            if path == "/api/state":
                s = state()
                pub = lambda k, j: {"id": k, **{x: v for x, v in j.items() if x not in ("proc", "body")}}
                s["jobs"] = [pub(k, j) for k, j in JOBS.items() if j["proc"].poll() is None]
                s["failed"] = [pub(k, j) for k, j in JOBS.items() if (j["proc"].poll() or 0) > 0]  # stopped = negative, not failed
                s["claude"] = bool(shutil.which("claude"))
                s["settings"] = settings()
                return self.send(json.dumps(s).encode(), "application/json")
            f = (ROOT / path.lstrip("/")).resolve()
            if ROOT not in f.parents or not f.is_file():
                return self.send(b"not found", "text/plain", 404)
            # Range support so Safari can play and seek video
            size, start, end = f.stat().st_size, 0, f.stat().st_size - 1
            r = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
            if r:
                start, end = (int(r[1]), int(r[2]) if r[2] else end) if r[1] else (size - int(r[2]), end)
            self.send_response(206 if r else 200)
            self.send_header("Content-Type", mimetypes.guess_type(f.name)[0] or "application/octet-stream")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            if r:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            with open(f, "rb") as fh:
                fh.seek(start)
                left = end - start + 1
                while left > 0 and (chunk := fh.read(min(1 << 20, left))):
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        return
                    left -= len(chunk)

    ROOT.mkdir(parents=True, exist_ok=True)
    print(f"dashboard: http://localhost:{PORT}  serving {ROOT}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()

def ensure_dash():
    """Start the dashboard in the background once and open it. ORTHA_NO_DASH=1 to skip."""
    if os.environ.get("ORTHA_NO_DASH"):
        return
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", PORT)) == 0:
            return
    subprocess.Popen([sys.executable, __file__, "dash"], start_new_session=True, env={**os.environ, "ORTHA_DIR": str(ROOT)},
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.8)
    webbrowser.open(f"http://localhost:{PORT}")
    print(f"dashboard: http://localhost:{PORT}", flush=True)


# ---------- cli ----------
def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan"); s.add_argument("src")
    s.add_argument("--top", type=int, default=12); s.add_argument("--threshold", type=float, default=3)
    s.add_argument("--pre", type=int, default=40); s.add_argument("--post", type=int, default=15)
    s.add_argument("--no-audio", action="store_true")
    s.add_argument("--chat", help="chat file: TwitchDownloader JSON, yt-dlp .live_chat.json, or jsonl [seconds, text]")
    s.add_argument("--no-transcript", action="store_true", help="skip whole-stream transcription (faster, chat/audio only)")
    s.add_argument("--transcript-model", default="base", help="Whisper model for the whole-stream skim (default base)")
    g = sub.add_parser("grab"); g.add_argument("src"); g.add_argument("start", nargs="?"); g.add_argument("end", nargs="?")
    g.add_argument("--moments", help="grab several scanned moments at once, e.g. 1,3,5")
    g.add_argument("--model", default="small")
    pr = sub.add_parser("prep"); pr.add_argument("dir"); pr.add_argument("--model", default="small")
    r = sub.add_parser("render"); r.add_argument("dir")
    r.add_argument("--in", dest="in_", default="0"); r.add_argument("--out", required=True)
    r.add_argument("--layout", choices=["stack", "full", "fit"])
    r.add_argument("--cam", help="facecam box x,y,w,h as 0-1 fractions of the source frame (enables stack)")
    r.add_argument("--main", default="0,0,1,1", help="bottom-panel source box x,y,w,h in stack (default whole frame)")
    r.add_argument("--box", help="source box x,y,w,h that fills the frame in full layout")
    r.add_argument("--shots", help='JSON list (or file) of timed framings in seg time: [{"t":0,"layout":"full","box":"x,y,w,h"},{"t":6.2,"layout":"stack","cam":"..","main":".."}]')
    r.add_argument("--split", type=float, default=0.4, help="share of height for the facecam in stack")
    r.add_argument("--focus", type=float, default=0.5, help="horizontal crop center for gameplay, 0-1")
    r.add_argument("--hook"); r.add_argument("--hook-secs", type=float, default=0, help="0 = whole clip")
    r.add_argument("--hook-y", type=float, default=0.07, help="hook box top, fraction of height")
    r.add_argument("--cap-y", type=float, help="caption center, fraction of height (default: seam in stack, 0.64-0.68 otherwise)")
    r.add_argument("--tag", help="clipper handle watermark (default: saved `config clipper`)")
    r.add_argument("--no-tag", action="store_true")
    r.add_argument("--credit", help="optional extra overlay line; streamer credit normally goes in post.md")
    r.add_argument("--words", type=int, default=3, help="max words per caption")
    r.add_argument("--no-captions", action="store_true"); r.add_argument("--name")
    r.add_argument("--remember", action="store_true", help="save this framing as the channel's profile for future streams")
    lv = sub.add_parser("live"); lv.add_argument("src")
    lv.add_argument("--threshold", type=float, default=3.5)
    lv.add_argument("--pre", type=int, default=40); lv.add_argument("--post", type=int, default=15)
    fr = sub.add_parser("frames"); fr.add_argument("dir"); fr.add_argument("times", nargs="+")
    fd = sub.add_parser("find", help="who is live right now, most viewers first")
    fd.add_argument("game", nargs="?", default="Grand Theft Auto VI"); fd.add_argument("--top", type=int, default=20)
    cf = sub.add_parser("config", help="config clipper @handle (shown on every clip) | config cookies chrome (YouTube bot check) | config twitch_id/twitch_secret (find)")
    cf.add_argument("key", nargs="?", choices=["clipper", "cookies", "twitch_id", "twitch_secret"]); cf.add_argument("value", nargs="?")
    sub.add_parser("dash")
    a = p.parse_args()
    global CMD
    CMD = a.cmd
    if a.cmd not in ("dash", "config", "frames", "find"):
        ensure_dash()
    try:
        {"scan": scan, "grab": grab, "prep": prep, "render": render, "live": live, "dash": dash, "frames": frames, "config": config, "find": find}[a.cmd](a)
    except subprocess.CalledProcessError as e:
        log(f"failed: {' '.join(map(str, e.cmd))[:200]}", "error")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
