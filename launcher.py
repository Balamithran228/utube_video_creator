#!/usr/bin/env python3
"""
Front-page launcher for the YouTube video toolkit.

Pick what you want to do, click Continue, and the matching tool opens
in a fresh window. The launcher closes once you continue.

Run:
    python launcher.py
"""

import os
import sys
import subprocess
import tkinter as tk
from tkinter import ttk, messagebox
from pathlib import Path

HERE = Path(__file__).resolve().parent

TOOLS = [
    {
        "key": "tone",
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


class LauncherApp:
    def __init__(self, root):
        self.root = root
        self.root.title("YouTube video toolkit")
        # Sized to comfortably fit all current tool cards + title + button row.
        # The button row is anchored to the bottom (packed first below) so it
        # stays visible even if the window is squeezed smaller.
        self.root.geometry("760x680")
        self.root.minsize(680, 600)

        self.choice = tk.StringVar(value=TOOLS[0]["key"])

        outer = ttk.Frame(self.root, padding=20)
        outer.pack(fill="both", expand=True)

        # Header
        ttk.Label(
            outer,
            text="What would you like to do?",
            font=("Segoe UI", 14, "bold"),
        ).pack(anchor="w", pady=(0, 4))
        ttk.Label(
            outer,
            text="Pick one option below, then click Continue.",
            foreground="#555",
        ).pack(anchor="w", pady=(0, 10))

        # IMPORTANT: pack the bottom button row FIRST with side="bottom" so it's
        # always anchored to the bottom of the window. Without this, packing it
        # after the cards (which expand naturally) can push the Continue button
        # off-screen when there are many cards or the window is short.
        btn_row = ttk.Frame(outer)
        btn_row.pack(side="bottom", fill="x", pady=(16, 0))
        ttk.Button(
            btn_row, text="📂 Open workspace folder",
            command=self._open_workspace,
        ).pack(side="left")
        ttk.Button(
            btn_row, text="Continue ▶", command=self._continue, width=20,
        ).pack(side="right")

        # Cards fill the space between the header and the bottom button row.
        cards_frame = ttk.Frame(outer)
        cards_frame.pack(side="top", fill="both", expand=True)
        for tool in TOOLS:
            self._build_tool_card(cards_frame, tool)

        # Allow Enter to act as Continue
        self.root.bind("<Return>", lambda _e: self._continue())

    def _build_tool_card(self, parent, tool):
        card = ttk.LabelFrame(parent, padding=10)
        card.pack(fill="x", pady=4)
        ttk.Radiobutton(
            card,
            text=tool["title"],
            variable=self.choice,
            value=tool["key"],
        ).pack(anchor="w")
        ttk.Label(
            card,
            text=tool["subtitle"],
            foreground="#555",
            wraplength=620,
            justify="left",
        ).pack(anchor="w", padx=(20, 0), pady=(2, 0))

    def _continue(self):
        chosen = next(t for t in TOOLS if t["key"] == self.choice.get())
        script = HERE / chosen["script"]
        if not script.is_file():
            messagebox.showerror(
                "Tool missing",
                f"Could not find {chosen['script']} in:\n{HERE}",
            )
            return
        cmd = [sys.executable, str(script), *chosen["args"]]
        try:
            subprocess.Popen(cmd, cwd=str(HERE))
        except Exception as e:
            messagebox.showerror("Launch failed", f"Could not launch tool:\n{e}")
            return
        # Close the launcher once the tool is launched.
        self.root.destroy()

    def _open_workspace(self):
        if sys.platform == "win32":
            os.startfile(str(HERE))
        elif sys.platform == "darwin":
            subprocess.run(["open", str(HERE)])
        else:
            subprocess.run(["xdg-open", str(HERE)])


def main():
    root = tk.Tk()
    try:
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
    except Exception:
        pass
    LauncherApp(root)
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
