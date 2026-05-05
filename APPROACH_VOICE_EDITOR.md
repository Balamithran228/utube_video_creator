# Voice Editor — usage notes

A small desktop app for trimming spaces, mistakes, breaths, or any unwanted
section out of a voice recording. Load a file → paint cut regions on the
waveform → export a clean continuous audio file with the cuts removed.

Launched from `launcher.py` ("Trim & clean a voice recording") or directly:

```
python voice_editor.py
```

## What it does

1. Decode any audio file (MP3 / WAV / M4A / FLAC / AAC / OGG / OPUS) into a
   numpy array via pydub + ffmpeg.
2. Render a custom waveform with a violet → cyan gradient on a tk Canvas.
3. Let you paint **cut regions** with click-drag, keyboard markers, or the
   side-panel list. Cuts can sit anywhere — start, middle, end, multiple
   places — and overlapping cuts auto-merge.
4. Preview playback with a "skip cuts" toggle so you can listen to the
   result before exporting.
5. Export the surviving regions concatenated together, with a 5 ms cosine
   crossfade at every join (no audible click), to **WAV** (lossless) or
   **MP3** (192/256/320 kbps).

## Controls

| Action                             | Mouse                  | Keyboard            |
|------------------------------------|------------------------|---------------------|
| Play / pause                       | ▶ button               | `Space`             |
| Stop                               | ⏹ button               | —                   |
| Paint a new cut                    | left-drag on waveform  | `[` then `]`        |
| Resize a cut                       | drag its edge          | —                   |
| Move a cut                         | drag its body          | —                   |
| Delete a cut                       | ✕ in list, or double-click on the cut | `Delete` |
| Reset zoom                         | double-click empty area or **Fit** | —      |
| Zoom                               | mouse wheel / **+** / **−** | —              |
| Seek                               | click the waveform      | `←/→` (1 s), `Shift+←/→` (10 s) |
| Undo / redo                        | ↶ / ↷ buttons          | `Ctrl+Z` / `Ctrl+Y` |
| Toggle dark / light                | top-right button       | —                   |

## Export formats

- **WAV** — lossless, larger file. Default.
- **MP3** — 128 / 192 / 256 / 320 kbps CBR. 192 kbps is a sensible default.

The output filename defaults to `<original>_trimmed_<YYYYMMDD_HHMMSS>.<ext>`
in the same folder as the input. You can change it from the save dialog.

## Audio details

- Stereo and mono both supported; channel layout is preserved.
- Sample rate is preserved (no resampling).
- Internally everything is float32 in `[-1, 1]`; clipped before quantizing
  back to int16 on export.
- Cut joins get a 5 ms half-cosine crossfade ramp on each side. This is
  short enough to be inaudible against speech but long enough to suppress
  the click that a hard splice would produce.
- Min cut length: 20 ms. A drag shorter than that is treated as a click
  (seeks the cursor) so accidental clicks don't become tiny cuts.

## Limits

- Files up to ~30 minutes load comfortably. Longer files will work but
  load and render slower because the whole file is decoded into memory.
- The waveform is downsampled to ~4000 peak columns regardless of file
  length, so render time on the canvas stays constant.

## Dependencies

```
pydub          # audio decode/encode (calls ffmpeg)
numpy          # array math
customtkinter  # modern themed UI chrome
sounddevice    # callback-based audio playback
ffmpeg         # the actual decoder/encoder, must be on PATH or WinGet
```

ffmpeg discovery uses the same logic as the rest of the toolkit
(`find_ffmpeg` from `simple_video_creator.py`) — so the same WinGet /
PATH / static-candidate fallback chain applies.
