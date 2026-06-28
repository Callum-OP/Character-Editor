import { ModelViewer } from "./viewer.js";

const viewer = new ModelViewer(document.getElementById("view"));

const COLORS = {
  reference: { color: 0x36c98e, wireColor: 0x36c98e }, // green
  source:    { color: 0x9aa7b8, wireColor: 0x4f9dff }, // neutral/blue
  result:    { color: 0xffb454, wireColor: 0xffb454 }, // amber
};
const MARKER_COLOR = { reference: 0x36c98e, source: 0x4f9dff };

const el = (id) => document.getElementById(id);
const setStatus = (m, c) => { const s = el("status"); s.textContent = m; s.className = "status " + (c || ""); };
const fmt = (n) => (n == null ? "?" : n.toLocaleString());

let refFile = null, srcFile = null;
let jobId = null;
let markerMode = false;
let marks = { reference: [], source: [] };

async function checkEngine() {
  const e0 = el("engine");
  try {
    const e = await (await fetch("/api/engine")).json();
    if (e.name === "blender") { e0.textContent = "Engine: Blender ✓"; e0.className = "engine ok"; }
    else if (e.name) { e0.innerHTML = "Wrap needs <b>Blender</b>. Install it and set BLENDER_PATH."; e0.className = "engine bad"; }
    else { e0.innerHTML = "No engine found. Install <b>Blender</b>."; e0.className = "engine bad"; }
  } catch { e0.textContent = "Backend not reachable."; e0.className = "engine bad"; }
}

function activeSeg(view) {
  document.querySelectorAll(".seg[data-view]").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
}

function renderMarkers() {
  viewer.setMarkers("reference", marks.reference, MARKER_COLOR.reference);
  viewer.setMarkers("source", marks.source, MARKER_COLOR.source);
  const r = marks.reference.length, s = marks.source.length;
  let txt = `Reference: ${r} · Yours: ${s}`;
  if (r !== s) txt += ` — place an equal number (pairs match in order).`;
  else if (r > 0) txt += ` — ${r} pair${r > 1 ? "s" : ""} ready.`;
  el("markerInfo").textContent = r + s === 0 ? "No landmarks placed." : txt;
}

// ---- prepare (phase 1): exchange files for index-correct preview OBJs --------
async function prepare() {
  if (!refFile || !srcFile) return;
  setStatus("Preparing models for picking…", "busy");
  el("run").disabled = true;
  const fd = new FormData();
  fd.append("reference", refFile);
  fd.append("source", srcFile);
  try {
    const data = await (await fetch("/api/wrap/prepare", { method: "POST", body: fd })).json();
    if (data.detail) throw new Error(data.detail);
    jobId = data.job_id;
    marks = { reference: [], source: [] };
    await viewer.setModelFromOBJUrl("reference", data.reference_view_url, COLORS.reference);
    await viewer.setModelFromOBJUrl("source", data.source_view_url, COLORS.source);
    viewer.setActive("source"); activeSeg("source");
    el("hint").classList.add("hidden");
    el("landmarks").classList.remove("hidden");
    viewer.enablePicking(onPick);
    renderMarkers();
    el("run").disabled = false;
    setStatus(`Ready. ${data.source_shape_keys || 0} shape keys detected. ` +
              `Optionally place landmarks, then Wrap.`, "");
  } catch (e) { setStatus("Prepare failed: " + e.message, "err"); }
}

function onPick({ key, index }) {
  if (!markerMode) return;
  if (key !== "reference" && key !== "source") return;
  marks[key].push(index);
  renderMarkers();
}

el("refFile").addEventListener("change", async () => {
  refFile = el("refFile").files[0] || null;
  if (refFile) { try { await viewer.setModelFromFile("reference", refFile, COLORS.reference); viewer.setActive("reference"); activeSeg("reference"); el("hint").classList.add("hidden"); } catch {} }
  if (refFile && srcFile) prepare();
});
el("srcFile").addEventListener("change", async () => {
  srcFile = el("srcFile").files[0] || null;
  if (srcFile) { try { await viewer.setModelFromFile("source", srcFile, COLORS.source); viewer.setActive("source"); activeSeg("source"); el("hint").classList.add("hidden"); } catch {} }
  if (refFile && srcFile) prepare();
});

// ---- landmark controls ------------------------------------------------------
el("markerMode").addEventListener("click", () => {
  markerMode = !markerMode;
  el("markerMode").textContent = "Place markers: " + (markerMode ? "ON" : "off");
  el("markerMode").classList.toggle("active", markerMode);
  viewer.controls.enableRotate = !markerMode ? true : true; // keep orbit; picks are stationary clicks
  setStatus(markerMode ? `Click matching points on the “${viewer.active}” view.` : "", markerMode ? "busy" : "");
});
el("undoMarker").addEventListener("click", () => {
  const k = viewer.active;
  if (marks[k] && marks[k].length) { marks[k].pop(); renderMarkers(); }
});
el("clearMarkers").addEventListener("click", () => { marks = { reference: [], source: [] }; renderMarkers(); });

// ---- view toggles -----------------------------------------------------------
document.querySelectorAll(".seg[data-view]").forEach((b) => b.addEventListener("click", () => {
  if (!viewer.has(b.dataset.view)) { setStatus(`Nothing loaded for “${b.dataset.view}” yet.`, ""); return; }
  viewer.setActive(b.dataset.view); activeSeg(b.dataset.view);
  if (markerMode) setStatus(`Click matching points on the “${b.dataset.view}” view.`, "busy");
}));
el("wire").addEventListener("change", (e) => viewer.setWire(e.target.checked));
el("shade").addEventListener("change", (e) => viewer.setShade(e.target.checked));
el("strength").addEventListener("input", () => el("strengthVal").textContent = el("strength").value);
el("smooth").addEventListener("input", () => el("smoothVal").textContent = el("smooth").value);

// ---- wrap (phase 2) ---------------------------------------------------------
el("run").addEventListener("click", async () => {
  if (!jobId) { setStatus("Load both models first.", "err"); return; }
  if (marks.reference.length !== marks.source.length) {
    setStatus("Landmark counts differ — place an equal number on each (or clear them).", "err");
    return;
  }
  el("run").disabled = true;
  setStatus("Wrapping… morphing, projecting and smoothing.", "busy");
  el("stats").classList.add("hidden"); el("download").classList.add("hidden");
  const fd = new FormData();
  fd.append("job_id", jobId);
  fd.append("strength", el("strength").value / 100);
  fd.append("smooth_iters", el("smooth").value);
  fd.append("shape_keys", el("shapeKeys").value);
  fd.append("align", el("align").value);
  fd.append("sym_axis", el("symAxis").value);
  fd.append("out_format", el("outFormat").value);
  if (marks.reference.length)
    fd.append("landmarks", JSON.stringify({ ref: marks.reference, src: marks.source }));
  try {
    const resp = await fetch("/api/wrap", { method: "POST", body: fd });
    if (!resp.ok) throw new Error((await resp.text()).slice(0, 300));
    const data = await resp.json();
    await viewer.setModelFromOBJUrl("result", data.view_url, COLORS.result);
    viewer.setActive("result"); activeSeg("result");
    showStats(data.stats);
    const d = el("download");
    d.href = data.download_url; d.setAttribute("download", data.download_name);
    d.textContent = "Download " + data.download_name; d.classList.remove("hidden");
    setStatus("Done.", "ok");
  } catch (e) { setStatus("Failed: " + e.message, "err"); }
  finally { el("run").disabled = false; }
});

function showStats(s) {
  if (!s) return;
  const rows = [
    ["Engine", s.engine || "blender"],
    ["Landmarks used", fmt(s.landmarks)],
    ["Symmetry", s.sym_axis && s.sym_axis !== "none" ? s.sym_axis.toUpperCase() : "—"],
    ["Shape keys", s.shape_key_mode],
    ["Kept shape keys", s.shape_keys != null ? fmt(s.shape_keys) : "—"],
    ["Your vertices", fmt(s.source_vertices)],
    ["Reference vertices", fmt(s.reference_vertices)],
    ["Surface match", s.residual_pct != null
      ? (s.residual_pct < 0.01 ? "exact" : (100 - s.residual_pct).toFixed(1) + "%") : "?"],
    ["Mean vertex move", s.mean_offset != null ? s.mean_offset.toFixed(4) : "?"],
  ];
  let html = rows.map(([k, v]) => `<div class="k">${k}</div><div class="v">${v}</div>`).join("");
  if (s.notes && s.notes.length)
    html += `<div class="k">Notes</div><div class="v note">${s.notes.join("; ")}</div>`;
  el("stats").innerHTML = html; el("stats").classList.remove("hidden");
}

checkEngine();
