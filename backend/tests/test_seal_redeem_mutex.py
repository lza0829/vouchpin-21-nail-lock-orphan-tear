"""钉扎×核销临界互斥（钉扎优先）并发测试。

用线程池制造真实并发，用闸内延迟钩子（VOUCHPIN_*_DELAY_MS）把交错时序固定下来：
  1. test_seal_then_redeem_rejected：先开钉、钉扎持锁期间打核销 → 核销 409、批只含锁前事实
  2. test_redeem_then_seal_includes_it：先打在途核销、再钉 → 该核销入批
  3. test_two_entries_concurrent_writes：页面 /events 与 API /redeem 同时写 → 均 open、无丢失
  4. test_seal_failure_rolls_back：钉扎提交前故障 → 同成同败、事实仍 open、无游离回执
另含对账与「无成功回执但游离」的强一致断言，以及锁后孤儿/重复钉扎/无锁短窗拒写/
冲突桶消化的闭包回归。
"""
from __future__ import annotations

import concurrent.futures as cf
from contextlib import contextmanager

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


# ---------------------------------------------------------------------------
# 闭包回归：锁释放与写入交错、锁后 open 扫描、重复钉扎、无锁短窗、冲突桶
# ---------------------------------------------------------------------------


def test_sealed_day_rejects_write_with_no_rows_left(client, day):
    """钉扎提交、锁释放后再核销：双入口都必须 409，且事实/事件均无残留。"""
    make_open_fact(client, day, f"{day}-A")
    seal_res = client.post("/api/redeem/seal", json={"business_day": day})
    assert seal_res.status_code == 200

    for entry in ("page", "api"):
        code = f"{day}-POST-SEAL-{entry}"
        r = _post_redeem(client, day, code, entry=entry)
        assert r.status_code == 409, r.text

    facts = client.get(f"/api/facts?business_day={day}").json()
    assert {f["status"] for f in facts} == {"sealed"}
    assert all(f["batch_id"] is not None for f in facts)
    # 「应拒已落库」防线：被拒券码在事件表同样不得出现 redeem 残留
    events = client.get(f"/api/events?business_day={day}").json()
    redeem_codes = {e["coupon_code"] for e in events if e["event_type"] == "redeem"}
    assert f"{day}-POST-SEAL-page" not in redeem_codes
    assert f"{day}-POST-SEAL-api" not in redeem_codes


def test_preview_does_not_eat_open_after_sealed(client, day):
    """总览已钉后，preview/orphans 不得再把任何行当可钉 open 放大。"""
    make_open_fact(client, day, f"{day}-A")
    make_open_fact(client, day, f"{day}-B")
    client.post("/api/redeem/seal", json={"business_day": day})

    p = client.get(f"/api/redeem/preview?business_day={day}").json()
    assert p["already_sealed"] is True
    assert p["open_count"] == 0
    assert p["facts"] == []
    assert p["orphan_open_count"] == 0

    o = client.get(f"/api/redeem/orphans?business_day={day}").json()
    assert o["orphan_open_count"] == 0
    assert o["facts"] == []


def test_double_seal_is_rejected(client, day):
    """禁止再钉第二份现行：同日二次 seal 必须 409，不产生第二个批次。"""
    make_open_fact(client, day, f"{day}-A")
    first = client.post("/api/redeem/seal", json={"business_day": day})
    assert first.status_code == 200

    second = client.post("/api/redeem/seal", json={"business_day": day})
    assert second.status_code == 409, second.text

    batches = [b for b in client.get("/api/batches").json() if b["business_day"] == day]
    assert len(batches) == 1


def test_sealed_check_holds_even_without_day_lock(client, day, monkeypatch):
    """短窗无锁也认 sealed：绕过业务日闸直接走写事务，仍须在 INSERT 前 409。"""
    make_open_fact(client, day, f"{day}-A")
    client.post("/api/redeem/seal", json={"business_day": day})

    import app.services.event_service as event_svc

    real_lock = event_svc.gating.day_write_lock

    @contextmanager
    def broken_lock(_day):
        # 模拟短窗提前释放/未取得锁：临界区实际不互斥
        yield

    monkeypatch.setattr(event_svc.gating, "day_write_lock", broken_lock)
    try:
        r = _post_redeem(client, day, f"{day}-NOLOCK", entry="api")
    finally:
        monkeypatch.setattr(event_svc.gating, "day_write_lock", real_lock)
    assert r.status_code == 409

    facts = client.get(f"/api/facts?business_day={day}").json()
    assert all(f["coupon_code"] != f"{day}-NOLOCK" for f in facts)
    assert {f["status"] for f in facts} == {"sealed"}


def test_lock_window_is_held_for_full_critical_section(client, day, monkeypatch):
    """锁释放与写入交错：钉扎持锁期间进入的核销，锁释放后只能 409，绝不成孤儿。"""
    make_open_fact(client, day, f"{day}-A")
    monkeypatch.setenv("VOUCHPIN_SEAL_DELAY_MS", "800")

    with cf.ThreadPoolExecutor(max_workers=2) as ex:
        f_seal = ex.submit(client.post, "/api/redeem/seal", json={"business_day": day})
        import time
        time.sleep(0.2)
        f_redeem = ex.submit(_post_redeem, client, day, f"{day}-RACE", entry="page")
        seal_res = f_seal.result()
        redeem_res = f_redeem.result()

    assert seal_res.status_code == 200
    assert redeem_res.status_code == 409

    # 锁中写入不得成孤儿：没有任何 open 残留，被拒券码完全不存在
    facts = client.get(f"/api/facts?business_day={day}").json()
    assert {f["status"] for f in facts} == {"sealed"}
    assert all(f["coupon_code"] != f"{day}-RACE" for f in facts)

    client.put("/api/settings/day", json={"business_day": day})
    d = client.get("/api/dashboard").json()
    assert d["day_sealed"] is True
    assert d["orphan_open_count"] == 0
    assert d["sealed_reconciles"] is True


def test_orphan_quarantine_closes_and_dashboard_flags_it(client, day):
    """历史孤儿（锁期应拒却已落库的 open）经冲突桶消化后，闭包重新闭合。"""
    from app.db import connect
    from app.repositories import facts as fact_repo
    from app.repositories import events as event_repo

    make_open_fact(client, day, f"{day}-A")
    seal_res = client.post("/api/redeem/seal", json={"business_day": day})
    bid = seal_res.json()["batch_id"]

    # 直接在库内植入一行「应拒已落库、未进任何批」的锁后 open 孤儿
    conn = connect(immediate=True)
    try:
        eid = event_repo.insert_event(
            conn, event_type="redeem", coupon_code=f"{day}-ORPHAN",
            campaign_code="NEW10", amount=10.0, business_day=day,
            occurred_at="2099-01-01T00:00:00",
        )
        orphan_id = fact_repo.insert_fact(
            conn, coupon_code=f"{day}-ORPHAN", campaign_code="NEW10",
            amount=10.0, business_day=day, event_id=eid, status="open",
        )
        conn.commit()
    finally:
        conn.close()

    # 孤儿必须能被扫到，且 preview 不把它当可钉 open
    o = client.get(f"/api/redeem/orphans?business_day={day}").json()
    assert o["orphan_open_count"] == 1
    assert o["facts"][0]["id"] == orphan_id
    p = client.get(f"/api/redeem/preview?business_day={day}").json()
    assert p["open_count"] == 0 and p["orphan_open_count"] == 1

    # 总览对账必须翻成不一致，禁止「已钉却显示闭合」
    client.put("/api/settings/day", json={"business_day": day})
    d = client.get("/api/dashboard").json()
    assert d["day_sealed"] is True and d["orphan_open_count"] == 1
    assert d["sealed_reconciles"] is False

    # 有孤儿时重钉依旧 409：必须先消化孤儿，不得再钉第二份现行
    assert client.post("/api/redeem/seal", json={"business_day": day}).status_code == 409

    # 消化：硬剔进冲突桶，不进任何批次、不改批头
    q = client.post("/api/redeem/quarantine", json={"business_day": day})
    assert q.status_code == 200, q.text
    assert q.json()["quarantined_count"] == 1

    facts = client.get(f"/api/facts?business_day={day}").json()
    orphan_row = next(f for f in facts if f["id"] == orphan_id)
    assert orphan_row["status"] == "conflict" and orphan_row["batch_id"] is None
    # 原批次明细与批头保持闭包，未被事后篡改
    bd = client.get(f"/api/batches/{bid}").json()
    assert bd["reconcile"]["matches"] is True
    assert all(f["id"] != orphan_id for f in bd["detail"])

    d2 = client.get("/api/dashboard").json()
    assert d2["orphan_open_count"] == 0
    assert d2["conflict_fact_count"] == 1
    assert d2["sealed_reconciles"] is True

    # 冲突桶事实不再是可钉 open：再扫孤儿为空，preview 仍无可钉
    assert client.get(f"/api/redeem/orphans?business_day={day}").json()["orphan_open_count"] == 0
    assert client.get(f"/api/redeem/preview?business_day={day}").json()["open_count"] == 0


def test_quarantine_without_sealed_day_is_rejected(client, day):
    """未钉日子不存在锁后孤儿，隔离请求必须 409，正常 open 不受影响。"""
    make_open_fact(client, day, f"{day}-A")
    r = client.post("/api/redeem/quarantine", json={"business_day": day})
    assert r.status_code == 409
    facts = client.get(f"/api/facts?business_day={day}").json()
    assert {f["status"] for f in facts} == {"open"}
