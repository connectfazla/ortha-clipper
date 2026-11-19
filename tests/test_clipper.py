# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy", "pillow", "imageio-ffmpeg"]
# ///
"""Self-check for the logic that can silently go wrong. Run: uv run --script tests/test_clipper.py"""
import importlib.util, json, os, sys, tempfile
from pathlib import Path

import numpy as np

os.environ["ORTHA_NO_DASH"] = "1"
spec = importlib.util.spec_from_file_location("clipper", Path(__file__).parents[1] / "skills/clip/clipper.py")
sc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sc)
rng = np.random.default_rng(0)

# spike detection is relative to the channel's own baseline
small = rng.poisson(10, 400).astype(float); small[200] = 60
z = sc.baseline_z(small)
assert z[200] > 5 and np.delete(z, 200).max() < 4, (z[200], np.delete(z, 200).max())
big = rng.poisson(1000, 400).astype(float); big[200] = 1060  # +60 msgs is noise for a big channel
assert sc.baseline_z(big)[200] < 3
assert sc.baseline_z([5] * 11).max() == 0  # warm-up: no spikes before a baseline exists

# source naming
assert sc.slug("https://www.youtube.com/watch?v=MvqroO8Le8c&t=10") == "youtube-MvqroO8Le8c"
assert sc.slug("https://youtu.be/MvqroO8Le8c") == "youtube-MvqroO8Le8c"
assert sc.slug("https://www.twitch.tv/videos/2931745878") == "twitch-2931745878"
assert sc.slug("https://www.twitch.tv/kaicenat") == "twitch-kaicenat"
assert sc.secs("1:02:03") == 3723 and sc.secs("62:03") == 3723 and sc.secs("7.5") == 7.5

# chat file formats
with tempfile.TemporaryDirectory() as t:
    p = Path(t) / "td.json"
    p.write_text(json.dumps({"comments": [{"content_offset_seconds": 12.5, "commenter": {"name": "bob"}, "message": {"body": "KEKW"}}]}))
    assert sc.read_chat_file(p) == [[12.5, "KEKW", "bob"]]
    line = {"replayChatItemAction": {"videoOffsetTimeMsec": "4000", "actions": [{"addChatItemAction": {"item": {
        "liveChatTextMessageRenderer": {"authorName": {"simpleText": "amy"},
                                        "message": {"runs": [{"text": "no way "}, {"emoji": {"shortcuts": [":fire:"]}}]}}}}}]}}
    p.write_text(json.dumps(line) + "\n")
    assert sc.read_chat_file(p) == [[4.0, "no way :fire:", "amy"]]
    p.write_text('[1.0, "hi"]\n[2.0, "W", "cat"]\n')
    assert sc.read_chat_file(p) == [[1.0, "hi", ""], [2.0, "W", "cat"]]

# game detection: GTA VI must win over GTA V, and plain titles get nothing
assert sc.detect_game("GTA 6 FIRST PLAYTHROUGH")["game"] == "GTA VI"
assert sc.detect_game("Grand Theft Auto VI | Vice City day 2")["game"] == "GTA VI"
assert sc.detect_game("NoPixel GTA RP grind")["game"] == "GTA V"
assert sc.detect_game("chilling and chatting")["game"] == ""

# caption pages break on word count, pauses and sentence ends
w = lambda word, s, e: {"w": word, "s": s, "e": e}
words = [w("so", 0, .2), w("I", .2, .3), w("went", .3, .5), w("there.", .5, .8), w("Then", .9, 1.1), w("after", 2.0, 2.2)]
assert [[x["w"] for x in p] for p in sc.pages(words, 3)] == [["so", "I", "went"], ["there."], ["Then"], ["after"]]

# caption timeline covers the clip exactly and starts in sync
with tempfile.TemporaryDirectory() as t:
    caps, _ = sc.caption_track(words, 0.25, 2.5, Path(t), 3)
    durs = [float(l.split()[1]) for l in caps.read_text().splitlines() if l.startswith("duration")]
    assert abs(sum(durs) - 2.25) < 0.02, sum(durs)
    assert caps.read_text().splitlines()[1].endswith("c0000.png'")  # first word already on screen at t=0

print("ok: all Ortha Clipper checks passed")
