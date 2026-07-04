"""
Runs INSIDE Blender (headless):  blender --background --python blender_remesh.py -- <args>

Imports an arbitrary mesh, runs the QuadriFlow remesher to produce all-quad
topology that approximates the original surface, then exports it.

Args after the `--` separator:
    --input   <path>          source model (.obj/.glb/.gltf/.fbx/.ply/.stl)
    --output  <path>          destination file (extension decides format)
    --faces   <int>           target quad count (QuadriFlow target_faces)
    --sym-axis <x|y|z|none>   build hard mirror symmetry across this axis with
                              a clean, splittable center edge loop (default none)
    --topology <quad|quad_dominant|triangle>
                              output topology type (default quad). quad uses
                              QuadriFlow; quad_dominant and triangle use an
                              adaptive voxel remesh that preserves thin features
                              (fingers/toes) far better.
    --no-sharp                optional, disables sharp-edge preservation
    --convert-only            optional, import+export without remeshing
                              (used to re-encode an existing quad mesh)

Emits a single JSON line to stdout prefixed with RETOPO_RESULT: so the
calling process can parse stats reliably.
"""
import sys
import json
import os
import math

import bpy
import bmesh


def argv_after_dashes():
    if "--" in sys.argv:
        return sys.argv[sys.argv.index("--") + 1:]
    return []


def parse_args(args):
    out = {
        "input": None,
        "output": None,
        "faces": 5000,
        "sym_axis": "none",
        "topology": "quad",
        "sharp": True,
        "convert_only": False,
    }
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--input":
            out["input"] = args[i + 1]; i += 2
        elif a == "--output":
            out["output"] = args[i + 1]; i += 2
        elif a == "--faces":
            out["faces"] = int(args[i + 1]); i += 2
        elif a == "--sym-axis":
            out["sym_axis"] = args[i + 1].lower(); i += 2
        elif a == "--topology":
            out["topology"] = args[i + 1].lower(); i += 2
        elif a == "--no-sharp":
            out["sharp"] = False; i += 1
        elif a == "--convert-only":
            out["convert_only"] = True; i += 1
        else:
            i += 1
    return out


def clear_scene():
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete()
    # purge orphan data so repeated runs in one process stay clean
    for block in (bpy.data.meshes, bpy.data.objects):
        for b in list(block):
            try:
                block.remove(b)
            except Exception:
                pass


def import_model(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".obj":
        # Blender 4.x uses wm.obj_import; 3.x used import_scene.obj
        if hasattr(bpy.ops.wm, "obj_import"):
            bpy.ops.wm.obj_import(filepath=path)
        else:
            bpy.ops.import_scene.obj(filepath=path)
    elif ext in (".glb", ".gltf"):
        bpy.ops.import_scene.gltf(filepath=path)
    elif ext == ".fbx":
        bpy.ops.import_scene.fbx(filepath=path)
    elif ext == ".ply":
        if hasattr(bpy.ops.wm, "ply_import"):
            bpy.ops.wm.ply_import(filepath=path)
        else:
            bpy.ops.import_mesh.ply(filepath=path)
    elif ext == ".stl":
        if hasattr(bpy.ops.wm, "stl_import"):
            bpy.ops.wm.stl_import(filepath=path)
        else:
            bpy.ops.import_mesh.stl(filepath=path)
    else:
        raise ValueError("Unsupported input extension: %s" % ext)


def collect_meshes():
    return [o for o in bpy.context.scene.objects if o.type == "MESH"]


def join_meshes(meshes):
    """Join all imported mesh objects into one active object."""
    bpy.ops.object.select_all(action="DESELECT")
    for m in meshes:
        m.select_set(True)
    bpy.context.view_layer.objects.active = meshes[0]
    if len(meshes) > 1:
        bpy.ops.object.join()
    return bpy.context.view_layer.objects.active


def triangle_count(obj):
    obj.data.calc_loop_triangles()
    return len(obj.data.loop_triangles)


def face_count(obj):
    return len(obj.data.polygons)


def quad_count(obj):
    return sum(1 for p in obj.data.polygons if len(p.vertices) == 4)


def clean_mesh(obj):
    """Merge doubles and make normals consistent so QuadriFlow accepts it.

    Exported/scanned meshes commonly have split seam vertices and flipped
    faces, which makes them non-manifold and causes QuadriFlow to cancel.
    """
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.mesh.remove_doubles(threshold=1e-5)
    bpy.ops.mesh.normals_make_consistent(inside=False)
    bpy.ops.object.mode_set(mode="OBJECT")


# --------------------------------------------------------------- hard symmetry
AXIS_INDEX = {"x": 0, "y": 1, "z": 2}

# QuadriFlow is numerically scale-sensitive: it silently cancels on meshes
# whose absolute size is small (a ~0.3-unit head fails; the same mesh scaled
# to ~10 units succeeds). We normalize to this size before remeshing and
# restore the original scale afterward.
QF_WORK_SIZE = 10.0


def make_active(obj):
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj


def axis_extent(obj):
    vs = obj.data.vertices
    if not vs:
        return 1.0
    span = 0.0
    for i in range(3):
        lo = min(v.co[i] for v in vs)
        hi = max(v.co[i] for v in vs)
        span = max(span, hi - lo)
    return span or 1.0


def scale_to(obj, target):
    """Uniformly scale the mesh (about the origin) so its largest dimension
    equals `target`. Scaling about the origin keeps the x/y/z=0 planes fixed,
    so a centered symmetry seam stays exactly on its plane."""
    ext = axis_extent(obj)
    if ext <= 0:
        return
    s = target / ext
    obj.scale = (s, s, s)
    make_active(obj)
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)


def resolution_for(faces, dense):
    """Voxel grid resolution (cells across the largest dimension).

    `dense` (quad-mode intermediate that feeds QuadriFlow) is high and uniform
    so thin features survive before QuadriFlow redistributes. Otherwise the
    resolution targets roughly `faces` output and pairs with adaptivity to stay
    efficient. A high floor is essential: at ~150 cells, fingers/toes vanish."""
    r = math.sqrt(max(64, faces)) * (6.0 if dense else 4.4)
    return int(min(600, max(200 if dense else 120, r)))


def voxel_remesh(obj, resolution, adaptivity=0.0):
    """Rebuild as a guaranteed-manifold mesh. Higher `resolution` preserves
    thin features; `adaptivity` > 0 sheds faces from flat areas while keeping
    detail on curved/thin regions (efficient feature-preserving remesh)."""
    max_dim = axis_extent(obj)
    obj.data.remesh_voxel_size = max_dim / max(16, resolution)
    obj.data.remesh_voxel_adaptivity = adaptivity
    make_active(obj)
    bpy.ops.object.voxel_remesh()


def tris_to_quads(obj):
    """Merge adjacent triangles into quads where shape allows -> quad-dominant
    topology (mostly quads, some triangles) that keeps the underlying detail.

    Uses the bmesh op rather than bpy.ops.mesh.tris_convert_to_quads, which
    crashes (access violation) in headless Blender 5.0."""
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    kw = dict(angle_face_threshold=math.radians(40),
              angle_shape_threshold=math.radians(40))
    try:
        bmesh.ops.join_triangles(bm, faces=bm.faces[:], cmp_seam=False,
                                 cmp_sharp=False, cmp_uvs=False,
                                 cmp_materials=False, **kw)
    except TypeError:  # flag names vary across versions
        bmesh.ops.join_triangles(bm, faces=bm.faces[:], **kw)
    bm.to_mesh(obj.data)
    bm.free()


def triangulate(obj):
    """Convert all faces to triangles (voxel remesh emits a quad/tri mix)."""
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bmesh.ops.triangulate(bm, faces=bm.faces[:])
    bm.to_mesh(obj.data)
    bm.free()


def quadriflow_scaled(obj, faces, sharp, preserve_boundary):
    """Run QuadriFlow with automatic up-scaling and retries. `obj` should
    already be a clean, manifold(-with-boundary) mesh. Returns True on success.
    The original scale is always restored before returning."""
    orig = axis_extent(obj)
    ok = False
    for size in (QF_WORK_SIZE, QF_WORK_SIZE * 5, QF_WORK_SIZE * 20):
        scale_to(obj, size)
        make_active(obj)
        res = bpy.ops.object.quadriflow_remesh(
            target_faces=max(8, int(faces)),
            use_mesh_symmetry=False,
            use_preserve_sharp=sharp,
            use_preserve_boundary=preserve_boundary,
            smooth_normals=True,
            mode="FACES",
        )
        if "FINISHED" in res and quad_count(obj) > 0:
            ok = True
            break
        # CANCELLED leaves the mesh unchanged; retry at a larger scale
    scale_to(obj, orig)  # restore real-world size
    return ok


def center_on_axis(obj, axis):
    """Shift the mesh so its midpoint on `axis` sits at 0 (the split plane)."""
    vs = obj.data.vertices
    lo = min(v.co[axis] for v in vs)
    hi = max(v.co[axis] for v in vs)
    mid = (lo + hi) * 0.5
    for v in vs:
        v.co[axis] -= mid


def bisect_half(obj, axis):
    """Cut at the plane x/y/z = 0 and keep the negative side (open boundary).

    Used only by the quad path, which always feeds a clean QuadriFlow-ready or
    uniform-voxel mesh — the bisect operator is stable on those (it crashes only
    on adaptive-voxel output, which the quad path never produces)."""
    normal = [0.0, 0.0, 0.0]
    normal[axis] = 1.0
    make_active(obj)
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.mesh.bisect(
        plane_co=(0, 0, 0), plane_no=tuple(normal),
        use_fill=False, clear_inner=False, clear_outer=True,
    )
    bpy.ops.object.mode_set(mode="OBJECT")


def snap_boundary_to_plane(obj, axis, tol):
    """Pin the cut-edge vertices exactly onto the plane so the mirror welds
    into a single clean center edge loop."""
    me = obj.data
    bm = bmesh.new()
    bm.from_mesh(me)
    for v in bm.verts:
        if v.is_boundary and abs(v.co[axis]) < tol:
            v.co[axis] = 0.0
    bm.to_mesh(me)
    bm.free()


def mirror_apply(obj, axis, merge_tol):
    make_active(obj)
    mod = obj.modifiers.new(name="Mirror", type="MIRROR")
    mod.use_axis = (axis == 0, axis == 1, axis == 2)
    mod.use_clip = True
    mod.use_mirror_merge = True
    mod.merge_threshold = merge_tol
    bpy.ops.object.modifier_apply(modifier=mod.name)


def weld_and_fix(obj, tol):
    make_active(obj)
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.mesh.remove_doubles(threshold=tol)
    bpy.ops.mesh.normals_make_consistent(inside=False)
    bpy.ops.object.mode_set(mode="OBJECT")


def clean_half(obj):
    """Tidy a freshly-bisected half: drop loose/degenerate geometry and make
    normals consistent, leaving a clean manifold-with-boundary surface."""
    make_active(obj)
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.mesh.delete_loose()
    bpy.ops.mesh.dissolve_degenerate()
    bpy.ops.mesh.normals_make_consistent(inside=False)
    bpy.ops.object.mode_set(mode="OBJECT")


def mirror_and_weld(obj, axis):
    """Snap the cut boundary onto the plane, mirror, and weld the seam into a
    single clean center edge loop. Shared by every topology mode."""
    size = axis_extent(obj)
    snap_boundary_to_plane(obj, axis, tol=size * 0.02)
    mirror_apply(obj, axis, merge_tol=size * 0.001)
    weld_and_fix(obj, tol=size * 0.0005)


def try_symmetric_quad(obj, axis, faces, sharp):
    """Bisect -> QuadriFlow one half -> mirror+weld. Returns True on success.
    On failure the object is left bisected/partial; caller should restore."""
    bisect_half(obj, axis)
    if len(obj.data.polygons) == 0:
        return False
    clean_half(obj)
    # mirroring doubles the faces, so aim for half the target on this side
    if not quadriflow_scaled(obj, faces // 2, sharp, preserve_boundary=True):
        return False
    mirror_and_weld(obj, axis)
    return True


def symmetric_remesh(obj, axis, topology, faces, sharp, notes):
    """Hard-symmetry retopology with a clean, splittable center seam.

    The symmetric base is always built with the QuadriFlow pipeline
    (bisect a clean/uniform-voxel half -> QuadriFlow -> mirror), which is the
    only mirror/bisect path that does NOT crash or hang in headless Blender
    5.0.1 -- it operates on clean meshes, never on adaptive-voxel geometry,
    which corrupts every bisect/mirror op in this build. For triangle/hybrid
    output we convert that symmetric quad base afterward. (The adaptive,
    feature-preserving voxel remesh is available in non-symmetric mode.)"""
    clean_mesh(obj)
    center_on_axis(obj, axis)

    backup = obj.data.copy()
    ok = try_symmetric_quad(obj, axis, faces, sharp)
    if not ok:
        notes.append("symmetry: voxel-remesh fallback (input not QuadriFlow-ready)")
        make_active(obj)
        obj.data = backup.copy()
        voxel_remesh(obj, resolution_for(faces, dense=True), adaptivity=0.0)
        center_on_axis(obj, axis)
        ok = try_symmetric_quad(obj, axis, faces, sharp)
    bpy.data.meshes.remove(backup)
    if not ok:
        raise RuntimeError(
            "Could not build symmetric topology for this model. "
            "Try a lower target count or disable symmetry."
        )

    # Convert the symmetric all-quad base to the requested topology.
    if topology == "triangle":
        triangulate(obj)
        notes.append("symmetric triangles derived from the quad base")
    elif topology == "quad_dominant":
        # the all-quad base already satisfies quad-dominant; nothing to do
        notes.append("symmetric output uses the quad engine "
                     "(use non-symmetric for adaptive feature preservation)")


def nonsym_remesh(obj, topology, faces, sharp, notes):
    """Non-symmetric retopology for any topology mode."""
    clean_mesh(obj)

    if topology == "quad":
        if quadriflow_scaled(obj, faces, sharp, preserve_boundary=True):
            return
        notes.append("voxel-remesh fallback (input was not QuadriFlow-ready)")
        voxel_remesh(obj, resolution_for(faces, dense=True), adaptivity=0.0)
        if quadriflow_scaled(obj, faces, sharp, preserve_boundary=True):
            return
        raise RuntimeError(
            "QuadriFlow could not remesh this model even after cleanup. "
            "Try a different topology mode."
        )

    # triangle / quad_dominant
    voxel_remesh(obj, resolution_for(faces, dense=False), adaptivity=0.6)
    if topology == "quad_dominant":
        tris_to_quads(obj)
    else:  # triangle
        triangulate(obj)


def export_model(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".obj":
        # path_mode COPY writes the texture image next to the .obj/.mtl so a
        # painted model's texture is delivered alongside it (bundled as a zip
        # by the caller), instead of the .mtl pointing at a missing file.
        if hasattr(bpy.ops.wm, "obj_export"):
            bpy.ops.wm.obj_export(filepath=path, export_selected_objects=False, path_mode="COPY")
        else:
            bpy.ops.export_scene.obj(filepath=path, path_mode="COPY")
    elif ext in (".glb", ".gltf"):
        fmt = "GLB" if ext == ".glb" else "GLTF_SEPARATE"
        # Export shape keys as morph targets WITH their own normals so deformed
        # blend shapes shade smoothly instead of faceting in other programs.
        bpy.ops.export_scene.gltf(
            filepath=path, export_format=fmt, export_normals=True,
            export_morph=True, export_morph_normal=True,
        )
    elif ext == ".fbx":
        # path_mode COPY + embed_textures packs any image textures *inside* the
        # .fbx, so a painted model exports as one self-contained file rather than
        # an .fbx that references missing external images.
        bpy.ops.export_scene.fbx(filepath=path, path_mode="COPY", embed_textures=True)
    elif ext == ".ply":
        if hasattr(bpy.ops.wm, "ply_export"):
            bpy.ops.wm.ply_export(filepath=path)
        else:
            bpy.ops.export_mesh.ply(filepath=path)
    elif ext == ".stl":
        if hasattr(bpy.ops.wm, "stl_export"):
            bpy.ops.wm.stl_export(filepath=path)
        else:
            bpy.ops.export_mesh.stl(filepath=path)
    else:
        raise ValueError("Unsupported output extension: %s" % ext)


def main():
    cfg = parse_args(argv_after_dashes())
    if not cfg["input"] or not cfg["output"]:
        print("RETOPO_RESULT:" + json.dumps({"ok": False, "error": "missing input/output"}))
        sys.exit(1)

    clear_scene()
    import_model(cfg["input"])

    meshes = collect_meshes()
    if not meshes:
        print("RETOPO_RESULT:" + json.dumps({"ok": False, "error": "no mesh found in input"}))
        sys.exit(1)

    obj = join_meshes(meshes)

    # Apply transforms so QuadriFlow works in a clean object space.
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)

    src_tris = triangle_count(obj)
    src_faces = face_count(obj)
    src_verts = len(obj.data.vertices)

    notes = []
    sym = cfg["sym_axis"]
    topology = cfg["topology"]
    if topology not in ("quad", "quad_dominant", "triangle"):
        topology = "quad"
    if not cfg["convert_only"]:
        if sym in AXIS_INDEX:
            symmetric_remesh(obj, AXIS_INDEX[sym], topology, cfg["faces"],
                             cfg["sharp"], notes)
        else:
            nonsym_remesh(obj, topology, cfg["faces"], cfg["sharp"], notes)

    out_faces = face_count(obj)
    out_quads = quad_count(obj)
    out_tris = sum(1 for p in obj.data.polygons if len(p.vertices) == 3)
    out_verts = len(obj.data.vertices)

    os.makedirs(os.path.dirname(os.path.abspath(cfg["output"])), exist_ok=True)
    export_model(cfg["output"])

    print("RETOPO_RESULT:" + json.dumps({
        "ok": True,
        "topology": topology,
        "source": {"triangles": src_tris, "faces": src_faces, "vertices": src_verts},
        "result": {
            "faces": out_faces,
            "quads": out_quads,
            "triangles": out_tris,
            "quad_ratio": (out_quads / out_faces) if out_faces else 0,
            "vertices": out_verts,
        },
        "notes": notes,
        "output": cfg["output"],
    }))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # surface any failure as structured output
        import traceback
        traceback.print_exc()
        print("RETOPO_RESULT:" + json.dumps({"ok": False, "error": str(e)}))
        sys.exit(1)
