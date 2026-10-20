// Bana listing editor — vanilla canvas editor for the layout of one image.
// Coordinates are kept in image pixels; the canvas is scaled to fit. Export returns the edited layout in image pixels.
window.banaEditor = (function () {
  let st = null, img = null, canvas = null, ctx = null, scale = 1, sel = null, drag = null, undo = [], mode = "select";
  const $ = (id) => document.getElementById(id);

  function init() {
    canvas = $("bana-canvas"); if (!canvas) return false;
    ctx = canvas.getContext("2d");
    canvas.addEventListener("mousedown", onDown); canvas.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp); canvas.addEventListener("dblclick", onDbl);
    canvas.addEventListener("contextmenu", (e) => e.preventDefault());
    window.addEventListener("keydown", (e) => { if (document.activeElement && ["INPUT", "TEXTAREA"].includes(document.activeElement.tagName)) return; if (e.key === "Delete" || e.key === "Backspace") { del(); e.preventDefault(); } if ((e.ctrlKey || e.metaKey) && e.key === "z") { doUndo(); e.preventDefault(); } });
    window.addEventListener("resize", () => { if (st) { fit(); draw(); } });
    ["bana-text", "bana-sub"].forEach((id) => { const el = $(id); if (el) el.addEventListener("input", onText); });
    const k = $("bana-kind"); if (k) k.addEventListener("change", onText);
    const ct = $("bana-title"), cs = $("bana-csub"); if (ct) ct.addEventListener("input", onCard); if (cs) cs.addEventListener("input", onCard);
    const dc = $("bana-disc"); if (dc) dc.addEventListener("input", () => { if (st) st.disclaimer = dc.value; draw(); });
    bind("bana-add-callout", () => add("feature", "CALLOUT")); bind("bana-add-road", () => add("road", "ROAD NAME"));
    bind("bana-del", del); bind("bana-undo", doUndo); bind("bana-corner", () => { mode = mode === "corner" ? "select" : "corner"; status(mode === "corner" ? "Click on the boundary to add a corner (click again to stop)" : ""); });
    return true;
  }
  function bind(id, fn) { const el = $(id); if (el) el.onclick = (e) => { e.preventDefault(); fn(); }; }
  function status(t) { const el = $("bana-status"); if (el) el.textContent = t; }

  function load(p) {
    if (!canvas && !init()) return;
    st = JSON.parse(JSON.stringify(p.layout)); st.W = p.W; st.H = p.H; st.boundary_edited = false; sel = null; undo = []; mode = "select";
    img = new Image(); img.onload = () => { fit(); draw(); }; img.src = p.image;
    const ct = $("bana-title"), cs = $("bana-csub"), dc = $("bana-disc");
    if (ct) ct.value = st.card.title; if (cs) cs.value = st.card.sub; if (dc) dc.value = st.disclaimer || "";
    panel(); status("Loaded. Drag corners, labels or the card. Double-click a label to rename.");
  }
  function fit() { const wrap = canvas.parentElement; const w = Math.min(wrap.clientWidth || 1000, 1152); scale = w / st.W; canvas.width = Math.round(st.W * scale); canvas.height = Math.round(st.H * scale); }
  function S(v) { return v * scale; }

  function rr(x, y, w, h, r) { ctx.beginPath(); ctx.moveTo(x + r, y); ctx.arcTo(x + w, y, x + w, y + h, r); ctx.arcTo(x + w, y + h, x, y + h, r); ctx.arcTo(x, y + h, x, y, r); ctx.arcTo(x, y, x + w, y, r); ctx.closePath(); }
  function labelBox(l) { ctx.font = `bold ${S(22)}px Poppins, sans-serif`; const w = ctx.measureText(l.text || " ").width + S(28); const h = S(40) + (l.sub ? S(20) : 0); return { x: S(l.x) - w / 2, y: S(l.y) - S(60) - h, w, h }; }
  function cardBox() { ctx.font = `bold ${S(44)}px Poppins, sans-serif`; const w = Math.max(ctx.measureText(st.card.title).width, (ctx.font = `${S(18)}px Poppins, sans-serif`, ctx.measureText(st.card.sub).width)) + S(44); const h = S(96); return { x: S(st.card.x) - w / 2, y: S(st.card.y) - S(170) - h, w, h }; }

  function draw() {
    if (!st || !img) return;
    ctx.clearRect(0, 0, canvas.width, canvas.height); ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
    // boundary
    const b = st.boundary; if (b.length) {
      ctx.beginPath(); b.forEach((p, i) => (i ? ctx.lineTo(S(p[0]), S(p[1])) : ctx.moveTo(S(p[0]), S(p[1])))); ctx.closePath();
      ctx.fillStyle = "rgba(255,255,255,0.13)"; ctx.fill(); ctx.lineWidth = S(4); ctx.strokeStyle = "#fff"; ctx.shadowColor = "rgba(0,0,0,.6)"; ctx.shadowBlur = 4; ctx.stroke(); ctx.shadowBlur = 0;
      b.forEach((p, i) => { ctx.beginPath(); ctx.arc(S(p[0]), S(p[1]), sel && sel.t === "v" && sel.i === i ? 9 : 6, 0, 7); ctx.fillStyle = sel && sel.t === "v" && sel.i === i ? "#ff3b3b" : "#ffffff"; ctx.fill(); ctx.lineWidth = 2; ctx.strokeStyle = "#1f6f4a"; ctx.stroke(); });
    }
    // card
    const cb = cardBox(); ctx.strokeStyle = "rgba(255,255,255,.9)"; ctx.lineWidth = S(4); ctx.beginPath(); ctx.moveTo(S(st.card.x), S(st.card.y)); ctx.lineTo(S(st.card.x), cb.y + cb.h); ctx.stroke();
    ctx.fillStyle = "rgba(255,255,255,.95)"; rr(cb.x, cb.y, cb.w, cb.h, S(12)); ctx.fill();
    if (sel && sel.t === "card") { ctx.strokeStyle = "#ff3b3b"; ctx.lineWidth = 3; ctx.stroke(); }
    ctx.fillStyle = "#111"; ctx.textAlign = "center"; ctx.font = `bold ${S(44)}px Poppins, sans-serif`; ctx.fillText(st.card.title, cb.x + cb.w / 2, cb.y + S(50));
    ctx.font = `${S(18)}px Poppins, sans-serif`; ctx.fillText(st.card.sub, cb.x + cb.w / 2, cb.y + S(80));
    ctx.beginPath(); ctx.arc(S(st.card.x), S(st.card.y), 5, 0, 7); ctx.fillStyle = "#fff"; ctx.fill();
    // labels
    st.labels.forEach((l, i) => {
      const bx = labelBox(l); const dark = l.kind === "feature";
      ctx.strokeStyle = "rgba(255,255,255,.9)"; ctx.lineWidth = S(2.5); ctx.beginPath(); ctx.moveTo(S(l.x), S(l.y)); ctx.lineTo(bx.x + bx.w / 2, bx.y + bx.h); ctx.stroke();
      ctx.beginPath(); ctx.arc(S(l.x), S(l.y), dark ? 5 : 7, 0, 7); ctx.fillStyle = dark ? "#fff" : "#dc2626"; ctx.fill();
      ctx.fillStyle = dark ? "rgba(10,10,10,.75)" : "rgba(255,255,255,.95)"; rr(bx.x, bx.y, bx.w, bx.h, S(8)); ctx.fill();
      if (sel && sel.t === "l" && sel.i === i) { ctx.strokeStyle = "#ff3b3b"; ctx.lineWidth = 3; ctx.stroke(); }
      ctx.fillStyle = dark ? "#fff" : "#111"; ctx.font = `bold ${S(22)}px Poppins, sans-serif`; ctx.textAlign = "center"; ctx.fillText(l.text || "", bx.x + bx.w / 2, bx.y + S(28));
      if (l.sub) { ctx.font = `${S(16)}px Poppins, sans-serif`; ctx.fillText(l.sub, bx.x + bx.w / 2, bx.y + S(50)); }
    });
    // disclaimer
    ctx.font = `bold ${S(20)}px Poppins, sans-serif`; ctx.textAlign = "right"; ctx.fillStyle = "#fff"; ctx.shadowColor = "#000"; ctx.shadowBlur = 4; ctx.fillText(st.disclaimer || "", canvas.width - S(28), canvas.height - S(44)); ctx.shadowBlur = 0;
  }

  function pos(e) { const r = canvas.getBoundingClientRect(); return { x: (e.clientX - r.left) / scale, y: (e.clientY - r.top) / scale }; }
  function hit(p) {
    for (let i = 0; i < st.boundary.length; i++) { const v = st.boundary[i]; if (Math.hypot(v[0] - p.x, v[1] - p.y) < 12 / scale) return { t: "v", i }; }
    for (let i = st.labels.length - 1; i >= 0; i--) { const l = st.labels[i]; const b = labelBox(l); const sx = S(p.x), sy = S(p.y); if ((sx >= b.x && sx <= b.x + b.w && sy >= b.y && sy <= b.y + b.h) || Math.hypot(l.x - p.x, l.y - p.y) < 14 / scale) return { t: "l", i }; }
    const cb = cardBox(); const sx = S(p.x), sy = S(p.y); if (sx >= cb.x && sx <= cb.x + cb.w && sy >= cb.y && sy <= cb.y + cb.h) return { t: "card" };
    return null;
  }
  function nearestEdge(p) { let best = null; const b = st.boundary; for (let i = 0; i < b.length; i++) { const a = b[i], c = b[(i + 1) % b.length]; const dx = c[0] - a[0], dy = c[1] - a[1]; const L = dx * dx + dy * dy || 1; let t = ((p.x - a[0]) * dx + (p.y - a[1]) * dy) / L; t = Math.max(0, Math.min(1, t)); const d = Math.hypot(a[0] + dx * t - p.x, a[1] + dy * t - p.y); if (!best || d < best.d) best = { d, i, x: a[0] + dx * t, y: a[1] + dy * t }; } return best; }
  function snap() { undo.push(JSON.stringify({ boundary: st.boundary, labels: st.labels, card: st.card, boundary_edited: st.boundary_edited })); if (undo.length > 60) undo.shift(); }
  function doUndo() { const s = undo.pop(); if (!s) return; Object.assign(st, JSON.parse(s)); sel = null; panel(); draw(); }

  function onDown(e) {
    if (!st) return; const p = pos(e);
    if (mode === "corner") { const ne = nearestEdge(p); if (ne && ne.d < 40 / scale) { snap(); st.boundary.splice(ne.i + 1, 0, [ne.x, ne.y]); st.boundary_edited = true; sel = { t: "v", i: ne.i + 1 }; draw(); } return; }
    sel = hit(p); panel();
    if (sel) { snap(); drag = { p, start: JSON.parse(JSON.stringify(sel.t === "v" ? st.boundary[sel.i] : sel.t === "l" ? [st.labels[sel.i].x, st.labels[sel.i].y] : [st.card.x, st.card.y])) }; }
    draw();
  }
  function onMove(e) {
    if (!drag || !sel) return; const p = pos(e); const dx = p.x - drag.p.x, dy = p.y - drag.p.y;
    if (sel.t === "v") { st.boundary[sel.i] = [drag.start[0] + dx, drag.start[1] + dy]; st.boundary_edited = true; }
    else if (sel.t === "l") { st.labels[sel.i].x = drag.start[0] + dx; st.labels[sel.i].y = drag.start[1] + dy; }
    else { st.card.x = drag.start[0] + dx; st.card.y = drag.start[1] + dy; }
    draw();
  }
  function onUp() { drag = null; }
  function onDbl(e) { const p = pos(e); const h = hit(p); if (h && h.t === "l") { const t = prompt("Label text", st.labels[h.i].text); if (t !== null) { snap(); st.labels[h.i].text = t.toUpperCase(); panel(); draw(); } } }
  function add(kind, text) { if (!st) return; snap(); const id = "u" + Date.now(); st.labels.push({ id, kind, text, sub: "", x: st.W / 2, y: st.H / 2 }); sel = { t: "l", i: st.labels.length - 1 }; panel(); draw(); }
  function del() { if (!st || !sel) return; snap(); if (sel.t === "l") st.labels.splice(sel.i, 1); else if (sel.t === "v" && st.boundary.length > 3) { st.boundary.splice(sel.i, 1); st.boundary_edited = true; } sel = null; panel(); draw(); }
  function onText() { if (!st || !sel || sel.t !== "l") return; const l = st.labels[sel.i]; l.text = ($("bana-text").value || "").toUpperCase(); l.sub = $("bana-sub").value || ""; l.kind = $("bana-kind").value; draw(); }
  function onCard() { if (!st) return; st.card.title = $("bana-title").value; st.card.sub = $("bana-csub").value; draw(); }
  function panel() {
    const box = $("bana-labelpanel"); if (!box) return;
    if (sel && sel.t === "l") { box.style.display = "block"; const l = st.labels[sel.i]; $("bana-text").value = l.text; $("bana-sub").value = l.sub || ""; $("bana-kind").value = l.kind; }
    else box.style.display = "none";
  }
  function exportLayout() { if (!st) return null; return { boundary: st.boundary, boundary_edited: st.boundary_edited, card: st.card, labels: st.labels, disclaimer: st.disclaimer }; }
  return { load, export: exportLayout, init };
})();
