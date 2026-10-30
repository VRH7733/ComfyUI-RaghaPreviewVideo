# SPDX-License-Identifier: GPL-3.0-only
"""
ComfyUI-FastVideoPreview: the "Ragha Preview Video" node

A "Fast Preview" for video, built as a tribute to the Fast Preview node in
kijai's ComfyUI-KJNodes (https://github.com/kijai/ComfyUI-KJNodes).

It follows the same method: encode in memory, write nothing to disk, and push the
bytes to the browser over ComfyUI's own binary websocket preview channel
(BinaryEventTypes.PREVIEW_IMAGE_WITH_METADATA). The difference is the payload:
KJNodes sends one JPEG/PNG still; this node sends small H.264 clips (with audio,
and an optional second clip for A/B comparison), and web/fast_video_preview.js
plays them on the node.

Frames are streamed: decoded (or read from the batch), shrunk and encoded one at a
time, so a 1-minute 4K video previews with a small, constant amount of RAM.

Because this follows KJNodes' structure, it is released under the same licence,
GPL-3.0 (see LICENSE).

Install: put this folder in ComfyUI/custom_nodes/ and restart ComfyUI.
"""

import io
import json
import struct
import time
from fractions import Fraction

try:
    from server import PromptServer, BinaryEventTypes
except ImportError:  # running outside ComfyUI
    PromptServer = None
    BinaryEventTypes = None

WEB_DIRECTORY = "./web"
LOG = "[Ragha Preview Video]"


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
# Sending
# ---------------------------------------------------------------------------
_MAGIC = b"RPV1"


def _pack(clips, compare):
    """
    One preview message for one or two clips:
    b"RPV1" + uint32 header length + JSON header + clip bytes (in header order).
    web/fast_video_preview.js unpacks it.
    """
    header = {"compare": compare, "clips": [
        {k: c[k] for k in ("slot", "mime", "audio", "fps", "width", "height")} | {"bytes": len(c["data"])}
        for c in clips
    ]}
    head = json.dumps(header).encode("utf-8")
    return _MAGIC + struct.pack(">I", len(head)) + head + b"".join(c["data"] for c in clips)


class _EncodedPayload:
    """
    Stands in for the PIL image that ComfyUI's preview sender expects. The sender
    only calls .save(fp, ...) to get bytes; here the bytes are already encoded
    (video clips), so save() just writes them. This lets the clips travel through
    the stock binary preview channel exactly like KJNodes' still image does.
    """

    mode = "RGB"

    def __init__(self, data, size):
        self._data = data
        self.size = size
        self.width, self.height = size

    def save(self, fp, *args, **kwargs):
        fp.write(self._data)


# ---------------------------------------------------------------------------
# The node
# ---------------------------------------------------------------------------
class FastVideoPreview:
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
                    "default": True,
                    "tooltip": "Send a new preview on every Run, even when nothing changed. "
                               "Turn off when you use the outputs, so the nodes after this one are not re-run.",
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
    def IS_CHANGED(cls, refresh_every_run=True, **kwargs):
        # A preview should appear on every Run, even when nothing upstream changed.
        # Otherwise ComfyUI's cache skips this node and no new preview is sent.
        return float("NaN") if refresh_every_run else False

    RETURN_TYPES = ("IMAGE", "VIDEO", "AUDIO")
    RETURN_NAMES = ("images", "video", "audio")
    OUTPUT_TOOLTIPS = (
        "The 'images' input, passed through unchanged.",
        "The 'video' input, passed through unchanged.",
        "The 'audio' input, or the A video's audio track.",
    )
    FUNCTION = "preview"
    CATEGORY = "video/preview"
    OUTPUT_NODE = True
    DESCRIPTION = ("Fast video preview over the binary websocket, with sound and A/B compare. "
                   "Nothing is written to disk. A tribute to kijai's Fast Preview.")

    def preview(self, fps, max_size, quality, max_frames, compare="slider", include_audio=True,
                refresh_every_run=True, images=None, video=None, images_b=None, video_b=None,
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

        data = _pack(clips, compare)
        payload = _EncodedPayload(data, (clips[0]["width"], clips[0]["height"]))

        if PromptServer is not None and unique_id is not None:
            server = PromptServer.instance
            client_supports_metadata = False
            if hasattr(BinaryEventTypes, "PREVIEW_IMAGE_WITH_METADATA"):
                try:
                    from comfy_api import feature_flags
                    client_supports_metadata = feature_flags.supports_feature(
                        server.sockets_metadata, server.client_id, "supports_preview_metadata"
                    )
                except Exception:
                    client_supports_metadata = False

            # "JPEG" only labels the stock sender's header; the bytes are video clips
            # and web/fast_video_preview.js unpacks them.
            if client_supports_metadata:
                server.send_sync(
                    BinaryEventTypes.PREVIEW_IMAGE_WITH_METADATA,
                    (
                        ("JPEG", payload, None),
                        {
                            "node_id": unique_id,
                            "display_node_id": unique_id,
                            # ComfyUI has no PROMPT_ID hidden input; the server tracks the running prompt
                            "prompt_id": getattr(server, "last_prompt_id", None) or "",
                        },
                    ),
                    server.client_id,
                )
            else:
                server.send_sync(
                    BinaryEventTypes.UNENCODED_PREVIEW_IMAGE,
                    ("JPEG", payload, None),
                    server.client_id,
                )

        return {"ui": {"fast_video_preview": [True]}, "result": (images, video, audio_out)}


NODE_CLASS_MAPPINGS = {"RaghaPreviewVideo": FastVideoPreview}
NODE_DISPLAY_NAME_MAPPINGS = {"RaghaPreviewVideo": "Ragha Preview Video"}
__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
