#!/usr/bin/env python3
"""
Audio Converter — extract MP3 audio from a video or convert an audio file.

Pick a video (mp4/mkv/mov/…) or an audio file (m4a/aac/wav/flac/…),
choose where to save the MP3, click Convert.

Run:
    python audio_converter.py
"""

import os
import sys
import subprocess
import threading
import queue
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox

import customtkinter as ctk

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from simple_video_creator import find_ffmpeg, configure_pydub, _safe_console  # noqa: E402

INPUT_EXTS = {
    ".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v", ".flv",
    ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus", ".mp3", ".wma",
}

BITRATE_PRESETS = ["128 kbps", "192 kbps", "256 kbps", "320 kbps"]
DEFAULT_BITRATE = "192 kbps"

THEMES = {
    "dark": {
        "appearance": "dark",
        "bg": "#0F1117",
        "panel": "#181B25",
        "panel2": "#222637",
        "stroke": "#2C3145",
        "text": "#F5F7FA",
        "muted": "#8B92A6",
        "accent": "#7C5CFF",
        "accent2": "#22D3EE",
        "accent3": "#F472B6",
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
    t = max(0.0, min(1.0, t))
    r1, g1, b1 = int(c1[1:3], 16), int(c1[3:5], 16), int(c1[5:7], 16)
    r2, g2, b2 = int(c2[1:3], 16), int(c2[3:5], 16), int(c2[5:7], 16)
    return f"#{int(r1+(r2-r1)*t):02x}{int(g1+(g2-g1)*t):02x}{int(b1+(b2-b1)*t):02x}"


def bitrate_to_ffmpeg(label):
    return label.split()[0] + "k"


class AudioConverterApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Audio Converter — Video/Audio → MP3")
        self.root.geometry("780x600")
        self.root.minsize(720, 520)

        self.T = THEMES["dark"]
        self.root.configure(fg_color=self.T["bg"])

        self.input_path = tk.StringVar()
        self.output_path = tk.StringVar()
        self.bitrate = tk.StringVar(value=DEFAULT_BITRATE)

        self.is_converting = False
        self.log_queue = queue.Queue()
        self.ffmpeg = find_ffmpeg()
        configure_pydub(self.ffmpeg)

        self._build_ui()
        self._poll_log()

        if self.ffmpeg:
            self._log(f"FFmpeg: {self.ffmpeg}")
        else:
            self._log("WARNING: FFmpeg not found on PATH. Convert will fail.")

    # ---------- UI ----------
    def _build_ui(self):
        T = self.T

        outer = ctk.CTkFrame(self.root, fg_color=T["bg"])
        outer.pack(fill="both", expand=True, padx=16, pady=16)

        # Title
        ctk.CTkLabel(
            outer, text="Convert video / audio  →  MP3",
            font=ctk.CTkFont("Segoe UI", 20, weight="bold"),
            text_color=T["text"],
        ).pack(anchor="w", pady=(0, 4))
        ctk.CTkLabel(
            outer,
            text="Pick any video or audio file — the audio track is extracted and saved as an MP3.",
            font=ctk.CTkFont("Segoe UI", 12),
            text_color=T["muted"],
        ).pack(anchor="w", pady=(0, 14))

        # 1. Input
        sec1 = ctk.CTkFrame(outer, fg_color=T["panel"], corner_radius=12,
                             border_width=1, border_color=T["stroke"])
        sec1.pack(fill="x", pady=(0, 8))
        ctk.CTkLabel(sec1, text="1.  Input file",
                      font=ctk.CTkFont("Segoe UI", 11, weight="bold"),
                      text_color=T["accent"]).pack(anchor="w", padx=14, pady=(10, 4))
        row1 = ctk.CTkFrame(sec1, fg_color="transparent")
        row1.pack(fill="x", padx=12, pady=(0, 12))
        row1.columnconfigure(0, weight=1)
        ctk.CTkEntry(
            row1, textvariable=self.input_path,
            placeholder_text="Pick a video or audio file…",
            fg_color=T["panel2"], border_color=T["stroke"],
            text_color=T["text"], placeholder_text_color=T["muted"],
        ).grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ctk.CTkButton(
            row1, text="Browse…", command=self._browse_input, width=96,
            fg_color=T["panel2"], hover_color=T["stroke"],
            text_color=T["text"], border_width=1, border_color=T["stroke"],
        ).grid(row=0, column=1)

        # 2. Output
        sec2 = ctk.CTkFrame(outer, fg_color=T["panel"], corner_radius=12,
                             border_width=1, border_color=T["stroke"])
        sec2.pack(fill="x", pady=(0, 8))
        ctk.CTkLabel(sec2, text="2.  Output MP3",
                      font=ctk.CTkFont("Segoe UI", 11, weight="bold"),
                      text_color=T["accent"]).pack(anchor="w", padx=14, pady=(10, 4))
        row2 = ctk.CTkFrame(sec2, fg_color="transparent")
        row2.pack(fill="x", padx=12, pady=(0, 12))
        row2.columnconfigure(0, weight=1)
        ctk.CTkEntry(
            row2, textvariable=self.output_path,
            placeholder_text="Where to save the MP3…",
            fg_color=T["panel2"], border_color=T["stroke"],
            text_color=T["text"], placeholder_text_color=T["muted"],
        ).grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ctk.CTkButton(
            row2, text="Browse…", command=self._browse_output, width=96,
            fg_color=T["panel2"], hover_color=T["stroke"],
            text_color=T["text"], border_width=1, border_color=T["stroke"],
        ).grid(row=0, column=1)

        # 3. Quality
        sec3 = ctk.CTkFrame(outer, fg_color=T["panel"], corner_radius=12,
                             border_width=1, border_color=T["stroke"])
        sec3.pack(fill="x", pady=(0, 8))
        ctk.CTkLabel(sec3, text="3.  Quality",
                      font=ctk.CTkFont("Segoe UI", 11, weight="bold"),
                      text_color=T["accent"]).pack(anchor="w", padx=14, pady=(10, 4))
        row3 = ctk.CTkFrame(sec3, fg_color="transparent")
        row3.pack(fill="x", padx=12, pady=(0, 12))
        ctk.CTkLabel(row3, text="Bitrate:",
                      text_color=T["text"], font=ctk.CTkFont("Segoe UI", 12)).pack(side="left", padx=(0, 10))
        ctk.CTkComboBox(
            row3, variable=self.bitrate, values=BITRATE_PRESETS, state="readonly",
            width=160, fg_color=T["panel2"], border_color=T["stroke"],
            text_color=T["text"], button_color=T["stroke"],
            button_hover_color=T["accent"],
            dropdown_fg_color=T["panel2"], dropdown_text_color=T["text"],
            dropdown_hover_color=T["accent"],
        ).pack(side="left", padx=(0, 14))
        ctk.CTkLabel(
            row3, text="Higher = better quality, larger file.  192 kbps is a good default.",
            text_color=T["muted"], font=ctk.CTkFont("Segoe UI", 11),
        ).pack(side="left")

        # 4. Buttons
        btn_row = ctk.CTkFrame(outer, fg_color="transparent")
        btn_row.pack(fill="x", pady=(0, 8))
        self.convert_btn = ctk.CTkButton(
            btn_row, text="▶   Convert to MP3", command=self._start, width=190,
            fg_color=T["accent"], hover_color=hex_lerp(T["accent"], "#FFFFFF", 0.15),
            text_color="#FFFFFF", font=ctk.CTkFont("Segoe UI", 13, weight="bold"),
        )
        self.convert_btn.pack(side="left")
        self.open_btn = ctk.CTkButton(
            btn_row, text="📂  Open output folder", command=self._open_output_folder,
            width=190, state="disabled",
            fg_color=T["panel2"], hover_color=T["stroke"],
            text_color=T["muted"], border_width=1, border_color=T["stroke"],
        )
        self.open_btn.pack(side="right")

        # 5. Progress
        sec5 = ctk.CTkFrame(outer, fg_color=T["panel"], corner_radius=12,
                             border_width=1, border_color=T["stroke"])
        sec5.pack(fill="x", pady=(0, 8))
        ctk.CTkLabel(sec5, text="Progress",
                      font=ctk.CTkFont("Segoe UI", 11, weight="bold"),
                      text_color=T["accent"]).pack(anchor="w", padx=14, pady=(10, 4))
        p_inner = ctk.CTkFrame(sec5, fg_color="transparent")
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

        # 6. Log
        sec6 = ctk.CTkFrame(outer, fg_color=T["panel"], corner_radius=12,
                             border_width=1, border_color=T["stroke"])
        sec6.pack(fill="both", expand=True)
        ctk.CTkLabel(sec6, text="Log",
                      font=ctk.CTkFont("Segoe UI", 11, weight="bold"),
                      text_color=T["accent"]).pack(anchor="w", padx=14, pady=(10, 4))
        self.log_text = ctk.CTkTextbox(
            sec6, font=ctk.CTkFont("Consolas", 10),
            fg_color=T["panel2"], text_color=T["text"],
            wrap="word", corner_radius=8,
        )
        self.log_text.pack(fill="both", expand=True, padx=12, pady=(0, 12))

    def _browse_input(self):
        f = filedialog.askopenfilename(
            title="Pick a video or audio file",
            initialdir=str(HERE),
            filetypes=[
                ("Video / audio", " ".join(f"*{e}" for e in sorted(INPUT_EXTS))),
                ("All files", "*.*"),
            ],
        )
        if not f:
            return
        self.input_path.set(f)
        if not self.output_path.get().strip():
            self._suggest_output_from_input()

    def _suggest_output_from_input(self):
        inp = Path(self.input_path.get())
        if inp.is_file():
            self.output_path.set(str(inp.with_suffix(".mp3")))

    def _browse_output(self):
        inp = Path(self.input_path.get()) if self.input_path.get() else None
        initdir = str(inp.parent) if inp and inp.parent.is_dir() else str(HERE)
        initfile = inp.with_suffix(".mp3").name if inp else "output.mp3"
        f = filedialog.asksaveasfilename(
            title="Save MP3 as…",
            initialdir=initdir,
            initialfile=initfile,
            defaultextension=".mp3",
            filetypes=[("MP3", "*.mp3"), ("All files", "*.*")],
        )
        if f:
            self.output_path.set(f)

    def _open_output_folder(self):
        out = self.output_path.get()
        folder = Path(out).parent if out else HERE
        if not folder.is_dir():
            folder = HERE
        if sys.platform == "win32":
            os.startfile(str(folder))
        elif sys.platform == "darwin":
            subprocess.run(["open", str(folder)])
        else:
            subprocess.run(["xdg-open", str(folder)])

    # ---------- Logging ----------
    def _log(self, message):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_queue.put(f"[{ts}] {message}")

    def _poll_log(self):
        try:
            while True:
                msg = self.log_queue.get_nowait()
                if hasattr(self, "log_text") and self.log_text.winfo_exists():
                    self.log_text.insert("end", msg + "\n")
                    self.log_text.see("end")
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

    # ---------- Convert ----------
    def _start(self):
        if self.is_converting:
            return

        inp = self.input_path.get().strip()
        out = self.output_path.get().strip()

        if not inp or not Path(inp).is_file():
            messagebox.showerror("Missing input", "Pick a valid input file.")
            return
        if not out:
            messagebox.showerror("Missing output", "Pick where to save the MP3.")
            return
        if Path(inp).resolve() == Path(out).resolve():
            messagebox.showerror("Same path", "Input and output cannot be the same file.")
            return
        if not self.ffmpeg:
            self.ffmpeg = find_ffmpeg()
            configure_pydub(self.ffmpeg)
            if not self.ffmpeg:
                messagebox.showerror("FFmpeg missing", "FFmpeg is required.")
                return

        if Path(out).exists():
            ok = messagebox.askyesno(
                "Overwrite?",
                f"{Path(out).name} already exists.\n\nReplace it?",
            )
            if not ok:
                return

        self.is_converting = True
        self.convert_btn.configure(state="disabled")
        self.open_btn.configure(state="disabled")
        self.log_text.delete("0.0", "end")
        self._set_progress(0, "Starting…")

        threading.Thread(
            target=self._convert_thread,
            args=(inp, out, bitrate_to_ffmpeg(self.bitrate.get())),
            daemon=True,
        ).start()

    def _convert_thread(self, inp, out, bitrate_arg):
        try:
            ok = self._do_convert(inp, out, bitrate_arg)
        except Exception as e:
            import traceback
            self._log(f"ERROR: {e}")
            self._log(traceback.format_exc())
            ok = False
        self.root.after(0, lambda: self._convert_done(ok, out))

    def _do_convert(self, inp, out, bitrate_arg):
        _safe_console()
        self._log(f"Input:   {inp}")
        self._log(f"Output:  {out}")
        self._log(f"Bitrate: {bitrate_arg}")
        self._set_progress(5, "Probing input…")

        cmd = [
            self.ffmpeg, "-y", "-loglevel", "error", "-stats",
            "-i", str(inp),
            "-vn",
            "-c:a", "libmp3lame",
            "-b:a", bitrate_arg,
            "-ar", "44100",
            "-ac", "2",
            "-id3v2_version", "3",
            str(out),
        ]
        self._set_progress(15, "Converting…")
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                self._log(line)
        proc.wait()
        if proc.returncode != 0:
            self._log(f"ffmpeg exit code {proc.returncode}")
            return False
        self._set_progress(100, "Done")
        return True

    def _convert_done(self, ok, out):
        self.is_converting = False
        self.convert_btn.configure(state="normal")
        if ok and Path(out).exists():
            size_mb = Path(out).stat().st_size / (1024 * 1024)
            self.status_label.configure(text=f"Done → {Path(out).name} ({size_mb:.2f} MB)")
            self.open_btn.configure(
                state="normal",
                text_color=self.T["text"],
                fg_color=self.T["panel2"],
            )
            messagebox.showinfo(
                "Conversion complete",
                f"Saved to:\n{out}\n\nClick 'Open output folder' to find it.",
            )
        else:
            self._set_progress(0, "Failed — see log")
            messagebox.showerror("Conversion failed", "Could not produce an MP3. Check the log.")


def main():
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    root = ctk.CTk()
    AudioConverterApp(root)
    root.lift()
    root.attributes("-topmost", True)
    root.after(500, lambda: root.attributes("-topmost", False))
    try:
        root.focus_force()
    except Exception:
        pass
    root.mainloop()


if __name__ == "__main__":
    main()
