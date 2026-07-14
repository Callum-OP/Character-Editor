# PyInstaller spec for the Character Editor desktop build.
# Build with:  packaging/build.ps1   (or: pyinstaller --noconfirm CharacterEditor.spec)
import os

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
BACKEND = os.path.join(ROOT, "backend")

# Resources the server needs at runtime. The Blender helper scripts must ship
# as real .py files (Blender runs them with --python), at the bundle root so
# the engine layers find them via paths.resource_dir().
datas = [
    (os.path.join(ROOT, "frontend"), "frontend"),
    (os.path.join(BACKEND, "blender_remesh.py"), "."),
    (os.path.join(BACKEND, "blender_clean.py"), "."),
    (os.path.join(BACKEND, "blender_convert.py"), "."),
    (os.path.join(BACKEND, "blender_wrap.py"), "."),
    (os.path.join(BACKEND, "rigger"), "rigger"),
]

a = Analysis(
    [os.path.join(BACKEND, "desktop.py")],
    pathex=[BACKEND],
    datas=datas,
    hiddenimports=[
        # uvicorn resolves these at runtime; PyInstaller can't see them.
        "uvicorn.logging", "uvicorn.loops", "uvicorn.loops.auto",
        "uvicorn.protocols", "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto", "uvicorn.protocols.http.h11_impl",
        "uvicorn.protocols.websockets", "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan", "uvicorn.lifespan.on", "uvicorn.lifespan.off",
    ],
    excludes=["tkinter"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="CharacterEditor",
    icon=os.path.join(SPECPATH, "assets", "app.ico"),
    console=False,
    upx=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    name="CharacterEditor",
    upx=False,
)
