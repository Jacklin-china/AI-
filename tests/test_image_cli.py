"""单镜入口的费用确认、恢复和人工账单集成测试，使用临时库与本地假图。"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kantoku.core import budget
from kantoku.schemas.media import ImageGenerationResult
from kantoku.shells import image_cli
from kantoku.tools.image_batch import parse_image_batch
from kantoku.tools.image_gen import LocalFakeImageProvider, ProviderAccessResult


@pytest.fixture
def environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    settings = SimpleNamespace(
        image=SimpleNamespace(
            provider="volcengine-jimeng",
            model="test-image",
            width=2560,
            height=1440,
            force_single=True,
            prompt_max_chars=800,
        ),
        budget=SimpleNamespace(
            accounting_utc_offset_hours=8,
            image_credit_cny=Decimal("0.10"),
            image_estimated_credits_per_call=3,
            image_daily_cny=Decimal("20"),
            image_project_cny=Decimal("20"),
            image_episode_cny=Decimal("20"),
            image_shot_cny=Decimal("1"),
            image_max_concurrency=1,
        ),
        storage=SimpleNamespace(sqlite_path=tmp_path / "ledger.db"),
    )
    monkeypatch.setattr(budget, "get_settings", lambda: settings)
    monkeypatch.setattr(image_cli, "get_settings", lambda: settings)
    provider = LocalFakeImageProvider(tmp_path / "images", model_id="test-image", actual_fen=30)
    factory = MagicMock(return_value=provider)
    monkeypatch.setattr(image_cli, "_provider", factory)
    prompt = tmp_path / "shot.txt"
    prompt.write_text("雨夜便利店，小雨穿红色围裙站在柜台前。", encoding="utf-8")
    args = [
        "generate",
        "--prompt-file",
        str(prompt),
        "--project",
        "video-001",
        "--episode",
        "ep01",
        "--shot",
        "1",
        "--request-id",
        "ep01-shot01-v1",
    ]
    return SimpleNamespace(settings=settings, provider=provider, factory=factory, args=args)


def test_preview_has_no_provider_or_ledger_side_effects(
    environment: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    assert image_cli.run(environment.args) == 0
    assert "预估：30 分" in capsys.readouterr().out
    environment.factory.assert_not_called()
    assert not environment.settings.storage.sqlite_path.exists()


def test_preflight_validates_local_readiness_without_submission_or_ledger(
    environment: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    assert image_cli.run(["preflight"]) == 0
    output = capsys.readouterr().out
    assert "本地生图自检通过" in output
    assert "没有产生费用" in output
    assert "2560x1440" in output
    assert environment.provider.submit_count == 0
    assert not environment.settings.storage.sqlite_path.exists()


def test_preflight_rejects_multi_image_mode_before_provider(
    environment: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    environment.settings.image.force_single = False

    assert image_cli.run(["preflight"]) == 1

    assert "force_single=true" in capsys.readouterr().err
    environment.factory.assert_not_called()


def test_doctor_checks_access_without_ledger_or_submission(
    environment: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    assert image_cli.run(["doctor"]) == 0
    output = capsys.readouterr().out
    assert "签名鉴权通过" in output
    assert "查询接口已确认可用" in output
    assert "没有创建任务" in output
    assert environment.provider.submit_count == 0
    assert not environment.settings.storage.sqlite_path.exists()


def test_doctor_reports_inconclusive_service_without_side_effects(
    environment: SimpleNamespace,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        environment.provider,
        "check_access",
        lambda: ProviderAccessResult(
            authenticated=None,
            service_ready=False,
            code=50500,
            message="Internal Error",
            request_id="safe-request-id",
        ),
    )

    assert image_cli.run(["doctor"]) == 2
    output = capsys.readouterr().out
    assert "签名未被明确拒绝" in output
    assert "服务可用性尚未确认" in output
    assert "code=50500" in output
    assert environment.provider.submit_count == 0
    assert not environment.settings.storage.sqlite_path.exists()


def test_paid_request_requires_explicit_api_estimate(environment: SimpleNamespace) -> None:
    assert image_cli.run([*environment.args, "--confirm-paid"]) == 1
    environment.factory.assert_not_called()
    assert not environment.settings.storage.sqlite_path.exists()


def test_rework_generation_requires_matching_human_approval(
    environment: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = [
        *environment.args,
        "--estimate-fen",
        "30",
        "--confirm-paid",
        "--rework-of",
        "old-request",
    ]
    monkeypatch.setattr(image_cli, "get_rework_item", lambda _: None)

    assert image_cli.run(args) == 1

    assert "返工尚未人工批准" in capsys.readouterr().err
    environment.factory.assert_not_called()
    assert not environment.settings.storage.sqlite_path.exists()


def test_confirmed_single_shot_is_recorded_and_duplicate_never_resubmits(
    environment: SimpleNamespace,
) -> None:
    args = [*environment.args, "--estimate-fen", "30", "--confirm-paid"]
    assert image_cli.run(args) == 0
    assert image_cli.run(args) == 0
    assert environment.provider.submit_count == 1
    assert budget.get_reservation("ep01-shot01-v1").actual_fen == 30


def test_over_budget_request_does_not_submit(
    environment: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    assert image_cli.run([*environment.args, "--estimate-fen", "101", "--confirm-paid"]) == 1
    assert environment.provider.submit_count == 0
    assert "单镜生图预算不足" in capsys.readouterr().err
    assert budget.list_ledger() == []


def test_query_recovers_unknown_without_resubmitting(
    environment: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    provider = environment.provider
    real_query = provider.query
    monkeypatch.setattr(provider, "query", MagicMock(side_effect=TimeoutError("private")))
    assert image_cli.run([*environment.args, "--estimate-fen", "30", "--confirm-paid"]) == 2
    assert "TimeoutError，需查询或人工对账" in capsys.readouterr().out
    monkeypatch.setattr(provider, "query", real_query)
    assert image_cli.run(["query", "ep01-shot01-v1"]) == 0
    assert provider.submit_count == 1


def test_manual_settlement_requires_confirmation_and_preserves_exact_cost(
    environment: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = environment.provider
    query = provider.query

    def unbilled(task_id: str) -> ImageGenerationResult:
        return query(task_id).model_copy(update={"actual_fen": None})

    monkeypatch.setattr(provider, "query", unbilled)
    assert image_cli.run([*environment.args, "--estimate-fen", "30", "--confirm-paid"]) == 0
    assert budget.get_reservation("ep01-shot01-v1").actual_fen is None
    args = ["settle", "ep01-shot01-v1", "--actual-cny", "0.29"]
    assert image_cli.run(args) == 1
    assert budget.get_reservation("ep01-shot01-v1").status == "succeeded"
    assert image_cli.run([*args, "--confirm-bill"]) == 0
    assert budget.get_reservation("ep01-shot01-v1").actual_fen == 29
    assert image_cli.run([*args, "--confirm-bill"]) == 0
    assert (
        image_cli.run(["settle", "ep01-shot01-v1", "--actual-cny", "0.28", "--confirm-bill"]) == 1
    )
    assert budget.get_reservation("ep01-shot01-v1").actual_fen == 29


@pytest.mark.parametrize("amount", ["-1", "0.001", "NaN", "Infinity", "abc", "1e100"])
def test_invalid_bill_rejected_before_mutation(environment: SimpleNamespace, amount: str) -> None:
    with pytest.raises(SystemExit) as caught:
        image_cli.run(["settle", "unused", "--actual-cny", amount, "--confirm-bill"])
    assert caught.value.code == 2
    environment.factory.assert_not_called()
    assert not environment.settings.storage.sqlite_path.exists()


def test_release_requires_confirmation_and_never_releases_unknown(
    environment: SimpleNamespace,
) -> None:
    def reserve(request_id: str) -> None:
        budget.reserve(
            reservation_id=request_id,
            job="image",
            project="video-001",
            episode="ep01",
            shot_no=1,
            kind="image",
            model="test-image",
            est_fen=30,
        )

    reserve("unsubmitted")
    assert image_cli.run(["release", "unsubmitted"]) == 1
    assert image_cli.run(["release", "unsubmitted", "--confirm-no-charge"]) == 0
    reserve("unknown")
    budget.mark_submitted("unknown")
    budget.mark_outcome("unknown", "unknown")
    assert image_cli.run(["release", "unknown", "--confirm-no-charge"]) == 1
    assert budget.get_reservation("unknown").status == "unknown"
    environment.factory.assert_not_called()


def test_query_missing_provider_id_needs_no_credentials(environment: SimpleNamespace) -> None:
    budget.reserve(
        reservation_id="unknown",
        job="image",
        project="video-001",
        episode="ep01",
        shot_no=1,
        kind="image",
        model="test-image",
        est_fen=30,
    )
    budget.mark_submitted("unknown")
    budget.mark_outcome("unknown", "unknown")
    assert image_cli.run(["query", "unknown"]) == 2
    environment.factory.assert_not_called()


def test_status_without_api_reports_empty_project_budget(
    environment: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    assert image_cli.run(["status", "video-001"]) == 0
    output = capsys.readouterr().out
    assert "已结算：0 分；仍预占：0 分" in output
    assert "可用预算：2000 分" in output
    environment.factory.assert_not_called()


def test_recipe_batch_preview_builds_manifest_without_provider(
    environment: SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    reference_map = tmp_path / "references.json"
    reference_map.write_text('{"hero": "https://example.test/hero.png"}', encoding="utf-8")
    batch = parse_image_batch(
        '{"project":"video-001","episode":"ep01","shots":'
        '[{"request_id":"recipe-s001-fixed","shot_no":1,"prompt":"雨夜镜头"}]}'
    )
    builder = MagicMock(return_value=batch)
    monkeypatch.setattr(image_cli, "build_recipe_batch", builder)

    assert (
        image_cli.run(
            [
                "batch-recipes",
                "--project",
                "video-001",
                "--episode",
                "ep01",
                "--prompt-version",
                "prompt-v1",
                "--reference-map",
                str(reference_map),
            ]
        )
        == 0
    )
    assert "配方版本：prompt-v1" in capsys.readouterr().out
    builder.assert_called_once_with(
        project="video-001",
        episode="ep01",
        prompt_version="prompt-v1",
        reference_urls_by_asset_id={"hero": "https://example.test/hero.png"},
    )
    environment.factory.assert_not_called()
