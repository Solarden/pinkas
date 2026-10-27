"""The test suite on Windows, against the app's own Postgres instead of docker-compose.test.yml.

    uv run --no-project --python 3.14 python packaging/winpthread.py
    uv run python packaging/run_tests.py [pytest arguments]

The release build runs it before it packages anything; the pull's Unix-only tests skip themselves.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import psycopg2

ROOT = Path(__file__).resolve().parent.parent
# The project isn't installed (package = false), and a script's own folder is the only one Python adds.
sys.path.insert(0, str(ROOT))
# The package's postgres.exe links libwinpthread-1.dll without shipping it; the build copies it in beside it.
os.environ["PATH"] = f"{ROOT / 'build' / 'dlls'}{os.pathsep}{os.environ['PATH']}"

from pinkas import postgres  # noqa: E402

base = Path(tempfile.mkdtemp(prefix="pinkas-tests-"))
server = postgres.start(base)
password = json.loads((base / "secrets.json").read_text(encoding="utf-8"))["postgres"]
admin = psycopg2.connect(postgres.dsn(server.port, "postgres", password, "postgres"))
admin.autocommit = True

# tests/conftest.py wipes only a database whose name ends in _test.
with admin.cursor() as cur:
    cur.execute("CREATE DATABASE pinkas_test")

admin.close()
env = dict(os.environ, PINKAS_TEST_DATABASE_URL=postgres.dsn(server.port, "postgres", password, "pinkas_test"))

try:
    code = subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *sys.argv[1:]], env=env).returncode
finally:
    postgres.stop(base / "pg")
    shutil.rmtree(base, ignore_errors=True)

sys.exit(code)
