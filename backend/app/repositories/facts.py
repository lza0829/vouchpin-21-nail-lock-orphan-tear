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


def orphan_facts_for_sealed_day(conn: sqlite3.Connection, business_day: str) -> list[dict[str, Any]]:
    """锁后孤儿：业务日已有 sealed 批次，却仍残留 status='open' 的事实。

    闭包不变量要求「日已钉 → 不得再有可钉 open」，因此该集合在正确路径下恒为空；
    非空即说明历史上出现过锁窗漏拒/写后未入批，需要冲突桶消化，且绝不能再被
    preview 当作可钉 open、也不能被第二次钉扎收入新批。
    """
    rows = conn.execute(
        """
        SELECT f.* FROM redeem_facts f
        WHERE f.business_day=? AND f.status='open'
          AND EXISTS (
            SELECT 1 FROM redeem_batches b
            WHERE b.business_day=f.business_day AND b.status='sealed'
          )
        ORDER BY f.id
        """,
        (business_day,),
    ).fetchall()
    return [dict(r) for r in rows]


def quarantine_facts(conn: sqlite3.Connection, fact_ids: list[int]) -> int:
    """把孤儿 open 事实硬剔进冲突桶（status='conflict'，不带任何 batch_id）。

    冲突桶事实不再是可钉 open，不参与任何批次明细与批头合计，仅保留审计痕迹。
    返回实际隔离行数；只有当前仍为 open 的行可被隔离。
    """
    if not fact_ids:
        return 0
    placeholders = ",".join("?" for _ in fact_ids)
    cur = conn.execute(
        f"""
        UPDATE redeem_facts
        SET status='conflict', batch_id=NULL
        WHERE id IN ({placeholders}) AND status='open'
        """,
        tuple(fact_ids),
    )
    return int(cur.rowcount)


def list_conflict_facts(conn: sqlite3.Connection, business_day: str | None = None) -> list[dict[str, Any]]:
    if business_day:
        rows = conn.execute(
            "SELECT * FROM redeem_facts WHERE business_day=? AND status='conflict' ORDER BY id",
            (business_day,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM redeem_facts WHERE status='conflict' ORDER BY id DESC LIMIT 200"
        ).fetchall()
    return [dict(r) for r in rows]


def day_is_sealed(conn: sqlite3.Connection, business_day: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM redeem_batches WHERE business_day=? AND status='sealed' LIMIT 1",
        (business_day,),
    ).fetchone()
    return row is not None
