"""
Desktop launcher for Character Editor.

Runs the FastAPI server on a free localhost port and opens the UI in its own
app window (Edge/Chrome "--app" mode with a dedicated profile, so the window
has no browser chrome and closing it closes the app). Falls back to the
default browser when neither is available.

This is the entry point for the packaged .exe, and also works from a source
checkout:  python desktop.py

Environment overrides:
  CHARACTER_EDITOR_PORT       fixed port instead of an automatically free one
  CHARACTER_EDITOR_NO_WINDOW  "1" = start the server only (no UI window);
                              used for testing and for running it as a plain
                              local server
"""
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
import webbrowser

import paths

WINDOW_SIZE = "1500,950"


def _log_file():
    logs = os.path.join(paths.data_dir(), "logs")
    os.makedirs(logs, exist_ok=True)
    return os.path.join(logs, "server.log")


def _ensure_streams():
    """A windowed (no-console) exe has no stdout/stderr; logging would crash.
    Point them at a log file so server output stays inspectable."""
    if sys.stdout is None or sys.stderr is None:
        f = open(_log_file(), "a", buffering=1, encoding="utf-8", errors="replace")
        if sys.stdout is None:
            sys.stdout = f
        if sys.stderr is None:
            sys.stderr = f


def _free_port():
    env = os.environ.get("CHARACTER_EDITOR_PORT")
    if env:
        return int(env)
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_ready(url, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url + "api/engine", timeout=2)
            return True
        except Exception:
            time.sleep(0.25)
    return False


def _find_app_browser():
    """An installed Edge or Chrome that supports chromeless --app windows."""
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    local = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        os.path.join(pf86, "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(pf, "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(pf, "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(pf86, "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(local, "Google", "Chrome", "Application", "chrome.exe"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def _sweep_old_profiles(root):
    """Delete window profiles from previous launches. Only clearly stale ones
    (a day old) — a fresher one may belong to another running instance."""
    try:
        for name in os.listdir(root):
            p = os.path.join(root, name)
            if time.time() - os.path.getmtime(p) > 86400:
                shutil.rmtree(p, ignore_errors=True)
    except OSError:
        pass


def _open_window(url):
    """Open the UI. Returns (proc, profile_dir); proc is a Popen tied to the
    window's lifetime, or None if only a normal browser tab could be opened."""
    browser = _find_app_browser()
    if browser:
        # A dedicated user-data-dir forces a separate browser process, so the
        # process exits when the window is closed — that's our app lifetime.
        # It must be a FRESH profile every launch: if leftover browser
        # processes still own a reused profile, the new launch hands the URL
        # to them and exits immediately, which would look like "window closed"
        # and shut the server down underneath a live window.
        root = os.path.join(paths.data_dir(), "window-profiles")
        os.makedirs(root, exist_ok=True)
        _sweep_old_profiles(root)
        profile = os.path.join(root, uuid.uuid4().hex)
        os.makedirs(profile, exist_ok=True)
        try:
            proc = subprocess.Popen([
                browser, "--app=" + url,
                "--user-data-dir=" + profile,
                "--no-first-run", "--no-default-browser-check",
                "--window-size=" + WINDOW_SIZE,
            ])
            return proc, profile
        except OSError:
            shutil.rmtree(profile, ignore_errors=True)
    webbrowser.open(url)
    return None, None


def main():
    _ensure_streams()
    port = _free_port()
    url = "http://127.0.0.1:%d/" % port

    import uvicorn
    import app as app_module
    config = uvicorn.Config(app_module.app, host="127.0.0.1", port=port,
                            log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    if not _wait_ready(url):
        print("Server failed to start — see", _log_file())
        server.should_exit = True
        sys.exit(1)
    print("Character Editor running at", url)

    if os.environ.get("CHARACTER_EDITOR_NO_WINDOW") == "1":
        # Server-only mode: run until interrupted.
        try:
            while thread.is_alive():
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    else:
        proc, profile = _open_window(url)
        try:
            if proc is not None:
                started = time.time()
                proc.wait()          # window closed -> quit
                if time.time() - started < 5:
                    # Exiting within seconds means the browser handed the URL
                    # to an already-running process — the window is still
                    # open, so keep serving instead of pulling the rug out.
                    print("browser handed the window off; server stays up")
                    while thread.is_alive():
                        time.sleep(1)
            else:
                while thread.is_alive():
                    time.sleep(1)    # plain browser tab -> run until Ctrl+C
        except KeyboardInterrupt:
            pass
        if profile:
            shutil.rmtree(profile, ignore_errors=True)

    server.should_exit = True
    thread.join(timeout=10)


if __name__ == "__main__":
    main()
