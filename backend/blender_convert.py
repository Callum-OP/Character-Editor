"""
Runs INSIDE Blender (headless):  blender --background --python blender_convert.py -- <args>

Format converter: imports a model and re-exports the WHOLE scene unchanged.
Unlike blender_remesh.py's --convert-only path (which joins every mesh into
one object for remeshing), this script preserves the full scene graph —
multiple objects, object names, parenting, armatures/skinning, shape keys,
animations and materials — so a rigged multi-part character survives the
round trip intact.

Texture handling per output format:
  .fbx        path_mode COPY + embed_textures packs images inside the file
  .glb        textures are embedded by the glTF binary format itself
  .gltf       writes .bin + texture images as sidecar files (caller zips)
  .obj        writes .mtl + texture images as sidecar files (caller zips)
  .ply/.stl   geometry only (PLY keeps vertex colors)

Args after the `--` separator:
    --input   <path>          source model (.obj/.glb/.gltf/.fbx/.ply/.stl)
    --output  <path>          converted model (extension decides format)
    --no-embed                FBX: write textures next to the file instead of
                              embedding them (some apps only read textures
                              from sidecar image files)
    --strip-rig               remove armatures, skinning and animations, keep
                              the mesh at rest pose (some apps can't read
                              textures on rigged models)
    --draco                   glTF/GLB only: Draco mesh compression
    --max-texture <px>        scale any texture above <px> down to <px> on its
                              long side (0 = leave textures untouched)
    --snapshot <png>          after exporting, re-import the converted file and
                              render it to this PNG — an honest preview of how
                              other applications will read the result

Emits a single JSON line to stdout prefixed with CONVERT_RESULT: so the
calling process can parse the result reliably.
"""
import sys
import json
import os
import re

import bpy


# --------------------------------------------------------------------------- #
# arg parsing (mirrors blender_remesh.py's hand-rolled parser)
# --------------------------------------------------------------------------- #
def argv_after_dashes():
    if "--" in sys.argv:
        return sys.argv[sys.argv.index("--") + 1:]
    return []


def parse_args(args):
    out = {"input": None, "output": None, "embed": True,
           "strip_rig": False, "draco": False, "max_texture": 0,
           "snapshot": None}
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--input":
            out["input"] = args[i + 1]; i += 2
        elif a == "--output":
            out["output"] = args[i + 1]; i += 2
        elif a == "--no-embed":
            out["embed"] = False; i += 1
        elif a == "--strip-rig":
            out["strip_rig"] = True; i += 1
        elif a == "--draco":
            out["draco"] = True; i += 1
        elif a == "--max-texture":
            out["max_texture"] = int(args[i + 1]); i += 2
        elif a == "--snapshot":
            out["snapshot"] = args[i + 1]; i += 2
        else:
            i += 1
    return out


# --------------------------------------------------------------------------- #
# scene / IO helpers (copied from blender_remesh.py — self-contained)
# --------------------------------------------------------------------------- #
def clear_scene():
    # Remove the default cube/light/camera so they don't leak into the export.
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
        # Blender 4.x uses wm.obj_import; 3.x used import_scene.obj
        if hasattr(bpy.ops.wm, "obj_import"):
            bpy.ops.wm.obj_import(filepath=path)
        else:
            bpy.ops.import_scene.obj(filepath=path)
    elif ext in (".glb", ".gltf"):
        # Don't let the importer "guess the original bind pose": the guess
        # reconstructs twist-bone orientations wrongly on some game rigs
        # (necks/wrists spiral) and leaves the whole scene posed away from
        # rest. Importing the node transforms as the rest pose keeps the
        # character exactly as glTF viewers display it.
        bpy.ops.import_scene.gltf(filepath=path, guess_original_bind_pose=False)
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


def materialize_textures(out_dir):
    """Write packed / in-memory images out as real PNG files in out_dir.

    glTF/GLB (and embedded FBX) imports keep textures packed in memory with no
    file on disk, and exporters using path_mode COPY silently skip images that
    aren't backed by a real file — the model would arrive with no textures.
    Saving them here also re-points each image's filepath at the written file,
    so the exporter references them by name in the same folder (which is
    exactly the layout strict importers expect).
    Returns how many images were written."""
    used, written = set(), 0
    for img in bpy.data.images:
        if img.name in ("Render Result", "Viewer Node"):
            continue
        if not img.size[0]:
            continue
        backed = img.filepath and os.path.isfile(bpy.path.abspath(img.filepath))
        if backed and not img.packed_file and not img.is_dirty:
            continue  # a clean on-disk file — COPY handles it already
        base = re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.splitext(img.name)[0]).strip("._") or "texture"
        name = base + ".png"
        n = 1
        while name.lower() in used:
            n += 1
            name = "%s_%d.png" % (base, n)
        used.add(name.lower())
        try:
            img.file_format = "PNG"
            img.filepath_raw = os.path.join(out_dir, name)
            img.save()
            written += 1
        except Exception:
            pass  # leave the image as-is; better a missing map than a failed export
    return written


def export_model(path, embed=True, draco=False):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".obj":
        # path_mode COPY writes texture images next to the .obj/.mtl so the
        # material is delivered alongside it (bundled as a zip by the caller),
        # instead of the .mtl pointing at a missing file.
        materialize_textures(os.path.dirname(os.path.abspath(path)))
        if hasattr(bpy.ops.wm, "obj_export"):
            bpy.ops.wm.obj_export(filepath=path, export_selected_objects=False, path_mode="COPY")
        else:
            bpy.ops.export_scene.obj(filepath=path, path_mode="COPY")
    elif ext in (".glb", ".gltf"):
        fmt = "GLB" if ext == ".glb" else "GLTF_SEPARATE"
        # Export shape keys as morph targets WITH their own normals so deformed
        # blend shapes shade smoothly instead of faceting in other programs.
        kwargs = dict(
            filepath=path, export_format=fmt, export_normals=True,
            export_morph=True, export_morph_normal=True,
        )
        if draco:
            kwargs["export_draco_mesh_compression_enable"] = True
        bpy.ops.export_scene.gltf(**kwargs)
    elif ext == ".fbx":
        # path_mode COPY + embed_textures packs any image textures *inside* the
        # .fbx, so the model exports as one self-contained file rather than an
        # .fbx that references missing external images. With embed off, the
        # textures are first written as PNGs next to the .fbx and referenced
        # RELATIVE (flat, by filename) — the layout strict importers like Clip
        # Studio Modeler expect. (COPY would stash them in a .fbm subfolder.)
        if embed:
            bpy.ops.export_scene.fbx(filepath=path, path_mode="COPY", embed_textures=True)
        else:
            materialize_textures(os.path.dirname(os.path.abspath(path)))
            bpy.ops.export_scene.fbx(filepath=path, path_mode="RELATIVE", embed_textures=False)
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


def bake_pose_into_rest():
    """Make the armature's rest pose equal its current pose (visuals unchanged).

    glTF files often display a pose that differs from the skin's bind pose;
    Blender imports that as a pose on top of the rest pose. The FBX round trip
    reconstructs such a pose imprecisely and the error compounds down long
    bone chains — on a 400-bone character the face/eye meshes drift several
    millimetres ("slightly mangled"). When the file has no animations the pose
    is purely cosmetic, so baking the deformed shape into the meshes and the
    pose into the rest pose makes rest == pose and the export exact.

    Skipped when there are animations (their curves are relative to the old
    rest pose) or when a skinned mesh has shape keys (baking would drop them).
    Returns True if a pose was baked."""
    from mathutils import Matrix
    scene = bpy.context.scene
    if bpy.data.actions:
        return False
    armatures = [o for o in scene.objects if o.type == "ARMATURE"]
    if not armatures:
        return False

    def is_posed(arm):
        ident = Matrix.Identity(4)
        for pb in arm.pose.bones:
            m = pb.matrix_basis
            if any(abs(m[i][j] - ident[i][j]) > 1e-5 for i in range(4) for j in range(4)):
                return True
        return False

    if not any(is_posed(a) for a in armatures):
        return False
    skinned = [o for o in scene.objects if o.type == "MESH"
               and any(m.type == "ARMATURE" and m.object for m in o.modifiers)]
    if any(o.data.shape_keys for o in skinned):
        return False

    # Replace each skinned mesh with its deformed (posed) shape. Vertex groups
    # live on the object and the armature modifier stays, so the mesh remains
    # skinned — and once the pose becomes the rest pose below, the modifier
    # deforms it by exactly nothing.
    for o in skinned:
        deps = bpy.context.evaluated_depsgraph_get()
        eo = o.evaluated_get(deps)
        baked = bpy.data.meshes.new_from_object(
            eo, preserve_all_data_layers=True, depsgraph=deps)
        old = o.data
        o.data = baked
        if old.users == 0:
            bpy.data.meshes.remove(old)

    for arm in armatures:
        # armature_apply moves the bones, which would drag along any object
        # parented to a bone — pin those back to where they were.
        keep = [(c, c.matrix_world.copy()) for c in scene.objects
                if c.parent == arm and c.parent_type == "BONE"]
        bpy.context.view_layer.objects.active = arm
        bpy.ops.object.mode_set(mode="POSE")
        bpy.ops.pose.select_all(action="SELECT")
        bpy.ops.pose.armature_apply(selected=False)
        bpy.ops.object.mode_set(mode="OBJECT")
        for c, mw in keep:
            c.matrix_world = mw
    return True


def strip_rig():
    """Remove armatures, skinning and animations, keeping meshes at rest pose.

    Some strict importers fail to read textures from an FBX that carries an
    armature, so "mesh only" export drops the rig.
    """
    scene = bpy.context.scene
    # Drop armature modifiers first so meshes keep their rest-pose shape.
    for o in scene.objects:
        if o.type != "MESH":
            continue
        for m in list(o.modifiers):
            if m.type == "ARMATURE":
                o.modifiers.remove(m)
    # Unparent meshes from armatures without moving them (the armature object
    # often carries the up-axis conversion rotation).
    for o in list(scene.objects):
        if o.parent and o.parent.type == "ARMATURE":
            mw = o.matrix_world.copy()
            o.parent = None
            o.matrix_world = mw
    for o in [o for o in scene.objects if o.type == "ARMATURE"]:
        bpy.data.objects.remove(o, do_unlink=True)
    for a in list(bpy.data.actions):
        bpy.data.actions.remove(a)


def cap_textures(max_px):
    """Scale any texture larger than max_px down to max_px on its long side.

    Textures dominate converted file size; this is the only lever that shrinks
    formats like FBX which have no mesh compression. Returns how many images
    were resized."""
    resized = 0
    for img in bpy.data.images:
        if img.name in ("Render Result", "Viewer Node"):
            continue
        w, h = img.size
        if not w or not h or max(w, h) <= max_px:
            continue
        f = max_px / float(max(w, h))
        try:
            img.scale(max(1, int(w * f)), max(1, int(h * f)))
            # Re-pack so exporters embed/copy the scaled pixels, not the
            # original source bytes.
            img.pack()
            resized += 1
        except Exception:
            pass  # leave the original texture untouched on any failure
    return resized


def render_snapshot(model_path, png_path):
    """Re-import the converted file and render it to a PNG.

    Importing what was actually written — instead of rendering the scene that
    was exported — makes the snapshot an honest preview of how other
    applications will reconstruct the file, so export defects (broken pose,
    lost textures) show up in the picture instead of only on the user's next
    import. Returns True when the PNG was written."""
    import math
    from mathutils import Vector
    try:
        clear_scene()
        import_model(model_path)

        # Workbench draws the viewport display color, which importers other
        # than glTF's leave fully opaque — copy each material's real alpha
        # into it so transparent shell meshes don't render as solid white.
        for m in bpy.data.materials:
            if not m.use_nodes:
                continue
            for n in m.node_tree.nodes:
                if n.type == "BSDF_PRINCIPLED":
                    a = n.inputs["Alpha"].default_value
                    if a < 1.0:
                        c = m.diffuse_color
                        m.diffuse_color = (c[0], c[1], c[2], a)

        deps = bpy.context.evaluated_depsgraph_get()
        mins = Vector((1e18, 1e18, 1e18))
        maxs = Vector((-1e18, -1e18, -1e18))
        for o in bpy.context.scene.objects:
            if o.type != "MESH":
                continue
            eo = o.evaluated_get(deps)
            me = eo.to_mesh()
            mw = eo.matrix_world
            for v in me.vertices:
                p = mw @ v.co
                mins = Vector(map(min, mins, p))
                maxs = Vector(map(max, maxs, p))
            eo.to_mesh_clear()
        if mins.x > maxs.x:
            return False
        center = (mins + maxs) / 2
        size = (maxs - mins).length or 1.0

        scene = bpy.context.scene
        scene.render.resolution_x = 512
        scene.render.resolution_y = 640
        scene.render.film_transparent = True
        scene.render.engine = "BLENDER_WORKBENCH"
        scene.display.shading.light = "STUDIO"
        scene.display.shading.color_type = "TEXTURE"

        cam_data = bpy.data.cameras.new("snapshot_cam")
        cam_data.clip_end = size * 20
        cam = bpy.data.objects.new("snapshot_cam", cam_data)
        scene.collection.objects.link(cam)
        scene.camera = cam
        # Slight three-quarter view reads better than a flat front-on shot.
        ang = math.radians(20)
        dist = size * 1.1
        cam.location = center + Vector((math.sin(ang) * dist, -math.cos(ang) * dist, 0))
        cam.rotation_euler = (center - cam.location).to_track_quat("-Z", "Y").to_euler()

        scene.render.filepath = png_path
        bpy.ops.render.render(write_still=True)
        return os.path.isfile(png_path)
    except Exception:
        return False


def scene_stats():
    """What the file actually contains, reported back to the UI."""
    meshes = [o for o in bpy.context.scene.objects if o.type == "MESH"]
    verts = sum(len(o.data.vertices) for o in meshes)
    faces = sum(len(o.data.polygons) for o in meshes)
    materials = {m.name for o in meshes for m in o.data.materials if m}
    images = [im for im in bpy.data.images
              if im.name not in ("Render Result", "Viewer Node") and (im.size[0] or im.packed_file)]
    armatures = [o for o in bpy.context.scene.objects if o.type == "ARMATURE"]
    return {
        "objects": len(bpy.context.scene.objects),
        "meshes": len(meshes),
        "vertices": verts,
        "faces": faces,
        "materials": len(materials),
        "textures": len(images),
        "armatures": len(armatures),
        "animations": len(bpy.data.actions),
    }


def main():
    cfg = parse_args(argv_after_dashes())
    if not cfg["input"] or not cfg["output"]:
        print("CONVERT_RESULT:" + json.dumps({"ok": False, "error": "missing input/output"}))
        sys.exit(1)

    clear_scene()
    import_model(cfg["input"])

    if not bpy.context.scene.objects:
        print("CONVERT_RESULT:" + json.dumps({"ok": False, "error": "no importable content found in input"}))
        sys.exit(1)

    stats = scene_stats()
    notes = []

    if cfg["strip_rig"] and (stats["armatures"] or stats["animations"]):
        strip_rig()
        notes.append("rig & animations stripped — exported mesh only")
        stats = scene_stats()

    out_ext = os.path.splitext(cfg["output"])[1].lower()
    if out_ext in (".ply", ".stl"):
        notes.append("%s carries geometry only — materials/textures are not part of the format"
                     % out_ext.lstrip(".").upper())
    if out_ext in (".ply", ".stl", ".obj") and (stats["armatures"] or stats["animations"]):
        notes.append("rig/animation data cannot be stored in %s and was dropped"
                     % out_ext.lstrip(".").upper())

    draco = cfg["draco"] and out_ext in (".glb", ".gltf")
    if cfg["max_texture"]:
        resized = cap_textures(cfg["max_texture"])
        if resized:
            notes.append("%d texture(s) scaled down to max %dpx" % (resized, cfg["max_texture"]))
        elif not draco:
            notes.append("nothing to compress — all textures already within %dpx" % cfg["max_texture"])
    if draco:
        notes.append("mesh data compressed — some apps can't open compressed glTF/GLB files")
    if out_ext == ".fbx" and not cfg["embed"]:
        notes.append("textures written as separate files next to the .fbx (keep them in the same folder)")
    if out_ext == ".fbx" and bake_pose_into_rest():
        notes.append("static pose baked into the rest pose so the FBX deforms exactly like the original")

    os.makedirs(os.path.dirname(os.path.abspath(cfg["output"])), exist_ok=True)
    export_model(cfg["output"], embed=cfg["embed"], draco=draco)

    snapshot = False
    if cfg["snapshot"]:
        snapshot = render_snapshot(cfg["output"], cfg["snapshot"])

    print("CONVERT_RESULT:" + json.dumps({
        "ok": True,
        "stats": stats,
        "notes": notes,
        "output": cfg["output"],
        "snapshot": snapshot,
    }))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # surface any failure as structured output
        import traceback
        traceback.print_exc()
        print("CONVERT_RESULT:" + json.dumps({"ok": False, "error": str(e)}))
        sys.exit(1)
