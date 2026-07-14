"""
Engine layer for the Character Rigger and Face Expressions tools.

Drives the reference rigging pipeline (backend/rigger/pipeline.py) inside
headless Blender, exactly as the original Node `blenderRunner.js` did: write a
job JSON, spawn Blender, watch the `[RIG] stage: msg` log lines, and treat the
job as done when its `output` file exists (or failed on a `[RIG] ERROR`).
"""
import os
import json
import uuid
import subprocess

import retopo  # reuse Blender detection
import paths

PIPELINE = os.path.join(paths.resource_dir(), "rigger", "pipeline.py")
DEFAULT_TARGET_HEIGHT = 1.8


def run_job(fields, on_log=None, timeout=1200):
    """Run the Blender pipeline with arbitrary job fields (must include output).
    Returns the parsed log lines. Raises on failure."""
    bl = retopo._find_blender()
    if not bl:
        raise RuntimeError(
            "The rigger needs Blender. Install it (https://www.blender.org/download/) "
            "and ensure it is on PATH or set BLENDER_PATH."
        )
    job = {"target_height": DEFAULT_TARGET_HEIGHT}
    job.update(fields)
    output = job.get("output")

    job_file = os.path.join(paths.work_dir(), "rigjob_%s.json" % uuid.uuid4().hex)
    os.makedirs(os.path.dirname(job_file), exist_ok=True)
    with open(job_file, "w") as f:
        json.dump(job, f)

    cmd = [bl, "--background", "--python", PIPELINE, "--", job_file]
    failed = None
    logs = []
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
    finally:
        try:
            os.remove(job_file)
        except OSError:
            pass

    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("[RIG]"):
            rest = line[len("[RIG]"):].strip()
            stage, _, msg = rest.partition(":")
            stage, msg = stage.strip(), msg.strip()
            logs.append({"stage": stage, "msg": msg})
            if stage == "ERROR":
                failed = msg
            if on_log:
                on_log(stage, msg)

    if failed or proc.returncode != 0 or not (output and os.path.isfile(output)):
        tail = out[-2000:]
        raise RuntimeError("Rigging job failed (exit %s). %s\n%s"
                           % (proc.returncode, failed or "", tail))
    return logs
