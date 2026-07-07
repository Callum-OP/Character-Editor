import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { OBJLoader } from "three/addons/loaders/OBJLoader.js";
import { FBXLoader } from "three/addons/loaders/FBXLoader.js";
import { PLYLoader } from "three/addons/loaders/PLYLoader.js";
import { STLLoader } from "three/addons/loaders/STLLoader.js";
import { GLTFExporter } from "three/addons/exporters/GLTFExporter.js";
import { ViewHelper } from "three/addons/helpers/ViewHelper.js";
import { sniffFormat, attachSmoothZoom } from "./viewer.js";

// --------------------------------------------------------------------------- //
// scene setup (mirrors paint.js' renderer/scene/lights/gizmo pattern)
// --------------------------------------------------------------------------- //
const canvas = document.getElementById("gl");
const stage = document.getElementById("stage");
const hint = document.getElementById("hint");
const loadingEl = document.getElementById("loading");

const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, preserveDrawingBuffer: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
renderer.outputColorSpace = THREE.SRGBColorSpace;

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x10131f);

const camera = new THREE.PerspectiveCamera(45, 1, 0.01, 1000);
camera.position.set(0, 1.2, 4);

const controls = new OrbitControls(camera, canvas);
controls.enableDamping = true;
controls.dampingFactor = 0.08;
// Bound the dolly so wheel-zoom can't drive the camera onto its target (distance
// 0 degenerates the view and blanks the canvas). Model frames to ~2.2 units.
controls.minDistance = 0.4;
controls.maxDistance = 20;
controls.enableZoom = false;                 // replaced by eased smooth zoom
const tickZoom = attachSmoothZoom(camera, controls, canvas);

scene.add(new THREE.HemisphereLight(0xffffff, 0x30343c, 1.0));
const keyLight = new THREE.DirectionalLight(0xffffff, 2.0);
keyLight.position.set(3, 5, 4); scene.add(keyLight);
const fillLight = new THREE.DirectionalLight(0xbcd0ff, 0.6);
fillLight.position.set(-4, 1, -3); scene.add(fillLight);

const grid = new THREE.GridHelper(20, 20, 0x2c3556, 0x1a2038);
grid.position.y = -1.001; scene.add(grid);

// pivot (turntable spin) wraps modelRoot (model fit transform) so exports,
// which read modelRoot, never bake in the spin orientation.
const pivot = new THREE.Group();
scene.add(pivot);
let modelRoot = null;

// nav gizmo
const gizmoCanvas = document.getElementById("gizmo-canvas");
const gizmoRenderer = new THREE.WebGLRenderer({ canvas: gizmoCanvas, alpha: true, antialias: true });
gizmoRenderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
gizmoRenderer.setSize(110, 110, false);
gizmoRenderer.setClearColor(0x000000, 0);
const viewHelper = new ViewHelper(camera, gizmoCanvas);
const clock = new THREE.Clock();
document.getElementById("gizmo").addEventListener("pointerup", (e) => { e.stopPropagation(); viewHelper.handleClick(e); });

function resize() {
  const w = stage.clientWidth, h = stage.clientHeight;
  renderer.setSize(w, h, false);
  camera.aspect = w / h || 1;
  camera.updateProjectionMatrix();
}
window.addEventListener("resize", resize);

function tick() {
  const delta = clock.getDelta();
  if (document.getElementById("spin").checked && modelRoot) pivot.rotation.y += delta * 0.6;
  if (viewHelper.animating) viewHelper.update(delta);
  controls.enabled = !viewHelper.animating;
  tickZoom();
  controls.update();
  renderer.render(scene, camera);
  viewHelper.center.copy(controls.target);
  viewHelper.render(gizmoRenderer);
  requestAnimationFrame(tick);
}
resize(); tick();

// --------------------------------------------------------------------------- //
// state + small helpers
// --------------------------------------------------------------------------- //
let styleTargets = [];   // [{ mesh, orig }]
let outlineMeshes = [];
let nativeRadius = 1;
let currentStyle = "toon";

const statusEl = document.getElementById("status");
const setStatus = (m, c) => { statusEl.textContent = m; statusEl.className = "status " + (c || ""); };
const showLoading = (on) => loadingEl.classList.toggle("hidden", !on);
const val = (id) => document.getElementById(id).value;
const checked = (id) => document.getElementById(id).checked;
const colorHex = (id) => new THREE.Color(val(id)).getHex();

// a white opaque 1x1 PNG, returned for texture URIs we can't resolve so the
// loader gets a valid image instead of 404-ing (white never darkens a mesh).
const BLANK_PX = (() => {
  const c = document.createElement("canvas"); c.width = c.height = 1;
  const x = c.getContext("2d"); x.fillStyle = "#ffffff"; x.fillRect(0, 0, 1, 1);
  return c.toDataURL("image/png");
})();

// --------------------------------------------------------------------------- //
// model loading (mirrors paint.js loadFiles: sniff bytes, multi-file glTF)
// --------------------------------------------------------------------------- //
async function loadFiles(fileList) {
  const files = Array.from(fileList || []);
  if (!files.length) return;
  showLoading(true); setStatus("Loading model…", "busy");
  try {
    const main = files.find((f) => /\.(glb|gltf|vrm|obj|fbx|ply|stl)$/i.test(f.name)) || files[0];
    const buf = await main.arrayBuffer();
    let fmt = sniffFormat(buf) || (main.name.split(".").pop() || "").toLowerCase();
    if (/\.vrm$/i.test(main.name)) fmt = "glb";

    const url = URL.createObjectURL(new Blob([buf]));
    const manager = new THREE.LoadingManager();
    const blobUrls = {};
    files.forEach((f) => { blobUrls[f.name.toLowerCase()] = URL.createObjectURL(f); });
    manager.setURLModifier((u) => {
      if (u === url) return u;                          // the model file itself
      const key = (u.split("/").pop() || "").toLowerCase();
      if (blobUrls[key]) return blobUrls[key];          // a sibling file the user uploaded
      // A self-contained upload can still reference *external* textures by URI
      // (common in VRM/glTF). We don't have those files, so hand back a blank
      // white pixel instead of letting each one 404 — the mesh just loads
      // untextured, which is fine for a stylized look.
      if (!/\.(bin|glb|gltf|vrm)$/.test(key)) return BLANK_PX;
      return u;
    });

    const root = await new Promise((resolve, reject) => {
      const fail = (e) => reject(e instanceof Error ? e : new Error("could not parse model"));
      if (fmt === "glb" || fmt === "gltf" || fmt === "vrm") {
        new GLTFLoader(manager).load(url, (g) => resolve(g.scene), undefined, fail);
      } else if (fmt === "fbx") {
        new FBXLoader(manager).load(url, resolve, undefined, fail);
      } else if (fmt === "obj") {
        const text = new TextDecoder().decode(buf);
        resolve(new OBJLoader().parse(text));
      } else if (fmt === "ply") {
        const g = new PLYLoader().parse(buf); g.computeVertexNormals();
        resolve(new THREE.Mesh(g, new THREE.MeshStandardMaterial()));
      } else if (fmt === "stl") {
        const g = new STLLoader().parse(buf); g.computeVertexNormals();
        resolve(new THREE.Mesh(g, new THREE.MeshStandardMaterial()));
      } else fail(new Error("unsupported format"));
    });

    Object.values(blobUrls).forEach(URL.revokeObjectURL);
    URL.revokeObjectURL(url);
    installModel(root);
    setStatus("Loaded. Pick a style.", "ok");
  } catch (err) {
    setStatus("Load failed: " + (err.message || err), "err");
  } finally { showLoading(false); }
}

function installModel(root) {
  if (modelRoot) { pivot.remove(modelRoot); }
  outlineMeshes = []; styleTargets = [];
  pivot.rotation.set(0, 0, 0);

  const group = new THREE.Group();
  group.add(root);
  modelRoot = group;
  pivot.add(group);

  // collect styleable meshes, remember their original material, and normalize.
  root.updateMatrixWorld(true);
  const meshes = [];
  root.traverse((o) => { if (o.isMesh && o.geometry) meshes.push(o); });
  meshes.forEach((o) => {
    const g = o.geometry;
    if (!g.attributes.normal) g.computeVertexNormals();
    // Drop blendshape (morph) data. It's unused for static styling, and a large
    // VRM morph set can exceed a GPU's vertex-shader limits — which surfaces as
    // "THREE.WebGLProgram: Vertex shader is not compiled" and a blank model.
    if (g.morphAttributes && Object.keys(g.morphAttributes).length) g.morphAttributes = {};
    g.morphTargetsRelative = false;
    o.morphTargetInfluences = undefined;
    o.morphTargetDictionary = undefined;
    // Skinned meshes carry a bounding volume that doesn't track their pose, so
    // Three.js culls them the instant the camera moves (model shows for one
    // frame, then vanishes). Disable per-object culling — there's only one model.
    o.frustumCulled = false;
    g.computeBoundingSphere();
    styleTargets.push({ mesh: o, orig: o.material, skinned: !!o.isSkinnedMesh });
  });

  // fit to a ~2.2-unit frame, sit on the grid
  const box = new THREE.Box3().setFromObject(group);
  const size = box.getSize(new THREE.Vector3());
  const center = box.getCenter(new THREE.Vector3());
  const maxDim = Math.max(size.x, size.y, size.z) || 1;
  const s = 2.2 / maxDim;
  group.scale.setScalar(s);
  group.position.set(-center.x * s, -box.min.y * s - 1.0, -center.z * s);

  // native outline unit: bounding radius in modelRoot-local space
  scene.updateMatrixWorld(true);
  const local = new THREE.Box3();
  const inv = new THREE.Matrix4().copy(modelRoot.matrixWorld).invert();
  styleTargets.forEach((t) => {
    t.mesh.geometry.computeBoundingBox();
    const b = t.mesh.geometry.boundingBox.clone();
    b.applyMatrix4(new THREE.Matrix4().copy(inv).multiply(t.mesh.matrixWorld));
    local.union(b);
  });
  nativeRadius = local.getSize(new THREE.Vector3()).length() / 2 || 1;

  controls.target.set(0, 0.1, 0);
  camera.position.set(0, 0.9, 3.4);
  hint.classList.add("hidden");
  applyStyle(currentStyle);
}

// --------------------------------------------------------------------------- //
// style materials
// --------------------------------------------------------------------------- //
function gradientTex(steps) {
  const data = new Uint8Array(steps);
  for (let i = 0; i < steps; i++) data[i] = Math.round((i / (steps - 1)) * 255);
  const tex = new THREE.DataTexture(data, steps, 1, THREE.RedFormat);
  tex.minFilter = tex.magFilter = THREE.NearestFilter;
  tex.needsUpdate = true;
  return tex;
}

const MATCAPS = {
  clay:  ["#f3ede4", "#b9ab97", "#5f5648"],
  metal: ["#ffffff", "#8ea0b8", "#141821"],
  skin:  ["#ffe9db", "#d99f86", "#7d4a3c"],
  pearl: ["#ffffff", "#cfd6f2", "#8b93c9"],
};
function matcapTex(kind) {
  const [hi, mid, lo] = MATCAPS[kind] || MATCAPS.clay;
  const s = 256, c = document.createElement("canvas"); c.width = c.height = s;
  const ctx = c.getContext("2d");
  ctx.fillStyle = lo; ctx.fillRect(0, 0, s, s);
  const g = ctx.createRadialGradient(s * 0.36, s * 0.30, s * 0.02, s * 0.5, s * 0.5, s * 0.62);
  g.addColorStop(0.0, hi);
  g.addColorStop(0.35, mid);
  g.addColorStop(1.0, lo);
  ctx.beginPath(); ctx.arc(s / 2, s / 2, s / 2, 0, Math.PI * 2);
  ctx.fillStyle = g; ctx.fill();
  const tex = new THREE.CanvasTexture(c);
  tex.colorSpace = THREE.SRGBColorSpace;
  return tex;
}

// A mesh's original material(s) as a flat list. A multi-material mesh (e.g. a
// shirt whose parts each have their own colour) carries a material array whose
// slots line up with the geometry's draw groups.
function origMaterialList(t) {
  return Array.isArray(t.orig) ? t.orig : [t.orig];
}

// True only if a texture actually has a decoded image to sample. A glTF/VRM
// texture whose source was external, undecodable, or 404'd still exists as a
// THREE.Texture object but has no image — sampling it returns black, which is
// what paints "random" parts (hands, etc.) black once textures are on.
function texHasImage(t) {
  const img = t && t.image;
  if (!img) return false;
  return !!(img.width || img.videoWidth || img.data || img.length ||
            (img.mipmaps && img.mipmaps.length));
}

// Resolve the "kept" look for one original material slot: its texture (only if
// the geometry has UVs *and* the texture has a real image), and its own base
// colour so an untextured-but-coloured part keeps that colour.
// Colours are copied in the renderer's linear working space (no hex round-trip),
// so they match the source exactly rather than drifting darker/lighter.
function keptLook(src, geom, base) {
  const hadMap = !!(src && src.map && src.map.isTexture);
  const uv = geom.attributes && geom.attributes.uv;
  const map = (hadMap && uv && texHasImage(src.map)) ? src.map : null;
  if (map) {
    // Sanitize fields three's shader assembler reads directly (channel -> MAP_UV,
    // matrix -> transform uniform), in case the loader left them undefined.
    if (typeof map.channel !== "number") map.channel = 0;
    if (!map.matrix) map.matrix = new THREE.Matrix3();
  }
  const color = new THREE.Color(base);
  if (map) {
    color.setHex(0xffffff);                     // let the texture show unmodified
  } else if (hadMap) {
    // Slot relied on a texture we couldn't decode, so its own colour factor is
    // usually a black placeholder — showing it would render the part black.
    // Fall back to the Base colour picker instead of a black blob.
  } else if (src && src.color && src.color.isColor) {
    color.copy(src.color);                       // genuinely untextured, coloured part
  }
  // Keep baked-in per-vertex colours if the mesh actually carries them.
  const vertexColors = !!(src && src.vertexColors && geom.attributes && geom.attributes.color);
  return { map, color, vertexColors };
}

// The per-slot look for a style: keep the model's own colour/texture when the
// "Keep …" toggle is on, otherwise a flat fill from the Base colour picker.
function lookForSlot(useMap, src, geom, base) {
  if (!useMap) return { map: null, color: new THREE.Color(base), vertexColors: false };
  return keptLook(src, geom, base);
}

function buildMaterial(style, t) {
  const base = colorHex("base-color");
  const useMap = checked("use-map");
  // toon / flat keep the model's own colour+texture per material slot, so a
  // multi-material mesh keeps each part's colour. Return an array matching the
  // geometry's draw groups (a lone slot stays a single material).
  if (style === "toon" || style === "flat") {
    const geom = t.mesh.geometry;
    const mats = origMaterialList(t).map((src) => {
      const look = lookForSlot(useMap, src, geom, base);
      if (style === "toon") {
        return new THREE.MeshToonMaterial({
          color: look.color, map: look.map, vertexColors: look.vertexColors,
          gradientMap: gradientTex(parseInt(val("tone-steps"), 10)),
        });
      }
      return new THREE.MeshStandardMaterial({
        color: look.color, map: look.map, vertexColors: look.vertexColors,
        roughness: 0.85, metalness: 0.0, flatShading: true,
      });
    });
    return mats.length === 1 ? mats[0] : mats;
  }
  switch (style) {
    case "matcap":
      return new THREE.MeshMatcapMaterial({ matcap: matcapTex(val("matcap-select")) });
    case "clay":
      return new THREE.MeshStandardMaterial({ color: base, roughness: 1.0, metalness: 0.0 });
    case "wireframe":
      return new THREE.MeshBasicMaterial({ color: base, wireframe: true });
    case "xray":
      return new THREE.MeshBasicMaterial({ color: base, transparent: true, opacity: 0.32, side: THREE.DoubleSide, depthWrite: false, blending: THREE.AdditiveBlending });
    case "normals":
      return new THREE.MeshNormalMaterial();
    default:
      return null;
  }
}

function applyStyle(style) {
  if (!styleTargets.length) { currentStyle = style; updateControls(); return; }
  currentStyle = style;
  styleTargets.forEach((t) => {
    try {
      if (style === "original") { t.mesh.material = t.orig; return; }
      const m = buildMaterial(style, t);
      if (m) t.mesh.material = m;
    } catch (e) { /* leave this mesh as-is if it can't take the material */ }
  });
  rebuildOutline();
  updateControls();
}

// --------------------------------------------------------------------------- //
// baked inverted-hull outline (real geometry -> survives GLB export)
// --------------------------------------------------------------------------- //
function outlineThicknessNative() {
  return parseFloat(val("outline-thickness")) * nativeRadius * 0.0012;
}
function bakedOutlineGeometry(geo, thickness) {
  const g = geo.clone();
  const pos = g.attributes.position;
  let nrm = g.attributes.normal;
  if (!nrm) { g.computeVertexNormals(); nrm = g.attributes.normal; }
  const p = pos.array, n = nrm.array;
  const out = new Float32Array(p.length);
  for (let i = 0; i < p.length; i += 3) {
    out[i] = p[i] + n[i] * thickness;
    out[i + 1] = p[i + 1] + n[i + 1] * thickness;
    out[i + 2] = p[i + 2] + n[i + 2] * thickness;
  }
  g.setAttribute("position", new THREE.BufferAttribute(out, 3));
  g.deleteAttribute("uv");
  return g;
}
function outlineEnabled() {
  return checked("outline-on") && currentStyle !== "original" && parseFloat(val("outline-thickness")) > 0;
}
function clearOutline() {
  outlineMeshes.forEach((m) => { m.parent && m.parent.remove(m); m.geometry.dispose(); m.material.dispose(); });
  outlineMeshes = [];
}
function rebuildOutline() {
  clearOutline();
  if (!outlineEnabled()) return;
  const th = outlineThicknessNative();
  const col = colorHex("outline-color");
  styleTargets.forEach((t) => {
    const om = new THREE.Mesh(
      bakedOutlineGeometry(t.mesh.geometry, th),
      new THREE.MeshBasicMaterial({ color: col, side: THREE.BackSide }),
    );
    om.frustumCulled = false;
    om.userData.isOutline = true;
    t.mesh.add(om);             // child -> inherits the mesh's transform exactly
    outlineMeshes.push(om);
  });
}

// --------------------------------------------------------------------------- //
// contextual control visibility
// --------------------------------------------------------------------------- //
const CTRL_STYLES = {
  base: ["toon", "flat", "clay", "wireframe", "xray"],
  usemap: ["toon", "flat"],
  tone: ["toon"],
  matcap: ["matcap"],
  "outline-grp": ["toon", "matcap", "flat", "clay", "wireframe", "xray", "normals"],
};
function updateControls() {
  document.querySelectorAll("[data-ctrl]").forEach((el) => {
    const which = el.getAttribute("data-ctrl");
    const styles = CTRL_STYLES[which];
    if (!styles) return;
    el.classList.toggle("hidden", !styles.includes(currentStyle));
  });
  document.querySelectorAll(".style").forEach((b) => b.classList.toggle("active", b.dataset.style === currentStyle));
}

// --------------------------------------------------------------------------- //
// export
// --------------------------------------------------------------------------- //

// Bake a mesh's geometry into static form. For a SkinnedMesh the visible shape
// comes from the skeleton deforming the geometry — the raw (bind) geometry can
// be collapsed near the origin, which is why an un-baked export shows "nothing"
// in Blender. Apply each vertex's bone transform so the export matches the view.
function bakedWorldGeometry(mesh) {
  const g = mesh.geometry.clone();
  if (mesh.isSkinnedMesh && mesh.skeleton) {
    mesh.skeleton.update();
    const pos = mesh.geometry.attributes.position;
    const out = g.attributes.position;
    const v = new THREE.Vector3();
    for (let i = 0; i < pos.count; i++) {
      v.fromBufferAttribute(pos, i);
      mesh.applyBoneTransform(i, v);   // bind space -> deformed local space
      out.setXYZ(i, v.x, v.y, v.z);
    }
    out.needsUpdate = true;
    g.computeVertexNormals();
  }
  g.deleteAttribute("skinIndex");
  g.deleteAttribute("skinWeight");
  return g;
}

// Reverse triangle winding so an inverted-hull outline reads correctly under
// glTF's back-face culling (glTF has no Three-style BackSide, so we flip the
// shell instead of relying on side=BackSide, which won't survive export).
function flipWinding(g) {
  if (g.index) {
    const a = g.index.array;
    for (let i = 0; i < a.length; i += 3) { const t = a[i + 1]; a[i + 1] = a[i + 2]; a[i + 2] = t; }
    g.index.needsUpdate = true;
  } else {
    for (const name in g.attributes) {
      const attr = g.attributes[name], arr = attr.array, is = attr.itemSize;
      for (let i = 0; i < attr.count; i += 3) {
        for (let c = 0; c < is; c++) {
          const a = (i + 1) * is + c, b = (i + 2) * is + c;
          const tmp = arr[a]; arr[a] = arr[b]; arr[b] = tmp;
        }
      }
      attr.needsUpdate = true;
    }
  }
  return g;
}

function buildExportRoot() {
  const root = new THREE.Group();
  root.name = "StyledModel";
  const base = colorHex("base-color");
  const useMap = checked("use-map");
  scene.updateMatrixWorld(true);
  const inv = new THREE.Matrix4().copy(modelRoot.matrixWorld).invert();
  const th = outlineThicknessNative();
  const outCol = colorHex("outline-color");
  const withOutline = outlineEnabled();

  styleTargets.forEach((t) => {
    const rel = new THREE.Matrix4().copy(inv).multiply(t.mesh.matrixWorld);
    const geo = bakedWorldGeometry(t.mesh);
    // portable unlit material per slot: flat cartoon fill (KHR_materials_unlit on
    // export), keeping each material's own colour/texture so multi-material
    // meshes export with their real colours rather than one flat fill.
    const mats = origMaterialList(t).map((src) => {
      const look = lookForSlot(useMap, src, geo, base);
      return new THREE.MeshBasicMaterial({ color: look.color, map: look.map, vertexColors: look.vertexColors });
    });
    const mesh = new THREE.Mesh(geo, mats.length === 1 ? mats[0] : mats);
    mesh.name = t.mesh.name || "mesh";
    mesh.applyMatrix4(rel);
    root.add(mesh);
    if (withOutline) {
      const om = new THREE.Mesh(
        flipWinding(bakedOutlineGeometry(geo, th)),
        new THREE.MeshBasicMaterial({ color: outCol }),   // single-sided; culling shows the rim
      );
      om.name = mesh.name + "_Outline";
      om.applyMatrix4(rel);
      root.add(om);
    }
  });
  return root;
}

function buildGLB() {
  const root = buildExportRoot();
  return new Promise((resolve, reject) => {
    new GLTFExporter().parse(root, (r) => resolve(new Blob([r], { type: "model/gltf-binary" })), reject, { binary: true });
  });
}

function downloadBlob(blob, name) {
  const a = document.createElement("a");
  a.download = name; a.href = URL.createObjectURL(blob); a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 4000);
}

function exportPNG() {
  if (!modelRoot) { setStatus("Load a model first.", "err"); return; }
  renderer.render(scene, camera);
  const a = document.createElement("a");
  a.download = "style_" + currentStyle + ".png";
  a.href = renderer.domElement.toDataURL("image/png");
  a.click();
  setStatus("Rendered PNG.", "ok");
}

async function exportGLB(saveToProject) {
  if (!modelRoot) { setStatus("Load a model first.", "err"); return; }
  showLoading(true); setStatus("Building GLB…", "busy");
  try {
    const blob = await buildGLB();
    const name = "styled_" + currentStyle + ".glb";
    if (saveToProject && window.Project) {
      await Project.saveResult({ blob, name, tool: "Style" });
      setStatus("Saved to project.", "ok");
    } else {
      downloadBlob(blob, name);
      setStatus("Downloaded " + name + ".", "ok");
    }
  } catch (err) {
    setStatus("Export failed: " + (err.message || err), "err");
  } finally { showLoading(false); }
}

// --------------------------------------------------------------------------- //
// UI wiring
// --------------------------------------------------------------------------- //
const fileInput = document.getElementById("file-input");
fileInput.addEventListener("change", () => {
  // Snapshot to a real array *before* clearing: fileInput.files is a live
  // FileList that value="" empties, which would leave loadFiles nothing to read.
  const fs = Array.from(fileInput.files);
  fileInput.value = "";     // allow re-selecting the same file to reload
  loadFiles(fs);
});
window.addEventListener("project:use-model", (e) => { if (e.detail && e.detail.file) loadFiles([e.detail.file]); });

document.querySelectorAll(".style").forEach((b) => b.addEventListener("click", () => applyStyle(b.dataset.style)));

document.getElementById("tone-steps").addEventListener("input", (e) => {
  document.getElementById("tone-val").textContent = e.target.value;
  if (currentStyle === "toon") applyStyle("toon");
});
document.getElementById("outline-thickness").addEventListener("input", (e) => {
  document.getElementById("outline-val").textContent = parseFloat(e.target.value).toFixed(1);
  rebuildOutline();
});
document.getElementById("outline-color").addEventListener("input", rebuildOutline);
document.getElementById("outline-on").addEventListener("change", rebuildOutline);
document.getElementById("base-color").addEventListener("input", () => applyStyle(currentStyle));
document.getElementById("use-map").addEventListener("change", () => applyStyle(currentStyle));
document.getElementById("matcap-select").addEventListener("change", () => { if (currentStyle === "matcap") applyStyle("matcap"); });
document.getElementById("bg-color").addEventListener("input", (e) => scene.background = new THREE.Color(e.target.value));
document.getElementById("show-grid").addEventListener("change", (e) => grid.visible = e.target.checked);

document.getElementById("png-btn").addEventListener("click", exportPNG);
document.getElementById("glb-btn").addEventListener("click", () => exportGLB(false));
document.getElementById("save-btn").addEventListener("click", () => exportGLB(true));

updateControls();

// Readiness signal for automated tests: set only after all listeners are wired,
// so a test won't set the file input before the change handler exists.
window.__styleReady = true;
