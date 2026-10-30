# ComfyUI-FastVideoPreview

**A fast, disk-free video preview node for ComfyUI.**
Connect a batch of frames or a video, press Run, and a small looping preview plays right on the node. Nothing is saved to your output or temp folders.

> ## Credit where it is due: thank you, Kijai
> This node is a tribute to the **Fast Preview** node in
> [**ComfyUI-KJNodes**](https://github.com/kijai/ComfyUI-KJNodes) by
> [**kijai**](https://github.com/kijai).
> His idea — encode the preview in memory and push it to the browser through ComfyUI's own
> binary websocket channel, skipping the disk and base64/JSON overhead — is what this project is built on.
> I read how Fast Preview works and followed the same method, but for video instead of a single still image.
> If you like this node, please go and star KJNodes: it has hundreds of nodes and a huge amount of care behind it.

## What it does
- Plays a looping, muted `<video>` preview directly on the node (with play/seek controls).
- Works with an `images` batch (e.g. VAE Decode output) **or** a `video` input (e.g. Load Video).
- Shrinks and encodes in RAM (H.264 `ultrafast`, WebM fallback), so previews are quick and light.
- Streams large video inputs one frame at a time, so even a 4K clip does not fill your RAM.
- Writes **nothing** to disk.

## Install

**Option 1 — git (recommended)**
```bash
cd ComfyUI/custom_nodes
git clone https://github.com/VRH7733/ComfyUI-FastVideoPreview
```

**Option 2 — manual**
Download the repo as a ZIP, extract it, and put the `ComfyUI-FastVideoPreview` folder into `ComfyUI/custom_nodes/`.
The folder must **directly contain** `__init__.py` (watch out for a doubled `ComfyUI-FastVideoPreview/ComfyUI-FastVideoPreview` folder after extracting).

**Option 3 — ComfyUI-Manager / Comfy Registry**
Search for **FastVideoPreview** in the Manager, or run `comfy node install comfyui-fastvideopreview`.

Then restart ComfyUI and hard-refresh the browser (Ctrl+F5).
Requires PyAV (`av`), which current ComfyUI already includes. If it is missing: `pip install av`.

## Use it
1. Double-click the canvas, search **Fast Video Preview (KJ Tribute)** (category `video/preview`), and add it.
   Its internal name is `FastVideoPreviewKJTribute`. Coming from 1.0.0? See the [changelog](CHANGELOG.md).
2. Connect `images` (for example from *VAE Decode*) or `video` (for example from *Load Video*).
3. Run the workflow. The preview appears on the node.

Example workflows are in [`examples/`](examples):
- `fast_video_preview_images.json` — Empty Image → Fast Video Preview (KJ Tribute) (no model or GPU needed)
- `fast_video_preview_video.json` — Load Video → Fast Video Preview (KJ Tribute) (pick your own clip in *Load Video*)

### Inputs
| Input | Meaning |
|---|---|
| `images` | Frame batch (IMAGE). Optional if `video` is connected. |
| `video` | VIDEO input. It keeps its own fps. Used only when `images` is empty. |
| `fps` | Playback speed for `images`. |
| `max_size` | Longest side of the preview in pixels. Smaller is faster. |
| `quality` | 1–100. |
| `max_frames` | `0` keeps every frame; otherwise frames are evenly sub-sampled to this count. |

## How it works
1. Frames are downscaled and encoded in memory (H.264 via PyAV, WebM fallback).
2. The bytes are sent through ComfyUI's built-in binary websocket preview channel
   (`PREVIEW_IMAGE_WITH_METADATA`) — the same channel KJNodes' Fast Preview uses for images.
   A tiny stand-in object lets the already-encoded clip pass through the stock image sender unchanged.
3. A small JavaScript extension (`web/fast_video_preview.js`) receives the blob (`b_preview_with_metadata`),
   matches it to the node by id, re-types it as video by looking at its bytes, and plays it.

## Good to know
- Previews live only in the browser tab; reloading the page clears them. Run the node again to bring one back.
- Very long `video` inputs are sub-sampled automatically to stay under a memory cap (about 800 MB).
- **Hosted/cloud ComfyUI** (Comfy Cloud, RunningHub, and similar) generally only offers nodes that the provider has pre-installed,
  so this node will not appear there until the provider adds it.
- Tested with ComfyUI 0.43.1 and frontend 1.57.4. Newer or older frontends may behave differently — please open an issue.

## Troubleshooting
| Symptom | Fix |
|---|---|
| Node missing from the list | The folder is nested one level too deep, or ComfyUI was not restarted. Check the startup log's *Import times for custom nodes*. |
| Blank player | Check the terminal for a traceback and the browser console (F12) for lines containing `FastVideoPreview`. |
| `No module named 'av'` | `pip install av` in ComfyUI's Python environment. |
| Out-of-memory with a huge video | Lower `max_size` or set `max_frames` (for example 48). |

## Licence
**GPL-3.0**, the same licence as ComfyUI-KJNodes, because this node follows the structure and method of KJNodes' Fast Preview.
See [`LICENSE`](LICENSE).

## Acknowledgements
- **kijai** — [ComfyUI-KJNodes](https://github.com/kijai/ComfyUI-KJNodes), the Fast Preview method this project is based on.
- The ComfyUI team — for the open, hackable preview channel this builds on.
