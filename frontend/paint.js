// ===========================================================================
// paint.js — Paint Studio: paint directly onto 3D models in the browser.
//
// Built on the shared design system + project workspace (shell.js). Expands the
// reference 3D-Texture-Painter into a layered, multi-brush studio:
//   - a full layer stack per mesh with blend modes / opacity / visibility
//   - advanced brushes: paint, airbrush, eraser, smudge (soft), blur (soft),
//     bucket fill and eyedropper
//   - symmetric "mirror" painting (paint one side, the other paints too) by
//     reflecting the pick ray across the model's centre plane
//   - undo/redo, PNG texture export, GLB export and project save/load
// ===========================================================================
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { OBJLoader } from "three/addons/loaders/OBJLoader.js";
import { FBXLoader } from "three/addons/loaders/FBXLoader.js";
import { PLYLoader } from "three/addons/loaders/PLYLoader.js";
import { STLLoader } from "three/addons/loaders/STLLoader.js";
import { GLTFExporter } from "three/addons/exporters/GLTFExporter.js";
import { ViewHelper } from "three/addons/helpers/ViewHelper.js";
import { PaintSurface, BLEND_MODES } from "./paintlayer.js";

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
const brush = {
  tool: "paint",              // paint | airbrush | erase | smudge | blur | fill | eyedropper
  color: [229, 72, 77],
  size: 60,
  opacity: 1.0,
  hardness: 0.7,
  flow: 1.0,
  spacing: 0.12,
};
const mirror = { on: false, axis: 0 };   // axis 0/1/2 = x/y/z
let textureRes = 1024;
let baseColor = "#d8d3c8";

let modelRoot = null;
const surfaces = new Map();   // mesh -> PaintSurface
let currentSurface = null;    // surface whose layers the panel shows

let isPainting = false;
let orbiting = false;
let strokeSurfaces = new Set();
let airTimer = null;          // airbrush build-up while holding still
let lastHits = [];

// ---------------------------------------------------------------------------
// Three.js
// ---------------------------------------------------------------------------
const canvas = document.getElementById("gl");
const stage = document.getElementById("stage");

const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
renderer.outputColorSpace = THREE.SRGBColorSpace;

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0b0e18);

const camera = new THREE.PerspectiveCamera(45, 1, 0.01, 1000);
camera.position.set(0, 1.2, 4);

const controls = new OrbitControls(camera, canvas);
controls.enableDamping = true;
controls.dampingFactor = 0.08;
// Left button paints; orbit with right, pan with middle.
controls.mouseButtons = { LEFT: null, MIDDLE: THREE.MOUSE.PAN, RIGHT: THREE.MOUSE.ROTATE };
controls.touches = { ONE: null, TWO: THREE.TOUCH.DOLLY_PAN };

scene.add(new THREE.HemisphereLight(0xffffff, 0x30343c, 1.0));
const keyLight = new THREE.DirectionalLight(0xffffff, 2.0);
keyLight.position.set(3, 5, 4); scene.add(keyLight);
const fillLight = new THREE.DirectionalLight(0xbcd0ff, 0.6);
fillLight.position.set(-4, 1, -3); scene.add(fillLight);

const grid = new THREE.GridHelper(20, 20, 0x2c3556, 0x1a2038);
grid.position.y = -1.001; scene.add(grid);

// Symmetry plane visual (shown while mirror is on).
const planeMat = new THREE.MeshBasicMaterial({
  color: 0x7c8cff, transparent: true, opacity: 0.12, side: THREE.DoubleSide, depthWrite: false,
});
const symPlane = new THREE.Mesh(new THREE.PlaneGeometry(6, 6), planeMat);
symPlane.visible = false;
scene.add(symPlane);

// Navigation gizmo (Blender-style, top-right).
const gizmoCanvas = document.getElementById("gizmo-canvas");
const gizmoRenderer = new THREE.WebGLRenderer({ canvas: gizmoCanvas, alpha: true, antialias: true });
gizmoRenderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
gizmoRenderer.setSize(110, 110, false);
gizmoRenderer.setClearColor(0x000000, 0);
const viewHelper = new ViewHelper(camera, gizmoCanvas);
const clock = new THREE.Clock();
document.getElementById("gizmo").addEventListener("pointerup", (e) => {
  e.stopPropagation();
  viewHelper.handleClick(e);
});

// ---------------------------------------------------------------------------
// Raycasting + mirror
// ---------------------------------------------------------------------------
const raycaster = new THREE.Raycaster();
const mirrorRay = new THREE.Raycaster();
const pointer = new THREE.Vector2();

function updatePointer(e) {
  const rect = canvas.getBoundingClientRect();
  pointer.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
  pointer.y = -((e.clientY - rect.top) / rect.height) * 2 + 1;
}

function firstUVHit(rc) {
  if (surfaces.size === 0) return null;
  const hits = rc.intersectObjects([...surfaces.keys()], false);
  for (const h of hits) {
    if (!h.uv) continue;
    return { surface: surfaces.get(h.object), uv: h.uv };
  }
  return null;
}

/**
 * Gather the paint targets under the cursor. The primary hit is a normal
 * raycast; the mirror hit reflects the *entire* ray across the model's centre
 * plane, so on a symmetric model it lands on the opposite side regardless of
 * how the UVs are laid out.
 */
function gatherHits() {
  raycaster.setFromCamera(pointer, camera);
  const primary = firstUVHit(raycaster);
  const out = [];
  if (primary) out.push({ ...primary, sub: 0 });
  if (mirror.on && primary) {
    const o = raycaster.ray.origin.clone();
    const d = raycaster.ray.direction.clone();
    o.setComponent(mirror.axis, -o.getComponent(mirror.axis));
    d.setComponent(mirror.axis, -d.getComponent(mirror.axis));
    mirrorRay.set(o, d.normalize());
    const m = firstUVHit(mirrorRay);
    if (m) out.push({ ...m, sub: 1 });
  }
  return { primary, hits: out };
}

// ---------------------------------------------------------------------------
// Undo / redo (raw active-layer pixels captured per stroke)
// ---------------------------------------------------------------------------
const history = [];
const redoStack = [];
const HISTORY_MAX = 12;
let pending = null;   // Map(surface -> {snap}) for the in-progress stroke

function beginUndoGroup() { pending = new Map(); }
function recordSurface(surface) {
  if (!pending || pending.has(surface)) return;
  pending.set(surface, { surface, snap: surface.snapshotLayer(surface.activeId) });
}
function commitUndoGroup() {
  if (!pending || pending.size === 0) { pending = null; return; }
  history.push([...pending.values()]);
  if (history.length > HISTORY_MAX) history.shift();
  redoStack.length = 0;
  pending = null;
  refreshHistoryButtons();
}
function undo() {
  const group = history.pop();
  if (!group) return;
  const after = group.map((e) => ({ surface: e.surface, snap: e.surface.snapshotLayer(e.snap.id) }));
  group.forEach((e) => e.surface.restoreLayer(e.snap));
  redoStack.push(after);
  queuePreview();
  refreshHistoryButtons();
}
function redo() {
  const group = redoStack.pop();
  if (!group) return;
  const before = group.map((e) => ({ surface: e.surface, snap: e.surface.snapshotLayer(e.snap.id) }));
  group.forEach((e) => e.surface.restoreLayer(e.snap));
  history.push(before);
  queuePreview();
  refreshHistoryButtons();
}
function refreshHistoryButtons() {
  document.getElementById("undo-btn").disabled = history.length === 0;
  document.getElementById("redo-btn").disabled = redoStack.length === 0;
}

// ---------------------------------------------------------------------------
// Pointer interaction
// ---------------------------------------------------------------------------
canvas.addEventListener("pointerdown", (e) => {
  if (e.button !== 0) { orbiting = true; return; }
  updatePointer(e);
  const { primary, hits } = gatherHits();
  if (!primary) return;
  selectSurface(primary.surface);

  if (brush.tool === "eyedropper") {
    setBrushColor(primary.surface.sampleColor(primary.uv));
    toast(`Picked ${rgbToHex(brush.color)}`);
    return;
  }
  if (brush.tool === "fill") {
    beginUndoGroup();
    recordSurface(primary.surface);
    primary.surface.floodFill(primary.uv, brush);
    commitUndoGroup();
    queuePreview();
    return;
  }

  isPainting = true;
  canvas.setPointerCapture(e.pointerId);
  strokeSurfaces = new Set();
  beginUndoGroup();
  for (const h of hits) startStrokeOn(h.surface);
  for (const h of hits) h.surface.strokeTo(h.uv, brush, h.sub);
  lastHits = hits;
  if (brush.tool === "airbrush") startAirbrush();
  queuePreview();
});

canvas.addEventListener("pointermove", (e) => {
  updatePointer(e);
  moveBrushCursor(e);
  if (!isPainting) return;
  const { hits } = gatherHits();
  for (const h of hits) {
    startStrokeOn(h.surface);
    h.surface.strokeTo(h.uv, brush, h.sub);
  }
  lastHits = hits;
  queuePreview();
});

function startStrokeOn(surface) {
  if (strokeSurfaces.has(surface)) return;
  recordSurface(surface);
  surface.beginStroke();
  strokeSurfaces.add(surface);
}

function endStroke() {
  stopAirbrush();
  if (isPainting) {
    for (const s of strokeSurfaces) s.endStroke(brush);
    commitUndoGroup();
    queuePreview();
  }
  isPainting = false;
  orbiting = false;
}
canvas.addEventListener("pointerup", endStroke);
canvas.addEventListener("pointercancel", endStroke);

// Airbrush keeps depositing paint while the button is held, even if still.
function startAirbrush() {
  stopAirbrush();
  airTimer = setInterval(() => {
    if (!isPainting || !lastHits.length) return;
    for (const h of lastHits) h.surface.strokeTo(h.uv, brush, h.sub);
    queuePreview();
  }, 40);
}
function stopAirbrush() { if (airTimer) { clearInterval(airTimer); airTimer = null; } }

// Brush cursor ring
const cursorEl = document.getElementById("brush-cursor");
canvas.addEventListener("pointerenter", () => { if (!orbiting) cursorEl.style.display = "block"; });
canvas.addEventListener("pointerleave", () => { cursorEl.style.display = "none"; });
function moveBrushCursor(e) {
  const d = THREE.MathUtils.clamp(brush.size * 0.5, 8, 320);
  cursorEl.style.width = cursorEl.style.height = d + "px";
  const r = stage.getBoundingClientRect();
  cursorEl.style.left = e.clientX - r.left + "px";
  cursorEl.style.top = e.clientY - r.top + "px";
  cursorEl.style.borderColor =
    brush.tool === "erase" ? "rgba(255,120,120,.9)" :
    brush.tool === "eyedropper" ? "rgba(120,200,255,.95)" :
    brush.tool === "smudge" || brush.tool === "blur" ? "rgba(180,150,255,.95)" :
    "rgba(255,255,255,.9)";
}

// ---------------------------------------------------------------------------
// Model management
// ---------------------------------------------------------------------------
function clearModel() {
  if (!modelRoot) return;
  for (const s of surfaces.values()) s.dispose();
  surfaces.clear();
  scene.remove(modelRoot);
  modelRoot.traverse((o) => { if (o.geometry) o.geometry.dispose(); });
  modelRoot = null;
  currentSurface = null;
  history.length = 0; redoStack.length = 0; refreshHistoryButtons();
}

function installModel(root) {
  clearModel();
  modelRoot = root;

  const box = new THREE.Box3().setFromObject(root);
  const size = box.getSize(new THREE.Vector3());
  const center = box.getCenter(new THREE.Vector3());
  const maxDim = Math.max(size.x, size.y, size.z) || 1;
  const scale = 2.2 / maxDim;
  root.scale.setScalar(scale);
  root.position.sub(center.multiplyScalar(scale));
  scene.add(root);

  let paintable = 0, skipped = 0;
  root.traverse((o) => {
    if (!o.isMesh) return;
    if (!o.geometry.attributes.uv) { skipped++; return; }
    const s = new PaintSurface(o, textureRes, baseColor);
    surfaces.set(o, s);
    if (!currentSurface) currentSurface = s;
    paintable++;
  });

  frameCamera();
  updateSymPlane();
  rebuildSurfaceSelect();
  rebuildLayers();
  queuePreview();

  if (paintable === 0) toast("⚠ Model has no UV coordinates — cannot paint on it.", "err");
  else if (skipped > 0) toast(`Loaded · ${paintable} paintable mesh(es), ${skipped} without UVs.`, "ok");
  else toast(`Loaded · ${paintable} paintable mesh(es).`, "ok");
}

function frameCamera() {
  const box = new THREE.Box3().setFromObject(modelRoot);
  const sphere = box.getBoundingSphere(new THREE.Sphere());
  const r = sphere.radius || 1.5;
  const dist = r / Math.sin((camera.fov * Math.PI) / 180 / 2);
  camera.position.set(sphere.center.x + dist * 0.5, sphere.center.y + dist * 0.4, sphere.center.z + dist);
  controls.target.copy(sphere.center);
  camera.near = r / 100; camera.far = r * 100;
  camera.updateProjectionMatrix();
  controls.update();
}

function updateSymPlane() {
  symPlane.visible = mirror.on;
  if (!mirror.on) return;
  symPlane.position.set(0, 0, 0);
  symPlane.rotation.set(0, 0, 0);
  if (mirror.axis === 0) symPlane.rotation.y = Math.PI / 2;       // normal = X
  else if (mirror.axis === 1) symPlane.rotation.x = Math.PI / 2;  // normal = Y
  // axis 2 (normal = Z): plane already faces Z
}

function makePrimitive(kind) {
  let geo;
  switch (kind) {
    case "sphere":    geo = new THREE.SphereGeometry(1, 64, 48); break;
    case "box":       geo = new THREE.BoxGeometry(1.5, 1.5, 1.5, 4, 4, 4); break;
    case "cylinder":  geo = new THREE.CylinderGeometry(0.8, 0.8, 2, 64, 8); break;
    case "torusknot": geo = new THREE.TorusKnotGeometry(0.8, 0.28, 200, 32); break;
    case "plane":     geo = new THREE.PlaneGeometry(2, 2, 8, 8); break;
    default:          geo = new THREE.SphereGeometry(1, 64, 48);
  }
  const mesh = new THREE.Mesh(geo, new THREE.MeshStandardMaterial());
  mesh.name = kind[0].toUpperCase() + kind.slice(1);
  const group = new THREE.Group();
  group.add(mesh);
  installModel(group);
}

// --- File loading (single/multi-file glTF, plus obj/fbx/ply/stl) ------------
const objLoader = new OBJLoader();

function loadFiles(fileList) {
  const files = [...fileList];
  if (!files.length) return;
  const isModel = (n) => /\.(glb|gltf|vrm|fbx|obj|ply|stl)$/i.test(n);
  const main = files.find((f) => isModel(f.name));
  if (!main) { toast("Unsupported file. Use .glb/.gltf/.fbx/.obj/.ply/.stl", "err"); return; }

  showLoading(true);
  const blobs = new Map();
  for (const f of files) blobs.set(f.name, URL.createObjectURL(f));
  const cleanup = () => { for (const u of blobs.values()) URL.revokeObjectURL(u); };

  const manager = new THREE.LoadingManager();
  manager.setURLModifier((url) => {
    if (url.startsWith("blob:") || url.startsWith("data:")) return url;
    const base = decodeURIComponent(url.split(/[?#]/)[0].split("/").pop());
    return blobs.get(base) || url;
  });

  const name = main.name.toLowerCase();
  const url = blobs.get(main.name);
  const done = (root) => { installModel(root); showLoading(false); cleanup(); };
  const fail = (err) => { console.error(err); toast("✕ Failed to load model.", "err"); showLoading(false); cleanup(); };
  const wrapGeom = (geom) => { const m = new THREE.Mesh(geom, new THREE.MeshStandardMaterial()); m.name = main.name; const g = new THREE.Group(); g.add(m); return g; };

  if (name.endsWith(".glb") || name.endsWith(".gltf") || name.endsWith(".vrm")) {
    new GLTFLoader(manager).load(url, (g) => done(g.scene), undefined, fail);
  } else if (name.endsWith(".fbx")) {
    new FBXLoader(manager).load(url, (obj) => done(obj), undefined, fail);
  } else if (name.endsWith(".obj")) {
    main.text().then((t) => { try { done(objLoader.parse(t)); } catch (err) { fail(err); } }, fail);
  } else if (name.endsWith(".ply")) {
    main.arrayBuffer().then((b) => { const g = new PLYLoader().parse(b); g.computeVertexNormals(); done(wrapGeom(g)); }, fail);
  } else if (name.endsWith(".stl")) {
    main.arrayBuffer().then((b) => { const g = new STLLoader().parse(b); g.computeVertexNormals(); done(wrapGeom(g)); }, fail);
  }
}

// ---------------------------------------------------------------------------
// 2D texture-map preview (of the current surface's composite)
// ---------------------------------------------------------------------------
const uvCanvas = document.getElementById("uv-canvas");
const uvCtx = uvCanvas.getContext("2d");
let previewDirty = false;
function queuePreview() { previewDirty = true; }
function drawPreview() {
  if (!previewDirty) return;
  previewDirty = false;
  uvCtx.clearRect(0, 0, uvCanvas.width, uvCanvas.height);
  if (currentSurface) uvCtx.drawImage(currentSurface.composite, 0, 0, uvCanvas.width, uvCanvas.height);
}

// ---------------------------------------------------------------------------
// Render loop
// ---------------------------------------------------------------------------
function resize() {
  const w = stage.clientWidth, h = stage.clientHeight;
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
}
window.addEventListener("resize", resize);

function tick() {
  const delta = clock.getDelta();
  if (viewHelper.animating) viewHelper.update(delta);
  controls.enabled = !viewHelper.animating && !isPainting;
  controls.update();
  drawPreview();
  renderer.render(scene, camera);
  viewHelper.center.copy(controls.target);
  viewHelper.render(gizmoRenderer);
  requestAnimationFrame(tick);
}

// ---------------------------------------------------------------------------
// Surface + layer panels
// ---------------------------------------------------------------------------
function selectSurface(s) {
  if (currentSurface === s) return;
  currentSurface = s;
  const sel = document.getElementById("surface-select");
  if (sel) sel.value = surfaceId(s);
  rebuildLayers();
  queuePreview();
}
function surfaceId(s) { return [...surfaces.values()].indexOf(s); }

function rebuildSurfaceSelect() {
  const wrap = document.getElementById("surface-row");
  const sel = document.getElementById("surface-select");
  const list = [...surfaces.values()];
  wrap.classList.toggle("hidden", list.length <= 1);
  sel.innerHTML = "";
  list.forEach((s, i) => {
    const o = document.createElement("option");
    o.value = i; o.textContent = s.name || `Mesh ${i + 1}`;
    sel.appendChild(o);
  });
  if (currentSurface) sel.value = surfaceId(currentSurface);
}

function rebuildLayers() {
  const host = document.getElementById("layers");
  host.innerHTML = "";
  if (!currentSurface) return;
  // Render top-to-bottom (top layer first, like every art app).
  const list = currentSurface.layerList();
  for (let i = list.length - 1; i >= 0; i--) {
    const l = list[i];
    const row = document.createElement("div");
    row.className = "layer" + (l.id === currentSurface.activeId ? " active" : "");

    const eye = document.createElement("button");
    eye.className = "lyr-eye" + (l.visible ? "" : " off");
    eye.title = "Toggle visibility";
    eye.textContent = l.visible ? "◉" : "○";
    eye.addEventListener("click", (e) => { e.stopPropagation(); currentSurface.setVisible(l.id, !l.visible); rebuildLayers(); queuePreview(); });

    const main = document.createElement("div");
    main.className = "lyr-main";
    const nameEl = document.createElement("div");
    nameEl.className = "lyr-name";
    nameEl.textContent = l.name;
    nameEl.title = "Double-click to rename";
    nameEl.addEventListener("dblclick", (e) => {
      e.stopPropagation();
      const n = prompt("Layer name:", l.name);
      if (n) { currentSurface.rename(l.id, n); rebuildLayers(); }
    });

    const ctrl = document.createElement("div");
    ctrl.className = "lyr-ctrl";
    const blend = document.createElement("select");
    blend.className = "lyr-blend";
    BLEND_MODES.forEach((b) => {
      const o = document.createElement("option");
      o.value = b.id; o.textContent = b.label;
      if (b.id === l.blend) o.selected = true;
      blend.appendChild(o);
    });
    blend.addEventListener("click", (e) => e.stopPropagation());
    blend.addEventListener("change", (e) => { currentSurface.setBlend(l.id, e.target.value); queuePreview(); });

    const op = document.createElement("input");
    op.type = "range"; op.min = 0; op.max = 100; op.step = 1;
    op.value = Math.round(l.opacity * 100);
    op.className = "lyr-op";
    op.title = "Layer opacity";
    op.addEventListener("click", (e) => e.stopPropagation());
    op.addEventListener("input", (e) => { currentSurface.setOpacity(l.id, e.target.value / 100); queuePreview(); });

    ctrl.appendChild(blend); ctrl.appendChild(op);
    main.appendChild(nameEl); main.appendChild(ctrl);
    row.appendChild(eye); row.appendChild(main);
    row.addEventListener("click", () => { currentSurface.setActive(l.id); rebuildLayers(); });
    host.appendChild(row);
  }
}

// ---------------------------------------------------------------------------
// UI wiring
// ---------------------------------------------------------------------------
function hexToRgb(hex) { const n = parseInt(hex.slice(1), 16); return [(n >> 16) & 255, (n >> 8) & 255, n & 255]; }
function rgbToHex([r, g, b]) { return "#" + [r, g, b].map((v) => v.toString(16).padStart(2, "0")).join(""); }
function setBrushColor(rgb) { brush.color = rgb; document.getElementById("brush-color").value = rgbToHex(rgb); }

function setTool(tool) {
  brush.tool = tool;
  document.querySelectorAll(".tool-grid .tool").forEach((t) => t.classList.toggle("active", t.dataset.tool === tool));
  // Flow only meaningfully applies to the deposit brushes.
  const flowRow = document.getElementById("flow-row");
  flowRow.classList.toggle("hidden", !["paint", "airbrush", "erase"].includes(tool));
}

function bindRange(id, valId, apply, fmt) {
  const el = document.getElementById(id);
  const label = document.getElementById(valId);
  const update = () => { const v = parseFloat(el.value); apply(v); if (label) label.textContent = fmt(v); };
  el.addEventListener("input", update);
  update();
}

function adjustSize(delta) {
  const el = document.getElementById("brush-size");
  el.value = THREE.MathUtils.clamp(parseInt(el.value, 10) + delta, +el.min, +el.max);
  el.dispatchEvent(new Event("input"));
}

function setupUI() {
  document.querySelectorAll("[data-primitive]").forEach((b) =>
    b.addEventListener("click", () => makePrimitive(b.dataset.primitive)));

  document.getElementById("file-input").addEventListener("change", (e) => {
    if (e.target.files.length) loadFiles(e.target.files);
    e.target.value = "";
  });

  document.getElementById("res-select").addEventListener("change", (e) => {
    textureRes = parseInt(e.target.value, 10);
    toast(`New models will use ${textureRes}×${textureRes} textures.`);
  });

  document.querySelectorAll(".tool-grid .tool").forEach((t) => t.addEventListener("click", () => setTool(t.dataset.tool)));

  document.getElementById("brush-color").addEventListener("input", (e) => { brush.color = hexToRgb(e.target.value); });
  bindRange("brush-size", "size-val", (v) => { brush.size = v; }, (v) => `${v}`);
  bindRange("brush-opacity", "opacity-val", (v) => { brush.opacity = v / 100; }, (v) => `${v}%`);
  bindRange("brush-hardness", "hardness-val", (v) => { brush.hardness = v / 100; }, (v) => `${v}%`);
  bindRange("brush-flow", "flow-val", (v) => { brush.flow = v / 100; }, (v) => `${v}%`);
  bindRange("brush-spacing", "spacing-val", (v) => { brush.spacing = v / 100; }, (v) => `${v}%`);

  // Mirror
  document.getElementById("mirror-on").addEventListener("change", (e) => { mirror.on = e.target.checked; updateSymPlane(); });
  document.getElementById("mirror-axis").addEventListener("change", (e) => { mirror.axis = { x: 0, y: 1, z: 2 }[e.target.value]; updateSymPlane(); });

  // Base color
  const baseInput = document.getElementById("base-color");
  baseInput.addEventListener("input", (e) => {
    baseColor = e.target.value;
    for (const s of surfaces.values()) s.baseColor = baseColor;
  });

  // Surface select
  document.getElementById("surface-select").addEventListener("change", (e) => {
    const s = [...surfaces.values()][+e.target.value];
    if (s) { currentSurface = s; rebuildLayers(); queuePreview(); }
  });

  // Layer buttons
  document.getElementById("layer-add").addEventListener("click", () => { if (currentSurface) { currentSurface.addLayer(); rebuildLayers(); queuePreview(); } });
  document.getElementById("layer-dup").addEventListener("click", () => { if (currentSurface) { currentSurface.duplicateLayer(currentSurface.activeId); rebuildLayers(); queuePreview(); } });
  document.getElementById("layer-del").addEventListener("click", () => { if (currentSurface && !currentSurface.deleteLayer(currentSurface.activeId)) toast("Can't delete the last layer.", "err"); rebuildLayers(); queuePreview(); });
  document.getElementById("layer-up").addEventListener("click", () => { if (currentSurface) { currentSurface.moveLayer(currentSurface.activeId, 1); rebuildLayers(); queuePreview(); } });
  document.getElementById("layer-down").addEventListener("click", () => { if (currentSurface) { currentSurface.moveLayer(currentSurface.activeId, -1); rebuildLayers(); queuePreview(); } });
  document.getElementById("layer-merge").addEventListener("click", () => { if (currentSurface) { currentSurface.mergeDown(currentSurface.activeId); rebuildLayers(); queuePreview(); } });

  // Canvas ops (operate on the active layer of the current surface)
  document.getElementById("fill-btn").addEventListener("click", () => {
    if (!currentSurface) return;
    beginUndoGroup(); recordSurface(currentSurface);
    currentSurface.fillLayer(currentSurface.activeId, brush.color);
    commitUndoGroup(); queuePreview();
  });
  document.getElementById("clear-btn").addEventListener("click", () => {
    if (!currentSurface) return;
    beginUndoGroup(); recordSurface(currentSurface);
    currentSurface.clearLayer(currentSurface.activeId);
    commitUndoGroup(); queuePreview();
  });
  document.getElementById("reset-btn").addEventListener("click", () => {
    if (!confirm("Reset every layer on all meshes back to a blank base?")) return;
    for (const s of surfaces.values()) s.reset();
    history.length = 0; redoStack.length = 0; refreshHistoryButtons();
    rebuildLayers(); queuePreview();
  });

  document.getElementById("undo-btn").addEventListener("click", undo);
  document.getElementById("redo-btn").addEventListener("click", redo);

  document.getElementById("export-btn").addEventListener("click", exportTexture);
  document.getElementById("glb-btn").addEventListener("click", () => exportGLB(false));
  document.getElementById("save-btn").addEventListener("click", () => exportGLB(true));

  // Swatches
  const swatchColors = ["#e5484d", "#f5a524", "#f7e733", "#46a758", "#4c8bf5",
    "#8e4ec6", "#ffffff", "#111111", "#d8d3c8", "#6b4f34", "#c96f4a", "#2d3b52",
    "#e93d82", "#12a594", "#bdee63", "#8b8d98"];
  const swWrap = document.getElementById("swatches");
  swatchColors.forEach((c) => {
    const el = document.createElement("div");
    el.className = "sw"; el.style.background = c;
    el.addEventListener("click", () => { brush.color = hexToRgb(c); document.getElementById("brush-color").value = c; });
    swWrap.appendChild(el);
  });

  // UV preview collapse
  document.getElementById("uv-toggle").addEventListener("click", (e) => {
    const p = document.getElementById("uv-preview");
    p.classList.toggle("collapsed");
    e.target.textContent = p.classList.contains("collapsed") ? "▸" : "▾";
  });

  // Keyboard shortcuts
  window.addEventListener("keydown", (e) => {
    if (e.target.tagName === "INPUT" || e.target.tagName === "SELECT" || e.target.tagName === "TEXTAREA") return;
    if ((e.ctrlKey || e.metaKey) && (e.key === "z" || e.key === "Z")) { e.preventDefault(); e.shiftKey ? redo() : undo(); return; }
    if ((e.ctrlKey || e.metaKey) && (e.key === "y" || e.key === "Y")) { e.preventDefault(); redo(); return; }
    const map = { b: "paint", a: "airbrush", e: "erase", s: "smudge", u: "blur", g: "fill", i: "eyedropper", m: null };
    const k = e.key.toLowerCase();
    if (k === "m") { const c = document.getElementById("mirror-on"); c.checked = !c.checked; c.dispatchEvent(new Event("change")); }
    else if (k in map && map[k]) setTool(map[k]);
    if (e.key === "[") adjustSize(-8);
    if (e.key === "]") adjustSize(8);
  });

  // Project: "use current model" button loads the project's model.
  window.addEventListener("project:use-model", (e) => { if (e.detail && e.detail.file) loadFiles([e.detail.file]); });

  refreshHistoryButtons();
}

// ---------------------------------------------------------------------------
// Export
// ---------------------------------------------------------------------------
function exportTexture() {
  if (!currentSurface) { toast("Load a model first.", "err"); return; }
  const a = document.createElement("a");
  a.download = (currentSurface.name || "texture") + ".png";
  a.href = currentSurface.composite.toDataURL("image/png");
  a.click();
  toast("Exported texture PNG");
}

// Export the whole painted model as GLB. When `toProject` is set, save it as
// the active project's current model instead of downloading.
function exportGLB(toProject) {
  if (!modelRoot) { toast("Load a model first.", "err"); return; }
  showLoading(true);
  const exporter = new GLTFExporter();
  exporter.parse(modelRoot, (result) => {
    const blob = new Blob([result], { type: "model/gltf-binary" });
    const name = "painted.glb";
    if (toProject && window.Project) {
      Project.saveResult({ blob, name, tool: "Paint" }).finally(() => showLoading(false));
    } else {
      const a = document.createElement("a");
      a.download = name; a.href = URL.createObjectURL(blob); a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 4000);
      showLoading(false);
      toast("Exported painted.glb");
    }
  }, (err) => { console.error(err); toast("✕ GLB export failed.", "err"); showLoading(false); }, { binary: true });
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function toast(msg, type) { if (window.toast) window.toast(msg, type); }
function showLoading(on) { document.getElementById("loading").classList.toggle("hidden", !on); }

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------
setupUI();
setTool("paint");
resize();
makePrimitive("sphere");
tick();
