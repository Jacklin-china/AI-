"""真实验收工具的安全门禁离线回归；绝不发起付费请求。"""

from pathlib import Path
from runpy import run_path

import pytest
from test_llm import _settings


@pytest.mark.parametrize("arguments", [[], ["--allow-paid", "--max-cny", "3"],
                                       ["--allow-paid", "--max-cny", "0"]])
def test_director_live_eval_requires_explicit_bounded_authority(
    monkeypatch: pytest.MonkeyPatch, arguments: list[str],
) -> None:
    namespace = run_path(str(Path(__file__).parents[1] / "scripts/verify_director_pipeline.py"))
    runner = namespace["run"]
    monkeypatch.setitem(runner.__globals__, "get_settings",
                        lambda: pytest.fail("无授权不得读取凭据或调用模型"))
    monkeypatch.setattr("sys.argv", ["verify_director_pipeline.py", *arguments])
    with pytest.raises(SystemExit) as error:
        runner()
    assert error.value.code == 2


def test_director_live_eval_rejects_unpriced_model_before_app_or_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = run_path(str(Path(__file__).parents[1] / "scripts/verify_director_pipeline.py"))
    runner = namespace["run"]
    monkeypatch.setitem(runner.__globals__, "get_settings", _settings)
    monkeypatch.setitem(runner.__globals__, "StudioApplication",
                        lambda: pytest.fail("未知模型价格不得启动实测"))
    monkeypatch.setattr("sys.argv", ["verify_director_pipeline.py", "--allow-paid"])
    with pytest.raises(SystemExit) as error:
        runner()
    assert error.value.code == 2
