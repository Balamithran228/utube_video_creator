"""Diagnostic: run Silero VAD on the user's audio and report how many speech segments
we'd get for different minimum-pause thresholds. Helps decide if existing audio is usable."""
import sys
from pathlib import Path
import numpy as np
import torch
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "video_toolkit"))
from simple_video_creator import find_ffmpeg, configure_pydub
from paths import SAMPLE_AUDIO_DIR
configure_pydub(find_ffmpeg())

from pydub import AudioSegment
from silero_vad import load_silero_vad, get_speech_timestamps

import os
# Try the new audio first, fall back to the original sample
for candidate in ["mkv2 -enhanced-v2.mp3", "mk 1 enhanced-v2.mp3", "enhanced mp33.mp3"]:
    path = SAMPLE_AUDIO_DIR / candidate
    if os.path.isfile(path):
        AUDIO = str(path)
        break
else:
    raise SystemExit("No audio file found in workspace")
print(f"Audio: {os.path.basename(AUDIO)}")

print("Loading Silero VAD model…")
model = load_silero_vad()

print("Reading audio via pydub and converting to 16 kHz mono float32…")
seg = AudioSegment.from_file(AUDIO).set_frame_rate(16000).set_channels(1).set_sample_width(2)
samples = np.array(seg.get_array_of_samples(), dtype=np.int16).astype(np.float32) / 32768.0
wav = torch.from_numpy(samples)
duration_s = len(wav) / 16000
print(f"Audio: {duration_s:.1f}s")

# Try several minimum-silence thresholds and count resulting segments.
# Silero's `min_silence_duration_ms` is the pause length required to split segments.
print(f"\n{'='*60}")
print("How many segments would we get at each pause threshold?")
print(f"{'='*60}")
for min_pause_ms in [500, 800, 1000, 1500, 2000, 2500, 3000]:
    ts = get_speech_timestamps(
        wav, model,
        sampling_rate=16000,
        min_silence_duration_ms=min_pause_ms,
        min_speech_duration_ms=250,
        threshold=0.5,
    )
    print(f"  pause >= {min_pause_ms:>4} ms  ->  {len(ts):>4} segments")

# Best detail at a sensible threshold:
print(f"\n{'='*60}")
print("Per-segment breakdown at min_pause = 1500 ms (default)")
print(f"{'='*60}")
ts = get_speech_timestamps(
    wav, model,
    sampling_rate=16000,
    min_silence_duration_ms=1500,
    min_speech_duration_ms=250,
    threshold=0.5,
    return_seconds=True,
)
for i, seg in enumerate(ts, 1):
    s = seg['start']
    e = seg['end']
    print(f"  seg {i:3d}: {s:7.2f}s - {e:7.2f}s  ({e-s:5.2f}s)")
print(f"\nTotal: {len(ts)} segments")
