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
from tkinter import ttk, filedialog, messagebox, scrolledtext

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from simple_video_creator import find_ffmpeg, configure_pydub, _safe_console  # noqa: E402

# Anything ffmpeg can read as an audio source is fair game for input.
INPUT_EXTS = {
    ".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v", ".flv",     # video
    ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus", ".mp3", ".wma",  # audio
}

BITRATE_PRESETS = ["128 kbps", "192 kbps", "256 kbps", "320 kbps"]
DEFAULT_BITRATE = "192 kbps"


def bitrate_to_ffmpeg(label):
    """'192 kbps' -> '192k'."""
    return label.split()[0] + "k"


class AudioConverterApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Audio Converter — Video/Audio → MP3")
        self.root.geometry("760x540")
        self.root.minsize(720, 480)

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
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)

        ttk.Label(
            outer,
            text="Convert any video or audio file into an MP3.",
            font=("Segoe UI", 11, "bold"),
        ).pack(anchor="w", pady=(0, 4))
        ttk.Label(
            outer,
            text="The output MP3 saves to the location you choose. By default it goes next to the input file.",
            foreground="#555",
        ).pack(anchor="w", pady=(0, 12))

        # 1. Input
        src = ttk.LabelFrame(outer, text="1. Input file", padding=10)
        src.pack(fill="x", pady=(0, 8))
        src.columnconfigure(0, weight=1)
        ttk.Entry(src, textvariable=self.input_path).grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ttk.Button(src, text="Browse…", command=self._browse_input).grid(row=0, column=1)

        # 2. Output
        dst = ttk.LabelFrame(outer, text="2. Output MP3", padding=10)
        dst.pack(fill="x", pady=(0, 8))
        dst.columnconfigure(0, weight=1)
        ttk.Entry(dst, textvariable=self.output_path).grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ttk.Button(dst, text="Browse…", command=self._browse_output).grid(row=0, column=1)

        # 3. Bitrate
        opts = ttk.LabelFrame(outer, text="3. Quality", padding=10)
        opts.pack(fill="x", pady=(0, 8))
        ttk.Label(opts, text="Bitrate:").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Combobox(
            opts, textvariable=self.bitrate, state="readonly",
            values=BITRATE_PRESETS, width=14,
        ).grid(row=0, column=1, sticky="w")
        ttk.Label(
            opts,
            text="Higher = better quality, larger file. 192 kbps is a sensible default.",
            foreground="#666",
        ).grid(row=0, column=2, sticky="w", padx=(12, 0))

        # 4. Action
        btn_row = ttk.Frame(outer)
        btn_row.pack(fill="x", pady=(0, 8))
        self.convert_btn = ttk.Button(btn_row, text="▶  Convert to MP3", command=self._start, width=22)
        self.convert_btn.pack(side="left")
        self.open_btn = ttk.Button(
            btn_row, text="📂 Open output folder", command=self._open_output_folder, state="disabled",
        )
        self.open_btn.pack(side="right")

        # 5. Progress
        prog = ttk.LabelFrame(outer, text="Progress", padding=10)
        prog.pack(fill="x", pady=(0, 8))
        prog.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(prog, mode="determinate", maximum=100)
        self.progress.grid(row=0, column=0, sticky="ew")
        self.percent_label = ttk.Label(prog, text="0%", width=6, anchor="e")
        self.percent_label.grid(row=0, column=1, padx=(8, 0))
        self.status_label = ttk.Label(prog, text="Idle", foreground="#555")
        self.status_label.grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))

        # 6. Log
        log_frame = ttk.LabelFrame(outer, text="Log", padding=8)
        log_frame.pack(fill="both", expand=True)
        self.log_text = scrolledtext.ScrolledText(log_frame, height=8, wrap="word", font=("Consolas", 9))
        self.log_text.pack(fill="both", expand=True)

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
        # Auto-suggest output: same folder, same stem, .mp3 extension.
        if not self.output_path.get().strip():
            self._suggest_output_from_input()

    def _suggest_output_from_input(self):
        inp = Path(self.input_path.get())
        if inp.is_file():
            self.output_path.set(str(inp.with_suffix(".mp3")))

    def _browse_output(self):
        # If we have an input, use its folder/stem as the suggestion.
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
        self.progress["value"] = pct
        self.percent_label.config(text=f"{pct}%")
        if status is not None:
            self.status_label.config(text=status)
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

        # Confirm overwrite if the output already exists
        if Path(out).exists():
            ok = messagebox.askyesno(
                "Overwrite?",
                f"{Path(out).name} already exists.\n\nReplace it?",
            )
            if not ok:
                return

        self.is_converting = True
        self.convert_btn.config(state="disabled")
        self.open_btn.config(state="disabled")
        self.log_text.delete("1.0", "end")
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

        # `-vn` discards any video stream; `libmp3lame` is the standard MP3 encoder
        # available in every full ffmpeg build (including Gyan.FFmpeg WinGet package).
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
        self.convert_btn.config(state="normal")
        if ok and Path(out).exists():
            size_mb = Path(out).stat().st_size / (1024 * 1024)
            self.status_label.config(text=f"Done → {Path(out).name} ({size_mb:.2f} MB)")
            self.open_btn.config(state="normal")
            messagebox.showinfo(
                "Conversion complete",
                f"Saved to:\n{out}\n\nClick 'Open output folder' to find it.",
            )
        else:
            self._set_progress(0, "Failed — see log")
            messagebox.showerror("Conversion failed", "Could not produce an MP3. Check the log.")


def main():
    root = tk.Tk()
    try:
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
    except Exception:
        pass
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
