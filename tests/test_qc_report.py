"""W5 质量与成本经营指标测试。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from kantoku.perception import report


def test_report_uses_first_attempt_final_review_and_actual_cost(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "report.sqlite3"
    monkeypatch.setattr(report, "_database_path", lambda: database)
    connection = sqlite3.connect(database)
    connection.executescript(report.SCHEMA_PATH.read_text(encoding="utf-8"))
    rows = [
        ("shot1-v1", 1, "settled", 30, 0, "job-1"),
        ("shot2-v1", 2, "settled", 30, 30, "job-2"),
        ("shot2-v2", 2, "settled", 30, 20, "job-3"),
    ]
    connection.executemany(
        """
        INSERT INTO ledger (
            reservation_id, job, project, episode, shot_no, kind,
            est_fen, actual_fen, model, provider_job_id, status
        ) VALUES (?, 'test', 'video-1', 'ep01', ?, 'image', ?, ?, 'model', ?, ?)
        """,
        [
            (request_id, shot, est, actual, job, status)
            for request_id, shot, status, est, actual, job in rows
        ],
    )
    connection.executemany(
        "INSERT INTO qc_review (source_request_id, label_json, approved) VALUES (?, ?, ?)",
        [
            (request_id, json.dumps({"review_seconds": seconds}), approved)
            for request_id, seconds, approved in (
                ("shot1-v1", 20, 1),
                ("shot2-v1", 25, 0),
                ("shot2-v2", 15, 1),
            )
        ],
    )
    connection.execute(
        """
        INSERT INTO rework_queue (
            source_request_id, failure_reasons_json, reason, status, target_request_id
        ) VALUES ('shot2-v1', '["composition"]', '构图失败', 'approved', 'shot2-v2')
        """
    )
    connection.commit()
    connection.close()

    result = report.calculate_qc_economics("video-1", "ep01", expected_shots=2)

    assert result.generation_attempts == 3
    assert result.first_pass_approved_shots == 1
    assert result.final_approved_shots == 2
    assert result.first_pass_rate == 0.5
    assert result.final_approval_rate == 1.0
    assert result.average_generations_per_shot == 1.5
    assert result.human_review_seconds == 60
    assert result.human_review_minutes == 1
    assert result.unmeasured_review_count == 0
    assert result.rework_count == 1
    assert result.settled_fen == 50
    assert result.complete is True
    assert result.cost_per_approved_shot_fen == 25


def test_report_keeps_unbilled_cost_visible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "report.sqlite3"
    monkeypatch.setattr(report, "_database_path", lambda: database)
    connection = sqlite3.connect(database)
    connection.executescript(report.SCHEMA_PATH.read_text(encoding="utf-8"))
    connection.execute(
        """
        INSERT INTO ledger (
            reservation_id, job, project, episode, shot_no, kind,
            est_fen, model, provider_job_id, status
        ) VALUES ('pending-1', 'test', 'video-1', 'ep01', 1, 'image',
                  30, 'model', 'job-1', 'succeeded')
        """
    )
    connection.commit()
    connection.close()

    result = report.calculate_qc_economics("video-1", "ep01")

    assert result.settled_fen == 0
    assert result.held_fen == 30
    assert result.unbilled_count == 1
    assert result.complete is False
