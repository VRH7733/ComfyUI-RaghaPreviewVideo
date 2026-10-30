# Ragha Preview Video (ComfyUI-FastVideoPreview)

**Ragha Preview Video: a fast, disk-free video preview node for ComfyUI, with sound and A/B compare.**
Connect a batch of frames or a video, press Run, and a small looping preview plays right on the node, with its sound.
Connect a second clip and compare the two with a slider or side by side. Nothing is saved to your output or temp folders.

> ## Credit where it is due: thank you, Kijai
> This node is a tribute to the **Fast Preview** node in
> [**ComfyUI-KJNodes**](https://github.com/kijai/ComfyUI-KJNodes) by
> [**kijai**](https://github.com/kijai).
> His idea — encode the preview in memory and push it to the browser through ComfyUI's own
> binary websocket channel, skipping the disk and base64/JSON overhead — is what this project is built on.
> I read how Fast Preview works and followed the same method, but for video instead of a single still image.
> If you like this node, please go and star KJNodes: it has hundreds of nodes and a huge amount of care behind it.

## What it does
- Plays a looping `<video>` preview directly on the node (with play/seek controls).
- Works with an `images` batch (e.g. VAE Decode output) **or** a `video` input (e.g. Load Video).
- **Sound:** plays the `audio` input, or the video's own audio track. It starts muted (browsers only autoplay muted video); click the speaker to listen.
- **A/B compare:** connect `images_b` or `video_b` and the two clips play in sync, with a drag **slider** or **side by side**.
  Good for before/after checks of upscaling, interpolation, colour grading or a new seed.
- **Passthrough:** `images`, `video` and `audio` outputs let you put the node in the middle of a workflow.
- Shrinks and encodes in RAM (H.264 `ultrafast`, WebM fallback), so previews are quick and light.
- Streams video one frame at a time (decode, shrink, encode), so a 1-minute 4K video uses about 200 MB of RAM.
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
Search for **Ragha Preview Video** (or FastVideoPreview) in the Manager, or run `comfy node install comfyui-fastvideopreview`.

Then restart ComfyUI and hard-refresh the browser (Ctrl+F5).
Requires PyAV (`av`), which current ComfyUI already includes. If it is missing: `pip install av`.

## Use it
1. Double-click the canvas, search **Ragha Preview Video** (category `video/preview`), and add it.
   Its internal name is `RaghaPreviewVideo`. Coming from 1.0.0? See the [changelog](CHANGELOG.md).
2. Connect `images` (for example from *VAE Decode*) or `video` (for example from *Load Video*).
   Optionally connect `images_b` / `video_b` to compare, and `audio` for sound.
3. Run the workflow. The preview appears on the node. In compare mode, drag across the picture to move the slider.

Example workflows are in [`examples/`](examples):
- `fast_video_preview_images.json` — Empty Image → Ragha Preview Video (no model or GPU needed)
- `fast_video_preview_video.json` — Load Video → Ragha Preview Video, with the video's sound (pick your own clip in *Load Video*)
- `fast_video_preview_compare.json` — A/B compare: a video against an inverted copy of itself

### Inputs
| Input | Meaning |
|---|---|
| `images` | A: frame batch (IMAGE). Optional if `video` is connected. |
| `video` | A: VIDEO input. It keeps its own fps. Used only when `images` is empty. |
| `images_b` / `video_b` | B: a second clip to compare against A. A B frame batch plays at A's frame rate. |
| `audio` | AUDIO to play with A. Without it, the A video's own audio track is used. |
| `fps` | Playback speed for `images`. |
| `max_size` | Longest side of the preview in pixels. Smaller is faster. |
| `quality` | 1–100. |
| `max_frames` | `0` keeps every frame; otherwise frames are evenly sub-sampled to this count. |
| `compare` | `slider` or `side by side`, used when B is connected. |
| `include_audio` | Put the sound in the preview. |
| `refresh_every_run` | Send a new preview on every Run. Turn it off when you use the outputs, so the nodes after this one are not re-run each time. |

### Outputs
| Output | Meaning |
|---|---|
| `images` | The `images` input, unchanged. |
| `video` | The `video` input, unchanged. |
| `audio` | The `audio` input, or the A video's audio track. |

An output is empty when its input is not connected (for example `images` when you only connected `video`).

## Requirements and performance guide

**Software**
- ComfyUI with the current frontend (tested: ComfyUI 0.43.1, frontend 1.57.4). No extra pip packages: it only uses `torch`, `numpy` and `av` (PyAV), which ComfyUI already installs.
- Works on CPU-only machines. A GPU is not needed for the preview itself.

**Browser** (the preview plays in the ComfyUI tab)
| Browser | Status |
|---|---|
| Chrome / Edge (Windows, macOS, Linux) | Recommended. Frame-exact A/B sync. |
| Firefox | Works. Plays H.264 using the system's codecs. |
| Safari (macOS) | Should work (H.264 is built in); not tested. |
- Previews start **muted** because browsers only autoplay muted video. Click the speaker to hear the sound.
- Keep the ComfyUI tab in front while comparing. Browsers slow down hidden tabs, and A/B sync may drift until you come back.

**RAM and speed**, measured on a low-end laptop (AMD Ryzen 3 3250U, 2 cores, 6 GB RAM, CPU only)
with 4K 30 fps phone-like videos (~67 Mbit/s, with sound), default settings (`max_size` 512, `max_frames` 0):

| Input video | Preview built in | Extra RAM used by the node | Preview size sent to the browser |
|---|---|---|---|
| 4K, 15 s | 9 s | ~180 MB | 0.5 MB |
| 4K, 30 s | 16 s | ~185 MB | 1.1 MB |
| 4K, 60 s | 29 s | ~195 MB | 2.2 MB |
| 4K, 30 s, A/B compare (two videos) | 27 s | ~180 MB | 1.7 MB |
| 720p, 81-frame IMAGE batch (e.g. a generated clip) | 2.6 s | ~30 MB on top of the batch itself | 0.01 MB |

- RAM stays about the same **whatever the video length**: frames are decoded, shrunk and encoded one at a time.
- Time is mostly spent **decoding** the 4K source, so it grows with video length and is faster on CPUs with more cores.
  A modern 6–8-core CPU should be roughly 2–4× faster than the numbers above.
- Minimum for 1-minute 4K: **8 GB system RAM** (ComfyUI itself plus the node). 16 GB is comfortable alongside models.

**Settings for long or 4K videos**
| You want | Set |
|---|---|
| Faster previews | `max_size` 256 or 384 |
| Lighter previews of long videos | `max_frames` 240 (keeps the full duration, fewer frames) |
| Smaller previews | `quality` 30–40 |
| Previews in the middle of a workflow | `refresh_every_run` off |

## Compared with kijai's Fast Preview
| | Fast Preview (KJNodes) | Ragha Preview Video |
|---|---|---|
| Sends the preview through ComfyUI's binary websocket, in memory | Yes | Yes (same channel, same sending code) |
| Writes files to output or temp | No | No |
| What you see | One still image (the first frame) | The whole clip playing, with sound |
| Input | IMAGE | IMAGE or VIDEO (streamed, so 4K is fine) |
| Compare two results | No | A/B slider or side by side |
| Outputs | None | images, video, audio passthrough |

## How it works
1. Frames are downscaled and encoded in memory (H.264 + AAC audio via PyAV, WebM fallback without audio).
2. The clips (A, and B when comparing) are packed into one message and sent through ComfyUI's built-in
   binary websocket preview channel (`PREVIEW_IMAGE_WITH_METADATA`) — the same channel KJNodes' Fast Preview uses for images.
   A tiny stand-in object lets the already-encoded bytes pass through the stock image sender unchanged.
3. A small JavaScript extension (`web/fast_video_preview.js`) receives the blob (`b_preview_with_metadata`),
   matches it to the node by id, unpacks the clips and plays them, keeping B in sync with A.

## Good to know
- Previews live only in the browser tab; reloading the page clears them. Run the node again to bring one back.
- A VIDEO input's trim (start and duration, e.g. from a trim node) is respected, for both picture and sound.
- **Hosted/cloud ComfyUI** (Comfy Cloud, RunningHub, and similar) generally only offers nodes that the provider has pre-installed,
  so this node will not appear there until the provider adds it.
- Tested with ComfyUI 0.43.1 and frontend 1.57.4. Newer or older frontends may behave differently — please open an issue.

## Troubleshooting
| Symptom | Fix |
|---|---|
| Node missing from the list | The folder is nested one level too deep, or ComfyUI was not restarted. Check the startup log's *Import times for custom nodes*. |
| Blank player | Check the terminal for a traceback and the browser console (F12) for lines containing `Ragha Preview Video`. |
| No sound | Click the speaker button. If it is missing, the clip has no audio: connect `audio` or use a video with sound, and keep `include_audio` on. |
| Nodes after it re-run every time | Turn off `refresh_every_run`. |
| `No module named 'av'` | `pip install av` in ComfyUI's Python environment. |
| Preview takes long on a 4K video | Lower `max_size` (256) or set `max_frames` (240). See the guide above. |
| A and B drift apart | Keep the ComfyUI tab in front; browsers slow down hidden tabs. |

## Licence
**GPL-3.0**, the same licence as ComfyUI-KJNodes, because this node follows the structure and method of KJNodes' Fast Preview.
See [`LICENSE`](LICENSE).

## Acknowledgements
- **kijai** — [ComfyUI-KJNodes](https://github.com/kijai/ComfyUI-KJNodes), the Fast Preview method this project is based on.
- The ComfyUI team — for the open, hackable preview channel this builds on.
