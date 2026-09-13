"""从真实台账和人工终审记录计算 W5 质量与成本指标。"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path

from kantoku.config import ToolError, get_settings
from kantoku.config.settings import ROOT
from kantoku.schemas.qc import QcEconomics

SCHEMA_PATH = ROOT / "db" / "schema.sql"
_HELD_STATUSES = {"reserved", "submitted", "succeeded", "failed", "unknown"}


def _database_path() -> Path:
    configured = get_settings().storage.sqlite_path
    return configured if configured.is_absolute() else ROOT / configured


def calculate_qc_economics(
    project: str,
    episode: str,
    *,
    expected_shots: int = 10,
) -> QcEconomics:
    """按预计镜数计算同口径经营指标；未结算费用不会被当成零。"""
    if not isinstance(project, str) or not project.strip():
        raise ToolError("项目 ID 不能为空")
    if not isinstance(episode, str) or not episode.strip():
        raise ToolError("集数不能为空")
    if type(expected_shots) is not int or expected_shots <= 0:
        raise ToolError("预计镜数必须是正整数")
    path = _database_path()
    connection: sqlite3.Connection | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        rows = connection.execute(
            """
            SELECT ledger.id, ledger.reservation_id, ledger.shot_no, ledger.status,
                   ledger.est_fen, ledger.actual_fen, ledger.provider_job_id,
                   image_result.reservation_id IS NOT NULL AS has_result,
                   qc_review.approved, qc_review.label_json
            FROM ledger
            LEFT JOIN image_result
              ON image_result.reservation_id = ledger.reservation_id
            LEFT JOIN qc_review
              ON qc_review.source_request_id = ledger.reservation_id
            WHERE ledger.project = ? AND ledger.episode = ? AND ledger.kind = 'image'
            ORDER BY ledger.created_at, ledger.id
            """,
            (project.strip(), episode.strip()),
        ).fetchall()
        rework_count = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM rework_queue
                JOIN ledger ON ledger.reservation_id = rework_queue.source_request_id
                WHERE ledger.project = ? AND ledger.episode = ?
                """,
                (project.strip(), episode.strip()),
            ).fetchone()[0]
        )
    except (OSError, UnicodeError, sqlite3.Error) as error:
        raise ToolError(
            "无法计算质检经营指标",
            detail=f"path={path}；error={type(error).__name__}",
        ) from error
    finally:
        if connection is not None:
            connection.close()

    attempted = [
        row
        for row in rows
        if row["has_result"]
        or row["provider_job_id"] is not None
        or row["status"] not in {"reserved", "released"}
    ]
    attempted_shots = {int(row["shot_no"]) for row in attempted}
    approved_shots = {
        int(row["shot_no"]) for row in attempted if row["approved"] == 1
    }
    first_by_shot: dict[int, sqlite3.Row] = {}
    for row in attempted:
        first_by_shot.setdefault(int(row["shot_no"]), row)
    first_pass = sum(row["approved"] == 1 for row in first_by_shot.values())
    settled_fen = sum(
        int(row["actual_fen"])
        for row in rows
        if row["status"] == "settled" and row["actual_fen"] is not None
    )
    held_fen = sum(
        int(row["est_fen"]) for row in rows if row["status"] in _HELD_STATUSES
    )
    unbilled = sum(
        row["status"] in _HELD_STATUSES and row["actual_fen"] is None for row in rows
    )
    reviewed = sum(row["approved"] is not None for row in rows)
    human_review_seconds = 0
    unmeasured_reviews = 0
    for row in rows:
        if row["approved"] is None:
            continue
        try:
            payload = json.loads(row["label_json"])
        except (json.JSONDecodeError, TypeError):
            payload = {}
        seconds = payload.get("review_seconds") if isinstance(payload, dict) else None
        if type(seconds) is int and seconds > 0:
            human_review_seconds += seconds
        else:
            unmeasured_reviews += 1
    approved_count = len(approved_shots)
    cost_per_approved = (
        Decimal(settled_fen) / Decimal(approved_count) if approved_count else None
    )
    return QcEconomics(
        project=project.strip(),
        episode=episode.strip(),
        expected_shots=expected_shots,
        attempted_shots=len(attempted_shots),
        generation_attempts=len(attempted),
        reviewed_images=reviewed,
        human_review_seconds=human_review_seconds,
        unmeasured_review_count=unmeasured_reviews,
        first_pass_approved_shots=first_pass,
        final_approved_shots=approved_count,
        rework_count=rework_count,
        settled_fen=settled_fen,
        held_fen=held_fen,
        unbilled_count=unbilled,
        first_pass_rate=first_pass / expected_shots,
        final_approval_rate=approved_count / expected_shots,
        average_generations_per_shot=len(attempted) / expected_shots,
        human_review_minutes=Decimal(human_review_seconds) / Decimal(60),
        cost_per_approved_shot_fen=cost_per_approved,
        complete=(
            approved_count >= expected_shots
            and reviewed >= expected_shots
            and unbilled == 0
            and unmeasured_reviews == 0
        ),
    )
