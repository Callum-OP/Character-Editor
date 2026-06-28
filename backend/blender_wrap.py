"""
Runs INSIDE Blender (headless):  blender --background --python blender_wrap.py -- <args>

Conforms a source model's vertices to a reference model's surface WITHOUT
changing the source topology, so its features and shape keys are kept. The
deformation is computed as a per-vertex displacement via nearest-surface
projection (a BVH tree over the reference), optionally smoothed, then applied
either to the base mesh only or to the base + every shape key (preserving each
key's relative offsets).

Everything is done with bmesh / mathutils math rather than Blender modifiers,
which are crash-prone in headless Blender 5.0.1.

Args after `--`:
    --reference <path>        the target shape to match
    --source    <path>        the model to reshape (may carry shape keys)
    --output    <path>        conformed result (extension picks format)
    --view-output <path>      OBJ of the conformed base, for the web viewer
    --strength  <float 0..1>  how far to move onto the reference (1 = fully)
    --smooth-iters <int>      Laplacian passes over the displacement field
    --shape-keys <preserve|base>
    --align <bbox|none>       auto match size+center, or assume pre-aligned

Emits one JSON line prefixed RETOPO_RESULT: with stats.
"""
import sys
import os
import json

import bpy
import bmesh
import mathutils
from mathutils.bvhtree import BVHTree

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import blender_remesh as br  # reuse import/export/scene helpers


def argv_after_dashes():
    return sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []


def parse_args(args):
    out = {"reference": None, "source": None, "output": None, "view_output": None,
           "strength": 1.0, "smooth_iters": 3, "shape_keys": "preserve", "align": "bbox"}
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--reference": out["reference"] = args[i + 1]; i += 2
        elif a == "--source": out["source"] = args[i + 1]; i += 2
        elif a == "--output": out["output"] = args[i + 1]; i += 2
        elif a == "--view-output": out["view_output"] = args[i + 1]; i += 2
        elif a == "--strength": out["strength"] = float(args[i + 1]); i += 2
        elif a == "--smooth-iters": out["smooth_iters"] = int(args[i + 1]); i += 2
        elif a == "--shape-keys": out["shape_keys"] = args[i + 1].lower(); i += 2
        elif a == "--align": out["align"] = args[i + 1].lower(); i += 2
        else: i += 1
    return out


def import_collect(path):
    """Import a model and return the mesh objects it added to the scene."""
    before = set(bpy.context.scene.objects)
    br.import_model(path)
    return [o for o in bpy.context.scene.objects
            if o not in before and o.type == "MESH"]


def join_objects(objs, prefer_shape_keys=False):
    if len(objs) == 1:
        return objs[0]
    active = objs[0]
    if prefer_shape_keys:
        for o in objs:
            if o.data.shape_keys:
                active = o
                break
    bpy.ops.object.select_all(action="DESELECT")
    for o in objs:
        o.select_set(True)
    bpy.context.view_layer.objects.active = active
    bpy.ops.object.join()
    return bpy.context.view_layer.objects.active


def make_active(obj):
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj


def apply_transforms(obj):
    make_active(obj)
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)


def bbox(obj):
    vs = obj.data.vertices
    lo = mathutils.Vector((min(v.co[i] for v in vs) for i in range(3)))
    hi = mathutils.Vector((max(v.co[i] for v in vs) for i in range(3)))
    return lo, hi


def align_source(src, ref):
    """Translate+scale the source so its bounding box matches the reference's
    (centers coincide, largest dimensions equal). Applies to base + shape keys."""
    s_lo, s_hi = bbox(src)
    r_lo, r_hi = bbox(ref)
    s_center, r_center = (s_lo + s_hi) * 0.5, (r_lo + r_hi) * 0.5
    s_size = max((s_hi - s_lo)[i] for i in range(3)) or 1.0
    r_size = max((r_hi - r_lo)[i] for i in range(3)) or 1.0
    s = r_size / s_size
    M = (mathutils.Matrix.Translation(r_center)
         @ mathutils.Matrix.Diagonal((s, s, s, 1.0))
         @ mathutils.Matrix.Translation(-s_center))
    make_active(src)
    src.matrix_world = M @ src.matrix_world
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)


def reference_bvh(ref):
    bm = bmesh.new()
    bm.from_mesh(ref.data)
    bmesh.ops.triangulate(bm, faces=bm.faces[:])
    bvh = BVHTree.FromBMesh(bm)
    bm.free()
    return bvh


def basis_positions(src):
    sk = src.data.shape_keys
    if sk and sk.key_blocks:
        return [v.co.copy() for v in sk.key_blocks[0].data]
    return [v.co.copy() for v in src.data.vertices]


def build_adjacency(src):
    adj = [[] for _ in range(len(src.data.vertices))]
    for e in src.data.edges:
        a, b = e.vertices
        adj[a].append(b)
        adj[b].append(a)
    return adj


def smooth_field(delta, adj, iterations, factor=0.5):
    for _ in range(max(0, iterations)):
        new = delta[:]
        for i, nb in enumerate(adj):
            if not nb:
                continue
            avg = mathutils.Vector((0.0, 0.0, 0.0))
            for j in nb:
                avg += delta[j]
            avg /= len(nb)
            new[i] = delta[i].lerp(avg, factor)
        delta = new
    return delta


def main():
    cfg = parse_args(argv_after_dashes())
    for k in ("reference", "source", "output"):
        if not cfg[k]:
            print("RETOPO_RESULT:" + json.dumps({"ok": False, "error": "missing " + k}))
            sys.exit(1)

    br.clear_scene()
    ref = join_objects(import_collect(cfg["reference"]))
    apply_transforms(ref)
    src_objs = import_collect(cfg["source"])
    if not src_objs:
        print("RETOPO_RESULT:" + json.dumps({"ok": False, "error": "no mesh in source"}))
        sys.exit(1)
    src = join_objects(src_objs, prefer_shape_keys=True)
    apply_transforms(src)

    notes = []
    if cfg["align"] == "bbox":
        align_source(src, ref)

    bvh = reference_bvh(ref)
    ref_vert_count = len(ref.data.vertices)
    # The BVH is an independent copy, so the reference object is no longer
    # needed. Remove it now so every export below contains only the result.
    try:
        bpy.data.objects.remove(ref, do_unlink=True)
    except Exception:
        pass
    ref = None
    basis = basis_positions(src)
    strength = max(0.0, min(1.0, cfg["strength"]))

    # per-vertex displacement onto the nearest reference surface point
    delta = [mathutils.Vector((0.0, 0.0, 0.0))] * len(basis)
    delta = list(delta)
    misses = 0
    for i, p in enumerate(basis):
        loc, _normal, _idx, dist = bvh.find_nearest(p)
        if loc is None:
            misses += 1
            continue
        target = p.lerp(loc, strength)
        delta[i] = target - p
    if misses:
        notes.append("%d vertices had no nearby reference surface" % misses)

    delta = smooth_field(delta, build_adjacency(src), cfg["smooth_iters"])

    mags = [d.length for d in delta]
    mean_off = (sum(mags) / len(mags)) if mags else 0.0
    max_off = max(mags) if mags else 0.0

    sk = src.data.shape_keys
    has_keys = bool(sk and sk.key_blocks)
    kept_keys = 0
    if cfg["shape_keys"] == "preserve" and has_keys:
        # shift every key by the same per-vertex delta -> relative offsets kept
        for kb in sk.key_blocks:
            for i, d in enumerate(delta):
                kb.data[i].co = kb.data[i].co + d
        kept_keys = len(sk.key_blocks) - 1  # exclude Basis
    else:
        if has_keys:
            make_active(src)
            src.shape_key_clear()
            if cfg["shape_keys"] == "preserve":
                notes.append("source had no usable shape keys to preserve")
        for i, d in enumerate(delta):
            src.data.vertices[i].co = basis[i] + d

    src.data.update()

    # export the conformed base as OBJ for the viewer
    if cfg["view_output"]:
        make_active(src)
        os.makedirs(os.path.dirname(os.path.abspath(cfg["view_output"])), exist_ok=True)
        br.export_model(cfg["view_output"])

    # export the requested deliverable (the reference is already gone)
    make_active(src)
    os.makedirs(os.path.dirname(os.path.abspath(cfg["output"])), exist_ok=True)
    br.export_model(cfg["output"])

    print("RETOPO_RESULT:" + json.dumps({
        "ok": True,
        "shape_key_mode": cfg["shape_keys"],
        "shape_keys": kept_keys,
        "source_vertices": len(basis),
        "reference_vertices": ref_vert_count,
        "mean_offset": mean_off,
        "max_offset": max_off,
        "strength": strength,
        "notes": notes,
        "output": cfg["output"],
    }))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        traceback.print_exc()
        print("RETOPO_RESULT:" + json.dumps({"ok": False, "error": str(e)}))
        sys.exit(1)
