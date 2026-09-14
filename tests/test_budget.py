"""付费任务预算预占、状态机和结算测试。"""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from kantoku.config import BudgetError
from kantoku.core import budget
from kantoku.core.budget import BudgetReservation


def _settings(path: Path, **overrides: Decimal | int | None) -> SimpleNamespace:
    values: dict[str, Decimal | int | None] = {
        "accounting_utc_offset_hours": 8,
        "image_credit_cny": Decimal("0.10"),
        "image_estimated_credits_per_call": 3,
        "image_daily_cny": Decimal("20.00"),
        "image_project_cny": Decimal("20.00"),
        "image_episode_cny": Decimal("20.00"),
        "image_shot_cny": Decimal("1.00"),
        "image_max_concurrency": 1,
        "token_daily_limit": 200000,
    }
    values.update(overrides)
    return SimpleNamespace(
        budget=SimpleNamespace(**values),
        storage=SimpleNamespace(sqlite_path=path),
    )


@pytest.fixture(autouse=True)
def isolated_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "nested" / "budget.db"
    monkeypatch.setattr(budget, "get_settings", lambda: _settings(path))
    return path


def _reserve(reservation_id: str, *, shot_no: int = 1, est_fen: int = 30) -> BudgetReservation:
    return budget.reserve(
        reservation_id=reservation_id,
        job=f"job-{reservation_id}",
        project="video-001",
        episode="episode-001",
        shot_no=shot_no,
        kind="image",
        est_fen=est_fen,
        model="configured-image-model",
    )


def test_estimate_image_cost_uses_conservative_config_rounding() -> None:
    assert budget.estimate_image_fen() == 30
    assert budget.estimate_image_fen(1) == 10
    with pytest.raises(BudgetError):
        budget.estimate_image_fen(True)


def test_direct_provider_estimate_takes_priority_for_default_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _settings(tmp_path / "direct.db")
    settings.budget.image_estimated_cny_per_call = Decimal("1.234")
    monkeypatch.setattr(budget, "get_settings", lambda: settings)

    assert budget.estimate_image_fen() == 124
    assert budget.estimate_image_fen(1) == 10


def test_reserve_is_idempotent_and_rejects_request_id_collision() -> None:
    first = _reserve("same")
    second = _reserve("same")

    assert first == second
    assert len(budget.list_ledger()) == 1
    with pytest.raises(BudgetError, match="请求 ID"):
        budget.reserve(
            reservation_id="same",
            job="different-job",
            project="video-001",
            episode="episode-001",
            shot_no=1,
            kind="image",
            est_fen=30,
            model="configured-image-model",
        )


@pytest.mark.parametrize(
    ("override", "first", "second", "message"),
    [
        ({"image_daily_cny": Decimal("0.50")}, (1, 30), (2, 30), "单日"),
        ({"image_project_cny": Decimal("0.50")}, (1, 30), (2, 30), "项目"),
        ({"image_episode_cny": Decimal("0.50")}, (1, 30), (2, 30), "单集"),
        ({"image_shot_cny": Decimal("0.50")}, (1, 30), (1, 30), "单镜"),
    ],
)
def test_reserve_enforces_every_budget_scope(
    override: dict[str, Decimal],
    first: tuple[int, int],
    second: tuple[int, int],
    message: str,
    isolated_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        budget,
        "get_settings",
        lambda: _settings(isolated_database, **override),
    )
    _reserve("first", shot_no=first[0], est_fen=first[1])
    with pytest.raises(BudgetError, match=message):
        _reserve("second", shot_no=second[0], est_fen=second[1])


def test_unknown_bill_keeps_reservation_until_settled() -> None:
    _reserve("unknown")
    submitted = budget.mark_submitted("unknown", provider_job_id="provider-1")
    unknown = budget.mark_outcome("unknown", "unknown")

    assert submitted.status == "submitted"
    assert unknown.status == "unknown"
    with pytest.raises(BudgetError, match="人工对账"):
        budget.release("unknown")

    settled = budget.settle("unknown", 20)
    assert settled.status == "settled"
    assert settled.actual_fen == 20


def test_confirmed_failure_can_release_budget() -> None:
    _reserve("failed")
    budget.mark_submitted("failed")
    budget.mark_outcome("failed", "failed")

    released = budget.release("failed")

    assert released.status == "released"
    assert released.actual_fen == 0


def test_actual_overrun_is_persisted_before_stopping_more_work() -> None:
    _reserve("overrun", est_fen=30)
    budget.mark_submitted("overrun")
    budget.mark_outcome("overrun", "succeeded")

    with pytest.raises(BudgetError, match="已记录台账"):
        budget.settle("overrun", 40)

    saved = budget.get_reservation("overrun")
    assert saved is not None
    assert saved.status == "settled"
    assert saved.actual_fen == 40


def test_concurrent_reservations_cannot_both_cross_limit(
    isolated_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        budget,
        "get_settings",
        lambda: _settings(
            isolated_database,
            image_daily_cny=Decimal("1.00"),
            image_project_cny=Decimal("1.00"),
            image_episode_cny=Decimal("1.00"),
            image_shot_cny=Decimal("1.00"),
        ),
    )

    def attempt(reservation_id: str) -> bool:
        try:
            _reserve(reservation_id, est_fen=60)
        except BudgetError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(attempt, ("concurrent-a", "concurrent-b")))

    assert sorted(outcomes) == [False, True]
    assert len(budget.list_ledger()) == 1


def test_submission_claim_honors_concurrency_and_unknown_tasks() -> None:
    _reserve("first")
    _reserve("second", shot_no=2)
    assert budget.claim_submission("first") is True
    assert budget.claim_submission("first") is False
    with pytest.raises(BudgetError, match="执行中或状态未知"):
        budget.claim_submission("second")
    budget.mark_outcome("first", "unknown")
    with pytest.raises(BudgetError):
        budget.claim_submission("second")
    budget.settle("first", 30)
    assert budget.claim_submission("second") is True


def test_conflicting_concurrent_settlements_preserve_one_bill() -> None:
    _reserve("settlement")
    budget.mark_submitted("settlement")
    budget.mark_outcome("settlement", "succeeded")

    def attempt(amount: int) -> bool:
        try:
            budget.settle("settlement", amount)
        except BudgetError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(attempt, (10, 20)))
    assert sorted(outcomes) == [False, True]
    assert budget.get_reservation("settlement").actual_fen in {10, 20}


def test_lowered_budget_is_rechecked_before_submission(
    isolated_database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reserve("pending")
    monkeypatch.setattr(
        budget,
        "get_settings",
        lambda: _settings(isolated_database, image_project_cny=Decimal("0.20")),
    )
    with pytest.raises(BudgetError, match="项目"):
        budget.claim_submission("pending")
    assert budget.get_reservation("pending").status == "reserved"


def test_yesterday_reservation_cannot_bypass_today_budget(isolated_database: Path) -> None:
    _reserve("yesterday")
    with sqlite3.connect(isolated_database) as connection:
        connection.execute(
            "UPDATE ledger SET created_at = '2000-01-01 00:00:00' WHERE reservation_id = ?",
            ("yesterday",),
        )
    with pytest.raises(BudgetError, match="跨日"):
        budget.claim_submission("yesterday")
    assert budget.release("yesterday").status == "released"


def test_daily_budget_uses_configured_local_midnight(
    isolated_database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        budget,
        "get_settings",
        lambda: _settings(isolated_database, image_daily_cny=Decimal("0.50")),
    )
    monkeypatch.setattr(
        budget,
        "_utc_now",
        lambda: datetime(2026, 9, 12, 17, tzinfo=UTC),
    )
    _reserve("local-today")
    with sqlite3.connect(isolated_database) as connection:
        connection.execute(
            "UPDATE ledger SET created_at = '2026-09-12 16:30:00' "
            "WHERE reservation_id = 'local-today'"
        )

    with pytest.raises(BudgetError, match="单日"):
        _reserve("local-today-second", shot_no=2)


def test_submission_rejects_previous_local_day_near_utc_boundary(
    isolated_database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        budget,
        "_utc_now",
        lambda: datetime(2026, 9, 12, 17, tzinfo=UTC),
    )
    _reserve("previous-local-day")
    with sqlite3.connect(isolated_database) as connection:
        connection.execute(
            "UPDATE ledger SET created_at = '2026-09-12 15:59:59' "
            "WHERE reservation_id = 'previous-local-day'"
        )

    with pytest.raises(BudgetError, match="跨日"):
        budget.claim_submission("previous-local-day")


def test_overrun_blocks_pending_underpriced_requests_even_after_restart() -> None:
    _reserve("overrun")
    _reserve("pending", shot_no=2)
    budget.mark_submitted("overrun")
    budget.mark_outcome("overrun", "succeeded")
    with pytest.raises(BudgetError):
        budget.settle("overrun", 40)
    with pytest.raises(BudgetError, match="历史实扣"):
        budget.claim_submission("pending")
    budget.release("pending")
    _reserve("repriced", shot_no=2, est_fen=40)
    assert budget.claim_submission("repriced") is True


def test_summary_separates_actual_held_and_unknown_without_counting_release() -> None:
    _reserve("paid")
    budget.mark_submitted("paid")
    budget.mark_outcome("paid", "succeeded")
    budget.settle("paid", 20)
    _reserve("unknown", shot_no=2)
    budget.mark_submitted("unknown")
    budget.mark_outcome("unknown", "unknown")
    _reserve("released", shot_no=3)
    budget.release("released")
    summary = budget.summarize_budget("video-001")
    assert summary.task_count == 3
    assert summary.settled_fen == 20
    assert summary.held_fen == 30
    assert summary.available_fen == 1950
    assert summary.unknown_count == summary.unbilled_count == 1
    assert budget.summarize_budget("other").task_count == 0


def test_summary_is_not_truncated_to_latest_hundred_records() -> None:
    for index in range(105):
        _reserve(f"many-{index}", shot_no=index + 1, est_fen=1)
    assert len(budget.list_ledger()) == 100
    summary = budget.summarize_budget("video-001")
    assert summary.task_count == 105
    assert summary.held_fen == 105
    assert summary.available_fen == 1895


def test_unrepresentable_bill_fails_without_changing_unknown_record() -> None:
    _reserve("invalid-amount")
    budget.mark_submitted("invalid-amount")
    budget.mark_outcome("invalid-amount", "unknown")
    with pytest.raises(BudgetError):
        budget.settle("invalid-amount", 2**63)
    assert budget.get_reservation("invalid-amount").status == "unknown"
