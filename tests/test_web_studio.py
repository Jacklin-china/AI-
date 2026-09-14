"""浏览器工作台：HTTP 会话边界、付费确认、历史记录与恢复。"""

import json
import re
import threading
from collections.abc import Iterator
from http.client import HTTPConnection
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_image_gen import _settings

from kantoku.config import ToolError
from kantoku.core import budget
from kantoku.shells import web_studio
from kantoku.tools import studio
from kantoku.tools.image_gen import LocalFakeImageProvider


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> web_studio.StudioApplication:
    settings = _settings(tmp_path / "budget.db")
    settings.image = SimpleNamespace(
        prompt_max_chars=1000, force_single=True, model="test", width=100, height=100
    )
    settings.llm = SimpleNamespace(model_chat="text-test", model_vision="vision-test")
    for module in (web_studio, studio, budget):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    provider = LocalFakeImageProvider(tmp_path / "output", model_id="test", actual_fen=30)
    monkeypatch.setattr(web_studio, "_provider", lambda: provider)
    return web_studio.StudioApplication()


@pytest.fixture
def server(app: web_studio.StudioApplication) -> Iterator[int]:
    server = web_studio.make_server(app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_http_root_and_session_boundary(server: int, app: web_studio.StudioApplication) -> None:
    connection = HTTPConnection("127.0.0.1", server, timeout=5)
    try:
        connection.request("GET", "/")
        response = connection.getresponse()
        assert response.status == 200
        assert app.token.encode() in response.read()
        for path in ("/api/state", "/media/anything"):
            connection.request("GET", path)
            response = connection.getresponse()
            assert response.status == 403
            response.read()
        connection.request("GET", "/", headers={"Host": "attacker.example"})
        response = connection.getresponse()
        assert response.status == 403
        response.read()
        connection.request(
            "POST",
            "/api/generate",
            body="{}",
            headers={
                "X-Studio-Token": app.token,
                "Origin": "https://attacker.example",
            },
        )
        response = connection.getresponse()
        assert response.status == 403
        response.read()
        assert app.job["state"] == "idle"
        connection.request("GET", "/api/state", headers={"X-Studio-Token": app.token})
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read())["tasks"] == []
    finally:
        connection.close()


def test_vue_bundle_is_served_without_exposing_session_token(
    server: int, app: web_studio.StudioApplication
) -> None:
    connection = HTTPConnection("127.0.0.1", server, timeout=5)
    try:
        connection.request("GET", "/")
        response = connection.getresponse()
        html = response.read().decode("utf-8")
        asset = re.search(r'src="(/assets/[^"]+\.js)"', html)
        assert response.status == 200
        assert asset is not None
        assert "__TOKEN__" not in html

        connection.request("GET", asset.group(1))
        response = connection.getresponse()
        bundle = response.read()
        assert response.status == 200
        assert response.getheader("Content-Type") == "text/javascript"
        assert app.token.encode() not in bundle

        connection.request("GET", "/assets/%2e%2e/%2e%2e/.env")
        response = connection.getresponse()
        assert response.status == 404
        response.read()
    finally:
        connection.close()


def test_generate_requires_confirmation_and_reuses_original_task(
    app: web_studio.StudioApplication,
) -> None:
    data = {"project": "p", "prompt": "coffee", "shot_no": 1, "price": "0.30"}
    with pytest.raises(ToolError, match="明确确认"):
        app.perform("generate", data)
    assert studio.list_tasks() == []
    result = app.perform("generate", {**data, "confirmed": True})
    assert result["status"] == "succeeded"
    recovery = app.perform("recover", {"request_id": result["request_id"], "confirmed": True})
    assert recovery == result
    state = app.state()
    assert state["tasks"][0]["has_image"] is True
    assert budget.summarize_budget("p").task_count == 1


def test_media_reads_only_recorded_asset(server: int, app: web_studio.StudioApplication) -> None:
    result = app.perform(
        "generate",
        {
            "project": "p",
            "prompt": "coffee",
            "shot_no": 1,
            "price": "0.30",
            "confirmed": True,
        },
    )
    connection = HTTPConnection("127.0.0.1", server, timeout=5)
    try:
        connection.request(
            "GET", "/media/" + result["request_id"], headers={"X-Studio-Token": app.token}
        )
        response = connection.getresponse()
        assert response.status == 200
        assert response.read().startswith(b"\x89PNG")
        connection.request("GET", "/media/../../.env", headers={"X-Studio-Token": app.token})
        response = connection.getresponse()
        assert response.status == 400
        response.read()
    finally:
        connection.close()


def test_single_background_operation_and_recovery_of_worker_error(
    app: web_studio.StudioApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = threading.Event()
    completed = threading.Event()

    def blocked(action: str, data: dict[str, object]) -> dict[str, object]:
        gate.wait(5)
        completed.set()
        raise ToolError("mock failure")

    monkeypatch.setattr(app, "perform", blocked)
    app.start("compose", {})
    try:
        with pytest.raises(ToolError, match="已有操作"):
            app.start("generate", {})
    finally:
        gate.set()
        assert completed.wait(5)


def test_narrative_prompt_does_not_insert_poster_layout() -> None:
    narrative = studio.compose_prompt("雨天搀扶老人过街", "叙事静帧", "普通观众", "纪实")
    assert "海报要求" not in narrative
    assert "支撑关系" in narrative
    assert "海报要求" in studio.compose_prompt("咖啡", "宣传海报", "客人", "插画")
