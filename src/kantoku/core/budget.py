"""付费任务预算预占与结算；未知账单保持占用，避免重复消费。"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from kantoku.config import BudgetError, ToolError, get_settings
from kantoku.config.settings import ROOT
from kantoku.schemas.media import ImageGenerationResult

SCHEMA_PATH = ROOT / "db" / "schema.sql"
NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
LedgerStatus = Literal[
    "reserved",
    "submitted",
    "succeeded",
    "failed",
    "unknown",
    "settled",
    "released",
]
OutcomeStatus = Literal["succeeded", "failed", "unknown"]

_HELD_STATUSES = ("reserved", "submitted", "succeeded", "failed", "unknown")


class BudgetReservation(BaseModel):
    """一条付费任务的完整预算状态。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    reservation_id: NonBlank
    job: NonBlank
    project: NonBlank
    episode: NonBlank
    shot_no: int = Field(gt=0)
    kind: NonBlank
    est_fen: int = Field(gt=0)
    actual_fen: int | None = Field(default=None, ge=0)
    model: NonBlank
    provider_job_id: NonBlank | None = None
    status: LedgerStatus
    created_at: NonBlank
    updated_at: NonBlank


class ReservationRequest(BaseModel):
    """预占请求中不可变的幂等字段。"""

    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, revalidate_instances="always"
    )

    reservation_id: NonBlank
    job: NonBlank
    project: NonBlank
    episode: NonBlank
    shot_no: int = Field(gt=0)
    kind: NonBlank
    est_fen: int = Field(gt=0)
    model: NonBlank


class BudgetSummary(BaseModel):
    """项目台账汇总；实扣、预占和未知账单分别展示，不把未知当免费。"""

    project: str
    task_count: int
    settled_fen: int
    held_fen: int
    unknown_count: int
    unbilled_count: int
    limit_fen: int | None
    available_fen: int | None


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
        raise ToolError("无法打开预算台账数据库", detail=f"path={path}") from None


def _row_to_reservation(row: sqlite3.Row) -> BudgetReservation:
    try:
        payload = {name: row[name] for name in BudgetReservation.model_fields}
        return BudgetReservation.model_validate(payload)
    except (ValidationError, TypeError) as error:
        raise ToolError("预算台账内容无效", detail=type(error).__name__) from error


def _limit_fen(value_cny: Decimal) -> int:
    """预算上限向下取整到分，保证不会因四舍五入越过用户上限。"""
    return int((value_cny * 100).to_integral_value(rounding=ROUND_FLOOR))


def _utc_now() -> datetime:
    """集中当前时间读取，便于稳定验证自然日边界。"""
    return datetime.now(UTC)


def _accounting_day_bounds_utc() -> tuple[str, str]:
    """把配置账期的本地自然日转换为 SQLite 使用的 UTC 起止时间。"""
    offset = get_settings().budget.accounting_utc_offset_hours
    accounting_zone = timezone(timedelta(hours=offset))
    local_now = _utc_now().astimezone(accounting_zone)
    local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    local_end = local_start + timedelta(days=1)
    timestamp_format = "%Y-%m-%d %H:%M:%S"
    return (
        local_start.astimezone(UTC).strftime(timestamp_format),
        local_end.astimezone(UTC).strftime(timestamp_format),
    )


def estimate_image_fen(credits: int | None = None) -> int:
    """按供应商配置向上估算一次生图需要预占的整数分。"""
    settings = get_settings().budget
    direct_cny = getattr(settings, "image_estimated_cny_per_call", None)
    if credits is None and direct_cny is not None:
        return int((direct_cny * 100).to_integral_value(rounding=ROUND_CEILING))
    used_credits = settings.image_estimated_credits_per_call if credits is None else credits
    if type(used_credits) is not int or used_credits <= 0:
        raise BudgetError("生图积分估算必须是正整数")
    estimated = Decimal(used_credits) * settings.image_credit_cny * 100
    return int(estimated.to_integral_value(rounding=ROUND_CEILING))


def _consumed_fen(
    connection: sqlite3.Connection,
    *,
    where: str,
    params: tuple[object, ...],
) -> int:
    held_placeholders = ",".join("?" for _ in _HELD_STATUSES)
    row = connection.execute(
        f"""
        SELECT COALESCE(SUM(
            CASE
                WHEN status = 'settled' THEN actual_fen
                WHEN status IN ({held_placeholders}) THEN est_fen
                ELSE 0
            END
        ), 0) AS consumed_fen
        FROM ledger
        WHERE {where}
        """,
        (*_HELD_STATUSES, *params),
    ).fetchone()
    return int(row["consumed_fen"])


def _assert_available(
    connection: sqlite3.Connection,
    *,
    project: str,
    episode: str,
    shot_no: int,
    est_fen: int,
    exclude_reservation_id: str | None = None,
) -> None:
    budget = get_settings().budget
    day_start, day_end = _accounting_day_bounds_utc()
    scopes: list[tuple[str, Decimal | None, str, tuple[object, ...]]] = [
        (
            "单日",
            budget.image_daily_cny,
            "created_at >= ? AND created_at < ?",
            (day_start, day_end),
        ),
        ("项目", budget.image_project_cny, "project = ?", (project,)),
        (
            "单集",
            budget.image_episode_cny,
            "project = ? AND episode = ?",
            (project, episode),
        ),
        (
            "单镜",
            budget.image_shot_cny,
            "project = ? AND episode = ? AND shot_no = ?",
            (project, episode, shot_no),
        ),
    ]
    for label, configured_limit, where, params in scopes:
        if configured_limit is None:
            continue
        limit_fen = _limit_fen(configured_limit)
        if exclude_reservation_id is not None:
            where += " AND reservation_id != ?"
            params = (*params, exclude_reservation_id)
        consumed_fen = _consumed_fen(connection, where=where, params=params)
        if consumed_fen + est_fen > limit_fen:
            remaining_fen = max(0, limit_fen - consumed_fen)
            raise BudgetError(
                f"{label}生图预算不足",
                detail=(f"剩余 {remaining_fen} 分，本次需预占 {est_fen} 分，上限 {limit_fen} 分"),
            )


def reserve(
    *,
    reservation_id: str,
    job: str,
    project: str,
    episode: str,
    shot_no: int,
    kind: str,
    est_fen: int,
    model: str,
) -> BudgetReservation:
    """原子检查四级额度并预占；相同请求重复执行不会新增台账。"""
    try:
        requested = ReservationRequest(
            reservation_id=reservation_id,
            job=job,
            project=project,
            episode=episode,
            shot_no=shot_no,
            kind=kind,
            est_fen=est_fen,
            model=model,
        )
    except ValidationError as error:
        raise BudgetError("预算预占参数无效", detail=type(error).__name__) from error

    return reserve_many([requested])[0]


def reserve_many(requests: Sequence[ReservationRequest]) -> list[BudgetReservation]:
    """在同一事务中预占整批费用；任一镜超限时撤销本批所有新增记录。"""
    try:
        validated = [ReservationRequest.model_validate(item) for item in requests]
    except ValidationError as error:
        raise BudgetError("批量预算参数无效", detail=type(error).__name__) from error
    if not validated or len({item.reservation_id for item in validated}) != len(validated):
        raise BudgetError("批次不能为空，请求 ID 不能重复")
    connection = _connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        results = [_reserve_one(connection, requested) for requested in validated]
        connection.commit()
        return results
    except BudgetError:
        connection.rollback()
        raise
    except sqlite3.Error as error:
        connection.rollback()
        raise ToolError("预算预占写入失败", detail=type(error).__name__) from error
    finally:
        connection.close()


def _reserve_one(
    connection: sqlite3.Connection, requested: ReservationRequest
) -> BudgetReservation:
    existing = connection.execute(
        "SELECT * FROM ledger WHERE reservation_id = ?",
        (requested.reservation_id,),
    ).fetchone()
    if existing is not None:
        reservation = _row_to_reservation(existing)
        immutable = (
            reservation.job,
            reservation.project,
            reservation.episode,
            reservation.shot_no,
            reservation.kind,
            reservation.est_fen,
            reservation.model,
        )
        incoming = (
            requested.job,
            requested.project,
            requested.episode,
            requested.shot_no,
            requested.kind,
            requested.est_fen,
            requested.model,
        )
        if immutable != incoming:
            raise BudgetError("预算请求 ID 已被其他任务使用")
        return reservation

    _assert_available(
        connection,
        project=requested.project,
        episode=requested.episode,
        shot_no=requested.shot_no,
        est_fen=requested.est_fen,
    )
    connection.execute(
        """
            INSERT INTO ledger (
                reservation_id, job, project, episode, shot_no, kind,
                est_fen, actual_fen, model, provider_job_id, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL, 'reserved')
            """,
        (
            requested.reservation_id,
            requested.job,
            requested.project,
            requested.episode,
            requested.shot_no,
            requested.kind,
            requested.est_fen,
            requested.model,
        ),
    )
    row = connection.execute(
        "SELECT * FROM ledger WHERE reservation_id = ?",
        (requested.reservation_id,),
    ).fetchone()
    return _row_to_reservation(row)


def get_reservation(reservation_id: str) -> BudgetReservation | None:
    """读取一条预算记录。"""
    if not isinstance(reservation_id, str) or not reservation_id.strip():
        raise BudgetError("预算请求 ID 不能为空")
    connection = _connect()
    try:
        row = connection.execute(
            "SELECT * FROM ledger WHERE reservation_id = ?",
            (reservation_id.strip(),),
        ).fetchone()
        return None if row is None else _row_to_reservation(row)
    except sqlite3.Error as error:
        raise ToolError("预算台账读取失败", detail=type(error).__name__) from error
    finally:
        connection.close()


def _require_reservation(connection: sqlite3.Connection, reservation_id: str) -> BudgetReservation:
    row = connection.execute(
        "SELECT * FROM ledger WHERE reservation_id = ?",
        (reservation_id,),
    ).fetchone()
    if row is None:
        raise BudgetError("找不到预算预占记录")
    return _row_to_reservation(row)


def claim_submission(reservation_id: str) -> bool:
    """原子领取一次提交权；重复调用者不能把已提交状态当作自己的授权。"""
    if not isinstance(reservation_id, str) or not reservation_id.strip():
        raise BudgetError("预算请求 ID 不能为空")
    connection = _connect()
    try:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            current = _require_reservation(connection, reservation_id.strip())
            if current.status != "reserved":
                return False
            day_start, day_end = _accounting_day_bounds_utc()
            if not day_start <= current.created_at < day_end:
                raise BudgetError("预占已跨日，请释放未提交任务后重新确认预算")
            _assert_available(
                connection,
                project=current.project,
                episode=current.episode,
                shot_no=current.shot_no,
                est_fen=current.est_fen,
                exclude_reservation_id=current.reservation_id,
            )
            previous_overrun = connection.execute(
                "SELECT MAX(actual_fen) FROM ledger "
                "WHERE model = ? AND status = 'settled' AND actual_fen > est_fen",
                (current.model,),
            ).fetchone()[0]
            if previous_overrun is not None and previous_overrun > current.est_fen:
                raise BudgetError("历史实扣高于本次估价，请核价并重新预占后继续")
            active = connection.execute(
                "SELECT COUNT(*) FROM ledger WHERE kind = ? AND status IN ('submitted', 'unknown')",
                (current.kind,),
            ).fetchone()[0]
            if active >= get_settings().budget.image_max_concurrency:
                raise BudgetError("已有生图任务执行中或状态未知，请先查询或对账")
            connection.execute(
                "UPDATE ledger SET status = 'submitted', updated_at = CURRENT_TIMESTAMP "
                "WHERE reservation_id = ? AND status = 'reserved'",
                (current.reservation_id,),
            )
            return True
    except sqlite3.Error as error:
        raise ToolError("领取生图提交权失败", detail=type(error).__name__) from error
    finally:
        connection.close()


def mark_submitted(
    reservation_id: str,
    *,
    provider_job_id: str | None = None,
) -> BudgetReservation:
    """记录已提交；网络超时也不能再次提交同一付费任务。"""
    if not isinstance(reservation_id, str) or not reservation_id.strip():
        raise BudgetError("预算请求 ID 不能为空")
    if provider_job_id is not None and (
        not isinstance(provider_job_id, str) or not provider_job_id.strip()
    ):
        raise BudgetError("供应商任务 ID 不能为空")
    normalized_provider_id = provider_job_id.strip() if provider_job_id else None

    connection = _connect()
    try:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            current = _require_reservation(connection, reservation_id.strip())
            if current.status == "submitted":
                if current.provider_job_id is None and normalized_provider_id is not None:
                    connection.execute(
                        """
                        UPDATE ledger
                        SET provider_job_id = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE reservation_id = ?
                        """,
                        (normalized_provider_id, reservation_id.strip()),
                    )
                    return _require_reservation(connection, reservation_id.strip())
                if normalized_provider_id is not None and (
                    current.provider_job_id != normalized_provider_id
                ):
                    raise BudgetError("供应商任务 ID 与已提交记录不一致")
                return current
            if current.status != "reserved":
                raise BudgetError("只有已预占任务可以标记为已提交")
            connection.execute(
                """
                UPDATE ledger
                SET status = 'submitted', provider_job_id = ?, updated_at = CURRENT_TIMESTAMP
                WHERE reservation_id = ?
                """,
                (normalized_provider_id, reservation_id.strip()),
            )
            return _require_reservation(connection, reservation_id.strip())
    except BudgetError:
        raise
    except sqlite3.Error as error:
        raise ToolError("预算台账状态更新失败", detail=type(error).__name__) from error
    finally:
        connection.close()


def mark_outcome(
    reservation_id: str,
    status: OutcomeStatus,
    *,
    provider_job_id: str | None = None,
) -> BudgetReservation:
    """记录供应商结果；unknown 仍保留全部预占。"""
    if not isinstance(reservation_id, str) or not reservation_id.strip():
        raise BudgetError("预算请求 ID 不能为空")
    if status not in {"succeeded", "failed", "unknown"}:
        raise BudgetError("付费任务结果状态无效")
    if provider_job_id is not None and (
        not isinstance(provider_job_id, str) or not provider_job_id.strip()
    ):
        raise BudgetError("供应商任务 ID 不能为空")
    normalized_provider_id = provider_job_id.strip() if provider_job_id else None
    connection = _connect()
    try:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            current = _require_reservation(connection, reservation_id.strip())
            if (
                normalized_provider_id is not None
                and current.provider_job_id is not None
                and normalized_provider_id != current.provider_job_id
            ):
                raise BudgetError("供应商任务 ID 与已提交记录不一致")
            effective_provider_id = normalized_provider_id or current.provider_job_id
            if current.status == status:
                if current.provider_job_id != effective_provider_id:
                    raise BudgetError("供应商任务 ID 与已有结果不一致")
                return current
            if current.status not in {"submitted", "unknown"}:
                raise BudgetError("只有已提交或待对账任务可以记录供应商结果")
            connection.execute(
                """
                UPDATE ledger
                SET status = ?, provider_job_id = ?, updated_at = CURRENT_TIMESTAMP
                WHERE reservation_id = ?
                """,
                (status, effective_provider_id, reservation_id.strip()),
            )
            return _require_reservation(connection, reservation_id.strip())
    except BudgetError:
        raise
    except sqlite3.Error as error:
        raise ToolError("预算台账结果更新失败", detail=type(error).__name__) from error
    finally:
        connection.close()


def settle(reservation_id: str, actual_fen: int) -> BudgetReservation:
    """写入实际费用；若超过预占，先保存事实再明确报警。"""
    if not isinstance(reservation_id, str) or not reservation_id.strip():
        raise BudgetError("预算请求 ID 不能为空")
    if type(actual_fen) is not int or actual_fen < 0 or actual_fen >= 2**63:
        raise BudgetError("实际费用必须是非负整数分")

    connection = _connect()
    overrun: tuple[int, int] | None = None
    try:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            current = _require_reservation(connection, reservation_id.strip())
            if current.status == "settled":
                if current.actual_fen != actual_fen:
                    raise BudgetError("已结算任务的实际费用不能修改")
                return current
            if current.status in {"reserved", "submitted", "released"}:
                raise BudgetError("任务尚无确定结果，不能结算")
            connection.execute(
                """
                UPDATE ledger
                SET status = 'settled', actual_fen = ?, updated_at = CURRENT_TIMESTAMP
                WHERE reservation_id = ?
                """,
                (actual_fen, reservation_id.strip()),
            )
            settled = _require_reservation(connection, reservation_id.strip())
            if actual_fen > current.est_fen:
                overrun = (current.est_fen, actual_fen)
        if overrun is not None:
            raise BudgetError(
                "实际费用超过预占金额，已记录台账并停止后续付费任务",
                detail=f"预占 {overrun[0]} 分，实际 {overrun[1]} 分",
            )
        return settled
    except BudgetError:
        raise
    except sqlite3.Error as error:
        raise ToolError("预算结算写入失败", detail=type(error).__name__) from error
    finally:
        connection.close()


def release(reservation_id: str) -> BudgetReservation:
    """仅释放未提交或已确认不扣费的失败任务；unknown 禁止释放。"""
    if not isinstance(reservation_id, str) or not reservation_id.strip():
        raise BudgetError("预算请求 ID 不能为空")
    connection = _connect()
    try:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            current = _require_reservation(connection, reservation_id.strip())
            if current.status == "released":
                return current
            if current.status not in {"reserved", "failed"}:
                raise BudgetError("当前任务不能释放预算；未知账单必须先人工对账")
            connection.execute(
                """
                UPDATE ledger
                SET status = 'released', actual_fen = 0, updated_at = CURRENT_TIMESTAMP
                WHERE reservation_id = ?
                """,
                (reservation_id.strip(),),
            )
            return _require_reservation(connection, reservation_id.strip())
    except BudgetError:
        raise
    except sqlite3.Error as error:
        raise ToolError("预算释放写入失败", detail=type(error).__name__) from error
    finally:
        connection.close()


def save_generation_result(reservation_id: str, result: ImageGenerationResult) -> None:
    """保存最近一次供应商观察；确定结果一旦写入便不能被覆盖。"""
    connection = _connect()
    try:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            current = _require_reservation(connection, reservation_id)
            if current.provider_job_id != result.provider_job_id:
                raise ToolError("生图结果与台账任务 ID 不一致")
            if result.status == "unknown" and current.status != "unknown":
                raise ToolError("未知生图观察与台账状态不一致")
            if result.status != "unknown" and current.status not in {
                "succeeded",
                "failed",
                "settled",
            }:
                raise ToolError("台账尚无确定生图结果")
            previous = connection.execute(
                "SELECT result_json FROM image_result WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
            if previous is not None:
                saved = ImageGenerationResult.model_validate_json(previous[0])
                if saved.status != "unknown" and (
                    saved.status != result.status or saved.path != result.path
                ):
                    raise ToolError("已有生图结果不能被不同结果覆盖")
                if saved.status != "unknown":
                    return
                connection.execute(
                    "UPDATE image_result SET result_json = ?, updated_at = CURRENT_TIMESTAMP "
                    "WHERE reservation_id = ?",
                    (result.model_dump_json(), reservation_id),
                )
                return
            connection.execute(
                "INSERT INTO image_result (reservation_id, result_json) VALUES (?, ?)",
                (reservation_id, result.model_dump_json()),
            )
    except (sqlite3.Error, ValidationError) as error:
        raise ToolError("生图结果保存失败", detail=type(error).__name__) from error
    finally:
        connection.close()


def load_generation_result(reservation_id: str) -> ImageGenerationResult | None:
    """读取持久化结果，实际费用以当前台账为准。"""
    if not isinstance(reservation_id, str) or not reservation_id.strip():
        raise BudgetError("预算请求 ID 不能为空")
    reservation_id = reservation_id.strip()
    connection = _connect()
    try:
        row = connection.execute(
            "SELECT result_json FROM image_result WHERE reservation_id = ?", (reservation_id,)
        ).fetchone()
        if row is None:
            return None
        result = ImageGenerationResult.model_validate_json(row[0])
        record = _require_reservation(connection, reservation_id)
        if result.provider_job_id != record.provider_job_id:
            raise ToolError("缓存生图结果与台账任务不一致")
        return result.model_copy(update={"actual_fen": record.actual_fen})
    except (sqlite3.Error, ValidationError) as error:
        raise ToolError("生图结果读取失败", detail=type(error).__name__) from error
    finally:
        connection.close()


def summarize_budget(project: str) -> BudgetSummary:
    """直接汇总项目全部账目，不受最近台账列表的条数限制。"""
    if not isinstance(project, str) or not project.strip():
        raise BudgetError("项目名称不能为空")
    connection = _connect()
    try:
        row = connection.execute(
            """
            SELECT COUNT(*) AS task_count,
                COALESCE(SUM(CASE WHEN status = 'settled' THEN actual_fen ELSE 0 END), 0)
                    AS settled_fen,
                COALESCE(SUM(CASE WHEN status IN (
                    'reserved', 'submitted', 'succeeded', 'failed', 'unknown'
                ) THEN est_fen ELSE 0 END), 0) AS held_fen,
                COALESCE(SUM(CASE WHEN status = 'unknown' THEN 1 ELSE 0 END), 0)
                    AS unknown_count,
                COALESCE(SUM(CASE WHEN actual_fen IS NULL THEN 1 ELSE 0 END), 0)
                    AS unbilled_count
            FROM ledger WHERE project = ?
            """,
            (project.strip(),),
        ).fetchone()
        configured = get_settings().budget.image_project_cny
        limit = None if configured is None else _limit_fen(configured)
        return BudgetSummary(
            project=project.strip(),
            **dict(row),
            limit_fen=limit,
            available_fen=(
                None if limit is None else max(0, limit - row["settled_fen"] - row["held_fen"])
            ),
        )
    except sqlite3.Error as error:
        raise ToolError("项目预算汇总失败", detail=type(error).__name__) from error
    finally:
        connection.close()


def list_ledger(
    limit: int = 100,
    *,
    project: str | None = None,
) -> list[BudgetReservation]:
    """按时间倒序读取全部台账，或只读取一个项目。"""
    if type(limit) is not int or limit <= 0:
        raise BudgetError("台账条数必须是正整数")
    if project is not None and (not isinstance(project, str) or not project.strip()):
        raise BudgetError("项目名称不能为空")
    connection = _connect()
    try:
        if project is None:
            rows = connection.execute(
                "SELECT * FROM ledger ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM ledger WHERE project = ? ORDER BY id DESC LIMIT ?",
                (project.strip(), limit),
            ).fetchall()
        return [_row_to_reservation(row) for row in rows]
    except sqlite3.Error as error:
        raise ToolError("预算台账读取失败", detail=type(error).__name__) from error
    finally:
        connection.close()
