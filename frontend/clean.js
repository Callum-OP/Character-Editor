import { ModelViewer } from "./viewer.js";

const viewer = new ModelViewer(document.getElementById("view"));

// ---- engine status ----------------------------------------------------------
async function checkEngine() {
  const el = document.getElementById("engine");
  try {
    const e = await (await fetch("/api/engine")).json();
    if (e.name === "blender") { el.textContent = "Engine: Blender ✓"; el.className = "engine ok"; }
    else if (e.name === "instant-meshes") { el.innerHTML = "Cleanup needs <b>Blender</b> (Instant Meshes can't repair)."; el.className = "engine bad"; }
    else { el.innerHTML = "No engine found. Install <b>Blender</b> and set BLENDER_PATH."; el.className = "engine bad"; }
  } catch { el.textContent = "Backend not reachable."; el.className = "engine bad"; }
}

// ---- UI wiring --------------------------------------------------------------
const fileInput = document.getElementById("file");
const runBtn = document.getElementById("run");
const statusEl = document.getElementById("status");
const statsEl = document.getElementById("stats");
const dlEl = document.getElementById("download");
const lodListEl = document.getElementById("lodList");
const hint = document.getElementById("hint");
const modeSel = document.getElementById("mode");
const mergeDist = document.getElementById("mergeDist");

mergeDist.addEventListener("input", () =>
  document.getElementById("mergeVal").textContent = mergeDist.value);

modeSel.addEventListener("change", () => {
  const lod = modeSel.value === "lod";
  document.getElementById("cleanFields").classList.toggle("hidden", lod);
  document.getElementById("lodFields").classList.toggle("hidden", !lod);
  runBtn.textContent = lod ? "Generate LOD" : "Clean up";
});

const setStatus = (msg, cls) => { statusEl.textContent = msg; statusEl.className = "status " + (cls || ""); };
const fmt = (n) => (n == null ? "?" : n.toLocaleString());

let currentFile = null;
async function loadInput(file) {
  currentFile = file || null;
  statsEl.classList.add("hidden"); dlEl.classList.add("hidden"); lodListEl.classList.add("hidden");
  if (!currentFile) { runBtn.disabled = true; return; }
  setStatus("Loading preview…", "busy");
  try {
    await viewer.setModelFromFile("original", currentFile);
    viewer.setActive("original"); activeSeg("original");
    hint.classList.add("hidden");
    runBtn.disabled = false; setStatus("Ready. Click Run.", "");
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
  statsEl.classList.add("hidden"); dlEl.classList.add("hidden"); lodListEl.classList.add("hidden");
  const fd = new FormData();
  fd.append("file", currentFile);
  fd.append("out_format", document.getElementById("outFormat").value);
  try {
    if (modeSel.value === "lod") await runLod(fd);
    else await runClean(fd);
  } catch (err) { setStatus("Failed: " + err.message, "err"); }
  finally { runBtn.disabled = false; }
});

async function runClean(fd) {
  setStatus("Cleaning up…", "busy");
  fd.append("merge_dist", mergeDist.value);
  fd.append("fix_normals", document.getElementById("fixNormals").checked);
  fd.append("fill_holes", document.getElementById("fillHoles").checked);
  fd.append("delete_loose", document.getElementById("deleteLoose").checked);
  fd.append("degenerate", document.getElementById("degenerate").checked);
  fd.append("flip", document.getElementById("flip").checked);

  const resp = await fetch("/api/clean", { method: "POST", body: fd });
  if (!resp.ok) throw new Error((await resp.text()).slice(0, 300));
  const data = await resp.json();
  await viewer.setModelFromOBJUrl("result", data.view_url);
  viewer.setActive("result"); activeSeg("result"); hint.classList.add("hidden");
  showCleanStats(data.stats);
  dlEl.href = data.download_url; dlEl.setAttribute("download", data.download_name);
  dlEl.textContent = "Download " + data.download_name; dlEl.classList.remove("hidden");
  setStatus("Done.", "ok");
  if (window.Project) Project.offerResult({ url: data.download_url, name: data.download_name, tool: "Cleanup" });
}

async function runLod(fd) {
  setStatus("Generating LOD levels…", "busy");
  fd.append("ratios", document.getElementById("ratios").value);
  fd.append("target_faces", document.getElementById("targetFaces").value || "0");
  fd.append("preserve_uv", document.getElementById("preserveUv").checked);
  fd.append("preserve_boundary", document.getElementById("preserveBoundary").checked);

  const resp = await fetch("/api/lod", { method: "POST", body: fd });
  if (!resp.ok) throw new Error((await resp.text()).slice(0, 300));
  const data = await resp.json();
  showLodLevels(data);
  // Preview the first (highest-detail) level.
  if (data.levels && data.levels.length) {
    await viewer.setModelFromOBJUrl("result", data.levels[0].view_url);
    viewer.setActive("result"); activeSeg("result"); hint.classList.add("hidden");
  }
  setStatus("Done — " + (data.levels ? data.levels.length : 0) + " level(s).", "ok");
}

function showCleanStats(s) {
  if (!s) return;
  const b = s.before || {}, a = s.after || {};
  const rows = [
    ["", "Before → After"],
    ["Vertices", fmt(b.vertices) + " → " + fmt(a.vertices)],
    ["Faces", fmt(b.faces) + " → " + fmt(a.faces)],
    ["Non-manifold edges", fmt(b.non_manifold_edges) + " → " + fmt(a.non_manifold_edges)],
    ["Boundary / hole edges", fmt(b.boundary_edges) + " → " + fmt(a.boundary_edges)],
    ["Loose vertices", fmt(b.loose_vertices) + " → " + fmt(a.loose_vertices)],
  ];
  let html = rows.map(([k, v]) => `<div class="k">${k}</div><div class="v">${v}</div>`).join("");
  if (s.notes && s.notes.length)
    html += `<div class="k">Notes</div><div class="v note">${s.notes.join("; ")}</div>`;
  statsEl.innerHTML = html; statsEl.classList.remove("hidden");
}

function showLodLevels(data) {
  const src = data.source || {};
  const levels = data.levels || [];
  let html = `<div class="k">Source faces</div><div class="v">${fmt(src.faces)}</div>`;
  lodListEl.innerHTML = html;
  levels.forEach((lv) => {
    const row = document.createElement("div");
    row.style.gridColumn = "1 / -1";
    row.style.display = "flex";
    row.style.gap = "8px";
    row.style.alignItems = "center";
    row.style.justifyContent = "space-between";
    row.innerHTML =
      `<span>LOD ${lv.level} · ${(lv.ratio * 100).toFixed(1)}% · ${fmt(lv.faces)} faces</span>`;
    const dl = document.createElement("a");
    dl.href = lv.download_url; dl.setAttribute("download", lv.download_name);
    dl.className = "seg"; dl.textContent = "Download";
    const use = document.createElement("button");
    use.type = "button"; use.className = "seg"; use.textContent = "Preview";
    use.addEventListener("click", async () => {
      await viewer.setModelFromOBJUrl("result", lv.view_url);
      viewer.setActive("result"); activeSeg("result");
      if (window.Project) Project.offerResult({ url: lv.download_url, name: lv.download_name, tool: "LOD" });
    });
    const acts = document.createElement("span");
    acts.style.display = "flex"; acts.style.gap = "6px";
    acts.append(use, dl);
    row.append(acts);
    lodListEl.append(row);
  });
  lodListEl.classList.remove("hidden");
}

// view toggles
function activeSeg(view) {
  document.querySelectorAll("footer .seg").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
}
document.querySelectorAll("footer .seg").forEach((b) => b.addEventListener("click", () => {
  if (!b.dataset.view || !viewer.has(b.dataset.view)) return;
  viewer.setActive(b.dataset.view); activeSeg(b.dataset.view);
}));
document.getElementById("wire").addEventListener("change", (e) => viewer.setWire(e.target.checked));
document.getElementById("shade").addEventListener("change", (e) => viewer.setShade(e.target.checked));

checkEngine();
