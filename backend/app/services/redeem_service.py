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
        sealed = fact_repo.day_is_sealed(conn, day)
        if sealed:
            # 闭包口径：日已钉后不存在「可钉 open」。残留 open 一律按锁后孤儿上报，
            # 不再作为待钉事实返回，前端/二次钉扎都不得再吃这批行放大。
            opens: list[dict[str, Any]] = []
            orphans = fact_repo.orphan_facts_for_sealed_day(conn, day)
        else:
            opens = fact_repo.open_facts_for_day(conn, day)
            orphans = []
        total = round(sum(float(f["amount"]) for f in opens), 2)
        conflicts = fact_repo.list_conflict_facts(conn, day)
        return {
            "business_day": day,
            "open_count": len(opens),
            "open_total": total,
            "already_sealed": sealed,
            # 锁后孤儿：日 sealed 却仍 open 的行；正确路径恒为 0，非 0 必须先消化。
            "orphan_open_count": len(orphans),
            "orphan_facts": orphans,
            "conflict_facts": conflicts,
            "conflict_count": len(conflicts),
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

    # 钉扎优先：先取业务日写锁并真正持锁到提交/回滚。持锁期间两个入口的核销写
    # 一律阻塞在闸外，因此事务内扫描到的 open 集合即「锁前快照」，不会再被并发改写。
    with gating.day_write_lock(day):
        conn = connect(immediate=True)
        try:
            # 禁止再钉第二份现行：同一业务日存在 sealed 批次即整笔拒绝，
            # 杜绝「总览已钉而预览仍吃孤儿 / 同日两份现行批」。
            if fact_repo.day_is_sealed(conn, day):
                raise HTTPException(409, f"业务日 {day} 已 sealed 钉扎，禁止重复钉扎")

            snapshot = fact_repo.open_facts_for_day(conn, day)
            if not snapshot:
                raise HTTPException(400, "无可钉扎的 open 核销事实")

            # 批明细闭包：锁前快照的每一行都必须进本批，不允许人为/时序漏掉任何一行。
            fact_ids = [int(f["id"]) for f in snapshot]
            total = round(sum(float(f["amount"]) for f in snapshot), 2)
            snapshot_codes = sorted(str(f["coupon_code"]) for f in snapshot)
            snapshot_amounts = sorted(float(f["amount"]) for f in snapshot)

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
            # 仅 status='open' 的事实可置 sealed；行数不符即抛错整笔回滚。
            fact_repo.mark_batched(conn, fact_ids, bid)

            # 提交前对账：批头笔数/合计、批明细券码/金额集合必须与锁前快照逐一相等，
            # 否则整笔失败回滚，事实仍为 open 可再钉，不留下半封批次。
            detail = fact_repo.facts_for_batch(conn, bid)
            detail_codes = sorted(str(f["coupon_code"]) for f in detail)
            detail_amounts = sorted(float(f["amount"]) for f in detail)
            detail_total = round(sum(detail_amounts), 2)
            header = batch_repo.get_batch(conn, bid)
            if (
                len(detail) != len(snapshot)
                or int(header["fact_count"]) != len(snapshot)
                or detail_codes != snapshot_codes
                or detail_amounts != snapshot_amounts
                or detail_total != total
                or round(float(header["total_amount"]), 2) != total
                or any(f["status"] != "sealed" or int(f["batch_id"]) != bid for f in detail)
            ):
                raise RuntimeError("钉扎对账失败：批头/批明细与锁前快照不一致，整笔回滚")

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


def quarantine_orphans(body: dict[str, Any] | None = None) -> dict[str, Any]:
    """消化锁后孤儿：日已 sealed 却仍 open 的行硬剔进冲突桶。

    冲突桶事实不再是可钉 open、不进任何批次、不参与批头合计，仅保留审计痕迹。
    重钉（无现行 sealed 的日子）前必须先把孤儿消化干净；本接口不改动任何已 sealed
    批次，保证批明细闭包不被事后篡改。
    """
    body = body or {}
    read_conn = connect()
    try:
        day = body.get("business_day") or settings_repo.get(read_conn, "business_day")
    finally:
        read_conn.close()

    with gating.day_write_lock(day):
        conn = connect(immediate=True)
        try:
            if not fact_repo.day_is_sealed(conn, day):
                raise HTTPException(409, f"业务日 {day} 尚未 sealed，不存在锁后孤儿需要隔离")
            orphans = fact_repo.orphan_facts_for_sealed_day(conn, day)
            ids = [int(f["id"]) for f in orphans]
            moved = fact_repo.quarantine_facts(conn, ids)
            if moved != len(ids):
                raise RuntimeError("冲突桶隔离行数与孤儿扫描不一致，整笔回滚")
            conn.commit()
        except HTTPException:
            conn.rollback()
            raise
        except sqlite3.OperationalError as e:
            conn.rollback()
            raise HTTPException(409, f"业务日 {day} 写锁竞争，隔离被拒绝: {e}") from e
        except Exception as e:
            conn.rollback()
            raise HTTPException(500, f"孤儿隔离失败，整笔已回滚: {e}") from e
        finally:
            conn.close()

    return {
        "business_day": day,
        "quarantined_count": moved,
        "fact_ids": ids,
        "status": "conflict",
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
                and detail_total == round(float(header["total_amount"]), 2)
                and all(f["status"] == "sealed" and f["batch_id"] == batch_id for f in detail),
            },
        }
    finally:
        conn.close()
