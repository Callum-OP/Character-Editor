"""
Engine layer for the Mesh Cleanup / LOD tool. Drives the Blender headless
clean script. Requires Blender (reuses retopo's Blender detection).
"""
import os
import json
import subprocess

import retopo
import paths

CLEAN_SCRIPT = os.path.join(paths.resource_dir(), "blender_clean.py")


def _blender():
    bl = retopo._find_blender()
    if not bl:
        raise RuntimeError(
            "The cleanup tool needs Blender. Install it "
            "(https://www.blender.org/download/) and ensure it is on PATH or "
            "set BLENDER_PATH."
        )
    return bl


def _run(cmd):
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    result_line = None
    for line in (proc.stdout or "").splitlines():
        if line.startswith("CLEAN_RESULT:"):
            result_line = line[len("CLEAN_RESULT:"):]
    if result_line is None:
        raise RuntimeError(
            "Cleanup produced no result. Exit %s.\n%s"
            % (proc.returncode, (proc.stderr or proc.stdout)[-2000:])
        )
    data = json.loads(result_line)
    if not data.get("ok"):
        raise RuntimeError("Cleanup failed: %s" % data.get("error", "unknown"))
    data["engine"] = "blender"
    return data


def run_clean(in_path, out_path, view_path, ops):
    """Repair a mesh. `ops` is a dict of booleans/floats:
    merge_dist, fix_normals, fill_holes, delete_loose, degenerate, flip."""
    cmd = [
        _blender(), "--background", "--factory-startup", "--python", CLEAN_SCRIPT,
        "--", "--mode", "clean", "--input", in_path,
        "--output", out_path, "--view-output", view_path,
        "--merge-dist", str(float(ops.get("merge_dist", 0.0))),
    ]
    if ops.get("fix_normals"):
        cmd.append("--fix-normals")
    if ops.get("fill_holes"):
        cmd.append("--fill-holes")
    if ops.get("delete_loose"):
        cmd.append("--delete-loose")
    if ops.get("degenerate"):
        cmd.append("--degenerate")
    if ops.get("flip"):
        cmd.append("--flip")
    return _run(cmd)


def run_lod(in_path, out_dir, ratios, out_ext=".glb", target_faces=0,
            preserve_uv=True, preserve_boundary=True):
    """Generate decimated LOD meshes. `ratios` is a list of collapse ratios
    (0-1); if `target_faces` > 0 it wins and a single level is produced."""
    levels_csv = ",".join(str(r) for r in ratios) if ratios else "0.5,0.25,0.125"
    cmd = [
        _blender(), "--background", "--factory-startup", "--python", CLEAN_SCRIPT,
        "--", "--mode", "lod", "--input", in_path,
        "--out-dir", out_dir, "--out-ext", out_ext,
        "--lod-levels", levels_csv, "--target-faces", str(int(target_faces)),
    ]
    if preserve_uv:
        cmd.append("--preserve-uv")
    if preserve_boundary:
        cmd.append("--preserve-boundary")
    return _run(cmd)
