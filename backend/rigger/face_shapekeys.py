"""
Marker-driven ARKit-52 shape-key generator.

Given a head mesh and the user's face markers (front-view anchor points, see
face_markers.py), build a Basis plus the 52 ARKit blendshapes as Blender shape
keys. The morph target for each shape is its FULL (weight = 1.0) deflection;
consumers blend 0..1.

Why marker-driven: detecting eyes/mouth from geometry is unreliable on bare
mannequin meshes (project memory). Markers tell us where the features ARE, so
each blendshape is a region query (vertices near an anchor) plus a displacement
(translate / rotate-about-jaw / close-lid). Approximate but robust on any head.

    build(obj, markers_xz, H, log=...) -> creates shape keys on obj.data

Geometry is computed in WORLD space (markers are world x/z; depth Y is sampled
from the mesh front surface) and written back to local shape-key coords via the
inverse object transform — so it's correct whether or not obj is parented to an
armature.
"""

import math
from mathutils import Vector, Matrix

import arkit


# --------------------------------------------------------------------------- #
# Falloff
# --------------------------------------------------------------------------- #
def _smooth(t):
    """Smoothstep on a 0..1 falloff (flat at both ends -> no creasing)."""
    if t <= 0.0:
        return 0.0
    if t >= 1.0:
        return 1.0
    return t * t * (3.0 - 2.0 * t)


def _add(out, wv, center, radius, vec):
    """Translate every vertex within `radius` of `center` by `vec`, faded."""
    if radius <= 1e-9:
        return
    for i, p in wv:
        d = (p - center).length
        if d < radius:
            f = _smooth(1.0 - d / radius)
            cur = out.get(i)
            out[i] = (cur + vec * f) if cur else vec * f


# --------------------------------------------------------------------------- #
# Resolve markers -> world anchors (sample depth from the mesh front surface)
# --------------------------------------------------------------------------- #
def _front_y(wv, x, z, r):
    """Front-surface depth at (x,z): the most-forward (min Y) nearby vertex.

    The head faces -Y, so the face surface is the minimum-Y side. Returns None
    when no vertex is near (caller falls back to 0)."""
    r2 = r * r
    best = None
    for _, p in wv:
        if (p.x - x) ** 2 + (p.z - z) ** 2 < r2:
            if best is None or p.y < best:
                best = p.y
    return best


def _resolve(markers_xz, wv, H):
    """{name:[x,z]} -> {name: world Vector}, snapped onto the mesh surface.

    We sample the front-surface depth at (x,z), then snap the anchor to the
    nearest actual vertex. Snapping matters: proportional default markers can sit
    a few cm off the real feature, and if the anchor floats off the surface the
    radius query catches nothing (every mouth/eye shape silently no-ops). Landing
    on a real vertex guarantees the region query has something to move.
    """
    r = 0.035 * H
    anchors = {}
    for name, (x, z) in markers_xz.items():
        y = _front_y(wv, x, z, r)
        if y is None:
            y = _front_y(wv, x, z, r * 3.0) or 0.0
        a = Vector((x, y, z))
        nearest = min(wv, key=lambda iv: (iv[1] - a).length_squared)[1]
        anchors[name] = nearest.copy()
    return anchors


# --------------------------------------------------------------------------- #
# Deformation context — resolved anchors + derived scales, all world-space
# --------------------------------------------------------------------------- #
class Ctx:
    def __init__(self, anchors, wv, H):
        self.wv = wv
        self.H = H
        a = anchors
        self.nose_tip = a["nose_tip"]
        self.upper_lip = a["upper_lip"]
        self.lower_lip = a["lower_lip"]
        self.chin = a["chin"]
        self.mouth_center = (self.upper_lip + self.lower_lip) * 0.5

        def side(key):
            return {"L": a[f"{key}_l"], "R": a[f"{key}_r"]}

        self.brow_inner = side("brow_inner")
        self.brow_outer = side("brow_outer")
        self.eye_inner = side("eye_inner")
        self.eye_outer = side("eye_outer")
        self.eye_top = side("eye_top")
        self.eye_bottom = side("eye_bottom")
        self.cheek = side("cheek")
        self.nostril = side("nostril")
        self.mouth_corner = side("mouth_corner")

        self.eye_center = {
            s: (self.eye_inner[s] + self.eye_outer[s]
                + self.eye_top[s] + self.eye_bottom[s]) * 0.25
            for s in "LR"
        }
        self.eye_w = {s: max((self.eye_outer[s] - self.eye_inner[s]).length, 1e-4)
                      for s in "LR"}
        self.eye_w_avg = 0.5 * (self.eye_w["L"] + self.eye_w["R"])
        self.mouth_w = max((self.mouth_corner["L"] - self.mouth_corner["R"]).length,
                           1e-4)
        # Lower-face vertical scale (brow down to chin).
        brow_z = 0.5 * (self.brow_inner["L"].z + self.brow_inner["R"].z)
        self.face_h = max(brow_z - self.chin.z, 1e-4)
        self.head_w = max(abs(self.eye_outer["L"].x) + abs(self.eye_outer["R"].x),
                          1e-4)
        # Jaw hinge ~ near the ears: outboard of the eyes, mid-height, at eye depth.
        hinge_z = 0.5 * (self.eye_center["L"].z + self.mouth_center.z)
        self.jaw_hinge = {
            s: Vector((self.eye_outer[s].x * 1.15, self.eye_outer[s].y, hinge_z))
            for s in "LR"
        }
        # Jaw shapes ramp in BELOW the upper lip, so the lower jaw drops relative
        # to a fixed upper lip and the lips actually part (starting at the nose
        # drags the upper lip down too and the mouth stays shut).
        self.jaw_top = self.upper_lip.z

    def lip_side(self, s):
        """A point on the lips toward side s (for upperUp / lowerDown etc.)."""
        return self.mouth_center.lerp(self.mouth_corner[s], 0.55)


# --------------------------------------------------------------------------- #
# Recipes — each returns {vertex_index: world-space displacement Vector}.
# Magnitudes are fractions of face scales so they adapt to head size.
# Sign convention: +X = character's left, -Y = front, +Z = up. ARKit "Left"
# (suffix _l, sign +1) is the character's left.
# --------------------------------------------------------------------------- #
_SIGN = {"L": 1.0, "R": -1.0}


# --- brows ----------------------------------------------------------------- #
# Brow/eye/cheek motions scale with eye width (same reason as the mouth: face_h
# over-drives them, especially read up close on head-only meshes).
def _brow_inner_up(ctx):
    out = {}
    for s in "LR":
        _add(out, ctx.wv, ctx.brow_inner[s], ctx.eye_w[s] * 1.0,
             Vector((0, 0, 0.50 * ctx.eye_w[s])))
    return out


def _brow_down(ctx, s):
    out = {}
    v = Vector((0, 0, -0.45 * ctx.eye_w[s]))
    _add(out, ctx.wv, ctx.brow_inner[s], ctx.eye_w[s] * 1.1, v)
    _add(out, ctx.wv, ctx.brow_outer[s], ctx.eye_w[s] * 1.1, v)
    return out


def _brow_outer_up(ctx, s):
    out = {}
    _add(out, ctx.wv, ctx.brow_outer[s], ctx.eye_w[s] * 1.1,
         Vector((0, 0, 0.55 * ctx.eye_w[s])))
    return out


# --- cheeks ---------------------------------------------------------------- #
def _cheek_puff(ctx):
    out = {}
    for s in "LR":
        v = Vector((_SIGN[s] * 0.05 * ctx.head_w, -0.06 * ctx.head_w, 0))
        _add(out, ctx.wv, ctx.cheek[s], ctx.eye_w[s] * 1.1, v)
    return out


def _cheek_squint(ctx, s):
    out = {}
    _add(out, ctx.wv, ctx.cheek[s], ctx.eye_w[s] * 0.9,
         Vector((0, 0, 0.35 * ctx.eye_w[s])))
    return out


# --- eyes ------------------------------------------------------------------ #
def _eye_blink(ctx, s):
    """Close the upper lid: pull eye_top verts down to the eye centerline."""
    out = {}
    c = ctx.eye_top[s]
    cen_z = ctx.eye_center[s].z
    r = ctx.eye_w[s] * 0.7
    for i, p in ctx.wv:
        d = (p - c).length
        if d < r:
            f = _smooth(1.0 - d / r)
            out[i] = Vector((0, 0, (cen_z - p.z) * 0.9 * f))
    return out


def _eye_wide(ctx, s):
    out = {}
    r = ctx.eye_w[s] * 0.6
    _add(out, ctx.wv, ctx.eye_top[s], r, Vector((0, 0, 0.20 * ctx.eye_w[s])))
    _add(out, ctx.wv, ctx.eye_bottom[s], r, Vector((0, 0, -0.14 * ctx.eye_w[s])))
    return out


def _eye_squint(ctx, s):
    out = {}
    _add(out, ctx.wv, ctx.eye_bottom[s], ctx.eye_w[s] * 0.6,
         Vector((0, 0, 0.22 * ctx.eye_w[s])))
    _add(out, ctx.wv, ctx.eye_outer[s], ctx.eye_w[s] * 0.5,
         Vector((-_SIGN[s] * 0.02 * ctx.eye_w[s], 0, 0.12 * ctx.eye_w[s])))
    return out


def _eye_look(ctx, s, dx, dz):
    """Approximate gaze: a small lid-follow shift. Mesh-only eyes have no
    eyeball, so true gaze is bone-driven — this keeps the morph target present
    and plausible without overdriving the lids."""
    out = {}
    _add(out, ctx.wv, ctx.eye_center[s], ctx.eye_w[s] * 0.8,
         Vector((dx * 0.02 * ctx.eye_w[s], 0, dz * 0.12 * ctx.eye_w[s])))
    return out


# --- jaw ------------------------------------------------------------------- #
def _jaw_open(ctx):
    """Open the mouth: rotate the jaw/chin mass about the hinge AND explicitly
    drop the lower-lip region. Rotation alone barely parts the lips (they sit
    right at the no-motion line and just ride down together) — the explicit lip
    drop is what actually opens the mouth."""
    out = {}
    ang = math.radians(15.0)
    rot = Matrix.Rotation(ang, 4, "X")
    hinge = (ctx.jaw_hinge["L"] + ctx.jaw_hinge["R"]) * 0.5
    top, bot = ctx.mouth_center.z, ctx.chin.z
    span = max(top - bot, 1e-6)
    for i, p in ctx.wv:
        if p.z < top:
            w = _smooth(min(1.0, (top - p.z) / span))
            rel = p - hinge
            out[i] = (rot @ rel - rel) * w
    # Part the lips: drop the lower lip clear of the upper.
    _add(out, ctx.wv, ctx.lower_lip, ctx.mouth_w * 0.8,
         Vector((0, 0, -0.5 * ctx.mouth_w)))
    return out


def _jaw_shift(ctx, vec):
    out = {}
    top, bot = ctx.jaw_top, ctx.chin.z
    span = max(top - bot, 1e-6)
    for i, p in ctx.wv:
        if p.z < top:
            w = _smooth(min(1.0, (top - p.z) / span))
            out[i] = vec * w
    return out


# --- mouth ----------------------------------------------------------------- #
# Mouth motions scale with the MOUTH WIDTH, not face height. face_h is ~3× the
# mouth width, so face_h-scaled smiles were ~3× too strong; and the old radii
# (~0.4-0.5 × mouth_w) only reached part of the lips. Radii are now 0.55-0.85 ×
# mouth_w so whole-mouth shapes cover corner-to-corner.
def _mouth_close(ctx):
    out = {}
    r = ctx.mouth_w * 0.85
    mz = ctx.mouth_center.z
    for i, p in ctx.wv:
        du = (p - ctx.upper_lip).length
        if du < r:
            f = _smooth(1.0 - du / r)
            out[i] = Vector((0, 0, (mz - p.z) * 0.5 * f))
        dl = (p - ctx.lower_lip).length
        if dl < r:
            f = _smooth(1.0 - dl / r)
            cur = out.get(i, Vector((0, 0, 0)))
            out[i] = cur + Vector((0, 0, (mz - p.z) * 0.5 * f))
    return out


def _mouth_funnel(ctx):
    out = {}
    fwd = Vector((0, -0.35 * ctx.mouth_w, 0))
    _add(out, ctx.wv, ctx.upper_lip, ctx.mouth_w * 0.8, fwd)
    _add(out, ctx.wv, ctx.lower_lip, ctx.mouth_w * 0.8, fwd)
    for s in "LR":   # corners drawn inward
        _add(out, ctx.wv, ctx.mouth_corner[s], ctx.mouth_w * 0.6,
             Vector((-_SIGN[s] * 0.12 * ctx.mouth_w, -0.12 * ctx.mouth_w, 0)))
    return out


def _mouth_pucker(ctx):
    out = {}
    fwd = Vector((0, -0.5 * ctx.mouth_w, 0))
    _add(out, ctx.wv, ctx.upper_lip, ctx.mouth_w * 0.7, fwd)
    _add(out, ctx.wv, ctx.lower_lip, ctx.mouth_w * 0.7, fwd)
    for s in "LR":
        _add(out, ctx.wv, ctx.mouth_corner[s], ctx.mouth_w * 0.6,
             Vector((-_SIGN[s] * 0.22 * ctx.mouth_w, -0.2 * ctx.mouth_w, 0)))
    return out


def _mouth_shift(ctx, sign):
    """Whole mouth slides sideways (mouthLeft/Right)."""
    out = {}
    v = Vector((sign * 0.12 * ctx.mouth_w, 0, 0))
    for anchor in (ctx.upper_lip, ctx.lower_lip,
                   ctx.mouth_corner["L"], ctx.mouth_corner["R"]):
        _add(out, ctx.wv, anchor, ctx.mouth_w * 0.7, v)
    return out


def _mouth_smile(ctx, s):
    out = {}
    _add(out, ctx.wv, ctx.mouth_corner[s], ctx.mouth_w * 0.65,
         Vector((_SIGN[s] * 0.16 * ctx.mouth_w, 0, 0.30 * ctx.mouth_w)))
    return out


def _mouth_frown(ctx, s):
    out = {}
    _add(out, ctx.wv, ctx.mouth_corner[s], ctx.mouth_w * 0.65,
         Vector((-_SIGN[s] * 0.08 * ctx.mouth_w, 0, -0.26 * ctx.mouth_w)))
    return out


def _mouth_dimple(ctx, s):
    out = {}
    _add(out, ctx.wv, ctx.mouth_corner[s], ctx.mouth_w * 0.4,
         Vector((-_SIGN[s] * 0.06 * ctx.mouth_w, -0.1 * ctx.mouth_w, 0)))
    return out


def _mouth_stretch(ctx, s):
    out = {}
    _add(out, ctx.wv, ctx.mouth_corner[s], ctx.mouth_w * 0.55,
         Vector((_SIGN[s] * 0.18 * ctx.mouth_w, 0, 0)))
    return out


def _mouth_press(ctx, s):
    out = {}
    _add(out, ctx.wv, ctx.lip_side(s), ctx.mouth_w * 0.5,
         Vector((0, 0, 0.08 * ctx.mouth_w)))
    return out


def _mouth_lower_down(ctx, s):
    out = {}
    _add(out, ctx.wv, ctx.lip_side(s), ctx.mouth_w * 0.55,
         Vector((0, 0, -0.28 * ctx.mouth_w)))
    return out


def _mouth_upper_up(ctx, s):
    out = {}
    _add(out, ctx.wv, ctx.lip_side(s), ctx.mouth_w * 0.55,
         Vector((0, 0, 0.28 * ctx.mouth_w)))
    return out


def _mouth_roll_lower(ctx):
    out = {}
    _add(out, ctx.wv, ctx.lower_lip, ctx.mouth_w * 0.7,
         Vector((0, 0.2 * ctx.mouth_w, 0.08 * ctx.mouth_w)))
    return out


def _mouth_roll_upper(ctx):
    out = {}
    _add(out, ctx.wv, ctx.upper_lip, ctx.mouth_w * 0.7,
         Vector((0, 0.2 * ctx.mouth_w, -0.08 * ctx.mouth_w)))
    return out


def _mouth_shrug_lower(ctx):
    out = {}
    _add(out, ctx.wv, ctx.lower_lip, ctx.mouth_w * 0.75,
         Vector((0, -0.12 * ctx.mouth_w, 0.16 * ctx.mouth_w)))
    return out


def _mouth_shrug_upper(ctx):
    out = {}
    _add(out, ctx.wv, ctx.upper_lip, ctx.mouth_w * 0.75,
         Vector((0, -0.12 * ctx.mouth_w, 0.16 * ctx.mouth_w)))
    return out


# --- nose ------------------------------------------------------------------ #
def _nose_sneer(ctx, s):
    out = {}
    _add(out, ctx.wv, ctx.nostril[s], ctx.eye_w[s] * 0.6,
         Vector((0, 0, 0.3 * ctx.eye_w[s])))
    _add(out, ctx.wv, ctx.lip_side(s), ctx.mouth_w * 0.45,
         Vector((0, 0, 0.2 * ctx.mouth_w)))
    return out


# --- tongue ---------------------------------------------------------------- #
def _tongue_out(ctx):
    """Most input heads have no tongue geometry; nudge the mouth interior
    forward so the morph target exists. Visibly a no-op without a tongue mesh."""
    out = {}
    _add(out, ctx.wv, ctx.mouth_center, ctx.mouth_w * 0.3,
         Vector((0, -0.4 * ctx.mouth_w, 0)))
    return out


# --------------------------------------------------------------------------- #
# Registry: every ARKit name -> a ctx-recipe (mirror pairs via closures).
# --------------------------------------------------------------------------- #
def _build_registry():
    r = {
        "browInnerUp": _brow_inner_up,
        "cheekPuff": _cheek_puff,
        "jawOpen": _jaw_open,
        "jawForward": lambda c: _jaw_shift(c, Vector((0, -0.06 * c.head_w, 0))),
        "jawLeft": lambda c: _jaw_shift(c, Vector((0.05 * c.head_w, 0, 0))),
        "jawRight": lambda c: _jaw_shift(c, Vector((-0.05 * c.head_w, 0, 0))),
        "mouthClose": _mouth_close,
        "mouthFunnel": _mouth_funnel,
        "mouthPucker": _mouth_pucker,
        "mouthLeft": lambda c: _mouth_shift(c, 1.0),
        "mouthRight": lambda c: _mouth_shift(c, -1.0),
        "mouthRollLower": _mouth_roll_lower,
        "mouthRollUpper": _mouth_roll_upper,
        "mouthShrugLower": _mouth_shrug_lower,
        "mouthShrugUpper": _mouth_shrug_upper,
        "tongueOut": _tongue_out,
    }

    def pair(prefix, fn):
        for word, s in arkit.SIDE_SUFFIX.items():
            r[f"{prefix}{word}"] = (lambda f, side: (lambda c: f(c, side)))(fn, s)

    pair("browDown", _brow_down)
    pair("browOuterUp", _brow_outer_up)
    pair("cheekSquint", _cheek_squint)
    pair("eyeBlink", _eye_blink)
    pair("eyeSquint", _eye_squint)
    pair("eyeWide", _eye_wide)
    pair("mouthDimple", _mouth_dimple)
    pair("mouthFrown", _mouth_frown)
    pair("mouthLowerDown", _mouth_lower_down)
    pair("mouthPress", _mouth_press)
    pair("mouthSmile", _mouth_smile)
    pair("mouthStretch", _mouth_stretch)
    pair("mouthUpperUp", _mouth_upper_up)
    pair("noseSneer", _nose_sneer)

    # Gaze: (dx toward nose = -sign as "In"), dz up/down.
    for word, s in arkit.SIDE_SUFFIX.items():
        sign = _SIGN[s]
        r[f"eyeLookUp{word}"] = (lambda si: (lambda c: _eye_look(c, si, 0, 1)))(s)
        r[f"eyeLookDown{word}"] = (lambda si: (lambda c: _eye_look(c, si, 0, -1)))(s)
        r[f"eyeLookIn{word}"] = (lambda si, sg: (lambda c: _eye_look(c, si, -sg, 0)))(s, sign)
        r[f"eyeLookOut{word}"] = (lambda si, sg: (lambda c: _eye_look(c, si, sg, 0)))(s, sign)

    return r


REGISTRY = _build_registry()


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def build(obj, markers_xz, H, head_base=None, sets=None, log=lambda *a: None):
    """Create Basis + the 52 ARKit shape keys on obj.data from face markers.

    `head_base` (world Z, e.g. the neck height) confines deformation to the head:
    without it, "below the mouth" shapes (jawOpen) grab the whole torso. `sets`
    is accepted for forward-compat (category filtering); we currently always emit
    the full ARKit-52 so output is contract-complete.
    """
    mw = obj.matrix_world
    mw_inv = mw.inverted()
    wv_all = [(v.index, mw @ v.co) for v in obj.data.vertices]
    world_co = {i: p for i, p in wv_all}

    # Anchors resolve against the full mesh (chin/jaw snap near the neck); the
    # deformable set is gated to head verts so shapes can't move the body.
    anchors = _resolve(markers_xz, wv_all, H)
    if head_base is not None:
        floor = head_base - 0.02
        wv = [(i, p) for i, p in wv_all if p.z >= floor]
    else:
        wv = wv_all
    ctx = Ctx(anchors, wv, H)
    log("face", f"resolved {len(anchors)} face anchors "
                f"(eye {ctx.eye_w_avg:.3f}m, mouth {ctx.mouth_w:.3f}m)")

    if obj.data.shape_keys is None:
        obj.shape_key_add(name="Basis", from_mix=False)

    missing = [n for n in arkit.NAMES if n not in REGISTRY]
    if missing:
        log("face", f"WARNING: no recipe for {len(missing)}: {missing[:5]}...")

    moved_total = 0
    for name in arkit.NAMES:
        fn = REGISTRY.get(name)
        disp = fn(ctx) if fn else {}
        key = obj.shape_key_add(name=name, from_mix=False)
        for i, d in disp.items():
            if d.length_squared > 1e-12:
                key.data[i].co = mw_inv @ (world_co[i] + d)
        moved_total += len(disp)

    log("face", f"built {len(arkit.NAMES)} ARKit shape keys "
                f"({moved_total} vertex moves total)")
