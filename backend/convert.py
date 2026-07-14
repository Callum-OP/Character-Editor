"""
Engine layer for the Format Converter tool. Drives the Blender headless
convert script, which re-exports the whole scene (objects, rigs, animations,
materials, textures) unchanged. Requires Blender (reuses retopo's detection).
"""
import os
import json
import subprocess

import retopo
import paths

CONVERT_SCRIPT = os.path.join(paths.resource_dir(), "blender_convert.py")


def _blender():
    bl = retopo._find_blender()
    if not bl:
        raise RuntimeError(
            "The converter needs Blender. Install it "
            "(https://www.blender.org/download/) and ensure it is on PATH or "
            "set BLENDER_PATH."
        )
    return bl


def run_convert(in_path, out_path, embed_textures=True, strip_rig=False,
                draco=False, max_texture=0):
    """Re-encode a model into another format, preserving the full scene.

    embed_textures=False writes FBX textures as sidecar image files instead of
    packing them into the .fbx; strip_rig removes armatures/animations (both
    for strict importers). draco enables Draco mesh compression for glTF/GLB;
    max_texture > 0 scales oversized textures down."""
    cmd = [
        _blender(), "--background", "--factory-startup", "--python", CONVERT_SCRIPT,
        "--", "--input", in_path, "--output", out_path,
    ]
    if not embed_textures:
        cmd.append("--no-embed")
    if strip_rig:
        cmd.append("--strip-rig")
    if draco:
        cmd.append("--draco")
    if max_texture:
        cmd += ["--max-texture", str(int(max_texture))]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    result_line = None
    for line in (proc.stdout or "").splitlines():
        if line.startswith("CONVERT_RESULT:"):
            result_line = line[len("CONVERT_RESULT:"):]
    if result_line is None:
        raise RuntimeError(
            "Conversion produced no result. Exit %s.\n%s"
            % (proc.returncode, (proc.stderr or proc.stdout)[-2000:])
        )
    data = json.loads(result_line)
    if not data.get("ok"):
        raise RuntimeError("Conversion failed: %s" % data.get("error", "unknown"))
    data["engine"] = "blender"
    return data
