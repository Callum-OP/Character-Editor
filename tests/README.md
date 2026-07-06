# Frontend tests (Playwright + pytest)

Headless-browser tests for the tool pages. They catch the class of bugs that
only surface at runtime in a real browser — shader-compile failures, blank
canvases (e.g. SkinnedMesh frustum culling), broken model loads, stray 404s —
none of which a syntax check or "does the page return 200" would find.

## What's covered

- **`test_smoke.py`** — every tool page loads and runs its startup JS with **no
  console errors / page errors / same-origin request failures**.
- **`test_style_studio.py`** — loads real GLB fixtures and exercises Style
  Studio end to end: every style compiles, the outline toggles, a **skinned**
  model stays visible after an orbit (the frustum-culling regression), and the
  stylized-GLB export runs (skinning bake + winding-flipped outline).

Bugs this suite already caught: the `rig.js` `/null` image request, the
Style Studio file-input clearing its own `FileList`, the SkinnedMesh vanish, and
the `undefined` texture-channel shader crash.

## Running

From the backend's Python environment (it needs `fastapi` + `uvicorn`):

```bash
pip install -r tests/requirements-test.txt
python -m playwright install chromium
python -m pytest tests -q
```

The suite starts the real FastAPI app itself (fresh uvicorn per test on a free
port) — you don't need a server running. Blender is **not** required; the tests
use the committed GLB fixtures.

## Fixtures

`tests/fixtures/cube.glb` and `skinned.glb` are small committed models. To
regenerate them (needs Blender):

```bash
blender --background --factory-startup --python tests/fixtures/make_fixtures.py
```

## Notes

- Headless Chromium renders WebGL via SwiftShader (configured in `conftest.py`),
  so shader-compile errors reproduce in CI.
- Each test gets its own server **and** browser; sharing them across many
  software-WebGL pages accumulates state and eventually stalls navigations.
- Pages are navigated with `wait_until="commit"` (not `load`) because some pull
  in `<model-viewer>` / deferred module scripts that keep `load` pending.
