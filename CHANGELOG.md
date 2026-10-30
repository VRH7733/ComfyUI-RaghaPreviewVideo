# Changelog

## 1.1.0
- The node is now **Fast Video Preview (KJ Tribute)**, internal name `FastVideoPreviewKJTribute`, so its name is unique on every platform and carries the tribute to kijai.
- **Workflows saved with 1.0.0** show the old `FastVideoPreview` node as missing: delete it and add *Fast Video Preview (KJ Tribute)* in its place (same inputs and settings).
- Removed the unused `PROMPT_ID` hidden input. Previews now carry the real prompt id from the server.
- Added a registry icon.

## 1.0.0
- First release: in-memory video preview over ComfyUI's binary websocket preview channel, following the method of kijai's KJNodes Fast Preview.
- `images` and `video` inputs, `fps`, `max_size`, `quality`, `max_frames`.
- Streaming decode for `video` inputs with a memory cap.
- The node re-runs on every Run, so a preview always appears even when nothing upstream changed.
- Example workflows and GPL-3.0 licence.
