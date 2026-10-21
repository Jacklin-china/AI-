"""Isolated homepage browser acceptance using the existing offline production fixture.

No external provider is constructed. The one-pixel fake image is a transport test,
not a real creative result. Generation waits for data/qa/quick-comic-ui/release.
"""

from __future__ import annotations

import argparse
import sys
import threading
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

import pytest  # noqa: E402
from test_home_quick_domain import production  # noqa: E402
from test_web_studio import app as app_fixture  # noqa: E402

from kantoku.config import ToolError, logging_setup  # noqa: E402
from kantoku.config.runtime_paths import get_runtime_paths, isolated_runtime  # noqa: E402
from kantoku.shells import web_studio  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, help="旧数据只保留，不再复用")
    args = parser.parse_args()
    if args.directory:
        parser.error("本验收脚本只使用自动隔离目录；旧 QA 数据保留供人工审查")
    with isolated_runtime(prefix="kantoku-home-"), ExitStack() as cleanup:
        _run(get_runtime_paths().data, cleanup)


def _run(directory: Path, cleanup: ExitStack) -> None:
    print(f"QA temporary data: {directory}", flush=True)
    logging_setup.LOG_DIR = get_runtime_paths().logs
    logging_setup.setup_logging("INFO")
    patch = pytest.MonkeyPatch()
    app = app_fixture.__wrapped__(patch, directory, SimpleNamespace(addfinalizer=cleanup.callback))
    fixture = production.__wrapped__(app, patch)
    next(fixture)
    # Use an isolated build when supplied, otherwise the existing production build.
    if (directory / "web" / "index.html").is_file():
        patch.setattr(web_studio, "STATIC", directory / "web")
    provider = app.image_service.provider
    query = provider.query
    submit = provider.submit
    stopped = threading.Event()

    def submission(**kwargs):
        if (directory / "fail-image").exists():
            # Explicit, offline HTTP rejection; there is no real provider or charge.
            import httpx2
            from openai import BadRequestError

            response = httpx2.Response(400, request=httpx2.Request("POST", "https://offline.test"),
                                       headers={"x-request-id": "offline-error-request"})
            try:
                raise BadRequestError("Offline diagnostic rejection", response=response,
                                      body={"code": "OfflineTestError", "message": "offline only"})
            except BadRequestError as cause:
                raise ToolError("离线模拟图片请求拒绝") from cause
        return submit(**kwargs)

    def waiting(job_id: str):
        while not stopped.wait(1):
            if (directory / "release").exists():
                return query(job_id)
        raise RuntimeError("Offline UI acceptance stopped, no real image submitted")

    patch.setattr(provider, "query", waiting)
    patch.setattr(provider, "submit", submission)
    server = web_studio.make_server(app, port=8768)
    print("Offline homepage QA: http://127.0.0.1:8768/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stopped.set()
        server.server_close()
        # Never let a test worker outlive its provider guards.
        app.runner.close()
        fixture.close()
        patch.undo()
        logging_setup.logger.remove()
        logging_setup.LOG_DIR = None


if __name__ == "__main__":
    main()
