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
from tkinter import messagebox, filedialog

import customtkinter as ctk

from PIL import Image, ImageTk
try:
    from .paths import OUTPUT_DIR, PROJECT_ROOT, ensure_output_dir
except ImportError:
    from paths import OUTPUT_DIR, PROJECT_ROOT, ensure_output_dir

# Reuse helpers from the existing pipeline scripts
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from simple_video_creator import find_ffmpeg, configure_pydub, _safe_console  # noqa: E402
WORKSPACE = PROJECT_ROOT

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
        self.root.geometry("940x720")
        self.root.minsize(820, 600)

        self.T = THEMES["dark"]
        self.root.configure(fg_color=self.T["bg"])

        self.ffmpeg = find_ffmpeg()
        configure_pydub(self.ffmpeg)
        self.ffprobe = find_ffprobe(self.ffmpeg)

        self._thumb_cache = {}
        self._thumb_dir = Path(tempfile.mkdtemp(prefix="vm_thumbs_"))

        self.videos = []
        self.ordered_paths = []

        self.resolution_var = tk.StringVar(
            value=f"{DEFAULT_RESOLUTION_KEY} ({RESOLUTION_PRESETS[DEFAULT_RESOLUTION_KEY][0]}×{RESOLUTION_PRESETS[DEFAULT_RESOLUTION_KEY][1]})"
        )

        self.is_rendering = False
        self.log_queue = queue.Queue()

        T = self.T
        self.outer = ctk.CTkFrame(self.root, fg_color=T["bg"])
        self.outer.pack(fill="both", expand=True, padx=14, pady=14)

        self.header = ctk.CTkLabel(
            self.outer, text="Step 1 of 2 — Pick the videos to merge",
            font=ctk.CTkFont("Segoe UI", 15, weight="bold"), text_color=T["text"],
        )
        self.header.pack(anchor="w", pady=(0, 10))

        self.body = ctk.CTkFrame(self.outer, fg_color="transparent")
        self.body.pack(fill="both", expand=True)

        self.footer = ctk.CTkFrame(self.outer, fg_color="transparent")
        self.footer.pack(fill="x", pady=(10, 0))

        self._poll_log()
        self._show_gallery()
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
        scan_dirs = [OUTPUT_DIR, WORKSPACE]
        seen = set()
        candidates = []
        for folder in scan_dirs:
            if not folder.is_dir():
                continue
            for p in folder.iterdir():
                if p not in seen:
                    seen.add(p)
                    candidates.append(p)
        for p in sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True):
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
        placeholder = Image.new("RGB", (THUMB_W, THUMB_H), color="#222637")
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
        T = self.T
        self._clear_body()
        self.header.configure(text="Step 1 of 2 — Pick the videos to merge")

        if not self.ffmpeg:
            messagebox.showerror("FFmpeg missing",
                                  "FFmpeg is required for video metadata + thumbnails.")

        # Toolbar
        bar = ctk.CTkFrame(self.body, fg_color="transparent")
        bar.pack(fill="x", pady=(0, 10))
        ctk.CTkButton(bar, text="🔄  Refresh", command=self._refresh_gallery, width=110,
                       fg_color=T["panel2"], hover_color=T["stroke"], text_color=T["text"],
                       border_width=1, border_color=T["stroke"]).pack(side="left")
        ctk.CTkButton(bar, text="➕  Add file from elsewhere…", command=self._add_external_file,
                       width=210, fg_color=T["panel2"], hover_color=T["stroke"],
                       text_color=T["text"], border_width=1, border_color=T["stroke"],
                       ).pack(side="left", padx=(10, 0))
        self.gallery_summary = ctk.CTkLabel(bar, text="", text_color=T["muted"],
                                             font=ctk.CTkFont("Segoe UI", 11))
        self.gallery_summary.pack(side="right")

        # Scrollable gallery grid
        canvas_holder = ctk.CTkFrame(self.body, fg_color="transparent")
        canvas_holder.pack(fill="both", expand=True)
        self.gallery_inner = ctk.CTkScrollableFrame(
            canvas_holder, fg_color=T["bg"],
            scrollbar_fg_color=T["panel"], scrollbar_button_color=T["stroke"],
            scrollbar_button_hover_color=T["accent"],
        )
        self.gallery_inner.pack(fill="both", expand=True)

        # Footer
        self.continue_btn = ctk.CTkButton(
            self.footer, text="Continue  ▶", command=self._continue_to_reorder, width=160,
            fg_color=T["accent"], hover_color=hex_lerp(T["accent"], "#FFFFFF", 0.15),
            text_color="#FFFFFF", font=ctk.CTkFont("Segoe UI", 13, weight="bold"),
            state="disabled",
        )
        self.continue_btn.pack(side="right")
        ctk.CTkButton(
            self.footer, text="📂  Open workspace",
            command=lambda: open_in_default_player(HERE), width=170,
            fg_color=T["panel2"], hover_color=T["stroke"],
            text_color=T["text"], border_width=1, border_color=T["stroke"],
        ).pack(side="left")
        self.selected_count_label = ctk.CTkLabel(
            self.footer, text="0 selected", text_color=T["muted"],
            font=ctk.CTkFont("Segoe UI", 11),
        )
        self.selected_count_label.pack(side="right", padx=(0, 14))

        self._refresh_gallery()

    def _refresh_gallery(self):
        self.videos = self._scan_workspace()
        for child in self.gallery_inner.winfo_children():
            child.destroy()

        if not self.videos:
            ctk.CTkLabel(
                self.gallery_inner,
                text="No video files found in the workspace yet.\nCreate one with the tone or legacy creator first.",
                text_color=self.T["muted"], font=ctk.CTkFont("Segoe UI", 13), justify="center",
            ).grid(row=0, column=0, padx=20, pady=40, columnspan=GALLERY_COLS)
            self.gallery_summary.configure(text="0 videos")
            self._update_continue_state()
            return

        for i, v in enumerate(self.videos):
            r, c = divmod(i, GALLERY_COLS)
            self._build_gallery_card(self.gallery_inner, v).grid(
                row=r, column=c, padx=8, pady=8, sticky="nw",
            )

        total_size = sum(v["size"] for v in self.videos)
        self.gallery_summary.configure(text=f"{len(self.videos)} videos · {fmt_size(total_size)}")
        self._update_continue_state()

    def _build_gallery_card(self, parent, video):
        T = self.T
        card = ctk.CTkFrame(parent, fg_color=T["panel"], corner_radius=12,
                             border_width=1, border_color=T["stroke"])

        thumb = self._get_thumb(video["path"])
        thumb_label = tk.Label(card, image=thumb, cursor="hand2", bd=0, bg=T["panel"])
        thumb_label.image = thumb
        thumb_label.pack(padx=8, pady=(8, 4))
        thumb_label.bind("<Button-1>", lambda _e, p=video["path"]: open_in_default_player(p))

        name = video["name"]
        if len(name) > 30:
            name = name[:27] + "…"
        ctk.CTkLabel(card, text=name, font=ctk.CTkFont("Segoe UI", 10, weight="bold"),
                      text_color=T["text"], wraplength=196).pack(anchor="w", padx=8, pady=(0, 2))
        ctk.CTkLabel(
            card,
            text=f"{fmt_duration(video['duration'])}  ·  {fmt_size(video['size'])}",
            text_color=T["muted"], font=ctk.CTkFont("Segoe UI", 10),
        ).pack(anchor="w", padx=8)
        ctk.CTkCheckBox(
            card, text="Add to merge", variable=video["selected"],
            command=self._update_continue_state,
            text_color=T["text"], fg_color=T["accent"], hover_color=T["accent2"],
            font=ctk.CTkFont("Segoe UI", 11),
        ).pack(anchor="w", padx=8, pady=(6, 10))
        return card

    def _update_continue_state(self):
        T = self.T
        n = sum(1 for v in self.videos if v["selected"].get())
        self.selected_count_label.configure(text=f"{n} selected")
        if n >= 2:
            self.continue_btn.configure(
                state="normal", fg_color=T["accent"],
                text_color="#FFFFFF",
            )
        else:
            self.continue_btn.configure(
                state="disabled", fg_color=T["panel2"],
                text_color=T["muted"],
            )

    def _add_external_file(self):
        f = filedialog.askopenfilename(
            title="Pick a video to add",
            initialdir=str(HERE),
            filetypes=[("Video", " ".join(f"*{e}" for e in sorted(VIDEO_EXTS))), ("All files", "*.*")],
        )
        if not f:
            return
        p = Path(f)
        try:
            size = p.stat().st_size
        except OSError:
            return
        self.videos.append({
            "path": p, "name": p.name, "size": size,
            "duration": get_video_duration_s(self.ffprobe, p),
            "selected": tk.BooleanVar(value=True),
        })
        for child in self.gallery_inner.winfo_children():
            child.destroy()
        for i, v in enumerate(self.videos):
            r, c = divmod(i, GALLERY_COLS)
            self._build_gallery_card(self.gallery_inner, v).grid(
                row=r, column=c, padx=8, pady=8, sticky="nw",
            )
        self.gallery_summary.configure(
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
        T = self.T
        self._clear_body()
        self.header.configure(text="Step 2 of 2 — Drag the videos into the order you want")

        ctk.CTkLabel(
            self.body,
            text="Top plays first, bottom plays last.  Drag a row to reorder, or use ▲ ▼ buttons.  ✕ removes a row.",
            text_color=T["muted"], font=ctk.CTkFont("Segoe UI", 12), wraplength=820, justify="left",
        ).pack(anchor="w", pady=(0, 10))

        # Listbox + side buttons
        body = ctk.CTkFrame(self.body, fg_color="transparent")
        body.pack(fill="both", expand=True)

        list_wrap = ctk.CTkFrame(body, fg_color=T["panel"], corner_radius=12,
                                  border_width=1, border_color=T["stroke"])
        list_wrap.pack(side="left", fill="both", expand=True)
        self.reorder_list = tk.Listbox(
            list_wrap, font=("Segoe UI", 11), activestyle="none", selectmode="single",
            bg=T["panel2"], fg=T["text"],
            selectbackground=T["accent"], selectforeground="#FFFFFF",
            relief="flat", borderwidth=0,
            highlightthickness=1, highlightcolor=T["stroke"], highlightbackground=T["panel"],
        )
        self.reorder_list.pack(side="left", fill="both", expand=True, padx=8, pady=8)
        rsb = ctk.CTkScrollbar(list_wrap, orientation="vertical",
                                command=self.reorder_list.yview,
                                fg_color=T["panel"], button_color=T["stroke"],
                                button_hover_color=T["accent"])
        rsb.pack(side="right", fill="y", pady=8)
        self.reorder_list.configure(yscrollcommand=rsb.set)

        self.reorder_list.bind("<Button-1>", self._drag_start)
        self.reorder_list.bind("<B1-Motion>", self._drag_motion)
        self.reorder_list.bind("<ButtonRelease-1>", self._drag_end)
        self.reorder_list.bind("<Double-Button-1>", self._preview_selected)

        side = ctk.CTkFrame(body, fg_color="transparent")
        side.pack(side="right", fill="y", padx=(12, 0))
        _btn = dict(width=110, fg_color=T["panel2"], hover_color=T["stroke"],
                    text_color=T["text"], border_width=1, border_color=T["stroke"])
        ctk.CTkButton(side, text="▲  Up", command=lambda: self._move_selected(-1), **_btn).pack(pady=3)
        ctk.CTkButton(side, text="▼  Down", command=lambda: self._move_selected(1), **_btn).pack(pady=3)
        ctk.CTkButton(side, text="✕  Remove", command=self._remove_selected,
                       **{**_btn, "hover_color": T["danger"]}).pack(pady=3)
        ctk.CTkButton(side, text="▶  Preview", command=self._preview_selected, **_btn).pack(pady=3)

        # Output settings
        sec_out = ctk.CTkFrame(self.body, fg_color=T["panel"], corner_radius=12,
                                border_width=1, border_color=T["stroke"])
        sec_out.pack(fill="x", pady=(10, 0))
        ctk.CTkLabel(sec_out, text="Output settings",
                      font=ctk.CTkFont("Segoe UI", 11, weight="bold"),
                      text_color=T["accent"]).pack(anchor="w", padx=14, pady=(10, 4))
        so_inner = ctk.CTkFrame(sec_out, fg_color="transparent")
        so_inner.pack(fill="x", padx=12, pady=(0, 12))
        ctk.CTkLabel(so_inner, text="Output quality:", text_color=T["text"],
                      font=ctk.CTkFont("Segoe UI", 11)).pack(side="left", padx=(0, 10))
        ctk.CTkComboBox(
            so_inner, variable=self.resolution_var, state="readonly",
            values=[f"{k} ({w}×{h})" for k, (w, h) in RESOLUTION_PRESETS.items()],
            width=240, fg_color=T["panel2"], border_color=T["stroke"], text_color=T["text"],
            button_color=T["stroke"], button_hover_color=T["accent"],
            dropdown_fg_color=T["panel2"], dropdown_text_color=T["text"],
            dropdown_hover_color=T["accent"],
        ).pack(side="left", padx=(0, 12))
        ctk.CTkLabel(so_inner, text="(All inputs will be scaled & padded to fit)",
                      text_color=T["muted"], font=ctk.CTkFont("Segoe UI", 11)).pack(side="left")

        # Footer
        ctk.CTkButton(
            self.footer, text="◀  Back", command=self._show_gallery, width=110,
            fg_color=T["panel2"], hover_color=T["stroke"],
            text_color=T["text"], border_width=1, border_color=T["stroke"],
        ).pack(side="left")
        ctk.CTkButton(
            self.footer, text="Merge  ▶", command=self._start_render, width=150,
            fg_color=T["accent"], hover_color=hex_lerp(T["accent"], "#FFFFFF", 0.15),
            text_color="#FFFFFF", font=ctk.CTkFont("Segoe UI", 13, weight="bold"),
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
        T = self.T
        self._clear_body()
        self.header.configure(text="Merging videos…")

        ctk.CTkLabel(
            self.body,
            text=f"Merging {len(self.ordered_paths)} videos into one. This re-encodes everything — may take a few minutes.",
            text_color=T["muted"], font=ctk.CTkFont("Segoe UI", 12), wraplength=840, justify="left",
        ).pack(anchor="w", pady=(0, 10))

        # Order summary
        order_sec = ctk.CTkFrame(self.body, fg_color=T["panel"], corner_radius=12,
                                  border_width=1, border_color=T["stroke"])
        order_sec.pack(fill="x", pady=(0, 8))
        ctk.CTkLabel(order_sec, text="Order",
                      font=ctk.CTkFont("Segoe UI", 11, weight="bold"),
                      text_color=T["accent"]).pack(anchor="w", padx=14, pady=(10, 4))
        for i, p in enumerate(self.ordered_paths, start=1):
            ctk.CTkLabel(order_sec, text=f"  {i:>2}.   {Path(p).name}",
                          text_color=T["text"], font=ctk.CTkFont("Segoe UI", 11),
                          anchor="w").pack(anchor="w", padx=14)
        ctk.CTkFrame(order_sec, fg_color="transparent", height=8).pack()

        # Progress
        prog_sec = ctk.CTkFrame(self.body, fg_color=T["panel"], corner_radius=12,
                                 border_width=1, border_color=T["stroke"])
        prog_sec.pack(fill="x", pady=(0, 8))
        ctk.CTkLabel(prog_sec, text="Progress",
                      font=ctk.CTkFont("Segoe UI", 11, weight="bold"),
                      text_color=T["accent"]).pack(anchor="w", padx=14, pady=(10, 4))
        p_inner = ctk.CTkFrame(prog_sec, fg_color="transparent")
        p_inner.pack(fill="x", padx=12, pady=(0, 12))
        self.progress = ctk.CTkProgressBar(
            p_inner, progress_color=T["accent"], fg_color=T["panel2"], height=14, corner_radius=7,
        )
        self.progress.set(0.15)
        self.progress.pack(fill="x", pady=(0, 6))
        self.status_label = ctk.CTkLabel(
            p_inner, text="Starting…", text_color=T["muted"],
            font=ctk.CTkFont("Segoe UI", 11), anchor="w",
        )
        self.status_label.pack(anchor="w")

        # Log
        log_sec = ctk.CTkFrame(self.body, fg_color=T["panel"], corner_radius=12,
                                border_width=1, border_color=T["stroke"])
        log_sec.pack(fill="both", expand=True)
        ctk.CTkLabel(log_sec, text="Log",
                      font=ctk.CTkFont("Segoe UI", 11, weight="bold"),
                      text_color=T["accent"]).pack(anchor="w", padx=14, pady=(10, 4))
        self.log_text = ctk.CTkTextbox(
            log_sec, font=ctk.CTkFont("Consolas", 10),
            fg_color=T["panel2"], text_color=T["text"], wrap="word", corner_radius=8,
        )
        self.log_text.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        ctk.CTkButton(
            self.footer, text="Close", command=self._on_close, width=110,
            fg_color=T["panel2"], hover_color=T["stroke"],
            text_color=T["text"], border_width=1, border_color=T["stroke"],
        ).pack(side="right")

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
                    if hasattr(self, "status_label") and self.status_label.winfo_exists():
                        self.status_label.configure(text=msg.split("] ", 1)[-1][:80])
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
        out_path = ensure_output_dir() / f"merged_{timestamp}.mp4"

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
        if out_path and Path(out_path).exists():
            size_mb = Path(out_path).stat().st_size / (1024 * 1024)
            try:
                self.status_label.configure(text=f"Done → {out_path.name} ({size_mb:.1f} MB)")
                self.progress.set(1.0)
                self.progress.configure(progress_color=self.T["ok"])
            except Exception:
                pass
            messagebox.showinfo(
                "Merge complete",
                f"Saved to:\n{out_path}\n\nClick OK to keep the merger open, or close the window.",
            )
        else:
            try:
                self.status_label.configure(text="Failed — see log")
                self.progress.configure(progress_color=self.T["danger"])
            except Exception:
                pass
            messagebox.showerror("Merge failed", "Could not produce a merged file. Check the log for details.")


def main():
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    root = ctk.CTk()
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
