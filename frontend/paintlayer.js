// ===========================================================================
// paintlayer.js — the painting engine behind the Paint Studio tool.
//
// A PaintSurface owns one paintable mesh and a *stack* of layers, much like a
// high-end 2D art program. Every layer is its own RGBA canvas with a blend
// mode (normal / multiply / screen / glow…), an opacity and a visibility flag.
// The layers are flattened top-to-bottom into a single `composite` canvas that
// is bound to the mesh as a CanvasTexture.
//
// Painting model (for the colour / erase brushes):
//   - `snapshot` : the active layer's pixels captured at stroke start
//   - `stroke`   : the in-progress stroke, accumulated on its own buffer
//   - live view  : composite = flatten(layers), where the active layer is shown
//                  as snapshot + stroke (so overlapping stamps stay one flat
//                  opacity instead of darkening).
//   - on stroke end the stroke buffer is baked into the active layer for real.
//
// Soft-effect brushes (smudge / blur) mutate the active layer in place instead,
// because they transform existing pixels rather than lay down colour.
// ===========================================================================
import * as THREE from "three";

// Blend modes exposed in the UI. `op` is the Canvas2D globalCompositeOperation
// used when the layer is flattened onto everything beneath it.
export const BLEND_MODES = [
  { id: "normal",     label: "Normal",       op: "source-over" },
  { id: "multiply",   label: "Multiply",     op: "multiply" },
  { id: "darken",     label: "Darken",       op: "darken" },
  { id: "burn",       label: "Color Burn",   op: "color-burn" },
  { id: "screen",     label: "Screen",       op: "screen" },
  { id: "lighten",    label: "Lighten",      op: "lighten" },
  { id: "dodge",      label: "Color Dodge",  op: "color-dodge" },
  { id: "glow",       label: "Glow / Add",   op: "lighter" },
  { id: "overlay",    label: "Overlay",      op: "overlay" },
  { id: "softlight",  label: "Soft Light",   op: "soft-light" },
  { id: "hardlight",  label: "Hard Light",   op: "hard-light" },
  { id: "difference", label: "Difference",   op: "difference" },
];
const OP = Object.fromEntries(BLEND_MODES.map((b) => [b.id, b.op]));

let _layerSeq = 0;

export class PaintSurface {
  constructor(mesh, size, baseColor) {
    this.mesh = mesh;
    this.size = size;
    this.baseColor = baseColor;
    this.name = mesh.name || "Mesh";

    this.layers = [];        // bottom -> top
    this.activeId = null;

    // Flattened output shown on the model.
    this.composite = makeCanvas(size);
    this.cctx = this.composite.getContext("2d", { willReadFrequently: false });

    // Scratch buffers reused across strokes.
    this.stroke = makeCanvas(size);     // current stroke, full colour
    this.snapshot = makeCanvas(size);   // active layer at stroke start
    this._tmp = makeCanvas(size);       // live active-layer preview
    this._tmpctx = this._tmp.getContext("2d", { willReadFrequently: false });

    this._last = {};        // per sub-path (0 = primary, 1 = mirror) {x,y}

    // Seed a "Base" layer from the mesh's existing texture, else a flat fill.
    const base = this._newLayer("Base");
    const src = mesh.material && mesh.material.map;
    if (src && src.image) {
      try { base.ctx.drawImage(src.image, 0, 0, size, size); }
      catch (e) { fillCanvas(base.canvas, baseColor); }
    } else {
      fillCanvas(base.canvas, baseColor);
    }
    this.layers.push(base);
    this.activeId = base.id;

    this.texture = new THREE.CanvasTexture(this.composite);
    this.texture.colorSpace = THREE.SRGBColorSpace;
    this.texture.anisotropy = 8;

    this._origMaterial = mesh.material;
    mesh.material = new THREE.MeshStandardMaterial({
      map: this.texture, roughness: 0.82, metalness: 0.0,
    });

    this.recomposite();
  }

  // ---- layer model --------------------------------------------------------
  _newLayer(name) {
    const canvas = makeCanvas(this.size);
    return {
      id: ++_layerSeq,
      name,
      canvas,
      ctx: canvas.getContext("2d", { willReadFrequently: true }),
      blend: "normal",
      opacity: 1.0,
      visible: true,
    };
  }

  layerList() { return this.layers; }
  activeLayer() { return this.layers.find((l) => l.id === this.activeId) || this.layers[this.layers.length - 1]; }
  _index(id) { return this.layers.findIndex((l) => l.id === id); }

  setActive(id) { if (this._index(id) >= 0) this.activeId = id; }

  addLayer(name) {
    const l = this._newLayer(name || `Layer ${this.layers.length}`);
    const i = this._index(this.activeId);
    this.layers.splice(i + 1, 0, l);   // above the active layer
    this.activeId = l.id;
    this.recomposite();
    return l;
  }

  duplicateLayer(id) {
    const i = this._index(id);
    if (i < 0) return;
    const src = this.layers[i];
    const l = this._newLayer(src.name + " copy");
    l.ctx.drawImage(src.canvas, 0, 0);
    l.blend = src.blend; l.opacity = src.opacity; l.visible = src.visible;
    this.layers.splice(i + 1, 0, l);
    this.activeId = l.id;
    this.recomposite();
  }

  deleteLayer(id) {
    if (this.layers.length <= 1) return false;   // keep at least one
    const i = this._index(id);
    if (i < 0) return false;
    this.layers.splice(i, 1);
    if (this.activeId === id) this.activeId = this.layers[Math.max(0, i - 1)].id;
    this.recomposite();
    return true;
  }

  moveLayer(id, dir) {
    const i = this._index(id);
    const j = i + dir;
    if (i < 0 || j < 0 || j >= this.layers.length) return;
    const [l] = this.layers.splice(i, 1);
    this.layers.splice(j, 0, l);
    this.recomposite();
  }

  // Flatten a layer down onto the one below it (respecting blend + opacity).
  mergeDown(id) {
    const i = this._index(id);
    if (i <= 0) return;                 // nothing beneath
    const top = this.layers[i], below = this.layers[i - 1];
    const c = below.ctx;
    c.globalCompositeOperation = "source-over";
    c.globalAlpha = 1;
    // Fold the top layer's own opacity/blend into `below`.
    c.globalCompositeOperation = OP[top.blend] || "source-over";
    c.globalAlpha = top.opacity;
    if (top.visible) c.drawImage(top.canvas, 0, 0);
    c.globalCompositeOperation = "source-over";
    c.globalAlpha = 1;
    this.layers.splice(i, 1);
    this.activeId = below.id;
    this.recomposite();
  }

  setBlend(id, blend) { const l = this.layers[this._index(id)]; if (l) { l.blend = blend; this.recomposite(); } }
  setOpacity(id, v)   { const l = this.layers[this._index(id)]; if (l) { l.opacity = clamp01(v); this.recomposite(); } }
  setVisible(id, v)   { const l = this.layers[this._index(id)]; if (l) { l.visible = !!v; this.recomposite(); } }
  rename(id, name)    { const l = this.layers[this._index(id)]; if (l) l.name = name; }

  clearLayer(id) {
    const l = this.layers[this._index(id)];
    if (!l) return;
    if (l === this.layers[0]) fillCanvas(l.canvas, this.baseColor);  // base stays opaque
    else { l.ctx.globalCompositeOperation = "source-over"; l.ctx.clearRect(0, 0, this.size, this.size); }
    this.recomposite();
  }

  fillLayer(id, rgb) {
    const l = this.layers[this._index(id)];
    if (!l) return;
    l.ctx.globalCompositeOperation = "source-over";
    l.ctx.globalAlpha = 1;
    l.ctx.fillStyle = `rgb(${rgb[0]},${rgb[1]},${rgb[2]})`;
    l.ctx.fillRect(0, 0, this.size, this.size);
    this.recomposite();
  }

  // Load an external image (a re-imported texture map) onto its own new layer,
  // stretched to fill the UV texture space. Handy for continuing work on an
  // exported PNG, or dropping a photo/pattern onto the model as a base.
  addImageLayer(image, name) {
    const l = this._newLayer(name || "Texture");
    l.ctx.globalCompositeOperation = "source-over";
    l.ctx.globalAlpha = 1;
    l.ctx.drawImage(image, 0, 0, this.size, this.size);
    const i = this._index(this.activeId);
    this.layers.splice(i + 1, 0, l);   // above the active layer
    this.activeId = l.id;
    this.recomposite();
    return l;
  }

  // ---- compositing --------------------------------------------------------
  // Flatten all layers into `composite`. If `liveActive` is supplied it is
  // substituted for the active layer's pixels (used for the live stroke view).
  _composite(liveActive) {
    const c = this.cctx, S = this.size;
    c.globalCompositeOperation = "source-over";
    c.globalAlpha = 1;
    c.clearRect(0, 0, S, S);
    for (const l of this.layers) {
      if (!l.visible) continue;
      const src = (l.id === this.activeId && liveActive) ? liveActive : l.canvas;
      c.globalCompositeOperation = OP[l.blend] || "source-over";
      c.globalAlpha = l.opacity;
      c.drawImage(src, 0, 0);
    }
    c.globalCompositeOperation = "source-over";
    c.globalAlpha = 1;
    this.texture.needsUpdate = true;
  }

  recomposite() { this._composite(null); }

  // ---- coordinates + sampling --------------------------------------------
  uvToPixel(uv) {
    return { x: uv.x * this.size, y: (1 - uv.y) * this.size };  // flipY canvas
  }

  /** Sample the composited (visible) colour at a uv as [r,g,b]. */
  sampleColor(uv) {
    const p = this.uvToPixel(uv);
    const x = THREE.MathUtils.clamp(Math.floor(p.x), 0, this.size - 1);
    const y = THREE.MathUtils.clamp(Math.floor(p.y), 0, this.size - 1);
    const d = this.cctx.getImageData(x, y, 1, 1).data;
    return [d[0], d[1], d[2]];
  }

  // ---- strokes ------------------------------------------------------------
  beginStroke() {
    this._last = {};
    const s = this.snapshot.getContext("2d");
    s.globalCompositeOperation = "source-over";
    s.globalAlpha = 1;
    s.clearRect(0, 0, this.size, this.size);
    s.drawImage(this.activeLayer().canvas, 0, 0);
    this.stroke.getContext("2d").clearRect(0, 0, this.size, this.size);
  }

  /** Extend sub-path `sub` (0 = primary, 1 = mirror) to `uv`. */
  strokeTo(uv, brush, sub = 0) {
    const p = this.uvToPixel(uv);
    const last = this._last[sub];
    const soft = brush.tool === "smudge" || brush.tool === "blur";
    if (last) {
      const dx = p.x - last.x, dy = p.y - last.y;
      const dist = Math.hypot(dx, dy);
      // A big jump means the cursor crossed a UV seam / island — don't streak.
      if (dist > brush.size * 4 && !soft) {
        this._apply(p.x, p.y, p.x, p.y, brush);
      } else {
        const step = Math.max(1, brush.size * brush.spacing);
        const n = Math.max(1, Math.ceil(dist / step));
        let px = last.x, py = last.y;
        for (let i = 1; i <= n; i++) {
          const t = i / n;
          const x = last.x + dx * t, y = last.y + dy * t;
          this._apply(x, y, px, py, brush);
          px = x; py = y;
        }
      }
    } else {
      this._apply(p.x, p.y, p.x, p.y, brush);
    }
    this._last[sub] = p;
    this._live(brush);
  }

  _apply(x, y, px, py, brush) {
    switch (brush.tool) {
      case "smudge": this._smudge(x, y, px, py, brush); break;
      case "blur":   this._blur(x, y, brush); break;
      case "erase":  this._stamp(this.stroke.getContext("2d"), x, y, brush, [255, 255, 255]); break;
      default:       this._stamp(this.stroke.getContext("2d"), x, y, brush, brush.color); break;
    }
  }

  _live(brush) {
    if (brush.tool === "smudge" || brush.tool === "blur") {
      this._composite(this.activeLayer().canvas);   // mutated in place
    } else {
      this._paintActiveInto(this._tmpctx, brush);
      this._composite(this._tmp);
    }
  }

  endStroke(brush) {
    const a = this.activeLayer().ctx;
    if (brush.tool === "erase") {
      a.globalCompositeOperation = "destination-out";
      a.globalAlpha = brush.opacity;
      a.drawImage(this.stroke, 0, 0);
    } else if (brush.tool !== "smudge" && brush.tool !== "blur") {
      a.globalCompositeOperation = "source-over";
      a.globalAlpha = brush.opacity;
      a.drawImage(this.stroke, 0, 0);
    }
    a.globalCompositeOperation = "source-over";
    a.globalAlpha = 1;
    this.stroke.getContext("2d").clearRect(0, 0, this.size, this.size);
    this._last = {};
    this.recomposite();
  }

  // Build the live active layer = snapshot + stroke (colour) / minus (erase).
  _paintActiveInto(t, brush) {
    const S = this.size;
    t.globalCompositeOperation = "source-over";
    t.globalAlpha = 1;
    t.clearRect(0, 0, S, S);
    t.drawImage(this.snapshot, 0, 0);
    if (brush.tool === "erase") {
      t.globalCompositeOperation = "destination-out";
      t.globalAlpha = brush.opacity;
    } else {
      t.globalCompositeOperation = "source-over";
      t.globalAlpha = brush.opacity;
    }
    t.drawImage(this.stroke, 0, 0);
    t.globalCompositeOperation = "source-over";
    t.globalAlpha = 1;
  }

  // One soft brush dab onto the stroke buffer at `flow` alpha (build-up).
  _stamp(ctx, x, y, brush, color) {
    const r = Math.max(0.5, brush.size / 2);
    const [cr, cg, cb] = color;
    const a = brush.flow;
    const g = ctx.createRadialGradient(x, y, 0, x, y, r);
    const hard = Math.min(0.98, Math.max(0, brush.hardness));
    g.addColorStop(0, `rgba(${cr},${cg},${cb},${a})`);
    g.addColorStop(hard, `rgba(${cr},${cg},${cb},${a})`);
    g.addColorStop(1, `rgba(${cr},${cg},${cb},0)`);
    ctx.globalCompositeOperation = "source-over";
    ctx.globalAlpha = 1;
    ctx.fillStyle = g;
    ctx.beginPath();
    ctx.arc(x, y, r, 0, Math.PI * 2);
    ctx.fill();
  }

  // Smudge: drag the active layer's pixels from the previous point into a soft
  // circular region at the current point, blended at `opacity` strength.
  _smudge(x, y, px, py, brush) {
    const dx = x - px, dy = y - py;
    if (dx === 0 && dy === 0) return;
    const a = this.activeLayer();
    const r = Math.max(1, brush.size / 2);
    const ctx = a.ctx;
    ctx.save();
    ctx.beginPath();
    ctx.arc(x, y, r, 0, Math.PI * 2);
    ctx.clip();
    ctx.globalCompositeOperation = "source-over";
    ctx.globalAlpha = THREE.MathUtils.clamp(brush.opacity, 0, 0.95);
    ctx.drawImage(a.canvas, dx, dy);   // brings the (px,py) neighbourhood to (x,y)
    ctx.restore();
    ctx.globalAlpha = 1;
  }

  // Blur: soften a circular region of the active layer using a canvas filter.
  _blur(x, y, brush) {
    const a = this.activeLayer();
    const r = Math.max(1, brush.size / 2);
    const amt = Math.max(1, brush.size * 0.05 * (0.4 + brush.opacity));
    const ctx = a.ctx;
    ctx.save();
    ctx.beginPath();
    ctx.arc(x, y, r, 0, Math.PI * 2);
    ctx.clip();
    ctx.globalCompositeOperation = "source-over";
    ctx.globalAlpha = 1;
    if ("filter" in ctx) ctx.filter = `blur(${amt}px)`;
    ctx.drawImage(a.canvas, 0, 0);
    if ("filter" in ctx) ctx.filter = "none";
    ctx.restore();
  }

  // Paint-bucket flood fill on the active layer (scanline, colour tolerance).
  // opts.respectSeams keeps the fill inside the UV island under the cursor, so a
  // seam that splits the surface into separate texture areas also splits the
  // fill (touching-but-different islands won't bleed into one another).
  floodFill(uv, brush, opts = {}) {
    const tolerance = opts.tolerance ?? 32;
    const S = this.size;
    const p = this.uvToPixel(uv);
    const x0 = Math.floor(p.x), y0 = Math.floor(p.y);
    if (x0 < 0 || y0 < 0 || x0 >= S || y0 >= S) return;
    const c = this.activeLayer().ctx;
    c.globalAlpha = 1;
    c.globalCompositeOperation = "source-over";
    const img = c.getImageData(0, 0, S, S);
    const data = img.data;
    const seed = (y0 * S + x0) * 4;
    const sr = data[seed], sg = data[seed + 1], sb = data[seed + 2], sa = data[seed + 3];
    const [fr, fg, fb] = brush.tool === "erase" ? [0, 0, 0] : brush.color;
    const erase = brush.tool === "erase";
    const alpha = brush.opacity;

    // Seam gate: constrain to the seed texel's UV island (0 = uncovered/seam gap,
    // in which case we skip the gate rather than trap the fill in the gaps).
    const islands = opts.respectSeams ? this._ensureIslandMap() : null;
    const seedIsland = islands ? islands[y0 * S + x0] : 0;
    const useIsland = !!(islands && seedIsland);

    const matches = (i) =>
      (!useIsland || islands[i >> 2] === seedIsland) &&
      Math.abs(data[i] - sr) <= tolerance &&
      Math.abs(data[i + 1] - sg) <= tolerance &&
      Math.abs(data[i + 2] - sb) <= tolerance &&
      Math.abs(data[i + 3] - sa) <= tolerance;

    const paint = (i) => {
      if (erase) { data[i + 3] = data[i + 3] * (1 - alpha); return; }
      data[i]     = fr * alpha + data[i]     * (1 - alpha);
      data[i + 1] = fg * alpha + data[i + 1] * (1 - alpha);
      data[i + 2] = fb * alpha + data[i + 2] * (1 - alpha);
      data[i + 3] = 255 * alpha + data[i + 3] * (1 - alpha);
    };

    const visited = new Uint8Array(S * S);
    const stack = [x0, y0];
    while (stack.length) {
      const y = stack.pop();
      const x = stack.pop();
      if (visited[y * S + x] || !matches((y * S + x) * 4)) continue;
      let xl = x;
      while (xl > 0 && !visited[y * S + (xl - 1)] && matches((y * S + (xl - 1)) * 4)) xl--;
      let xr = x;
      while (xr < S - 1 && !visited[y * S + (xr + 1)] && matches((y * S + (xr + 1)) * 4)) xr++;
      for (let sx = xl; sx <= xr; sx++) {
        const pix = y * S + sx;
        visited[pix] = 1;
        paint(pix * 4);
        if (y > 0) stack.push(sx, y - 1);
        if (y < S - 1) stack.push(sx, y + 1);
      }
    }
    c.putImageData(img, 0, 0);
    this.recomposite();
  }

  // Build (once, then cache) a texel -> UV-island id map for the mesh. Two
  // triangles are in the same island when they share an edge in UV space; a UV
  // seam is exactly where 3D-adjacent triangles are *not* adjacent in UV, so the
  // seam separates islands. Triangles are rasterized into the map at their island
  // id (+1; 0 means no triangle covers that texel).
  _ensureIslandMap() {
    if (this._islandMap !== undefined) return this._islandMap;
    const geo = this.mesh.geometry;
    const uvAttr = geo && geo.getAttribute("uv");
    if (!uvAttr) return (this._islandMap = null);

    const S = this.size;
    const index = geo.index ? geo.index.array : null;
    const triCount = (index ? index.length : uvAttr.count) / 3 | 0;
    const corner = (t, k) => (index ? index[t * 3 + k] : t * 3 + k);

    // Union-find over triangles, merging any two that share a quantized UV edge.
    const parent = new Int32Array(triCount);
    for (let i = 0; i < triCount; i++) parent[i] = i;
    const find = (x) => { while (parent[x] !== x) { parent[x] = parent[parent[x]]; x = parent[x]; } return x; };
    const union = (a, b) => { a = find(a); b = find(b); if (a !== b) parent[b] = a; };

    const Q = 4096;
    const vkey = (v) => Math.round(uvAttr.getX(v) * Q) + "," + Math.round(uvAttr.getY(v) * Q);
    const edgeMap = new Map();
    const linkEdge = (t, k1, k2) => {
      const ek = k1 < k2 ? k1 + "|" + k2 : k2 + "|" + k1;
      const prev = edgeMap.get(ek);
      if (prev === undefined) edgeMap.set(ek, t); else union(prev, t);
    };
    for (let t = 0; t < triCount; t++) {
      const ka = vkey(corner(t, 0)), kb = vkey(corner(t, 1)), kc = vkey(corner(t, 2));
      linkEdge(t, ka, kb); linkEdge(t, kb, kc); linkEdge(t, kc, ka);
    }

    const map = new Int32Array(S * S);       // 0 = uncovered
    for (let t = 0; t < triCount; t++) {
      const id = find(t) + 1;
      const a = corner(t, 0), b = corner(t, 1), cc = corner(t, 2);
      rasterTri(map, S, id,
        uvAttr.getX(a), uvAttr.getY(a),
        uvAttr.getX(b), uvAttr.getY(b),
        uvAttr.getX(cc), uvAttr.getY(cc));
    }
    return (this._islandMap = map);
  }

  // ---- undo helpers (raw active-layer pixels) -----------------------------
  snapshotLayer(id) {
    const l = this.layers[this._index(id)] || this.activeLayer();
    return { id: l.id, data: l.ctx.getImageData(0, 0, this.size, this.size) };
  }
  restoreLayer(snap) {
    const l = this.layers[this._index(snap.id)];
    if (!l) return;
    l.ctx.globalCompositeOperation = "source-over";
    l.ctx.globalAlpha = 1;
    l.ctx.putImageData(snap.data, 0, 0);
    this.recomposite();
  }

  reset() {
    // Drop every layer back to a single fresh base.
    this.layers = [];
    const base = this._newLayer("Base");
    fillCanvas(base.canvas, this.baseColor);
    this.layers.push(base);
    this.activeId = base.id;
    this.recomposite();
  }

  dispose() {
    this.texture.dispose();
    if (this.mesh.material) this.mesh.material.dispose();
  }
}

// ---------------------------------------------------------------------------
function makeCanvas(size) {
  const c = document.createElement("canvas");
  c.width = c.height = size;
  return c;
}
// Rasterize a triangle (given in UV space, 0..1) into `map` at value `id`, in the
// same flipped pixel space the painter samples (x = u*S, y = (1-v)*S).
function rasterTri(map, S, id, u0, v0, u1, v1, u2, v2) {
  const x0 = u0 * S, y0 = (1 - v0) * S;
  const x1 = u1 * S, y1 = (1 - v1) * S;
  const x2 = u2 * S, y2 = (1 - v2) * S;
  const minX = Math.max(0, Math.floor(Math.min(x0, x1, x2)));
  const maxX = Math.min(S - 1, Math.ceil(Math.max(x0, x1, x2)));
  const minY = Math.max(0, Math.floor(Math.min(y0, y1, y2)));
  const maxY = Math.min(S - 1, Math.ceil(Math.max(y0, y1, y2)));
  const det = (y1 - y2) * (x0 - x2) + (x2 - x1) * (y0 - y2);
  if (Math.abs(det) < 1e-9) return;              // degenerate in UV
  const inv = 1 / det, eps = -1e-4;
  for (let y = minY; y <= maxY; y++) {
    for (let x = minX; x <= maxX; x++) {
      const px = x + 0.5, py = y + 0.5;
      const a = ((y1 - y2) * (px - x2) + (x2 - x1) * (py - y2)) * inv;
      const b = ((y2 - y0) * (px - x2) + (x0 - x2) * (py - y2)) * inv;
      const g = 1 - a - b;
      if (a >= eps && b >= eps && g >= eps) map[y * S + x] = id;
    }
  }
}
function fillCanvas(canvas, color) {
  const ctx = canvas.getContext("2d");
  ctx.globalCompositeOperation = "source-over";
  ctx.globalAlpha = 1;
  ctx.fillStyle = color;
  ctx.fillRect(0, 0, canvas.width, canvas.height);
}
function clamp01(v) { return Math.max(0, Math.min(1, v)); }
