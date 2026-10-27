"""The Windows build in two steps, so each can be signed before the next (.github/workflows/release-windows.yml):

    uv run --no-project --python 3.14 python packaging/winpthread.py
    uv run --exact --no-dev --group release python packaging/build.py app 1.0.0
    uv run --exact --no-dev --group release python packaging/build.py installer 1.0.0

`app` writes PyInstaller's folder, build/pyinstaller/dist/pinkas; `installer` wraps that folder with Inno Setup
(packaging/pinkas.iss) into build/pinkas-setup-<version>.exe and prints its SHA-256. Windows only. Without the dev
group, and with --exact taking out what the tests installed, their packages stay out of the build.
"""

import hashlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"
WORK = BUILD / "pyinstaller"
DLL = BUILD / "dlls" / "libwinpthread-1.dll"
ISCC = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Inno Setup 6" / "ISCC.exe"
# web.ROOT is the folder above the package, which in the build is PyInstaller's _internal: the data goes there.
DATA = {"templates": "templates", "static": "static", "addon/Pinkas": "addon/Pinkas", "migrations": "migrations",
        "alembic.ini": "."}
# The exe's own name and version: SignPath signs only a binary whose metadata names this product and version.
VERSION_INFO = """VSVersionInfo(
  ffi=FixedFileInfo(filevers={numbers}, prodvers={numbers}),
  kids=[
    StringFileInfo([StringTable("040904B0", [
      StringStruct("ProductName", "pinkas"), StringStruct("ProductVersion", "{version}"),
      StringStruct("FileDescription", "pinkas"), StringStruct("FileVersion", "{version}"),
      StringStruct("OriginalFilename", "pinkas.exe")])]),
    VarFileInfo([VarStruct("Translation", [1033, 1200])]),
  ],
)
"""


def app(version: str) -> None:
    import pixeltable_pgserver

    postgres = Path(pixeltable_pgserver.__file__).parent / "pginstall18"
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True)
    numbers = (*map(int, version.split(".")), 0)
    (WORK / "version.txt").write_text(VERSION_INFO.format(numbers=numbers, version=version), encoding="utf-8")
    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--onedir", "--windowed", "--name", "pinkas",
        "--distpath", str(WORK / "dist"), "--workpath", str(WORK / "work"), "--specpath", str(WORK),
        "--version-file", str(WORK / "version.txt"),
        # pinkas isn't installed (package = false): main.py's import is found from here, whatever the folder.
        "--paths", str(ROOT),
        # Only Postgres 18: the package's Postgres 16 would add 40 MB the app never runs.
        "--collect-submodules", "pixeltable_pgserver",
        "--add-data", f"{postgres};pixeltable_pgserver/pginstall18",
        # pginstall18's postgres.exe links it, and the package doesn't ship it.
        "--add-binary", f"{DLL};pixeltable_pgserver/pginstall18/bin",
        # The migrations are data files to PyInstaller, so it never sees what they import.
        "--hidden-import", "sqlalchemy.dialects.postgresql.psycopg2", "--hidden-import", "logging.config",
        *(arg for source, target in DATA.items() for arg in ("--add-data", f"{ROOT / source};{target}")),
        str(ROOT / "packaging" / "main.py"),
    ], check=True)


def installer(version: str) -> None:
    if not ISCC.exists():
        sys.exit(f"Inno Setup 6 is missing: the folder alone is in {WORK / 'dist' / 'pinkas'}")

    subprocess.run([str(ISCC), f"/DAppVersion={version}", str(ROOT / "packaging" / "pinkas.iss")], check=True)
    built = BUILD / f"pinkas-setup-{version}.exe"
    print(f"{built.name} SHA-256 {hashlib.sha256(built.read_bytes()).hexdigest()}")


if __name__ == "__main__":
    step, version = sys.argv[1:3]

    # Windows' version resource takes numbers only.
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        sys.exit(f"{version}: a version is three numbers, as in 1.0.0")

    {"app": app, "installer": installer}[step](version)
