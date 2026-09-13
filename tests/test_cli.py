from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kantoku.config import LLMError
from kantoku.shells import cli


@pytest.fixture(autouse=True)
def offline_cli(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    monkeypatch.setattr(cli, "setup_logging", MagicMock())
    monkeypatch.setattr(cli, "init_trace_table", MagicMock())
    monkeypatch.setattr(cli, "list_personas", MagicMock(return_value=[]))
    monkeypatch.setattr(cli, "list_ledger", MagicMock(return_value=[]))
    mock = MagicMock(return_value="你好")
    monkeypatch.setattr(cli, "run_agent", mock)
    return mock


def test_default_help_does_not_call_api(offline_cli: MagicMock) -> None:
    assert cli.run([]) == 0
    offline_cli.assert_not_called()


def test_one_question(offline_cli: MagicMock, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.run(["--ask", "你好"]) == 0
    offline_cli.assert_called_once_with("你好")
    assert "你好" in capsys.readouterr().out


def test_interactive_five_rounds_and_trace_command(
    monkeypatch: pytest.MonkeyPatch,
    offline_cli: MagicMock,
) -> None:
    entries = iter([" ", "一", "二", "三", "四", "五", "/traces", "exit"])
    monkeypatch.setattr("builtins.input", lambda _: next(entries))
    traces = MagicMock(return_value=[])
    monkeypatch.setattr(cli, "recent_traces", traces)
    assert cli.run(["--chat"]) == 0
    assert offline_cli.call_count == 5
    traces.assert_called_once_with(5)


def test_error_prints_safe_message_only(
    offline_cli: MagicMock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    offline_cli.side_effect = LLMError("请求失败", detail="DO-NOT-LOG-THIS")
    assert cli.run(["--ask", "测试"]) == 1
    error_output = capsys.readouterr().err
    assert "请求失败" in error_output
    assert "DO-NOT-LOG-THIS" not in error_output


@pytest.mark.parametrize("args", [["--traces", "0"], ["--ledger", "0"], ["--ask", "  "]])
def test_invalid_arguments_fail_before_call(args: list[str], offline_cli: MagicMock) -> None:
    with pytest.raises(SystemExit) as caught:
        cli.run(args)
    assert caught.value.code == 2
    offline_cli.assert_not_called()


def test_validate_file_without_api_or_runtime_initialization(
    tmp_path: Path,
    offline_cli: MagicMock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "storyboard.json"
    path.write_text(
        '{"episode":"测试","shots":[{"shot_no":1,"desc":"空街",'
        '"camera":"远景","duration_s":3,"characters":[]}]}',
        encoding="utf-8",
    )
    assert cli.run(["--validate-storyboard", str(path)]) == 0
    assert "1 镜" in capsys.readouterr().out
    offline_cli.assert_not_called()
    cli.init_trace_table.assert_not_called()


def test_validate_missing_file_fails_cleanly(tmp_path: Path) -> None:
    assert cli.run(["--validate-storyboard", str(tmp_path / "missing.json")]) == 1


def test_save_persona_file_without_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    offline_cli: MagicMock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "persona.json"
    path.write_text(
        '{"name":"小雨","appearance":"黑色短发","outfit":"红色围裙","style_tokens":["日系动画"]}',
        encoding="utf-8",
    )
    saver = MagicMock()
    monkeypatch.setattr(cli, "save_persona", saver)

    assert cli.run(["--save-persona", str(path)]) == 0
    assert saver.call_args.args[0].name == "小雨"
    assert "角色卡已保存：小雨" in capsys.readouterr().out
    offline_cli.assert_not_called()
    cli.init_trace_table.assert_not_called()


def test_context_command_prints_reduction_without_api(
    monkeypatch: pytest.MonkeyPatch,
    offline_cli: MagicMock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    board = SimpleNamespace(
        shots=[
            SimpleNamespace(
                shot_no=1,
                characters=[],
                model_dump=lambda **_: {"shot_no": 1},
            )
        ]
    )
    monkeypatch.setattr(cli, "load_storyboard", MagicMock(return_value=board))
    monkeypatch.setattr(cli, "build_context", MagicMock(return_value='{"cropped":true}'))
    monkeypatch.setattr(cli, "estimate_tokens", MagicMock(side_effect=[100, 25]))

    assert cli.run(["--context", "测试集", "1"]) == 0
    output = capsys.readouterr().out
    assert "100 → 25" in output
    assert "减少 75.0%" in output
    offline_cli.assert_not_called()


@pytest.mark.parametrize("stop", [EOFError(), KeyboardInterrupt()])
def test_interactive_exit_signal(monkeypatch: pytest.MonkeyPatch, stop: BaseException) -> None:
    monkeypatch.setattr("builtins.input", MagicMock(side_effect=stop))
    assert cli.run(["--chat"]) == 0


@pytest.mark.parametrize("arguments", [[], ["--help"]])
def test_real_cli_entrypoint_emits_utf8_without_api(arguments: list[str]) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "kantoku.shells.cli", *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
        env={**os.environ, "PYTHONIOENCODING": "ascii"},
    )
    assert result.returncode == 0
    assert "监督酱" in result.stdout
    assert "--validate-storyboard" in result.stdout
    assert result.stderr == ""


@pytest.mark.parametrize(
    "marker,label",
    [
        (True, "0/0（已报告）"),
        (False, "未知（供应商未报告有效用量）"),
        (None, "未知（历史记录或调用方未标记）"),
    ],
)
def test_trace_display_distinguishes_unknown_from_zero(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    marker: bool | None,
    label: str,
) -> None:
    record = SimpleNamespace(
        ts="test-time",
        kind="llm.chat.attempt",
        model="test-model",
        cost_fen=None,
        ok=True,
        latency_ms=1,
        in_tokens=0,
        out_tokens=0,
        usage_reported=marker,
    )
    monkeypatch.setattr(cli, "recent_traces", MagicMock(return_value=[record]))
    assert cli.run(["--traces", "1"]) == 0
    assert f"token={label}" in capsys.readouterr().out


def test_ledger_display_preserves_unknown_actual_cost(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    record = SimpleNamespace(
        reservation_id="request-1",
        created_at="test-time",
        project="video-001",
        episode="episode-001",
        shot_no=1,
        status="unknown",
        est_fen=30,
        actual_fen=None,
        model="image-model",
        provider_job_id="provider-1",
    )
    ledger = MagicMock(return_value=[record])
    monkeypatch.setattr(cli, "list_ledger", ledger)

    assert cli.run(["--ledger", "5"]) == 0
    output = capsys.readouterr().out
    assert "实际=待对账" in output
    assert "实际=0 分" not in output
    ledger.assert_called_once_with(5)
