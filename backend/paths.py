"""
Resolves where the app reads its resources and writes its data, for both ways
of running it:

  * source checkout — resources live in the repo (backend/, frontend/) and all
    work/project data is written to backend/work, exactly as before.
  * frozen desktop build (PyInstaller / installed app) — resources are read
    from the bundle, which is read-only when installed (e.g. under Program
    Files / WindowsApps), so all data is written to a per-user folder:
    %LOCALAPPDATA%\\CharacterEditor.
"""
import os
import sys

FROZEN = bool(getattr(sys, "frozen", False))


def resource_dir():
    """Folder holding backend resources (Blender scripts, rigger/ pipeline)."""
    if FROZEN:
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


def frontend_dir():
    """Folder holding the static frontend the server serves."""
    if FROZEN:
        return os.path.join(resource_dir(), "frontend")
    return os.path.normpath(os.path.join(resource_dir(), "..", "frontend"))


def data_dir():
    """User-writable root for jobs, projects and logs."""
    if FROZEN:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "CharacterEditor")
    return os.path.dirname(os.path.abspath(__file__))


def work_dir():
    return os.path.join(data_dir(), "work")
