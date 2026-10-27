"""PyInstaller's entry point for pinkas.exe (packaging/build.py)."""

import sys

from pinkas.desktop import main

sys.exit(main())
