"""
3DAutoRigger — Blender headless rigging pipeline.

Run via:
    blender --background --python backend/pipeline.py -- <job.json>

The job JSON looks like:
    {
        "input":  "C:/path/to/model.glb",   // optional; omit to use a generated test mesh
        "output": "C:/path/to/rigged.glb",
        "target_height": 1.8                  // metres; mesh is scaled to this
    }

Stages: import -> normalize -> build skeleton -> skin -> export.

This is the v1 engine. The skeleton is fit heuristically from the mesh
bounding box using standard human body proportions. The `build_skeleton`
function is the single seam where a smarter rigger (RigNet / SMPL-fit /
landmark detection) plugs in later — everything up- and down-stream stays
the same.
"""

import bpy
import json
import sys
import os
from mathutils import Vector

# Make sibling modules importable when run via `blender --python backend/pipeline.py`.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import landmarks
import bone_naming
import markers as markers_mod
import face_markers
import face_shapekeys


# --------------------------------------------------------------------------- #
# Logging — prefixed lines so the Electron side can parse progress.
# --------------------------------------------------------------------------- #
def log(stage, msg):
    print(f"[RIG] {stage}: {msg}", flush=True)


def fail(msg):
    print(f"[RIG] ERROR: {msg}", flush=True)
    sys.exit(1)


# --------------------------------------------------------------------------- #
# Scene helpers
# --------------------------------------------------------------------------- #
def reset_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)


def import_model(path):
    ext = os.path.splitext(path)[1].lower()
    log("import", f"loading {os.path.basename(path)} ({ext})")
    if ext == ".glb" or ext == ".gltf":
        bpy.ops.import_scene.gltf(filepath=path)
    elif ext == ".obj":
        bpy.ops.wm.obj_import(filepath=path)
    elif ext == ".fbx":
        bpy.ops.import_scene.fbx(filepath=path)
    else:
        fail(f"unsupported input format: {ext}")
    # Drop anything that isn't mesh geometry (existing rigs, lights, cameras,
    # empties) so it can't leak into the rigged output.
    meshes = []
    for o in list(bpy.context.scene.objects):
        if o.type == "MESH":
            meshes.append(o)
        else:
            bpy.data.objects.remove(o, do_unlink=True)
    if not meshes:
        fail("no mesh found in the imported file")
    # Strip any prior rigging so re-rigging starts clean: an existing armature
    # modifier + leftover vertex groups make bone-heat weighting fail outright.
    for m in meshes:
        for mod in list(m.modifiers):
            if mod.type == "ARMATURE":
                m.modifiers.remove(mod)
        if m.parent:
            mw = m.matrix_world.copy()
            m.parent = None
            m.matrix_world = mw
        m.vertex_groups.clear()
        if m.data.shape_keys:
            m.shape_key_clear()
    return meshes


def _add_box(name, center, dims):
    bpy.ops.mesh.primitive_cube_add(size=1, location=center)
    o = bpy.context.active_object
    o.name = name
    o.scale = dims
    bpy.ops.object.transform_apply(scale=True)
    return o


def make_test_human():
    """Build a blocky humanoid (T-pose) so landmark detection has real features."""
    log("import", "no input given — generating a test humanoid mesh")
    parts = []
    # head + neck
    parts.append(_add_box("head",  (0, 0, 1.63), (0.20, 0.22, 0.24)))
    parts.append(_add_box("neck",  (0, 0, 1.50), (0.10, 0.10, 0.10)))
    # torso + pelvis
    parts.append(_add_box("torso", (0, 0, 1.20), (0.34, 0.20, 0.50)))
    parts.append(_add_box("pelvis", (0, 0, 0.93), (0.30, 0.18, 0.16)))
    # arms (T-pose: horizontal along X) + hands
    parts.append(_add_box("arm_L",  (0.44, 0, 1.40), (0.52, 0.09, 0.09)))
    parts.append(_add_box("arm_R", (-0.44, 0, 1.40), (0.52, 0.09, 0.09)))
    parts.append(_add_box("hand_L",  (0.74, 0, 1.40), (0.10, 0.12, 0.12)))
    parts.append(_add_box("hand_R", (-0.74, 0, 1.40), (0.10, 0.12, 0.12)))
    # legs (separated so the crotch is detectable) + feet
    parts.append(_add_box("leg_L",  (0.10, 0, 0.45), (0.13, 0.15, 0.90)))
    parts.append(_add_box("leg_R", (-0.10, 0, 0.45), (0.13, 0.15, 0.90)))
    parts.append(_add_box("foot_L",  (0.10, -0.08, 0.03), (0.12, 0.26, 0.06)))
    parts.append(_add_box("foot_R", (-0.10, -0.08, 0.03), (0.12, 0.26, 0.06)))

    # Voxel-remesh the overlapping boxes into ONE connected, deformable mesh.
    # (Separate boxes can't bend at joints — they have no geometry there.)
    bpy.ops.object.select_all(action="DESELECT")
    for p in parts:
        p.select_set(True)
    bpy.context.view_layer.objects.active = parts[0]
    bpy.ops.object.join()
    body = bpy.context.view_layer.objects.active
    mod = body.modifiers.new("Remesh", "REMESH")
    mod.mode = "VOXEL"
    mod.voxel_size = 0.04
    bpy.ops.object.modifier_apply(modifier=mod.name)
    bpy.ops.object.shade_smooth()
    log("import", f"remeshed test figure -> {len(body.data.vertices)} verts")
    return [body]


def join_meshes(meshes):
    """Combine all mesh objects into one so we bind a single skin."""
    bpy.ops.object.select_all(action="DESELECT")
    for m in meshes:
        m.select_set(True)
    bpy.context.view_layer.objects.active = meshes[0]
    if len(meshes) > 1:
        bpy.ops.object.join()
    obj = bpy.context.view_layer.objects.active
    obj.name = "RigTarget"
    return obj


# --------------------------------------------------------------------------- #
# Normalize: center on origin (feet at Z=0) and scale to target height.
# --------------------------------------------------------------------------- #
def normalize(obj, target_height):
    log("normalize", "centering and scaling to target height")
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)

    # Apply any existing transform so bbox is in world space.
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)

    coords = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    min_z = min(c.z for c in coords)
    max_z = max(c.z for c in coords)
    height = max_z - min_z
    if height <= 1e-6:
        fail("degenerate mesh height")

    scale = target_height / height
    obj.scale = (scale, scale, scale)
    bpy.ops.object.transform_apply(scale=True)

    # Re-evaluate bbox after scaling, then drop feet to Z=0 and center XY.
    coords = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    min_x = min(c.x for c in coords); max_x = max(c.x for c in coords)
    min_y = min(c.y for c in coords); max_y = max(c.y for c in coords)
    min_z = min(c.z for c in coords)
    obj.location.x -= (min_x + max_x) / 2.0
    obj.location.y -= (min_y + max_y) / 2.0
    obj.location.z -= min_z
    bpy.ops.object.transform_apply(location=True)
    log("normalize", f"height now ~{target_height:.2f}m, feet at Z=0")
    return obj


# --------------------------------------------------------------------------- #
# Build skeleton from detected landmarks.
#
# Bone hierarchy matches the common Mixamo / Unreal "Humanoid" layout so the
# output is engine-friendly. The landmark dict (see landmarks.py) provides the
# joint Z heights and X magnitudes; this function only assembles bones from it,
# so swapping the rigger (RigNet / SMPL-fit) means replacing detect_landmarks,
# not this code.
# --------------------------------------------------------------------------- #
def _bone(edit_bones, name, head, tail, parent=None, connected=False):
    b = edit_bones.new(name)
    b.head = head
    b.tail = tail
    if parent is not None:
        b.parent = parent
        b.use_connect = connected
    return b


def clean_mesh(obj):
    """Make the mesh watertight-ish for clean skinning + broad app compatibility.

    Open holes (non-manifold boundary edges) and stray geometry are a common
    cause of figure-posing apps crashing when they finalize a character. We merge
    duplicate verts, fill holes, and make normals consistent.
    """
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    before = len(obj.data.vertices)
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    # 1.5mm merge fully seals this mesh (0 non-manifold) while keeping detail.
    bpy.ops.mesh.remove_doubles(threshold=0.0015)
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.mesh.fill_holes(sides=0)
    bpy.ops.mesh.normals_make_consistent(inside=False)
    bpy.ops.object.mode_set(mode="OBJECT")
    log("clean", f"watertight pass ({before} verts -> {len(obj.data.vertices)})")


def sample_points(obj):
    """World-space point cloud for analysis, densified if the mesh is low-poly.

    Cross-section detection samples points by height, so it needs points
    distributed over surfaces — not just at a box's corners. For low-poly
    meshes we temporarily apply a simple subdivision to fill them in.
    """
    nverts = len(obj.data.vertices)
    if nverts >= 5000:
        return [obj.matrix_world @ v.co for v in obj.data.vertices]

    mod = obj.modifiers.new("AnalyzeSubdiv", "SUBSURF")
    mod.subdivision_type = "SIMPLE"
    mod.levels = 3
    deps = bpy.context.evaluated_depsgraph_get()
    ev = obj.evaluated_get(deps)
    eme = ev.to_mesh()
    pts = [obj.matrix_world @ v.co for v in eme.vertices]
    ev.to_mesh_clear()
    obj.modifiers.remove(mod)
    log("landmark", f"densified {nverts} -> {len(pts)} sample points")
    return pts


# Finger layout: (internal name, lateral offset in spread-units, length scale).
# Four fingers fan across the hand width; the thumb is handled separately.
_FINGERS = [
    ("Index",  1.5, 0.95),
    ("Middle", 0.5, 1.00),
    ("Ring",  -0.5, 0.92),
    ("Pinky", -1.5, 0.78),
]
_SEG_FRACS = (0.45, 0.33, 0.22)   # three phalanges as fractions of finger length


def _heuristic_tips(wrist, tip, knuckle):
    """Fallback fingertip targets when the hand mesh has no detectable fan
    (mittens / very low-poly): fan 4 fingers across the hand width + a thumb."""
    hand_vec = tip - wrist
    hand_len = hand_vec.length or 0.01
    hdir = hand_vec.normalized()
    spread = Vector((0, 1, 0))        # fan across front-back (Y)
    unit = hand_len * 0.14
    finger_zone = hand_len * 0.45
    tips = {}
    for fname, yoff, lmul in _FINGERS:
        base = knuckle + spread * (yoff * unit)
        tips[fname] = base + hdir * (finger_zone * lmul)
    thumb_dir = (hdir + spread * -0.7).normalized()
    pos = wrist + hand_vec * 0.25 + spread * (-2.0 * unit)
    tips["Thumb"] = pos + thumb_dir * (finger_zone * 0.8)
    return tips


def _build_fingers_from_tips(eb, side, parent, wrist, knuckle, tips):
    """Build 5 fingers x 3 joints aiming each finger from the knuckle line to its
    fingertip target `tips[name]` (world Vectors). Finger bases are spread 85% of
    the way to each tip so they stay separated (near-parallel) rather than all
    starting at the single knuckle point. The thumb roots nearer the wrist.

    This is the single seam the hand-marker editor drives: pass user-placed
    fingertip targets and the fingers land exactly on the real fingers.
    """
    four = ["Index", "Middle", "Ring", "Pinky"]
    tipvs = [tips[f] for f in four]
    mean_tip = sum(tipvs, Vector((0, 0, 0))) / 4.0
    for fname, tipv in zip(four, tipvs):
        base = knuckle + (tipv - mean_tip) * 0.85    # bases spread, stay separated
        seg = tipv - base
        prev, pos = parent, base.copy()
        for j in range(3):
            nxt = pos + seg * _SEG_FRACS[j]
            prev = _bone(eb, f"{fname}{j + 1}_{side}", pos, nxt, prev, j > 0)
            pos = nxt

    tipv = tips["Thumb"]
    base = wrist.lerp(tipv, 0.25)
    seg = tipv - base
    prev, pos = parent, base.copy()
    for j in range(3):
        nxt = pos + seg * _SEG_FRACS[j]
        prev = _bone(eb, f"Thumb{j + 1}_{side}", pos, nxt, prev, j > 0)
        pos = nxt


_FINGER_NAMES = ["Thumb", "Index", "Middle", "Ring", "Pinky"]

# Hand/finger bones are scaled toward the wrist by this factor so they sit
# right on the model (full-size bones read as oversized/chunky in figure apps).
HAND_SCALE = 0.8
# Palm (Hand bone) length as a fraction of wrist->fingertip; fingers start here.
PALM_FRAC = 0.5


def _detect_hand_span(obj, wrist, tip):
    """Measure the finger fan from the RAW hand mesh (the sample cloud is
    subsurf-smoothed, which merges fingers, so we read obj.data.vertices).

    Low-poly hands are too sparse to isolate each finger reliably, but the
    overall fan is measurable: returns where the fingers spread to (lo..hi along
    a perpendicular axis) and how far they reach (tip_t along the hand). None on
    mitten/flat hands so we fall back to the heuristic fan.
    """
    hand_vec = tip - wrist
    hand_len = hand_vec.length
    if hand_len < 1e-4:
        return None
    axis = hand_vec.normalized()
    mw = obj.matrix_world

    fr = []  # finger region: (t_along_hand, perp_vec)
    for v in obj.data.vertices:
        d = (mw @ v.co) - wrist
        t = d.dot(axis)
        if t <= hand_len * 0.5 or t > hand_len * 1.8:
            continue
        perp = d - axis * t
        if perp.length < hand_len * 2.0:
            fr.append((t, perp))
    if len(fr) < 10:
        return None

    ys = [s[1].y for s in fr]
    zs = [s[1].z for s in fr]
    use_y = (max(ys) - min(ys)) >= (max(zs) - min(zs))
    vals = ys if use_y else zs
    spread = max(vals) - min(vals)
    if spread < hand_len * 0.35:          # no real fan -> mitten -> fallback
        return None
    return {
        "axis": axis, "perp": Vector((0, 1, 0)) if use_y else Vector((0, 0, 1)),
        "lo": min(vals), "hi": max(vals),
        "tip_t": max(s[0] for s in fr), "hand_len": hand_len, "wrist": wrist,
        # (fan position, reach) per vert, so each finger gets its OWN length
        # (the fingers vary in length; a single tip_t overshoots the short ones).
        "samples": [((s[1].y if use_y else s[1].z), s[0]) for s in fr],
    }


def finger_tips(obj, side, wrist, tip):
    """Proposed fingertip targets {Index,Middle,Ring,Pinky,Thumb: world Vector}.

    Derived from the detected finger fan (per-finger reach) when the hand mesh
    has one, else a heuristic fan. This is what the hand-marker editor shows for
    the user to nudge, and the default the rig uses when un-edited.
    """
    knuckle = wrist.lerp(tip, PALM_FRAC * HAND_SCALE)
    span = _detect_hand_span(obj, wrist, tip)
    if not span:
        return _heuristic_tips(wrist, tip, knuckle)
    axis, perp, w = span["axis"], span["perp"], span["wrist"]
    hand_len, lo, hi, tip_t = span["hand_len"], span["lo"], span["hi"], span["tip_t"]
    width = hi - lo
    samples = span.get("samples", [])

    def reach_at(s_pos):
        near = [t for (pc, t) in samples if abs(pc - s_pos) < max(width * 0.18, 0.006)]
        return max(near) if near else tip_t

    tips = {}
    for i, fname in enumerate(["Index", "Middle", "Ring", "Pinky"]):
        s_tip = lo + width * (i + 0.5) / 4.0
        tips[fname] = w + (axis * reach_at(s_tip) + perp * s_tip) * HAND_SCALE
    tips["Thumb"] = w + (axis * (hand_len * 0.7) + perp * (lo - width * 0.5)) * HAND_SCALE
    return tips


# --------------------------------------------------------------------------- #
# Hand close-up view — top-down render + fingertip markers for the editor.
#
# Fingers fan in the depth (Y) plane in a T-pose, so the front/side views can't
# separate them. A top-down (look down -Z) close-up of each hand shows the
# fingers spread out; the user drags 5 fingertip markers onto the real tips and
# the rig builds finger bones straight to them (see _build_fingers_from_tips).
# --------------------------------------------------------------------------- #
HAND_RES = 512


def _hand_anchor(obj, lm, side):
    """Wrist + fingertip-reference points for one hand, in world space."""
    sign = 1 if side == "L" else -1
    wx, hx = sign * lm["wrist_x"], sign * lm["hand_x"]
    vw = [obj.matrix_world @ v.co for v in obj.data.vertices]
    span = abs(hx - wx) or 0.05

    def ycenter(z, x):
        ys = [p.y for p in vw if abs(p.z - z) < 0.06 and abs(p.x - x) < 0.13]
        return (min(ys) + max(ys)) / 2.0 if ys else 0.0

    wrist = Vector((wx, ycenter(lm["wrist_z"], wx), lm["wrist_z"]))
    tip = Vector((hx, wrist.y, lm["hand_z"]))
    return wrist, tip, span


def hand_calib(obj, lm, side):
    """Top-down ortho mapping for one hand: image px <-> world (X, Y)."""
    wrist, tip, span = _hand_anchor(obj, lm, side)
    return {
        "res": HAND_RES, "ortho": max(span * 2.8, 0.06),
        "cx": (wrist.x + tip.x) / 2.0, "cy": (wrist.y + tip.y) / 2.0,
        "hand_z": lm["hand_z"], "side": side,
    }


def hand_world_to_px(x, y, c):
    return [(x - c["cx"]) / c["ortho"] * c["res"] + c["res"] / 2.0,
            c["res"] / 2.0 - (y - c["cy"]) / c["ortho"] * c["res"]]


def hand_px_to_world(px, py, c):
    return Vector(((px / c["res"] - 0.5) * c["ortho"] + c["cx"],
                   (0.5 - py / c["res"]) * c["ortho"] + c["cy"], c["hand_z"]))


def propose_finger_markers(obj, lm, side, calib):
    """{finger: [px, py]} fingertip markers for the hand editor (proposed)."""
    wrist, tip, _ = _hand_anchor(obj, lm, side)
    tips = finger_tips(obj, side, wrist, tip)
    return {f: hand_world_to_px(v.x, v.y, calib) for f, v in tips.items()}


def render_hand_view(obj, lm, side, out_png, calib):
    """Render a top-down close-up of one hand for the fingertip editor."""
    import math
    scene = bpy.context.scene
    cam_data = bpy.data.cameras.new("HandCam")
    cam_data.type = "ORTHO"
    cam_data.ortho_scale = calib["ortho"]
    cam = bpy.data.objects.new("HandCam", cam_data)
    scene.collection.objects.link(cam)
    cam.location = (calib["cx"], calib["cy"], lm["hand_z"] + 2.0)
    cam.rotation_euler = (0.0, 0.0, 0.0)        # look straight down -Z
    scene.camera = cam

    if not any(o.type == "LIGHT" for o in scene.objects):
        d = bpy.data.lights.new("Sun", "SUN")
        d.energy = 3.0
        light = bpy.data.objects.new("Sun", d)
        scene.collection.objects.link(light)
        light.rotation_euler = (0.0, 0.0, 0.0)
    scene.render.engine = "BLENDER_WORKBENCH"
    sh = scene.display.shading
    sh.light = "STUDIO"
    sh.color_type = "SINGLE"
    sh.single_color = (0.82, 0.82, 0.85)
    scene.render.resolution_x = HAND_RES
    scene.render.resolution_y = HAND_RES
    scene.render.filepath = out_png
    os.makedirs(os.path.dirname(os.path.abspath(out_png)), exist_ok=True)
    bpy.ops.render.render(write_still=True)
    bpy.data.objects.remove(cam, do_unlink=True)


def build_skeleton(obj, lm, fingers=False, standard=False, finger_tips_override=None):
    """Create a humanoid armature from a detected landmark dict `lm`.

    fingers=False builds a single Hand bone per side and no finger bones — a
    clean ~22-bone humanoid. Finger bones are opt-in.

    finger_tips_override = {"L": {finger: Vector}, "R": {...}} supplies user-
    placed fingertip targets from the hand editor (else auto-detected).

    standard=True builds the "standard bone" SHAPE so the rig auto-recognises in
    figure-posing apps (used with bone_naming="standard"): exactly ONE shoulder
    bone per side (= leftshoulder_bb_) instead of our Clavicle+Shoulder pair, and
    no head-direction bone (it has no standard-bone equivalent). Everything else
    maps 1:1 to the spec names in bone_naming.STANDARD_BB.
    """
    log("skeleton", "assembling armature from landmarks"
                    + ("" if fingers else " (no fingers)")
                    + (" (standard shape)" if standard else ""))

    arm = bpy.data.armatures.new("AutoRig")
    rig = bpy.data.objects.new("AutoRig", arm)
    bpy.context.collection.objects.link(rig)
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode="EDIT")
    eb = arm.edit_bones

    # The mesh isn't centered on Y=0 (toes/face stick forward, so the bounding
    # box centers ahead of the body core). Bones built at Y=0 then sit in front
    # of the body. `by(z, x)` returns the body's front-back (Y) center at a given
    # height/side so each bone lands inside the mesh.
    vw = [obj.matrix_world @ v.co for v in obj.data.vertices]

    def by(z, x=0.0, ztol=0.06, xtol=0.13):
        ys = [p.y for p in vw if abs(p.z - z) < ztol and abs(p.x - x) < xtol]
        return (min(ys) + max(ys)) / 2.0 if ys else 0.0

    # HIPS is the character root — every other bone descends from it. Figure apps
    # want the hip bone as the root; an extra floor/root bone above it is a known
    # cause of "register as character" failures, so we don't add one.
    #
    # Spine chain (centered on X=0). The lower torso is split in two (belly bone),
    # and the topmost spine bone (Spine2) reaches up to the neck — there's no
    # separate chest bone. hips -> spine(belly low) -> spine1(belly high) ->
    # spine2(abdomen->neck). Each joint at the body's Y for its height.
    spine_mid = (lm["spine_z"] + lm["spine1_z"]) / 2.0
    neck_base = lm["neck_z"] - 0.10 * (lm["head_top_z"] - lm["neck_z"])   # neck sits a bit lower
    b1 = (spine_mid + neck_base) / 2.0
    hips   = _bone(eb, "Hips",   (0, by(lm["hips_z"]),  lm["hips_z"]),  (0, by(lm["spine_z"]), lm["spine_z"]),  None,   False)
    spine  = _bone(eb, "Spine",  (0, by(lm["spine_z"]), lm["spine_z"]), (0, by(spine_mid),     spine_mid),      hips,   True)
    spine1 = _bone(eb, "Spine1", (0, by(spine_mid),     spine_mid),     (0, by(b1),            b1),             spine,  True)
    spine2 = _bone(eb, "Spine2", (0, by(b1),            b1),            (0, by(neck_base),     neck_base),      spine1, True)

    # One neck + a big head (neck lower & bigger; head ~70% of neck->crown).
    nh = neck_base + 0.30 * (lm["head_top_z"] - neck_base)
    head_h = lm["head_top_z"] - nh
    neck = _bone(eb, "Neck", (0, by(neck_base), neck_base), (0, by(nh), nh), spine2, True)
    head = _bone(eb, "Head", (0, by(nh), nh), (0, by(lm["head_top_z"]), lm["head_top_z"]), neck, True)
    # Forward-pointing face joint (the head-facing-direction target some figure
    # apps ask you to assign as part of the head region). Faces -Y (front).
    if not standard:   # the standard-bone skeleton has no head-direction bone
        face_z = nh + 0.5 * head_h
        hfy = by(face_z)
        _bone(eb, "HeadFace", (0, hfy, face_z), (0, hfy - 0.9 * head_h, face_z),
              head, False)

    # Arms + legs, mirrored L/R. side sign: +X = left.
    for side, sign in (("L", 1), ("R", -1)):
        shx, ex, wx, hx = (sign * lm["shoulder_x"], sign * lm["elbow_x"],
                           sign * lm["wrist_x"], sign * lm["hand_x"])
        shz = lm["shoulder_z"]
        if standard:
            # Standard-bone skeleton: ONE shoulder bone (clavicle, chest -> joint);
            # the arm parents straight to it (no second Shoulder bone).
            clav = _bone(eb, f"Clavicle_{side}",
                         (0, by(lm["chest_z"]), lm["chest_z"]),
                         (shx, by(shz, shx), shz), spine2, False)
            arm_parent = clav
        else:
            # Clavicle (near the chest) -> Shoulder (at the joint): two bones.
            clav = _bone(eb, f"Clavicle_{side}",
                         (0, by(lm["chest_z"]), lm["chest_z"]),
                         (shx * 0.5, by(shz, shx * 0.5), shz), spine2, False)
            arm_parent = _bone(eb, f"Shoulder_{side}",
                               (shx * 0.5, by(shz, shx * 0.5), shz),
                               (shx, by(shz, shx), shz), clav, True)
        ua = _bone(eb, f"UpperArm_{side}",
                   (shx, by(shz, shx), shz),
                   (ex, by(lm["elbow_z"], ex), lm["elbow_z"]), arm_parent, True)
        la = _bone(eb, f"LowerArm_{side}",
                   (ex, by(lm["elbow_z"], ex), lm["elbow_z"]),
                   (wx, by(lm["wrist_z"], wx), lm["wrist_z"]), ua, True)
        wrist_p = Vector((wx, by(lm["wrist_z"], wx), lm["wrist_z"]))
        # Tip shares the wrist's depth (Y) so the hand axis follows the arm. If it
        # used the fingertips' own Y-center, the axis would tilt into the finger
        # fan and the finger bones would land skewed off the real fingers.
        tip_p = Vector((hx, wrist_p.y, lm["hand_z"]))
        if fingers:
            # Hand spans wrist -> knuckles; fingers start AT the knuckle so they
            # don't overlap the hand bone. Fingertip targets come from the hand
            # editor (finger_tips_override) when the user placed them, else from
            # auto-detection.
            knuckle = wrist_p.lerp(tip_p, PALM_FRAC * HAND_SCALE)
            hand = _bone(eb, f"Hand_{side}", wrist_p, knuckle, la, True)
            tips = (finger_tips_override or {}).get(side)
            if tips:
                log("skeleton", f"using edited fingertips on {side} hand")
            else:
                tips = finger_tips(obj, side, wrist_p, tip_p)
            _build_fingers_from_tips(eb, side, hand, wrist_p, knuckle, tips)
        else:
            # Single hand bone (wrist -> fingertips) — no finger bones.
            _bone(eb, f"Hand_{side}", wrist_p, tip_p, la, True)

        lx, kx, ax = sign * lm["hip_x"], sign * lm["knee_x"], sign * lm["ankle_x"]
        ul = _bone(eb, f"UpperLeg_{side}",
                   (lx, by(lm["hips_z"], lx), lm["hips_z"]),
                   (kx, by(lm["knee_z"], kx), lm["knee_z"]), hips, False)
        ll = _bone(eb, f"LowerLeg_{side}",
                   (kx, by(lm["knee_z"], kx), lm["knee_z"]),
                   (ax, by(lm["ankle_z"], ax), lm["ankle_z"]), ul, True)
        # Foot scaled ~15% shorter (toward the ankle) to better match the model.
        ay = by(lm["ankle_z"], ax)
        bally = ay + 0.85 * (lm["ball_y"] - ay)
        toey = ay + 0.85 * (lm["foot_tip_y"] - ay)
        ft = _bone(eb, f"Foot_{side}",
                   (ax, ay, lm["ankle_z"]),
                   (ax, bally, 0.0), ll, True)
        _bone(eb, f"Toe_{side}",
              (ax, bally, 0.0),
              (ax, toey, 0.0), ft, True)

    bpy.ops.object.mode_set(mode="OBJECT")
    log("skeleton", f"created {len(arm.bones)} bones")
    return rig


# --------------------------------------------------------------------------- #
# Skin: bind mesh to armature with Blender's automatic (bone-heat) weights.
# --------------------------------------------------------------------------- #
def skin(obj, rig, lm, H):
    """Robust skinning via a watertight voxel proxy.

    Bone-heat auto-weighting fails outright on real character meshes
    (intersecting/non-manifold geometry from joined sub-meshes, hair, etc.) —
    producing zero weights. Instead we auto-weight a voxel-remeshed (watertight)
    copy, which bone-heat handles reliably, then transfer those weights onto the
    real mesh by nearest surface point.
    """
    log("skin", "building watertight weight proxy")
    proxy = obj.copy()
    proxy.data = obj.data.copy()
    proxy.name = "WeightProxy"
    bpy.context.collection.objects.link(proxy)
    bpy.context.view_layer.objects.active = proxy
    rm = proxy.modifiers.new("Remesh", "REMESH")
    rm.mode = "VOXEL"
    rm.voxel_size = 0.03
    bpy.ops.object.modifier_apply(modifier=rm.name)

    log("skin", "auto-weighting proxy")
    bpy.ops.object.select_all(action="DESELECT")
    proxy.select_set(True)
    rig.select_set(True)
    bpy.context.view_layer.objects.active = rig
    try:
        bpy.ops.object.parent_set(type="ARMATURE_AUTO")
    except RuntimeError as e:
        fail(f"proxy weighting failed: {e}")

    log("skin", "transferring weights to mesh")
    # Ensure the real mesh has a vertex group per bone for ARMATURE_NAME binding.
    existing = {vg.name for vg in obj.vertex_groups}
    for b in rig.data.bones:
        if b.name not in existing:
            obj.vertex_groups.new(name=b.name)

    bpy.ops.object.select_all(action="DESELECT")
    proxy.select_set(True)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = proxy   # source = active
    bpy.ops.object.data_transfer(
        data_type="VGROUP_WEIGHTS",
        vert_mapping="POLYINTERP_NEAREST",
        layers_select_src="ALL",
        layers_select_dst="NAME",
        mix_mode="REPLACE",
    )

    # Bind the real mesh using the transferred weights (no recompute).
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    rig.select_set(True)
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.parent_set(type="ARMATURE_NAME")

    bpy.data.objects.remove(proxy, do_unlink=True)
    _mask_weights(obj, lm, H)
    _mask_fingers(obj, rig)
    _ensure_finger_weights(obj, rig)
    # Which bones each vertex must NEVER hold — the hard left/right + per-finger
    # separations. Passed into the smoothing pass so it can soften WITHIN a limb
    # (and curl fingers) without bleeding weight back across the crotch or
    # between adjacent fingers.
    forbidden = _separation_forbidden(obj, lm, H)
    _add_finger_forbidden(obj, forbidden)
    _cleanup_weights(obj, forbidden)
    log("skin", "skinning complete")


def _mask_weights(obj, lm, H):
    """Force each limb to be self-contained by zeroing impossible weights.

    Bone-heat gives smooth, far-reaching weights that bleed across the body
    (left leg influencing the right, arms influencing the belly, neck pulling
    the torso). We hard-clear each bone's weight outside the region it can
    plausibly own:
      * left-side limb bones may only weight the body's left half, right-side
        the right half (this is what keeps the two legs/arms independent);
      * leg bones may not reach above the hip joint;
      * arm/hand/finger bones may not reach below the chest;
      * neck/head may not reach below the neck base.
    The central spine bones (Hips/Spine/Spine1/Spine2/Chest) are never masked,
    so every vertex keeps at least one valid influence; normalization restores
    a clean per-vertex sum afterwards.
    """
    fingers = [f"{f}{n}" for f in ("Thumb", "Index", "Middle", "Ring", "Pinky")
               for n in (1, 2, 3)]
    arm_parts = ["Clavicle", "Shoulder", "UpperArm", "LowerArm", "Hand"] + fingers
    leg_parts = ["UpperLeg", "LowerLeg", "Foot", "Toe"]

    def named(parts, side):
        return [f"{p}_{side}" for p in parts]

    L_limbs = named(arm_parts, "L") + named(leg_parts, "L")
    R_limbs = named(arm_parts, "R") + named(leg_parts, "R")
    leg_bones = named(leg_parts, "L") + named(leg_parts, "R")
    arm_bones = named(arm_parts, "L") + named(arm_parts, "R")
    head_bones = ["Neck", "Head"]
    spine_bones = ["Hips", "Spine", "Spine1", "Spine2"]

    groups = {vg.name: vg for vg in obj.vertex_groups}
    # Left/right masking (x) is strict — that's what keeps limbs independent.
    # The vertical bands are deliberately GENEROUS so the torso/neck keep their
    # natural blending (hard vertical cuts were regressing the spine region).
    x_margin = 0.008                        # tight midline so L/R don't cross
    # Legs stay OFF the pelvis so the Hips bone owns it as ONE section (else
    # left/right leg weights split the pelvis down the middle).
    hips_top = lm["hips_z"] + 0.02 * H
    neck_base = lm["neck_z"] - 0.10 * (lm["head_top_z"] - lm["neck_z"])
    neck_bot = neck_base                      # neck/head weight only above the (lowered) neck base
    arm_bot = lm["chest_z"] - 0.12 * H

    # Each spine bone owns a clean vertical band (with blend margin) so the torso
    # reads as hips / waist / lower-chest / upper-chest, and the chest can't reach
    # down into the belly and stretch it to a point.
    sm = 0.05 * H
    spine_mid = (lm["spine_z"] + lm["spine1_z"]) / 2.0
    b1 = (spine_mid + neck_base) / 2.0
    spine_bands = {
        "Hips":   (None, lm["spine_z"] + sm),
        "Spine":  (lm["hips_z"] - sm, spine_mid + sm),
        "Spine1": (lm["spine_z"] - sm, b1 + sm),
        "Spine2": (spine_mid - sm, neck_base + sm),
    }

    idx2name = {vg.index: vg.name for vg in obj.vertex_groups}
    rm = {name: set() for name in set(L_limbs + R_limbs + leg_bones
                                      + arm_bones + head_bones + spine_bones)}
    for v in obj.data.vertices:
        co = obj.matrix_world @ v.co
        i = v.index
        z = co.z
        remove = set()
        if co.x < -x_margin:                # right half -> no left-limb weight
            remove.update(L_limbs)
        elif co.x > x_margin:               # left half -> no right-limb weight
            remove.update(R_limbs)
        if z > hips_top:
            remove.update(leg_bones)
        if z < arm_bot:
            remove.update(arm_bones)
        if z < neck_bot:
            remove.update(head_bones)
        for n, (zmin, zmax) in spine_bands.items():
            if (zmin is not None and z < zmin) or (zmax is not None and z > zmax):
                remove.add(n)

        # Never strip a vertex of ALL its weight: an orphaned vertex binds to the
        # armature root / FBX neutral_bone at the origin and collapses to the
        # floor between the feet. Keep its dominant influence if masking empties it.
        cur = {idx2name[g.group]: g.weight for g in v.groups if g.weight > 0.0}
        if not cur:
            continue
        if not (set(cur) - remove):
            remove.discard(max(cur, key=cur.get))
        for n in remove:
            if n in cur:
                rm[n].add(i)

    for name, idxs in rm.items():
        vg = groups.get(name)
        if vg and idxs:
            vg.remove(list(idxs))


def _pt_seg_dist(p, a, b):
    """Distance from point p to segment a-b."""
    ab = b - a
    ll = ab.length_squared
    t = 0.0 if ll == 0.0 else max(0.0, min(1.0, (p - a).dot(ab) / ll))
    return (p - (a + ab * t)).length


def _mask_fingers(obj, rig):
    """Separate finger JOINTS so each finger can curl (not move as one section).

    The voxel weight proxy can't resolve the ~1.5cm finger phalanges, so every
    vertex on a finger ends up weighted to all three of its segment bones (and
    often neighbouring fingers too) — the finger then moves rigidly. For each
    vertex touching more than one finger-segment bone, keep ONLY the single
    nearest segment (point-to-centerline) and drop the rest. The later weight
    smooth re-blends adjacent joints so curling stays smooth. Runs before
    renaming, so bones use internal names.
    """
    names = ["Thumb", "Index", "Middle", "Ring", "Pinky"]
    bones = rig.data.bones
    groups = {vg.name: vg for vg in obj.vertex_groups}
    idx2name = {vg.index: vg.name for vg in obj.vertex_groups}
    mw = obj.matrix_world
    total = 0

    for side in ("L", "R"):
        segs = {}                          # segment bone name -> (head, tail)
        for fn in names:
            for n in (1, 2, 3):
                b = bones.get(f"{fn}{n}_{side}")
                if b:
                    segs[f"{fn}{n}_{side}"] = (b.head_local.copy(), b.tail_local.copy())
        if len(segs) < 2:
            continue
        rm = {sn: set() for sn in segs}
        for v in obj.data.vertices:
            cur = [idx2name[g.group] for g in v.groups if g.weight > 0.0]
            touched = [sn for sn in segs if sn in cur]
            if len(touched) < 2:
                continue
            p = mw @ v.co
            best = min(touched, key=lambda sn: _pt_seg_dist(p, *segs[sn]))
            for sn in touched:
                if sn != best:
                    rm[sn].add(v.index)
        for sn, idxs in rm.items():
            vg = groups.get(sn)
            if vg and idxs:
                vg.remove(list(idxs))
                total += len(idxs)
    if total:
        log("skin", f"separated finger joints ({total} reassignments)")


def _ensure_finger_weights(obj, rig):
    """No finger bone may be weightless.

    Empty deform bones break some apps' "finalize as character" step AND leave
    the joint unposeable. The voxel weight proxy is too coarse to reach the thin
    distal phalanges / edge fingers, so they often end up with no weights — give
    any empty finger segment weight on the mesh verts nearest its centerline.
    """
    names = ["Thumb", "Index", "Middle", "Ring", "Pinky"]
    mw = obj.matrix_world
    wv = [(mw @ v.co, v.index) for v in obj.data.vertices]
    used = {g.group for v in obj.data.vertices for g in v.groups if g.weight > 1e-6}
    fixed = 0
    for side in ("L", "R"):
        for fn in names:
            for n in (1, 2, 3):
                bname = f"{fn}{n}_{side}"
                b = rig.data.bones.get(bname)
                vg = obj.vertex_groups.get(bname)
                if not b or not vg or vg.index in used:
                    continue
                a, c = b.head_local.copy(), b.tail_local.copy()
                near = sorted(wv, key=lambda pc: _pt_seg_dist(pc[0], a, c))[:3]
                for _, vi in near:
                    vg.add([vi], 1.0, "REPLACE")
                fixed += 1
    if fixed:
        bpy.ops.object.select_all(action="DESELECT")
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        bpy.ops.object.vertex_group_normalize_all(group_select_mode="ALL",
                                                  lock_active=False)
        log("skin", f"filled {fixed} empty finger bones")


_FINGER_NAMES_SEP = ("Thumb", "Index", "Middle", "Ring", "Pinky")
_ARM_PARTS_SEP = (["Clavicle", "Shoulder", "UpperArm", "LowerArm", "Hand"]
                  + [f"{f}{n}" for f in _FINGER_NAMES_SEP for n in (1, 2, 3)])
_LEG_PARTS_SEP = ["UpperLeg", "LowerLeg", "Foot", "Toe"]


def _separation_forbidden(obj, lm, H):
    """Per-vertex set of bone GROUP INDICES the vertex must never hold — the HARD
    left/right + limb-reach separations (same rules as _mask_weights, minus the
    soft spine bands). Used to keep weight-smoothing from re-bleeding weight
    across the body's midline. Returns list[set] indexed by vertex.
    """
    def named(parts, side):
        return [f"{p}_{side}" for p in parts]

    name2idx = {vg.name: vg.index for vg in obj.vertex_groups}

    def idxs(names):
        return {name2idx[n] for n in names if n in name2idx}

    L_i = idxs(named(_ARM_PARTS_SEP, "L") + named(_LEG_PARTS_SEP, "L"))
    R_i = idxs(named(_ARM_PARTS_SEP, "R") + named(_LEG_PARTS_SEP, "R"))
    leg_i = idxs(named(_LEG_PARTS_SEP, "L") + named(_LEG_PARTS_SEP, "R"))
    arm_i = idxs(named(_ARM_PARTS_SEP, "L") + named(_ARM_PARTS_SEP, "R"))
    head_i = idxs(["Neck", "Head"])

    x_margin = 0.008
    hips_top = lm["hips_z"] + 0.02 * H
    neck_base = lm["neck_z"] - 0.10 * (lm["head_top_z"] - lm["neck_z"])
    arm_bot = lm["chest_z"] - 0.12 * H
    mw = obj.matrix_world

    forbidden = [set() for _ in range(len(obj.data.vertices))]
    for v in obj.data.vertices:
        co = mw @ v.co
        f = forbidden[v.index]
        if co.x < -x_margin:
            f |= L_i                       # right half: no left-limb weight
        elif co.x > x_margin:
            f |= R_i                       # left half: no right-limb weight
        if co.z > hips_top:
            f |= leg_i                     # above the hips: no leg weight
        if co.z < arm_bot:
            f |= arm_i                     # below the chest: no arm/finger weight
        if co.z < neck_base:
            f |= head_i                    # below the neck: no neck/head weight
    return forbidden


def _add_finger_forbidden(obj, forbidden):
    """Forbid each finger vertex from holding OTHER fingers' segments (so
    smoothing can't drag a neighbouring finger along). A vertex's owning finger
    is whichever finger segment it currently carries (one each, after
    _mask_fingers); its own 3 segments stay allowed so the finger still curls."""
    name2idx = {vg.name: vg.index for vg in obj.vertex_groups}
    idx2name = {i: n for n, i in name2idx.items()}
    seg_by_finger, all_finger_i = {}, set()
    for side in ("L", "R"):
        for fn in _FINGER_NAMES_SEP:
            s = {name2idx[f"{fn}{n}_{side}"] for n in (1, 2, 3)
                 if f"{fn}{n}_{side}" in name2idx}
            if s:
                seg_by_finger[(side, fn)] = s
                all_finger_i |= s

    def owner(name):
        for side in ("L", "R"):
            for fn in _FINGER_NAMES_SEP:
                if name.startswith(fn) and name.endswith(f"_{side}"):
                    return (side, fn)
        return None

    for v in obj.data.vertices:
        own = None
        for g in v.groups:
            if g.weight > 0.0:
                o = owner(idx2name.get(g.group, ""))
                if o:
                    own = o
                    break
        if own:
            forbidden[v.index] |= (all_finger_i - seg_by_finger.get(own, set()))


def _cleanup_weights(obj, forbidden=None):
    """Cap influences and soften hard mask boundaries (broadly app-friendly).

    - manual edge smooth: blends the HARD mask boundaries so vertices at a zone
      edge don't tear/stretch apart when posed (the "sticking out" artifact).
      Done by hand because Blender's vertex_group_smooth operator can't run in
      headless --background (it needs a viewport context).
    - limit to 4 influences/vertex: figure apps and game engines cap bone
      influences; too many can make some apps fail when finalizing the rig.
    """
    _smooth_weights(obj, iterations=1, factor=0.3, forbidden=forbidden)
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.vertex_group_limit_total(group_select_mode="ALL", limit=4)
    bpy.ops.object.vertex_group_normalize_all(group_select_mode="ALL",
                                              lock_active=False)


def _smooth_weights(obj, iterations=2, factor=0.5, forbidden=None):
    """Laplacian-style weight smoothing across mesh edges (headless-safe).

    `forbidden` (list[set] of group indices per vertex) blocks a vertex from ever
    gaining weight for a bone on the wrong side / wrong finger, so smoothing
    softens within a limb but never bleeds across the midline or between fingers.
    """
    me = obj.data
    n = len(me.vertices)
    neighbors = [[] for _ in range(n)]
    for e in me.edges:
        a, b = e.vertices
        neighbors[a].append(b)
        neighbors[b].append(a)

    weights = [dict() for _ in range(n)]
    for v in me.vertices:
        for g in v.groups:
            if g.weight > 0.0:
                weights[v.index][g.group] = g.weight

    for _ in range(iterations):
        nxt = [dict() for _ in range(n)]
        for vi in range(n):
            acc = {}
            for gi, w in weights[vi].items():
                acc[gi] = acc.get(gi, 0.0) + (1.0 - factor) * w
            nb = neighbors[vi]
            if nb:
                f = factor / len(nb)
                for ni in nb:
                    for gi, w in weights[ni].items():
                        acc[gi] = acc.get(gi, 0.0) + f * w
            if forbidden:
                for gi in forbidden[vi]:
                    acc.pop(gi, None)       # never let a wrong-side bone bleed in
            nxt[vi] = acc
        weights = nxt

    groups = list(obj.vertex_groups)
    for vi in range(n):
        idx = [vi]
        for vg in groups:
            w = weights[vi].get(vg.index, 0.0)
            if w > 0.0005:
                vg.add(idx, w, "REPLACE")
            else:
                vg.remove(idx)


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #
def export_glb(path):
    log("export", f"writing {os.path.basename(path)}")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.export_scene.gltf(
        filepath=path,
        export_format="GLB",
        use_selection=True,
        export_skins=True,
        export_yup=True,
    )
    log("export", "done")


def simplify_materials(obj):
    """Replace the mesh's materials with one plain material for the FBX.

    Some figure apps' character finalize is reported to crash on the materials
    themselves (missing image data, multiple texture nodes, vertex colors) — and
    our GLB export warns about exactly those. The preview GLB keeps the originals;
    only the FBX (the figure app's input) is simplified.
    """
    me = obj.data
    me.materials.clear()
    mat = bpy.data.materials.new("Character")
    mat.use_nodes = True
    me.materials.append(mat)
    for p in me.polygons:
        p.material_index = 0
    try:
        while len(me.color_attributes):
            me.color_attributes.remove(me.color_attributes[0])
    except Exception:  # noqa: BLE001 - color attrs are optional
        pass
    log("export", "simplified materials for FBX")


def export_fbx(path, leaf_bones=True):
    """Export an engine-friendly FBX (armature + mesh, no baked animation).

    FBX 7.4 binary with a standard humanoid bone hierarchy works broadly —
    game engines, DCC tools, and figure-posing apps (which need FBX <=7.4) alike.
    T-pose so downstream auto-mapping lines up.

    leaf_bones=False for the standard-bone path: Blender's auto leaf bones get the
    wrong names (head_bb__end, not the spec's head_end_bb_), so they'd be extra
    non-standard bones — better to omit them than mis-name them.
    """
    log("export", f"writing {os.path.basename(path)}")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.export_scene.fbx(
        filepath=path,
        use_selection=True,
        object_types={"ARMATURE", "MESH"},
        add_leaf_bones=leaf_bones,   # mixamo: End Sites; standard: off (see above)
        bake_anim=False,
        mesh_smooth_type="FACE",
        apply_unit_scale=True,
        apply_scale_options="FBX_SCALE_ALL",
        axis_forward="-Z",
        axis_up="Y",
        primary_bone_axis="Y",
        secondary_bone_axis="X",
    )
    log("export", "done (fbx)")


# --------------------------------------------------------------------------- #
# Front view for the Mixamo-style marker editor
# --------------------------------------------------------------------------- #
def render_front(obj, out_png, H):
    """Render an ortho front view and return the pixel<->world calibration."""
    import math
    scene = bpy.context.scene
    res, ortho, cz = 768, 1.95, H / 2.0

    d = bpy.data.lights.new("Sun", "SUN")
    d.energy = 3.0
    light = bpy.data.objects.new("Sun", d)
    scene.collection.objects.link(light)
    light.rotation_euler = (math.radians(55), 0, math.radians(25))

    cam_data = bpy.data.cameras.new("PrepCam")
    cam_data.type = "ORTHO"
    cam_data.ortho_scale = ortho
    cam = bpy.data.objects.new("PrepCam", cam_data)
    scene.collection.objects.link(cam)
    cam.location = (0.0, -5.0, cz)
    cam.rotation_euler = (math.radians(90), 0, 0)
    scene.camera = cam

    scene.render.engine = "BLENDER_WORKBENCH"
    sh = scene.display.shading
    sh.light = "STUDIO"
    sh.color_type = "SINGLE"
    sh.single_color = (0.82, 0.82, 0.85)
    scene.render.resolution_x = res
    scene.render.resolution_y = res
    scene.render.filepath = out_png
    os.makedirs(os.path.dirname(os.path.abspath(out_png)), exist_ok=True)
    bpy.ops.render.render(write_still=True)
    return {"res": res, "ortho": ortho, "center_z": cz, "height": H}


# --------------------------------------------------------------------------- #
# Face shape keys (ARKit 52) — optional stage, shares the marker mechanism.
# --------------------------------------------------------------------------- #
def add_face_shapekeys(obj, job, lm, H):
    pts = [obj.matrix_world @ v.co for v in obj.data.vertices]
    base_z, top_z, head_only = face_markers.head_band(pts, lm)
    # The face_only job comes from the UI's "head only" choice — trust it over
    # the landmark guess (which misreads stylized heads).
    if job.get("face_only"):
        head_only = True
    if head_only:
        base_z, top_z = min(p.z for p in pts), max(p.z for p in pts)
        log("face", "treating the whole input as a head")
    # Edited markers from the app override the auto-detected default layout.
    if job.get("face_markers") and job.get("calib"):
        log("face", "using edited face markers")
        marks = face_markers.from_px(job["face_markers"], job["calib"])
    else:
        det_base, det_top = face_markers.detection_band(base_z, top_z, head_only, H)
        face = face_markers.detect_face(pts, det_base, det_top)
        marks = face_markers.default_markers(face)
    face_shapekeys.build(obj, marks, H, head_base=base_z,
                         sets=job.get("face_sets"), log=log)


def export_all(obj, output, leaf_bones=True):
    """Write the preview GLB and an FBX sibling (FBX failure is non-fatal so a
    flaky export can't sink an otherwise-good result)."""
    export_glb(output)
    fbx_path = os.path.splitext(output)[0] + ".fbx"
    try:
        simplify_materials(obj)         # some apps crash on the original materials
        export_fbx(fbx_path, leaf_bones=leaf_bones)
    except Exception as e:  # noqa: BLE001 - report and continue
        log("export", f"WARNING: FBX export failed, GLB still available: {e}")


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def parse_args():
    argv = sys.argv
    if "--" not in argv:
        fail("no job file passed (use: -- <job.json>)")
    job_path = argv[argv.index("--") + 1]
    with open(job_path, "r", encoding="utf-8") as f:
        return json.load(f)


def main():
    job = parse_args()
    output = job.get("output")
    if not output:
        fail("job is missing 'output' path")
    target_height = float(job.get("target_height", 1.8))

    reset_scene()

    input_path = job.get("input")
    if input_path and os.path.exists(input_path):
        meshes = import_model(input_path)
    else:
        meshes = make_test_human()

    obj = join_meshes(meshes)
    obj = normalize(obj, target_height)
    clean_mesh(obj)
    points = sample_points(obj)
    lm = landmarks.detect_landmarks(points, target_height, log=log)

    # PREP: render a front view and emit draggable markers, then stop. The app
    # shows these for the user to nudge before the real rig is built.
    if job.get("mode") == "prep":
        front_png = job.get("front_png") or (os.path.splitext(output)[0] + "_front.png")
        calib = render_front(obj, front_png, target_height)
        world = markers_mod.to_markers(lm)
        px = {k: markers_mod.world_to_px(v[0], v[1], calib) for k, v in world.items()}
        # Auto-detected face anchors on the same front image, for the editor.
        fb_z, ft_z, fo = face_markers.head_band(points, lm)
        if job.get("head_only"):
            fo = True
            fb_z, ft_z = min(p.z for p in points), max(p.z for p in points)
        db, dt = face_markers.detection_band(fb_z, ft_z, fo, target_height)
        face = face_markers.detect_face(points, db, dt)
        face_px = face_markers.to_px(face_markers.default_markers(face), calib)
        # Top-down hand close-ups + proposed fingertip markers (per side).
        # Skipped for head-only inputs (no arms to frame).
        hand_views, hand_cal, finger_px = {}, {}, {}
        base_png = os.path.splitext(front_png)[0]
        for s in ("L", "R") if not job.get("head_only") else ():
            hc = hand_calib(obj, lm, s)
            hp = f"{base_png}_hand_{s}.png"
            render_hand_view(obj, lm, s, hp, hc)
            hand_views[s], hand_cal[s] = hp, hc
            finger_px[s] = propose_finger_markers(obj, lm, s, hc)
        with open(output, "w", encoding="utf-8") as f:
            json.dump({"front": front_png, "calib": calib,
                       "markers": px, "face_markers": face_px,
                       "hand_views": hand_views, "hand_calib": hand_cal,
                       "finger_markers": finger_px}, f)
        log("prep", f"front view + {len(px)} body + {len(face_px)} face + "
                     f"{sum(len(v) for v in finger_px.values())} finger markers")
        return

    # FACE-ONLY: add ARKit shape keys to the mesh and export — no rigging.
    # Gives a standalone "face tool" out of the same pipeline (one codebase).
    if job.get("face_only"):
        add_face_shapekeys(obj, job, lm, target_height)
        export_all(obj, output)
        log("done", output)
        return

    # RIG: if the app passed edited markers, they override detection.
    if job.get("markers") and job.get("calib"):
        calib = job["calib"]
        world = {k: markers_mod.px_to_world(v[0], v[1], calib)
                 for k, v in job["markers"].items()}
        lm = markers_mod.from_markers(world, lm, target_height)
        log("rig", "using edited markers")

    # Edited fingertips from the hand editor -> world targets per side.
    finger_override = None
    if job.get("finger_markers") and job.get("hand_calib"):
        finger_override = {}
        for s, marks in job["finger_markers"].items():
            c = job["hand_calib"].get(s)
            if c:
                finger_override[s] = {f: hand_px_to_world(p[0], p[1], c)
                                      for f, p in marks.items()}
        log("rig", "using edited fingertips")

    naming_scheme = job.get("bone_naming", "mixamo")
    rig = build_skeleton(obj, lm, fingers=bool(job.get("fingers", True)),
                         standard=(naming_scheme == "standard"),
                         finger_tips_override=finger_override)
    # Skin with internal bone names (so weight masking can find limbs by role),
    # then rename bones AND their vertex groups together for the target app.
    skin(obj, rig, lm, target_height)
    bone_naming.rename(rig, naming_scheme, obj=obj, log=log)

    # FACE: optional ARKit shape keys on the same mesh, so the exported character
    # both poses (armature) and emotes (shape keys). Built after skinning so the
    # keys ride on the final mesh/vertex order.
    if job.get("face_shapekeys"):
        add_face_shapekeys(obj, job, lm, target_height)

    export_all(obj, output, leaf_bones=(naming_scheme != "standard"))
    log("done", output)


if __name__ == "__main__":
    main()
