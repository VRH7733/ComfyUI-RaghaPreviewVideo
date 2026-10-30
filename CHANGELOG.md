# Changelog

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
