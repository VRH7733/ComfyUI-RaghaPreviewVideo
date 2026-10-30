// SPDX-License-Identifier: GPL-3.0-only
// Ragha Preview Video - the on-node player.
// The node's UI output lists clips by token; each clip is fetched from the server's
// in-memory shelf and played here: one clip on its own, or A and B with a compare
// slider / side by side, B kept frame-aligned with A.
const { app } = window.comfyAPI.app;
const { api } = window.comfyAPI.api;

const NODE_TYPE = "RaghaPreviewVideo";
const OUTPUT_KEY = "ragha_preview_video";
const CONTROLS_PX = 30;
const TAG = "[Ragha Preview Video]";

const css = (el, text) => { el.style.cssText = text; return el; };
const make = (tag, text = "") => css(document.createElement(tag), text);
const shield = (el) => {
  // clicks on the player must not drag or select the node underneath
  ["pointerdown", "mousedown", "wheel", "dblclick"].forEach((t) => el.addEventListener(t, (ev) => ev.stopPropagation()));
};
const secs = (t) => (Number.isFinite(t) ? t.toFixed(1) : "0.0");

class RaghaPlayer {
  constructor(node) {
    this.node = node;
    this.view = "single";
    this.split = 0.5;
    this.urls = [];
    this.loadId = 0;

    this.root = make("div", "width:100%;height:100%;display:flex;flex-direction:column;overflow:hidden;");
    this.stage = make("div", "position:relative;flex:1;min-height:0;display:flex;align-items:center;justify-content:center;overflow:hidden;");
    this.note = make("div", "position:absolute;inset:0;display:none;align-items:center;justify-content:center;text-align:center;padding:8px;font:12px sans-serif;color:#bbb;");
    this.a = this.#makeVideo();
    this.b = this.#makeVideo();
    this.edge = make("div", "position:absolute;top:0;bottom:0;width:2px;margin-left:-1px;background:#fff;box-shadow:0 0 4px #000;pointer-events:none;");
    this.edge.append(make("div", "position:absolute;top:50%;left:50%;width:18px;height:18px;margin:-9px 0 0 -9px;border-radius:50%;background:#fff;box-shadow:0 0 4px #000;"));
    this.tagA = this.#label("A", "left");
    this.tagB = this.#label("B", "right");

    this.controls = make("div", `height:${CONTROLS_PX}px;display:none;align-items:center;gap:6px;padding:0 6px;font:12px sans-serif;color:#ddd;background:rgba(0,0,0,.35);`);
    this.playBtn = this.#button("❚❚", "Play / pause");
    this.scrub = make("input", "flex:1;min-width:40px;");
    Object.assign(this.scrub, { type: "range", min: "0", max: "1000", value: "0" });
    this.clock = make("span", "min-width:64px;text-align:right;font-variant-numeric:tabular-nums;");
    this.soundBtn = this.#button("🔇", "Sound on / off (A's sound)");
    this.controls.append(this.playBtn, this.scrub, this.clock, this.soundBtn);
    this.root.append(this.stage, this.controls);
    shield(this.stage);
    shield(this.controls);

    this.#wire();
    this.#arrange("single");
  }

  #makeVideo() {
    const v = document.createElement("video");
    Object.assign(v, { loop: true, muted: true, autoplay: true, playsInline: true }); // autoplay needs muted
    v.addEventListener("loadedmetadata", () => { this.#retime(); this.#resize(); });
    return v;
  }

  #label(text, side) {
    const el = make("div", `position:absolute;top:4px;${side}:4px;padding:1px 6px;border-radius:4px;font:600 11px sans-serif;color:#fff;background:rgba(0,0,0,.6);pointer-events:none;`);
    el.textContent = text;
    return el;
  }

  #button(text, title) {
    const el = make("button", "border:0;border-radius:4px;padding:2px 7px;background:#444;color:#fff;cursor:pointer;font:12px sans-serif;");
    el.textContent = text;
    el.title = title;
    return el;
  }

  #wire() {
    const { a, b } = this;
    // dragging across the picture moves the compare line
    let dragging = false;
    const follow = (ev) => {
      const box = this.stage.getBoundingClientRect();
      this.#setSplit((ev.clientX - box.left) / box.width);
    };
    this.stage.addEventListener("pointerdown", (ev) => {
      if (this.view !== "slider") return;
      dragging = true;
      this.stage.setPointerCapture?.(ev.pointerId);
      follow(ev);
    });
    this.stage.addEventListener("pointermove", (ev) => dragging && follow(ev));
    this.stage.addEventListener("pointerup", () => { dragging = false; });

    this.playBtn.addEventListener("click", () => (a.paused ? a.play().catch(() => {}) : a.pause()));
    this.scrub.addEventListener("input", () => {
      if (a.duration) a.currentTime = (Number(this.scrub.value) / 1000) * a.duration;
    });
    this.soundBtn.addEventListener("click", () => {
      a.muted = !a.muted;
      this.soundBtn.textContent = a.muted ? "🔇" : "🔊";
    });

    a.addEventListener("play", () => { this.playBtn.textContent = "❚❚"; if (this.view !== "single") b.play().catch(() => {}); });
    a.addEventListener("pause", () => { this.playBtn.textContent = "▶"; b.pause(); this.#align(); });
    a.addEventListener("seeked", () => this.#align());
    a.addEventListener("ratechange", () => this.#retime());
    a.addEventListener("timeupdate", () => {
      this.#align();
      if (a.duration) this.scrub.value = String(Math.round((a.currentTime / a.duration) * 1000));
      this.clock.textContent = `${secs(a.currentTime)} / ${secs(a.duration)} s`;
    });

    // keep B aligned on every frame A shows; a slow timer covers browsers without frame callbacks
    const perFrame = () => {
      this.#align();
      this.frameHandle = a.requestVideoFrameCallback(perFrame);
    };
    if (a.requestVideoFrameCallback) this.frameHandle = a.requestVideoFrameCallback(perFrame);
    this.timer = setInterval(() => this.#align(), 250);
  }

  // B's natural speed so both clips reach their end together
  #baseRate() {
    const { a, b } = this;
    return a.duration && b.duration ? a.playbackRate * (b.duration / a.duration) : a.playbackRate;
  }

  #retime() {
    this.b.playbackRate = this.#baseRate();
  }

  // align B to A by progress; small gaps are closed by speed, large ones by a jump
  #align() {
    const { a, b } = this;
    if (this.view === "single" || !a.duration || !b.duration) return;
    const want = (a.currentTime / a.duration) * b.duration;
    let gap = want - b.currentTime;
    if (gap > b.duration / 2) gap -= b.duration; // across the loop point
    else if (gap < -b.duration / 2) gap += b.duration;
    const base = this.#baseRate();
    if (a.paused || Math.abs(gap) > 0.25) {
      if (Math.abs(gap) > 0.01) b.currentTime = want;
      b.playbackRate = base;
      return;
    }
    b.playbackRate = base * (1 + Math.max(-0.2, Math.min(0.2, gap * 3)));
  }

  #setSplit(x) {
    this.split = Math.min(1, Math.max(0, x));
    this.b.style.clipPath = `inset(0 0 0 ${this.split * 100}%)`;
    this.edge.style.left = `${this.split * 100}%`;
  }

  #arrange(view) {
    const { a, b, stage } = this;
    this.view = view;
    stage.replaceChildren(this.note);
    this.controls.style.display = view === "single" ? "none" : "flex";
    a.controls = view === "single";
    const cover = "position:absolute;inset:0;width:100%;height:100%;object-fit:contain;";
    stage.style.cursor = view === "slider" ? "ew-resize" : "";
    if (view === "single") {
      css(a, "max-width:100%;max-height:100%;");
      stage.append(a);
    } else if (view === "slider") {
      css(a, cover);
      css(b, cover + "pointer-events:none;");
      stage.append(a, b, this.edge, this.tagA, this.tagB);
      this.#setSplit(this.split);
    } else {
      css(a, "width:50%;height:100%;object-fit:contain;");
      css(b, "width:50%;height:100%;object-fit:contain;");
      stage.append(a, b, this.tagA, this.tagB);
    }
    this.#resize();
  }

  #resize() {
    try {
      const { a, b, node } = this;
      if (!a.videoWidth) return;
      let ratio = a.videoHeight / a.videoWidth;
      if (this.view === "side by side" && b.videoWidth) ratio = Math.max(ratio, b.videoHeight / b.videoWidth) / 2;
      const width = Math.max(node.size[0], 260);
      const height = Math.max(120, Math.round((width - 24) * ratio) + 8 + (this.view === "single" ? 0 : CONTROLS_PX));
      if (node._raghaHeight === height) return;
      node._raghaHeight = height;
      node.setSize([width, node.computeSize()[1]]);
      app.graph.setDirtyCanvas(true, true);
    } catch (err) {
      console.warn(TAG, "resize failed", err);
    }
  }

  #say(text) {
    this.note.textContent = text;
    this.note.style.display = text ? "flex" : "none";
  }

  async #download(clip) {
    const res = await api.fetchApi(`/ragha_preview_video/clip/${encodeURIComponent(clip.token)}`, { cache: "no-store" });
    if (!res.ok) throw new Error(res.status === 404 ? "expired" : `HTTP ${res.status}`);
    const blob = await res.blob();
    return URL.createObjectURL(blob.type ? blob : new Blob([blob], { type: clip.mime }));
  }

  // card = { compare, clips: [{ token, slot, mime, audio, ... }] } from the node's UI output
  async show(card) {
    const ticket = ++this.loadId;
    const clipA = card?.clips?.find((c) => c.slot === "A");
    const clipB = card?.clips?.find((c) => c.slot === "B");
    if (!clipA) return;
    try {
      const [urlA, urlB] = await Promise.all([this.#download(clipA), clipB ? this.#download(clipB) : null]);
      if (ticket !== this.loadId) {
        [urlA, urlB].forEach((u) => u && URL.revokeObjectURL(u));
        return; // a newer preview arrived while this one was loading
      }
      const stale = this.urls.splice(0, this.urls.length, ...[urlA, urlB].filter(Boolean));
      this.#say("");
      this.a.src = urlA;
      if (urlB) this.b.src = urlB;
      else this.b.removeAttribute("src");
      this.soundBtn.style.display = clipA.audio ? "" : "none";
      this.#arrange(clipB ? (card.compare === "side by side" ? "side by side" : "slider") : "single");
      this.a.play().catch(() => {});
      if (urlB) this.b.play().catch(() => {});
      stale.forEach((u) => URL.revokeObjectURL(u));
    } catch (err) {
      if (ticket !== this.loadId) return;
      this.#say(err.message === "expired" ? "This preview is no longer in memory. Run the node again." : "Could not load the preview.");
      console.warn(TAG, "could not load preview", err);
    }
  }

  dispose() {
    if (this.frameHandle) this.a.cancelVideoFrameCallback?.(this.frameHandle);
    clearInterval(this.timer);
    this.urls.forEach((u) => URL.revokeObjectURL(u));
    this.urls = [];
  }
}

app.registerExtension({
  name: "Ragha.PreviewVideo",
  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData?.name !== NODE_TYPE) return;
    const proto = nodeType.prototype;

    const created = proto.onNodeCreated;
    proto.onNodeCreated = function () {
      const out = created?.apply(this, arguments);
      this.raghaPlayer = new RaghaPlayer(this);
      const widget = this.addDOMWidget("ragha_player", "ragha_player", this.raghaPlayer.root, {
        serialize: false,
        hideOnZoom: false,
        getMinHeight: () => this._raghaHeight || 160,
      });
      widget.computeSize = (w) => [w, this._raghaHeight || 160];
      this.setSize([Math.max(this.size[0], 320), Math.max(this.size[1], 300)]);
      return out;
    };

    const executed = proto.onExecuted;
    proto.onExecuted = function (output) {
      const out = executed?.apply(this, arguments);
      const card = output?.[OUTPUT_KEY]?.[0];
      if (card) this.raghaPlayer?.show(card);
      return out;
    };

    const removed = proto.onRemoved;
    proto.onRemoved = function () {
      this.raghaPlayer?.dispose();
      return removed?.apply(this, arguments);
    };
  },
});
