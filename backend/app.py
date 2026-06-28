"""
FastAPI server for the Mesh Hub (retopology + shape-match wrap tools).

Endpoints:
  GET  /                 -> serves the hub frontend
  GET  /api/engine       -> which retopology engine is available
  POST /api/retopo       -> upload a model, get back quad-remeshed model + stats
  POST /api/wrap         -> upload reference + source, get a conformed model
  GET  /api/download/{id}/{name} -> fetch a produced result file
"""
import os
import uuid
import shutil

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import retopo
import wrap

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(HERE, "work")
FRONTEND = os.path.normpath(os.path.join(HERE, "..", "frontend"))
os.makedirs(WORK, exist_ok=True)

SUPPORTED_IN = {".obj", ".glb", ".gltf", ".fbx", ".ply", ".stl"}
SUPPORTED_OUT = {".obj", ".glb", ".gltf", ".fbx", ".ply", ".stl"}

app = FastAPI(title="3D Auto Quad Topology")


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


@app.post("/api/wrap")
async def wrap_endpoint(
    reference: UploadFile = File(...),
    source: UploadFile = File(...),
    strength: float = Form(1.0),
    smooth_iters: int = Form(3),
    shape_keys: str = Form("preserve"),
    align: str = Form("bbox"),
    out_format: str = Form("glb"),
):
    ref_ext = os.path.splitext(reference.filename or "")[1].lower()
    src_ext = os.path.splitext(source.filename or "")[1].lower()
    for ext in (ref_ext, src_ext):
        if ext not in SUPPORTED_IN:
            raise HTTPException(400, f"Unsupported input format '{ext}'.")
    out_ext = "." + out_format.lower().lstrip(".")
    if out_ext not in SUPPORTED_OUT:
        raise HTTPException(400, f"Unsupported output format '{out_ext}'.")
    shape_keys = shape_keys.lower()
    if shape_keys not in ("preserve", "base"):
        raise HTTPException(400, "shape_keys must be preserve/base")
    if align.lower() not in ("bbox", "none"):
        raise HTTPException(400, "align must be bbox/none")
    strength = max(0.0, min(1.0, float(strength)))
    smooth_iters = max(0, min(50, int(smooth_iters)))

    job_id = uuid.uuid4().hex
    job_dir = os.path.join(WORK, job_id)
    os.makedirs(job_dir, exist_ok=True)

    ref_path = os.path.join(job_dir, "reference" + ref_ext)
    src_path = os.path.join(job_dir, "source" + src_ext)
    view_path = os.path.join(job_dir, "result.obj")
    out_name = "result" + out_ext
    out_path = os.path.join(job_dir, out_name)

    with open(ref_path, "wb") as f:
        shutil.copyfileobj(reference.file, f)
    with open(src_path, "wb") as f:
        shutil.copyfileobj(source.file, f)

    try:
        data = wrap.run_wrap(
            ref_path, src_path, out_path, view_path,
            strength=strength, smooth_iters=smooth_iters,
            shape_keys=shape_keys, align=align.lower(),
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


@app.get("/api/download/{job_id}/{name}")
def download(job_id: str, name: str):
    # guard against path traversal
    if "/" in name or "\\" in name or ".." in job_id or ".." in name:
        raise HTTPException(400, "bad path")
    path = os.path.join(WORK, job_id, name)
    if not os.path.isfile(path):
        raise HTTPException(404, "not found")
    return FileResponse(path, filename=name)


# Serve the frontend (mounted last so /api/* wins).
app.mount("/", StaticFiles(directory=FRONTEND, html=True), name="frontend")
