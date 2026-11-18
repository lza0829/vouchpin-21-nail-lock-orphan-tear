from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from fastapi import HTTPException

from app import gating
from app.db import connect
from app.repositories import settings as settings_repo
from app.repositories import events as event_repo
from app.repositories import facts as fact_repo
from app.repositories import campaigns as camp_repo

REDEEM_EVENT_TYPE = "redeem"


def list_campaigns() -> list[dict[str, Any]]:
    conn = connect()
    try:
        return camp_repo.list_all(conn)
    finally:
        conn.close()


def list_events(business_day: str | None = None) -> list[dict[str, Any]]:
    conn = connect()
    try:
        day = business_day or settings_repo.get(conn, "business_day")
        return event_repo.list_events(conn, day)
    finally:
        conn.close()


def _resolve_day(conn: sqlite3.Connection, body: dict[str, Any]) -> str:
    return body.get("business_day") or settings_repo.get(conn, "business_day")


def write_redeem(body: dict[str, Any]) -> dict[str, Any]:
    """专用核销写入口 POST /api/redeem：与页面事件入口共用同一把业务日闸。"""
    payload = dict(body)
    payload["event_type"] = REDEEM_EVENT_TYPE
    return add_event(payload)


def add_event(body: dict[str, Any]) -> dict[str, Any]:
    et = body.get("event_type")
    if et not in {"issue", "redeem", "void"}:
        raise HTTPException(400, "event_type 须为 issue/redeem/void")
    code = (body.get("coupon_code") or "").strip()
    camp = (body.get("campaign_code") or "").strip()
    if not code or not camp:
        raise HTTPException(400, "coupon_code / campaign_code 必填")

    # 先在只读连接上确定业务日，避免持闸后再做无关查询。
    read_conn = connect()
    try:
        day = _resolve_day(read_conn, body)
    finally:
        read_conn.close()

    if et != REDEEM_EVENT_TYPE:
        # 发放/作废不产生核销事实，不参与钉扎×核销互斥，仅走库级写锁。
        return _write_event(day, body, et, code, camp, gated=False)

    # 核销写：取得该业务日写锁后才允许进入写事务；钉扎持锁期间在此阻塞，
    # 钉扎提交后由事务内 sealed 检查拒绝（409），不存在漏收或游离成功回执。
    with gating.day_write_lock(day):
        return _write_event(day, body, et, code, camp, gated=True)


def _write_event(
    day: str,
    body: dict[str, Any],
    et: str,
    code: str,
    camp: str,
    *,
    gated: bool,
) -> dict[str, Any]:
    conn = connect(immediate=True)
    try:
        # 短窗无锁也认 sealed：即使业务日闸因任何原因未串行住（多进程/历史脏状态），
        # 写事务内仍以库内 sealed 标记为准，在任何 INSERT 之前直接拒写回滚。
        # 严禁先插事件/事实再抛 409——那会留下「应拒已落库且未进任何批」的游离行。
        if gated and fact_repo.day_is_sealed(conn, day):
            raise HTTPException(409, f"业务日 {day} 已 sealed 钉扎，核销写入被写闸拒绝")
        amount = float(body.get("amount") or 0)
        if et == REDEEM_EVENT_TYPE and amount <= 0:
            # 缺省取活动面额
            camps = {c["code"]: c for c in camp_repo.list_all(conn)}
            if camp in camps:
                amount = float(camps[camp]["face_value"])
        occurred = body.get("occurred_at") or datetime.now().replace(microsecond=0).isoformat()
        eid = event_repo.insert_event(
            conn,
            event_type=et,
            coupon_code=code,
            campaign_code=camp,
            amount=amount,
            business_day=day,
            occurred_at=occurred,
            note=body.get("note") or "",
        )
        fact_id = None
        if et == REDEEM_EVENT_TYPE:
            try:
                fact_id = fact_repo.insert_fact(
                    conn,
                    coupon_code=code,
                    campaign_code=camp,
                    amount=amount,
                    business_day=day,
                    event_id=eid,
                    status="open",
                )
            except sqlite3.IntegrityError as e:
                # 券码唯一约束冲突：整笔回滚，调用方收 409，不留下事件成功/事实缺失的半态。
                raise HTTPException(409, f"核销事实写入失败（券码可能已核销）: {e}") from e
            # 闸内停顿钩子：事实已在本事务插入、尚未提交，稳定复现「钉扎持锁时来核销」。
            gating.gate_pause("REDEEM")
        conn.commit()
        return {"event_id": eid, "fact_id": fact_id, "business_day": day, "status": "open" if fact_id else None}
    except HTTPException:
        if conn.in_transaction:
            conn.rollback()
        raise
    except sqlite3.OperationalError as e:
        conn.rollback()
        # 跨进程库级写锁等待触顶：明确以冲突语义拒绝，而非让调用方稍后重试。
        raise HTTPException(409, f"业务日 {day} 写锁竞争，核销写入被拒绝: {e}") from e
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
