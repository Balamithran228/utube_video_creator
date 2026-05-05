# Segment Preview & Editor

A modal that opens between segmentation and final render in
`tone_video_creator.py`. After clicking **Convert**, the user sees
every (image, audio) pair laid out as an arrow-connected card strip
**[1] → [2] → … → [N]** and can verify, edit, replace, add to, or
delete each one before committing to the slow MP4 encode.

This document covers every feature, how each one works internally,
and what the user can do to verify it from the GUI.

---

## Table of contents

1. [Quick start](#quick-start)
2. [The card strip](#the-card-strip)
3. [Detail modal](#detail-modal)
4. [Audio editor](#audio-editor) — req 2
5. [Image actions (single image)](#image-actions-single-image) — req 3
6. [Audio actions](#audio-actions) — req 4
7. [Multi-image segments](#multi-image-segments) — req 5
8. [Image undo / revert](#image-undo--revert) — feature 1
9. [Bulk-add multiple images](#bulk-add-multiple-images) — top must
10. [Custom duration weights](#custom-duration-weights) — feature 3
11. [Drag-and-drop image reordering](#drag-and-drop-image-reordering) — feature 2
12. [Visual timeline ruler](#visual-timeline-ruler) — feature 5
13. [Per-image-chunk dividers in audio editor](#per-image-chunk-dividers-in-audio-editor) — feature 4
14. [Render preview for one segment](#render-preview-for-one-segment) — feature 6
15. [Modern UI: StyledPopupMenu](#modern-ui-styledpopupmenu)
16. [Window controls (F10 / F11)](#window-controls-f10--f11)
17. [Pipeline integration](#pipeline-integration)
18. [Running the tests](#running-the-tests)

---

## Quick start

```bash
py -3.10 tone_video_creator.py
```

Pick an images folder + voice file → click **▶ Convert** → wait until the
preview strip opens. Everything in this doc is in that strip.

Run the test suite:

```bash
py -3.10 _segment_preview_test.py
```

**88 cases passing** at the time of writing.

---

## The card strip

After segmentation finishes, a dark-themed modal opens with a horizontal
scrollable strip of cards: `[1] → [2] → [3] → … → [N]`, one per
(image, audio_segment) pair.

### What each card shows

- **Thumbnail** — primary image (`images[0]`), or `🎵 (audio only)` placeholder.
- **Segment number** — 1-based index. Renumbers automatically if any
  segment is deleted.
- **Duration label** — total audio time. For multi-image segments it
  also shows the per-image slice: `00:06.000  (00:03.000 ea)` or
  the actual weighted slice when weights aren't uniform.
- **`+N` badge** (top-LEFT corner, cyan) — appears when the segment
  has 2+ images.
- **`⋮` kebab** (top-RIGHT corner) — opens the image-actions menu.
- **Border colour:**
  - default = grey stroke
  - **pink** = audio has been edited (history ≥ 2 versions)
  - **cyan** = multi-image segment (audio time-shares)
  - **red** = orphan slot from an audio-only delete (no audio →
    skipped at render)

### Continue / Cancel

Anchored at the bottom of the window:

- **Continue → Render** — kick off the MP4 encode.
- **Cancel** — wipe the temp dir and produce no output.
- **Esc** = cancel, **Enter** = render.

### How to test

1. Convert any working `images_dir + voice file`. The strip opens.
2. Verify N cards appear with arrows between them.
3. Verify duration labels match the reported segment durations from the log.
4. Try resizing the window. Strip scrolls horizontally if cards overflow.

---

## Detail modal

Click any card → opens the detail modal with:

- **Topbar:** title (`Segment 5 — img_004.png · 00:08.234`), position
  indicator (`5 / 33`), **🎬 Preview render** button, **✕ Close** button.
- **Big image area** with **◀ ▶** arrows on the sides for segment
  navigation. Image kebab `⋮` overlaid in the top-right.
- **Audio player** (play/pause, scrubber, time readout) with its own
  audio kebab `⋮`.
- **Status line** — explains current edit state (e.g.,
  `Edited (v3) · was 8.5s → now 6.2s` or `(no audio)` warning).
- **Hint footer** — keyboard shortcuts.

### Keyboard shortcuts

| Key | Action |
|-----|--------|
| ← / → | Step to previous / next segment |
| Space | Play / pause audio |
| Esc | Close modal |
| F11 | Toggle fullscreen |
| F10 | Toggle maximize |

### How to test

1. Click any card → detail modal opens.
2. Press ← / →. Each press moves to a neighbour segment, with
   image and audio reloading.
3. Press Space. Audio plays. Space again pauses.
4. Press F11. Modal goes fullscreen. F11 again restores.
5. Press Esc. Modal closes; you're back on the strip.

---

## Audio editor

### Requirement (req 2)

Per-segment audio editor with paint-cuts-on-waveform UI, undo/redo,
preview-with-cuts-skipped playback, and a was/now confirmation dialog.

### How it works

The audio kebab in the detail modal has **✏ Edit audio…** which opens
**`SegmentEditorModal`**. This embeds the same `WaveformCanvas`,
`Project`, `Cut`, and `Player` that `voice_editor.py` uses.

Workflow:

1. Paint a region on the waveform → cut.
2. Drag cut edges to resize, body to move; double-click to delete.
3. **`[` / `]`** marks the in/out from the playhead.
4. **Ctrl+Z / Ctrl+Y** undo / redo within the editor.
5. **Trim & confirm →** renders the trimmed audio to disk and opens
   `ConfirmAudioModal` showing was/now durations + a player.
6. **Confirm** → replaces this segment's audio. Card border turns pink.
   **Re-edit** → closes only the confirm dialog; editor stays open
   with cuts intact.

Trimmed audio files are written next to the originals (e.g.,
`seg_005.edit1.wav`, `seg_005.edit2.wav`) in the temp dir.

### Per-segment edit history

Each pair has a `_history` list. Each confirmed trim appends a new
version. **↶ Undo last edit** pops one version, **↺ Revert to original**
truncates back to the original. Both are in the audio kebab menu.

### How to test

1. Click any card → detail modal.
2. Audio kebab → **✏ Edit audio…**. The mini editor opens.
3. Paint a region: click + drag on the waveform.
4. Press Space — playback skips the cut region (preview).
5. Click **Trim & confirm →**. Confirm modal opens with was/now
   durations and a player.
6. Click **✓ Confirm — replace audio**. Editor and confirm both
   close. Card border turns pink.
7. Audio kebab again → **↶ Undo last edit**. Card border returns
   to grey, audio reverts.

---

## Image actions (single image)

### Requirement (req 3)

Image kebab on each card and in the detail modal, with options to
replace / delete the image. The delete should ask whether to drop
just the image (audio extends from previous) or the whole segment.

### How it works

#### Image kebab menu (when segment has exactly 1 image)

- **📁 Replace this image…** — file picker
- **↑ Duplicate previous image** — copies image from segment N-1
- **↓ Duplicate next image** — copies image from segment N+1
- **🖼 Use [Front / Back / etc.]** — uses one of the configured
  defaults (the start / end image from the launcher form is surfaced
  here as a quick replacement target)
- **➕ Add another image…** — switches to multi-image mode
- **🗑 Delete image…** — opens `DeleteImageModal` with two options:
  - **🖼 Keep audio, drop image only** — `pair["images"] = []`. At
    render time, the previous segment's image extends across this audio.
  - **🗑 Delete entire segment** — image + audio both vanish from
    the chain; later segments shift down and indices renumber.

The strip refreshes immediately after every action.

### How to test

1. **Replace:** Card ⋮ → Replace this image… → pick a different
   file. Thumbnail updates.
2. **Duplicate previous:** On segment 2's card ⋮ → Duplicate
   previous image. Segment 2's thumb now matches segment 1's.
3. **Drop image keep audio:** Card ⋮ → Delete image… →
   "Keep audio, drop image only". Card now shows
   `🎵 (audio only)` placeholder and red-ish border. Render: image
   from segment N-1 extends across this audio.
4. **Delete entire:** Card ⋮ → Delete image… →
   "Delete entire segment". Card disappears; remaining cards
   renumber.

---

## Audio actions

### Requirement (req 4)

Audio kebab `⋮` on the detail modal's audio player has Edit / Undo /
Revert plus a new **Delete audio…** opening a popup with three options:
delete entire segment, delete only the audio (image kept; audios shift
up), or replace audio with another file (image extends to new
duration).

### How it works

#### Delete-only-audio: shift-up semantic

If you delete `audio[k]`, the audios shift up:

- `audio[k+1] → slot k`, `audio[k+2] → slot k+1`, …, `audio[N] → slot N-1`.
- The trailing pair (slot N) gets `audio = None` (orphan).
- The strip shows the orphan with a red `(no audio)` badge.
- At render time, `_apply_preview_edits` filters out `audio=None` pairs.

This implements your description: "third audio is now matched with the
second image". Per-segment `_history` migrates with the audio.

#### Replace audio with longer file

The new audio is loaded with pydub to probe duration. The image
displays for the full new duration (no clipping). Replacement is
appended to `_history`, so Undo / Revert still work.

### How to test

1. **Delete audio only:** Detail modal → audio ⋮ → Delete audio… →
   "Delete audio only". Strip shifts; trailing card is red `(no audio)`.
2. **Replace with longer file:** Audio ⋮ → Delete audio… →
   "Replace audio with a file…". Pick a longer file. Card duration
   updates to new length.
3. **Render:** Continue → Render. Output MP4 has N-1 segments after
   the audio-only delete (orphan dropped); replacement plays for full
   new length.

---

## Multi-image segments

### Requirement (req 5)

Add multiple images to one segment; audio time-shares equally; each
image has its own kebab with swap-left / swap-right.

### How it works

#### Data model

Each pair carries:

- **`images: List[str]`** — 0+ image paths.
- **`weights: List[float]`** — per-image duration share (default uniform).
- **`_image_history: List[List[str]]`** — image undo/redo snapshots.

#### Pipeline expansion

`_apply_preview_edits(preview_pairs, tmp_dir)` walks each pair:

- 0 images → audio-only fallback to previous image
- 1 image → standard render unit
- 2+ images → slice the source audio with pydub into N chunks
  weighted by `pair["weights"]`, write chunk wavs to `tmp_dir`,
  emit one render unit per (image, chunk).

#### UI

- **Card** — primary image as thumb + cyan **`+N`** badge in the
  top-LEFT + duration label `total (per ea)`.
- **Detail modal (n ≥ 2)** — horizontal row of sub-panels, each
  with image + caption (`Image 2 of 3 · 00:20.000`) +
  per-image kebab + per-image weight slider. Plus a colored
  timeline ruler below the row.

### How to test

1. **Add another image:** Card ⋮ → ➕ Add another image… (or in
   detail modal). Pick a file. Card shows `+1` badge.
2. **Add 3rd image:** Repeat. Card shows `+2`. Detail modal now
   has 3 sub-panels.
3. **Swap:** In detail modal, click the 2nd image's kebab → ◀ Swap
   left. Images 1 and 2 swap.
4. **Render:** Continue. Output MP4: image 1 plays for the first
   third of the audio, image 2 the middle third, image 3 the last
   third (when weights are uniform).

---

## Image undo / revert

### Feature 1

Mirrors the audio history pattern. Per-segment `_image_history`
records every image-list state. Undo pops, Revert truncates to
original.

### How it works

- Initialised in `SegmentPreviewWindow.__init__` with one snapshot
  (the original).
- Every image mutation calls `_commit_image_state(idx)` which appends
  the new state if it differs from the previous.
- **`_on_pair_image_undo(idx)`** pops the latest snapshot and restores
  the previous state.
- **`_on_pair_image_revert(idx)`** truncates back to the original
  snapshot and resets weights to uniform.

### Menu items

In the image kebab, when there's history beyond the original:

- **↶ Undo image change**
- **↺ Revert images to original**

### How to test

1. Convert. On segment 1, add 2 extra images → 3 total.
2. Swap one. Replace another. The kebab now shows Undo / Revert.
3. **↶ Undo image change** → reverts the swap (or last action).
4. Repeat until original is reached. Undo becomes hidden.
5. Re-do edits. **↺ Revert images to original** → all changes gone in
   one shot.

---

## Bulk-add multiple images

### Top must-implement feature

`_action_add_another` uses `filedialog.askopenfilenames` (plural).
You can multi-select with **Ctrl** or **Shift** and all of them are
appended in selection order.

### How to test

1. Card ⋮ → ➕ Add another image…
2. In the file dialog, hold **Ctrl** and click 5 different images.
3. Click Open. All 5 are appended in selection order.
4. Card badge shows `+5`. Detail modal shows 6 sub-panels (original
   + 5 added).

---

## Custom duration weights

### Feature 3

Each image in a multi-image segment can take a custom share of the
audio. Default = uniform; override via per-image slider in the
detail modal.

### How it works

- **Data:** `pair["weights"]: List[float]` — same length as
  `images`. Default `[1.0, 1.0, …]`.
- **UI:** in the detail modal, each sub-panel has a small slider
  labelled "share" running `0.5 → 4.0` (steps of 0.1). Mid (1.0)
  = "default share".
- **Pipeline:** `_apply_preview_edits` normalises weights to sum=1
  and slices audio cumulatively (final chunk picks up the
  rounding remainder so audio is never lost).

#### Example

- 60s audio + 3 images with weights `[3, 1, 1]`
- Sum = 5; slices = `36s`, `12s`, `12s`.

### How to test

1. Add 2 extra images to segment 1 (3 total).
2. Detail modal: drag the **share** slider under image 1 to the
   right (e.g., 3.0). Watch the per-image caption (`Image 1 of 3 · …`)
   update live, and the colored ruler below shrink the others.
3. Click **🎬 Preview render** at the top of the detail modal. The
   preview MP4 shows image 1 for ~60% of the audio, the others
   shorter — matching the slider weights.
4. Card duration label shows the total + the per-image average,
   updated in real time.

---

## Drag-and-drop image reordering

### Feature 2

In the detail modal's multi-image row, click+drag any image to
another sub-panel's position. The image and its weight move together.

### How it works

- Each sub-image label has Tk bindings for `<ButtonPress-1>`,
  `<B1-Motion>`, `<ButtonRelease-1>`.
- On press: record source index. On motion: mark "drag started".
- On release: hit-test all sub-panels with the cursor's screen
  coords. If a different sub-panel is found → trigger
  `_on_pair_image_reordered(idx, src, dst)`.
- The mutation pops at `src`, inserts at `dst`. Weights move with
  their images.
- Cursor on hover changes to **`fleur`** (4-arrow drag cursor) over
  the image labels.

The kebab and slider have their own click handling and don't
trigger drags.

### How to test

1. Add 3 extra images so the detail modal has 4 sub-panels.
2. Hover over any image — cursor turns into a 4-arrow drag cursor.
3. Click + drag one image onto another sub-panel. Release. The
   images swap positions immediately.
4. The colored ruler below updates to reflect the new order.

---

## Visual timeline ruler

### Feature 5

Under the multi-image row in the detail modal: a colored bar
showing each image's slice of the audio, captioned with the
per-image duration. Updates live when you drag a weight slider.

### How it works

- A `tk.Canvas` packed below the multi-image row.
- For each image, a colored rectangle (alternating violet / cyan)
  covers `weight[i] / sum(weights)` of the canvas width.
- Captions inside read `#1 · 00:36.000`.
- Bound to `<Configure>` so it redraws on window resize.
- Bound to slider commands so it redraws live during weight drags.

### How to test

1. In a multi-image segment's detail modal, look below the
   sub-panel row → a thin coloured bar with `#1 / #2 / #3 …`.
2. Drag any weight slider — the colored bar shifts proportionally
   in real time.
3. Resize the modal — bar redraws to fit.

---

## Per-image-chunk dividers in audio editor

### Feature 4

When you open the audio editor for a multi-image segment, vertical
dashed lines show where each image's chunk lives. Visual hint only —
editing still works on the full audio. After saving, the (possibly
shorter) trimmed audio is re-split across the images at render time.

### How it works

- Detail modal's `_open_editor` computes chunk boundaries from
  `pair["duration_ms"]` and `pair["weights"]` (in seconds).
- Boundaries pass through the `chunk_boundaries_s` constructor
  param of `SegmentEditorModal`.
- After `_sync_canvas`, `_draw_chunk_dividers` overlays vertical
  dashed pink lines on the waveform at those times, tagged
  `chunk_divider`.
- Lines redraw when canvas is reconfigured.

### How to test

1. On a multi-image segment, audio kebab → **✏ Edit audio…**.
2. The waveform now shows vertical dashed pink lines at each chunk
   boundary.
3. Paint a cut spanning a divider — the cut is on the FULL audio,
   not per-chunk.
4. Confirm. The pipeline re-splits the trimmed audio across the
   images by weight.

---

## Render preview for one segment

### Feature 6

A **🎬 Preview render** button in the detail modal's topbar. Click →
encode JUST this segment to a small temp MP4 (image + audio chunks
+ ffmpeg) → open with the system default video player. Lets you
sanity-check timing before committing to the slow full render.

### How it works

- Worker thread runs `_render_single_segment_preview(image_paths,
  audio_path, weights, …)`.
- For 1-image: one ffmpeg `-loop 1 -i image -i audio` call.
- For N-images: pydub slices the audio into N weighted chunks, ffmpeg
  encodes each `(image[i], chunk[i])` clip, and ffmpeg concat
  joins them losslessly.
- Output is in a unique temp dir (e.g., `segpreview_render_…/preview.mp4`).
- `_open_with_default_player(path)` calls
  `os.startfile` (Windows) / `open` (macOS) / `xdg-open` (Linux).
- Button label flips to "🎬 Rendering…" while in flight.

### How to test

1. In any single-image segment's detail modal, click **🎬 Preview render**.
2. Button goes to "Rendering…", then a system video player opens
   with just that segment's MP4.
3. In a multi-image segment, do the same. The preview MP4 shows
   the images transitioning at the weighted chunk boundaries.

---

## Modern UI: StyledPopupMenu

The image and audio kebab menus are no longer tk's OS-native menu.
They use `StyledPopupMenu` — a CTk-based popup with rounded corners,
themed fonts, hover effects, and accent-coloured call-to-action items.

### Features

- **Headers** (non-clickable section titles in the accent colour).
- **Separators** (thin horizontal lines).
- **Command items** with hover, disabled state (gray), and an
  optional **accent** flag (highlights "Add another image" and
  "Delete audio…" as primary CTAs).
- **Auto-close** on Escape, focus loss, or after a command runs.
- **Auto-clamps** position to the screen edges.

### How to test

1. Click any kebab. The popup has rounded corners, themed
   font (Segoe UI 11), and violet hover on items.
2. Hover over a disabled item ("Duplicate previous image" on
   segment 1) — no hover effect, label stays grey.
3. Press Escape with the menu open → closes.
4. Click anywhere outside the menu → closes.

---

## Window controls (F10 / F11)

`_install_window_features` is applied to every Toplevel:

- **Adaptive geometry** — initial size = `min(ideal, 92% of screen)`,
  centered horizontally and biased toward the upper third
  vertically.
- **F11** — toggle fullscreen.
- **F10** — toggle maximize (system "zoomed" state).
- **OS title bar** — minimize / maximize / close buttons remain
  available on every window.

Applied to: `SegmentPreviewWindow`, `SegmentDetailModal`,
`SegmentEditorModal`, `ConfirmAudioModal`, `DeleteImageModal`,
`DeleteAudioModal`. The small confirm/delete modals get adaptive
geometry but skip F11 (they're not meant to fullscreen).

### How to test

1. In any preview window, press **F11**. Window goes
   fullscreen. F11 again restores.
2. Press **F10**. Window maximizes. F10 again restores.
3. Use the OS title bar's minimize button — works as expected.

---

## Pipeline integration

`run_tone_pipeline` accepts a `pause_for_preview` callback. After
segmentation but before render, it:

1. Builds `preview_pairs` from `(images, segments)` with the new
   data model: `{"images": [...], "audio": ..., "duration_ms": ...,
   "index": k}`.
2. Calls `pause_for_preview(preview_pairs)`. The callback is
   blocking; it returns `True` (Continue) or `False` (Cancel).
   The preview window is built on the Tk main thread via
   `root.after`; the worker blocks until the user decides.
3. After Continue, calls `_apply_preview_edits(preview_pairs,
   tmp_dir=str(seg_dir))` to rebuild the renderable
   `(segments, images)` list.
4. The pipeline log prints a summary:
   `preview edits applied: N audio edit(s)/replace(s), N
   multi-image segment(s) (audio split equally), …`
5. Standard `_render_and_concat` proceeds with the rebuilt list.

`_apply_preview_edits` handles every preview-side mutation:

| Preview mutation | Pipeline effect |
|---|---|
| Image replaced | The new path appears in the rendered list |
| Image dropped (`images=[]`) | Falls back to previous segment's image |
| Audio edited | The trimmed `seg_NNN.editK.wav` is used |
| Audio replaced | The user-picked file is used (any duration) |
| Audio dropped (`audio=None`) | Pair skipped entirely |
| Full segment deleted | Pair removed from `preview_pairs` |
| Multi-image (n ≥ 2) | Audio split into N weighted chunks; one
   render unit per image |

---

## Running the tests

```bash
py -3.10 _segment_preview_test.py
```

**88 cases passing** (as of this writing). Coverage layered by
feature:

- *Data layer* (7) — `_next_edit_path`, `Project.render`, hex_lerp,
  fmt_ms.
- *Pair history* (5) — audio version stack, undo, revert.
- *End-to-end wav round-trip* (1).
- *GUI smoke* (6) — player, detail modal, editor save/confirm/re-edit,
  empty-result rejection, full callback chain.
- *Image actions / req 3* (9) — change, drop, segment-delete, last-pair
  guard, duplicate, default surfacing, DeleteImageModal, audio-only
  card placeholder, detail modal None-image rendering.
- *Pipeline rebuild / req 3* (8).
- *Audio actions / req 4* (12) — shift correctness at middle/first,
  guard, history migrates, replace probes duration, bogus file
  rejected, three-button modal, disabled state, card badge,
  end-to-end shift, all-audio-None error, mixed combos.
- *Multi-image / req 5* (9) — migration both directions, append,
  swap, remove-one, targeted index replace, card +N, per-slice
  duration, detail-row sub-kebabs.
- *Multi-image pipeline / req 5* (6) — splitting, mixed pairs,
  fallback semantics, end-to-end.
- *UI polish* (7) — StyledPopupMenu, bulk-add, window features.
- *Polish features round 2* (18) — image undo / revert, weights
  defaults / clamping / alignment / pipeline / degenerate, drag-drop
  reorder / weight migration / no-op, timeline ruler creation
  + skip on single, editor chunk_boundaries, render preview single
  / multi / invalid.

Run a single test by name:

```bash
py -3.10 -c "
import _segment_preview_test as t
import sys
[t for t in dir(t) if 'image_undo' in t]
"
```

(Or just inspect the test file — each test is a top-level
`@test('name')` decorator.)

---

## File layout

| File | What lives there |
|---|---|
| `segment_preview.py` | All UI classes and pipeline-glue helpers |
| `tone_video_creator.py` | `_apply_preview_edits` + pipeline integration |
| `_segment_preview_test.py` | Full test suite (88 cases) |
| `voice_editor.py` | Reused `Project`, `Cut`, `Player`, `WaveformCanvas`, `compute_peaks`, `load_audio`, `save_audio`, `fmt_time` |
| `simple_video_creator.py` | Reused `_render_and_concat`, `find_ffmpeg`, `configure_pydub` |

---

## Conventions / non-obvious things

- **Always `py -3.10`** — deps live in 3.10; default pip goes to 3.13.
- **Theme palette** — violet `#7C5CFF` / cyan `#22D3EE` / pink `#F472B6`,
  matching launcher and Voice Editor.
- **`pair["image"]`** is the legacy scalar — it's kept in sync as
  `pair["images"][0]` (or `None`) for backward-compat readers.
- **Refresh model:** every mutation handler calls
  `_refresh_card(idx)` and `_refresh_open_detail(idx)` so both
  the strip card and the open detail modal stay in sync.
- **Test naming** — `_<feature>_test.py` (underscore prefix).
- **Side-anchored action area** — the project pattern of
  `side="bottom"` for Convert / Progress / Log / Continue rows.

