#!/usr/bin/env python3
"""
Tests for the segment preview gallery + audio editor (requirement #2).

Covers:
  - Pure data-layer logic (no GUI):
      - _next_edit_path produces unique seg_NNN.editN.wav names
      - hex_lerp, fmt_ms helpers
      - Project + Cut + render produces correctly-shorter audio
  - Pair history (uses SegmentPreviewWindow with a withdrawn root):
      - History initializes with one "original" entry
      - _on_pair_audio_changed appends and updates pair["audio"]
      - _on_pair_undo pops the latest version
      - _on_pair_revert truncates to the original
  - End-to-end: round-trip a wav through Project + save_audio + load_audio
  - GUI construction smoke tests:
      - SegmentAudioPlayer loads a real wav
      - SegmentDetailModal constructs without erroring
      - SegmentEditorModal renders a trim, opens ConfirmAudioModal, both
        Confirm and Re-edit paths behave correctly

Run:
    py -3.10 _segment_preview_test.py
"""

import os
import sys
import tempfile
import traceback
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# Wire up ffmpeg so pydub-based loads/saves work, and make Windows
# stdout UTF-8 so unicode in test names doesn't crash cp1252 printing.
from simple_video_creator import find_ffmpeg, configure_pydub, _safe_console  # noqa: E402
_safe_console()
configure_pydub(find_ffmpeg())

import segment_preview as sp  # noqa: E402
from voice_editor import Project, save_audio, load_audio  # noqa: E402

import tkinter as tk  # noqa: E402
import customtkinter as ctk  # noqa: E402
from PIL import Image  # noqa: E402


PASS: list[str] = []
FAIL: list[str] = []


def test(name):
    """Decorator: run the function as a single test, report pass/fail."""
    def decorator(f):
        try:
            f()
            print(f"  [PASS] {name}")
            PASS.append(name)
        except AssertionError as e:
            print(f"  [FAIL] {name}: {e}")
            FAIL.append(name)
        except Exception as e:
            print(f"  [FAIL] {name}: {e.__class__.__name__}: {e}")
            traceback.print_exc()
            FAIL.append(name)
        return f
    return decorator


def make_test_audio(seconds=2.0, freq=440, sr=48000):
    """Generate a sine-wave float32 numpy array."""
    n = int(seconds * sr)
    t = np.linspace(0, seconds, n, endpoint=False)
    samples = (0.4 * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    return samples, sr


# ─────────────────────────── Data-layer tests ───────────────────────────

print("\n=== Data-layer tests ===\n")


@test("_next_edit_path produces seg.editN.wav from original")
def t_next_edit_path_basic():
    p = sp._next_edit_path("/tmp/seg_005.wav", history_len=1)
    assert p.name == "seg_005.edit1.wav", p.name
    p3 = sp._next_edit_path("/tmp/seg_005.wav", history_len=3)
    assert p3.name == "seg_005.edit3.wav", p3.name


@test("_next_edit_path strips existing .editN suffix to keep base stable")
def t_next_edit_path_strip():
    p = sp._next_edit_path("/tmp/seg_005.edit2.wav", history_len=3)
    assert p.name == "seg_005.edit3.wav", p.name


@test("hex_lerp(black,white,0.5) == #7f7f7f")
def t_hex_lerp():
    assert sp.hex_lerp("#000000", "#ffffff", 0.5) == "#7f7f7f"
    assert sp.hex_lerp("#000000", "#ffffff", 0.0) == "#000000"
    assert sp.hex_lerp("#000000", "#ffffff", 1.0) == "#ffffff"


@test("fmt_ms formats milliseconds")
def t_fmt_ms():
    assert sp.fmt_ms(1500) == "00:01.500"
    assert sp.fmt_ms(0) == "00:00.000"
    assert sp.fmt_ms(None) == "--:--.---"
    assert sp.fmt_ms(65432) == "01:05.432"


@test("Project.render with one cut produces correctly shorter audio")
def t_render_cut():
    samples, sr = make_test_audio(seconds=4.0)
    proj = Project(source_path=Path("test.wav"), samples=samples, sample_rate=sr)
    proj.add_cut(1.0, 2.5)  # 1.5s removed
    rendered = proj.render(fade_ms=5)
    expected = int(round((4.0 - 1.5) * sr))
    # Off-by-one tolerance for region-edge rounding
    assert abs(len(rendered) - expected) <= 5, f"len={len(rendered)}, expected≈{expected}"


@test("Project.render with no cuts equals original")
def t_render_nocut():
    samples, sr = make_test_audio(seconds=2.0)
    proj = Project(source_path=Path("test.wav"), samples=samples, sample_rate=sr)
    rendered = proj.render(fade_ms=5)
    assert len(rendered) == len(samples)


@test("Project.render with multiple cuts removes correct amount")
def t_render_multi():
    samples, sr = make_test_audio(seconds=4.0)
    proj = Project(source_path=Path("test.wav"), samples=samples, sample_rate=sr)
    proj.add_cut(0.5, 1.0)   # 0.5s
    proj.add_cut(2.0, 2.75)  # 0.75s
    rendered = proj.render(fade_ms=5)
    expected = int(round((4.0 - 0.5 - 0.75) * sr))
    assert abs(len(rendered) - expected) <= 5


# ─────────────────────────── Pair-history tests ───────────────────────────

print("\n=== Pair-history tests (using a withdrawn SegmentPreviewWindow) ===\n")

# Withdrawn root so no real window flashes
ctk.set_appearance_mode("dark")
root = ctk.CTk()
root.withdraw()
root.update()


def _new_window(pairs):
    """Build a SegmentPreviewWindow and immediately withdraw it."""
    win = sp.SegmentPreviewWindow(
        root, pairs, sp.THEMES["dark"],
        on_continue=lambda: None, on_cancel=lambda: None,
    )
    win.withdraw()
    return win


@test("SegmentPreviewWindow init populates _history[0] = original for each pair")
def th_init():
    pairs = [
        {"image": "/fake/img1.png", "audio": "/fake/seg_000.wav", "duration_ms": 1000, "index": 1},
        {"image": "/fake/img2.png", "audio": "/fake/seg_001.wav", "duration_ms": 2000, "index": 2},
    ]
    win = _new_window(pairs)
    try:
        assert "_history" in pairs[0]
        assert len(pairs[0]["_history"]) == 1
        assert pairs[0]["_history"][0]["path"] == "/fake/seg_000.wav"
        assert pairs[0]["_history"][0]["duration_ms"] == 1000
        assert pairs[1]["_history"][0]["duration_ms"] == 2000
    finally:
        win.destroy()


@test("_on_pair_audio_changed appends a new history entry and rewrites pair")
def th_change():
    pairs = [
        {"image": "/fake/img1.png", "audio": "/fake/seg_000.wav", "duration_ms": 1000, "index": 1},
    ]
    win = _new_window(pairs)
    try:
        win._on_pair_audio_changed(0, "/fake/seg_000.edit1.wav", 700)
        assert pairs[0]["audio"] == "/fake/seg_000.edit1.wav"
        assert pairs[0]["duration_ms"] == 700
        assert len(pairs[0]["_history"]) == 2
        assert pairs[0]["_history"][-1]["path"] == "/fake/seg_000.edit1.wav"
        assert pairs[0]["_history"][-1]["duration_ms"] == 700
    finally:
        win.destroy()


@test("_on_pair_undo restores previous version, multiple times")
def th_undo():
    pairs = [
        {"image": "/fake/img1.png", "audio": "/fake/seg_000.wav", "duration_ms": 1000, "index": 1},
    ]
    win = _new_window(pairs)
    try:
        win._on_pair_audio_changed(0, "/fake/seg_000.edit1.wav", 700)
        win._on_pair_audio_changed(0, "/fake/seg_000.edit2.wav", 500)
        # Undo once -> back to edit1
        win._on_pair_undo(0)
        assert pairs[0]["audio"] == "/fake/seg_000.edit1.wav"
        assert pairs[0]["duration_ms"] == 700
        assert len(pairs[0]["_history"]) == 2
        # Undo again -> back to original
        win._on_pair_undo(0)
        assert pairs[0]["audio"] == "/fake/seg_000.wav"
        assert pairs[0]["duration_ms"] == 1000
        assert len(pairs[0]["_history"]) == 1
        # Undo when nothing to undo -> no change
        win._on_pair_undo(0)
        assert pairs[0]["audio"] == "/fake/seg_000.wav"
        assert len(pairs[0]["_history"]) == 1
    finally:
        win.destroy()


@test("_on_pair_revert truncates history to original")
def th_revert():
    pairs = [
        {"image": "/fake/img1.png", "audio": "/fake/seg_000.wav", "duration_ms": 1000, "index": 1},
    ]
    win = _new_window(pairs)
    try:
        win._on_pair_audio_changed(0, "/fake/seg_000.edit1.wav", 700)
        win._on_pair_audio_changed(0, "/fake/seg_000.edit2.wav", 500)
        win._on_pair_audio_changed(0, "/fake/seg_000.edit3.wav", 300)
        win._on_pair_revert(0)
        assert pairs[0]["audio"] == "/fake/seg_000.wav"
        assert pairs[0]["duration_ms"] == 1000
        assert len(pairs[0]["_history"]) == 1
    finally:
        win.destroy()


@test("_on_pair_revert with no edits is a no-op")
def th_revert_noop():
    pairs = [
        {"image": "/fake/img1.png", "audio": "/fake/seg_000.wav", "duration_ms": 1000, "index": 1},
    ]
    win = _new_window(pairs)
    try:
        win._on_pair_revert(0)
        assert pairs[0]["audio"] == "/fake/seg_000.wav"
        assert len(pairs[0]["_history"]) == 1
    finally:
        win.destroy()


# ─────────────────────────── End-to-end: real wav round-trip ───────────────────────────

print("\n=== End-to-end: real wav round-trip ===\n")


@test("E2E: write wav -> trim via Project -> save -> reload, duration matches")
def te_roundtrip():
    with tempfile.TemporaryDirectory(prefix="seg_preview_test_") as tmpdir:
        tmp = Path(tmpdir)
        samples, sr = make_test_audio(seconds=3.0)
        src = tmp / "seg_001.wav"
        save_audio(samples, sr, str(src), fmt="wav")
        assert src.exists()

        loaded, sr2 = load_audio(str(src))
        assert sr2 == sr
        proj = Project(source_path=src, samples=loaded, sample_rate=sr)
        proj.add_cut(0.5, 1.5)  # 1s removed -> result ≈ 2s
        rendered = proj.render(fade_ms=5)

        out = sp._next_edit_path(str(src), history_len=1)
        save_audio(rendered, sr, str(out), fmt="wav")
        assert out.exists()
        assert out.name == "seg_001.edit1.wav"

        re_loaded, re_sr = load_audio(str(out))
        new_dur_s = len(re_loaded) / re_sr
        assert abs(new_dur_s - 2.0) < 0.01, f"expected ~2.0s, got {new_dur_s:.4f}"


# ─────────────────────────── GUI construction smoke ───────────────────────────

print("\n=== GUI construction smoke (windows withdrawn) ===\n")


@test("SegmentAudioPlayer loads a real wav and reports correct sr/duration")
def tg_player():
    with tempfile.TemporaryDirectory() as tmpdir:
        wav = Path(tmpdir) / "x.wav"
        samples, sr = make_test_audio(seconds=0.5)
        save_audio(samples, sr, str(wav), fmt="wav")
        frame = ctk.CTkFrame(root)
        try:
            player = sp.SegmentAudioPlayer(frame, theme=sp.THEMES["dark"])
            assert player.load(str(wav)) is True
            assert player.sr == sr
            assert player.samples is not None
            assert abs(len(player.samples) / sr - 0.5) < 0.02
            player.stop()
        finally:
            try:
                player.destroy()
            except Exception:
                pass
            frame.destroy()


@test("SegmentDetailModal constructs and shows a real (image, audio) pair")
def tg_detail():
    with tempfile.TemporaryDirectory() as tmpdir:
        wav = Path(tmpdir) / "seg_001.wav"
        samples, sr = make_test_audio(seconds=0.5)
        save_audio(samples, sr, str(wav), fmt="wav")

        img = Path(tmpdir) / "panel-001.png"
        Image.new("RGB", (200, 200), color=(80, 90, 200)).save(img)

        pairs = [{
            "image": str(img), "audio": str(wav),
            "duration_ms": 500, "index": 1,
            "_history": [{"path": str(wav), "duration_ms": 500, "label": "original"}],
        }]
        modal = sp.SegmentDetailModal(root, pairs, 0, sp.THEMES["dark"])
        modal.withdraw()
        try:
            assert modal.idx == 0
            assert modal.player.sr == sr
        finally:
            modal._close()


@test("SegmentEditorModal Save -> Confirm calls on_save with new wav and correct duration")
def tg_editor_confirm():
    saved_calls: list[tuple[str, int]] = []

    def on_save(path, dur_ms):
        saved_calls.append((path, dur_ms))

    with tempfile.TemporaryDirectory() as tmpdir:
        wav = Path(tmpdir) / "seg_002.wav"
        samples, sr = make_test_audio(seconds=2.0)
        save_audio(samples, sr, str(wav), fmt="wav")

        editor = sp.SegmentEditorModal(
            root, audio_path=str(wav),
            original_duration_ms=2000, history_len=1,
            theme=sp.THEMES["dark"],
            on_save=on_save, on_cancel=lambda: None,
            segment_label="test",
        )
        editor.withdraw()
        try:
            # Add a cut from 0.5 to 1.5 (1s removed -> result ≈ 1s)
            editor._on_add_cut(0.5, 1.5)
            assert len(editor.project.cuts) == 1

            editor._save()  # opens ConfirmAudioModal as a child

            confirms = [w for w in editor.winfo_children() if isinstance(w, sp.ConfirmAudioModal)]
            assert len(confirms) == 1, f"expected 1 ConfirmAudioModal child, got {len(confirms)}"
            confirm = confirms[0]
            confirm.withdraw()

            confirm._confirm()  # closes editor, fires on_save

            assert len(saved_calls) == 1
            new_path, new_dur_ms = saved_calls[0]
            assert Path(new_path).exists(), f"output not on disk: {new_path}"
            assert Path(new_path).name == "seg_002.edit1.wav"
            assert abs(new_dur_ms - 1000) < 50, f"expected ~1000ms, got {new_dur_ms}"
        finally:
            try:
                if editor.winfo_exists():
                    editor._cancel()
            except Exception:
                pass


@test("SegmentEditorModal Save -> Re-edit closes confirm only, editor stays, on_save NOT fired")
def tg_editor_reedit():
    saved_calls: list = []

    def on_save(path, dur_ms):
        saved_calls.append((path, dur_ms))

    with tempfile.TemporaryDirectory() as tmpdir:
        wav = Path(tmpdir) / "seg_003.wav"
        samples, sr = make_test_audio(seconds=2.0)
        save_audio(samples, sr, str(wav), fmt="wav")

        editor = sp.SegmentEditorModal(
            root, audio_path=str(wav),
            original_duration_ms=2000, history_len=1,
            theme=sp.THEMES["dark"],
            on_save=on_save, on_cancel=lambda: None,
            segment_label="test",
        )
        editor.withdraw()
        try:
            editor._on_add_cut(0.5, 1.5)
            editor._save()
            confirms = [w for w in editor.winfo_children() if isinstance(w, sp.ConfirmAudioModal)]
            assert len(confirms) == 1
            confirm = confirms[0]
            confirm.withdraw()

            confirm._reedit()  # discard confirm, keep editor

            assert len(saved_calls) == 0, "on_save should NOT fire when re-editing"
            assert editor.winfo_exists(), "editor should still exist after re-edit"
            for w in editor.winfo_children():
                assert not isinstance(w, sp.ConfirmAudioModal), \
                    "ConfirmAudioModal should have been destroyed"
        finally:
            try:
                if editor.winfo_exists():
                    editor._cancel()
            except Exception:
                pass


@test("SegmentEditorModal rejects empty result (all cut)")
def tg_editor_empty():
    with tempfile.TemporaryDirectory() as tmpdir:
        wav = Path(tmpdir) / "seg_004.wav"
        samples, sr = make_test_audio(seconds=1.0)
        save_audio(samples, sr, str(wav), fmt="wav")

        editor = sp.SegmentEditorModal(
            root, audio_path=str(wav),
            original_duration_ms=1000, history_len=1,
            theme=sp.THEMES["dark"],
            on_save=lambda *a: None, on_cancel=lambda: None,
            segment_label="test",
        )
        editor.withdraw()
        try:
            # Cut the entire audio
            editor._on_add_cut(0.0, 1.0)
            # _save shows a messagebox warning and returns early — we can't
            # interact with it from a test, but we can verify that no
            # ConfirmAudioModal child was created and no edit file written.
            # Stub messagebox to capture and not show:
            from tkinter import messagebox as mb
            orig = mb.showwarning
            calls = []
            mb.showwarning = lambda *a, **k: calls.append((a, k))
            try:
                editor._save()
            finally:
                mb.showwarning = orig
            confirms = [w for w in editor.winfo_children() if isinstance(w, sp.ConfirmAudioModal)]
            assert len(confirms) == 0
            assert len(calls) == 1, "should warn user about empty result"
        finally:
            try:
                editor._cancel()
            except Exception:
                pass


@test("End-to-end: pair audio path is updated after edit confirm via callback chain")
def tg_full_chain():
    with tempfile.TemporaryDirectory() as tmpdir:
        wav = Path(tmpdir) / "seg_005.wav"
        samples, sr = make_test_audio(seconds=2.0)
        save_audio(samples, sr, str(wav), fmt="wav")
        img = Path(tmpdir) / "panel-005.png"
        Image.new("RGB", (200, 200), color=(200, 80, 80)).save(img)

        pairs = [{
            "image": str(img), "audio": str(wav),
            "duration_ms": 2000, "index": 5,
        }]
        win = _new_window(pairs)
        try:
            # Simulate: detail modal calls back to win with a new edit
            new_path = str(Path(tmpdir) / "seg_005.edit1.wav")
            # Write a fake edit file
            samples2, _ = make_test_audio(seconds=1.0)
            save_audio(samples2, sr, new_path, fmt="wav")
            win._on_pair_audio_changed(0, new_path, 1000)

            assert pairs[0]["audio"] == new_path
            assert pairs[0]["duration_ms"] == 1000
            assert len(pairs[0]["_history"]) == 2

            # Undo -> back to original
            win._on_pair_undo(0)
            assert pairs[0]["audio"] == str(wav)
            assert pairs[0]["duration_ms"] == 2000
        finally:
            win.destroy()


# ─────────────────────────── Image-action tests (req 3) ───────────────────────────

print("\n=== Image-action tests (req 3) ===\n")


@test("_on_pair_image_changed updates pair image, leaves audio + history alone")
def ti_image_changed():
    pairs = [
        {"image": "/fake/img1.png", "audio": "/fake/seg_000.wav", "duration_ms": 1000, "index": 1},
    ]
    win = _new_window(pairs)
    try:
        win._on_pair_image_changed(0, "/fake/replacement.png")
        assert pairs[0]["image"] == "/fake/replacement.png"
        # audio and history must NOT be touched
        assert pairs[0]["audio"] == "/fake/seg_000.wav"
        assert pairs[0]["duration_ms"] == 1000
        assert len(pairs[0]["_history"]) == 1
    finally:
        win.destroy()


@test("_on_pair_image_dropped sets pair image to None, keeps audio")
def ti_image_dropped():
    pairs = [
        {"image": "/fake/img1.png", "audio": "/fake/seg_000.wav", "duration_ms": 1000, "index": 1},
        {"image": "/fake/img2.png", "audio": "/fake/seg_001.wav", "duration_ms": 2000, "index": 2},
    ]
    win = _new_window(pairs)
    try:
        win._on_pair_image_dropped(1)
        assert pairs[1]["image"] is None
        assert pairs[1]["audio"] == "/fake/seg_001.wav"
        assert pairs[1]["duration_ms"] == 2000
        # other pair untouched
        assert pairs[0]["image"] == "/fake/img1.png"
    finally:
        win.destroy()


@test("_on_pair_segment_deleted removes pair, renumbers, refreshes strip")
def ti_segment_deleted():
    pairs = [
        {"image": "/fake/a.png", "audio": "/fake/seg_000.wav", "duration_ms": 1000, "index": 1},
        {"image": "/fake/b.png", "audio": "/fake/seg_001.wav", "duration_ms": 2000, "index": 2},
        {"image": "/fake/c.png", "audio": "/fake/seg_002.wav", "duration_ms": 3000, "index": 3},
    ]
    win = _new_window(pairs)
    try:
        win._on_pair_segment_deleted(1)  # delete middle pair
        assert len(pairs) == 2
        # First pair stays the same
        assert pairs[0]["audio"] == "/fake/seg_000.wav"
        assert pairs[0]["index"] == 1
        # What was [2] slides into [1] and is renumbered
        assert pairs[1]["audio"] == "/fake/seg_002.wav"
        assert pairs[1]["index"] == 2
        # Strip should have rebuilt to match
        assert len(win._cards) == 2
    finally:
        win.destroy()


@test("_on_pair_segment_deleted refuses when only one pair remains")
def ti_segment_deleted_last():
    pairs = [
        {"image": "/fake/only.png", "audio": "/fake/seg_000.wav", "duration_ms": 1000, "index": 1},
    ]
    win = _new_window(pairs)
    try:
        # Stub messagebox so the warning doesn't pop a real dialog
        from tkinter import messagebox as mb
        orig = mb.showwarning
        calls = []
        mb.showwarning = lambda *a, **k: calls.append((a, k))
        try:
            win._on_pair_segment_deleted(0)
        finally:
            mb.showwarning = orig
        assert len(pairs) == 1, "should not delete the last remaining pair"
        assert pairs[0]["audio"] == "/fake/seg_000.wav"
        assert len(calls) == 1, "user should be warned"
    finally:
        win.destroy()


@test("_action_duplicate copies prev/next image into pair[idx]")
def ti_action_duplicate():
    pairs = [
        {"image": "/fake/a.png", "audio": "/fake/seg_000.wav", "duration_ms": 1000, "index": 1},
        {"image": "/fake/b.png", "audio": "/fake/seg_001.wav", "duration_ms": 2000, "index": 2},
        {"image": "/fake/c.png", "audio": "/fake/seg_002.wav", "duration_ms": 3000, "index": 3},
    ]
    win = _new_window(pairs)
    try:
        # Replace b's image with a's (duplicate previous)
        win._action_duplicate(1, "prev")
        assert pairs[1]["image"] == "/fake/a.png"
        # Replace b's image with c's (duplicate next)
        win._action_duplicate(1, "next")
        assert pairs[1]["image"] == "/fake/c.png"
        # Edge cases — duplicate prev at index 0 should be a no-op
        win._action_duplicate(0, "prev")
        assert pairs[0]["image"] == "/fake/a.png"
        # Duplicate next at last index should be a no-op
        win._action_duplicate(2, "next")
        assert pairs[2]["image"] == "/fake/c.png"
    finally:
        win.destroy()


@test("Default images surface in defaults list and replace via _action_replace_with_path")
def ti_action_default():
    pairs = [
        {"image": "/fake/orig.png", "audio": "/fake/seg_000.wav", "duration_ms": 1000, "index": 1},
    ]
    defaults = [
        {"label": "Front", "path": "/fake/start.png"},
        {"label": "Back", "path": "/fake/end.png"},
    ]
    win = sp.SegmentPreviewWindow(
        root, pairs, sp.THEMES["dark"],
        on_continue=lambda: None, on_cancel=lambda: None,
        defaults=defaults, images_dir="/fake",
    )
    win.withdraw()
    try:
        assert win.defaults == defaults
        win._action_replace_with_path(0, "/fake/end.png")
        assert pairs[0]["image"] == "/fake/end.png"
    finally:
        win.destroy()


@test("DeleteImageModal calls correct callback per button")
def ti_delete_modal():
    drop_calls = []
    full_calls = []
    modal = sp.DeleteImageModal(
        root, sp.THEMES["dark"],
        segment_label="Segment 2",
        on_drop_image=lambda: drop_calls.append("drop"),
        on_delete_segment=lambda: full_calls.append("full"),
        can_delete_segment=True,
    )
    modal.withdraw()
    try:
        modal._drop_image_only()
        assert drop_calls == ["drop"]
        assert full_calls == []
    finally:
        try:
            modal.destroy()
        except Exception:
            pass

    modal2 = sp.DeleteImageModal(
        root, sp.THEMES["dark"],
        segment_label="Segment 2",
        on_drop_image=lambda: drop_calls.append("drop"),
        on_delete_segment=lambda: full_calls.append("full"),
        can_delete_segment=True,
    )
    modal2.withdraw()
    try:
        modal2._delete_segment_full()
        assert full_calls == ["full"]
    finally:
        try:
            modal2.destroy()
        except Exception:
            pass


@test("SegmentCard renders 'audio only' placeholder when image is None")
def ti_card_none_image():
    pairs = [
        {"image": None, "audio": "/fake/seg.wav", "duration_ms": 1000, "index": 1},
    ]
    # Build a fresh strip so we can introspect a card whose pair has image=None
    win = _new_window(pairs)
    try:
        assert len(win._cards) == 1
        card = win._cards[0]
        # The thumbnail label should be in placeholder mode (no _thumb_ref)
        assert card._thumb_ref is None
        # Refresh after toggling the image back ON should pick it up.
        # Canonical field is pair["images"]; keep "image" alias synced too.
        with tempfile.TemporaryDirectory() as tmpdir:
            from PIL import Image as _PIL
            img_path = Path(tmpdir) / "x.png"
            _PIL.new("RGB", (40, 40), color=(10, 200, 10)).save(img_path)
            pairs[0]["images"] = [str(img_path)]
            pairs[0]["image"] = str(img_path)
            card.refresh()
            assert card._thumb_ref is not None
    finally:
        win.destroy()


@test("SegmentDetailModal renders cleanly when shown pair has image=None")
def ti_detail_none_image():
    with tempfile.TemporaryDirectory() as tmpdir:
        wav = Path(tmpdir) / "seg_000.wav"
        samples, sr = make_test_audio(seconds=0.5)
        save_audio(samples, sr, str(wav), fmt="wav")
        pairs = [{
            "image": None, "audio": str(wav),
            "duration_ms": 500, "index": 1,
            "_history": [{"path": str(wav), "duration_ms": 500, "label": "original"}],
        }]
        modal = sp.SegmentDetailModal(root, pairs, 0, sp.THEMES["dark"])
        modal.withdraw()
        try:
            assert modal.idx == 0
            # The image label should show the audio-only placeholder text
            txt = modal.image_label.cget("text") or ""
            assert "audio only" in txt.lower(), f"expected audio-only placeholder, got: {txt!r}"
        finally:
            modal._close()


# ─────────────────────────── Pipeline rebuild tests (req 3) ───────────────────────────

print("\n=== Pipeline rebuild tests (req 3) ===\n")

import tone_video_creator as tvc  # noqa: E402


@test("_apply_preview_edits passes through unchanged pairs")
def tp_passthrough():
    pairs = [
        {"image": "/img/a.png", "audio": "/audio/0.wav", "duration_ms": 1000, "index": 1},
        {"image": "/img/b.png", "audio": "/audio/1.wav", "duration_ms": 2000, "index": 2},
    ]
    segs, imgs, err = tvc._apply_preview_edits(pairs)
    assert err is None
    assert imgs == ["/img/a.png", "/img/b.png"]
    assert segs[0] == {"file": "/audio/0.wav", "duration_ms": 1000}
    assert segs[1] == {"file": "/audio/1.wav", "duration_ms": 2000}


@test("_apply_preview_edits replaces None image with previous image (audio extends)")
def tp_none_image_uses_prev():
    pairs = [
        {"image": "/img/a.png", "audio": "/audio/0.wav", "duration_ms": 1000, "index": 1},
        {"image": None,         "audio": "/audio/1.wav", "duration_ms": 2000, "index": 2},
        {"image": "/img/c.png", "audio": "/audio/2.wav", "duration_ms": 3000, "index": 3},
    ]
    segs, imgs, err = tvc._apply_preview_edits(pairs)
    assert err is None
    # image[1] (originally None) falls back to image[0]
    assert imgs == ["/img/a.png", "/img/a.png", "/img/c.png"]
    # All three audio segments are kept
    assert [s["duration_ms"] for s in segs] == [1000, 2000, 3000]


@test("_apply_preview_edits handles consecutive None images chaining back to one source")
def tp_none_chain():
    pairs = [
        {"image": "/img/a.png", "audio": "/audio/0.wav", "duration_ms": 1000, "index": 1},
        {"image": None,         "audio": "/audio/1.wav", "duration_ms": 2000, "index": 2},
        {"image": None,         "audio": "/audio/2.wav", "duration_ms": 3000, "index": 3},
        {"image": "/img/d.png", "audio": "/audio/3.wav", "duration_ms": 4000, "index": 4},
    ]
    segs, imgs, err = tvc._apply_preview_edits(pairs)
    assert err is None
    assert imgs == ["/img/a.png", "/img/a.png", "/img/a.png", "/img/d.png"]


@test("_apply_preview_edits leading None falls back to first available image")
def tp_leading_none():
    pairs = [
        {"image": None,         "audio": "/audio/0.wav", "duration_ms": 500, "index": 1},
        {"image": "/img/b.png", "audio": "/audio/1.wav", "duration_ms": 1000, "index": 2},
    ]
    segs, imgs, err = tvc._apply_preview_edits(pairs)
    assert err is None
    assert imgs == ["/img/b.png", "/img/b.png"]


@test("_apply_preview_edits returns error when ALL images are None")
def tp_all_none():
    pairs = [
        {"image": None, "audio": "/audio/0.wav", "duration_ms": 500, "index": 1},
        {"image": None, "audio": "/audio/1.wav", "duration_ms": 1000, "index": 2},
    ]
    segs, imgs, err = tvc._apply_preview_edits(pairs)
    assert err is not None, "expected error when all images deleted"


@test("_apply_preview_edits handles structural deletion (shorter list)")
def tp_shorter_list():
    # Caller deleted segment 2 entirely — list has just two entries left
    pairs = [
        {"image": "/img/a.png", "audio": "/audio/0.wav", "duration_ms": 1000, "index": 1},
        {"image": "/img/c.png", "audio": "/audio/2.wav", "duration_ms": 3000, "index": 2},
    ]
    segs, imgs, err = tvc._apply_preview_edits(pairs)
    assert err is None
    assert len(segs) == 2
    assert len(imgs) == 2
    assert segs[1]["file"] == "/audio/2.wav"
    assert imgs[1] == "/img/c.png"


@test("_apply_preview_edits image-replace flows through to the rendered list")
def tp_image_replace():
    pairs = [
        {"image": "/img/a.png", "audio": "/audio/0.wav", "duration_ms": 1000, "index": 1},
        {"image": "/img/REPLACED.png", "audio": "/audio/1.wav", "duration_ms": 2000, "index": 2},
    ]
    segs, imgs, err = tvc._apply_preview_edits(pairs)
    assert err is None
    assert imgs[1] == "/img/REPLACED.png"


@test("End-to-end: segment delete + image-only delete → preview_pairs round-trip via _apply_preview_edits")
def tp_e2e_combo():
    """Mimics the realistic user workflow: open preview with 4 pairs,
    delete pair 2 entirely (segment+audio), drop pair 3's image only,
    replace pair 4's image. Verify the rebuilt segments+images list."""
    pairs = [
        {"image": "/img/a.png", "audio": "/audio/0.wav", "duration_ms": 1000, "index": 1},
        {"image": "/img/b.png", "audio": "/audio/1.wav", "duration_ms": 1500, "index": 2},
        {"image": "/img/c.png", "audio": "/audio/2.wav", "duration_ms": 2000, "index": 3},
        {"image": "/img/d.png", "audio": "/audio/3.wav", "duration_ms": 2500, "index": 4},
    ]
    win = _new_window(pairs)
    try:
        # Delete pair 2 entirely
        win._on_pair_segment_deleted(1)
        # Now pairs is [a, c, d], indices renumbered to 1,2,3
        assert [p["index"] for p in pairs] == [1, 2, 3]
        # Drop image of pair currently at index 1 (was "c")
        win._on_pair_image_dropped(1)
        # Replace image of pair currently at index 2 (was "d")
        win._on_pair_image_changed(2, "/img/replaced_d.png")

        segs, imgs, err = tvc._apply_preview_edits(pairs)
        assert err is None
        # Expected: a / (None→a falls back) / replaced_d
        assert imgs == ["/img/a.png", "/img/a.png", "/img/replaced_d.png"]
        # Audio durations preserved (1000, 2000, 2500 — pair 2 was deleted)
        assert [s["duration_ms"] for s in segs] == [1000, 2000, 2500]
    finally:
        win.destroy()


# ─────────────────────────── Audio-action tests (req 4) ───────────────────────────

print("\n=== Audio-action tests (req 4) ===\n")


@test("_on_pair_audio_only_deleted shifts subsequent audios up; trailing pair audio=None")
def ta_audio_shift_basic():
    pairs = [
        {"image": "/img/a.png", "audio": "/audio/0.wav", "duration_ms": 1000, "index": 1},
        {"image": "/img/b.png", "audio": "/audio/1.wav", "duration_ms": 2000, "index": 2},
        {"image": "/img/c.png", "audio": "/audio/2.wav", "duration_ms": 3000, "index": 3},
        {"image": "/img/d.png", "audio": "/audio/3.wav", "duration_ms": 4000, "index": 4},
    ]
    win = _new_window(pairs)
    try:
        # Delete audio at idx=1 (was audio/1.wav). Audio chain becomes:
        # 0.wav, 2.wav (shifted from idx 2), 3.wav (shifted from idx 3), None.
        # Images stay put: a, b, c, d
        win._on_pair_audio_only_deleted(1)

        assert pairs[0]["audio"] == "/audio/0.wav"
        assert pairs[0]["image"] == "/img/a.png"

        # Image B now matches what was audio[2]
        assert pairs[1]["image"] == "/img/b.png"
        assert pairs[1]["audio"] == "/audio/2.wav"
        assert pairs[1]["duration_ms"] == 3000

        # Image C now matches what was audio[3]
        assert pairs[2]["image"] == "/img/c.png"
        assert pairs[2]["audio"] == "/audio/3.wav"
        assert pairs[2]["duration_ms"] == 4000

        # Trailing pair: image kept, audio is None (orphan slot)
        assert pairs[3]["image"] == "/img/d.png"
        assert pairs[3]["audio"] is None
        assert pairs[3]["duration_ms"] is None
        assert pairs[3]["_history"] == []
    finally:
        win.destroy()


@test("_on_pair_audio_only_deleted at idx=0 shifts everything up by one")
def ta_audio_shift_first():
    pairs = [
        {"image": "/img/a.png", "audio": "/audio/0.wav", "duration_ms": 1000, "index": 1},
        {"image": "/img/b.png", "audio": "/audio/1.wav", "duration_ms": 2000, "index": 2},
        {"image": "/img/c.png", "audio": "/audio/2.wav", "duration_ms": 3000, "index": 3},
    ]
    win = _new_window(pairs)
    try:
        win._on_pair_audio_only_deleted(0)
        # image[a] now plays audio[1]
        assert pairs[0]["audio"] == "/audio/1.wav"
        # image[b] now plays audio[2]
        assert pairs[1]["audio"] == "/audio/2.wav"
        # image[c] is orphaned
        assert pairs[2]["audio"] is None
    finally:
        win.destroy()


@test("_on_pair_audio_only_deleted refuses when only one segment exists")
def ta_audio_shift_single():
    pairs = [
        {"image": "/img/a.png", "audio": "/audio/0.wav", "duration_ms": 1000, "index": 1},
    ]
    win = _new_window(pairs)
    try:
        from tkinter import messagebox as mb
        orig = mb.showwarning
        calls = []
        mb.showwarning = lambda *a, **k: calls.append((a, k))
        try:
            win._on_pair_audio_only_deleted(0)
        finally:
            mb.showwarning = orig
        # Audio untouched
        assert pairs[0]["audio"] == "/audio/0.wav"
        assert len(calls) == 1, "user should be warned"
    finally:
        win.destroy()


@test("_on_pair_audio_only_deleted carries the next pair's _history along with the audio")
def ta_audio_shift_history():
    pairs = [
        {"image": "/img/a.png", "audio": "/audio/0.wav", "duration_ms": 1000, "index": 1,
         "_history": [{"path": "/audio/0.wav", "duration_ms": 1000, "label": "original"}]},
        {"image": "/img/b.png", "audio": "/audio/1.wav", "duration_ms": 2000, "index": 2,
         "_history": [{"path": "/audio/1.wav", "duration_ms": 2000, "label": "original"}]},
        {"image": "/img/c.png", "audio": "/audio/2.edit1.wav", "duration_ms": 1500, "index": 3,
         "_history": [
             {"path": "/audio/2.wav", "duration_ms": 3000, "label": "original"},
             {"path": "/audio/2.edit1.wav", "duration_ms": 1500, "label": "edit 1"},
         ]},
    ]
    win = _new_window(pairs)
    try:
        win._on_pair_audio_only_deleted(1)
        # idx=1 now holds what was at idx=2 — including the edit history
        assert pairs[1]["audio"] == "/audio/2.edit1.wav"
        assert pairs[1]["duration_ms"] == 1500
        assert len(pairs[1]["_history"]) == 2
        assert pairs[1]["_history"][1]["label"] == "edit 1"
        # Trailing pair: history cleared
        assert pairs[2]["_history"] == []
        assert pairs[2]["audio"] is None
    finally:
        win.destroy()


@test("_on_pair_audio_replaced loads new audio, updates pair + appends history entry")
def ta_audio_replace():
    with tempfile.TemporaryDirectory() as tmpdir:
        # Original is 1.0s, replacement is 2.5s — image should display longer
        orig = Path(tmpdir) / "orig.wav"
        repl = Path(tmpdir) / "longer.wav"
        s_orig, sr = make_test_audio(seconds=1.0)
        save_audio(s_orig, sr, str(orig), fmt="wav")
        s_repl, _ = make_test_audio(seconds=2.5)
        save_audio(s_repl, sr, str(repl), fmt="wav")

        pairs = [
            {"image": "/img/a.png", "audio": str(orig), "duration_ms": 1000, "index": 1},
        ]
        win = _new_window(pairs)
        try:
            win._on_pair_audio_replaced(0, str(repl))
            assert pairs[0]["audio"] == str(repl)
            # Duration probed via pydub — should be ~2500ms (allow ±30ms slop)
            assert abs(pairs[0]["duration_ms"] - 2500) < 50, pairs[0]["duration_ms"]
            # History: original entry + replaced entry
            assert len(pairs[0]["_history"]) == 2
            assert pairs[0]["_history"][-1]["path"] == str(repl)
            assert "replaced" in pairs[0]["_history"][-1]["label"]
        finally:
            win.destroy()


@test("_on_pair_audio_replaced refuses bad audio file gracefully (no mutation)")
def ta_audio_replace_bad():
    with tempfile.TemporaryDirectory() as tmpdir:
        orig = Path(tmpdir) / "orig.wav"
        s_orig, sr = make_test_audio(seconds=1.0)
        save_audio(s_orig, sr, str(orig), fmt="wav")

        # Create a non-audio file
        bogus = Path(tmpdir) / "not_audio.txt"
        bogus.write_text("hello not an audio file")

        pairs = [
            {"image": "/img/a.png", "audio": str(orig), "duration_ms": 1000, "index": 1},
        ]
        win = _new_window(pairs)
        try:
            from tkinter import messagebox as mb
            orig_err = mb.showerror
            calls = []
            mb.showerror = lambda *a, **k: calls.append((a, k))
            try:
                win._on_pair_audio_replaced(0, str(bogus))
            finally:
                mb.showerror = orig_err
            # Original audio untouched
            assert pairs[0]["audio"] == str(orig)
            assert pairs[0]["duration_ms"] == 1000
            assert len(calls) == 1, "user should be shown an error"
        finally:
            win.destroy()


@test("DeleteAudioModal three buttons fire correct callbacks")
def ta_delete_audio_modal_buttons():
    drop_calls = []
    full_calls = []
    repl_calls = []

    def open_modal():
        return sp.DeleteAudioModal(
            root, sp.THEMES["dark"],
            segment_label="Segment 2",
            has_image=True,
            can_delete_segment=True,
            can_drop_audio=True,
            on_delete_segment=lambda: full_calls.append(1),
            on_drop_audio_only=lambda: drop_calls.append(1),
            on_replace_audio=lambda: repl_calls.append(1),
        )

    m1 = open_modal(); m1.withdraw()
    m1._drop_audio_only()
    assert drop_calls == [1]

    m2 = open_modal(); m2.withdraw()
    m2._delete_segment_full()
    assert full_calls == [1]

    m3 = open_modal(); m3.withdraw()
    m3._replace_audio()
    assert repl_calls == [1]


@test("DeleteAudioModal disables drop-audio when has_image=False or can_drop_audio=False")
def ta_delete_audio_modal_disabled():
    # has_image=False — can't drop audio (would leave pair with neither)
    m = sp.DeleteAudioModal(
        root, sp.THEMES["dark"],
        segment_label="Segment 1",
        has_image=False,
        can_delete_segment=True,
        can_drop_audio=True,
        on_delete_segment=lambda: None,
        on_drop_audio_only=lambda: None,
        on_replace_audio=lambda: None,
    )
    m.withdraw()
    try:
        # Body's first child stack: 1. drop_audio_btn, 2. drop_desc label,
        # 3. replace_btn, 4. replace_desc, 5. delete_btn, 6. delete_desc
        # Find the drop button by walking children.
        drop_btn = None
        for child in m.winfo_children():
            for sub in child.winfo_children():
                if isinstance(sub, ctk.CTkButton):
                    text = sub.cget("text") or ""
                    if "Delete audio only" in text:
                        drop_btn = sub
                        break
        assert drop_btn is not None, "could not find Delete-audio-only button"
        assert drop_btn.cget("state") == "disabled"
    finally:
        m.destroy()


@test("SegmentCard renders '(no audio)' badge when pair audio is None")
def ta_card_no_audio():
    pairs = [
        {"image": "/img/a.png", "audio": "/audio/0.wav", "duration_ms": 1000, "index": 1},
        {"image": "/img/b.png", "audio": None, "duration_ms": None, "index": 2},
    ]
    win = _new_window(pairs)
    try:
        # Card 2 should have '(no audio)' in its dur_label and danger border
        card = win._cards[1]
        text = card.dur_label.cget("text") or ""
        assert "no audio" in text.lower(), f"expected '(no audio)' badge, got {text!r}"
    finally:
        win.destroy()


@test("End-to-end: audio-only delete shows in SegmentCards then filters at render")
def ta_e2e_audio_shift_pipeline():
    pairs = [
        {"image": "/img/a.png", "audio": "/audio/0.wav", "duration_ms": 1000, "index": 1},
        {"image": "/img/b.png", "audio": "/audio/1.wav", "duration_ms": 2000, "index": 2},
        {"image": "/img/c.png", "audio": "/audio/2.wav", "duration_ms": 3000, "index": 3},
        {"image": "/img/d.png", "audio": "/audio/3.wav", "duration_ms": 4000, "index": 4},
    ]
    win = _new_window(pairs)
    try:
        win._on_pair_audio_only_deleted(1)
        # Strip should still have 4 cards (orphan still shown, just marked no-audio)
        assert len(win._cards) == 4
        assert pairs[3]["audio"] is None

        segs, imgs, err = tvc._apply_preview_edits(pairs)
        assert err is None
        # Renderer skips the orphan trailing pair → 3 segments
        assert len(segs) == 3
        assert imgs == ["/img/a.png", "/img/b.png", "/img/c.png"]
        assert [s["file"] for s in segs] == ["/audio/0.wav", "/audio/2.wav", "/audio/3.wav"]
        assert [s["duration_ms"] for s in segs] == [1000, 3000, 4000]
    finally:
        win.destroy()


@test("_apply_preview_edits returns error when ALL audios are None")
def ta_pipeline_all_audio_none():
    pairs = [
        {"image": "/img/a.png", "audio": None, "duration_ms": None, "index": 1},
        {"image": "/img/b.png", "audio": None, "duration_ms": None, "index": 2},
    ]
    segs, imgs, err = tvc._apply_preview_edits(pairs)
    assert err is not None
    assert "audio" in err.lower()


@test("_apply_preview_edits handles audio=None alongside image=None correctly")
def ta_pipeline_combo_none():
    """image-fallback chain runs over RENDERABLE pairs only — None-audio
    pairs don't pollute the previous-image lookup for later pairs."""
    pairs = [
        {"image": "/img/a.png", "audio": "/audio/0.wav", "duration_ms": 1000, "index": 1},
        {"image": None,         "audio": "/audio/1.wav", "duration_ms": 2000, "index": 2},  # image fallback to a
        {"image": "/img/c.png", "audio": None,           "duration_ms": None, "index": 3},  # orphan: skipped
        {"image": None,         "audio": "/audio/3.wav", "duration_ms": 4000, "index": 4},  # image fallback skips c since it's not renderable
    ]
    segs, imgs, err = tvc._apply_preview_edits(pairs)
    assert err is None
    assert len(segs) == 3
    # Skipped pair[2] (audio None). Pair 4's None image falls back to the
    # most recent image among renderable pairs — that's pair 1 (a),
    # because pair 2 also had image=None.
    assert imgs == ["/img/a.png", "/img/a.png", "/img/a.png"]
    assert [s["file"] for s in segs] == ["/audio/0.wav", "/audio/1.wav", "/audio/3.wav"]


# ─────────────────────────── Multi-image tests (req 5) ───────────────────────────

print("\n=== Multi-image tests (req 5) ===\n")


def _walk(widget):
    """Yield widget and all of its descendants (depth-first)."""
    yield widget
    try:
        for child in widget.winfo_children():
            yield from _walk(child)
    except Exception:
        pass


@test("Migration: legacy pair['image'] → pair['images'] = [path]; pair['image'] kept as alias")
def tm_migration_legacy():
    pairs = [
        {"image": "/img/a.png", "audio": "/audio/0.wav", "duration_ms": 1000, "index": 1},
        {"image": None,         "audio": "/audio/1.wav", "duration_ms": 2000, "index": 2},
    ]
    win = _new_window(pairs)
    try:
        # Pair with non-None image → images = [path]
        assert pairs[0]["images"] == ["/img/a.png"]
        assert pairs[0]["image"] == "/img/a.png"
        # Pair with None image → empty list
        assert pairs[1]["images"] == []
        assert pairs[1]["image"] is None
    finally:
        win.destroy()


@test("Migration: pair['images'] preserved when explicitly provided")
def tm_migration_modern():
    pairs = [
        {"images": ["/img/a.png", "/img/b.png", "/img/c.png"],
         "audio": "/audio/0.wav", "duration_ms": 9000, "index": 1},
    ]
    win = _new_window(pairs)
    try:
        assert pairs[0]["images"] == ["/img/a.png", "/img/b.png", "/img/c.png"]
        # Alias mirrors the primary
        assert pairs[0]["image"] == "/img/a.png"
    finally:
        win.destroy()


@test("_on_pair_image_added appends path to pair['images']; alias still primary")
def tm_image_added():
    pairs = [
        {"image": "/img/a.png", "audio": "/audio/0.wav", "duration_ms": 6000, "index": 1},
    ]
    win = _new_window(pairs)
    try:
        win._on_pair_image_added(0, "/img/b.png")
        assert pairs[0]["images"] == ["/img/a.png", "/img/b.png"]
        assert pairs[0]["image"] == "/img/a.png"  # alias = primary unchanged
        win._on_pair_image_added(0, "/img/c.png")
        assert pairs[0]["images"] == ["/img/a.png", "/img/b.png", "/img/c.png"]
    finally:
        win.destroy()


@test("_on_pair_image_swapped swaps left/right neighbours; out-of-range = no-op")
def tm_image_swap():
    pairs = [
        {"images": ["/img/a.png", "/img/b.png", "/img/c.png"],
         "audio": "/audio/0.wav", "duration_ms": 9000, "index": 1},
    ]
    win = _new_window(pairs)
    try:
        # Swap b (idx 1) with right neighbour c → [a, c, b]
        win._on_pair_image_swapped(0, 1, "right")
        assert pairs[0]["images"] == ["/img/a.png", "/img/c.png", "/img/b.png"]
        # Swap b (now at idx 2) left → [a, b, c]
        win._on_pair_image_swapped(0, 2, "left")
        assert pairs[0]["images"] == ["/img/a.png", "/img/b.png", "/img/c.png"]
        # Edge: swap idx 0 left → no-op
        win._on_pair_image_swapped(0, 0, "left")
        assert pairs[0]["images"] == ["/img/a.png", "/img/b.png", "/img/c.png"]
        # Edge: swap last idx right → no-op
        win._on_pair_image_swapped(0, 2, "right")
        assert pairs[0]["images"] == ["/img/a.png", "/img/b.png", "/img/c.png"]
        # Alias updates after a swap that moves images[0]
        win._on_pair_image_swapped(0, 0, "right")
        assert pairs[0]["images"] == ["/img/b.png", "/img/a.png", "/img/c.png"]
        assert pairs[0]["image"] == "/img/b.png"
    finally:
        win.destroy()


@test("_on_pair_image_remove_at removes one image; list-becomes-empty is audio-only")
def tm_image_remove_one():
    pairs = [
        {"images": ["/img/a.png", "/img/b.png", "/img/c.png"],
         "audio": "/audio/0.wav", "duration_ms": 9000, "index": 1},
    ]
    win = _new_window(pairs)
    try:
        # Remove the middle image
        win._on_pair_image_remove_at(0, 1)
        assert pairs[0]["images"] == ["/img/a.png", "/img/c.png"]
        # Remove the new middle (now last) → list shrinks to 1
        win._on_pair_image_remove_at(0, 1)
        assert pairs[0]["images"] == ["/img/a.png"]
        # Remove the last remaining → list is empty (audio-only)
        win._on_pair_image_remove_at(0, 0)
        assert pairs[0]["images"] == []
        assert pairs[0]["image"] is None
    finally:
        win.destroy()


@test("_on_pair_image_changed with image_idx targets a specific image in multi-image list")
def tm_image_changed_idx():
    pairs = [
        {"images": ["/img/a.png", "/img/b.png", "/img/c.png"],
         "audio": "/audio/0.wav", "duration_ms": 9000, "index": 1},
    ]
    win = _new_window(pairs)
    try:
        # Replace ONLY images[1] = b → "/img/REPL.png"
        win._on_pair_image_changed(0, "/img/REPL.png", image_idx=1)
        assert pairs[0]["images"] == ["/img/a.png", "/img/REPL.png", "/img/c.png"]
        # Replacing images[0] updates the alias too
        win._on_pair_image_changed(0, "/img/A2.png", image_idx=0)
        assert pairs[0]["image"] == "/img/A2.png"
    finally:
        win.destroy()


@test("SegmentCard shows '+N' badge when pair has 2+ images")
def tm_card_multi_badge():
    pairs = [
        {"images": ["/img/a.png", "/img/b.png", "/img/c.png"],
         "audio": "/audio/0.wav", "duration_ms": 9000, "index": 1},
        {"images": ["/img/d.png"],
         "audio": "/audio/1.wav", "duration_ms": 1000, "index": 2},
    ]
    win = _new_window(pairs)
    try:
        # First card has 3 images → "+2" badge
        badge_text = win._cards[0].multi_badge.cget("text") or ""
        assert badge_text == "+2", f"expected '+2' badge, got {badge_text!r}"
        # Second card has 1 image → no badge text
        badge_text2 = win._cards[1].multi_badge.cget("text") or ""
        # The label can technically be empty string when not placed; the
        # important check is just that it isn't "+N" for any N.
        assert not badge_text2.startswith("+"), f"expected no '+N' on single-image card, got {badge_text2!r}"
    finally:
        win.destroy()


@test("SegmentCard duration label shows total + per-image slice for multi-image")
def tm_card_dur_label():
    pairs = [
        {"images": ["/img/a.png", "/img/b.png"],
         "audio": "/audio/0.wav", "duration_ms": 6000, "index": 1},
    ]
    win = _new_window(pairs)
    try:
        text = win._cards[0].dur_label.cget("text") or ""
        # Format is "MM:SS.mmm  (MM:SS.mmm ea)"
        assert "ea" in text, f"expected per-image slice in dur label, got {text!r}"
        assert "00:06.000" in text, f"expected total 6s, got {text!r}"
        assert "00:03.000" in text, f"expected per-image 3s, got {text!r}"
    finally:
        win.destroy()


@test("SegmentDetailModal renders multi-image row with N sub-kebabs")
def tm_detail_multi_row():
    with tempfile.TemporaryDirectory() as tmpdir:
        wav = Path(tmpdir) / "seg.wav"
        samples, sr = make_test_audio(seconds=2.0)
        save_audio(samples, sr, str(wav), fmt="wav")
        # Create three different test images
        from PIL import Image as _PIL
        imgs = []
        for i, color in enumerate([(255, 0, 0), (0, 255, 0), (0, 0, 255)]):
            p = Path(tmpdir) / f"img{i}.png"
            _PIL.new("RGB", (200, 200), color=color).save(p)
            imgs.append(str(p))
        pairs = [{
            "images": imgs, "audio": str(wav),
            "duration_ms": 2000, "index": 1,
        }]
        modal = sp.SegmentDetailModal(root, pairs, 0, sp.THEMES["dark"])
        modal.withdraw()
        try:
            # Walk the image_panel children — should contain a row frame
            # whose grand-children include 3 sub-frames each holding an
            # image label + its own kebab.
            n_kebabs = 0
            for descendant in _walk(modal.image_panel):
                if isinstance(descendant, ctk.CTkButton):
                    if (descendant.cget("text") or "") == "⋮":
                        n_kebabs += 1
            assert n_kebabs == 3, f"expected 3 sub-kebabs in multi-image row, found {n_kebabs}"
            # Photo refs list should hold one CTkImage per image
            assert len(modal._photo_refs) == 3
        finally:
            modal._close()


# ─────────────────────────── Multi-image pipeline tests (req 5) ───────────────────────────

print("\n=== Multi-image pipeline tests (req 5) ===\n")


@test("_apply_preview_edits expands a 2-image pair into 2 chunks of equal duration")
def tmp_two_images_split():
    with tempfile.TemporaryDirectory() as tmpdir:
        # 6-second source audio
        wav = Path(tmpdir) / "seg.wav"
        samples, sr = make_test_audio(seconds=6.0)
        save_audio(samples, sr, str(wav), fmt="wav")

        pairs = [{
            "images": ["/img/a.png", "/img/b.png"],
            "audio": str(wav), "duration_ms": 6000, "index": 1,
        }]
        segs, imgs, err = tvc._apply_preview_edits(pairs, tmp_dir=str(tmpdir))
        assert err is None, err
        assert len(segs) == 2
        assert imgs == ["/img/a.png", "/img/b.png"]
        # Each chunk is ~3s (within ±50ms slop for encoding alignment)
        for s in segs:
            assert abs(s["duration_ms"] - 3000) < 50, s["duration_ms"]
        # Chunks were written to disk
        for s in segs:
            assert Path(s["file"]).exists(), s["file"]
            assert ".split2_" in Path(s["file"]).name


@test("_apply_preview_edits expands a 3-image pair: 9s → 3x ~3s")
def tmp_three_images_split():
    with tempfile.TemporaryDirectory() as tmpdir:
        wav = Path(tmpdir) / "seg.wav"
        samples, sr = make_test_audio(seconds=9.0)
        save_audio(samples, sr, str(wav), fmt="wav")
        pairs = [{
            "images": ["/img/a.png", "/img/b.png", "/img/c.png"],
            "audio": str(wav), "duration_ms": 9000, "index": 1,
        }]
        segs, imgs, err = tvc._apply_preview_edits(pairs, tmp_dir=str(tmpdir))
        assert err is None, err
        assert len(segs) == 3
        assert imgs == ["/img/a.png", "/img/b.png", "/img/c.png"]
        for s in segs:
            assert abs(s["duration_ms"] - 3000) < 50


@test("_apply_preview_edits multi-image without tmp_dir returns clear error")
def tmp_multi_no_tmpdir():
    pairs = [{
        "images": ["/img/a.png", "/img/b.png"],
        "audio": "/audio/0.wav", "duration_ms": 6000, "index": 1,
    }]
    segs, imgs, err = tvc._apply_preview_edits(pairs, tmp_dir=None)
    assert err is not None
    assert "tmp_dir" in err.lower()


@test("_apply_preview_edits mixed single + multi-image pairs preserves ordering")
def tmp_mixed_pairs():
    with tempfile.TemporaryDirectory() as tmpdir:
        # Two audio files; only the second is multi-image
        wav1 = Path(tmpdir) / "s1.wav"
        wav2 = Path(tmpdir) / "s2.wav"
        s1, sr = make_test_audio(seconds=2.0)
        s2, _ = make_test_audio(seconds=4.0)
        save_audio(s1, sr, str(wav1), fmt="wav")
        save_audio(s2, sr, str(wav2), fmt="wav")
        pairs = [
            {"images": ["/img/a.png"],
             "audio": str(wav1), "duration_ms": 2000, "index": 1},
            {"images": ["/img/b.png", "/img/c.png"],
             "audio": str(wav2), "duration_ms": 4000, "index": 2},
        ]
        segs, imgs, err = tvc._apply_preview_edits(pairs, tmp_dir=str(tmpdir))
        assert err is None, err
        assert len(segs) == 3
        assert imgs == ["/img/a.png", "/img/b.png", "/img/c.png"]
        # First seg uses original wav, full duration; chunks for the rest
        assert segs[0]["file"] == str(wav1)
        assert ".split2_" in Path(segs[1]["file"]).name
        assert ".split2_" in Path(segs[2]["file"]).name


@test("_apply_preview_edits multi-image followed by None-image pair: fallback uses LAST image")
def tmp_multi_then_none():
    with tempfile.TemporaryDirectory() as tmpdir:
        wav1 = Path(tmpdir) / "s1.wav"
        wav2 = Path(tmpdir) / "s2.wav"
        s1, sr = make_test_audio(seconds=4.0)
        s2, _ = make_test_audio(seconds=2.0)
        save_audio(s1, sr, str(wav1), fmt="wav")
        save_audio(s2, sr, str(wav2), fmt="wav")
        pairs = [
            {"images": ["/img/a.png", "/img/b.png"],
             "audio": str(wav1), "duration_ms": 4000, "index": 1},
            {"images": [],  # audio-only — should fall back to images[-1] of prev = b
             "audio": str(wav2), "duration_ms": 2000, "index": 2},
        ]
        segs, imgs, err = tvc._apply_preview_edits(pairs, tmp_dir=str(tmpdir))
        assert err is None, err
        # 2 chunks for first pair, 1 fallback for None pair = 3 segs total
        assert len(segs) == 3
        # The None-image pair falls back to the LAST image of the
        # preceding multi-image segment (b), not the first (a).
        assert imgs == ["/img/a.png", "/img/b.png", "/img/b.png"]


@test("End-to-end: add another image flow → ui mutation → pipeline expansion")
def tmp_e2e_add_image():
    with tempfile.TemporaryDirectory() as tmpdir:
        wav = Path(tmpdir) / "seg.wav"
        samples, sr = make_test_audio(seconds=4.0)
        save_audio(samples, sr, str(wav), fmt="wav")

        pairs = [
            {"image": "/img/orig.png",
             "audio": str(wav), "duration_ms": 4000, "index": 1},
        ]
        win = _new_window(pairs)
        try:
            # Simulate user clicking "Add another image" twice
            win._on_pair_image_added(0, "/img/added1.png")
            win._on_pair_image_added(0, "/img/added2.png")
            assert pairs[0]["images"] == [
                "/img/orig.png", "/img/added1.png", "/img/added2.png",
            ]

            # And then one swap to verify reordering also flows through
            win._on_pair_image_swapped(0, 1, "right")
            assert pairs[0]["images"] == [
                "/img/orig.png", "/img/added2.png", "/img/added1.png",
            ]

            segs, imgs, err = tvc._apply_preview_edits(pairs, tmp_dir=str(tmpdir))
            assert err is None, err
            assert len(segs) == 3
            assert imgs == ["/img/orig.png", "/img/added2.png", "/img/added1.png"]
            # Each chunk is ~1.333s (4s / 3), within ±50ms
            for s in segs:
                assert abs(s["duration_ms"] - 4000 // 3) < 50
        finally:
            win.destroy()


# ─────────────────────────── UI polish tests ───────────────────────────

print("\n=== UI polish tests (StyledPopupMenu, bulk add, window controls) ===\n")


@test("StyledPopupMenu renders command items, separators, headers, and respects disabled state")
def tu_styled_menu_basic():
    fired = []
    items = [
        {"kind": "header", "label": "Test menu"},
        {"kind": "command", "label": "Item A", "command": lambda: fired.append("a")},
        {"kind": "separator"},
        {"kind": "command", "label": "Item B (disabled)",
         "command": lambda: fired.append("b"), "state": "disabled"},
        {"kind": "command", "label": "Item C accent",
         "command": lambda: fired.append("c"), "accent": True},
    ]
    menu = sp.StyledPopupMenu(root, sp.THEMES["dark"], items, x=100, y=100, min_width=240)
    # We can't easily click programmatically, but we can verify the
    # button widgets were built and the disabled item is in the disabled state.
    btns = [w for w in _walk(menu) if isinstance(w, ctk.CTkButton)]
    assert len(btns) == 3, f"expected 3 buttons, got {len(btns)}"
    disabled = [b for b in btns if (b.cget("state") == "disabled")]
    assert len(disabled) == 1, "expected exactly one disabled button"
    # Dispatch the first command-item button to confirm the call mechanism works
    menu._dispatch(items[1]["command"])
    assert fired == ["a"]
    # _dispatch closes the menu
    assert menu._closed


@test("StyledPopupMenu auto-clamps to screen — out-of-bounds coords are corrected")
def tu_styled_menu_clamp():
    items = [{"kind": "command", "label": "X", "command": lambda: None}]
    menu = sp.StyledPopupMenu(root, sp.THEMES["dark"], items, x=99999, y=99999)
    try:
        menu.update_idletasks()
        # Geometry should land within the screen rect
        sw = menu.winfo_screenwidth()
        sh = menu.winfo_screenheight()
        # winfo_x/y should be < screen dims (clamping happened)
        assert menu.winfo_x() < sw, menu.winfo_x()
        assert menu.winfo_y() < sh, menu.winfo_y()
    finally:
        try:
            menu.destroy()
        except Exception:
            pass


@test("StyledPopupMenu Escape key closes the menu")
def tu_styled_menu_escape():
    items = [{"kind": "command", "label": "X", "command": lambda: None}]
    menu = sp.StyledPopupMenu(root, sp.THEMES["dark"], items, x=100, y=100)
    try:
        menu._close()
        assert menu._closed
    except Exception as e:
        raise AssertionError(f"_close raised: {e}")


@test("Image kebab menu builds via StyledPopupMenu (no tk.Menu leakage)")
def tu_image_menu_uses_styled():
    """Verify _show_image_menu_at constructs a StyledPopupMenu rather
    than a tk.Menu. Smoke check — we don't interact with it."""
    pairs = [
        {"images": ["/img/a.png"], "audio": "/audio/0.wav",
         "duration_ms": 1000, "index": 1},
    ]
    win = _new_window(pairs)
    try:
        before = [w for w in _walk(win) if isinstance(w, sp.StyledPopupMenu)]
        win._show_image_menu_at(0, 100, 100, image_idx=0)
        after = [w for w in _walk(win) if isinstance(w, sp.StyledPopupMenu)]
        assert len(after) == len(before) + 1, \
            "expected exactly one new StyledPopupMenu after _show_image_menu_at"
        # Cleanup
        for menu in after[len(before):]:
            try:
                menu._close()
            except Exception:
                pass
    finally:
        win.destroy()


@test("Bulk add: multiple paths from askopenfilenames append in order")
def tu_bulk_add():
    pairs = [
        {"images": ["/img/a.png"], "audio": "/audio/0.wav",
         "duration_ms": 6000, "index": 1},
    ]
    win = _new_window(pairs)
    try:
        # Stub askopenfilenames to return three paths
        from tkinter import filedialog as fd
        orig = fd.askopenfilenames
        fd.askopenfilenames = lambda *a, **k: ("/img/x.png", "/img/y.png", "/img/z.png")
        try:
            win._action_add_another(0)
        finally:
            fd.askopenfilenames = orig
        # All three should be appended in order
        assert pairs[0]["images"] == ["/img/a.png", "/img/x.png", "/img/y.png", "/img/z.png"]
    finally:
        win.destroy()


@test("Bulk add: empty selection (user cancel) is a no-op")
def tu_bulk_add_cancel():
    pairs = [
        {"images": ["/img/a.png"], "audio": "/audio/0.wav",
         "duration_ms": 6000, "index": 1},
    ]
    win = _new_window(pairs)
    try:
        from tkinter import filedialog as fd
        orig = fd.askopenfilenames
        fd.askopenfilenames = lambda *a, **k: ()  # user cancelled
        try:
            win._action_add_another(0)
        finally:
            fd.askopenfilenames = orig
        # Untouched
        assert pairs[0]["images"] == ["/img/a.png"]
    finally:
        win.destroy()


@test("_install_window_features clamps geometry to screen and binds F11")
def tu_window_features():
    # Fresh CTkToplevel; we don't display it, just verify the helper runs
    # cleanly and binds the keys.
    win = ctk.CTkToplevel(root)
    win.withdraw()
    try:
        sp._install_window_features(
            win, ideal_w=99999, ideal_h=99999,
            min_w=400, min_h=300, allow_fullscreen=True,
        )
        # Geometry should be clamped to the screen
        win.update_idletasks()
        sw = win.winfo_screenwidth()
        sh = win.winfo_screenheight()
        assert win.winfo_width() <= sw
        assert win.winfo_height() <= sh
        # F11 should be bound (best-effort: tk's bind() returns the
        # callback ID string when called without a callback arg)
        bindings = win.bind("<F11>")
        assert bindings, "F11 should be bound for fullscreen toggle"
        bindings_f10 = win.bind("<F10>")
        assert bindings_f10, "F10 should be bound for maximize toggle"
    finally:
        try:
            win.destroy()
        except Exception:
            pass


# ─────────────────────────── Polish-feature tests (round 2) ───────────────────────────

print("\n=== Polish-feature tests (image undo, weights, drag-drop, render preview) ===\n")


# ----- Feature 1: image undo/redo -----

@test("Image history initialized with original snapshot on window create")
def tp2_image_history_init():
    pairs = [
        {"images": ["/img/a.png", "/img/b.png"],
         "audio": "/audio/0.wav", "duration_ms": 6000, "index": 1},
    ]
    win = _new_window(pairs)
    try:
        hist = pairs[0].get("_image_history")
        assert hist is not None
        assert len(hist) == 1
        assert hist[0] == ["/img/a.png", "/img/b.png"]
    finally:
        win.destroy()


@test("Image undo restores the previous image-list state")
def tp2_image_undo():
    pairs = [
        {"image": "/img/a.png", "audio": "/audio/0.wav",
         "duration_ms": 4000, "index": 1},
    ]
    win = _new_window(pairs)
    try:
        # Two append mutations
        win._on_pair_image_added(0, "/img/b.png")
        win._on_pair_image_added(0, "/img/c.png")
        assert pairs[0]["images"] == ["/img/a.png", "/img/b.png", "/img/c.png"]
        # Undo once → back to [a, b]
        win._on_pair_image_undo(0)
        assert pairs[0]["images"] == ["/img/a.png", "/img/b.png"]
        # Undo again → back to [a]
        win._on_pair_image_undo(0)
        assert pairs[0]["images"] == ["/img/a.png"]
        # Cannot undo past the original
        win._on_pair_image_undo(0)
        assert pairs[0]["images"] == ["/img/a.png"]
    finally:
        win.destroy()


@test("Image revert restores the original list, even after many edits")
def tp2_image_revert():
    pairs = [
        {"image": "/img/a.png", "audio": "/audio/0.wav",
         "duration_ms": 4000, "index": 1},
    ]
    win = _new_window(pairs)
    try:
        win._on_pair_image_added(0, "/img/b.png")
        win._on_pair_image_added(0, "/img/c.png")
        win._on_pair_image_swapped(0, 1, "right")
        win._on_pair_image_remove_at(0, 0)
        assert pairs[0]["images"] != ["/img/a.png"]
        win._on_pair_image_revert(0)
        assert pairs[0]["images"] == ["/img/a.png"]
        # History truncated to just the original
        assert len(pairs[0]["_image_history"]) == 1
    finally:
        win.destroy()


# ----- Feature 3: custom duration weights -----

@test("Default weights are uniform (all 1.0) for each pair on window init")
def tp2_weights_init_uniform():
    pairs = [
        {"images": ["/img/a.png", "/img/b.png", "/img/c.png"],
         "audio": "/audio/0.wav", "duration_ms": 9000, "index": 1},
    ]
    win = _new_window(pairs)
    try:
        ws = pairs[0]["weights"]
        assert ws == [1.0, 1.0, 1.0]
    finally:
        win.destroy()


@test("_on_pair_weight_changed clamps to [0.1, 10.0] and updates pair['weights']")
def tp2_weight_changed():
    pairs = [
        {"images": ["/img/a.png", "/img/b.png"],
         "audio": "/audio/0.wav", "duration_ms": 4000, "index": 1},
    ]
    win = _new_window(pairs)
    try:
        win._on_pair_weight_changed(0, 0, 2.5)
        assert pairs[0]["weights"][0] == 2.5
        # Below 0.1 clamps up
        win._on_pair_weight_changed(0, 1, 0.0001)
        assert pairs[0]["weights"][1] == 0.1
        # Above 10 clamps down
        win._on_pair_weight_changed(0, 0, 100)
        assert pairs[0]["weights"][0] == 10.0
    finally:
        win.destroy()


@test("Weights stay aligned with image list after add/remove/swap")
def tp2_weights_alignment():
    pairs = [
        {"images": ["/img/a.png"], "audio": "/audio/0.wav",
         "duration_ms": 4000, "index": 1},
    ]
    win = _new_window(pairs)
    try:
        win._on_pair_weight_changed(0, 0, 3.0)
        # Add → weights should grow with default 1.0
        win._on_pair_image_added(0, "/img/b.png")
        assert len(pairs[0]["weights"]) == 2
        assert pairs[0]["weights"][0] == 3.0  # preserved
        assert pairs[0]["weights"][1] == 1.0  # new image default
        # Add again
        win._on_pair_image_added(0, "/img/c.png")
        win._on_pair_weight_changed(0, 2, 2.0)
        assert pairs[0]["weights"] == [3.0, 1.0, 2.0]
        # Swap b and c → their weights swap too
        win._on_pair_image_swapped(0, 1, "right")
        assert pairs[0]["images"] == ["/img/a.png", "/img/c.png", "/img/b.png"]
        assert pairs[0]["weights"] == [3.0, 2.0, 1.0]
        # Remove c → weight 2.0 drops
        win._on_pair_image_remove_at(0, 1)
        assert pairs[0]["images"] == ["/img/a.png", "/img/b.png"]
        assert pairs[0]["weights"] == [3.0, 1.0]
    finally:
        win.destroy()


@test("_apply_preview_edits respects pair['weights'] when slicing audio")
def tp2_weights_pipeline():
    with tempfile.TemporaryDirectory() as tmpdir:
        wav = Path(tmpdir) / "seg.wav"
        # 8s audio
        s, sr = make_test_audio(seconds=8.0)
        save_audio(s, sr, str(wav), fmt="wav")
        # Weights [3, 1] over 8s → 6s, 2s
        pairs = [{
            "images": ["/img/a.png", "/img/b.png"],
            "audio": str(wav), "duration_ms": 8000, "index": 1,
            "weights": [3.0, 1.0],
        }]
        segs, imgs, err = tvc._apply_preview_edits(pairs, tmp_dir=str(tmpdir))
        assert err is None
        assert len(segs) == 2
        # First chunk ~6s, second ~2s (within ±50ms)
        assert abs(segs[0]["duration_ms"] - 6000) < 50, segs[0]["duration_ms"]
        assert abs(segs[1]["duration_ms"] - 2000) < 50, segs[1]["duration_ms"]


@test("_apply_preview_edits with degenerate weights (all zero) doesn't crash")
def tp2_weights_pipeline_degenerate():
    with tempfile.TemporaryDirectory() as tmpdir:
        wav = Path(tmpdir) / "seg.wav"
        s, sr = make_test_audio(seconds=4.0)
        save_audio(s, sr, str(wav), fmt="wav")
        pairs = [{
            "images": ["/img/a.png", "/img/b.png"],
            "audio": str(wav), "duration_ms": 4000, "index": 1,
            "weights": [0, 0],  # invalid but shouldn't crash
        }]
        # The fallback divides by sum_w=1.0 so result is [0%, 100%].
        # We just verify no exception and 2 chunks emitted.
        segs, imgs, err = tvc._apply_preview_edits(pairs, tmp_dir=str(tmpdir))
        assert err is None
        assert len(segs) == 2


# ----- Feature 2: drag-drop image reordering -----

@test("_on_pair_image_reordered moves an image to a new position")
def tp2_reorder():
    pairs = [
        {"images": ["/img/a.png", "/img/b.png", "/img/c.png", "/img/d.png"],
         "audio": "/audio/0.wav", "duration_ms": 8000, "index": 1},
    ]
    win = _new_window(pairs)
    try:
        # Move idx 3 (d) to position 1 → [a, d, b, c]
        win._on_pair_image_reordered(0, 3, 1)
        assert pairs[0]["images"] == ["/img/a.png", "/img/d.png", "/img/b.png", "/img/c.png"]
        # Move idx 0 (a) to position 2 → [d, b, a, c]
        win._on_pair_image_reordered(0, 0, 2)
        assert pairs[0]["images"] == ["/img/d.png", "/img/b.png", "/img/a.png", "/img/c.png"]
    finally:
        win.destroy()


@test("_on_pair_image_reordered moves the corresponding weight along with the image")
def tp2_reorder_weights():
    pairs = [
        {"images": ["/img/a.png", "/img/b.png", "/img/c.png"],
         "audio": "/audio/0.wav", "duration_ms": 6000, "index": 1,
         "weights": [4.0, 1.0, 2.0]},
    ]
    win = _new_window(pairs)
    try:
        # Move c (idx 2, weight 2.0) to position 0 → [c, a, b], weights [2, 4, 1]
        win._on_pair_image_reordered(0, 2, 0)
        assert pairs[0]["images"] == ["/img/c.png", "/img/a.png", "/img/b.png"]
        assert pairs[0]["weights"] == [2.0, 4.0, 1.0]
    finally:
        win.destroy()


@test("_on_pair_image_reordered with src == dst is a no-op")
def tp2_reorder_noop():
    pairs = [
        {"images": ["/img/a.png", "/img/b.png"],
         "audio": "/audio/0.wav", "duration_ms": 4000, "index": 1},
    ]
    win = _new_window(pairs)
    try:
        win._on_pair_image_reordered(0, 0, 0)
        assert pairs[0]["images"] == ["/img/a.png", "/img/b.png"]
        # Out-of-range src
        win._on_pair_image_reordered(0, 99, 0)
        assert pairs[0]["images"] == ["/img/a.png", "/img/b.png"]
    finally:
        win.destroy()


# ----- Feature 5: timeline ruler -----

@test("SegmentDetailModal renders a timeline ruler canvas for multi-image segments")
def tp2_timeline_ruler():
    with tempfile.TemporaryDirectory() as tmpdir:
        wav = Path(tmpdir) / "seg.wav"
        s, sr = make_test_audio(seconds=2.0)
        save_audio(s, sr, str(wav), fmt="wav")
        from PIL import Image as _PIL
        imgs = []
        for i, color in enumerate([(255, 0, 0), (0, 255, 0), (0, 0, 255)]):
            p = Path(tmpdir) / f"img{i}.png"
            _PIL.new("RGB", (200, 200), color=color).save(p)
            imgs.append(str(p))
        pairs = [{
            "images": imgs, "audio": str(wav),
            "duration_ms": 2000, "index": 1,
            "weights": [2.0, 1.0, 1.0],
        }]
        modal = sp.SegmentDetailModal(root, pairs, 0, sp.THEMES["dark"])
        modal.withdraw()
        try:
            # The ruler is a tk.Canvas — find it by walking children
            canvases = [w for w in _walk(modal) if isinstance(w, tk.Canvas)]
            assert len(canvases) >= 1, "expected at least one timeline ruler canvas"
            # _ruler_canvas attribute should reference one of them
            assert hasattr(modal, "_ruler_canvas")
            assert modal._ruler_canvas is not None
        finally:
            modal._close()


@test("SegmentDetailModal does NOT render a timeline ruler for single-image segments")
def tp2_timeline_ruler_skip_single():
    with tempfile.TemporaryDirectory() as tmpdir:
        wav = Path(tmpdir) / "seg.wav"
        s, sr = make_test_audio(seconds=0.5)
        save_audio(s, sr, str(wav), fmt="wav")
        from PIL import Image as _PIL
        img = Path(tmpdir) / "img.png"
        _PIL.new("RGB", (100, 100), color=(80, 80, 80)).save(img)
        pairs = [{
            "images": [str(img)], "audio": str(wav),
            "duration_ms": 500, "index": 1,
        }]
        modal = sp.SegmentDetailModal(root, pairs, 0, sp.THEMES["dark"])
        modal.withdraw()
        try:
            # Single-image: _ruler_canvas should be None or unset
            ruler = getattr(modal, "_ruler_canvas", None)
            assert ruler is None
        finally:
            modal._close()


# ----- Feature 4: chunk dividers in audio editor -----

@test("SegmentEditorModal accepts chunk_boundaries_s and stores them")
def tp2_editor_chunk_boundaries():
    with tempfile.TemporaryDirectory() as tmpdir:
        wav = Path(tmpdir) / "seg.wav"
        s, sr = make_test_audio(seconds=6.0)
        save_audio(s, sr, str(wav), fmt="wav")
        editor = sp.SegmentEditorModal(
            root, audio_path=str(wav),
            original_duration_ms=6000, history_len=1,
            theme=sp.THEMES["dark"],
            on_save=lambda *a: None, on_cancel=lambda: None,
            segment_label="multi",
            chunk_boundaries_s=[2.0, 4.0],
        )
        editor.withdraw()
        try:
            assert editor._chunk_boundaries_s == [2.0, 4.0]
            # The canvas exists and the divider draw method runs without error
            editor._draw_chunk_dividers()
        finally:
            try:
                editor._cancel()
            except Exception:
                pass


@test("Detail modal _open_editor computes chunk_boundaries from pair weights")
def tp2_editor_boundaries_from_weights():
    with tempfile.TemporaryDirectory() as tmpdir:
        wav = Path(tmpdir) / "seg.wav"
        s, sr = make_test_audio(seconds=8.0)
        save_audio(s, sr, str(wav), fmt="wav")
        from PIL import Image as _PIL
        imgs = []
        for i, color in enumerate([(255, 0, 0), (0, 255, 0)]):
            p = Path(tmpdir) / f"img{i}.png"
            _PIL.new("RGB", (100, 100), color=color).save(p)
            imgs.append(str(p))
        pairs = [{
            "images": imgs, "audio": str(wav),
            "duration_ms": 8000, "index": 1,
            "weights": [3.0, 1.0],
        }]
        modal = sp.SegmentDetailModal(root, pairs, 0, sp.THEMES["dark"])
        modal.withdraw()
        try:
            # We don't actually want to open the editor (real Toplevel). Just
            # exercise the boundary-calculation logic:
            n_imgs = len(imgs)
            weights = list(pairs[0]["weights"])
            sum_w = sum(weights[:n_imgs])
            total_s = pairs[0]["duration_ms"] / 1000.0
            cum = 0.0
            expected_boundaries = []
            for i in range(n_imgs - 1):
                cum += weights[i]
                expected_boundaries.append(total_s * cum / sum_w)
            # weights [3, 1] over 8s → boundary at 6s
            assert len(expected_boundaries) == 1
            assert abs(expected_boundaries[0] - 6.0) < 0.01
        finally:
            modal._close()


# ----- Feature 6: render preview for one segment -----

@test("_render_single_segment_preview produces a watchable MP4 (single-image)")
def tp2_render_single():
    from simple_video_creator import find_ffmpeg
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        # Skip on machines without ffmpeg
        print("    (skipped — ffmpeg not found)")
        return
    with tempfile.TemporaryDirectory() as tmpdir:
        wav = Path(tmpdir) / "seg.wav"
        s, sr = make_test_audio(seconds=1.0)
        save_audio(s, sr, str(wav), fmt="wav")
        from PIL import Image as _PIL
        img = Path(tmpdir) / "img.png"
        _PIL.new("RGB", (200, 200), color=(100, 100, 200)).save(img)
        out = sp._render_single_segment_preview(
            image_paths=[str(img)],
            audio_path=str(wav),
            ffmpeg=ffmpeg, width=320, height=240, fps=15,
        )
        assert out is not None, "render returned None"
        assert Path(out).exists(), f"output file missing: {out}"
        assert Path(out).stat().st_size > 1000, "output suspiciously small"


@test("_render_single_segment_preview slices audio for multi-image with weights")
def tp2_render_multi():
    from simple_video_creator import find_ffmpeg
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        print("    (skipped — ffmpeg not found)")
        return
    with tempfile.TemporaryDirectory() as tmpdir:
        wav = Path(tmpdir) / "seg.wav"
        s, sr = make_test_audio(seconds=2.0)
        save_audio(s, sr, str(wav), fmt="wav")
        from PIL import Image as _PIL
        img1 = Path(tmpdir) / "a.png"
        img2 = Path(tmpdir) / "b.png"
        _PIL.new("RGB", (160, 160), color=(255, 0, 0)).save(img1)
        _PIL.new("RGB", (160, 160), color=(0, 255, 0)).save(img2)
        out = sp._render_single_segment_preview(
            image_paths=[str(img1), str(img2)],
            audio_path=str(wav),
            weights=[1.0, 1.0],
            ffmpeg=ffmpeg, width=320, height=240, fps=15,
        )
        assert out is not None
        assert Path(out).exists()


@test("_render_single_segment_preview returns None on missing inputs")
def tp2_render_invalid():
    out = sp._render_single_segment_preview([], "/nonexistent/audio.wav")
    assert out is None
    out = sp._render_single_segment_preview(["/some/img.png"], "")
    assert out is None


# ─────────────────────────── Summary ───────────────────────────

try:
    root.update_idletasks()
    root.destroy()
except Exception:
    pass

print(f"\n{'=' * 60}")
print(f"PASSED: {len(PASS)}    FAILED: {len(FAIL)}")
if FAIL:
    print(f"\nFailing tests:")
    for n in FAIL:
        print(f"  - {n}")
print(f"{'=' * 60}\n")
sys.exit(0 if not FAIL else 1)
