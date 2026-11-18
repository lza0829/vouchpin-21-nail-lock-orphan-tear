from __future__ import annotations

from fastapi import APIRouter, Query
from app.services import event_service as svc

router = APIRouter(tags=["events"])


@router.get("/campaigns")
def campaigns():
    return svc.list_campaigns()


@router.get("/events")
def events(business_day: str | None = Query(None)):
    return svc.list_events(business_day)


# 核销写入口一（页面表单）：event_type=redeem 走与 /redeem 相同的业务日写闸。
@router.post("/events")
def create_event(body: dict):
    return svc.add_event(body)


# 核销写入口二（API 专用）：收敛到同一服务函数、同一把业务日锁。
@router.post("/redeem")
def create_redeem(body: dict):
    return svc.write_redeem(body)
