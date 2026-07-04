import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";

// ---- expression presets: named combinations of ARKit blendshapes -----------
const PRESETS = {
  "Neutral": {},
  "Smile": { mouthSmileLeft: 1, mouthSmileRight: 1, cheekSquintLeft: 0.4, cheekSquintRight: 0.4 },
  "Big grin": { mouthSmileLeft: 1, mouthSmileRight: 1, cheekSquintLeft: 0.7, cheekSquintRight: 0.7, jawOpen: 0.18, eyeSquintLeft: 0.4, eyeSquintRight: 0.4 },
  "Frown": { mouthFrownLeft: 1, mouthFrownRight: 1, browDownLeft: 0.4, browDownRight: 0.4 },
  "Sad": { browInnerUp: 0.8, mouthFrownLeft: 0.6, mouthFrownRight: 0.6 },
  "Angry": { browDownLeft: 1, browDownRight: 1, noseSneerLeft: 0.5, noseSneerRight: 0.5, mouthPressLeft: 0.4, mouthPressRight: 0.4 },
  "Surprise": { jawOpen: 0.55, eyeWideLeft: 1, eyeWideRight: 1, browInnerUp: 0.8, browOuterUpLeft: 0.6, browOuterUpRight: 0.6 },
  "Blink": { eyeBlinkLeft: 1, eyeBlinkRight: 1 },
  "Wink (L)": { eyeBlinkLeft: 1, mouthSmileLeft: 0.5 },
  "Jaw open": { jawOpen: 1 },
  "Kiss / pucker": { mouthPucker: 1 },
  "Disgust": { noseSneerLeft: 1, noseSneerRight: 1, mouthUpperUpLeft: 0.5, mouthUpperUpRight: 0.5, browDownLeft: 0.4, browDownRight: 0.4 },
};

// ---- minimal Three.js viewer with morph-target driving ----------------------
const canvas = document.getElementById("view");
const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0b0e18);
const camera = new THREE.PerspectiveCamera(35, 1, 0.001, 100);
camera.position.set(0, 0.1, 1.2);
const controls = new OrbitControls(camera, canvas);
controls.enableDamping = true;
scene.add(new THREE.HemisphereLight(0xffffff, 0x404050, 1.2));
const key = new THREE.DirectionalLight(0xffffff, 1.6); key.position.set(2, 3, 4); scene.add(key);
const rim = new THREE.DirectionalLight(0x88aaff, 0.6); rim.position.set(-3, 1, -2); scene.add(rim);
const grid = new THREE.GridHelper(1.6, 24, 0x2c3556, 0x1a2038);
grid.position.y = -0.32; scene.add(grid);

let root = null;                  // loaded model root
let morphMeshes = [];             // meshes that carry the ARKit morphs
let morphNames = [];              // union of morph names

function resize() {
  const w = canvas.clientWidth, h = canvas.clientHeight;
  if (canvas.width !== w || canvas.height !== h) {
    renderer.setSize(w, h, false); camera.aspect = w / h; camera.updateProjectionMatrix();
  }
}
(function loop() { resize(); controls.update(); renderer.render(scene, camera); requestAnimationFrame(loop); })();

function frame(obj) {
  const box = new THREE.Box3().setFromObject(obj);
  const size = box.getSize(new THREE.Vector3()), center = box.getCenter(new THREE.Vector3());
  const s = 0.6 / (Math.max(size.x, size.y, size.z) || 1);
  obj.scale.setScalar(s);
  obj.position.sub(center.multiplyScalar(s));
  controls.target.set(0, 0, 0);
}

async function loadGLB(url) {
  if (root) { scene.remove(root); root = null; }
  morphMeshes = []; morphNames = [];
  const gltf = await new GLTFLoader().loadAsync(url);
  root = gltf.scene;
  root.traverse((o) => {
    if (o.isMesh && o.morphTargetDictionary) {
      morphMeshes.push(o);
      for (const n of Object.keys(o.morphTargetDictionary))
        if (!morphNames.includes(n)) morphNames.push(n);
    }
    if (o.isMesh && o.material) { o.material.side = THREE.DoubleSide; }
  });
  scene.add(root);
  frame(root);
  const hint = el("hint");
  if (hint) hint.classList.add("hidden");   // model is showing — clear the overlay
}

// set one morph by name across all meshes that have it
function setMorph(name, value) {
  for (const m of morphMeshes) {
    const i = m.morphTargetDictionary[name];
    if (i !== undefined) m.morphTargetInfluences[i] = value;
  }
}
function setWeights(weights) {
  for (const n of morphNames) setMorph(n, weights[n] || 0);
}

// ---- UI ---------------------------------------------------------------------
const el = (id) => document.getElementById(id);
const setStatus = (m, c) => { const s = el("status"); s.textContent = m; s.className = "status " + (c || ""); };
let current = {};   // current weights
let file = null;
let glbUrl = null, glbDownload = null, fbxDownload = null;   // result URLs

async function checkEngine() {
  const e0 = el("engine");
  try {
    const c = await (await fetch("/api/rig/config")).json();
    if (c.blender) { e0.textContent = "Engine: Blender ✓"; e0.className = "engine ok"; }
    else { e0.innerHTML = "Needs <b>Blender</b>. Set BLENDER_PATH."; e0.className = "engine bad"; }
  } catch { e0.textContent = "Backend not reachable."; e0.className = "engine bad"; }
}

el("file").addEventListener("change", () => {
  file = el("file").files[0] || null;
  el("file").value = "";                // allow re-selecting the same file to reload
  el("run").disabled = !file;
  if (file) setStatus("Ready. Generate expressions.", "");
});
// carry a head model in from the active project
window.addEventListener("project:use-model", (e) => {
  file = e.detail.file;
  el("run").disabled = false;
  setStatus("Loaded from project. Generate expressions.", "");
});

el("run").addEventListener("click", async () => {
  if (!file) return;
  el("run").disabled = true;
  setStatus("Detecting face + building ARKit-52 shape keys… (~20s)", "busy");
  el("exprPanel").classList.add("hidden");
  try {
    // phase 1: prep (head-only) — auto-detect face markers + front view
    const ext = file.name.split(".").pop().toLowerCase();
    const prep = await (await fetch("/api/rig/prep?headOnly=1&ext=" + encodeURIComponent(ext),
      { method: "POST", headers: { "Content-Type": "application/octet-stream" },
        body: await file.arrayBuffer() })).json();
    if (prep.error) throw new Error(prep.error);
    // phase 2: build face shape keys
    const build = await (await fetch("/api/rig/build", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: prep.token, headOnly: true,
        faceMarkers: prep.faceMarkers, calib: prep.calib }),
    })).json();
    if (build.error) throw new Error(build.error);
    await loadGLB(build.glbUrl);
    glbUrl = build.glbUrl;
    glbDownload = build.glbDownload;
    fbxDownload = build.fbxDownload || null;
    buildExprUI();
    el("exprPanel").classList.remove("hidden");
    setStatus(`Done — ${morphNames.length} shape keys. Pick an expression.`, "ok");
    if (window.Project) {
      const base = file && file.name ? file.name.replace(/\.[^.]+$/, "") : "head";
      Project.saveResult({ url: glbUrl, name: base + "_face.glb", tool: "Face" });
    }
  } catch (e) { setStatus("Failed: " + e.message, "err"); }
  finally { el("run").disabled = false; }
});

function buildExprUI() {
  // preset dropdown
  const sel = el("preset");
  sel.innerHTML = "";
  for (const name of Object.keys(PRESETS)) {
    const o = document.createElement("option"); o.value = name; o.textContent = name; sel.appendChild(o);
  }
  sel.onchange = () => applyPreset(sel.value);

  // per-shape sliders (only the morphs the model actually has)
  el("skCount").textContent = `(${morphNames.length})`;
  const box = el("sliders"); box.innerHTML = "";
  const order = morphNames.slice().sort();
  for (const name of order) {
    const row = document.createElement("label"); row.className = "sk-row";
    const lab = document.createElement("span"); lab.textContent = name; lab.className = "sk-name";
    const sl = document.createElement("input");
    sl.type = "range"; sl.min = 0; sl.max = 1; sl.step = 0.05; sl.value = 0; sl.dataset.name = name;
    sl.addEventListener("input", () => { current[name] = +sl.value; setMorph(name, +sl.value); });
    row.appendChild(lab); row.appendChild(sl); box.appendChild(row);
  }
  applyPreset("Neutral");
}

function applyPreset(name) {
  current = { ...(PRESETS[name] || {}) };
  setWeights(current);
  // sync sliders
  for (const sl of el("sliders").querySelectorAll("input[type=range]"))
    sl.value = current[sl.dataset.name] || 0;
}

// ---- export in the chosen format -------------------------------------------
// GLB/FBX come straight from the rig build; glTF/OBJ/PLY/STL are transcoded from
// the GLB by the Blender convert endpoint (shared with Cloth Studio).
el("download").addEventListener("click", async () => {
  if (!glbUrl) return;
  const fmt = el("outFormat").value;
  const btn = el("download");
  if (fmt === "glb") return void (window.location.href = glbDownload);
  if (fmt === "fbx" && fbxDownload) return void (window.location.href = fbxDownload);
  btn.disabled = true;
  setStatus(`Converting to ${fmt.toUpperCase()}…`, "busy");
  try {
    const glb = await (await fetch(glbUrl)).blob();
    const fd = new FormData();
    fd.append("file", new File([glb], "face.glb", { type: "model/gltf-binary" }));
    fd.append("out_format", fmt);
    const res = await fetch("/api/cloth/convert", { method: "POST", body: fd });
    const data = await res.json();
    if (!res.ok || !data.download_url) throw new Error(data.detail || "conversion failed");
    window.location.href = data.download_url;
    setStatus(`Exported ${fmt.toUpperCase()}.`, "ok");
  } catch (e) {
    setStatus("Export failed: " + e.message, "err");
  } finally {
    btn.disabled = false;
  }
});

el("resetExpr").addEventListener("click", () => { el("preset").value = "Neutral"; applyPreset("Neutral"); });
el("wire").addEventListener("change", (e) => {
  for (const m of morphMeshes.length ? morphMeshes : []) m.material.wireframe = e.target.checked;
  if (root) root.traverse((o) => { if (o.isMesh && o.material) o.material.wireframe = e.target.checked; });
});

checkEngine();
