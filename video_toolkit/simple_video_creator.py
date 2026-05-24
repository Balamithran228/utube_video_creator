#!/usr/bin/env python3
"""
Simple Video Creator
- Pick images folder + voice file (or use the workspace defaults)
- Silences of 2 seconds or more are removed (configurable via MIN_SILENCE_MS)
- Photo N is shown for the duration of segment N (photo extends to match)
- Output: 1920x1080 16:9 H.264, saved to workspace
"""

import os
import sys
import shutil
import subprocess
import tempfile
import threading
import queue
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox

import customtkinter as ctk
try:
    from .paths import DEFAULT_IMAGES, PROJECT_ROOT, ensure_output_dir, first_existing_audio
except ImportError:
    from paths import DEFAULT_IMAGES, PROJECT_ROOT, ensure_output_dir, first_existing_audio

try:
    from pydub import AudioSegment
    from pydub.silence import detect_nonsilent
except ImportError:
    print("Installing pydub...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "pydub"])
    from pydub import AudioSegment
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
DEFAULT_AUDIO = first_existing_audio()

VIDEO_W = 1920
VIDEO_H = 1080
FPS = 30
MIN_SILENCE_MS = 2000
SILENCE_THRESH_DB = -40
KEEP_PADDING_MS = 80
IMG_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".webp"}

# Keyword-segmentation mode (the user marks segment boundaries by saying these words).
KEYWORD_START_FORMS = ("start", "started", "starting", "starts")
KEYWORD_SPLIT_FORMS = ("continue", "continued", "continues", "continuing")
KEYWORD_DEDUP_WINDOW_S = 2.0  # collapse repeated keyword hits within this window (Whisper hallucinations)
KEYWORD_MIN_DURATION_S = 0.05  # drop zero-duration tokens (also hallucinations)
WHISPER_MODEL = "base.en"
INTRA_SEGMENT_MAX_SILENCE_MS = 700  # silences longer than this are removed within a segment
INTRA_SEGMENT_PAD_MS = 60  # small padding around kept non-silent ranges

# Silero VAD mode — splits audio at every speech pause longer than VAD_MIN_PAUSE_MS.
VAD_MIN_PAUSE_MS = 1500       # gap between speech blocks that counts as a segment boundary
VAD_MIN_SPEECH_MS = 250        # ignore speech blocks shorter than this (noise / single phonemes)
VAD_THRESHOLD = 0.5            # confidence threshold for "this is speech"
VAD_PAD_MS = 200               # extra context kept at the start/end of each detected segment


def find_ffmpeg():
    exe = shutil.which("ffmpeg")
    if exe:
        return exe

    # On Windows, refresh PATH from the registry so we pick up entries added by installers
    # (winget/choco) without needing a shell restart.
    if sys.platform == "win32":
        try:
            import winreg
            paths = []
            for hive, sub in [
                (winreg.HKEY_CURRENT_USER, r"Environment"),
                (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
            ]:
                try:
                    with winreg.OpenKey(hive, sub) as k:
                        val, _ = winreg.QueryValueEx(k, "Path")
                        paths.append(os.path.expandvars(val))
                except OSError:
                    pass
            merged = os.pathsep.join([os.environ.get("PATH", "")] + paths)
            exe = shutil.which("ffmpeg", path=merged)
            if exe:
                return exe
        except Exception:
            pass

    # Look in common install locations as a last resort.
    static_candidates = [
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files (x86)\ffmpeg\bin\ffmpeg.exe",
        os.path.expanduser(r"~\scoop\shims\ffmpeg.exe"),
        os.path.expanduser(r"~\scoop\apps\ffmpeg\current\bin\ffmpeg.exe"),
        os.path.expanduser(r"~\AppData\Local\Microsoft\WinGet\Links\ffmpeg.exe"),
    ]
    for c in static_candidates:
        if os.path.isfile(c):
            return c

    # Glob any version under the WinGet Gyan.FFmpeg package folder.
    winget_root = os.path.expanduser(
        r"~\AppData\Local\Microsoft\WinGet\Packages"
    )
    if os.path.isdir(winget_root):
        for entry in os.listdir(winget_root):
            if entry.lower().startswith("gyan.ffmpeg"):
                pkg_dir = os.path.join(winget_root, entry)
                for sub in os.listdir(pkg_dir):
                    candidate = os.path.join(pkg_dir, sub, "bin", "ffmpeg.exe")
                    if os.path.isfile(candidate):
                        return candidate
    return None


def _normalize_word(w):
    return w.strip().lower().strip('.,!?;:"\'-—…()[]')


def transcribe_with_words(audio_path, model_size=WHISPER_MODEL, log=print):
    """Run Whisper on the audio, return a flat list of (word, start_s, end_s)."""
    from faster_whisper import WhisperModel
    log(f"Loading Whisper model '{model_size}' (first run downloads ~150 MB)…")
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    log("Transcribing audio (this can take a minute)…")
    segments_iter, info = model.transcribe(
        audio_path,
        word_timestamps=True,
        language="en",
        vad_filter=False,
    )
    words = []
    for seg in segments_iter:
        if seg.words:
            for w in seg.words:
                words.append((_normalize_word(w.word), float(w.start), float(w.end)))
    log(f"Transcribed {len(words)} words ({info.duration:.1f}s of audio)")
    return words


def find_keyword_segments(words, audio_len_ms, log=print,
                          start_kw=KEYWORD_START_FORMS[0], split_kw=KEYWORD_SPLIT_FORMS[0]):
    """Convert a transcribed word list into segment time ranges (ms).

    Rules:
      • The first occurrence of `start_kw` marks the beginning of segment 1.
        If no 'start' is found, segment 1 begins at audio start.
      • Every occurrence of `split_kw` ends the current segment and begins the next.
      • Marker words themselves are excluded from the segment audio.
      • The final segment runs until end of audio.
    """
    start_pos_s = None
    splits = []  # list of (split_word_start_s, split_word_end_s)
    for w_text, w_start, w_end in words:
        if w_text == start_kw and start_pos_s is None:
            start_pos_s = w_end
            log(f"  '{start_kw}' marker found at {w_start:.2f}s")
        elif w_text == split_kw:
            splits.append((w_start, w_end))

    if start_pos_s is None:
        log(f"  no '{start_kw}' word — beginning at 0s")
        start_pos_s = 0.0

    log(f"  {len(splits)} '{split_kw}' markers found")

    audio_len_s = audio_len_ms / 1000.0
    segments_s = []
    if not splits:
        segments_s.append((start_pos_s, audio_len_s))
    else:
        segments_s.append((start_pos_s, splits[0][0]))
        for i in range(len(splits) - 1):
            segments_s.append((splits[i][1], splits[i + 1][0]))
        # Trailing segment after the last marker (if there's actual content left)
        if splits[-1][1] < audio_len_s - 0.2:
            segments_s.append((splits[-1][1], audio_len_s))

    # Drop empty / near-empty segments (<0.3s) so we don't end up with silent stubs
    segments_ms = []
    for s, e in segments_s:
        s_ms, e_ms = int(round(s * 1000)), int(round(e * 1000))
        if e_ms - s_ms >= 300:
            segments_ms.append((s_ms, e_ms))
    return segments_ms


def compress_internal_silences(seg_audio,
                               max_silence_ms=INTRA_SEGMENT_MAX_SILENCE_MS,
                               silence_thresh_db=SILENCE_THRESH_DB,
                               pad_ms=INTRA_SEGMENT_PAD_MS):
    """Remove silences longer than `max_silence_ms` inside a segment, keeping
    short natural pauses intact. Returns a (possibly shorter) AudioSegment."""
    if len(seg_audio) == 0:
        return seg_audio
    nonsilent = detect_nonsilent(
        seg_audio,
        min_silence_len=max_silence_ms,
        silence_thresh=silence_thresh_db,
    )
    if not nonsilent:
        return seg_audio  # entirely quiet — keep as-is
    out = AudioSegment.empty()
    for s, e in nonsilent:
        s = max(0, s - pad_ms)
        e = min(len(seg_audio), e + pad_ms)
        out += seg_audio[s:e]
    return out


def configure_pydub(ffmpeg_path):
    """Point pydub at our discovered ffmpeg/ffprobe so MP3 loading works
    even when they're not on PATH yet (e.g. fresh winget install)."""
    if not ffmpeg_path:
        return
    AudioSegment.converter = ffmpeg_path
    bin_dir = os.path.dirname(ffmpeg_path)
    ffprobe = os.path.join(bin_dir, "ffprobe.exe" if sys.platform == "win32" else "ffprobe")
    if os.path.isfile(ffprobe):
        AudioSegment.ffprobe = ffprobe
        # Also expose to subprocesses so libraries that look up via shutil.which find them.
        os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")


class VideoCreator:
    def __init__(self, root):
        self.root = root
        self.root.title("Simple Video Creator")
        self.root.geometry("780x660")
        self.root.minsize(720, 580)

        self.T = THEMES["dark"]
        self.root.configure(fg_color=self.T["bg"])

        self.images_dir = tk.StringVar()
        self.audio_file = tk.StringVar()

        self.is_running = False
        self.stop_requested = False
        self.log_queue = queue.Queue()
        self.ffmpeg = find_ffmpeg()
        configure_pydub(self.ffmpeg)

        self._build_ui()
        self._poll_log()

        if self.ffmpeg:
            self._log("INFO", f"FFmpeg found: {self.ffmpeg}")
        else:
            self._log("WARNING", "FFmpeg not found on PATH. Install it before clicking Convert.")
            self._log("WARNING", "Download: https://www.gyan.dev/ffmpeg/builds/  (add bin folder to PATH)")

    def _build_ui(self):
        T = self.T
        outer = ctk.CTkFrame(self.root, fg_color=T["bg"])
        outer.pack(fill="both", expand=True, padx=16, pady=16)

        # === Source selection ===
        sec1 = ctk.CTkFrame(outer, fg_color=T["panel"], corner_radius=12,
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
                      fg_color=T["panel2"], border_color=T["stroke"],
                      text_color=T["text"], placeholder_text_color=T["muted"],
                      ).grid(row=0, column=1, sticky="ew", padx=(0, 8), pady=4)
        ctk.CTkButton(src, text="Browse…", command=self._browse_images, width=90,
                       fg_color=T["panel2"], hover_color=T["stroke"],
                       text_color=T["text"], border_width=1, border_color=T["stroke"],
                       ).grid(row=0, column=2, pady=4)

        ctk.CTkLabel(src, text="Voice file:", text_color=T["text"],
                      font=ctk.CTkFont("Segoe UI", 11)).grid(row=1, column=0, sticky="w", padx=(0, 8), pady=4)
        ctk.CTkEntry(src, textvariable=self.audio_file, placeholder_text="Pick a voice recording…",
                      fg_color=T["panel2"], border_color=T["stroke"],
                      text_color=T["text"], placeholder_text_color=T["muted"],
                      ).grid(row=1, column=1, sticky="ew", padx=(0, 8), pady=4)
        ctk.CTkButton(src, text="Browse…", command=self._browse_audio, width=90,
                       fg_color=T["panel2"], hover_color=T["stroke"],
                       text_color=T["text"], border_width=1, border_color=T["stroke"],
                       ).grid(row=1, column=2, pady=4)

        ctk.CTkButton(
            src, text="✨  Use sample defaults (sample_assets/images + sample_assets/audio)",
            command=self._use_defaults,
            fg_color=T["panel2"], hover_color=T["stroke"],
            text_color=T["accent2"], border_width=1, border_color=T["stroke"],
            anchor="w",
        ).grid(row=2, column=0, columnspan=3, sticky="ew", pady=(6, 0))

        # === Settings summary ===
        sec2 = ctk.CTkFrame(outer, fg_color=T["panel"], corner_radius=12,
                             border_width=1, border_color=T["stroke"])
        sec2.pack(fill="x", pady=(0, 8))
        ctk.CTkLabel(sec2, text="2.  Settings (fixed for top quality)",
                      font=ctk.CTkFont("Segoe UI", 11, weight="bold"),
                      text_color=T["accent"]).pack(anchor="w", padx=14, pady=(10, 4))
        ctk.CTkLabel(
            sec2,
            text=(
                f"  •  Output: {VIDEO_W}×{VIDEO_H} (16:9), {FPS} fps, H.264 high quality\n"
                f"  •  Silences ≥ {MIN_SILENCE_MS} ms are removed\n"
                "  •  Photo N is shown for the duration of speech segment N\n"
                "  •  Image fits inside the 16:9 frame (black bars if needed — no cropping)"
            ),
            justify="left", text_color=T["muted"], font=ctk.CTkFont("Segoe UI", 11),
        ).pack(anchor="w", padx=14, pady=(0, 12))

        # === Buttons ===
        btn_row = ctk.CTkFrame(outer, fg_color="transparent")
        btn_row.pack(fill="x", pady=(0, 8))
        self.convert_btn = ctk.CTkButton(
            btn_row, text="▶   Convert", command=self._start, width=150,
            fg_color=T["accent"], hover_color=hex_lerp(T["accent"], "#FFFFFF", 0.15),
            text_color="#FFFFFF", font=ctk.CTkFont("Segoe UI", 13, weight="bold"),
        )
        self.convert_btn.pack(side="left")
        self.stop_btn = ctk.CTkButton(
            btn_row, text="⏹  Stop", command=self._stop, width=110, state="disabled",
            fg_color=T["panel2"], hover_color=T["danger"],
            text_color=T["muted"], border_width=1, border_color=T["stroke"],
        )
        self.stop_btn.pack(side="left", padx=(10, 0))
        self.open_btn = ctk.CTkButton(
            btn_row, text="📂  Open workspace", command=self._open_workspace, width=160,
            fg_color=T["panel2"], hover_color=T["stroke"],
            text_color=T["text"], border_width=1, border_color=T["stroke"],
        )
        self.open_btn.pack(side="right")

        # === Progress ===
        sec3 = ctk.CTkFrame(outer, fg_color=T["panel"], corner_radius=12,
                             border_width=1, border_color=T["stroke"])
        sec3.pack(fill="x", pady=(0, 8))
        ctk.CTkLabel(sec3, text="3.  Progress",
                      font=ctk.CTkFont("Segoe UI", 11, weight="bold"),
                      text_color=T["accent"]).pack(anchor="w", padx=14, pady=(10, 4))
        p_inner = ctk.CTkFrame(sec3, fg_color="transparent")
        p_inner.pack(fill="x", padx=12, pady=(0, 12))
        p_row = ctk.CTkFrame(p_inner, fg_color="transparent")
        p_row.pack(fill="x")
        p_row.columnconfigure(0, weight=1)
        self.progress = ctk.CTkProgressBar(
            p_row, progress_color=T["accent"], fg_color=T["panel2"],
            height=14, corner_radius=7,
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

        # === Log ===
        sec4 = ctk.CTkFrame(outer, fg_color=T["panel"], corner_radius=12,
                             border_width=1, border_color=T["stroke"])
        sec4.pack(fill="both", expand=True)
        ctk.CTkLabel(sec4, text="Log",
                      font=ctk.CTkFont("Segoe UI", 11, weight="bold"),
                      text_color=T["accent"]).pack(anchor="w", padx=14, pady=(10, 4))
        self.log_text = ctk.CTkTextbox(
            sec4, font=ctk.CTkFont("Consolas", 10),
            fg_color=T["panel2"], text_color=T["text"],
            wrap="word", corner_radius=8,
        )
        self.log_text.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.log_text._textbox.tag_config("INFO", foreground=T["text"])
        self.log_text._textbox.tag_config("OK", foreground=T["ok"])
        self.log_text._textbox.tag_config("WARNING", foreground="#F59E0B")
        self.log_text._textbox.tag_config("ERROR", foreground=T["danger"])
        self.log_text._textbox.tag_config("STEP", foreground=T["accent2"],
                                           font=("Consolas", 10, "bold"))

    # ---------- UI helpers ----------
    def _browse_images(self):
        d = filedialog.askdirectory(title="Select images folder", initialdir=str(WORKSPACE))
        if d:
            self.images_dir.set(d)
            n = sum(1 for f in os.listdir(d) if Path(f).suffix.lower() in IMG_EXTS)
            self._log("INFO", f"Images folder set ({n} images): {d}")

    def _browse_audio(self):
        f = filedialog.askopenfilename(
            title="Select voice file",
            initialdir=str(WORKSPACE),
            filetypes=[("Audio", "*.mp3 *.wav *.m4a *.flac *.ogg"), ("All files", "*.*")],
        )
        if f:
            self.audio_file.set(f)
            self._log("INFO", f"Audio file set: {f}")

    def _use_defaults(self):
        if not DEFAULT_IMAGES.is_dir():
            messagebox.showerror("Defaults missing", f"Images folder not found:\n{DEFAULT_IMAGES}")
            return
        if not DEFAULT_AUDIO.is_file():
            messagebox.showerror("Defaults missing", f"Audio not found:\n{DEFAULT_AUDIO}")
            return
        self.images_dir.set(str(DEFAULT_IMAGES))
        self.audio_file.set(str(DEFAULT_AUDIO))
        n = sum(1 for f in os.listdir(DEFAULT_IMAGES) if Path(f).suffix.lower() in IMG_EXTS)
        self._log("OK", f"Loaded workspace defaults ({n} images, audio: {DEFAULT_AUDIO.name})")

    def _open_workspace(self):
        if sys.platform == "win32":
            os.startfile(str(WORKSPACE))
        elif sys.platform == "darwin":
            subprocess.run(["open", str(WORKSPACE)])
        else:
            subprocess.run(["xdg-open", str(WORKSPACE)])

    def _log(self, level, message):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_queue.put((level, f"[{ts}] {message}"))

    def _poll_log(self):
        try:
            while True:
                level, msg = self.log_queue.get_nowait()
                tag = level if level in ("INFO", "OK", "WARNING", "ERROR", "STEP") else "INFO"
                self.log_text._textbox.insert("end", msg + "\n", tag)
                self.log_text._textbox.see("end")
        except queue.Empty:
            pass
        self.root.after(100, self._poll_log)

    def _set_progress(self, pct, status=None):
        pct = max(0, min(100, int(pct)))
        self.progress.set(pct / 100)
        self.percent_label.configure(text=f"{pct}%")
        if status is not None:
            self.status_label.configure(text=status)
        self.root.update_idletasks()

    # ---------- Pipeline control ----------
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
                messagebox.showerror(
                    "FFmpeg missing",
                    "FFmpeg is required.\n\nDownload a build from https://www.gyan.dev/ffmpeg/builds/\n"
                    "Extract it, then add the 'bin' folder to your PATH and restart this app.",
                )
                return

        self.is_running = True
        self.stop_requested = False
        self.convert_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.log_text._textbox.delete("1.0", "end")
        self._set_progress(0, "Starting…")
        threading.Thread(target=self._run_pipeline_safe, daemon=True).start()

    def _stop(self):
        self.stop_requested = True
        self._log("WARNING", "Stop requested — finishing current step…")

    def _finish(self, ok, output_path=None):
        self.is_running = False
        self.convert_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        if ok and output_path:
            self._set_progress(100, f"Done → {output_path.name}")
            self.root.after(
                0,
                lambda: messagebox.showinfo(
                    "Video ready",
                    f"Saved to:\n{output_path}\n\n"
                    f"Resolution: {VIDEO_W}×{VIDEO_H}\n"
                    f"Click 'Open workspace' to find it.",
                ),
            )
        elif self.stop_requested:
            self._set_progress(0, "Stopped")
        else:
            self._set_progress(0, "Failed — see log")

    def _run_pipeline_safe(self):
        try:
            self._run_pipeline()
        except Exception as e:
            import traceback
            self._log("ERROR", f"Fatal: {e}")
            self._log("ERROR", traceback.format_exc())
            self.root.after(0, lambda: self._finish(False))

    # ---------- Pipeline ----------
    def _run_pipeline(self):
        images_dir = Path(self.images_dir.get())
        audio_path = Path(self.audio_file.get())

        images = sorted(
            [str(images_dir / f) for f in os.listdir(images_dir) if Path(f).suffix.lower() in IMG_EXTS]
        )
        if not images:
            self._log("ERROR", "No images found in the folder.")
            self.root.after(0, lambda: self._finish(False))
            return

        # ---- Step 1: detect speech segments ----
        self._log("STEP", "Step 1/4 — Loading audio and finding speech segments")
        self._set_progress(2, "Loading audio…")
        audio = AudioSegment.from_file(str(audio_path))
        total_ms = len(audio)
        self._log("INFO", f"Audio length: {total_ms/1000:.2f} s")

        self._set_progress(6, f"Removing silences ≥ {MIN_SILENCE_MS} ms…")
        nonsilent = detect_nonsilent(
            audio,
            min_silence_len=MIN_SILENCE_MS,
            silence_thresh=SILENCE_THRESH_DB,
        )
        if not nonsilent:
            self._log("ERROR", "No speech detected — try a different audio file.")
            self.root.after(0, lambda: self._finish(False))
            return
        self._log("OK", f"Found {len(nonsilent)} speech segments")

        # Pair photos 1:1 with segments. Mismatches are handled so no audio is dropped:
        #   • more segments than images → trailing extra segments merge into the last photo
        #   • more images than segments → trailing extra images are skipped (no audio for them)
        n_imgs = len(images)
        n_segs = len(nonsilent)
        n_pairs = min(n_imgs, n_segs)
        merge_tail = n_segs > n_imgs
        if n_imgs != n_segs:
            if merge_tail:
                self._log(
                    "WARNING",
                    f"More segments ({n_segs}) than images ({n_imgs}). "
                    f"Last image will hold for the final {n_segs - n_imgs + 1} segments so no audio is lost.",
                )
            else:
                self._log(
                    "WARNING",
                    f"More images ({n_imgs}) than segments ({n_segs}). "
                    f"Only the first {n_pairs} images will be used.",
                )

        # ---- Step 2: export each speech segment to its own WAV ----
        self._log("STEP", "Step 2/4 — Saving speech segments")
        tmp = Path(tempfile.mkdtemp(prefix="svc_"))
        seg_dir = tmp / "segments"
        seg_dir.mkdir()
        clip_dir = tmp / "clips"
        clip_dir.mkdir()

        segments = []
        for i in range(n_pairs):
            if self.stop_requested:
                shutil.rmtree(tmp, ignore_errors=True)
                self.root.after(0, lambda: self._finish(False))
                return
            if merge_tail and i == n_pairs - 1:
                # Concatenate the last image's segment with all remaining segments.
                combined = AudioSegment.empty()
                total_dur = 0
                for j in range(i, n_segs):
                    s, e = nonsilent[j]
                    s = max(0, s - KEEP_PADDING_MS)
                    e = min(total_ms, e + KEEP_PADDING_MS)
                    combined += audio[s:e]
                    total_dur += e - s
                seg_path = seg_dir / f"seg_{i:03d}.wav"
                combined.export(str(seg_path), format="wav")
                segments.append({"file": str(seg_path), "duration_ms": total_dur})
            else:
                s, e = nonsilent[i]
                s = max(0, s - KEEP_PADDING_MS)
                e = min(total_ms, e + KEEP_PADDING_MS)
                seg_path = seg_dir / f"seg_{i:03d}.wav"
                audio[s:e].export(str(seg_path), format="wav")
                segments.append({"file": str(seg_path), "duration_ms": e - s})
            pct = 6 + int((i + 1) / n_pairs * 9)  # 6 → 15
            self._set_progress(pct, f"Saving segment {i+1}/{n_pairs}")
        self._log("OK", f"Saved {len(segments)} segments")

        # ---- Step 3: render each photo+segment into a clip ----
        self._log("STEP", "Step 3/4 — Rendering clips (photo extends to segment duration)")
        clips = []
        for i in range(n_pairs):
            if self.stop_requested:
                shutil.rmtree(tmp, ignore_errors=True)
                self.root.after(0, lambda: self._finish(False))
                return
            img = images[i]
            seg = segments[i]
            clip_path = clip_dir / f"clip_{i:03d}.mp4"
            dur_s = seg["duration_ms"] / 1000.0
            self._log("INFO", f"  [{i+1}/{n_pairs}] {Path(img).name}  →  {dur_s:.2f}s")
            ok = self._render_clip(img, seg["file"], dur_s, str(clip_path))
            if ok:
                clips.append(str(clip_path))
            else:
                self._log("ERROR", f"  Failed to render clip {i+1}")
            pct = 15 + int((i + 1) / n_pairs * 75)  # 15 → 90
            self._set_progress(pct, f"Rendering clip {i+1}/{n_pairs}")

        if not clips:
            self._log("ERROR", "No clips were rendered.")
            shutil.rmtree(tmp, ignore_errors=True)
            self.root.after(0, lambda: self._finish(False))
            return

        # ---- Step 4: concatenate ----
        self._log("STEP", "Step 4/4 — Joining clips into final video")
        self._set_progress(92, "Joining clips…")
        out_name = f"video_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4"
        out_path = ensure_output_dir() / out_name
        ok = self._concat(clips, str(out_path), tmp)
        if not ok or not out_path.exists():
            self._log("ERROR", "Concatenation failed.")
            shutil.rmtree(tmp, ignore_errors=True)
            self.root.after(0, lambda: self._finish(False))
            return

        size_mb = out_path.stat().st_size / (1024 * 1024)
        self._log("OK", "═" * 50)
        self._log("OK", f"DONE: {out_path}")
        self._log("OK", f"Size: {size_mb:.1f} MB  |  {VIDEO_W}×{VIDEO_H}  |  {len(clips)} clips")
        self._log("OK", "═" * 50)

        shutil.rmtree(tmp, ignore_errors=True)
        self.root.after(0, lambda: self._finish(True, out_path))

    # ---------- FFmpeg helpers ----------
    def _render_clip(self, image_path, audio_path, duration_s, output_path):
        # Scale to fit inside 16:9 (preserve aspect, no crop), pad black bars to exactly 1920x1080.
        vf = (
            f"scale={VIDEO_W}:{VIDEO_H}:force_original_aspect_ratio=decrease:flags=lanczos,"
            f"pad={VIDEO_W}:{VIDEO_H}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"setsar=1"
        )
        cmd = [
            self.ffmpeg, "-y", "-loglevel", "error",
            "-loop", "1",
            "-i", image_path,
            "-i", audio_path,
            "-vf", vf,
            "-c:v", "libx264",
            "-crf", "18",
            "-preset", "medium",
            "-tune", "stillimage",
            "-pix_fmt", "yuv420p",
            "-r", str(FPS),
            "-c:a", "aac",
            "-b:a", "192k",
            "-ar", "48000",
            "-t", f"{duration_s:.3f}",
            "-shortest",
            "-movflags", "+faststart",
            output_path,
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if res.returncode != 0:
                self._log("WARNING", f"ffmpeg: {res.stderr.strip()[:200]}")
                return False
            return True
        except Exception as e:
            self._log("ERROR", f"ffmpeg call failed: {e}")
            return False

    def _concat(self, clip_paths, output_path, tmp_dir):
        list_file = Path(tmp_dir) / "concat.txt"
        with open(list_file, "w", encoding="utf-8") as f:
            for c in clip_paths:
                # Use forward slashes in concat list to avoid escaping issues on Windows
                f.write(f"file '{Path(c).as_posix()}'\n")
        cmd = [
            self.ffmpeg, "-y", "-loglevel", "error",
            "-f", "concat", "-safe", "0",
            "-i", str(list_file),
            "-c", "copy",
            "-movflags", "+faststart",
            output_path,
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if res.returncode != 0:
                self._log("ERROR", f"concat: {res.stderr.strip()[:300]}")
                return False
            return True
        except Exception as e:
            self._log("ERROR", f"concat call failed: {e}")
            return False


def _safe_console():
    """Reconfigure stdout/stderr to UTF-8 so prints with non-ASCII chars don't crash
    on Windows consoles using cp1252."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def run_headless(images_dir, audio_path):
    _safe_console()
    """Run the full pipeline without the GUI. Returns the output Path or None."""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        print("ERROR: FFmpeg not found.")
        return None
    configure_pydub(ffmpeg)

    images_dir = Path(images_dir)
    audio_path = Path(audio_path)
    images = sorted(
        [str(images_dir / f) for f in os.listdir(images_dir) if Path(f).suffix.lower() in IMG_EXTS]
    )
    if not images:
        print("ERROR: no images.")
        return None

    print(f"[1/4] Loading audio: {audio_path.name}")
    audio = AudioSegment.from_file(str(audio_path))
    total_ms = len(audio)
    print(f"      length = {total_ms/1000:.2f}s")

    print(f"[1/4] Detecting silences ≥ {MIN_SILENCE_MS}ms (thresh {SILENCE_THRESH_DB} dB)…")
    nonsilent = detect_nonsilent(
        audio, min_silence_len=MIN_SILENCE_MS, silence_thresh=SILENCE_THRESH_DB
    )
    print(f"      found {len(nonsilent)} speech segments, {len(images)} images")
    if not nonsilent:
        print("ERROR: no speech detected.")
        return None

    n_imgs = len(images)
    n_segs = len(nonsilent)
    n_pairs = min(n_imgs, n_segs)
    merge_tail = n_segs > n_imgs
    if n_imgs != n_segs:
        if merge_tail:
            print(
                f"      more segments ({n_segs}) than images ({n_imgs}) — "
                f"last image will hold for the final {n_segs - n_imgs + 1} segments"
            )
        else:
            print(f"      more images ({n_imgs}) than segments ({n_segs}) — using first {n_pairs}")

    tmp = Path(tempfile.mkdtemp(prefix="svc_"))
    seg_dir = tmp / "segments"; seg_dir.mkdir()
    clip_dir = tmp / "clips"; clip_dir.mkdir()

    print(f"[2/4] Saving {n_pairs} segments…")
    segments = []
    for i in range(n_pairs):
        if merge_tail and i == n_pairs - 1:
            combined = AudioSegment.empty()
            total_dur = 0
            for j in range(i, n_segs):
                s, e = nonsilent[j]
                s = max(0, s - KEEP_PADDING_MS)
                e = min(total_ms, e + KEEP_PADDING_MS)
                combined += audio[s:e]
                total_dur += e - s
            seg_path = seg_dir / f"seg_{i:03d}.wav"
            combined.export(str(seg_path), format="wav")
            segments.append({"file": str(seg_path), "duration_ms": total_dur})
        else:
            s, e = nonsilent[i]
            s = max(0, s - KEEP_PADDING_MS)
            e = min(total_ms, e + KEEP_PADDING_MS)
            seg_path = seg_dir / f"seg_{i:03d}.wav"
            audio[s:e].export(str(seg_path), format="wav")
            segments.append({"file": str(seg_path), "duration_ms": e - s})

    print(f"[3/4] Rendering {n_pairs} clips…")
    clips = []
    for i in range(n_pairs):
        img = images[i]
        seg = segments[i]
        clip_path = clip_dir / f"clip_{i:03d}.mp4"
        dur_s = seg["duration_ms"] / 1000.0
        vf = (
            f"scale={VIDEO_W}:{VIDEO_H}:force_original_aspect_ratio=decrease:flags=lanczos,"
            f"pad={VIDEO_W}:{VIDEO_H}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"
        )
        cmd = [
            ffmpeg, "-y", "-loglevel", "error",
            "-loop", "1", "-i", img, "-i", seg["file"],
            "-vf", vf,
            "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-tune", "stillimage",
            "-pix_fmt", "yuv420p", "-r", str(FPS),
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            "-t", f"{dur_s:.3f}", "-shortest", "-movflags", "+faststart",
            str(clip_path),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            print(f"  [{i+1}] FAIL: {res.stderr.strip()[:200]}")
        else:
            clips.append(str(clip_path))
            print(f"  [{i+1}/{n_pairs}] {Path(img).name} → {dur_s:.2f}s ✓")

    if not clips:
        shutil.rmtree(tmp, ignore_errors=True)
        return None

    print(f"[4/4] Joining {len(clips)} clips…")
    list_file = tmp / "concat.txt"
    with open(list_file, "w", encoding="utf-8") as f:
        for c in clips:
            f.write(f"file '{Path(c).as_posix()}'\n")
    out_name = f"video_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4"
    out_path = ensure_output_dir() / out_name
    res = subprocess.run(
        [ffmpeg, "-y", "-loglevel", "error",
         "-f", "concat", "-safe", "0", "-i", str(list_file),
         "-c", "copy", "-movflags", "+faststart", str(out_path)],
        capture_output=True, text=True,
    )
    shutil.rmtree(tmp, ignore_errors=True)
    if res.returncode != 0 or not out_path.exists():
        print(f"ERROR: concat failed: {res.stderr[:300]}")
        return None
    size_mb = out_path.stat().st_size / (1024 * 1024)
    print(f"DONE: {out_path}  ({size_mb:.1f} MB)")
    return out_path


def _render_and_concat(ffmpeg, segments, images, output_path, log=print, prepend_clips=None,
                       width=VIDEO_W, height=VIDEO_H, fps=FPS):
    """Encode each (image, audio_file, duration_ms) pair and join into a single MP4.

    `prepend_clips` is an optional list of pre-built MP4 paths inserted at the
    beginning of the concat list (e.g. an intro/outro video). They MUST already
    match the target encode (same width x height, H.264, AAC 48kHz) — use
    reencode_to_target with the same width/height/fps to coerce them.
    """
    n = len(segments)
    tmp = Path(tempfile.mkdtemp(prefix="svc_render_"))
    clip_dir = tmp / "clips"
    clip_dir.mkdir()

    clips = list(prepend_clips or [])
    for i in range(n):
        img = images[i]
        seg = segments[i]
        clip_path = clip_dir / f"clip_{i:03d}.mp4"
        dur_s = seg["duration_ms"] / 1000.0
        vf = (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease:flags=lanczos,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"
        )
        cmd = [
            ffmpeg, "-y", "-loglevel", "error",
            "-loop", "1", "-i", img, "-i", seg["file"],
            "-vf", vf,
            "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-tune", "stillimage",
            "-pix_fmt", "yuv420p", "-r", str(fps),
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            "-t", f"{dur_s:.3f}", "-shortest", "-movflags", "+faststart",
            str(clip_path),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            log(f"  [{i+1}] FAIL: {res.stderr.strip()[:200]}")
        else:
            clips.append(str(clip_path))
            log(f"  [{i+1}/{n}] {Path(img).name} -> {dur_s:.2f}s ok")

    if not clips:
        shutil.rmtree(tmp, ignore_errors=True)
        return False

    list_file = tmp / "concat.txt"
    with open(list_file, "w", encoding="utf-8") as f:
        for c in clips:
            f.write(f"file '{Path(c).as_posix()}'\n")
    res = subprocess.run(
        [ffmpeg, "-y", "-loglevel", "error",
         "-f", "concat", "-safe", "0", "-i", str(list_file),
         "-c", "copy", "-movflags", "+faststart", str(output_path)],
        capture_output=True, text=True,
    )
    shutil.rmtree(tmp, ignore_errors=True)
    if res.returncode != 0 or not Path(output_path).exists():
        log(f"ERROR: concat failed: {res.stderr[:300]}")
        return False
    return True


def run_keyword_pipeline(images_dir, audio_path, log=print):
    """Keyword-based segmentation:
    - first 'start' word marks segment 1 boundary
    - each 'continue' word marks the boundary between segments
    - within each segment, silences > INTRA_SEGMENT_MAX_SILENCE_MS are removed
    - photo N is shown for the duration of segment N
    """
    _safe_console()
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        log("ERROR: FFmpeg not found.")
        return None
    configure_pydub(ffmpeg)

    images_dir = Path(images_dir)
    audio_path = Path(audio_path)
    images = sorted(
        [str(images_dir / f) for f in os.listdir(images_dir) if Path(f).suffix.lower() in IMG_EXTS]
    )
    if not images:
        log("ERROR: no images.")
        return None

    log(f"[1/5] Loading audio: {audio_path.name}")
    audio = AudioSegment.from_file(str(audio_path))
    total_ms = len(audio)
    log(f"      length = {total_ms/1000:.2f}s")

    log(f"[2/5] Transcribing audio with Whisper to find '{KEYWORD_START_FORMS[0]}' / '{KEYWORD_SPLIT_FORMS[0]}' markers")
    words = transcribe_with_words(str(audio_path), log=log)

    log(f"[3/5] Building segments from keyword markers")
    seg_ranges = find_keyword_segments(words, total_ms, log=log)
    if not seg_ranges:
        log("ERROR: no usable segments found.")
        return None
    log(f"      built {len(seg_ranges)} segments, {len(images)} images")

    n_imgs = len(images)
    n_segs = len(seg_ranges)
    n_pairs = min(n_imgs, n_segs)
    if n_imgs != n_segs:
        if n_segs > n_imgs:
            log(f"      more segments than images — last image will hold for the final {n_segs - n_imgs + 1} segments")
        else:
            log(f"      more images than segments — using only the first {n_pairs} images")

    log(f"[4/5] Cutting segments and removing internal silences > {INTRA_SEGMENT_MAX_SILENCE_MS}ms…")
    tmp = Path(tempfile.mkdtemp(prefix="svc_kw_"))
    seg_dir = tmp / "segments"
    seg_dir.mkdir()

    segments = []
    for i in range(n_pairs):
        if i == n_pairs - 1 and n_segs > n_imgs:
            # Merge all trailing segments into the last one
            merged = AudioSegment.empty()
            for j in range(i, n_segs):
                s_ms, e_ms = seg_ranges[j]
                seg = audio[s_ms:e_ms]
                seg = compress_internal_silences(seg)
                merged += seg
            seg_path = seg_dir / f"seg_{i:03d}.wav"
            merged.export(str(seg_path), format="wav")
            segments.append({"file": str(seg_path), "duration_ms": len(merged)})
            log(f"  segment {i+1}: merged {n_segs - i} raw → {len(merged)/1000:.2f}s")
        else:
            s_ms, e_ms = seg_ranges[i]
            seg = audio[s_ms:e_ms]
            raw_dur = len(seg)
            seg = compress_internal_silences(seg)
            seg_path = seg_dir / f"seg_{i:03d}.wav"
            seg.export(str(seg_path), format="wav")
            segments.append({"file": str(seg_path), "duration_ms": len(seg)})
            log(f"  segment {i+1}: {raw_dur/1000:.2f}s -> {len(seg)/1000:.2f}s after silence trim")

    out_name = f"video_kw_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4"
    out_path = ensure_output_dir() / out_name
    log(f"[5/5] Rendering {n_pairs} clips and joining…")
    ok = _render_and_concat(ffmpeg, segments, images[:n_pairs], str(out_path), log=log)
    shutil.rmtree(tmp, ignore_errors=True)
    if not ok:
        return None
    size_mb = out_path.stat().st_size / (1024 * 1024)
    log(f"DONE: {out_path}  ({size_mb:.1f} MB)")
    return out_path


def detect_speech_segments_vad(audio_path,
                               min_pause_ms=VAD_MIN_PAUSE_MS,
                               min_speech_ms=VAD_MIN_SPEECH_MS,
                               threshold=VAD_THRESHOLD,
                               log=print):
    """Run Silero VAD over the audio and return speech-segment time ranges (ms).
    Loads audio via pydub (works with MP3 on Windows where torchaudio fails)."""
    import numpy as np
    import torch
    from silero_vad import load_silero_vad, get_speech_timestamps

    log("Loading Silero VAD model (downloads ~30 MB on first run)…")
    model = load_silero_vad()

    log("Resampling audio to 16 kHz mono for VAD…")
    seg = AudioSegment.from_file(audio_path).set_frame_rate(16000).set_channels(1).set_sample_width(2)
    samples = np.array(seg.get_array_of_samples(), dtype=np.int16).astype(np.float32) / 32768.0
    wav = torch.from_numpy(samples)

    log(f"Running VAD (min_pause={min_pause_ms}ms, min_speech={min_speech_ms}ms, threshold={threshold})…")
    ts = get_speech_timestamps(
        wav, model,
        sampling_rate=16000,
        min_silence_duration_ms=min_pause_ms,
        min_speech_duration_ms=min_speech_ms,
        threshold=threshold,
        return_seconds=False,
    )
    # Convert sample indices (16 kHz) to milliseconds.
    ranges_ms = [(int(round(t["start"] / 16)), int(round(t["end"] / 16))) for t in ts]
    log(f"VAD found {len(ranges_ms)} speech segments")
    return ranges_ms


def run_vad_pipeline(images_dir, audio_path, log=print,
                     min_pause_ms=VAD_MIN_PAUSE_MS):
    """Silero-VAD-based segmentation: speech blocks separated by pauses become segments.
    Photo N is shown for the duration of segment N. No markers, no keywords."""
    _safe_console()
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        log("ERROR: FFmpeg not found.")
        return None
    configure_pydub(ffmpeg)

    images_dir = Path(images_dir)
    audio_path = Path(audio_path)
    images = sorted(
        [str(images_dir / f) for f in os.listdir(images_dir) if Path(f).suffix.lower() in IMG_EXTS]
    )
    if not images:
        log("ERROR: no images.")
        return None

    log(f"[1/4] Loading audio: {audio_path.name}")
    audio = AudioSegment.from_file(str(audio_path))
    total_ms = len(audio)
    log(f"      length = {total_ms/1000:.2f}s")

    log(f"[2/4] Detecting speech with Silero VAD")
    ranges = detect_speech_segments_vad(str(audio_path), min_pause_ms=min_pause_ms, log=log)
    if not ranges:
        log("ERROR: VAD found no speech.")
        return None

    n_imgs = len(images)
    n_segs = len(ranges)
    n_pairs = min(n_imgs, n_segs)
    if n_imgs != n_segs:
        if n_segs > n_imgs:
            log(f"      more segments ({n_segs}) than images ({n_imgs}) — last image will hold for the final {n_segs - n_imgs + 1} segments")
        else:
            log(f"      more images ({n_imgs}) than segments ({n_segs}) — using only the first {n_pairs} images")

    log(f"[3/4] Cutting {n_pairs} segments…")
    tmp = Path(tempfile.mkdtemp(prefix="svc_vad_"))
    seg_dir = tmp / "segments"
    seg_dir.mkdir()

    segments = []
    for i in range(n_pairs):
        if i == n_pairs - 1 and n_segs > n_imgs:
            merged = AudioSegment.empty()
            for j in range(i, n_segs):
                s_ms, e_ms = ranges[j]
                s_ms = max(0, s_ms - VAD_PAD_MS)
                e_ms = min(total_ms, e_ms + VAD_PAD_MS)
                merged += audio[s_ms:e_ms]
            seg_path = seg_dir / f"seg_{i:03d}.wav"
            merged.export(str(seg_path), format="wav")
            segments.append({"file": str(seg_path), "duration_ms": len(merged)})
            log(f"  segment {i+1}: merged {n_segs - i} VAD blocks -> {len(merged)/1000:.2f}s")
        else:
            s_ms, e_ms = ranges[i]
            s_ms = max(0, s_ms - VAD_PAD_MS)
            e_ms = min(total_ms, e_ms + VAD_PAD_MS)
            seg = audio[s_ms:e_ms]
            seg_path = seg_dir / f"seg_{i:03d}.wav"
            seg.export(str(seg_path), format="wav")
            segments.append({"file": str(seg_path), "duration_ms": len(seg)})
            log(f"  segment {i+1}: {len(seg)/1000:.2f}s")

    out_name = f"video_vad_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4"
    out_path = ensure_output_dir() / out_name
    log(f"[4/4] Rendering {n_pairs} clips and joining…")
    ok = _render_and_concat(ffmpeg, segments, images[:n_pairs], str(out_path), log=log)
    shutil.rmtree(tmp, ignore_errors=True)
    if not ok:
        return None
    size_mb = out_path.stat().st_size / (1024 * 1024)
    log(f"DONE: {out_path}  ({size_mb:.1f} MB)")
    return out_path


def main():
    cli_modes = ("--cli", "--keywords", "--vad")
    if any(flag in sys.argv for flag in cli_modes):
        images = str(DEFAULT_IMAGES)
        audio = str(DEFAULT_AUDIO)
        if "--images" in sys.argv:
            images = sys.argv[sys.argv.index("--images") + 1]
        if "--audio" in sys.argv:
            audio = sys.argv[sys.argv.index("--audio") + 1]

        if "--vad" in sys.argv:
            min_pause = VAD_MIN_PAUSE_MS
            if "--pause-ms" in sys.argv:
                min_pause = int(sys.argv[sys.argv.index("--pause-ms") + 1])
            result = run_vad_pipeline(images, audio, min_pause_ms=min_pause)
        elif "--keywords" in sys.argv:
            result = run_keyword_pipeline(images, audio)
        else:
            result = run_headless(images, audio)
        sys.exit(0 if result else 1)

    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    root = ctk.CTk()
    VideoCreator(root)
    root.mainloop()


if __name__ == "__main__":
    main()
