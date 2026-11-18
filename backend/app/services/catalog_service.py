from __future__ import annotations

from app.db import connect
from app.repositories import settings as settings_repo
from app.repositories import facts as fact_repo
from app.repositories import batches as batch_repo
from app.repositories import events as event_repo


def dashboard() -> dict:
    conn = connect()
    try:
        day = settings_repo.get(conn, "business_day")
        facts = fact_repo.list_facts(conn, day)
        open_n = sum(1 for f in facts if f["status"] == "open")
        sealed_n = sum(1 for f in facts if f["status"] == "sealed")
        conflict_n = sum(1 for f in facts if f["status"] == "conflict")
        total = round(sum(float(f["amount"]) for f in facts), 2)
        events = event_repo.list_events(conn, day)
        batches = [b for b in batch_repo.list_batches(conn) if b["business_day"] == day]
        sealed_today = fact_repo.day_is_sealed(conn, day)
        batch_fact_sum = sum(int(b["fact_count"]) for b in batches)
        batch_amount_sum = round(sum(float(b["total_amount"]) for b in batches), 2)
        sealed_amount = round(
            sum(float(f["amount"]) for f in facts if f["status"] == "sealed"), 2
        )
        # 锁后孤儿：日已 sealed 却仍 open 的行。正确路径恒为 0；非 0 即闭包未闭合。
        orphan_rows = fact_repo.orphan_facts_for_sealed_day(conn, day) if sealed_today else []
        orphan_n = len(orphan_rows)
        # 严格对账：已钉计数/金额必须与批头之和逐一对上，且日已钉后不得残留任何
        # 可钉 open（孤儿）。任一不满足即报不一致，禁止「总览已钉而预览仍吃孤儿」。
        reconciles = (
            sealed_n == batch_fact_sum
            and sealed_amount == batch_amount_sum
            and orphan_n == 0
        )
        return {
            "business_day": day,
            "event_count": len(events),
            "fact_count": len(facts),
            "open_fact_count": open_n,
            "sealed_fact_count": sealed_n,
            "conflict_fact_count": conflict_n,
            "redeem_total_today": total,
            "batch_count": len(batches),
            "day_sealed": sealed_today,
            "sealed_reconciles": reconciles,
            # 锁后仍 open 的孤儿数：闭包不变量下必须恒为 0。
            "orphan_open_count": orphan_n,
            "orphan_facts": orphan_rows,
        }
    finally:
        conn.close()
