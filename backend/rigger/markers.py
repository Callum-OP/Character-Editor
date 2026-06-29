"""
Mixamo-style editable joint markers.

Bridges the abstract landmark dict (hips_z, shoulder_x, ...) and a set of
draggable front-view joint POINTS the user can nudge in the app:

    to_markers(lm)        -> {name: [world_x, world_z]}   for display
    from_markers(m, base) -> a full landmark dict          after editing
    world_to_px / px_to_world  map between world and front-view image pixels
                               using the calibration from render_views.py

Center joints (head_top, neck, hip) are fixed on X=0; the rest are mirrored
left/right. Non-edited landmark fields (spine split, hands, toes) are filled
from `base` (the auto-detected dict) or recomputed from the edited joints.
"""

import landmarks

PAIRED = ["shoulder", "elbow", "wrist", "knee", "ankle"]
CENTER = ["head_top", "neck", "chest", "hip"]


def to_markers(lm):
    m = {
        "head_top": [0.0, lm["head_top_z"]],
        "neck":     [0.0, lm["neck_z"]],
        "chest":    [0.0, lm["chest_z"]],
        "hip":      [0.0, lm["hips_z"]],
    }
    for p in PAIRED:
        x, z = lm[f"{p}_x"], lm[f"{p}_z"]
        m[f"{p}_l"] = [x, z]
        m[f"{p}_r"] = [-x, z]
    return m


def from_markers(m, base, H):
    lm = dict(base)
    lm["head_top_z"] = m["head_top"][1]
    lm["neck_z"] = m["neck"][1]
    lm["hips_z"] = m["hip"][1]
    for p in PAIRED:
        xl, zl = m[f"{p}_l"]
        xr, zr = m[f"{p}_r"]
        lm[f"{p}_x"] = (abs(xl) + abs(xr)) / 2.0   # symmetric rig
        lm[f"{p}_z"] = (zl + zr) / 2.0
    lm["hip_x"] = lm["knee_x"]                       # leg root aligns to knee X

    # Torso joints from the hip/chest/neck markers: Spine + Spine1 split the
    # hips->chest span; Spine2 sits at the chest marker.
    lm["chest_z"] = m["chest"][1]
    lm["spine_z"] = landmarks._lerp(lm["hips_z"], lm["chest_z"], 1.0 / 3.0)
    lm["spine1_z"] = landmarks._lerp(lm["hips_z"], lm["chest_z"], 2.0 / 3.0)
    lm["head_z"] = min(lm["neck_z"] + 0.03 * H, lm["head_top_z"])

    # Keep the hand a fixed length beyond the (possibly moved) wrist.
    lm["hand_x"] = lm["wrist_x"] + (base["hand_x"] - base["wrist_x"])
    lm["hand_z"] = lm["wrist_z"] + (base["hand_z"] - base["wrist_z"])
    return lm


# --- world <-> front-view image pixels (see render_views.py calib) ----------- #
def world_to_px(x, z, calib):
    res, ortho, cz = calib["res"], calib["ortho"], calib["center_z"]
    return [(x / ortho + 0.5) * res, (0.5 - (z - cz) / ortho) * res]


def px_to_world(px, py, calib):
    res, ortho, cz = calib["res"], calib["ortho"], calib["center_z"]
    return [(px / res - 0.5) * ortho, cz + (0.5 - py / res) * ortho]
