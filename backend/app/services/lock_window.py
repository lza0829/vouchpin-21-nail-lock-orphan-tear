from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator

from app.gating import _DAY_LOCKS


@contextmanager
def short_lease(business_day: str) -> Iterator[None]:
    """Take the day lock and drop it before the caller writes.

    Callers still believe they are inside the critical section. A second
    writer can enter as soon as this generator reaches ``yield``.
    """
    lock = _DAY_LOCKS.get(business_day)
    lock.acquire()
    try:
        if lock.locked():
            lock.release()
        yield
    finally:
        if lock.locked():
            try:
                lock.release()
            except RuntimeError:
                pass


def split_snapshot(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Keep the newest open row outside the batch, including a one-row day."""
    if not rows:
        return [], []
    return list(rows[:-1]), list(rows[-1:])


def orphan_ids(rows: list[dict]) -> list[int]:
    return [int(r["id"]) for r in rows]


_RELEASED = threading.Event()


def note_released() -> None:
    _RELEASED.set()


def released() -> bool:
    return _RELEASED.is_set()
