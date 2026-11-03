# SPDX-License-Identifier: GPL-3.0-only
"""
Video QC: finds the problems AI video generators typically make, frame by frame.

Works on small thumbnails that the frame sources hand over while the preview is
being encoded, so it costs almost nothing extra and never holds the whole clip.

Per-frame flags (bitmask):
  FROZEN  frame is a copy of the previous one (stutter / duplicate frame)
  CUT     scene cut: the picture changes completely
  POP     glitch frame: differs from both neighbours while they agree with each other
  FLICKER brightness jumps up/down around this frame (not a steady fade)
  BLACK   (nearly) black frame
  FLASH   very large brightness spike, or a (nearly) white frame
"""

import numpy as np

FROZEN, CUT, POP, FLICKER, BLACK, FLASH = 1, 2, 4, 8, 16, 32
FLAG_NAMES = {FROZEN: "frozen", CUT: "cut", POP: "glitch", FLICKER: "flicker", BLACK: "black", FLASH: "flash"}

_LUMA = np.array([0.299, 0.587, 0.114], dtype=np.float32)
_AB_SIDE = 48  # thumbnails kept for A/B comparison are this small


def _small(thumb):
    """Shrink a float thumbnail to at most _AB_SIDE px (block average), as uint8."""
    h, w = thumb.shape[:2]
    step = max(1, int(np.ceil(max(h, w) / _AB_SIDE)))
    if step > 1:
        hh, ww = (h // step) * step, (w // step) * step
        thumb = thumb[:hh, :ww].reshape(hh // step, step, ww // step, step, 3).mean(axis=(1, 3))
    return (np.clip(thumb, 0, 1) * 255).round().astype(np.uint8)


def _resize_nearest(img, shape):
    h, w = shape[:2]
    ys = (np.arange(h) * img.shape[0] / h).astype(int)
    xs = (np.arange(w) * img.shape[1] / w).astype(int)
    return img[ys][:, xs]


class Analyzer:
    def __init__(self, keep_small=False):
        self.keep_small = keep_small
        self.small = []
        self.luma, self.contrast, self.diff, self.d2 = [], [], [], []
        self._prev = self._prev2 = self._first = None

    def add(self, thumb):
        y = thumb @ _LUMA
        self.luma.append(float(y.mean()))
        self.contrast.append(float(y.std()))
        self.diff.append(float(np.abs(thumb - self._prev).mean()) if self._prev is not None else 0.0)
        self.d2.append(float(np.abs(thumb - self._prev2).mean()) if self._prev2 is not None else 0.0)
        if self._first is None:
            self._first = thumb
        self._prev2, self._prev = self._prev, thumb
        if self.keep_small:
            self.small.append(_small(thumb))

    @property
    def count(self):
        return len(self.luma)

    def finish(self):
        n = self.count
        luma = np.array(self.luma, dtype=np.float64)
        contrast = np.array(self.contrast, dtype=np.float64)
        diff = np.array(self.diff, dtype=np.float64)
        d2 = np.array(self.d2, dtype=np.float64)
        flags = np.zeros(n, dtype=np.int64)
        steps = diff[1:] if n > 1 else np.zeros(0)
        med = float(np.median(steps)) if steps.size else 0.0

        def local_median(i, r=8):
            lo, hi = max(1, i - r), min(n, i + r + 1)
            w = np.concatenate([diff[lo:i], diff[i + 1:hi]])
            return float(np.median(w)) if w.size else med

        local = np.array([local_median(i) if i else 0.0 for i in range(n)])

        # brightness jitter: second difference of the mean brightness (a steady fade gives 0)
        jitter = np.zeros(n)
        if n >= 3:
            jitter[1:-1] = np.abs(luma[1:-1] - (luma[:-2] + luma[2:]) / 2)

        # 1) single odd frames: frame i differs from both neighbours, the neighbours agree with each
        #    other, and the frames around them are calm (so steady alternation is flicker, not this)
        single = np.zeros(n, dtype=bool)
        for i in range(1, n - 1):
            a, b, c = diff[i], diff[i + 1], d2[i + 1]  # c: frame i+1 against frame i-1
            low = min(a, b)
            if low <= max(0.03, 3.0 * c, 3.0 * min(local[i], local[i + 1])):
                continue
            if diff[i - 1] > 0.5 * low or (i + 2 < n and diff[i + 2] > 0.5 * low):
                continue
            single[i] = True
            if luma[i] < 0.025 and contrast[i] < 0.02:
                flags[i] |= BLACK
            elif luma[i] - (luma[i - 1] + luma[i + 1]) / 2 > 0.12:
                flags[i] |= FLASH
            else:
                flags[i] |= POP
        # 2) frozen frames and scene cuts (a cut stays cut: the next frame does not go back)
        for i in range(1, n):
            d = diff[i]
            if d < 0.0012 or (d < 0.004 and d < 0.2 * med):
                flags[i] |= FROZEN
            if single[i] or (i > 1 and single[i - 1]):
                continue
            if d > max(0.10, 6.0 * local[i]):
                flags[i] |= CUT
        # 3) flicker: brightness goes up and down around the frame
        near_cut = np.zeros(n, dtype=bool)
        for i in np.nonzero(flags & CUT)[0]:
            near_cut[max(0, i - 1):i + 1] = True
        skip = near_cut.copy()
        for i in np.nonzero(single)[0]:
            skip[max(0, i - 1):i + 2] = True  # one odd frame also bends its neighbours' jitter
        for i in range(1, n - 1):
            if not skip[i] and jitter[i] > 0.012:
                flags[i] |= FLICKER
        # 4) whole black / white frames that were not caught as single odd frames (e.g. a black tail)
        for i in range(n):
            if flags[i] & (BLACK | FLASH):
                continue
            if luma[i] < 0.025 and contrast[i] < 0.02:
                flags[i] |= BLACK
            elif luma[i] > 0.97 and contrast[i] < 0.02:
                flags[i] |= FLASH

        # a black run is reported as black, not also as frozen
        flags[(flags & BLACK) > 0] &= ~FROZEN

        # black runs that touch the start/end and fade in/out (no hard cut) are intentional fades
        fades = []
        black = (flags & BLACK) > 0
        if n and black[0]:
            e = int(np.argmin(black)) if not black.all() else n
            if e >= n or not flags[e] & CUT:
                fades.extend(range(0, e))
        if n and black[-1] and not black.all():
            s0 = n - int(np.argmin(black[::-1]))
            if not flags[s0] & CUT:
                fades.extend(range(s0, n))

        usable = jitter[1:-1][~skip[1:-1]] if n >= 3 else np.zeros(0)
        flicker = float(usable.mean() * 255) if usable.size else 0.0  # in 8-bit brightness levels

        seam = None
        if n >= 3 and self._first is not None:
            seam_diff = float(np.abs(self._prev - self._first).mean())
            seam = seam_diff / max(med, 1e-3)

        return {
            "frames": n,
            "flags": flags.tolist(),
            "motion": [round(float(x) * 255, 2) for x in diff],
            "luma": [round(float(x) * 255, 1) for x in luma],
            "flicker": round(flicker, 2),
            "motion_median": round(med * 255, 2),
            "seam_ratio": None if seam is None else round(seam, 2),
            "fades": sorted(set(fades)),
        }


def compare(small_a, small_b):
    """A vs B, aligned by progress. Returns per-A-frame mean difference (levels) and PSNR (dB)."""
    na, nb = len(small_a), len(small_b)
    if not na or not nb:
        return None
    per, mse_sum = [], 0.0
    for i in range(na):
        j = round(i * (nb - 1) / (na - 1)) if na > 1 else 0
        a = small_a[i].astype(np.float32)
        b = small_b[j]
        if b.shape != small_a[i].shape:
            b = _resize_nearest(b, small_a[i].shape)
        d = a - b.astype(np.float32)
        per.append(float(np.abs(d).mean()))
        mse_sum += float((d * d).mean())
    mse = mse_sum / na
    psnr = 99.0 if mse <= 1e-9 else 10 * np.log10(255.0 ** 2 / mse)
    worst = int(np.argmax(per))
    return {"diff": [round(x, 2) for x in per], "mean": round(float(np.mean(per)), 2),
            "psnr": round(float(psnr), 1), "worst": worst}


def last_good(qc):
    """Index of the last frame that is not black, a flash or a glitch (-1 when every frame is bad)."""
    bad = BLACK | FLASH | POP
    for i in range(len(qc["flags"]) - 1, -1, -1):
        if not qc["flags"][i] & bad:
            return i
    return -1


def runs(indices):
    """[3,4,5,9] -> [(3,5),(9,9)]"""
    out = []
    for i in indices:
        if out and i == out[-1][1] + 1:
            out[-1][1] = i
        else:
            out.append([i, i])
    return [tuple(r) for r in out]


def _fmt_runs(idx, limit=6):
    parts = [f"{a}" if a == b else f"{a}-{b}" for a, b in runs(idx)]
    more = len(parts) - limit
    return ", ".join(parts[:limit]) + (f" (+{more} more)" if more > 0 else "")


def verdict(qc):
    """'OK' / 'CHECK' / 'BAD' plus the reasons, from a finish() result."""
    flags = qc["flags"]
    count = lambda f: sum(1 for x in flags if x & f)
    n = max(1, qc["frames"])
    reasons, level = [], 0
    if qc["flicker"] >= 2.0:
        reasons.append("strong flicker"); level = max(level, 2)
    elif qc["flicker"] >= 0.8 or count(FLICKER) > max(2, n // 50):
        reasons.append("flicker"); level = max(level, 1)
    if count(POP):
        reasons.append(f"{count(POP)} glitch frame(s)"); level = max(level, 2 if count(POP) > 2 else 1)
    if count(FLASH):
        reasons.append(f"{count(FLASH)} flash frame(s)"); level = max(level, 1)
    fades = set(qc.get("fades", ()))
    black = sum(1 for i, x in enumerate(flags) if x & BLACK and i not in fades)
    if black:
        reasons.append(f"{black} black frame(s)"); level = max(level, 2 if black > 2 else 1)
    frozen = count(FROZEN)
    if frozen and frozen < n - 1:
        reasons.append(f"{frozen} frozen frame(s)"); level = max(level, 2 if frozen > n // 10 else 1)
    return ["OK", "CHECK", "BAD"][level], reasons


def report(qc, info, ab=None, picked=None):
    """Plain-text QC report for the STRING output."""
    flags = qc["flags"]
    n = qc["frames"]
    pick = lambda f: [i for i, x in enumerate(flags) if x & f]
    lines = []
    lines.append(f"Ragha Video Inspector - A: {n} frames, {info['width']}x{info['height']} "
                 f"@ {info['fps']:.3g} fps ({n / max(info['fps'], 0.1):.2f} s)")
    v, reasons = verdict(qc)
    lines.append(f"Verdict: {v}" + (f" ({'; '.join(reasons)})" if reasons else " (no problems found)"))
    fl = qc["flicker"]
    lines.append(f"Flicker: {fl:.2f} brightness levels per frame "
                 f"({'steady' if fl < 0.8 else 'noticeable' if fl < 2 else 'strong'})"
                 + (f"; flagged at frames {_fmt_runs(pick(FLICKER))}" if pick(FLICKER) else ""))
    frozen = pick(FROZEN)
    if frozen and len(frozen) >= n - 1:
        lines.append("Frozen frames: the whole clip is a still image")
    else:
        lines.append(f"Frozen / duplicate frames: {len(frozen)}" + (f" at {_fmt_runs(frozen)}" if frozen else ""))
    fades = set(qc.get("fades", ()))
    for flag, label in ((POP, "Glitch frames"), (CUT, "Scene cuts"), (FLASH, "Flash frames"), (BLACK, "Black frames")):
        idx = pick(flag)
        if flag == BLACK and fades:
            idx = [i for i in idx if i not in fades]
            lines.append(f"Fade to/from black at the clip edges: frames {_fmt_runs(sorted(fades))} (not counted)")
        lines.append(f"{label}: {len(idx)}" + (f" at {_fmt_runs(idx)}" if idx else ""))
    m = qc["motion_median"]
    lines.append(f"Motion: {m:.1f} levels per frame ({'still' if m < 0.5 else 'low' if m < 3 else 'medium' if m < 10 else 'high'})")
    s = qc["seam_ratio"]
    if s is not None:
        lines.append("Loop seam: " + ("seamless" if s <= 2.0 else f"visible jump ({s:.1f}x a normal frame step)"))
    if ab:
        lines.append(f"A vs B: mean difference {ab['mean']:.2f} levels, PSNR {ab['psnr']:.1f} dB, "
                     f"most different at A frame {ab['worst']}")
    if picked is not None:
        lines.append(f"Picked frame: {picked}")
    lines.append("Frame numbers start at 0.")
    return "\n".join(lines)
