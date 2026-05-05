#!/usr/bin/env python3
"""
Segment preview gallery — dark-themed modal that lets the user verify
each (image, audio_segment) pair before the final video render.

Used by tone_video_creator.py: after segmentation but before the MP4
encode, we open a preview window with a horizontal strip of cards
([1] → [2] → [3] → … → [N]). Click any card to open a big modal
showing that segment's image and an audio player. Left/Right arrows
flip to the prev/next pair without closing.

The public entry point is `show_preview_blocking(root, pairs)`. It
must be called from a NON-UI thread (the pipeline worker), and
internally bounces the UI build onto the Tk main thread via
root.after(). Returns True if the user clicked Continue, False if
they cancelled / closed the window.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import List, Dict, Optional, Callable

import numpy as np
from PIL import Image
from pydub import AudioSegment

import tkinter as tk
from tkinter import messagebox, filedialog

import customtkinter as ctk
import sounddevice as sd

# Reuse the voice editor's reusable pieces — Project (cut model + render),
# Cut, Player (sounddevice playback w/ skip regions), WaveformCanvas
# (paint cuts on the waveform), compute_peaks, load_audio, save_audio,
# fmt_time. They were written as standalone components in voice_editor.py.
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
from voice_editor import (  # noqa: E402
    Project, Cut, Player, WaveformCanvas,
    compute_peaks, load_audio, save_audio, fmt_time,
)


# ─────────────────────────── Theme ───────────────────────────
# Same palette as voice_editor.py / launcher.py so the toolkit feels like
# one product. Duplicated rather than imported to avoid pulling either of
# those modules in just for their dict.
THEMES = {
    "dark": {
        "appearance": "dark",
        "bg": "#0F1117",
        "panel": "#181B25",
        "panel2": "#222637",
        "stroke": "#2C3145",
        "text": "#F5F7FA",
        "muted": "#8B92A6",
        "accent": "#7C5CFF",     # violet
        "accent2": "#22D3EE",    # cyan
        "accent3": "#F472B6",    # pink
        "danger": "#F43F5E",
        "ok": "#34D399",
    },
    "light": {
        "appearance": "light",
        "bg": "#F5F6FB",
        "panel": "#FFFFFF",
        "panel2": "#EEF0F8",
        "stroke": "#D9DDEA",
        "text": "#0F1117",
        "muted": "#5C6478",
        "accent": "#6D4AFF",
        "accent2": "#0891B2",
        "accent3": "#DB2777",
        "danger": "#E11D48",
        "ok": "#059669",
    },
}


def hex_lerp(c1: str, c2: str, t: float) -> str:
    """Linear blend between two #rrggbb hex colors."""
    t = max(0.0, min(1.0, t))
    r1, g1, b1 = int(c1[1:3], 16), int(c1[3:5], 16), int(c1[5:7], 16)
    r2, g2, b2 = int(c2[1:3], 16), int(c2[3:5], 16), int(c2[5:7], 16)
    r = int(r1 + (r2 - r1) * t)
    g = int(g1 + (g2 - g1) * t)
    b = int(b1 + (b2 - b1) * t)
    return f"#{r:02x}{g:02x}{b:02x}"


def _fmt_s(s: float) -> str:
    s = max(0.0, float(s))
    m = int(s // 60)
    rem = s - m * 60
    return f"{m:02d}:{rem:06.3f}"


def fmt_ms(ms: Optional[int]) -> str:
    if ms is None:
        return "--:--.---"
    return _fmt_s(ms / 1000.0)


# ─────────────────────────── Window helpers ───────────────────────────

def _install_window_features(window, ideal_w=None, ideal_h=None,
                             min_w=None, min_h=None,
                             allow_fullscreen=True):
    """Apply adaptive geometry + maximize/fullscreen keybinds to a Toplevel.

    - Initial size = min(ideal, 92% of screen).
    - Centered horizontally; biased toward the upper third vertically.
    - F11 toggles fullscreen, F10 toggles maximize, Esc exits fullscreen.
    - The OS-native title bar's minimize/maximize/close buttons remain
      available (we don't remove decorations)."""
    try:
        sw = window.winfo_screenwidth()
        sh = window.winfo_screenheight()
    except Exception:
        sw, sh = 1920, 1080

    if ideal_w and ideal_h:
        w = min(ideal_w, int(sw * 0.92))
        h = min(ideal_h, int(sh * 0.92))
        if min_w:
            w = max(w, min_w)
        if min_h:
            h = max(h, min_h)
        x = max(0, (sw - w) // 2)
        y = max(20, (sh - h) // 4)
        try:
            window.geometry(f"{w}x{h}+{x}+{y}")
        except Exception:
            pass
    if min_w and min_h:
        try:
            window.minsize(min_w, min_h)
        except Exception:
            pass

    if allow_fullscreen:
        def _toggle_fullscreen(_e=None):
            try:
                cur = bool(window.attributes("-fullscreen"))
                window.attributes("-fullscreen", not cur)
            except Exception:
                pass

        def _toggle_zoom(_e=None):
            try:
                if window.state() == "zoomed":
                    window.state("normal")
                else:
                    window.state("zoomed")
            except Exception:
                pass

        def _exit_fullscreen(_e=None):
            try:
                if window.attributes("-fullscreen"):
                    window.attributes("-fullscreen", False)
            except Exception:
                pass

        try:
            window.bind("<F11>", _toggle_fullscreen)
            window.bind("<F10>", _toggle_zoom)
            # Esc exits fullscreen but does NOT close the window —
            # other handlers can layer their own Esc behavior.
            window.bind_all("<F11>", _toggle_fullscreen, add="+")
        except Exception:
            pass


# ─────────────────────────── Single-segment preview render ───────────────────────────

def _render_single_segment_preview(image_paths: List[str], audio_path: str,
                                   weights: Optional[List[float]] = None,
                                   width: int = 1280, height: int = 720,
                                   fps: int = 30,
                                   ffmpeg: Optional[str] = None) -> Optional[str]:
    """Quick-encode one preview MP4 of a single segment.

    For 1-image segments → one ffmpeg call (loop image + audio).
    For N-image segments → slice the audio into N weighted chunks and
    concat N (image, chunk) clips, the same way the full pipeline does.

    Returns the path to the produced MP4, or None on failure. Output
    file lives in a temp dir that the caller is expected to clean up
    (or just leave to the OS) once the preview window is closed."""
    if not image_paths or not audio_path:
        return None

    # Discover ffmpeg if not provided
    if ffmpeg is None:
        try:
            HERE = Path(__file__).resolve().parent
            if str(HERE) not in sys.path:
                sys.path.insert(0, str(HERE))
            from simple_video_creator import find_ffmpeg, configure_pydub
            ffmpeg = find_ffmpeg()
            if ffmpeg:
                configure_pydub(ffmpeg)
        except Exception:
            ffmpeg = "ffmpeg"
    if not ffmpeg:
        return None

    tmp = Path(tempfile.mkdtemp(prefix="segpreview_render_"))
    n = len(image_paths)

    # Slice audio per weights when multi-image
    if n == 1:
        clip_audios = [audio_path]
        clip_durations_s = []
        try:
            seg = AudioSegment.from_file(audio_path)
            clip_durations_s.append(len(seg) / 1000.0)
        except Exception:
            clip_durations_s.append(5.0)
    else:
        try:
            seg = AudioSegment.from_file(audio_path)
        except Exception as e:
            print(f"[preview-render] could not load audio: {e}")
            return None
        total_ms = len(seg)
        ws = list(weights or [])
        while len(ws) < n:
            ws.append(1.0)
        sum_w = sum(ws[:n]) if sum(ws[:n]) > 0 else 1.0
        boundaries = [0]
        for i in range(n - 1):
            cum = sum(ws[: i + 1])
            boundaries.append(int(round(total_ms * cum / sum_w)))
        boundaries.append(total_ms)
        clip_audios = []
        clip_durations_s = []
        for i in range(n):
            chunk = seg[boundaries[i]:boundaries[i + 1]]
            cp = tmp / f"chunk_{i}.wav"
            chunk.export(str(cp), format="wav")
            clip_audios.append(str(cp))
            clip_durations_s.append((boundaries[i + 1] - boundaries[i]) / 1000.0)

    # Render each (image, chunk_audio) into a per-clip MP4
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease:flags=lanczos,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"
    )
    clips = []
    for i, (img, aud) in enumerate(zip(image_paths, clip_audios)):
        clip_path = tmp / f"clip_{i}.mp4"
        cmd = [
            ffmpeg, "-y", "-loglevel", "error",
            "-loop", "1", "-i", str(img), "-i", str(aud),
            "-vf", vf,
            "-c:v", "libx264", "-crf", "20", "-preset", "veryfast",
            "-tune", "stillimage",
            "-pix_fmt", "yuv420p", "-r", str(fps),
            "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
            "-shortest", "-movflags", "+faststart",
            str(clip_path),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            print(f"[preview-render] clip {i} failed: {res.stderr.strip()[:200]}")
            shutil.rmtree(tmp, ignore_errors=True)
            return None
        clips.append(str(clip_path))

    # Concat into one preview MP4
    out = tmp / "preview.mp4"
    if len(clips) == 1:
        try:
            shutil.move(clips[0], str(out))
        except Exception:
            return None
    else:
        list_file = tmp / "concat.txt"
        with open(list_file, "w", encoding="utf-8") as f:
            for c in clips:
                f.write(f"file '{Path(c).as_posix()}'\n")
        cmd = [
            ffmpeg, "-y", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", str(list_file),
            "-c", "copy", "-movflags", "+faststart",
            str(out),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            print(f"[preview-render] concat failed: {res.stderr.strip()[:200]}")
            return None

    return str(out)


def _open_with_default_player(path: str) -> bool:
    """Open `path` with the OS default video player. Returns True if
    the open command was issued (no guarantee it succeeded)."""
    try:
        if sys.platform == "win32":
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
        return True
    except Exception as e:
        print(f"[preview-render] could not open {path}: {e}")
        return False


# ─────────────────────────── Window-control button strip ───────────────────────────

def _add_wm_buttons(bar: "ctk.CTkFrame", window, theme: dict,
                    allow_fullscreen: bool = True) -> None:
    """Append  −  □  ⛶  (minimize / maximize / fullscreen) buttons to the
    right side of any header/topbar frame.

    − = iconify (minimize to taskbar)
    □ = toggle between normal and zoomed (maximised)
    ⛶ = toggle fullscreen (no decorations, fills the screen). Hidden for
         small utility dialogs when allow_fullscreen=False."""

    t = theme
    BTN_W, BTN_H = 32, 28

    def _minimize():
        try:
            window.iconify()
        except Exception:
            pass

    def _toggle_maximize():
        try:
            if window.state() == "zoomed":
                window.state("normal")
            else:
                window.state("zoomed")
        except Exception:
            pass

    def _toggle_fullscreen():
        try:
            cur = bool(window.attributes("-fullscreen"))
            window.attributes("-fullscreen", not cur)
        except Exception:
            pass

    # Container so the three buttons sit flush against each other
    grp = ctk.CTkFrame(bar, fg_color="transparent")
    grp.pack(side="right", padx=(4, 10), pady=6)

    ctk.CTkButton(
        grp, text="−", width=BTN_W, height=BTN_H,
        command=_minimize,
        fg_color=t["panel2"], hover_color=t["accent2"],
        text_color=t["text"], corner_radius=6,
        font=ctk.CTkFont("Segoe UI", 14, "bold"),
    ).pack(side="left", padx=2)

    ctk.CTkButton(
        grp, text="□", width=BTN_W, height=BTN_H,
        command=_toggle_maximize,
        fg_color=t["panel2"], hover_color=t["accent2"],
        text_color=t["text"], corner_radius=6,
        font=ctk.CTkFont("Segoe UI", 12, "bold"),
    ).pack(side="left", padx=2)

    if allow_fullscreen:
        ctk.CTkButton(
            grp, text="⛶", width=BTN_W, height=BTN_H,
            command=_toggle_fullscreen,
            fg_color=t["panel2"], hover_color=t["accent"],
            text_color=t["text"], corner_radius=6,
            font=ctk.CTkFont("Segoe UI", 11),
        ).pack(side="left", padx=2)


# ─────────────────────────── Styled popup menu ───────────────────────────

class StyledPopupMenu(tk.Toplevel):
    """Modern CTk-themed popup menu.

    Built on tk.Toplevel (not CTkToplevel) to avoid CTkToplevel's
    internal after() delays which can prevent the window from appearing
    when the caller already has a CTkToplevel grab active.

    Uses grab_set_global() so the popup works correctly even when a
    parent window (e.g. SegmentDetailModal) holds an active local grab.
    The previous grab owner is restored when the menu closes.

    Closes on: Escape, click outside the popup bounds, or after a
    command button is activated.

    Each item is a dict:
      {"kind": "command",   "label": str, "command": callable,
                            "state": "normal"|"disabled",
                            "accent": bool — highlights as primary CTA}
      {"kind": "separator"}
      {"kind": "header",    "label": str}  # non-clickable section title
    """

    BUTTON_HEIGHT = 34

    def __init__(self, master, theme, items, x=None, y=None, min_width=260):
        super().__init__(master)
        self.theme = theme
        self._closed = False

        # Remember who owned the grab before us so we can restore it on close
        try:
            self._prev_grab = self.grab_current()
        except Exception:
            self._prev_grab = None

        # Strip window decorations for a true popup look
        self.overrideredirect(True)
        try:
            self.attributes("-topmost", True)
        except Exception:
            pass
        self.configure(bg=theme["bg"])

        # Themed card (CTkFrame as root widget inside the bare Toplevel)
        card = ctk.CTkFrame(
            self, fg_color=theme["panel2"], corner_radius=12,
            border_width=1, border_color=theme["stroke"],
        )
        card.pack(fill="both", expand=True, padx=3, pady=3)

        for item in items:
            kind = item.get("kind", "command")
            if kind == "separator":
                ctk.CTkFrame(
                    card, fg_color=theme["stroke"], height=1, corner_radius=0,
                ).pack(fill="x", padx=10, pady=4)
                continue
            if kind == "header":
                ctk.CTkLabel(
                    card, text=item.get("label", ""),
                    text_color=theme["accent"],
                    font=ctk.CTkFont("Segoe UI", 10, "bold"),
                    anchor="w",
                ).pack(fill="x", padx=14, pady=(8, 2))
                continue

            state = item.get("state", "normal")
            cmd = item.get("command")
            disabled = state == "disabled"
            accent = bool(item.get("accent"))
            text_color = (
                theme["muted"] if disabled else
                (theme["accent"] if accent else theme["text"])
            )
            hover_color = (
                theme["panel"] if disabled else
                (theme["accent"] if accent else theme["accent2"])
            )
            btn = ctk.CTkButton(
                card,
                text=item.get("label", ""),
                command=(lambda c=cmd: self._dispatch(c)) if not disabled else None,
                fg_color="transparent",
                hover_color=hover_color,
                text_color=text_color,
                corner_radius=8, anchor="w",
                height=self.BUTTON_HEIGHT,
                font=ctk.CTkFont("Segoe UI", 11),
            )
            if disabled:
                btn.configure(state="disabled")
            btn.pack(fill="x", padx=6, pady=1)

        # Measure, position, clamp to screen
        self.update_idletasks()
        try:
            req_w = max(min_width, card.winfo_reqwidth() + 6)
            req_h = card.winfo_reqheight() + 6
        except Exception:
            req_w, req_h = min_width, 320
        if x is None or y is None:
            try:
                x, y = self.winfo_pointerx(), self.winfo_pointery()
            except Exception:
                x, y = 100, 100
        try:
            sw = self.winfo_screenwidth()
            sh = self.winfo_screenheight()
            x = min(max(0, x), sw - req_w - 4)
            y = min(max(0, y), sh - req_h - 4)
            self.geometry(f"{req_w}x{req_h}+{x}+{y}")
        except Exception:
            pass

        # Global grab — captures all pointer events, bypasses any local
        # grab that a parent window (e.g. a modal dialog) may hold.
        try:
            self.grab_set_global()
        except Exception:
            try:
                self.grab_set()
            except Exception:
                pass

        self.lift()
        try:
            self.focus_force()
        except Exception:
            pass

        # Close on Escape
        self.bind("<Escape>", lambda _e: self._close())
        # Close when the click lands outside the popup bounds.
        # With global grab, Button-1 on any screen position is delivered
        # here; we check screen coords to distinguish inside/outside.
        self.bind("<Button-1>", self._on_any_click, add="+")

    # ----- internals -----

    def _on_any_click(self, event):
        """Global grab delivers all Button-1 events here. Close if the
        click was outside the popup's screen rectangle."""
        try:
            px, py = self.winfo_rootx(), self.winfo_rooty()
            pw, ph = self.winfo_width(), self.winfo_height()
            if not (px <= event.x_root <= px + pw and
                    py <= event.y_root <= py + ph):
                self._close()
        except Exception:
            pass

    def _dispatch(self, cmd):
        self._close()
        if cmd:
            try:
                cmd()
            except Exception as e:
                print(f"[StyledPopupMenu] command raised: {e}")

    def _close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self.grab_release()
        except Exception:
            pass
        # Restore the grab that was active before we opened so the parent
        # modal continues to own events correctly.
        if self._prev_grab:
            try:
                if self._prev_grab.winfo_exists():
                    self._prev_grab.grab_set()
            except Exception:
                pass
        try:
            self.destroy()
        except Exception:
            pass


# ─────────────────────────── Audio loading ───────────────────────────

def _load_audio_samples(path: str):
    """Decode any audio file pydub can open into float32 [-1, 1] + sample rate."""
    seg = AudioSegment.from_file(path)
    sr = seg.frame_rate
    arr = np.array(seg.get_array_of_samples())
    if seg.channels == 2:
        arr = arr.reshape((-1, 2))
    sample_max = float(1 << (8 * seg.sample_width - 1))
    arr = arr.astype(np.float32) / sample_max
    return arr, sr


# ─────────────────────────── Audio player widget ───────────────────────────

class SegmentAudioPlayer(ctk.CTkFrame):
    """Compact play/pause + seek + time display for one audio file.

    Uses sounddevice with an OutputStream callback so seeking is instant
    and we can update the playhead at ~20 Hz from the UI tick."""

    def __init__(self, master, theme, **kwargs):
        super().__init__(
            master, fg_color=theme["panel2"], corner_radius=12,
            border_width=1, border_color=theme["stroke"], **kwargs,
        )
        self.theme = theme

        self.samples: Optional[np.ndarray] = None
        self.sr: int = 0
        self.position: int = 0
        self.is_playing: bool = False
        self.stream: Optional[sd.OutputStream] = None
        self._lock = threading.Lock()
        self._suppress_seek_callback = False
        self._tick_after_id = None

        self.grid_columnconfigure(1, weight=1)

        self.play_btn = ctk.CTkButton(
            self, text="▶", width=44, height=44,
            command=self.toggle,
            fg_color=theme["accent2"], hover_color=hex_lerp(theme["accent2"], "#000000", 0.2),
            text_color="#0F1117", corner_radius=22,
            font=ctk.CTkFont("Segoe UI", 16, "bold"),
        )
        self.play_btn.grid(row=0, column=0, padx=(10, 8), pady=10, sticky="w")

        self.seek_var = ctk.DoubleVar(value=0.0)
        self.seek_bar = ctk.CTkSlider(
            self, from_=0.0, to=1.0, variable=self.seek_var,
            command=self._on_seek_drag,
            progress_color=theme["accent"],
            button_color=theme["accent"],
            button_hover_color=theme["accent2"],
            fg_color=theme["panel"],
        )
        self.seek_bar.grid(row=0, column=1, padx=(0, 10), pady=10, sticky="ew")

        self.time_label = ctk.CTkLabel(
            self, text="00:00.000 / 00:00.000",
            text_color=theme["muted"], font=ctk.CTkFont("Consolas", 11),
        )
        self.time_label.grid(row=0, column=2, padx=(0, 12), pady=10, sticky="e")

    # ----- public -----

    def load(self, audio_path: str) -> bool:
        self.stop()
        try:
            samples, sr = _load_audio_samples(audio_path)
        except Exception as e:
            print(f"[SegmentAudioPlayer] load failed: {e}")
            self.samples = None
            self.sr = 0
            self._update_time_label()
            return False
        self.samples = samples
        self.sr = sr
        self.position = 0
        self._suppress_seek_callback = True
        try:
            self.seek_var.set(0.0)
        finally:
            self._suppress_seek_callback = False
        self._update_time_label()
        return True

    def toggle(self):
        if self.is_playing:
            self.pause()
        else:
            self.play()

    def play(self):
        if self.samples is None or self.sr <= 0:
            return
        if self.position >= len(self.samples):
            self.position = 0
        if self.stream is not None:
            self._close_stream()
        channels = 1 if self.samples.ndim == 1 else self.samples.shape[1]
        try:
            self.stream = sd.OutputStream(
                samplerate=self.sr,
                channels=channels,
                callback=self._sd_callback,
                dtype="float32",
                blocksize=1024,
                finished_callback=self._on_stream_finished,
            )
            self.stream.start()
            self.is_playing = True
            self.play_btn.configure(text="⏸")
            self._schedule_tick()
        except Exception as e:
            print(f"[SegmentAudioPlayer] couldn't open output stream: {e}")
            self.is_playing = False

    def pause(self):
        self._close_stream()
        self.is_playing = False
        self.play_btn.configure(text="▶")
        self._cancel_tick()

    def stop(self):
        self.pause()
        self.position = 0
        self._suppress_seek_callback = True
        try:
            self.seek_var.set(0.0)
        finally:
            self._suppress_seek_callback = False
        self._update_time_label()

    def destroy(self):
        try:
            self.stop()
        except Exception:
            pass
        super().destroy()

    # ----- internals -----

    def _close_stream(self):
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None

    def _on_stream_finished(self):
        # Called from sounddevice's worker thread — bounce to the UI thread.
        self.is_playing = False
        try:
            self.after(0, self._on_finished_ui)
        except Exception:
            pass

    def _on_finished_ui(self):
        self.play_btn.configure(text="▶")
        if self.samples is not None and self.position >= len(self.samples):
            self.position = 0
            self._suppress_seek_callback = True
            try:
                self.seek_var.set(0.0)
            finally:
                self._suppress_seek_callback = False
            self._update_time_label()
        self._cancel_tick()

    def _sd_callback(self, outdata, frames, time_info, status):
        with self._lock:
            samples = self.samples
            if samples is None:
                outdata[:] = 0
                raise sd.CallbackStop
            n_total = len(samples)
            channels = outdata.shape[1]
            pos = self.position
            if pos >= n_total:
                outdata[:] = 0
                self.position = n_total
                raise sd.CallbackStop
            chunk_end = min(pos + frames, n_total)
            chunk = samples[pos:chunk_end]
            n = len(chunk)
            if samples.ndim == 1:
                outdata[:n, 0] = chunk
                if channels == 2:
                    outdata[:n, 1] = chunk
            else:
                outdata[:n] = chunk[:, :channels]
            if n < frames:
                outdata[n:] = 0
            self.position = chunk_end
            if chunk_end >= n_total:
                # Last buffer for this segment — let sounddevice drain then call finished.
                raise sd.CallbackStop

    def _schedule_tick(self):
        self._cancel_tick()
        try:
            self._tick_after_id = self.after(50, self._on_tick)
        except Exception:
            self._tick_after_id = None

    def _cancel_tick(self):
        if self._tick_after_id is not None:
            try:
                self.after_cancel(self._tick_after_id)
            except Exception:
                pass
            self._tick_after_id = None

    def _on_tick(self):
        if self.samples is None:
            return
        n_total = max(1, len(self.samples))
        frac = max(0.0, min(1.0, self.position / n_total))
        self._suppress_seek_callback = True
        try:
            self.seek_var.set(frac)
        finally:
            self._suppress_seek_callback = False
        self._update_time_label()
        if self.is_playing:
            try:
                self._tick_after_id = self.after(50, self._on_tick)
            except Exception:
                self._tick_after_id = None

    def _on_seek_drag(self, value):
        if self._suppress_seek_callback:
            return
        if self.samples is None:
            return
        frac = float(value)
        n_total = len(self.samples)
        self.position = max(0, min(n_total - 1, int(round(frac * n_total))))
        self._update_time_label()

    def _update_time_label(self):
        if self.samples is None or self.sr <= 0:
            self.time_label.configure(text="00:00.000 / 00:00.000")
            return
        total_s = len(self.samples) / float(self.sr)
        cur_s = self.position / float(self.sr)
        self.time_label.configure(text=f"{_fmt_s(cur_s)} / {_fmt_s(total_s)}")


# ─────────────────────────── Editor + Confirm modals ───────────────────────────

def _next_edit_path(audio_path: str, history_len: int) -> Path:
    """Return a unique path for the next edited version of `audio_path`.

    Original is `seg_NNN.wav`; v1 → `seg_NNN.edit1.wav`, v2 → `seg_NNN.edit2.wav`, ...
    `history_len` = current number of versions (so next index = history_len)."""
    p = Path(audio_path)
    # Strip any existing .editN suffix to keep the base stable
    stem = p.stem
    if "." in stem and stem.split(".")[-1].startswith("edit"):
        stem = ".".join(stem.split(".")[:-1])
    return p.parent / f"{stem}.edit{history_len}.wav"


class ConfirmAudioModal(ctk.CTkToplevel):
    """Listen to the freshly trimmed audio, then Confirm or Re-edit.

    Confirm replaces the segment's audio. Re-edit closes this modal and
    leaves the editor open so the user can continue tweaking their cuts."""

    def __init__(self, master, audio_path: str,
                 original_duration_ms: Optional[int],
                 new_duration_ms: int,
                 theme,
                 on_confirm: Callable[[], None],
                 on_reedit: Callable[[], None]):
        super().__init__(master)
        self.theme = theme
        self.on_confirm = on_confirm
        self.on_reedit = on_reedit
        self._decided = False

        self.configure(fg_color=theme["bg"])
        self.title("Confirm trimmed audio")
        _install_window_features(
            self, ideal_w=720, ideal_h=320,
            min_w=560, min_h=260, allow_fullscreen=False,
        )
        self.transient(master)

        self._build_ui(audio_path, original_duration_ms, new_duration_ms)

        self.lift()
        try:
            self.focus_force()
            self.grab_set()
        except Exception:
            pass

        self.bind("<Return>", lambda _e: self._confirm())
        self.bind("<Escape>", lambda _e: self._reedit())
        self.protocol("WM_DELETE_WINDOW", self._reedit)

    def _build_ui(self, audio_path, orig_ms, new_ms):
        t = self.theme

        top = ctk.CTkFrame(
            self, fg_color=t["panel"], corner_radius=14,
            border_width=1, border_color=t["stroke"],
        )
        top.pack(fill="x", padx=14, pady=(14, 8))
        _add_wm_buttons(top, self, t, allow_fullscreen=False)
        ctk.CTkLabel(
            top, text="Trimmed audio",
            font=ctk.CTkFont("Segoe UI", 16, "bold"),
            text_color=t["accent"],
        ).pack(side="left", padx=16, pady=10)
        delta_ms = max(0, (orig_ms or 0) - new_ms) if orig_ms is not None else 0
        if orig_ms is not None:
            info = f"Was {fmt_ms(orig_ms)}  →  Now {fmt_ms(new_ms)}  (saved {fmt_ms(delta_ms)})"
        else:
            info = f"Trimmed to {fmt_ms(new_ms)}"
        ctk.CTkLabel(
            top, text=info,
            text_color=t["muted"], font=ctk.CTkFont("Segoe UI", 11),
        ).pack(side="left", padx=(0, 16), pady=10)

        # Player
        player_wrap = ctk.CTkFrame(self, fg_color="transparent")
        player_wrap.pack(fill="x", padx=14, pady=(0, 8))
        self.player = SegmentAudioPlayer(player_wrap, theme=t)
        self.player.pack(fill="x")
        self.player.load(audio_path)

        # Buttons
        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.pack(side="bottom", fill="x", padx=14, pady=14)

        ctk.CTkButton(
            btn_row, text="✓  Confirm — replace audio", width=260, height=42,
            command=self._confirm,
            fg_color=t["accent"], hover_color=hex_lerp(t["accent"], "#000000", 0.2),
            text_color="#FFFFFF", corner_radius=12,
            font=ctk.CTkFont("Segoe UI", 13, "bold"),
        ).pack(side="right", padx=4)
        ctk.CTkButton(
            btn_row, text="↺  Re-edit", width=140, height=42,
            command=self._reedit,
            fg_color=t["panel2"], hover_color=t["accent2"],
            text_color=t["text"], corner_radius=12,
            font=ctk.CTkFont("Segoe UI", 12),
        ).pack(side="right", padx=4)
        ctk.CTkLabel(
            btn_row,
            text="Enter = confirm  ·  Esc = re-edit",
            text_color=t["muted"], font=ctk.CTkFont("Segoe UI", 10),
        ).pack(side="left", padx=4)

    def _confirm(self):
        if self._decided:
            return
        self._decided = True
        try:
            self.player.stop()
        except Exception:
            pass
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()
        try:
            if self.on_confirm:
                self.on_confirm()
        except Exception:
            pass

    def _reedit(self):
        if self._decided:
            return
        self._decided = True
        try:
            self.player.stop()
        except Exception:
            pass
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()
        try:
            if self.on_reedit:
                self.on_reedit()
        except Exception:
            pass


class SegmentEditorModal(ctk.CTkToplevel):
    """Mini trim editor for one audio segment.

    Embeds voice_editor.py's WaveformCanvas + Project + Player so the
    user can paint cut regions, preview the result with cuts skipped,
    undo/redo, then Save → opens ConfirmAudioModal as a child. Confirm
    closes this editor and bubbles the new path up via on_save; Re-edit
    closes only the confirm dialog and leaves the editor open with the
    cuts intact."""

    def __init__(self, master, audio_path: str, original_duration_ms: Optional[int],
                 history_len: int,
                 theme,
                 on_save: Callable[[str, int], None],
                 on_cancel: Callable[[], None],
                 segment_label: str = "Segment",
                 chunk_boundaries_s: Optional[List[float]] = None):
        super().__init__(master)
        self.theme = theme
        self.on_save = on_save
        self.on_cancel = on_cancel
        self.audio_path = audio_path
        self._history_len_at_open = history_len
        self._original_duration_ms = original_duration_ms
        self._mark_in_s: Optional[float] = None
        self._closed = False
        # For multi-image segments — chunk transition points in seconds
        # (excluding 0 and end). Drawn as dashed vertical lines on the
        # waveform so the user can see where each image's chunk lives.
        self._chunk_boundaries_s = list(chunk_boundaries_s or [])

        self.configure(fg_color=theme["bg"])
        self.title(f"Edit audio — {segment_label}")
        _install_window_features(
            self, ideal_w=1180, ideal_h=720,
            min_w=900, min_h=540,
        )
        self.transient(master)

        # Load audio into a Project + Player
        try:
            samples, sr = load_audio(audio_path)
        except Exception as e:
            messagebox.showerror("Load failed", f"Could not load audio:\n{e}")
            self.destroy()
            try:
                on_cancel()
            except Exception:
                pass
            return

        self.project = Project(source_path=Path(audio_path), samples=samples, sample_rate=sr)
        self.player = Player()
        self.player.load(samples, sr)
        self.player.on_position = lambda s: self.after(0, lambda s=s: self._on_player_pos_ui(s))
        self.player.on_finished = lambda: self.after(0, self._on_player_finished_ui)
        self.preview_cuts_var = ctk.BooleanVar(value=True)

        self._build_ui(segment_label)
        self._sync_canvas()
        self._refresh_summary()

        self.lift()
        try:
            self.focus_force()
            self.grab_set()
        except Exception:
            pass

        self.bind("<space>", lambda _e: self._toggle_play())
        self.bind("<Control-z>", lambda _e: self._undo_edit())
        self.bind("<Control-Z>", lambda _e: self._undo_edit())
        self.bind("<Control-y>", lambda _e: self._redo_edit())
        self.bind("<Control-Y>", lambda _e: self._redo_edit())
        self.bind("<bracketleft>", lambda _e: self._mark_in())
        self.bind("<bracketright>", lambda _e: self._mark_out())
        self.bind("<Escape>", lambda _e: self._cancel())
        self.bind("<Delete>", lambda _e: self._delete_last_cut())
        self.protocol("WM_DELETE_WINDOW", self._cancel)

    def _build_ui(self, segment_label: str):
        t = self.theme

        # Top bar
        topbar = ctk.CTkFrame(
            self, fg_color=t["panel"], corner_radius=14,
            border_width=1, border_color=t["stroke"],
        )
        topbar.pack(fill="x", padx=14, pady=(14, 8))
        _add_wm_buttons(topbar, self, t, allow_fullscreen=True)

        ctk.CTkLabel(
            topbar, text=f"Edit — {segment_label}",
            font=ctk.CTkFont("Segoe UI", 16, "bold"),
            text_color=t["accent"],
        ).pack(side="left", padx=16, pady=10)
        ctk.CTkLabel(
            topbar,
            text="Drag on the waveform to mark a cut. Drag a cut's edge to resize, body to move. Double-click a cut to delete.",
            text_color=t["muted"], font=ctk.CTkFont("Segoe UI", 11),
        ).pack(side="left", padx=(0, 16), pady=10)

        # Waveform
        wave_frame = ctk.CTkFrame(
            self, fg_color=t["panel"], corner_radius=14,
            border_width=1, border_color=t["stroke"],
        )
        wave_frame.pack(fill="both", expand=True, padx=14, pady=(0, 8))

        self.canvas = WaveformCanvas(
            wave_frame, theme=t,
            callbacks={
                "add_cut": self._on_add_cut,
                "modify_cut": self._on_modify_cut_live,
                "commit_cut_modify": self._on_commit_modify,
                "seek": self._on_seek,
                "delete_cut": self._on_delete_cut_idx,
            },
            bg=t["panel"],
        )
        self.canvas.pack(fill="both", expand=True, padx=10, pady=10)

        # Transport
        transport = ctk.CTkFrame(self, fg_color="transparent")
        transport.pack(fill="x", padx=14, pady=(0, 6))

        self.play_btn = ctk.CTkButton(
            transport, text="▶  Play", width=110, command=self._toggle_play,
            fg_color=t["accent2"], hover_color=hex_lerp(t["accent2"], "#000000", 0.2),
            text_color="#0F1117", corner_radius=10,
            font=ctk.CTkFont("Segoe UI", 12, "bold"),
        )
        self.play_btn.pack(side="left", padx=(0, 4))
        ctk.CTkButton(
            transport, text="⏹", width=44, command=self._stop_play,
            fg_color=t["panel2"], hover_color=t["stroke"], text_color=t["text"],
            corner_radius=10,
        ).pack(side="left", padx=4)

        ctk.CTkSwitch(
            transport, text="Preview cuts skipped",
            variable=self.preview_cuts_var,
            command=self._sync_player_skips,
            text_color=t["text"], button_color=t["accent"], progress_color=t["accent"],
        ).pack(side="left", padx=(16, 8))

        ctk.CTkButton(
            transport, text="[ Mark in", width=92, command=self._mark_in,
            fg_color=t["panel2"], hover_color=t["stroke"], text_color=t["text"],
            corner_radius=10,
        ).pack(side="left", padx=2)
        ctk.CTkButton(
            transport, text="] Mark out", width=98, command=self._mark_out,
            fg_color=t["panel2"], hover_color=t["stroke"], text_color=t["text"],
            corner_radius=10,
        ).pack(side="left", padx=2)

        ctk.CTkButton(
            transport, text="↶ Undo", width=80, command=self._undo_edit,
            fg_color=t["panel2"], hover_color=t["stroke"], text_color=t["text"],
            corner_radius=10,
        ).pack(side="right", padx=2)
        ctk.CTkButton(
            transport, text="↷ Redo", width=80, command=self._redo_edit,
            fg_color=t["panel2"], hover_color=t["stroke"], text_color=t["text"],
            corner_radius=10,
        ).pack(side="right", padx=2)
        ctk.CTkButton(
            transport, text="Fit", width=44, command=self.canvas.reset_zoom,
            fg_color=t["panel2"], hover_color=t["stroke"], text_color=t["text"],
            corner_radius=10,
        ).pack(side="right", padx=2)
        ctk.CTkButton(
            transport, text="+", width=38, command=lambda: self.canvas.zoom(0.7),
            fg_color=t["panel2"], hover_color=t["stroke"], text_color=t["text"],
            corner_radius=10,
        ).pack(side="right", padx=2)
        ctk.CTkButton(
            transport, text="−", width=38, command=lambda: self.canvas.zoom(1.4),
            fg_color=t["panel2"], hover_color=t["stroke"], text_color=t["text"],
            corner_radius=10,
        ).pack(side="right", padx=2)

        # Summary row
        summary = ctk.CTkFrame(self, fg_color=t["panel2"], corner_radius=10)
        summary.pack(fill="x", padx=14, pady=(0, 8))
        self.summary_orig = ctk.CTkLabel(
            summary, text="Original: --",
            text_color=t["text"], font=ctk.CTkFont("Consolas", 11),
        )
        self.summary_orig.pack(side="left", padx=12, pady=8)
        self.summary_cut = ctk.CTkLabel(
            summary, text="Cuts: --",
            text_color=t["danger"], font=ctk.CTkFont("Consolas", 11),
        )
        self.summary_cut.pack(side="left", padx=12, pady=8)
        self.summary_result = ctk.CTkLabel(
            summary, text="Result: --",
            text_color=t["ok"], font=ctk.CTkFont("Consolas", 11, "bold"),
        )
        self.summary_result.pack(side="left", padx=12, pady=8)

        # Footer hint + Save / Cancel
        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.pack(side="bottom", fill="x", padx=14, pady=(0, 14))

        ctk.CTkButton(
            btn_row, text="Trim & confirm →", width=200, height=42,
            command=self._save,
            fg_color=t["accent"], hover_color=hex_lerp(t["accent"], "#000000", 0.2),
            text_color="#FFFFFF", corner_radius=12,
            font=ctk.CTkFont("Segoe UI", 13, "bold"),
        ).pack(side="right", padx=4)
        ctk.CTkButton(
            btn_row, text="Cancel", width=120, height=42,
            command=self._cancel,
            fg_color=t["panel2"], hover_color=t["danger"],
            text_color=t["text"], corner_radius=12,
            font=ctk.CTkFont("Segoe UI", 12),
        ).pack(side="right", padx=4)
        ctk.CTkLabel(
            btn_row,
            text="Space play/pause  ·  [ ] mark in/out  ·  Ctrl+Z/Y undo/redo  ·  Esc cancel",
            text_color=t["muted"], font=ctk.CTkFont("Segoe UI", 10),
        ).pack(side="left", padx=4)

    # ----- player -----

    def _toggle_play(self):
        if self.player.is_playing:
            self.player.pause()
            self.play_btn.configure(text="▶  Play")
        else:
            self._sync_player_skips()
            from_s = None
            if self.player.position >= len(self.player.samples):
                from_s = 0.0
            self.player.play(from_s)
            self.play_btn.configure(text="⏸  Pause")

    def _stop_play(self):
        self.player.stop()
        self.play_btn.configure(text="▶  Play")
        self.canvas.set_cursor(0.0)

    def _on_player_pos_ui(self, s: float):
        if self._closed:
            return
        self.canvas.set_cursor(s)

    def _on_player_finished_ui(self):
        if self._closed:
            return
        self.play_btn.configure(text="▶  Play")
        self.player.position = 0
        self.canvas.set_cursor(0.0)

    def _on_seek(self, s: float):
        self.player.seek(s)
        self.canvas.set_cursor(s)

    def _sync_player_skips(self):
        self.player.set_skip_regions_seconds([(c.start_s, c.end_s) for c in self.project.cuts])
        self.player.set_skip_enabled(bool(self.preview_cuts_var.get()))

    # ----- cut handlers (wired into WaveformCanvas) -----

    def _on_add_cut(self, a: float, b: float):
        if self.project.add_cut(a, b):
            self._sync_canvas()
            self._refresh_summary()
            self._sync_player_skips()

    def _on_modify_cut_live(self, mode: str, idx: int, s: float, offset: float, snapshot: bool):
        if not (0 <= idx < len(self.project.cuts)):
            return
        c = self.project.cuts[idx]
        if mode == "edge_left":
            new_start = max(0.0, min(c.end_s - 0.020, s))
            self.project.cuts[idx] = Cut(new_start, c.end_s)
        elif mode == "edge_right":
            new_end = max(c.start_s + 0.020, min(self.project.duration_s, s))
            self.project.cuts[idx] = Cut(c.start_s, new_end)
        elif mode == "move":
            width = c.end_s - c.start_s
            new_start = max(0.0, min(self.project.duration_s - width, s - offset))
            self.project.cuts[idx] = Cut(new_start, new_start + width)
        self._sync_canvas()
        self._refresh_summary()
        self._sync_player_skips()

    def _on_commit_modify(self):
        self.project._normalize()
        self.project._snapshot()
        self._sync_canvas()
        self._refresh_summary()
        self._sync_player_skips()

    def _on_delete_cut_idx(self, idx: int):
        self.project.remove_cut(idx)
        self._sync_canvas()
        self._refresh_summary()
        self._sync_player_skips()

    def _delete_last_cut(self):
        if self.project.cuts:
            self._on_delete_cut_idx(len(self.project.cuts) - 1)

    def _mark_in(self):
        cur = self.player.position / self.player.sr if self.player.sr else 0.0
        self._mark_in_s = cur

    def _mark_out(self):
        if self._mark_in_s is None:
            return
        cur = self.player.position / self.player.sr if self.player.sr else 0.0
        self._on_add_cut(self._mark_in_s, cur)
        self._mark_in_s = None

    def _undo_edit(self):
        self.project.undo()
        self._sync_canvas()
        self._refresh_summary()
        self._sync_player_skips()

    def _redo_edit(self):
        self.project.redo()
        self._sync_canvas()
        self._refresh_summary()
        self._sync_player_skips()

    def _sync_canvas(self):
        peaks = compute_peaks(self.project.samples, n_columns=2400)
        self.canvas.set_audio(peaks, self.project.duration_s)
        self.canvas.set_cuts(self.project.cuts)
        # Re-draw chunk dividers (multi-image segments only)
        self.after(30, self._draw_chunk_dividers)

    def _draw_chunk_dividers(self):
        """Overlay vertical dashed lines on the waveform at chunk
        transition points. Visual hint for multi-image segments —
        the user paints cuts on the FULL audio; the pipeline re-splits
        whatever remains across the images at render time."""
        if not self._chunk_boundaries_s:
            return
        try:
            self.canvas.delete("chunk_divider")
        except Exception:
            return
        try:
            w = self.canvas.winfo_width()
            h = self.canvas.winfo_height()
        except Exception:
            return
        duration = self.project.duration_s or 1.0
        if duration <= 0 or w <= 0:
            return
        for t_s in self._chunk_boundaries_s:
            x = int(round(t_s / duration * w))
            try:
                self.canvas.create_line(
                    x, 0, x, h,
                    fill=self.theme.get("accent3", "#F472B6"),
                    width=2, dash=(4, 3),
                    tags="chunk_divider",
                )
            except Exception:
                pass

    def _refresh_summary(self):
        orig = self.project.duration_s
        cut = self.project.total_cut_s()
        result = max(0.0, orig - cut)
        self.summary_orig.configure(text=f"Original:  {fmt_time(orig)}")
        self.summary_cut.configure(text=f"Cuts:  {fmt_time(cut)}  ({len(self.project.cuts)})")
        self.summary_result.configure(text=f"Result:  {fmt_time(result)}")

    # ----- save/cancel -----

    def _save(self):
        # Apply cuts → render
        try:
            self.player.stop()
        except Exception:
            pass

        rendered = self.project.render(fade_ms=5)
        sr = self.project.sample_rate
        if rendered is None or len(rendered) == 0:
            messagebox.showwarning(
                "Empty result",
                "Your cuts would remove all audio. Adjust the cuts and try again.",
            )
            return

        # Guard against very-short result (< 200 ms) — would render to a near-empty clip
        result_dur_ms = int(round(len(rendered) / sr * 1000))
        if result_dur_ms < 200:
            messagebox.showwarning(
                "Too short",
                f"Trimmed audio is only {result_dur_ms} ms — pick a slightly longer region.",
            )
            return

        new_path = _next_edit_path(self.audio_path, self._history_len_at_open)
        try:
            save_audio(rendered, sr, str(new_path), fmt="wav")
        except Exception as e:
            messagebox.showerror("Save failed", f"Could not write trimmed wav:\n{e}")
            return

        # Open the confirm dialog as a child of this editor.
        # If they confirm, we bubble the new path up via on_save and close.
        # If they re-edit, we just stay here so they can adjust their cuts.
        ConfirmAudioModal(
            self, str(new_path),
            original_duration_ms=self._original_duration_ms,
            new_duration_ms=result_dur_ms,
            theme=self.theme,
            on_confirm=lambda: self._on_confirmed(str(new_path), result_dur_ms),
            on_reedit=self._on_reedit,
        )

    def _on_confirmed(self, new_path: str, new_dur_ms: int):
        self._closed = True
        try:
            self.player.stop()
        except Exception:
            pass
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()
        try:
            if self.on_save:
                self.on_save(new_path, new_dur_ms)
        except Exception:
            pass

    def _on_reedit(self):
        # Confirm dialog closed; editor stays open, focus returns here.
        try:
            self.lift()
            self.focus_force()
            self.grab_set()
        except Exception:
            pass

    def _cancel(self):
        self._closed = True
        try:
            self.player.stop()
        except Exception:
            pass
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()
        try:
            if self.on_cancel:
                self.on_cancel()
        except Exception:
            pass


# ─────────────────────────── Delete image confirmation modal ───────────────────────────

class DeleteImageModal(ctk.CTkToplevel):
    """Two-choice confirmation: delete just the image (keep audio) or
    delete the entire segment (image + audio gone, list shrinks).

    The user picks one of the two; cancel/escape leaves everything intact."""

    def __init__(self, master, theme,
                 segment_label: str,
                 on_drop_image: Callable[[], None],
                 on_delete_segment: Callable[[], None],
                 can_delete_segment: bool = True):
        super().__init__(master)
        self.theme = theme
        self.on_drop_image = on_drop_image
        self.on_delete_segment = on_delete_segment
        self._decided = False

        self.configure(fg_color=theme["bg"])
        self.title("Delete segment")
        _install_window_features(
            self, ideal_w=600, ideal_h=340,
            min_w=500, min_h=300, allow_fullscreen=False,
        )
        self.transient(master)

        self._build_ui(segment_label, can_delete_segment)

        self.lift()
        try:
            self.focus_force()
            self.grab_set()
        except Exception:
            pass

        self.bind("<Escape>", lambda _e: self._cancel())
        self.protocol("WM_DELETE_WINDOW", self._cancel)

    def _build_ui(self, segment_label: str, can_delete_segment: bool):
        t = self.theme

        header = ctk.CTkFrame(
            self, fg_color=t["panel"], corner_radius=14,
            border_width=1, border_color=t["stroke"],
        )
        header.pack(fill="x", padx=14, pady=(14, 8))
        _add_wm_buttons(header, self, t, allow_fullscreen=False)
        ctk.CTkLabel(
            header, text=f"Delete — {segment_label}",
            font=ctk.CTkFont("Segoe UI", 16, "bold"),
            text_color=t["accent"],
        ).pack(side="left", padx=16, pady=10)
        ctk.CTkLabel(
            header, text="Choose how to delete this segment:",
            text_color=t["muted"], font=ctk.CTkFont("Segoe UI", 11),
        ).pack(side="left", padx=(0, 16), pady=10)

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=14, pady=(0, 8))

        # Option A — keep audio, drop image
        a_btn = ctk.CTkButton(
            body, text="🖼   Keep audio, drop image only",
            command=self._drop_image_only,
            height=64,
            fg_color=t["panel2"], hover_color=t["accent"],
            text_color=t["text"], corner_radius=12,
            font=ctk.CTkFont("Segoe UI", 13, "bold"),
            anchor="w",
        )
        a_btn.pack(fill="x", pady=(8, 4))
        ctk.CTkLabel(
            body,
            text="    The previous image extends to cover this segment's audio at render time.",
            text_color=t["muted"], font=ctk.CTkFont("Segoe UI", 10),
            anchor="w", justify="left",
        ).pack(fill="x", padx=(8, 0), pady=(0, 8))

        # Option B — delete entire segment
        b_btn = ctk.CTkButton(
            body, text="🗑   Delete entire segment (audio + image)",
            command=self._delete_segment_full,
            height=64,
            fg_color=t["panel2"], hover_color=t["danger"],
            text_color=t["text"], corner_radius=12,
            font=ctk.CTkFont("Segoe UI", 13, "bold"),
            anchor="w",
            state=("normal" if can_delete_segment else "disabled"),
        )
        b_btn.pack(fill="x", pady=(0, 4))
        b_text = ("    The segment vanishes from the strip. Later segments shift down."
                  if can_delete_segment
                  else "    Disabled — at least one segment must remain.")
        ctk.CTkLabel(
            body, text=b_text,
            text_color=t["muted"], font=ctk.CTkFont("Segoe UI", 10),
            anchor="w", justify="left",
        ).pack(fill="x", padx=(8, 0), pady=(0, 8))

        # Footer
        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.pack(side="bottom", fill="x", padx=14, pady=(0, 14))
        ctk.CTkButton(
            btn_row, text="Cancel", width=120, height=38,
            command=self._cancel,
            fg_color=t["panel2"], hover_color=t["stroke"],
            text_color=t["text"], corner_radius=10,
            font=ctk.CTkFont("Segoe UI", 12),
        ).pack(side="right", padx=4)
        ctk.CTkLabel(
            btn_row, text="Esc = cancel",
            text_color=t["muted"], font=ctk.CTkFont("Segoe UI", 10),
        ).pack(side="left", padx=4)

    def _drop_image_only(self):
        if self._decided:
            return
        self._decided = True
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()
        try:
            if self.on_drop_image:
                self.on_drop_image()
        except Exception:
            pass

    def _delete_segment_full(self):
        if self._decided:
            return
        self._decided = True
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()
        try:
            if self.on_delete_segment:
                self.on_delete_segment()
        except Exception:
            pass

    def _cancel(self):
        if self._decided:
            return
        self._decided = True
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()


# ─────────────────────────── Delete audio confirmation modal ───────────────────────────

class DeleteAudioModal(ctk.CTkToplevel):
    """Three-choice popup for the audio kebab's Delete action:

       - Delete audio + image (whole segment vanishes from the chain)
       - Delete only the audio (image kept, all later audios shift up
                                 by one — the trailing image ends up
                                 audio-less and is skipped at render)
       - Replace audio with a file (file picker → bubble up via callback)

    Each option carries a one-line description so the user can make an
    informed choice. Cancel/Esc leaves everything intact."""

    def __init__(self, master, theme,
                 segment_label: str,
                 has_image: bool,
                 can_delete_segment: bool,
                 can_drop_audio: bool,
                 on_delete_segment: Callable[[], None],
                 on_drop_audio_only: Callable[[], None],
                 on_replace_audio: Callable[[], None]):
        super().__init__(master)
        self.theme = theme
        self.on_delete_segment = on_delete_segment
        self.on_drop_audio_only = on_drop_audio_only
        self.on_replace_audio = on_replace_audio
        self._decided = False

        self.configure(fg_color=theme["bg"])
        self.title("Delete or replace audio")
        _install_window_features(
            self, ideal_w=660, ideal_h=460,
            min_w=560, min_h=400, allow_fullscreen=False,
        )
        self.transient(master)

        self._build_ui(segment_label, has_image, can_delete_segment, can_drop_audio)

        self.lift()
        try:
            self.focus_force()
            self.grab_set()
        except Exception:
            pass

        self.bind("<Escape>", lambda _e: self._cancel())
        self.protocol("WM_DELETE_WINDOW", self._cancel)

    def _build_ui(self, segment_label, has_image, can_delete_segment, can_drop_audio):
        t = self.theme

        header = ctk.CTkFrame(
            self, fg_color=t["panel"], corner_radius=14,
            border_width=1, border_color=t["stroke"],
        )
        header.pack(fill="x", padx=14, pady=(14, 8))
        _add_wm_buttons(header, self, t, allow_fullscreen=False)
        ctk.CTkLabel(
            header, text=f"Audio actions — {segment_label}",
            font=ctk.CTkFont("Segoe UI", 16, "bold"),
            text_color=t["accent"],
        ).pack(side="left", padx=16, pady=10)
        ctk.CTkLabel(
            header, text="Pick what to do with this segment's audio:",
            text_color=t["muted"], font=ctk.CTkFont("Segoe UI", 11),
        ).pack(side="left", padx=(0, 16), pady=10)

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=14, pady=(0, 8))

        # Option A — drop audio only (image kept; audios shift up)
        a_btn = ctk.CTkButton(
            body, text="🎵   Delete audio only — keep image",
            command=self._drop_audio_only,
            height=58,
            fg_color=t["panel2"], hover_color=t["accent"],
            text_color=t["text"], corner_radius=12,
            font=ctk.CTkFont("Segoe UI", 13, "bold"),
            anchor="w",
            state=("normal" if (can_drop_audio and has_image) else "disabled"),
        )
        a_btn.pack(fill="x", pady=(8, 4))
        a_desc = (
            "    The next segment's audio takes this slot, and so on down the chain.\n"
            "    The trailing image will end up without audio and is skipped at render."
            if (can_drop_audio and has_image)
            else
            "    Disabled — needs an image to keep, and at least 2 segments in the chain."
        )
        ctk.CTkLabel(
            body, text=a_desc,
            text_color=t["muted"], font=ctk.CTkFont("Segoe UI", 10),
            anchor="w", justify="left",
        ).pack(fill="x", padx=(8, 0), pady=(0, 8))

        # Option B — replace audio with a file
        b_btn = ctk.CTkButton(
            body, text="📁   Replace audio with a file…",
            command=self._replace_audio,
            height=58,
            fg_color=t["panel2"], hover_color=t["accent2"],
            text_color=t["text"], corner_radius=12,
            font=ctk.CTkFont("Segoe UI", 13, "bold"),
            anchor="w",
        )
        b_btn.pack(fill="x", pady=(0, 4))
        ctk.CTkLabel(
            body,
            text=(
                "    Pick any audio file — wav / mp3 / m4a / flac / ogg.\n"
                "    Longer files are fine; the image will display for the new duration."
            ),
            text_color=t["muted"], font=ctk.CTkFont("Segoe UI", 10),
            anchor="w", justify="left",
        ).pack(fill="x", padx=(8, 0), pady=(0, 8))

        # Option C — delete entire segment (audio + image)
        c_btn = ctk.CTkButton(
            body, text="🗑   Delete entire segment (audio + image)",
            command=self._delete_segment_full,
            height=58,
            fg_color=t["panel2"], hover_color=t["danger"],
            text_color=t["text"], corner_radius=12,
            font=ctk.CTkFont("Segoe UI", 13, "bold"),
            anchor="w",
            state=("normal" if can_delete_segment else "disabled"),
        )
        c_btn.pack(fill="x", pady=(0, 4))
        c_desc = (
            "    The whole segment vanishes from the strip and later segments shift down."
            if can_delete_segment
            else "    Disabled — at least one segment must remain in the strip."
        )
        ctk.CTkLabel(
            body, text=c_desc,
            text_color=t["muted"], font=ctk.CTkFont("Segoe UI", 10),
            anchor="w", justify="left",
        ).pack(fill="x", padx=(8, 0), pady=(0, 8))

        # Footer
        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.pack(side="bottom", fill="x", padx=14, pady=(0, 14))
        ctk.CTkButton(
            btn_row, text="Cancel", width=120, height=38,
            command=self._cancel,
            fg_color=t["panel2"], hover_color=t["stroke"],
            text_color=t["text"], corner_radius=10,
            font=ctk.CTkFont("Segoe UI", 12),
        ).pack(side="right", padx=4)
        ctk.CTkLabel(
            btn_row, text="Esc = cancel",
            text_color=t["muted"], font=ctk.CTkFont("Segoe UI", 10),
        ).pack(side="left", padx=4)

    def _drop_audio_only(self):
        if self._decided:
            return
        self._decided = True
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()
        try:
            if self.on_drop_audio_only:
                self.on_drop_audio_only()
        except Exception:
            pass

    def _replace_audio(self):
        if self._decided:
            return
        self._decided = True
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()
        try:
            if self.on_replace_audio:
                self.on_replace_audio()
        except Exception:
            pass

    def _delete_segment_full(self):
        if self._decided:
            return
        self._decided = True
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()
        try:
            if self.on_delete_segment:
                self.on_delete_segment()
        except Exception:
            pass

    def _cancel(self):
        if self._decided:
            return
        self._decided = True
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()


# ─────────────────────────── Detail modal ───────────────────────────

class SegmentDetailModal(ctk.CTkToplevel):
    """Big image + audio player + prev/next/keyboard navigation.

    Opened by clicking a card in SegmentPreviewWindow. Esc / X closes
    just this modal and returns to the strip; the strip stays open
    until the user clicks Continue or Cancel."""

    IMAGE_MAX_W = 760
    IMAGE_MAX_H = 460

    def __init__(self, master, pairs: List[Dict], start_index: int, theme,
                 on_audio_changed: Optional[Callable[[int, str, int], None]] = None,
                 on_pair_undo: Optional[Callable[[int], None]] = None,
                 on_pair_revert: Optional[Callable[[int], None]] = None,
                 on_image_menu: Optional[Callable[[int, int, int, int], None]] = None,
                 on_audio_delete_menu: Optional[Callable[[int], None]] = None):
        super().__init__(master)
        self.theme = theme
        self.pairs = pairs
        self.idx = start_index
        # CTkImage refs kept alive — multi-image rows hold many
        self._photo_refs: List[ctk.CTkImage] = []
        self.on_audio_changed = on_audio_changed
        self.on_pair_undo = on_pair_undo
        self.on_pair_revert = on_pair_revert
        self.on_image_menu = on_image_menu
        self.on_audio_delete_menu = on_audio_delete_menu

        self.configure(fg_color=theme["bg"])
        self.title("Segment preview")
        _install_window_features(
            self, ideal_w=1040, ideal_h=820,
            min_w=720, min_h=560,
        )
        self.transient(master)

        self._build_ui()
        self.show_index(self.idx)

        # Bring forward and grab focus so keyboard works immediately
        self.lift()
        try:
            self.focus_force()
            self.grab_set()
        except Exception:
            pass

        self.bind("<Left>", lambda _e: self.prev())
        self.bind("<Right>", lambda _e: self.next())
        self.bind("<Escape>", lambda _e: self._close())
        self.bind("<space>", lambda _e: self.player.toggle())
        self.protocol("WM_DELETE_WINDOW", self._close)

    def _build_ui(self):
        t = self.theme

        topbar = ctk.CTkFrame(
            self, fg_color=t["panel"], corner_radius=14,
            border_width=1, border_color=t["stroke"],
        )
        topbar.pack(fill="x", padx=14, pady=(14, 8))

        self.title_label = ctk.CTkLabel(
            topbar, text="",
            font=ctk.CTkFont("Segoe UI", 16, "bold"),
            text_color=t["accent"],
        )
        self.title_label.pack(side="left", padx=(16, 8), pady=10)

        self.position_label = ctk.CTkLabel(
            topbar, text="",
            font=ctk.CTkFont("Segoe UI", 12),
            text_color=t["muted"],
        )
        self.position_label.pack(side="left", padx=(0, 16), pady=10)

        # Window controls  − □ ⛶  appear first so they're flush-right
        _add_wm_buttons(topbar, self, t, allow_fullscreen=True)

        ctk.CTkButton(
            topbar, text="✕  Close", width=88,
            command=self._close,
            fg_color=t["panel2"], hover_color=t["danger"],
            text_color=t["text"], corner_radius=10,
        ).pack(side="right", padx=(4, 4), pady=10)

        # Quick render preview button — encodes JUST this segment (with
        # its audio + N-image weighted chunks) into a tiny MP4 and opens
        # it with the system player. Useful for visual sanity-check
        # before committing to the full render.
        self.preview_btn = ctk.CTkButton(
            topbar, text="🎬  Preview render", width=160,
            command=self._action_preview_render,
            fg_color=t["accent2"],
            hover_color=hex_lerp(t["accent2"], "#000000", 0.2),
            text_color="#0F1117", corner_radius=10,
            font=ctk.CTkFont("Segoe UI", 12, "bold"),
        )
        self.preview_btn.pack(side="right", padx=4, pady=10)

        # Main row: ◀  [image]  ▶
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="both", expand=True, padx=14, pady=4)

        self.prev_btn = ctk.CTkButton(
            row, text="◀", width=56, height=140,
            command=self.prev,
            fg_color=t["panel2"], hover_color=t["accent"],
            text_color=t["text"], corner_radius=14,
            font=ctk.CTkFont("Segoe UI", 22, "bold"),
        )
        self.prev_btn.pack(side="left", padx=(0, 10), fill="y", pady=20)

        # Image panel is a dynamic container: it gets repopulated by
        # _render_image_panel() per the current pair's image count.
        # 0 images → audio-only placeholder, 1 image → big single image,
        # 2+ images → horizontal row of sub-panels each with its own
        # kebab so the user can replace/swap/delete a specific image.
        self.image_panel = ctk.CTkFrame(
            row, fg_color=t["panel"], corner_radius=14,
            border_width=1, border_color=t["stroke"],
        )
        self.image_panel.pack(side="left", fill="both", expand=True)
        # Kept for compatibility with code that references image_label
        # for the audio-only "(audio only)" check (e.g., tests). The
        # field is replaced wholesale in _render_image_panel.
        self.image_label: Optional[ctk.CTkLabel] = None
        self.image_menu_btn: Optional[ctk.CTkButton] = None

        self.next_btn = ctk.CTkButton(
            row, text="▶", width=56, height=140,
            command=self.next,
            fg_color=t["panel2"], hover_color=t["accent"],
            text_color=t["text"], corner_radius=14,
            font=ctk.CTkFont("Segoe UI", 22, "bold"),
        )
        self.next_btn.pack(side="left", padx=(10, 0), fill="y", pady=20)

        # Audio player + kebab menu (⋮) below the image. Player on the
        # left expands; the kebab button hangs on the right edge.
        player_wrap = ctk.CTkFrame(self, fg_color="transparent")
        player_wrap.pack(fill="x", padx=14, pady=(4, 4))
        player_wrap.grid_columnconfigure(0, weight=1)
        self.player = SegmentAudioPlayer(player_wrap, theme=t)
        self.player.grid(row=0, column=0, sticky="ew")

        self.menu_btn = ctk.CTkButton(
            player_wrap, text="⋮", width=44, height=44,
            command=self._show_menu,
            fg_color=t["panel2"], hover_color=t["accent"],
            text_color=t["text"], corner_radius=22,
            font=ctk.CTkFont("Segoe UI", 18, "bold"),
        )
        self.menu_btn.grid(row=0, column=1, padx=(8, 0), sticky="e")

        # Status line below player — shows edit state / instructions
        self.status_label = ctk.CTkLabel(
            self, text="",
            text_color=t["muted"], font=ctk.CTkFont("Segoe UI", 10),
            anchor="w",
        )
        self.status_label.pack(fill="x", padx=14, pady=(2, 0))

        # Footer hint
        hint = ctk.CTkLabel(
            self,
            text="←  /  →   prev / next     ·     Space   play / pause     ·     ⋮   edit menu     ·     Esc   close",
            text_color=t["muted"], font=ctk.CTkFont("Segoe UI", 10),
        )
        hint.pack(pady=(2, 12))

    def show_index(self, idx: int):
        # Defensive: if pairs were mutated (deletions), clamp the index.
        if not self.pairs:
            self._close()
            return
        idx = max(0, min(len(self.pairs) - 1, idx))
        self.idx = idx
        pair = self.pairs[idx]

        images = pair.get("images") or []
        n_imgs = len(images)
        if n_imgs == 0:
            img_name = "(no image)"
        elif n_imgs == 1:
            img_name = Path(images[0]).name
        else:
            img_name = f"{n_imgs} images (each ~{fmt_ms((pair.get('duration_ms') or 0) // n_imgs)})"
        seg_no = pair.get("index", idx + 1)
        dur_text = fmt_ms(pair.get("duration_ms"))
        self.title_label.configure(text=f"Segment {seg_no} — {img_name}  ·  {dur_text}")
        self.position_label.configure(text=f"{idx + 1} / {len(self.pairs)}")

        # Build/rebuild the image area so it matches the current image count
        self._render_image_panel(pair)

        # Audio — None means user deleted the audio (orphan trailing slot)
        self.player.stop()
        if pair.get("audio"):
            self.player.load(pair["audio"])
        else:
            # Player can be loaded with nothing — clear samples so play is no-op
            self.player.samples = None
            self.player.sr = 0
            self.player._update_time_label()

        # Boundary buttons
        self.prev_btn.configure(state=("normal" if idx > 0 else "disabled"))
        self.next_btn.configure(state=("normal" if idx < len(self.pairs) - 1 else "disabled"))

        # Edit-state status line — shows version / orig duration when edited,
        # or a clear "no audio" warning when audio was deleted.
        if not pair.get("audio"):
            self.status_label.configure(
                text="(no audio) — this slot will be skipped at render. "
                     "Click ⋮ on the player to add audio back, or delete the segment.",
                text_color=self.theme["danger"],
            )
        else:
            history = pair.get("_history", [])
            if len(history) > 1:
                orig_ms = history[0].get("duration_ms") if isinstance(history[0], dict) else None
                label = f"Edited (v{len(history) - 1})  ·  was {fmt_ms(orig_ms)}  →  now {fmt_ms(pair.get('duration_ms'))}"
                self.status_label.configure(text=label, text_color=self.theme["accent2"])
            else:
                self.status_label.configure(
                    text="Click ⋮ to edit, undo, revert, replace, or delete this segment's audio.",
                    text_color=self.theme["muted"],
                )

    def _render_image_panel(self, pair: Dict):
        """(Re)build the image area inside self.image_panel based on the
        current image count for this pair.

        - 0 images → big "(audio only)" placeholder + a single kebab.
        - 1 image  → the existing single-image view + single kebab.
        - 2+ images → horizontal row of sub-panels, each with its own
                     kebab acting on that specific image_idx (so the
                     user can swap-left, swap-right, or replace just
                     one of the multiple images)."""
        t = self.theme

        # Wipe whatever was rendered last time
        for child in list(self.image_panel.winfo_children()):
            try:
                child.destroy()
            except Exception:
                pass
        self._photo_refs = []
        self.image_label = None
        self.image_menu_btn = None

        images = pair.get("images") or []
        n = len(images)

        if n == 0:
            # Audio-only placeholder
            self.image_label = ctk.CTkLabel(
                self.image_panel,
                text="🎵\n(audio only)\n\nThe previous segment's image will\nextend across this audio at render time.",
                text_color=t["muted"], font=ctk.CTkFont("Segoe UI", 12),
            )
            self.image_label.pack(fill="both", expand=True, padx=12, pady=12)
            self.image_menu_btn = ctk.CTkButton(
                self.image_panel, text="⋮", width=34, height=34,
                command=lambda: self._open_image_menu(0),
                fg_color=t["panel2"], hover_color=t["accent"],
                text_color=t["text"], corner_radius=17,
                border_width=1, border_color=t["stroke"],
                font=ctk.CTkFont("Segoe UI", 16, "bold"),
            )
            self.image_menu_btn.place(relx=1.0, rely=0.0, anchor="ne", x=-10, y=10)
            return

        if n == 1:
            self.image_label = ctk.CTkLabel(
                self.image_panel, text="", text_color=t["muted"],
                font=ctk.CTkFont("Segoe UI", 12),
            )
            self.image_label.pack(fill="both", expand=True, padx=12, pady=12)
            try:
                pil_img = Image.open(images[0]).copy()
                if pil_img.mode not in ("RGB", "RGBA"):
                    pil_img = pil_img.convert("RGBA")
                pil_img.thumbnail((self.IMAGE_MAX_W, self.IMAGE_MAX_H), Image.LANCZOS)
                photo = ctk.CTkImage(
                    light_image=pil_img, dark_image=pil_img, size=pil_img.size,
                )
                self._photo_refs.append(photo)
                self.image_label.configure(image=photo, text="")
            except Exception as e:
                self.image_label.configure(image=None, text=f"Could not load image:\n{e}")
            self.image_menu_btn = ctk.CTkButton(
                self.image_panel, text="⋮", width=34, height=34,
                command=lambda: self._open_image_menu(0),
                fg_color=t["panel2"], hover_color=t["accent"],
                text_color=t["text"], corner_radius=17,
                border_width=1, border_color=t["stroke"],
                font=ctk.CTkFont("Segoe UI", 16, "bold"),
            )
            self.image_menu_btn.place(relx=1.0, rely=0.0, anchor="ne", x=-10, y=10)
            return

        # n >= 2: horizontal row of sub-panels (with per-image weight
        # sliders + a colored timeline ruler below).
        per_w = max(140, self.IMAGE_MAX_W // n - 12)
        per_h = self.IMAGE_MAX_H

        # Container that holds the row + ruler
        multi_box = ctk.CTkFrame(self.image_panel, fg_color="transparent")
        multi_box.pack(fill="both", expand=True, padx=8, pady=(8, 0))

        row = ctk.CTkFrame(multi_box, fg_color="transparent")
        row.pack(fill="both", expand=True)

        # Compute per-image durations using current weights
        weights = list(pair.get("weights") or [])
        while len(weights) < n:
            weights.append(1.0)
        total_ms = pair.get("duration_ms") or 0
        sum_w = sum(weights) if sum(weights) > 0 else 1.0

        # Track sub-panels for drag-drop hit-testing
        self._sub_panels: List = []

        for i, img_path in enumerate(images):
            sub = ctk.CTkFrame(
                row, fg_color=t["panel2"], corner_radius=12,
                border_width=1, border_color=t["stroke"],
            )
            sub.pack(side="left", fill="both", expand=True, padx=4, pady=2)
            self._sub_panels.append(sub)

            sub_label = ctk.CTkLabel(
                sub, text="", text_color=t["muted"],
                font=ctk.CTkFont("Segoe UI", 11),
            )
            sub_label.pack(fill="both", expand=True, padx=8, pady=(8, 4))

            try:
                pil_img = Image.open(img_path).copy()
                if pil_img.mode not in ("RGB", "RGBA"):
                    pil_img = pil_img.convert("RGBA")
                pil_img.thumbnail((per_w, per_h), Image.LANCZOS)
                photo = ctk.CTkImage(
                    light_image=pil_img, dark_image=pil_img, size=pil_img.size,
                )
                self._photo_refs.append(photo)
                sub_label.configure(image=photo, text="")
            except Exception as e:
                sub_label.configure(image=None, text=f"Could not load:\n{e}")

            # Per-image caption: position + this image's actual slice
            slice_ms = int(total_ms * weights[i] / sum_w) if total_ms else 0
            caption = ctk.CTkLabel(
                sub,
                text=f"Image {i + 1} of {n}  ·  {fmt_ms(slice_ms)}",
                text_color=t["accent2"], font=ctk.CTkFont("Segoe UI", 10, "bold"),
            )
            caption.pack(padx=8, pady=(0, 4))

            # Per-image weight slider — drag to change this image's
            # share of the audio duration. Default mid = 1.0.
            slider_row = ctk.CTkFrame(sub, fg_color="transparent")
            slider_row.pack(fill="x", padx=8, pady=(0, 8))
            ctk.CTkLabel(
                slider_row, text="share",
                text_color=t["muted"],
                font=ctk.CTkFont("Segoe UI", 9),
            ).pack(side="left", padx=(2, 6))
            w_var = ctk.DoubleVar(value=weights[i])
            slider = ctk.CTkSlider(
                slider_row, from_=0.5, to=4.0, variable=w_var,
                number_of_steps=35,
                command=lambda v, ii=i, cap=caption: self._on_weight_drag(
                    self.idx, ii, v, cap, total_ms,
                ),
                progress_color=t["accent"],
                button_color=t["accent"],
                button_hover_color=t["accent2"],
                fg_color=t["panel"],
            )
            slider.pack(side="left", fill="x", expand=True)

            # Per-image kebab — acts on this specific image_idx
            sub_kebab = ctk.CTkButton(
                sub, text="⋮", width=28, height=28,
                command=lambda i=i: self._open_image_menu(i),
                fg_color=t["panel"], hover_color=t["accent"],
                text_color=t["text"], corner_radius=14,
                border_width=1, border_color=t["stroke"],
                font=ctk.CTkFont("Segoe UI", 14, "bold"),
            )
            sub_kebab.place(relx=1.0, rely=0.0, anchor="ne", x=-6, y=6)

            # Drag-and-drop reordering: bind on the image label only —
            # CTkButton (the kebab) consumes its own clicks so it won't
            # trigger drag. Slider has its own click handling too.
            sub_label.bind("<ButtonPress-1>", lambda e, ii=i: self._dnd_start(ii))
            sub_label.bind("<B1-Motion>", self._dnd_motion)
            sub_label.bind("<ButtonRelease-1>", self._dnd_release)
            try:
                sub_label.configure(cursor="fleur")
            except Exception:
                pass

        # Timeline ruler under the row — visual map of the audio split
        # across images. Updates live as the user drags weight sliders.
        self._ruler_canvas = self._render_timeline_ruler(multi_box, pair)

    def _render_timeline_ruler(self, parent, pair):
        """Draw a colored timeline ruler showing each image's slice of
        the audio. Returns the tk.Canvas so callers can refresh it
        in-place (e.g., during weight-slider drag)."""
        t = self.theme
        n = len(pair.get("images") or [])
        if n < 2:
            return None
        ruler_wrap = ctk.CTkFrame(parent, fg_color=t["panel"],
                                  corner_radius=8, height=40)
        ruler_wrap.pack(fill="x", padx=4, pady=(6, 4))
        canvas = tk.Canvas(ruler_wrap, height=36, bg=t["panel"],
                           highlightthickness=0, bd=0)
        canvas.pack(fill="x", padx=4, pady=4)
        canvas.bind("<Configure>", lambda _e: self._redraw_timeline_ruler(canvas, pair))
        # Initial draw — schedule so winfo_width is realistic
        self.after(50, lambda: self._redraw_timeline_ruler(canvas, pair))
        return canvas

    def _redraw_timeline_ruler(self, canvas, pair):
        """Repaint the ruler with the current weights → slice durations."""
        if not canvas:
            return
        try:
            canvas.delete("all")
        except Exception:
            return
        t = self.theme
        images = pair.get("images") or []
        n = len(images)
        if n < 2:
            return
        try:
            w = max(40, canvas.winfo_width())
            h = max(20, canvas.winfo_height())
        except Exception:
            w, h = 800, 36
        weights = list(pair.get("weights") or [])
        while len(weights) < n:
            weights.append(1.0)
        sum_w = sum(weights) if sum(weights) > 0 else 1.0
        total_ms = pair.get("duration_ms") or 0

        # Compute boundary x-positions
        positions = [0.0]
        for wgt in weights:
            positions.append(positions[-1] + w * wgt / sum_w)
        positions[-1] = w  # snap last to width

        accent_a = t["accent"]
        accent_b = t["accent2"]

        for i in range(n):
            x0 = positions[i]
            x1 = positions[i + 1]
            color = accent_a if i % 2 == 0 else accent_b
            canvas.create_rectangle(
                x0 + 2, 4, x1 - 2, h - 4,
                fill=color, outline="",
            )
            # Caption inside (only if wide enough)
            if (x1 - x0) > 60:
                slice_ms = int(total_ms * weights[i] / sum_w) if total_ms else 0
                canvas.create_text(
                    (x0 + x1) / 2, h / 2,
                    text=f"#{i + 1}  ·  {fmt_ms(slice_ms)}",
                    fill="#FFFFFF",
                    font=("Segoe UI", 9, "bold"),
                )
            elif (x1 - x0) > 24:
                canvas.create_text(
                    (x0 + x1) / 2, h / 2, text=f"#{i + 1}",
                    fill="#FFFFFF", font=("Segoe UI", 9, "bold"),
                )

    def _on_weight_drag(self, idx, image_idx, new_value, caption_label, total_ms):
        """Slider command — fires on every motion. Update the pair's
        weights, refresh the per-image caption, redraw the ruler.
        Skip the parent SegmentPreviewWindow's full refresh because
        that would destroy the slider widget mid-drag."""
        if not isinstance(self.master, SegmentPreviewWindow):
            return
        win = self.master
        win._on_pair_weight_changed(idx, image_idx, new_value)
        # Update this caption inline
        weights = win.pairs[idx].get("weights") or []
        if 0 <= image_idx < len(weights):
            sum_w = sum(weights) if sum(weights) > 0 else 1.0
            slice_ms = int(total_ms * weights[image_idx] / sum_w) if total_ms else 0
            try:
                caption_label.configure(
                    text=f"Image {image_idx + 1} of {len(weights)}  ·  {fmt_ms(slice_ms)}",
                )
            except Exception:
                pass
        # Repaint the ruler
        if hasattr(self, "_ruler_canvas") and self._ruler_canvas:
            self._redraw_timeline_ruler(self._ruler_canvas, win.pairs[idx])

    # ----- Drag-and-drop image reordering -----

    def _dnd_start(self, src_idx):
        self._dnd_src = src_idx
        self._dnd_started = False

    def _dnd_motion(self, _e):
        if hasattr(self, "_dnd_src") and self._dnd_src is not None:
            self._dnd_started = True

    def _dnd_release(self, e):
        src = getattr(self, "_dnd_src", None)
        started = getattr(self, "_dnd_started", False)
        self._dnd_src = None
        self._dnd_started = False
        if src is None or not started:
            return
        # Find which sub-panel the cursor was over at release
        target = self._sub_panel_at(e.x_root, e.y_root)
        if target is None or target == src:
            return
        if isinstance(self.master, SegmentPreviewWindow):
            self.master._on_pair_image_reordered(self.idx, src, target)

    def _sub_panel_at(self, x_root, y_root):
        if not hasattr(self, "_sub_panels"):
            return None
        for i, sub in enumerate(self._sub_panels):
            try:
                if not sub.winfo_exists():
                    continue
                sx = sub.winfo_rootx()
                sy = sub.winfo_rooty()
                sw = sub.winfo_width()
                sh = sub.winfo_height()
                if sx <= x_root <= sx + sw and sy <= y_root <= sy + sh:
                    return i
            except Exception:
                continue
        return None

    def _action_preview_render(self):
        """Encode just this segment into a quick preview MP4 and launch
        it in the system default player. Runs the render on a worker
        thread so the UI doesn't freeze."""
        if not (0 <= self.idx < len(self.pairs)):
            return
        pair = self.pairs[self.idx]
        if not pair.get("audio"):
            messagebox.showinfo(
                "Preview render",
                "This segment has no audio — render preview is unavailable.",
                parent=self,
            )
            return
        images = list(pair.get("images") or [])
        if not images:
            # Audio-only: at full render the previous image extends here,
            # but for a single-segment preview we don't have access to
            # that fallback. Tell the user.
            messagebox.showinfo(
                "Preview render",
                "This segment is audio-only. Add an image first, or use "
                "the Continue → Render flow to see the previous image extend.",
                parent=self,
            )
            return

        # Snapshot weights so a slider drag mid-render doesn't change
        # what we're rendering.
        weights = list(pair.get("weights") or [1.0] * len(images))
        audio = pair["audio"]

        # Disable the button so the user can't double-launch
        try:
            self.preview_btn.configure(state="disabled", text="🎬  Rendering…")
        except Exception:
            pass

        def worker():
            out = None
            try:
                out = _render_single_segment_preview(
                    image_paths=images, audio_path=audio,
                    weights=weights,
                )
            except Exception as e:
                print(f"[SegmentDetailModal] preview render raised: {e}")
            self.after(0, lambda p=out: self._preview_render_done(p))

        threading.Thread(target=worker, daemon=True).start()

    def _preview_render_done(self, output_path: Optional[str]):
        """Called on the UI thread when _action_preview_render finishes."""
        try:
            self.preview_btn.configure(state="normal", text="🎬  Preview render")
        except Exception:
            pass
        if not output_path:
            messagebox.showerror(
                "Preview render failed",
                "Could not produce a preview MP4 — check the log for details.",
                parent=self,
            )
            return
        if not _open_with_default_player(output_path):
            messagebox.showinfo(
                "Preview render",
                f"Rendered to:\n{output_path}\n\n"
                "(Couldn't auto-launch a player — open it manually.)",
                parent=self,
            )

    def refresh_current(self):
        """Reload image + audio + status for the currently shown segment.
        Called by SegmentPreviewWindow after a pair's audio changes."""
        self.show_index(self.idx)

    def prev(self):
        if self.idx > 0:
            self.show_index(self.idx - 1)

    def next(self):
        if self.idx < len(self.pairs) - 1:
            self.show_index(self.idx + 1)

    # ----- kebab menu -----

    def _show_menu(self):
        pair = self.pairs[self.idx]
        history = pair.get("_history", [])
        has_edits = len(history) > 1
        has_audio = bool(pair.get("audio"))

        items = [
            {"kind": "header",
             "label": f"Audio — Segment {pair.get('index', self.idx + 1)}"},
            {"kind": "command",
             "label": "✏    Edit audio…",
             "command": self._open_editor,
             "state": "normal" if has_audio else "disabled"},
            {"kind": "separator"},
            {"kind": "command",
             "label": "↶    Undo last edit",
             "command": self._undo_audio,
             "state": "normal" if (has_audio and has_edits) else "disabled"},
            {"kind": "command",
             "label": "↺    Revert to original",
             "command": self._revert_audio,
             "state": "normal" if (has_audio and has_edits) else "disabled"},
            {"kind": "separator"},
            {"kind": "command",
             # When audio is None (orphan slot), the label flips so the
             # user understands they can fill the slot back in.
             "label": ("🗑    Delete audio…" if has_audio
                       else "📁    Add or replace audio…"),
             "command": self._open_audio_delete_menu,
             "accent": True},
        ]

        x = self.menu_btn.winfo_rootx()
        y = self.menu_btn.winfo_rooty() + self.menu_btn.winfo_height() + 4
        StyledPopupMenu(self, theme=self.theme, items=items, x=x, y=y,
                        min_width=260)

    def _open_audio_delete_menu(self):
        """Bubble up to the parent window which owns the file picker +
        pair list mutations needed for the delete/replace popup."""
        if not self.on_audio_delete_menu:
            return
        try:
            self.on_audio_delete_menu(self.idx)
        except Exception as e:
            print(f"[SegmentDetailModal] audio delete menu raised: {e}")

    def _open_image_menu(self, image_idx: int = 0):
        """Image kebab — delegates to the parent SegmentPreviewWindow which
        has the defaults list + mutation callbacks. `image_idx` selects
        which image within the segment the menu acts on (0 for single-
        image segments and the audio-only placeholder; 0..N-1 for the
        per-image kebabs in a multi-image row)."""
        if not self.on_image_menu:
            return
        # Use the click source widget if available — otherwise fall back to
        # the title/topbar position. For multi-image, the source kebab's
        # own coords aren't tracked here, so we approximate from the modal
        # itself (the menu pops near the upper-left of the window).
        try:
            x = self.winfo_pointerx()
            y = self.winfo_pointery() + 6
        except Exception:
            x = self.winfo_rootx() + 100
            y = self.winfo_rooty() + 100
        try:
            self.on_image_menu(self.idx, x, y, image_idx)
        except Exception as e:
            print(f"[SegmentDetailModal] image menu raised: {e}")

    def _open_editor(self):
        try:
            self.player.stop()
        except Exception:
            pass
        pair = self.pairs[self.idx]
        history = pair.get("_history", [])
        images = pair.get("images") or []
        n_imgs = len(images)
        img_name = Path(pair["image"]).name if pair.get("image") else "(no image)"
        if n_imgs >= 2:
            seg_label = (f"Segment {pair.get('index', self.idx + 1)} — "
                         f"{n_imgs} images")
        else:
            seg_label = f"Segment {pair.get('index', self.idx + 1)} — {img_name}"

        # For multi-image segments, compute chunk boundary times (in
        # seconds) using the current weights — they show as dashed
        # vertical lines on the waveform.
        chunk_boundaries_s: List[float] = []
        if n_imgs >= 2 and pair.get("duration_ms"):
            weights = list(pair.get("weights") or [])
            while len(weights) < n_imgs:
                weights.append(1.0)
            sum_w = sum(weights[:n_imgs]) if sum(weights[:n_imgs]) > 0 else 1.0
            total_s = pair["duration_ms"] / 1000.0
            cum = 0.0
            for i in range(n_imgs - 1):
                cum += weights[i]
                chunk_boundaries_s.append(total_s * cum / sum_w)

        # Released grab so the editor's grab_set doesn't clash with ours.
        try:
            self.grab_release()
        except Exception:
            pass

        SegmentEditorModal(
            self,
            audio_path=pair["audio"],
            original_duration_ms=(history[0].get("duration_ms")
                                  if history and isinstance(history[0], dict) else pair.get("duration_ms")),
            history_len=max(1, len(history)),
            theme=self.theme,
            segment_label=seg_label,
            on_save=self._on_edit_saved,
            on_cancel=self._on_edit_cancelled,
            chunk_boundaries_s=chunk_boundaries_s,
        )

    def _on_edit_saved(self, new_path: str, new_duration_ms: int):
        # Bubble change up to the preview window, then refresh ourselves.
        if self.on_audio_changed:
            try:
                self.on_audio_changed(self.idx, new_path, new_duration_ms)
            except Exception as e:
                print(f"[SegmentDetailModal] on_audio_changed raised: {e}")
        self.refresh_current()
        # Re-grab focus on this modal
        try:
            self.lift()
            self.focus_force()
            self.grab_set()
        except Exception:
            pass

    def _on_edit_cancelled(self):
        try:
            self.lift()
            self.focus_force()
            self.grab_set()
        except Exception:
            pass

    def _undo_audio(self):
        if self.on_pair_undo:
            try:
                self.on_pair_undo(self.idx)
            except Exception as e:
                print(f"[SegmentDetailModal] on_pair_undo raised: {e}")
        self.refresh_current()

    def _revert_audio(self):
        if self.on_pair_revert:
            try:
                self.on_pair_revert(self.idx)
            except Exception as e:
                print(f"[SegmentDetailModal] on_pair_revert raised: {e}")
        self.refresh_current()

    def _close(self):
        try:
            self.player.stop()
        except Exception:
            pass
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()


# ─────────────────────────── Card strip ───────────────────────────

class SegmentCard(ctk.CTkFrame):
    """Single tile in the horizontal strip — small thumbnail + label."""

    THUMB_W = 130
    THUMB_H = 74

    def __init__(self, master, theme, pair: Dict, idx: int, on_click: Callable[[int], None],
                 on_image_menu: Optional[Callable[[int, int, int], None]] = None, **kwargs):
        super().__init__(
            master,
            fg_color=theme["panel2"], corner_radius=10,
            border_width=2, border_color=theme["stroke"],
            **kwargs,
        )
        self.theme = theme
        self.pair = pair
        self.idx = idx
        self.on_click = on_click
        self.on_image_menu = on_image_menu
        self._thumb_ref = None

        self.thumb_label = ctk.CTkLabel(
            self, text="(no preview)", width=self.THUMB_W, height=self.THUMB_H,
            text_color=theme["muted"], fg_color=theme["panel"], corner_radius=6,
            font=ctk.CTkFont("Segoe UI", 10),
        )
        self.thumb_label.pack(padx=8, pady=(8, 4))

        self.title_label = ctk.CTkLabel(
            self, text=f"Segment {pair.get('index', idx + 1)}",
            font=ctk.CTkFont("Segoe UI", 11, "bold"),
            text_color=theme["text"],
        )
        self.title_label.pack(padx=8, pady=(0, 0))

        self.dur_label = ctk.CTkLabel(
            self, text=fmt_ms(pair.get("duration_ms")),
            font=ctk.CTkFont("Consolas", 10),
            text_color=theme["muted"],
        )
        self.dur_label.pack(padx=8, pady=(0, 8))

        # Forward clicks/hover from any child to this card
        for w in (self, self.thumb_label, self.title_label, self.dur_label):
            w.bind("<Button-1>", self._fire_click)
            w.bind("<Enter>", self._on_enter)
            w.bind("<Leave>", self._on_leave)
            try:
                w.configure(cursor="hand2")
            except Exception:
                pass

        # Small kebab overlaid on the top-right corner of the card. Click
        # opens the image-actions menu (replace / delete / fill default).
        # CTkButton consumes its own clicks so this doesn't bubble up to
        # the card's Button-1 binding. The card kebab always acts on the
        # primary image (image_idx=0); per-image kebabs in the detail
        # modal handle the secondary images.
        self.kebab_btn = ctk.CTkButton(
            self, text="⋮", width=22, height=22,
            command=self._open_kebab_menu,
            fg_color=theme["panel"], hover_color=theme["accent"],
            text_color=theme["text"], corner_radius=11,
            border_width=1, border_color=theme["stroke"],
            font=ctk.CTkFont("Segoe UI", 11, "bold"),
        )
        self.kebab_btn.place(relx=1.0, rely=0.0, anchor="ne", x=-6, y=10)

        # "+N" badge in the top-LEFT corner — appears only when the
        # segment has 2+ images so the user knows the audio is shared.
        self.multi_badge = ctk.CTkLabel(
            self, text="",
            fg_color=theme["accent2"], text_color="#0F1117",
            corner_radius=8,
            font=ctk.CTkFont("Segoe UI", 10, "bold"),
            width=30, height=18,
        )
        # placed (or unplaced) by refresh()

        # refresh() picks up audio=None / image=None / edited state and
        # also (re)loads the thumb, so first render is always correct.
        self.refresh()

    def _load_thumb(self):
        # Card thumb shows the PRIMARY image (images[0]). When there are
        # additional images, refresh() pins a "+N" badge on top-left.
        images = self.pair.get("images") or []
        img_path = images[0] if images else None
        if not img_path:
            self._thumb_ref = None
            self.thumb_label.configure(image=None, text="🎵\n(audio only)")
            return
        try:
            pil_img = Image.open(img_path).copy()
            if pil_img.mode not in ("RGB", "RGBA"):
                pil_img = pil_img.convert("RGBA")
            pil_img.thumbnail((self.THUMB_W, self.THUMB_H), Image.LANCZOS)
            self._thumb_ref = ctk.CTkImage(
                light_image=pil_img, dark_image=pil_img,
                size=pil_img.size,
            )
            self.thumb_label.configure(image=self._thumb_ref, text="")
        except Exception:
            self._thumb_ref = None
            self.thumb_label.configure(image=None, text="(no preview)")

    def _open_kebab_menu(self):
        if not self.on_image_menu:
            return
        x = self.kebab_btn.winfo_rootx()
        y = self.kebab_btn.winfo_rooty() + self.kebab_btn.winfo_height() + 2
        try:
            # Card kebab always acts on the primary image (image_idx=0).
            # Per-image kebabs live in the detail modal's multi-image row.
            self.on_image_menu(self.idx, x, y, 0)
        except Exception as e:
            print(f"[SegmentCard] image menu raised: {e}")

    def _fire_click(self, _e):
        try:
            self.on_click(self.idx)
        except Exception as e:
            print(f"[SegmentCard] click error: {e}")

    def _on_enter(self, _e):
        self.configure(border_color=self.theme["accent2"])
        self.title_label.configure(text_color=self.theme["accent2"])

    def _on_leave(self, _e):
        # Restore stroke based on whether this segment is currently edited.
        edited = isinstance(self.pair.get("_history"), list) and len(self.pair["_history"]) > 1
        self.configure(border_color=(self.theme["accent3"] if edited else self.theme["stroke"]))
        self.title_label.configure(text_color=self.theme["text"])

    def refresh(self):
        """Pick up the latest pair state — duration, edit history, image list.
        Called by SegmentPreviewWindow after the pair's audio or images change."""
        # Refresh thumbnail (primary image may have changed or been cleared)
        self._load_thumb()
        # Title may need updating if pair was renumbered after a deletion
        self.title_label.configure(text=f"Segment {self.pair.get('index', self.idx + 1)}")

        # Multi-image badge: show "+N" in top-left when 2+ images share
        # this segment's audio. Hide otherwise.
        images = self.pair.get("images") or []
        n_imgs = len(images)
        if n_imgs >= 2:
            self.multi_badge.configure(text=f"+{n_imgs - 1}")
            self.multi_badge.place(relx=0.0, rely=0.0, anchor="nw", x=10, y=10)
        else:
            try:
                self.multi_badge.place_forget()
            except Exception:
                pass

        has_audio = bool(self.pair.get("audio"))
        if not has_audio:
            # Orphan slot left over after audio-only delete. Show a clear
            # "(no audio)" badge in danger color so the user knows this
            # slot is currently skipped at render.
            self.dur_label.configure(text="(no audio)", text_color=self.theme["danger"])
            self.configure(border_color=self.theme["danger"])
            return
        # Duration label: total audio + per-image slice if multi-image.
        total_ms = self.pair.get("duration_ms")
        if n_imgs >= 2 and total_ms:
            per_ms = total_ms // n_imgs
            self.dur_label.configure(text=f"{fmt_ms(total_ms)}  ({fmt_ms(per_ms)} ea)")
        else:
            self.dur_label.configure(text=fmt_ms(total_ms))
        edited = isinstance(self.pair.get("_history"), list) and len(self.pair["_history"]) > 1
        if edited:
            self.configure(border_color=self.theme["accent3"])
            self.dur_label.configure(text_color=self.theme["accent3"])
        elif n_imgs >= 2:
            self.configure(border_color=self.theme["accent2"])
            self.dur_label.configure(text_color=self.theme["accent2"])
        else:
            self.configure(border_color=self.theme["stroke"])
            self.dur_label.configure(text_color=self.theme["muted"])


class SegmentPreviewWindow(ctk.CTkToplevel):
    """Modal preview gallery: horizontal arrow-connected card strip with
    Continue / Cancel buttons. Clicking a card opens SegmentDetailModal."""

    def __init__(self, master, pairs: List[Dict], theme,
                 on_continue: Callable[[], None],
                 on_cancel: Callable[[], None],
                 defaults: Optional[List[Dict]] = None,
                 images_dir: Optional[str] = None):
        super().__init__(master)
        self.theme = theme
        self.pairs = pairs
        self.on_continue = on_continue
        self.on_cancel = on_cancel
        self.defaults = list(defaults) if defaults else []
        self.images_dir = images_dir
        self._decided = False
        self._open_detail_modal: Optional[SegmentDetailModal] = None

        # Migrate legacy pair["image"] (scalar) → pair["images"] (list of
        # 0..N image paths). Each pair audio time-shares equally between
        # all its images at render time. We keep pair["image"] in sync
        # with images[0] (or None) so any back-compat reader still works.
        for p in self.pairs:
            if "images" in p:
                pass  # already migrated
            elif "image" in p:
                img = p["image"]
                p["images"] = [img] if img else []
            else:
                p["images"] = []
            p["image"] = p["images"][0] if p["images"] else None

        # Each pair gets a per-segment history of (path, duration_ms).
        # Index 0 is the original. Edits append; undo pops; revert truncates.
        for p in self.pairs:
            if "_history" not in p:
                p["_history"] = [{
                    "path": p["audio"],
                    "duration_ms": p.get("duration_ms"),
                    "label": "original",
                }]
            # Image history: snapshots of the images list. Same model as
            # audio history — index 0 is the original list, mutations
            # append new states, Undo pops, Revert truncates.
            if "_image_history" not in p:
                p["_image_history"] = [list(p.get("images") or [])]
            # Per-image weights for time-share split. Default = uniform
            # (all 1.0). User can adjust via sliders in the detail modal.
            if "weights" not in p:
                p["weights"] = [1.0] * len(p.get("images") or [])

        self.configure(fg_color=theme["bg"])
        self.title("Preview segments before render")

        # Width grows with segment count; clamped to 92% of screen by
        # _install_window_features. F11 = fullscreen, F10 = maximize.
        n = len(pairs)
        approx_w = min(1400, max(900, n * 55 + 260))
        approx_h = 520
        _install_window_features(
            self, ideal_w=approx_w, ideal_h=approx_h,
            min_w=900, min_h=440,
        )
        self.transient(master)

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._cancel)

        self.lift()
        try:
            self.focus_force()
            self.grab_set()
        except Exception:
            pass

        self.bind("<Escape>", lambda _e: self._cancel())
        self.bind("<Return>", lambda _e: self._continue())

    def _build_ui(self):
        t = self.theme

        # Top bar — title + subtitle
        topbar = ctk.CTkFrame(
            self, fg_color=t["panel"], corner_radius=14,
            border_width=1, border_color=t["stroke"],
        )
        topbar.pack(fill="x", padx=14, pady=(14, 8))
        _add_wm_buttons(topbar, self, t, allow_fullscreen=True)

        title_box = ctk.CTkFrame(topbar, fg_color="transparent")
        title_box.pack(side="left", padx=16, pady=10, fill="y")
        ctk.CTkLabel(
            title_box, text="Preview before render",
            font=ctk.CTkFont("Segoe UI", 18, "bold"),
            text_color=t["accent"],
            anchor="w",
        ).pack(anchor="w")
        ctk.CTkLabel(
            title_box,
            text=f"{len(self.pairs)} segment{'s' if len(self.pairs) != 1 else ''} paired with images.  "
                 "Click any tile to verify the audio matches the image.",
            font=ctk.CTkFont("Segoe UI", 11),
            text_color=t["muted"],
            anchor="w",
        ).pack(anchor="w")

        # Bottom button row — packed BEFORE the strip with side="bottom" so
        # it stays anchored even if the strip grows/scrolls.
        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.pack(side="bottom", fill="x", padx=14, pady=(0, 14))

        self.continue_btn = ctk.CTkButton(
            btn_row, text="Continue → Render", width=200, height=42,
            command=self._continue,
            fg_color=t["accent"], hover_color=hex_lerp(t["accent"], "#000000", 0.2),
            text_color="#FFFFFF", corner_radius=12,
            font=ctk.CTkFont("Segoe UI", 13, "bold"),
        )
        self.continue_btn.pack(side="right", padx=4)

        ctk.CTkButton(
            btn_row, text="Cancel", width=120, height=42,
            command=self._cancel,
            fg_color=t["panel2"], hover_color=t["danger"],
            text_color=t["text"], corner_radius=12,
            font=ctk.CTkFont("Segoe UI", 12, "bold"),
        ).pack(side="right", padx=4)

        ctk.CTkLabel(
            btn_row,
            text="Click a tile to inspect  ·  Esc = cancel  ·  Enter = render",
            text_color=t["muted"], font=ctk.CTkFont("Segoe UI", 10),
        ).pack(side="left", padx=4)

        # Horizontal scrollable strip with cards + arrows
        strip_container = ctk.CTkFrame(
            self, fg_color=t["panel"], corner_radius=14,
            border_width=1, border_color=t["stroke"],
        )
        strip_container.pack(fill="both", expand=True, padx=14, pady=(0, 8))

        self.strip = ctk.CTkScrollableFrame(
            strip_container,
            orientation="horizontal",
            fg_color=t["panel"],
            scrollbar_button_color=t["panel2"],
            scrollbar_button_hover_color=t["accent"],
        )
        self.strip.pack(fill="both", expand=True, padx=10, pady=10)

        self._cards: List[SegmentCard] = []
        self._populate_strip()

    def _populate_strip(self):
        """Build (or rebuild) the cards + arrows inside self.strip from self.pairs."""
        t = self.theme
        for i, pair in enumerate(self.pairs):
            if i > 0:
                arrow = ctk.CTkLabel(
                    self.strip, text="→",
                    font=ctk.CTkFont("Segoe UI", 22, "bold"),
                    text_color=t["accent2"],
                )
                arrow.pack(side="left", padx=4, pady=8)
            card = SegmentCard(
                self.strip, theme=t, pair=pair, idx=i,
                on_click=self._open_detail,
                on_image_menu=self._show_image_menu_at,
            )
            card.pack(side="left", padx=2, pady=8)
            self._cards.append(card)

    def _rebuild_strip(self):
        """Destroy all cards/arrows and re-populate after a structural change
        (segment deleted). Card indices and pair["index"] labels are
        regenerated from the current pairs list."""
        for child in list(self.strip.winfo_children()):
            try:
                child.destroy()
            except Exception:
                pass
        self._cards = []
        # Renumber pair["index"] (1-based) so the new visual order matches.
        for i, p in enumerate(self.pairs):
            p["index"] = i + 1
        self._populate_strip()

    def _open_detail(self, idx: int):
        # Detail modal grabs focus on top of this window's grab. When it
        # closes, focus returns here.
        try:
            self._open_detail_modal = SegmentDetailModal(
                self, self.pairs, idx, self.theme,
                on_audio_changed=self._on_pair_audio_changed,
                on_pair_undo=self._on_pair_undo,
                on_pair_revert=self._on_pair_revert,
                on_image_menu=self._show_image_menu_at,
                on_audio_delete_menu=self._show_audio_delete_modal,
            )
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"[SegmentPreviewWindow] detail open failed: {e}")
            self._open_detail_modal = None

    # ----- Image-action menu (shared by card kebabs + detail-modal kebab) -----

    def _show_image_menu_at(self, idx: int, x: int, y: int, image_idx: int = 0):
        """Build and pop the image-actions menu at screen coords (x, y).

        `image_idx` selects which image within pair[idx]["images"] the
        actions operate on. The card kebab always passes 0 (primary).
        Per-image kebabs in the detail modal pass their own slot index.

        The menu adapts based on how many images the segment has:
          - 0  (audio-only) → Pick / use prev/next / default / delete segment
          - 1               → Replace / Add another / Delete image…
          - 2+              → Replace / Add another / Swap left / Swap right
                              / Delete this image / Delete entire segment"""
        if not (0 <= idx < len(self.pairs)):
            return
        pair = self.pairs[idx]
        images = pair.get("images") or []
        n_images = len(images)
        is_first_pair = idx == 0
        is_last_pair = idx == len(self.pairs) - 1
        # Adjacent images for "duplicate previous/next" — look at the
        # PRIMARY image of the neighbour pair (their images[0] or None).
        prev_has_image = (not is_first_pair) and bool(self.pairs[idx - 1].get("image"))
        next_has_image = (not is_last_pair) and bool(self.pairs[idx + 1].get("image"))
        only_one_left = len(self.pairs) <= 1
        is_first_img = image_idx == 0
        is_last_img = image_idx == n_images - 1
        has_image_history = len(pair.get("_image_history") or []) > 1

        items: List[Dict] = []

        if n_images == 0:
            # Audio-only slot — fill it back in or delete
            items.append({"kind": "header",
                          "label": f"Segment {pair.get('index', idx + 1)} — audio only"})
            items.append({"kind": "command",
                          "label": "📁   Pick image from file…",
                          "command": lambda: self._action_replace_pick(idx, image_idx=0)})
            items.append({"kind": "command",
                          "label": "↑    Use previous image",
                          "command": lambda: self._action_duplicate(idx, "prev", image_idx=0),
                          "state": "normal" if prev_has_image else "disabled"})
            items.append({"kind": "command",
                          "label": "↓    Use next image",
                          "command": lambda: self._action_duplicate(idx, "next", image_idx=0),
                          "state": "normal" if next_has_image else "disabled"})
            for d in self.defaults:
                items.append({"kind": "command",
                              "label": f"🖼    {d.get('label', Path(d['path']).name)}",
                              "command": lambda p=d["path"]: self._action_replace_with_path(idx, p, image_idx=0)})
            if has_image_history:
                items.append({"kind": "separator"})
                items.append({"kind": "command",
                              "label": "↶    Undo image change",
                              "command": lambda: self._on_pair_image_undo(idx)})
                items.append({"kind": "command",
                              "label": "↺    Revert images to original",
                              "command": lambda: self._on_pair_image_revert(idx)})
            items.append({"kind": "separator"})
            items.append({"kind": "command",
                          "label": "🗑    Delete entire segment",
                          "command": lambda: self._action_delete_segment(idx),
                          "state": "disabled" if only_one_left else "normal"})
        else:
            # 1+ images: header tells the user which image they're editing
            if n_images == 1:
                header_label = f"Segment {pair.get('index', idx + 1)}"
            else:
                header_label = f"Image {image_idx + 1} of {n_images}  ·  Segment {pair.get('index', idx + 1)}"
            items.append({"kind": "header", "label": header_label})

            items.append({"kind": "command",
                          "label": "📁   Replace this image…",
                          "command": lambda: self._action_replace_pick(idx, image_idx=image_idx)})
            items.append({"kind": "command",
                          "label": "↑    Duplicate previous image",
                          "command": lambda: self._action_duplicate(idx, "prev", image_idx=image_idx),
                          "state": "normal" if prev_has_image else "disabled"})
            items.append({"kind": "command",
                          "label": "↓    Duplicate next image",
                          "command": lambda: self._action_duplicate(idx, "next", image_idx=image_idx),
                          "state": "normal" if next_has_image else "disabled"})
            for d in self.defaults:
                items.append({"kind": "command",
                              "label": f"🖼    Use {d.get('label', Path(d['path']).name)}",
                              "command": lambda p=d["path"]: self._action_replace_with_path(idx, p, image_idx=image_idx)})

            items.append({"kind": "separator"})
            items.append({"kind": "command",
                          "label": "➕   Add another image…",
                          "command": lambda: self._action_add_another(idx),
                          "accent": True})

            if n_images >= 2:
                items.append({"kind": "command",
                              "label": "◀    Swap left",
                              "command": lambda: self._action_swap(idx, image_idx, "left"),
                              "state": "normal" if not is_first_img else "disabled"})
                items.append({"kind": "command",
                              "label": "▶    Swap right",
                              "command": lambda: self._action_swap(idx, image_idx, "right"),
                              "state": "normal" if not is_last_img else "disabled"})
                if has_image_history:
                    items.append({"kind": "separator"})
                    items.append({"kind": "command",
                                  "label": "↶    Undo image change",
                                  "command": lambda: self._on_pair_image_undo(idx)})
                    items.append({"kind": "command",
                                  "label": "↺    Revert images to original",
                                  "command": lambda: self._on_pair_image_revert(idx)})
                items.append({"kind": "separator"})
                items.append({"kind": "command",
                              "label": "🗑    Delete this image",
                              "command": lambda: self._action_remove_one(idx, image_idx)})
                items.append({"kind": "command",
                              "label": "🗑    Delete entire segment",
                              "command": lambda: self._action_delete_segment(idx),
                              "state": "disabled" if only_one_left else "normal"})
            else:
                if has_image_history:
                    items.append({"kind": "separator"})
                    items.append({"kind": "command",
                                  "label": "↶    Undo image change",
                                  "command": lambda: self._on_pair_image_undo(idx)})
                    items.append({"kind": "command",
                                  "label": "↺    Revert images to original",
                                  "command": lambda: self._on_pair_image_revert(idx)})
                items.append({"kind": "separator"})
                items.append({"kind": "command",
                              "label": "🗑    Delete image…",
                              "command": lambda: self._action_delete_image(idx)})

        StyledPopupMenu(self, theme=self.theme, items=items, x=x, y=y,
                        min_width=300)

    # ----- Image-action handlers -----

    def _action_replace_pick(self, idx: int, image_idx: int = 0):
        """Open a file picker; on success, set pair[idx]["images"][image_idx]
        (or append to the empty list if the slot is audio-only)."""
        IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tiff", ".tif")
        types = [
            ("Image files", " ".join(f"*{e}" for e in IMAGE_EXTS)),
            ("All files", "*.*"),
        ]
        initial = self.images_dir if (self.images_dir and Path(self.images_dir).is_dir()) else None
        path = filedialog.askopenfilename(
            title="Pick an image",
            initialdir=initial,
            filetypes=types,
            parent=self,
        )
        if path:
            self._on_pair_image_changed(idx, path, image_idx=image_idx)

    def _action_duplicate(self, idx: int, direction: str, image_idx: int = 0):
        """Copy the primary image from the adjacent pair (prev or next)
        into pair[idx]["images"][image_idx]."""
        if direction == "prev" and idx > 0:
            src = self.pairs[idx - 1].get("image")
        elif direction == "next" and idx < len(self.pairs) - 1:
            src = self.pairs[idx + 1].get("image")
        else:
            src = None
        if src:
            self._on_pair_image_changed(idx, src, image_idx=image_idx)

    def _action_replace_with_path(self, idx: int, path: str, image_idx: int = 0):
        if path:
            self._on_pair_image_changed(idx, path, image_idx=image_idx)

    def _action_delete_image(self, idx: int):
        """Open the two-choice DeleteImageModal (single-image segments)."""
        if not (0 <= idx < len(self.pairs)):
            return
        pair = self.pairs[idx]
        seg_label = f"Segment {pair.get('index', idx + 1)}"
        DeleteImageModal(
            self, self.theme,
            segment_label=seg_label,
            on_drop_image=lambda: self._on_pair_image_dropped(idx),
            on_delete_segment=lambda: self._on_pair_segment_deleted(idx),
            can_delete_segment=(len(self.pairs) > 1),
        )

    def _action_delete_segment(self, idx: int):
        """Delete entire segment from an audio-only slot — straight to the
        deletion (no need to ask 'image only' since there's no image)."""
        self._on_pair_segment_deleted(idx)

    def _action_add_another(self, idx: int):
        """Pick one OR MORE images and APPEND each to pair[idx]["images"].

        The audio time-shares equally across all images at render time, so
        a 60-second segment with 3 images will display each image for 20s.
        Multi-select is supported — Ctrl/Shift in the file dialog. Order
        matches the dialog's selection order (Tk's documented behaviour)."""
        IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tiff", ".tif")
        types = [
            ("Image files", " ".join(f"*{e}" for e in IMAGE_EXTS)),
            ("All files", "*.*"),
        ]
        initial = self.images_dir if (self.images_dir and Path(self.images_dir).is_dir()) else None
        paths = filedialog.askopenfilenames(
            title="Pick image(s) to add — Ctrl / Shift to select multiple",
            initialdir=initial,
            filetypes=types,
            parent=self,
        )
        if not paths:
            return
        # askopenfilenames returns a tuple on success
        for path in paths:
            if path:
                self._on_pair_image_added(idx, path)

    def _action_swap(self, idx: int, image_idx: int, direction: str):
        """Swap pair[idx]["images"][image_idx] with its neighbour."""
        self._on_pair_image_swapped(idx, image_idx, direction)

    def _action_remove_one(self, idx: int, image_idx: int):
        """Remove a single image from a multi-image segment (no modal —
        the other images keep playing for the audio's duration)."""
        self._on_pair_image_remove_at(idx, image_idx)

    # ----- Audio delete / replace popup (called from detail modal kebab) -----

    def _show_audio_delete_modal(self, idx: int):
        """Open the three-choice DeleteAudioModal for pair[idx]."""
        if not (0 <= idx < len(self.pairs)):
            return
        pair = self.pairs[idx]
        seg_label = f"Segment {pair.get('index', idx + 1)}"
        DeleteAudioModal(
            self, self.theme,
            segment_label=seg_label,
            has_image=bool(pair.get("image")),
            can_delete_segment=(len(self.pairs) > 1),
            can_drop_audio=(len(self.pairs) > 1 and bool(pair.get("audio"))),
            on_delete_segment=lambda: self._on_pair_segment_deleted(idx),
            on_drop_audio_only=lambda: self._on_pair_audio_only_deleted(idx),
            on_replace_audio=lambda: self._action_audio_replace_pick(idx),
        )

    def _action_audio_replace_pick(self, idx: int):
        """Open a file picker for an audio file; on success, replace pair audio."""
        types = [
            ("Audio files", "*.wav *.mp3 *.m4a *.flac *.ogg *.aac *.wma"),
            ("All files", "*.*"),
        ]
        # Prefer the existing audio's directory as initial dir
        initial = None
        cur = self.pairs[idx].get("audio") if 0 <= idx < len(self.pairs) else None
        if cur:
            try:
                initial = str(Path(cur).parent)
            except Exception:
                initial = None
        path = filedialog.askopenfilename(
            title="Pick replacement audio",
            initialdir=initial,
            filetypes=types,
            parent=self,
        )
        if path:
            self._on_pair_audio_replaced(idx, path)

    # ----- Pair-history mutations (called by detail modal) -----

    def _on_pair_audio_changed(self, idx: int, new_path: str, new_duration_ms: int):
        """User confirmed a trim — append the new version to history and
        make it the active audio for this pair."""
        if not (0 <= idx < len(self.pairs)):
            return
        pair = self.pairs[idx]
        history = pair.setdefault("_history", [{
            "path": pair["audio"],
            "duration_ms": pair.get("duration_ms"),
            "label": "original",
        }])
        history.append({
            "path": new_path,
            "duration_ms": new_duration_ms,
            "label": f"edit {len(history)}",
        })
        pair["audio"] = new_path
        pair["duration_ms"] = new_duration_ms
        self._refresh_card(idx)

    def _on_pair_undo(self, idx: int):
        """Pop the latest version from history; revert pair to the previous one."""
        if not (0 <= idx < len(self.pairs)):
            return
        pair = self.pairs[idx]
        history = pair.get("_history") or []
        if len(history) <= 1:
            return
        history.pop()
        prev = history[-1]
        pair["audio"] = prev["path"]
        pair["duration_ms"] = prev.get("duration_ms")
        self._refresh_card(idx)

    def _on_pair_revert(self, idx: int):
        """Discard all edits and restore the original audio."""
        if not (0 <= idx < len(self.pairs)):
            return
        pair = self.pairs[idx]
        history = pair.get("_history") or []
        if len(history) <= 1:
            return
        original = history[0]
        pair["_history"] = [original]
        pair["audio"] = original["path"]
        pair["duration_ms"] = original.get("duration_ms")
        self._refresh_card(idx)

    def _refresh_card(self, idx: int):
        if 0 <= idx < len(self._cards):
            try:
                self._cards[idx].refresh()
            except Exception as e:
                print(f"[SegmentPreviewWindow] card refresh failed: {e}")

    def _refresh_open_detail(self, idx_changed: int):
        """If the detail modal is showing the changed pair, refresh it."""
        m = self._open_detail_modal
        if not m:
            return
        try:
            if not m.winfo_exists():
                self._open_detail_modal = None
                return
        except Exception:
            self._open_detail_modal = None
            return
        if m.idx == idx_changed:
            try:
                m.refresh_current()
            except Exception as e:
                print(f"[SegmentPreviewWindow] detail refresh failed: {e}")

    # ----- Image-mutation entry points (also exercised by tests) -----

    def _sync_image_alias(self, idx: int):
        """Keep pair["image"] (legacy scalar) aligned with pair["images"][0]
        so any back-compat reader still sees the primary image. Internally
        all multi-image semantics flow through pair["images"]."""
        if 0 <= idx < len(self.pairs):
            p = self.pairs[idx]
            imgs = p.get("images") or []
            p["image"] = imgs[0] if imgs else None

    def _commit_image_state(self, idx: int):
        """Snapshot pair[idx]["images"] into _image_history. Called after
        any image mutation so Undo/Revert can roll back. Skips if the
        new state is identical to the latest entry (no-op mutations
        don't pollute the history)."""
        if not (0 <= idx < len(self.pairs)):
            return
        p = self.pairs[idx]
        hist = p.setdefault("_image_history", [list(p.get("images") or [])])
        new_state = list(p.get("images") or [])
        if not hist or hist[-1] != new_state:
            hist.append(new_state)
        # Keep weights aligned with images length — append 1.0 for new
        # slots, truncate when slots are removed.
        weights = p.setdefault("weights", [])
        n = len(new_state)
        while len(weights) < n:
            weights.append(1.0)
        if len(weights) > n:
            del weights[n:]

    def _on_pair_image_undo(self, idx: int):
        """Undo the last image mutation for pair[idx]. Pops the most
        recent _image_history entry and restores the previous state."""
        if not (0 <= idx < len(self.pairs)):
            return
        p = self.pairs[idx]
        hist = p.get("_image_history") or []
        if len(hist) <= 1:
            return  # nothing to undo
        hist.pop()
        p["images"] = list(hist[-1])
        # Re-sync weights length
        weights = p.setdefault("weights", [])
        while len(weights) < len(p["images"]):
            weights.append(1.0)
        if len(weights) > len(p["images"]):
            del weights[len(p["images"]):]
        self._sync_image_alias(idx)
        self._refresh_card(idx)
        self._refresh_open_detail(idx)

    def _on_pair_image_revert(self, idx: int):
        """Discard all image edits and restore the original list."""
        if not (0 <= idx < len(self.pairs)):
            return
        p = self.pairs[idx]
        hist = p.get("_image_history") or []
        if len(hist) <= 1:
            return
        original = list(hist[0])
        p["_image_history"] = [original]
        p["images"] = list(original)
        p["weights"] = [1.0] * len(original)
        self._sync_image_alias(idx)
        self._refresh_card(idx)
        self._refresh_open_detail(idx)

    def _on_pair_image_changed(self, idx: int, new_path: str, image_idx: int = 0):
        """Replace pair[idx]["images"][image_idx] with `new_path`.

        If image_idx is out of range — typically because the segment was
        audio-only (empty list) — the new path is appended so the slot
        gets its first image."""
        if not (0 <= idx < len(self.pairs)):
            return
        images = self.pairs[idx].setdefault("images", [])
        if 0 <= image_idx < len(images):
            images[image_idx] = new_path
        else:
            images.append(new_path)
        self._sync_image_alias(idx)
        self._commit_image_state(idx)
        self._refresh_card(idx)
        self._refresh_open_detail(idx)

    def _on_pair_image_dropped(self, idx: int):
        """Clear all images for pair[idx] — the segment becomes audio-only.
        At render time the previous segment's image will extend over this audio."""
        if not (0 <= idx < len(self.pairs)):
            return
        self.pairs[idx]["images"] = []
        self._sync_image_alias(idx)
        self._commit_image_state(idx)
        self._refresh_card(idx)
        self._refresh_open_detail(idx)

    def _on_pair_image_added(self, idx: int, new_path: str):
        """Append `new_path` to pair[idx]["images"]. The segment's audio
        will time-share equally across all images at render time
        (1 minute audio + 2 images → 30s each, etc.)."""
        if not (0 <= idx < len(self.pairs)):
            return
        if not new_path:
            return
        self.pairs[idx].setdefault("images", []).append(new_path)
        self._sync_image_alias(idx)
        self._commit_image_state(idx)
        self._refresh_card(idx)
        self._refresh_open_detail(idx)

    def _on_pair_image_swapped(self, idx: int, image_idx: int, direction: str):
        """Swap pair[idx]["images"][image_idx] with its left or right
        neighbor. direction = 'left' | 'right'. Out-of-range swaps are no-ops."""
        if not (0 <= idx < len(self.pairs)):
            return
        images = self.pairs[idx].get("images") or []
        if direction == "left":
            j = image_idx - 1
        elif direction == "right":
            j = image_idx + 1
        else:
            return
        if not (0 <= image_idx < len(images)) or not (0 <= j < len(images)):
            return
        images[image_idx], images[j] = images[j], images[image_idx]
        # Weights swap with their images so each image keeps its slice
        weights = self.pairs[idx].get("weights") or []
        if image_idx < len(weights) and j < len(weights):
            weights[image_idx], weights[j] = weights[j], weights[image_idx]
        self._sync_image_alias(idx)
        self._commit_image_state(idx)
        self._refresh_card(idx)
        self._refresh_open_detail(idx)

    def _on_pair_image_remove_at(self, idx: int, image_idx: int):
        """Remove ONE image from pair[idx]["images"] at image_idx (used
        when a multi-image segment has more than one and the user wants
        to drop only this specific slot — the others continue to share
        the audio). If the list becomes empty, the segment becomes
        audio-only (same end state as `_on_pair_image_dropped`)."""
        if not (0 <= idx < len(self.pairs)):
            return
        images = self.pairs[idx].get("images") or []
        if 0 <= image_idx < len(images):
            images.pop(image_idx)
        # Drop the corresponding weight too
        weights = self.pairs[idx].get("weights") or []
        if 0 <= image_idx < len(weights):
            weights.pop(image_idx)
        self._sync_image_alias(idx)
        self._commit_image_state(idx)
        self._refresh_card(idx)
        self._refresh_open_detail(idx)

    def _on_pair_image_reordered(self, idx: int, src: int, dst: int):
        """Move pair[idx]["images"][src] to position dst. Used by
        drag-and-drop reordering in the multi-image detail row."""
        if not (0 <= idx < len(self.pairs)):
            return
        images = self.pairs[idx].get("images") or []
        if not (0 <= src < len(images)) or not (0 <= dst < len(images)):
            return
        if src == dst:
            return
        item = images.pop(src)
        images.insert(dst, item)
        # Move weight along with image
        weights = self.pairs[idx].get("weights") or []
        if 0 <= src < len(weights):
            w = weights.pop(src)
            insert_at = min(dst, len(weights))
            weights.insert(insert_at, w)
        self._sync_image_alias(idx)
        self._commit_image_state(idx)
        self._refresh_card(idx)
        self._refresh_open_detail(idx)

    def _on_pair_weight_changed(self, idx: int, image_idx: int, new_weight: float):
        """User adjusted a per-image duration-share slider. Clamp to a
        sane range (0.1 .. 10.0) and refresh the card so the duration
        labels update."""
        if not (0 <= idx < len(self.pairs)):
            return
        p = self.pairs[idx]
        n = len(p.get("images") or [])
        weights = p.setdefault("weights", [1.0] * n)
        while len(weights) < n:
            weights.append(1.0)
        if len(weights) > n:
            del weights[n:]
        if 0 <= image_idx < len(weights):
            weights[image_idx] = max(0.1, min(10.0, float(new_weight)))
        # Refresh the card so its caption shows the new per-image duration.
        # We DON'T refresh the detail modal mid-drag because that would
        # destroy the slider widget the user is currently dragging.
        self._refresh_card(idx)

    def _on_pair_audio_only_deleted(self, idx: int):
        """Delete pair[idx]'s AUDIO only — image stays. All later audios
        shift up by one (audio[k+1] → slot k, ..., audio[N] → slot N-1),
        and the trailing pair is left with audio=None. The renderer skips
        pairs whose audio is None.

        This implements the user's "third audio segment is now matched
        with the second image" semantic — deleting in the middle of the
        chain pulls subsequent audios up to fill the gap."""
        if not (0 <= idx < len(self.pairs)):
            return
        if len(self.pairs) <= 1:
            try:
                messagebox.showwarning(
                    "Cannot delete",
                    "Only one segment remains — nothing left to shift up.\n"
                    "Use 'Delete entire segment' if you want to remove it.",
                    parent=self,
                )
            except Exception:
                pass
            return

        # Shift audio context up. Each pair owns:
        #   audio (path), duration_ms, _history (per-segment edit chain)
        # All three move together.
        for i in range(idx, len(self.pairs) - 1):
            nxt = self.pairs[i + 1]
            self.pairs[i]["audio"] = nxt.get("audio")
            self.pairs[i]["duration_ms"] = nxt.get("duration_ms")
            self.pairs[i]["_history"] = list(nxt.get("_history", []))

        # Trailing pair: image kept, audio becomes None (orphan slot).
        last = self.pairs[-1]
        last["audio"] = None
        last["duration_ms"] = None
        last["_history"] = []

        # Refresh every card (audio context changed for all of them at idx..end)
        self._rebuild_strip()

        # If detail modal is open, refresh whatever it's currently showing
        m = self._open_detail_modal
        if m:
            try:
                if m.winfo_exists():
                    m.show_index(m.idx)
                else:
                    self._open_detail_modal = None
            except Exception:
                pass

    def _on_pair_audio_replaced(self, idx: int, new_path: str):
        """Replace pair[idx]'s audio with the file at `new_path`.

        Loads the new audio just enough to determine its duration, then
        appends a "replaced" entry to that pair's _history (so the user
        can still Undo / Revert via the existing audio kebab items). The
        image keeps displaying for the new (possibly longer) duration."""
        if not (0 <= idx < len(self.pairs)):
            return
        try:
            seg = AudioSegment.from_file(new_path)
            new_dur_ms = int(len(seg))
        except Exception as e:
            try:
                messagebox.showerror(
                    "Replace failed",
                    f"Could not load audio:\n{e}",
                    parent=self,
                )
            except Exception:
                pass
            return

        pair = self.pairs[idx]
        history = pair.setdefault("_history", [])
        if not history:
            # Defensive — initialise with a synthetic original entry so
            # Undo can roll back to whatever was there before this replace.
            history.append({
                "path": pair.get("audio"),
                "duration_ms": pair.get("duration_ms"),
                "label": "original",
            })
        history.append({
            "path": new_path,
            "duration_ms": new_dur_ms,
            "label": f"replaced (v{len(history)})",
        })
        pair["audio"] = new_path
        pair["duration_ms"] = new_dur_ms

        self._refresh_card(idx)
        self._refresh_open_detail(idx)

    def _on_pair_segment_deleted(self, idx: int):
        """Remove pair[idx] entirely (image + audio). Renumber remaining
        pairs and rebuild the card strip. Refuses if only one pair remains."""
        if not (0 <= idx < len(self.pairs)):
            return
        if len(self.pairs) <= 1:
            try:
                messagebox.showwarning(
                    "Cannot delete",
                    "At least one segment must remain in the strip.",
                    parent=self,
                )
            except Exception:
                pass
            return
        self.pairs.pop(idx)

        # If detail modal is open, point it at a sensible neighbor or close
        # it if the just-deleted pair was the one being shown and there's
        # no neighbor to slide to.
        m = self._open_detail_modal
        if m:
            try:
                if not m.winfo_exists():
                    self._open_detail_modal = None
                    m = None
            except Exception:
                self._open_detail_modal = None
                m = None
        if m:
            new_idx = m.idx
            if m.idx == idx:
                new_idx = min(idx, len(self.pairs) - 1)
            elif m.idx > idx:
                new_idx = m.idx - 1
            try:
                m.idx = new_idx
                m.show_index(new_idx)
            except Exception as e:
                print(f"[SegmentPreviewWindow] detail re-show after delete failed: {e}")

        self._rebuild_strip()

    def _continue(self):
        if self._decided:
            return
        self._decided = True
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()
        try:
            if self.on_continue:
                self.on_continue()
        except Exception:
            pass

    def _cancel(self):
        if self._decided:
            return
        self._decided = True
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()
        try:
            if self.on_cancel:
                self.on_cancel()
        except Exception:
            pass


# ─────────────────────────── Public entry point ───────────────────────────

def show_preview_blocking(root, pairs: List[Dict], theme_name: str = "dark",
                          defaults: Optional[List[Dict]] = None,
                          images_dir: Optional[str] = None) -> bool:
    """Block the calling thread until the user accepts (Continue) or
    rejects (Cancel / X / Esc) the preview.

    Must be called from a NON-UI worker thread. The UI is built on the
    Tk main thread via root.after(0, ...).

    `pairs` is a list of dicts shaped like:
        {
            "image": str,        # path to the image for this segment, or None
            "audio": str,        # path to the segment audio file (wav)
            "duration_ms": int,  # segment duration
            "index": int,        # 1-based segment number for labels
        }

    `defaults` is an optional list of {"label", "path"} entries surfaced
    in the per-segment image kebab as quick replacement targets (e.g.,
    "Front image", "Back image"). `images_dir` is the directory the
    "Pick file…" dialog opens to by default.

    Returns True if the user wants to proceed with rendering, False
    otherwise. If the UI fails to build or `pairs` is empty, returns
    True (fail-open: don't block render on a UI bug).
    """
    if not pairs:
        return True

    theme = THEMES.get(theme_name, THEMES["dark"])

    decision = {"proceed": False}
    done_event = threading.Event()

    def on_continue():
        decision["proceed"] = True
        done_event.set()

    def on_cancel():
        decision["proceed"] = False
        done_event.set()

    def show_ui():
        try:
            ctk.set_appearance_mode(theme["appearance"])
            SegmentPreviewWindow(
                root, pairs, theme, on_continue, on_cancel,
                defaults=defaults, images_dir=images_dir,
            )
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"[show_preview_blocking] failed to build preview UI: {e}")
            decision["proceed"] = True  # fail-open
            done_event.set()

    try:
        root.after(0, show_ui)
    except Exception as e:
        print(f"[show_preview_blocking] couldn't schedule UI: {e}")
        return True

    done_event.wait()
    return decision["proceed"]
