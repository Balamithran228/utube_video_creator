# Changelog

Tracks user-facing features and noteworthy changes to the toolkit.
Newest entries on top.

---

## 2026-05-05 — Segment preview polish round (6 features + UI uplift)

Six focused improvements to the segment preview, plus a UI uplift that
replaces the OS-native popup menu with a CTk-themed one and a
responsive window pass.

### Image undo / revert (feature 1)

Mirrors the audio history pattern. Every pair now carries
`_image_history` — a stack of `images` snapshots. The image kebab
shows **↶ Undo image change** and **↺ Revert images to original**
when there's history beyond the original. Weights snap back too.

### Bulk-add multiple images (top must-implement)

`_action_add_another` now uses `filedialog.askopenfilenames`. Hold
Ctrl / Shift in the dialog to select many images at once — they're
appended in selection order. Building a 33-panel video where each
panel needs 3 images now takes 33 dialogs instead of 99.

### Custom duration weights (feature 3)

Each image in a multi-image segment can take a custom share of the
audio. The detail modal now shows a small **share** slider under
each sub-image (range 0.5–4.0, default mid = uniform). A 60-second
segment with weights `[3, 1, 1]` gives the first image 36 s and the
others 12 s each. `_apply_preview_edits` honours weights when
slicing the audio at render time.

### Drag-and-drop image reordering (feature 2)

Click + drag any image in the multi-image row to a different
sub-panel's position; release to drop. The image and its weight
move together. Cursor switches to **fleur** (4-arrow) over the image
labels so the affordance is discoverable.

### Visual timeline ruler (feature 5)

Below the multi-image row, a colored bar shows each image's slice of
the audio captioned with the per-image duration (`#1 · 00:36.000`).
Updates live as you drag a weight slider, and redraws on window
resize.

### Per-image-chunk dividers in audio editor (feature 4)

When the audio editor opens for a multi-image segment, it draws
vertical dashed pink lines at the chunk transition points so the
user can see where each image's chunk lives. Editing still works on
the full audio; the pipeline re-splits the trimmed result across
the images at render time.

### Render preview for one segment (feature 6)

A new **🎬 Preview render** button in the detail modal's topbar
encodes JUST that segment to a quick MP4 (image + audio chunks +
ffmpeg, weights respected) and opens it with the system default
video player. Lets you sanity-check timing before committing to the
slow full render. Runs on a worker thread; the button label flips
to "🎬 Rendering…" while in flight.

### Modern UI: `StyledPopupMenu`

`tk.Menu` (OS-native, ugly) is replaced everywhere with a custom
CTk-based `StyledPopupMenu` — rounded corners (12 px), themed
font (Segoe UI 11), violet hover, gray italic for disabled items,
accent-coloured call-to-action items ("Add another image…",
"Delete audio…"). Auto-closes on Escape, focus loss, or command
click. Auto-clamps to screen edges.

### Responsive window pass

Every Toplevel uses `_install_window_features` for adaptive
geometry (clamped to 92 % of screen, centered horizontally, biased
toward the upper third vertically) plus **F11** fullscreen and
**F10** maximize keybinds. OS-native title-bar minimize / maximize /
close buttons remain available.

### Tests

**88 cases passing** — 18 added this round covering image undo /
revert, weight defaults / clamping / alignment / pipeline /
degenerate cases, drag-drop reorder + weight migration + no-op
guards, timeline ruler creation + skip-on-single, editor
`chunk_boundaries_s` parameter, render-preview single / multi /
invalid input.

### Docs

`APPROACH_SEGMENT_PREVIEW.md` — the full feature reference, with
internals, kebab-menu structure, and how-to-test steps for every
feature.

---

## 2026-05-05 — Segment Preview & Editor (`segment_preview.py`)

A new modal that opens between segmentation and final render in
`tone_video_creator.py`. After clicking **Convert**, the user sees
every (image, audio) pair laid out as an arrow-connected card strip
**[1] → [2] → … → [N]** and can verify, edit, replace, or delete
each one before committing to the slow MP4 encode.

### Card strip
- Horizontal scrollable strip of dark-themed cards, one per
  segment, with a violet → cyan accent palette matching the
  launcher and Voice Editor.
- Each card carries a small **⋮ kebab** in the top-right corner
  for image actions, plus a centered thumbnail and duration label.
- Pink border + pink duration label appears on cards whose audio
  has been edited; cyan border + cyan duration appears on cards
  with multiple images time-sharing the audio; red border + red
  `(no audio)` label appears on orphan trailing slots left over
  from an audio-only delete.
- **Continue → Render** kicks off the MP4 encode. **Cancel** wipes
  the temp dir and produces no output.

### Detail modal (click any card)
- Big image area with **◀ ▶** segment-step buttons and an audio
  player + audio kebab below.
- Image kebab (overlaid in the image area's top-right) opens the
  full image-actions menu.
- Audio kebab next to the player opens the audio-actions menu
  (Edit / Undo / Revert / Delete).
- Status line under the player shows edit history (`Edited (v3)
  · was 8.5s → now 6.2s`), audio-shift orphan warnings, and
  multi-image slice info.
- Keyboard: ← / → step segments, Space play/pause, Esc close.

### Audio editor (req 2)
- The audio kebab's **✏ Edit audio…** opens a full mini trim editor
  that reuses `voice_editor.py`'s `WaveformCanvas`, `Project`,
  `Cut`, and `Player` — paint regions on the waveform, drag-edges
  to resize, double-click to delete a cut, mark in/out from the
  current playhead, undo/redo, **Trim & confirm →** opens a
  side-by-side **was/now** confirmation dialog with a player so
  you can listen before replacing the segment audio.
- Per-segment edit history: every confirmed trim becomes a new
  version on a stack. **↶ Undo last edit** pops one version,
  **↺ Revert to original** pops everything down to the original.
- The temp dir holds the original `seg_NNN.wav` and edited
  versions (`seg_NNN.edit1.wav`, `seg_NNN.edit2.wav`, …) until the
  user cancels or render finishes.

### Image actions (req 3)
- **🔁 Replace image** — pick a file, duplicate the previous /
  next segment's image, or use a named **Default image** (the
  Front and Back images configured in the launcher form are
  surfaced here as quick replacement targets).
- **🗑 Delete image…** opens a two-button modal:
  - **Keep audio, drop image only** — `pair["image"] = None`. At
    render time the previous segment's image extends across this
    audio (the "linked-list skip").
  - **Delete entire segment** — image + audio both vanish from
    the chain; later segments shift down and indices renumber.

### Audio actions (req 4)
- **🗑 Delete audio…** opens a three-button modal each with a
  one-line description:
  - **Delete audio only** — image kept; all later audios shift up
    by one (`audio[k+1] → slot k`, …) so the third audio segment
    matches the second image, etc. The trailing pair becomes an
    orphan (audio=None, red border, skipped at render).
  - **Replace audio with a file** — pick wav / mp3 / m4a / flac /
    ogg / aac / wma. The new file's duration is probed via pydub
    and the image displays for the full new length, even when the
    replacement is *longer* than the original.
  - **Delete entire segment** — same as the image-side full delete.

### Multi-image segments (req 5)
- New menu items **➕ Add another image…** (single-select OR
  multi-select via Ctrl/Shift in the dialog), **◀ Swap left**,
  **▶ Swap right**, **🗑 Delete this image**.
- A segment can hold any number of images; the audio time-shares
  *equally* across them at render time. 1-minute audio + 3 images
  → 20 s each.
- Cards show a cyan **+N** badge in the top-LEFT corner and a
  duration label like `01:00.000  (00:20.000 ea)`.
- Detail modal renders a horizontal row of sub-panels when the
  segment has 2+ images, each with its own ⋮ kebab and a caption
  `Image 2 of 3 · ~20s`.
- At render time, `_apply_preview_edits` slices the source audio
  with pydub into N equal chunks (written to the temp dir) and
  emits one `(image, chunk_wav)` render unit per image, so the
  chunks play back gaplessly.

### Modern UI polish
- Custom **`StyledPopupMenu`** class replaces `tk.Menu` — rounded
  corners, themed fonts, hover effects, accent-coloured
  call-to-action items. Auto-closes on Escape, focus loss, or
  command click.
- All segment-preview windows centre themselves on screen with
  adaptive geometry clamped to 92 % of the screen — no more
  modals that overflow off the bottom of small displays.
- **F11** toggles fullscreen, **F10** toggles maximize on every
  preview / detail / editor window. OS-native title-bar
  minimize / maximize / close buttons remain available too.

### Pipeline integration (`tone_video_creator.py`)
- `run_tone_pipeline` accepts a `pause_for_preview` callback. The
  callback is invoked from the worker thread after segmentation
  and before render; the preview window is built on the Tk main
  thread via `root.after` and the worker blocks until the user
  decides Continue or Cancel.
- After preview, the new `_apply_preview_edits(preview_pairs,
  tmp_dir)` helper rebuilds the renderable `(segments, images)`
  list — handling None images (previous-image fallback chain),
  None audios (orphan slots dropped at render), full-segment
  deletions, image replacements, and multi-image audio splitting.
- A summary line in the run log explains exactly what changed:
  `preview edits applied: 2 audio edit(s)/replace(s),
  1 multi-image segment(s) (audio split equally),
  1 image-only delete(s), 0 audio-only delete(s),
  0 full segment delete(s)`.

### `tone_video_creator.py` UI fix
- The form (Files / Mode / Tuning / Audio / Overlays / Extras)
  now lives inside a Canvas with a vertical scrollbar so it
  works on smaller windows. The action area
  (Convert / Progress / Log) is bottom-anchored. Window default
  height is adaptive (`min(980, max(720, screen × 0.92 − 40))`).

### Tests (`_segment_preview_test.py`)
**63 cases passing.** Coverage:
- *Data layer* — `_next_edit_path` naming, `Project.render` math
  for one / no / multiple cuts, `hex_lerp`, `fmt_ms`.
- *Pair history* — init populates original entry,
  `_on_pair_audio_changed` appends, `_on_pair_undo` pops,
  `_on_pair_revert` truncates, no-op revert.
- *End-to-end wav round-trip* — write → trim via Project →
  save → reload, duration matches.
- *GUI smoke* — `SegmentAudioPlayer` loads a real wav,
  `SegmentDetailModal` constructs with a real (image, audio)
  pair, `SegmentEditorModal` Save → Confirm fires `on_save`
  with new wav and correct duration, Save → Re-edit closes
  confirm only and leaves the editor intact, all-cut rejection.
- *Image actions* (req 3) — change / drop / segment-delete /
  last-pair guard / duplicate prev|next / default surfacing /
  DeleteImageModal callbacks / audio-only card placeholder /
  detail modal None-image rendering.
- *Pipeline rebuild* (req 3) — pass-through, None-image
  previous-fallback, consecutive Nones chaining, leading-None
  forward fallback, all-images-None error, structural deletion,
  image-replace flow, end-to-end combo.
- *Audio actions* (req 4) — shift-up correctness at middle and
  first index, single-pair guard, history migrates with audio,
  replace probes duration + appends history, bogus-file rejected,
  three-button modal callbacks, drop-audio disabled when
  has_image=False, card `(no audio)` badge, end-to-end shift →
  render filter, all-audio-None error, mixed image-None +
  audio-None chain.
- *Multi-image* (req 5) — both data-model migration paths
  (legacy scalar → list and modern list passthrough), append,
  swap left/right with edge no-ops + alias re-sync,
  remove-one-with-list-empties, targeted index replace, card
  `+N` badge, per-image-slice duration label, detail-modal
  multi-row with N sub-kebabs (counted by widget walker),
  2-image and 3-image splitting with real wavs, missing-tmp_dir
  error, mixed single + multi-pair ordering, multi-then-None
  fallback uses the **last** image, end-to-end add + swap +
  pipeline expansion.

Run with `py -3.10 _segment_preview_test.py`.

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
