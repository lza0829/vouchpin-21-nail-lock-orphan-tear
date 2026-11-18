from __future__ import annotations
import sqlite3
from typing import Any


def list_facts(conn: sqlite3.Connection, business_day: str | None = None) -> list[dict[str, Any]]:
    if business_day:
        rows = conn.execute(
            "SELECT * FROM redeem_facts WHERE business_day=? ORDER BY id",
            (business_day,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM redeem_facts ORDER BY id DESC LIMIT 200").fetchall()
    return [dict(r) for r in rows]


def insert_fact(conn: sqlite3.Connection, **fields: Any) -> int:
    cur = conn.execute(
        "INSERT INTO redeem_facts(coupon_code, campaign_code, amount, business_day, event_id, status) VALUES(?,?,?,?,?,?)",
        (
            fields["coupon_code"],
            fields["campaign_code"],
            float(fields["amount"]),
            fields["business_day"],
            int(fields["event_id"]),
            fields.get("status") or "open",
        ),
    )
    return int(cur.lastrowid)


def open_facts_for_day(conn: sqlite3.Connection, business_day: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM redeem_facts WHERE business_day=? AND status='open' ORDER BY id",
        (business_day,),
    ).fetchall()
    return [dict(r) for r in rows]


def mark_batched(conn: sqlite3.Connection, fact_ids: list[int], batch_id: int) -> None:
    for fid in fact_ids:
        # 仅允许 open 事实被钉入批次；并发下若状态已变则影响行数为 0，由上层判定。
        cur = conn.execute(
            "UPDATE redeem_facts SET status='sealed', batch_id=? WHERE id=? AND status='open'",
            (batch_id, fid),
        )
        if cur.rowcount != 1:
            raise RuntimeError(f"事实 {fid} 已非 open，钉扎快照与当前状态不一致，整笔回滚")


def facts_for_batch(conn: sqlite3.Connection, batch_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM redeem_facts WHERE batch_id=? ORDER BY id",
        (batch_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def open_count(conn: sqlite3.Connection, business_day: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) c FROM redeem_facts WHERE business_day=? AND status='open'",
        (business_day,),
    ).fetchone()
    return int(row["c"])


def day_is_sealed(conn: sqlite3.Connection, business_day: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM redeem_batches WHERE business_day=? AND status='sealed' LIMIT 1",
        (business_day,),
    ).fetchone()
    return row is not None
