"""
Landmark detection for humanoid auto-rigging.

Given a normalized mesh (Z up, feet at Z=0, facing -Y, arms extending along
+/-X in a T/A pose), find the real positions of skeletal joints by analyzing
horizontal cross-sections of the mesh.

The core idea: slice the body into thin horizontal slabs along Z and, for each
slab, measure width, depth, centroid, and how many separated X-clusters the
vertices form. From those profiles the key joints fall out:

  * crotch   -> highest Z where the legs are still two separate X-clusters
  * leg X    -> centers of those two clusters
  * hand     -> the slab whose vertices reach furthest out in X
  * neck     -> narrowest cross-section in the upper body
  * shoulder -> just below the neck, at the edge of the torso
  * head top -> the topmost vertex

Endpoints (hands, feet, crotch, neck) are mesh-derived; intermediate joints
(elbow, knee, spine) are interpolated between them. Anything detection can't
find confidently falls back to standard body proportions, so the function
always returns a complete, usable landmark set.

detect_landmarks(points, H) takes a world-space point cloud (anything with
.x/.y/.z, e.g. mathutils Vectors) and returns a flat dict of scalars consumed
by pipeline.build_skeleton(). X values are magnitudes (>=0); left/right sign is
applied by the skeleton builder.
"""


# --------------------------------------------------------------------------- #
# Small numeric helpers (kept dependency-free so this stays easy to test).
# --------------------------------------------------------------------------- #
def _median(xs):
    xs = sorted(xs)
    n = len(xs)
    if n == 0:
        return None
    mid = n // 2
    return xs[mid] if n % 2 else 0.5 * (xs[mid - 1] + xs[mid])


def _lerp(a, b, t):
    return a + (b - a) * t


def _cluster_1d(xs, gap):
    """Split sorted X values into clusters separated by gaps larger than `gap`.

    Returns a list of (min, max, center, count) tuples, left-to-right.
    """
    xs = sorted(xs)
    clusters = []
    members = [xs[0]]
    prev = xs[0]
    for x in xs[1:]:
        if x - prev > gap:
            clusters.append(members)
            members = []
        members.append(x)
        prev = x
    clusters.append(members)
    return [(m[0], m[-1], sum(m) / len(m), len(m)) for m in clusters]


# --------------------------------------------------------------------------- #
# Proportional fallback — standard humanoid ratios as fractions of height H,
# measured from the floor. Used to seed the result and to fill any landmark
# that cross-section detection can't pin down.
# --------------------------------------------------------------------------- #
# Where the mid-limb joint sits along the limb, as a fraction from the root.
# >0.5 pulls the elbow toward the hand and the knee toward the foot, which
# reads more naturally (and matches Clip-Studio-style figures).
ELBOW_BIAS = 0.58
KNEE_BIAS = 0.58
# The navel marks the hips/torso boundary: the Hips section ends here, and
# Waist / Lower-torso / Upper-torso (Spine / Spine1 / Chest) split the
# navel -> neck span into equal thirds above it.
NAVEL_FRAC = 0.30    # navel height as a fraction from the crotch up to the neck
# Ball of foot as a fraction of the foot's forward reach (rest goes to the toe).
BALL_FRAC = 0.6


def proportional_landmarks(H):
    return {
        # spine chain (centered on X=0): hips -> waist -> abdomen -> chest
        "hips_z":     0.53 * H,
        "spine_z":    0.62 * H,   # waist (navel)
        "spine1_z":   0.69 * H,   # lower torso
        "chest_z":    0.76 * H,   # upper torso
        "neck_z":     0.83 * H,
        "head_z":     0.88 * H,
        "head_top_z": 1.00 * H,
        # arms (x = magnitude from center)
        "shoulder_x": 0.10 * H, "shoulder_z": 0.82 * H,
        "elbow_x":    0.18 * H, "elbow_z":    0.61 * H,
        "wrist_x":    0.24 * H, "wrist_z":    0.45 * H,
        "hand_x":     0.27 * H, "hand_z":     0.42 * H,
        # legs
        "hip_x":      0.09 * H,
        "knee_x":     0.09 * H, "knee_z":     0.26 * H,
        "ankle_x":    0.09 * H, "ankle_z":    0.04 * H,
        "ball_y":     -0.07 * H,   # ball of foot (Foot -> Toe split)
        "foot_tip_y": -0.12 * H,   # toe tip
    }


# --------------------------------------------------------------------------- #
# Cross-section profiling
# --------------------------------------------------------------------------- #
def _build_profile(points, n_slabs, leg_gap):
    if not points:
        return None, None, None
    min_z = min(p.z for p in points)
    max_z = max(p.z for p in points)
    span = max_z - min_z
    if span <= 1e-6:
        return None, None, None

    slab_h = span / n_slabs
    slabs = [[] for _ in range(n_slabs)]
    for p in points:
        idx = int((p.z - min_z) / slab_h)
        if idx >= n_slabs:
            idx = n_slabs - 1
        slabs[idx].append(p)

    rows = []
    for i, sv in enumerate(slabs):
        z = min_z + (i + 0.5) * slab_h
        if not sv:
            rows.append(None)
            continue
        xs = [p.x for p in sv]
        ys = [p.y for p in sv]
        rows.append({
            "z": z,
            "xmin": min(xs), "xmax": max(xs),
            "xw": max(xs) - min(xs),
            "yw": max(ys) - min(ys),
            "ymin": min(ys),
            "cx": sum(xs) / len(xs),
            "clusters": _cluster_1d(xs, leg_gap),
        })
    return rows, min_z, max_z


def _central_width(row):
    """Width of the cluster nearest X=0 (the torso/limb-free trunk)."""
    c = min(row["clusters"], key=lambda c: abs(c[2]))
    return c[1] - c[0]


def _profile_min(thick, t_lo, t_hi):
    """Index of the thinnest valid bin whose normalized position is in [t_lo,t_hi].

    Joints (wrist, elbow, knee) sit where a limb is narrowest, so a thickness
    minimum in the right span locates them — more accurate than a fixed bias.
    Returns None if no bin in range has data.
    """
    n = len(thick)
    cand = [(thick[i], i) for i in range(n)
            if thick[i] is not None and t_lo <= (i + 0.5) / n <= t_hi]
    return min(cand)[1] if cand else None


def _analyze_arm(points, sign, sh_x, hand_x, arm_z, H):
    """Scan one arm along X (it extends ±X in a T/A pose) for the wrist + elbow.

    Bins arm vertices by distance out from the shoulder; each bin's thickness is
    its Y+Z spread (the limb cross-section). The wrist is the thinnest bin in the
    distal span (before the hand widens), the elbow the thinnest in the mid span.
    Bin centroid-Z is tracked so joints follow a drooping (A-pose) arm. Returns a
    dict of magnitudes/heights, or None if the arm is too sparse.
    """
    reach = hand_x - sh_x
    if reach < 0.05 * H:
        return None
    arm = [p for p in points
           if sign * p.x > sh_x * 0.7 and sign * p.x < hand_x + 0.04 * H
           and abs(p.z - arm_z) < 0.14 * H]
    if len(arm) < 30:
        return None
    nb = 20
    bins = [[] for _ in range(nb)]
    for p in arm:
        t = (sign * p.x - sh_x) / reach          # 0 = shoulder, 1 = hand
        if 0.0 <= t < 1.0:
            bins[int(t * nb)].append(p)
    cz = [None] * nb
    th = [None] * nb
    for i, b in enumerate(bins):
        if len(b) < 4:
            continue
        ys = [p.y for p in b]
        zs = [p.z for p in b]
        cz[i] = sum(zs) / len(zs)
        th[i] = (max(ys) - min(ys)) + (max(zs) - min(zs))

    def z_at(i):
        for d in range(nb):
            for j in (i - d, i + d):
                if 0 <= j < nb and cz[j] is not None:
                    return cz[j]
        return arm_z

    # Arm height = median centroid-Z of the mid arm (the tube), NOT the
    # shoulder end (which catches the trapezius/neck and reads too high).
    mid_z = _median([cz[i] for i in range(nb)
                     if cz[i] is not None and 0.15 <= (i + 0.5) / nb <= 0.75])
    res = {"arm_z": mid_z if mid_z is not None else z_at(0)}
    wi = _profile_min(th, 0.58, 0.92)             # wrist: distal narrowing
    if wi is not None:
        res["wrist_x"] = sh_x + reach * ((wi + 0.5) / nb)
        res["wrist_z"] = z_at(wi)
    ei = _profile_min(th, 0.32, 0.62)             # elbow: mid-arm narrowing
    if ei is not None:
        res["elbow_x"] = sh_x + reach * ((ei + 0.5) / nb)
        res["elbow_z"] = z_at(ei)
    return res


def _analyze_leg(points, sign, leg_x, hips_z, ankle_z, H):
    """Scan one leg along Z for the knee (the narrowing between thigh and calf)."""
    span = hips_z - ankle_z
    if span < 0.1 * H:
        return None
    leg = [p for p in points
           if abs(p.x - sign * leg_x) < 0.09 * H and ankle_z < p.z < hips_z]
    if len(leg) < 30:
        return None
    nb = 20
    bins = [[] for _ in range(nb)]
    for p in leg:
        t = (p.z - ankle_z) / span               # 0 = ankle, 1 = hip
        if 0.0 <= t < 1.0:
            bins[int(t * nb)].append(p)
    th = [None] * nb
    for i, b in enumerate(bins):
        if len(b) < 4:
            continue
        xs = [p.x for p in b]
        ys = [p.y for p in b]
        th[i] = (max(xs) - min(xs)) + (max(ys) - min(ys))
    ki = _profile_min(th, 0.40, 0.65)             # knee ~ mid leg
    if ki is None:
        return None
    return {"knee_z": ankle_z + span * ((ki + 0.5) / nb)}


def _mean(vals):
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


# --------------------------------------------------------------------------- #
# Main detection
# --------------------------------------------------------------------------- #
def detect_landmarks(points, H, n_slabs=160, log=lambda *a: None):
    lm = proportional_landmarks(H)
    leg_gap = 0.035 * H

    rows, min_z, max_z = _build_profile(points, n_slabs, leg_gap)
    if rows is None:
        log("landmark", "profiling failed — using proportional fallback")
        return lm

    valid = [r for r in rows if r]
    span = max_z - min_z

    # --- legs: crotch = highest Z (in lower body) with two X-clusters -------- #
    leg_rows = [r for r in valid
                if r["z"] < min_z + 0.55 * span and len(r["clusters"]) >= 2]
    if leg_rows:
        crotch_z = max(r["z"] for r in leg_rows)
        # leg X centers, sampled from the lower legs (most reliably separated)
        low = [r for r in leg_rows if r["z"] < min_z + 0.30 * span] or leg_rows
        leg_xs = [abs(c[2]) for r in low for c in r["clusters"]]
        leg_x = _median(leg_xs)
        if leg_x and leg_x > 1e-4:
            # The crotch is only reliable when the thighs are visibly apart.
            # When they touch (common on real characters) the legs separate
            # only near the calves, dragging the crotch far too low — so trust
            # the detection only inside a plausible band, else use proportion.
            if min_z + 0.44 * span <= crotch_z <= min_z + 0.57 * span:
                lm["hips_z"] = crotch_z
                src = f"detected @ {crotch_z:.2f}m"
            else:
                lm["hips_z"] = min_z + 0.50 * span
                src = f"fallback @ {lm['hips_z']:.2f}m (implausible crotch {crotch_z:.2f})"
            lm["hip_x"] = leg_x
            lm["knee_x"] = leg_x
            lm["ankle_x"] = leg_x
            log("landmark", f"crotch {src}, leg X +/-{leg_x:.2f}m")

    # --- ankle + foot: bottom slab geometry ---------------------------------- #
    bottom = [r for r in valid if r["z"] < min_z + 0.12 * span]
    if bottom:
        lm["ankle_z"] = min_z + 0.04 * span
        lm["foot_tip_y"] = min(r["ymin"] for r in bottom)  # forward (-Y) reach
        lm["ball_y"] = lm["foot_tip_y"] * BALL_FRAC        # Foot -> Toe split

    # knee: detect the thigh/calf narrowing per leg (avg both), else bias.
    lm["knee_z"] = _lerp(lm["hips_z"], lm["ankle_z"], KNEE_BIAS)
    knee_zs = [(_analyze_leg(points, s, lm["knee_x"], lm["hips_z"], lm["ankle_z"], H)
                or {}).get("knee_z") for s in (1, -1)]
    knee_det = _mean(knee_zs)
    if knee_det is not None:
        lm["knee_z"] = knee_det
        log("landmark", f"knee detected @ {knee_det:.2f}m")

    # --- neck: narrowest central cross-section, searched high so it lands on --- #
    # the actual neck and doesn't swallow the upper torso.
    upper = [r for r in valid
             if min_z + 0.80 * span < r["z"] < min_z + 0.96 * span]
    if upper:
        neck = min(upper, key=_central_width)
        neck_z = neck["z"]
        lm["neck_z"] = neck_z
        lm["head_z"] = min(neck_z + 0.03 * H, max_z)
        lm["head_top_z"] = max_z
        # Shoulders sit just below the neck, at the TORSO edge. Measure torso
        # width from a band BELOW the arms (mid-upper torso): at shoulder height
        # a T-pose arm merges with the torso cross-section and would otherwise
        # blow the width up to the full armspan (placing the shoulder near the
        # hand). The narrowest sample in the band best resists arm contamination.
        sh_z = neck_z - 0.04 * H
        lm["shoulder_z"] = sh_z
        torso_band = [r for r in valid
                      if min_z + 0.55 * span < r["z"] < min_z + 0.72 * span]
        if torso_band:
            torso_half = min(_central_width(r) for r in torso_band) * 0.5
            # Shoulder joint sits at the torso edge (arm root), not inside it, so
            # the upper-arm bone starts where the arm actually attaches.
            lm["shoulder_x"] = max(torso_half, 0.04 * H)
        # Torso sections: Hips end at the navel; Waist / Lower-torso / Upper-torso
        # (Spine / Spine1 / Chest) split the navel -> neck span into equal thirds.
        navel_z = _lerp(lm["hips_z"], neck_z, NAVEL_FRAC)
        third = (neck_z - navel_z) / 3.0
        lm["spine_z"] = navel_z
        lm["spine1_z"] = navel_z + third
        lm["chest_z"] = navel_z + 2.0 * third
        log("landmark", f"neck @ {neck_z:.2f}m, navel @ {navel_z:.2f}m, "
                        f"shoulders @ {lm['shoulder_z']:.2f}m")

    # --- hands: slab reaching furthest out in X ------------------------------ #
    reach = max(valid, key=lambda r: r["xmax"])
    hand_x = reach["xmax"]
    if hand_x > lm["shoulder_x"] * 1.2:  # arms actually extend outward
        hand_z = reach["z"]
        lm["hand_x"] = hand_x
        lm["hand_z"] = hand_z
        # Defaults (used if per-arm geometry analysis can't resolve a joint).
        lm["wrist_x"] = hand_x * 0.88
        lm["wrist_z"] = hand_z
        lm["elbow_x"] = _lerp(lm["shoulder_x"], lm["wrist_x"], ELBOW_BIAS)
        lm["elbow_z"] = _lerp(lm["shoulder_z"], lm["wrist_z"], ELBOW_BIAS)
        log("landmark", f"hand reach +/-{hand_x:.2f}m @ {hand_z:.2f}m")

        # Per-arm scan for the real wrist + elbow (average both sides).
        arms = [_analyze_arm(points, s, lm["shoulder_x"], hand_x, hand_z, H)
                for s in (1, -1)]
        arms = [a for a in arms if a]
        if arms:
            wx = _mean([a.get("wrist_x") for a in arms])
            wz = _mean([a.get("wrist_z") for a in arms])
            ex = _mean([a.get("elbow_x") for a in arms])
            ez = _mean([a.get("elbow_z") for a in arms])
            if wx is not None:
                lm["wrist_x"], lm["wrist_z"] = wx, wz
            if ex is not None:
                lm["elbow_x"], lm["elbow_z"] = ex, ez
            # Align the shoulder height with the arm so the upper-arm bone runs
            # along the arm instead of slanting down from under the neck.
            arm_z = _mean([a.get("arm_z") for a in arms])
            if arm_z is not None:
                lm["shoulder_z"] = arm_z
            log("landmark",
                f"arm joints: elbow +/-{lm['elbow_x']:.2f}@{lm['elbow_z']:.2f}, "
                f"wrist +/-{lm['wrist_x']:.2f}@{lm['wrist_z']:.2f}, "
                f"shoulder z {lm['shoulder_z']:.2f}")

    return lm
