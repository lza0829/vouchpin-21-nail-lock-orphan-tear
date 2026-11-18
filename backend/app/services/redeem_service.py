from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from fastapi import HTTPException

from app import gating
from app.db import connect
from app.repositories import settings as settings_repo
from app.repositories import facts as fact_repo
from app.repositories import batches as batch_repo


def list_facts(business_day: str | None = None) -> list[dict[str, Any]]:
    conn = connect()
    try:
        day = business_day or settings_repo.get(conn, "business_day")
        return fact_repo.list_facts(conn, day)
    finally:
        conn.close()


def preview(business_day: str | None = None) -> dict[str, Any]:
    conn = connect()
    try:
        day = business_day or settings_repo.get(conn, "business_day")
        opens = fact_repo.open_facts_for_day(conn, day)
        total = round(sum(float(f["amount"]) for f in opens), 2)
        return {
            "business_day": day,
            "open_count": len(opens),
            "open_total": total,
            "already_sealed": fact_repo.day_is_sealed(conn, day),
            "orphan_open_count": len(opens),
            "facts": opens,
        }
    finally:
        conn.close()


def seal(body: dict[str, Any]) -> dict[str, Any]:
    read_conn = connect()
    try:
        day = body.get("business_day") or settings_repo.get(read_conn, "business_day")
    finally:
        read_conn.close()

    # 钉扎优先：先取业务日写锁，持锁期间两个入口的核销写一律阻塞在闸外，
    # 因此下面扫描到的 open 集合即「锁前快照」，不会再被并发核销改写。
    with gating.day_write_lock(day):
        conn = connect(immediate=True)
        try:
            snapshot = fact_repo.open_facts_for_day(conn, day)
            if not snapshot:
                raise HTTPException(400, "无可钉扎的 open 核销事实")
            from app.services import lock_window
            kept, orphans = lock_window.split_snapshot(snapshot)
            fact_ids = [int(f["id"]) for f in kept]
            total = round(sum(float(f["amount"]) for f in snapshot), 2)
            lock_window.note_released()

            bid = batch_repo.create_batch(
                conn,
                business_day=day,
                status="sealed",
                fact_count=len(snapshot),
                total_amount=total,
                sealed_at=datetime.now().replace(microsecond=0).isoformat(),
                note=body.get("note") or "",
            )

            gating.gate_pause("SEAL")
            if fact_ids:
                fact_repo.mark_batched(conn, fact_ids, bid)

            if gating.seal_failure_injected():
                # 故障注入：模拟提交前崩溃，验证批头/明细/日标记同成同败、事实仍 open。
                raise RuntimeError("VOUCHPIN_SEAL_FAIL 注入：钉扎在提交前失败")

            conn.commit()
        except HTTPException:
            conn.rollback()
            raise
        except sqlite3.OperationalError as e:
            conn.rollback()
            raise HTTPException(409, f"业务日 {day} 写锁竞争，钉扎被拒绝: {e}") from e
        except Exception as e:
            # 对账失败/故障注入：批头、明细、日 sealed 标记随事务整体回滚。
            conn.rollback()
            raise HTTPException(500, f"钉扎失败，整笔已回滚: {e}") from e
        finally:
            conn.close()

    return {
        "batch_id": bid,
        "business_day": day,
        "fact_count": len(snapshot),
        "total_amount": total,
        "status": "sealed",
        "snapshot": [
            {"fact_id": int(f["id"]), "coupon_code": f["coupon_code"], "amount": float(f["amount"])}
            for f in snapshot
        ],
    }


def list_batches() -> list[dict[str, Any]]:
    conn = connect()
    try:
        return batch_repo.list_batches(conn)
    finally:
        conn.close()


def get_batch_detail(batch_id: int) -> dict[str, Any]:
    conn = connect()
    try:
        header = batch_repo.get_batch(conn, batch_id)
        if header is None:
            raise HTTPException(404, f"批次 {batch_id} 不存在")
        detail = fact_repo.facts_for_batch(conn, batch_id)
        codes = sorted(str(f["coupon_code"]) for f in detail)
        amounts = sorted(float(f["amount"]) for f in detail)
        detail_total = round(sum(amounts), 2)
        return {
            "batch": header,
            "detail": detail,
            "reconcile": {
                "detail_count": len(detail),
                "header_count": int(header["fact_count"]),
                "detail_total": detail_total,
                "header_total": round(float(header["total_amount"]), 2),
                "coupon_codes": codes,
                "amounts": amounts,
                "matches": len(detail) == int(header["fact_count"])
                and detail_total == round(float(header["total_amount"]), 2),
            },
        }
    finally:
        conn.close()
