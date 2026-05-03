# Approach 1 — Voice Activity Detection (VAD)

> **Script:** [`simple_video_creator.py`](simple_video_creator.py) — run with `--vad`.
> **Easiest entry point:** [`launcher.py`](launcher.py) — the "Voice pauses (legacy)" card opens the silence-based GUI. For the proper VAD pipeline, run the script directly with `--vad`.

The program splits your audio at every natural pause longer than a threshold using a small neural network (Silero VAD). No markers, no clapping, no special words. You just **record continuously and pause between segments**.

---

## When to use this approach

✅ Best when:
- You record continuously and find it natural to pause between segments
- You want zero manual marking actions
- Your audio is reasonably clean (any normal phone or laptop mic is fine)

⚠️ Skip this if:
- Your segments flow into each other with no real pause (use [Approach 2 — Tone](APPROACH_TONE.md) instead)
- You record in extremely noisy environments (background music, traffic) where VAD may over-segment

---

## What you do (recording side)

1. Open any voice-recording app
2. Start recording
3. Speak segment 1
4. **Pause for at least 2 seconds**
5. Speak segment 2
6. **Pause for at least 2 seconds**
7. … continue until done
8. Stop and save

That's it. No tones, no claps, no spoken markers.

> **Tip:** count "one-Mississippi, two-Mississippi" silently to make sure your pause is long enough. A pause that's too short can merge two segments.

---

## What the program does (under the hood)

1. **Loads your audio** with pydub (MP3/WAV/M4A/FLAC/OGG all work)
2. **Resamples** to 16 kHz mono for the VAD model
3. **Runs Silero VAD** — a 30 MB ONNX/PyTorch model that classifies each ~30 ms frame as speech or non-speech
4. **Groups** consecutive speech frames into "speech blocks", merging gaps shorter than `--pause-ms`
5. **Pads** each block by 200 ms so word edges aren't clipped
6. **Pairs** segment N with photo N
7. **Renders** each pair as a 1080p H.264 clip with the audio
8. **Concatenates** clips into a single MP4

---

## Run it

```bash
python simple_video_creator.py --vad --audio your_recording.mp3
```

### Options

| Flag | Default | Meaning |
|---|---|---|
| `--audio path` | (auto) | Path to your audio file |
| `--images path` | `section-…/` | Folder with `panel-001.png`, `panel-002.png`, … |
| `--pause-ms N` | `1500` | Pause length (ms) that counts as a segment boundary |

### Tuning `--pause-ms`

You can use the `_vad_test.py` diagnostic to see how many segments each threshold gives **without rendering**:

```bash
python _vad_test.py
```

Output:
```
pause >= 1000 ms  ->  36 segments
pause >= 1500 ms  ->  31 segments
pause >= 2000 ms  ->  29 segments
```

Pick the threshold whose count matches your photo count, then run with `--pause-ms <value>`.

---

## Pros and cons

| Pros | Cons |
|---|---|
| Zero manual action while recording | Detection isn't 100% — close pauses may merge or short ones split |
| Robust to breath / room tone | Requires a second neural model dependency (silero-vad + torch) |
| Works on any language | First run downloads ~30 MB |
| One continuous take is fine | Pause discipline matters — short pauses → wrong count |

---

## When VAD fails — fall back to [Approach 2](APPROACH_TONE.md)

If your segment count keeps drifting and you can't get reliable pause behavior, switch to tone markers — see `APPROACH_TONE.md`. Tone detection is **deterministic** (a 1020 Hz tone is a 1020 Hz tone, no neural network involved) and survives noisy or fluid recordings where VAD struggles.
