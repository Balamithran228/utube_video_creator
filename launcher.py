#!/usr/bin/env python3
"""
Front-page launcher for the YouTube video toolkit.

Modern dark/light themed launcher. Click a tool card to select it,
then click Continue (or double-click the card) to open it. The
launcher closes once the chosen tool is launched.

Run:
    python launcher.py
"""

import os
import sys
import subprocess
from pathlib import Path

import customtkinter as ctk

HERE = Path(__file__).resolve().parent

TOOLS = [
    {
        "key": "tone",
        "icon": "🎵",
        "title": "Create video — Tone markers (recommended)",
        "subtitle": (
            "For new recordings where you played a 1020 Hz tone burst between "
            "segments. Splits the audio at every tone, pairs each segment with "
            "an image, and renders a 1080p (or higher) MP4."
        ),
        "script": "tone_video_creator.py",
        "args": [],
    },
    {
        "key": "legacy",
        "icon": "🎤",
        "title": "Create video — Voice pauses (legacy)",
        "subtitle": (
            "Older approach for recordings without tone markers. Splits at "
            "long silences in the audio. Use this only if you have a recording "
            "you can't redo with tones."
        ),
        "script": "simple_video_creator.py",
        "args": [],
    },
    {
        "key": "merge",
        "icon": "🎬",
        "title": "Merge / browse videos",
        "subtitle": (
            "See every MP4 in this workspace as a thumbnail gallery. Tick the "
            "ones you want, drag them into the order you want, and combine them "
            "into a single output."
        ),
        "script": "video_merger.py",
        "args": [],
    },
    {
        "key": "voice_edit",
        "icon": "✂️",
        "title": "Trim & clean a voice recording",
        "subtitle": (
            "Load any voice file, paint cut regions on the waveform to remove "
            "silences, mistakes, or breaths anywhere in the audio, and export "
            "a clean continuous WAV or MP3 with the cuts removed."
        ),
        "script": "voice_editor.py",
        "args": [],
    },
    {
        "key": "mp3",
        "icon": "🔊",
        "title": "Convert video/audio → MP3",
        "subtitle": (
            "Upload any video (mp4/mkv/mov/…) or audio file (m4a/wav/flac/…), "
            "pick where to save it, and get an MP3 back. Useful for stripping "
            "the audio out of a finished YouTube video."
        ),
        "script": "audio_converter.py",
        "args": [],
    },
]


# Same palette as voice_editor.py so the toolkit looks like one product.
THEMES = {
    "dark": {
        "appearance": "dark",
        "bg": "#0F1117",
        "panel": "#181B25",
        "panel2": "#222637",
        "stroke": "#2C3145",
        "text": "#F5F7FA",
        "muted": "#8B92A6",
        "accent": "#7C5CFF",       # violet — selected card border, primary button
        "accent2": "#22D3EE",      # cyan — hover, accent labels
        "accent3": "#F472B6",      # pink — emphasis
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


class ToolCard(ctk.CTkFrame):
    """Selectable card for a single tool. Click to select, double-click to launch."""

    def __init__(self, parent, theme, tool, on_select, on_launch, **kwargs):
        super().__init__(
            parent,
            fg_color=theme["panel2"],
            corner_radius=14,
            border_width=2,
            border_color=theme["stroke"],
            **kwargs,
        )
        self.theme = theme
        self.tool = tool
        self.on_select = on_select
        self.on_launch = on_launch
        self._selected = False
        self._hover = False

        # Layout: icon on left, text block on right, chevron on far right
        self.grid_columnconfigure(1, weight=1)

        self.icon_label = ctk.CTkLabel(
            self, text=tool["icon"],
            font=ctk.CTkFont("Segoe UI Emoji", 28),
            text_color=theme["accent"],
            width=56,
        )
        self.icon_label.grid(row=0, column=0, rowspan=2, padx=(16, 8), pady=14, sticky="n")

        self.title_label = ctk.CTkLabel(
            self, text=tool["title"],
            font=ctk.CTkFont("Segoe UI", 14, "bold"),
            text_color=theme["text"],
            anchor="w", justify="left",
        )
        self.title_label.grid(row=0, column=1, padx=(0, 12), pady=(14, 0), sticky="ew")

        self.subtitle_label = ctk.CTkLabel(
            self, text=tool["subtitle"],
            font=ctk.CTkFont("Segoe UI", 11),
            text_color=theme["muted"],
            anchor="w", justify="left",
            wraplength=560,
        )
        self.subtitle_label.grid(row=1, column=1, padx=(0, 12), pady=(2, 14), sticky="ew")

        self.chevron = ctk.CTkLabel(
            self, text="›",
            font=ctk.CTkFont("Segoe UI", 22, "bold"),
            text_color=theme["muted"],
            width=24,
        )
        self.chevron.grid(row=0, column=2, rowspan=2, padx=(0, 16), pady=14, sticky="e")

        # Forward clicks anywhere on the card to selection
        for w in (self, self.icon_label, self.title_label, self.subtitle_label, self.chevron):
            w.bind("<Button-1>", self._on_click)
            w.bind("<Double-Button-1>", self._on_double_click)
            w.bind("<Enter>", self._on_enter)
            w.bind("<Leave>", self._on_leave)
            try:
                w.configure(cursor="hand2")
            except Exception:
                pass

    def set_selected(self, selected: bool):
        self._selected = selected
        self._restyle()

    def _on_click(self, _e):
        self.on_select(self.tool["key"])

    def _on_double_click(self, _e):
        self.on_launch(self.tool["key"])

    def _on_enter(self, _e):
        self._hover = True
        self._restyle()

    def _on_leave(self, _e):
        self._hover = False
        self._restyle()

    def _restyle(self):
        t = self.theme
        if self._selected:
            self.configure(border_color=t["accent"], fg_color=t["panel"])
            self.chevron.configure(text_color=t["accent"], text="▶")
            self.icon_label.configure(text_color=t["accent"])
        elif self._hover:
            self.configure(border_color=t["accent2"], fg_color=t["panel2"])
            self.chevron.configure(text_color=t["accent2"], text="›")
            self.icon_label.configure(text_color=t["accent2"])
        else:
            self.configure(border_color=t["stroke"], fg_color=t["panel2"])
            self.chevron.configure(text_color=t["muted"], text="›")
            self.icon_label.configure(text_color=t["accent"])


class LauncherApp:
    def __init__(self, root: ctk.CTk):
        self.root = root
        self.theme_name = "dark"
        self.theme = THEMES[self.theme_name]
        ctk.set_appearance_mode(self.theme["appearance"])
        ctk.set_default_color_theme("blue")

        self.root.title("YouTube Video Toolkit")
        self.root.geometry("820x780")
        self.root.minsize(720, 660)
        self.root.configure(fg_color=self.theme["bg"])

        self.selected_key = ctk.StringVar(value=TOOLS[0]["key"])
        self.cards: dict[str, ToolCard] = {}

        self._build_ui()
        self._select(TOOLS[0]["key"])

        # Keyboard shortcuts
        self.root.bind("<Return>", lambda _e: self._continue())
        self.root.bind("<Down>", lambda _e: self._move_selection(1))
        self.root.bind("<Up>", lambda _e: self._move_selection(-1))
        self.root.bind("<Escape>", lambda _e: self.root.destroy())

    # ─────────────────────── UI build ───────────────────────

    def _build_ui(self):
        t = self.theme

        # Top bar
        topbar = ctk.CTkFrame(
            self.root, fg_color=t["panel"], corner_radius=14,
            border_width=1, border_color=t["stroke"],
        )
        topbar.pack(fill="x", padx=14, pady=(14, 8))

        title_box = ctk.CTkFrame(topbar, fg_color="transparent")
        title_box.pack(side="left", padx=16, pady=10, fill="y")
        ctk.CTkLabel(
            title_box, text="YouTube Video Toolkit",
            font=ctk.CTkFont("Segoe UI", 20, "bold"),
            text_color=t["accent"],
            anchor="w",
        ).pack(anchor="w")
        ctk.CTkLabel(
            title_box, text="Pick a tool below, then click Continue.",
            font=ctk.CTkFont("Segoe UI", 11),
            text_color=t["muted"],
            anchor="w",
        ).pack(anchor="w")

        self.theme_btn = ctk.CTkButton(
            topbar, text=("☀  Light mode" if self.theme_name == "dark" else "🌙  Dark mode"),
            width=128, command=self._toggle_theme,
            fg_color=t["panel2"], hover_color=t["stroke"],
            text_color=t["text"], corner_radius=10,
        )
        self.theme_btn.pack(side="right", padx=(8, 16), pady=10)

        ctk.CTkButton(
            topbar, text="📂  Open workspace", width=160,
            command=self._open_workspace,
            fg_color=t["panel2"], hover_color=t["stroke"],
            text_color=t["text"], corner_radius=10,
        ).pack(side="right", padx=8, pady=10)

        # Bottom button row — packed BEFORE cards with side="bottom" so it's
        # always anchored to the bottom regardless of how many cards we add.
        btn_row = ctk.CTkFrame(self.root, fg_color="transparent")
        btn_row.pack(side="bottom", fill="x", padx=14, pady=(0, 14))

        self.continue_btn = ctk.CTkButton(
            btn_row, text="Continue  ▶", width=180, height=44,
            command=self._continue,
            fg_color=t["accent"], hover_color=hex_lerp(t["accent"], "#000000", 0.2),
            text_color="#FFFFFF", corner_radius=12,
            font=ctk.CTkFont("Segoe UI", 14, "bold"),
        )
        self.continue_btn.pack(side="right", padx=4)

        self.hint_label = ctk.CTkLabel(
            btn_row,
            text="Click a card to select • Double-click to launch • Enter = Continue",
            text_color=t["muted"],
            font=ctk.CTkFont("Segoe UI", 10),
        )
        self.hint_label.pack(side="left", padx=4)

        # Scrollable cards area between top bar and bottom button row
        cards_container = ctk.CTkScrollableFrame(
            self.root, fg_color=t["bg"], corner_radius=0,
            scrollbar_button_color=t["panel2"],
            scrollbar_button_hover_color=t["accent"],
        )
        cards_container.pack(fill="both", expand=True, padx=14, pady=(0, 8))

        self.cards = {}
        for tool in TOOLS:
            card = ToolCard(
                cards_container, theme=t, tool=tool,
                on_select=self._select,
                on_launch=self._launch_key,
            )
            card.pack(fill="x", padx=4, pady=6)
            self.cards[tool["key"]] = card

    # ─────────────────────── Selection ───────────────────────

    def _select(self, key: str):
        self.selected_key.set(key)
        for k, card in self.cards.items():
            card.set_selected(k == key)

    def _move_selection(self, delta: int):
        keys = [t["key"] for t in TOOLS]
        try:
            idx = keys.index(self.selected_key.get())
        except ValueError:
            idx = 0
        new_idx = max(0, min(len(keys) - 1, idx + delta))
        self._select(keys[new_idx])

    # ─────────────────────── Theme toggle ───────────────────────

    def _toggle_theme(self):
        self.theme_name = "light" if self.theme_name == "dark" else "dark"
        self.theme = THEMES[self.theme_name]
        ctk.set_appearance_mode(self.theme["appearance"])
        # Re-style by rebuilding the UI — keeps things simple and reliable.
        prev_selected = self.selected_key.get()
        for child in self.root.winfo_children():
            child.destroy()
        self.root.configure(fg_color=self.theme["bg"])
        self._build_ui()
        self._select(prev_selected)

    # ─────────────────────── Launching ───────────────────────

    def _continue(self):
        self._launch_key(self.selected_key.get())

    def _launch_key(self, key: str):
        chosen = next((t for t in TOOLS if t["key"] == key), None)
        if chosen is None:
            return
        script = HERE / chosen["script"]
        if not script.is_file():
            self._show_error(
                "Tool missing",
                f"Could not find {chosen['script']} in:\n{HERE}",
            )
            return
        cmd = [sys.executable, str(script), *chosen["args"]]
        try:
            subprocess.Popen(cmd, cwd=str(HERE))
        except Exception as e:
            self._show_error("Launch failed", f"Could not launch tool:\n{e}")
            return
        self.root.destroy()

    def _show_error(self, title: str, message: str):
        # customtkinter doesn't ship a messagebox; fall back to tkinter.
        from tkinter import messagebox
        messagebox.showerror(title, message)

    def _open_workspace(self):
        if sys.platform == "win32":
            os.startfile(str(HERE))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(HERE)])
        else:
            subprocess.Popen(["xdg-open", str(HERE)])


def main():
    root = ctk.CTk()
    LauncherApp(root)
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
