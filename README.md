# Voice-to-Video Creator

Turn a folder of images + a single voice recording into a YouTube-ready 1080p MP4. Two interchangeable approaches for finding segment boundaries, optional intro/outro extras, and on-screen text overlays — all driven from a tkinter GUI or CLI.

---

## Quick start

> **On a Mac?** Follow [SETUP_MAC.md](SETUP_MAC.md) to install Python, FFmpeg, and the dependencies. Two paths covered: `uv` (recommended) and traditional `pip`.

```bash
python launcher.py                  # ← start here: front page with all four tools
```

The launcher shows four cards. Pick one, click **Continue**, and the chosen tool opens in its own window:

| Card | Tool | What it does |
|---|---|---|
| Create video — Tone markers *(recommended)* | [`tone_video_creator.py`](tone_video_creator.py) | Splits audio at 1020 Hz tone bursts, pairs each segment with a photo, renders MP4 |
| Create video — Voice pauses *(legacy)* | [`simple_video_creator.py`](simple_video_creator.py) | Splits at long silences. Use only for old recordings without tones |
| Merge / browse videos | [`video_merger.py`](video_merger.py) | Thumbnail gallery → tick → drag to reorder → merge into one MP4 |
| Convert video/audio → MP3 | [`audio_converter.py`](audio_converter.py) | Strips audio out of any video (or re-encodes any audio) into a 128/192/256/320 kbps MP3 |

Or run any tool directly:

```bash
python tone_video_creator.py        # main creator GUI
python tone_video_creator.py --audio your.mp3 --mode 2 --resolution 1080p --volume-boost 30   # CLI
python video_merger.py              # gallery → reorder → merge
python audio_converter.py           # any media → MP3
```

The tone-creator GUI pre-fills with the most recent audio in the workspace and the `section-…/` images folder. Tick the options you want, click **▶ Convert**.

---

## Two approaches for splitting audio into segments

| | **Approach 1 — VAD** | **Approach 2 — Tone** *(default)* |
|---|---|---|
| Script | `simple_video_creator.py --vad` | `tone_video_creator.py` |
| Recording style | Talk continuously, pause ≥ 2 s between segments | Play a 1020 Hz tone (phone tone-gen app) at boundaries |
| Detection | Silero VAD neural model | FFT bandpass — deterministic |
| Manual action per segment | None (just pause) | One tap on phone to play tone |
| Reliability | ~90% (depends on pause discipline) | ~100% (a tone is a tone) |
| Works on existing recordings? | Any audio with pauses | Only if tones were recorded |
| First-run cost | ~30 MB model download | Just FFmpeg |
| Best for | One-off recordings | High-volume / 200-segment batches |

Per-approach docs: [APPROACH_VAD.md](APPROACH_VAD.md), [APPROACH_TONE.md](APPROACH_TONE.md).

---

## What the pipeline always does

Both approaches end up at the same render stage and apply these automatically:

1. **Segment the audio** at detected boundaries (pauses or tones)
2. **Mute the tone** — for tone mode, segment edges are shrunk inward 80 ms past every detected tone so the beep is never audible
3. **Tighten internal silences** — any silence > 400 ms inside a segment is removed. When one voice ends, the next starts.
4. **Pair photo N with segment N**, render each as a 1920×1080 H.264 clip
5. **Concatenate** into a single MP4 (with optional intro video)
6. **Burn in text overlays** if requested

---

## The tone GUI at a glance (`tone_video_creator.py`)

| Section | What it does |
|---|---|
| **1. Pick your files** | Images folder + voice file (auto-picked from workspace) |
| **2. Recording mode** | Mode 1 (audio starts WITH a tone — N tones → N segments) <br> Mode 2 (audio bookended with talking — N tones → N+1 segments) |
| **3. Tone tuning** | Frequency (default 1020 Hz), threshold (default 0.30) |
| **4. Audio & output** | **Voice boost** slider (0–100% → 0…+12 dB) <br> **Output quality** dropdown (360p / 480p / 720p / **1080p** / 1440p / 2160p) |
| **5. Text overlays** | ☐ Scrolling ticker at bottom (right→left, looping) <br> ☐ Static text on right side <br> Text input (default `Varabm VoiceOver`) |
| **6. Extras** | ☐ Start video — plays first as intro (its own audio) <br> ☐ Start image — maps to segment 1 (shifts all panels by one) <br> ☐ End image — maps to the trailing segment (resolves "more segments than panels") |
| **▶ Convert** | Runs the pipeline; progress bar updates per stage and per clip |

The voice boost is useful when your source recording is too quiet. It linearly maps the slider to gain in dB (50% = +6 dB, 100% = +12 dB). If the math predicts clipping the log writes a `WILL CLIP, expect distortion` warning — back the slider off when you see it. Realistic range for typical voice recordings: 20–50%.

Output resolution affects render time and file size. Text overlay font sizes scale with the chosen resolution so 360p doesn't look broken.

---

## Counting math (when does each option help?)

You have **N panels** and the audio splits into **K segments**. Use this table to plan:

| Setup | Tones to play (Mode 2) | Segments built | Panels needed |
|---|---|---|---|
| Plain | N − 1 | N | N |
| With End Image | N | N + 1 | N + end → fits |
| With Start Image | N | N + 1 | start + N → fits |
| With both | N + 1 | N + 2 | start + N + end → fits |

**Example: 33 panels.**
- Plain Mode 2 → play **32** tones → 33 segments → matches.
- With End Image → play **33** tones → 34 segments → matches (33 panels + end).
- Your existing audio already has 33 tones, so **ticking End Image** is the right way to use it: panel 33 plays at its natural length and the trailing audio gets the disclaimer/end image.

---

## Project layout

```
launcher.py                  ← front page (the easiest way in)
tone_video_creator.py        ← main creator (GUI + CLI) — Approach 2 (tone)
simple_video_creator.py      ← Approach 1 (VAD) + shared helpers (DO NOT delete)
video_merger.py              ← thumbnail gallery → reorder → merge multiple MP4s
audio_converter.py           ← convert any video/audio to MP3

APPROACH_TONE.md             ← Tone recording instructions
APPROACH_VAD.md              ← VAD recording instructions

_tone_probe.py               ← diagnostic: count tones at each threshold
_vad_test.py                 ← diagnostic: count VAD segments at each pause threshold

section-20260502-235344/     ← 33 PNG panels (the main image series)
start_image/                 ← optional intro image (DISCLAIMER!.png)
end_image/                   ← optional outro image
start_video/                 ← optional intro video (with its own audio)

mkv2 -enhanced-v2.mp3        ← latest voice recording
video_tone_*.mp4             ← outputs from the tone creator
merged_*.mp4                 ← outputs from the video merger

requirements.txt
```

`simple_video_creator.py` exports helpers (`_render_and_concat`, `find_ffmpeg`, `tighten_segment`, etc.) that the other tools import — keep it even if you never use the legacy GUI.

---

## Diagnostics — pick a threshold without rendering

```bash
python _tone_probe.py    # how many 1020 Hz tones at each threshold
python _vad_test.py      # how many VAD speech segments at each pause threshold
```

Look for a **plateau** — a range of thresholds where the count stays the same. That's where to operate.

---

## Dependencies

```
pydub
numpy                 (tone FFT)
faster-whisper        (only if you ever revisit keyword markers — currently unused in production)
silero-vad + torch    (Approach 1 only)
FFmpeg                (system binary; install via `winget install Gyan.FFmpeg`)
```

The scripts find FFmpeg automatically — checks `PATH`, the registry's user/system Path entries, and common WinGet/Scoop install locations.

---

## Recording cheat sheets

### Tone (Approach 2, Mode 2 — the user's default workflow)

1. Install a tone-generator app on your phone, set to **1020 Hz**.
2. Record voice continuously. Between each pair of segments, hold the phone near the mic and play the tone for 0.5–1 second.
3. For **N panels**, play **N − 1** tones (plus 1 more if using End Image, plus 1 more if using Start Image).
4. Save the recording, run the GUI, tick what you need, **Convert**.

### VAD (Approach 1)

1. Record voice continuously. Pause **≥ 2 seconds** between each segment.
2. Run `python simple_video_creator.py --vad --audio your.mp3`.
3. Adjust `--pause-ms` if the segment count is off (use `_vad_test.py` to preview).
