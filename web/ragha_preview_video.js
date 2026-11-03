// SPDX-License-Identifier: GPL-3.0-only
// Ragha Video Inspector - the on-node player.
//
// The node's UI output lists clips by token (A, optional B, optional J = self-difference view of A)
// plus the QC result. Each clip is fetched from the server's in-memory shelf and played here with:
//  - a verdict badge and a QC timeline (motion curve, A-vs-B curve, flagged frames); click to seek
//  - frame stepping, jump to previous / next flagged frame, speed, loop-seam check
//  - views: A, jitter (frame vs previous frame), and with B: slider, side by side, difference, onion skin
//  - a pin button that writes the current frame into `pick_frame` (Run again to get it as an IMAGE)
const { app } = window.comfyAPI.app;
const { api } = window.comfyAPI.api;

const NODE_TYPE = "RaghaPreviewVideo";
const OUTPUT_KEY = "ragha_preview_video";
const TAG = "[Ragha Video Inspector]";
const HEADER_PX = 22;
const TIMELINE_PX = 40;
const CONTROLS_PX = 28;
const LEGEND_PX = 16;

// must match ragha/qc.py
const FLAGS = [
  { bit: 2, name: "cut", color: "#ffd23f" },
  { bit: 4, name: "glitch", color: "#ff4fd8" },
  { bit: 32, name: "flash", color: "#ffffff" },
  { bit: 16, name: "black", color: "#7a7a7a" },
  { bit: 8, name: "flicker", color: "#ff8c1a" },
  { bit: 1, name: "frozen", color: "#3fa7ff" },
];
const ISSUE_BITS = 2 | 4 | 8 | 16 | 32 | 1;
const VERDICT_COLOR = { OK: "#2e9e4f", CHECK: "#c98a00", BAD: "#c0392b" };
const COMPARE_VIEWS = ["slider", "side by side", "difference", "onion skin"];

const css = (el, text) => { el.style.cssText = text; return el; };
const make = (tag, text = "") => css(document.createElement(tag), text);
const shield = (el) => {
  // clicks on the player must not drag or select the node underneath
  ["pointerdown", "mousedown", "wheel", "dblclick"].forEach((t) => el.addEventListener(t, (ev) => ev.stopPropagation()));
};
const flagNames = (f) => FLAGS.filter((x) => f & x.bit).map((x) => x.name);

class RaghaPlayer {
  constructor(node) {
    this.node = node;
    this.view = "A";
    this.split = 0.5;
    this.urls = [];
    this.loadId = 0;
    this.card = null;
    this.frame = 0;

    this.root = make("div", "width:100%;height:100%;display:flex;flex-direction:column;overflow:hidden;font:12px sans-serif;color:#ddd;outline:none;");
    this.root.tabIndex = 0;

    // header: verdict badge + frame readout
    this.header = make("div", `height:${HEADER_PX}px;display:none;align-items:center;gap:6px;padding:0 4px;white-space:nowrap;overflow:hidden;`);
    this.badge = make("span", "padding:1px 7px;border-radius:9px;font:700 11px sans-serif;color:#fff;background:#555;");
    this.reasons = make("span", "flex:1;overflow:hidden;text-overflow:ellipsis;color:#ccc;");
    this.readout = make("span", "font-variant-numeric:tabular-nums;color:#eee;");
    this.header.append(this.badge, this.reasons, this.readout);

    // stage
    this.stage = make("div", "position:relative;flex:1;min-height:0;overflow:hidden;background:#000;");
    this.layer = make("div", "position:absolute;inset:0;isolation:isolate;");
    this.note = make("div", "position:absolute;inset:0;display:flex;align-items:center;justify-content:center;text-align:center;padding:8px;color:#999;");
    this.note.textContent = "Run the workflow to inspect the video here.";
    this.a = this.#makeVideo();
    this.b = this.#makeVideo();
    this.j = this.#makeVideo();
    this.edge = make("div", "position:absolute;top:0;bottom:0;width:2px;margin-left:-1px;background:#fff;box-shadow:0 0 4px #000;pointer-events:none;");
    this.edge.append(make("div", "position:absolute;top:50%;left:50%;width:16px;height:16px;margin:-8px 0 0 -8px;border-radius:50%;background:#fff;box-shadow:0 0 4px #000;"));
    this.tagA = this.#label("A", "left");
    this.tagB = this.#label("B", "right");
    this.stage.append(this.layer, this.note);

    // timeline
    this.timeline = make("canvas", `height:${TIMELINE_PX}px;width:100%;display:none;cursor:pointer;background:#151515;`);

    // controls
    this.controls = make("div", `height:${CONTROLS_PX}px;display:none;align-items:center;gap:3px;padding:0 2px;`);
    this.prevIssue = this.#button("⏮", "Previous flagged frame");
    this.stepBack = this.#button("◀", "One frame back  (←)");
    this.playBtn = this.#button("❚❚", "Play / pause  (space)");
    this.stepFwd = this.#button("▶", "One frame forward  (→)");
    this.nextIssue = this.#button("⏭", "Next flagged frame");
    this.speed = this.#select(["0.25", "0.5", "1", "2"], "Playback speed", (v) => `${v}×`);
    this.speed.value = "1";
    this.viewSel = this.#select(["A"], "View");
    this.seamBtn = this.#button("∞", "Loop-seam check: play the last second into the first");
    this.soundBtn = this.#button("🔇", "Sound on / off (A's sound)");
    this.pinBtn = this.#button("📌", "Pick this frame: writes it to pick_frame. Run again to get it from 'picked_frame'.");
    this.goodBtn = this.#button("✓", "pick_frame = -2: the last GOOD frame (skips black / flash / glitch frames at the end)");
    const gap = make("span", "flex:1;");
    this.controls.append(this.prevIssue, this.stepBack, this.playBtn, this.stepFwd, this.nextIssue, gap,
      this.speed, this.viewSel, this.seamBtn, this.soundBtn, this.pinBtn, this.goodBtn);

    // legend
    this.legend = make("div", `height:${LEGEND_PX}px;display:none;align-items:center;gap:8px;padding:0 4px;font-size:10px;color:#aaa;white-space:nowrap;overflow:hidden;`);

    this.root.append(this.header, this.stage, this.timeline, this.controls, this.legend);
    [this.stage, this.timeline, this.controls, this.header, this.legend].forEach(shield);
    this.#wire();
    this.#arrange("A");
  }

  // ---------------------------------------------------------------- building blocks
  #makeVideo() {
    const v = document.createElement("video");
    Object.assign(v, { loop: true, muted: true, autoplay: false, playsInline: true }); // autoplay needs muted
    v.addEventListener("loadedmetadata", () => { this.#retime(); this.#resize(); this.#drawTimeline(); });
    return v;
  }

  #label(text, side) {
    const el = make("div", `position:absolute;top:4px;${side}:4px;padding:1px 6px;border-radius:4px;font:600 11px sans-serif;color:#fff;background:rgba(0,0,0,.6);pointer-events:none;z-index:3;`);
    el.textContent = text;
    return el;
  }

  #button(text, title) {
    const el = make("button", "border:0;border-radius:4px;padding:1px 5px;min-width:22px;background:#3a3a3a;color:#fff;cursor:pointer;font:12px sans-serif;line-height:20px;");
    el.textContent = text;
    el.title = title;
    return el;
  }

  #select(values, title, label = (v) => v) {
    const el = make("select", "background:#3a3a3a;color:#fff;border:0;border-radius:4px;font:11px sans-serif;height:22px;max-width:96px;");
    el.title = title;
    this.#fill(el, values, label);
    return el;
  }

  #fill(el, values, label = (v) => v) {
    el.replaceChildren(...values.map((v) => { const o = document.createElement("option"); o.value = v; o.textContent = label(v); return o; }));
  }

  // ---------------------------------------------------------------- frames and time
  get frames() { return this.card?.frames || 0; }
  get srcFps() { return this.card?.src_fps || 16; }
  get previewFps() { return this.card?.clips?.find((c) => c.slot === "A")?.fps || this.srcFps; }
  get step() { return Math.max(1, Math.round(this.srcFps / this.previewFps)); }

  #frameAt(t) {
    return Math.max(0, Math.min(this.frames - 1, Math.floor(t * this.srcFps + 1e-4)));
  }

  seekFrame(f) {
    const { a } = this;
    if (!a.duration || !this.frames) return;
    f = Math.max(0, Math.min(this.frames - 1, f));
    a.pause();
    a.currentTime = Math.min(a.duration - 1e-3, (f + 0.5) / this.srcFps);
    this.frame = f;
    this.#update();
  }

  // jump to the start of the previous / next run of flagged frames
  #issueFrom(dir) {
    const flags = this.card?.qc?.flags;
    if (!flags) return;
    const bad = (i) => i >= 0 && i < flags.length && (flags[i] & ISSUE_BITS);
    let i = this.frame;
    while (bad(i) && bad(i + dir)) i += dir; // leave the run we are in
    for (i += dir; i >= 0 && i < flags.length; i += dir) {
      if (!bad(i)) continue;
      if (dir < 0) while (bad(i - 1)) i--; // land on the first frame of that run
      this.seekFrame(i);
      return;
    }
  }

  #widget(name) {
    return this.node.widgets?.find((w) => w.name === name);
  }

  #setWidget(name, value) {
    const w = this.#widget(name);
    if (!w) return;
    w.value = value;
    w.callback?.(value);
    this.node.setDirtyCanvas?.(true, true);
  }

  // ---------------------------------------------------------------- events
  #wire() {
    const { a } = this;
    let dragging = false;
    const follow = (ev) => {
      const box = this.stage.getBoundingClientRect();
      this.#setSplit((ev.clientX - box.left) / box.width);
    };
    this.stage.addEventListener("pointerdown", (ev) => {
      this.root.focus({ preventScroll: true });
      if (this.view !== "slider") return;
      dragging = true;
      this.stage.setPointerCapture?.(ev.pointerId);
      follow(ev);
    });
    this.stage.addEventListener("pointermove", (ev) => dragging && follow(ev));
    this.stage.addEventListener("pointerup", () => { dragging = false; });

    // timeline: click / drag to seek, hover to read the flags
    let scrubbing = false;
    const frameOf = (ev) => {
      const box = this.timeline.getBoundingClientRect();
      return Math.floor(((ev.clientX - box.left) / box.width) * this.frames);
    };
    this.timeline.addEventListener("pointerdown", (ev) => {
      scrubbing = true;
      this.timeline.setPointerCapture?.(ev.pointerId);
      this.seekFrame(frameOf(ev));
    });
    this.timeline.addEventListener("pointermove", (ev) => {
      const f = Math.max(0, Math.min(this.frames - 1, frameOf(ev)));
      if (scrubbing) this.seekFrame(f);
      const flags = this.card?.qc?.flags?.[f] || 0;
      const ab = this.card?.ab?.diff?.[f];
      this.timeline.title = `frame ${f}` + (flags ? `: ${flagNames(flags).join(", ")}` : "") +
        (ab !== undefined ? `  ·  A vs B ${ab.toFixed(1)}` : "");
    });
    this.timeline.addEventListener("pointerup", () => { scrubbing = false; });

    this.playBtn.addEventListener("click", () => (a.paused ? a.play().catch(() => {}) : a.pause()));
    this.stepBack.addEventListener("click", () => this.seekFrame(this.frame - this.step));
    this.stepFwd.addEventListener("click", () => this.seekFrame(this.frame + this.step));
    this.prevIssue.addEventListener("click", () => this.#issueFrom(-1));
    this.nextIssue.addEventListener("click", () => this.#issueFrom(1));
    this.speed.addEventListener("change", () => { a.playbackRate = Number(this.speed.value); this.#retime(); });
    this.viewSel.addEventListener("change", () => {
      const v = this.viewSel.value;
      if (COMPARE_VIEWS.includes(v)) this.#setWidget("compare", v);
      this.#arrange(v);
    });
    this.seamBtn.addEventListener("click", () => {
      if (!a.duration) return;
      a.currentTime = Math.max(0, a.duration - Math.min(1, a.duration / 3));
      a.play().catch(() => {});
    });
    this.soundBtn.addEventListener("click", () => {
      a.muted = !a.muted;
      this.soundBtn.textContent = a.muted ? "🔇" : "🔊";
    });
    this.pinBtn.addEventListener("click", () => {
      this.#setWidget("pick_frame", this.frame);
      this.pinned = this.frame;
      this.#legend(`pick_frame = ${this.frame}. Run again to output it.`);
      this.#drawTimeline();
    });
    this.goodBtn.addEventListener("click", () => {
      this.#setWidget("pick_frame", -2);
      this.pinned = null;
      this.#legend("pick_frame = -2 (last good frame). Run again to output it.");
      this.#drawTimeline();
    });

    this.root.addEventListener("keydown", (ev) => {
      const keys = { ArrowLeft: () => this.seekFrame(this.frame - this.step), ArrowRight: () => this.seekFrame(this.frame + this.step),
        " ": () => (a.paused ? a.play().catch(() => {}) : a.pause()) };
      if (keys[ev.key]) { keys[ev.key](); ev.preventDefault(); ev.stopPropagation(); }
    });

    a.addEventListener("play", () => { this.playBtn.textContent = "❚❚"; this.#followers().forEach((v) => v.play().catch(() => {})); });
    a.addEventListener("pause", () => { this.playBtn.textContent = "▶"; this.#followers().forEach((v) => v.pause()); this.#align(); this.#update(); });
    a.addEventListener("seeked", () => { this.#align(); this.#update(); });
    a.addEventListener("ratechange", () => this.#retime());
    a.addEventListener("timeupdate", () => { this.#align(); this.#update(); });

    // keep followers aligned on every frame A shows; a slow timer covers browsers without frame callbacks
    const perFrame = (_now, meta) => {
      if (meta && !a.paused) { this.frame = this.#frameAt(meta.mediaTime); this.#update(); }
      this.#align();
      this.frameHandle = a.requestVideoFrameCallback(perFrame);
    };
    if (a.requestVideoFrameCallback) this.frameHandle = a.requestVideoFrameCallback(perFrame);
    this.timer = setInterval(() => this.#align(), 250);
    this.resizeObs = new ResizeObserver(() => this.#drawTimeline());
    this.resizeObs.observe(this.timeline);
  }

  // ---------------------------------------------------------------- sync
  #followers() {
    const out = [];
    if (this.b.src && ["slider", "side by side", "difference", "onion skin"].includes(this.view)) out.push(this.b);
    if (this.j.src && this.view === "jitter") out.push(this.j);
    return out;
  }

  #baseRate(v) {
    const { a } = this;
    return a.duration && v.duration ? a.playbackRate * (v.duration / a.duration) : a.playbackRate;
  }

  #retime() {
    [this.b, this.j].forEach((v) => { if (v.src) v.playbackRate = this.#baseRate(v); });
  }

  // align followers to A by progress; small gaps are closed by speed, large ones by a jump
  #align() {
    const { a } = this;
    if (!a.duration) return;
    for (const v of this.#followers()) {
      if (!v.duration) continue;
      const want = (a.currentTime / a.duration) * v.duration;
      let gap = want - v.currentTime;
      if (gap > v.duration / 2) gap -= v.duration; // across the loop point
      else if (gap < -v.duration / 2) gap += v.duration;
      const base = this.#baseRate(v);
      if (a.paused || Math.abs(gap) > 0.25) {
        if (Math.abs(gap) > 0.01) v.currentTime = want;
        v.playbackRate = base;
        continue;
      }
      v.playbackRate = base * (1 + Math.max(-0.2, Math.min(0.2, gap * 3)));
    }
  }

  // ---------------------------------------------------------------- views
  #setSplit(x) {
    this.split = Math.min(1, Math.max(0, x));
    this.b.style.clipPath = `inset(0 0 0 ${this.split * 100}%)`;
    this.edge.style.left = `${this.split * 100}%`;
  }

  #arrange(view) {
    const { a, b, j, layer, stage } = this;
    this.view = view;
    this.viewSel.value = view;
    layer.replaceChildren();
    layer.style.filter = "";
    stage.querySelectorAll(":scope > div:not(:first-child)").forEach((el) => el !== this.note && el.remove());
    const cover = "position:absolute;inset:0;width:100%;height:100%;object-fit:contain;";
    stage.style.cursor = view === "slider" ? "ew-resize" : "";
    [b, j].forEach((v) => v.pause());
    if (view === "side by side") {
      css(a, "position:absolute;left:0;top:0;width:50%;height:100%;object-fit:contain;");
      css(b, "position:absolute;right:0;top:0;width:50%;height:100%;object-fit:contain;");
      layer.append(a, b);
      stage.append(this.tagA, this.tagB);
    } else {
      css(a, cover);
      layer.append(a);
      if (view === "slider") {
        css(b, cover + "pointer-events:none;");
        layer.append(b);
        stage.append(this.edge, this.tagA, this.tagB);
        this.#setSplit(this.split);
      } else if (view === "difference") {
        css(b, cover + "mix-blend-mode:difference;");
        layer.append(b);
        layer.style.filter = "brightness(3)";
        stage.append(this.#tag("|A − B| ×3"));
      } else if (view === "onion skin") {
        css(b, cover + "opacity:.5;");
        layer.append(b);
        stage.append(this.#tag("A + B 50%"));
      } else if (view === "jitter") {
        css(j, cover);
        layer.append(j);
        stage.append(this.#tag("change vs previous frame ×4"));
      }
    }
    if (!a.paused) this.#followers().forEach((v) => v.play().catch(() => {}));
    this.#align();
    this.#resize();
  }

  #tag(text) {
    const el = this.#label(text, "left");
    return el;
  }

  // ---------------------------------------------------------------- readouts
  #update() {
    const { a } = this;
    if (a.paused || !a.requestVideoFrameCallback) this.frame = this.#frameAt(a.currentTime);
    if (!this.frames) return;
    const flags = this.card?.qc?.flags?.[this.frame] || 0;
    const names = flagNames(flags);
    this.readout.textContent = `f ${this.frame} / ${this.frames - 1} · ${(this.frame / this.srcFps).toFixed(2)} s` +
      (names.length ? ` · ${names.join(", ")}` : "");
    this.readout.style.color = names.length ? "#ffb347" : "#eee";
    this.#drawTimeline();
  }

  #legend(text) {
    this.legend.replaceChildren();
    if (text) { this.legend.textContent = text; return; }
    const flags = this.card?.qc?.flags || [];
    const present = FLAGS.filter((f) => flags.some((x) => x & f.bit));
    const item = (color, name, shape = "border-radius:2px;") => {
      const s = make("span", "display:inline-flex;align-items:center;gap:3px;");
      s.append(make("span", `display:inline-block;width:8px;height:8px;background:${color};${shape}`), document.createTextNode(name));
      return s;
    };
    this.legend.append(item("#9a9a9a", "motion", "height:2px;"));
    if (this.card?.ab) this.legend.append(item("#33d6c5", "A vs B", "height:2px;"));
    present.forEach((f) => this.legend.append(item(f.color, f.name)));
    if (this.card?.qc) {
      const fl = make("span", "margin-left:auto;");
      fl.textContent = `flicker ${this.card.qc.flicker.toFixed(2)}` + (this.card.ab ? ` · PSNR ${this.card.ab.psnr} dB` : "");
      this.legend.append(fl);
    }
  }

  #drawTimeline() {
    const cv = this.timeline;
    const n = this.frames;
    if (!n || cv.style.display === "none") return;
    const dpr = window.devicePixelRatio || 1;
    const W = Math.max(1, Math.round(cv.clientWidth * dpr));
    const H = Math.max(1, Math.round(TIMELINE_PX * dpr));
    if (cv.width !== W) cv.width = W;
    if (cv.height !== H) cv.height = H;
    const g = cv.getContext("2d");
    g.clearRect(0, 0, W, H);
    const x = (i) => ((i + 0.5) / n) * W;
    const bandH = Math.round(8 * dpr);
    const top = bandH + 2 * dpr;

    // curves: motion (grey) and A vs B (teal), scaled to their 98th percentile
    const curve = (vals, color) => {
      if (!vals?.length) return;
      const sorted = [...vals].sort((p, q) => p - q);
      const peak = Math.max(1e-3, sorted[Math.floor(sorted.length * 0.98)] || 1, 1);
      g.strokeStyle = color;
      g.lineWidth = 1.2 * dpr;
      g.beginPath();
      vals.forEach((v, i) => {
        const y = H - 2 * dpr - Math.min(1, v / peak) * (H - top - 4 * dpr);
        i ? g.lineTo(x(i), y) : g.moveTo(x(i), y);
      });
      g.stroke();
    };
    curve(this.card?.qc?.motion, "#9a9a9a");
    curve(this.card?.ab?.diff, "#33d6c5");

    // flagged frames: a coloured band on top, and a faint full-height tick for strong issues
    const flags = this.card?.qc?.flags || [];
    const w = Math.max(1.5 * dpr, W / n);
    flags.forEach((f, i) => {
      if (!f) return;
      const hit = FLAGS.find((d) => f & d.bit);
      g.fillStyle = hit.color;
      g.fillRect(x(i) - w / 2, 0, w, bandH);
      if (f & (2 | 4 | 16 | 32)) {
        g.globalAlpha = 0.35;
        g.fillRect(x(i) - w / 2, bandH, w, H - bandH);
        g.globalAlpha = 1;
      }
    });

    // picked frame (green) and playhead (red)
    const pickW = this.#widget("pick_frame");
    const pickIdx = this.pinned ?? (pickW && pickW.value >= 0 ? pickW.value : this.card?.pick);
    if (Number.isFinite(pickIdx) && pickIdx >= 0 && pickIdx < n) {
      g.fillStyle = "#3ddc84";
      g.beginPath();
      g.moveTo(x(pickIdx) - 5 * dpr, H);
      g.lineTo(x(pickIdx) + 5 * dpr, H);
      g.lineTo(x(pickIdx), H - 8 * dpr);
      g.fill();
    }
    g.fillStyle = "#ff3b3b";
    g.fillRect(x(this.frame) - dpr, 0, 2 * dpr, H);
  }

  #resize() {
    try {
      const { a, b, node } = this;
      if (!a.videoWidth) return;
      let ratio = a.videoHeight / a.videoWidth;
      if (this.view === "side by side" && b.videoWidth) ratio = Math.max(ratio, b.videoHeight / b.videoWidth) / 2;
      const width = Math.max(node.size[0], 340);
      const extras = HEADER_PX + TIMELINE_PX + CONTROLS_PX + LEGEND_PX;
      const height = Math.max(160, Math.round((width - 24) * ratio) + 8 + extras);
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

  // card = { clips: [{ token, slot, ... }], frames, src_fps, qc, ab, compare, pick, report } from the node's UI output
  async show(card) {
    const ticket = ++this.loadId;
    const slot = (s) => card?.clips?.find((c) => c.slot === s);
    const clipA = slot("A"), clipB = slot("B"), clipJ = slot("J");
    if (!clipA) return;
    try {
      const urls = await Promise.all([clipA, clipB, clipJ].map((c) => (c ? this.#download(c) : null)));
      if (ticket !== this.loadId) {
        urls.forEach((u) => u && URL.revokeObjectURL(u));
        return; // a newer preview arrived while this one was loading
      }
      const stale = this.urls.splice(0, this.urls.length, ...urls.filter(Boolean));
      const sameClips = this.card && this.card.clips?.[0]?.token === clipA.token;
      const keepFrame = sameClips ? this.frame : 0;
      this.card = card;
      this.pinned = null;
      this.#say("");
      const [urlA, urlB, urlJ] = urls;
      this.a.src = urlA;
      [[this.b, urlB], [this.j, urlJ]].forEach(([v, u]) => (u ? (v.src = u) : v.removeAttribute("src")));
      this.soundBtn.style.display = clipA.audio ? "" : "none";

      const views = ["A"];
      if (clipJ) views.push("jitter");
      if (clipB) views.push(...COMPARE_VIEWS);
      this.#fill(this.viewSel, views);
      const qc = card.qc;
      this.header.style.display = "flex";
      this.timeline.style.display = "block";
      this.controls.style.display = "flex";
      this.legend.style.display = "flex";
      [this.prevIssue, this.nextIssue].forEach((el) => (el.style.display = qc ? "" : "none"));
      this.badge.textContent = qc ? qc.verdict : "QC off";
      this.badge.style.background = qc ? VERDICT_COLOR[qc.verdict] || "#555" : "#555";
      this.reasons.textContent = qc ? (qc.reasons.length ? qc.reasons.join(" · ") : "no problems found") : "";
      this.header.title = card.report || "";
      this.#legend();

      this.#arrange(clipB ? (COMPARE_VIEWS.includes(card.compare) ? card.compare : "slider") : (this.view === "jitter" && clipJ ? "jitter" : "A"));
      this.a.playbackRate = Number(this.speed.value);
      if (sameClips) {
        this.a.addEventListener("loadeddata", () => this.seekFrame(keepFrame), { once: true });
      } else {
        this.a.play().catch(() => {});
      }
      stale.forEach((u) => URL.revokeObjectURL(u));
      this.#update();
    } catch (err) {
      if (ticket !== this.loadId) return;
      this.#say(err.message === "expired" ? "This preview is no longer in memory. Run the node again." : "Could not load the preview.");
      console.warn(TAG, "could not load preview", err);
    }
  }

  dispose() {
    if (this.frameHandle) this.a.cancelVideoFrameCallback?.(this.frameHandle);
    clearInterval(this.timer);
    this.resizeObs?.disconnect();
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
        getMinHeight: () => this._raghaHeight || 200,
      });
      widget.computeSize = (w) => [w, this._raghaHeight || 200];
      this.setSize([Math.max(this.size[0], 380), Math.max(this.size[1], 420)]);
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
