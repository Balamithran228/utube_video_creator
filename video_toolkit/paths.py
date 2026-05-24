from __future__ import annotations

from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent

DOCS_DIR = PROJECT_ROOT / "docs"
SAMPLE_ASSETS_DIR = PROJECT_ROOT / "sample_assets"
SAMPLE_IMAGES_DIR = SAMPLE_ASSETS_DIR / "images"
SAMPLE_AUDIO_DIR = SAMPLE_ASSETS_DIR / "audio"
OPTIONAL_ASSETS_DIR = SAMPLE_ASSETS_DIR / "optional"
OUTPUT_DIR = PROJECT_ROOT / "outputs"

DEFAULT_IMAGES = SAMPLE_IMAGES_DIR / "section-20260502-235344"
START_IMAGE_DIR = OPTIONAL_ASSETS_DIR / "start_image"
START_VIDEO_DIR = OPTIONAL_ASSETS_DIR / "start_video"
END_IMAGE_DIR = OPTIONAL_ASSETS_DIR / "end_image"


def first_existing_audio() -> Path:
    """Return the preferred sample audio, or the newest audio in sample assets/root."""
    preferred = SAMPLE_AUDIO_DIR / "mkv2 -enhanced-v2.mp3"
    if preferred.is_file():
        return preferred

    audio_exts = {".mp3", ".wav", ".m4a", ".flac", ".ogg"}
    candidates = [
        p for folder in (SAMPLE_AUDIO_DIR, PROJECT_ROOT)
        if folder.is_dir()
        for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in audio_exts
    ]
    if candidates:
        return max(candidates, key=lambda p: p.stat().st_mtime)
    return preferred


def ensure_output_dir() -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    return OUTPUT_DIR
