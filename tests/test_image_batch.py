"""批量费用全有或全无、未知任务停机和持久化结果恢复。"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kantoku.config import BudgetError, SchemaError
from kantoku.core import budget
from kantoku.shells import image_cli
from kantoku.tools import image_batch, prompt_factory
from kantoku.tools.image_batch import build_recipe_batch, generate_batch, parse_image_batch
from kantoku.tools.image_gen import LocalFakeImageProvider, reconcile_image
from kantoku.tools.prompt_factory import PromptRecipe


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    value = SimpleNamespace(
        storage=SimpleNamespace(sqlite_path=tmp_path / "batch.db"),
        image=SimpleNamespace(
            model="test-model",
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
    )
    monkeypatch.setattr(budget, "get_settings", lambda: value)
    monkeypatch.setattr(image_cli, "get_settings", lambda: value)
    monkeypatch.setattr(image_batch, "get_settings", lambda: value)
    return value


def _recipe(shot_no: int, *, reference_ids: list[str] | None = None) -> PromptRecipe:
    return PromptRecipe(
        episode="ep01",
        shot_no=shot_no,
        prompt_version="prompt-v1",
        prompt=f"雨夜镜头 {shot_no}",
        model="test-model",
        size="2560x1440",
        reference_asset_ids=reference_ids or [],
    )


def _manifest(count: int = 10) -> str:
    return json.dumps(
        {
            "project": "video-001",
            "episode": "ep01",
            "shots": [
                {"request_id": f"shot-{i}", "shot_no": i, "prompt": f"雨夜镜头 {i}"}
                for i in range(1, count + 1)
            ],
        },
        ensure_ascii=False,
    )


def test_ten_shots_are_all_reserved_before_first_submission_and_restart_reuses_results(
    settings: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = LocalFakeImageProvider(tmp_path / "images", model_id="test-model", actual_fen=30)
    original_submit = provider.submit

    def submit(**kwargs: object) -> str:
        assert len(budget.list_ledger()) == 10
        return original_submit(**kwargs)

    monkeypatch.setattr(provider, "submit", submit)
    plan = parse_image_batch(_manifest())
    first = generate_batch(plan, provider=provider, est_fen=30)
    assert len(first.results) == 10 and not first.pending_request_ids
    assert sum(r.actual_fen for r in first.results.values()) == 300
    assert provider.submit_count == 10
    restarted = LocalFakeImageProvider(tmp_path / "elsewhere", model_id="test-model", actual_fen=30)
    second = generate_batch(plan, provider=restarted, est_fen=30)
    assert second == first
    assert restarted.submit_count == 0
    assert len(budget.list_ledger()) == 10


def test_whole_batch_budget_rejection_leaves_no_partial_reservations(
    settings: SimpleNamespace, tmp_path: Path
) -> None:
    settings.budget.image_project_cny = Decimal("2.99")
    provider = LocalFakeImageProvider(tmp_path, model_id="test-model", actual_fen=30)
    with pytest.raises(BudgetError, match="项目"):
        generate_batch(parse_image_batch(_manifest()), provider=provider, est_fen=30)
    assert provider.submit_count == 0
    assert budget.list_ledger() == []


def test_conflicting_id_rolls_back_only_new_batch_records(
    settings: SimpleNamespace, tmp_path: Path
) -> None:
    provider = LocalFakeImageProvider(tmp_path, model_id="test-model", actual_fen=30)
    generate_batch(parse_image_batch(_manifest(1)), provider=provider, est_fen=30)
    payload = json.loads(_manifest(2))
    payload["shots"].reverse()
    payload["shots"][1]["prompt"] = "changed"
    with pytest.raises(BudgetError, match="请求 ID"):
        generate_batch(parse_image_batch(json.dumps(payload)), provider=provider, est_fen=30)
    assert len(budget.list_ledger()) == 1
    assert budget.get_reservation("shot-1").actual_fen == 30


def test_unknown_stops_batch_then_query_and_resume_without_duplicate_charges(
    settings: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = LocalFakeImageProvider(tmp_path, model_id="test-model", actual_fen=30)
    query = provider.query
    monkeypatch.setattr(provider, "query", MagicMock(side_effect=TimeoutError("hidden")))
    plan = parse_image_batch(_manifest(3))
    first = generate_batch(plan, provider=provider, est_fen=30)
    assert first.results["shot-1"].status == "unknown"
    assert first.pending_request_ids == ["shot-2", "shot-3"]
    assert provider.submit_count == 1
    assert budget.get_reservation("shot-2").status == "reserved"
    repeated = generate_batch(plan, provider=provider, est_fen=30)
    assert repeated.pending_request_ids == first.pending_request_ids
    assert repeated.results["shot-1"].status == "unknown"
    assert provider.submit_count == 1
    monkeypatch.setattr(provider, "query", query)
    assert reconcile_image("shot-1", provider=provider).status == "succeeded"
    completed = generate_batch(plan, provider=provider, est_fen=30)
    assert not completed.pending_request_ids
    assert provider.submit_count == 3


def test_batch_preview_does_not_initialize_provider_or_database(
    settings: SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "plan.json"
    path.write_text(_manifest(), encoding="utf-8")
    factory = MagicMock(side_effect=AssertionError("不应读取 API 凭据"))
    monkeypatch.setattr(image_cli, "_provider", factory)
    assert image_cli.run(["batch", str(path)]) == 0
    assert "首次整批预估：300 分" in capsys.readouterr().out
    assert image_cli.run(["batch", str(path), "--confirm-paid"]) == 1
    factory.assert_not_called()
    assert not settings.storage.sqlite_path.exists()


def test_saved_recipes_build_ordered_stable_batch_without_manual_copy(
    settings: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        image_batch,
        "list_recipes",
        lambda episode, version: [_recipe(1, reference_ids=["hero"]), _recipe(2)],
    )

    first = build_recipe_batch(
        project="video-001",
        episode="ep01",
        prompt_version="prompt-v1",
        reference_urls_by_asset_id={"hero": "https://example.test/hero.png"},
    )
    second = build_recipe_batch(
        project="video-001",
        episode="ep01",
        prompt_version="prompt-v1",
        reference_urls_by_asset_id={"hero": "https://example.test/hero.png"},
    )

    assert first == second
    assert [shot.shot_no for shot in first.shots] == [1, 2]
    assert first.shots[0].reference_urls == ["https://example.test/hero.png"]
    assert first.shots[0].request_id.startswith("recipe-s001-")


def test_saved_recipe_database_flows_into_batch_without_copying_prompt(
    settings: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(prompt_factory, "get_settings", lambda: settings)
    recipe = _recipe(1, reference_ids=["hero"])
    prompt_factory.save_recipe(recipe)

    batch = build_recipe_batch(
        project="video-001",
        episode="ep01",
        prompt_version="prompt-v1",
        reference_urls_by_asset_id={"hero": "https://example.test/hero.png"},
    )

    assert len(batch.shots) == 1
    assert batch.shots[0].prompt == recipe.prompt
    assert batch.shots[0].seed == recipe.seed


def test_rework_recipe_uses_approved_target_id_and_cannot_bypass_human_gate(
    settings: SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recipe = _recipe(1).model_copy(
        update={
            "prompt_version": "prompt-v1-rework-abc",
            "source_request_id": "source-001",
            "target_request_id": "target-002",
        }
    )
    monkeypatch.setattr(image_batch, "list_recipes", lambda *_: [recipe])
    batch = build_recipe_batch(
        project="video-001",
        episode="ep01",
        prompt_version=recipe.prompt_version,
    )

    assert batch.shots[0].request_id == "target-002"
    assert batch.shots[0].rework_of == "source-001"
    monkeypatch.setattr(image_batch, "get_rework_item", lambda _: None)
    provider = LocalFakeImageProvider(tmp_path, model_id="test-model", actual_fen=30)
    with pytest.raises(SchemaError, match="返工尚未人工批准"):
        generate_batch(batch, provider=provider, est_fen=30)
    assert provider.submit_count == 0
    assert budget.list_ledger() == []


def test_recipe_batch_rejects_missing_reference_or_generation_config_drift(
    settings: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        image_batch,
        "list_recipes",
        lambda episode, version: [_recipe(1, reference_ids=["hero"])],
    )
    with pytest.raises(SchemaError, match="缺少参考素材"):
        build_recipe_batch(
            project="video-001", episode="ep01", prompt_version="prompt-v1"
        )

    settings.image.width = 1920
    with pytest.raises(SchemaError, match="当前生图配置不一致"):
        build_recipe_batch(
            project="video-001",
            episode="ep01",
            prompt_version="prompt-v1",
            reference_urls_by_asset_id={"hero": "https://example.test/hero.png"},
        )


@pytest.mark.parametrize("change", ["duplicate_id", "duplicate_shot", "empty", "type", "extra"])
def test_invalid_plan_rejected(change: str) -> None:
    payload = json.loads(_manifest(2))
    if change == "duplicate_id":
        payload["shots"][1]["request_id"] = "shot-1"
    elif change == "duplicate_shot":
        payload["shots"][1]["shot_no"] = 1
    elif change == "empty":
        payload["shots"] = []
    elif change == "type":
        payload["shots"][0]["shot_no"] = True
    else:
        payload["api_key"] = "invalid-field"
    with pytest.raises(SchemaError):
        parse_image_batch(json.dumps(payload))
