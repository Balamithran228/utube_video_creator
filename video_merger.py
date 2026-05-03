#!/usr/bin/env python3
"""
Video Merger / Browser

Three-step flow:
  1. Gallery  — every MP4 in the workspace shown as a thumbnail. Click the
                thumbnail to preview in your default player. Tick the box to
                add it to the merge queue.
  2. Reorder  — selected videos shown as a list. Drag rows up/down (or use
                the buttons) to set the play order.
  3. Render   — ffmpeg re-encodes everything to a common resolution and
                concatenates into `merged_<timestamp>.mp4`.

Run:
    python video_merger.py
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

import tkinter as tk
from tkinter import ttk, messagebox, filedialog

from PIL import Image, ImageTk

# Reuse helpers from the existing pipeline scripts
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from simple_video_creator import find_ffmpeg, configure_pydub, _safe_console  # noqa: E402

# Same preset list as tone_video_creator so the look + feel stays consistent.
RESOLUTION_PRESETS = {
    "360p":  (640,  360),
    "480p":  (854,  480),
    "720p":  (1280, 720),
    "1080p": (1920, 1080),
    "1440p": (2560, 1440),
    "2160p": (3840, 2160),
}
DEFAULT_RESOLUTION_KEY = "1080p"

VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}
THUMB_W, THUMB_H = 200, 112  # 16:9 thumbs in the gallery
GALLERY_COLS = 3             # how many cards per row in the gallery


def find_ffprobe(ffmpeg_path):
    """ffprobe lives next to ffmpeg in every install we care about."""
    if not ffmpeg_path:
        return None
    bin_dir = Path(ffmpeg_path).parent
    name = "ffprobe.exe" if sys.platform == "win32" else "ffprobe"
    candidate = bin_dir / name
    return str(candidate) if candidate.is_file() else None


def get_video_duration_s(ffprobe, path):
    """Return duration in seconds, or 0.0 if it can't be determined."""
    if not ffprobe:
        return 0.0
    res = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True,
    )
    try:
        return float(res.stdout.strip())
    except (ValueError, AttributeError):
        return 0.0


def fmt_duration(seconds):
    if seconds <= 0:
        return "?:??"
    m, s = divmod(int(round(seconds)), 60)
    return f"{m}:{s:02d}"


def fmt_size(bytes_):
    mb = bytes_ / (1024 * 1024)
    if mb < 1:
        return f"{bytes_/1024:.0f} KB"
    return f"{mb:.1f} MB"


def open_in_default_player(path):
    if sys.platform == "win32":
        os.startfile(str(path))
    elif sys.platform == "darwin":
        subprocess.run(["open", str(path)])
    else:
        subprocess.run(["xdg-open", str(path)])


class VideoMergerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Video Merger")
        self.root.geometry("900x700")
        self.root.minsize(820, 600)

        self.ffmpeg = find_ffmpeg()
        configure_pydub(self.ffmpeg)
        self.ffprobe = find_ffprobe(self.ffmpeg)

        # Cache of PhotoImage objects (anchor on self so they aren't GC'd)
        self._thumb_cache = {}
        self._thumb_dir = Path(tempfile.mkdtemp(prefix="vm_thumbs_"))

        # Gallery state — populated by _scan_workspace()
        # Each entry: dict(path, name, size, duration, selected: BooleanVar)
        self.videos = []

        # Reorder state — list of paths in the chosen play order
        self.ordered_paths = []

        # Output settings
        self.resolution_var = tk.StringVar(
            value=f"{DEFAULT_RESOLUTION_KEY} ({RESOLUTION_PRESETS[DEFAULT_RESOLUTION_KEY][0]}×{RESOLUTION_PRESETS[DEFAULT_RESOLUTION_KEY][1]})"
        )

        # Render state
        self.is_rendering = False
        self.log_queue = queue.Queue()

        # Outer frame — content swaps in/out of `self.body` based on the step.
        self.outer = ttk.Frame(self.root, padding=12)
        self.outer.pack(fill="both", expand=True)

        self.header = ttk.Label(
            self.outer, text="Step 1 of 2 — Pick the videos to merge",
            font=("Segoe UI", 13, "bold"),
        )
        self.header.pack(anchor="w", pady=(0, 8))

        self.body = ttk.Frame(self.outer)
        self.body.pack(fill="both", expand=True)

        self.footer = ttk.Frame(self.outer)
        self.footer.pack(fill="x", pady=(8, 0))

        # Background log poller (used during render)
        self._poll_log()

        # Kick off the gallery view
        self._show_gallery()

        # Clean up the thumb temp dir on close
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self):
        try:
            shutil.rmtree(self._thumb_dir, ignore_errors=True)
        except Exception:
            pass
        self.root.destroy()

    # ---------- Workspace scan ----------
    def _scan_workspace(self):
        """Find every video file in the workspace and gather basic metadata."""
        videos = []
        for p in sorted(HERE.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if not p.is_file() or p.suffix.lower() not in VIDEO_EXTS:
                continue
            try:
                size = p.stat().st_size
            except OSError:
                continue
            videos.append({
                "path": p,
                "name": p.name,
                "size": size,
                "duration": get_video_duration_s(self.ffprobe, p),
                "selected": tk.BooleanVar(value=False),
            })
        return videos

    def _get_thumb(self, video_path):
        """Return a PhotoImage thumbnail for the given video. Caches per session."""
        key = str(video_path)
        if key in self._thumb_cache:
            return self._thumb_cache[key]

        thumb_jpg = self._thumb_dir / f"{Path(video_path).stem}_{abs(hash(key))}.jpg"
        if not thumb_jpg.is_file() and self.ffmpeg:
            # Grab a frame at 1s (or earliest available); fail silently if the
            # video is shorter than that — the placeholder image kicks in.
            cmd = [
                self.ffmpeg, "-y", "-loglevel", "error",
                "-ss", "1",
                "-i", str(video_path),
                "-vframes", "1",
                "-vf", (
                    f"scale={THUMB_W}:{THUMB_H}:force_original_aspect_ratio=decrease,"
                    f"pad={THUMB_W}:{THUMB_H}:(ow-iw)/2:(oh-ih)/2:color=black"
                ),
                str(thumb_jpg),
            ]
            subprocess.run(cmd, capture_output=True)

        if thumb_jpg.is_file():
            try:
                img = Image.open(thumb_jpg)
                photo = ImageTk.PhotoImage(img)
                self._thumb_cache[key] = photo
                return photo
            except Exception:
                pass

        # Placeholder grey rectangle if extraction failed
        placeholder = Image.new("RGB", (THUMB_W, THUMB_H), color="#444")
        photo = ImageTk.PhotoImage(placeholder)
        self._thumb_cache[key] = photo
        return photo

    # ---------- Step 1: Gallery ----------
    def _clear_body(self):
        for child in self.body.winfo_children():
            child.destroy()
        for child in self.footer.winfo_children():
            child.destroy()

    def _show_gallery(self):
        self._clear_body()
        self.header.config(text="Step 1 of 2 — Pick the videos to merge")

        if not self.ffmpeg:
            messagebox.showerror(
                "FFmpeg missing",
                "FFmpeg is required for video metadata + thumbnails.",
            )

        # Toolbar above the gallery: Refresh + Add file
        bar = ttk.Frame(self.body)
        bar.pack(fill="x", pady=(0, 8))
        ttk.Button(bar, text="🔄 Refresh", command=self._refresh_gallery).pack(side="left")
        ttk.Button(bar, text="➕ Add file from elsewhere…", command=self._add_external_file).pack(side="left", padx=(8, 0))
        self.gallery_summary = ttk.Label(bar, text="", foreground="#555")
        self.gallery_summary.pack(side="right")

        # Scrollable canvas for the thumbnail grid
        canvas_holder = ttk.Frame(self.body)
        canvas_holder.pack(fill="both", expand=True)

        self.gallery_canvas = tk.Canvas(canvas_holder, highlightthickness=0)
        vsb = ttk.Scrollbar(canvas_holder, orient="vertical", command=self.gallery_canvas.yview)
        self.gallery_canvas.configure(yscrollcommand=vsb.set)
        self.gallery_canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        self.gallery_inner = ttk.Frame(self.gallery_canvas)
        self.gallery_inner.bind(
            "<Configure>",
            lambda _e: self.gallery_canvas.configure(scrollregion=self.gallery_canvas.bbox("all")),
        )
        self.gallery_canvas.create_window((0, 0), window=self.gallery_inner, anchor="nw")

        # Mouse-wheel scroll inside the canvas
        self.gallery_canvas.bind_all(
            "<MouseWheel>",
            lambda e: self.gallery_canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"),
        )

        # Footer: Continue
        self.continue_btn = ttk.Button(
            self.footer, text="Continue ▶", command=self._continue_to_reorder, width=18,
        )
        self.continue_btn.pack(side="right")
        ttk.Button(
            self.footer, text="📂 Open workspace", command=lambda: open_in_default_player(HERE),
        ).pack(side="left")
        self.selected_count_label = ttk.Label(self.footer, text="0 selected", foreground="#555")
        self.selected_count_label.pack(side="right", padx=(0, 12))

        self._refresh_gallery()

    def _refresh_gallery(self):
        self.videos = self._scan_workspace()
        # Wipe the inner frame
        for child in self.gallery_inner.winfo_children():
            child.destroy()

        if not self.videos:
            ttk.Label(
                self.gallery_inner,
                text="No video files found in the workspace yet.\nCreate one with the tone or legacy creator first.",
                foreground="#888",
                justify="center",
            ).grid(row=0, column=0, padx=20, pady=40)
            self.gallery_summary.config(text="0 videos")
            self._update_continue_state()
            return

        for i, v in enumerate(self.videos):
            r, c = divmod(i, GALLERY_COLS)
            self._build_gallery_card(self.gallery_inner, v).grid(
                row=r, column=c, padx=8, pady=8, sticky="nw",
            )

        total_size = sum(v["size"] for v in self.videos)
        self.gallery_summary.config(text=f"{len(self.videos)} videos · {fmt_size(total_size)}")
        self._update_continue_state()

    def _build_gallery_card(self, parent, video):
        card = ttk.Frame(parent, relief="solid", borderwidth=1, padding=6)

        thumb = self._get_thumb(video["path"])
        thumb_label = tk.Label(card, image=thumb, cursor="hand2", bd=0)
        thumb_label.image = thumb  # keep ref
        thumb_label.pack()
        thumb_label.bind("<Button-1>", lambda _e, p=video["path"]: open_in_default_player(p))

        # Filename — truncate gently if too long
        name = video["name"]
        if len(name) > 32:
            name = name[:29] + "…"
        ttk.Label(card, text=name, font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(4, 0))
        ttk.Label(
            card,
            text=f"{fmt_duration(video['duration'])} · {fmt_size(video['size'])}",
            foreground="#666",
        ).pack(anchor="w")

        ttk.Checkbutton(
            card, text="Add to merge", variable=video["selected"],
            command=self._update_continue_state,
        ).pack(anchor="w", pady=(4, 0))
        return card

    def _update_continue_state(self):
        n = sum(1 for v in self.videos if v["selected"].get())
        self.selected_count_label.config(text=f"{n} selected")
        if n >= 2:
            self.continue_btn.config(state="normal")
        else:
            self.continue_btn.config(state="disabled")

    def _add_external_file(self):
        f = filedialog.askopenfilename(
            title="Pick a video to add",
            initialdir=str(HERE),
            filetypes=[("Video", " ".join(f"*{e}" for e in sorted(VIDEO_EXTS))), ("All files", "*.*")],
        )
        if not f:
            return
        # Copy/symlink not needed — we can reference the file in place.
        p = Path(f)
        try:
            size = p.stat().st_size
        except OSError:
            return
        self.videos.append({
            "path": p,
            "name": p.name,
            "size": size,
            "duration": get_video_duration_s(self.ffprobe, p),
            "selected": tk.BooleanVar(value=True),
        })
        # Re-render the grid so the new card shows up + checkbox state updates
        for child in self.gallery_inner.winfo_children():
            child.destroy()
        for i, v in enumerate(self.videos):
            r, c = divmod(i, GALLERY_COLS)
            self._build_gallery_card(self.gallery_inner, v).grid(
                row=r, column=c, padx=8, pady=8, sticky="nw",
            )
        self.gallery_summary.config(
            text=f"{len(self.videos)} videos · {fmt_size(sum(v['size'] for v in self.videos))}"
        )
        self._update_continue_state()

    def _continue_to_reorder(self):
        picked = [v for v in self.videos if v["selected"].get()]
        if len(picked) < 2:
            messagebox.showwarning("Need at least 2", "Pick at least two videos to merge.")
            return
        self.ordered_paths = [v["path"] for v in picked]
        self._show_reorder()

    # ---------- Step 2: Reorder ----------
    def _show_reorder(self):
        self._clear_body()
        self.header.config(text="Step 2 of 2 — Drag the videos into the order you want")

        intro = ttk.Label(
            self.body,
            text=(
                "Top of the list plays first, bottom plays last. "
                "Drag a row to reorder, or use the ▲ ▼ buttons. ✕ removes a row."
            ),
            foreground="#555",
            wraplength=820,
        )
        intro.pack(anchor="w", pady=(0, 8))

        # Listbox + scrollbar + side buttons
        body = ttk.Frame(self.body)
        body.pack(fill="both", expand=True)

        list_frame = ttk.Frame(body)
        list_frame.pack(side="left", fill="both", expand=True)

        self.reorder_list = tk.Listbox(
            list_frame, font=("Segoe UI", 10), activestyle="dotbox", selectmode="single",
        )
        self.reorder_list.pack(side="left", fill="both", expand=True)
        rsb = ttk.Scrollbar(list_frame, orient="vertical", command=self.reorder_list.yview)
        rsb.pack(side="right", fill="y")
        self.reorder_list.config(yscrollcommand=rsb.set)

        # Drag-to-reorder: press, drag, release.
        self.reorder_list.bind("<Button-1>", self._drag_start)
        self.reorder_list.bind("<B1-Motion>", self._drag_motion)
        self.reorder_list.bind("<ButtonRelease-1>", self._drag_end)
        # Double-click previews the video.
        self.reorder_list.bind("<Double-Button-1>", self._preview_selected)

        side = ttk.Frame(body, padding=(10, 0, 0, 0))
        side.pack(side="right", fill="y")
        ttk.Button(side, text="▲ Up", command=lambda: self._move_selected(-1), width=10).pack(pady=2)
        ttk.Button(side, text="▼ Down", command=lambda: self._move_selected(1), width=10).pack(pady=2)
        ttk.Button(side, text="✕ Remove", command=self._remove_selected, width=10).pack(pady=2)
        ttk.Button(side, text="▶ Preview", command=self._preview_selected, width=10).pack(pady=2)

        # Output settings
        settings = ttk.LabelFrame(self.body, text="Output settings", padding=10)
        settings.pack(fill="x", pady=(8, 0))
        ttk.Label(settings, text="Output quality:").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Combobox(
            settings, textvariable=self.resolution_var, state="readonly",
            values=[f"{k} ({w}×{h})" for k, (w, h) in RESOLUTION_PRESETS.items()],
            width=22,
        ).grid(row=0, column=1, sticky="w")
        ttk.Label(
            settings,
            text="(All inputs will be scaled & padded to fit this resolution)",
            foreground="#666",
        ).grid(row=0, column=2, sticky="w", padx=(12, 0))

        # Footer
        ttk.Button(self.footer, text="◀ Back", command=self._show_gallery).pack(side="left")
        ttk.Button(
            self.footer, text="Merge ▶", command=self._start_render, width=18,
        ).pack(side="right")

        self._refresh_reorder_list()

    def _refresh_reorder_list(self):
        self.reorder_list.delete(0, "end")
        for i, p in enumerate(self.ordered_paths, start=1):
            self.reorder_list.insert("end", f"{i:>2}.  {Path(p).name}")

    def _move_selected(self, delta):
        sel = self.reorder_list.curselection()
        if not sel:
            return
        i = sel[0]
        j = i + delta
        if j < 0 or j >= len(self.ordered_paths):
            return
        self.ordered_paths[i], self.ordered_paths[j] = self.ordered_paths[j], self.ordered_paths[i]
        self._refresh_reorder_list()
        self.reorder_list.selection_set(j)
        self.reorder_list.activate(j)

    def _remove_selected(self):
        sel = self.reorder_list.curselection()
        if not sel:
            return
        i = sel[0]
        del self.ordered_paths[i]
        self._refresh_reorder_list()
        if self.ordered_paths:
            new_i = min(i, len(self.ordered_paths) - 1)
            self.reorder_list.selection_set(new_i)

    def _preview_selected(self, _e=None):
        sel = self.reorder_list.curselection()
        if not sel:
            return
        open_in_default_player(self.ordered_paths[sel[0]])

    # Drag-to-reorder bindings: track which index we started on, swap on motion.
    def _drag_start(self, e):
        self._drag_index = self.reorder_list.nearest(e.y)

    def _drag_motion(self, e):
        if not hasattr(self, "_drag_index") or self._drag_index is None:
            return
        i = self._drag_index
        j = self.reorder_list.nearest(e.y)
        if j == i or j < 0 or j >= len(self.ordered_paths):
            return
        self.ordered_paths[i], self.ordered_paths[j] = self.ordered_paths[j], self.ordered_paths[i]
        self._drag_index = j
        self._refresh_reorder_list()
        self.reorder_list.selection_clear(0, "end")
        self.reorder_list.selection_set(j)
        self.reorder_list.activate(j)

    def _drag_end(self, _e):
        self._drag_index = None

    # ---------- Step 3: Render ----------
    def _selected_resolution(self):
        label = self.resolution_var.get()
        key = label.split()[0] if label else DEFAULT_RESOLUTION_KEY
        return RESOLUTION_PRESETS.get(key, RESOLUTION_PRESETS[DEFAULT_RESOLUTION_KEY])

    def _start_render(self):
        if self.is_rendering:
            return
        if len(self.ordered_paths) < 2:
            messagebox.showwarning("Need at least 2", "Pick at least two videos.")
            return
        if not self.ffmpeg:
            messagebox.showerror("FFmpeg missing", "FFmpeg is required to merge videos.")
            return

        self.is_rendering = True
        self._show_render_screen()
        threading.Thread(target=self._render_thread, daemon=True).start()

    def _show_render_screen(self):
        self._clear_body()
        self.header.config(text="Merging videos…")

        info = ttk.Label(
            self.body,
            text=f"Merging {len(self.ordered_paths)} videos into one. This re-encodes everything, so it can take a few minutes.",
            foreground="#555",
            wraplength=820,
        )
        info.pack(anchor="w", pady=(0, 8))

        # Order summary
        order_box = ttk.LabelFrame(self.body, text="Order", padding=8)
        order_box.pack(fill="x", pady=(0, 8))
        for i, p in enumerate(self.ordered_paths, start=1):
            ttk.Label(order_box, text=f"{i:>2}.  {Path(p).name}").pack(anchor="w")

        # Progress + log
        prog_frame = ttk.LabelFrame(self.body, text="Progress", padding=8)
        prog_frame.pack(fill="x", pady=(0, 8))
        prog_frame.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(prog_frame, mode="indeterminate")
        self.progress.grid(row=0, column=0, sticky="ew")
        self.progress.start(15)
        self.status_label = ttk.Label(prog_frame, text="Starting…", foreground="#555")
        self.status_label.grid(row=1, column=0, sticky="w", pady=(6, 0))

        log_frame = ttk.LabelFrame(self.body, text="Log", padding=4)
        log_frame.pack(fill="both", expand=True)
        self.log_text = tk.Text(log_frame, height=10, wrap="word", font=("Consolas", 9))
        self.log_text.pack(fill="both", expand=True)

        ttk.Button(self.footer, text="Close", command=self._on_close).pack(side="right")

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

    def _render_thread(self):
        try:
            out_path = self._do_merge()
        except Exception as e:
            import traceback
            self._log(f"ERROR: {e}")
            self._log(traceback.format_exc())
            self.root.after(0, lambda: self._render_done(None))
            return
        self.root.after(0, lambda: self._render_done(out_path))

    def _do_merge(self):
        _safe_console()
        out_w, out_h = self._selected_resolution()
        self._log(f"Target resolution: {out_w}x{out_h}")

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = HERE / f"merged_{timestamp}.mp4"

        # Build a concat-filter command that scales/pads each input to a
        # common resolution and unifies framerate + audio params, then
        # concatenates. Slower than the demuxer, but always works regardless
        # of mismatched codecs/dims/fps in the inputs.
        n = len(self.ordered_paths)
        cmd = [self.ffmpeg, "-y", "-loglevel", "error", "-stats"]
        for p in self.ordered_paths:
            cmd += ["-i", str(p)]

        filter_parts = []
        concat_inputs = []
        for i in range(n):
            filter_parts.append(
                f"[{i}:v]scale={out_w}:{out_h}:force_original_aspect_ratio=decrease:flags=lanczos,"
                f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2:color=black,"
                f"setsar=1,fps=30,format=yuv420p[v{i}];"
                f"[{i}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[a{i}]"
            )
            concat_inputs.append(f"[v{i}][a{i}]")
        filter_parts.append(
            f"{''.join(concat_inputs)}concat=n={n}:v=1:a=1[outv][outa]"
        )
        filter_complex = ";".join(filter_parts)

        cmd += [
            "-filter_complex", filter_complex,
            "-map", "[outv]", "-map", "[outa]",
            "-c:v", "libx264", "-crf", "18", "-preset", "medium",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
            "-movflags", "+faststart",
            str(out_path),
        ]

        self._log(f"Running ffmpeg with {n} inputs (concat filter)…")
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        # Stream stderr/stdout into the log so the user sees ffmpeg's progress lines
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                self._log(line)
        proc.wait()
        if proc.returncode != 0 or not out_path.exists():
            self._log(f"ffmpeg exit code {proc.returncode}")
            return None
        return out_path

    def _render_done(self, out_path):
        self.is_rendering = False
        try:
            self.progress.stop()
        except Exception:
            pass
        if out_path and Path(out_path).exists():
            size_mb = Path(out_path).stat().st_size / (1024 * 1024)
            self.status_label.config(text=f"Done → {out_path.name} ({size_mb:.1f} MB)")
            self.progress["mode"] = "determinate"
            self.progress["value"] = 100
            messagebox.showinfo(
                "Merge complete",
                f"Saved to:\n{out_path}\n\nClick OK to keep the merger open, or close the window.",
            )
        else:
            self.status_label.config(text="Failed — see log")
            messagebox.showerror("Merge failed", "Could not produce a merged file. Check the log for details.")


def main():
    root = tk.Tk()
    try:
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
    except Exception:
        pass
    VideoMergerApp(root)
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
