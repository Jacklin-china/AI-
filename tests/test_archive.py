"""人工终审图片归档测试。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from kantoku.config import ToolError
from kantoku.schemas.media import ImageGenerationResult
from kantoku.schemas.qc import HumanQcLabel, QcResult
from kantoku.tools import archive


def _label(path: Path, *, approved: bool = True) -> HumanQcLabel:
    return HumanQcLabel(
        id="qc-001",
        image_path=path,
        target_platform="抖音",
        genre="都市治愈漫剧",
        target_audience="18-30 岁女性",
        cinematography_requirements="主体明确，冷暖光分离",
        cinematography_notes="主体明确，冷暖光分离",
        result=QcResult(
            broken_hands=False,
            watermark=False,
            composition_ok=True,
            persona_consistency=4,
            confidence=1.0,
            reason="人工确认可用" if approved else "画面文字乱码",
        ),
        approved=approved,
        failure_reasons=[] if approved else ["garbled_text"],
    )


def test_archive_is_idempotent_and_keeps_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\narchive-image")
    monkeypatch.setattr(
        archive,
        "load_generation_result",
        lambda _: ImageGenerationResult(
            path=source,
            provider_job_id="provider-001",
            status="succeeded",
        ),
    )
    monkeypatch.setattr(archive, "load_human_review", lambda _: _label(source))
    monkeypatch.setattr(archive, "_archive_root", lambda: tmp_path / "archive")
    monkeypatch.setattr(archive, "_database_path", lambda: tmp_path / "kantoku.db")
    monkeypatch.setattr(
        archive,
        "get_reservation",
        lambda _: SimpleNamespace(project="demo", episode="ep01", shot_no=1),
    )
    monkeypatch.setattr(archive, "_characters_for", lambda *_: ("小雨",))

    first = archive.archive_reviewed_image("request-001")
    second = archive.archive_reviewed_image("request-001")

    assert first == second
    assert source.is_file()
    assert first.image_path.read_bytes() == source.read_bytes()
    metadata = json.loads(first.metadata_path.read_text(encoding="utf-8"))
    assert metadata["source_request_id"] == "request-001"
    assert metadata["label"]["approved"] is True
    assert metadata["characters"] == ["小雨"]
    assert first.shot_no == 1
    assert "shot-001" in first.image_path.name
    assert archive.search_archived_images(character="小雨") == [first]
    assert archive.search_archived_images(character="阿杰") == []


def test_archive_requires_human_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\narchive-image")
    monkeypatch.setattr(
        archive,
        "load_generation_result",
        lambda _: ImageGenerationResult(
            path=source,
            provider_job_id="provider-001",
            status="succeeded",
        ),
    )
    monkeypatch.setattr(archive, "load_human_review", lambda _: None)

    with pytest.raises(ToolError, match="人工终审"):
        archive.archive_reviewed_image("request-001")


def test_archive_never_overwrites_different_content(tmp_path: Path) -> None:
    target = tmp_path / "result.png"
    target.write_bytes(b"old")

    with pytest.raises(ToolError, match="禁止覆盖"):
        archive._write_once(target, b"new")
