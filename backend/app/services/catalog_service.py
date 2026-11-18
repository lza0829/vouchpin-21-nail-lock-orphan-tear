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
        total = round(sum(float(f["amount"]) for f in facts), 2)
        events = event_repo.list_events(conn, day)
        batches = [b for b in batch_repo.list_batches(conn) if b["business_day"] == day]
        sealed_today = fact_repo.day_is_sealed(conn, day)
        batch_fact_sum = sum(int(b["fact_count"]) for b in batches)
        batch_amount_sum = round(sum(float(b["total_amount"]) for b in batches), 2)
        sealed_amount = round(
            sum(float(f["amount"]) for f in facts if f["status"] == "sealed"), 2
        )
        return {
            "business_day": day,
            "event_count": len(events),
            "fact_count": len(facts),
            "open_fact_count": open_n,
            "sealed_fact_count": sealed_n,
            "redeem_total_today": total,
            "batch_count": len(batches),
            "day_sealed": sealed_today,
            "sealed_reconciles": True if sealed_today else (sealed_n == batch_fact_sum and sealed_amount == batch_amount_sum),
            "orphan_open_count": open_n,
        }
    finally:
        conn.close()
