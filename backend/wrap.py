"""
Engine layer for the Shape Match (Wrap) tool. Drives the Blender headless
wrap script. Requires Blender (reuses retopo's Blender detection).
"""
import os
import json
import subprocess

import retopo

HERE = os.path.dirname(os.path.abspath(__file__))
WRAP_SCRIPT = os.path.join(HERE, "blender_wrap.py")


def run_wrap(reference, source, output, view_output, strength=1.0,
             smooth_iters=3, shape_keys="preserve", align="bbox"):
    bl = retopo._find_blender()
    if not bl:
        raise RuntimeError(
            "The wrap tool needs Blender. Install it (https://www.blender.org/download/) "
            "and ensure it is on PATH or set BLENDER_PATH."
        )
    cmd = [
        bl, "--background", "--factory-startup", "--python", WRAP_SCRIPT, "--",
        "--reference", reference, "--source", source, "--output", output,
        "--view-output", view_output, "--strength", str(strength),
        "--smooth-iters", str(int(smooth_iters)),
        "--shape-keys", shape_keys, "--align", align,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    result_line = None
    for line in (proc.stdout or "").splitlines():
        if line.startswith("RETOPO_RESULT:"):
            result_line = line[len("RETOPO_RESULT:"):]
    if result_line is None:
        raise RuntimeError(
            "Wrap produced no result. Exit %s.\n%s"
            % (proc.returncode, (proc.stderr or proc.stdout)[-2000:])
        )
    data = json.loads(result_line)
    if not data.get("ok"):
        raise RuntimeError("Wrap failed: %s" % data.get("error", "unknown"))
    data["engine"] = "blender"
    return data
