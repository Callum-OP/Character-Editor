"""
Runs INSIDE Blender (headless):  blender --background --python blender_clean.py -- <args>

Two modes:

  --mode clean   Diagnose and repair a mesh: merge doubles, recalculate normals,
                 fill holes, delete loose geometry, remove degenerate faces,
                 optionally flip. Reports before/after diagnostics.

  --mode lod     Generate one or more decimated level-of-detail meshes from the
                 input with the Decimate (collapse) modifier.

Args after the `--` separator:
    --input   <path>              source model (.obj/.glb/.gltf/.fbx/.ply/.stl)
    --mode    <clean|lod>         (default clean)

  clean mode:
    --output      <path>          repaired model (extension decides format)
    --view-output <path>          OBJ copy for the browser viewer (optional)
    --merge-dist  <float>         merge-by-distance threshold (0 disables)
    --fix-normals                 recalculate normals consistently (outside)
    --fill-holes                  fill boundary holes
    --delete-loose                delete loose verts/edges
    --degenerate                  dissolve zero-area faces / zero-length edges
    --flip                        flip all face normals (after any fix)

  lod mode:
    --out-dir      <path>         folder to write the LOD files into
    --out-ext      <.glb|…>       download format for each level (default .glb)
    --lod-levels   <csv floats>   collapse ratios, e.g. "0.5,0.25,0.125"
    --target-faces <int>          if >0, produce a single level at this face count
    --preserve-uv                 keep UV seams intact (Decimate delimit UV)
    --preserve-boundary           keep open boundaries during collapse

Emits a single JSON line to stdout prefixed with CLEAN_RESULT: so the calling
process can parse the result reliably.
"""
import sys
import json
import os

import bpy
import bmesh


# --------------------------------------------------------------------------- #
# arg parsing (mirrors blender_remesh.py's hand-rolled parser)
# --------------------------------------------------------------------------- #
def argv_after_dashes():
    if "--" in sys.argv:
        return sys.argv[sys.argv.index("--") + 1:]
    return []


def parse_args(args):
    out = {
        "input": None,
        "mode": "clean",
        "output": None,
        "view_output": None,
        "merge_dist": 0.0,
        "fix_normals": False,
        "fill_holes": False,
        "delete_loose": False,
        "degenerate": False,
        "flip": False,
        "out_dir": None,
        "out_ext": ".glb",
        "lod_levels": "0.5,0.25,0.125",
        "target_faces": 0,
        "preserve_uv": False,
        "preserve_boundary": False,
    }
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--input":
            out["input"] = args[i + 1]; i += 2
        elif a == "--mode":
            out["mode"] = args[i + 1].lower(); i += 2
        elif a == "--output":
            out["output"] = args[i + 1]; i += 2
        elif a == "--view-output":
            out["view_output"] = args[i + 1]; i += 2
        elif a == "--merge-dist":
            out["merge_dist"] = float(args[i + 1]); i += 2
        elif a == "--fix-normals":
            out["fix_normals"] = True; i += 1
        elif a == "--fill-holes":
            out["fill_holes"] = True; i += 1
        elif a == "--delete-loose":
            out["delete_loose"] = True; i += 1
        elif a == "--degenerate":
            out["degenerate"] = True; i += 1
        elif a == "--flip":
            out["flip"] = True; i += 1
        elif a == "--out-dir":
            out["out_dir"] = args[i + 1]; i += 2
        elif a == "--out-ext":
            out["out_ext"] = args[i + 1].lower(); i += 2
        elif a == "--lod-levels":
            out["lod_levels"] = args[i + 1]; i += 2
        elif a == "--target-faces":
            out["target_faces"] = int(args[i + 1]); i += 2
        elif a == "--preserve-uv":
            out["preserve_uv"] = True; i += 1
        elif a == "--preserve-boundary":
            out["preserve_boundary"] = True; i += 1
        else:
            i += 1
    return out


# --------------------------------------------------------------------------- #
# scene / IO helpers (copied from blender_remesh.py — self-contained)
# --------------------------------------------------------------------------- #
def clear_scene():
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete()
    for block in (bpy.data.meshes, bpy.data.objects):
        for b in list(block):
            try:
                block.remove(b)
            except Exception:
                pass


def import_model(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".obj":
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
    bpy.ops.object.select_all(action="DESELECT")
    for m in meshes:
        m.select_set(True)
    bpy.context.view_layer.objects.active = meshes[0]
    if len(meshes) > 1:
        bpy.ops.object.join()
    return bpy.context.view_layer.objects.active


def make_active(obj):
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj


def export_model(path):
    ext = os.path.splitext(path)[1].lower()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    if ext == ".obj":
        if hasattr(bpy.ops.wm, "obj_export"):
            bpy.ops.wm.obj_export(filepath=path, export_selected_objects=False, path_mode="COPY")
        else:
            bpy.ops.export_scene.obj(filepath=path, path_mode="COPY")
    elif ext in (".glb", ".gltf"):
        fmt = "GLB" if ext == ".glb" else "GLTF_SEPARATE"
        bpy.ops.export_scene.gltf(
            filepath=path, export_format=fmt, export_normals=True,
            export_morph=True, export_morph_normal=True,
        )
    elif ext == ".fbx":
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


# --------------------------------------------------------------------------- #
# diagnostics
# --------------------------------------------------------------------------- #
def diagnostics(obj):
    """Cheap health check via bmesh. Non-manifold + boundary edges reveal the
    problems (holes, seams, T-junctions) that break downstream tools."""
    me = obj.data
    me.calc_loop_triangles()
    bm = bmesh.new()
    bm.from_mesh(me)
    non_manifold = sum(1 for e in bm.edges if not e.is_manifold)
    boundary = sum(1 for e in bm.edges if len(e.link_faces) == 1)
    loose_verts = sum(1 for v in bm.verts if not v.link_edges)
    bm.free()
    return {
        "vertices": len(me.vertices),
        "faces": len(me.polygons),
        "triangles": len(me.loop_triangles),
        "non_manifold_edges": non_manifold,
        "boundary_edges": boundary,
        "loose_vertices": loose_verts,
    }


# --------------------------------------------------------------------------- #
# clean mode
# --------------------------------------------------------------------------- #
def run_clean(obj, cfg, notes):
    make_active(obj)
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")

    if cfg["merge_dist"] > 0:
        bpy.ops.mesh.remove_doubles(threshold=cfg["merge_dist"])
        bpy.ops.mesh.select_all(action="SELECT")
    if cfg["degenerate"]:
        bpy.ops.mesh.dissolve_degenerate()
        bpy.ops.mesh.select_all(action="SELECT")
    if cfg["delete_loose"]:
        bpy.ops.mesh.delete_loose()
        bpy.ops.mesh.select_all(action="SELECT")
    if cfg["fill_holes"]:
        # sides=0 -> fill holes of any edge count
        try:
            bpy.ops.mesh.fill_holes(sides=0)
        except RuntimeError as e:
            notes.append("fill holes: %s" % e)
        bpy.ops.mesh.select_all(action="SELECT")
    if cfg["fix_normals"]:
        bpy.ops.mesh.normals_make_consistent(inside=False)
    if cfg["flip"]:
        bpy.ops.mesh.select_all(action="SELECT")
        bpy.ops.mesh.flip_normals()

    bpy.ops.object.mode_set(mode="OBJECT")


# --------------------------------------------------------------------------- #
# lod mode
# --------------------------------------------------------------------------- #
def apply_decimate(obj, ratio, preserve_uv, preserve_boundary):
    make_active(obj)
    mod = obj.modifiers.new(name="Decimate", type="DECIMATE")
    mod.decimate_type = "COLLAPSE"
    mod.ratio = max(0.0, min(1.0, ratio))
    mod.use_collapse_triangulate = False
    delimit = set()
    if preserve_uv:
        delimit.add("UV")
    if preserve_boundary:
        # keeping the outer boundary sharp also delimits the collapse there
        delimit.add("SHARP")
    if delimit:
        mod.delimit = delimit
    bpy.ops.object.modifier_apply(modifier=mod.name)


def run_lod(obj, cfg):
    levels_in = []
    if cfg["target_faces"] > 0:
        # Decimate collapse works on triangles and outputs a triangulated mesh,
        # so target the triangle count for a closer hit than the quad count.
        obj.data.calc_loop_triangles()
        orig = len(obj.data.loop_triangles)
        ratio = cfg["target_faces"] / orig if orig else 1.0
        levels_in = [max(0.001, min(1.0, ratio))]
    else:
        for tok in cfg["lod_levels"].split(","):
            tok = tok.strip()
            if not tok:
                continue
            try:
                r = float(tok)
            except ValueError:
                continue
            if 0.0 < r <= 1.0:
                levels_in.append(r)
    if not levels_in:
        raise RuntimeError("no valid LOD ratios given")

    out_dir = cfg["out_dir"]
    out_ext = cfg["out_ext"]
    os.makedirs(out_dir, exist_ok=True)
    backup = obj.data.copy()

    results = []
    for i, ratio in enumerate(levels_in, start=1):
        # restore the pristine mesh for each independent level
        make_active(obj)
        obj.data = backup.copy()
        apply_decimate(obj, ratio, cfg["preserve_uv"], cfg["preserve_boundary"])

        view_name = "lod%d.obj" % i
        export_model(os.path.join(out_dir, view_name))
        file_name = view_name
        if out_ext != ".obj":
            file_name = "lod%d%s" % (i, out_ext)
            export_model(os.path.join(out_dir, file_name))

        results.append({
            "level": i,
            "ratio": round(ratio, 4),
            "faces": len(obj.data.polygons),
            "vertices": len(obj.data.vertices),
            "view": view_name,
            "file": file_name,
        })

    bpy.data.meshes.remove(backup)
    return results


# --------------------------------------------------------------------------- #
def main():
    cfg = parse_args(argv_after_dashes())
    if not cfg["input"]:
        print("CLEAN_RESULT:" + json.dumps({"ok": False, "error": "missing input"}))
        sys.exit(1)

    clear_scene()
    import_model(cfg["input"])
    meshes = collect_meshes()
    if not meshes:
        print("CLEAN_RESULT:" + json.dumps({"ok": False, "error": "no mesh found in input"}))
        sys.exit(1)

    obj = join_meshes(meshes)
    make_active(obj)
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)

    notes = []

    if cfg["mode"] == "lod":
        before = diagnostics(obj)
        levels = run_lod(obj, cfg)
        print("CLEAN_RESULT:" + json.dumps({
            "ok": True,
            "mode": "lod",
            "source": before,
            "levels": levels,
            "notes": notes,
        }))
        return

    # clean mode
    before = diagnostics(obj)
    run_clean(obj, cfg, notes)
    after = diagnostics(obj)

    if cfg["view_output"]:
        export_model(cfg["view_output"])
    if cfg["output"]:
        export_model(cfg["output"])

    print("CLEAN_RESULT:" + json.dumps({
        "ok": True,
        "mode": "clean",
        "before": before,
        "after": after,
        "notes": notes,
        "output": cfg["output"],
    }))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        traceback.print_exc()
        print("CLEAN_RESULT:" + json.dumps({"ok": False, "error": str(e)}))
        sys.exit(1)
