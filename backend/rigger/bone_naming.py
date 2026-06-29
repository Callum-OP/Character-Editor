"""
Bone-naming schemes for downstream-tool compatibility.

The pipeline builds bones with internal names (Hips, Spine, Spine1, Spine2,
Neck, Head, Shoulder_L, UpperArm_L, ...). Different target apps expect different
naming conventions; this module renames the rig to match one.

Renaming happens AFTER build_skeleton but BEFORE skinning, so Blender's
automatic weights create vertex groups already matching the final bone names
(no vertex-group renaming needed).

Schemes
-------
- "mixamo":   Mixamo-standard role names — broadly compatible (game engines,
              DCC tools, most figure-posing apps). The default.
- "standard": a figure-posing app's "standard bone" spec (the lowercase *_bb_*
              names). When the FBX bones carry these EXACT names and the right
              axes, such apps auto-recognise the skeleton on import — no manual
              bone mapping. Used together with build_skeleton(standard=True),
              which builds the matching skeleton shape (one shoulder bone, no
              head-direction bone).
- "internal": keep our own readable names (handy for debugging the preview).
"""

import os
import json

# Internal name -> Mixamo role name.
MIXAMO = {
    "Hips": "Hips",
    "Spine": "Spine",
    "Spine1": "Spine1",
    "Spine2": "Spine2",
    "Neck": "Neck",
    "Head": "Head",
    "HeadFace": "HeadFace",
    "Clavicle_L": "LeftShoulder",
    "Shoulder_L": "LeftShoulder2",
    "UpperArm_L": "LeftArm",
    "LowerArm_L": "LeftForeArm",
    "Hand_L": "LeftHand",
    "Clavicle_R": "RightShoulder",
    "Shoulder_R": "RightShoulder2",
    "UpperArm_R": "RightArm",
    "LowerArm_R": "RightForeArm",
    "Hand_R": "RightHand",
    "UpperLeg_L": "LeftUpLeg",
    "LowerLeg_L": "LeftLeg",
    "Foot_L": "LeftFoot",
    "Toe_L": "LeftToeBase",
    "UpperLeg_R": "RightUpLeg",
    "LowerLeg_R": "RightLeg",
    "Foot_R": "RightFoot",
    "Toe_R": "RightToeBase",
}

# Finger mappings: {Finger}{n}_{L|R} -> {Left|Right}Hand{Finger}{n}
for _side, _prefix in (("L", "Left"), ("R", "Right")):
    for _finger in ("Thumb", "Index", "Middle", "Ring", "Pinky"):
        for _n in (1, 2, 3):
            MIXAMO[f"{_finger}{_n}_{_side}"] = f"{_prefix}Hand{_finger}{_n}"

# Internal name -> standard-bone spec name (the lowercase "_bb_" convention a
# figure-posing app auto-recognises). Read verbatim from that app's reference
# guide model, so they are exact. Assumes the matching skeleton shape from
# build_skeleton(standard=True): one shoulder bone per side (no separate
# Shoulder), no head-direction bone. End/tip bones and 4th finger joints are
# part of the spec but not emitted yet.
STANDARD_BB = {
    "Hips": "hips_bb_",
    "Spine": "spine_bb_",
    "Spine1": "spine1_bb_",
    "Spine2": "spine2_bb_",
    "Neck": "neck_bb_",
    "Head": "head_bb_",
    "Clavicle_L": "leftshoulder_bb_",
    "UpperArm_L": "leftarm_bb_",
    "LowerArm_L": "leftforearm_bb_",
    "Hand_L": "lefthand_bb_",
    "Clavicle_R": "rightshoulder_bb_",
    "UpperArm_R": "rightarm_bb_",
    "LowerArm_R": "rightforearm_bb_",
    "Hand_R": "righthand_bb_",
    "UpperLeg_L": "leftupleg_bb_",
    "LowerLeg_L": "leftleg_bb_",
    "Foot_L": "leftfoot_bb_",
    "Toe_L": "lefttoebase_bb_",
    "UpperLeg_R": "rightupleg_bb_",
    "LowerLeg_R": "rightleg_bb_",
    "Foot_R": "rightfoot_bb_",
    "Toe_R": "righttoebase_bb_",
}

# Fingers: {Finger}{n}_{L|R} -> {left|right}hand{finger}{n}_bb_ (n = 1..3; the
# spec's 4th joint is the fingertip end, which we don't build).
for _side, _prefix in (("L", "left"), ("R", "right")):
    for _finger in ("Thumb", "Index", "Middle", "Ring", "Pinky"):
        for _n in (1, 2, 3):
            STANDARD_BB[f"{_finger}{_n}_{_side}"] = \
                f"{_prefix}hand{_finger.lower()}{_n}_bb_"


def _load_overrides(log=lambda *a: None):
    """Merge backend/bone_names.json (internal-name -> standard-bone name) into
    STANDARD_BB. Keys starting with "_" are treated as comments. This is the seam
    for tweaking/extending the standard-bone names without editing code."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bone_names.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return
    except (ValueError, OSError) as e:  # noqa: BLE001 - report and continue
        log("rename", f"WARNING: couldn't read bone_names.json: {e}")
        return
    added = {k: v for k, v in data.items() if not k.startswith("_")}
    STANDARD_BB.update(added)
    log("rename", f"loaded {len(added)} standard bone names from bone_names.json")


SCHEMES = {"mixamo": MIXAMO, "standard": STANDARD_BB, "internal": None}


def rename(rig, scheme_name, obj=None, log=lambda *a: None):
    """Rename the armature's bones (and a skinned mesh's matching vertex groups).

    Pass `obj` (the bound mesh) when the mesh is already skinned so its vertex
    groups are renamed in lockstep — otherwise the armature modifier can no
    longer match bones to groups by name.
    """
    if scheme_name == "standard":
        _load_overrides(log)        # pull any overrides from bone_names.json
    scheme = SCHEMES.get(scheme_name, MIXAMO)
    if scheme is None:
        log("rename", f"keeping internal bone names ({scheme_name})")
        return

    bones = rig.data.bones
    unmapped = [b.name for b in bones if b.name not in scheme]
    for old, new in scheme.items():
        b = bones.get(old)
        if b:
            b.name = new
        if obj is not None:
            vg = obj.vertex_groups.get(old)
            if vg:
                vg.name = new
    if unmapped:
        log("rename", f"WARNING: {len(unmapped)} bones not in '{scheme_name}' "
                      f"scheme kept internal names: {', '.join(unmapped)}")
    log("rename", f"applied '{scheme_name}' naming to {len(scheme)} bones")
