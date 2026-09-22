"""PyCharm-friendly development launcher for the Kantoku backend."""

from __future__ import annotations

from kantoku.config.logging_setup import setup_logging
from kantoku.shells.web_studio import serve

if __name__ == "__main__":
    setup_logging()
    raise SystemExit(serve(port=8000, open_browser=False))
