# Character Editor

A web-based toolkit for preparing 3D characters & meshes. The landing page (`/`)
is a hub linking to individual tools; it's built to grow.

**Tools**
1. **Auto Quad Topology** (`/topology.html`) — turn any model into clean quad /
   quad-dominant / triangle topology, with optional hard mirror symmetry.
2. **Shape Match (Wrap)** (`/wrap.html`) — conform your model's vertices to a
   reference shape *without changing your topology*, keeping your features and
   shape keys.
3. **Character Rigger** (`/rig.html`) — auto-detect joints on a humanoid model,
   tweak the markers, and build a skinned Mixamo-style skeleton (fingers + an
   optional ARKit face). Ported from the reference 3D Auto Rigger.
4. **Face Expressions** (`/face.html`) — generate the 52 ARKit facial shape keys
   for a head model, then apply expressions (smile, surprise, blink…) live and
   download the shape-keyed model.
5. **Paint Studio** (`/paint.html`) — paint textures directly onto a model in the
   browser. A layered studio (blend modes, per-layer opacity/visibility) with an
   advanced brush set — paint, airbrush, eraser, soft **smudge** & **blur**,
   bucket fill and eyedropper — plus **mirror symmetry** so one stroke paints
   both sides. Load an existing texture (PNG/JPG/WebP) back in as a layer to keep
   editing it. Painting runs client-side (Three.js); export the texture as PNG or
   the painted model as **GLB / FBX / OBJ / glTF / PLY / STL** — GLB & FBX carry
   the texture embedded, OBJ/glTF come back as a zip with the texture bundled
   (non-GLB formats transcode through the Blender backend). Save back to the
   project as a self-contained GLB.

## How it works

- **Frontend** (`frontend/`) — a small static hub. Each tool is its own page.
  Topology/Wrap/Face share a Three.js viewer; the Rigger uses a 2D marker editor
  (`rig.js`) over a Blender-rendered front view + `<model-viewer>` for preview.
- **Backend** (`backend/`) — a FastAPI server that drives Blender headless:
  `/api/retopo`, `/api/wrap`, `/api/rig/prep`, `/api/rig/build`.
- **Engine** — **Blender headless**. Topology uses **QuadriFlow**; Wrap uses a
  BVH nearest-surface projection; the Rigger/Face tools run the reference
  rigging pipeline (`backend/rigger/pipeline.py`: joint detection → skeleton fit
  → voxel-proxy weight transfer; ARKit-52 shape keys via `face_shapekeys.py`).
  Blender natively imports/exports OBJ, glTF/GLB, FBX, PLY, STL.

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
  app.py             FastAPI server + static hosting (all /api/* endpoints)
  retopo.py          quad engine detection + invocation (Blender / Instant Meshes)
  blender_remesh.py  runs inside Blender: import → QuadriFlow / voxel → export
  wrap.py / blender_wrap.py   Shape Match engine + Blender script
  rig.py             rigger engine layer (writes a job JSON, drives Blender)
  rigger/            reference rigging pipeline, run inside Blender:
                     pipeline.py, landmarks.py, markers.py, bone_naming.py,
                     arkit.py, face_markers.py, face_shapekeys.py, render_views.py
  requirements.txt
frontend/
  index.html         hub landing page (tool cards)
  topology.html / topology.js   Auto Quad Topology tool
  wrap.html / wrap.js           Shape Match (Wrap) tool
  rig.html / rig.js / rig.css   Character Rigger (marker editor)
  face.html / face.js           Face Expressions (morph-target preview)
  viewer.js          shared ModelViewer + true-quad OBJ parser
  vendor/model-viewer.min.js    <model-viewer> for rig result preview
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
- **Landmarks** *(optional)* — place matching points on each model (eye corners,
  nose tip, mouth corners…) to align features precisely. See below.
- **Symmetry** (None / X / Y / Z) — forces the wrapped result to be mirror-
  symmetric, like the topology tool's symmetry option.
- **Wrap strength** (0–100%) — how far vertices snap onto the reference surface.
  100% fully conforms; 0% applies the landmark morph only (no surface snapping).
- **Smoothing passes** — Laplacian relaxation of the displacement field.
- **Shape keys** — *Preserve* (keep every blend shape working; needs FBX/glTF
  output) or *Conform base only* (drop keys).
- **Preserve internal parts** *(mouth/teeth/eyes)* — keep inner geometry that the
  reference doesn't have. See below.
- **Alignment** — *Auto* matches bounding-box size & center; *None* assumes the
  models are pre-aligned. Both models should face the same way.

## Preserving internal geometry (mouth, teeth, eyes)

If your model has geometry *inside* it — an open mouth with teeth/tongue, eye
interiors — and the reference is just an outer surface, plain projection would
drag those inner vertices onto the outer face and flatten them. With this option
on (default), the wrap:

1. classifies each vertex as **internal** if it is *occluded* (a ray cast
   outward along its normal hits the model itself — i.e. it sits in a pocket
   like a mouth cavity or behind an eyelid) **or** has no good reference match;
   everything else is **outer surface**. The occlusion test is reference-
   independent, so a whole mouth interior is caught even when the reference's
   mouth is shallow/closed — which is what stops the mouth getting flattened,
2. conforms only the outer surface to the reference,
3. **carries** each internal vertex by the displacement of the nearest outer-
   surface vertices (a spatial, not edge-based, interpolation — so it works even
   for separate islands like eyeballs or teeth, which would otherwise be left
   behind and poke through).

Because the carry uses the *surface directly covering* each internal part, an
eyeball follows its eyelid and teeth follow the lips — they move and reshape with
the surface in front of them instead of bulging through it. Verified by rendering
a doll head wrapped to a different-shaped reference: the eyeballs sit inside the
sockets and the teeth stay behind the lips, while the outer face matches the
reference. Turning the option off projects everything (the old flattening).

## Landmark-guided matching

Nearest-surface projection alone has no idea an eye should map to an eye. Place a
few matching points to fix that:

1. Load both models — the app sends them to the backend, which returns
   index-preserving OBJ previews (so a picked point maps to an exact vertex).
2. Switch the **View** to a model, turn on **Place markers**, and click matching
   points on each model. Pairs match in order (ref #1 ↔ yours #1).
3. Run the wrap. The landmarks drive a warp *before* surface projection:
   - **4+ pairs** → a **thin-plate spline** (smooth affine + local bending) that
     places and scales features (move the eye to the reference eye, resize the
     mouth to match, etc.),
   - **1–3 pairs** → a global similarity fit (translate + rotate + uniform scale).

Marker conveniences:
- **Auto-detect** — suggests a set of geometric feature points (salient
  extremes) on both models as a starting set; refine them by dragging.
- **Drag** any marker to move it; it snaps to the nearest vertex live.
- **Ctrl+Z** undoes the last marker change (add / move / clear / load).
- **Save / Load** export and re-import landmarks as JSON; markers are also
  auto-saved in the browser per model pair, so reloading restores them.
- **Symmetry**: when on, the symmetry **plane** is shown and you only place
  markers on **one side + the centre line** — each is auto-mirrored to the other
  side (faint *ghost* markers show where), and the backend uses the mirrored set.

Landmarks are stored as **vertex indices**, not coordinates, so there's no
coordinate round-tripping between the browser and Blender's axis conventions.

## How it works internally

`backend/blender_wrap.py` (two phases — `prepare` then wrap):
1. import reference + source, apply transforms, optionally bbox-align the source,
2. if landmarks: warp the source by TPS / similarity so landmarks hit their
   targets,
3. build a **BVH tree** over the reference surface and project each (warped)
   vertex toward the nearest reference point by `strength`,
4. **Laplacian-smooth the displacement field** (not the mesh) to avoid shrinkage,
5. if symmetry: average mirror-paired final positions so the result is symmetric,
6. apply: *preserve* shifts the base **and every shape key** by the same per-
   vertex delta (relative offsets unchanged); *base only* moves the base and
   clears keys.

All geometry math is **bmesh / mathutils / numpy** (Blender bundles numpy) — no
Shrinkwrap/edit-mode operators, which are the robust path in headless Blender.

Verified: self-wrap = 0 offset; similar→small, dissimilar→large offsets; output
always has the **same vertex count as your model** (topology preserved); the
symmetry option raises mirror-symmetry substantially (≈100% on a symmetric base).

### Limitations / roadmap

- Landmark correspondence is manual and as good as the points you place; with no
  landmarks it falls back to pure nearest-surface (which can mismatch features
  where the two shapes differ a lot).
- Symmetry quality depends on the base mesh having a clean mirror correspondence
  (a strongly asymmetric base won't reach a perfect mirror).
- Models should share a rough orientation; auto-align only matches size/center.
- "Preserve shape keys" requires an output format that stores them (GLB/glTF/FBX).

---

# Tool 3 — Character Rigger

Auto-rig a humanoid model. Ported from the reference **3D Auto Rigger** (the
Blender pipeline is reused verbatim under `backend/rigger/`).

**Flow:** drop a model (or "Rig a test figure") → **Rig model** (Blender renders
a front view and auto-detects joints) → drag the joint markers onto the right
spots (Mirror copies left↔right) → **Build rig** → preview and download `.glb` /
`.fbx`. The skeleton is a Mixamo-style Humanoid hierarchy, skinned via a
watertight voxel-proxy weight transfer.

- **Fingers** — switch the overlay to *Hands* for a top-down close-up per hand
  with draggable fingertip markers.
- **Face shape keys** — tick *Facial shape keys (ARKit 52)*, switch to *Face*,
  drag the anchors onto eyes/brows/nose/lips/chin; the output carries the
  armature **and** the 52 ARKit shape keys on one mesh.
- **Standard bones** — emit a figure-app-friendly bone naming so compatible apps
  auto-recognize the rig.
- **Head only** — produces ARKit face shape keys with no skeleton (same as the
  Face Expressions tool's backend).

Verified end-to-end: prep detects all 14 body joints + face markers + front
render; build outputs a skinned GLB **and** FBX.

# Tool 4 — Face Expressions

Generate the **52 ARKit blendshapes** for a head model, then *apply expressions*.

**Flow:** load a head → **Generate expressions** (Blender auto-detects the face
band and builds the shape keys via `backend/rigger/face_shapekeys.py`) → the
result loads in a Three.js viewer that drives the morph targets. Pick a **preset**
(Smile, Surprise, Frown, Blink, Kiss, Angry, Disgust…) — each is a named
combination of ARKit shapes — or fine-tune individual shapes with sliders. The
downloaded GLB carries all 52 shape keys, ready for any ARKit-driven pipeline
(VTubing, game engines, iPhone face capture).

Verified: the generated GLB contains Basis + 52 correctly-named ARKit morph
targets, driven live in the browser.
