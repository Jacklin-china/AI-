"""不联网验证生图付费编排与未知账单恢复。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import pytest

from kantoku.config import BudgetError, ToolError
from kantoku.config.observability import run_trace
from kantoku.core import budget
from kantoku.schemas.media import ImageGenerationResult
from kantoku.tools import image_gen
from kantoku.tools.image_gen import (
    LocalFakeImageProvider,
    gen_image,
    prepare_image_reservation,
    reconcile_image,
    release_failed_image,
)


def _settings(path: Path, *, shot_limit: Decimal = Decimal("1.00")) -> SimpleNamespace:
    return SimpleNamespace(
        budget=SimpleNamespace(
            accounting_utc_offset_hours=8,
            image_credit_cny=Decimal("0.10"),
            image_estimated_credits_per_call=3,
            image_daily_cny=Decimal("20.00"),
            image_project_cny=Decimal("20.00"),
            image_episode_cny=Decimal("20.00"),
            image_shot_cny=shot_limit,
            image_max_concurrency=1,
            token_daily_limit=200000,
        ),
        storage=SimpleNamespace(sqlite_path=path),
    )


@pytest.fixture(autouse=True)
def isolated_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "budget.db"
    monkeypatch.setattr(budget, "get_settings", lambda: _settings(path))
    return path


def _generate(
    provider: LocalFakeImageProvider,
    *,
    client_request_id: str = "request-1",
) -> ImageGenerationResult:
    return gen_image(
        "雨夜便利店，人物站在收银台前",
        1,
        project="video-001",
        episode="episode-001",
        client_request_id=client_request_id,
        provider=provider,
    )


def test_fake_generation_runs_reserve_submit_query_and_settle(tmp_path: Path) -> None:
    provider = LocalFakeImageProvider(
        tmp_path / "images", model_id="configured-image-model", actual_fen=30
    )

    result = _generate(provider)

    assert result.status == "succeeded"
    assert result.path is not None and result.path.read_bytes().startswith(b"\x89PNG")
    saved = budget.get_reservation("request-1")
    assert saved is not None
    assert saved.status == "settled"
    assert saved.actual_fen == 30
    assert provider.submit_count == 1


def test_ten_shot_dry_run_stays_far_below_video_budget(tmp_path: Path) -> None:
    provider = LocalFakeImageProvider(
        tmp_path / "images", model_id="configured-image-model", actual_fen=30
    )

    results = [
        gen_image(
            f"镜头 {shot_no}",
            shot_no,
            project="video-ten-shots",
            episode="episode-001",
            client_request_id=f"ten-shot-{shot_no}",
            provider=provider,
        )
        for shot_no in range(1, 11)
    ]
    records = budget.list_ledger(project="video-ten-shots")

    assert all(result.status == "succeeded" for result in results)
    assert len(records) == 10
    assert sum(record.actual_fen or 0 for record in records) == 300
    assert provider.submit_count == 10


def test_budget_rejection_happens_before_provider_submission(
    tmp_path: Path,
    isolated_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        budget,
        "get_settings",
        lambda: _settings(isolated_database, shot_limit=Decimal("0.20")),
    )
    provider = LocalFakeImageProvider(
        tmp_path / "images", model_id="configured-image-model", actual_fen=30
    )

    with pytest.raises(BudgetError, match="单镜"):
        _generate(provider)

    assert provider.submit_count == 0
    assert budget.list_ledger() == []


class _SubmitTimeoutProvider(LocalFakeImageProvider):
    def submit(
        self,
        *,
        prompt: str,
        shot_no: int,
        client_request_id: str,
        reference_urls: Sequence[str],
        seed: int | None,
    ) -> str:
        self.submit_count += 1
        raise TimeoutError("do-not-expose-provider-details")


def test_submit_timeout_becomes_unknown_and_same_request_is_not_resubmitted(
    tmp_path: Path,
) -> None:
    provider = _SubmitTimeoutProvider(
        tmp_path / "images", model_id="configured-image-model", actual_fen=30
    )

    first = _generate(provider, client_request_id="submit-timeout")
    second = _generate(provider, client_request_id="submit-timeout")

    assert first.status == "unknown"
    assert "do-not-expose-provider-details" not in (first.error or "")
    assert second.status == "unknown"
    assert provider.submit_count == 1
    saved = budget.get_reservation("submit-timeout")
    assert saved is not None and saved.status == "unknown"
    saved_result = budget.load_generation_result("submit-timeout")
    assert saved_result is not None
    assert saved_result.error == "NEEDS_RECONCILIATION：TimeoutError，需查询或人工对账"


class _RecoveringQueryProvider(LocalFakeImageProvider):
    def __init__(self, output_dir: Path, *, model_id: str, actual_fen: int) -> None:
        super().__init__(output_dir, model_id=model_id, actual_fen=actual_fen)
        self.query_count = 0

    def query(self, provider_job_id: str) -> ImageGenerationResult:
        self.query_count += 1
        if self.query_count == 1:
            raise TimeoutError("first-query-timeout")
        return super().query(provider_job_id)


def test_query_timeout_is_reconciled_without_second_paid_submission(tmp_path: Path) -> None:
    provider = _RecoveringQueryProvider(
        tmp_path / "images", model_id="configured-image-model", actual_fen=20
    )

    first = _generate(provider, client_request_id="query-timeout")
    recovered = reconcile_image("query-timeout", provider=provider)

    assert first.status == "unknown"
    assert recovered.status == "succeeded"
    assert provider.submit_count == 1
    assert provider.query_count == 2
    saved = budget.get_reservation("query-timeout")
    assert saved is not None
    assert saved.status == "settled"
    assert saved.actual_fen == 20


def test_retry_of_submitted_job_polls_original_task_without_resubmit(tmp_path: Path) -> None:
    provider = _RecoveringQueryProvider(
        tmp_path / "images", model_id="configured-image-model", actual_fen=20
    )
    first = _generate(provider, client_request_id="existing-job")
    recovered = _generate(provider, client_request_id="existing-job")

    assert first.status == "unknown"
    assert recovered.status == "succeeded"
    assert recovered.provider_job_id == first.provider_job_id
    assert provider.submit_count == 1
    assert provider.query_count == 2
    assert budget.get_reservation("existing-job").actual_fen == 20


def test_released_before_submit_is_not_misreported_as_paid_job(tmp_path: Path) -> None:
    provider = LocalFakeImageProvider(
        tmp_path / "images", model_id="configured-image-model", actual_fen=20
    )
    request = prepare_image_reservation(
        "雨夜便利店，人物站在收银台前", 1,
        project="video-001", episode="episode-001",
        client_request_id="released-before-submit", provider=provider,
    )
    budget.reserve(**request.model_dump())
    budget.release(request.reservation_id)

    with pytest.raises(BudgetError, match="未提交且预算已释放"):
        _generate(provider, client_request_id=request.reservation_id)
    assert provider.submit_count == 0


def test_explicit_new_request_creates_new_provider_job(tmp_path: Path) -> None:
    provider = LocalFakeImageProvider(
        tmp_path / "images", model_id="configured-image-model", actual_fen=20
    )
    first = _generate(provider, client_request_id="first-generation")
    second = _generate(provider, client_request_id="user-requested-regeneration")

    assert first.provider_job_id != second.provider_job_id
    assert provider.submit_count == 2
    assert len(budget.list_ledger()) == 2


def test_generation_record_links_run_provider_key_and_artifact(tmp_path: Path) -> None:
    provider = LocalFakeImageProvider(
        tmp_path / "images", model_id="configured-image-model", actual_fen=20
    )
    with run_trace("run-test-generation", "generate"):
        result = _generate(provider, client_request_id="generation-123")
    assert result.status == "succeeded"
    record = budget.get_reservation("generation-123")
    assert record is not None
    assert record.run_id == "run-test-generation"
    assert record.provider == "local-fake"
    assert record.idempotency_key == "generation-123"
    assert record.provider_job_id == result.provider_job_id
    linked = budget.attach_image_artifact("generation-123", "artifact-123")
    assert linked.artifact_id == "artifact-123"


class _UnbilledFailureProvider(LocalFakeImageProvider):
    def query(self, provider_job_id: str) -> ImageGenerationResult:
        return ImageGenerationResult(
            path=None,
            provider_job_id=provider_job_id,
            status="failed",
            actual_fen=None,
            error="供应商确认失败且不扣费",
        )


def test_failed_task_requires_explicit_release_when_bill_is_missing(tmp_path: Path) -> None:
    provider = _UnbilledFailureProvider(
        tmp_path / "images", model_id="configured-image-model", actual_fen=0
    )

    result = _generate(provider, client_request_id="confirmed-failure")
    saved = budget.get_reservation("confirmed-failure")

    assert result.status == "failed"
    assert saved is not None and saved.status == "failed"
    release_failed_image("confirmed-failure")
    released = budget.get_reservation("confirmed-failure")
    assert released is not None and released.status == "released"


def test_concurrent_same_request_submits_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = LocalFakeImageProvider(
        tmp_path / "images", model_id="configured-image-model", actual_fen=30
    )
    barrier = Barrier(2)
    original = image_gen.reserve

    def simultaneous_reserve(**kwargs: object) -> budget.BudgetReservation:
        record = original(**kwargs)
        barrier.wait(timeout=10)
        return record

    monkeypatch.setattr(image_gen, "reserve", simultaneous_reserve)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(_generate, provider) for _ in range(2)]
        results = [future.result(timeout=15) for future in futures]
    assert sorted(result.status for result in results) == ["succeeded", "unknown"]
    assert provider.submit_count == 1
    assert len(budget.list_ledger()) == 1
    assert budget.get_reservation("request-1").actual_fen == 30


def test_reusing_request_id_with_different_prompt_is_rejected(tmp_path: Path) -> None:
    provider = LocalFakeImageProvider(tmp_path, model_id="configured-image-model", actual_fen=30)
    _generate(provider)
    with pytest.raises(BudgetError, match="请求 ID"):
        gen_image(
            "不同的画面",
            1,
            project="video-001",
            episode="episode-001",
            client_request_id="request-1",
            provider=provider,
        )
    assert provider.submit_count == 1


class _MutableIdentityProvider(LocalFakeImageProvider):
    width = 2560
    height = 1440

    def generation_identity(self) -> Mapping[str, str | int | bool]:
        return {
            "provider": "local-fake",
            "model": self.model_id,
            "width": self.width,
            "height": self.height,
            "force_single": True,
        }


def test_reusing_request_id_after_generation_config_change_is_rejected(tmp_path: Path) -> None:
    provider = _MutableIdentityProvider(
        tmp_path, model_id="configured-image-model", actual_fen=30
    )
    _generate(provider)
    provider.width = 1920
    provider.height = 1080

    with pytest.raises(BudgetError, match="请求 ID"):
        _generate(provider)

    assert provider.submit_count == 1


def test_reconcile_rejects_changed_model_before_query(tmp_path: Path) -> None:
    provider = _RecoveringQueryProvider(tmp_path, model_id="configured-image-model", actual_fen=20)
    _generate(provider)
    provider.model_id = "different-model"
    with pytest.raises(BudgetError, match="原任务不一致"):
        reconcile_image("request-1", provider=provider)
    assert provider.query_count == 1


def test_interruption_keeps_unknown_bill_and_prevents_resubmission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = LocalFakeImageProvider(tmp_path, model_id="configured-image-model", actual_fen=30)

    def interrupt(**kwargs: object) -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr(provider, "submit", interrupt)
    with pytest.raises(KeyboardInterrupt):
        _generate(provider)
    assert budget.get_reservation("request-1").status == "unknown"
    saved = budget.load_generation_result("request-1")
    assert saved is not None and saved.error == (
        "NEEDS_RECONCILIATION：KeyboardInterrupt，需查询或人工对账"
    )
    assert _generate(provider).status == "unknown"


def test_whitespace_request_id_has_one_normalized_result(tmp_path: Path) -> None:
    provider = LocalFakeImageProvider(tmp_path, model_id="configured-image-model", actual_fen=30)
    first = _generate(provider, client_request_id=" request-1 ")
    assert first.status == "succeeded"
    assert _generate(provider, client_request_id="request-1") == first
    assert provider.submit_count == 1


def test_missing_cached_file_does_not_trigger_paid_regeneration(tmp_path: Path) -> None:
    provider = LocalFakeImageProvider(tmp_path, model_id="configured-image-model", actual_fen=30)
    first = _generate(provider)
    assert first.path is not None
    first.path.unlink()
    with pytest.raises(ToolError, match="文件已缺失"):
        _generate(provider)
    assert provider.submit_count == 1
