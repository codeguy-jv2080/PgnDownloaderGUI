# Build only through build.bat / package_app.py, after closing the application.
from pathlib import Path

from PyInstaller.config import CONF
from PyInstaller.utils.hooks import collect_submodules
from package_app import collect_in_place, require_build_context


project_root = Path(SPECPATH)
require_build_context(project_root, Path(CONF["distpath"]), Path(CONF["workpath"]))


class InPlaceCOLLECT(COLLECT):
    def assemble(self):
        collect_in_place(self)


datas = [
    (str(project_root / "web"), "web"),
    (str(project_root / "THIRD_PARTY_NOTICES.md"), "."),
]
for source_path in (project_root / "vendor" / "pgn_downloader").rglob("*"):
    if source_path.is_file() and "__pycache__" not in source_path.parts and source_path.suffix != ".pyc":
        datas.append((str(source_path), source_path.parent.relative_to(project_root).as_posix()))
if (project_root / "LICENSE").is_file():
    datas.append((str(project_root / "LICENSE"), "."))

hiddenimports = (
    collect_submodules("uvicorn")
    + collect_submodules("webview")
    + [
        "pgn_downloader",
        "pgn_downloader.lichess",
        "pgn_downloader.chess_com",
        "pgn_downloader.date_parser",
    ]
)

a = Analysis(
    [str(project_root / "main.py")],
    pathex=[str(project_root), str(project_root / "vendor")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6", "gi"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PgnDownloaderGUI",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=True,
    contents_directory="_internal",
    uac_admin=False,
)
coll = InPlaceCOLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="PgnDownloaderGUI",
)
