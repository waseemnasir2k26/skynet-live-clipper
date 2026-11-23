# skynet-live-clipper

Watches a live stream as it happens, finds the moments worth a Short, and cuts branded 9:16 clips on its own.

```
live URL ──► yt-dlp + ffmpeg ──► 10 s segments ─┬─► audio loudness per second ─┐
             live chat (yt-dlp) ───────────────┼─► chat velocity + hype tokens ├─► rolling z-scores ─► spike ─► candidate
                                                └─► faster-whisper (CUDA) ─────┘                                  │
                                                                                                                  ▼
          out/clips/<id>/clip.mp4 + cover.jpg + meta.json ◄── render (NVENC) ◄── LLM judge (claude haiku, strict JSON)
```

## What it does

1. **Ingest.** `mode: live` captures any yt-dlp URL itself (YouTube is proven; Twitch / Kick go through the same
   yt-dlp path but were not tested). It cuts the stream into 10 s MPEG-TS segments and downloads live chat.
   `mode: file` follows an existing capture file while it grows (or a finished VOD) with its chat JSON.
   If the followed file stops growing while the stream is still live, the engine switches to its own capture.
2. **Signals, every second of stream time.**
   - chat messages per second against a 10-minute rolling median/MAD baseline (robust z-score),
   - hype tokens in chat (😂 💀 lol, "L", channel emotes like `:_BakchodianLOL:`, superchats ×5),
   - audio loudness (loudest 250 ms per second) against its own rolling baseline,
   - faster-whisper `large-v3` on the GPU, Hindi, words with timestamps (Devanagari, transliterated to Roman Hinglish).
3. **Candidates.** A combined score (chat and audio both count, agreement is rewarded) crosses a threshold → one
   candidate per excursion. Chat-led peaks are shifted back by the viewer reaction lag (12 s). If nothing fires for
   5 minutes, the best window in that span is still judged, so quiet stretches are not ignored.
4. **LLM judge.** Headless `claude -p --model haiku` gets the transcript window, loudness peaks, chat bursts and
   the chat messages, plus 5 frames of the window it opens with its Read tool (so it sees what happens on screen), and returns strict JSON: score 0-100, reason, start/end (20-55 s, hook first), title, caption,
   hashtags, `safe`. Windows that show sexual content, strip-club / lap-dance scenes or a streamer-pixelated
   scene are rejected as unsafe for Shorts (found on this stream: GTA VI club scenes). Calls are capped per hour and per stream. If the CLI is missing, fails or the cap is hit, a deterministic
   signal-based score and window are used instead (marked in `reasons`).
5. **Cut + design.** Facecam found once per stream (OpenCV YuNet face detector over 12 frames; override in config).
   Stacked layout: zoomed facecam on top, gameplay below, a yellow divider, word-timed captions (2-3 words, active
   word highlighted), a hook title for the first 2.6 s, a branded glass card (LIVE / credit / "clipped by AI") placed
   over the spot where the original facecam would show twice. Loudness normalised to -14 LUFS. `h264_nvenc` with
   `-hwaccel cuda`, libx264 fallback. A designed `cover.jpg` (loudest frame + title).
6. **Gates.** Music copyright scan (Shazam-based `music-copyright-check` skill, if installed) runs on every clip and
   writes `copyright.txt` next to it. Nothing is ever published or uploaded by this engine.

## Output contract (`C:\clipper-data\out\`, Linux: `/var/lib/clipper-data/out/`)

- `clips/<clip_id>/clip.mp4`, `cover.jpg`, `meta.json` = `{id, stream_url, streamer, stream_title, t_start, t_end,
duration, created_at, score, reasons[], signals{chat_z, audio_z, llm_score}, transcript, title, caption,
hashtags[], status:"ready"}` (+ `render.json`, `copyright.txt` extras that the contract does not depend on)
- `state.json` rewritten every 5 s, `events.jsonl` and `signals.jsonl` append-only
- optional input `commands.jsonl`: `{"ts": ..., "cmd": "watch", "url": "..."}` switches the engine to that live URL

## Run

Windows (tested):

```
py -3.11 -m pip install -r requirements.txt
py -3.11 run.py                                   # uses config.yaml (source.mode file|live)
py -3.11 run.py --live https://www.youtube.com/watch?v=VIDEO_ID
py -3.11 run.py --file capture.mp4 --chat capture.live_chat.json
py -3.11 run.py --profile light                   # small whisper on CPU, libx264, 3 clips/hour
```

Stop cleanly: create the file `C:\clipper-data\STOP` (delete it before the next start).
Logs: `C:\clipper-data\<stream_id>\engine.log`. Counters: `C:\clipper-data\<stream_id>\stats.json`.

Linux / VPS: `sudo bash deploy/install.sh`, then `systemctl enable --now skynet-live-clipper`. The light profile is
meant for 4 vCPU / 8 GB without a GPU: capture + chat/audio detection + `small` whisper int8 on CPU + libx264
veryfast, one clip at a time, systemd caps CPU at 300% and memory at 5 GB.

## Modes (state.json `mode` = live | replay | vod)

- `live`: own capture of a live URL, or following a capture file that is still growing.
- `vod`: a finished file (`--file`), processed as fast as the box allows. `--start-epoch` maps a wall-timed live chat
  file onto a VOD (offset found by audio cross-correlation, see below).
- `replay`: `--file X --replay [--replay-from S] [--replay-rate 1.0]` feeds a recording through the exact real-time
  path (ffmpeg `-readrate`, chat released only when the video reaches it). For screen recording the dashboard; it is
  always labelled `replay`, never `live`, and its moment-to-clip latency is measured against replay time.

## Channel watcher

`py -3.11 watch.py --channel https://www.youtube.com/@CarryisLive/streams --every 300` polls `<channel>/live` with
one metadata-only yt-dlp call, starts `run.py --live <url>` when the channel is live, and goes back to polling when
the engine exits. Never starts a second engine; caps on polls, hours and starts per day. Log
`C:\clipper-data\watch.log`. Stop the watcher only: create `C:\clipper-data\WATCH-STOP`; `STOP` stops both.

## Heavy lane (after the laptop's full-power window): Kaggle -> Colab -> VPS light

`py -3.11 -m skynet_clipper.kaggle_lane pack CHUNK.mkv [CHAT.json] --start-epoch E`, then `push <slug>` and
`pull <slug>`. It uploads a PRIVATE dataset (chunk + chat + this repo's code, no secrets) and a PRIVATE GPU kernel
(2x T4) that runs the same engine in `vod` mode with large-v3 whisper on CUDA and libx264. The claude CLI judge
is not available there, so clips get the deterministic fallback score; re-judge on the laptop later. Status: a 60 s test chunk was pushed
(private) and ran over an hour without finishing (most likely the `-follow` hang, now fixed with `follow: never`, not re-tested); the GPU run was replaced by a no-op version to stop it. Colab (needs a browser tab) and the VPS light profile
(`deploy/`) are the next lanes.

## Safety and caps (all in `config.yaml`)

- `safety.max_steps`, `safety.max_wall_hours`, `judge.max_calls_per_hour`, `judge.max_calls_per_stream`,
  `clips.max_per_hour`; STOP file breaker.
- Disk guard: capture stops below `ingest.min_free_gb` (40 GB); segments older than 8 h are pruned.
- One render at a time, ffmpeg at BelowNormal priority with capped threads; the shared `RENDER.lock` is taken when
  renders take longer than 60 s.
- `safety.full_power_until`: after that time the engine switches itself to light mode (small whisper on CPU,
  libx264, 3 clips/hour).
- Supervisor restarts capture with backoff when HLS drops; HLS URLs are re-resolved on every restart.

## Tests

`py -3.11 -m pytest -q tests` covers spike detection, rolling z-scores, dedupe, budgets, chat parsing, judge JSON
parsing/bounds, transliteration and the output contract schema.

## Honest limits

- Captions come from Whisper Hindi ASR on a loud game mix; some words are wrong. Roman Hinglish is a rule-based
  transliteration of Devanagari (good on common words, imperfect on English words Whisper wrote in Devanagari).
- The facecam layout assumes a stable webcam position; a mid-stream scene change keeps the first layout until restart.
- Chat timing is mapped by wall clock; viewer delay is a fixed 12 s guess.
- Twitch / Kick, the Linux install script and the systemd unit were written but not run.

## What was tested on 2026-11-24 (CarryisLive GTA VI stream ipZCIW7DNJo)

- Live mode against the real YouTube stream: 170 s captured + 160 chat messages (smoke run, ASR/judge off).
- Following the growing live capture while the stream was live (02:52-03:09): 8 clips cut from captured material.
- Replay of the finished VOD with chat replay: 2.5 h of stream (VOD 3600-12600 s) through the real-time path,
  3x / 1x / 3x legs: 81 candidates, 81 vision-LLM judgments, 14 clips, 0 errors.
- Channel watcher polling. Not tested: Twitch/Kick, Linux/systemd install, Kaggle lane end to end, light mode switch.
