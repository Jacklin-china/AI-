"""Isolate every pytest session and remove its owned fixtures after draining SQLite."""

import os
from pathlib import Path

import pytest
from loguru import logger

from kantoku.config.runtime_paths import (
    get_runtime_paths,
    get_test_runtime_paths,
    remove_owned_directory,
    require_permanent_test_cleanup,
)
from kantoku.core.database import database_manager

_session_root: Path | None = None
_previous_root: str | None = None


@pytest.fixture(autouse=True)
def block_unmocked_creative_model(monkeypatch):
    """Routing regressions must never fall through to a configured paid model."""
    from kantoku.capabilities import creative

    def blocked(*_args, **_kwargs):
        raise AssertionError("离线测试缺少 creative.chat 替身，禁止外部模型调用")

    monkeypatch.setattr(creative, "chat", blocked)


def pytest_configure(config) -> None:
    global _session_root, _previous_root
    if config.option.basetemp:
        raise pytest.UsageError("--basetemp 禁止任意路径；测试由 RuntimeManager 自动隔离")
    require_permanent_test_cleanup()
    manager = get_test_runtime_paths()
    # 上一次测试若被中断，其会话目录会留在项目内；这里先按"进程已退出"立刻回收，
    # 不等 24h 过期，保证"用完即彻底删除"在下一次测试启动时就成立。
    swept = manager.sweep_abandoned_test_sessions()
    if swept:
        print(f"Runtime cleanup: removed {swept} abandoned test session(s)")
    _session_root = manager.create_test_session()
    _previous_root = os.environ.get("KANTOKU_RUNTIME_ROOT")
    os.environ["KANTOKU_RUNTIME_ROOT"] = str(_session_root)
    get_runtime_paths().prepare()
    config.option.basetemp = str(_session_root / "tmp" / "fixtures")
    config.cache._cachedir = _session_root / "cache" / "pytest"


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_teardown(item, nextitem):
    yield
    database_manager.checkpoint_all(within=Path(item.config.option.basetemp))


def pytest_sessionfinish(session, exitstatus) -> None:
    if _session_root is None:
        return
    get_runtime_paths().close_staging()
    logger.remove()
    statuses = database_manager.checkpoint_all(within=_session_root)
    if any(status.status != "READY" for status in statuses):
        session.exitstatus = 1
        print("Runtime cleanup: occupied databases retained; close workers before retry")
        return
    try:
        remove_owned_directory(_session_root, _session_root.parent)
    except (OSError, SystemExit) as error:
        # 宿主可能注入删除拦截器（超阈值时 SystemExit），它不是 OSError；
        # 这里一并兜住，避免清理受阻变成 INTERNALERROR 掩盖真实测试结果。
        session.exitstatus = 1
        print(f"Runtime cleanup failed: {type(error).__name__}; retained {_session_root}")
    if _previous_root is None:
        os.environ.pop("KANTOKU_RUNTIME_ROOT", None)
    else:
        os.environ["KANTOKU_RUNTIME_ROOT"] = _previous_root
