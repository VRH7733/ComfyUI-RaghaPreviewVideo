# SPDX-License-Identifier: GPL-3.0-only
"""
Self-test for the QC detector, no ComfyUI needed (torch + numpy + av only):

    python tests/test_qc.py

Builds a synthetic clip with defects planted at known frames, runs the node on it,
and checks every defect is found at the right frame and nothing else is flagged.
"""
import importlib.util
import pathlib
import sys

import numpy as np
import torch

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ragha_pack", ROOT / "__init__.py",
                                              submodule_search_locations=[str(ROOT)])
pack = importlib.util.module_from_spec(spec)
sys.modules["ragha_pack"] = pack
spec.loader.exec_module(pack)

W, H, N, FPS = 320, 180, 120, 24
yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)


def scene(i, kind=0):
    t = i / FPS
    if kind == 0:
        img = np.stack([0.5 + 0.4 * np.sin(xx / W * 6.28 + t * 2), 0.5 + 0.4 * np.sin(yy / H * 6.28 - t * 1.5),
                        0.4 + 0.2 * np.cos((xx + yy) / 150 + t)], -1)
    else:
        img = np.stack([0.3 + 0.3 * np.cos(yy / H * 9 + t * 2), 0.2 + 0.2 * np.sin(xx / W * 4 + t),
                        0.7 + 0.2 * np.sin((xx - yy) / 100 - t)], -1)
    cx = W * (0.2 + 0.6 * i / N)
    img[(xx - cx) ** 2 + (yy - H / 2) ** 2 < 20 ** 2] = [1.0, 0.95, 0.2]
    return np.clip(img, 0, 1)


def make_clip(defects=True):
    rng = np.random.default_rng(0)
    frames = []
    for i in range(N):
        f = scene(i, 0 if (i < 90 or not defects) else 1)  # scene cut at 90
        if defects:
            if 20 <= i <= 23:
                f = scene(19)  # frozen
            if i == 35:
                f = np.zeros_like(f)  # black
            if i == 45:
                f = np.clip(f + 0.6, 0, 1)  # flash
            if i == 55:
                f = rng.random(f.shape).astype(np.float32)  # glitch
            if 64 <= i <= 74:
                f = np.clip(f * (1.12 if i % 2 else 0.88), 0, 1)  # flicker
        frames.append(f)
    return torch.from_numpy(np.stack(frames).astype(np.float32))


def run(images, **kw):
    args = dict(fps=24.0, max_size=256, quality=50, max_frames=0, unique_id=str(id(images)), images=images)
    args.update(kw)
    return pack.RaghaPreviewVideo().inspect(**args)


def card_of(result):
    return result["ui"]["ragha_preview_video"][0]


def flagged(card, bit):
    return [i for i, f in enumerate(card["qc"]["flags"]) if f & bit]


def main():
    from ragha_pack.ragha import qc as Q

    bad = make_clip()
    card = card_of(run(bad))
    assert flagged(card, Q.FROZEN) == [20, 21, 22, 23], flagged(card, Q.FROZEN)
    assert flagged(card, Q.BLACK) == [35], flagged(card, Q.BLACK)
    assert flagged(card, Q.FLASH) == [45], flagged(card, Q.FLASH)
    assert flagged(card, Q.POP) == [55], flagged(card, Q.POP)
    assert flagged(card, Q.CUT) == [90], flagged(card, Q.CUT)
    fl = flagged(card, Q.FLICKER)
    assert set(range(64, 75)) <= set(fl) <= set(range(63, 76)), fl
    assert card["qc"]["verdict"] == "BAD"
    assert {c["slot"] for c in card["clips"]} == {"A", "J"}

    # picked frames: an exact frame, the last frame, the last GOOD frame
    assert torch.equal(run(bad, pick_frame=37)["result"][3][0], bad[37])
    assert card_of(run(bad, pick_frame=-1))["pick"] == N - 1
    tail = torch.cat([bad[:100], torch.zeros(6, H, W, 3)])
    assert card_of(run(tail, pick_frame=-2))["pick"] == 99

    good = card_of(run(make_clip(defects=False)))
    assert good["qc"]["verdict"] == "OK" and not any(good["qc"]["flags"]), good["qc"]

    ab = card_of(run(make_clip(defects=False), images_b=bad))
    assert ab["ab"]["worst"] in (35, 45, 55), ab["ab"]
    assert {c["slot"] for c in ab["clips"]} == {"A", "B", "J"}
    print("QC self-test passed.")


if __name__ == "__main__":
    main()
