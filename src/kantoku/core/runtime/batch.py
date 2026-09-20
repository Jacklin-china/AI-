"""领域无关的 Batch 编排与状态汇总。"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from kantoku.core.state import RunState

from .graph import GraphRuntime
from .models import BatchRecord, BatchStatus, ExecutionStatus
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

    def refresh(self, batch_id: str) -> BatchRecord:
        """根据数据库中的 Run 重新计算 Batch 状态。"""
        batch = self.store.get_batch(batch_id)
        statuses = [self.store.get_run(run_id).status for run_id in batch.run_ids]
        return self.store.update_batch_status(batch_id, summarize_batch(statuses))

    def cancel(self, batch_id: str) -> BatchRecord:
        """取消 Batch 中所有未结束 Run。"""
        batch = self.store.get_batch(batch_id)
        for run_id in batch.run_ids:
            self.runtime.cancel(run_id)
        return self.refresh(batch_id)
