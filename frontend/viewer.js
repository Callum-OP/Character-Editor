// Shared 3D viewer used by every tool page in the hub.
// Wraps Three.js scene setup, multi-model management, a true-quad OBJ parser,
// and multi-format file loading behind a small ModelViewer class.
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { FBXLoader } from "three/addons/loaders/FBXLoader.js";
import { PLYLoader } from "three/addons/loaders/PLYLoader.js";
import { STLLoader } from "three/addons/loaders/STLLoader.js";

// ---------------------------------------------------------- true-quad OBJ parse
// Polygon faces (quads / ngons) become real polygon edges, so quads render as
// quads rather than showing triangulation diagonals.
export function parseOBJ(text) {
  const verts = [];
  const triIdx = [];
  const edgeSet = new Set();
  const edgePts = [];
  const pushEdge = (a, b) => {
    const k = a < b ? a + "_" + b : b + "_" + a;
    if (edgeSet.has(k)) return;
    edgeSet.add(k);
    edgePts.push(a, b);
  };
  for (const line of text.split("\n")) {
    if (line[0] === "v" && line[1] === " ") {
      const p = line.split(/\s+/);
      verts.push(+p[1], +p[2], +p[3]);
    } else if (line[0] === "f" && line[1] === " ") {
      const toks = line.trim().split(/\s+/).slice(1);
      const idx = toks.map((t) => {
        let i = parseInt(t.split("/")[0], 10);
        if (i < 0) i = verts.length / 3 + i; else i -= 1;
        return i;
      });
      for (let i = 0; i < idx.length; i++) pushEdge(idx[i], idx[(i + 1) % idx.length]);
      for (let i = 1; i < idx.length - 1; i++) triIdx.push(idx[0], idx[i], idx[i + 1]);
    }
  }
  const pos = new Float32Array(verts);
  const surface = new THREE.BufferGeometry();
  surface.setAttribute("position", new THREE.BufferAttribute(pos, 3));
  surface.setIndex(triIdx);
  const edgePos = new Float32Array(edgePts.length * 3);
  for (let i = 0; i < edgePts.length; i++) {
    edgePos[i * 3] = pos[edgePts[i] * 3];
    edgePos[i * 3 + 1] = pos[edgePts[i] * 3 + 1];
    edgePos[i * 3 + 2] = pos[edgePts[i] * 3 + 2];
  }
  const edges = new THREE.BufferGeometry();
  edges.setAttribute("position", new THREE.BufferAttribute(edgePos, 3));
  return { surface, edges };
}

function geometryFromObject(obj) {
  const geoms = [];
  obj.traverse((c) => {
    if (c.isMesh && c.geometry) {
      const g = c.geometry.clone();
      g.applyMatrix4(c.matrixWorld);
      const pos = g.getAttribute("position");
      const ng = new THREE.BufferGeometry();
      ng.setAttribute("position", pos.clone());
      ng.setIndex(g.index ? g.index.clone() : [...Array(pos.count).keys()]);
      geoms.push(ng);
    }
  });
  if (!geoms.length) return null;
  let totalV = 0, totalI = 0;
  geoms.forEach((g) => { totalV += g.getAttribute("position").count; totalI += g.index.count; });
  const pos = new Float32Array(totalV * 3);
  const idx = new Uint32Array(totalI);
  let vo = 0, io = 0;
  geoms.forEach((g) => {
    pos.set(g.getAttribute("position").array, vo * 3);
    const gi = g.index.array;
    for (let i = 0; i < gi.length; i++) idx[io + i] = gi[i] + vo;
    vo += g.getAttribute("position").count;
    io += gi.length;
  });
  const out = new THREE.BufferGeometry();
  out.setAttribute("position", new THREE.BufferAttribute(pos, 3));
  out.setIndex(new THREE.BufferAttribute(idx, 1));
  return out;
}

// Parse a File of any supported format into { surface, edges } geometry.
// OBJ keeps real polygon edges; other formats get triangle wireframes.
export async function geometryFromFile(file) {
  const ext = file.name.split(".").pop().toLowerCase();
  if (ext === "obj") return parseOBJ(await file.text());
  const buf = await file.arrayBuffer();
  let object3d = null, geom = null;
  if (ext === "glb" || ext === "gltf") {
    const gltf = await new Promise((res, rej) => new GLTFLoader().parse(buf, "", res, rej));
    object3d = gltf.scene;
  } else if (ext === "fbx") {
    object3d = new FBXLoader().parse(buf, "");
  } else if (ext === "ply") {
    geom = new PLYLoader().parse(buf);
  } else if (ext === "stl") {
    geom = new STLLoader().parse(buf);
  } else {
    throw new Error("Unsupported format: " + ext);
  }
  if (!geom && object3d) { object3d.updateMatrixWorld(true); geom = geometryFromObject(object3d); }
  if (!geom) throw new Error("No mesh found in file.");
  return { surface: geom, edges: new THREE.WireframeGeometry(geom) };
}

export class ModelViewer {
  constructor(canvas, { background = 0x14161a } = {}) {
    this.canvas = canvas;
    this.renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
    this.renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color(background);
    this.camera = new THREE.PerspectiveCamera(45, 1, 0.01, 1000);
    this.camera.position.set(2.4, 1.8, 2.8);
    this.controls = new OrbitControls(this.camera, canvas);
    this.controls.enableDamping = true;

    this.scene.add(new THREE.HemisphereLight(0xffffff, 0x404050, 1.1));
    const key = new THREE.DirectionalLight(0xffffff, 1.4); key.position.set(3, 5, 4);
    this.scene.add(key);
    const rim = new THREE.DirectionalLight(0x88aaff, 0.6); rim.position.set(-4, 2, -3);
    this.scene.add(rim);
    const grid = new THREE.GridHelper(10, 20, 0x2e353f, 0x232830);
    grid.position.y = -1.001; this.scene.add(grid);

    this.wireMat = new THREE.LineBasicMaterial({ color: 0x4f9dff });
    this.models = {};        // key -> { group, surface, wire, markers }
    this.active = null;
    this.showWire = true;
    this.showShade = true;
    this.raycaster = new THREE.Raycaster();
    this.pointer = new THREE.Vector2();
    this.pickCallback = null;

    const loop = () => {
      this._resize();
      this.controls.update();
      this.renderer.render(this.scene, this.camera);
      requestAnimationFrame(loop);
    };
    loop();
  }

  _resize() {
    const w = this.canvas.clientWidth, h = this.canvas.clientHeight;
    if (this.canvas.width !== w || this.canvas.height !== h) {
      this.renderer.setSize(w, h, false);
      this.camera.aspect = w / h;
      this.camera.updateProjectionMatrix();
    }
  }

  _frame(group) {
    const box = new THREE.Box3().setFromObject(group);
    const size = new THREE.Vector3(), center = new THREE.Vector3();
    box.getSize(size); box.getCenter(center);
    const s = 2 / (Math.max(size.x, size.y, size.z) || 1);
    group.scale.setScalar(s);
    group.position.set(-center.x * s, -center.y * s, -center.z * s);
  }

  setModel(key, geom, { color = 0x9aa7b8, wireColor } = {}) {
    this.clear(key);
    const group = new THREE.Group();
    geom.surface.computeVertexNormals();
    const surfaceMat = new THREE.MeshStandardMaterial({
      color, roughness: 0.7, metalness: 0.05, side: THREE.DoubleSide,
    });
    const surface = new THREE.Mesh(geom.surface, surfaceMat);
    group.add(surface);
    let wire = null;
    if (geom.edges) {
      wire = new THREE.LineSegments(
        geom.edges, wireColor ? new THREE.LineBasicMaterial({ color: wireColor }) : this.wireMat);
      group.add(wire);
    }
    const markers = new THREE.Group();
    group.add(markers);
    this.scene.add(group);
    this._frame(group);
    const box = new THREE.Box3().setFromBufferAttribute(geom.surface.getAttribute("position"));
    const localSize = box.getSize(new THREE.Vector3()).length() || 1;
    this.models[key] = { group, surface, wire, markers, geom: geom.surface, localSize };
    if (!this.active) this.active = key;
    this._apply();
  }

  // ---- landmark picking ------------------------------------------------------
  enablePicking(callback) {
    this.pickCallback = callback;
    if (this._pickBound) return;
    this._pickBound = (ev) => this._onClick(ev);
    this.canvas.addEventListener("pointerdown", this._down = (e) => {
      this._downXY = [e.clientX, e.clientY];
    });
    this.canvas.addEventListener("pointerup", this._pickBound);
  }

  _onClick(ev) {
    if (!this.pickCallback || !this.active) return;
    // ignore drags (orbit) — only treat near-stationary clicks as picks
    if (this._downXY) {
      const dx = ev.clientX - this._downXY[0], dy = ev.clientY - this._downXY[1];
      if (dx * dx + dy * dy > 25) return;
    }
    const m = this.models[this.active];
    if (!m) return;
    const r = this.canvas.getBoundingClientRect();
    this.pointer.x = ((ev.clientX - r.left) / r.width) * 2 - 1;
    this.pointer.y = -((ev.clientY - r.top) / r.height) * 2 + 1;
    this.raycaster.setFromCamera(this.pointer, this.camera);
    const hits = this.raycaster.intersectObject(m.surface, false);
    if (!hits.length) return;
    const hit = hits[0];
    // nearest of the triangle's 3 vertices to the hit point -> vertex index
    const pos = m.geom.getAttribute("position");
    const lp = m.surface.worldToLocal(hit.point.clone());
    let best = -1, bestD = Infinity;
    for (const vi of [hit.face.a, hit.face.b, hit.face.c]) {
      const dx = pos.getX(vi) - lp.x, dy = pos.getY(vi) - lp.y, dz = pos.getZ(vi) - lp.z;
      const d = dx * dx + dy * dy + dz * dz;
      if (d < bestD) { bestD = d; best = vi; }
    }
    if (best >= 0) this.pickCallback({ key: this.active, index: best });
  }

  _labelSprite(text, color) {
    const c = document.createElement("canvas"); c.width = c.height = 64;
    const ctx = c.getContext("2d");
    ctx.fillStyle = "rgba(0,0,0,0.6)"; ctx.beginPath(); ctx.arc(32, 32, 30, 0, 7); ctx.fill();
    ctx.fillStyle = "#fff"; ctx.font = "bold 36px sans-serif";
    ctx.textAlign = "center"; ctx.textBaseline = "middle"; ctx.fillText(text, 32, 34);
    const tex = new THREE.CanvasTexture(c);
    const spr = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, depthTest: false }));
    return spr;
  }

  // render markers for a model from a list of vertex indices
  setMarkers(key, indices, color = 0xffffff) {
    const m = this.models[key];
    if (!m) return;
    m.markers.clear();
    const pos = m.geom.getAttribute("position");
    const r = m.localSize * 0.012;
    indices.forEach((vi, n) => {
      if (vi == null || vi < 0 || vi >= pos.count) return;
      const sph = new THREE.Mesh(
        new THREE.SphereGeometry(r, 16, 12),
        new THREE.MeshBasicMaterial({ color, depthTest: false }));
      sph.renderOrder = 999;
      sph.position.set(pos.getX(vi), pos.getY(vi), pos.getZ(vi));
      const label = this._labelSprite(String(n + 1), color);
      label.scale.setScalar(r * 3.2);
      label.position.set(0, r * 2.2, 0);
      sph.add(label);
      m.markers.add(sph);
    });
  }

  async setModelFromFile(key, file, opts) { this.setModel(key, await geometryFromFile(file), opts); }
  async setModelFromOBJUrl(key, url, opts) { this.setModel(key, parseOBJ(await (await fetch(url)).text()), opts); }

  has(key) { return !!this.models[key]; }

  clear(key) {
    const m = this.models[key];
    if (!m) return;
    this.scene.remove(m.group);
    m.group.traverse((o) => { if (o.geometry) o.geometry.dispose(); });
    delete this.models[key];
    if (this.active === key) this.active = Object.keys(this.models)[0] || null;
  }

  setActive(key) { this.active = key; this._apply(); }
  setWire(v) { this.showWire = v; this._apply(); }
  setShade(v) { this.showShade = v; this._apply(); }

  _apply() {
    for (const [k, m] of Object.entries(this.models)) {
      const on = k === this.active;
      m.group.visible = on;
      m.surface.visible = on && this.showShade;
      if (m.wire) m.wire.visible = on && this.showWire;
    }
  }
}
