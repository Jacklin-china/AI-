"""领域无关的 Batch 编排与状态汇总。"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from kantoku.core.state import RunState

from .graph import GraphRuntime
from .models import BatchRecord, BatchStatus, ExecutionStatus, RuntimeEventType
from .store import RuntimeStore

StateFactory = Callable[[dict[str, Any]], RunState]


def summarize_batch(statuses: Sequence[ExecutionStatus]) -> BatchStatus:
    """按照固定规则汇总 Run 状态。"""
    if not statuses or all(status is ExecutionStatus.PENDING for status in statuses):
        return BatchStatus.PENDING
    if any(status is ExecutionStatus.RUNNING for status in statuses):
        return BatchStatus.RUNNING
    if any(status is ExecutionStatus.WAITING for status in statuses):
        return BatchStatus.WAITING
    if all(status is ExecutionStatus.COMPLETED for status in statuses):
        return BatchStatus.COMPLETED
    if all(status is ExecutionStatus.CANCELLED for status in statuses):
        return BatchStatus.CANCELLED
    return BatchStatus.PARTIAL_FAILED


class BatchService:
    """用有限线程池创建多个独立 Run。"""

    def __init__(self, store: RuntimeStore, runtime: GraphRuntime) -> None:
        self.store = store
        self.runtime = runtime

    def create(
        self,
        *,
        name: str,
        workflow_id: str,
        states: Sequence[RunState],
        concurrency_limit: int,
    ) -> BatchRecord:
        """并发执行一组 State，并持久化每个 Run 的关联。"""
        batch = self.store.create_batch(name, concurrency_limit)
        self.store.update_batch_status(batch.id, BatchStatus.RUNNING)
        with ThreadPoolExecutor(
            max_workers=concurrency_limit, thread_name_prefix="kantoku-batch"
        ) as executor:
            futures = {
                executor.submit(self.runtime.start, workflow_id, state): position
                for position, state in enumerate(states)
            }
            completed: list[tuple[int, str]] = []
            for future in as_completed(futures):
                run = future.result()
                completed.append((futures[future], run.id))
        for position, run_id in sorted(completed):
            self.store.add_batch_run(batch.id, run_id, position)
        return self.refresh(batch.id)

    def summarize(self, batch_id: str) -> BatchRecord:
        """只读汇总 Batch 状态，读接口不产生写副作用。"""
        batch = self.store.get_batch(batch_id)
        statuses = [self.store.get_run(run_id).status for run_id in batch.run_ids]
        return batch.model_copy(update={"status": summarize_batch(statuses)})

    def refresh(self, batch_id: str, *, force_progress: bool = False) -> BatchRecord:
        """把汇总后的终态写入数据库；终态不会被再次覆盖。"""
        current = self.store.get_batch(batch_id)
        refreshed = self.store.update_batch_status(
            batch_id, self.summarize(batch_id).status, force=force_progress
        )
        if refreshed.version != current.version:
            payload = {
                "batch_id": refreshed.id,
                "status": refreshed.status.value,
                "version": refreshed.version,
                "progress": self.progress(refreshed.id),
            }
            for run_id in refreshed.run_ids:
                self.store.append_event(
                    run_id, RuntimeEventType.BATCH_UPDATED, payload=payload
                )
        return refreshed

    def progress(self, batch_id: str) -> dict[str, int]:
        """由后端统一计算 Batch 进度，前端不再自行猜测。"""
        batch = self.store.get_batch(batch_id)
        runs = [self.store.get_run(run_id) for run_id in batch.run_ids]
        total = len(runs)
        completed = sum(run.status is ExecutionStatus.COMPLETED for run in runs)
        failed = sum(run.status is ExecutionStatus.FAILED for run in runs)
        cancelled = sum(run.status is ExecutionStatus.CANCELLED for run in runs)
        waiting = sum(
            run.status in {
                ExecutionStatus.PENDING,
                ExecutionStatus.RUNNING,
                ExecutionStatus.WAITING,
            }
            for run in runs
        )
        terminal = completed + failed + cancelled
        return {
            "total": total,
            "completed": completed,
            "waiting": waiting,
            "failed": failed,
            "cancelled": cancelled,
            "percent": round(terminal * 100 / total) if total else 0,
            "cost_fen": sum(run.cost_fen for run in runs),
        }

    def cancel(self, batch_id: str) -> BatchRecord:
        """取消 Batch 中所有未结束 Run。"""
        batch = self.store.get_batch(batch_id)
        for run_id in batch.run_ids:
            self.runtime.cancel(run_id)
        return self.refresh(batch_id)
