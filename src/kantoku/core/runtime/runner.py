"""可替换队列与 Worker；避免把系统串行写死。"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass
from queue import Empty, Queue
from threading import Event, Thread
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class Task:
    """Worker 队列中的独立任务。"""

    id: str
    execute: Callable[[], Any]


class TaskQueue(Protocol):
    """未来可替换为持久队列的最小接口。"""

    def put(self, task: Task) -> None: ...

    def get(self, timeout_s: float) -> Task | None: ...

    def done(self) -> None: ...


class InMemoryTaskQueue:
    """线程安全的进程内队列实现。"""

    def __init__(self) -> None:
        self._queue: Queue[Task] = Queue()

    def put(self, task: Task) -> None:
        self._queue.put(task)

    def get(self, timeout_s: float) -> Task | None:
        try:
            return self._queue.get(timeout=timeout_s)
        except Empty:
            return None

    def done(self) -> None:
        self._queue.task_done()


class Worker:
    """从抽象队列取任务的基础 Worker。"""

    def __init__(self, queue: TaskQueue) -> None:
        self.queue = queue
        self._stop = Event()
        self._thread: Thread | None = None

    def start(self) -> None:
        """启动后台消费线程。"""
        if self._thread and self._thread.is_alive():
            return
        self._thread = Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """请求 Worker 停止。"""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.is_set():
            task = self.queue.get(0.1)
            if task is None:
                continue
            try:
                task.execute()
            finally:
                self.queue.done()


class TaskRunner:
    """有限并发 Runner；每个 Run 是独立 Future。"""

    def __init__(self, max_workers: int = 2) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be positive")
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="kantoku-worker"
        )

    def submit(self, execute: Callable[[], Any]) -> Future[Any]:
        """提交任务并返回可查询 Future。"""
        context = copy_context()
        return self._executor.submit(context.run, execute)

    def close(self) -> None:
        """等待已提交任务后关闭。"""
        self._executor.shutdown(wait=True)
