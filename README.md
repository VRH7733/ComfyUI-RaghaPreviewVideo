# Ragha Preview Video

**A video preview player for ComfyUI that lives on the node: sound, A/B compare, 4K-ready, nothing saved to disk.**
Connect a batch of frames or a video, press Run, and a small looping preview plays right on the node, with its sound.
Connect a second clip and compare the two with a drag slider or side by side.

> ## Thank you, Kijai
> This node was inspired by the **Fast Preview** node in
> [**ComfyUI-KJNodes**](https://github.com/kijai/ComfyUI-KJNodes) by [**kijai**](https://github.com/kijai):
> his idea that a preview does not need to be written to disk is what started this project.
> Ragha Preview Video is its own implementation, built for video (streaming decode, an in-memory clip shelf,
> its own player with sound and A/B compare). If you like it, please go and star KJNodes too.

## What it does
- Plays a looping preview directly on the node, with play/seek controls.
- Works with an `images` batch (e.g. VAE Decode output) **or** a `video` input (e.g. Load Video).
- **Sound:** plays the `audio` input, or the video's own audio track. It starts muted (browsers only autoplay muted video); click the speaker to listen.
- **A/B compare:** connect `images_b` or `video_b` and the two clips play frame-aligned, with a drag **slider** or **side by side**.
  Good for before/after checks of upscaling, interpolation, colour grading or a new seed.
- **Passthrough:** `images`, `video` and `audio` outputs let you put the node in the middle of a workflow.
- **4K-ready:** video is streamed one frame at a time (decode, shrink, encode), so a 1-minute 4K video uses about 200 MB of RAM.
- **Nothing on disk:** clips are kept in memory only, never in your output or temp folders.
- **Cached runs are free:** when nothing changed, ComfyUI reuses the result and the node shows the last preview again without re-encoding.

## Install

**Option 1 — ComfyUI-Manager / Comfy Registry**
Search for **Ragha Preview Video** in the Manager, or run `comfy node install comfyui-ragha-preview-video`.

**Option 2 — git**
```bash
cd ComfyUI/custom_nodes
git clone https://github.com/VRH7733/ComfyUI-RaghaPreviewVideo
```

**Option 3 — manual**
Download the repo as a ZIP, extract it, and put the folder into `ComfyUI/custom_nodes/`.
The folder must **directly contain** `__init__.py` (watch out for a doubled folder after extracting).

Then restart ComfyUI and hard-refresh the browser (Ctrl+F5).

## Use it
1. Double-click the canvas, search **Ragha Preview Video** (category `video/ragha`), and add it.
   Its internal name is `RaghaPreviewVideo`. Coming from 1.0.0? See the [changelog](CHANGELOG.md).
2. Connect `images` (for example from *VAE Decode*) or `video` (for example from *Load Video*).
   Optionally connect `images_b` / `video_b` to compare, and `audio` for sound.
3. Run the workflow. The preview appears on the node. In compare mode, drag across the picture to move the slider.

Example workflows are in [`examples/`](examples):
- `ragha_preview_images.json` — Empty Image → Ragha Preview Video (no model or GPU needed)
- `ragha_preview_video.json` — Load Video → Ragha Preview Video, with the video's sound (pick your own clip in *Load Video*)
- `ragha_preview_compare.json` — A/B compare: a video against an inverted copy of itself

### Inputs
| Input | Meaning |
|---|---|
| `images` | A: frame batch (IMAGE). Optional if `video` is connected. |
| `video` | A: VIDEO input. It keeps its own fps and trim. Used only when `images` is empty. |
| `images_b` / `video_b` | B: a second clip to compare against A. A B frame batch plays at A's frame rate. |
| `audio` | AUDIO to play with A. Without it, the A video's own audio track is used. |
| `fps` | Playback speed for `images`. |
| `max_size` | Longest side of the preview in pixels. Smaller is faster. |
| `quality` | 1–100. |
| `max_frames` | `0` keeps every frame; otherwise frames are evenly sub-sampled to this count (the duration stays the same). |
| `compare` | `slider` or `side by side`, used when B is connected. |
| `include_audio` | Put the sound in the preview. |
| `refresh_every_run` | Off by default. On: re-encode on every Run even when nothing changed (the nodes after it then re-run too). |

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

| Input video | Preview built in | Extra RAM used by the node | Preview size |
|---|---|---|---|
| 4K, 15 s | 9 s | ~180 MB | 0.5 MB |
| 4K, 30 s | 16–22 s | ~185 MB | 1.1 MB |
| 4K, 60 s | 29–35 s | ~90–195 MB | 2.2 MB |
| 4K, 30 s, A/B compare (two videos) | 27–33 s | ~175 MB | 1.7 MB |
| 720p, 81-frame IMAGE batch (e.g. a generated clip) | 2.6 s | ~30 MB on top of the batch itself | 0.01 MB |

(Ranges are from repeated runs; the laptop was also running a browser playing the previews.)

- RAM stays about the same **whatever the video length**: frames are decoded, shrunk and encoded one at a time.
- Finished clips are kept in memory (up to ~192 MB in total, oldest dropped first; each node keeps only its latest preview).
- Time is mostly spent **decoding** the 4K source, so it grows with video length and is faster on CPUs with more cores.
  A modern 6–8-core CPU should be roughly 2–4× faster than the numbers above.
- Minimum for 1-minute 4K: **8 GB system RAM** (ComfyUI itself plus the node). 16 GB is comfortable alongside models.

**Settings for long or 4K videos**
| You want | Set |
|---|---|
| Faster previews | `max_size` 256 or 384 |
| Lighter previews of long videos | `max_frames` 240 (keeps the full duration, fewer frames) |
| Smaller previews | `quality` 30–40 |

## Compared with kijai's Fast Preview
| | Fast Preview (KJNodes) | Ragha Preview Video |
|---|---|---|
| Writes files to output or temp | No | No |
| What you see | One still image (the first frame) | The whole clip playing, with sound |
| Input | IMAGE | IMAGE or VIDEO (streamed, so 4K is fine) |
| How the preview reaches the browser | Binary websocket image message | Kept in server memory; the player fetches it by a one-off token |
| Run again with nothing changed | No new preview | Shows the last preview again (no re-encode) |
| Compare two results | No | A/B slider or side by side |
| Outputs | None | images, video, audio passthrough |

## How it works
1. **Stream:** frames come from the VIDEO file (decoded with PyAV, honouring its trim) or from the IMAGE batch, are shrunk
   and encoded to H.264 one at a time (with AAC sound written alongside; WebM without sound if H.264 is unavailable).
2. **Shelf:** the finished clips are kept in a small in-memory store under random tokens. Nothing touches the disk.
3. **Play:** the node's UI output carries only the tokens. The player (`web/ragha_preview_video.js`) fetches each clip from
   `/ragha_preview_video/clip/<token>` and plays it on the node, keeping B frame-aligned with A.

## Good to know
- Clips live in ComfyUI's memory until newer previews replace them, and are gone after a restart. Reloading the browser page clears the player; run the node again to bring a preview back.
- A VIDEO input's trim (start and duration, e.g. from a trim node) is respected, for both picture and sound.
- **Hosted/cloud ComfyUI** (Comfy Cloud, RunningHub, and similar) only offers nodes that the provider has installed.
- Tested with ComfyUI 0.43.1 and frontend 1.57.4. Newer or older frontends may behave differently — please open an issue.

## Troubleshooting
| Symptom | Fix |
|---|---|
| Node missing from the list | The folder is nested one level too deep, or ComfyUI was not restarted. Check the startup log's *Import times for custom nodes*. |
| "This preview is no longer in memory" | ComfyUI was restarted or many newer previews pushed it out. Run the node again. |
| Blank player | Check the terminal and the browser console (F12) for lines containing `Ragha Preview Video`. |
| No sound | Click the speaker button. If it is missing, the clip has no audio: connect `audio` or use a video with sound, and keep `include_audio` on. |
| `No module named 'av'` | `pip install av` in ComfyUI's Python environment. |
| Preview takes long on a 4K video | Lower `max_size` (256) or set `max_frames` (240). See the guide above. |
| A and B drift apart | Keep the ComfyUI tab in front; browsers slow down hidden tabs. |

## Licence
**GPL-3.0**. See [`LICENSE`](LICENSE).

## Acknowledgements
- **kijai** — [ComfyUI-KJNodes](https://github.com/kijai/ComfyUI-KJNodes): his Fast Preview inspired this node.
- The ComfyUI team — for the open, extensible server and frontend this builds on.
