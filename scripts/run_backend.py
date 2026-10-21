"""PyCharm-friendly development launcher for the Kantoku backend."""

from __future__ import annotations

import sys

from kantoku.__main__ import main

if __name__ == "__main__":
    sys.argv[1:] = ["serve", "--no-browser", *sys.argv[1:]]
    raise SystemExit(main())
