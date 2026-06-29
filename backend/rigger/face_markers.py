"""
Editable FACE markers — the facial counterpart to markers.py.

The body rig lets the user nudge draggable joint points on a front view; the
face stage reuses that exact mechanism. Instead of skeletal joints these are
facial anchors (eye corners, lids, brows, mouth corners, lips, cheeks, nose,
chin) that tell the shape-key engine WHERE the features are — so it works on any
head without detecting facial geometry (which fails on bare mannequin meshes,
see project memory).

    default_markers(lm, H) -> {name: [world_x, world_z]}   starting layout
    to_px / from_px reuse markers.world_to_px / px_to_world (same calibration)

Center anchors live on X=0; paired anchors are mirrored L/R (suffix _l / _r,
where _l is the character's left == +X, matching ARKit's "Left").

Depth (Y) is NOT a marker — the engine samples it from the mesh front surface at
each anchor (see face_shapekeys.resolve), the same trick build_skeleton uses for
bone depth.
"""

import markers as _markers

# Anchors fixed on the facial midline (X = 0).
CENTER = ["nose_tip", "upper_lip", "lower_lip", "chin"]

# Anchors mirrored left/right. The user places/edits the left set; the right is
# mirrored, exactly like the body PAIRED joints.
PAIRED = [
    "brow_inner", "brow_outer",
    "eye_inner", "eye_outer", "eye_top", "eye_bottom",
    "cheek", "nostril", "mouth_corner",
]


def head_band(points, lm):
    """Vertical extent (base_z, top_z) of the face region, + a head-only flag.

    landmarks.detect_landmarks assumes a full humanoid body. A real body has its
    neck well below the crown (~0.83*H); if the detected "neck" sits within ~12%
    of the top of the mesh, no real neck was found — the input is a bare head, so
    the WHOLE mesh is the face region (otherwise the head-gate would exclude
    everything below the forehead and nothing deforms).
    """
    zs = [p.z for p in points]
    min_z, max_z = min(zs), max(zs)
    span = max(max_z - min_z, 1e-4)
    neck, top = lm["neck_z"], lm["head_top_z"]
    if (top - neck) < 0.12 * span:
        return min_z, max_z, True
    return neck, top, False


def detection_band(gate_base, top, head_only, H):
    """The Z range to search for the face. For a head-only mesh that's the whole
    thing; for a body, just the top ~0.22*H so the nose search can't latch onto a
    forward-protruding chest/clavicle below the neck."""
    if head_only:
        return gate_base, top
    return max(gate_base, top - 0.22 * H), top


def detect_face(points, base_z, top_z):
    """Locate the face geometrically and return {nose_z, half_w}.

    Proportional guesses land on the forehead because head_top is usually hair,
    not skull. The nose tip, by contrast, is a reliable anchor: it's the
    most-forward (min-Y; the face looks down -Y) point of the head. We take the
    forward-most cluster as the nose, then measure the head half-width at that
    height. Everything else is placed relative to the nose. `points` are
    world-space vectors restricted to [base_z, top_z].
    """
    span = max(top_z - base_z, 1e-4)
    full = [p for p in points if base_z <= p.z <= top_z] or list(points)
    # Search the nose in the MID-face band only — otherwise min-Y can latch onto
    # a protruding chin (too low) or a hair fringe / brow (too high).
    lo, hi = base_z + 0.30 * span, base_z + 0.68 * span
    mid = [p for p in full if lo <= p.z <= hi] or full
    k = max(5, len(mid) // 150)
    front = sorted(mid, key=lambda p: p.y)[:k]       # most-forward = nose region
    nose_z = sum(p.z for p in front) / k
    cx = sum(p.x for p in front) / k
    vb = 0.08 * span
    xs = sorted(abs(p.x - cx) for p in full if abs(p.z - nose_z) < vb)
    half_w = xs[int(0.9 * (len(xs) - 1))] if xs else 0.4 * span
    half_w = max(half_w, 1e-3)
    return {"nose_z": nose_z, "half_w": half_w, "span": span}


def default_markers(face):
    """Starting face layout anchored to the detected nose (see detect_face).

    Vertical positions are offsets from the nose tip as fractions of a face
    height estimated from the head width (≈ 2.6 × half-width — humans are ~1.45×
    taller-than-wide in the face), capped to the detected band. Horizontal
    positions are fractions of the half-width. The user fine-tunes from here
    (move/scale the whole set at once).

    (Chin detection was tried as a second anchor but the lips protrude as far as
    the chin and fooled the min-Y search — width-derived height is more stable.)
    """
    nose = face["nose_z"]
    hw = max(face["half_w"], 1e-3)
    F = min(2.6 * hw, 0.9 * face.get("span", 2.6 * hw))

    chin_z = nose - 0.50 * F
    mouth_z = nose - 0.30 * F
    cheek_z = nose - 0.06 * F
    eye_z = nose + 0.22 * F
    brow_z = nose + 0.37 * F
    nostril_z = nose - 0.07 * F
    lip_gap = 0.05 * F
    eye_h = 0.05 * F

    m = {
        "nose_tip":  [0.0, nose],
        "upper_lip": [0.0, mouth_z + lip_gap],
        "lower_lip": [0.0, mouth_z - lip_gap],
        "chin":      [0.0, chin_z],
    }

    # (name, x-fraction of half-width, z) for the LEFT side; right mirrors.
    paired_xz = {
        "brow_inner":  (0.22, brow_z),
        "brow_outer":  (0.62, brow_z),
        "eye_inner":   (0.24, eye_z),
        "eye_outer":   (0.62, eye_z),
        "eye_top":     (0.43, eye_z + eye_h),
        "eye_bottom":  (0.43, eye_z - 0.6 * eye_h),
        "cheek":       (0.58, cheek_z),
        "nostril":     (0.16, nostril_z),
        "mouth_corner": (0.30, mouth_z),
    }
    for name, (xf, z) in paired_xz.items():
        x = xf * hw
        m[f"{name}_l"] = [x, z]
        m[f"{name}_r"] = [-x, z]
    return m


def to_px(markers, calib):
    """World (x,z) anchors -> front-view pixel coords for the editor."""
    return {k: _markers.world_to_px(v[0], v[1], calib) for k, v in markers.items()}


def from_px(px, calib):
    """Editor pixel coords -> world (x,z) anchors."""
    return {k: _markers.px_to_world(v[0], v[1], calib) for k, v in px.items()}
