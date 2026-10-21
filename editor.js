// Bana listing editor — vanilla canvas editor for the layout of one image.
// It draws with the SAME geometry as layout.py (font sizes, paddings, radii, pin/pole sizes, line widths all scale with
// `k` = the server's image scale), and the payload carries the exact pixel position the server placed every box at, so what
// you see here is what Apply / Preview produce. Coordinates are image pixels; the canvas is scaled to fit.
window.banaEditor = (function () {
  let st = null, img = null, canvas = null, ctx = null, scale = 1, sel = null, drag = null, undo = [], mode = "select";
  const $ = (id) => document.getElementById(id);
  const BOLD = (px) => `700 ${px}px Poppins, sans-serif`, MED = (px) => `500 ${px}px Poppins, sans-serif`;
  const LINE_H = (px) => 1.34 * px + 4;          // PIL: font.getbbox("Ag")[3] + 4 for Poppins
  const ASCENT = (px) => 1.07 * px;              // PIL anchor "ma" → baseline offset

  function init() {
    canvas = $("bana-canvas"); if (!canvas) return false;
    ctx = canvas.getContext("2d");
    canvas.addEventListener("mousedown", onDown); canvas.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp); canvas.addEventListener("dblclick", onDbl);
    canvas.addEventListener("contextmenu", (e) => e.preventDefault());
    window.addEventListener("keydown", (e) => {
      if (document.activeElement && ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName)) return;
      if (e.key === "Delete" || e.key === "Backspace") { del(); e.preventDefault(); }
      if ((e.ctrlKey || e.metaKey) && e.key === "z") { doUndo(); e.preventDefault(); }
    });
    window.addEventListener("resize", () => { if (st) { fit(); draw(); } });
    ["bana-text", "bana-sub"].forEach((id) => { const el = $(id); if (el) el.addEventListener("input", onText); });
    const k = $("bana-kind"); if (k) k.addEventListener("change", onText);
    const ct = $("bana-title"), cs = $("bana-csub"); if (ct) ct.addEventListener("input", onCard); if (cs) cs.addEventListener("input", onCard);
    const dc = $("bana-disc"); if (dc) dc.addEventListener("input", () => { if (st) st.disclaimer = dc.value; draw(); });
    const sh = $("bana-shadow"); if (sh) sh.addEventListener("change", () => { if (st) { snap(); st.style.shadow = sh.checked; draw(); } });
    const fl = $("bana-fill"); if (fl) fl.addEventListener("change", () => { if (st) { snap(); st.style.fill = fl.checked; draw(); } });
    bind("bana-add-callout", () => add("feature", "CALLOUT")); bind("bana-add-road", () => add("road", "ROAD NAME"));
    bind("bana-del", del); bind("bana-undo", doUndo);
    bind("bana-corner", () => { mode = mode === "corner" ? "select" : "corner"; status(mode === "corner" ? "Click on the boundary to add a corner (click again to stop)" : ""); });
    bind("bana-bigger", () => resize(1.12)); bind("bana-smaller", () => resize(1 / 1.12));
    bind("bana-line-bigger", () => { snap(); st.style.line_scale = Math.min(4, (st.style.line_scale || 1) * 1.2); draw(); });
    bind("bana-line-smaller", () => { snap(); st.style.line_scale = Math.max(0.3, (st.style.line_scale || 1) / 1.2); draw(); });
    bind("bana-toggle-lines", () => { snap(); st.style.line_visible = !st.style.line_visible; buttons(); draw(); });
    bind("bana-toggle-card", () => { snap(); st.card.visible = !st.card.visible; if (st.card.visible) sel = { t: "cb" }; buttons(); panel(); draw(); });
    bind("bana-toggle-disc", () => { snap(); st.disc.visible = !st.disc.visible; if (st.disc.visible) sel = { t: "dc" }; buttons(); panel(); draw(); });
    return true;
  }
  function bind(id, fn) { const el = $(id); if (el) el.onclick = (e) => { e.preventDefault(); if (st) fn(); }; }
  function status(t) { const el = $("bana-status"); if (el) el.textContent = t; }

  function load(p) {
    if (!canvas && !init()) return;
    st = JSON.parse(JSON.stringify(p.layout)); st.W = p.W; st.H = p.H; st.boundary_edited = !!p.draw; st.mpp = p.mpp || null;
    st.reference = p.layout.reference || []; st.k = p.layout.scale || Math.max(0.6, Math.min(2.2, p.W / 1200));
    st.style = Object.assign({ shadow: false, line_scale: 1, line_visible: true, fill: true }, st.style || {});
    st.card = Object.assign({ scale: 1, visible: true }, st.card); st.disc = Object.assign({ scale: 1, visible: true }, st.disc || {});
    st.labels.forEach((l) => { if (l.scale == null) l.scale = 1; if (l.visible == null) l.visible = true; if (l.bx == null) { l.bx = l.x; l.by = l.y - 58 * st.k; l.mode = "bottom"; } });
    sel = null; undo = []; mode = "select";
    img = new Image(); img.onload = () => { fit(); draw(); }; img.src = p.image;
    const ct = $("bana-title"), cs = $("bana-csub"), dc = $("bana-disc"), sh = $("bana-shadow"), fl = $("bana-fill");
    if (ct) ct.value = st.card.title; if (cs) cs.value = st.card.sub; if (dc) dc.value = st.disclaimer || "";
    if (sh) sh.checked = !!st.style.shadow; if (fl) fl.checked = st.style.fill !== false;
    const ov = $("bana-overridden"); if (ov) ov.textContent = p.overridden ? "This image has its own edits (override)." : "";
    panel(); buttons(); status("Loaded. Drag a label box to move it, drag its pin to move the point; drag the card, corners or the disclaimer. Double-click a label to rename.");
  }
  let lastW = 0;
  function fit() { const wrap = canvas.parentElement; const w = Math.min(wrap.clientWidth || 1000, 1152); lastW = wrap.clientWidth; scale = w / st.W; canvas.width = Math.round(st.W * scale); canvas.height = Math.round(st.H * scale); }
  setInterval(() => { if (st && img && canvas && canvas.parentElement && canvas.parentElement.clientWidth && canvas.parentElement.clientWidth !== lastW) { fit(); draw(); } }, 400);
  function S(v) { return v * scale; }

  // ---------- geometry (mirrors layout.py / listing_shot.py) ----------
  function measure(lines, sizes, pad) {           // in image px: {w, h, heights}
    const widths = lines.map((t, i) => { ctx.font = i ? MED(S(sizes[i])) : BOLD(S(sizes[i])); return ctx.measureText(t || " ").width / scale; });
    const heights = sizes.map(LINE_H);
    return { w: Math.max(...widths) + pad[0] * 2, h: heights.reduce((a, b) => a + b, 0) + pad[1] * 2 + (lines.length - 1) * 4, heights };
  }
  function cardGeom() {
    const k = st.k * (st.card.scale || 1); const sizes = [58 * k, 24 * k], pad = [22 * k, 14 * k];
    const m = measure([st.card.title, st.card.sub], sizes, pad);
    return { k, sizes, pad, w: m.w, h: m.h, heights: m.heights, x0: st.card.bx - m.w / 2, y0: st.card.by - m.h, pinR: 24 * k };
  }
  function labelGeom(l) {
    const k = st.k * (l.scale || 1); const road = l.kind === "road";
    const lines = [l.text || " "].concat(l.sub ? [l.sub] : []); const sizes = road ? [24 * k] : [26 * k].concat(l.sub ? [19 * k] : []); const pad = [14 * k, 8 * k];
    const m = measure(lines, sizes, pad); const y0 = l.mode === "top" ? l.by : l.by - m.h;
    return { k, lines, sizes, pad, w: m.w, h: m.h, heights: m.heights, x0: l.bx - m.w / 2, y0, road };
  }
  function discGeom() {
    const px = 24 * st.k * (st.disc.scale || 1); ctx.font = BOLD(S(px)); const w = ctx.measureText(st.disclaimer || "").width / scale; const h = 1.34 * px;
    let x = st.disc.x * st.W - w, y = st.disc.y * st.H - h; x = Math.min(Math.max(x, 4), st.W - w - 4); y = Math.min(Math.max(y, 4), st.H - h - 4);
    return { px, w, h, x, y };
  }

  // ---------- drawing ----------
  function rr(x, y, w, h, r) { ctx.beginPath(); ctx.moveTo(x + r, y); ctx.arcTo(x + w, y, x + w, y + h, r); ctx.arcTo(x + w, y + h, x, y + h, r); ctx.arcTo(x, y + h, x, y, r); ctx.arcTo(x, y, x + w, y, r); ctx.closePath(); }
  function shadow(on, blur, dy, a) { if (on && st.style.shadow) { ctx.shadowColor = `rgba(0,0,0,${a})`; ctx.shadowBlur = S(blur) * 2; ctx.shadowOffsetY = S(dy); } else { ctx.shadowColor = "transparent"; ctx.shadowBlur = 0; ctx.shadowOffsetY = 0; } }
  function textLines(lines, sizes, heights, cx, y0, padY, fg) {
    let yy = y0 + padY; ctx.textAlign = "center"; ctx.textBaseline = "alphabetic"; ctx.fillStyle = fg;
    lines.forEach((t, i) => { ctx.font = i ? MED(S(sizes[i])) : BOLD(S(sizes[i])); ctx.fillText(t, S(cx), S(yy + ASCENT(sizes[i]))); yy += heights[i] + 4; });
  }
  function box(x0, y0, w, h, r, bg) { shadow(true, 10, 6, 0.47); ctx.fillStyle = bg; rr(S(x0), S(y0), S(w), S(h), S(r)); ctx.fill(); shadow(false); }
  function pin(x, y, r) {
    shadow(true, 6, 5, 0.59); ctx.fillStyle = "#dc2626";
    ctx.beginPath(); ctx.arc(S(x), S(y - 2 * r), S(r), 0, 7); ctx.fill();
    ctx.beginPath(); ctx.moveTo(S(x - 0.75 * r), S(y - 1.5 * r)); ctx.lineTo(S(x + 0.75 * r), S(y - 1.5 * r)); ctx.lineTo(S(x), S(y)); ctx.closePath(); ctx.fill(); shadow(false);
    ctx.fillStyle = "#fff"; ctx.beginPath(); ctx.arc(S(x), S(y - 2 * r), S(0.4 * r), 0, 7); ctx.fill();
  }
  function selBox(x0, y0, w, h) { ctx.save(); shadow(false); ctx.strokeStyle = "#ff3b3b"; ctx.lineWidth = 2; ctx.setLineDash([6, 4]); ctx.strokeRect(S(x0) - 3, S(y0) - 3, S(w) + 6, S(h) + 6); ctx.restore(); }

  function draw() {
    if (!st || !img) return;
    ctx.clearRect(0, 0, canvas.width, canvas.height); ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
    const k = st.k, b = st.boundary;
    if (st.reference && st.reference.length) {
      ctx.save(); ctx.setLineDash([S(10), S(8)]); ctx.lineWidth = S(3); ctx.strokeStyle = "rgba(255,220,80,0.9)"; ctx.beginPath();
      st.reference.forEach((p, i) => (i ? ctx.lineTo(S(p[0]), S(p[1])) : ctx.moveTo(S(p[0]), S(p[1])))); ctx.closePath(); ctx.stroke(); ctx.restore();
    }
    // boundary: fill + crisp white line with a dark edge (+ soft shadow when on)
    if (b.length >= 2) {
      const path = () => { ctx.beginPath(); b.forEach((p, i) => (i ? ctx.lineTo(S(p[0]), S(p[1])) : ctx.moveTo(S(p[0]), S(p[1])))); if (b.length >= 3) ctx.closePath(); };
      const w = Math.max(2, Math.round(5 * k * (st.style.line_scale || 1)));
      if (st.style.line_visible) {
        if (st.style.fill !== false && b.length >= 3) { path(); ctx.fillStyle = "rgba(255,255,255,0.133)"; ctx.fill(); }
        ctx.lineJoin = "round"; ctx.lineCap = "round";
        if (st.style.shadow) { shadow(true, 4, 2, 0.67); path(); ctx.strokeStyle = "#fff"; ctx.lineWidth = S(w); ctx.stroke(); shadow(false); }
        path(); ctx.strokeStyle = "rgba(0,0,0,0.47)"; ctx.lineWidth = S(w + 2); ctx.stroke();
        path(); ctx.strokeStyle = "#fff"; ctx.lineWidth = S(w); ctx.stroke();
      } else { ctx.save(); ctx.setLineDash([4, 6]); path(); ctx.strokeStyle = "rgba(255,255,255,0.45)"; ctx.lineWidth = 1.5; ctx.stroke(); ctx.restore(); }
      b.forEach((p, i) => { const on = sel && sel.t === "v" && sel.i === i; ctx.beginPath(); ctx.arc(S(p[0]), S(p[1]), on ? 8 : 5, 0, 7); ctx.fillStyle = on ? "#ff3b3b" : "#fff"; ctx.fill(); ctx.lineWidth = 1.5; ctx.strokeStyle = "#1f6f4a"; ctx.stroke(); });
    }
    // card: pole → box → pin
    if (st.card.visible) {
      const g = cardGeom(); const pw = Math.max(3, Math.round(5 * g.k)), pr = Math.max(5, Math.round(pw * 1.4));
      ctx.lineCap = "butt";
      if (!st.style.shadow) { ctx.strokeStyle = "rgba(0,0,0,0.47)"; ctx.lineWidth = S(pw + 2); ctx.beginPath(); ctx.moveTo(S(st.card.bx), S(st.card.by + 10 * g.k)); ctx.lineTo(S(st.card.x), S(st.card.y)); ctx.stroke(); ctx.fillStyle = "rgba(0,0,0,0.47)"; ctx.beginPath(); ctx.arc(S(st.card.x), S(st.card.y), S(pr + 1), 0, 7); ctx.fill(); }
      shadow(true, 4, 3, 0.63); ctx.strokeStyle = "#fff"; ctx.lineWidth = S(pw); ctx.beginPath(); ctx.moveTo(S(st.card.bx), S(st.card.by + 10 * g.k)); ctx.lineTo(S(st.card.x), S(st.card.y)); ctx.stroke();
      ctx.fillStyle = "#fff"; ctx.beginPath(); ctx.arc(S(st.card.x), S(st.card.y), S(pr), 0, 7); ctx.fill(); shadow(false);
      box(g.x0, g.y0, g.w, g.h, 14 * g.k, "rgba(255,255,255,0.95)");
      textLines([st.card.title, st.card.sub], g.sizes, g.heights, st.card.bx, g.y0, g.pad[1], "#141414");
      pin(st.card.bx, g.y0 - 6, g.pinR);
      if (sel && (sel.t === "cb" || sel.t === "cp")) selBox(g.x0, g.y0, g.w, g.h);
    }
    // labels
    st.labels.forEach((l, i) => {
      if (l.visible === false) return;
      const g = labelGeom(l); const endY = l.mode === "top" ? g.y0 - 2 : g.y0 + g.h + 2;
      ctx.lineCap = "butt";
      if (g.road) {
        pin(l.x, l.y, 14 * g.k);
        if (l.mode === "top" || Math.abs(l.bx - l.x) > 20 * g.k) { ctx.strokeStyle = "rgba(255,255,255,0.86)"; ctx.lineWidth = S(Math.max(2, 2 * g.k)); ctx.beginPath(); ctx.moveTo(S(l.x), S(l.y - 14 * g.k)); ctx.lineTo(S(l.bx), S(endY)); ctx.stroke(); }
        box(g.x0, g.y0, g.w, g.h, 9 * g.k, "rgba(255,255,255,0.925)");
        textLines(g.lines, g.sizes, g.heights, l.bx, g.y0, g.pad[1], "#141414");
      } else {
        shadow(true, 3, 2, 0.59); ctx.fillStyle = "#fff"; ctx.beginPath(); ctx.arc(S(l.x), S(l.y), S(6 * g.k), 0, 7); ctx.fill();
        ctx.strokeStyle = "rgba(255,255,255,0.92)"; ctx.lineWidth = S(Math.max(2, 3 * g.k)); ctx.beginPath(); ctx.moveTo(S(l.x), S(l.y)); ctx.lineTo(S(l.bx), S(endY)); ctx.stroke(); shadow(false);
        box(g.x0, g.y0, g.w, g.h, 9 * g.k, "rgba(10,10,10,0.59)");
        textLines(g.lines, g.sizes, g.heights, l.bx, g.y0, g.pad[1], "#fff");
      }
      if (sel && (sel.t === "lb" || sel.t === "lp") && sel.i === i) selBox(g.x0, g.y0, g.w, g.h);
    });
    // live acreage (satellite frames only)
    if (st.mpp && b.length >= 3) {
      let a = 0; for (let i = 0; i < b.length; i++) { const p = b[i], q = b[(i + 1) % b.length]; a += p[0] * q[1] - q[0] * p[1]; }
      const acres = Math.abs(a) / 2 * st.mpp * st.mpp / 4046.8564; st.acres = acres;
      const el = $("bana-acres"); if (el) el.textContent = "≈ " + acres.toFixed(2) + " acres";
    }
    // disclaimer (outlined text, bottom-right by default, draggable)
    if (st.disc.visible && st.disclaimer) {
      const g = discGeom(); ctx.font = BOLD(S(g.px)); ctx.textAlign = "left"; ctx.textBaseline = "alphabetic"; const by = S(g.y + ASCENT(g.px));
      ctx.lineJoin = "round"; ctx.strokeStyle = "rgba(0,0,0,0.78)"; ctx.lineWidth = S(4); ctx.strokeText(st.disclaimer, S(g.x), by);
      ctx.fillStyle = "#fff"; ctx.fillText(st.disclaimer, S(g.x), by);
      if (sel && sel.t === "dc") selBox(g.x, g.y, g.w, g.h);
    }
  }

  // ---------- interaction ----------
  function pos(e) { const r = canvas.getBoundingClientRect(); return { x: (e.clientX - r.left) * (st.W / r.width), y: (e.clientY - r.top) * (st.H / r.height) }; }
  function inBox(p, x0, y0, w, h) { return p.x >= x0 && p.x <= x0 + w && p.y >= y0 && p.y <= y0 + h; }
  function hit(p) {
    const cs = canvas.getBoundingClientRect().width / st.W;                      // on-screen px per image px
    for (let i = 0; i < st.boundary.length; i++) { const v = st.boundary[i]; if (Math.hypot(v[0] - p.x, v[1] - p.y) < 12 / cs) return { t: "v", i }; }
    for (let i = st.labels.length - 1; i >= 0; i--) { const l = st.labels[i]; if (l.visible === false) continue; const g = labelGeom(l); if (inBox(p, g.x0, g.y0, g.w, g.h)) return { t: "lb", i }; }
    for (let i = st.labels.length - 1; i >= 0; i--) { const l = st.labels[i]; if (l.visible === false) continue; const g = labelGeom(l); const py = g.road ? l.y - 28 * g.k : l.y; if (Math.hypot(l.x - p.x, l.y - p.y) < 16 / cs || Math.hypot(l.x - p.x, py - p.y) < 16 / cs) return { t: "lp", i }; }
    if (st.card.visible) { const g = cardGeom(); if (inBox(p, g.x0, g.y0 - 2.6 * g.pinR, g.w, g.h + 2.6 * g.pinR)) return { t: "cb" }; if (Math.hypot(st.card.x - p.x, st.card.y - p.y) < 16 / cs) return { t: "cp" }; }
    if (st.disc.visible && st.disclaimer) { const g = discGeom(); if (inBox(p, g.x, g.y, g.w, g.h)) return { t: "dc" }; }
    return null;
  }
  function nearestEdge(p) { let best = null; const b = st.boundary; for (let i = 0; i < b.length; i++) { const a = b[i], c = b[(i + 1) % b.length]; const dx = c[0] - a[0], dy = c[1] - a[1]; const L = dx * dx + dy * dy || 1; let t = ((p.x - a[0]) * dx + (p.y - a[1]) * dy) / L; t = Math.max(0, Math.min(1, t)); const d = Math.hypot(a[0] + dx * t - p.x, a[1] + dy * t - p.y); if (!best || d < best.d) best = { d, i, x: a[0] + dx * t, y: a[1] + dy * t }; } return best; }
  function snap() { undo.push(JSON.stringify({ boundary: st.boundary, labels: st.labels, card: st.card, disc: st.disc, style: st.style, boundary_edited: st.boundary_edited })); if (undo.length > 60) undo.shift(); }
  function doUndo() { const s = undo.pop(); if (!s) return; Object.assign(st, JSON.parse(s)); sel = null; panel(); buttons(); draw(); }

  function onDown(e) {
    if (!st) return; const p = pos(e);
    if (mode === "corner") { const ne = nearestEdge(p); if (ne && ne.d < 40 / (canvas.getBoundingClientRect().width / st.W)) { snap(); st.boundary.splice(ne.i + 1, 0, [ne.x, ne.y]); st.boundary_edited = true; sel = { t: "v", i: ne.i + 1 }; draw(); } return; }
    sel = hit(p); panel();
    if (sel) {
      snap();
      const l = sel.i != null ? st.labels[sel.i] : null;
      const start = sel.t === "v" ? [st.boundary[sel.i]] : sel.t === "lb" ? [[l.bx, l.by]] : sel.t === "lp" ? [[l.x, l.y], [l.bx, l.by]] :
                    sel.t === "cb" ? [[st.card.bx, st.card.by]] : sel.t === "cp" ? [[st.card.x, st.card.y], [st.card.bx, st.card.by]] : [[st.disc.x * st.W, st.disc.y * st.H]];
      drag = { p, start: JSON.parse(JSON.stringify(start)) };
    }
    draw();
  }
  function onMove(e) {
    if (!drag || !sel) return; const p = pos(e); const dx = p.x - drag.p.x, dy = p.y - drag.p.y; const s = drag.start;
    if (sel.t === "v") { st.boundary[sel.i] = [s[0][0] + dx, s[0][1] + dy]; st.boundary_edited = true; }
    else if (sel.t === "lb") { const l = st.labels[sel.i]; l.bx = s[0][0] + dx; l.by = s[0][1] + dy; l.mode = l.by < l.y ? "bottom" : "top"; }
    else if (sel.t === "lp") { const l = st.labels[sel.i]; l.x = s[0][0] + dx; l.y = s[0][1] + dy; l.bx = s[1][0] + dx; l.by = s[1][1] + dy; }
    else if (sel.t === "cb") { st.card.bx = s[0][0] + dx; st.card.by = s[0][1] + dy; }
    else if (sel.t === "cp") { st.card.x = s[0][0] + dx; st.card.y = s[0][1] + dy; st.card.bx = s[1][0] + dx; st.card.by = s[1][1] + dy; }
    else if (sel.t === "dc") { st.disc.x = (s[0][0] + dx) / st.W; st.disc.y = (s[0][1] + dy) / st.H; }
    draw();
  }
  function onUp() { drag = null; }
  function onDbl(e) { const p = pos(e); const h = hit(p); if (h && (h.t === "lb" || h.t === "lp")) { const t = prompt("Label text", st.labels[h.i].text); if (t !== null) { snap(); st.labels[h.i].text = t.toUpperCase(); panel(); draw(); } } }
  function add(kind, text) { if (!st) return; snap(); const id = "u" + Date.now(); st.labels.push({ id, kind, text, sub: "", x: st.W / 2, y: st.H / 2, bx: st.W / 2, by: st.H / 2 - (kind === "road" ? 46 : 58) * st.k, mode: "bottom", scale: 1, visible: true }); sel = { t: "lb", i: st.labels.length - 1 }; panel(); draw(); }
  function del() {
    if (!st || !sel) return; snap();
    if (sel.t === "lb" || sel.t === "lp") st.labels.splice(sel.i, 1);
    else if (sel.t === "v") { if (st.boundary.length > 3) { st.boundary.splice(sel.i, 1); st.boundary_edited = true; } else { status("A boundary needs 3 corners — use “Hide lines” to remove the outline entirely."); } }
    else if (sel.t === "cb" || sel.t === "cp") st.card.visible = false;
    else if (sel.t === "dc") st.disc.visible = false;
    sel = null; panel(); buttons(); draw();
  }
  function resize(f) {
    if (!st || !sel) { status("Select a label, the card, the disclaimer or a corner first (corners: use the line width buttons)."); return; }
    snap();
    if (sel.t === "lb" || sel.t === "lp") { const l = st.labels[sel.i]; l.scale = Math.min(3, Math.max(0.35, (l.scale || 1) * f)); }
    else if (sel.t === "cb" || sel.t === "cp") st.card.scale = Math.min(3, Math.max(0.35, (st.card.scale || 1) * f));
    else if (sel.t === "dc") st.disc.scale = Math.min(3, Math.max(0.35, (st.disc.scale || 1) * f));
    else if (sel.t === "v") st.style.line_scale = Math.min(4, Math.max(0.3, (st.style.line_scale || 1) * f));
    draw();
  }
  function onText() { if (!st || !sel || (sel.t !== "lb" && sel.t !== "lp")) return; const l = st.labels[sel.i]; l.text = ($("bana-text").value || "").toUpperCase(); l.sub = $("bana-sub").value || ""; l.kind = $("bana-kind").value; draw(); }
  function onCard() { if (!st) return; st.card.title = $("bana-title").value; st.card.sub = $("bana-csub").value; draw(); }
  function panel() {
    const box = $("bana-labelpanel"); if (!box) return;
    if (sel && (sel.t === "lb" || sel.t === "lp")) { box.style.display = "block"; const l = st.labels[sel.i]; $("bana-text").value = l.text; $("bana-sub").value = l.sub || ""; $("bana-kind").value = l.kind; }
    else box.style.display = "none";
    const nm = $("bana-selname"); if (nm) nm.textContent = !sel ? "nothing selected" : sel.t === "v" ? "boundary corner" : (sel.t === "lb" || sel.t === "lp") ? "label: " + (st.labels[sel.i].text || "") : (sel.t === "cb" || sel.t === "cp") ? "acreage card" : "disclaimer";
  }
  function buttons() {
    const a = $("bana-toggle-lines"); if (a) a.textContent = st.style.line_visible ? "Hide lines" : "Show lines";
    const b = $("bana-toggle-card"); if (b) b.textContent = st.card.visible ? "Hide card" : "Show card";
    const c = $("bana-toggle-disc"); if (c) c.textContent = st.disc.visible ? "Hide disclaimer" : "Show disclaimer";
  }
  function exportLayout() {
    if (!st) return null;
    return { boundary: st.boundary, boundary_edited: st.boundary_edited, card: st.card, labels: st.labels, disclaimer: st.disclaimer,
             disc: { x: st.disc.x, y: st.disc.y, scale: st.disc.scale, visible: st.disc.visible }, style: st.style };
  }
  function debug(clientX, clientY) { const p = pos({ clientX, clientY }); return { p, hit: hit(p), rect: canvas.getBoundingClientRect(), cw: canvas.width, W: st.W, scale, b0: st.boundary[0] }; }
  return { load, export: exportLayout, init, debug };
})();
