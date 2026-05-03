# Running this project on macOS

This guide gets the toolkit working on macOS. Two paths — pick whichever you're comfortable with:

- **[Option A — `uv`](#option-a--uv-recommended)** — modern, fast (10–100× faster than pip), isolated per-project. Recommended.
- **[Option B — system Python + pip](#option-b--system-python--pip)** — traditional. Works fine if you don't want another tool.

Both paths require **Homebrew** + **FFmpeg** + a recent **Python**. The pipelines work identically on Mac and Windows; only the install steps differ.

---

## Prerequisites (both paths)

### 1. Install Homebrew

If you don't have it already:

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

Verify:

```bash
brew --version
```

### 2. Install FFmpeg

The pipeline shells out to `ffmpeg` and `ffprobe` for every render, so this is non-negotiable.

```bash
brew install ffmpeg
```

Verify:

```bash
ffmpeg -version
ffprobe -version
which ffmpeg     # should print /opt/homebrew/bin/ffmpeg (Apple Silicon) or /usr/local/bin/ffmpeg (Intel)
```

### 3. Clone the repo

```bash
git clone https://github.com/Balamithran228/utube_video_creator.git
cd utube_video_creator
```

---

## Option A — `uv` (recommended)

[`uv`](https://github.com/astral-sh/uv) is a drop-in replacement for `pip` + `venv` written in Rust. It's much faster, manages Python versions for you, and keeps each project's dependencies isolated.

### Install uv

```bash
brew install uv
```

Verify:

```bash
uv --version
```

### Set up the project

```bash
cd utube_video_creator

# Create an isolated environment in .venv/ (uv picks an appropriate Python version)
uv venv

# Activate it (your shell prompt will gain a (.venv) prefix)
source .venv/bin/activate

# Install dependencies into the venv
uv pip install -r requirements.txt
```

### Run

```bash
python launcher.py
```

The launcher window opens. Pick a tool, click Continue.

### Run without activating the venv

If you don't want to remember to `source .venv/bin/activate`, use `uv run`:

```bash
uv run python launcher.py
```

`uv run` automatically uses the project's `.venv`.

### Adding the optional VAD dependency

Only needed if you'll use the legacy `simple_video_creator.py --vad` pipeline (Approach 1 — voice-pause detection). Skip otherwise:

```bash
uv pip install silero-vad torch
```

> **Apple Silicon (M1/M2/M3 Macs):** the standard `torch` wheel from PyPI is already arm64-native since torch 2.0. No special index URL needed.

---

## Option B — system Python + pip

### Install Python (with tkinter)

macOS ships with a Python, but for GUI apps you want a fresh Homebrew install. **Important:** the GUI uses `tkinter`, which on Homebrew Python is a separate formula.

```bash
brew install python@3.11
brew install python-tk@3.11
```

Verify (and confirm tkinter loads):

```bash
python3.11 --version
python3.11 -c "import tkinter; tkinter.Tk().destroy(); print('tkinter OK')"
```

### Set up the project (with a venv — recommended)

Even outside of `uv`, an isolated venv keeps the project's dependencies from polluting your global Python:

```bash
cd utube_video_creator

# Create the venv
python3.11 -m venv .venv

# Activate it
source .venv/bin/activate

# Install dependencies
pip install --upgrade pip
pip install -r requirements.txt
```

### Or install globally (not recommended)

If you really want everything on system Python with no venv:

```bash
pip3.11 install -r requirements.txt
```

### Run

```bash
python launcher.py
```

(Or `python3.11 launcher.py` if you skipped the venv and your default `python` points elsewhere.)

### Adding the optional VAD dependency

```bash
pip install silero-vad torch
```

---

## First-run sanity check

After install, verify everything imports cleanly:

```bash
python -c "import pydub, PIL, numpy, tkinter; print('All deps OK')"
python -c "from PIL import ImageTk; print('Pillow ImageTk OK')"
ffmpeg -version | head -1
ffprobe -version | head -1
```

Then launch:

```bash
python launcher.py
```

Pick **"Convert video/audio → MP3"** for the fastest smoke test — pick any video file you have, click Convert, you should see a `<name>.mp3` appear next to it.

---

## Troubleshooting

### `ModuleNotFoundError: No module named 'tkinter'`

You're on Homebrew Python without the tk module. Fix:

```bash
brew install python-tk@3.11    # match your installed python@ version
```

If you used `uv venv`, recreate the venv after installing `python-tk`:

```bash
rm -rf .venv
uv venv
source .venv/bin/activate
uv pip install -r requirements.txt
```

### `Couldn't find ffmpeg or avconv` (pydub warning)

`ffmpeg` isn't on `PATH`. Either:

```bash
which ffmpeg                                 # confirms install location
brew install ffmpeg                          # if missing
echo 'export PATH="/opt/homebrew/bin:$PATH"' >> ~/.zshrc && source ~/.zshrc   # if installed but not on PATH
```

### GUI window won't appear / opens behind other windows

The launcher tries to force itself to the front, but macOS sometimes overrides that. Click the Python rocket icon in the Dock. Or run from a terminal you have focused on so the window inherits focus.

### "Operation not permitted" when reading audio file

macOS sometimes quarantines downloaded files. Right-click the file in Finder → Get Info → uncheck any quarantine flag, or run:

```bash
xattr -d com.apple.quarantine "your audio file.mp3"
```

### Apple Silicon: torch install seems slow

The `torch` wheel is ~200 MB. First install takes 30–60 seconds even on fast connections. Subsequent `uv` operations reuse the cache.

### `tkinter.TclError: image "pyimage1" doesn't exist`

You created a second `Tk()` root somewhere. The toolkit only ever creates one. If you've been hacking on the code, restart your interpreter.

### Renders are very slow

H.264 encoding speed depends on CPU. Apple Silicon is fast at this. If you're on an Intel Mac and 1080p takes >10 minutes, drop the resolution dropdown to 720p — the pipeline now supports it.

---

## What's different vs Windows

The codebase is mostly platform-agnostic. The handful of platform-specific bits:

| What | Windows | macOS |
|---|---|---|
| FFmpeg discovery | Registry walk + WinGet glob | `which ffmpeg` (Homebrew bin dir) |
| "Open output folder" | `os.startfile()` | `open` command |
| Default fonts for text overlays | Arial / Segoe UI | DejaVu Sans Bold (auto-detected, fallback path baked in) |
| Path separators | Backslash | Forward slash (Python's `pathlib` handles both) |

You shouldn't notice any of this in normal use.

---

## Updating later

When new commits land in the repo:

```bash
cd utube_video_creator
git pull

# uv path:
source .venv/bin/activate
uv pip install -r requirements.txt    # in case requirements changed

# pip path:
source .venv/bin/activate
pip install -r requirements.txt
```
