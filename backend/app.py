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
  GET  /api/download/{id}/{name} -> fetch a produced result file
"""
import os
import json
import uuid
import shutil
import time

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import retopo
import wrap
import rig

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(HERE, "work")
FRONTEND = os.path.normpath(os.path.join(HERE, "..", "frontend"))
os.makedirs(WORK, exist_ok=True)

SUPPORTED_IN = {".obj", ".glb", ".gltf", ".fbx", ".ply", ".stl"}
SUPPORTED_OUT = {".obj", ".glb", ".gltf", ".fbx", ".ply", ".stl"}

app = FastAPI(title="Character Editor")


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


# Serve the frontend (mounted last so /api/* wins).
app.mount("/", StaticFiles(directory=FRONTEND, html=True), name="frontend")
