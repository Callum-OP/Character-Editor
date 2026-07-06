"""
Style Studio: exercises the paths that only fail at runtime in a real browser —
per-style shader compilation, the outline, SkinnedMesh frustum culling, and the
stylized-GLB export (skinning bake + winding flip).
"""
from conftest import fixture_path, canvas_has_content

STYLES = ["toon", "matcap", "flat", "clay", "wireframe", "xray", "normals", "original"]


def _load(page, app_url, model):
    # "commit": don't block on deferred module scripts (the CDN three.js import).
    page.goto(app_url + "/style-studio.html", wait_until="commit", timeout=60000)
    # Wait until the module has wired its listeners before touching the input —
    # otherwise the change event fires before loadFiles is even attached.
    page.wait_for_function("window.__styleReady === true", timeout=30000)
    page.set_input_files("#file-input", fixture_path(model))
    # installModel hides the hint — a deterministic "model is loaded" signal.
    page.wait_for_function(
        "document.getElementById('hint').classList.contains('hidden')", timeout=30000
    )


def test_cube_every_style_compiles(page, app_url, errors):
    _load(page, app_url, "cube.glb")
    assert canvas_has_content(page), "cube did not render"
    for s in STYLES:
        page.click('.style[data-style="%s"]' % s)
        page.wait_for_timeout(200)
    # back to a style that exposes the outline controls, then round-trip them
    page.click('.style[data-style="toon"]'); page.wait_for_timeout(150)
    page.uncheck("#outline-on"); page.wait_for_timeout(150)
    page.check("#outline-on"); page.wait_for_timeout(150)
    assert errors == [], "shader/style errors:\n  " + "\n  ".join(errors)
    assert canvas_has_content(page), "canvas went blank after cycling styles"


def test_skinned_survives_orbit(page, app_url, errors):
    # The SkinnedMesh frustum-culling bug made the model vanish on the first
    # camera move; this asserts it stays visible after an orbit drag.
    _load(page, app_url, "skinned.glb")
    # Hide the grid so the canvas content reflects the *model* only, not the
    # always-present grid (the toggle lives in a collapsed <details>, so drive
    # it via JS rather than a visible click).
    page.evaluate(
        "() => { const c = document.getElementById('show-grid');"
        " c.checked = false; c.dispatchEvent(new Event('change', {bubbles:true})); }"
    )
    page.wait_for_timeout(300)
    assert canvas_has_content(page), "skinned model did not render"

    box = page.locator("#gl").bounding_box()
    cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.mouse.move(cx, cy)
    page.mouse.down()
    page.mouse.move(cx + 130, cy + 50, steps=12)
    page.mouse.up()
    page.wait_for_timeout(500)

    assert canvas_has_content(page), "model vanished after orbit (frustum culling regression)"
    assert errors == [], "errors during skinned interaction:\n  " + "\n  ".join(errors)


def test_stylized_glb_export(page, app_url, errors):
    # Exercises buildGLB(): skinning bake + winding-flipped outline + GLTFExporter.
    _load(page, app_url, "skinned.glb")
    with page.expect_download() as dl:
        page.click("#glb-btn")
    path = dl.value.path()
    import os
    assert path and os.path.getsize(path) > 0, "exported GLB is empty"
    assert errors == [], "errors during export:\n  " + "\n  ".join(errors)
