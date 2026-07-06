"""
Pytest + Playwright harness for the frontend.

Boots the real FastAPI app (uvicorn) on a fresh free port per test (isolating
each test from the last — a shared server/browser accumulates socket + software
WebGL state and eventually stalls), and provides:
  - app_url    : the running server's URL
  - errors      : a list that fills with console.error / pageerror / same-origin
                  request failures for the current page (assert it stays empty)

Run from the backend virtualenv (it must have fastapi + uvicorn):
  pip install -r tests/requirements-test.txt
  playwright install chromium
  pytest tests -q
"""
import os
import sys
import time
import socket
import subprocess
import urllib.request

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(REPO, "backend")
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

# Substrings in a failing URL that are benign and must never fail a test.
ALLOWED = ("favicon.ico",)


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def app_url():
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--port", str(port)],
        cwd=BACKEND, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    url = "http://127.0.0.1:%d" % port
    try:
        for _ in range(120):
            try:
                urllib.request.urlopen(url + "/", timeout=1)
                break
            except Exception:
                if proc.poll() is not None:
                    out = proc.stdout.read().decode(errors="ignore") if proc.stdout else ""
                    raise RuntimeError("uvicorn exited early:\n" + out)
                time.sleep(0.5)
        else:
            raise RuntimeError("server did not become ready")
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


@pytest.fixture
def browser(browser_type, browser_type_launch_args):
    """Fresh browser per test. Software WebGL (SwiftShader) accumulates GPU state
    across many WebGL pages in one browser process and eventually stalls new
    navigations; a per-test browser keeps every test on a clean process."""
    b = browser_type.launch(**browser_type_launch_args)
    yield b
    b.close()


@pytest.fixture(scope="session")
def browser_type_launch_args(browser_type_launch_args):
    # Force a software WebGL2 backend so headless Chromium actually renders
    # (and so shader-compile errors reproduce) on machines without a GPU.
    args = list(browser_type_launch_args.get("args", []))
    args += [
        "--use-gl=angle", "--use-angle=swiftshader",
        "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist",
    ]
    return {**browser_type_launch_args, "args": args}


@pytest.fixture
def errors(page):
    """Attach listeners (before the test navigates) and collect real problems."""
    found = []

    def on_console(msg):
        if msg.type == "error" and not any(a in msg.text for a in ALLOWED):
            found.append("console.error: " + msg.text)

    def on_pageerror(exc):
        found.append("pageerror: " + str(exc))

    def on_requestfailed(req):
        # Only same-origin failures matter; ignore CDN hiccups + allowlisted URLs.
        if req.url.startswith("http") and not any(a in req.url for a in ALLOWED):
            if "127.0.0.1" in req.url or "localhost" in req.url:
                found.append("requestfailed: " + req.url)

    page.on("console", on_console)
    page.on("pageerror", on_pageerror)
    page.on("requestfailed", on_requestfailed)
    return found


def fixture_path(name):
    return os.path.join(FIXTURES, name)


def canvas_has_content(page, selector="canvas"):
    """True if the canvas rendered more than a flat background (i.e. the model
    is actually visible). Guards against silent 'blank canvas' regressions like
    the SkinnedMesh frustum-culling vanish."""
    from PIL import Image
    import io
    png = page.locator(selector).first.screenshot()
    im = Image.open(io.BytesIO(png)).convert("RGB").resize((64, 64))
    colors = im.getcolors(64 * 64)
    # getcolors returns None when there are many distinct colors (definitely
    # content); a flat background collapses to 1-2 entries.
    return colors is None or len(colors) > 4
