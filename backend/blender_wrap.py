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
    --landmarks <path>        optional JSON {"ref":[idx...], "src":[idx...]} of
                              matching vertex indices used to guide the warp
                              (thin-plate-spline / similarity fit before
                              projection) so features line up
    --sym-axis <none|x|y|z>   symmetrize the displacement across this axis

Landmarks are VERTEX INDICES into the imported meshes (the same indexing the
prepare step exposes to the UI), so no fragile coordinate round-tripping.

Emits one JSON line prefixed RETOPO_RESULT: with stats.
"""
import sys
import os
import json

import bpy
import bmesh
import mathutils
import numpy as np
from mathutils.bvhtree import BVHTree
from mathutils.kdtree import KDTree

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import blender_remesh as br  # reuse import/export/scene helpers


def argv_after_dashes():
    return sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []


def parse_args(args):
    out = {"reference": None, "source": None, "output": None, "view_output": None,
           "strength": 1.0, "smooth_iters": 3, "shape_keys": "preserve", "align": "bbox",
           "landmarks": None, "sym_axis": "none", "keep_internal": True,
           "dilate": 0,
           "prepare": False, "ref_out": None, "src_out": None}
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--project-all": out["keep_internal"] = False; i += 1
        elif a == "--dilate": out["dilate"] = int(args[i + 1]); i += 2
        elif a == "--prepare": out["prepare"] = True; i += 1
        elif a == "--ref-out": out["ref_out"] = args[i + 1]; i += 2
        elif a == "--src-out": out["src_out"] = args[i + 1]; i += 2
        elif a == "--reference": out["reference"] = args[i + 1]; i += 2
        elif a == "--source": out["source"] = args[i + 1]; i += 2
        elif a == "--output": out["output"] = args[i + 1]; i += 2
        elif a == "--view-output": out["view_output"] = args[i + 1]; i += 2
        elif a == "--strength": out["strength"] = float(args[i + 1]); i += 2
        elif a == "--smooth-iters": out["smooth_iters"] = int(args[i + 1]); i += 2
        elif a == "--shape-keys": out["shape_keys"] = args[i + 1].lower(); i += 2
        elif a == "--align": out["align"] = args[i + 1].lower(); i += 2
        elif a == "--landmarks": out["landmarks"] = args[i + 1]; i += 2
        elif a == "--sym-axis": out["sym_axis"] = args[i + 1].lower(); i += 2
        else: i += 1
    return out


AXIS_INDEX = {"x": 0, "y": 1, "z": 2}


def umeyama(src, dst):
    """Best-fit similarity (rotation+uniform scale+translation) mapping src->dst.
    src, dst: (n,3) numpy arrays. Returns a warp(points)->points function."""
    src_mean, dst_mean = src.mean(0), dst.mean(0)
    sc, dc = src - src_mean, dst - dst_mean
    cov = (dc.T @ sc) / len(src)
    U, S, Vt = np.linalg.svd(cov)
    d = np.sign(np.linalg.det(U @ Vt))
    R = U @ np.diag([1.0, 1.0, d]) @ Vt
    var = (sc ** 2).sum() / len(src)
    scale = float((S * [1.0, 1.0, d]).sum() / var) if var > 1e-12 else 1.0
    t = dst_mean - scale * (R @ src_mean)
    return lambda pts: (scale * (R @ pts.T).T) + t


def tps_warp(src, dst):
    """Thin-plate-spline warp exactly mapping src landmarks -> dst landmarks
    (affine + smooth local bending). Returns a warp(points)->points function."""
    n = len(src)
    K = np.sqrt(((src[:, None, :] - src[None, :, :]) ** 2).sum(-1))  # |pi-pj|
    P = np.hstack([np.ones((n, 1)), src])
    L = np.zeros((n + 4, n + 4))
    L[:n, :n] = K + np.eye(n) * 1e-6   # light regularization
    L[:n, n:] = P
    L[n:, :n] = P.T
    Y = np.vstack([dst, np.zeros((4, 3))])
    W = np.linalg.solve(L, Y)
    w, a = W[:n], W[n:]

    def warp(pts):
        dd = np.sqrt(((pts[:, None, :] - src[None, :, :]) ** 2).sum(-1))
        return dd @ w + np.hstack([np.ones((len(pts), 1)), pts]) @ a
    return warp


def symmetrize_positions(target, basis, axis):
    """Force the wrapped result to be mirror-symmetric across `axis`.

    Vertex correspondence (which vertex mirrors which) is found from the *base*
    mesh; the paired target positions are then averaged with each other's
    mirror, so the output is exactly symmetric even if the base wasn't.
    Returns symmetrized target positions."""
    n = len(basis)
    bx = [b[axis] for b in basis]
    bcenter = (min(bx) + max(bx)) * 0.5
    kd = KDTree(n)
    for i, b in enumerate(basis):
        kd.insert(b, i)
    kd.balance()
    partner = [0] * n
    for i, b in enumerate(basis):
        mb = b.copy()
        mb[axis] = 2.0 * bcenter - mb[axis]
        _co, j, _d = kd.find(mb)
        partner[i] = j
    tx = [t[axis] for t in target]
    tcenter = (min(tx) + max(tx)) * 0.5
    out = [t.copy() for t in target]
    for i in range(n):
        j = partner[i]
        mtj = target[j].copy()
        mtj[axis] = 2.0 * tcenter - mtj[axis]   # mirror partner across centre
        avg = (target[i] + mtj) * 0.5
        out[i] = avg
        mavg = avg.copy()
        mavg[axis] = 2.0 * tcenter - mavg[axis]
        out[j] = mavg
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


def bake_matrix(obj, M):
    """Bake a 4x4 transform into the mesh data and reset the object transform.

    Unlike bpy.ops.object.transform_apply, this also transforms EVERY shape key
    (and works headless) — the operator drops/refuses shape keys, which is why
    blend shapes were vanishing. We transform the base mesh vertices AND all
    shape keys, keeping mesh.vertices == Basis: the exporter writes
    mesh.vertices as the base, so it must stay in sync or the output is
    identical to the input."""
    for v in obj.data.vertices:
        v.co = M @ v.co
    sk = obj.data.shape_keys
    if sk and sk.key_blocks:
        for kb in sk.key_blocks:
            for d in kb.data:
                d.co = M @ d.co
    obj.matrix_basis = mathutils.Matrix.Identity(4)
    obj.data.update()


def apply_transforms(obj):
    bake_matrix(obj, obj.matrix_world.copy())


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
    bake_matrix(src, M)  # transforms basis + all shape keys (keeps blend shapes)


def reference_bvh(ref):
    bm = bmesh.new()
    bm.from_mesh(ref.data)
    bmesh.ops.triangulate(bm, faces=bm.faces[:])
    bvh = BVHTree.FromBMesh(bm)
    bm.free()
    return bvh


def feature_suggestions(obj):
    """Geometric salient points: the most extreme vertex in +/-X, +/-Y, +/-Z and
    the most protruding point relative to the centroid. Orientation-consistent
    between two similarly-posed models, so they pair up — a starting set the user
    refines (not ML eye/mouth detection)."""
    vs = obj.data.vertices
    if not vs:
        return []
    idx = []
    for axis in range(3):
        idx.append(min(range(len(vs)), key=lambda i: vs[i].co[axis]))
        idx.append(max(range(len(vs)), key=lambda i: vs[i].co[axis]))
    c = mathutils.Vector((0, 0, 0))
    for v in vs:
        c += v.co
    c /= len(vs)
    idx.append(max(range(len(vs)), key=lambda i: (vs[i].co - c).length))
    # de-dup while preserving order
    seen, out = set(), []
    for i in idx:
        if i not in seen:
            seen.add(i); out.append(i)
    return out


def nearest_index(positions, kd, query):
    _co, j, _d = kd.find(query)
    return j


def mirror_landmarks(ref, basis, ref_idx, src_idx, axis):
    """Expand landmark pairs with their mirror across `axis` so a one-sided set
    becomes symmetric. Centre-line points (mirror == self) are left single."""
    ref_co = [v.co for v in ref.data.vertices]
    rkd = KDTree(len(ref_co))
    for i, p in enumerate(ref_co):
        rkd.insert(p, i)
    rkd.balance()
    skd = KDTree(len(basis))
    for i, p in enumerate(basis):
        skd.insert(p, i)
    skd.balance()
    rx = [p[axis] for p in ref_co]
    sx = [p[axis] for p in basis]
    rc = (min(rx) + max(rx)) * 0.5
    sc = (min(sx) + max(sx)) * 0.5
    out_r, out_s = list(ref_idx), list(src_idx)
    seen = set(zip(ref_idx, src_idx))
    for ri, si in zip(ref_idx, src_idx):
        rp = ref_co[ri].copy(); rp[axis] = 2 * rc - rp[axis]
        sp = basis[si].copy();  sp[axis] = 2 * sc - sp[axis]
        rj = nearest_index(ref_co, rkd, rp)
        sj = nearest_index(basis, skd, sp)
        if rj == ri and sj == si:
            continue  # centre-line point
        if (rj, sj) in seen:
            continue
        seen.add((rj, sj)); out_r.append(rj); out_s.append(sj)
    return out_r, out_s


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


def prepare(cfg):
    """Import both models and re-export each as an OBJ that preserves vertex
    order, so the UI can place landmarks as vertex indices that map exactly to
    the meshes the wrap step will use."""
    br.clear_scene()
    ref = join_objects(import_collect(cfg["reference"]))
    apply_transforms(ref)
    ref_n = len(ref.data.vertices)
    ref_auto = feature_suggestions(ref)
    make_active(ref)
    os.makedirs(os.path.dirname(os.path.abspath(cfg["ref_out"])), exist_ok=True)
    br.export_model(cfg["ref_out"])
    bpy.data.objects.remove(ref, do_unlink=True)

    src = join_objects(import_collect(cfg["source"]), prefer_shape_keys=True)
    apply_transforms(src)
    src_n = len(src.data.vertices)
    src_keys = (len(src.data.shape_keys.key_blocks) - 1) if src.data.shape_keys else 0
    src_auto = feature_suggestions(src)
    make_active(src)
    br.export_model(cfg["src_out"])
    # pair the two suggestion sets to equal length (same extreme order)
    m = min(len(ref_auto), len(src_auto))
    print("RETOPO_RESULT:" + json.dumps({
        "ok": True, "reference_vertices": ref_n, "source_vertices": src_n,
        "source_shape_keys": src_keys,
        "auto_landmarks": {"ref": ref_auto[:m], "src": src_auto[:m]},
    }))


def main():
    cfg = parse_args(argv_after_dashes())
    if cfg["prepare"]:
        if not (cfg["reference"] and cfg["source"] and cfg["ref_out"] and cfg["src_out"]):
            print("RETOPO_RESULT:" + json.dumps({"ok": False, "error": "prepare needs reference/source/ref-out/src-out"}))
            sys.exit(1)
        prepare(cfg)
        return

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
    _rl, _rh = bbox(ref)
    ref_size = max((_rh - _rl)[i] for i in range(3)) or 1.0
    basis = basis_positions(src)
    strength = max(0.0, min(1.0, cfg["strength"]))

    # ---- optional landmark-guided pre-warp ---------------------------------
    n_landmarks = 0
    warp = None
    if cfg["landmarks"]:
        with open(cfg["landmarks"]) as f:
            lm = json.load(f)
        ref_idx, src_idx = lm.get("ref", []), lm.get("src", [])
        pairs = min(len(ref_idx), len(src_idx))
        ref_idx, src_idx = ref_idx[:pairs], src_idx[:pairs]
        # with symmetry on, mirror each landmark so a one-sided set is symmetric
        if cfg["sym_axis"] in AXIS_INDEX and pairs:
            ref_idx, src_idx = mirror_landmarks(ref, basis, ref_idx, src_idx,
                                                AXIS_INDEX[cfg["sym_axis"]])
        ref_co = [ref.data.vertices[k].co.copy() for k in ref_idx
                  if 0 <= k < ref_vert_count]
        src_co = [basis[k] for k in src_idx if 0 <= k < len(basis)]
        n_landmarks = min(len(ref_co), len(src_co))
        if n_landmarks >= 1:
            S = np.array([[c.x, c.y, c.z] for c in src_co[:n_landmarks]])
            D = np.array([[c.x, c.y, c.z] for c in ref_co[:n_landmarks]])
            warp = tps_warp(S, D) if n_landmarks >= 4 else umeyama(S, D)
            if n_landmarks < 4:
                notes.append("only %d landmark pairs — used a global "
                             "similarity fit (4+ enables local shaping)" % n_landmarks)

    # warped base positions (landmark morph), then nearest-surface projection
    if warp is not None:
        warped_np = warp(np.array([[p.x, p.y, p.z] for p in basis]))
        warped = [mathutils.Vector((float(r[0]), float(r[1]), float(r[2]))) for r in warped_np]
    else:
        warped = basis

    # The BVH is an independent copy; drop the reference object so exports below
    # contain only the result.
    try:
        bpy.data.objects.remove(ref, do_unlink=True)
    except Exception:
        pass
    ref = None

    adj = build_adjacency(src)

    # Source vertex normals (basis) — used to tell outer surface from internal
    # geometry (mouth bag, teeth, eye interiors), which face inward.
    nbm = bmesh.new()
    nbm.from_mesh(src.data)
    nbm.normal_update()
    nbm.verts.ensure_lookup_table()
    src_norms = [nbm.verts[i].normal.copy() for i in range(len(basis))]
    nbm.free()

    # ---- classify: conform (outer surface) vs carry (internal) -------------
    # A vertex is INTERNAL (carried, not projected) if either:
    #   (a) it is occluded — a ray cast outward along its normal hits the model
    #       itself, i.e. it sits in a pocket (mouth cavity, behind an eyelid), or
    #   (b) it has no good reference match (inward-facing / far).
    # The occlusion test is reference-independent, so it reliably catches a whole
    # mouth interior even when the reference's mouth is shallow/closed — which is
    # what stops the mouth getting flattened onto the reference.
    conform = [True] * len(basis)
    n_internal = 0
    if cfg["keep_internal"] and strength > 0.0:
        # BVH of the source surface itself, for the occlusion ray test
        sbm = bmesh.new()
        sbm.from_mesh(src.data)
        bmesh.ops.triangulate(sbm, faces=sbm.faces[:])
        src_bvh = BVHTree.FromBMesh(sbm)
        sbm.free()
        _slo, _shi = bbox(src)
        src_size = max((_shi - _slo)[i] for i in range(3)) or 1.0
        eps = src_size * 1e-4
        occl_thr = src_size * 0.35
        dist_thr = ref_size * 0.12
        for i, p in enumerate(basis):
            n = src_norms[i]
            hit, _hn, _hi, hdist = src_bvh.ray_cast(p + n * eps, n)
            occluded = hit is not None and hdist is not None and hdist < occl_thr
            wp = warped[i]
            loc, nrm, _idx, dist = bvh.find_nearest(wp)
            ref_ok = (loc is not None and dist <= dist_thr
                      and nrm is not None and n.dot(nrm) > 0.1)
            ok = ref_ok and not occluded
            conform[i] = ok
            if not ok:
                n_internal += 1
        if len(basis) - n_internal < max(8, int(0.05 * len(basis))):
            conform = [True] * len(basis); n_internal = 0
            notes.append("internal detection found little outer surface; projected all")
        elif n_internal and cfg["dilate"] > 0:
            # Optionally dilate the carried region a few edge-rings into the
            # surface so openings (mouth/eye rims) are preserved. Off by default:
            # it decouples internal parts (eyeballs) from the surface in front of
            # them, which can make them bulge through; with dilate=0 internal
            # parts follow their covering surface.
            carry_set = set(i for i in range(len(basis)) if not conform[i])
            for _ in range(cfg["dilate"]):
                ring = set()
                for i in carry_set:
                    ring.update(adj[i])
                carry_set |= ring
            for i in carry_set:
                conform[i] = False
    conform_idx = [i for i in range(len(basis)) if conform[i]]
    carry_idx = [i for i in range(len(basis)) if not conform[i]]
    n_carry = len(carry_idx)
    if n_internal:
        extra = n_carry - n_internal
        msg = "kept %d internal vertices (carried with the covering surface)" % n_internal
        if extra:
            msg += " + %d surrounding (dilated)" % extra
        notes.append(msg)

    # ---- displacement field d, added to the warped positions ---------------
    d = [mathutils.Vector((0.0, 0.0, 0.0)) for _ in basis]
    misses = 0
    if strength > 0.0:
        # conform vertices: project onto the reference (with strength)
        for i in conform_idx:
            loc, _n, _idx, _dd = bvh.find_nearest(warped[i])
            if loc is None:
                misses += 1
            else:
                d[i] = (loc - warped[i]) * strength
        # smooth the conform displacement and re-project (even, accurate fit)
        for _ in range(cfg["smooth_iters"]):
            nd = list(d)
            for i in conform_idx:
                nb = [j for j in adj[i] if conform[j]]
                if nb:
                    avg = mathutils.Vector((0.0, 0.0, 0.0))
                    for j in nb:
                        avg += d[j]
                    avg /= len(nb)
                    nd[i] = d[i].lerp(avg, 0.5)
            d = nd
            for i in conform_idx:
                loc, _n, _idx, _dd = bvh.find_nearest(warped[i] + d[i])
                if loc is not None:
                    d[i] = (loc - warped[i]) * strength
        # Carry internal parts by SPATIAL interpolation of the surface
        # displacement: each internal vertex takes the distance-weighted
        # displacement of the nearest outer-surface vertices. Unlike edge
        # diffusion this works across disconnected islands (eyeballs, teeth),
        # which otherwise stay put while the surface moves and poke through.
        if carry_idx and conform_idx:
            ckd = KDTree(len(conform_idx))
            for n, i in enumerate(conform_idx):
                ckd.insert(warped[i], n)
            ckd.balance()
            K = min(8, len(conform_idx))
            for i in carry_idx:
                acc = mathutils.Vector((0.0, 0.0, 0.0))
                wsum = 0.0
                for _co, n, dist in ckd.find_n(warped[i], K):
                    w = 1.0 / (dist + 1e-6)
                    acc += d[conform_idx[n]] * w
                    wsum += w
                if wsum > 0.0:
                    d[i] = acc / wsum
    if misses:
        notes.append("%d surface vertices had no nearby reference" % misses)

    final = [warped[i] + d[i] for i in range(len(basis))]

    if cfg["sym_axis"] in AXIS_INDEX:
        final = symmetrize_positions(final, basis, AXIS_INDEX[cfg["sym_axis"]])

    # accuracy readout over the conformed (outer) surface only
    res_d = []
    for i in conform_idx:
        loc, _n, _i2, dd = bvh.find_nearest(final[i])
        if loc is not None:
            res_d.append(dd)
    surface_residual = (sum(res_d) / len(res_d)) if res_d else 0.0
    residual_pct = 100.0 * surface_residual / ref_size

    # how far the carried (internal) parts sit off the reference surface (depth),
    # and how many ended up on the OUTER side (poking through) — should be low.
    car_d = []
    outside = 0
    for i in carry_idx:
        loc, nrm, _i2, dd = bvh.find_nearest(final[i])
        if loc is not None:
            car_d.append(dd)
            if nrm is not None and (final[i] - loc).dot(nrm) > ref_size * 0.002:
                outside += 1
    internal_depth_pct = 100.0 * (sum(car_d) / len(car_d)) / ref_size if car_d else 0.0
    internal_outside_pct = 100.0 * outside / len(carry_idx) if carry_idx else 0.0

    delta = [final[i] - basis[i] for i in range(len(basis))]

    mags = [d.length for d in delta]
    mean_off = (sum(mags) / len(mags)) if mags else 0.0
    max_off = max(mags) if mags else 0.0

    sk = src.data.shape_keys
    has_keys = bool(sk and sk.key_blocks)
    kept_keys = 0
    if cfg["shape_keys"] == "preserve" and has_keys:
        # shift every key by the same per-vertex delta -> relative offsets kept,
        # and move the base mesh vertices too (the exporter writes those as the
        # base; without this the file comes back unchanged).
        for kb in sk.key_blocks:
            for i, d in enumerate(delta):
                kb.data[i].co = kb.data[i].co + d
        for i, d in enumerate(delta):
            src.data.vertices[i].co = src.data.vertices[i].co + d
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
        "surface_residual": surface_residual,
        "residual_pct": residual_pct,
        "internal_kept": n_internal,
        "internal_depth_pct": internal_depth_pct,
        "internal_outside_pct": internal_outside_pct,
        "strength": strength,
        "landmarks": n_landmarks,
        "sym_axis": cfg["sym_axis"],
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
