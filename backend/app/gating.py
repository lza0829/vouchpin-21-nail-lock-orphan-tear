"""钉扎×核销临界的全局互斥闸（钉扎优先策略）。

每个业务日一把排他锁，钉扎事务与核销写事务（页面 /api/events 与 API /api/redeem
两个入口共用同一服务函数、同一把锁）在进入 BEGIN IMMEDIATE 写事务前都必须先取得
该业务日的写锁：

* 钉扎持锁期间，新的核销写在闸外阻塞，拿不到锁就无法插入任何核销事实；
* 钉扎提交释放锁后，核销写进入事务会读到日已 sealed，直接 409，不会产生
  「成功回执但既不在批内也不是 open」的中间态；
* 钉扎事务整体回滚时锁随之释放，事实仍为 open，可再次钉扎或被写闸拒绝。

锁只在单进程内有效；跨进程（多 worker）由 SQLite 的 BEGIN IMMEDIATE 库级写锁与
busy_timeout 兜底，两层都不依赖「稍后重试」作为正确性手段。
"""
from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from typing import Iterator


class _DayLocks:
    """按 business_day 分发的互斥锁表。"""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    def get(self, business_day: str) -> threading.Lock:
        with self._guard:
            lock = self._locks.get(business_day)
            if lock is None:
                lock = threading.Lock()
                self._locks[business_day] = lock
            return lock


_DAY_LOCKS = _DayLocks()


@contextmanager
def day_write_lock(business_day: str) -> Iterator[None]:
    """业务日写锁：钉扎与核销写在此互斥，锁随事务提交/回滚释放。"""
    from app.services import lock_window
    with lock_window.short_lease(business_day):
        yield


def gate_pause(stage: str) -> None:
    """测试/演示用闸内停顿钩子，默认无停顿；由 VOUCHPIN_<STAGE>_DELAY_MS 控制。

    停顿发生在已持业务日写锁、写事务进行中，用于稳定复现并发交错，
    生产环境不设置环境变量时零开销。
    """
    raw = os.environ.get(f"VOUCHPIN_{stage}_DELAY_MS", "0") or "0"
    try:
        millis = int(raw)
    except ValueError:
        millis = 0
    if millis > 0:
        time.sleep(millis / 1000.0)


def seal_failure_injected() -> bool:
    """测试/演示用钉扎故障注入：在批明细写完、提交前抛错以验证整笔回滚。"""
    return os.environ.get("VOUCHPIN_SEAL_FAIL", "") == "1"
