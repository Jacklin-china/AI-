"""Isolated homepage browser acceptance using the existing offline production fixture.

No external provider is constructed. The one-pixel fake image is a transport test,
not a real creative result. Generation waits for data/qa/quick-comic-ui/release.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

import pytest  # noqa: E402
from test_home_quick_domain import production  # noqa: E402
from test_web_studio import app as app_fixture  # noqa: E402

from kantoku.shells import web_studio  # noqa: E402


def main() -> None:
    directory = ROOT / "data" / "qa" / "quick-comic-ui"
    directory.mkdir(parents=True, exist_ok=True)
    patch = pytest.MonkeyPatch()
    app = app_fixture.__wrapped__(patch, directory)
    fixture = production.__wrapped__(app, patch)
    next(fixture)
    patch.setattr(web_studio, "STATIC", directory / "web")
    provider = app.image_service.provider
    query = provider.query
    stopped = threading.Event()

    def waiting(job_id: str):
        while not stopped.wait(1):
            if (directory / "release").exists():
                return query(job_id)
        raise RuntimeError("Offline UI acceptance stopped, no real image submitted")

    patch.setattr(provider, "query", waiting)
    server = web_studio.make_server(app, port=8768)
    print("Offline homepage QA: http://127.0.0.1:8768/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stopped.set()
        server.shutdown()
        server.server_close()
        # Never let a test worker outlive its provider guards.
        app.runner.close()
        fixture.close()
        patch.undo()


if __name__ == "__main__":
    main()
