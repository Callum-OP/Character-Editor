import { ModelViewer } from "./viewer.js";

const viewer = new ModelViewer(document.getElementById("view"));

// ---- engine status ----------------------------------------------------------
async function checkEngine() {
  const el = document.getElementById("engine");
  try {
    const e = await (await fetch("/api/engine")).json();
    if (e.name === "blender") { el.textContent = "Engine: Blender ✓"; el.className = "engine ok"; }
    else if (e.name === "instant-meshes") { el.innerHTML = "Conversion needs <b>Blender</b> (Instant Meshes can't transcode)."; el.className = "engine bad"; }
    else { el.innerHTML = "No engine found. Install <b>Blender</b> and set BLENDER_PATH."; el.className = "engine bad"; }
  } catch { el.textContent = "Backend not reachable."; el.className = "engine bad"; }
}

// ---- UI wiring --------------------------------------------------------------
const fileInput = document.getElementById("file");
const runBtn = document.getElementById("run");
const statusEl = document.getElementById("status");
const statsEl = document.getElementById("stats");
const dlEl = document.getElementById("download");
const hint = document.getElementById("hint");
const outFormat = document.getElementById("outFormat");
const fmtNote = document.getElementById("fmtNote");

const FMT_NOTES = {
  fbx: "FBX packs the mesh, rig, animations and textures into a single file.",
  glb: "GLB is glTF's binary form — mesh, rig, animations and textures in one file.",
  gltf: "glTF (separate) writes a .gltf + .bin + texture images, delivered as one .zip.",
  obj: "OBJ writes a .mtl and texture images alongside the mesh, delivered as one .zip. No rig/animation.",
  ply: "PLY stores geometry and per-vertex colors only — no textures, rig or animation.",
  stl: "STL stores triangles only — ideal for 3D printing, everything else is dropped.",
};
const embedRow = document.getElementById("embedRow");
const embedNote = document.getElementById("embedNote");
const compressEl = document.getElementById("compress");

function syncOptions() {
  fmtNote.textContent = FMT_NOTES[outFormat.value] || "";
  // Embedding is an FBX choice: GLB always embeds by design, glTF/OBJ always
  // write sidecar files, PLY/STL carry no textures at all.
  const isFbx = outFormat.value === "fbx";
  embedRow.classList.toggle("hidden", !isFbx);
  embedNote.classList.toggle("hidden", !isFbx);
  // Nothing to compress in PLY/STL (geometry-only, no Draco, no textures).
  const noCompress = outFormat.value === "ply" || outFormat.value === "stl";
  compressEl.disabled = noCompress;
  if (noCompress) compressEl.value = "none";
}
outFormat.addEventListener("change", syncOptions);
syncOptions();

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
    runBtn.disabled = false; setStatus("Ready. Pick a format and convert.", "");
  } catch (err) {
    // Preview is a convenience — conversion happens server-side, so a file the
    // browser can't parse can still be converted.
    setStatus("Preview failed (" + err.message + ") — conversion may still work.", "");
    runBtn.disabled = false;
  }
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
  statsEl.classList.add("hidden"); dlEl.classList.add("hidden");
  const fd = new FormData();
  fd.append("file", currentFile);
  fd.append("out_format", outFormat.value);
  fd.append("embed_textures", document.getElementById("embedTex").checked);
  fd.append("strip_rig", document.getElementById("stripRig").checked);
  fd.append("compress", compressEl.value);
  try {
    setStatus("Converting…", "busy");
    const resp = await fetch("/api/convert", { method: "POST", body: fd });
    if (!resp.ok) throw new Error((await resp.text()).slice(0, 300));
    const data = await resp.json();

    dlEl.href = data.download_url; dlEl.setAttribute("download", data.download_name);
    dlEl.textContent = "Download " + data.download_name; dlEl.classList.remove("hidden");
    showStats(data);

    // Preview the converted file when it's a single model file (zips bundle
    // sidecar textures the browser-side loaders can't resolve).
    if (!data.zipped) {
      try {
        const blob = await (await fetch(data.download_url)).blob();
        await viewer.setModelFromFile("result", new File([blob], data.download_name));
        viewer.setActive("result"); activeSeg("result"); hint.classList.add("hidden");
      } catch { /* result preview is best-effort */ }
    }
    setStatus("Done.", "ok");
  } catch (err) { setStatus("Failed: " + err.message, "err"); }
  finally { runBtn.disabled = false; }
});

function showStats(data) {
  const s = data.stats;
  let rows = [];
  if (s) {
    rows = [
      ["Objects", fmt(s.objects) + (s.meshes != null ? " (" + fmt(s.meshes) + " mesh)" : "")],
      ["Vertices", fmt(s.vertices)],
      ["Faces", fmt(s.faces)],
      ["Materials", fmt(s.materials)],
      ["Textures", fmt(s.textures)],
    ];
    if (s.armatures) rows.push(["Armatures", fmt(s.armatures)]);
    if (s.animations) rows.push(["Animations", fmt(s.animations)]);
  }
  let html = rows.map(([k, v]) => `<div class="k">${k}</div><div class="v">${v}</div>`).join("");
  if (data.notes && data.notes.length)
    html += `<div class="k">Notes</div><div class="v note">${data.notes.join("; ")}</div>`;
  if (!html) return;
  statsEl.innerHTML = html; statsEl.classList.remove("hidden");
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
