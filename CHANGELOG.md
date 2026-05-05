# Changelog

Tracks user-facing features and noteworthy changes to the toolkit.
Newest entries on top.

---

## 2026-05-05 — Modern launcher front page

The `launcher.py` front page was rebuilt from the original ttk
prototype into a proper modern UI that matches the Voice Editor:

- **customtkinter-based design** with the same violet → cyan palette
  (#7C5CFF / #22D3EE) used throughout the toolkit.
- **Selectable tool cards** with hover and selected states. Click a
  card to select it (border turns violet, chevron arrow rotates from
  `›` to `▶`); double-click to launch immediately.
- **Dark / light theme toggle** in the top-right corner.
- **Keyboard navigation**: ↑ / ↓ to move between cards, **Enter** to
  Continue, **Esc** to close the launcher.
- **Anchored Continue button** packed with `side="bottom"` so it
  stays visible regardless of how many tool cards are added.
- **Scrollable card area** so the launcher can grow to any number
  of tools without breaking the layout.

The interaction model is unchanged: pick one of the five tools,
click Continue (or double-click the card / press Enter), the chosen
tool's window opens and the launcher closes.

---

## 2026-05-05 — Voice Editor

A new **standalone audio-trim app** was added: `voice_editor.py`,
exposed on the launcher as **"Trim & clean a voice recording"**.

### Capability
Load any audio file (MP3 / WAV / M4A / FLAC / AAC / OGG / OPUS),
paint **cut regions** anywhere on the waveform — start, middle, end,
or many places at once — and export a clean continuous audio file
with the cuts removed.

### UI
- Custom waveform on a `tk.Canvas` with a violet → cyan vertical
  gradient, time grid, live cut handles, drag-to-paint regions,
  drag-edge to resize, drag-body to move, double-click-to-delete,
  mouse-wheel zoom centered on the cursor.
- customtkinter chrome (top bar, transport row, side panel, export
  panel, scrollable cuts list).
- Dark / light theme toggle that rebuilds the UI cleanly.

### Audio engine
- Decodes via pydub + ffmpeg into a float32 numpy array in `[-1, 1]`.
- Cut regions are normalized: sorted by start, overlapping/touching
  cuts auto-merge, sub-20 ms cuts are rejected so accidental clicks
  don't litter the list.
- `render(fade_ms=5)` concatenates the surviving regions and applies
  a 5 ms half-cosine crossfade at every join — short enough to be
  inaudible against speech, long enough to suppress click artifacts.
- Stereo and mono both supported; channel layout and sample rate are
  preserved end-to-end.

### Playback
- `sounddevice` callback-based output stream with a **"Preview with
  cuts skipped"** toggle. When ON, playback live-skips over the
  marked regions so you can hear the result before exporting.
- Keyboard: `Space` play/pause, `[` / `]` mark in/out, `←` / `→`
  seek 1 s, `Shift+←/→` 10 s, `Ctrl+Z` / `Ctrl+Y` undo/redo,
  `Delete` removes the last cut.

### Export
- **WAV** (lossless) and **MP3** (128 / 192 / 256 / 320 kbps CBR).
- Default filename is `<original>_trimmed_<YYYYMMDD_HHMMSS>.<ext>`
  in the same folder as the input.
- Renders on a worker thread so the UI stays responsive.

### Tests
- **Headless data-model suite** (`_voice_editor_test.py`):
  17 cases covering basic cuts, edge cuts (start/end), scattered
  cuts, overlap auto-merge, touching-cut merge, full coverage,
  too-short rejection, undo/redo, keep_regions, fade-at-join,
  stereo preservation, WAV roundtrip, peak computation, sort
  invariants. **17/17 pass.**
- **End-to-end test** (`_voice_editor_e2e.py`): extracts audio
  from a workspace MP4 with ffmpeg, applies 5 scattered cuts
  (including an overlap-merge case), renders, exports to both
  WAV and MP3, round-trips both, asserts duration matches the
  computed `keep_regions` sum. **Passed** against
  `merged_20260503_141244.mp4` (308.46 s → 305.46 s, 5 cuts,
  3 internal fade joins, MP3 7 MB vs WAV 52 MB).
- **UI smoke test**: launches CTk root, programmatically loads
  audio, adds three cuts, toggles the theme twice, redraws the
  canvas — all without exceptions.

### Dependencies added
- `customtkinter >= 5.2.0` — modern themed widgets
- `sounddevice >= 0.4.6` — callback-based audio playback

ffmpeg discovery reuses the existing logic from
`simple_video_creator.py` (`find_ffmpeg` / `configure_pydub`),
so the same WinGet / PATH / static-candidate fallback chain
applies.

### Docs
- `APPROACH_VOICE_EDITOR.md` — controls, formats, limits, deps.

---

## 2026-05-03 — initial commit

The original tone-marker video creator and supporting tools were
imported into git:

- `tone_video_creator.py` — production video pipeline using 1020 Hz
  tone bursts as segment markers.
- `simple_video_creator.py` — alternative pipeline with Silero VAD
  segmentation for legacy recordings without tone markers.
- `youtube_video_app.py` — original full-featured tk app, largely
  superseded by the focused tools above.
- `video_merger.py` — thumbnail gallery + drag-to-order video merge.
- `audio_converter.py` — extract MP3 from any video / audio source.
- `launcher.py` — front-page picker (ttk, original style).
- `APPROACH_TONE.md` / `APPROACH_VAD.md` — recording instructions
  and tuning notes for each segmentation approach.
