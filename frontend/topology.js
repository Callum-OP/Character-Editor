import * as THREE from "three";
import { ModelViewer } from "./viewer.js";

const viewer = new ModelViewer(document.getElementById("view"));

// ---- split plane (topology-specific overlay) --------------------------------
const splitPlane = new THREE.Mesh(
  new THREE.PlaneGeometry(3, 3),
  new THREE.MeshBasicMaterial({ color: 0x36c98e, transparent: true, opacity: 0.12,
    side: THREE.DoubleSide, depthWrite: false })
);
splitPlane.add(new THREE.LineSegments(
  new THREE.EdgesGeometry(new THREE.PlaneGeometry(3, 3)),
  new THREE.LineBasicMaterial({ color: 0x36c98e, transparent: true, opacity: 0.5 })));
splitPlane.visible = false;
viewer.scene.add(splitPlane);

function updateSplitPlane() {
  const axis = document.getElementById("symAxis").value;
  splitPlane.visible = document.getElementById("symPlane").checked && axis !== "none";
  splitPlane.rotation.set(0, 0, 0);
  if (axis === "x") splitPlane.rotation.y = Math.PI / 2;
  else if (axis === "y") splitPlane.rotation.x = Math.PI / 2;
}

// ---- engine status ----------------------------------------------------------
async function checkEngine() {
  const el = document.getElementById("engine");
  try {
    const e = await (await fetch("/api/engine")).json();
    if (e.name === "blender") { el.textContent = "Engine: Blender QuadriFlow ✓"; el.className = "engine ok"; }
    else if (e.name === "instant-meshes") { el.textContent = "Engine: Instant Meshes ✓"; el.className = "engine ok"; }
    else { el.innerHTML = "No engine found. Install <b>Blender</b> and set BLENDER_PATH."; el.className = "engine bad"; }
  } catch { el.textContent = "Backend not reachable."; el.className = "engine bad"; }
}

// ---- UI wiring --------------------------------------------------------------
const fileInput = document.getElementById("file");
const runBtn = document.getElementById("run");
const statusEl = document.getElementById("status");
const facesInput = document.getElementById("faces");
const statsEl = document.getElementById("stats");
const dlEl = document.getElementById("download");
const hint = document.getElementById("hint");

facesInput.addEventListener("input", () =>
  document.getElementById("facesVal").textContent = facesInput.value);

const setStatus = (msg, cls) => { statusEl.textContent = msg; statusEl.className = "status " + (cls || ""); };
const fmt = (n) => (n == null ? "?" : n.toLocaleString());

let currentFile = null;
async function loadInput(file) {
  currentFile = file || null;
  statsEl.classList.add("hidden"); dlEl.classList.add("hidden");
  if (!currentFile) { runBtn.disabled = true; return; }
  setStatus("Loading preview…", "busy");
  try {
    await viewer.setModelFromFile("original", currentFile);
    viewer.setActive("original"); activeSeg("original");
    hint.classList.add("hidden");
    runBtn.disabled = false; setStatus("Ready. Click Retopologize.", "");
  } catch (err) { setStatus("Preview failed: " + err.message, "err"); runBtn.disabled = false; }
}
fileInput.addEventListener("change", () => {
  const f = fileInput.files[0] || null;
  fileInput.value = "";                 // allow re-selecting the same file to reload
  loadInput(f);
});
window.addEventListener("project:use-model", (e) => loadInput(e.detail.file));

runBtn.addEventListener("click", async () => {
  if (!currentFile) return;
  runBtn.disabled = true;
  setStatus("Retopologizing… dense meshes can take a while.", "busy");
  statsEl.classList.add("hidden"); dlEl.classList.add("hidden");
  const fd = new FormData();
  fd.append("file", currentFile);
  fd.append("faces", facesInput.value);
  fd.append("out_format", document.getElementById("outFormat").value);
  fd.append("sym_axis", document.getElementById("symAxis").value);
  fd.append("topology", document.getElementById("topology").value);
  fd.append("sharp", document.getElementById("sharp").checked);
  try {
    const resp = await fetch("/api/retopo", { method: "POST", body: fd });
    if (!resp.ok) throw new Error((await resp.text()).slice(0, 300));
    const data = await resp.json();
    await viewer.setModelFromOBJUrl("result", data.view_url);
    viewer.setActive("result"); activeSeg("result"); hint.classList.add("hidden");
    showStats(data.stats);
    dlEl.href = data.download_url; dlEl.setAttribute("download", data.download_name);
    dlEl.textContent = "Download " + data.download_name; dlEl.classList.remove("hidden");
    setStatus("Done.", "ok");
    if (window.Project) Project.saveResult({ url: data.download_url, name: data.download_name, tool: "Topology" });
  } catch (err) { setStatus("Failed: " + err.message, "err"); }
  finally { runBtn.disabled = false; }
});

function showStats(s) {
  if (!s) return;
  const src = s.source || {}, res = s.result || {};
  const topoLabel = { quad: "Quads", quad_dominant: "Quad-dominant", triangle: "Triangles" };
  const rows = [
    ["Engine", s.engine || "?"], ["Topology", topoLabel[s.topology] || s.topology || "?"],
    ["Source triangles", fmt(src.triangles)], ["Source vertices", fmt(src.vertices)],
    ["Result faces", fmt(res.faces)], ["Result quads", fmt(res.quads)],
    ["Result triangles", fmt(res.triangles)],
    ["Quad ratio", res.quad_ratio != null ? (res.quad_ratio * 100).toFixed(1) + "%" : "?"],
    ["Result vertices", fmt(res.vertices)],
  ];
  let html = rows.map(([k, v]) => `<div class="k">${k}</div><div class="v">${v}</div>`).join("");
  if (s.notes && s.notes.length)
    html += `<div class="k">Notes</div><div class="v note">${s.notes.join("; ")}</div>`;
  statsEl.innerHTML = html; statsEl.classList.remove("hidden");
}

// view toggles
function activeSeg(view) {
  document.querySelectorAll(".seg").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
}
document.querySelectorAll(".seg").forEach((b) => b.addEventListener("click", () => {
  if (!viewer.has(b.dataset.view)) return;
  viewer.setActive(b.dataset.view); activeSeg(b.dataset.view);
}));
document.getElementById("wire").addEventListener("change", (e) => viewer.setWire(e.target.checked));
document.getElementById("shade").addEventListener("change", (e) => viewer.setShade(e.target.checked));
document.getElementById("symAxis").addEventListener("change", updateSplitPlane);
document.getElementById("symPlane").addEventListener("change", updateSplitPlane);

const topoSel = document.getElementById("topology");
const TOPO_HELP = {
  quad: "All-quad flow, ideal for subdivision/animation. Very thin parts (fingers, toes) may merge at low counts — raise the count or try hybrid.",
  quad_dominant: "Mostly quads with triangles where needed. Adaptive density preserves thin features (fingers/toes) while staying efficient.",
  triangle: "Pure triangles via adaptive remesh. Best feature preservation and the most efficient for irregular/organic shapes and 3D printing.",
};
topoSel.addEventListener("change", () => document.getElementById("topoHelp").textContent = TOPO_HELP[topoSel.value]);

updateSplitPlane();
checkEngine();
