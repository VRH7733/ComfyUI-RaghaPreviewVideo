# SPDX-License-Identifier: GPL-3.0-only
"""
Ragha Video Inspector (node id RaghaPreviewVideo).

One streaming pass over the clip does three jobs at once:
  1. encodes a small H.264 preview in memory (nothing on disk), like kijai's Fast Preview but for video;
  2. runs a QC analysis on tiny thumbnails: flicker, frozen/duplicate frames, glitch frames,
     scene cuts, black/flash frames, loop seam, and A-vs-B difference;
  3. remembers frame timestamps so the frame picker can return any frame at full resolution.

The player (web/ragha_preview_video.js) shows the clip with a QC timeline, frame stepping,
A/B compare (slider, side by side, difference, onion skin) and a "pick this frame" button
that fills `pick_frame`, so the next Run outputs that exact frame as an IMAGE.
"""

import sys
import time
import weakref

from . import qc as QC
from .clip import LOG, SHELF, ClipWriter, audio_from_input, audio_from_video
from .sources import grab_frame, open_source

COMPARE_MODES = ["slider", "side by side", "difference", "onion skin"]
UI_KEY = "ragha_preview_video"

# node id -> what the last run produced, so changing only pick_frame / compare does not re-encode
_MEMO = {}


def _ref(x):
    if x is None:
        return None
    try:
        return weakref.ref(x)
    except TypeError:
        return x


def _same(a, b):
    a = a() if isinstance(a, weakref.ref) else a
    b = b() if isinstance(b, weakref.ref) else b
    return a is b and a is not None or (a is None and b is None)


def _output_used(prompt, node, slot):
    """Is output `slot` of this node linked to anything? (True when we cannot tell.)"""
    if not isinstance(prompt, dict):
        return True
    for other in prompt.values():
        for value in (other.get("inputs") or {}).values():
            if isinstance(value, list) and len(value) == 2 and str(value[0]) == node and value[1] == slot:
                return True
    return False


def _placeholder():
    import torch

    return torch.zeros((1, 64, 64, 3))


def _resolve(index, frames):
    i = index if index >= 0 else frames + index
    return max(0, min(frames - 1, i))


def _progress(total):
    try:
        import comfy.utils

        return comfy.utils.ProgressBar(max(1, total))
    except Exception:
        return None


def _heat_lut():
    """256 colours black -> red -> yellow -> white, packed as little-endian uint32 (R | G<<8 | B<<16)."""
    import numpy as np

    v = np.clip(np.arange(256, dtype=np.float32) * (4.0 / 255.0), 0, 1)  # x4 so small jitter is visible
    r = (np.clip(v * 3, 0, 1) * 255).astype(np.uint32)
    g = (np.clip(v * 3 - 1, 0, 1) * 255).astype(np.uint32)
    b = (np.clip(v * 3 - 2, 0, 1) * 255).astype(np.uint32)
    packed = r | (g << 8) | (b << 16)
    return packed if sys.byteorder == "little" else packed.byteswap()


_LUT = None


def _heat(rgb, prev):
    """Self-difference frame: how much each pixel changed since the previous preview frame, as a heat map."""
    import numpy as np

    global _LUT
    if _LUT is None:
        _LUT = _heat_lut()
    if prev is None:
        return np.zeros_like(rgb)
    d = np.maximum(rgb, prev)
    d -= np.minimum(rgb, prev)  # |a - b| in uint8 without overflow
    level = np.maximum(np.maximum(d[..., 0], d[..., 1]), d[..., 2])
    px = np.take(_LUT, level).view(np.uint8).reshape(level.shape + (4,))
    return np.ascontiguousarray(px[..., :3])


def _build_clip(slot, src, quality, audio, analyzer, bar, bar_offset, heat=False):
    """Encode the preview (and optionally the self-difference clip) while feeding the QC analyser."""
    t0 = time.perf_counter()
    writer = ClipWriter(src.width, src.height, src.fps, quality, audio)
    # the jitter view is a diagnostic heat map: half resolution is plenty and encodes 4x faster
    hw, hh = max(2, src.width // 4 * 2), max(2, src.height // 4 * 2)
    heat_writer = ClipWriter(hw, hh, src.fps, quality) if heat else None
    shown, prev = 0, None
    try:
        for i, rgb, thumb in src.frames:
            if analyzer is not None:
                analyzer.add(thumb)
            if rgb is not None:
                writer.add(rgb)
                if heat_writer is not None:
                    half = rgb[: hh * 2 : 2, : hw * 2 : 2]
                    heat_writer.add(_heat(half, prev))
                    prev = half
            shown = i + 1
            if bar is not None and i % 8 == 0:
                bar.update_absolute(bar_offset + i)
    except Exception:
        writer.abort()
        if heat_writer is not None:
            heat_writer.abort()
        raise
    data, has_audio = writer.finish()
    heat_clip = None
    if heat_writer is not None:
        heat_data, _ = heat_writer.finish()
        heat_clip = {"slot": "J", "data": heat_data, "mime": heat_writer.mime, "audio": False, "fps": src.fps,
                     "width": hw, "height": hh, "frames": shown}
    if writer.frames == 0:
        raise ValueError(f"Ragha Video Inspector: no frames in {slot}.")
    print(f"{LOG} {slot}: {shown} frames analysed, {writer.frames} previewed, {src.width}x{src.height} "
          f"@ {src.fps:.3g} fps, {len(data) / 1e6:.2f} MB{' + sound' if has_audio else ''}, "
          f"{time.perf_counter() - t0:.1f} s")
    clip = {"slot": slot, "data": data, "mime": writer.mime, "audio": has_audio, "fps": src.fps,
            "width": src.width, "height": src.height, "frames": shown}
    return clip, heat_clip


class RaghaPreviewVideo:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "fps": ("FLOAT", {
                    "default": 16.0, "min": 1.0, "max": 120.0, "step": 0.5,
                    "tooltip": "Playback fps for 'images'. A VIDEO input keeps its own fps.",
                }),
                "max_size": ("INT", {
                    "default": 512, "min": 128, "max": 2048, "step": 64,
                    "tooltip": "Longest side of the preview in pixels. Smaller = faster. "
                               "QC and the picked frame always use the full clip.",
                }),
                "quality": ("INT", {"default": 50, "min": 1, "max": 100}),
                "max_frames": ("INT", {
                    "default": 0, "min": 0, "max": 4096,
                    "tooltip": "0 = every frame in the preview. Otherwise the preview is sub-sampled to this count. "
                               "QC still checks every frame.",
                }),
                "compare": (COMPARE_MODES, {
                    "default": "slider",
                    "tooltip": "How A and B are shown when images_b or video_b is connected. "
                               "'difference' lights up every pixel where A and B differ; 'onion skin' overlays B at 50%. "
                               "You can also switch this on the player.",
                }),
                "include_audio": ("BOOLEAN", {
                    "default": True,
                    "tooltip": "Play sound from 'audio', or else from the A video's own audio track.",
                }),
                "refresh_every_run": ("BOOLEAN", {
                    "default": False,
                    "tooltip": "Re-encode the preview on every Run even when nothing changed. Usually not needed.",
                }),
                "pick_frame": ("INT", {
                    "default": -1, "min": -2, "max": 1_000_000,
                    "tooltip": "Frame sent to 'picked_frame' at full resolution. -1 = last frame, -2 = last GOOD frame "
                               "(skips black, flash and glitch frames at the end; for image-to-video continuation). "
                               "Set it with the pin button on the player.",
                }),
                "analyze": ("BOOLEAN", {
                    "default": True,
                    "tooltip": "Run the QC check (flicker, frozen, glitch, cut, black/flash frames, loop seam, A vs B).",
                }),
            },
            "optional": {
                "images": ("IMAGE", {"tooltip": "A: frame batch."}),
                "video": ("VIDEO", {"tooltip": "A: video. Used when 'images' is not connected."}),
                "images_b": ("IMAGE", {"tooltip": "B: frame batch to compare against A."}),
                "video_b": ("VIDEO", {"tooltip": "B: video to compare against A."}),
                "audio": ("AUDIO", {"tooltip": "Sound for the preview (plays with A)."}),
            },
            "hidden": {
                "unique_id": "UNIQUE_ID",
                "prompt": "PROMPT",
            },
        }

    @classmethod
    def IS_CHANGED(cls, refresh_every_run=False, **kwargs):
        return float("NaN") if refresh_every_run else False

    RETURN_TYPES = ("IMAGE", "VIDEO", "AUDIO", "IMAGE", "STRING")
    RETURN_NAMES = ("images", "video", "audio", "picked_frame", "qc_report")
    OUTPUT_TOOLTIPS = (
        "The 'images' input, passed through unchanged.",
        "The 'video' input, passed through unchanged.",
        "The 'audio' input, or the A video's audio track.",
        "The frame chosen with pick_frame (or the pin button on the player), full resolution.",
        "QC report: verdict, flicker, frozen/glitch/cut/black/flash frames with frame numbers, loop seam, A vs B.",
    )
    FUNCTION = "inspect"
    CATEGORY = "video/ragha"
    OUTPUT_NODE = True
    DESCRIPTION = (
        "Video inspector: plays a disk-free preview on the node (like kijai's Fast Preview, for video) "
        "and checks every frame for flicker, frozen/duplicate frames, glitch frames, scene cuts, black/flash frames "
        "and the loop seam, drawn as a clickable timeline. A/B compare (slider, side by side, difference, onion skin). "
        "Pick any frame on the player and get it at full resolution from 'picked_frame' (-1 = last frame, for "
        "image-to-video continuation). The QC report is also a STRING output."
    )

    def inspect(self, fps, max_size, quality, max_frames, compare="slider", include_audio=True,
                refresh_every_run=False, pick_frame=-1, analyze=True, images=None, video=None,
                images_b=None, video_b=None, audio=None, unique_id=None, prompt=None):
        if images is None and video is None:
            raise ValueError("Ragha Video Inspector: connect either 'images' or 'video'.")
        if compare not in COMPARE_MODES:
            compare = "slider"
        node = str(unique_id)
        has_b = images_b is not None or video_b is not None
        key = dict(images=_ref(images), video=_ref(video), images_b=_ref(images_b), video_b=_ref(video_b),
                   audio=_ref(audio), settings=(fps, max_size, quality, max_frames, include_audio, analyze))

        memo = _MEMO.get(node)
        fresh = not (memo and not refresh_every_run and memo["settings"] == key["settings"]
                     and all(_same(memo[k], key[k]) for k in ("images", "video", "images_b", "video_b", "audio"))
                     and SHELF.alive(memo["tokens"]))
        if fresh:
            memo = self._run(node, key, fps, max_size, quality, max_frames, include_audio, analyze,
                             images, video, images_b, video_b, audio, has_b)
        else:
            print(f"{LOG} inputs unchanged: reusing the preview and QC (only the picked frame / view changed).")

        qc, ab, info = memo["qc"], memo["ab"], memo["info"]
        index = pick_frame
        if pick_frame == -2:
            index = QC.last_good(qc) if qc else -1
        if _output_used(prompt, node, 3):
            picked, picked_index = grab_frame(images, video, index, memo["times"])
        else:  # nothing reads picked_frame: skip decoding a full-resolution frame
            picked, picked_index = _placeholder(), _resolve(index, info["frames"])
        text = QC.report(qc, info, ab, picked_index) if qc else (
            f"Ragha Video Inspector - A: {info['frames']} frames (QC off). Picked frame: {picked_index}")

        card = dict(memo["card"])
        card.update(compare=compare, pick=picked_index, pick_setting=pick_frame, report=text)
        return {"ui": {UI_KEY: [card]}, "result": (images, video, memo["audio_out"], picked, text)}

    # -- one streaming pass per clip -------------------------------------------------
    def _run(self, node, key, fps, max_size, quality, max_frames, include_audio, analyze,
             images, video, images_b, video_b, audio, has_b):
        sound, audio_out = None, audio
        if audio is not None:
            sound = audio_from_input(audio)
        elif video is not None:
            try:
                sound = audio_from_video(video)
            except Exception as e:
                print(f"{LOG} could not read the video's audio ({e}).")
            if sound is not None:
                import torch

                audio_out = {"waveform": torch.from_numpy(sound[0]).unsqueeze(0), "sample_rate": sound[1]}

        src_a = open_source(images, video, fps, max_size, max_frames, "A")
        estimate_a = int(images.shape[0]) if images is not None else 0
        if not estimate_a:
            try:
                estimate_a = int(video.get_frame_count())
            except Exception:
                estimate_a = 0
        total = max(1, estimate_a) * (2 if has_b else 1)
        bar = _progress(total)

        an_a = QC.Analyzer(keep_small=has_b) if analyze else None
        clip_a, heat_a = _build_clip("A", src_a, quality, sound if include_audio else None, an_a, bar, 0, heat=analyze)
        clips = [clip_a]
        times = list(src_a.times)
        an_b = None
        if has_b:
            # a B frame batch plays at A's rate, so frame N of B lines up with frame N of A
            src_b = open_source(images_b, video_b, src_a.src_fps, max_size,
                                max_frames, "B")
            an_b = QC.Analyzer(keep_small=True) if analyze else None
            clips.append(_build_clip("B", src_b, quality, None, an_b, bar, clips[0]["frames"])[0])
        if heat_a is not None:
            clips.append(heat_a)  # "J": the self-difference (jitter) view of A
        if bar is not None:
            bar.update_absolute(total, total)

        qc_a = an_a.finish() if an_a else None
        ab = QC.compare(an_a.small, an_b.small) if (an_a and an_b) else None
        info = {"frames": clips[0]["frames"], "fps": src_a.src_fps, "width": src_a.src_width,
                "height": src_a.src_height}

        tokens = SHELF.shelve(node, clips)
        card = {
            "clips": [
                {"token": t, "slot": c["slot"], "mime": c["mime"], "audio": c["audio"],
                 "fps": round(c["fps"], 3), "width": c["width"], "height": c["height"], "bytes": len(c["data"]),
                 "frames": c["frames"]}
                for t, c in zip(tokens, clips)
            ],
            "frames": info["frames"],
            "src_fps": round(info["fps"], 4),
            "src_size": [info["width"], info["height"]],
        }
        if qc_a:
            v, reasons = QC.verdict(qc_a)
            card["qc"] = {k: qc_a[k] for k in ("flags", "motion", "flicker", "seam_ratio", "motion_median")}
            card["qc"].update(verdict=v, reasons=reasons)
        if ab:
            card["ab"] = {"diff": ab["diff"], "mean": ab["mean"], "psnr": ab["psnr"], "worst": ab["worst"]}

        memo = dict(key, tokens=tokens, times=times, qc=qc_a, ab=ab, info=info, card=card, audio_out=audio_out)
        _MEMO[node] = memo
        return memo
