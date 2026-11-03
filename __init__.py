# SPDX-License-Identifier: GPL-3.0-only
"""
Ragha Video Inspector (pack: comfyui-ragha-preview-video, node id: RaghaPreviewVideo)

A disk-free video preview on the node (kijai's Fast Preview idea, for video) that is also a
video QC tool: every frame is checked for flicker, frozen/duplicate frames, glitch frames,
scene cuts, black/flash frames and the loop seam, shown as a clickable timeline under the
player. A/B compare (slider, side by side, difference, onion skin), a self-difference
"jitter" view, and a frame picker that outputs any frame (or the last good frame) at full
resolution for image-to-video continuation.

Code: ragha/ (sources, qc, clip encoder + in-memory shelf, node) and web/ragha_preview_video.js.
"""

from .ragha.clip import SHELF
from .ragha.node import RaghaPreviewVideo

try:
    from server import PromptServer
except Exception:  # running outside ComfyUI (tests)
    PromptServer = None

WEB_DIRECTORY = "./web"
ROUTE = "/ragha_preview_video/clip/{token}"


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

NODE_CLASS_MAPPINGS = {"RaghaPreviewVideo": RaghaPreviewVideo}
NODE_DISPLAY_NAME_MAPPINGS = {"RaghaPreviewVideo": "Ragha Video Inspector"}
__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
