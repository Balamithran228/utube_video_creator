# Approach 2 — 1020 Hz Tone Markers

> **Script:** `video_toolkit/tone_video_creator.py`
> **Easiest entry point:** [`launcher.py`](launcher.py) — pick "Create video — Tone markers" and click Continue.

The program splits your audio at every **1020 Hz tone burst** and pairs each resulting segment with a photo. Detection is deterministic — pure FFT, no AI.

---

## Two recording modes

You pick one based on **what's at the very start and very end** of your recording.

### Mode 1 — `--mode 1` (audio bookended with tones)

Pattern: `[tone] [seg1] [tone] [seg2] … [tone] [segN]`

- Audio **starts** with a tone
- Each tone marks the START of a segment
- Audio before the first tone is discarded
- **N tones → N segments**

For 33 panels: play **33** tones.

### Mode 2 — `--mode 2` (audio bookended with talking) ✓ default

Pattern: `[seg1] [tone] [seg2] [tone] … [tone] [segN]`

- Audio starts and ends with **talking** (no tones at the edges)
- Tones are dividers between segments
- **N tones → N+1 segments**

For 33 panels (plain): play **32** tones. With Extras enabled the count changes — see below.

---

## Cleanup applied to BOTH modes

Both modes automatically:

1. **Mute the tone.** Each segment's edges are shrunk inward 80 ms past the detected tone boundary. The 1020 Hz beep is **never audible** in the output video.
2. **Remove internal silences > 400 ms.** When your voice ends, the next voice starts. Short natural pauses (< 400 ms) are kept so speech still sounds natural.

---

## Audio & output settings (GUI section 4)

Two controls live in this section:

### Voice boost (slider 0–100%)
Amplifies the voice before rendering. The slider maps linearly to **0 dB at 0%** and **+12 dB at 100%** of gain.

| Slider | Gain | Approx. effect |
|---|---|---|
| 0% | +0 dB | No change (default) |
| 25% | +3 dB | Slightly louder |
| 50% | +6 dB | 2× amplitude (~clearly louder) |
| 100% | +12 dB | 4× amplitude (~2× perceived loudness) |

**Heads-up — clipping:** if your source recording's peak is already close to 0 dBFS, large boost values will clip and produce distortion. The pipeline checks for this and writes a `WILL CLIP, expect distortion` warning into the log. If you see it, drop the slider back. Realistic range for typical voice recordings: **20–50%**.

### Output quality (dropdown)
Picks the output resolution. All clips and the final video are rendered at the chosen size; aspect ratio is preserved by padding (no cropping).

| Option | Pixels | Use for |
|---|---|---|
| 360p | 640×360 | Quick test renders, small files |
| 480p | 854×480 | Low-bandwidth uploads |
| 720p | 1280×720 | Decent quality, fast renders |
| **1080p** | 1920×1080 | YouTube standard (default) |
| 1440p | 2560×1440 | Higher-quality YouTube |
| 2160p | 3840×2160 | 4K |

Higher resolutions take noticeably longer to render and produce larger files. Text overlays scale automatically with the chosen resolution.

---

## Optional Extras (the GUI checkboxes)

Each Extra is a separate checkbox in the GUI. Untick = the feature is not added.

### Start Video
A separate video file (with its own audio) prepended to the final video as an intro. Re-encoded to match the rest of the timeline (1920×1080, H.264, AAC 48 kHz).
- Auto-picks the first file in `sample_assets/optional/start_video/` if present
- Doesn't affect segment counting — it's an entirely separate clip at the head

### Start Image
A still image used for **segment 1**. When ticked:
- The Start Image gets the audio of segment 1
- `panel-001.png` shifts to segment 2
- `panel-002.png` shifts to segment 3
- … etc.
- Auto-picks the first file in `sample_assets/optional/start_image/`

### End Image
A still image used for the **last (trailing) segment**. Useful when your audio has one more segment than you have panels (common in Mode 2 where N tones produce N+1 segments). When ticked:
- All your `panel-*.png` images keep their normal slots
- The trailing segment maps to the End Image
- Auto-picks the first file in `sample_assets/optional/end_image/`

### Counting reference

| Setup | Mode 2 tones | Segments | Image slots needed |
|---|---|---|---|
| Plain (no extras) | N − 1 | N | N panels |
| + End Image | N | N + 1 | N panels + 1 end |
| + Start Image | N | N + 1 | 1 start + N panels |
| + Start Image + End Image | N + 1 | N + 2 | 1 start + N panels + 1 end |

### Text overlays (separate from Extras)

| Checkbox | Effect |
|---|---|
| Scrolling ticker at the bottom | Right-to-left looping text, semi-transparent black box behind |
| Static text on the right side | Vertically centered, white with black outline |
| Text input | What gets displayed (default `Varabm VoiceOver`) |

---

## What you do (recording side)

1. Install any 1020 Hz tone-generator app on your phone:
   - **Android:** "Frequency Sound Generator" (free)
   - **iOS:** "Tone Generator" (free)
   - **Web:** [onlinetonegenerator.com](https://onlinetonegenerator.com/) — set to 1020 Hz
2. Set the frequency to **1020 Hz** at a comfortable volume.
3. Record voice continuously. At each segment boundary, hold your phone near the mic and play the tone for **0.5–1 second**.
4. Stop and save.

> **Tip:** the tone doesn't need to be perfectly steady — the detector merges brief flickers within 500 ms.

---

## Run it

### GUI (default)

```bash
python video_toolkit/tone_video_creator.py
```

The window opens; everything is ticking checkboxes and pressing **▶ Convert**.

### CLI

```bash
# Basic Mode 2 with no extras
python video_toolkit/tone_video_creator.py --audio your.mp3 --mode 2

# Mode 2 with End Image enabled
python video_toolkit/tone_video_creator.py --audio your.mp3 --mode 2 --end-image sample_assets/optional/end_image/disclaimer.jpg

# Quieter source — boost voice 30% and render at 720p
python video_toolkit/tone_video_creator.py --audio your.mp3 --mode 2 --volume-boost 30 --resolution 720p

# All extras + both text overlays + boost + 1440p
python video_toolkit/tone_video_creator.py --audio your.mp3 --mode 2 \
    --start-video sample_assets/optional/start_video/intro.mp4 \
    --start-image sample_assets/optional/start_image/disclaimer.png \
    --end-image sample_assets/optional/end_image/outro.jpg \
    --scroll --side --text "My Channel" \
    --volume-boost 25 --resolution 1440p
```

### CLI flags

| Flag | Default | Meaning |
|---|---|---|
| `--audio path` | most recent in workspace | Path to your audio |
| `--images path` | `section-…/` | Folder with photos |
| `--mode N` | `pre-tone` | `1` (or `pre-tone`) for Mode 1; `2` (or `separator`) for Mode 2 |
| `--freq N` | `1020` | Tone frequency in Hz |
| `--threshold N` | `0.30` | Energy ratio (0–1); lower = more sensitive |
| `--volume-boost N` | `0` | Voice boost in % (0–100). 0 = no change, 50 = +6 dB, 100 = +12 dB. May clip if source is already loud — watch the log. |
| `--resolution KEY` | `1080p` | Output resolution. One of `360p`, `480p`, `720p`, `1080p`, `1440p`, `2160p` |
| `--scroll` | off | Add scrolling ticker at bottom |
| `--side` | off | Add static text on right side |
| `--text "..."` | `Varabm VoiceOver` | Text shown in overlays |
| `--start-video path` | none | Intro video file (will be re-encoded to match) |
| `--start-image path` | none | Image used for segment 1 (shifts panels by one) |
| `--end-image path` | none | Image used for the trailing segment |

> Unknown flags now print a `WARNING: unknown argument` line instead of being silently ignored — handy for catching typos like `--theshold`.

---

## Tuning the threshold

Use the diagnostic to see how many tones each threshold finds **without rendering**:

```bash
python tests/_tone_probe.py
```

Look for a **plateau** — a range of thresholds where the count stays the same:

```
ratio > 0.20  ->  33 beeps
ratio > 0.30  ->  33 beeps   ← plateau, use this
ratio > 0.40  ->  40 beeps   ← starts catching false positives
```

---

## Pros and cons

| Pros | Cons |
|---|---|
| Deterministic — no AI, no model | Requires playing a tone during recording |
| Works in any environment, any noise | One extra device (phone with tone app) |
| Survives terrible audio quality | Recording must include the tones |
| Tone is fully muted in output | Mode (1 vs 2) must match recording structure |
| Internal silences auto-removed | Aggressive 400 ms threshold could clip very long pauses |
| GUI checkboxes for every option | — |

---

## Comparison with [Approach 1 — VAD](APPROACH_VAD.md)

| | VAD | Tone |
|---|---|---|
| Manual action per segment | None (just pause) | Tap to play tone |
| Deterministic? | No (neural model) | Yes (FFT) |
| Tone audible in output? | n/a | No — automatically muted |
| Works on existing recordings? | Any audio with pauses | Only if tones were recorded |
| Handles 200-segment batches reliably? | With strict pause discipline | Yes — tone is unambiguous |

For high-volume / mission-critical work, **Approach 2 (tone)** is the safer bet — both because tone detection is deterministic and because the tone-strip + silence-tighten + Extras combo gives you complete control over edge cases.
