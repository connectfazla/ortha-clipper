---
name: clip
description: Ortha Clipper. Turn Twitch / YouTube / Kick streams (live or VOD, e.g. GTA VI gameplay) and local recordings into captioned 9:16 shorts. Finds the moments where chat and audio spike above the channel's own normal, cuts them, frames facecam + gameplay or follows whoever is talking, burns word-by-word captions, a hook and the clipper's tag. Use when the user wants to clip a stream, find highlights in a VOD, watch a live stream for clip-worthy moments, or make Shorts / TikToks / Reels from gameplay.
---

# Ortha Clipper: you are the editor

`clipper.py` sits next to this file. Call it `C`: run `uv run --script <this skill's base dir>/clipper.py <cmd>`. uv installs the dependencies on first run. ffmpeg should be on PATH; if it isn't, a bundled one is used. Output goes to `./clips/<source>/` (`ORTHA_DIR` overrides).

The script does the mechanical work. You make the editorial calls, the way a good human editor would.

**Dashboard:** every command makes sure `http://localhost:8790` is running and opens it the first time. It shows progress, the chat timeline against the channel baseline, moments, playable clips, and controls for scanning, editing and re-rendering. Tell the user the URL once.

**Clipper tag:** clips carry the clipper's own handle, not the source channel. If `C config` shows no `clipper`, ask the user once ("What handle should go on your clips?"), then run `C config clipper "@handle"`. Streamer credit goes in `post.md`, not on the video.

**Game + streamer memory:** `scan`, `grab` and `prep` print one line with the source title, channel and detected game (GTA VI, GTA V, Fortnite, Valorant…) and its hashtags. When a game is known, the hook names what happened in game terms ("1 VS 3", "HE GOT WASTED", "CLUTCH"), the title names the game, and post.md includes those hashtags. If a channel has saved framing, they print it too. Reuse it when the facecam hasn't moved, and add `--remember` to a render to save or update it.

## Save tokens: tier the work

- **You (main thread):** scan, pick moments, and delegate. Read the printed scan summary, not the whole `moments.json`.
- **`clip-editor` subagent (Sonnet):** one per moment, spawned in parallel. Its images and re-renders stay out of your context. Pass it the moment dir, the `clipper.py` path, the moment's `top_chat`, and the streamer's channel.
- **Escalate** to `model: "opus"` only for a clip that comes back `needs review`.
- If subagents aren't available, follow `agents/clip-editor.md` yourself, one clip at a time.

## VOD / long video (posted after the stream, or a local file)

1. `C scan <url-or-file>`: gets the chat, downloads only the smallest audio track (never the video), scores every 5 s against the channel's own trailing baseline, and prints ranked moments. Run it in the background; it takes about 1 min per hour of stream.
   - YouTube: chat replay is automatic. Twitch VOD: there's no public chat-replay API, so it scores audio only. For chat scoring, the user exports with [TwitchDownloader](https://github.com/lay295/TwitchDownloader) (`TwitchDownloaderCLI chatdownload --id <vod> -o chat.json`), then pass `--chat chat.json`. Local file: audio only.
   - `--threshold 4` for fewer moments, `2.5` for more. `--top 20`.
2. Triage the summary. Spikes are counted in distinct chatters, so "6 people/5s vs 1" means six different people reacted, not one spammer. `KEKW`/`LUL` spam means funny, `?????` confusion or a fail, `W`/`POG` hype, `F` a death. Skip giveaways, raids and sub trains. Keep moments a stranger would laugh or gasp at with no context.
3. `C grab <src> <start> <end>` for each pick. Only that section is downloaded. You get `seg.mp4`, `transcript.txt`, `words.json`, `frame_grid.jpg` and `sheet.jpg` (a frame every few seconds).
4. Hand each moment dir to a `clip-editor`. It does in/out, framing, shots, hook, render, QA and post copy.
5. Report the final mp4 paths and hooks to the user.

## Live stream

`C live <url>` keeps a rolling 4-minute buffer and reads chat (Twitch: anonymous IRC, YouTube: live chat). When chat spikes above this channel's trailing baseline (`--threshold`, default 3.5), it waits 15 s for the reaction, then saves `clips/<src>/live/<time>/seg.mp4` + `chat.txt` and prints `MOMENT <dir>`.

- Run it with `run_in_background`. Watch for `MOMENT` lines with Monitor if you have it.
- For each moment: `C prep <dir>` (transcript + sheet), then a `clip-editor`. Use `chat.txt` as its chat context.
- The baseline needs about 60 s to warm up. After the stream, `scan` the VOD to catch anything live mode missed.

## Render reference

```
C render <dir> --in 4.2 --out 38.5 --hook "..." --name slug
   framing (pick one):
     --cam x,y,w,h [--main x,y,w,h] [--split 0.4]   facecam on top, gameplay/main below
     --layout full --box x,y,w,h [--focus 0.5]      one box fills 9:16
     --layout fit                                    whole frame over a blurred copy
     --shots '[{"t":4.2,"layout":"stack","cam":"..","main":".."},{"t":12,"layout":"full","box":".."}]'
            timed cuts in seg time, e.g. follow whoever is talking
   text: --hook-secs 4 (0 = whole clip)  --hook-y 0.07  --cap-y 0.42  --words 3  --no-captions
   tag:  --tag "@x" overrides the saved handle, --no-tag removes it
   --remember  save this framing as the channel profile for future streams
C frames <dir> 12.5 14 20      gridded frames at exact seconds, to check who's talking or reacting
```

## Notes

- Whisper: `--model small` (default) on grab/prep. `medium` for heavy accents or loud game audio, `base` for speed.
- Subscriber-only or age-gated VODs need cookies. Tell the user; don't work around it.
- Clipping someone else's stream needs their permission or a clipping program they run. If the user hasn't mentioned one, remind them once.
