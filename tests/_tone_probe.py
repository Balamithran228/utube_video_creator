"""Quick diagnostic: scan the new audio for 1020 Hz tone bursts and report
how many we find at different thresholds. Helps tune the real detector."""
import os, sys
from pathlib import Path
import numpy as np
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "video_toolkit"))
from simple_video_creator import find_ffmpeg, configure_pydub
from paths import first_existing_audio
configure_pydub(find_ffmpeg())
from pydub import AudioSegment

AUDIO = first_existing_audio()

print(f"Loading {os.path.basename(AUDIO)}…")
seg = AudioSegment.from_file(AUDIO).set_frame_rate(8000).set_channels(1).set_sample_width(2)
samples = np.array(seg.get_array_of_samples(), dtype=np.float32) / 32768.0
total_s = len(samples) / 8000
print(f"Audio: {total_s:.1f}s, {len(samples)} samples @ 8 kHz")

# Sliding-window FFT, look at energy ratio in bin near 1020 Hz.
TARGET = 1020
SR = 8000
WIN_MS = 50
HOP_MS = 10
win = int(SR * WIN_MS / 1000)
hop = int(SR * HOP_MS / 1000)
n_frames = max(0, (len(samples) - win) // hop + 1)

frame_starts = np.arange(n_frames) * hop
frames = np.stack([samples[s:s+win] for s in frame_starts]) * np.hanning(win)
spectra = np.abs(np.fft.rfft(frames, axis=1)) ** 2
freqs = np.fft.rfftfreq(win, 1.0 / SR)
target_bin = int(np.argmin(np.abs(freqs - TARGET)))
print(f"Target bin: {target_bin}  ({freqs[target_bin]:.1f} Hz, ±{freqs[1]:.1f} Hz resolution)")

# Energy at target ± 1 bin / total energy
target_e = np.sum(spectra[:, max(0,target_bin-1):target_bin+2], axis=1)
total_e = np.sum(spectra, axis=1) + 1e-9
ratios = target_e / total_e

# Also check absolute target energy (so quiet tones stand out)
print(f"\n{'='*60}")
print("How many 'beep' events at each ratio threshold?")
print("(min beep duration: 100 ms)")
print(f"{'='*60}")

def count_runs(mask, hop_ms=HOP_MS, min_dur_ms=100):
    runs = []
    i, n = 0, len(mask)
    while i < n:
        if mask[i]:
            j = i
            while j < n and mask[j]:
                j += 1
            dur_ms = (j - i) * hop_ms
            if dur_ms >= min_dur_ms:
                runs.append((i * hop_ms, j * hop_ms, dur_ms))
            i = j
        else:
            i += 1
    return runs

for thr in [0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80]:
    runs = count_runs(ratios > thr)
    print(f"  ratio > {thr:.2f}  ->  {len(runs):>4} beeps")

# Show a sample of detected beeps at a sensible threshold
print(f"\n{'='*60}")
print("Sample beeps at ratio > 0.50 (first 25):")
print(f"{'='*60}")
runs = count_runs(ratios > 0.50)
for i, (s, e, d) in enumerate(runs[:25], 1):
    print(f"  beep {i:3d}  {s/1000:7.2f}s - {e/1000:7.2f}s  ({d} ms)")
print(f"\nTotal beeps at ratio > 0.50: {len(runs)}")

# Also dump the strongest hits regardless of threshold for sanity
print(f"\nTop 10 frames by 1020 Hz ratio:")
top_idx = np.argsort(ratios)[-10:][::-1]
for idx in top_idx:
    print(f"  t={idx*HOP_MS/1000:7.2f}s  ratio={ratios[idx]:.3f}  abs_e={target_e[idx]:.4f}")
