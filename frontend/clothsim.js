// Browser cloth engine for the Cloth Studio tool — a small XPBD (Extended
// Position-Based Dynamics) solver plus the bits around it: a body collider, the
// procedural garment "patterns" that get draped, and a default mannequin.
//
// This is the same family of technique dedicated cloth-simulation tools use
// under the hood:
// the garment is a dense particle grid held together by *distance* constraints
// (so it can't stretch) and *bending* constraints (so it resists — but doesn't
// forbid — folding). Gravity pulls it down, it collides with the body, and the
// folds / wrinkles emerge on their own from excess material + collision, exactly
// like real fabric. Lower bending stiffness + more material = more wrinkles.
//
// References: Macklin et al. "XPBD" and "Small Steps in Physics Simulation";
// the distance + simplified bending constraints follow Carmen Cincotti's writeups.
import * as THREE from "three";
import { mergeGeometries } from "three/addons/utils/BufferGeometryUtils.js";

// ----------------------------------------------------------------- scratch
// Hot loops avoid allocations by reusing these.
const _a = new THREE.Vector3(), _b = new THREE.Vector3(), _c = new THREE.Vector3();
const _d = new THREE.Vector3(), _e = new THREE.Vector3();

// Stiffness sliders (0..1) map to XPBD *compliance* (inverse stiffness). 0 here
// means "as stiff as the solver allows", 1 means "very soft". Stretch wants to
// stay near-rigid; bending spans a big range because that's what tunes wrinkles.
function stretchComp(s) { return (1 - clamp01(s)) * 0.002 + 5e-8; }
function shearComp(s)   { return (1 - clamp01(s)) * 0.02  + 1e-7; }
function bendComp(s)    { const t = 1 - clamp01(s); return t * t * 0.25 + 2e-6; }
const clamp01 = (x) => Math.max(0, Math.min(1, x));

// =================================================================== ClothSim
// Owns the particle state and steps it forward. Geometry/indices are supplied by
// a garment spec; the same spec's `pinned` set fixes particles in place (a pin).
export class ClothSim {
  constructor(spec, opts = {}) {
    this.indices = spec.indices;            // Uint32Array triangle list
    this.uv = spec.uv || null;
    this.n = spec.count;                    // particle count
    const N = this.n;
    this.pos = new Float32Array(spec.positions);   // current  (3N)
    this.prev = new Float32Array(3 * N);           // previous  (3N)
    this.vel = new Float32Array(3 * N);            // velocity  (3N)
    this.invMass = new Float32Array(N);
    this.pinned = new Uint8Array(N);
    this.pinPos = new Float32Array(spec.positions);   // where pins are held
    this.home = new Float32Array(spec.positions);     // rest pose (for reset)

    this._mass = opts.mass || 0.3;
    for (let i = 0; i < N; i++) this.invMass[i] = 1 / this._mass;
    if (spec.pinned) for (const i of spec.pinned) this.setPinned(i, true);

    // edges: parallel arrays. group 0=structural 1=shear 2=bend
    this.ei = spec.ei; this.ej = spec.ej; this.rest = spec.rest; this.egroup = spec.egroup;
    this.lambda = new Float32Array(this.ei.length);

    this.collider = null;
    this.params = {
      gravity: -9.8, damping: 1.2, friction: 0.55, thickness: 0.03,
      substeps: 12, wind: 0, comp: [stretchComp(0.95), shearComp(0.7), bendComp(0.35)],
      floor: null,                          // y of an optional ground plane
      cling: 0.3,                           // 0..1 attraction that hugs cloth to the body
      clingBand: 0.06,                      // how far off the skin cling still reaches (<= collider pad)
      slack: 0,                             // 0..~0.12 rest-length compression -> extra buckling wrinkles
    };
    this._wind = new THREE.Vector3();
  }

  setStiffness({ stretch, shear, bend }) {
    if (stretch != null) this.params.comp[0] = stretchComp(stretch);
    if (shear   != null) this.params.comp[1] = shearComp(shear);
    if (bend    != null) this.params.comp[2] = bendComp(bend);
  }

  setMass(m) { this._mass = Math.max(0.02, m); const w = 1 / this._mass; for (let i = 0; i < this.n; i++) if (!this.pinned[i]) this.invMass[i] = w; }
  setParam(k, v) { this.params[k] = v; }
  setCollider(c) { this.collider = c; }

  setPinned(i, on) {
    this.pinned[i] = on ? 1 : 0;
    this.invMass[i] = on ? 0 : (1 / (this._mass || 0.3));
    if (on) { this.pinPos[3 * i] = this.pos[3 * i]; this.pinPos[3 * i + 1] = this.pos[3 * i + 1]; this.pinPos[3 * i + 2] = this.pos[3 * i + 2]; }
  }

  reset() {
    this.pos.set(this.home);
    this.prev.set(this.home);
    this.vel.fill(0);
    // re-anchor pins to the rest pose
    for (let i = 0; i < this.n; i++) if (this.pinned[i]) {
      this.pinPos[3 * i] = this.home[3 * i]; this.pinPos[3 * i + 1] = this.home[3 * i + 1]; this.pinPos[3 * i + 2] = this.home[3 * i + 2];
    }
  }

  // One frame -> several XPBD substeps (small steps are far more stable and give
  // crisper folds than many constraint iterations in one big step).
  step(dt) {
    dt = Math.min(dt, 1 / 30);
    const sub = Math.max(1, this.params.substeps | 0);
    const sdt = dt / sub;
    for (let s = 0; s < sub; s++) this._substep(sdt);
  }

  _substep(sdt) {
    const { pos, prev, vel, invMass, pinned, n } = this;
    const g = this.params.gravity, drag = Math.max(0, 1 - this.params.damping * sdt);
    this._wind.set(Math.sin(performanceLess() * 1.7) * 0.6, 0, Math.cos(performanceLess() * 1.3));
    const wind = this.params.wind, w = this._wind;

    // 1. integrate (predict)
    for (let i = 0; i < n; i++) {
      if (pinned[i]) { prev[3 * i] = pos[3 * i]; prev[3 * i + 1] = pos[3 * i + 1]; prev[3 * i + 2] = pos[3 * i + 2]; continue; }
      const ix = 3 * i;
      vel[ix + 1] += g * sdt;
      if (wind) { vel[ix] += w.x * wind * sdt; vel[ix + 2] += w.z * wind * sdt; }
      vel[ix] *= drag; vel[ix + 1] *= drag; vel[ix + 2] *= drag;
      prev[ix] = pos[ix]; prev[ix + 1] = pos[ix + 1]; prev[ix + 2] = pos[ix + 2];
      pos[ix] += vel[ix] * sdt; pos[ix + 1] += vel[ix + 1] * sdt; pos[ix + 2] += vel[ix + 2] * sdt;
    }

    // 2. solve constraints (single Gauss-Seidel sweep; lambdas reset each substep)
    this.lambda.fill(0);
    const { ei, ej, rest, egroup, lambda } = this;
    const comp = this.params.comp, isdt2 = 1 / (sdt * sdt), slack = this.params.slack;
    for (let k = 0; k < ei.length; k++) {
      const i = ei[k], j = ej[k];
      const wi = invMass[i], wj = invMass[j];
      const wsum = wi + wj; if (wsum === 0) continue;
      const ix = 3 * i, jx = 3 * j;
      let dx = pos[ix] - pos[jx], dy = pos[ix + 1] - pos[jx + 1], dz = pos[ix + 2] - pos[jx + 2];
      const len = Math.sqrt(dx * dx + dy * dy + dz * dz); if (len < 1e-9) continue;
      const grp = egroup[k];
      // Shrink the target length of in-plane (structural/shear) edges so the sheet
      // wants to be smaller than the space it fills; pinned + collided, it can't
      // contract, so it buckles out of plane into extra folds. Bend edges (grp 2)
      // are left alone so this reads as wrinkling, not curling.
      const r = slack && grp < 2 ? rest[k] * (1 - slack) : rest[k];
      const C = len - r;
      const alpha = comp[grp] * isdt2;
      const dl = -(C + alpha * lambda[k]) / (wsum + alpha);
      lambda[k] += dl;
      const s = dl / len;
      dx *= s; dy *= s; dz *= s;
      pos[ix] += dx * wi; pos[ix + 1] += dy * wi; pos[ix + 2] += dz * wi;
      pos[jx] -= dx * wj; pos[jx + 1] -= dy * wj; pos[jx + 2] -= dz * wj;
    }

    // 3. pins hold their target, then collide against body + floor
    for (let i = 0; i < n; i++) {
      if (pinned[i]) { pos[3 * i] = this.pinPos[3 * i]; pos[3 * i + 1] = this.pinPos[3 * i + 1]; pos[3 * i + 2] = this.pinPos[3 * i + 2]; continue; }
      this._collide(i);
    }

    // 4. derive velocity from the position change
    for (let i = 0; i < n; i++) {
      if (pinned[i]) { vel[3 * i] = vel[3 * i + 1] = vel[3 * i + 2] = 0; continue; }
      const ix = 3 * i, inv = 1 / sdt;
      vel[ix] = (pos[ix] - prev[ix]) * inv;
      vel[ix + 1] = (pos[ix + 1] - prev[ix + 1]) * inv;
      vel[ix + 2] = (pos[ix + 2] - prev[ix + 2]) * inv;
    }
  }

  _collide(i) {
    const pos = this.pos, prev = this.prev, ix = 3 * i, th = this.params.thickness;
    const f = this.params.friction;
    // floor
    if (this.params.floor != null && pos[ix + 1] < this.params.floor + th) {
      pos[ix + 1] = this.params.floor + th;
      // ground friction: cancel horizontal slip
      prev[ix] += (pos[ix] - prev[ix]) * f;
      prev[ix + 2] += (pos[ix + 2] - prev[ix + 2]) * f;
    }
    if (!this.collider) return;
    const q = this.collider.query(pos[ix], pos[ix + 1], pos[ix + 2]);
    if (!q) return;
    // Target rest position = a `th`-thick shell floating just off the skin.
    const tx = q.x + q.nx * th, ty = q.y + q.ny * th, tz = q.z + q.nz * th;
    const cling = this.params.cling;
    if (q.dist < th) {
      // penetrating / touching: hard push-out onto the shell, full friction grip
      pos[ix] = tx; pos[ix + 1] = ty; pos[ix + 2] = tz;
      this._friction(ix, q, f);
    } else if (cling > 0 && q.dist < th + this.params.clingBand) {
      // floating just off the body: draw it onto the shell so the fabric hugs the
      // skin instead of ballooning out. Fractional pull -> can't overshoot.
      pos[ix] += (tx - pos[ix]) * cling;
      pos[ix + 1] += (ty - pos[ix + 1]) * cling;
      pos[ix + 2] += (tz - pos[ix + 2]) * cling;
      this._friction(ix, q, f * 0.5);
    }
  }

  // Cancel the tangential part of this substep's motion so cloth grips the body
  // (surface normal in `hit`) instead of sliding off it. `f` = friction fraction.
  _friction(ix, hit, f) {
    const pos = this.pos, prev = this.prev;
    const mx = pos[ix] - prev[ix], my = pos[ix + 1] - prev[ix + 1], mz = pos[ix + 2] - prev[ix + 2];
    const dn = mx * hit.nx + my * hit.ny + mz * hit.nz;    // normal component of motion
    prev[ix] += (mx - dn * hit.nx) * f;
    prev[ix + 1] += (my - dn * hit.ny) * f;
    prev[ix + 2] += (mz - dn * hit.nz) * f;
  }

  // kinetic energy proxy — used to auto-pause once the drape settles
  energy() {
    let e = 0; const v = this.vel;
    for (let i = 0; i < this.n; i++) { if (this.pinned[i]) continue; e += v[3 * i] ** 2 + v[3 * i + 1] ** 2 + v[3 * i + 2] ** 2; }
    return e / Math.max(1, this.n);
  }
}

// Time source for wind that doesn't rely on Date.now/performance.now identity in
// any odd sandbox; a slowly increasing counter is plenty for a gust wave.
let _t = 0;
function performanceLess() { _t += 0.0007; return _t; }

// =============================================================== BodyCollider
// Point-vs-mesh collision: cloth particles are pushed out to `thickness` from
// the nearest body triangle. A uniform spatial grid keeps it fast — each tri is
// bucketed into the cells its (padded) bounds overlap, so a particle only tests
// the handful of triangles in its own cell.
export class BodyCollider {
  constructor(geometry, pad = 0.06) {
    const g = geometry.index ? geometry.toNonIndexed() : geometry;
    this.tris = g.getAttribute("position").array;   // 9 floats per triangle
    this.T = this.tris.length / 9;
    this.pad = pad;

    const bb = new THREE.Box3().setFromBufferAttribute(g.getAttribute("position"));
    this.min = bb.min.clone();
    const size = bb.getSize(new THREE.Vector3());
    const diag = size.length() || 1;
    this.cell = Math.max(diag / 48, 1e-3);
    this.nx = Math.max(1, Math.ceil(size.x / this.cell) + 1);
    this.ny = Math.max(1, Math.ceil(size.y / this.cell) + 1);
    this.nz = Math.max(1, Math.ceil(size.z / this.cell) + 1);
    this.grid = new Map();

    const t = this.tris;
    for (let f = 0; f < this.T; f++) {
      const o = f * 9;
      let lx = Infinity, ly = Infinity, lz = Infinity, hx = -Infinity, hy = -Infinity, hz = -Infinity;
      for (let v = 0; v < 3; v++) {
        const x = t[o + v * 3], y = t[o + v * 3 + 1], z = t[o + v * 3 + 2];
        if (x < lx) lx = x; if (y < ly) ly = y; if (z < lz) lz = z;
        if (x > hx) hx = x; if (y > hy) hy = y; if (z > hz) hz = z;
      }
      const ci0 = this._ci(lx - pad), ci1 = this._ci(hx + pad);
      const cj0 = this._cj(ly - pad), cj1 = this._cj(hy + pad);
      const ck0 = this._ck(lz - pad), ck1 = this._ck(hz + pad);
      for (let ci = ci0; ci <= ci1; ci++)
        for (let cj = cj0; cj <= cj1; cj++)
          for (let ck = ck0; ck <= ck1; ck++) {
            const key = ci + this.nx * (cj + this.ny * ck);
            let arr = this.grid.get(key); if (!arr) this.grid.set(key, arr = []);
            arr.push(f);
          }
    }
  }
  _ci(x) { return Math.max(0, Math.min(this.nx - 1, Math.floor((x - this.min.x) / this.cell))); }
  _cj(y) { return Math.max(0, Math.min(this.ny - 1, Math.floor((y - this.min.y) / this.cell))); }
  _ck(z) { return Math.max(0, Math.min(this.nz - 1, Math.floor((z - this.min.z) / this.cell))); }

  // Nearest point on the body to (px,py,pz). Returns null (no triangles nearby)
  // or {dist, x,y,z, nx,ny,nz} — the nearest *surface* point, its distance, and
  // the unit normal from surface toward the query point. The caller decides what
  // to do with it (push out of a contact, or gently cling to a near miss).
  query(px, py, pz) {
    const key = this._ci(px) + this.nx * (this._cj(py) + this.ny * this._ck(pz));
    const arr = this.grid.get(key); if (!arr) return null;
    const t = this.tris;
    let best = Infinity, bx = 0, by = 0, bz = 0;
    _a.set(px, py, pz);
    for (let n = 0; n < arr.length; n++) {
      const o = arr[n] * 9;
      _b.set(t[o], t[o + 1], t[o + 2]);
      _c.set(t[o + 3], t[o + 4], t[o + 5]);
      _d.set(t[o + 6], t[o + 7], t[o + 8]);
      closestOnTri(_a, _b, _c, _d, _e);
      const dx = px - _e.x, dy = py - _e.y, dz = pz - _e.z;
      const d2 = dx * dx + dy * dy + dz * dz;
      if (d2 < best) { best = d2; bx = _e.x; by = _e.y; bz = _e.z; }
    }
    if (best === Infinity) return null;
    const dist = Math.sqrt(best);
    let nx, ny, nz;
    if (dist > 1e-6) { nx = (px - bx) / dist; ny = (py - by) / dist; nz = (pz - bz) / dist; }
    else { nx = 0; ny = 1; nz = 0; }                // degenerate: shove up
    return { dist, x: bx, y: by, z: bz, nx, ny, nz };
  }
}

// closest point on triangle abc to p (Ericson, Real-Time Collision Detection).
// Pure scalar math so it never clobbers the module scratch vectors the caller
// (BodyCollider.resolve) is holding the triangle in.
function closestOnTri(p, a, b, c, out) {
  const ABx = b.x - a.x, ABy = b.y - a.y, ABz = b.z - a.z;
  const ACx = c.x - a.x, ACy = c.y - a.y, ACz = c.z - a.z;
  const APx = p.x - a.x, APy = p.y - a.y, APz = p.z - a.z;
  const d1 = ABx * APx + ABy * APy + ABz * APz;
  const d2 = ACx * APx + ACy * APy + ACz * APz;
  if (d1 <= 0 && d2 <= 0) { out.set(a.x, a.y, a.z); return; }
  const BPx = p.x - b.x, BPy = p.y - b.y, BPz = p.z - b.z;
  const d3 = ABx * BPx + ABy * BPy + ABz * BPz;
  const d4 = ACx * BPx + ACy * BPy + ACz * BPz;
  if (d3 >= 0 && d4 <= d3) { out.set(b.x, b.y, b.z); return; }
  const vc = d1 * d4 - d3 * d2;
  if (vc <= 0 && d1 >= 0 && d3 <= 0) { const v = d1 / (d1 - d3); out.set(a.x + ABx * v, a.y + ABy * v, a.z + ABz * v); return; }
  const CPx = p.x - c.x, CPy = p.y - c.y, CPz = p.z - c.z;
  const d5 = ABx * CPx + ABy * CPy + ABz * CPz;
  const d6 = ACx * CPx + ACy * CPy + ACz * CPz;
  if (d6 >= 0 && d5 <= d6) { out.set(c.x, c.y, c.z); return; }
  const vb = d5 * d2 - d1 * d6;
  if (vb <= 0 && d2 >= 0 && d6 <= 0) { const w = d2 / (d2 - d6); out.set(a.x + ACx * w, a.y + ACy * w, a.z + ACz * w); return; }
  const va = d3 * d6 - d5 * d4;
  if (va <= 0 && (d4 - d3) >= 0 && (d5 - d6) >= 0) {
    const w = (d4 - d3) / ((d4 - d3) + (d5 - d6));
    out.set(b.x + (c.x - b.x) * w, b.y + (c.y - b.y) * w, b.z + (c.z - b.z) * w); return;
  }
  const denom = 1 / (va + vb + vc);
  const v = vb * denom, w = vc * denom;
  out.set(a.x + ABx * v + ACx * w, a.y + ABy * v + ACy * w, a.z + ABz * v + ACz * w);
}

// =================================================================== garments
// Every garment is a (u,v) particle grid. `wrapU` closes the grid into a tube
// (skirts/dresses). Constraints: structural (4-neighbour), shear (diagonals) and
// bend (skip-one neighbour — the cheap, stable bending model). Top-edge rings can
// be pinned, optionally only every Nth particle to gather the cloth into pleats.
function buildGrid({ nu, nv, wrapU, place, pinRow0 = false, pinStep = 1, pinPredicate = null }) {
  const count = nu * nv;
  const positions = new Float32Array(count * 3);
  const uv = new Float32Array(count * 2);
  const pos3 = new THREE.Vector3();
  const idx = (u, v) => v * nu + u;
  for (let v = 0; v < nv; v++) {
    for (let u = 0; u < nu; u++) {
      place(u, v, pos3);
      const p = idx(u, v);
      positions[3 * p] = pos3.x; positions[3 * p + 1] = pos3.y; positions[3 * p + 2] = pos3.z;
      uv[2 * p] = u / (nu - (wrapU ? 0 : 1)); uv[2 * p + 1] = v / (nv - 1);
    }
  }

  // triangles
  const tri = [];
  const uMax = wrapU ? nu : nu - 1;
  for (let v = 0; v < nv - 1; v++) {
    for (let u = 0; u < uMax; u++) {
      const u1 = (u + 1) % nu;
      const a = idx(u, v), b = idx(u1, v), c = idx(u1, v + 1), d = idx(u, v + 1);
      tri.push(a, b, d, b, c, d);
    }
  }

  // edges
  const ei = [], ej = [], rest = [], egroup = [];
  const dist = (p, q) => {
    const dx = positions[3 * p] - positions[3 * q], dy = positions[3 * p + 1] - positions[3 * q + 1], dz = positions[3 * p + 2] - positions[3 * q + 2];
    return Math.sqrt(dx * dx + dy * dy + dz * dz);
  };
  const add = (p, q, group) => { ei.push(p); ej.push(q); rest.push(dist(p, q)); egroup.push(group); };
  for (let v = 0; v < nv; v++) {
    for (let u = 0; u < nu; u++) {
      const p = idx(u, v);
      if (u < nu - 1 || wrapU) add(p, idx((u + 1) % nu, v), 0);          // structural across
      if (v < nv - 1) add(p, idx(u, v + 1), 0);                          // structural down
      if (v < nv - 1 && (u < nu - 1 || wrapU)) {                         // shear diagonals
        add(p, idx((u + 1) % nu, v + 1), 1);
        add(idx((u + 1) % nu, v), idx(u, v + 1), 1);
      }
      if (u < nu - 2 || wrapU) add(p, idx((u + 2) % nu, v), 2);          // bend across
      if (v < nv - 2) add(p, idx(u, v + 2), 2);                          // bend down
    }
  }

  // pins
  const pinned = new Set();
  for (let v = 0; v < nv; v++) for (let u = 0; u < nu; u++) {
    const p = idx(u, v);
    let pin = false;
    if (pinRow0 && v === 0 && (u % pinStep === 0)) pin = true;
    if (pinPredicate && pinPredicate(u, v, positions, p)) pin = true;
    if (pin) pinned.add(p);
  }

  return {
    count, positions, uv, indices: new Uint32Array(tri), pinned,
    ei: new Int32Array(ei), ej: new Int32Array(ej),
    rest: new Float32Array(rest), egroup: new Uint8Array(egroup),
    nu, nv, wrapU,
  };
}

// A tube wrapped around an arbitrary axis p0->p1 (vertical for a leg, angled for
// a sleeve), with an elliptical cross-section that can taper along its length.
// Returns the same spec shape as buildGrid so it can be merged like any panel.
function axisFrame(dir) {
  const ref = Math.abs(dir.z) < 0.9 ? new THREE.Vector3(0, 0, 1) : new THREE.Vector3(1, 0, 0);
  const a = new THREE.Vector3().crossVectors(dir, ref).normalize();
  const b = new THREE.Vector3().crossVectors(dir, a).normalize();
  return { a, b };
}
function tubeAxisSpec(o) {
  const dir = new THREE.Vector3().subVectors(o.p1, o.p0);
  const len = dir.length() || 1; dir.normalize();
  const { a, b } = axisFrame(dir);
  return buildGrid({
    nu: o.nu, nv: o.nv, wrapU: true, pinRow0: !!o.pinTop, pinStep: o.pinStep || 1,
    place: (u, v, out) => {
      const t = v / (o.nv - 1), ang = (u / o.nu) * Math.PI * 2;
      const rx = o.r0x + (o.r1x - o.r0x) * t, rz = o.r0z + (o.r1z - o.r0z) * t;
      out.copy(o.p0).addScaledVector(dir, len * t)
        .addScaledVector(a, Math.cos(ang) * rx)
        .addScaledVector(b, Math.sin(ang) * rz);
    },
  });
}

// Append extra constraints to a spec (e.g. a sewn inseam). pairs: [i,j,rest,group].
function addConstraints(spec, pairs) {
  const m = pairs.length, base = spec.ei.length;
  const ei = new Int32Array(base + m), ej = new Int32Array(base + m);
  const rest = new Float32Array(base + m), egroup = new Uint8Array(base + m);
  ei.set(spec.ei); ej.set(spec.ej); rest.set(spec.rest); egroup.set(spec.egroup);
  for (let i = 0; i < m; i++) {
    ei[base + i] = pairs[i][0]; ej[base + i] = pairs[i][1];
    rest[base + i] = pairs[i][2]; egroup[base + i] = pairs[i][3] || 0;
  }
  return { ...spec, ei, ej, rest, egroup };
}

// Combine several panels into one cloth spec — the solver treats them as one
// particle system, so a t-shirt (torso + 2 sleeves) or shorts (2 legs) drapes as
// a unit. Pieces aren't sewn; each is pinned where it meets the body and the rest
// hangs, which reads correctly and stays robust on any body.
function mergePieces(pieces) {
  let count = 0, ne = 0, ni = 0;
  for (const p of pieces) { count += p.count; ne += p.ei.length; ni += p.indices.length; }
  const positions = new Float32Array(count * 3), uv = new Float32Array(count * 2);
  const indices = new Uint32Array(ni);
  const ei = new Int32Array(ne), ej = new Int32Array(ne), rest = new Float32Array(ne), egroup = new Uint8Array(ne);
  const pinned = new Set();
  let vo = 0, eo = 0, io = 0;
  for (const p of pieces) {
    positions.set(p.positions, vo * 3);
    if (p.uv) uv.set(p.uv, vo * 2);
    for (let i = 0; i < p.indices.length; i++) indices[io + i] = p.indices[i] + vo;
    for (let i = 0; i < p.ei.length; i++) {
      ei[eo + i] = p.ei[i] + vo; ej[eo + i] = p.ej[i] + vo;
      rest[eo + i] = p.rest[i]; egroup[eo + i] = p.egroup[i];
    }
    for (const idx of p.pinned) pinned.add(idx + vo);
    vo += p.count; eo += p.ei.length; io += p.indices.length;
  }
  return { count, positions, uv, indices, pinned, ei, ej, rest, egroup };
}

// Body metrics a garment sizes itself against.
function metrics(box) {
  const size = box.getSize(new THREE.Vector3());
  const c = box.getCenter(new THREE.Vector3());
  return {
    cx: c.x, cz: c.z, minY: box.min.y, maxY: box.max.y,
    rx: Math.max(size.x * 0.5, 0.05), rz: Math.max(size.z * 0.5, 0.05),
    h: size.y || 1, width: size.x || 1, depth: size.z || 1,
  };
}

// detail (1..3) -> grid resolution multiplier. Higher = finer wrinkles, slower.
const DETAIL = { 1: 0.62, 2: 1.0, 3: 1.5 };

// Half-depth (max |z - cz|) of the body within a Y band. We size tubes/necklines
// off DEPTH rather than the bounding box width because outstretched/down arms
// inflate the box's width but barely touch its depth — so this gives a snug,
// arm-proof radius at any height. Width radius is derived via a human aspect.
function depthAtBand(pos, yLo, yHi, cz) {
  let mz = 0; const n = pos.length / 3;
  for (let i = 0; i < n; i++) {
    const y = pos[3 * i + 1];
    if (y >= yLo && y <= yHi) { const dz = Math.abs(pos[3 * i + 2] - cz); if (dz > mz) mz = dz; }
  }
  return mz;
}
// Half-width (max |x - cx|) in a Y band — used where the actual body width is
// wanted (e.g. a shirt spanning the shoulders), accepting that arms count too.
function widthAtBand(pos, yLo, yHi, cx) {
  let mx = 0; const n = pos.length / 3;
  for (let i = 0; i < n; i++) {
    const y = pos[3 * i + 1];
    if (y >= yLo && y <= yHi) { const dx = Math.abs(pos[3 * i] - cx); if (dx > mx) mx = dx; }
  }
  return mx;
}

export function buildGarment(type, box, opts = {}) {
  const m = metrics(box);
  const full = opts.fullness ?? 1.3;        // extra material -> folds
  const k = DETAIL[opts.detail || 2];
  const bp = opts.bodyPositions;            // Float32Array of body verts (world)
  // depth at a height fraction of the body, with a sensible fallback
  const depthAt = (frac, halfBand = 0.04) => {
    if (!bp) return m.rz;
    const y = m.minY + m.h * frac;
    return depthAtBand(bp, y - m.h * halfBand, y + m.h * halfBand, m.cz) || m.rz;
  };
  const widthAt = (frac, halfBand = 0.04) => {
    if (!bp) return m.rx;
    const y = m.minY + m.h * frac;
    return widthAtBand(bp, y - m.h * halfBand, y + m.h * halfBand, m.cx) || m.rx;
  };
  const ASPECT = 1.4;                       // torso width:depth for the ellipse

  if (type === "skirt" || type === "dress" || type === "cloak") {
    const topFrac = type === "dress" ? 0.66 : type === "cloak" ? 0.86 : 0.56;
    const botFrac = type === "cloak" ? -0.02 : type === "dress" ? 0.12 : 0.06;
    const flare = type === "skirt" ? 1.7 : type === "cloak" ? 1.9 : 1.25;
    const topY = m.minY + m.h * topFrac, botY = m.minY + m.h * botFrac;
    const rz0 = depthAt(topFrac);
    const topRz = rz0 * 1.08, topRx = rz0 * ASPECT * 1.08;   // snug at the pinned ring
    const nu = Math.max(24, Math.round(56 * k));
    const nv = Math.max(14, Math.round(34 * k));
    // cloth ring radius is `full` bigger than the body it's pinned to -> it must
    // gather. We pin only every Nth top particle so the slack falls into pleats.
    const pinStep = Math.max(2, Math.round(nu / 16));
    const spec = buildGrid({
      nu, nv, wrapU: true, pinRow0: true, pinStep,
      place: (u, v, out) => {
        const a = (u / nu) * Math.PI * 2, t = v / (nv - 1);
        const grow = 1 + (flare - 1) * t * t;
        const rx = topRx * full * grow, rz = topRz * full * grow;
        out.set(m.cx + Math.cos(a) * rx, topY + (botY - topY) * t, m.cz + Math.sin(a) * rz);
      },
    });
    // Pinned particles snap onto the snug body ring (not the gathered cloth ring).
    for (const p of spec.pinned) {
      const u = p % nu, a = (u / nu) * Math.PI * 2;
      spec.positions[3 * p] = m.cx + Math.cos(a) * topRx;
      spec.positions[3 * p + 2] = m.cz + Math.sin(a) * topRz;
    }
    return spec;
  }

  if (type === "cape") {
    // a sheet hanging down the back, pinned along the shoulder line
    const nu = Math.max(22, Math.round(46 * k));
    const nv = Math.max(20, Math.round(40 * k));
    const topY = m.minY + m.h * 0.84, botY = m.minY + m.h * 0.08;
    const rzCh = depthAt(0.78, 0.06);
    const halfW = rzCh * 2.4 * full, zBack = m.cz - rzCh * 1.05;
    const pinStep = Math.max(2, Math.round(nu / 14));
    return buildGrid({
      nu, nv, wrapU: false, pinRow0: true, pinStep,
      place: (u, v, out) => {
        const sx = (u / (nu - 1)) * 2 - 1, t = v / (nv - 1);
        // wrap the shoulders slightly forward at the top so it sits on them
        const z = zBack + Math.max(0, 1 - t * 4) * (1 - Math.abs(sx)) * rzCh * 0.9;
        out.set(m.cx + sx * halfW, topY + (botY - topY) * t, z);
      },
    });
  }

  if (type === "poncho") {
    // a horizontal square over the shoulders with a neck hole; corners drape down
    const nu = Math.max(30, Math.round(50 * k));
    const nv = Math.max(30, Math.round(50 * k));
    const y = m.minY + m.h * 0.86;
    const neck = depthAt(0.9, 0.03) * 1.35;          // neckline ~ body depth at neck
    const halfW = neck * 3.4 * full, halfD = neck * 3.4 * full;
    return buildGrid({
      nu, nv, wrapU: false,
      place: (u, v, out) => {
        const sx = (u / (nu - 1)) * 2 - 1, sz = (v / (nv - 1)) * 2 - 1;
        out.set(m.cx + sx * halfW, y, m.cz + sz * halfD);
      },
      // pin a ring of particles around the neck hole so it rests on the shoulders
      pinPredicate: (u, v, P, p) => {
        const dx = P[3 * p] - m.cx, dz = P[3 * p + 2] - m.cz;
        const r = Math.hypot(dx, dz);
        return r < neck * 1.25 && r > neck * 0.55;
      },
    });
  }

  if (type === "scarf") {
    // a long narrow strip laid over the neck, pinned across the middle; the two
    // ends fall down the front and back
    const nu = Math.max(8, Math.round(10 * k));
    const nv = Math.max(40, Math.round(64 * k));
    const neckY = m.minY + m.h * 0.82, halfW = m.h * 0.05;
    const rzN = depthAt(0.85, 0.04);
    const len = m.h * 0.5, zF = m.cz + rzN * 1.25, zB = m.cz - rzN * 1.25;
    return buildGrid({
      nu, nv, wrapU: false,
      place: (u, v, out) => {
        const sx = (u / (nu - 1)) * 2 - 1, t = v / (nv - 1);
        // v: 0 = bottom-front, .5 = over the neck, 1 = bottom-back
        let x, y, z;
        if (t < 0.5) { const s = t / 0.5; z = zF; y = neckY - len * (1 - s); x = m.cx + sx * halfW; }
        else { const s = (t - 0.5) / 0.5; z = zB; y = neckY - len * s; x = m.cx + sx * halfW; }
        // lift the middle to arch over the neck
        const arch = 1 - Math.min(1, Math.abs(t - 0.5) / 0.18);
        z += (m.cz - z) * arch; y += arch * rzN * 0.9;
        out.set(x, y, z);
      },
      pinPredicate: (u, v) => Math.abs(v / (nv - 1) - 0.5) < 0.06,
    });
  }

  if (type === "tshirt") {
    // torso tube from the shoulders to the waist (pinned snug at the shoulders,
    // flaring out toward the hem) plus two short sleeve tubes over the arms
    const V = THREE.Vector3;
    const shoulderY = m.minY + m.h * 0.80, waistY = m.minY + m.h * 0.54;
    // Width comes from the actual body span at the shoulders (so the shirt wraps
    // shoulder-to-shoulder), tapering to a depth-based, arm-free waist. Depth
    // gives the front/back. Fitted so collision holds it up — no crumpling.
    const shRx = widthAt(0.80, 0.05) * 0.94, shRz = depthAt(0.80, 0.05);
    const waRz = depthAt(0.55, 0.05);
    const ease = 1 + (full - 1) * 0.3;
    const nu = Math.max(24, Math.round(48 * k)), nv = Math.max(12, Math.round(24 * k));
    const torso = tubeAxisSpec({
      p0: new V(m.cx, shoulderY, m.cz), p1: new V(m.cx, waistY, m.cz),
      r0x: shRx, r0z: shRz * 1.12,
      r1x: waRz * 1.55 * ease, r1z: waRz * 1.2 * ease,
      nu, nv, pinTop: true,
    });
    const shX = shRx, slLen = m.h * 0.16, slR = Math.max(shRz * 0.6, 0.06);
    const snu = Math.max(12, Math.round(18 * k)), snv = Math.max(6, Math.round(10 * k));
    const sleeves = [-1, 1].map((s) => tubeAxisSpec({
      p0: new V(m.cx + s * shX * 0.72, shoulderY + m.h * 0.012, m.cz),
      p1: new V(m.cx + s * (shX + 0.07), shoulderY - slLen, m.cz),
      r0x: slR * 1.2, r0z: slR * 1.2, r1x: slR * 1.08 * ease, r1z: slR * 1.08 * ease,
      nu: snu, nv: snv, pinTop: true,
    }));
    return mergePieces([torso, ...sleeves]);
  }

  if (type === "shorts") {
    // a single hip tube (waistband -> above the knee) with a sewn INSEAM: the
    // front-centre column is stitched to the back-centre column down the middle,
    // which splits the tube into a left and right leg — the standard way to make
    // shorts/culottes without branching topology.
    const V = THREE.Vector3;
    const hipY = m.minY + m.h * 0.57, botY = m.minY + m.h * 0.30;
    const rzHip = Math.max(depthAt(0.55, 0.04), 0.07);       // seat depth
    const rxHip = rzHip * 1.45;                              // hip width (arm-free)
    const ease = 1 + (full - 1) * 0.3;
    const nu = Math.max(24, Math.round(40 * k)), nv = Math.max(14, Math.round(22 * k));
    const tube = tubeAxisSpec({
      p0: new V(m.cx, hipY, m.cz), p1: new V(m.cx, botY, m.cz),
      r0x: rxHip * 1.04, r0z: rzHip * 1.1,
      r1x: rxHip * 1.12 * ease, r1z: rzHip * 1.22 * ease,
      nu, nv, pinTop: true,
    });
    // u=nu/4 is the front centreline (+z), u=3nu/4 the back (-z). Pinch them
    // together from the crotch down so the two leg openings form.
    const uF = Math.round(nu * 0.25), uB = Math.round(nu * 0.75);
    const crotchV = Math.round(nv * 0.42), pairs = [];
    for (let v = crotchV; v < nv; v++) pairs.push([v * nu + uF, v * nu + uB, 0.012, 0]);
    return addConstraints(tube, pairs);
  }

  // "sheet": a free hanging banner pinned along its top edge — a sandbox panel
  const nu = Math.max(24, Math.round(44 * k));
  const nv = Math.max(24, Math.round(44 * k));
  const topY = m.maxY * 0.98, halfW = m.width * 0.9 * full, z = m.cz + m.rz + 0.15;
  return buildGrid({
    nu, nv, wrapU: false, pinRow0: true, pinStep: Math.max(2, Math.round(nu / 12)),
    place: (u, v, out) => {
      const sx = (u / (nu - 1)) * 2 - 1, t = v / (nv - 1);
      out.set(m.cx + sx * halfW, topY - t * m.h * 0.9, z);
    },
  });
}

// ============================================================ custom garment
// Turn an ARBITRARY imported garment mesh (a t-shirt, dress, cape… the user
// modelled) into a cloth spec, so the solver can drape it exactly like the
// procedural patterns above. The steps:
//
//   1. Weld by position. Exported meshes split vertices at UV/normal seams, so
//      the raw topology is full of holes as far as the cloth is concerned. We
//      merge coincident vertices into ONE particle each, giving a single
//      connected sheet that can't tear at a seam.
//   2. Every unique triangle edge → a structural (stretch) constraint.
//   3. Every interior edge (shared by two triangles) → a bend constraint between
//      the two opposite vertices, so the fabric resists — but doesn't forbid —
//      folding, just like buildGrid's skip-one bend edges.
//   4. Pin the top band: particles in the top `pinTop` fraction of the garment's
//      height are fixed, so e.g. a shirt hangs from its shoulders/collar and the
//      rest falls under gravity. The user can refine with the pin brush.
//
// `geom` must already be placed in world space (fitted onto the body); the
// pins are captured at those world positions.
export function buildGarmentFromGeometry(geom, opts = {}) {
  const pinTopFrac = clamp01(opts.pinTop ?? 0.12);
  const posAttr = geom.getAttribute("position");
  const uvAttr = geom.getAttribute("uv");
  const srcCount = posAttr.count;

  // Weld tolerance scales with the model so it catches seams without collapsing
  // real detail (~1/2000 of the bounding-box diagonal).
  geom.computeBoundingBox();
  const diag = geom.boundingBox.getSize(new THREE.Vector3()).length() || 1;
  const q = 1 / Math.max(diag / 2000, 1e-6);

  const map = new Map();                    // quantized position -> particle index
  const remap = new Int32Array(srcCount);   // source vertex -> particle index
  const P = [], UV = [];
  for (let i = 0; i < srcCount; i++) {
    const x = posAttr.getX(i), y = posAttr.getY(i), z = posAttr.getZ(i);
    const key = Math.round(x * q) + "," + Math.round(y * q) + "," + Math.round(z * q);
    let ni = map.get(key);
    if (ni === undefined) {
      ni = P.length / 3; map.set(key, ni);
      P.push(x, y, z);
      if (uvAttr) UV.push(uvAttr.getX(i), uvAttr.getY(i));
    }
    remap[i] = ni;
  }
  const count = P.length / 3;
  const positions = Float32Array.from(P);
  if (count < 3) throw new Error("garment mesh has too few vertices to drape");

  // Remapped, degenerate-culled triangle list.
  const srcIndex = geom.index ? geom.index.array : null;
  const triLen = srcIndex ? srcIndex.length : srcCount;
  const gv = (t) => remap[srcIndex ? srcIndex[t] : t];
  const tri = [];
  for (let t = 0; t + 2 < triLen; t += 3) {
    const a = gv(t), b = gv(t + 1), c = gv(t + 2);
    if (a !== b && b !== c && a !== c) tri.push(a, b, c);
  }
  if (!tri.length) throw new Error("garment mesh has no faces to drape");

  // Unique edges; track the opposite vertex from each adjacent triangle so
  // interior edges can spawn a bend constraint across the shared edge.
  const ekey = (a, b) => (a < b ? a * count + b : b * count + a);
  const edges = new Map();
  const addEdge = (a, b, opp) => {
    const k = ekey(a, b);
    let e = edges.get(k);
    if (!e) edges.set(k, e = { i: a, j: b, opp: [] });
    e.opp.push(opp);
  };
  for (let t = 0; t < tri.length; t += 3) {
    const a = tri[t], b = tri[t + 1], c = tri[t + 2];
    addEdge(a, b, c); addEdge(b, c, a); addEdge(c, a, b);
  }

  const ei = [], ej = [], rest = [], egroup = [];
  const dist = (p, r) => {
    const dx = positions[3 * p] - positions[3 * r];
    const dy = positions[3 * p + 1] - positions[3 * r + 1];
    const dz = positions[3 * p + 2] - positions[3 * r + 2];
    return Math.sqrt(dx * dx + dy * dy + dz * dz);
  };
  for (const e of edges.values()) {
    ei.push(e.i); ej.push(e.j); rest.push(dist(e.i, e.j)); egroup.push(0);  // stretch
    if (e.opp.length >= 2) {                                                // bend
      ei.push(e.opp[0]); ej.push(e.opp[1]);
      rest.push(dist(e.opp[0], e.opp[1])); egroup.push(2);
    }
  }

  // Pin the top band of the garment (its highest `pinTop` slice in Y).
  let minY = Infinity, maxY = -Infinity;
  for (let i = 0; i < count; i++) { const y = positions[3 * i + 1]; if (y < minY) minY = y; if (y > maxY) maxY = y; }
  const thresh = maxY - (maxY - minY) * pinTopFrac;
  const pinned = new Set();
  if (pinTopFrac > 0) for (let i = 0; i < count; i++) if (positions[3 * i + 1] >= thresh) pinned.add(i);

  return {
    count, positions,
    uv: uvAttr ? Float32Array.from(UV) : null,
    indices: Uint32Array.from(tri), pinned,
    ei: Int32Array.from(ei), ej: Int32Array.from(ej),
    rest: Float32Array.from(rest), egroup: Uint8Array.from(egroup),
  };
}

// =================================================================== mannequin
// A simple genderless body from capsules so the tool is usable with no upload.
// Returned merged geometry doubles as the collider source.
export function makeMannequin() {
  const parts = [];
  const cap = (rTop, rBot, h, x, y, z, rx = 0, rz = 0) => {
    const g = new THREE.CylinderGeometry(rTop, rBot, h, 18, 1, false);
    if (rx) g.rotateX(rx); if (rz) g.rotateZ(rz);
    g.translate(x, y, z); parts.push(g);
  };
  const ball = (r, x, y, z, sx = 1, sy = 1, sz = 1) => {
    const g = new THREE.SphereGeometry(r, 20, 16); g.scale(sx, sy, sz); g.translate(x, y, z); parts.push(g);
  };

  ball(0.13, 0, 1.66, 0, 1, 1.15, 0.95);           // head
  cap(0.055, 0.07, 0.12, 0, 1.5, 0);               // neck
  // torso (chest -> waist), elliptical via scale on a cylinder
  const torso = new THREE.CylinderGeometry(0.17, 0.13, 0.46, 22, 1);
  torso.scale(1.18, 1, 0.62); torso.translate(0, 1.24, 0); parts.push(torso);
  const hips = new THREE.SphereGeometry(0.17, 22, 16); hips.scale(1.25, 0.8, 0.78); hips.translate(0, 0.97, 0); parts.push(hips);
  // legs
  for (const s of [-1, 1]) {
    cap(0.085, 0.07, 0.5, s * 0.09, 0.7, 0);       // thigh
    ball(0.07, s * 0.095, 0.45, 0);                // knee
    cap(0.06, 0.045, 0.46, s * 0.1, 0.22, 0);      // shin
    ball(0.06, s * 0.1, 0.02, 0.03, 1.1, 0.7, 1.5);// foot
  }
  // arms (slightly out)
  for (const s of [-1, 1]) {
    ball(0.075, s * 0.21, 1.42, 0);                // shoulder
    cap(0.06, 0.05, 0.4, s * 0.27, 1.2, 0, 0, s * 0.18);   // upper arm
    cap(0.05, 0.04, 0.4, s * 0.33, 0.82, 0, 0, s * 0.1);   // forearm
    ball(0.05, s * 0.36, 0.6, 0, 1, 1.3, 0.7);     // hand
  }

  const merged = mergeGeometries(parts, false);
  merged.computeVertexNormals();
  return merged;
}
