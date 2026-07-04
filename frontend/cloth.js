import * as THREE from "three";
import { ModelViewer, geometryFromFile } from "./viewer.js";
import { ClothSim, BodyCollider, buildGarment, buildGarmentFromGeometry, makeMannequin } from "./clothsim.js";
import { GLTFExporter } from "three/addons/exporters/GLTFExporter.js";
import { OBJExporter } from "three/addons/exporters/OBJExporter.js";

// Reuse the shared viewer purely as a render substrate (scene/camera/lights/its
// own render loop). We run a *separate* loop that steps the cloth solver and
// pushes new positions into the geometry; the viewer renders whatever it sees.
const viewer = new ModelViewer(document.getElementById("view"));
const { scene, camera, renderer, controls, canvas, raycaster } = viewer;
camera.position.set(0, 1.15, 3.6);
controls.target.set(0, 0.95, 0);

const $ = (id) => document.getElementById(id);
const status = (m, c) => { $("status").textContent = m || ""; $("status").className = "status " + (c || ""); };
const simStat = (m, c) => { $("simStat").textContent = m || ""; $("simStat").className = "status " + (c || ""); };
const pointer = new THREE.Vector2();

// ------------------------------------------------------------------- state
let bodyMesh = null;          // display mesh for the body
let bodyGeomWorld = null;     // body geometry in world coords (collider source)
let bodyBox = null;           // THREE.Box3 of the body in world coords
let bodyFit = null;           // {pivot,scale} transform fitBody applied (uploads)
let bodyIsMannequin = true;   // built-in body vs an uploaded reference body
let collider = null;
let garmentSource = "preset"; // "preset" | "custom"
let customGarmentGeom = null; // raw imported garment geometry (original coords)
let sim = null;               // ClothSim
let clothMesh = null;
let avgEdge = 0.05;           // mean structural edge length (brush radius unit)
const pinGroup = new THREE.Group(); scene.add(pinGroup);

let playing = false, settleHits = 0;
let pinTool = "add";          // 'add' | 'del' | null
const clock = new THREE.Clock();

if (new URLSearchParams(location.search).has("debug"))
  window.__cloth = { get sim() { return sim; }, get clothMesh() { return clothMesh; }, get body() { return bodyMesh; }, viewer };

const clothMat = new THREE.MeshStandardMaterial({
  color: new THREE.Color("#b5483f"), roughness: 0.72, metalness: 0.0,
  side: THREE.DoubleSide, flatShading: false,
});

// ------------------------------------------------------------------- body
function fitBody(geom) {
  // scale to ~1.7 tall, drop feet to y=0, centre on x/z. The pivot+scale are
  // stashed in bodyFit so a custom garment authored in the SAME scene can be
  // mapped through the identical transform and land exactly on the body.
  geom.computeBoundingBox();
  const bb = geom.boundingBox, size = new THREE.Vector3(), c = new THREE.Vector3();
  bb.getSize(size); bb.getCenter(c);
  const s = 1.7 / (size.y || 1);
  const pivot = new THREE.Vector3(c.x, bb.min.y, c.z);
  bodyFit = { pivot, scale: s };
  geom.translate(-pivot.x, -pivot.y, -pivot.z);   // feet at 0, centred x/z
  geom.scale(s, s, s);
  geom.computeVertexNormals(); geom.computeBoundingBox();
  return geom;
}

function setBody(geom, { isMannequin = false } = {}) {
  if (bodyMesh) { scene.remove(bodyMesh); bodyMesh.geometry.dispose(); }
  bodyIsMannequin = isMannequin;
  bodyGeomWorld = geom;
  bodyMesh = new THREE.Mesh(geom, new THREE.MeshStandardMaterial({
    color: isMannequin ? 0xcdc3b6 : 0xc9c2b8, roughness: 0.95, metalness: 0.0,
    side: THREE.DoubleSide,
  }));
  bodyMesh.visible = $("showBody").checked;
  scene.add(bodyMesh);
  bodyBox = new THREE.Box3().setFromBufferAttribute(geom.getAttribute("position"));
  const ctr = bodyBox.getCenter(new THREE.Vector3());
  controls.target.set(ctr.x, ctr.y, ctr.z);
  collider = new BodyCollider(geom, 0.08);
  syncGarmentSourceUI();
  rebuildGarment();
}

$("body").addEventListener("change", async () => {
  const f = $("body").files[0]; if (!f) return;
  $("body").value = "";                 // allow re-selecting the same file to reload
  status("Loading body…", "busy");
  try {
    const { surface } = await geometryFromFile(f);
    setBody(fitBody(surface), { isMannequin: false });
    status("Body loaded.", "ok");
  } catch (e) { status("Body failed: " + e.message, "err"); }
});
// carry a model in from the active project as the collision body
window.addEventListener("project:use-model", async (e) => {
  status("Loading body from project…", "busy");
  try {
    const { surface } = await geometryFromFile(e.detail.file);
    setBody(fitBody(surface), { isMannequin: false });
    status("Body loaded from project.", "ok");
  } catch (err) { status("Body failed: " + err.message, "err"); }
});
$("showBody").addEventListener("change", (e) => { if (bodyMesh) bodyMesh.visible = e.target.checked; });
$("floor").addEventListener("change", () => { if (sim) sim.setParam("floor", $("floor").checked ? bodyBox.min.y : null); });

// ------------------------------------------------------------------- garment
// Sync the panel to the current garment source (preset vs custom-mesh controls,
// and whether the "align to body's space" option is meaningful).
function syncGarmentSourceUI() {
  const custom = garmentSource === "custom";
  $("presetControls").classList.toggle("hidden", custom);
  $("customControls").classList.toggle("hidden", !custom);
  // Shared-space alignment only applies to an uploaded reference body.
  $("shareSpaceRow").classList.toggle("hidden", bodyIsMannequin);
  $("shareSpaceHint").classList.toggle("hidden", bodyIsMannequin);
  refreshProjectGarmentBtn();
}

function clearCloth() {
  if (clothMesh) { scene.remove(clothMesh); clothMesh.geometry.dispose(); clothMesh = null; }
  sim = null; pinGroup.clear();
}

// Place a raw imported garment into world space over the current body. With an
// uploaded body and "align" on, it's mapped through the body's own fit transform
// (exact, for garments authored in the same scene). Otherwise it's auto-fitted
// onto the body's upper half (scaled to the body width, top edge at the
// shoulders). A manual scale + height nudge is layered on either way.
function placeGarment(raw) {
  const g = raw.clone();
  const shared = !bodyIsMannequin && bodyFit && $("shareSpace").checked;
  if (shared) {
    g.translate(-bodyFit.pivot.x, -bodyFit.pivot.y, -bodyFit.pivot.z);
    g.scale(bodyFit.scale, bodyFit.scale, bodyFit.scale);
  } else {
    g.computeBoundingBox();
    const gb = g.boundingBox, gs = gb.getSize(new THREE.Vector3()), gc = gb.getCenter(new THREE.Vector3());
    const bSize = bodyBox.getSize(new THREE.Vector3()), bCtr = bodyBox.getCenter(new THREE.Vector3());
    // Fit so the garment spans ~34% of body height (typical upper-garment reach).
    const s = (bSize.y * 0.34) / (gs.y || 1);
    g.translate(-gc.x, -gb.max.y, -gc.z);        // origin at garment top-centre
    g.scale(s, s, s);
    const shoulderY = bodyBox.min.y + bSize.y * 0.82;
    g.translate(bCtr.x, shoulderY, bCtr.z);      // top edge to the shoulder line
  }
  const fs = +$("fitScale").value, off = +$("fitOffset").value;
  if (fs !== 1) {
    g.computeBoundingBox();
    const c = g.boundingBox.getCenter(new THREE.Vector3());
    g.translate(-c.x, -c.y, -c.z); g.scale(fs, fs, fs); g.translate(c.x, c.y, c.z);
  }
  if (off) g.translate(0, off, 0);
  g.computeBoundingBox();
  return g;
}

// Reshape a freshly-built preset garment to the size/position sliders, turning a
// template into custom clothing. Length scales the hem drop below the garment's
// top (pinned) band; Width scales girth radially about the body's vertical axis;
// Height raises/lowers the whole garment on the body. Each edge's rest length is
// scaled by that edge's own length change, so intentional slack (e.g. a gathered
// skirt's pleats) is preserved instead of being fought by the stretch solver.
function reshapeSpec(spec) {
  const L = +$("garmentLength").value, G = +$("garmentGirth").value, R = +$("garmentRise").value;
  if (L === 1 && G === 1 && R === 0) return;
  const p = spec.positions, n = spec.count, E = spec.ei.length;
  const old = new Float32Array(E);                       // per-edge length before
  for (let k = 0; k < E; k++) {
    const a = spec.ei[k], b = spec.ej[k];
    old[k] = Math.hypot(p[3 * a] - p[3 * b], p[3 * a + 1] - p[3 * b + 1], p[3 * a + 2] - p[3 * b + 2]);
  }
  let topY = -Infinity;
  for (let i = 0; i < n; i++) if (p[3 * i + 1] > topY) topY = p[3 * i + 1];
  const c = bodyBox.getCenter(new THREE.Vector3());
  const riseY = R * (bodyBox.max.y - bodyBox.min.y);
  for (let i = 0; i < n; i++) {
    const ix = 3 * i;
    p[ix] = c.x + (p[ix] - c.x) * G;                     // girth about the body axis
    p[ix + 2] = c.z + (p[ix + 2] - c.z) * G;
    p[ix + 1] = topY + (p[ix + 1] - topY) * L + riseY;   // hem drop + reposition
  }
  for (let k = 0; k < E; k++) {                          // rescale rest lengths to match
    if (old[k] < 1e-9) continue;
    const a = spec.ei[k], b = spec.ej[k];
    const nl = Math.hypot(p[3 * a] - p[3 * b], p[3 * a + 1] - p[3 * b + 1], p[3 * a + 2] - p[3 * b + 2]);
    spec.rest[k] *= nl / old[k];
  }
}

function rebuildGarment() {
  if (!bodyBox) return;
  playing = false; setPlay(false);

  let spec;
  if (garmentSource === "custom") {
    if (!customGarmentGeom) { clearCloth(); simStat("Load a garment mesh file to drape.", ""); hint(true); return; }
    try {
      spec = buildGarmentFromGeometry(placeGarment(customGarmentGeom), { pinTop: +$("pinTop").value });
    } catch (e) { clearCloth(); simStat("Garment mesh: " + e.message, "err"); return; }
  } else {
    spec = buildGarment($("garment").value, bodyBox, {
      fullness: +$("fullness").value, detail: +$("detail").value,
      bodyPositions: bodyGeomWorld.getAttribute("position").array,
    });
    reshapeSpec(spec);
  }

  sim = new ClothSim(spec, { mass: +$("mass").value });
  sim.setCollider(collider);

  // mean structural edge → brush radius unit
  let sum = 0, cnt = 0;
  for (let k = 0; k < spec.egroup.length; k++) if (spec.egroup[k] === 0) { sum += spec.rest[k]; cnt++; }
  avgEdge = cnt ? sum / cnt : 0.05;

  // cloth mesh shares the solver's position buffer (zero-copy updates)
  if (clothMesh) { scene.remove(clothMesh); clothMesh.geometry.dispose(); }
  const g = new THREE.BufferGeometry();
  g.setAttribute("position", new THREE.BufferAttribute(sim.pos, 3));
  // Always give the garment a UV set so the exported mesh can be painted. Preset
  // patterns carry their own grid UVs; a draped *custom* mesh may have none (e.g.
  // a retopo/rigged model with no UVs), so fall back to a cylindrical unwrap.
  const uv = spec.uv || cylindricalUV(sim.pos, spec.count);
  g.setAttribute("uv", new THREE.BufferAttribute(uv, 2));
  g.setIndex(new THREE.BufferAttribute(spec.indices, 1));
  g.computeVertexNormals();
  clothMesh = new THREE.Mesh(g, clothMat);
  clothMesh.frustumCulled = false;
  scene.add(clothMesh);

  applyFabric();
  if ($("floor").checked) sim.setParam("floor", bodyBox.min.y);
  refreshPins();
  const heavy = spec.count > 6000;
  simStat(spec.count + " particles" + (heavy ? " · high-res, sim may be slow" : "") + " · press Drape.",
    heavy ? "busy" : "");
  hint(true);
}
$("rebuild").addEventListener("click", rebuildGarment);
$("garment").addEventListener("change", rebuildGarment);
$("detail").addEventListener("change", rebuildGarment);
$("fullness").addEventListener("input", () => { $("fullnessVal").textContent = (+$("fullness").value).toFixed(2); });
$("fullness").addEventListener("change", rebuildGarment);
// size / position reshape sliders (preset garments -> custom clothing)
for (const id of ["garmentLength", "garmentGirth", "garmentRise"]) {
  $(id).addEventListener("input", () => { $(id + "Val").textContent = (+$(id).value).toFixed(2); });
  $(id).addEventListener("change", rebuildGarment);
}

// ---- custom garment mesh source
$("garmentSource").addEventListener("change", () => {
  garmentSource = $("garmentSource").value;
  syncGarmentSourceUI();
  rebuildGarment();
});
// Load a garment mesh (from a File) as the custom garment to drape.
async function loadGarmentFile(f) {
  const { surface } = await geometryFromFile(f);
  customGarmentGeom = surface;
  garmentSource = "custom"; $("garmentSource").value = "custom";
  syncGarmentSourceUI();
  rebuildGarment();
}
$("garmentFile").addEventListener("change", async () => {
  const f = $("garmentFile").files[0]; if (!f) return;
  $("garmentFile").value = "";          // allow re-selecting the same file to reload
  status("Loading garment…", "busy");
  try { await loadGarmentFile(f); status("Garment loaded — press Drape.", "ok"); }
  catch (e) { status("Garment failed: " + e.message, "err"); }
});

// Pull the active project's current model in AS THE GARMENT (not the body), so a
// character/garment carried through the pipeline can be draped & reshaped here.
function refreshProjectGarmentBtn() {
  const has = !!(window.Project && Project.current && Project.current());
  $("garmentFromProject").classList.toggle("hidden", !has);
}
$("garmentFromProject").addEventListener("click", async () => {
  if (!window.Project) return;
  status("Loading garment from project…", "busy");
  try {
    const f = await Project.getCurrentFile();
    if (!f) { status("No current model in this project yet.", "err"); return; }
    await loadGarmentFile(f);
    status("Garment loaded from project — press Drape.", "ok");
  } catch (e) { status("Garment failed: " + e.message, "err"); }
});
window.addEventListener("project:change", refreshProjectGarmentBtn);
setTimeout(refreshProjectGarmentBtn, 600);   // project manifest loads async on boot
$("shareSpace").addEventListener("change", rebuildGarment);
$("pinTop").addEventListener("input", () => $("pinTopVal").textContent = Math.round(+$("pinTop").value * 100) + "%");
$("pinTop").addEventListener("change", rebuildGarment);
$("fitScale").addEventListener("input", () => $("fitScaleVal").textContent = (+$("fitScale").value).toFixed(2));
$("fitScale").addEventListener("change", rebuildGarment);
$("fitOffset").addEventListener("input", () => $("fitOffsetVal").textContent = (+$("fitOffset").value).toFixed(2));
$("fitOffset").addEventListener("change", rebuildGarment);

// Fallback UV unwrap for a garment that arrived without any: wrap the texture
// around the body's vertical axis (u = angle about the centroid, v = height).
// Good enough to paint a draped tube/sheet; presets keep their own grid UVs.
function cylindricalUV(pos, count) {
  let minY = Infinity, maxY = -Infinity, cx = 0, cz = 0;
  for (let i = 0; i < count; i++) {
    const y = pos[3 * i + 1];
    if (y < minY) minY = y; if (y > maxY) maxY = y;
    cx += pos[3 * i]; cz += pos[3 * i + 2];
  }
  cx /= count || 1; cz /= count || 1;
  const h = (maxY - minY) || 1;
  const uv = new Float32Array(count * 2);
  for (let i = 0; i < count; i++) {
    const ang = Math.atan2(pos[3 * i + 2] - cz, pos[3 * i] - cx);   // -π..π
    uv[2 * i] = (ang + Math.PI) / (2 * Math.PI);                     // 0..1 around
    uv[2 * i + 1] = (pos[3 * i + 1] - minY) / h;                     // 0..1 up
  }
  return uv;
}

// ------------------------------------------------------------------- fabric
const PRESETS = {
  cotton:  { stretch: 0.95, bend: 0.32, mass: 0.30, friction: 0.55, cling: 0.30 },
  silk:    { stretch: 0.97, bend: 0.12, mass: 0.16, friction: 0.35, cling: 0.50 },
  chiffon: { stretch: 0.96, bend: 0.05, mass: 0.10, friction: 0.30, cling: 0.55 },
  denim:   { stretch: 0.99, bend: 0.70, mass: 0.60, friction: 0.65, cling: 0.12 },
  leather: { stretch: 0.99, bend: 0.85, mass: 0.85, friction: 0.70, cling: 0.10 },
  wool:    { stretch: 0.94, bend: 0.45, mass: 0.45, friction: 0.60, cling: 0.35 },
};
$("preset").addEventListener("change", () => {
  const p = PRESETS[$("preset").value]; if (!p) return;
  $("stretch").value = p.stretch; $("bend").value = p.bend;
  $("mass").value = p.mass; $("friction").value = p.friction;
  $("cling").value = p.cling;
  applyFabric();
});

function applyFabric() {
  $("stretchVal").textContent = (+$("stretch").value).toFixed(2);
  $("bendVal").textContent = (+$("bend").value).toFixed(2);
  $("massVal").textContent = (+$("mass").value).toFixed(2);
  $("frictionVal").textContent = (+$("friction").value).toFixed(2);
  $("clingVal").textContent = (+$("cling").value).toFixed(2);
  $("gapVal").textContent = (+$("gap").value).toFixed(3);
  $("slackVal").textContent = (+$("slack").value).toFixed(2);
  $("gravityVal").textContent = (+$("gravity").value).toFixed(1);
  $("windVal").textContent = (+$("wind").value).toFixed(1);
  clothMat.color.set($("color").value);
  clothMat.side = $("dbl").checked ? THREE.DoubleSide : THREE.FrontSide;
  if (!sim) return;
  sim.setStiffness({ stretch: +$("stretch").value, shear: 0.6, bend: +$("bend").value });
  sim.setMass(+$("mass").value);
  sim.setParam("friction", +$("friction").value);
  sim.setParam("cling", +$("cling").value);
  sim.setParam("thickness", +$("gap").value);
  sim.setParam("slack", +$("slack").value);
  sim.setParam("gravity", -(+$("gravity").value));
  sim.setParam("wind", +$("wind").value);
}
for (const id of ["stretch", "bend", "mass", "friction", "cling", "gap", "slack", "gravity", "wind"])
  $(id).addEventListener("input", applyFabric);
$("color").addEventListener("input", applyFabric);
$("dbl").addEventListener("change", applyFabric);

// ------------------------------------------------------------------- simulate
function setPlay(on) {
  playing = on; settleHits = 0;
  $("play").classList.toggle("active", on);
  $("play").textContent = on ? "❚❚ Pause" : "▶ Drape";
  if (on) { clock.getDelta(); hint(false); }
}
$("play").addEventListener("click", () => setPlay(!playing));
$("resetSim").addEventListener("click", () => {
  if (!sim) return; setPlay(false); sim.reset(); updateCloth(); refreshPins();
  simStat("Reset to flat pattern.", "");
});
$("step").addEventListener("click", () => {
  if (!sim) return; setPlay(false);
  for (let i = 0; i < 4; i++) sim.step(1 / 60);
  updateCloth(); simStat("Stepped · energy " + sim.energy().toExponential(1), "");
});

function updateCloth() {
  if (!clothMesh) return;
  clothMesh.geometry.attributes.position.needsUpdate = true;
  clothMesh.geometry.computeVertexNormals();
  clothMesh.geometry.computeBoundingSphere();
}

// the sim loop (separate from the viewer's render loop)
function tick() {
  requestAnimationFrame(tick);
  if (!playing || !sim) return;
  const dt = Math.min(clock.getDelta(), 1 / 30);
  sim.step(dt);
  updateCloth();
  const e = sim.energy();
  simStat("Draping · energy " + e.toExponential(1), "busy");
  if (e < 4e-5) { if (++settleHits > 30) { setPlay(false); simStat("Draped — settled. ✓", "ok"); } }
  else settleHits = 0;
}
tick();

// ------------------------------------------------------------------- pins
function refreshPins() {
  pinGroup.clear();
  if (!sim || !$("showPins").checked) return;
  const pts = [];
  for (let i = 0; i < sim.n; i++) if (sim.pinned[i]) pts.push(sim.pos[3 * i], sim.pos[3 * i + 1], sim.pos[3 * i + 2]);
  if (!pts.length) return;
  const g = new THREE.BufferGeometry();
  g.setAttribute("position", new THREE.BufferAttribute(new Float32Array(pts), 3));
  pinGroup.add(new THREE.Points(g, new THREE.PointsMaterial({
    color: 0xffd23f, size: Math.max(0.02, avgEdge * 0.9), sizeAttenuation: true, depthTest: false,
  })));
}
$("showPins").addEventListener("change", refreshPins);
$("brush").addEventListener("input", () => $("brushVal").textContent = $("brush").value);
$("unpinAll").addEventListener("click", () => {
  if (!sim) return;
  for (let i = 0; i < sim.n; i++) if (sim.pinned[i]) sim.setPinned(i, false);
  refreshPins();
});

function setPinTool(t) {
  pinTool = t;
  $("pinAdd").classList.toggle("active", t === "add");
  $("pinDel").classList.toggle("active", t === "del");
  $("pinOff").classList.toggle("active", t === null);
}
$("pinAdd").addEventListener("click", () => setPinTool("add"));
$("pinDel").addEventListener("click", () => setPinTool("del"));
$("pinOff").addEventListener("click", () => setPinTool(null));

function setRay(e) {
  const r = canvas.getBoundingClientRect();
  pointer.x = ((e.clientX - r.left) / r.width) * 2 - 1;
  pointer.y = -((e.clientY - r.top) / r.height) * 2 + 1;
  raycaster.setFromCamera(pointer, camera);
}
function pickCloth(e) {
  if (!clothMesh) return null;
  setRay(e);
  const hits = raycaster.intersectObject(clothMesh, false);
  return hits.length ? hits[0].point : null;
}
function paintPins(point) {
  if (!sim) return;
  const on = pinTool === "add";
  const radius = +$("brush").value * avgEdge;
  const cx = bodyBox ? bodyBox.getCenter(new THREE.Vector3()).x : 0;
  const within = (px, py, pz) => {
    for (let i = 0; i < sim.n; i++) {
      const dx = sim.pos[3 * i] - px, dy = sim.pos[3 * i + 1] - py, dz = sim.pos[3 * i + 2] - pz;
      if (dx * dx + dy * dy + dz * dz <= radius * radius) sim.setPinned(i, on);
    }
  };
  within(point.x, point.y, point.z);
  if ($("pinMirror").checked) within(2 * cx - point.x, point.y, point.z);
  refreshPins();
}

let painting = false;
canvas.addEventListener("pointerdown", (e) => {
  if (pinTool === null) return;          // orbit only
  const p = pickCloth(e);
  if (p) { painting = true; controls.enabled = false; paintPins(p); }
});
canvas.addEventListener("pointermove", (e) => {
  if (!painting) return;
  const p = pickCloth(e); if (p) paintPins(p);
});
window.addEventListener("pointerup", () => { if (painting) { painting = false; controls.enabled = true; } });

// ------------------------------------------------------------------- export
function triggerBlob(blob, name) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a"); a.href = url; a.download = name; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 4000);
}
function exportRoot() {
  const g = clothMesh.geometry.clone();
  g.computeVertexNormals();
  return new THREE.Group().add(new THREE.Mesh(g, clothMat));
}
$("export").addEventListener("click", async () => {
  if (!sim || !clothMesh) { status("Build a garment first.", "err"); return; }
  const fmt = $("outFormat").value;
  status("Exporting…", "busy");
  try {
    const root = exportRoot();
    const glb = await new Promise((res, rej) => new GLTFExporter().parse(root, res, rej, { binary: true }));
    const glbBlob = new Blob([glb], { type: "model/gltf-binary" });
    if (window.Project) Project.saveResult({ blob: glbBlob, name: "garment.glb", tool: "Cloth" });
    if (fmt === "glb") { triggerBlob(glbBlob, "garment.glb"); status("Saved garment.glb", "ok"); return; }
    if (fmt === "obj") {
      triggerBlob(new Blob([new OBJExporter().parse(root)], { type: "text/plain" }), "garment.obj");
      status("Saved garment.obj", "ok"); return;
    }
    status("Transcoding to " + fmt.toUpperCase() + " via Blender…", "busy");
    const fd = new FormData();
    fd.append("file", new File([glbBlob], "garment.glb")); fd.append("out_format", fmt);
    const resp = await fetch("/api/cloth/convert", { method: "POST", body: fd });
    if (!resp.ok) throw new Error((await resp.text()).slice(0, 200));
    const data = await resp.json();
    triggerBlob(await (await fetch(data.download_url)).blob(), data.download_name);
    status("Saved " + data.download_name, "ok");
  } catch (e) { status("Export failed: " + e.message, "err"); }
});

// ------------------------------------------------------------------- misc
$("shot").addEventListener("click", () => {
  renderer.render(scene, camera);
  const a = document.createElement("a");
  a.href = renderer.domElement.toDataURL("image/png"); a.download = "garment.png"; a.click();
});
function hint(show) { $("hint").classList.toggle("hidden", !show); }

async function checkEngine() {
  const el = $("engine");
  try {
    const e = await (await fetch("/api/engine")).json();
    if (e.name === "blender") { el.textContent = "Blender ✓ (FBX/PLY/STL export ready)"; el.className = "engine ok"; }
    else { el.innerHTML = "No Blender — GLB/OBJ export still works; FBX needs Blender."; el.className = "engine"; }
  } catch { el.textContent = "Backend offline — GLB/OBJ export still works."; el.className = "engine"; }
}

// boot with the built-in mannequin
setBody(makeMannequin(), { isMannequin: true });
setPinTool("add");
applyFabric();
checkEngine();
