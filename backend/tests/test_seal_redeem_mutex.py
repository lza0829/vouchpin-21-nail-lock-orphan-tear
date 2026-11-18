"""钉扎×核销临界互斥（钉扎优先）并发测试。

用线程池制造真实并发，用闸内延迟钩子（VOUCHPIN_*_DELAY_MS）把交错时序固定下来：
  1. test_seal_then_redeem_rejected：先开钉、钉扎持锁期间打核销 → 核销 409、批只含锁前事实
  2. test_redeem_then_seal_includes_it：先打在途核销、再钉 → 该核销入批
  3. test_two_entries_concurrent_writes：页面 /events 与 API /redeem 同时写 → 均 open、无丢失
  4. test_seal_failure_rolls_back：钉扎提交前故障 → 同成同败、事实仍 open、无游离回执
另含对账与「无成功回执但游离」的强一致断言。
"""
from __future__ import annotations

import concurrent.futures as cf

import pytest

from tests.conftest import make_open_fact

SEAL_DELAY = {"VOUCHPIN_SEAL_DELAY_MS": "600"}
REDEEM_DELAY = {"VOUCHPIN_REDEEM_DELAY_MS": "600"}


def _post_redeem(client, day, code, *, entry, camp="NEW10"):
    if entry == "page":  # 入口一：页面事件接口
        return client.post(
            "/api/events",
            json={"event_type": "redeem", "coupon_code": code, "campaign_code": camp, "business_day": day},
        )
    # 入口二：专用核销接口
    return client.post(
        "/api/redeem",
        json={"coupon_code": code, "campaign_code": camp, "business_day": day},
    )


def test_seal_then_redeem_rejected(client, day, monkeypatch):
    # 锁前已存在 2 笔 open，构成钉扎快照
    make_open_fact(client, day, f"{day}-A")
    make_open_fact(client, day, f"{day}-B")

    monkeypatch.setenv("VOUCHPIN_SEAL_DELAY_MS", "800")  # 钉扎持锁窗口
    with cf.ThreadPoolExecutor(max_workers=2) as ex:
        f_seal = ex.submit(client.post, "/api/redeem/seal", json={"business_day": day})
        # 确保钉扎先拿到业务日锁后再发起核销
        import time
        time.sleep(0.2)
        f_redeem_page = ex.submit(_post_redeem, client, day, f"{day}-R1", entry="page")
        f_redeem_api = ex.submit(_post_redeem, client, day, f"{day}-R2", entry="api")
        seal_res = f_seal.result()
        page_res = f_redeem_page.result()
        api_res = f_redeem_api.result()

    assert seal_res.status_code == 200, seal_res.text
    sealed = seal_res.json()
    assert sealed["fact_count"] == 2
    assert sorted(s["coupon_code"] for s in sealed["snapshot"]) == [f"{day}-A", f"{day}-B"]

    # 钉扎优先：持锁期间来的两笔核销，在锁释放后读到 sealed → 双双 409
    assert page_res.status_code == 409
    assert api_res.status_code == 409

    # 批次明细只含锁前 open 事实，券码/金额集合与快照可对上
    bd = client.get(f"/api/batches/{sealed['batch_id']}").json()
    assert bd["reconcile"]["matches"] is True
    assert bd["reconcile"]["coupon_codes"] == [f"{day}-A", f"{day}-B"]

    # 被拒核销不得留下「成功回执但游离」：事实表里没有它们，事件表里也没有 redeem 残留
    facts = client.get(f"/api/facts?business_day={day}").json()
    fact_codes = {f["coupon_code"] for f in facts}
    assert f"{day}-R1" not in fact_codes and f"{day}-R2" not in fact_codes
    assert {f["status"] for f in facts} == {"sealed"}
    events = client.get(f"/api/events?business_day={day}").json()
    assert f"{day}-R1" not in {e["coupon_code"] for e in events if e["event_type"] == "redeem"}

    # 日已 sealed 后再核销依旧 409
    late = _post_redeem(client, day, f"{day}-LATE", entry="api")
    assert late.status_code == 409


def test_redeem_then_seal_includes_it(client, day, monkeypatch):
    make_open_fact(client, day, f"{day}-A")
    monkeypatch.setenv("VOUCHPIN_REDEEM_DELAY_MS", "700")  # 核销在途窗口

    with cf.ThreadPoolExecutor(max_workers=2) as ex:
        # 先打一笔在途核销（先取得业务日锁，钉扎在闸外等）
        f_redeem = ex.submit(_post_redeem, client, day, f"{day}-INFLIGHT", entry="api")
        import time
        time.sleep(0.15)
        f_seal = ex.submit(client.post, "/api/redeem/seal", json={"business_day": day})
        redeem_res = f_redeem.result()
        seal_res = f_seal.result()

    # 钉扎优先语义下：核销先持锁则成功落 open，钉扎随后把锁前全部 open（含在途这笔）收入批
    assert redeem_res.status_code == 200
    assert redeem_res.json()["status"] == "open"
    assert seal_res.status_code == 200
    sealed = seal_res.json()
    assert sealed["fact_count"] == 2
    assert sorted(s["coupon_code"] for s in sealed["snapshot"]) == [f"{day}-A", f"{day}-INFLIGHT"]

    bd = client.get(f"/api/batches/{sealed['batch_id']}").json()
    assert bd["reconcile"]["matches"] is True
    assert bd["reconcile"]["detail_count"] == 2
    # 在途核销必须进入批明细，状态 sealed、带 batch_id，不存在游离 open
    inflight = next(f for f in bd["detail"] if f["coupon_code"] == f"{day}-INFLIGHT")
    assert inflight["status"] == "sealed" and inflight["batch_id"] == sealed["batch_id"]
    facts = client.get(f"/api/facts?business_day={day}").json()
    assert all(f["status"] == "sealed" for f in facts)


def test_two_entries_concurrent_writes(client, day, monkeypatch):
    monkeypatch.setenv("VOUCHPIN_REDEEM_DELAY_MS", "300")
    n_per_entry = 5
    with cf.ThreadPoolExecutor(max_workers=10) as ex:
        futs = []
        for i in range(n_per_entry):
            futs.append(ex.submit(_post_redeem, client, day, f"{day}-P{i}", entry="page"))
            futs.append(ex.submit(_post_redeem, client, day, f"{day}-X{i}", entry="api"))
        results = [f.result() for f in futs]

    # 双入口同时写：业务日锁把它们串行化，全部成功且互不覆盖
    assert all(r.status_code == 200 for r in results), [r.text for r in results if r.status_code != 200]
    facts = client.get(f"/api/facts?business_day={day}").json()
    assert len(facts) == 2 * n_per_entry
    assert {f["status"] for f in facts} == {"open"}  # 无 sealed 批，全部可再钉
    assert len({f["coupon_code"] for f in facts}) == 2 * n_per_entry  # 无重复/丢失

    # 随后钉扎一次性收口，批明细与全部成功回执一一对应
    seal_res = client.post("/api/redeem/seal", json={"business_day": day})
    assert seal_res.status_code == 200
    bd = client.get(f"/api/batches/{seal_res.json()['batch_id']}").json()
    assert bd["reconcile"]["matches"] is True
    assert bd["reconcile"]["detail_count"] == 2 * n_per_entry


def test_seal_failure_rolls_back(client, day, monkeypatch):
    make_open_fact(client, day, f"{day}-A")
    make_open_fact(client, day, f"{day}-B")
    monkeypatch.setenv("VOUCHPIN_SEAL_FAIL", "1")

    seal_res = client.post("/api/redeem/seal", json={"business_day": day})
    assert seal_res.status_code == 500  # 注入故障，整笔失败

    # 同成同败：没有批次残留
    batches = [b for b in client.get("/api/batches").json() if b["business_day"] == day]
    assert batches == []
    # 事实仍为 open、未带 batch_id，可再次钉扎
    facts = client.get(f"/api/facts?business_day={day}").json()
    assert all(f["status"] == "open" and f["batch_id"] is None for f in facts)
    # 日未 sealed，核销闸仍放行
    ok = _post_redeem(client, day, f"{day}-C", entry="page")
    assert ok.status_code == 200

    # 关闭故障注入后重新钉扎成功，三笔全入批
    monkeypatch.delenv("VOUCHPIN_SEAL_FAIL", raising=False)
    seal_res2 = client.post("/api/redeem/seal", json={"business_day": day})
    assert seal_res2.status_code == 200
    assert seal_res2.json()["fact_count"] == 3
    bd = client.get(f"/api/batches/{seal_res2.json()['batch_id']}").json()
    assert bd["reconcile"]["matches"] is True


def test_dashboard_sealed_count_matches_batches(client, day):
    make_open_fact(client, day, f"{day}-A")
    make_open_fact(client, day, f"{day}-B")
    client.post("/api/redeem/seal", json={"business_day": day})
    # 把总览业务日切到本用例的日子再核对计数
    client.put("/api/settings/day", json={"business_day": day})
    d = client.get("/api/dashboard").json()
    assert d["day_sealed"] is True
    assert d["sealed_fact_count"] == 2
    assert d["open_fact_count"] == 0
    assert d["sealed_reconciles"] is True
