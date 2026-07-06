"""
Generates the small GLB fixtures the Playwright tests load. Run once with Blender:

  blender --background --factory-startup --python tests/fixtures/make_fixtures.py

Produces (next to this script):
  cube.glb     — a plain static UV-unwrapped cube (baseline load test)
  skinned.glb  — a subdivided cylinder bound to a 2-bone armature; exercises the
                 SkinnedMesh path (frustum culling + skinned export baking).
"""
import os
import bpy

HERE = os.path.dirname(os.path.abspath(__file__))


def reset():
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete()
    for block in (bpy.data.meshes, bpy.data.objects, bpy.data.armatures, bpy.data.materials):
        for b in list(block):
            try:
                block.remove(b)
            except Exception:
                pass


def select_only(objs):
    bpy.ops.object.select_all(action="DESELECT")
    for o in objs:
        o.select_set(True)
    bpy.context.view_layer.objects.active = objs[0]


def make_cube():
    reset()
    bpy.ops.mesh.primitive_cube_add(size=1.0)
    cube = bpy.context.active_object
    cube.name = "CubeFixture"
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.uv.smart_project()
    bpy.ops.object.mode_set(mode="OBJECT")
    mat = bpy.data.materials.new("CubeMat")
    mat.use_nodes = True
    cube.data.materials.append(mat)
    select_only([cube])
    bpy.ops.export_scene.gltf(
        filepath=os.path.join(HERE, "cube.glb"),
        export_format="GLB", use_selection=True,
    )


def make_skinned():
    reset()
    bpy.ops.mesh.primitive_cylinder_add(vertices=12, radius=0.3, depth=2.0)
    body = bpy.context.active_object
    body.name = "SkinnedFixture"
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.subdivide(number_cuts=6)
    bpy.ops.object.mode_set(mode="OBJECT")
    mat = bpy.data.materials.new("SkinMat")
    mat.use_nodes = True
    body.data.materials.append(mat)

    bpy.ops.object.armature_add(enter_editmode=True, location=(0, 0, 0))
    arm = bpy.context.active_object
    arm.name = "Rig"
    eb = arm.data.edit_bones
    b0 = eb[0]
    b0.name = "Bone1"
    b0.head = (0, 0, -1.0)
    b0.tail = (0, 0, 0.0)
    b1 = eb.new("Bone2")
    b1.head = (0, 0, 0.0)
    b1.tail = (0, 0, 1.0)
    b1.parent = b0
    bpy.ops.object.mode_set(mode="OBJECT")

    select_only([body, arm])
    bpy.context.view_layer.objects.active = arm
    bpy.ops.object.parent_set(type="ARMATURE_AUTO")

    select_only([body, arm])
    bpy.ops.export_scene.gltf(
        filepath=os.path.join(HERE, "skinned.glb"),
        export_format="GLB", use_selection=True,
    )


make_cube()
make_skinned()
print("FIXTURES_DONE")
