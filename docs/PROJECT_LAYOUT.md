# Project Layout

The repository is arranged so the root stays clean and the main entry point remains simple.

```text
launcher.py                 Root shortcut. Run this first.
requirements.txt            Python dependencies.
README.md                   Main usage guide.

video_toolkit/              Application source code.
  launcher.py               Tool picker UI used by the root launcher.
  tone_video_creator.py     Recommended tone-marker video creator.
  simple_video_creator.py   Legacy voice-pause creator and shared render helpers.
  segment_preview.py        Segment preview and per-segment audio editor.
  voice_editor.py           Standalone waveform trim/clean tool.
  video_merger.py           MP4 browser and merger.
  audio_converter.py        Video/audio to MP3 converter.
  paths.py                  Shared folder paths for samples and outputs.

sample_assets/              Example inputs and optional intro/outro assets.
  images/                   Image panels used by the creator.
  audio/                    Sample voice recordings.
  optional/start_image/     Optional first image.
  optional/end_image/       Optional last image.
  optional/start_video/     Optional intro video.

outputs/                    Generated videos and test exports.
docs/                       Detailed guides, approaches, and changelog.
tests/                      Diagnostics and smoke/e2e test scripts.
```

Generated MP4/WAV/MP3 outputs should go in `outputs/`. The app writes new creator and merger videos there automatically. Sample input media lives in `sample_assets/` so it is separate from generated work.
