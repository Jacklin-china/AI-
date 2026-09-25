"""Local diagnostics must not report an unusable Qwen endpoint as ready."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from kantoku import doctor
from kantoku.config import ConfigError


def test_doctor_blocks_qwen_when_workspace_endpoint_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = SimpleNamespace(
        llm=SimpleNamespace(
            api_key_env="DEEPSEEK_API_KEY", model_chat="deepseek-chat",
            chat_api_key=lambda: "configured",
            vision_api_key_env="ARK_API_KEY", model_vision="qwen3-vl-plus",
            vision_api_key=lambda: "configured",
        ),
        image=SimpleNamespace(
            provider="alibaba-qwen-image", api_key_env="DASHSCOPE_API_KEY",
            model="qwen-image-3.0", api_key=lambda: "configured",
        ),
    )

    def reject_placeholder(**_kwargs: object) -> None:
        raise ConfigError("workspace required")

    monkeypatch.setattr(doctor, "get_settings", lambda: settings)
    monkeypatch.setattr(doctor, "RuntimeStore", lambda _path: object())
    monkeypatch.setattr(doctor, "configured_database_path", lambda: tmp_path / "db.sqlite")
    monkeypatch.setattr(
        doctor, "_provider", lambda: SimpleNamespace(validate_request=reject_placeholder),
    )

    qwen = next(check for check in doctor.run_doctor() if check.name == "Qwen Image")
    assert qwen.status == "BLOCKED"
    assert "业务空间地址未就绪" in qwen.detail
