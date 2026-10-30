# Changelog

## 1.3.0
- **New home**: the GitHub repo is `VRH7733/ComfyUI-RaghaPreviewVideo` and the registry pack is `comfyui-ragha-preview-video`.
  Earlier versions were published under a previous pack, which has been retired.
- **New delivery of the preview**: finished clips are kept on an in-memory shelf (never on disk) and the player fetches them by
  one-off token from `/ragha_preview_video/clip/<token>`. The node no longer uses the websocket image-preview messages.
- **Cached runs show the preview again** without re-encoding, so `refresh_every_run` is now **off by default**.
- **New player** (`web/ragha_preview_video.js`): written from scratch, loads clips when the node reports its output and
  shows a message when a preview has expired.
- Renamed files: `web/ragha_preview_video.js` and `examples/ragha_preview_*.json`. Category is now `video/ragha`.
  The node name (`RaghaPreviewVideo`), inputs and outputs are unchanged, so saved workflows keep working.

## 1.2.1
- **Much lower RAM for long and 4K videos**: frames are now decoded, shrunk and encoded one at a time instead of being collected first.
  A 1-minute 4K video went from ~1.1 GB to ~200 MB of extra RAM, and previews are 8–16% faster. Image batches are shrunk a few frames at a time.
- **Frame-exact A/B sync**: B now follows A on every video frame (with a timer fallback), and catches up by adjusting its speed instead of jumping.
  Measured gap: 1 ms typical, under 15 ms worst (one frame at 30 fps is 33 ms).
- A VIDEO input's trim window (start and duration) is now respected for picture and sound.
- Sound is written in step with the video.
- New README section: requirements and performance guide (RAM, browser, settings for 4K), and a comparison with kijai's Fast Preview.

## 1.2.0
- **A/B compare**: new optional `images_b` / `video_b` inputs. A and B play in sync with a drag **slider** or **side by side** (`compare`). B follows A by progress, so frame N of B lines up with frame N of A.
- **Sound**: the preview plays audio from the new `audio` input, or from the A video's own audio track (`include_audio`). Use the speaker button or the player's controls to unmute.
- **Passthrough outputs**: `images`, `video` and `audio` outputs, so the node can sit in the middle of a workflow.
- **`refresh_every_run`** (on by default): turn it off when you use the outputs, so the nodes after this one are not re-run on every Run.
- New example: `fast_video_preview_compare.json` (a video against an inverted copy of itself).
- Still writes nothing to disk.

## 1.1.0
- The node is now **Ragha Preview Video**, internal name `RaghaPreviewVideo`, so its name is unique on every platform. It is still a tribute to kijai's Fast Preview (see the README).
- **Workflows saved with 1.0.0** show the old `FastVideoPreview` node as missing: delete it and add *Ragha Preview Video* in its place (same inputs and settings).
- Removed the unused `PROMPT_ID` hidden input. Previews now carry the real prompt id from the server.
- Added a registry icon.

## 1.0.0
- First release: in-memory video preview over ComfyUI's binary websocket preview channel, following the method of kijai's KJNodes Fast Preview.
- `images` and `video` inputs, `fps`, `max_size`, `quality`, `max_frames`.
- Streaming decode for `video` inputs with a memory cap.
- The node re-runs on every Run, so a preview always appears even when nothing upstream changed.
- Example workflows and GPL-3.0 licence.
