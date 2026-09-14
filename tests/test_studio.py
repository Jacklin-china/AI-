"""工作台付费边界、任务持久化与重启恢复的离线验证。"""

from pathlib import Path
from types import SimpleNamespace

import pytest
from test_image_gen import _settings

from kantoku.config import BudgetError, ToolError
from kantoku.core import budget
from kantoku.tools import studio
from kantoku.tools.image_gen import LocalFakeImageProvider


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> LocalFakeImageProvider:
    settings = _settings(tmp_path / "studio.db")
    settings.image = SimpleNamespace(prompt_max_chars=1000, force_single=True)
    monkeypatch.setattr(studio, "get_settings", lambda: settings)
    monkeypatch.setattr(budget, "get_settings", lambda: settings)
    return LocalFakeImageProvider(tmp_path / "images", model_id="test", actual_fen=30)


def test_create_reload_generate_recover_exportable_image(provider: LocalFakeImageProvider) -> None:
    task = studio.create_task("第一份海报", "自然光下的咖啡", 1, 30)
    assert studio.list_tasks() == [task]
    assert budget.get_reservation(task.request_id) is None
    result = studio.execute_task(task, provider=provider, confirmed=True)
    assert result.path is not None and result.path.is_file()
    reloaded = studio.list_tasks()[0]
    assert studio.recover_task(reloaded, provider=provider) == result
    assert studio.execute_task(reloaded, provider=provider, confirmed=True) == result
    assert provider.submit_count == 1


def test_no_confirmation_no_paid_call(provider: LocalFakeImageProvider) -> None:
    task = studio.create_task("p", "coffee", 1, 30)
    with pytest.raises(BudgetError):
        studio.execute_task(task, provider=provider)
    with pytest.raises(ToolError, match="尚未提交"):
        studio.recover_task(task, provider=provider)
    assert provider.submit_count == 0
    assert budget.get_reservation(task.request_id) is None


def test_budget_rejects_before_submission(provider: LocalFakeImageProvider) -> None:
    task = studio.create_task("p", "coffee", 1, 101)
    with pytest.raises(BudgetError):
        studio.execute_task(task, provider=provider, confirmed=True)
    assert provider.submit_count == 0


def test_unknown_task_queries_original_job_only(
    provider: LocalFakeImageProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = provider.query
    monkeypatch.setattr(provider, "query", lambda job: (_ for _ in ()).throw(TimeoutError()))
    task = studio.create_task("p", "coffee", 1, 30)
    result = studio.execute_task(task, provider=provider, confirmed=True)
    assert result.status == "unknown"
    assert budget.summarize_budget("p").held_fen == 30
    with pytest.raises(BudgetError, match="避免重复收费"):
        studio.create_task("p", "another coffee", 1, 30)
    monkeypatch.setattr(provider, "query", original)
    assert studio.recover_task(studio.list_tasks()[0], provider=provider).status == "succeeded"
    assert provider.submit_count == 1


def test_invalid_and_unwritable_drafts_never_submit(
    provider: LocalFakeImageProvider,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    with pytest.raises(ToolError):
        studio.create_task(" ", "coffee", 1, 30)
    with pytest.raises(ToolError):
        studio.create_task("p", "x" * 1001, 1, 30)
    blocker = tmp_path / "not-a-folder"
    blocker.touch()
    monkeypatch.setattr(studio, "task_directory", lambda: blocker / "child")
    with pytest.raises(ToolError, match="尚未提交"):
        studio.create_task("p", "coffee", 1, 30)
    assert provider.submit_count == 0


def test_ai_design_confirmation_and_output_validation(
    provider: LocalFakeImageProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []

    def caller(messages: object) -> SimpleNamespace:
        calls.append(messages)
        return SimpleNamespace(content="自然侧光，咖啡杯，留白")

    monkeypatch.setattr(studio, "chat", caller)
    with pytest.raises(BudgetError):
        studio.refine_prompt("coffee")
    assert not calls
    assert studio.refine_prompt("coffee", confirmed=True) == "自然侧光，咖啡杯，留白"
    assert len(calls) == 1
    monkeypatch.setattr(studio, "chat", lambda messages: SimpleNamespace(content=""))
    with pytest.raises(ToolError):
        studio.refine_prompt("coffee", confirmed=True)


def test_window_opens_and_displays_saved_image(
    provider: LocalFakeImageProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tkinter as tk

    from kantoku.shells import studio as window

    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("运行环境没有图形显示")
    root.withdraw()
    try:
        task = studio.create_task("window", "coffee", 1, 30)
        result = studio.execute_task(task, provider=provider, confirmed=True)
        app = window.Studio(root)
        app.show_result(result)
        root.update()
        assert app.photo is not None
        assert "生成成功" in app.status.get()
        assert app.history.size() == 1
    finally:
        root.destroy()
