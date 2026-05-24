#!/usr/bin/env python3
"""
Voice Editor — load a voice file, paint cut regions on the waveform,
preview the result, and export a clean continuous audio file with
the cuts removed.

Run:
    python voice_editor.py
"""

from __future__ import annotations

import os
import sys
import math
import threading
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np
from pydub import AudioSegment

import tkinter as tk
from tkinter import filedialog, messagebox

import customtkinter as ctk
import sounddevice as sd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from simple_video_creator import find_ffmpeg, configure_pydub, _safe_console  # noqa: E402
try:
    from .paths import OUTPUT_DIR
except ImportError:
    from paths import OUTPUT_DIR


# ─────────────────────────── Theme ───────────────────────────

THEMES = {
    "dark": {
        "appearance": "dark",
        "bg": "#0F1117",
        "panel": "#181B25",
        "panel2": "#222637",
        "stroke": "#2C3145",
        "text": "#F5F7FA",
        "muted": "#8B92A6",
        "accent": "#7C5CFF",      # violet
        "accent2": "#22D3EE",     # cyan
        "accent3": "#F472B6",     # pink (highlight)
        "danger": "#F43F5E",      # red (cuts)
        "ok": "#34D399",
        "wave_top": "#A78BFA",
        "wave_mid": "#7C5CFF",
        "wave_bot": "#22D3EE",
        "cursor": "#22D3EE",
        "grid": "#2C3145",
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
        "wave_top": "#9F7BFF",
        "wave_mid": "#6D4AFF",
        "wave_bot": "#0891B2",
        "cursor": "#0891B2",
        "grid": "#D9DDEA",
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


def fmt_time(s: float) -> str:
    if s is None or math.isnan(s) or math.isinf(s):
        return "--:--.---"
    s = max(0.0, s)
    m = int(s // 60)
    rem = s - m * 60
    return f"{m:02d}:{rem:06.3f}"


# ─────────────────────────── Data model ───────────────────────────

@dataclass
class Cut:
    start_s: float
    end_s: float

    def length(self) -> float:
        return max(0.0, self.end_s - self.start_s)

    def clone(self) -> "Cut":
        return Cut(self.start_s, self.end_s)


@dataclass
class Project:
    source_path: Path
    samples: np.ndarray            # float32, shape (N,) mono or (N, 2) stereo
    sample_rate: int
    cuts: List[Cut] = field(default_factory=list)
    history: List[List[Cut]] = field(default_factory=lambda: [[]])
    history_idx: int = 0

    @property
    def duration_s(self) -> float:
        return len(self.samples) / float(self.sample_rate) if self.sample_rate else 0.0

    @property
    def channels(self) -> int:
        return 1 if self.samples.ndim == 1 else self.samples.shape[1]

    # ----- editing ops -----

    def add_cut(self, a: float, b: float, snapshot: bool = True) -> bool:
        a, b = float(min(a, b)), float(max(a, b))
        a = max(0.0, a)
        b = min(self.duration_s, b)
        if b - a < 0.020:
            return False
        self.cuts.append(Cut(a, b))
        self._normalize()
        if snapshot:
            self._snapshot()
        return True

    def remove_cut(self, idx: int):
        if 0 <= idx < len(self.cuts):
            del self.cuts[idx]
            self._snapshot()

    def clear_cuts(self):
        if self.cuts:
            self.cuts = []
            self._snapshot()

    def update_cut(self, idx: int, start_s: float, end_s: float, snapshot: bool = True):
        if not (0 <= idx < len(self.cuts)):
            return
        a, b = float(min(start_s, end_s)), float(max(start_s, end_s))
        a = max(0.0, a)
        b = min(self.duration_s, b)
        if b - a < 0.020:
            return
        self.cuts[idx].start_s = a
        self.cuts[idx].end_s = b
        self._normalize()
        if snapshot:
            self._snapshot()

    def _normalize(self):
        if not self.cuts:
            return
        ordered = sorted(self.cuts, key=lambda c: c.start_s)
        merged: List[Cut] = [ordered[0].clone()]
        for c in ordered[1:]:
            top = merged[-1]
            if c.start_s <= top.end_s:
                top.end_s = max(top.end_s, c.end_s)
            else:
                merged.append(c.clone())
        self.cuts = merged

    def _snapshot(self):
        # Drop any redo-tail
        self.history = self.history[: self.history_idx + 1]
        self.history.append([c.clone() for c in self.cuts])
        self.history_idx = len(self.history) - 1
        # Cap history to 100 entries
        if len(self.history) > 100:
            self.history = self.history[-100:]
            self.history_idx = len(self.history) - 1

    def can_undo(self) -> bool:
        return self.history_idx > 0

    def can_redo(self) -> bool:
        return self.history_idx < len(self.history) - 1

    def undo(self):
        if self.can_undo():
            self.history_idx -= 1
            self.cuts = [c.clone() for c in self.history[self.history_idx]]

    def redo(self):
        if self.can_redo():
            self.history_idx += 1
            self.cuts = [c.clone() for c in self.history[self.history_idx]]

    # ----- derived -----

    def total_cut_s(self) -> float:
        return sum(c.length() for c in self.cuts)

    def keep_regions(self) -> List[Tuple[float, float]]:
        """Returns list of (start_s, end_s) intervals that survive the cuts."""
        if not self.cuts:
            return [(0.0, self.duration_s)] if self.duration_s > 0 else []
        regs: List[Tuple[float, float]] = []
        cursor = 0.0
        for c in self.cuts:
            if c.start_s > cursor:
                regs.append((cursor, c.start_s))
            cursor = max(cursor, c.end_s)
        if cursor < self.duration_s:
            regs.append((cursor, self.duration_s))
        return regs

    def render(self, fade_ms: int = 5) -> np.ndarray:
        """Apply cuts and return concatenated samples with cosine fades at every join."""
        regs = self.keep_regions()
        if not regs:
            shape = (0,) if self.samples.ndim == 1 else (0, self.samples.shape[1])
            return np.zeros(shape, dtype=np.float32)

        sr = self.sample_rate
        fade_n = max(1, int(sr * fade_ms / 1000))
        chunks: List[np.ndarray] = []
        eps = 1.0 / sr  # one sample
        for i, (a, b) in enumerate(regs):
            i0 = int(round(a * sr))
            i1 = int(round(b * sr))
            chunk = self.samples[i0:i1].astype(np.float32, copy=True)
            if len(chunk) == 0:
                continue
            n = len(chunk)
            # fade-in: only if this region starts inside the source (i.e. there's
            # a cut just before it). Otherwise we'd attenuate the natural opening.
            if a > eps and n >= 2:
                k = min(fade_n, n // 2)
                ramp = 0.5 - 0.5 * np.cos(np.pi * np.arange(k, dtype=np.float32) / k)
                if chunk.ndim == 1:
                    chunk[:k] *= ramp
                else:
                    chunk[:k] *= ramp[:, None]
            # fade-out: only if this region ends inside the source.
            if b < self.duration_s - eps and n >= 2:
                k = min(fade_n, n // 2)
                ramp = 0.5 - 0.5 * np.cos(np.pi * np.arange(k, dtype=np.float32) / k)
                ramp = ramp[::-1]
                if chunk.ndim == 1:
                    chunk[-k:] *= ramp
                else:
                    chunk[-k:] *= ramp[:, None]
            chunks.append(chunk)
        if not chunks:
            shape = (0,) if self.samples.ndim == 1 else (0, self.samples.shape[1])
            return np.zeros(shape, dtype=np.float32)
        return np.concatenate(chunks, axis=0)


# ─────────────────────────── I/O ───────────────────────────

def load_audio(path: str) -> Tuple[np.ndarray, int]:
    """Decode any audio file pydub can open into a float32 numpy array in [-1, 1]."""
    seg = AudioSegment.from_file(path)
    sr = seg.frame_rate
    arr = np.array(seg.get_array_of_samples())
    if seg.channels == 2:
        arr = arr.reshape((-1, 2))
    sample_max = float(1 << (8 * seg.sample_width - 1))
    arr = arr.astype(np.float32) / sample_max
    return arr, sr


def save_audio(samples: np.ndarray, sr: int, out_path: str, fmt: Optional[str] = None,
               mp3_bitrate: str = "192k"):
    """Save a float32 numpy array via pydub. fmt defaults to the file extension."""
    fmt = (fmt or Path(out_path).suffix.lstrip(".").lower() or "wav").lower()
    clipped = np.clip(samples, -1.0, 1.0)
    int16 = (clipped * 32767.0).astype(np.int16)
    channels = 1 if int16.ndim == 1 else int16.shape[1]
    if int16.ndim == 2:
        # pydub wants interleaved bytes
        int16 = int16.reshape(-1)
    seg = AudioSegment(
        int16.tobytes(),
        frame_rate=sr,
        sample_width=2,
        channels=channels,
    )
    kwargs = {}
    if fmt == "mp3":
        kwargs["bitrate"] = mp3_bitrate
    seg.export(out_path, format=fmt, **kwargs)


def compute_peaks(samples: np.ndarray, n_columns: int) -> np.ndarray:
    """Returns (n_columns, 2) of [min, max] per column for waveform display."""
    if n_columns <= 0:
        return np.zeros((1, 2), dtype=np.float32)
    if samples.ndim > 1:
        mono = samples.mean(axis=1)
    else:
        mono = samples
    n = len(mono)
    if n == 0:
        return np.zeros((n_columns, 2), dtype=np.float32)
    bin_size = max(1, n // n_columns)
    cols = min(n_columns, math.ceil(n / bin_size))
    peaks = np.zeros((n_columns, 2), dtype=np.float32)
    for i in range(cols):
        s = i * bin_size
        e = min(s + bin_size, n)
        chunk = mono[s:e]
        peaks[i] = (chunk.min(), chunk.max())
    return peaks


# ─────────────────────────── Player ───────────────────────────

class Player:
    """sounddevice playback that can skip over cut regions on the fly."""

    def __init__(self):
        self.stream: Optional[sd.OutputStream] = None
        self.samples: Optional[np.ndarray] = None
        self.sr: int = 0
        self.position: int = 0  # in samples
        self.skip_regions: List[Tuple[int, int]] = []  # in sample units, sorted
        self.skip_enabled: bool = False
        self.is_playing: bool = False
        self.on_position = None      # kept for API compat; no longer called from audio thread
        self.on_finished = None      # callback()
        self._pos_seconds: float = 0.0   # written by audio thread; read by UI poll
        self._lock = threading.Lock()

    def load(self, samples: np.ndarray, sr: int):
        self.stop()
        self.samples = samples
        self.sr = sr
        self.position = 0
        self._pos_seconds = 0.0

    def set_skip_regions_seconds(self, regions: List[Tuple[float, float]]):
        with self._lock:
            self.skip_regions = sorted(
                (int(round(a * self.sr)), int(round(b * self.sr))) for a, b in regions
            )

    def set_skip_enabled(self, enabled: bool):
        self.skip_enabled = enabled

    def seek(self, s: float):
        if self.samples is None:
            return
        self.position = max(0, min(len(self.samples) - 1, int(round(s * self.sr))))
        self._pos_seconds = self.position / self.sr if self.sr else 0.0

    def toggle(self, from_s: Optional[float] = None):
        if self.is_playing:
            self.pause()
        else:
            self.play(from_s)

    def play(self, from_s: Optional[float] = None):
        if self.samples is None or self.sr <= 0:
            return
        if from_s is not None:
            self.seek(from_s)
        if self.stream is not None:
            self._close_stream()
        channels = 1 if self.samples.ndim == 1 else self.samples.shape[1]
        try:
            self.stream = sd.OutputStream(
                samplerate=self.sr,
                channels=channels,
                callback=self._callback,
                dtype="float32",
                blocksize=4096,
                finished_callback=self._on_stream_finished,
            )
            self.stream.start()
            self.is_playing = True
        except Exception as e:
            print(f"[player] could not open output stream: {e}")
            self.is_playing = False

    def pause(self):
        self._close_stream()
        self.is_playing = False

    def stop(self):
        self.pause()
        self.position = 0
        self._pos_seconds = 0.0

    def _close_stream(self):
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None

    def _on_stream_finished(self):
        self.is_playing = False
        if self.on_finished:
            try:
                self.on_finished()
            except Exception:
                pass

    def _advance_past_skips(self, pos: int) -> int:
        if not self.skip_enabled:
            return pos
        for a, b in self.skip_regions:
            if a <= pos < b:
                pos = b
        return pos

    def _callback(self, outdata, frames, time_info, status):
        if status:
            # Underrun / overflow — non-fatal, just keep going.
            pass
        with self._lock:
            samples = self.samples
            if samples is None:
                outdata[:] = 0
                raise sd.CallbackStop
            n_total = len(samples)
            channels = outdata.shape[1]

            pos = self._advance_past_skips(self.position)

            if pos >= n_total:
                outdata[:] = 0
                self.position = n_total
                self._pos_seconds = self.position / self.sr if self.sr else 0.0
                raise sd.CallbackStop

            # How far can we copy before bumping into the next skip region?
            chunk_end = min(pos + frames, n_total)
            if self.skip_enabled:
                for a, b in self.skip_regions:
                    if pos < a < chunk_end:
                        chunk_end = a
                        break

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
            self._pos_seconds = self.position / self.sr if self.sr else 0.0


# ─────────────────────────── Waveform Canvas ───────────────────────────

class WaveformCanvas(tk.Canvas):
    """Custom waveform display with click-drag region painting + cut handles."""

    HANDLE_PX = 5

    def __init__(self, parent, theme, callbacks, **kwargs):
        super().__init__(parent, highlightthickness=0, bd=0, **kwargs)
        self.theme = theme
        self.callbacks = callbacks  # dict of named handlers
        self.peaks: np.ndarray = np.zeros((0, 2), dtype=np.float32)
        self.duration_s: float = 0.0
        self.cuts: List[Cut] = []
        self.cursor_s: float = 0.0
        self.view_start: float = 0.0
        self.view_end: float = 0.0
        self.preview_cut: Optional[Tuple[float, float]] = None
        self._drag_mode: Optional[str] = None
        self._drag_idx: int = -1
        self._drag_offset: float = 0.0
        self._drag_start_s: float = 0.0
        self._draw_pending = False

        self.bind("<Configure>", lambda _e: self._schedule_redraw())
        self.bind("<Button-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Motion>", self._on_hover)
        self.bind("<MouseWheel>", self._on_wheel)
        self.bind("<Double-Button-1>", self._on_double_click)

    # ---- public ----

    def set_audio(self, peaks: np.ndarray, duration_s: float):
        self.peaks = peaks
        self.duration_s = duration_s
        self.view_start = 0.0
        self.view_end = duration_s
        self._schedule_redraw()

    def clear_audio(self):
        self.peaks = np.zeros((0, 2), dtype=np.float32)
        self.duration_s = 0.0
        self.view_start = 0.0
        self.view_end = 0.0
        self.cuts = []
        self.cursor_s = 0.0
        self._schedule_redraw()

    def set_cuts(self, cuts: List[Cut]):
        self.cuts = cuts
        self._schedule_redraw()

    def set_cursor(self, s: float):
        self.cursor_s = max(0.0, min(self.duration_s, s))
        self._schedule_redraw()

    def move_cursor_only(self, s: float):
        """Move the playhead without repainting the full waveform."""
        self.cursor_s = max(0.0, min(self.duration_s, s))
        line = self.find_withtag("playhead_line")
        marker = self.find_withtag("playhead_marker")
        if not line or not marker:
            self._schedule_redraw()
            return
        h = self.winfo_height()
        cx = self.s_to_x(self.cursor_s)
        self.coords(line[0], cx, 0, cx, h)
        self.coords(marker[0], cx - 6, 0, cx + 6, 0, cx, 8)

    def set_theme(self, theme):
        self.theme = theme
        self.configure(bg=theme["panel"])
        self._schedule_redraw()

    def reset_zoom(self):
        self.view_start = 0.0
        self.view_end = self.duration_s
        self._schedule_redraw()

    def zoom(self, factor: float, anchor_x: Optional[int] = None):
        if self.duration_s <= 0:
            return
        if anchor_x is None:
            anchor_x = self.winfo_width() // 2
        anchor_s = self.x_to_s(anchor_x)
        view_w = self.view_end - self.view_start
        new_w = max(0.2, min(self.duration_s, view_w * factor))
        ratio = (anchor_s - self.view_start) / view_w if view_w > 0 else 0.5
        self.view_start = max(0.0, anchor_s - ratio * new_w)
        self.view_end = self.view_start + new_w
        if self.view_end > self.duration_s:
            self.view_end = self.duration_s
            self.view_start = max(0.0, self.view_end - new_w)
        self._schedule_redraw()

    # ---- coords ----

    def s_to_x(self, s: float) -> float:
        w = max(1, self.winfo_width())
        view_w = max(1e-9, self.view_end - self.view_start)
        return (s - self.view_start) / view_w * w

    def x_to_s(self, x: float) -> float:
        w = max(1, self.winfo_width())
        view_w = self.view_end - self.view_start
        return self.view_start + (x / w) * view_w

    # ---- redraw ----

    def _schedule_redraw(self):
        if self._draw_pending:
            return
        self._draw_pending = True
        self.after_idle(self._redraw)

    def _redraw(self):
        self._draw_pending = False
        self.delete("all")
        w = self.winfo_width()
        h = self.winfo_height()
        if w < 4 or h < 4:
            return
        t = self.theme
        self.configure(bg=t["panel"])

        # Panel rounded backdrop (faux — Canvas can't do real radii, so a flat panel)
        self.create_rectangle(0, 0, w, h, fill=t["panel"], outline=t["stroke"])

        # Center line
        mid = h / 2
        self.create_line(0, mid, w, mid, fill=t["grid"])

        # Time grid (every "nice" interval)
        view_w = self.view_end - self.view_start
        if view_w > 0:
            step = self._nice_step(view_w)
            first = math.floor(self.view_start / step) * step
            ticks = []
            v = first
            while v <= self.view_end + step:
                if v >= self.view_start:
                    x = self.s_to_x(v)
                    self.create_line(x, 0, x, h, fill=t["grid"])
                    ticks.append((x, v))
                v += step
            for x, v in ticks:
                self.create_text(
                    x + 4, h - 4, anchor="sw",
                    text=fmt_time(v), fill=t["muted"],
                    font=("Consolas", 8),
                )

        # Waveform — vertical bars per pixel column, gradient color top↔bot
        if len(self.peaks) > 0 and self.duration_s > 0:
            n = len(self.peaks)
            view_a = max(0, int((self.view_start / self.duration_s) * n))
            view_b = min(n, max(view_a + 1, int((self.view_end / self.duration_s) * n)))
            visible = self.peaks[view_a:view_b]
            vlen = len(visible)
            if vlen > 0:
                amp = (h * 0.42)
                step = vlen / float(w)
                for i in range(w):
                    idx = int(i * step)
                    if idx >= vlen:
                        break
                    lo, hi = visible[idx]
                    y_top = mid - hi * amp
                    y_bot = mid - lo * amp
                    if y_bot - y_top < 1:
                        y_top -= 0.5
                        y_bot += 0.5
                    # vertical gradient: top color near peak, mid color near center
                    rel = i / float(max(1, w - 1))
                    color = hex_lerp(t["wave_top"], t["wave_bot"], rel)
                    self.create_line(i, y_top, i, y_bot, fill=color)

        # Cuts
        for c in self.cuts:
            x1 = self.s_to_x(c.start_s)
            x2 = self.s_to_x(c.end_s)
            if x2 < 0 or x1 > w:
                continue
            self.create_rectangle(
                x1, 0, x2, h,
                fill=t["danger"], stipple="gray25",
                outline=t["danger"], width=1,
            )
            # Edge handles
            self.create_line(x1, 0, x1, h, fill=t["danger"], width=2)
            self.create_line(x2, 0, x2, h, fill=t["danger"], width=2)
            # Length label
            label = f"−{fmt_time(c.length())}"
            self.create_text(
                (x1 + x2) / 2, 12, text=label,
                fill="#FFFFFF", font=("Segoe UI", 9, "bold"),
            )

        # Live preview while painting a new cut
        if self.preview_cut:
            a, b = sorted(self.preview_cut)
            x1 = self.s_to_x(a)
            x2 = self.s_to_x(b)
            self.create_rectangle(
                x1, 0, x2, h,
                fill=t["accent3"], stipple="gray50",
                outline=t["accent3"], width=2,
            )

        # Cursor
        cx = self.s_to_x(self.cursor_s)
        self.create_line(cx, 0, cx, h, fill=t["cursor"], width=2, tags=("playhead", "playhead_line"))
        self.create_polygon(
            cx - 6, 0, cx + 6, 0, cx, 8,
            fill=t["cursor"], outline=t["cursor"], tags=("playhead", "playhead_marker"),
        )

    def _nice_step(self, view_w: float) -> float:
        # Aim for ~6-12 ticks across the visible area
        target = view_w / 8
        candidates = [0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600]
        for c in candidates:
            if c >= target:
                return c
        return candidates[-1]

    # ---- input ----

    def _hit_cut(self, x: float) -> Optional[Tuple[str, int, float]]:
        for i, c in enumerate(self.cuts):
            x1 = self.s_to_x(c.start_s)
            x2 = self.s_to_x(c.end_s)
            if abs(x - x1) <= self.HANDLE_PX:
                return ("edge_left", i, 0.0)
            if abs(x - x2) <= self.HANDLE_PX:
                return ("edge_right", i, 0.0)
            if x1 < x < x2:
                # offset from cut's start, in seconds
                return ("move", i, self.x_to_s(x) - c.start_s)
        return None

    def _on_press(self, event):
        if self.duration_s <= 0:
            return
        hit = self._hit_cut(event.x)
        if hit is not None:
            mode, idx, off = hit
            self._drag_mode = mode
            self._drag_idx = idx
            self._drag_offset = off
        else:
            self._drag_mode = "new"
            self._drag_start_s = self.x_to_s(event.x)
            self.preview_cut = (self._drag_start_s, self._drag_start_s)
            self._schedule_redraw()

    def _on_drag(self, event):
        if self.duration_s <= 0 or self._drag_mode is None:
            return
        s = max(0.0, min(self.duration_s, self.x_to_s(event.x)))
        if self._drag_mode == "new":
            self.preview_cut = (self._drag_start_s, s)
            self._schedule_redraw()
        else:
            cb = self.callbacks.get("modify_cut")
            if cb:
                cb(self._drag_mode, self._drag_idx, s, self._drag_offset, snapshot=False)

    def _on_release(self, event):
        if self.duration_s <= 0:
            self._drag_mode = None
            return
        if self._drag_mode == "new" and self.preview_cut:
            a, b = sorted(self.preview_cut)
            self.preview_cut = None
            if b - a < 0.020:
                # Treat as a click: seek
                cb = self.callbacks.get("seek")
                if cb:
                    cb(a)
            else:
                cb = self.callbacks.get("add_cut")
                if cb:
                    cb(a, b)
        elif self._drag_mode in ("edge_left", "edge_right", "move"):
            cb = self.callbacks.get("commit_cut_modify")
            if cb:
                cb()
        self._drag_mode = None
        self._drag_idx = -1
        self._schedule_redraw()

    def _on_hover(self, event):
        hit = self._hit_cut(event.x) if self._drag_mode is None else None
        if hit is None:
            self.configure(cursor="")
        else:
            mode = hit[0]
            self.configure(cursor="sb_h_double_arrow" if mode.startswith("edge") else "fleur")

    def _on_wheel(self, event):
        if self.duration_s <= 0:
            return
        delta = event.delta / 120.0
        factor = 0.85 if delta > 0 else 1.18
        self.zoom(factor, anchor_x=event.x)

    def _on_double_click(self, event):
        # Double click on a cut deletes it; double click empty area resets zoom.
        hit = self._hit_cut(event.x)
        if hit is None:
            self.reset_zoom()
        else:
            cb = self.callbacks.get("delete_cut")
            if cb:
                cb(hit[1])


# ─────────────────────────── Main app ───────────────────────────

class VoiceEditorApp:
    def __init__(self, root: ctk.CTk):
        self.root = root
        self.theme_name = "dark"
        self.theme = THEMES[self.theme_name]

        ctk.set_appearance_mode(self.theme["appearance"])
        ctk.set_default_color_theme("blue")

        self.root.title("Voice Editor")
        self.root.geometry("1180x740")
        self.root.minsize(960, 640)
        self.root.configure(fg_color=self.theme["bg"])

        self.project: Optional[Project] = None
        self.player = Player()
        self.player.on_finished = lambda: self.root.after(0, self._on_player_finished)
        self._playhead_after_id = None

        self.ffmpeg = find_ffmpeg()
        configure_pydub(self.ffmpeg)

        self.preview_cuts_enabled = ctk.BooleanVar(value=True)
        self.export_format = ctk.StringVar(value="WAV")
        self.export_bitrate = ctk.StringVar(value="192 kbps")
        self.is_exporting = False
        self.last_saved_path: Optional[Path] = None

        self._build_ui()
        self._refresh_cut_list()
        self._refresh_summary()
        self._refresh_actions()

        # Keyboard shortcuts
        self.root.bind("<space>", lambda _e: self._toggle_play())
        self.root.bind("<Control-z>", lambda _e: self._undo())
        self.root.bind("<Control-y>", lambda _e: self._redo())
        self.root.bind("<Control-Z>", lambda _e: self._undo())
        self.root.bind("<Control-Y>", lambda _e: self._redo())
        self.root.bind("<bracketleft>", lambda _e: self._mark_in())
        self.root.bind("<bracketright>", lambda _e: self._mark_out())
        self.root.bind("<Left>", lambda _e: self._nudge(-1.0))
        self.root.bind("<Right>", lambda _e: self._nudge(1.0))
        self.root.bind("<Shift-Left>", lambda _e: self._nudge(-10.0))
        self.root.bind("<Shift-Right>", lambda _e: self._nudge(10.0))
        self.root.bind("<Delete>", lambda _e: self._delete_selected_cut())

        self._mark_in_s: Optional[float] = None

    # ─────────────────────── UI build ───────────────────────

    def _build_ui(self):
        t = self.theme

        # Top bar
        topbar = ctk.CTkFrame(
            self.root, fg_color=t["panel"], corner_radius=14,
            border_width=1, border_color=t["stroke"],
        )
        topbar.pack(fill="x", padx=14, pady=(14, 8))

        title = ctk.CTkLabel(
            topbar, text="Voice Editor",
            font=ctk.CTkFont("Segoe UI", 18, "bold"),
            text_color=t["accent"],
        )
        title.pack(side="left", padx=(16, 8), pady=10)

        self.subtitle = ctk.CTkLabel(
            topbar, text="Trim spaces and unwanted sections out of any voice recording.",
            font=ctk.CTkFont("Segoe UI", 12),
            text_color=t["muted"],
        )
        self.subtitle.pack(side="left", padx=(0, 16), pady=10)

        self.theme_btn = ctk.CTkButton(
            topbar, text="☀  Light mode", width=128,
            command=self._toggle_theme,
            fg_color=t["panel2"], hover_color=t["stroke"],
            text_color=t["text"], corner_radius=10,
        )
        self.theme_btn.pack(side="right", padx=(8, 16), pady=10)

        self.load_btn = ctk.CTkButton(
            topbar, text="📂  Load audio…", width=160,
            command=self._open_audio,
            fg_color=t["accent"], hover_color=hex_lerp(t["accent"], "#000000", 0.2),
            text_color="#FFFFFF", corner_radius=10,
            font=ctk.CTkFont("Segoe UI", 12, "bold"),
        )
        self.load_btn.pack(side="right", padx=8, pady=10)

        # File info strip
        info_row = ctk.CTkFrame(self.root, fg_color="transparent")
        info_row.pack(fill="x", padx=14, pady=(0, 6))
        self.file_label = ctk.CTkLabel(
            info_row, text="No file loaded",
            text_color=t["text"], font=ctk.CTkFont("Segoe UI", 12, "bold"),
            anchor="w",
        )
        self.file_label.pack(side="left", padx=4)
        self.time_label = ctk.CTkLabel(
            info_row, text="00:00.000 / 00:00.000",
            text_color=t["muted"], font=ctk.CTkFont("Consolas", 12),
        )
        self.time_label.pack(side="right", padx=4)

        # Waveform area
        wave_frame = ctk.CTkFrame(
            self.root, fg_color=t["panel"], corner_radius=14,
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

        # Transport row
        transport = ctk.CTkFrame(self.root, fg_color="transparent")
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

        self.preview_switch = ctk.CTkSwitch(
            transport, text="Preview with cuts skipped",
            variable=self.preview_cuts_enabled,
            command=self._on_preview_toggled,
            text_color=t["text"], button_color=t["accent"], progress_color=t["accent"],
        )
        self.preview_switch.pack(side="left", padx=(16, 8))

        ctk.CTkLabel(transport, text="Zoom:", text_color=t["muted"]).pack(side="left", padx=(16, 4))
        ctk.CTkButton(
            transport, text="−", width=38, command=lambda: self.canvas.zoom(1.4),
            fg_color=t["panel2"], hover_color=t["stroke"], text_color=t["text"],
            corner_radius=10,
        ).pack(side="left", padx=2)
        ctk.CTkButton(
            transport, text="+", width=38, command=lambda: self.canvas.zoom(0.7),
            fg_color=t["panel2"], hover_color=t["stroke"], text_color=t["text"],
            corner_radius=10,
        ).pack(side="left", padx=2)
        ctk.CTkButton(
            transport, text="Fit", width=44, command=self.canvas.reset_zoom,
            fg_color=t["panel2"], hover_color=t["stroke"], text_color=t["text"],
            corner_radius=10,
        ).pack(side="left", padx=2)

        ctk.CTkButton(
            transport, text="↶  Undo", width=80, command=self._undo,
            fg_color=t["panel2"], hover_color=t["stroke"], text_color=t["text"],
            corner_radius=10,
        ).pack(side="right", padx=2)
        ctk.CTkButton(
            transport, text="↷  Redo", width=80, command=self._redo,
            fg_color=t["panel2"], hover_color=t["stroke"], text_color=t["text"],
            corner_radius=10,
        ).pack(side="right", padx=2)
        ctk.CTkButton(
            transport, text="[ Mark in", width=92, command=self._mark_in,
            fg_color=t["panel2"], hover_color=t["stroke"], text_color=t["text"],
            corner_radius=10,
        ).pack(side="right", padx=2)
        ctk.CTkButton(
            transport, text="] Mark out", width=98, command=self._mark_out,
            fg_color=t["panel2"], hover_color=t["stroke"], text_color=t["text"],
            corner_radius=10,
        ).pack(side="right", padx=2)

        # Bottom row: cuts list (left) + summary/export (right)
        bottom = ctk.CTkFrame(self.root, fg_color="transparent")
        bottom.pack(fill="both", padx=14, pady=(0, 14))

        # ----- Cuts list -----
        cuts_frame = ctk.CTkFrame(
            bottom, fg_color=t["panel"], corner_radius=14,
            border_width=1, border_color=t["stroke"],
        )
        cuts_frame.pack(side="left", fill="both", expand=True, padx=(0, 8))

        cut_header = ctk.CTkFrame(cuts_frame, fg_color="transparent")
        cut_header.pack(fill="x", padx=12, pady=(10, 4))
        ctk.CTkLabel(
            cut_header, text="Cuts",
            font=ctk.CTkFont("Segoe UI", 13, "bold"),
            text_color=t["accent3"],
        ).pack(side="left")
        ctk.CTkButton(
            cut_header, text="Clear all", width=88, command=self._clear_all_cuts,
            fg_color=t["panel2"], hover_color=t["danger"], text_color=t["text"],
            corner_radius=10,
        ).pack(side="right")

        self.cut_scroll = ctk.CTkScrollableFrame(
            cuts_frame, fg_color=t["panel2"], height=200,
        )
        self.cut_scroll.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        # ----- Summary + Export -----
        side = ctk.CTkFrame(
            bottom, fg_color=t["panel"], corner_radius=14,
            border_width=1, border_color=t["stroke"],
        )
        side.pack(side="right", fill="y")

        ctk.CTkLabel(
            side, text="Result preview",
            font=ctk.CTkFont("Segoe UI", 13, "bold"),
            text_color=t["accent2"],
        ).pack(anchor="w", padx=14, pady=(12, 6))

        self.summary_orig = ctk.CTkLabel(
            side, text="Original:  00:00.000",
            text_color=t["text"], font=ctk.CTkFont("Consolas", 12),
            anchor="w",
        )
        self.summary_orig.pack(fill="x", padx=14)
        self.summary_cut = ctk.CTkLabel(
            side, text="Cuts:      00:00.000",
            text_color=t["danger"], font=ctk.CTkFont("Consolas", 12),
            anchor="w",
        )
        self.summary_cut.pack(fill="x", padx=14)
        self.summary_result = ctk.CTkLabel(
            side, text="Result:    00:00.000",
            text_color=t["ok"], font=ctk.CTkFont("Consolas", 12, "bold"),
            anchor="w",
        )
        self.summary_result.pack(fill="x", padx=14, pady=(0, 8))

        self.export_btn = ctk.CTkButton(
            side, text="Save trimmed audio", width=220, command=self._export,
            fg_color=t["accent"], hover_color=hex_lerp(t["accent"], "#000000", 0.2),
            text_color="#FFFFFF", corner_radius=12,
            font=ctk.CTkFont("Segoe UI", 13, "bold"),
        )
        self.export_btn.pack(fill="x", padx=12, pady=(0, 8))

        self.saved_path_label = ctk.CTkLabel(
            side, text="Saved file: none yet",
            text_color=t["muted"], font=ctk.CTkFont("Segoe UI", 10),
            anchor="w", justify="left", wraplength=220,
        )
        self.saved_path_label.pack(fill="x", padx=14, pady=(0, 8))

        # Export options
        exp_box = ctk.CTkFrame(side, fg_color=t["panel2"], corner_radius=10)
        exp_box.pack(fill="x", padx=12, pady=(4, 8))

        ctk.CTkLabel(
            exp_box, text="Export format",
            text_color=t["muted"], font=ctk.CTkFont("Segoe UI", 11),
        ).pack(anchor="w", padx=12, pady=(8, 0))
        fmt_row = ctk.CTkFrame(exp_box, fg_color="transparent")
        fmt_row.pack(fill="x", padx=8, pady=(2, 6))
        ctk.CTkRadioButton(
            fmt_row, text="WAV (lossless)", variable=self.export_format, value="WAV",
            text_color=t["text"], fg_color=t["accent"], hover_color=t["accent"],
            command=self._refresh_actions,
        ).pack(anchor="w", padx=4, pady=2)
        ctk.CTkRadioButton(
            fmt_row, text="MP3 (smaller)", variable=self.export_format, value="MP3",
            text_color=t["text"], fg_color=t["accent"], hover_color=t["accent"],
            command=self._refresh_actions,
        ).pack(anchor="w", padx=4, pady=2)

        self.bitrate_label = ctk.CTkLabel(
            exp_box, text="MP3 bitrate",
            text_color=t["muted"], font=ctk.CTkFont("Segoe UI", 11),
        )
        self.bitrate_label.pack(anchor="w", padx=12, pady=(4, 0))
        self.bitrate_menu = ctk.CTkOptionMenu(
            exp_box,
            variable=self.export_bitrate,
            values=["128 kbps", "192 kbps", "256 kbps", "320 kbps"],
            fg_color=t["panel"], button_color=t["accent"], button_hover_color=t["accent2"],
            text_color=t["text"], dropdown_fg_color=t["panel"], dropdown_text_color=t["text"],
        )
        self.bitrate_menu.pack(fill="x", padx=12, pady=(2, 10))

        self.progress = ctk.CTkProgressBar(
            side, mode="determinate",
            progress_color=t["accent2"], fg_color=t["panel2"],
        )
        self.progress.set(0)
        self.progress.pack(fill="x", padx=12, pady=(0, 6))

        self.status_label = ctk.CTkLabel(
            side, text="Idle", text_color=t["muted"],
            font=ctk.CTkFont("Segoe UI", 11), anchor="w",
        )
        self.status_label.pack(fill="x", padx=14, pady=(0, 12))

        # Hint footer
        hint = ctk.CTkLabel(
            self.root,
            text=("Drag on the waveform to mark a cut.  "
                  "Drag a cut's edge to resize, body to move.  Double-click a cut to delete.  "
                  "Space = play/pause   [ ] = mark in/out   Ctrl+Z/Y = undo/redo"),
            text_color=t["muted"],
            font=ctk.CTkFont("Segoe UI", 10),
        )
        hint.pack(pady=(0, 10))

    # ─────────────────────── Theme toggle ───────────────────────

    def _toggle_theme(self):
        self.theme_name = "light" if self.theme_name == "dark" else "dark"
        self.theme = THEMES[self.theme_name]
        ctk.set_appearance_mode(self.theme["appearance"])
        # We can't restyle every ctk widget without re-creating the UI tree.
        # The simplest reliable approach is to re-build it.
        for child in self.root.winfo_children():
            child.destroy()
        self._build_ui()
        # Re-attach data
        if self.project:
            peaks = compute_peaks(self.project.samples, n_columns=4000)
            self.canvas.set_audio(peaks, self.project.duration_s)
            self.canvas.set_cuts(self.project.cuts)
            self.canvas.set_cursor(self.player.position / self.player.sr if self.player.sr else 0.0)
            self.file_label.configure(text=Path(self.project.source_path).name)
        self._refresh_cut_list()
        self._refresh_summary()
        self._refresh_actions()
        self.theme_btn.configure(
            text=("☀  Light mode" if self.theme_name == "dark" else "🌙  Dark mode")
        )

    # ─────────────────────── Loading ───────────────────────

    def _open_audio(self):
        path = filedialog.askopenfilename(
            title="Select audio file",
            initialdir=str(HERE),
            filetypes=[
                ("Audio files", "*.mp3 *.wav *.m4a *.flac *.aac *.ogg *.opus"),
                ("All files", "*.*"),
            ],
        )
        if not path:
            return
        self._load_path(path)

    def _load_path(self, path: str):
        try:
            samples, sr = load_audio(path)
        except Exception as e:
            messagebox.showerror("Load failed", f"Could not decode audio:\n{e}")
            return
        self.player.stop()
        self.project = Project(source_path=Path(path), samples=samples, sample_rate=sr)
        peaks = compute_peaks(samples, n_columns=4000)
        self.canvas.set_audio(peaks, self.project.duration_s)
        self.canvas.set_cuts(self.project.cuts)
        self.canvas.set_cursor(0.0)
        self.player.load(samples, sr)
        self.file_label.configure(
            text=f"{Path(path).name}  ·  {self.project.channels}ch  ·  {sr} Hz"
        )
        self._refresh_cut_list()
        self._refresh_summary()
        self._refresh_actions()
        self._update_time_label(0.0)
        self._mark_in_s = None
        self._sync_player_skips()

    # ─────────────────────── Playback ───────────────────────

    def _toggle_play(self):
        if not self.project:
            return
        if self.player.is_playing:
            self.player.pause()
            self._cancel_playhead_poll()
            self.play_btn.configure(text="▶  Play")
        else:
            from_s = None
            if self.player.position >= len(self.player.samples):
                from_s = 0.0
            self._sync_player_skips()
            self.player.play(from_s)
            if self.player.is_playing:
                self._start_playhead_poll()
                self.play_btn.configure(text="⏸  Pause")

    def _stop_play(self):
        self.player.stop()
        self._cancel_playhead_poll()
        self.play_btn.configure(text="▶  Play")
        self.canvas.set_cursor(0.0)
        self._update_time_label(0.0)

    def _start_playhead_poll(self):
        self._cancel_playhead_poll()
        self._poll_playhead()

    def _cancel_playhead_poll(self):
        if self._playhead_after_id is not None:
            try:
                self.root.after_cancel(self._playhead_after_id)
            except Exception:
                pass
            self._playhead_after_id = None

    def _poll_playhead(self):
        if not self.project:
            self._playhead_after_id = None
            return
        s = self.player._pos_seconds
        self.canvas.move_cursor_only(s)
        self._update_time_label(s)
        if self.player.is_playing:
            self._playhead_after_id = self.root.after(40, self._poll_playhead)
        else:
            self._playhead_after_id = None

    def _on_player_finished(self):
        self._cancel_playhead_poll()
        self.play_btn.configure(text="▶  Play")
        self.player.position = 0
        self.player._pos_seconds = 0.0
        self.canvas.set_cursor(0.0)
        self._update_time_label(0.0)

    def _on_seek(self, s: float):
        self.player.seek(s)
        self.canvas.set_cursor(s)
        self._update_time_label(s)

    def _nudge(self, ds: float):
        if not self.project:
            return
        cur = self.player.position / self.player.sr if self.player.sr else 0.0
        self._on_seek(max(0.0, min(self.project.duration_s, cur + ds)))

    def _on_preview_toggled(self):
        self._sync_player_skips()

    def _sync_player_skips(self):
        if not self.project:
            self.player.set_skip_regions_seconds([])
            self.player.set_skip_enabled(False)
            return
        self.player.set_skip_regions_seconds([(c.start_s, c.end_s) for c in self.project.cuts])
        self.player.set_skip_enabled(bool(self.preview_cuts_enabled.get()))

    # ─────────────────────── Cut handlers ───────────────────────

    def _on_add_cut(self, a: float, b: float):
        if not self.project:
            return
        if self.project.add_cut(a, b):
            self.canvas.set_cuts(self.project.cuts)
            self._refresh_cut_list()
            self._refresh_summary()
            self._sync_player_skips()

    def _on_modify_cut_live(self, mode: str, idx: int, s: float, offset: float, snapshot: bool):
        if not self.project or not (0 <= idx < len(self.project.cuts)):
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
        # Live: no snapshot, no normalize (overlap during drag is OK)
        self.canvas.set_cuts(self.project.cuts)
        self._refresh_cut_list()
        self._refresh_summary()
        self._sync_player_skips()

    def _on_commit_modify(self):
        if not self.project:
            return
        self.project._normalize()
        self.project._snapshot()
        self.canvas.set_cuts(self.project.cuts)
        self._refresh_cut_list()
        self._refresh_summary()
        self._sync_player_skips()

    def _on_delete_cut_idx(self, idx: int):
        if not self.project:
            return
        self.project.remove_cut(idx)
        self.canvas.set_cuts(self.project.cuts)
        self._refresh_cut_list()
        self._refresh_summary()
        self._sync_player_skips()

    def _delete_selected_cut(self):
        if self.project and self.project.cuts:
            self._on_delete_cut_idx(len(self.project.cuts) - 1)

    def _clear_all_cuts(self):
        if not self.project or not self.project.cuts:
            return
        self.project.clear_cuts()
        self.canvas.set_cuts(self.project.cuts)
        self._refresh_cut_list()
        self._refresh_summary()
        self._sync_player_skips()

    def _undo(self):
        if not self.project:
            return
        self.project.undo()
        self.canvas.set_cuts(self.project.cuts)
        self._refresh_cut_list()
        self._refresh_summary()
        self._sync_player_skips()

    def _redo(self):
        if not self.project:
            return
        self.project.redo()
        self.canvas.set_cuts(self.project.cuts)
        self._refresh_cut_list()
        self._refresh_summary()
        self._sync_player_skips()

    def _mark_in(self):
        if not self.project:
            return
        cur = self.player.position / self.player.sr if self.player.sr else 0.0
        self._mark_in_s = cur
        self.status_label.configure(text=f"Mark in: {fmt_time(cur)} — press ] to set out")

    def _mark_out(self):
        if not self.project or self._mark_in_s is None:
            return
        cur = self.player.position / self.player.sr if self.player.sr else 0.0
        self._on_add_cut(self._mark_in_s, cur)
        self._mark_in_s = None
        self.status_label.configure(text="Cut added")

    # ─────────────────────── List / summary refresh ───────────────────────

    def _refresh_cut_list(self):
        for child in self.cut_scroll.winfo_children():
            child.destroy()
        if not self.project or not self.project.cuts:
            ctk.CTkLabel(
                self.cut_scroll, text="No cuts yet — drag on the waveform to mark one.",
                text_color=self.theme["muted"], font=ctk.CTkFont("Segoe UI", 11),
            ).pack(pady=12)
            return
        for i, c in enumerate(self.project.cuts):
            row = ctk.CTkFrame(
                self.cut_scroll, fg_color=self.theme["panel"], corner_radius=8,
                border_width=1, border_color=self.theme["stroke"],
            )
            row.pack(fill="x", padx=4, pady=3)
            ctk.CTkLabel(
                row, text=f"{i + 1:>2}.", width=28,
                text_color=self.theme["muted"], font=ctk.CTkFont("Consolas", 11),
            ).pack(side="left", padx=(8, 4), pady=4)
            ctk.CTkLabel(
                row,
                text=f"{fmt_time(c.start_s)}  →  {fmt_time(c.end_s)}",
                text_color=self.theme["text"], font=ctk.CTkFont("Consolas", 11),
            ).pack(side="left", padx=4, pady=4)
            ctk.CTkLabel(
                row, text=f"({fmt_time(c.length())})",
                text_color=self.theme["danger"], font=ctk.CTkFont("Consolas", 11, "bold"),
            ).pack(side="left", padx=8, pady=4)
            ctk.CTkButton(
                row, text="↦ Go", width=44,
                command=lambda s=c.start_s: self._on_seek(s),
                fg_color=self.theme["panel2"], hover_color=self.theme["stroke"],
                text_color=self.theme["text"], corner_radius=8,
            ).pack(side="right", padx=4, pady=2)
            ctk.CTkButton(
                row, text="✕", width=32,
                command=lambda idx=i: self._on_delete_cut_idx(idx),
                fg_color=self.theme["panel2"], hover_color=self.theme["danger"],
                text_color=self.theme["text"], corner_radius=8,
            ).pack(side="right", padx=(2, 8), pady=2)

    def _refresh_summary(self):
        if not self.project:
            self.summary_orig.configure(text="Original:  00:00.000")
            self.summary_cut.configure(text="Cuts:      00:00.000")
            self.summary_result.configure(text="Result:    00:00.000")
            return
        orig = self.project.duration_s
        cut = self.project.total_cut_s()
        result = max(0.0, orig - cut)
        self.summary_orig.configure(text=f"Original:  {fmt_time(orig)}")
        self.summary_cut.configure(text=f"Cuts:      {fmt_time(cut)}  ({len(self.project.cuts)})")
        self.summary_result.configure(text=f"Result:    {fmt_time(result)}")

    def _refresh_actions(self):
        has_proj = self.project is not None
        result_s = (self.project.duration_s - self.project.total_cut_s()) if has_proj else 0.0
        can_export = has_proj and not self.is_exporting and result_s > 0.020
        self.export_btn.configure(state=("normal" if can_export else "disabled"))
        # Bitrate only matters for MP3
        if self.export_format.get() == "MP3":
            self.bitrate_label.configure(text_color=self.theme["text"])
            self.bitrate_menu.configure(state="normal")
        else:
            self.bitrate_label.configure(text_color=self.theme["muted"])
            self.bitrate_menu.configure(state="disabled")

    def _update_time_label(self, cur_s: float):
        if not self.project:
            self.time_label.configure(text="00:00.000 / 00:00.000")
            return
        self.time_label.configure(text=f"{fmt_time(cur_s)} / {fmt_time(self.project.duration_s)}")

    # ─────────────────────── Export ───────────────────────

    def _export(self):
        if not self.project or self.is_exporting:
            return
        fmt = self.export_format.get().lower()
        ext = fmt
        default_name = self.project.source_path.stem + f"_trimmed_{datetime.now():%Y%m%d_%H%M%S}.{ext}"
        out = filedialog.asksaveasfilename(
            title="Save trimmed audio as…",
            initialdir=str(OUTPUT_DIR if OUTPUT_DIR.is_dir() else self.project.source_path.parent),
            initialfile=default_name,
            defaultextension=f".{ext}",
            filetypes=[
                ("WAV (lossless)", "*.wav"),
                ("MP3", "*.mp3"),
                ("All files", "*.*"),
            ] if fmt == "wav" else [
                ("MP3", "*.mp3"),
                ("WAV (lossless)", "*.wav"),
                ("All files", "*.*"),
            ],
        )
        if not out:
            return
        bitrate = self.export_bitrate.get().split()[0] + "k"
        self.is_exporting = True
        self._refresh_actions()
        self.status_label.configure(text="Rendering…")
        self.progress.set(0.05)

        threading.Thread(
            target=self._export_thread, args=(out, fmt, bitrate), daemon=True,
        ).start()

    def _export_thread(self, out_path: str, fmt: str, bitrate: str):
        try:
            self._set_progress(0.1, "Applying cuts…")
            rendered = self.project.render(fade_ms=5)
            self._set_progress(0.55, f"Writing {fmt.upper()}…")
            save_audio(rendered, self.project.sample_rate, out_path, fmt=fmt, mp3_bitrate=bitrate)
            self._set_progress(1.0, "Done")
            self.root.after(0, lambda: self._export_done(True, out_path))
        except Exception as e:
            traceback.print_exc()
            self.root.after(0, lambda err=e: self._export_done(False, out_path, err))

    def _set_progress(self, frac: float, status: str):
        self.root.after(0, lambda: (
            self.progress.set(frac),
            self.status_label.configure(text=status),
        ))

    def _export_done(self, ok: bool, out_path: str, err: Optional[Exception] = None):
        self.is_exporting = False
        self._refresh_actions()
        if ok:
            saved_path = Path(out_path)
            size = saved_path.stat().st_size / (1024 * 1024)
            self.last_saved_path = saved_path
            self.saved_path_label.configure(
                text=f"Saved file: {saved_path}",
                text_color=self.theme["ok"],
            )
            self._load_path(str(saved_path))
            self.status_label.configure(
                text=f"Saved and loaded -> {saved_path.name} ({size:.2f} MB)"
            )
            ans = messagebox.askyesno(
                "Saved trimmed audio",
                f"Saved to:\n{out_path}\n\nThe saved audio is now loaded on screen.\nOpen the folder now?",
            )
            if ans:
                self._open_folder(saved_path.parent)
        else:
            self.status_label.configure(text=f"Failed: {err}")
            messagebox.showerror("Export failed", f"Could not write file:\n{err}")

    def _open_folder(self, folder: Path):
        try:
            if sys.platform == "win32":
                os.startfile(str(folder))
            elif sys.platform == "darwin":
                import subprocess
                subprocess.Popen(["open", str(folder)])
            else:
                import subprocess
                subprocess.Popen(["xdg-open", str(folder)])
        except Exception:
            pass


# ─────────────────────────── Entry point ───────────────────────────

def main():
    _safe_console()
    root = ctk.CTk()
    app = VoiceEditorApp(root)
    root.lift()
    try:
        root.attributes("-topmost", True)
        root.after(500, lambda: root.attributes("-topmost", False))
        root.focus_force()
    except Exception:
        pass
    root.mainloop()


if __name__ == "__main__":
    main()
