# SPDX-License-Identifier: GPL-3.0-only
"""
Ragha Preview Video

Plays a video preview (with sound, and an optional A/B comparison) right on the node,
without writing anything to disk:

  1. frames are streamed from the source: decoded or read from the batch, shrunk and
     encoded to H.264 one at a time, so even a 1-minute 4K video needs little RAM;
  2. the finished clips are kept on a small in-memory shelf (never on disk);
  3. the node's UI output carries only short random tokens, and the player in
     web/ragha_preview_video.js fetches each clip from /ragha_preview_video/clip/<token>.

Because the tokens travel in the regular UI output, a cached run shows the last
preview again without re-encoding anything.

The idea of a disk-free preview was inspired by kijai's Fast Preview (ComfyUI-KJNodes);
see the README for credits.
"""

import io
import secrets
import threading
import time
from collections import OrderedDict
from fractions import Fraction

try:
    from server import PromptServer
except ImportError:  # running outside ComfyUI
    PromptServer = None

WEB_DIRECTORY = "./web"
LOG = "[Ragha Preview Video]"
ROUTE = "/ragha_preview_video/clip/{token}"


# ---------------------------------------------------------------------------
# Sizes
# ---------------------------------------------------------------------------
def _even(n):
    return max(2, int(round(n / 2)) * 2)  # yuv420p needs even sizes


def _fit(w, h, max_size):
    scale = min(1.0, max_size / max(w, h))
    return _even(w * scale), _even(h * scale)


# ---------------------------------------------------------------------------
# Frame sources: each returns (width, height, fps, iterator of [h,w,3] uint8 frames)
# ---------------------------------------------------------------------------
def _image_source(images, max_size, max_frames, fps, chunk=16):
    """IMAGE batch [B,H,W,C] float 0..1, shrunk a few frames at a time to keep RAM low."""
    import torch
    import torch.nn.functional as F

    total = images.shape[0]
    idx = list(range(total))
    if 0 < max_frames < total:
        idx = torch.linspace(0, total - 1, max_frames).round().long().tolist()
        fps = fps * len(idx) / total  # keep real-time duration
    h, w = images.shape[1], images.shape[2]
    nw, nh = _fit(w, h, max_size)

    def frames():
        for i in range(0, len(idx), chunk):
            x = images[idx[i:i + chunk]][..., :3].permute(0, 3, 1, 2).float()
            if (nh, nw) != (h, w):
                x = F.interpolate(x, size=(nh, nw), mode="bilinear", antialias=True, align_corners=False)
            x = (x.clamp(0, 1) * 255).round().to(torch.uint8).permute(0, 2, 3, 1).contiguous().cpu().numpy()
            yield from x

    return nw, nh, max(fps, 0.1), frames()


def _trim_window(video):
    """(start, duration) in seconds; duration 0 means 'to the end'."""
    try:
        start, duration = video.get_active_trim_window()
        return float(start or 0), float(duration or 0)
    except Exception:
        return 0.0, 0.0


def _video_source(video, max_size, max_frames):
    """VIDEO file: decode -> shrink -> hand over one frame at a time (never holds full-size frames)."""
    import av

    start, duration = _trim_window(video)
    container = av.open(video.get_stream_source())
    try:
        stream = container.streams.video[0]
        stream.thread_type = "AUTO"
        w, h = stream.codec_context.width, stream.codec_context.height
        nw, nh = _fit(w, h, max_size)
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

    def frames():
        try:
            kept = n = 0
            for frame in container.decode(stream):
                t = float(frame.pts * stream.time_base) if frame.pts is not None else None
                if t is not None and t < start - 1e-6:
                    continue
                if duration and t is not None and t >= start + duration - 1e-6:
                    break
                n += 1
                if (n - 1) % stride:
                    continue
                if 0 < max_frames <= kept:
                    break
                kept += 1
                yield frame.reformat(width=nw, height=nh, format="rgb24", interpolation="AREA").to_ndarray()
        finally:
            container.close()

    return nw, nh, max(fps / stride, 0.1), frames()


# ---------------------------------------------------------------------------
# Audio
# ---------------------------------------------------------------------------
def _audio_from_video(video):
    """A VIDEO's audio track -> ([C,T] float32, sample_rate), or None. Honours the trim window."""
    import numpy as np

    if not hasattr(video, "get_stream_source"):  # built from components: audio is already in memory
        comps = video.get_components()
        return _audio_from_input(comps.audio) if comps.audio else None

    import av

    start, duration = _trim_window(video)
    container = av.open(video.get_stream_source())
    try:
        if not container.streams.audio:
            return None
        resampler = av.AudioResampler(format="fltp")
        chunks, rate = [], None
        for frame in container.decode(container.streams.audio[0]):
            for f in resampler.resample(frame):
                rate = f.sample_rate
                chunks.append(f.to_ndarray())
        for f in resampler.resample(None):
            chunks.append(f.to_ndarray())
    finally:
        container.close()
    if not chunks or not rate:
        return None
    samples = np.concatenate(chunks, axis=1).astype(np.float32)
    a = int(start * rate)
    b = int((start + duration) * rate) if duration else samples.shape[1]
    return samples[:, a:b], int(rate)


def _audio_from_input(audio):
    """ComfyUI AUDIO {"waveform": [B,C,T], "sample_rate"} -> ([C,T] float32, sample_rate)."""
    waveform = audio.get("waveform") if isinstance(audio, dict) else None
    if waveform is None or waveform.shape[-1] == 0:
        return None
    if waveform.dim() == 3:
        waveform = waveform[0]
    return waveform.float().cpu().numpy(), int(audio["sample_rate"])


# ---------------------------------------------------------------------------
# In-memory encoder
# ---------------------------------------------------------------------------
class _KeepOpen(io.BytesIO):
    """PyAV closes the file object it writes to; keep the bytes readable."""

    def close(self):
        pass


class _ClipWriter:
    """
    Encodes frames into an in-memory H.264 MP4 as they arrive (WebM without sound if
    H.264 is unavailable), so only one frame is held at a time. Sound is written in
    step with the video and cut to the video's length.
    """

    def __init__(self, width, height, fps, quality, audio=None):
        self.width, self.height, self.fps = width, height, fps
        self.frames = 0
        self._apos = 0
        crf = int(round(40 - (quality - 1) * 22 / 99))  # quality 100 -> 18, 1 -> 40
        try:
            self._open("mp4", "libx264", {"crf": str(crf), "preset": "ultrafast"}, audio)
            self.mime = "video/mp4"
        except Exception as e:
            print(f"{LOG} H.264 encoder unavailable ({e}); using WebM without sound.")
            self._open("webm", "libvpx",
                       {"crf": str(min(crf, 63)), "b": "0", "deadline": "realtime", "cpu-used": "8"}, None)
            self.mime = "video/webm"

    def _open(self, fmt, codec, options, audio):
        import av
        import numpy as np

        self._buf = _KeepOpen()
        self._c = av.open(self._buf, mode="w", format=fmt)
        try:
            v = self._c.add_stream(codec, rate=Fraction(self.fps).limit_denominator(1000))
            v.width, v.height, v.pix_fmt = self.width, self.height, "yuv420p"
            v.options = options
            self._v, self._a = v, None
            if audio is not None and audio[0].shape[1] > 0:
                samples, rate = audio
                self._samples = np.ascontiguousarray(samples[:2], dtype=np.float32)  # mono or stereo
                self._rate = rate
                self._layout = "mono" if self._samples.shape[0] == 1 else "stereo"
                try:
                    self._a = self._c.add_stream("aac", rate=rate, layout=self._layout)
                except Exception as e:
                    print(f"{LOG} AAC encoder unavailable ({e}); previewing without sound.")
        except Exception:
            self._c.close()
            raise

    def _push_audio(self):
        import av

        upto = min(int(round(self.frames / self.fps * self._rate)), self._samples.shape[1])
        if upto <= self._apos:
            return
        f = av.AudioFrame.from_ndarray(self._samples[:, self._apos:upto].copy(), format="fltp", layout=self._layout)
        f.sample_rate = self._rate
        for p in self._a.encode(f):
            self._c.mux(p)
        self._apos = upto

    def add(self, rgb):
        import av

        for p in self._v.encode(av.VideoFrame.from_ndarray(rgb, format="rgb24")):
            self._c.mux(p)
        self.frames += 1
        if self._a is not None:
            self._push_audio()

    def finish(self):
        try:
            for p in self._v.encode():
                self._c.mux(p)
            if self._a is not None:
                self._push_audio()
                for p in self._a.encode():
                    self._c.mux(p)
        finally:
            self._c.close()
        return self._buf.getvalue(), self._a is not None and self._apos > 0

    def abort(self):
        try:
            self._c.close()
        except Exception:
            pass


def _make_clip(slot, images, video, fps, max_size, max_frames, quality, audio=None):
    t0 = time.perf_counter()
    source = None
    if images is None and video is not None:
        try:
            source = _video_source(video, max_size, max_frames)
        except Exception as e:
            # e.g. a VIDEO built from frames in this workflow: its frames are already in memory
            print(f"{LOG} {slot}: cannot stream this video ({e}); using its frames.")
            comps = video.get_components()
            images = comps.images
            try:
                fps = float(comps.frame_rate)
            except Exception:
                pass
    if source is None:
        source = _image_source(images, max_size, max_frames, fps)

    w, h, fps, frames = source
    writer = _ClipWriter(w, h, fps, quality, audio)
    try:
        for f in frames:
            writer.add(f)
    except Exception:
        writer.abort()
        raise
    data, has_audio = writer.finish()
    if writer.frames == 0:
        raise ValueError(f"Ragha Preview Video: no frames in {slot}.")
    print(f"{LOG} {slot}: {writer.frames} frames, {w}x{h} @ {fps:.3g} fps, "
          f"{len(data) / 1e6:.2f} MB{' + sound' if has_audio else ''}, {time.perf_counter() - t0:.1f} s")
    return {"slot": slot, "data": data, "mime": writer.mime, "audio": has_audio, "fps": fps,
            "width": w, "height": h}


# ---------------------------------------------------------------------------
# In-memory shelf for finished clips, served to the player over HTTP
# ---------------------------------------------------------------------------
class _ClipShelf:
    """
    Keeps finished preview clips in RAM (never on disk) under random tokens.
    Each node keeps only its latest clips; the oldest clips of other nodes are
    dropped first once the shelf is over its size budget.
    """

    def __init__(self, budget_bytes=192 * 2**20):
        self._budget = budget_bytes
        self._clips = OrderedDict()  # token -> (bytes, mime)
        self._by_node = {}           # node id -> tokens of its latest preview
        self._used = 0
        self._lock = threading.Lock()

    def _discard(self, token):
        item = self._clips.pop(token, None)
        if item is not None:
            self._used -= len(item[0])

    def shelve(self, node_id, clips):
        with self._lock:
            for token in self._by_node.pop(node_id, ()):
                self._discard(token)
            fresh = []
            for clip in clips:
                token = secrets.token_urlsafe(16)
                self._clips[token] = (clip["data"], clip["mime"])
                self._used += len(clip["data"])
                fresh.append(token)
            self._by_node[node_id] = fresh
            for token in list(self._clips):
                if self._used <= self._budget:
                    break
                if token not in fresh:
                    self._discard(token)
            return fresh

    def fetch(self, token):
        with self._lock:
            return self._clips.get(token)


SHELF = _ClipShelf()


def _add_route():
    if PromptServer is None or getattr(PromptServer, "instance", None) is None:
        return
    from aiohttp import web

    @PromptServer.instance.routes.get(ROUTE)
    async def ragha_clip(request):
        item = SHELF.fetch(request.match_info["token"])
        if item is None:
            return web.Response(status=404, text="This preview is no longer in memory. Run the node again.")
        data, mime = item
        return web.Response(body=data, content_type=mime, headers={"Cache-Control": "no-store"})


_add_route()


# ---------------------------------------------------------------------------
# The node
# ---------------------------------------------------------------------------
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
                    "tooltip": "Longest side of the preview in pixels. Smaller = faster.",
                }),
                "quality": ("INT", {"default": 50, "min": 1, "max": 100}),
                "max_frames": ("INT", {
                    "default": 0, "min": 0, "max": 4096,
                    "tooltip": "0 = every frame. Otherwise frames are sub-sampled evenly to this count "
                               "(faster previews of long videos).",
                }),
                "compare": (["slider", "side by side"], {
                    "default": "slider",
                    "tooltip": "How A and B are shown when images_b or video_b is connected.",
                }),
                "include_audio": ("BOOLEAN", {
                    "default": True,
                    "tooltip": "Play sound from 'audio', or else from the A video's own audio track.",
                }),
                "refresh_every_run": ("BOOLEAN", {
                    "default": False,
                    "tooltip": "Re-encode the preview on every Run even when nothing changed. Usually not needed: "
                               "a cached run shows the last preview again. Leave off when you use the outputs, "
                               "so the nodes after this one are not re-run.",
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
            },
        }

    @classmethod
    def IS_CHANGED(cls, refresh_every_run=False, **kwargs):
        return float("NaN") if refresh_every_run else False

    RETURN_TYPES = ("IMAGE", "VIDEO", "AUDIO")
    RETURN_NAMES = ("images", "video", "audio")
    OUTPUT_TOOLTIPS = (
        "The 'images' input, passed through unchanged.",
        "The 'video' input, passed through unchanged.",
        "The 'audio' input, or the A video's audio track.",
    )
    FUNCTION = "build_preview"
    CATEGORY = "video/ragha"
    OUTPUT_NODE = True
    DESCRIPTION = ("Plays a video preview on the node, with sound and an optional A/B comparison. "
                   "Streams 4K video with little RAM and writes nothing to disk.")

    def build_preview(self, fps, max_size, quality, max_frames, compare="slider", include_audio=True,
                      refresh_every_run=False, images=None, video=None, images_b=None, video_b=None,
                      audio=None, unique_id=None):
        if images is None and video is None:
            raise ValueError("Ragha Preview Video: connect either 'images' or 'video'.")

        sound = None
        audio_out = audio
        if audio is not None:
            sound = _audio_from_input(audio)
        elif video is not None:  # also feeds the audio output when include_audio is off
            try:
                sound = _audio_from_video(video)
            except Exception as e:
                print(f"{LOG} could not read the video's audio ({e}).")
            if sound is not None:
                import torch
                audio_out = {"waveform": torch.from_numpy(sound[0]).unsqueeze(0), "sample_rate": sound[1]}

        clips = [_make_clip("A", images, video, fps, max_size, max_frames, quality,
                            sound if include_audio else None)]
        if images_b is not None or video_b is not None:
            # a B frame batch plays at A's rate, so frame N of B lines up with frame N of A
            clips.append(_make_clip("B", images_b, video_b, clips[0]["fps"], max_size, max_frames, quality))

        tokens = SHELF.shelve(str(unique_id), clips)
        card = {
            "compare": compare,
            "clips": [
                {"token": t, "slot": c["slot"], "mime": c["mime"], "audio": c["audio"],
                 "fps": round(c["fps"], 3), "width": c["width"], "height": c["height"], "bytes": len(c["data"])}
                for t, c in zip(tokens, clips)
            ],
        }
        return {"ui": {"ragha_preview_video": [card]}, "result": (images, video, audio_out)}


NODE_CLASS_MAPPINGS = {"RaghaPreviewVideo": RaghaPreviewVideo}
NODE_DISPLAY_NAME_MAPPINGS = {"RaghaPreviewVideo": "Ragha Preview Video"}
__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
