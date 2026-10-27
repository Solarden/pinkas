"""Fetch libwinpthread-1.dll into a folder: the build's (build/dlls, the default) or the spike's.

    uv run --no-project --python 3.14 python packaging/winpthread.py [folder]

pixeltable-pgserver 0.6.0's Windows Postgres 18 is built with MinGW-Builds GCC 14.2 (UCRT) and links
this DLL without shipping it. This takes it from MSYS2's ucrt64 package, both hashes pinned.
Python 3.14 or newer: it reads the .tar.zst with the standard library.
"""

import hashlib
import io
import sys
import tarfile
import urllib.request
from pathlib import Path

URL = "https://repo.msys2.org/mingw/ucrt64/mingw-w64-ucrt-x86_64-libwinpthread-git-12.0.0.r747.g1a99f8514-1-any.pkg.tar.zst"
PACKAGE_SHA256 = "6884e4ee2b7ea8ae7864290255658d5706945006477a7ba94a928fdf6770992a"
DLL_SHA256 = "459e5414cd8a125bd903dacc5c46227c32ec5a8bdc6d879c062e199dda68cfd4"
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "build/dlls")

package = urllib.request.urlopen(URL, timeout=60).read()

if hashlib.sha256(package).hexdigest() != PACKAGE_SHA256:
    raise SystemExit("package hash mismatch")

with tarfile.open(fileobj=io.BytesIO(package), mode="r:zst") as archive:
    dll = archive.extractfile("ucrt64/bin/libwinpthread-1.dll").read()

if hashlib.sha256(dll).hexdigest() != DLL_SHA256:
    raise SystemExit("DLL hash mismatch")

OUT.mkdir(parents=True, exist_ok=True)
(OUT / "libwinpthread-1.dll").write_bytes(dll)
print(f"{OUT / 'libwinpthread-1.dll'} written")
