# Voice-to-Video Creator

Turn image panels plus one voice recording into a YouTube-ready MP4. The toolkit includes a launcher, the recommended tone-marker creator, a legacy pause-based creator, a segment preview editor, a voice editor, a video merger, and an audio-to-MP3 converter.

## Quick Start

```bash
python launcher.py
```

The root `launcher.py` is the easiest entry point. It opens a front page with all tools and keeps the project root clean.

On macOS, follow [docs/SETUP_MAC.md](docs/SETUP_MAC.md) first to install Python, FFmpeg, and dependencies.

## Which Branch To Use

Use this branch for the latest full feature set:

```bash
git checkout feature/segment-preview-editor
git pull origin feature/segment-preview-editor
```

## Project Layout

```text
launcher.py                 Start here.
video_toolkit/              All app source code.
sample_assets/              Example images, sample audio, optional intro/outro assets.
outputs/                    Generated videos and exported audio.
docs/                       Setup guides, approach notes, changelog.
tests/                      Diagnostics and smoke/e2e tests.
requirements.txt            Python dependencies.
```

See [docs/PROJECT_LAYOUT.md](docs/PROJECT_LAYOUT.md) for the full folder-by-folder explanation.

## Tools

| Launcher card | Source file | Purpose |
|---|---|---|
| Create video - Tone markers | `video_toolkit/tone_video_creator.py` | Recommended workflow. Splits audio at 1020 Hz tones, pairs each segment with an image, previews segments, then renders MP4. |
| Create video - Voice pauses | `video_toolkit/simple_video_creator.py` | Legacy workflow. Splits audio at long pauses. Also provides shared FFmpeg/render helpers used by other tools. |
| Merge / browse videos | `video_toolkit/video_merger.py` | Shows MP4 files, lets you select/reorder them, and exports one merged MP4. |
| Trim & clean a voice recording | `video_toolkit/voice_editor.py` | Waveform editor for cutting mistakes, long silences, or breaths from voice audio. |
| Convert video/audio to MP3 | `video_toolkit/audio_converter.py` | Extracts or converts media into MP3 at common bitrates. |

You can still run a tool directly:

```bash
python video_toolkit/tone_video_creator.py
python video_toolkit/video_merger.py
python video_toolkit/audio_converter.py
python video_toolkit/voice_editor.py
```

## Input And Output Folders

Sample/default inputs now live under `sample_assets/`:

```text
sample_assets/images/section-20260502-235344/   Main image panel series.
sample_assets/audio/                            Sample voice recordings.
sample_assets/optional/start_image/             Optional first still image.
sample_assets/optional/end_image/               Optional final still image.
sample_assets/optional/start_video/             Optional intro video.
```

Generated creator and merger videos are written to:

```text
outputs/
```

This keeps the root from filling with `video_tone_*.mp4`, `merged_*.mp4`, and test exports.

## Tone Workflow

Use this for new recordings.

1. Open a tone-generator app on your phone and set it to `1020 Hz`.
2. Record your voice continuously.
3. Play the tone between segments.
4. Run `python launcher.py`.
5. Choose `Create video - Tone markers`.
6. Confirm the images/audio and click Convert.

Mode 2 is the usual setup: the audio starts with talking, tones separate segments, and the audio ends with talking. For `N` panels, play `N - 1` tones. If you enable an end image, play one extra tone.

Detailed guide: [docs/APPROACH_TONE.md](docs/APPROACH_TONE.md).

## Segment Preview

The tone creator opens a preview before the final render. You can inspect each image/audio pair and edit a segment's audio before continuing. Details live in [docs/APPROACH_SEGMENT_PREVIEW.md](docs/APPROACH_SEGMENT_PREVIEW.md).

## Legacy Pause Workflow

Use the legacy creator only for recordings that do not have tones. It detects long pauses and treats them as segment boundaries.

```bash
python video_toolkit/simple_video_creator.py --vad --audio path/to/voice.mp3
```

Detailed guide: [docs/APPROACH_VAD.md](docs/APPROACH_VAD.md).

## Diagnostics

Diagnostics moved into `tests/`:

```bash
python tests/_tone_probe.py
python tests/_vad_test.py
python tests/_voice_editor_test.py
python tests/_segment_preview_test.py
```

`_tone_probe.py` helps choose a tone threshold. `_vad_test.py` helps decide whether pause detection is usable for a recording.

## Dependencies

Install the Python packages:

```bash
pip install -r requirements.txt
```

You also need FFmpeg installed on the system. On Windows, use WinGet/Chocolatey/Scoop. On macOS, use Homebrew:

```bash
brew install ffmpeg
```

Optional VAD dependencies are only needed for the legacy pause workflow:

```bash
pip install silero-vad torch
```
