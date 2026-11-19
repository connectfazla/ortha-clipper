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

- **You (main thread):** scan, read the summary and `skim.txt`, pick, and delegate. Don't open `moments.json` or full transcripts; the summary and skim carry what you need.
- **`clip-editor` subagent (Sonnet):** one per moment, spawned in parallel. Its images and re-renders stay out of your context. Pass it the moment dir, the `clipper.py` path, the moment's `top_chat`, and the streamer's channel.
- **Escalate** to `model: "opus"` only for a clip that comes back `needs review`.
- If subagents aren't available, follow `agents/clip-editor.md` yourself, one clip at a time.

## No link yet: find streams and campaigns

- `C find ["Grand Theft Auto VI"]` lists who is live now, most viewers first. YouTube always works (it needs cookies if the bot check fires). Twitch needs a free app from dev.twitch.tv/console, then `C config twitch_id <id>` and `C config twitch_secret <secret>`. Never use Twitch's private web client ID.
- Paid clipping programs: when the user asks where to earn, check live listings on Whop Content Rewards (`whop.com/discover`, search "clipping") and `clip.tech/clippers`. Report each campaign's rate per 1K views, budget left, rules and link exactly as the page shows them, and never guess a number. Clip only the creators a campaign covers, and follow its rules (length, tags, credit) in the render and `post.md`.

## VOD / long video (posted after the stream, or a local file)

1. `C scan <url-or-file>`: run it in the **foreground** with a 600000 ms timeout. Don't background it and poll; every poll resends your whole context. It gets the chat, the smallest *original-language* audio track (never the video), YouTube comment timestamps, and a local Whisper transcript of the whole stream (about 5 min per hour; `--no-transcript` skips it). **If it times out on a long stream, run it again: it resumes.**
   - Each moment's score blends capped, independent signals, so no single one decides: chat (distinct people vs this channel's normal, plus hype words), audio (loudness, silence followed by an outburst), transcript (reaction phrases, fast excited speech), and YouTube comments that timestamp the moment.
   - YouTube: chat replay is automatic. Twitch VOD: there's no public chat-replay API, so chat is skipped; audio, transcript and comments still work. For chat scoring, the user exports with [TwitchDownloader](https://github.com/lay295/TwitchDownloader) (`TwitchDownloaderCLI chatdownload --id <vod> -o chat.json`), then pass `--chat chat.json`.
   - `--threshold 4` for fewer moments, `2.5` for more. `--top 20`.
2. **Triage the summary.** Each moment prints `why:` (which signals fired), `said:` (the quote), chat and a top comment. Then **read `skim.txt`**: one line per 30 s of the whole stream, about 5k tokens per hour, with picked moments marked `*#n`. Add moments the scores missed. Streamers' best stories, hot takes and setups are often quiet in chat.
   What clippers keep (from working clipper playbooks):
   - reactions bigger than the moment, clutch plays or disasters with an obvious outcome
   - hot takes and unfiltered opinions, stories with a punchline, chat or donations read out with a reply, a guest walking in
   - firsts: a new game, a record, a reveal (post these fast)
   Skip: anything that needs minutes of context or a running joke, alerts on their own, dead air, and loud copyrighted music (it gets clips muted). Apply the **3-second test**: would a stranger get it within 3 s?
3. **Grab all picks in one call:** `C grab <src> --moments 1,3,5`. Add extra picks from the skim with `C grab <src> <start> <end>`. One Whisper load covers every clip. Each dir gets `context.md` (why it scored, the surrounding transcript, the chat in that window, comments pointing there), `seg.mp4`, `transcript.txt`, `words.json`, `frame_grid.jpg` and `sheet.jpg`. Nothing gets cut before `context.md` has been read.
4. Hand each moment dir to a `clip-editor`. Pass the dir, the script path, the streamer's channel and the moment's `why`/`said`/chat lines. It does in/out, framing, hook, captions, render, QA and post copy.
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
- yt-dlp says "Sign in to confirm you're not a bot": ask which browser they're logged into YouTube with, then run `C config cookies <chrome|safari|firefox|brave|edge>`.
- Clipping someone else's stream needs their permission or a clipping program they run. If the user hasn't mentioned one, remind them once.
