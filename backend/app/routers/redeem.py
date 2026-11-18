from __future__ import annotations

from fastapi import APIRouter, Query
from app.services import redeem_service as svc

router = APIRouter(tags=["redeem"])


@router.get("/facts")
def facts(business_day: str | None = Query(None)):
    return svc.list_facts(business_day)


@router.get("/redeem/preview")
def preview(business_day: str | None = Query(None)):
    return svc.preview(business_day)


@router.get("/redeem/orphans")
def orphans(business_day: str | None = Query(None)):
    data = svc.preview(business_day)
    return {
        "business_day": data["business_day"],
        "already_sealed": data["already_sealed"],
        "open_count": data["open_count"],
        "facts": data["facts"],
    }


@router.post("/redeem/seal")
def seal(body: dict):
    return svc.seal(body)


@router.get("/batches")
def batches():
    return svc.list_batches()


@router.get("/batches/{batch_id}")
def batch_detail(batch_id: int):
    return svc.get_batch_detail(batch_id)
