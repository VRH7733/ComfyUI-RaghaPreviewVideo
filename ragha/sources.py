# SPDX-License-Identifier: GPL-3.0-only
"""
Frame sources.

A source walks a clip ONE frame at a time and yields, for every source frame:

    (index, rgb, thumb)

  index  position of the frame in the clip (0-based, after any trim)
  rgb    [h, w, 3] uint8 preview frame, or None when this frame is skipped by max_frames
  thumb  [th, tw, 3] float32 0..1 thumbnail used by the QC analysis (every frame gets one)

Nothing ever holds more than a few full-size frames, so a 1-minute 4K video
needs little RAM.
"""

from dataclasses import dataclass, field

THUMB = 96  # longest side of the analysis thumbnails


def even(n):
    return max(2, int(round(n / 2)) * 2)  # yuv420p needs even sizes


def fit(w, h, max_size):
    scale = min(1.0, max_size / max(w, h))
    return even(w * scale), even(h * scale)


def thumb_size(w, h):
    scale = min(1.0, THUMB / max(w, h))
    return max(1, int(round(w * scale))), max(1, int(round(h * scale)))


def block_thumb(rgb):
    """Analysis thumbnail from an (already small) uint8 preview frame: block average, float32 0..1."""
    import numpy as np

    h, w = rgb.shape[:2]
    step = max(1, -(-max(h, w) // THUMB))
    hh, ww = (h // step) * step, (w // step) * step
    x = rgb[:hh, :ww].reshape(hh // step, step, ww // step, step, 3)
    return x.mean(axis=(1, 3), dtype=np.float32) / 255.0


@dataclass
class Source:
    width: int            # preview size
    height: int
    fps: float            # preview frame rate (lower than src_fps when max_frames skips frames)
    src_fps: float        # frame rate of the clip itself
    src_width: int
    src_height: int
    frames: object        # iterator of (index, rgb or None, thumb)
    times: list = field(default_factory=list)  # filled while iterating: seconds of each frame (video only)
    kind: str = "images"


# ---------------------------------------------------------------------------
# IMAGE batch
# ---------------------------------------------------------------------------
def image_source(images, max_size, max_frames, fps, chunk=16):
    """IMAGE batch [B,H,W,C] float 0..1, processed a few frames at a time."""
    import torch
    import torch.nn.functional as F

    total = int(images.shape[0])
    keep = None
    preview_fps = fps
    if 0 < max_frames < total:
        keep = set(torch.linspace(0, total - 1, max_frames).round().long().tolist())
        preview_fps = fps * len(keep) / total  # keep the real-time duration
    h, w = int(images.shape[1]), int(images.shape[2])
    nw, nh = fit(w, h, max_size)
    tw, th = thumb_size(w, h)

    def frames():
        for s in range(0, total, chunk):
            e = min(s + chunk, total)
            x = images[s:e][..., :3].permute(0, 3, 1, 2).float()
            thumbs = F.interpolate(x, size=(th, tw), mode="area") if (th, tw) != (h, w) else x
            thumbs = thumbs.clamp(0, 1).permute(0, 2, 3, 1).contiguous().cpu().numpy()
            picked = [i for i in range(s, e) if keep is None or i in keep]
            rgb = {}
            if picked:
                y = x[[i - s for i in picked]]
                if (nh, nw) != (h, w):
                    y = F.interpolate(y, size=(nh, nw), mode="bilinear", antialias=True, align_corners=False)
                y = (y.clamp(0, 1) * 255).round().to(torch.uint8).permute(0, 2, 3, 1).contiguous().cpu().numpy()
                rgb = dict(zip(picked, y))
            for k in range(e - s):
                yield s + k, rgb.get(s + k), thumbs[k]

    return Source(nw, nh, max(preview_fps, 0.1), max(float(fps), 0.1), w, h, frames(), kind="images")


# ---------------------------------------------------------------------------
# VIDEO
# ---------------------------------------------------------------------------
def trim_window(video):
    """(start, duration) in seconds; duration 0 means 'to the end'."""
    try:
        start, duration = video.get_active_trim_window()
        return float(start or 0), float(duration or 0)
    except Exception:
        return 0.0, 0.0


def can_stream(video):
    """Stream from the file when that gives exactly the frames the VIDEO stands for."""
    name = type(video).__name__
    if name == "VideoFromComponents":  # frames are already in memory
        return False
    if getattr(video, "_VideoFromFile__crop", None) is not None:  # cropped: let ComfyUI apply the crop
        return False
    return hasattr(video, "get_stream_source")


def video_source(video, max_size, max_frames):
    """VIDEO file: decode -> thumbnail + shrink -> hand over one frame at a time."""
    import av

    start, duration = trim_window(video)
    container = av.open(video.get_stream_source())
    try:
        stream = container.streams.video[0]
        stream.thread_type = "AUTO"
        w, h = stream.codec_context.width, stream.codec_context.height
        nw, nh = fit(w, h, max_size)
        fps = float(stream.average_rate or stream.guessed_rate or 24)
        if duration:
            total = int(duration * fps)
        elif stream.frames:
            total = int(stream.frames)
        elif container.duration:
            total = int(container.duration / av.time_base * fps)
        else:
            total = 0
        stride = -(-total // max_frames) if total and 0 < max_frames < total else 1
        if start:
            container.seek(int(start / stream.time_base), stream=stream)
    except Exception:
        container.close()
        raise

    src = Source(nw, nh, max(fps / stride, 0.1), max(fps, 0.1), w, h, None, kind="video")

    def frames():
        try:
            n = kept = 0
            for frame in container.decode(stream):
                t = float(frame.pts * stream.time_base) if frame.pts is not None else None
                if t is not None and t < start - 1e-6:
                    continue
                if duration and t is not None and t >= start + duration - 1e-6:
                    break
                # one shrink per frame: the preview frame doubles as the source of the QC thumbnail
                small = frame.reformat(width=nw, height=nh, format="rgb24", interpolation="AREA").to_ndarray()
                rgb = None
                if n % stride == 0 and not (0 < max_frames <= kept):
                    rgb = small
                    kept += 1
                src.times.append(t if t is not None else start + n / fps)
                yield n, rgb, block_thumb(small)
                n += 1
        finally:
            container.close()

    src.frames = frames()
    return src


def components_of(video):
    """(images [B,H,W,C], fps) for a VIDEO that cannot be streamed."""
    comps = video.get_components()
    try:
        fps = float(comps.frame_rate)
    except Exception:
        fps = 24.0
    return comps.images, fps


def open_source(images, video, fps, max_size, max_frames, slot="A"):
    """A Source for an IMAGE batch or a VIDEO (streamed when possible)."""
    if images is None and video is not None:
        if can_stream(video):
            try:
                return video_source(video, max_size, max_frames)
            except Exception as e:
                print(f"[Ragha Video Inspector] {slot}: cannot stream this video ({e}); using its frames.")
        images, fps = components_of(video)
    return image_source(images, max_size, max_frames, fps)


# ---------------------------------------------------------------------------
# Full-resolution single frame (the frame picker)
# ---------------------------------------------------------------------------
def grab_frame(images, video, index, times=None):
    """Frame `index` at full resolution as IMAGE [1,H,W,3]. index < 0 counts from the end."""
    import torch

    if images is None and video is not None and can_stream(video) and times:
        try:
            return _grab_video_frame(video, index, times)
        except Exception as e:
            print(f"[Ragha Video Inspector] could not seek in the video ({e}); using its frames.")
    if images is None:
        images, _ = components_of(video)
    total = int(images.shape[0])
    i = index if index >= 0 else total + index
    i = max(0, min(total - 1, i))
    return images[i:i + 1][..., :3].clone().float(), i


def _grab_video_frame(video, index, times):
    """Decode just frame `index` - through ComfyUI's own decoder when possible, so the picked frame is
    bit-identical to what Load Video -> Get Video Components would give (colour, rotation, crop, HDR)."""
    import av
    import numpy as np
    import torch

    i = index if index >= 0 else len(times) + index
    i = max(0, min(len(times) - 1, i))
    target = times[i]
    gap = (times[i] - times[i - 1]) if i > 0 else (times[1] - times[0] if len(times) > 1 else 1 / 24)
    if hasattr(video, "as_trimmed"):
        try:
            start, _ = trim_window(video)
            clip = video.as_trimmed(start_time=max(0.0, target - start - gap * 0.25), duration=gap * 0.5,
                                    strict_duration=False)
            frames = clip.get_components().images if clip is not None else None
            if frames is not None and frames.shape[0] >= 1:
                return frames[:1][..., :3].clone().float(), i
        except Exception as e:
            print(f"[Ragha Video Inspector] trim-decode of frame {i} failed ({e}); seeking instead.")

    container = av.open(video.get_stream_source())
    try:
        stream = container.streams.video[0]
        stream.thread_type = "AUTO"
        container.seek(max(0, int((target - 1.0) / stream.time_base)), stream=stream)  # keyframe before
        best = None
        for frame in container.decode(stream):
            if frame.pts is not None and float(frame.pts * stream.time_base) >= target - gap / 2:
                best = frame
                break
        if best is None:
            raise ValueError("frame not found")
        rgb = best.to_ndarray(format="rgb24")
    finally:
        container.close()
    return torch.from_numpy(np.ascontiguousarray(rgb)).float().div_(255.0).unsqueeze(0), i
