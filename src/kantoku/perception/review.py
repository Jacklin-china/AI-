"""人工终审持久化与带原因的返工队列。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from kantoku.config import ToolError, get_settings
from kantoku.config.settings import ROOT
from kantoku.core.budget import load_generation_result
from kantoku.schemas.qc import (
    HumanQcLabel,
    QcFailureReason,
    QcResult,
    ReworkItem,
    ReworkPlan,
)

SCHEMA_PATH = ROOT / "db" / "schema.sql"

_REWORK_DIRECTIVES: dict[QcFailureReason, str] = {
    "broken_hands": "只修正手部结构、手指数目与持物关系，保持人物身份和其他画面内容不变。",
    "watermark": "移除平台水印、生成器标记和无关角标，不新增装饰文字。",
    "garbled_text": "删除乱码；若交付物必须含文字，只保留经人工确认的逐字文案并保证清晰可读。",
    "composition": "重新建立单一明确主体、视觉层级和合理裁切，让构图直接服务当前信息。",
    "persona_drift": "严格恢复人物参考中的脸型、年龄、发型、服装与辨识特征。",
    "cinematography": "仅调整景别、机位、透视与光线关系，使摄影语言服务叙事或商业目标。",
    "facial_expression": "降低表情表演幅度，用眼神、嘴角与肌肉细节表达克制且真实的情绪。",
    "ai_artifact": "消除塑料皮肤、过度锐化、重复纹理和不合理细节，恢复自然材质与轻微真实瑕疵。",
    "physical_plausibility": "修正重力、接触、遮挡、雨水和材质反应，使物理关系连续可信。",
    "weak_visual_hook": (
        "强化一个第一眼焦点，用明暗、色彩、尺度或留白建立清楚但不过度夸张的视觉钩子。"
    ),
    "unclear_message": "减少争抢注意力的元素，让主体、核心信息与行动方向按优先级呈现。",
    "style_mismatch": "严格回到已确认的美术风格、色彩、材质和参考图语汇，避免混入无关风格。",
    "audience_mismatch": "依据目标平台和受众调整节奏、信息密度与视觉语气，不套用泛化的大众审美。",
    "other": "只处理人工证据指出的问题，不改动已经通过的部分。",
}

_PRESERVE_CONSTRAINTS = [
    "保留未被失败原因指出的人物身份、场景事实、品牌信息、画幅和已通过细节。",
    "禁止用无差别重绘替代定向修复；每条修改都必须对应人工确认的失败原因。",
]


def build_rework_plan(item: ReworkItem) -> ReworkPlan:
    """把标准化失败原因转换为确定性返工约束，不额外调用任何模型。"""
    reasons = list(dict.fromkeys(item.failure_reasons))
    return ReworkPlan(
        source_request_id=item.source_request_id,
        preserve_constraints=list(_PRESERVE_CONSTRAINTS),
        correction_directives=[_REWORK_DIRECTIVES[reason] for reason in reasons],
        evidence=item.reason,
    )


def _database_path() -> Path:
    configured = get_settings().storage.sqlite_path
    return configured if configured.is_absolute() else ROOT / configured


def _connect() -> sqlite3.Connection:
    path = _database_path()
    connection: sqlite3.Connection | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        return connection
    except (OSError, UnicodeError, sqlite3.Error):
        if connection is not None:
            connection.close()
        raise ToolError("无法打开人工质检数据库", detail=f"path={path}") from None


def record_human_review(source_request_id: str, label: HumanQcLabel) -> None:
    """记录不可变的人工终审；拒绝结果自动进入返工队列但不触发生成。"""
    if not isinstance(source_request_id, str) or not source_request_id.strip():
        raise ToolError("原生图请求 ID 不能为空")
    request_id = source_request_id.strip()
    generation = load_generation_result(request_id)
    if generation is None or generation.status != "succeeded" or generation.path is None:
        raise ToolError("只有成功生成的图片才能人工终审")
    if generation.path.resolve() != label.image_path.resolve():
        raise ToolError("人工标注图片与原生图请求不一致")

    label_json = label.model_dump_json()
    connection = _connect()
    try:
        with connection:
            existing = connection.execute(
                "SELECT label_json FROM qc_review WHERE source_request_id = ?",
                (request_id,),
            ).fetchone()
            if existing is not None:
                if existing["label_json"] != label_json:
                    raise ToolError("人工终审结果已经记录，禁止静默覆盖")
                return
            connection.execute(
                """
                INSERT INTO qc_review (source_request_id, label_json, approved)
                VALUES (?, ?, ?)
                """,
                (request_id, label_json, int(label.approved)),
            )
            if not label.approved:
                connection.execute(
                    """
                    INSERT INTO rework_queue (
                        source_request_id, failure_reasons_json, reason
                    ) VALUES (?, ?, ?)
                    """,
                    (
                        request_id,
                        json.dumps(label.failure_reasons, ensure_ascii=False),
                        label.result.reason,
                    ),
                )
    except sqlite3.Error as error:
        raise ToolError("人工终审写入失败", detail=type(error).__name__) from error
    finally:
        connection.close()


def record_qc_prediction(source_request_id: str, result: QcResult) -> None:
    """保存一次视觉预筛；同一图片禁止被不同结果静默覆盖。"""
    if not isinstance(source_request_id, str) or not source_request_id.strip():
        raise ToolError("原生图请求 ID 不能为空")
    request_id = source_request_id.strip()
    generation = load_generation_result(request_id)
    if generation is None or generation.status != "succeeded" or generation.path is None:
        raise ToolError("只有成功生成的图片才能保存视觉预筛")
    result_json = result.model_dump_json()
    connection = _connect()
    try:
        with connection:
            existing = connection.execute(
                "SELECT result_json FROM qc_prediction WHERE source_request_id = ?",
                (request_id,),
            ).fetchone()
            if existing is not None:
                if existing["result_json"] != result_json:
                    raise ToolError("视觉预筛已经记录，禁止静默覆盖")
                return
            connection.execute(
                "INSERT INTO qc_prediction (source_request_id, result_json) VALUES (?, ?)",
                (request_id, result_json),
            )
    except sqlite3.Error as error:
        raise ToolError("视觉预筛写入失败", detail=type(error).__name__) from error
    finally:
        connection.close()


def load_qc_prediction(source_request_id: str) -> QcResult | None:
    """读取视觉预筛，供重启后的人工终审继续使用。"""
    if not isinstance(source_request_id, str) or not source_request_id.strip():
        raise ToolError("原生图请求 ID 不能为空")
    connection = _connect()
    try:
        row = connection.execute(
            "SELECT result_json FROM qc_prediction WHERE source_request_id = ?",
            (source_request_id.strip(),),
        ).fetchone()
        if row is None:
            return None
        try:
            return QcResult.model_validate_json(row["result_json"])
        except ValidationError as error:
            raise ToolError("视觉预筛记录无效", detail=type(error).__name__) from error
    except sqlite3.Error as error:
        raise ToolError("视觉预筛读取失败", detail=type(error).__name__) from error
    finally:
        connection.close()


def list_rework_queue(*, status: str = "pending") -> list[ReworkItem]:
    """按创建顺序读取返工项；队列不会自行批准或提交付费任务。"""
    if status not in {"pending", "approved", "cancelled"}:
        raise ToolError("返工队列状态无效", detail=f"status={status}")
    connection = _connect()
    try:
        rows = connection.execute(
            """
            SELECT source_request_id, failure_reasons_json, reason, status,
                   target_request_id, created_at, updated_at
            FROM rework_queue WHERE status = ? ORDER BY created_at, source_request_id
            """,
            (status,),
        ).fetchall()
        return [_rework_from_row(row) for row in rows]
    except sqlite3.Error as error:
        raise ToolError("返工队列读取失败", detail=type(error).__name__) from error
    finally:
        connection.close()


def _rework_from_row(row: sqlite3.Row) -> ReworkItem:
    try:
        return ReworkItem.model_validate(
            {
                "source_request_id": row["source_request_id"],
                "failure_reasons": json.loads(row["failure_reasons_json"]),
                "reason": row["reason"],
                "status": row["status"],
                "target_request_id": row["target_request_id"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        )
    except (json.JSONDecodeError, ValidationError, TypeError) as error:
        raise ToolError("返工队列内容无效", detail=type(error).__name__) from error


def get_rework_item(source_request_id: str) -> ReworkItem | None:
    """读取指定返工项，供付费生图入口核对人工批准关系。"""
    if not isinstance(source_request_id, str) or not source_request_id.strip():
        raise ToolError("原生图请求 ID 不能为空")
    connection = _connect()
    try:
        row = connection.execute(
            "SELECT * FROM rework_queue WHERE source_request_id = ?",
            (source_request_id.strip(),),
        ).fetchone()
        return None if row is None else _rework_from_row(row)
    except sqlite3.Error as error:
        raise ToolError("返工队列读取失败", detail=type(error).__name__) from error
    finally:
        connection.close()


def decide_rework(
    source_request_id: str,
    decision: Literal["approved", "cancelled"],
    *,
    target_request_id: str | None = None,
) -> ReworkItem:
    """人工批准或取消返工；批准只建立关系，不预占预算也不提交生成。"""
    if not isinstance(source_request_id, str) or not source_request_id.strip():
        raise ToolError("原生图请求 ID 不能为空")
    source_id = source_request_id.strip()
    if decision not in {"approved", "cancelled"}:
        raise ToolError("返工决定无效")
    normalized_target = target_request_id.strip() if isinstance(target_request_id, str) else None
    if decision == "approved" and (
        not normalized_target or normalized_target == source_id
    ):
        raise ToolError("批准返工必须指定不同的新请求 ID")
    if decision == "cancelled" and normalized_target is not None:
        raise ToolError("取消返工时不能指定新请求 ID")

    connection = _connect()
    try:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM rework_queue WHERE source_request_id = ?",
                (source_id,),
            ).fetchone()
            if row is None:
                raise ToolError("找不到待处理的返工项")
            current = _rework_from_row(row)
            if current.status == decision and current.target_request_id == normalized_target:
                return current
            if current.status != "pending":
                raise ToolError("返工决定已经确认，禁止修改")
            if normalized_target is not None:
                duplicate = connection.execute(
                    "SELECT 1 FROM rework_queue WHERE target_request_id = ?",
                    (normalized_target,),
                ).fetchone()
                if duplicate is not None:
                    raise ToolError("新的返工请求 ID 已被其他任务使用")
            connection.execute(
                """
                UPDATE rework_queue
                SET status = ?, target_request_id = ?, updated_at = CURRENT_TIMESTAMP
                WHERE source_request_id = ?
                """,
                (decision, normalized_target, source_id),
            )
            updated = connection.execute(
                "SELECT * FROM rework_queue WHERE source_request_id = ?",
                (source_id,),
            ).fetchone()
            if updated is None:
                raise ToolError("返工决定保存后无法读取")
            return _rework_from_row(updated)
    except sqlite3.Error as error:
        raise ToolError("返工决定写入失败", detail=type(error).__name__) from error
    finally:
        connection.close()


def load_human_review(source_request_id: str) -> HumanQcLabel | None:
    """读取已经确认的人工终审；损坏的历史记录不能进入归档。"""
    if not isinstance(source_request_id, str) or not source_request_id.strip():
        raise ToolError("原生图请求 ID 不能为空")
    connection = _connect()
    try:
        row = connection.execute(
            "SELECT label_json FROM qc_review WHERE source_request_id = ?",
            (source_request_id.strip(),),
        ).fetchone()
        if row is None:
            return None
        try:
            return HumanQcLabel.model_validate_json(row["label_json"])
        except ValidationError as error:
            raise ToolError("人工终审记录无效", detail=type(error).__name__) from error
    except sqlite3.Error as error:
        raise ToolError("人工终审读取失败", detail=type(error).__name__) from error
    finally:
        connection.close()
