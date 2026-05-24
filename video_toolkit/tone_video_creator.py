#!/usr/bin/env python3
"""
Tone Video Creator (Approach 2)
================================

Splits audio into segments using 1020 Hz tone markers, then pairs each
segment with a photo and renders a 1080p MP4.

Two recording modes
-------------------
MODE 1 — `--mode 1` (alias of `--mode pre-tone`)
    Audio STARTS with a tone. Each tone marks the start of a segment.
    Pattern: [tone] [seg1] [tone] [seg2] ... [tone] [segN]
    N tones -> N segments. Anything before the first tone is discarded.

MODE 2 — `--mode 2` (alias of `--mode separator`)
    Audio starts and ends with TALKING (no tones at the edges).
    Tones are dividers between adjacent segments.
    Pattern: [seg1] [tone] [seg2] [tone] ... [tone] [segN]
    N tones -> N+1 segments. For 33 panels you'd play 32 tones.

Audio cleanup applied to BOTH modes
-----------------------------------
- 1020 Hz tone is fully muted (segment edges are shrunk inward by 80 ms
  so no beep ever bleeds in).
- Internal silences longer than 400 ms are removed. When one voice ends,
  the next voice starts — no audible dead air, no audible beeps.

Optional text overlays (off by default — add the flags to turn them on)
---------------------------------------------------------------------
  --scroll       Right-to-left scrolling ticker at the bottom (loops continuously)
  --side         Static text on the right edge, vertically centred
  --text "..."   Text to display (default: "Varabm VoiceOver")

Run
---
GUI (default — when launched with no args):
    python tone_video_creator.py

CLI:
    python tone_video_creator.py --audio your_recording.mp3 --mode 2
    python tone_video_creator.py --audio your_recording.mp3 --mode 2 --scroll --side
    python tone_video_creator.py --audio your_recording.mp3 --mode 1 --threshold 0.30

Output is written to the workspace as `video_tone_<timestamp>.mp4`.
"""

import os
import re
import sys
import shutil
import subprocess
import tempfile
import threading
import queue
from datetime import datetime
from pathlib import Path

import numpy as np

import tkinter as tk
from tkinter import filedialog, messagebox

import customtkinter as ctk
try:
    from .paths import (
        DEFAULT_IMAGES,
        END_IMAGE_DIR,
        PROJECT_ROOT,
        SAMPLE_AUDIO_DIR,
        START_IMAGE_DIR,
        START_VIDEO_DIR,
        ensure_output_dir,
    )
except ImportError:
    from paths import (
        DEFAULT_IMAGES,
        END_IMAGE_DIR,
        PROJECT_ROOT,
        SAMPLE_AUDIO_DIR,
        START_IMAGE_DIR,
        START_VIDEO_DIR,
        ensure_output_dir,
    )

# Reuse helpers from the main script (ffmpeg discovery, render+concat, etc.)
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from simple_video_creator import (  # noqa: E402
    AudioSegment,
    IMG_EXTS,
    VIDEO_W,
    VIDEO_H,
    FPS,
    _render_and_concat,
    _safe_console,
    compress_internal_silences,
    configure_pydub,
    find_ffmpeg,
)
from pydub.silence import detect_nonsilent

THEMES = {
    "dark": {
        "bg": "#0F1117", "panel": "#181B25", "panel2": "#222637",
        "stroke": "#2C3145", "text": "#F5F7FA", "muted": "#8B92A6",
        "accent": "#7C5CFF", "accent2": "#22D3EE", "accent3": "#F472B6",
        "danger": "#F43F5E", "ok": "#34D399",
    },
    "light": {
        "bg": "#F5F6FB", "panel": "#FFFFFF", "panel2": "#EEF0F8",
        "stroke": "#D9DDEA", "text": "#0F1117", "muted": "#5C6478",
        "accent": "#6D4AFF", "accent2": "#0891B2", "accent3": "#DB2777",
        "danger": "#E11D48", "ok": "#059669",
    },
}


def hex_lerp(c1: str, c2: str, t: float) -> str:
    t = max(0.0, min(1.0, t))
    r1, g1, b1 = int(c1[1:3], 16), int(c1[3:5], 16), int(c1[5:7], 16)
    r2, g2, b2 = int(c2[1:3], 16), int(c2[3:5], 16), int(c2[5:7], 16)
    return f"#{int(r1+(r2-r1)*t):02x}{int(g1+(g2-g1)*t):02x}{int(b1+(b2-b1)*t):02x}"


WORKSPACE = PROJECT_ROOT

VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}


def first_file_in(folder, exts):
    """Return the first file in `folder` whose extension is in `exts`, else None."""
    if not Path(folder).is_dir():
        return None
    matches = sorted(p for p in Path(folder).iterdir() if p.is_file() and p.suffix.lower() in exts)
    return str(matches[0]) if matches else None


def reencode_to_target(input_video, output_path, ffmpeg, log=print,
                       width=VIDEO_W, height=VIDEO_H, fps=FPS):
    """Re-encode `input_video` so it matches the rest of the pipeline's clips
    (target W x H, H.264 yuv420p `fps` fps, AAC 192k 48kHz). Returns True on success."""
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease:flags=lanczos,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"setsar=1"
    )
    cmd = [
        ffmpeg, "-y", "-loglevel", "error",
        "-i", str(input_video),
        "-vf", vf,
        "-r", str(fps),
        "-c:v", "libx264", "-crf", "18", "-preset", "medium",
        "-pix_fmt", "yuv420p",
        # Map only first audio stream if present, generate silent audio if missing
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart",
        str(output_path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        # Retry with silent audio added (in case the input video had no audio track)
        log(f"  reencode hit error, retrying with silent audio: {res.stderr.strip()[:120]}")
        silent_cmd = [
            ffmpeg, "-y", "-loglevel", "error",
            "-i", str(input_video),
            "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
            "-vf", vf,
            "-r", str(fps),
            "-c:v", "libx264", "-crf", "18", "-preset", "medium",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k",
            "-shortest",
            "-movflags", "+faststart",
            str(output_path),
        ]
        res2 = subprocess.run(silent_cmd, capture_output=True, text=True)
        if res2.returncode != 0:
            log(f"  reencode failed: {res2.stderr.strip()[:200]}")
            return False
    return True


# ---------- Output resolution + voice-boost ----------
RESOLUTION_PRESETS = {
    "360p":  (640,  360),
    "480p":  (854,  480),
    "720p":  (1280, 720),
    "1080p": (1920, 1080),
    "1440p": (2560, 1440),
    "2160p": (3840, 2160),
}
DEFAULT_RESOLUTION_KEY = "1080p"

# Slider 0..100 maps linearly to 0..VOICE_BOOST_MAX_DB.
# +12 dB ≈ 4× amplitude (~2× perceived loudness). Above this risks clipping.
VOICE_BOOST_MAX_DB = 12.0


def boost_pct_to_db(pct):
    pct = max(0, min(100, int(pct)))
    return (pct / 100.0) * VOICE_BOOST_MAX_DB


# ---------- Tone detector defaults ----------
TONE_FREQ_HZ = 1020
TONE_SAMPLE_RATE = 8000        # downsample for fast FFT (Nyquist 4 kHz, plenty for 1020 Hz)
TONE_WINDOW_MS = 50
TONE_HOP_MS = 10
TONE_RATIO_THRESHOLD = 0.30    # tone-bin energy / total energy
TONE_MIN_DURATION_MS = 150     # ignore brief spikes shorter than this
TONE_MERGE_GAP_MS = 500        # merge tones whose gap is below this (smooths flickers)

# ---------- Cleanup behaviour (applies to BOTH modes) ----------
# Inward shrink at every tone-adjacent segment edge — guarantees the 1020 Hz
# tone never bleeds into the segment audio (so the listener never hears a beep).
TONE_AVOID_MS = 80
# Maximum silence we keep inside a segment. Anything longer is removed so that
# "when my voice ends, the next voice starts" — no dead air, no audible gaps.
INTRA_SEGMENT_MAX_SILENCE_MS = 400
# Energy threshold for what counts as silence (in dB).
INTRA_SEGMENT_SILENCE_THRESH_DB = -40
# Padding to keep around non-silent ranges so word edges aren't clipped.
INTRA_SEGMENT_PAD_MS = 50


def detect_tones(audio_path,
                 freq_hz=TONE_FREQ_HZ,
                 sample_rate=TONE_SAMPLE_RATE,
                 window_ms=TONE_WINDOW_MS,
                 hop_ms=TONE_HOP_MS,
                 threshold=TONE_RATIO_THRESHOLD,
                 min_duration_ms=TONE_MIN_DURATION_MS,
                 merge_gap_ms=TONE_MERGE_GAP_MS,
                 log=print):
    """Find every range (in ms) where a `freq_hz` tone is present.

    Algorithm:
      1. Resample audio to `sample_rate` mono float
      2. Sliding-window FFT (`window_ms`/`hop_ms`)
      3. Per frame, compute energy in the bins around `freq_hz`
         divided by total spectral energy → 'ratio'
      4. Mark frames where ratio > threshold
      5. Group contiguous marked frames into runs, drop runs < min_duration_ms
      6. Merge runs separated by < merge_gap_ms (smooths brief drops mid-tone)
    """
    seg = (AudioSegment.from_file(audio_path)
           .set_frame_rate(sample_rate)
           .set_channels(1)
           .set_sample_width(2))
    samples = np.array(seg.get_array_of_samples(), dtype=np.float32) / 32768.0
    duration_ms = len(samples) * 1000 // sample_rate
    log(f"      audio loaded: {duration_ms/1000:.2f}s @ {sample_rate} Hz mono")

    win = int(sample_rate * window_ms / 1000)
    hop = int(sample_rate * hop_ms / 1000)
    n_frames = max(0, (len(samples) - win) // hop + 1)
    if n_frames == 0:
        return []

    starts = np.arange(n_frames) * hop
    frames = np.stack([samples[s:s + win] for s in starts]) * np.hanning(win)
    spectra = np.abs(np.fft.rfft(frames, axis=1)) ** 2
    freqs = np.fft.rfftfreq(win, 1.0 / sample_rate)
    target_bin = int(np.argmin(np.abs(freqs - freq_hz)))

    target_e = np.sum(spectra[:, max(0, target_bin - 1):target_bin + 2], axis=1)
    total_e = np.sum(spectra, axis=1) + 1e-9
    ratios = target_e / total_e
    above = ratios > threshold

    # Group contiguous frames
    runs_ms = []
    i = 0
    while i < n_frames:
        if above[i]:
            j = i
            while j < n_frames and above[j]:
                j += 1
            dur_ms = (j - i) * hop_ms
            if dur_ms >= min_duration_ms:
                runs_ms.append([i * hop_ms, j * hop_ms])
            i = j
        else:
            i += 1

    # Merge runs that are close together (the same tone briefly dropping below threshold)
    merged = []
    for start, end in runs_ms:
        if merged and start - merged[-1][1] <= merge_gap_ms:
            merged[-1][1] = end
        else:
            merged.append([start, end])
    log(f"      detected {len(merged)} tone events at {freq_hz} Hz (threshold {threshold})")
    return [(s, e) for s, e in merged]


def split_by_tones(audio, tones_ms, mode="pre-tone", avoid_ms=TONE_AVOID_MS, log=print):
    """Convert tone time-ranges into segment time-ranges, with inward shrink to avoid tone bleed.

    Modes (also accepted as '1' / '2'):
      pre-tone / 1  — Audio starts WITH a tone. Each tone marks the START of a segment.
                       Segment K = [end_of_tone_K, start_of_tone_{K+1}]
                       Trailing segment runs to end of audio.
                       N tones -> N segments.

      separator / 2 — Audio starts and ends WITHOUT a tone (talk on both edges).
                       Tones are DIVIDERS between segments.
                       Segment 1 = [audio_start, start_of_tone_1]
                       Segment K = [end_of_tone_{K-1}, start_of_tone_K]
                       Final segment = [end_of_tone_last, audio_end]
                       N tones -> N+1 segments.

      post-tone     — Mirror of pre-tone (audio ENDS with a tone).
                       Useful if the user played a tone after each segment finished.

    `avoid_ms` is shrunk inward from each TONE-adjacent edge, so a small detection
    error around the tone start/end never makes the beep audible.
    """
    if not tones_ms:
        return []
    total_ms = len(audio)
    raw = []  # (start_ms, end_ms, left_is_tone, right_is_tone)
    if mode in ("pre-tone", "1"):
        for i, (s, e) in enumerate(tones_ms):
            seg_start = e
            seg_end = tones_ms[i + 1][0] if i + 1 < len(tones_ms) else total_ms
            left_is_tone = True
            right_is_tone = (i + 1 < len(tones_ms))
            raw.append((seg_start, seg_end, left_is_tone, right_is_tone))
    elif mode == "post-tone":
        prev_end = 0
        for idx, (s, e) in enumerate(tones_ms):
            left_is_tone = (idx > 0)
            right_is_tone = True
            raw.append((prev_end, s, left_is_tone, right_is_tone))
            prev_end = e
    elif mode in ("separator", "2"):
        # First chunk: audio start (no tone) to first tone start (tone)
        raw.append((0, tones_ms[0][0], False, True))
        # Middle chunks: tone-end to next tone-start (tone on both sides)
        for i in range(len(tones_ms) - 1):
            raw.append((tones_ms[i][1], tones_ms[i + 1][0], True, True))
        # Final chunk: last tone end (tone) to audio end (no tone)
        raw.append((tones_ms[-1][1], total_ms, True, False))
    else:
        raise ValueError(f"Unknown mode: {mode}")

    # Apply inward shrink at every tone-adjacent edge; leave audio-edge boundaries alone.
    segs = []
    for s, e, left_is_tone, right_is_tone in raw:
        if left_is_tone:
            s += avoid_ms
        if right_is_tone:
            e -= avoid_ms
        if e - s >= 200:
            segs.append((s, e))
    log(f"      built {len(segs)} segments in '{mode}' mode (inward shrink {avoid_ms}ms at each tone edge)")
    return segs


def tighten_segment(seg_audio,
                    max_silence_ms=INTRA_SEGMENT_MAX_SILENCE_MS,
                    silence_thresh_db=INTRA_SEGMENT_SILENCE_THRESH_DB,
                    pad_ms=INTRA_SEGMENT_PAD_MS):
    """Remove all silences longer than `max_silence_ms` (including leading and trailing).
    Returns a (usually shorter) AudioSegment with no audible dead air."""
    if len(seg_audio) == 0:
        return seg_audio
    nonsilent = detect_nonsilent(
        seg_audio,
        min_silence_len=max_silence_ms,
        silence_thresh=silence_thresh_db,
    )
    if not nonsilent:
        # Whole segment was silent — keep a short tail so the photo doesn't flash by
        return seg_audio[:200]
    out = AudioSegment.empty()
    for s, e in nonsilent:
        s = max(0, s - pad_ms)
        e = min(len(seg_audio), e + pad_ms)
        out += seg_audio[s:e]
    return out


DEFAULT_OVERLAY_TEXT = "Varabm VoiceOver"
SCROLL_SPEED_PX_PER_S = 90       # how fast the bottom ticker moves; lower = slower
SCROLL_FONT_SIZE = 44
SIDE_FONT_SIZE = 32


def find_overlay_font():
    """Return the path to a usable bold font, or None."""
    candidates = [
        r"C:\Windows\Fonts\arialbd.ttf",
        r"C:\Windows\Fonts\arial.ttf",
        r"C:\Windows\Fonts\segoeuib.ttf",
        r"C:\Windows\Fonts\calibrib.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def _ffmpeg_escape_path(path):
    """Escape a Windows path for use inside an ffmpeg filter graph."""
    # Forward slashes are safer than backslashes inside filter expressions.
    p = path.replace("\\", "/")
    # Colons are filter-option separators; escape them.
    p = p.replace(":", r"\:")
    return p


def _ffmpeg_escape_text(text):
    """Escape text for the drawtext `text=` option."""
    # Backslash first, then colons / single-quotes
    return (text.replace("\\", "\\\\")
                .replace(":", r"\:")
                .replace("'", r"\'"))


def burn_in_text(input_path, output_path, ffmpeg,
                 scroll_text=None, side_text=None,
                 scroll_speed=SCROLL_SPEED_PX_PER_S,
                 height=VIDEO_H,
                 log=print):
    """Re-encode `input_path` into `output_path` with optional text overlays.
      - scroll_text: if set, a right-to-left looping ticker is drawn at the bottom
      - side_text:   if set, static text is drawn on the right edge, vertically centered
      - height:      output video height; font sizes scale proportionally so overlays
                     stay readable at 360p as well as 2160p
    Returns True on success."""
    if not scroll_text and not side_text:
        return False  # nothing to burn

    font_path = find_overlay_font()
    if not font_path:
        log("WARNING: no system font found — skipping overlay")
        return False
    font_esc = _ffmpeg_escape_path(font_path)

    # Scale font sizes proportional to height (designed against 1080p baseline)
    scale = max(0.25, height / 1080.0)
    scroll_fs = max(12, int(round(SCROLL_FONT_SIZE * scale)))
    side_fs = max(10, int(round(SIDE_FONT_SIZE * scale)))
    bottom_pad = max(10, int(round(50 * scale)))

    filters = []
    if scroll_text:
        # Right-to-left scrolling ticker with translucent black bar behind text
        # x = w - mod(t * speed, w + tw)  → starts at x=w, decreases to x=-tw, then loops
        text_esc = _ffmpeg_escape_text(scroll_text)
        filters.append(
            f"drawtext="
            f"fontfile='{font_esc}':"
            f"text='{text_esc}':"
            f"fontsize={scroll_fs}:fontcolor=white:"
            f"borderw=3:bordercolor=black:"
            f"box=1:boxcolor=black@0.45:boxborderw=14:"
            f"y=h-{scroll_fs + bottom_pad}:"
            f"x='w-mod(t*{scroll_speed}\\,w+tw)'"
        )
    if side_text:
        text_esc = _ffmpeg_escape_text(side_text)
        filters.append(
            f"drawtext="
            f"fontfile='{font_esc}':"
            f"text='{text_esc}':"
            f"fontsize={side_fs}:fontcolor=white:"
            f"borderw=2:bordercolor=black:"
            f"box=1:boxcolor=black@0.35:boxborderw=10:"
            f"x=w-tw-30:y=(h-th)/2"
        )

    vf = ",".join(filters)
    cmd = [
        ffmpeg, "-y", "-loglevel", "error",
        "-i", str(input_path),
        "-vf", vf,
        "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-tune", "stillimage",
        "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        "-movflags", "+faststart",
        str(output_path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        log(f"ERROR: text overlay failed: {res.stderr.strip()[:300]}")
        return False
    return True


def _pair_image_list(pair):
    """Return the canonical image list for a pair, accepting both the
    new-style pair["images"] (list) and the legacy pair["image"] (scalar)
    forms. Filters out empty / None entries."""
    if "images" in pair:
        return [i for i in (pair.get("images") or []) if i]
    img = pair.get("image")
    return [img] if img else []


def _apply_preview_edits(preview_pairs, tmp_dir=None):
    """Convert (possibly-mutated) preview_pairs into a (segments, images)
    list pair that the renderer can consume.

    The preview UI may:
      - replace any image path within pair["images"] (Replace),
      - drop all images for a pair (audio-only: previous image extends),
      - set pair["audio"] = None (audio-only delete: orphan trailing
        slot from the audio-shift — skipped entirely at render),
      - drop entries from the list entirely (Delete entire segment),
      - **append** images to a pair so audio time-shares equally (req 5).

    For multi-image pairs (>= 2 images), the segment's audio is sliced
    into N equal chunks at render time and each image is paired with
    one chunk. This requires `tmp_dir` to be a writable directory; when
    omitted, a multi-image pair returns an error rather than silently
    producing a degenerate render.

    Pairs whose image list is empty fall back to the most recent non-
    empty primary image (chain-style). Pairs with audio=None are skipped.

    Returns:
      (segments, images, error)
        segments: list of {"file", "duration_ms"} matching `images` 1:1
                  — for multi-image pairs, ONE entry per image with the
                  corresponding audio chunk written to `tmp_dir`.
        images: list of resolved image paths (no Nones)
        error:  human-readable error string if rendering can't proceed,
                otherwise None.
    """
    if not preview_pairs:
        return [], [], "preview produced no segments"

    # Skip audio-less orphans up front so subsequent logic (image
    # fallback chain, all-empty checks) only considers renderable pairs.
    renderable_pairs = [p for p in preview_pairs if p.get("audio")]
    if not renderable_pairs:
        return [], [], "all audio segments were deleted — cannot render"

    # Pick a fallback image to handle the case where leading pairs have
    # no images (nothing earlier to chain back to). If the entire
    # renderable set has no images at all, refuse.
    fallback = None
    for p in renderable_pairs:
        imgs = _pair_image_list(p)
        if imgs:
            fallback = imgs[0]
            break
    if fallback is None:
        return [], [], "all images were deleted — cannot render"

    new_segments = []
    new_images = []
    prev_img = None
    for pair in renderable_pairs:
        images = _pair_image_list(pair)
        n = len(images)

        if n == 0:
            # Audio-only pair: previous image extends over this segment.
            img = prev_img or fallback
            new_images.append(img)
            new_segments.append({
                "file": pair["audio"],
                "duration_ms": pair["duration_ms"],
            })
            continue

        if n == 1:
            img = images[0]
            new_images.append(img)
            new_segments.append({
                "file": pair["audio"],
                "duration_ms": pair["duration_ms"],
            })
            prev_img = img
            continue

        # n >= 2: split audio into N chunks weighted by pair["weights"]
        # (default = uniform, all 1.0 → equal split). Weights are
        # normalised to sum=1 so a segment with weights [2, 1, 1] over
        # 60s gives the first image 30s and the others 15s each.
        if tmp_dir is None:
            return [], [], (
                "multi-image segments require tmp_dir for audio splitting "
                "(internal error — pipeline must pass a writable dir)"
            )
        try:
            seg = AudioSegment.from_file(pair["audio"])
        except Exception as e:
            return [], [], f"could not load audio for split: {e}"
        total_ms = len(seg)
        if total_ms < n:
            return [], [], "audio too short to split across images"
        weights = list(pair.get("weights") or [])
        while len(weights) < n:
            weights.append(1.0)
        sum_w = sum(weights[:n]) if sum(weights[:n]) > 0 else 1.0
        # Compute boundaries cumulatively so rounding errors stay within
        # one chunk; the final chunk takes whatever's left.
        boundaries = [0]
        for i in range(n - 1):
            cum = sum(weights[: i + 1])
            boundaries.append(int(round(total_ms * cum / sum_w)))
        boundaries.append(total_ms)
        audio_stem = Path(pair["audio"]).stem
        for i, img in enumerate(images):
            start_ms = boundaries[i]
            end_ms = boundaries[i + 1]
            if end_ms - start_ms < 1:
                # Degenerate weight produced an empty chunk — give it a
                # symbolic 1ms so we don't crash. User can fix weights.
                end_ms = start_ms + 1
            chunk = seg[start_ms:end_ms]
            chunk_path = Path(tmp_dir) / f"{audio_stem}.split{n}_{i}.wav"
            chunk.export(str(chunk_path), format="wav")
            new_images.append(img)
            new_segments.append({
                "file": str(chunk_path),
                "duration_ms": int(end_ms - start_ms),
            })
        # The "primary image" for the next pair's None-fallback chain is
        # the LAST image in this segment (it visually trails into next).
        prev_img = images[-1]

    return new_segments, new_images, None


def run_tone_pipeline(images_dir,
                      audio_path,
                      freq_hz=TONE_FREQ_HZ,
                      threshold=TONE_RATIO_THRESHOLD,
                      mode="pre-tone",
                      scroll_text=None,
                      side_text=None,
                      start_image_path=None,
                      end_image_path=None,
                      start_video_path=None,
                      volume_boost_pct=0,
                      resolution=None,
                      pause_for_preview=None,
                      log=print):
    _safe_console()
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        log("ERROR: FFmpeg not found.")
        return None
    configure_pydub(ffmpeg)

    # Output resolution — default to module 1080p if caller didn't pick one.
    out_w, out_h = resolution if resolution else (VIDEO_W, VIDEO_H)
    log(f"  output resolution: {out_w}x{out_h}")

    images_dir = Path(images_dir)
    audio_path = Path(audio_path)
    main_images = sorted(
        [str(images_dir / f) for f in os.listdir(images_dir) if Path(f).suffix.lower() in IMG_EXTS]
    )
    if not main_images:
        log("ERROR: no images.")
        return None

    # Build the effective image list: [start_image] + main + [end_image] (if those are enabled).
    images = list(main_images)
    if start_image_path:
        if not Path(start_image_path).is_file():
            log(f"ERROR: start image not found: {start_image_path}")
            return None
        images.insert(0, str(start_image_path))
        log(f"  start image: {Path(start_image_path).name} -> segment 1")
    if end_image_path:
        if not Path(end_image_path).is_file():
            log(f"ERROR: end image not found: {end_image_path}")
            return None
        images.append(str(end_image_path))
        log(f"  end image: {Path(end_image_path).name} -> last segment")

    log(f"[1/4] Loading audio: {audio_path.name}")
    audio = AudioSegment.from_file(str(audio_path))
    total_ms = len(audio)
    log(f"      length = {total_ms/1000:.2f}s")

    if volume_boost_pct and volume_boost_pct > 0:
        gain_db = boost_pct_to_db(volume_boost_pct)
        peak_before = audio.max_dBFS
        audio = audio + gain_db
        peak_after = peak_before + gain_db
        warn = " — WILL CLIP, expect distortion" if peak_after > 0 else ""
        log(f"      voice boost: +{volume_boost_pct}% -> +{gain_db:.1f} dB "
            f"(peak {peak_before:.1f} -> {peak_after:.1f} dBFS{warn})")

    log(f"[2/4] Detecting {freq_hz} Hz tone markers")
    tones = detect_tones(str(audio_path), freq_hz=freq_hz, threshold=threshold, log=log)
    if not tones:
        log("ERROR: no tones detected — try lowering --threshold.")
        return None

    log(f"[3/4] Building segments ({mode}) and pairing with photos")
    seg_ranges = split_by_tones(audio, tones, mode=mode, log=log)
    if not seg_ranges:
        log("ERROR: no usable segments built from tones.")
        return None

    n_imgs = len(images)
    n_segs = len(seg_ranges)
    n_pairs = min(n_imgs, n_segs)
    if n_imgs != n_segs:
        if n_segs > n_imgs:
            log(f"      more segments ({n_segs}) than images ({n_imgs}) — last image holds for the trailing {n_segs - n_imgs + 1} segments")
        else:
            log(f"      more images ({n_imgs}) than segments ({n_segs}) — using only first {n_pairs} images")

    tmp = Path(tempfile.mkdtemp(prefix="svc_tone_"))
    seg_dir = tmp / "segments"
    seg_dir.mkdir()

    segments = []
    for i in range(n_pairs):
        if i == n_pairs - 1 and n_segs > n_imgs:
            # Last image absorbs all trailing segments. Tighten each before joining
            # so internal silences vanish even at the seam.
            merged = AudioSegment.empty()
            for j in range(i, n_segs):
                s_ms, e_ms = seg_ranges[j]
                piece = tighten_segment(audio[s_ms:e_ms])
                merged += piece
            seg_path = seg_dir / f"seg_{i:03d}.wav"
            merged.export(str(seg_path), format="wav")
            segments.append({"file": str(seg_path), "duration_ms": len(merged)})
            raw_total = sum((seg_ranges[j][1] - seg_ranges[j][0]) for j in range(i, n_segs))
            log(f"  segment {i+1}: merged {n_segs - i} raw {raw_total/1000:.2f}s -> tight {len(merged)/1000:.2f}s")
        else:
            s_ms, e_ms = seg_ranges[i]
            raw = audio[s_ms:e_ms]
            piece = tighten_segment(raw)
            seg_path = seg_dir / f"seg_{i:03d}.wav"
            piece.export(str(seg_path), format="wav")
            segments.append({"file": str(seg_path), "duration_ms": len(piece)})
            log(f"  segment {i+1}: raw {len(raw)/1000:.2f}s -> tight {len(piece)/1000:.2f}s")

    # Optional preview step — let the caller verify (image, audio) pairs
    # before we commit to the (slow) MP4 render. The callback is blocking;
    # returning False means the user cancelled and we should bail. The
    # callback may also MUTATE pair["audio"]/pair["duration_ms"] and/or
    # pair["images"] (replace / add another / swap / delete) before
    # returning; we rebuild segments + images from preview_pairs after.
    if pause_for_preview is not None:
        preview_pairs = [
            {
                "images": [images[i]],
                "audio": seg["file"],
                "duration_ms": seg["duration_ms"],
                "index": i + 1,
            }
            for i, seg in enumerate(segments)
        ]
        log("[3.5] Preview — verify each segment matches its image, then click Continue")
        try:
            proceed = pause_for_preview(preview_pairs)
        except Exception as e:
            log(f"  preview hook raised — proceeding with render: {e}")
            proceed = True
        if not proceed:
            log("Cancelled by user before render — no output produced.")
            shutil.rmtree(tmp, ignore_errors=True)
            return "CANCELLED"

        # Apply mutations from the preview UI. Mutations include: audio
        # edits/replacements, image replacements, image-only deletions
        # (None → prev image extends), full-segment deletions (entries
        # removed), audio-only deletions (audio shifted up, trailing
        # orphan slot), and multi-image expansion (per-segment audio
        # time-shares equally across N images at render time — that's
        # what tmp/ is for: the splitter writes chunk wavs there).
        original_segs = list(segments)
        original_imgs = list(images[:n_pairs])
        new_segments, new_images, err = _apply_preview_edits(
            preview_pairs, tmp_dir=str(seg_dir),
        )
        if err:
            log(f"ERROR after preview: {err}")
            shutil.rmtree(tmp, ignore_errors=True)
            return None
        # Diagnostics — log what changed so the run log is informative.
        n_after = len(new_segments)
        n_orig_pairs = len(original_segs)
        n_preview_pairs = len(preview_pairs)
        n_deleted = n_orig_pairs - n_preview_pairs  # full-segment removals
        n_image_subs = sum(
            1 for p in preview_pairs
            if not (p.get("images") or [p.get("image")] if p.get("image") else [])
        )
        n_audio_orphans = sum(
            1 for p in preview_pairs if p.get("audio") is None
        )
        n_multi_images = sum(
            1 for p in preview_pairs
            if (p.get("images") and len(p["images"]) >= 2)
        )
        # n_audio_edits = renderable pairs whose audio path no longer matches
        # the original at the same position. After audio-shift the whole
        # tail moves, so this count is upper-bound but informative.
        n_audio_edits = 0
        for i in range(min(n_preview_pairs, n_orig_pairs)):
            preview_audio = preview_pairs[i].get("audio")
            if preview_audio and preview_audio != original_segs[i]["file"]:
                n_audio_edits += 1
        if (n_deleted or n_audio_edits or n_image_subs or n_audio_orphans
                or n_multi_images):
            log(
                f"  preview edits applied: "
                f"{n_audio_edits} audio edit(s)/replace(s), "
                f"{n_multi_images} multi-image segment(s) (audio split equally), "
                f"{n_image_subs} image-only delete(s), "
                f"{n_audio_orphans} audio-only delete(s) (skipped at render), "
                f"{n_deleted} full segment delete(s)"
            )
        segments = new_segments
        images = new_images
        n_pairs = n_after

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    out_name = f"video_tone_{timestamp}.mp4"
    output_dir = ensure_output_dir()
    out_path = output_dir / out_name

    has_overlay = bool(scroll_text or side_text)
    base_path = (output_dir / f"_tonebase_{timestamp}.mp4") if has_overlay else out_path

    # If a start video was provided, re-encode it to match our target format and
    # prepend it to the concat list so it plays first.
    prepend_clips = []
    intro_clip_path = None
    if start_video_path:
        if not Path(start_video_path).is_file():
            log(f"ERROR: start video not found: {start_video_path}")
            shutil.rmtree(tmp, ignore_errors=True)
            return None
        intro_clip_path = tmp / "_intro.mp4"
        log(f"  start video: re-encoding {Path(start_video_path).name} to match target format")
        if reencode_to_target(start_video_path, intro_clip_path, ffmpeg, log=log,
                              width=out_w, height=out_h):
            prepend_clips.append(str(intro_clip_path))
            log(f"  start video ready: prepended to final concat")
        else:
            log("  WARNING: start video re-encode failed; continuing without it")

    log(f"[4/{5 if has_overlay else 4}] Rendering {n_pairs} clips and joining…")
    ok = _render_and_concat(
        ffmpeg, segments, images[:n_pairs], str(base_path), log=log,
        prepend_clips=prepend_clips,
        width=out_w, height=out_h,
    )
    shutil.rmtree(tmp, ignore_errors=True)
    if not ok:
        if has_overlay and base_path.exists():
            base_path.unlink()
        return None

    if has_overlay:
        log(f"[5/5] Burning in text overlays (scroll={bool(scroll_text)}, side={bool(side_text)})")
        ok = burn_in_text(
            str(base_path), str(out_path), ffmpeg,
            scroll_text=scroll_text, side_text=side_text, height=out_h, log=log,
        )
        base_path.unlink(missing_ok=True)
        if not ok:
            log("ERROR: text overlay step failed; no output produced.")
            return None

    size_mb = out_path.stat().st_size / (1024 * 1024)
    log(f"DONE: {out_path}  ({size_mb:.1f} MB)")
    return out_path


class ToneVideoApp:
    """tkinter GUI wrapping run_tone_pipeline. Same options as the CLI flags,
    rendered as widgets the user can tick or fill in."""

    STAGE_PROGRESS = {
        "[1/4]": 8,   "[1/5]": 6,
        "[2/4]": 22,  "[2/5]": 18,
        "[3/4]": 38,  "[3/5]": 30,
        "[4/4]": 95,  "[4/5]": 85,
        "[5/5]": 95,
    }

    def __init__(self, root):
        self.root = root
        self.root.title("Tone Video Creator (Approach 2)")
        self.T = THEMES["dark"]
        self.root.configure(fg_color=self.T["bg"])
        try:
            screen_h = root.winfo_screenheight()
        except Exception:
            screen_h = 1080
        default_h = min(980, max(720, int(screen_h * 0.92) - 40))
        self.root.geometry(f"880x{default_h}")
        self.root.minsize(800, 620)

        # Pipeline inputs
        self.images_dir = tk.StringVar(value=str(DEFAULT_IMAGES) if DEFAULT_IMAGES.is_dir() else "")
        self.audio_file = tk.StringVar()

        # Mode + tone tuning
        self.mode_var = tk.IntVar(value=2)
        self.freq_var = tk.IntVar(value=TONE_FREQ_HZ)
        self.threshold_var = tk.DoubleVar(value=TONE_RATIO_THRESHOLD)

        # Text overlays
        self.scroll_enabled = tk.BooleanVar(value=False)
        self.side_enabled = tk.BooleanVar(value=False)
        self.overlay_text = tk.StringVar(value=DEFAULT_OVERLAY_TEXT)

        # Optional extras: start image, end image, start video. Each is a
        # checkbox + path; auto-detected from workspace folders if present.
        self.start_image_enabled = tk.BooleanVar(value=False)
        self.end_image_enabled = tk.BooleanVar(value=False)
        self.start_video_enabled = tk.BooleanVar(value=False)
        self.start_image_path = tk.StringVar(value=first_file_in(START_IMAGE_DIR, IMG_EXTS) or "")
        self.end_image_path = tk.StringVar(value=first_file_in(END_IMAGE_DIR, IMG_EXTS) or "")
        self.start_video_path = tk.StringVar(value=first_file_in(START_VIDEO_DIR, VIDEO_EXTS) or "")

        # Audio + output settings
        self.volume_boost_var = tk.IntVar(value=0)
        self.resolution_var = tk.StringVar(value=DEFAULT_RESOLUTION_KEY)

        # Run state
        self.is_running = False
        self.log_queue = queue.Queue()
        self.ffmpeg = find_ffmpeg()
        configure_pydub(self.ffmpeg)

        self._render_clip_re = re.compile(r"\[(\d+)/(\d+)\]\s+panel-")

        self._build_ui()
        self._poll_log()

        if self.ffmpeg:
            self._log(f"FFmpeg: {self.ffmpeg}")
        else:
            self._log("WARNING: FFmpeg not found on PATH. Convert will fail.")

        # Auto-pick most recent audio in workspace if none chosen yet
        candidates = sorted(
            [p for folder in (SAMPLE_AUDIO_DIR, WORKSPACE) if folder.is_dir()
             for p in folder.iterdir()
             if p.suffix.lower() in {".mp3", ".wav", ".m4a", ".flac", ".ogg"}],
            key=lambda p: p.stat().st_mtime, reverse=True,
        )
        if candidates:
            self.audio_file.set(str(candidates[0]))
            self._log(f"Audio auto-selected: {candidates[0].name}")

    # ---------- UI ----------
    def _build_ui(self):
        T = self.T
        outer = ctk.CTkFrame(self.root, fg_color=T["bg"])
        outer.pack(fill="both", expand=True, padx=14, pady=14)

        # ───────── Bottom-anchored action area ─────────
        # Log / Progress / Buttons are packed side="bottom" first so they
        # are always visible regardless of form scroll position.

        sec_log = ctk.CTkFrame(outer, fg_color=T["panel"], corner_radius=12,
                                border_width=1, border_color=T["stroke"])
        sec_log.pack(side="bottom", fill="x", pady=(8, 0))
        ctk.CTkLabel(sec_log, text="Log",
                      font=ctk.CTkFont("Segoe UI", 11, weight="bold"),
                      text_color=T["accent"]).pack(anchor="w", padx=14, pady=(10, 4))
        self.log_text = ctk.CTkTextbox(
            sec_log, height=140, font=ctk.CTkFont("Consolas", 10),
            fg_color=T["panel2"], text_color=T["text"], wrap="word", corner_radius=8,
        )
        self.log_text.pack(fill="x", padx=12, pady=(0, 12))

        sec_prog = ctk.CTkFrame(outer, fg_color=T["panel"], corner_radius=12,
                                 border_width=1, border_color=T["stroke"])
        sec_prog.pack(side="bottom", fill="x", pady=(8, 0))
        ctk.CTkLabel(sec_prog, text="Progress",
                      font=ctk.CTkFont("Segoe UI", 11, weight="bold"),
                      text_color=T["accent"]).pack(anchor="w", padx=14, pady=(10, 4))
        p_inner = ctk.CTkFrame(sec_prog, fg_color="transparent")
        p_inner.pack(fill="x", padx=12, pady=(0, 12))
        p_row = ctk.CTkFrame(p_inner, fg_color="transparent")
        p_row.pack(fill="x")
        p_row.columnconfigure(0, weight=1)
        self.progress = ctk.CTkProgressBar(
            p_row, progress_color=T["accent"], fg_color=T["panel2"], height=14, corner_radius=7,
        )
        self.progress.set(0)
        self.progress.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        self.percent_label = ctk.CTkLabel(
            p_row, text="0%", width=50, anchor="e",
            text_color=T["muted"], font=ctk.CTkFont("Segoe UI", 11),
        )
        self.percent_label.grid(row=0, column=1)
        self.status_label = ctk.CTkLabel(
            p_inner, text="Idle", text_color=T["muted"],
            font=ctk.CTkFont("Segoe UI", 11), anchor="w",
        )
        self.status_label.pack(anchor="w", pady=(6, 0))

        btn_row = ctk.CTkFrame(outer, fg_color="transparent")
        btn_row.pack(side="bottom", fill="x", pady=(0, 8))
        self.convert_btn = ctk.CTkButton(
            btn_row, text="▶   Convert", command=self._start, width=160,
            fg_color=T["accent"], hover_color=hex_lerp(T["accent"], "#FFFFFF", 0.15),
            text_color="#FFFFFF", font=ctk.CTkFont("Segoe UI", 13, weight="bold"),
        )
        self.convert_btn.pack(side="left")
        ctk.CTkButton(
            btn_row, text="📂  Open workspace", command=self._open_workspace, width=170,
            fg_color=T["panel2"], hover_color=T["stroke"],
            text_color=T["text"], border_width=1, border_color=T["stroke"],
        ).pack(side="right")

        # ───────── Scrollable form (sections 1–6) ─────────
        form = ctk.CTkScrollableFrame(
            outer, fg_color=T["bg"],
            scrollbar_fg_color=T["panel"],
            scrollbar_button_color=T["stroke"],
            scrollbar_button_hover_color=T["accent"],
        )
        form.pack(side="top", fill="both", expand=True, pady=(0, 8))

        # 1. Files
        sec1 = ctk.CTkFrame(form, fg_color=T["panel"], corner_radius=12,
                             border_width=1, border_color=T["stroke"])
        sec1.pack(fill="x", pady=(0, 8))
        ctk.CTkLabel(sec1, text="1.  Pick your files",
                      font=ctk.CTkFont("Segoe UI", 11, weight="bold"),
                      text_color=T["accent"]).pack(anchor="w", padx=14, pady=(10, 4))
        src = ctk.CTkFrame(sec1, fg_color="transparent")
        src.pack(fill="x", padx=12, pady=(0, 12))
        src.columnconfigure(1, weight=1)
        ctk.CTkLabel(src, text="Images folder:", text_color=T["text"],
                      font=ctk.CTkFont("Segoe UI", 11)).grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
        ctk.CTkEntry(src, textvariable=self.images_dir, placeholder_text="Pick a folder of images…",
                      fg_color=T["panel2"], border_color=T["stroke"], text_color=T["text"],
                      placeholder_text_color=T["muted"]).grid(row=0, column=1, sticky="ew", padx=(0, 8), pady=4)
        ctk.CTkButton(src, text="Browse…", command=self._browse_images, width=90,
                       fg_color=T["panel2"], hover_color=T["stroke"], text_color=T["text"],
                       border_width=1, border_color=T["stroke"]).grid(row=0, column=2, pady=4)
        ctk.CTkLabel(src, text="Voice file:", text_color=T["text"],
                      font=ctk.CTkFont("Segoe UI", 11)).grid(row=1, column=0, sticky="w", padx=(0, 8), pady=4)
        ctk.CTkEntry(src, textvariable=self.audio_file, placeholder_text="Pick a voice recording…",
                      fg_color=T["panel2"], border_color=T["stroke"], text_color=T["text"],
                      placeholder_text_color=T["muted"]).grid(row=1, column=1, sticky="ew", padx=(0, 8), pady=4)
        ctk.CTkButton(src, text="Browse…", command=self._browse_audio, width=90,
                       fg_color=T["panel2"], hover_color=T["stroke"], text_color=T["text"],
                       border_width=1, border_color=T["stroke"]).grid(row=1, column=2, pady=4)

        # 2. Mode
        sec2 = ctk.CTkFrame(form, fg_color=T["panel"], corner_radius=12,
                             border_width=1, border_color=T["stroke"])
        sec2.pack(fill="x", pady=(0, 8))
        ctk.CTkLabel(sec2, text="2.  Recording mode",
                      font=ctk.CTkFont("Segoe UI", 11, weight="bold"),
                      text_color=T["accent"]).pack(anchor="w", padx=14, pady=(10, 4))
        mode_inner = ctk.CTkFrame(sec2, fg_color="transparent")
        mode_inner.pack(fill="x", padx=12, pady=(0, 12))
        ctk.CTkRadioButton(
            mode_inner,
            text="Mode 1 — Audio starts WITH a tone   (N tones → N segments)",
            variable=self.mode_var, value=1,
            text_color=T["text"], fg_color=T["accent"], hover_color=T["accent2"],
            font=ctk.CTkFont("Segoe UI", 12),
        ).pack(anchor="w", pady=4)
        ctk.CTkRadioButton(
            mode_inner,
            text="Mode 2 — Audio bookended with TALKING, tones in between   (N tones → N+1 segments)",
            variable=self.mode_var, value=2,
            text_color=T["text"], fg_color=T["accent"], hover_color=T["accent2"],
            font=ctk.CTkFont("Segoe UI", 12),
        ).pack(anchor="w", pady=4)

        # 3. Tone tuning
        sec3 = ctk.CTkFrame(form, fg_color=T["panel"], corner_radius=12,
                             border_width=1, border_color=T["stroke"])
        sec3.pack(fill="x", pady=(0, 8))
        ctk.CTkLabel(sec3, text="3.  Tone tuning  (defaults are good)",
                      font=ctk.CTkFont("Segoe UI", 11, weight="bold"),
                      text_color=T["accent"]).pack(anchor="w", padx=14, pady=(10, 4))
        tune = ctk.CTkFrame(sec3, fg_color="transparent")
        tune.pack(fill="x", padx=12, pady=(0, 12))
        ctk.CTkLabel(tune, text="Frequency (Hz):", text_color=T["text"],
                      font=ctk.CTkFont("Segoe UI", 11)).grid(row=0, column=0, sticky="w", padx=(0, 6))
        ctk.CTkEntry(tune, textvariable=self.freq_var, width=90,
                      fg_color=T["panel2"], border_color=T["stroke"], text_color=T["text"],
                      ).grid(row=0, column=1, sticky="w", padx=(0, 20))
        ctk.CTkLabel(tune, text="Threshold (0 – 1):", text_color=T["text"],
                      font=ctk.CTkFont("Segoe UI", 11)).grid(row=0, column=2, sticky="w", padx=(0, 6))
        ctk.CTkEntry(tune, textvariable=self.threshold_var, width=90,
                      fg_color=T["panel2"], border_color=T["stroke"], text_color=T["text"],
                      ).grid(row=0, column=3, sticky="w")

        # 4. Audio & output
        sec4 = ctk.CTkFrame(form, fg_color=T["panel"], corner_radius=12,
                             border_width=1, border_color=T["stroke"])
        sec4.pack(fill="x", pady=(0, 8))
        ctk.CTkLabel(sec4, text="4.  Audio & output",
                      font=ctk.CTkFont("Segoe UI", 11, weight="bold"),
                      text_color=T["accent"]).pack(anchor="w", padx=14, pady=(10, 4))
        ao = ctk.CTkFrame(sec4, fg_color="transparent")
        ao.pack(fill="x", padx=12, pady=(0, 12))
        ao.columnconfigure(1, weight=1)

        ctk.CTkLabel(ao, text="Voice boost:", text_color=T["text"],
                      font=ctk.CTkFont("Segoe UI", 11)).grid(row=0, column=0, sticky="w", padx=(0, 10))
        ctk.CTkSlider(
            ao, from_=0, to=100, number_of_steps=100,
            variable=self.volume_boost_var,
            command=lambda _v: self._update_boost_label(),
            progress_color=T["accent"], fg_color=T["panel2"],
            button_color=T["accent"], button_hover_color=T["accent2"],
        ).grid(row=0, column=1, sticky="ew", padx=(0, 10))
        self.boost_label = ctk.CTkLabel(
            ao, text="0% (no change)", width=170, anchor="w",
            text_color=T["muted"], font=ctk.CTkFont("Segoe UI", 11),
        )
        self.boost_label.grid(row=0, column=2, sticky="w")

        ctk.CTkLabel(ao, text="Output quality:", text_color=T["text"],
                      font=ctk.CTkFont("Segoe UI", 11)).grid(row=1, column=0, sticky="w", padx=(0, 10), pady=(10, 0))
        default_label = next(
            (f"{k} ({w}×{h})" for k, (w, h) in RESOLUTION_PRESETS.items() if k == DEFAULT_RESOLUTION_KEY),
            "1080p (1920×1080)",
        )
        self.resolution_var.set(default_label)
        ctk.CTkComboBox(
            ao, variable=self.resolution_var, state="readonly",
            values=[f"{k} ({w}×{h})" for k, (w, h) in RESOLUTION_PRESETS.items()],
            width=240,
            fg_color=T["panel2"], border_color=T["stroke"], text_color=T["text"],
            button_color=T["stroke"], button_hover_color=T["accent"],
            dropdown_fg_color=T["panel2"], dropdown_text_color=T["text"],
            dropdown_hover_color=T["accent"],
        ).grid(row=1, column=1, columnspan=2, sticky="w", pady=(10, 0))

        # 5. Text overlays
        sec5 = ctk.CTkFrame(form, fg_color=T["panel"], corner_radius=12,
                             border_width=1, border_color=T["stroke"])
        sec5.pack(fill="x", pady=(0, 8))
        ctk.CTkLabel(sec5, text="5.  Text overlays  (tick to include)",
                      font=ctk.CTkFont("Segoe UI", 11, weight="bold"),
                      text_color=T["accent"]).pack(anchor="w", padx=14, pady=(10, 4))
        ov = ctk.CTkFrame(sec5, fg_color="transparent")
        ov.pack(fill="x", padx=12, pady=(0, 12))
        ov.columnconfigure(1, weight=1)
        ctk.CTkCheckBox(
            ov, text="Scrolling ticker at the bottom (right → left, looping)",
            variable=self.scroll_enabled,
            text_color=T["text"], fg_color=T["accent"], hover_color=T["accent2"],
            font=ctk.CTkFont("Segoe UI", 12),
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=3)
        ctk.CTkCheckBox(
            ov, text="Static text on the right side (vertically centred)",
            variable=self.side_enabled,
            text_color=T["text"], fg_color=T["accent"], hover_color=T["accent2"],
            font=ctk.CTkFont("Segoe UI", 12),
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=3)
        ctk.CTkLabel(ov, text="Text:", text_color=T["muted"],
                      font=ctk.CTkFont("Segoe UI", 11)).grid(row=2, column=0, sticky="w", padx=(0, 8), pady=(6, 0))
        ctk.CTkEntry(ov, textvariable=self.overlay_text,
                      fg_color=T["panel2"], border_color=T["stroke"], text_color=T["text"],
                      ).grid(row=2, column=1, sticky="ew", pady=(6, 0))

        # 6. Extras
        sec6 = ctk.CTkFrame(form, fg_color=T["panel"], corner_radius=12,
                             border_width=1, border_color=T["stroke"])
        sec6.pack(fill="x", pady=(0, 8))
        ctk.CTkLabel(sec6, text="6.  Extras  (tick to include)",
                      font=ctk.CTkFont("Segoe UI", 11, weight="bold"),
                      text_color=T["accent"]).pack(anchor="w", padx=14, pady=(10, 4))
        ex = ctk.CTkFrame(sec6, fg_color="transparent")
        ex.pack(fill="x", padx=12, pady=(0, 12))
        ex.columnconfigure(2, weight=1)

        def add_extra_row(row, label, var_enabled, var_path, browse_cmd):
            ctk.CTkCheckBox(
                ex, text=label, variable=var_enabled,
                text_color=T["text"], fg_color=T["accent"], hover_color=T["accent2"],
                font=ctk.CTkFont("Segoe UI", 12),
            ).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=3)
            ctk.CTkEntry(
                ex, textvariable=var_path,
                fg_color=T["panel2"], border_color=T["stroke"], text_color=T["text"],
                placeholder_text_color=T["muted"],
            ).grid(row=row, column=1, columnspan=2, sticky="ew", padx=(0, 8), pady=3)
            ctk.CTkButton(
                ex, text="Browse…", command=browse_cmd, width=90,
                fg_color=T["panel2"], hover_color=T["stroke"],
                text_color=T["text"], border_width=1, border_color=T["stroke"],
            ).grid(row=row, column=3, pady=3)

        add_extra_row(
            0, "Start video (plays first as intro)",
            self.start_video_enabled, self.start_video_path,
            lambda: self._browse_into(self.start_video_path, "Pick start video", VIDEO_EXTS, START_VIDEO_DIR),
        )
        add_extra_row(
            1, "Start image (maps to segment 1)",
            self.start_image_enabled, self.start_image_path,
            lambda: self._browse_into(self.start_image_path, "Pick start image", IMG_EXTS, START_IMAGE_DIR),
        )
        add_extra_row(
            2, "End image (maps to last trailing segment)",
            self.end_image_enabled, self.end_image_path,
            lambda: self._browse_into(self.end_image_path, "Pick end image", IMG_EXTS, END_IMAGE_DIR),
        )

    # ---------- Helpers ----------
    def _browse_images(self):
        d = filedialog.askdirectory(title="Select images folder", initialdir=str(WORKSPACE))
        if d:
            self.images_dir.set(d)

    def _browse_into(self, target_var, title, exts, default_dir):
        types = [(",".join(exts), " ".join(f"*{e}" for e in sorted(exts))), ("All files", "*.*")]
        f = filedialog.askopenfilename(
            title=title,
            initialdir=str(default_dir if default_dir.is_dir() else WORKSPACE),
            filetypes=types,
        )
        if f:
            target_var.set(f)

    def _browse_audio(self):
        f = filedialog.askopenfilename(
            title="Select voice file",
            initialdir=str(WORKSPACE),
            filetypes=[("Audio", "*.mp3 *.wav *.m4a *.flac *.ogg"), ("All files", "*.*")],
        )
        if f:
            self.audio_file.set(f)

    def _open_workspace(self):
        if sys.platform == "win32":
            os.startfile(str(WORKSPACE))
        elif sys.platform == "darwin":
            subprocess.run(["open", str(WORKSPACE)])
        else:
            subprocess.run(["xdg-open", str(WORKSPACE)])

    def _log(self, message):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_queue.put(f"[{ts}] {message}")

    def _poll_log(self):
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self.log_text.insert("end", msg + "\n")
                self.log_text.see("end")
                self._maybe_update_progress(msg)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_log)

    def _maybe_update_progress(self, msg):
        # Big stage transitions
        for tag, pct in self.STAGE_PROGRESS.items():
            if tag in msg:
                self._set_progress(pct, msg.split("] ", 1)[-1].strip())
                return
        # Per-clip render lines like "[12/33] panel-012.png -> 5.43s ok"
        m = self._render_clip_re.search(msg)
        if m:
            cur, total = int(m.group(1)), int(m.group(2))
            base = self.STAGE_PROGRESS["[4/5]"] if (self.scroll_enabled.get() or self.side_enabled.get()) else self.STAGE_PROGRESS["[4/4]"]
            prev = self.STAGE_PROGRESS["[3/5]"] if (self.scroll_enabled.get() or self.side_enabled.get()) else self.STAGE_PROGRESS["[3/4]"]
            span = max(1, base - prev)
            pct = prev + int(cur / total * span)
            self._set_progress(pct, f"Rendering clip {cur}/{total}")

    def _set_progress(self, pct, status=None):
        pct = max(0, min(100, int(pct)))
        self.progress.set(pct / 100)
        self.percent_label.configure(text=f"{pct}%")
        if status is not None:
            self.status_label.configure(text=status)
        self.root.update_idletasks()

    def _update_boost_label(self):
        pct = int(self.volume_boost_var.get())
        if pct == 0:
            self.boost_label.config(text="0% (no change)")
        else:
            gain_db = boost_pct_to_db(pct)
            self.boost_label.config(text=f"{pct}% (+{gain_db:.1f} dB)")

    def _selected_resolution(self):
        """Return (width, height) tuple for the chosen quality dropdown entry."""
        label = self.resolution_var.get()
        # The label looks like "720p (1280×720)"; strip to the leading key.
        key = label.split()[0] if label else DEFAULT_RESOLUTION_KEY
        return RESOLUTION_PRESETS.get(key, RESOLUTION_PRESETS[DEFAULT_RESOLUTION_KEY])

    # ---------- Pipeline ----------
    def _start(self):
        if self.is_running:
            return
        if not self.images_dir.get() or not Path(self.images_dir.get()).is_dir():
            messagebox.showerror("Missing", "Pick an images folder.")
            return
        if not self.audio_file.get() or not Path(self.audio_file.get()).is_file():
            messagebox.showerror("Missing", "Pick a voice file.")
            return
        if not self.ffmpeg:
            self.ffmpeg = find_ffmpeg()
            configure_pydub(self.ffmpeg)
            if not self.ffmpeg:
                messagebox.showerror("FFmpeg missing", "FFmpeg is required.")
                return

        self.is_running = True
        self.convert_btn.configure(state="disabled")
        self.log_text.delete("0.0", "end")
        self._set_progress(0, "Starting…")

        scroll_text = self.overlay_text.get() if self.scroll_enabled.get() else None
        side_text = self.overlay_text.get() if self.side_enabled.get() else None
        mode_str = "pre-tone" if self.mode_var.get() == 1 else "separator"

        start_image = self.start_image_path.get() if self.start_image_enabled.get() else None
        end_image = self.end_image_path.get() if self.end_image_enabled.get() else None
        start_video = self.start_video_path.get() if self.start_video_enabled.get() else None

        volume_boost = int(self.volume_boost_var.get())
        resolution = self._selected_resolution()

        # Build the preview hook: invoked by the worker thread between
        # segmentation and rendering. show_preview_blocking schedules the
        # UI on the main thread and blocks until the user decides.
        from segment_preview import show_preview_blocking

        # Surface the optional Start/End images as quick replacement targets
        # in the per-segment image kebab — even when the user didn't include
        # them in the rendered chain, they're still useful as fillers.
        preview_defaults = []
        if self.start_image_path.get() and Path(self.start_image_path.get()).is_file():
            preview_defaults.append({
                "label": "Front (start) image",
                "path": self.start_image_path.get(),
            })
        if self.end_image_path.get() and Path(self.end_image_path.get()).is_file():
            preview_defaults.append({
                "label": "Back (end) image",
                "path": self.end_image_path.get(),
            })

        preview_images_dir = self.images_dir.get() or None

        def pause_for_preview(pairs):
            return show_preview_blocking(
                self.root, pairs, theme_name="dark",
                defaults=preview_defaults,
                images_dir=preview_images_dir,
            )

        threading.Thread(
            target=self._run_pipeline_safe,
            kwargs=dict(
                images=self.images_dir.get(),
                audio=self.audio_file.get(),
                mode=mode_str,
                freq=self.freq_var.get(),
                threshold=self.threshold_var.get(),
                scroll_text=scroll_text,
                side_text=side_text,
                start_image=start_image,
                end_image=end_image,
                start_video=start_video,
                volume_boost=volume_boost,
                resolution=resolution,
                pause_for_preview=pause_for_preview,
            ),
            daemon=True,
        ).start()

    def _run_pipeline_safe(self, images, audio, mode, freq, threshold,
                           scroll_text, side_text,
                           start_image=None, end_image=None, start_video=None,
                           volume_boost=0, resolution=None,
                           pause_for_preview=None):
        try:
            result = run_tone_pipeline(
                images, audio,
                freq_hz=freq, threshold=threshold, mode=mode,
                scroll_text=scroll_text, side_text=side_text,
                start_image_path=start_image,
                end_image_path=end_image,
                start_video_path=start_video,
                volume_boost_pct=volume_boost,
                resolution=resolution,
                pause_for_preview=pause_for_preview,
                log=self._log,
            )
            self.root.after(0, lambda: self._finish(result, resolution))
        except Exception as e:
            import traceback
            self._log(f"ERROR: {e}")
            self._log(traceback.format_exc())
            self.root.after(0, lambda: self._finish(None, resolution))

    def _finish(self, result, resolution=None):
        self.is_running = False
        self.convert_btn.configure(state="normal")
        if result == "CANCELLED":
            self._set_progress(0, "Cancelled — no video produced")
        elif result:
            w, h = resolution if resolution else (VIDEO_W, VIDEO_H)
            self._set_progress(100, f"Done → {Path(result).name}")
            messagebox.showinfo(
                "Video ready",
                f"Saved to:\n{result}\n\nResolution: {w}×{h}",
            )
        else:
            self._set_progress(0, "Failed — see log")


def launch_gui():
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    root = ctk.CTk()
    ToneVideoApp(root)
    root.lift()
    root.attributes("-topmost", True)
    root.after(500, lambda: root.attributes("-topmost", False))
    try:
        root.focus_force()
    except Exception:
        pass
    root.mainloop()


def main():
    args = sys.argv[1:]
    # No args at all → launch the GUI
    if not args:
        launch_gui()
        return

    images = str(DEFAULT_IMAGES)
    audio = None
    freq = TONE_FREQ_HZ
    threshold = TONE_RATIO_THRESHOLD
    mode = "pre-tone"

    overlay_text = DEFAULT_OVERLAY_TEXT
    enable_scroll = False
    enable_side = False
    start_image_path = None
    end_image_path = None
    start_video_path = None
    volume_boost = 0
    resolution = None  # None -> falls back to 1080p in run_tone_pipeline

    i = 0
    while i < len(args):
        a = args[i]
        if a == "--images":
            images = args[i + 1]; i += 2
        elif a == "--audio":
            audio = args[i + 1]; i += 2
        elif a == "--freq":
            freq = int(args[i + 1]); i += 2
        elif a == "--threshold":
            threshold = float(args[i + 1]); i += 2
        elif a == "--mode":
            raw = args[i + 1].lower()
            mode = {"1": "pre-tone", "2": "separator"}.get(raw, raw)
            i += 2
        elif a == "--scroll":
            enable_scroll = True; i += 1
        elif a == "--side":
            enable_side = True; i += 1
        elif a == "--text":
            overlay_text = args[i + 1]; i += 2
        elif a == "--start-image":
            start_image_path = args[i + 1]; i += 2
        elif a == "--end-image":
            end_image_path = args[i + 1]; i += 2
        elif a == "--start-video":
            start_video_path = args[i + 1]; i += 2
        elif a == "--volume-boost":
            volume_boost = int(args[i + 1]); i += 2
        elif a == "--resolution":
            key = args[i + 1].lower()
            if key not in RESOLUTION_PRESETS:
                print(f"ERROR: --resolution must be one of {list(RESOLUTION_PRESETS)}")
                sys.exit(1)
            resolution = RESOLUTION_PRESETS[key]
            i += 2
        else:
            print(f"WARNING: unknown argument '{a}' — ignored")
            i += 1

    scroll_text = overlay_text if enable_scroll else None
    side_text = overlay_text if enable_side else None

    if audio is None:
        # Auto-pick the most recently modified mp3/wav in workspace as a convenience
        candidates = sorted(
            [p for folder in (SAMPLE_AUDIO_DIR, WORKSPACE) if folder.is_dir()
             for p in folder.iterdir()
             if p.suffix.lower() in {".mp3", ".wav", ".m4a", ".flac", ".ogg"}],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            print("ERROR: no audio found in workspace; pass --audio path/to/file.mp3")
            sys.exit(1)
        audio = str(candidates[0])
        print(f"[auto] Using most recent audio: {Path(audio).name}")

    result = run_tone_pipeline(
        images, audio,
        freq_hz=freq, threshold=threshold, mode=mode,
        scroll_text=scroll_text, side_text=side_text,
        start_image_path=start_image_path,
        end_image_path=end_image_path,
        start_video_path=start_video_path,
        volume_boost_pct=volume_boost,
        resolution=resolution,
    )
    sys.exit(0 if result else 1)


if __name__ == "__main__":
    main()
