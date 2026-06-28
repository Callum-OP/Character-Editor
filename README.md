# Mesh Hub

A web-based toolkit for cleaning up and reshaping 3D models. The landing page
(`/`) is a hub linking to individual tools; it's built to grow.

**Tools**
1. **Auto Quad Topology** (`/topology.html`) — turn any model into clean quad /
   quad-dominant / triangle topology, with optional hard mirror symmetry.
2. **Shape Match (Wrap)** (`/wrap.html`) — conform your model's vertices to a
   reference shape *without changing your topology*, keeping your features and
   shape keys.

## How it works

- **Frontend** (`frontend/`) — a small static hub. Each tool is its own page
  (`topology.html`, `wrap.html`) sharing a Three.js viewer module
  (`viewer.js`, exposes `ModelViewer` + a true-quad OBJ parser).
- **Backend** (`backend/`) — a FastAPI server that drives Blender headless and
  returns results plus stats (`/api/retopo`, `/api/wrap`).
- **Engine** — **Blender headless**. Topology uses its built-in **QuadriFlow**
  remesher; the wrap tool uses a BVH nearest-surface projection computed in
  bmesh/mathutils. Blender natively imports/exports OBJ, glTF/GLB, FBX, PLY,
  STL. Optionally an **Instant Meshes** binary can be used for OBJ/PLY quads.

---

# Tool 1 — Auto Quad Topology

Produces clean **quad / quad-dominant / triangle** topology approximating the
original surface — the same idea as ZBrush's ZRemesher or Instant Meshes. Pick
a target count, get a retopologized mesh you can inspect (true quad wireframe)
and download.

> Quality note: this generates a *fresh* quad mesh that approximates the
> original. High target counts look near-identical; low counts give a clean
> low-poly quad cage. It is not pixel-perfect surface-preserving retopo — that
> remains a hard, semi-manual problem.

## Prerequisites

1. **Python 3.9+**
2. **Blender 3.3+** (free): https://www.blender.org/download/
   The server auto-detects Blender on PATH, in the standard install folder, or
   via the `BLENDER_PATH` environment variable.
   - *(optional)* Instant Meshes: set `INSTANT_MESHES_PATH` to its binary.

## Setup & run

```bash
cd backend
python -m venv .venv
# Windows PowerShell:
.venv\Scripts\Activate.ps1
# macOS/Linux:
# source .venv/bin/activate

pip install -r requirements.txt
uvicorn app:app --reload --port 8000
```

Then open http://localhost:8000

If Blender isn't on your PATH, point the server at it, e.g. on Windows:

```powershell
$env:BLENDER_PATH = "C:\Program Files\Blender Foundation\Blender 4.2\blender.exe"
uvicorn app:app --port 8000
```

## Formats

| Format | Import | Export | Keeps quads on export |
|--------|:------:|:------:|:---------------------:|
| OBJ    | ✓ | ✓ | ✓ |
| FBX    | ✓ | ✓ | ✓ |
| glTF/GLB | ✓ | ✓ | ✗ (triangulated by spec) |
| PLY    | ✓ | ✓ | ✓ (polygon PLY) |
| STL    | ✓ | ✓ | ✗ (triangle-only format) |

The on-screen result always uses OBJ so the quad wireframe is accurate; your
download is re-encoded from that exact mesh into the format you choose.

## Project layout

```
backend/
  app.py             FastAPI server + static hosting (/api/retopo, /api/wrap)
  retopo.py          engine detection + invocation (Blender / Instant Meshes)
  blender_remesh.py  runs inside Blender: import → QuadriFlow / voxel → export
  wrap.py            wrap engine layer (drives Blender)
  blender_wrap.py    runs inside Blender: BVH nearest-surface conform
  requirements.txt
frontend/
  index.html         hub landing page (tool cards)
  topology.html / topology.js   Auto Quad Topology tool
  wrap.html / wrap.js           Shape Match (Wrap) tool
  viewer.js          shared ModelViewer + true-quad OBJ parser
  style.css
```

## Topology modes

Pick how the surface is rebuilt — this is the main lever for **preserving thin
features** like fingers and toes:

| Mode | Engine | Output | Thin-feature preservation | Best for |
|------|--------|--------|---------------------------|----------|
| **Quads** | QuadriFlow | 100% quads | weakest (uniform redistribution; raise count) | subdivision, animation cages |
| **Quad-dominant (hybrid)** | adaptive voxel + tris→quads | mostly quads, some tris | strong | balanced / general use |
| **Triangles** | adaptive voxel + triangulate | 100% triangles | strongest | organic shapes, 3D printing |

Why this matters: pure-quad remeshing redistributes faces uniformly, so a body
budgeted at N quads can't spare enough faces for fingers and they merge away.
The hybrid/triangle modes use an **adaptive voxel remesh** that concentrates
faces on curved/thin regions and thins out flat areas — so fingers and toes
survive at the same overall budget. (QuadriFlow is also numerically
scale-sensitive, so the app normalizes the mesh to ~10 units before remeshing
and restores the real scale afterward.)

**Symmetry + topology:** every mode can be made symmetric, but the symmetric
base is always built with the QuadriFlow pipeline (the only bisect/mirror path
that is stable in headless Blender 5.0.1 — the adaptive-voxel geometry crashes
or hangs every bisect/mirror operation in that build). Triangle/hybrid symmetric
output is therefore derived from that quad base (triangle = the quad mesh
triangulated). For **maximum thin-feature preservation, use the triangle or
hybrid mode without symmetry**, or raise the target count when you need both.
Verified: symmetric output is 100% mirror-symmetric with a clean center seam in
all three modes.

## Tuning results

- **Target face count** — the main control. Raise it for tighter shape
  fidelity. Exact in Quad mode; *approximate* in hybrid/triangle (adaptive
  density trades uniformity for feature retention).
- **Keep sharp edges** — preserves hard surface creases (good for hard-surface
  models, off for organic).
- **Symmetry (X / Y / Z / None)** — builds **hard mirror symmetry** with a
  clean, splittable center edge loop (default: X). It centers the model on the
  chosen axis, bisects it, retopologizes one half, then mirrors and welds the
  seam — so both halves have *identical* topology and there's a continuous edge
  loop sitting exactly on the plane that you can split or use as a mirror seam.
  Verified output: 100% of vertices have a mirror partner and the seam loop
  lies precisely on the plane. (Hard symmetry requires Blender, not Instant
  Meshes.) The viewer's green **split plane** shows where the seam lands.

### How symmetry works internally

`backend/blender_remesh.py :: symmetric_remesh()`:
1. clean (merge doubles + consistent normals) and center on the axis,
2. bisect at the plane, keep one half (open boundary at the cut),
3. QuadriFlow that half with `use_preserve_boundary` (target = half the count),
4. snap the boundary vertices exactly onto the plane,
5. apply a Mirror modifier with clip + merge, then weld the seam.

If QuadriFlow rejects the half, it falls back to a voxel remesh of the original
and retries (reported in the result `notes`).

---

# Tool 2 — Shape Match (Wrap)

Deform **your** model's vertices so its surface matches a **reference** model,
*without changing your topology*. Your edge flow, UVs' vertex order, features
(eyes/mouth) and **shape keys** are kept — the vertices just slide onto the
reference shape, so you don't have to move them by hand.

Typical use: you have a rigged head you like (with blend shapes), and a target
head shape you want it to match. Wrap yours to the target and keep rigging it.

## Inputs & options

- **Reference model** — the target shape to match.
- **Your model** — the mesh to reshape (may carry shape keys).
- **Wrap strength** (0–100%) — how far vertices move onto the reference surface.
  100% fully conforms; lower values blend toward the original.
- **Smoothing passes** — Laplacian relaxation of the displacement field to
  remove projection noise.
- **Shape keys** — *Preserve* (keep every blend shape working on the reshaped
  mesh; needs FBX/glTF output) or *Conform base only* (drop keys).
- **Alignment** — *Auto* matches bounding-box size & center (tuned for
  heads/faces); *None* assumes the models are already aligned. Both models
  should face the same way.

## How it works internally

`backend/blender_wrap.py`:
1. import reference + source, apply transforms, optionally bbox-align the source,
2. build a **BVH tree** over the (triangulated) reference surface,
3. for each source vertex, find the nearest point on the reference surface and
   compute a displacement `delta = lerp(vertex, nearest, strength)`,
4. **Laplacian-smooth the displacement field** (not the mesh) to avoid shrinkage,
5. apply: *preserve* shifts the base **and every shape key** by the same per-
   vertex delta (so each key's relative offset is unchanged); *base only* moves
   the base and clears keys.

All geometry math is done in **bmesh / mathutils** (no Shrinkwrap/edit-mode
operators), which is the robust path in headless Blender 5.0.1.

Output stats report mean/max vertex offset and how many shape keys were kept.
Verified: wrapping a model to **itself** yields exactly 0 offset; wrapping to a
similar shape gives small offsets; to a very different shape, large offsets —
and the output always has the **same vertex count as your model** (topology
preserved).

### Limitations / roadmap

- **Automatic correspondence only (v1).** Nearest-surface projection has no
  semantic awareness, so where two models differ a lot near a feature (an eye,
  the inside of a mouth), vertices can project to the wrong place. **Landmark-
  guided wrapping** (place matching points on both models) is the planned next
  upgrade for accurate feature alignment.
- Models should share a rough orientation; auto-align only matches size/center.
- "Preserve shape keys" requires an output format that stores them (GLB/glTF/FBX).
