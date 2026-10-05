"""PyInstaller entry point (kept outside the package so imports stay absolute)."""

import sys

from fmapp.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
