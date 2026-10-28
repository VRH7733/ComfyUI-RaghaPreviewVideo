# Changelog

## 1.0.0
- First release: in-memory video preview over ComfyUI's binary websocket preview channel, following the method of kijai's KJNodes Fast Preview.
- `images` and `video` inputs, `fps`, `max_size`, `quality`, `max_frames`.
- Streaming decode for `video` inputs with a memory cap.
- The node re-runs on every Run, so a preview always appears even when nothing upstream changed.
- Example workflows and GPL-3.0 licence.
