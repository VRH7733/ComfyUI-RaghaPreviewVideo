# SPDX-License-Identifier: GPL-3.0-only
"""
Audio helpers, the in-memory H.264 encoder and the in-memory clip shelf.
Nothing here ever writes to disk.
"""

import io
import secrets
import threading
from collections import OrderedDict
from fractions import Fraction

from .sources import trim_window

LOG = "[Ragha Video Inspector]"


# ---------------------------------------------------------------------------
# Audio
# ---------------------------------------------------------------------------
def audio_from_video(video):
    """A VIDEO's audio track -> ([C,T] float32, sample_rate), or None. Honours the trim window."""
    import numpy as np

    if not hasattr(video, "get_stream_source"):  # built from components: audio is already in memory
        comps = video.get_components()
        return audio_from_input(comps.audio) if comps.audio else None

    import av

    start, duration = trim_window(video)
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


def audio_from_input(audio):
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


class ClipWriter:
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

# ---------------------------------------------------------------------------
# In-memory shelf for finished clips, served to the player over HTTP
# ---------------------------------------------------------------------------
class ClipShelf:
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

    def alive(self, tokens):
        """True when every token is still on the shelf."""
        with self._lock:
            return bool(tokens) and all(t in self._clips for t in tokens)


SHELF = ClipShelf()
