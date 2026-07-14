"""
FastAPI server for Character Editor — the 3D character & mesh toolkit hub.

Endpoints:
  GET  /                 -> serves the hub frontend
  GET  /api/engine       -> which retopology engine is available
  POST /api/retopo       -> upload a model, get back quad-remeshed model + stats
  POST /api/wrap         -> upload reference + source, get a conformed model
  POST /api/rig/prep     -> detect joint/face markers + front view for editing
  POST /api/rig/build    -> build the rig / face shape keys from markers
  POST /api/cloth/convert -> transcode a browser-draped garment to FBX/etc.
  POST /api/convert      -> general format converter (any-to-any, whole scene)
  GET  /api/download/{id}/{name} -> fetch a produced result file
"""
import os
import re
import json
import uuid
import shutil
import time
import zipfile

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import retopo
import wrap
import rig
import clean
import convert

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(HERE, "work")
FRONTEND = os.path.normpath(os.path.join(HERE, "..", "frontend"))
os.makedirs(WORK, exist_ok=True)

SUPPORTED_IN = {".obj", ".glb", ".gltf", ".fbx", ".ply", ".stl"}
SUPPORTED_OUT = {".obj", ".glb", ".gltf", ".fbx", ".ply", ".stl"}

app = FastAPI(title="Character Editor")


@app.middleware("http")
async def always_revalidate(request: Request, call_next):
    """Static assets (JS/HTML/CSS) are served with only ETag/Last-Modified, so
    browsers fall back to *heuristic* caching and can serve a stale script for
    hours without asking the server — e.g. an old tool page missing a newly
    added event listener. Forcing `no-cache` keeps revalidation cheap (304s via
    the ETag) while guaranteeing the browser never runs stale frontend code."""
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/api/engine")
def engine():
    return retopo.detect_engine()


@app.post("/api/retopo")
async def retopo_endpoint(
    file: UploadFile = File(...),
    faces: int = Form(5000),
    out_format: str = Form("obj"),
    sym_axis: str = Form("none"),
    topology: str = Form("quad"),
    sharp: bool = Form(True),
):
    sym_axis = sym_axis.lower()
    if sym_axis not in ("none", "x", "y", "z"):
        raise HTTPException(400, "sym_axis must be one of none/x/y/z")
    topology = topology.lower()
    if topology not in ("quad", "quad_dominant", "triangle"):
        raise HTTPException(400, "topology must be quad/quad_dominant/triangle")
    in_ext = os.path.splitext(file.filename or "")[1].lower()
    if in_ext not in SUPPORTED_IN:
        raise HTTPException(400, f"Unsupported input format '{in_ext}'. "
                                 f"Allowed: {sorted(SUPPORTED_IN)}")

    out_ext = "." + out_format.lower().lstrip(".")
    if out_ext not in SUPPORTED_OUT:
        raise HTTPException(400, f"Unsupported output format '{out_ext}'.")

    faces = max(50, min(int(faces), 200000))

    job_id = uuid.uuid4().hex
    job_dir = os.path.join(WORK, job_id)
    os.makedirs(job_dir, exist_ok=True)

    in_path = os.path.join(job_dir, "input" + in_ext)
    # always keep an OBJ result for the viewer's true-quad wireframe,
    # plus the user's requested download format if different.
    view_path = os.path.join(job_dir, "result.obj")
    out_name = "result" + out_ext
    out_path = os.path.join(job_dir, out_name)

    with open(in_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    try:
        data = retopo.run_retopology(
            in_path, view_path, faces=faces, sym_axis=sym_axis, sharp=sharp,
            topology=topology
        )
        # The viewer always uses the canonical OBJ. For a different download
        # format, re-encode that exact quad mesh (no second remesh) so the
        # download matches the preview topology precisely.
        if out_ext != ".obj":
            retopo.convert_format(view_path, out_path)
        else:
            out_path = view_path
            out_name = "result.obj"
    except Exception as e:
        raise HTTPException(500, str(e))

    return JSONResponse({
        "job_id": job_id,
        "stats": data,
        "view_url": f"/api/download/{job_id}/result.obj",
        "download_url": f"/api/download/{job_id}/{out_name}",
        "download_name": out_name,
    })


@app.post("/api/clean")
async def clean_endpoint(
    file: UploadFile = File(...),
    out_format: str = Form("glb"),
    merge_dist: float = Form(0.0001),
    fix_normals: bool = Form(True),
    fill_holes: bool = Form(False),
    delete_loose: bool = Form(True),
    degenerate: bool = Form(True),
    flip: bool = Form(False),
):
    in_ext = os.path.splitext(file.filename or "")[1].lower()
    if in_ext not in SUPPORTED_IN:
        raise HTTPException(400, f"Unsupported input format '{in_ext}'. "
                                 f"Allowed: {sorted(SUPPORTED_IN)}")
    out_ext = "." + out_format.lower().lstrip(".")
    if out_ext not in SUPPORTED_OUT:
        raise HTTPException(400, f"Unsupported output format '{out_ext}'.")
    merge_dist = max(0.0, min(float(merge_dist), 1.0))

    job_id = uuid.uuid4().hex
    job_dir = os.path.join(WORK, job_id)
    os.makedirs(job_dir, exist_ok=True)
    in_path = os.path.join(job_dir, "input" + in_ext)
    view_path = os.path.join(job_dir, "result.obj")
    out_name = "result" + out_ext
    out_path = os.path.join(job_dir, out_name)
    with open(in_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    try:
        data = clean.run_clean(in_path, out_path, view_path, {
            "merge_dist": merge_dist, "fix_normals": fix_normals,
            "fill_holes": fill_holes, "delete_loose": delete_loose,
            "degenerate": degenerate, "flip": flip,
        })
        if out_ext == ".obj":
            out_path = view_path
            out_name = "result.obj"
    except Exception as e:
        raise HTTPException(500, str(e))

    return JSONResponse({
        "job_id": job_id,
        "stats": data,
        "view_url": f"/api/download/{job_id}/result.obj",
        "download_url": f"/api/download/{job_id}/{out_name}",
        "download_name": out_name,
    })


@app.post("/api/lod")
async def lod_endpoint(
    file: UploadFile = File(...),
    out_format: str = Form("glb"),
    ratios: str = Form("0.5,0.25,0.125"),
    target_faces: int = Form(0),
    preserve_uv: bool = Form(True),
    preserve_boundary: bool = Form(True),
):
    in_ext = os.path.splitext(file.filename or "")[1].lower()
    if in_ext not in SUPPORTED_IN:
        raise HTTPException(400, f"Unsupported input format '{in_ext}'.")
    out_ext = "." + out_format.lower().lstrip(".")
    if out_ext not in SUPPORTED_OUT:
        raise HTTPException(400, f"Unsupported output format '{out_ext}'.")

    ratio_list = []
    for tok in ratios.split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            r = float(tok)
        except ValueError:
            raise HTTPException(400, "ratios must be comma-separated numbers")
        if 0.0 < r <= 1.0:
            ratio_list.append(r)
    if not ratio_list and target_faces <= 0:
        raise HTTPException(400, "give at least one ratio in (0,1] or a target face count")
    target_faces = max(0, min(int(target_faces), 5000000))

    job_id = uuid.uuid4().hex
    job_dir = os.path.join(WORK, job_id)
    os.makedirs(job_dir, exist_ok=True)
    in_path = os.path.join(job_dir, "input" + in_ext)
    with open(in_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    try:
        data = clean.run_lod(in_path, job_dir, ratio_list, out_ext=out_ext,
                             target_faces=target_faces,
                             preserve_uv=preserve_uv,
                             preserve_boundary=preserve_boundary)
    except Exception as e:
        raise HTTPException(500, str(e))

    levels = []
    for lv in data.get("levels", []):
        levels.append({
            "level": lv["level"],
            "ratio": lv["ratio"],
            "faces": lv["faces"],
            "vertices": lv["vertices"],
            "view_url": f"/api/download/{job_id}/{lv['view']}",
            "download_url": f"/api/download/{job_id}/{lv['file']}",
            "download_name": f"lod{lv['level']}{out_ext}",
        })

    return JSONResponse({
        "job_id": job_id,
        "source": data.get("source"),
        "levels": levels,
    })


def _safe_job_dir(job_id):
    if not job_id or "/" in job_id or "\\" in job_id or ".." in job_id:
        raise HTTPException(400, "bad job id")
    job_dir = os.path.join(WORK, job_id)
    if not os.path.isdir(job_dir):
        raise HTTPException(404, "job not found")
    return job_dir


def _find_input(job_dir, stem):
    for ext in SUPPORTED_IN:
        p = os.path.join(job_dir, stem + ext)
        if os.path.isfile(p):
            return p
    raise HTTPException(400, f"missing {stem} for this job")


@app.post("/api/wrap/prepare")
async def wrap_prepare(reference: UploadFile = File(...), source: UploadFile = File(...)):
    """Phase 1: store both models and export index-preserving OBJs the UI uses
    for display and landmark picking."""
    ref_ext = os.path.splitext(reference.filename or "")[1].lower()
    src_ext = os.path.splitext(source.filename or "")[1].lower()
    for ext in (ref_ext, src_ext):
        if ext not in SUPPORTED_IN:
            raise HTTPException(400, f"Unsupported input format '{ext}'.")

    job_id = uuid.uuid4().hex
    job_dir = os.path.join(WORK, job_id)
    os.makedirs(job_dir, exist_ok=True)
    ref_path = os.path.join(job_dir, "reference" + ref_ext)
    src_path = os.path.join(job_dir, "source" + src_ext)
    with open(ref_path, "wb") as f:
        shutil.copyfileobj(reference.file, f)
    with open(src_path, "wb") as f:
        shutil.copyfileobj(source.file, f)

    ref_obj = os.path.join(job_dir, "reference_view.obj")
    src_obj = os.path.join(job_dir, "source_view.obj")
    try:
        data = wrap.prepare(ref_path, src_path, ref_obj, src_obj)
    except Exception as e:
        raise HTTPException(500, str(e))

    return JSONResponse({
        "job_id": job_id,
        "reference_view_url": f"/api/download/{job_id}/reference_view.obj",
        "source_view_url": f"/api/download/{job_id}/source_view.obj",
        "reference_vertices": data.get("reference_vertices"),
        "source_vertices": data.get("source_vertices"),
        "source_shape_keys": data.get("source_shape_keys"),
        "auto_landmarks": data.get("auto_landmarks", {"ref": [], "src": []}),
    })


@app.post("/api/wrap")
async def wrap_endpoint(
    job_id: str = Form(...),
    strength: float = Form(1.0),
    smooth_iters: int = Form(3),
    shape_keys: str = Form("preserve"),
    align: str = Form("bbox"),
    sym_axis: str = Form("none"),
    out_format: str = Form("glb"),
    landmarks: str = Form(""),
    keep_internal: bool = Form(True),
):
    """Phase 2: run the wrap using the job's stored models + picked landmarks."""
    job_dir = _safe_job_dir(job_id)
    ref_path = _find_input(job_dir, "reference")
    src_path = _find_input(job_dir, "source")

    out_ext = "." + out_format.lower().lstrip(".")
    if out_ext not in SUPPORTED_OUT:
        raise HTTPException(400, f"Unsupported output format '{out_ext}'.")
    shape_keys = shape_keys.lower()
    if shape_keys not in ("preserve", "base"):
        raise HTTPException(400, "shape_keys must be preserve/base")
    align = align.lower()
    if align not in ("bbox", "none"):
        raise HTTPException(400, "align must be bbox/none")
    sym_axis = sym_axis.lower()
    if sym_axis not in ("none", "x", "y", "z"):
        raise HTTPException(400, "sym_axis must be none/x/y/z")
    strength = max(0.0, min(1.0, float(strength)))
    smooth_iters = max(0, min(50, int(smooth_iters)))

    lm_path = None
    if landmarks.strip():
        try:
            lm = json.loads(landmarks)
            ref_i = [int(x) for x in lm.get("ref", [])]
            src_i = [int(x) for x in lm.get("src", [])]
        except Exception:
            raise HTTPException(400, "landmarks must be JSON {ref:[],src:[]}")
        if min(len(ref_i), len(src_i)) >= 1:
            lm_path = os.path.join(job_dir, "landmarks.json")
            with open(lm_path, "w") as f:
                json.dump({"ref": ref_i, "src": src_i}, f)

    view_path = os.path.join(job_dir, "result.obj")
    out_name = "result" + out_ext
    out_path = os.path.join(job_dir, out_name)
    try:
        data = wrap.run_wrap(
            ref_path, src_path, out_path, view_path,
            strength=strength, smooth_iters=smooth_iters,
            shape_keys=shape_keys, align=align,
            landmarks=lm_path, sym_axis=sym_axis, keep_internal=keep_internal,
        )
    except Exception as e:
        raise HTTPException(500, str(e))

    return JSONResponse({
        "job_id": job_id,
        "stats": data,
        "view_url": f"/api/download/{job_id}/result.obj",
        "download_url": f"/api/download/{job_id}/{out_name}",
        "download_name": out_name,
    })


@app.post("/api/cloth/convert")
async def cloth_convert(file: UploadFile = File(...), out_format: str = Form("fbx")):
    """Transcode a garment draped in the browser (uploaded as GLB) into another
    format. Browsers write GLB/OBJ themselves; FBX/glTF/PLY/STL go through the
    retopology engine's import->export (convert-only, no remeshing)."""
    out_ext = "." + out_format.lower().lstrip(".")
    if out_ext not in SUPPORTED_OUT:
        raise HTTPException(400, f"Unsupported output format '{out_ext}'.")
    in_ext = os.path.splitext(file.filename or "")[1].lower() or ".glb"
    if in_ext not in SUPPORTED_IN:
        raise HTTPException(400, f"Unsupported input format '{in_ext}'.")

    job_id = uuid.uuid4().hex
    job_dir = os.path.join(WORK, job_id)
    os.makedirs(job_dir, exist_ok=True)
    in_path = os.path.join(job_dir, "cloth_in" + in_ext)
    with open(in_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    out_name = "garment" + out_ext
    out_path = os.path.join(job_dir, out_name)
    try:
        if out_ext == in_ext:
            shutil.copy(in_path, out_path)
        else:
            retopo.convert_format(in_path, out_path)
    except Exception as e:
        raise HTTPException(500, str(e))

    return JSONResponse({
        "job_id": job_id,
        "download_url": f"/api/download/{job_id}/{out_name}",
        "download_name": out_name,
    })


@app.post("/api/convert")
async def convert_endpoint(
    file: UploadFile = File(...),
    out_format: str = Form("fbx"),
    embed_textures: bool = Form(True),
    strip_rig: bool = Form(False),
    compress: str = Form("none"),
):
    """General any-to-any format converter (the Converter tool).

    Unlike /api/paint/export (which routes through the retopology script and
    joins every mesh into one object), this preserves the full scene — multiple
    objects, armatures/skinning, shape keys, animations and materials — so a
    rigged multi-part character converts intact. FBX/GLB embed their textures
    in one file; OBJ/glTF write sidecar files and are returned as a .zip."""
    out_ext = "." + out_format.lower().lstrip(".")
    if out_ext not in SUPPORTED_OUT:
        raise HTTPException(400, f"Unsupported output format '{out_ext}'. "
                                 f"Allowed: {sorted(SUPPORTED_OUT)}")
    in_ext = os.path.splitext(file.filename or "")[1].lower()
    if in_ext not in SUPPORTED_IN:
        raise HTTPException(400, f"Unsupported input format '{in_ext}'. "
                                 f"Allowed: {sorted(SUPPORTED_IN)}")

    # Keep the user's file name on the result (sanitized for URL/path safety).
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.splitext(os.path.basename(file.filename or ""))[0]).strip("._")
    stem = stem or "model"

    job_id = uuid.uuid4().hex
    job_dir = os.path.join(WORK, job_id)
    # Export into a dedicated subfolder so we can bundle *everything* Blender
    # writes for this format (mesh + material + texture images) into one zip.
    export_dir = os.path.join(job_dir, "export")
    os.makedirs(export_dir, exist_ok=True)
    in_path = os.path.join(job_dir, "convert_in" + in_ext)
    with open(in_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    compress = compress.lower()
    if compress not in ("none", "light", "strong"):
        raise HTTPException(400, "compress must be one of none/light/strong")
    # Light keeps typical game textures untouched (cap 2048); strong halves
    # them (cap 1024). Both enable Draco for glTF/GLB.
    max_texture = {"none": 0, "light": 2048, "strong": 1024}[compress]
    draco = compress != "none"

    out_path = os.path.join(export_dir, stem + out_ext)
    stats, notes = None, []
    # Same-format uploads only skip Blender when no option asks for processing.
    needs_engine = (out_ext != in_ext or strip_rig or draco
                    or (out_ext == ".fbx" and not embed_textures))
    try:
        if not needs_engine:
            shutil.copy(in_path, out_path)
            notes.append("input already in the requested format — passed through unchanged")
        else:
            data = convert.run_convert(in_path, out_path, embed_textures=embed_textures,
                                       strip_rig=strip_rig, draco=draco,
                                       max_texture=max_texture)
            stats = data.get("stats")
            notes = data.get("notes", [])
    except Exception as e:
        raise HTTPException(500, str(e))

    # If the format produced sidecar files (an .mtl + texture image for OBJ, or
    # a .bin + textures for glTF), zip the whole export folder so the texture is
    # delivered with the model. A lone file is returned as-is.
    produced = [n for n in os.listdir(export_dir) if os.path.isfile(os.path.join(export_dir, n))]
    if len(produced) > 1:
        out_name = stem + "_" + out_ext.lstrip(".") + ".zip"
        zip_path = os.path.join(job_dir, out_name)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for n in produced:
                z.write(os.path.join(export_dir, n), n)
    else:
        out_name = stem + out_ext
        shutil.copy(out_path, os.path.join(job_dir, out_name))

    return JSONResponse({
        "job_id": job_id,
        "download_url": f"/api/download/{job_id}/{out_name}",
        "download_name": out_name,
        "zipped": len(produced) > 1,
        "stats": stats,
        "notes": notes,
    })


@app.post("/api/paint/export")
async def paint_export(file: UploadFile = File(...), out_format: str = Form("fbx")):
    """Transcode a painted model (uploaded as a GLB with its texture embedded)
    into another format, keeping the texture bundled with the mesh.

    Blender re-encodes it into an isolated export folder. FBX embeds the texture
    in one file; OBJ/glTF write the texture as sidecar files, so those are
    returned as a .zip so nothing is lost. PLY/STL carry geometry only."""
    out_ext = "." + out_format.lower().lstrip(".")
    if out_ext not in SUPPORTED_OUT:
        raise HTTPException(400, f"Unsupported output format '{out_ext}'.")
    in_ext = os.path.splitext(file.filename or "")[1].lower() or ".glb"
    if in_ext not in SUPPORTED_IN:
        raise HTTPException(400, f"Unsupported input format '{in_ext}'.")

    job_id = uuid.uuid4().hex
    job_dir = os.path.join(WORK, job_id)
    # Export into a dedicated subfolder so we can bundle *everything* Blender
    # writes for this format (mesh + material + texture images) into one zip.
    export_dir = os.path.join(job_dir, "export")
    os.makedirs(export_dir, exist_ok=True)
    in_path = os.path.join(job_dir, "painted_in" + in_ext)
    with open(in_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    out_path = os.path.join(export_dir, "painted" + out_ext)
    try:
        if out_ext == in_ext:
            shutil.copy(in_path, out_path)
        else:
            retopo.convert_format(in_path, out_path)
    except Exception as e:
        raise HTTPException(500, str(e))

    # If the format produced sidecar files (an .mtl + texture image for OBJ, or a
    # .bin + textures for glTF), zip the whole export folder so the texture is
    # delivered with the model. A lone file is returned as-is.
    produced = [n for n in os.listdir(export_dir) if os.path.isfile(os.path.join(export_dir, n))]
    if len(produced) > 1:
        out_name = "painted_" + out_ext.lstrip(".") + ".zip"
        zip_path = os.path.join(job_dir, out_name)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for n in produced:
                z.write(os.path.join(export_dir, n), n)
        return JSONResponse({
            "job_id": job_id,
            "download_url": f"/api/download/{job_id}/{out_name}",
            "download_name": out_name,
        })

    out_name = "painted" + out_ext
    shutil.copy(out_path, os.path.join(job_dir, out_name))
    return JSONResponse({
        "job_id": job_id,
        "download_url": f"/api/download/{job_id}/{out_name}",
        "download_name": out_name,
    })


@app.get("/api/download/{job_id}/{name}")
def download(job_id: str, name: str):
    # guard against path traversal
    if "/" in name or "\\" in name or ".." in job_id or ".." in name:
        raise HTTPException(400, "bad path")
    path = os.path.join(WORK, job_id, name)
    if not os.path.isfile(path):
        raise HTTPException(404, "not found")
    return FileResponse(path, filename=name)


# --------------------------------------------------------------------------- #
# Character Rigger + Face Expressions (reference rigging pipeline in Blender)
# Mirrors the original Node server's /api/prep + /api/rig contract so the ported
# editor frontend works unchanged.
# --------------------------------------------------------------------------- #
RIG_WORK = os.path.join(WORK, "rig")
os.makedirs(RIG_WORK, exist_ok=True)
_rig_sessions = {}   # token -> {"model": path|None}
_rig_files = {}      # token -> absolute path


def _rig_register(path):
    tok = uuid.uuid4().hex + os.path.splitext(path)[1].lower()
    _rig_files[tok] = path
    return tok


@app.get("/api/rig/config")
def rig_config():
    return {"blender": retopo._find_blender()}


@app.post("/api/rig/prep")
async def rig_prep(request: Request, ext: str = "glb", test: str = "0", headOnly: str = "0"):
    is_test = test == "1"
    stamp = "%d_%s" % (int(time.time() * 1000), uuid.uuid4().hex[:6])
    model_path = None
    if not is_test:
        safe_ext = "".join(c for c in ext.lower() if c.isalnum()) or "glb"
        body = await request.body()
        if not body:
            raise HTTPException(400, "empty model upload")
        model_path = os.path.join(RIG_WORK, "in_%s.%s" % (stamp, safe_ext))
        with open(model_path, "wb") as f:
            f.write(body)

    markers_json = os.path.join(RIG_WORK, "markers_%s.json" % stamp)
    front_png = os.path.join(RIG_WORK, "front_%s.png" % stamp)
    fields = {"mode": "prep", "output": markers_json, "front_png": front_png}
    if model_path:
        fields["input"] = model_path
    if headOnly == "1":
        fields["head_only"] = True

    try:
        rig.run_job(fields)
        with open(markers_json, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        raise HTTPException(500, str(e))

    token = uuid.uuid4().hex
    _rig_sessions[token] = {"model": model_path, "head_only": headOnly == "1"}
    hand_urls = {}
    for s, p in (data.get("hand_views") or {}).items():
        hand_urls[s] = "/api/rig/files/" + _rig_register(p)
    return JSONResponse({
        "token": token,
        "markers": data.get("markers"),
        "faceMarkers": data.get("face_markers"),
        "calib": data.get("calib"),
        "frontUrl": "/api/rig/files/" + _rig_register(front_png),
        "handViews": hand_urls,
        "handCalib": data.get("hand_calib"),
        "fingerMarkers": data.get("finger_markers"),
    })


@app.post("/api/rig/build")
async def rig_build(request: Request):
    body = json.loads((await request.body()).decode("utf-8") or "{}")
    sess = _rig_sessions.get(body.get("token"))
    if sess is None:
        raise HTTPException(400, "unknown session — run prep first")

    out = os.path.join(RIG_WORK, "rigged_%d_%s.glb" % (int(time.time() * 1000), uuid.uuid4().hex[:6]))
    fields = {"output": out}
    if sess.get("model"):
        fields["input"] = sess["model"]

    if body.get("headOnly"):
        fields["face_only"] = True
        if body.get("faceMarkers") and body.get("calib"):
            fields["face_markers"] = body["faceMarkers"]
            fields["calib"] = body["calib"]
    else:
        fields["fingers"] = body.get("fingers") is not False
        if body.get("boneNaming"):
            fields["bone_naming"] = body["boneNaming"]
        if body.get("markers") and body.get("calib"):
            fields["markers"] = body["markers"]
            fields["calib"] = body["calib"]
        if body.get("fingerMarkers") and body.get("handCalib"):
            fields["finger_markers"] = body["fingerMarkers"]
            fields["hand_calib"] = body["handCalib"]
        if body.get("faceShapekeys"):
            fields["face_shapekeys"] = True
            if body.get("faceMarkers") and body.get("calib"):
                fields["face_markers"] = body["faceMarkers"]
                fields["calib"] = body["calib"]

    try:
        rig.run_job(fields)
    except Exception as e:
        raise HTTPException(500, str(e))

    glb_tok = _rig_register(out)
    resp = {"glbUrl": "/api/rig/files/" + glb_tok,
            "glbDownload": "/api/rig/download/" + glb_tok}
    fbx = out[:-4] + ".fbx" if out.lower().endswith(".glb") else None
    if fbx and os.path.isfile(fbx):
        resp["fbxDownload"] = "/api/rig/download/" + _rig_register(fbx)
    return JSONResponse(resp)


@app.get("/api/rig/files/{tok}")
def rig_file(tok: str):
    path = _rig_files.get(tok)
    if not path or not os.path.isfile(path):
        raise HTTPException(404, "not found")
    return FileResponse(path)


@app.get("/api/rig/download/{tok}")
def rig_download(tok: str):
    path = _rig_files.get(tok)
    if not path or not os.path.isfile(path):
        raise HTTPException(404, "not found")
    return FileResponse(path, filename="rigged" + os.path.splitext(path)[1].lower())


# --------------------------------------------------------------------------- #
# Projects: persistent per-user workspaces that carry a model between tools.
# Each project is a folder under work/projects/<id> with a manifest.json and the
# copied-in asset files. Every tool can append its result as the new "current"
# model, and pull the current model as its input — so retopo -> wrap -> rig ->
# face -> cloth flows as one workspace instead of manual export/re-import.
# --------------------------------------------------------------------------- #
PROJECTS = os.path.join(WORK, "projects")
os.makedirs(PROJECTS, exist_ok=True)


def _safe_id(v):
    """Project/asset ids are our own hex tokens; reject anything else so the id
    can't escape the projects directory."""
    if not v or not v.isalnum():
        raise HTTPException(400, "bad id")
    return v


def _proj_dir(pid):
    return os.path.join(PROJECTS, _safe_id(pid))


def _load_manifest(pid):
    p = os.path.join(_proj_dir(pid), "manifest.json")
    if not os.path.isfile(p):
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _save_manifest(m):
    m["updated"] = int(time.time())
    with open(os.path.join(_proj_dir(m["id"]), "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(m, f, indent=2)
    return m


def _asset(m, aid):
    return next((a for a in m["assets"] if a["id"] == aid), None)


@app.post("/api/project")
async def project_create(request: Request):
    body = json.loads((await request.body()).decode("utf-8") or "{}")
    pid = uuid.uuid4().hex[:12]
    os.makedirs(_proj_dir(pid), exist_ok=True)
    now = int(time.time())
    m = {"id": pid, "name": (body.get("name") or "Untitled project").strip()[:80] or "Untitled project",
         "created": now, "updated": now, "current": None, "assets": []}
    return JSONResponse(_save_manifest(m))


# These project responses change over time under a stable URL (a project's
# "current" model is re-uploaded, results appended, etc.). Without an explicit
# no-store the browser can heuristically cache them and serve a *stale* manifest
# or, worse, the previous model's bytes from GET /current after a new upload.
NO_STORE = {"Cache-Control": "no-store"}


@app.get("/api/project")
def project_list():
    out = []
    for pid in (os.listdir(PROJECTS) if os.path.isdir(PROJECTS) else []):
        try:
            m = _load_manifest(pid)
        except HTTPException:
            continue
        if not m:
            continue
        cur = _asset(m, m.get("current"))
        out.append({"id": m["id"], "name": m["name"], "updated": m["updated"],
                    "created": m["created"], "assetCount": len(m["assets"]),
                    "current": cur["name"] if cur else None,
                    "currentTool": cur["tool"] if cur else None})
    out.sort(key=lambda x: x["updated"], reverse=True)
    return JSONResponse(out, headers=NO_STORE)


@app.get("/api/project/{pid}")
def project_get(pid: str):
    m = _load_manifest(pid)
    if not m:
        raise HTTPException(404, "no such project")
    return JSONResponse(m, headers=NO_STORE)


@app.patch("/api/project/{pid}")
async def project_update(pid: str, request: Request):
    m = _load_manifest(pid)
    if not m:
        raise HTTPException(404, "no such project")
    body = json.loads((await request.body()).decode("utf-8") or "{}")
    if body.get("name"):
        m["name"] = body["name"].strip()[:80] or m["name"]
    if "current" in body and (body["current"] is None or _asset(m, body["current"])):
        m["current"] = body["current"]
    return JSONResponse(_save_manifest(m))


@app.delete("/api/project/{pid}")
def project_delete(pid: str):
    d = _proj_dir(pid)
    if os.path.isdir(d):
        shutil.rmtree(d, ignore_errors=True)
    return {"ok": True}


@app.post("/api/project/{pid}/assets")
async def project_add_asset(pid: str, file: UploadFile = File(...),
                            tool: str = Form("Tool"), name: str = Form(None)):
    m = _load_manifest(pid)
    if not m:
        raise HTTPException(404, "no such project")
    orig = os.path.basename(name or file.filename or "model.glb")
    ext = os.path.splitext(orig)[1].lower() or ".glb"
    if ext not in SUPPORTED_IN:
        raise HTTPException(400, f"unsupported asset format '{ext}'")
    aid = uuid.uuid4().hex[:10]
    fname = aid + ext
    with open(os.path.join(_proj_dir(pid), fname), "wb") as f:
        shutil.copyfileobj(file.file, f)
    asset = {"id": aid, "tool": tool[:40], "name": orig[:80],
             "format": ext.lstrip("."), "file": fname, "created": int(time.time())}
    m["assets"].append(asset)
    m["current"] = aid
    _save_manifest(m)
    return JSONResponse({"project": m, "asset": asset})


@app.get("/api/project/{pid}/current")
def project_current(pid: str):
    m = _load_manifest(pid)
    if not m or not m.get("current"):
        raise HTTPException(404, "no current model")
    a = _asset(m, m["current"])
    path = os.path.join(_proj_dir(pid), a["file"]) if a else None
    if not path or not os.path.isfile(path):
        raise HTTPException(404, "missing file")
    return FileResponse(path, filename=a["name"], headers=NO_STORE)


@app.get("/api/project/{pid}/assets/{aid}")
def project_asset(pid: str, aid: str):
    m = _load_manifest(pid)
    a = _asset(m, _safe_id(aid)) if m else None
    path = os.path.join(_proj_dir(pid), a["file"]) if a else None
    if not path or not os.path.isfile(path):
        raise HTTPException(404, "not found")
    return FileResponse(path, filename=a["name"], headers=NO_STORE)


# Serve the frontend (mounted last so /api/* wins).
app.mount("/", StaticFiles(directory=FRONTEND, html=True), name="frontend")
