"""
The 52 ARKit facial blendshapes — the canonical name list and grouping.

This is the contract every consumer (iPhone face capture, VTubing apps, game
engines, glTF KHR_materials morph targets) expects. We always emit shape keys
with EXACTLY these names, in this order, so a generated character drops into any
ARKit-driven pipeline. `face_shapekeys.py` provides the deformation for each.

Names use the character's anatomical Left/Right (their left, our +X). Order
matches Apple's ARFaceAnchor.BlendShapeLocation enumeration.
"""

# Canonical 52, in ARKit's documented order.
NAMES = [
    # brows (5)
    "browDownLeft", "browDownRight", "browInnerUp",
    "browOuterUpLeft", "browOuterUpRight",
    # cheeks (3)
    "cheekPuff", "cheekSquintLeft", "cheekSquintRight",
    # eyes (14)
    "eyeBlinkLeft", "eyeBlinkRight",
    "eyeLookDownLeft", "eyeLookDownRight",
    "eyeLookInLeft", "eyeLookInRight",
    "eyeLookOutLeft", "eyeLookOutRight",
    "eyeLookUpLeft", "eyeLookUpRight",
    "eyeSquintLeft", "eyeSquintRight",
    "eyeWideLeft", "eyeWideRight",
    # jaw (4)
    "jawForward", "jawLeft", "jawRight", "jawOpen",
    # mouth (23)
    "mouthClose", "mouthDimpleLeft", "mouthDimpleRight",
    "mouthFrownLeft", "mouthFrownRight", "mouthFunnel",
    "mouthLeft", "mouthLowerDownLeft", "mouthLowerDownRight",
    "mouthPressLeft", "mouthPressRight", "mouthPucker", "mouthRight",
    "mouthRollLower", "mouthRollUpper", "mouthShrugLower", "mouthShrugUpper",
    "mouthSmileLeft", "mouthSmileRight", "mouthStretchLeft", "mouthStretchRight",
    "mouthUpperUpLeft", "mouthUpperUpRight",
    # nose (2)
    "noseSneerLeft", "noseSneerRight",
    # tongue (1)
    "tongueOut",
]

assert len(NAMES) == 52, f"ARKit set must be 52, got {len(NAMES)}"

# Convenience: the side suffix convention. ARKit "Left" == character's left == +X.
SIDE_SUFFIX = {"Left": "L", "Right": "R"}
