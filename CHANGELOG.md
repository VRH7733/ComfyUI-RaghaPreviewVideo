# Changelog

## 1.3.1
- Documentation only: changelog and release notes cleaned up. No code changes.

## 1.3.0
- **Home**: GitHub repo `VRH7733/ComfyUI-RaghaPreviewVideo`, registry pack `comfyui-ragha-preview-video`.
- **Preview delivery**: finished clips are kept on an in-memory shelf (never on disk) and the player fetches them by
  one-off token from `/ragha_preview_video/clip/<token>`.
- **Cached runs show the preview again** without re-encoding, so `refresh_every_run` is **off by default**.
- **Player** (`web/ragha_preview_video.js`): loads clips when the node reports its output and shows a message when a
  preview has expired.
- Files: `web/ragha_preview_video.js` and `examples/ragha_preview_*.json`. Category `video/ragha`.

## 1.2.1
- **Much lower RAM for long and 4K videos**: frames are decoded, shrunk and encoded one at a time.
  A 1-minute 4K video uses ~200 MB of extra RAM (down from ~1.1 GB), and previews are 8–16% faster. Image batches are shrunk a few frames at a time.
- **Frame-exact A/B sync**: B follows A on every video frame (with a timer fallback) and catches up by adjusting its speed instead of jumping.
  Measured gap: 1 ms typical, under 15 ms worst (one frame at 30 fps is 33 ms).
- A VIDEO input's trim window (start and duration) is respected for picture and sound.
- Sound is written in step with the video.
- README: requirements and performance guide (RAM, browser, settings for 4K).

## 1.2.0
- **A/B compare**: optional `images_b` / `video_b` inputs, shown with a drag **slider** or **side by side** (`compare`), frame-aligned with A.
- **Sound**: plays the `audio` input, or the A video's own audio track (`include_audio`).
- **Passthrough outputs**: `images`, `video` and `audio`, so the node can sit in the middle of a workflow.
- **`refresh_every_run`** option.
- A compare example workflow.
- Writes nothing to disk.

## 1.1.0
- The node is named **Ragha Preview Video** (`RaghaPreviewVideo`).
- Registry icon added; small fixes.

## 1.0.0
- First release: plays a preview of an IMAGE batch or a VIDEO directly on the node.
- `fps`, `max_size`, `quality` and `max_frames` settings.
- Example workflows and GPL-3.0 licence.
