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

Because this follows KJNodes' structure, it is released under the same licence,
GPL-3.0 (see LICENSE).

Install: put this folder in ComfyUI/custom_nodes/ and restart ComfyUI.
"""

import io
import json
import struct
from fractions import Fraction

try:
    from server import PromptServer, BinaryEventTypes
except ImportError:  # running outside ComfyUI
    PromptServer = None
    BinaryEventTypes = None

WEB_DIRECTORY = "./web"
LOG = "[Ragha Preview Video]"


# ---------------------------------------------------------------------------
# Frame preparation + in-memory encoders
# ---------------------------------------------------------------------------
def _prepare_frames(images, max_size, max_frames, fps):
    """[B,H,W,C] float 0..1 tensor -> ([B,h,w,3] uint8 ndarray, effective fps)."""
    import torch
    import torch.nn.functional as F

    total = images.shape[0]
    if 0 < max_frames < total:
        idx = torch.linspace(0, total - 1, max_frames).round().long()
        images = images[idx]
        fps = fps * images.shape[0] / total  # keep real-time duration

    images = images[..., :3]
    h, w = images.shape[1], images.shape[2]
    scale = min(1.0, max_size / max(h, w))
    nh = max(2, int(round(h * scale / 2)) * 2)  # yuv420p needs even sizes
    nw = max(2, int(round(w * scale / 2)) * 2)

    x = images.permute(0, 3, 1, 2).float()
    if (nh, nw) != (h, w):
        x = F.interpolate(x, size=(nh, nw), mode="bilinear", antialias=True, align_corners=False)
    x = (x.clamp(0, 1) * 255).round().to(torch.uint8)
    frames = x.permute(0, 2, 3, 1).contiguous().cpu().numpy()
    return frames, max(fps, 0.1)


class _KeepOpen(io.BytesIO):
    """PyAV closes the file object it writes to; keep the bytes readable."""

    def close(self):
        pass


def _encode_av(frames, fps, container_format, codec, options, audio=None):
    """Encode frames (and optional (samples [C,T] float32, rate) audio) to bytes."""
    import av
    import numpy as np

    h, w = frames.shape[1], frames.shape[2]
    buf = _KeepOpen()
    container = av.open(buf, mode="w", format=container_format)
    try:
        stream = container.add_stream(codec, rate=Fraction(fps).limit_denominator(1000))
        stream.width = w
        stream.height = h
        stream.pix_fmt = "yuv420p"
        stream.options = options

        astream = samples = None
        if audio is not None:
            samples, rate = audio
            samples = samples[:2]  # previews are mono or stereo
            samples = samples[:, : int(round(len(frames) / fps * rate))]  # match the video length
            if samples.shape[1] > 0:
                layout = "mono" if samples.shape[0] == 1 else "stereo"
                astream = container.add_stream("aac", rate=rate, layout=layout)

        for f in frames:
            for packet in stream.encode(av.VideoFrame.from_ndarray(f, format="rgb24")):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)

        if astream is not None:
            frame = av.AudioFrame.from_ndarray(
                np.ascontiguousarray(samples, dtype=np.float32), format="fltp", layout=astream.layout.name
            )
            frame.sample_rate = astream.rate
            for packet in astream.encode(frame):
                container.mux(packet)
            for packet in astream.encode():
                container.mux(packet)
    finally:
        container.close()
    return buf.getvalue(), astream is not None


def _encode(frames, fps, quality, audio=None):
    """Returns (bytes, mime, has_audio). H.264 MP4 first, WebM (no audio) as a fallback."""
    crf = int(round(40 - (quality - 1) * 22 / 99))  # quality 100 -> 18, 1 -> 40
    h264 = {"crf": str(crf), "preset": "ultrafast"}
    if audio is not None:
        try:
            data, has_audio = _encode_av(frames, fps, "mp4", "libx264", h264, audio)
            return data, "video/mp4", has_audio
        except Exception as e:
            print(f"{LOG} encode with audio failed ({e}); previewing without audio.")
    try:
        return _encode_av(frames, fps, "mp4", "libx264", h264)[0], "video/mp4", False
    except Exception as e:
        print(f"{LOG} H.264 encode failed ({e}); trying WebM.")
    data, _ = _encode_av(
        frames, fps, "webm", "libvpx",
        {"crf": str(min(crf, 63)), "b": "0", "deadline": "realtime", "cpu-used": "8"},
    )
    return data, "video/webm", False


def _frames_from_video(video, max_size, max_frames, mem_limit=800_000_000):
    """
    Stream-decode a VIDEO input and shrink every kept frame immediately, so a 4K clip
    never has to fit in RAM at full size. Returns ([N,h,w,3] uint8, effective fps).
    Frames are sub-sampled (stride) to honour max_frames and a memory cap.
    """
    import av
    import numpy as np

    container = av.open(video.get_stream_source())
    try:
        stream = container.streams.video[0]
        stream.thread_type = "AUTO"
        w, h = stream.codec_context.width, stream.codec_context.height
        scale = min(1.0, max_size / max(w, h))
        nw = max(2, int(round(w * scale / 2)) * 2)
        nh = max(2, int(round(h * scale / 2)) * 2)
        fps = float(stream.average_rate or 24)
        total = int(stream.frames or 0)
        per_frame = nw * nh * 3

        stride = 1
        if total:
            if 0 < max_frames < total:
                stride = -(-total // max_frames)
            while -(-total // stride) * per_frame > mem_limit:
                stride += 1

        frames = []
        count = 0
        for frame in container.decode(stream):
            if count % stride == 0:
                if len(frames) * per_frame >= mem_limit:
                    print(f"{LOG} memory cap reached; preview truncated.")
                    break
                if 0 < max_frames <= len(frames):
                    break
                frames.append(frame.reformat(width=nw, height=nh, format="rgb24").to_ndarray())
            count += 1
    finally:
        container.close()

    if not frames:
        raise ValueError("no frames decoded")
    return np.stack(frames), max(fps / stride, 0.1)


def _audio_from_video(video):
    """Decode a VIDEO input's audio track -> ([C,T] float32, sample_rate), or None."""
    import av
    import numpy as np

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
    return np.concatenate(chunks, axis=1).astype(np.float32), int(rate)


def _audio_from_input(audio):
    """ComfyUI AUDIO {"waveform": [B,C,T], "sample_rate"} -> ([C,T] float32, sample_rate)."""
    waveform = audio.get("waveform") if isinstance(audio, dict) else None
    if waveform is None or waveform.shape[-1] == 0:
        return None
    if waveform.dim() == 3:
        waveform = waveform[0]
    return waveform.float().cpu().numpy(), int(audio["sample_rate"])


def _make_clip(slot, images, video, fps, max_size, max_frames, quality, audio=None):
    frames = None
    if images is None and video is not None:
        try:
            # streaming decode: never holds full-size frames in RAM
            frames, fps = _frames_from_video(video, max_size, max_frames)
        except Exception as e:
            print(f"{LOG} streaming decode failed ({e}); using full decode.")
            comps = video.get_components()
            images = comps.images
            try:
                fps = float(comps.frame_rate)
            except Exception:
                pass
    if frames is None:
        frames, fps = _prepare_frames(images, max_size, max_frames, fps)
    data, mime, has_audio = _encode(frames, fps, quality, audio)
    return {"slot": slot, "data": data, "mime": mime, "audio": has_audio, "fps": fps,
            "width": int(frames.shape[2]), "height": int(frames.shape[1])}


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
                    "tooltip": "0 = every frame. Otherwise frames are sub-sampled evenly to this count.",
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
