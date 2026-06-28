"""
Retopology engine layer.

Locates a usable engine (Blender headless with QuadriFlow, or an Instant Meshes
binary) and runs it on an input model to produce all-quad output.

Engine resolution order:
  1. Instant Meshes  -- if INSTANT_MESHES_PATH is set and points at a binary
  2. Blender         -- BLENDER_PATH env, then PATH, then common install dirs
"""
import os
import glob
import json
import shutil
import subprocess
import platform

HERE = os.path.dirname(os.path.abspath(__file__))
BLENDER_SCRIPT = os.path.join(HERE, "blender_remesh.py")

# Formats Blender can round-trip. Instant Meshes only handles obj/ply.
BLENDER_FORMATS = {".obj", ".glb", ".gltf", ".fbx", ".ply", ".stl"}
INSTANT_FORMATS = {".obj", ".ply"}


def _find_blender():
    if os.environ.get("BLENDER_PATH") and os.path.isfile(os.environ["BLENDER_PATH"]):
        return os.environ["BLENDER_PATH"]

    found = shutil.which("blender")
    if found:
        return found

    candidates = []
    system = platform.system()
    if system == "Windows":
        for base in (r"C:\Program Files\Blender Foundation",
                     r"C:\Program Files (x86)\Blender Foundation"):
            candidates += glob.glob(os.path.join(base, "Blender*", "blender.exe"))
    elif system == "Darwin":
        candidates += glob.glob("/Applications/Blender*.app/Contents/MacOS/Blender")
        candidates.append("/Applications/Blender.app/Contents/MacOS/Blender")
    else:  # Linux
        candidates += ["/usr/bin/blender", "/usr/local/bin/blender",
                       "/snap/bin/blender"]
        candidates += glob.glob("/opt/blender*/blender")

    # Prefer the highest-versioned path that exists.
    existing = [c for c in candidates if os.path.isfile(c)]
    existing.sort(reverse=True)
    return existing[0] if existing else None


def _find_instant_meshes():
    p = os.environ.get("INSTANT_MESHES_PATH")
    if p and os.path.isfile(p):
        return p
    found = shutil.which("Instant Meshes") or shutil.which("instant-meshes")
    return found


def detect_engine():
    """Return dict describing the best available engine, or none."""
    im = _find_instant_meshes()
    if im:
        return {"name": "instant-meshes", "path": im, "formats": sorted(INSTANT_FORMATS)}
    bl = _find_blender()
    if bl:
        return {"name": "blender", "path": bl, "formats": sorted(BLENDER_FORMATS)}
    return {"name": None, "path": None, "formats": []}


def _run_blender(blender, in_path, out_path, faces, sym_axis, sharp,
                 topology="quad", convert_only=False):
    cmd = [
        blender, "--background", "--factory-startup",
        "--python", BLENDER_SCRIPT, "--",
        "--input", in_path,
        "--output", out_path,
        "--faces", str(faces),
        "--sym-axis", str(sym_axis or "none"),
        "--topology", str(topology or "quad"),
    ]
    if not sharp:
        cmd.append("--no-sharp")
    if convert_only:
        cmd.append("--convert-only")

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    stdout = proc.stdout or ""
    result_line = None
    for line in stdout.splitlines():
        if line.startswith("RETOPO_RESULT:"):
            result_line = line[len("RETOPO_RESULT:"):]
    if result_line is None:
        raise RuntimeError(
            "Blender produced no result. Exit %s.\n%s"
            % (proc.returncode, (proc.stderr or stdout)[-2000:])
        )
    data = json.loads(result_line)
    if not data.get("ok"):
        raise RuntimeError("Retopology failed: %s" % data.get("error", "unknown"))
    return data


def _run_instant_meshes(binary, in_path, out_path, faces, **_):
    # Instant Meshes CLI: -o output -f <face count> input
    cmd = [binary, "-o", out_path, "-f", str(faces), in_path]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0 or not os.path.isfile(out_path):
        raise RuntimeError(
            "Instant Meshes failed (exit %s): %s"
            % (proc.returncode, (proc.stderr or proc.stdout)[-2000:])
        )
    # Instant Meshes doesn't report stats; leave them for the caller to compute.
    return {"ok": True, "engine": "instant-meshes", "output": out_path,
            "source": {}, "result": {}}


def run_retopology(in_path, out_path, faces=5000, sym_axis="none", sharp=True,
                   topology="quad"):
    want_symmetry = str(sym_axis).lower() in ("x", "y", "z")
    # Instant Meshes only does pure quads; anything else is Blender's job.
    blender_only = want_symmetry or str(topology).lower() != "quad"
    engine = detect_engine()
    if engine["name"] == "instant-meshes":
        ext_in = os.path.splitext(in_path)[1].lower()
        ext_out = os.path.splitext(out_path)[1].lower()
        if not blender_only and ext_in in INSTANT_FORMATS and ext_out in INSTANT_FORMATS:
            data = _run_instant_meshes(engine["path"], in_path, out_path, faces)
            data["engine"] = "instant-meshes"
            return data
        # fall through to Blender for symmetry / topology / unsupported formats
        bl = _find_blender()
        if not bl:
            raise RuntimeError(
                "Instant Meshes can't do this (symmetry, topology mode, or format "
                "%s); install Blender." % ext_in
            )
        data = _run_blender(bl, in_path, out_path, faces, sym_axis, sharp, topology)
        data["engine"] = "blender"
        return data

    if engine["name"] == "blender":
        data = _run_blender(engine["path"], in_path, out_path, faces, sym_axis,
                            sharp, topology)
        data["engine"] = "blender"
        return data

    raise RuntimeError(
        "No retopology engine found. Install Blender (https://www.blender.org/download/) "
        "and ensure it is on PATH or set BLENDER_PATH."
    )


def convert_format(in_path, out_path):
    """Re-encode an existing mesh into another format without remeshing.

    Used to deliver the quad result in formats Instant Meshes can't write,
    or to derive a download format from the canonical OBJ result. Requires
    Blender; Instant Meshes has no conversion mode.
    """
    bl = _find_blender()
    if not bl:
        raise RuntimeError("Blender is required to export this format.")
    return _run_blender(bl, in_path, out_path, faces=0, sym_axis="none",
                        sharp=True, topology="quad", convert_only=True)
