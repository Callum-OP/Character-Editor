import { ModelViewer } from "./viewer.js";

const viewer = new ModelViewer(document.getElementById("view"));

// distinct colors so reference / source / result read clearly
const COLORS = {
  reference: { color: 0x36c98e, wireColor: 0x36c98e }, // green
  source:    { color: 0x9aa7b8, wireColor: 0x4f9dff }, // neutral/blue
  result:    { color: 0xffb454, wireColor: 0xffb454 }, // amber
};

async function checkEngine() {
  const el = document.getElementById("engine");
  try {
    const e = await (await fetch("/api/engine")).json();
    if (e.name === "blender") { el.textContent = "Engine: Blender ✓"; el.className = "engine ok"; }
    else if (e.name) { el.innerHTML = "Wrap needs <b>Blender</b>. Install it and set BLENDER_PATH."; el.className = "engine bad"; }
    else { el.innerHTML = "No engine found. Install <b>Blender</b>."; el.className = "engine bad"; }
  } catch { el.textContent = "Backend not reachable."; el.className = "engine bad"; }
}

const refInput = document.getElementById("refFile");
const srcInput = document.getElementById("srcFile");
const runBtn = document.getElementById("run");
const statusEl = document.getElementById("status");
const statsEl = document.getElementById("stats");
const dlEl = document.getElementById("download");
const hint = document.getElementById("hint");
const strength = document.getElementById("strength");
const smooth = document.getElementById("smooth");

strength.addEventListener("input", () => document.getElementById("strengthVal").textContent = strength.value);
smooth.addEventListener("input", () => document.getElementById("smoothVal").textContent = smooth.value);

const setStatus = (m, c) => { statusEl.textContent = m; statusEl.className = "status " + (c || ""); };
const fmt = (n) => (n == null ? "?" : n.toLocaleString());

let refFile = null, srcFile = null;

function activeSeg(view) {
  document.querySelectorAll(".seg").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
}
function maybeEnable() { runBtn.disabled = !(refFile && srcFile); }

refInput.addEventListener("change", async () => {
  refFile = refInput.files[0] || null;
  if (!refFile) return;
  setStatus("Loading reference…", "busy");
  try {
    await viewer.setModelFromFile("reference", refFile, COLORS.reference);
    viewer.setActive("reference"); activeSeg("reference"); hint.classList.add("hidden");
    setStatus(srcFile ? "Ready to wrap." : "Now load your model.", "");
  } catch (e) { setStatus("Reference failed: " + e.message, "err"); }
  maybeEnable();
});

srcInput.addEventListener("change", async () => {
  srcFile = srcInput.files[0] || null;
  if (!srcFile) return;
  setStatus("Loading your model…", "busy");
  try {
    await viewer.setModelFromFile("source", srcFile, COLORS.source);
    viewer.setActive("source"); activeSeg("source"); hint.classList.add("hidden");
    setStatus(refFile ? "Ready to wrap." : "Now load a reference.", "");
  } catch (e) { setStatus("Model failed: " + e.message, "err"); }
  maybeEnable();
});

runBtn.addEventListener("click", async () => {
  if (!refFile || !srcFile) return;
  runBtn.disabled = true;
  setStatus("Wrapping… projecting and smoothing vertices.", "busy");
  statsEl.classList.add("hidden"); dlEl.classList.add("hidden");
  const fd = new FormData();
  fd.append("reference", refFile);
  fd.append("source", srcFile);
  fd.append("strength", strength.value / 100);
  fd.append("smooth_iters", smooth.value);
  fd.append("shape_keys", document.getElementById("shapeKeys").value);
  fd.append("align", document.getElementById("align").value);
  fd.append("out_format", document.getElementById("outFormat").value);
  try {
    const resp = await fetch("/api/wrap", { method: "POST", body: fd });
    if (!resp.ok) throw new Error((await resp.text()).slice(0, 300));
    const data = await resp.json();
    await viewer.setModelFromOBJUrl("result", data.view_url, COLORS.result);
    viewer.setActive("result"); activeSeg("result"); hint.classList.add("hidden");
    showStats(data.stats);
    dlEl.href = data.download_url; dlEl.setAttribute("download", data.download_name);
    dlEl.textContent = "Download " + data.download_name; dlEl.classList.remove("hidden");
    setStatus("Done.", "ok");
  } catch (e) { setStatus("Failed: " + e.message, "err"); }
  finally { runBtn.disabled = false; }
});

function showStats(s) {
  if (!s) return;
  const rows = [
    ["Engine", s.engine || "blender"],
    ["Shape keys", s.shape_key_mode || "?"],
    ["Kept shape keys", s.shape_keys != null ? fmt(s.shape_keys) : "—"],
    ["Your vertices", fmt(s.source_vertices)],
    ["Reference vertices", fmt(s.reference_vertices)],
    ["Mean offset", s.mean_offset != null ? s.mean_offset.toFixed(4) : "?"],
    ["Max offset", s.max_offset != null ? s.max_offset.toFixed(4) : "?"],
  ];
  let html = rows.map(([k, v]) => `<div class="k">${k}</div><div class="v">${v}</div>`).join("");
  if (s.notes && s.notes.length)
    html += `<div class="k">Notes</div><div class="v note">${s.notes.join("; ")}</div>`;
  statsEl.innerHTML = html; statsEl.classList.remove("hidden");
}

document.querySelectorAll(".seg").forEach((b) => b.addEventListener("click", () => {
  if (!viewer.has(b.dataset.view)) { setStatus("Nothing loaded for “" + b.dataset.view + "” yet.", ""); return; }
  viewer.setActive(b.dataset.view); activeSeg(b.dataset.view);
}));
document.getElementById("wire").addEventListener("change", (e) => viewer.setWire(e.target.checked));
document.getElementById("shade").addEventListener("change", (e) => viewer.setShade(e.target.checked));

checkEngine();
