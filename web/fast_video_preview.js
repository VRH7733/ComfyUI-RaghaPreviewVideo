// SPDX-License-Identifier: GPL-3.0-only
// Fast Video Preview - frontend half. A tribute to kijai's KJNodes Fast Preview
// (GPL-3.0): same events (b_preview_with_metadata / b_preview), same node-matching
// idea, but the received bytes are a video clip, so they are played in a <video>.
const { app } = window.comfyAPI.app;
const { api } = window.comfyAPI.api;

let execId = null;
api.addEventListener("executing", (e) => { execId = e.detail ?? null; });

// The stock sender labels every blob as an image; look at the bytes instead.
function sniffMime(bytes) {
  const tag = (i, s) => [...s].every((c, k) => bytes[i + k] === c.charCodeAt(0));
  if (tag(4, "ftyp")) return "video/mp4";
  if (bytes[0] === 0x1a && bytes[1] === 0x45 && bytes[2] === 0xdf && bytes[3] === 0xa3) return "video/webm";
  return "video/mp4";
}

function sameNode(id, node) {
  const a = String(id ?? "");
  const b = String(node.id);
  return a === b || a.split(":").pop() === b;
}

function fitToVideo(node, video) {
  try {
    if (!video.videoWidth) return;
    const ratio = video.videoHeight / video.videoWidth;
    const width = Math.max(node.size[0], 260);
    const h = Math.max(120, Math.round((width - 24) * ratio) + 8);
    if (node._fvpHeight === h) return;
    node._fvpHeight = h;
    node.setSize([width, node.computeSize()[1]]);
    app.graph.setDirtyCanvas(true, true);
  } catch (e) {
    console.warn("[FastVideoPreview] resize failed", e);
  }
}

app.registerExtension({
  name: "FastVideoPreview",
  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData?.name !== "FastVideoPreview") return;

    const origCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      const result = origCreated ? origCreated.apply(this, arguments) : undefined;
      const nodeRef = this;

      const wrap = document.createElement("div");
      wrap.style.cssText =
        "width:100%;height:100%;display:flex;align-items:center;justify-content:center;overflow:hidden;";
      const video = document.createElement("video");
      video.loop = true;
      video.muted = true;
      video.autoplay = true;
      video.controls = true;
      video.playsInline = true;
      video.style.cssText = "max-width:100%;max-height:100%;";
      wrap.appendChild(video);

      const widget = nodeRef.addDOMWidget("preview", "fastvideopreview", wrap, {
        serialize: false,
        hideOnZoom: false,
        getMinHeight: () => nodeRef._fvpHeight || 160,
      });
      widget.computeSize = (w) => [w, nodeRef._fvpHeight || 160];
      video.addEventListener("loadedmetadata", () => fitToVideo(nodeRef, video));
      nodeRef.setSize([Math.max(nodeRef.size[0], 320), Math.max(nodeRef.size[1], 300)]);

      let currentUrl = null;
      const show = async (blob) => {
        try {
          const buf = await blob.arrayBuffer();
          const mime = sniffMime(new Uint8Array(buf, 0, 16));
          const url = URL.createObjectURL(new Blob([buf], { type: mime }));
          video.src = url;
          video.play().catch(() => {});
          if (currentUrl) URL.revokeObjectURL(currentUrl);
          currentUrl = url;
        } catch (e) {
          console.warn("[FastVideoPreview] could not show preview", e);
        }
      };

      const metaHandler = (e) => {
        const { blob, nodeId, displayNodeId } = e.detail;
        if (sameNode(displayNodeId || nodeId, nodeRef)) show(blob);
      };
      const plainHandler = (e) => {
        if (api.serverSupportsFeature?.("supports_preview_metadata")) return;
        if (sameNode(execId, nodeRef)) show(e.detail);
      };

      api.addEventListener("b_preview_with_metadata", metaHandler);
      api.addEventListener("b_preview", plainHandler);

      const origRemoved = nodeRef.onRemoved;
      nodeRef.onRemoved = function () {
        api.removeEventListener("b_preview_with_metadata", metaHandler);
        api.removeEventListener("b_preview", plainHandler);
        if (currentUrl) URL.revokeObjectURL(currentUrl);
        return origRemoved ? origRemoved.apply(this, arguments) : undefined;
      };
      return result;
    };
  },
});
