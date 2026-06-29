"""
Render orthographic front + side views of a model for MediaPipe joint detection.

    blender --background --python backend/render_views.py -- <input> <out_dir>

Writes <out_dir>/front.png and <out_dir>/side.png plus <out_dir>/calib.json,
which records the orthographic mapping so detected image pixels can be converted
back to 3D model coordinates:

    front view:  image gives world X (horizontal) and Z (vertical)
    side view:   image gives world Y (depth)      and Z (vertical)

The figure is normalized (Z up, feet at Z=0, height H) and centred, framed in a
square ortho camera of known scale, so the pixel->world maths is exact.
"""

import bpy
import sys
import os
import json
import math

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pipeline

HEIGHT = 1.8
RES = 768
ORTHO = 1.95         # metres covered across the square frame (figure fills more)
CENTER_Z = HEIGHT / 2.0


def parse_args():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    inp = argv[0] if len(argv) >= 1 and argv[0] else None
    out_dir = argv[1] if len(argv) >= 2 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "assets", "test", "views")
    return inp, os.path.abspath(out_dir)


def build_mesh(inp):
    pipeline.reset_scene()
    meshes = pipeline.import_model(inp) if inp and os.path.exists(inp) else pipeline.make_test_human()
    obj = pipeline.join_meshes(meshes)
    return pipeline.normalize(obj, HEIGHT)


def add_light():
    d = bpy.data.lights.new("Sun", type="SUN")
    d.energy = 3.0
    o = bpy.data.objects.new("Sun", d)
    bpy.context.scene.collection.objects.link(o)
    o.rotation_euler = (math.radians(55), 0, math.radians(25))


def render_view(view, out_png):
    scene = bpy.context.scene
    cam_data = bpy.data.cameras.new("Cam")
    cam_data.type = "ORTHO"
    cam_data.ortho_scale = ORTHO
    cam = bpy.data.objects.new("Cam", cam_data)
    scene.collection.objects.link(cam)
    if view == "front":            # look along +Y (figure faces -Y)
        cam.location = (0.0, -5.0, CENTER_Z)
        cam.rotation_euler = (math.radians(90), 0, 0)
    else:                           # side: look along +X
        cam.location = (-5.0, 0.0, CENTER_Z)
        cam.rotation_euler = (math.radians(90), 0, math.radians(-90))
    scene.camera = cam

    scene.render.engine = "BLENDER_WORKBENCH"
    sh = scene.display.shading
    sh.light = "STUDIO"
    sh.show_cavity = False
    # Photo-ish appearance helps MediaPipe recognise a person: skin tone on a
    # dark, contrasting background.
    sh.color_type = "SINGLE"
    sh.single_color = (0.85, 0.62, 0.5)
    sh.background_type = "VIEWPORT"
    sh.background_color = (0.12, 0.14, 0.20)
    scene.render.resolution_x = RES
    scene.render.resolution_y = RES
    scene.render.film_transparent = False
    scene.render.filepath = out_png
    bpy.ops.render.render(write_still=True)
    bpy.data.objects.remove(cam, do_unlink=True)


def main():
    inp, out_dir = parse_args()
    os.makedirs(out_dir, exist_ok=True)
    build_mesh(inp)
    add_light()
    render_view("front", os.path.join(out_dir, "front.png"))
    render_view("side", os.path.join(out_dir, "side.png"))

    # Pixel -> world mapping. Image origin is top-left; +y_px goes down.
    # world_x = (px/RES - 0.5) * ORTHO ; world_z = CENTER_Z - (py/RES - 0.5)*ORTHO
    calib = {
        "res": RES, "ortho": ORTHO, "center_z": CENTER_Z, "height": HEIGHT,
        "front": {"h_axis": "x", "h_sign": 1, "v_axis": "z"},
        "side":  {"h_axis": "y", "h_sign": 1, "v_axis": "z"},
    }
    with open(os.path.join(out_dir, "calib.json"), "w") as f:
        json.dump(calib, f, indent=2)
    pipeline.log("render", f"views written to {out_dir}")


if __name__ == "__main__":
    main()
