"""
Headless tests for voice_editor data model + render pipeline.

Run:
    python _voice_editor_test.py
"""

import math
import sys
import tempfile
from pathlib import Path

import numpy as np

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "video_toolkit"))
import voice_editor as ve  # noqa: E402  (importing the module — no UI is launched)


# ─── helpers ───

def make_tone(duration_s, sr=48000, freq=440.0, channels=1):
    n = int(duration_s * sr)
    t = np.arange(n, dtype=np.float32) / sr
    sig = (0.5 * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    if channels == 2:
        sig = np.stack([sig, sig], axis=-1)
    return sig


def approx_equal(a, b, eps=1e-3):
    return abs(a - b) < eps


# ─── tests ───

def t1_basic_cut_in_middle():
    sr = 48000
    samples = make_tone(10.0, sr)
    p = ve.Project(source_path=Path("x"), samples=samples, sample_rate=sr)
    assert p.add_cut(3.0, 5.0)
    out = p.render()
    expected = int(8.0 * sr)
    assert abs(len(out) - expected) <= 5, f"len {len(out)} ≠ {expected}"
    assert p.total_cut_s() == 2.0


def t2_cut_at_start():
    sr = 48000
    samples = make_tone(5.0, sr)
    p = ve.Project(source_path=Path("x"), samples=samples, sample_rate=sr)
    assert p.add_cut(0.0, 1.5)
    out = p.render()
    expected = int(3.5 * sr)
    assert abs(len(out) - expected) <= 5


def t3_cut_at_end():
    sr = 48000
    samples = make_tone(5.0, sr)
    p = ve.Project(source_path=Path("x"), samples=samples, sample_rate=sr)
    assert p.add_cut(4.0, 5.0)
    out = p.render()
    expected = int(4.0 * sr)
    assert abs(len(out) - expected) <= 5


def t4_multiple_scattered_cuts():
    sr = 48000
    samples = make_tone(20.0, sr)
    p = ve.Project(source_path=Path("x"), samples=samples, sample_rate=sr)
    cuts = [(1.0, 2.0), (5.0, 5.5), (10.0, 12.0), (18.0, 19.0)]
    for a, b in cuts:
        assert p.add_cut(a, b)
    total_cut = sum(b - a for a, b in cuts)
    out = p.render()
    expected = int((20.0 - total_cut) * sr)
    assert abs(len(out) - expected) <= 10, f"got {len(out)} expected {expected}"
    assert approx_equal(p.total_cut_s(), total_cut)


def t5_overlapping_cuts_auto_merge():
    sr = 48000
    samples = make_tone(20.0, sr)
    p = ve.Project(source_path=Path("x"), samples=samples, sample_rate=sr)
    p.add_cut(10.0, 15.0)
    p.add_cut(12.0, 18.0)  # overlaps prior
    assert len(p.cuts) == 1
    assert approx_equal(p.cuts[0].start_s, 10.0)
    assert approx_equal(p.cuts[0].end_s, 18.0)


def t6_touching_cuts_merge():
    sr = 48000
    samples = make_tone(10.0, sr)
    p = ve.Project(source_path=Path("x"), samples=samples, sample_rate=sr)
    p.add_cut(2.0, 4.0)
    p.add_cut(4.0, 6.0)  # exactly touches
    assert len(p.cuts) == 1
    assert approx_equal(p.cuts[0].start_s, 2.0)
    assert approx_equal(p.cuts[0].end_s, 6.0)


def t7_full_cut_yields_empty():
    sr = 48000
    samples = make_tone(3.0, sr)
    p = ve.Project(source_path=Path("x"), samples=samples, sample_rate=sr)
    p.add_cut(0.0, 3.0)
    out = p.render()
    assert len(out) == 0


def t8_too_short_cut_rejected():
    sr = 48000
    samples = make_tone(5.0, sr)
    p = ve.Project(source_path=Path("x"), samples=samples, sample_rate=sr)
    assert p.add_cut(1.0, 1.005) is False  # 5ms is < 20ms minimum
    assert len(p.cuts) == 0


def t9_undo_redo():
    sr = 48000
    samples = make_tone(10.0, sr)
    p = ve.Project(source_path=Path("x"), samples=samples, sample_rate=sr)
    p.add_cut(1.0, 2.0)
    p.add_cut(5.0, 6.0)
    assert len(p.cuts) == 2
    p.undo()
    assert len(p.cuts) == 1
    p.undo()
    assert len(p.cuts) == 0
    p.redo()
    assert len(p.cuts) == 1
    p.redo()
    assert len(p.cuts) == 2


def t10_keep_regions_inverts_cuts():
    sr = 48000
    samples = make_tone(10.0, sr)
    p = ve.Project(source_path=Path("x"), samples=samples, sample_rate=sr)
    p.add_cut(2.0, 3.0)
    p.add_cut(5.0, 7.0)
    regs = p.keep_regions()
    assert len(regs) == 3
    assert approx_equal(regs[0][0], 0.0) and approx_equal(regs[0][1], 2.0)
    assert approx_equal(regs[1][0], 3.0) and approx_equal(regs[1][1], 5.0)
    assert approx_equal(regs[2][0], 7.0) and approx_equal(regs[2][1], 10.0)


def t11_stereo_preserved():
    sr = 48000
    samples = make_tone(5.0, sr, channels=2)
    p = ve.Project(source_path=Path("x"), samples=samples, sample_rate=sr)
    p.add_cut(1.0, 2.0)
    out = p.render()
    assert out.ndim == 2 and out.shape[1] == 2
    assert abs(out.shape[0] - 4 * sr) <= 5


def t12_join_has_fade_no_click():
    """Audio just after a cut should ramp up from near zero, not jump."""
    sr = 48000
    # constant-amplitude tone — easy to see the fade ramp
    samples = make_tone(5.0, sr, freq=1.0)  # nearly DC; envelope check is robust
    samples.fill(0.5)  # constant signal
    p = ve.Project(source_path=Path("x"), samples=samples, sample_rate=sr)
    p.add_cut(2.0, 3.0)
    out = p.render(fade_ms=5)
    # split point in the output is at sample int(2.0*sr)
    join_idx = int(2.0 * sr)
    fade_n = int(sr * 5 / 1000)
    # the first sample after the join should be much smaller than 0.5
    assert out[join_idx] < 0.05, f"no fade at join, got {out[join_idx]}"
    # by the end of the fade window we're back near full amplitude
    assert out[join_idx + fade_n - 1] > 0.45


def t13_keep_regions_empty_when_full_cut():
    sr = 48000
    samples = make_tone(2.0, sr)
    p = ve.Project(source_path=Path("x"), samples=samples, sample_rate=sr)
    p.add_cut(0.0, 2.0)
    assert p.keep_regions() == []


def t14_no_cuts_returns_full_audio():
    sr = 48000
    samples = make_tone(3.0, sr)
    p = ve.Project(source_path=Path("x"), samples=samples, sample_rate=sr)
    out = p.render()
    assert len(out) == len(samples)
    # fade-only-applied-at-internal-joins → opening sample is unchanged
    assert approx_equal(float(out[0]), float(samples[0]))


def t15_save_load_wav_roundtrip():
    sr = 48000
    samples = make_tone(2.0, sr).astype(np.float32)
    with tempfile.TemporaryDirectory() as td:
        wav_path = Path(td) / "out.wav"
        ve.save_audio(samples, sr, str(wav_path), fmt="wav")
        assert wav_path.exists() and wav_path.stat().st_size > 0
        # round-trip
        back, back_sr = ve.load_audio(str(wav_path))
        assert back_sr == sr
        assert abs(len(back) - len(samples)) <= 5
        # max abs error on int16 quantization ~1/32767 ≈ 3.05e-5; allow ample margin
        err = float(np.max(np.abs(back[: len(samples)] - samples[: len(back)])))
        assert err < 1.0e-4, f"roundtrip error too large: {err}"


def t16_compute_peaks_shape():
    sr = 48000
    samples = make_tone(1.0, sr)
    peaks = ve.compute_peaks(samples, n_columns=200)
    assert peaks.shape == (200, 2)


def t17_normalize_sorts_unsorted_input():
    sr = 48000
    samples = make_tone(20.0, sr)
    p = ve.Project(source_path=Path("x"), samples=samples, sample_rate=sr)
    p.add_cut(10.0, 12.0)
    p.add_cut(2.0, 3.0)  # added later but earlier in time
    assert p.cuts[0].start_s < p.cuts[1].start_s


def main():
    tests = [
        ("basic cut in middle", t1_basic_cut_in_middle),
        ("cut at start", t2_cut_at_start),
        ("cut at end", t3_cut_at_end),
        ("multiple scattered cuts", t4_multiple_scattered_cuts),
        ("overlapping cuts auto-merge", t5_overlapping_cuts_auto_merge),
        ("touching cuts merge", t6_touching_cuts_merge),
        ("full cut yields empty", t7_full_cut_yields_empty),
        ("too-short cut rejected", t8_too_short_cut_rejected),
        ("undo / redo", t9_undo_redo),
        ("keep regions invert cuts", t10_keep_regions_inverts_cuts),
        ("stereo preserved", t11_stereo_preserved),
        ("join has fade (no click)", t12_join_has_fade_no_click),
        ("keep regions empty on full cut", t13_keep_regions_empty_when_full_cut),
        ("no cuts returns full audio", t14_no_cuts_returns_full_audio),
        ("WAV save/load roundtrip", t15_save_load_wav_roundtrip),
        ("compute peaks shape", t16_compute_peaks_shape),
        ("normalize sorts unsorted input", t17_normalize_sorts_unsorted_input),
    ]
    failed = []
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            failed.append((name, str(e)))
            print(f"  FAIL  {name}  →  {e}")
        except Exception as e:
            failed.append((name, repr(e)))
            print(f"  ERR   {name}  →  {e!r}")
    print()
    print(f"{len(tests) - len(failed)}/{len(tests)} passed")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
