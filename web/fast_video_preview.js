// SPDX-License-Identifier: GPL-3.0-only
// Ragha Preview Video - frontend half. A tribute to kijai's KJNodes Fast Preview
// (GPL-3.0): same events (b_preview_with_metadata / b_preview), same node-matching
// idea, but the received bytes are video clips (A, optional B, optional audio),
// so they are played in <video> elements, side by side or with a compare slider.
const { app } = window.comfyAPI.app;
const { api } = window.comfyAPI.api;

const LOG = "[Ragha Preview Video]";
const BAR_H = 30;

let execId = null;
api.addEventListener("executing", (e) => { execId = e.detail ?? null; });

// The stock sender labels every blob as an image; look at the bytes instead.
function sniffMime(bytes) {
  const tag = (i, s) => [...s].every((c, k) => bytes[i + k] === c.charCodeAt(0));
  if (tag(4, "ftyp")) return "video/mp4";
  if (bytes[0] === 0x1a && bytes[1] === 0x45 && bytes[2] === 0xdf && bytes[3] === 0xa3) return "video/webm";
  return "video/mp4";
}

// b"RPV1" + uint32 header length + JSON header + clip bytes. Older payloads are one bare clip.
function unpack(buf) {
  const bytes = new Uint8Array(buf);
  const magic = String.fromCharCode(...bytes.subarray(0, 4));
  if (magic !== "RPV1") {
    return { compare: "slider", clips: [{ slot: "A", mime: sniffMime(bytes.subarray(0, 16)), audio: false, data: buf }] };
  }
  const len = new DataView(buf).getUint32(4);
  const header = JSON.parse(new TextDecoder().decode(bytes.subarray(8, 8 + len)));
  let at = 8 + len;
  for (const c of header.clips) {
    c.data = buf.slice(at, at + c.bytes);
    at += c.bytes;
  }
  return header;
}

function sameNode(id, node) {
  const a = String(id ?? "");
  const b = String(node.id);
  return a === b || a.split(":").pop() === b;
}

function makeVideo() {
  const v = document.createElement("video");
  v.loop = true;
  v.muted = true; // browsers only autoplay muted; unmute with the controls
  v.autoplay = true;
  v.playsInline = true;
  return v;
}

function fmt(t) {
  return Number.isFinite(t) ? t.toFixed(1) : "0.0";
}

app.registerExtension({
  name: "VRH7733.RaghaPreviewVideo",
  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData?.name !== "RaghaPreviewVideo") return;

    const origCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      const result = origCreated ? origCreated.apply(this, arguments) : undefined;
      const nodeRef = this;

      const wrap = document.createElement("div");
      wrap.style.cssText = "width:100%;height:100%;display:flex;flex-direction:column;overflow:hidden;";
      const stage = document.createElement("div");
      stage.style.cssText = "position:relative;flex:1;min-height:0;display:flex;align-items:center;justify-content:center;overflow:hidden;";
      const videoA = makeVideo();
      const videoB = makeVideo();
      videoA.controls = true;

      // compare slider pieces
      const divider = document.createElement("div");
      divider.style.cssText = "position:absolute;top:0;bottom:0;width:2px;margin-left:-1px;background:#fff;box-shadow:0 0 4px #000;pointer-events:none;";
      const knob = document.createElement("div");
      knob.style.cssText = "position:absolute;top:50%;left:50%;width:18px;height:18px;margin:-9px 0 0 -9px;border-radius:50%;background:#fff;box-shadow:0 0 4px #000;";
      divider.appendChild(knob);
      const badge = (text, side) => {
        const b = document.createElement("div");
        b.textContent = text;
        b.style.cssText = `position:absolute;top:4px;${side}:4px;padding:1px 6px;border-radius:4px;font:600 11px sans-serif;color:#fff;background:rgba(0,0,0,.6);pointer-events:none;`;
        return b;
      };
      const badgeA = badge("A", "left");
      const badgeB = badge("B", "right");

      // controls for compare mode (native controls would be covered by clip B)
      const bar = document.createElement("div");
      bar.style.cssText = `height:${BAR_H}px;display:none;align-items:center;gap:6px;padding:0 6px;font:12px sans-serif;color:#ddd;background:rgba(0,0,0,.35);`;
      const btn = (text, title) => {
        const b = document.createElement("button");
        b.textContent = text;
        b.title = title;
        b.style.cssText = "border:0;border-radius:4px;padding:2px 7px;background:#444;color:#fff;cursor:pointer;font:12px sans-serif;";
        return b;
      };
      const playBtn = btn("❚❚", "Play / pause");
      const seek = document.createElement("input");
      seek.type = "range";
      seek.min = "0";
      seek.max = "1000";
      seek.value = "0";
      seek.style.cssText = "flex:1;min-width:40px;";
      const time = document.createElement("span");
      time.style.cssText = "min-width:64px;text-align:right;font-variant-numeric:tabular-nums;";
      const muteBtn = btn("🔇", "Sound on / off (plays A's audio)");
      bar.append(playBtn, seek, time, muteBtn);
      wrap.append(stage, bar);

      // keep the canvas from treating clicks on the player as node drags
      for (const el of [stage, bar]) {
        for (const ev of ["pointerdown", "mousedown", "wheel", "dblclick"]) el.addEventListener(ev, (e) => e.stopPropagation());
      }

      const widget = nodeRef.addDOMWidget("preview", "fastvideopreview", wrap, {
        serialize: false,
        hideOnZoom: false,
        getMinHeight: () => nodeRef._fvpHeight || 160,
      });
      widget.computeSize = (w) => [w, nodeRef._fvpHeight || 160];
      nodeRef.setSize([Math.max(nodeRef.size[0], 320), Math.max(nodeRef.size[1], 300)]);

      let mode = "single";
      let split = 0.5;

      const fit = () => {
        try {
          if (!videoA.videoWidth) return;
          let ratio = videoA.videoHeight / videoA.videoWidth;
          if (mode === "side by side" && videoB.videoWidth) {
            ratio = Math.max(ratio, videoB.videoHeight / videoB.videoWidth) / 2;
          }
          const width = Math.max(nodeRef.size[0], 260);
          const h = Math.max(120, Math.round((width - 24) * ratio) + 8 + (mode === "single" ? 0 : BAR_H));
          if (nodeRef._fvpHeight === h) return;
          nodeRef._fvpHeight = h;
          nodeRef.setSize([width, nodeRef.computeSize()[1]]);
          app.graph.setDirtyCanvas(true, true);
        } catch (e) {
          console.warn(LOG, "resize failed", e);
        }
      };
      videoA.addEventListener("loadedmetadata", fit);
      videoB.addEventListener("loadedmetadata", fit);

      const setSplit = (x) => {
        split = Math.min(1, Math.max(0, x));
        videoB.style.clipPath = `inset(0 0 0 ${split * 100}%)`;
        divider.style.left = `${split * 100}%`;
      };
      let dragging = false;
      const fromEvent = (e) => {
        const r = stage.getBoundingClientRect();
        setSplit((e.clientX - r.left) / r.width);
      };
      stage.addEventListener("pointerdown", (e) => {
        if (mode !== "slider") return;
        dragging = true;
        stage.setPointerCapture?.(e.pointerId);
        fromEvent(e);
      });
      stage.addEventListener("pointermove", (e) => { if (dragging) fromEvent(e); });
      stage.addEventListener("pointerup", () => { dragging = false; });

      const layout = (newMode) => {
        mode = newMode;
        stage.replaceChildren();
        bar.style.display = mode === "single" ? "none" : "flex";
        videoA.controls = mode === "single";
        const fill = "position:absolute;inset:0;width:100%;height:100%;object-fit:contain;";
        if (mode === "single") {
          videoA.style.cssText = "max-width:100%;max-height:100%;";
          stage.style.cursor = "";
          stage.append(videoA);
        } else if (mode === "slider") {
          videoA.style.cssText = fill;
          videoB.style.cssText = fill + "pointer-events:none;";
          stage.style.cursor = "ew-resize";
          stage.append(videoA, videoB, divider, badgeA, badgeB);
          setSplit(split);
        } else {
          const half = "width:50%;height:100%;object-fit:contain;";
          videoA.style.cssText = half;
          videoB.style.cssText = half;
          stage.style.cursor = "";
          stage.append(videoA, videoB, badgeA, badgeB);
        }
        fit();
      };
      layout("single");

      // B follows A by progress (not seconds), so clips of different length stay frame-aligned
      const rateB = () => {
        if (videoA.duration && videoB.duration) videoB.playbackRate = videoA.playbackRate * (videoB.duration / videoA.duration);
      };
      const syncB = () => {
        if (mode === "single" || !videoA.duration || !videoB.duration) return;
        const target = (videoA.currentTime / videoA.duration) * videoB.duration;
        if (Math.abs(videoB.currentTime - target) > 0.1 * Math.max(1, videoB.playbackRate)) videoB.currentTime = target;
      };
      videoA.addEventListener("loadedmetadata", rateB);
      videoB.addEventListener("loadedmetadata", rateB);
      videoA.addEventListener("play", () => { playBtn.textContent = "❚❚"; if (mode !== "single") videoB.play().catch(() => {}); });
      videoA.addEventListener("pause", () => { playBtn.textContent = "▶"; videoB.pause(); syncB(); });
      videoA.addEventListener("seeked", syncB);
      videoA.addEventListener("ratechange", rateB);
      videoA.addEventListener("timeupdate", () => {
        if (videoA.duration) seek.value = String(Math.round((videoA.currentTime / videoA.duration) * 1000));
        time.textContent = `${fmt(videoA.currentTime)} / ${fmt(videoA.duration)} s`;
      });
      let raf = 0;
      const tick = () => {
        syncB();
        raf = requestAnimationFrame(tick);
      };
      raf = requestAnimationFrame(tick);

      playBtn.addEventListener("click", () => { videoA.paused ? videoA.play().catch(() => {}) : videoA.pause(); });
      seek.addEventListener("input", () => {
        if (videoA.duration) videoA.currentTime = (Number(seek.value) / 1000) * videoA.duration;
      });
      muteBtn.addEventListener("click", () => {
        videoA.muted = !videoA.muted;
        muteBtn.textContent = videoA.muted ? "🔇" : "🔊";
      });

      const urls = [];
      const show = async (blob) => {
        try {
          const pack = unpack(await blob.arrayBuffer());
          const a = pack.clips.find((c) => c.slot === "A") || pack.clips[0];
          const b = pack.clips.find((c) => c.slot === "B");
          const old = urls.splice(0);
          const load = (video, clip) => {
            const url = URL.createObjectURL(new Blob([clip.data], { type: clip.mime }));
            urls.push(url);
            video.src = url;
          };
          load(videoA, a);
          if (b) load(videoB, b);
          else videoB.removeAttribute("src");
          muteBtn.style.display = a.audio ? "" : "none";
          layout(b ? (pack.compare === "side by side" ? "side by side" : "slider") : "single");
          videoA.play().catch(() => {});
          if (b) videoB.play().catch(() => {});
          old.forEach((u) => URL.revokeObjectURL(u));
        } catch (e) {
          console.warn(LOG, "could not show preview", e);
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
        cancelAnimationFrame(raf);
        urls.forEach((u) => URL.revokeObjectURL(u));
        return origRemoved ? origRemoved.apply(this, arguments) : undefined;
      };
      return result;
    };
  },
});
