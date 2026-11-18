#!/usr/bin/env python3
"""钉扎×核销临界互斥 —— 真实 HTTP 交错演示（钉扎优先）。

启动真实 uvicorn 进程，用线程池对两个核销写入口（页面 POST /api/events、
API POST /api/redeem）与钉扎 POST /api/redeem/seal 做并发交错，演示：

  场景 1  先开钉、再打核销（双入口各一笔）→ 钉扎成功只含锁前事实，并发核销 409 且无游离回执
  场景 2  先打在途核销、再开钉 → 在途核销先成功为 open，随后被本批完整收纳
  场景 3  双入口同时写（不钉扎）→ 全部串行成功、无丢失无重复，可一次钉入
  场景 4  钉扎提交前故障（独立故障注入进程）→ 整笔回滚，事实仍 open，无批次残留
  场景 5  锁释放后双入口核销 409 无残留、孤儿扫描为空、重复钉扎 409；
          植入锁后孤儿 → 总览报未闭合 → 冲突桶消化后闭包恢复、原批不被篡改

用法： python scripts/race_demo.py
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _req(method: str, url: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(port: int, data_dir: str, extra_env: dict | None = None) -> subprocess.Popen:
    env = dict(os.environ)
    env["DATA_DIR"] = data_dir
    env.update(extra_env or {})
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=BACKEND_DIR, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}/api"
    for _ in range(60):
        try:
            _req("GET", f"{base}/health")
            return proc
        except Exception:
            time.sleep(0.25)
    proc.kill()
    raise RuntimeError("server did not come up")


def stop_server(proc: subprocess.Popen) -> None:
    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def redeem(base: str, day: str, code: str, entry: str) -> tuple[int, dict]:
    if entry == "page":
        return _req("POST", f"{base}/events",
                    {"event_type": "redeem", "coupon_code": code, "campaign_code": "NEW10", "business_day": day})
    return _req("POST", f"{base}/redeem",
                {"coupon_code": code, "campaign_code": "NEW10", "business_day": day})


def hr(title: str) -> None:
    print("\n" + "=" * 78 + f"\n{title}\n" + "=" * 78)


def main() -> None:
    work = tempfile.mkdtemp(prefix="vouchpin-demo-")
    port, fail_port = _free_port(), _free_port()
    # 固定 0.7s 闸内窗口，保证交错稳定可复现
    proc = start_server(port, os.path.join(work, "main"),
                        {"VOUCHPIN_SEAL_DELAY_MS": "700", "VOUCHPIN_REDEEM_DELAY_MS": "700"})
    fail_proc = start_server(fail_port, os.path.join(work, "fail"),
                             {"VOUCHPIN_SEAL_FAIL": "1"})
    base, fail_base = f"http://127.0.0.1:{port}/api", f"http://127.0.0.1:{fail_port}/api"
    try:
        # ---------------- 场景 1：先开钉，钉扎持锁期间双入口来核销 ----------------
        hr("场景 1  先开钉 → 持锁期间页面/API 两入口同时打核销（钉扎优先：拒绝并发核销）")
        day = f"2088-01-{uuid.uuid4().int % 28 + 1:02d}"
        _req("PUT", f"{base}/settings/day", {"business_day": day})
        for c in ("S1-A", "S1-B"):
            redeem(base, day, c, "page")
        with cf.ThreadPoolExecutor(max_workers=3) as ex:
            f_seal = ex.submit(_req, "POST", f"{base}/redeem/seal", {"business_day": day})
            time.sleep(0.2)
            f_page = ex.submit(redeem, base, day, "S1-RACE-PAGE", "page")
            f_api = ex.submit(redeem, base, day, "S1-RACE-API", "api")
            sc_seal, seal = f_seal.result()
            sc_page, page_err = f_page.result()
            sc_api, api_err = f_api.result()
        print(f"钉扎结果: HTTP {sc_seal}，批 #{seal.get('batch_id')}，"
              f"笔数 {seal.get('fact_count')}，合计 {seal.get('total_amount')}")
        print(f"  锁前快照券码: {[s['coupon_code'] for s in seal.get('snapshot', [])]}")
        print(f"页面入口并发核销: HTTP {sc_page}（{page_err.get('detail')}）")
        print(f"API 入口并发核销: HTTP {sc_api}（{api_err.get('detail')}）")
        _, bd = _req("GET", f"{base}/batches/{seal['batch_id']}")
        print(f"批明细对账: {bd['reconcile']}")
        _, facts = _req("GET", f"{base}/facts?business_day={day}")
        codes = {f["coupon_code"] for f in facts}
        ghost = {"S1-RACE-PAGE", "S1-RACE-API"} & codes
        print(f"游离成功回执检查: 被拒券码出现在事实表 = {ghost or '无（正确）'}；"
              f"事实状态集合 = { {f['status'] for f in facts} }")
        assert sc_seal == 200 and sc_page == 409 and sc_api == 409 and not ghost
        assert bd["reconcile"]["matches"] and all(f["status"] == "sealed" for f in facts)

        # ---------------- 场景 2：先打在途核销，再开钉 ----------------
        hr("场景 2  先打在途核销（API 入口）→ 后开钉（在途事实必须进入本批）")
        day2 = f"2087-02-{uuid.uuid4().int % 28 + 1:02d}"
        _req("PUT", f"{base}/settings/day", {"business_day": day2})
        redeem(base, day2, "S2-A", "page")
        with cf.ThreadPoolExecutor(max_workers=2) as ex:
            f_rd = ex.submit(redeem, base, day2, "S2-INFLIGHT", "api")
            time.sleep(0.15)
            f_seal2 = ex.submit(_req, "POST", f"{base}/redeem/seal", {"business_day": day2})
            sc_rd, rd = f_rd.result()
            sc_seal2, seal2 = f_seal2.result()
        print(f"在途核销: HTTP {sc_rd}，回执 status={rd.get('status')}，fact_id={rd.get('fact_id')}")
        print(f"钉扎结果: HTTP {sc_seal2}，笔数 {seal2.get('fact_count')}，"
              f"快照券码 {[s['coupon_code'] for s in seal2.get('snapshot', [])]}")
        _, bd2 = _req("GET", f"{base}/batches/{seal2['batch_id']}")
        print(f"批明细对账: {bd2['reconcile']}")
        _, facts2 = _req("GET", f"{base}/facts?business_day={day2}")
        print(f"事实终态: {[(f['coupon_code'], f['status'], f['batch_id']) for f in facts2]}")
        assert sc_rd == 200 and sc_seal2 == 200 and seal2["fact_count"] == 2
        assert bd2["reconcile"]["matches"] and all(f["status"] == "sealed" for f in facts2)

        # ---------------- 场景 3：双入口同时写（不钉扎） ----------------
        hr("场景 3  页面 /events 与 API /redeem 同时各写 5 笔（无钉扎，业务日锁串行化）")
        day3 = f"2086-03-{uuid.uuid4().int % 28 + 1:02d}"
        _req("PUT", f"{base}/settings/day", {"business_day": day3})
        with cf.ThreadPoolExecutor(max_workers=10) as ex:
            futs = []
            for i in range(5):
                futs.append(ex.submit(redeem, base, day3, f"S3-P{i}", "page"))
                futs.append(ex.submit(redeem, base, day3, f"S3-X{i}", "api"))
            res = [f.result() for f in futs]
        codes3 = sorted(r[1].get("fact_id") for r in res)
        print(f"10 笔并发写入 HTTP 状态集合: {sorted({r[0] for r in res})}，fact_id 唯一数: {len(set(codes3))}")
        _, facts3 = _req("GET", f"{base}/facts?business_day={day3}")
        print(f"落库事实数: {len(facts3)}，状态集合: { {f['status'] for f in facts3} }")
        sc_s3, seal3 = _req("POST", f"{base}/redeem/seal", {"business_day": day3})
        _, bd3 = _req("GET", f"{base}/batches/{seal3['batch_id']}")
        print(f"随后钉扎: HTTP {sc_s3}，批对账 matches={bd3['reconcile']['matches']}，明细 {bd3['reconcile']['detail_count']} 笔")
        assert {r[0] for r in res} == {200} and len(facts3) == 10 and bd3["reconcile"]["matches"]

        # ---------------- 场景 4：钉扎提交前故障，整笔回滚 ----------------
        hr("场景 4  钉扎提交前故障注入 → 同成同败回滚（批头/明细/日标记不落，事实仍 open）")
        day4 = f"2085-04-{uuid.uuid4().int % 28 + 1:02d}"
        _req("PUT", f"{fail_base}/settings/day", {"business_day": day4})
        redeem(fail_base, day4, "S4-A", "page")
        redeem(fail_base, day4, "S4-B", "api")
        sc_fail, fail_body = _req("POST", f"{fail_base}/redeem/seal", {"business_day": day4})
        _, batches4 = _req("GET", f"{fail_base}/batches")
        _, facts4 = _req("GET", f"{fail_base}/facts?business_day={day4}")
        sc_after, after = redeem(fail_base, day4, "S4-C", "page")
        print(f"故障钉扎: HTTP {sc_fail}（{fail_body.get('detail')}）")
        print(f"该日批次残留: {[b for b in batches4 if b['business_day'] == day4] or '无（正确）'}")
        print(f"事实终态: {[(f['coupon_code'], f['status'], f['batch_id']) for f in facts4]}")
        print(f"回滚后再核销: HTTP {sc_after}，fact_id={after.get('fact_id')}（闸仍放行）")
        assert sc_fail == 500
        assert not [b for b in batches4 if b["business_day"] == day4]
        assert all(f["status"] == "open" and f["batch_id"] is None for f in facts4)
        assert sc_after == 200

        # ---------------- 场景 5：锁释放后写入交错 + 锁后孤儿扫描 + 冲突桶消化 ----------------
        hr("场景 5  钉扎释放后再核销（409 无残留）→ 孤儿扫描为空 → 重复钉扎 409 → 植入孤儿并消化")
        day5 = f"2084-05-{uuid.uuid4().int % 28 + 1:02d}"
        _req("PUT", f"{base}/settings/day", {"business_day": day5})
        redeem(base, day5, "S5-A", "page")
        sc_s5, seal5 = _req("POST", f"{base}/redeem/seal", {"business_day": day5})
        # 锁已释放：两个入口再核销都必须 409，且不留事件/事实
        sc_p5, body_p5 = redeem(base, day5, "S5-LATE-PAGE", "page")
        sc_x5, body_x5 = redeem(base, day5, "S5-LATE-API", "api")
        _, orphans5 = _req("GET", f"{base}/redeem/orphans?business_day={day5}")
        sc_seal_again, _ = _req("POST", f"{base}/redeem/seal", {"business_day": day5})
        print(f"钉扎: HTTP {sc_s5}，批 #{seal5.get('batch_id')}；锁后双入口核销: {sc_p5}/{sc_x5}（均应 409）")
        print(f"锁后孤儿扫描: orphan_open_count={orphans5.get('orphan_open_count')}（应 0）；"
              f"重复钉扎: HTTP {sc_seal_again}（应 409）")
        assert (sc_s5, sc_p5, sc_x5, sc_seal_again) == (200, 409, 409, 409)
        assert orphans5["orphan_open_count"] == 0 and orphans5["facts"] == []
        _, facts5 = _req("GET", f"{base}/facts?business_day={day5}")
        assert all(f["status"] == "sealed" for f in facts5)
        assert not {"S5-LATE-PAGE", "S5-LATE-API"} & {f["coupon_code"] for f in facts5}

        # 直接在库内植入一行「锁期应拒却已落库」的孤儿，验证扫描/总览/消化闭环
        import sqlite3
        db_path = os.path.join(work, "main", "clipbond.db")
        wconn = sqlite3.connect(db_path)
        wconn.execute(
            "INSERT INTO coupon_events(event_type, coupon_code, campaign_code, amount, business_day, occurred_at)"
            " VALUES('redeem','S5-ORPHAN','NEW10',10,?,?)",
            (day5, f"{day5}T23:00:00"),
        )
        eid = wconn.execute("SELECT last_insert_rowid()").fetchone()[0]
        wconn.execute(
            "INSERT INTO redeem_facts(coupon_code, campaign_code, amount, business_day, event_id, status)"
            " VALUES('S5-ORPHAN','NEW10',10,?,?,'open')",
            (day5, eid),
        )
        wconn.commit()
        wconn.close()
        _, orphans5b = _req("GET", f"{base}/redeem/orphans?business_day={day5}")
        _, dash5 = _req("GET", f"{base}/dashboard")  # 业务日已切到 day5
        sc_q5, q5 = _req("POST", f"{base}/redeem/quarantine", {"business_day": day5})
        _, facts5c = _req("GET", f"{base}/facts?business_day={day5}")
        _, bd5 = _req("GET", f"{base}/batches/{seal5['batch_id']}")
        print(f"植入孤儿后扫描: orphan_open_count={orphans5b.get('orphan_open_count')}（应 1），"
              f"总览闭包对账 sealed_reconciles={dash5.get('sealed_reconciles')}（应 False）")
        print(f"冲突桶消化: HTTP {sc_q5}，隔离 {q5.get('quarantined_count')} 笔；"
              f"原批对账仍 matches={bd5['reconcile']['matches']}")
        assert orphans5b["orphan_open_count"] == 1
        assert dash5["sealed_reconciles"] is False and dash5["orphan_open_count"] == 1
        assert sc_q5 == 200 and q5["quarantined_count"] == 1
        orphan_row = next(f for f in facts5c if f["coupon_code"] == "S5-ORPHAN")
        assert orphan_row["status"] == "conflict" and orphan_row["batch_id"] is None
        assert bd5["reconcile"]["matches"] and all(f["coupon_code"] != "S5-ORPHAN" for f in bd5["detail"])
        _, dash5b = _req("GET", f"{base}/dashboard")
        assert dash5b["sealed_reconciles"] is True and dash5b["orphan_open_count"] == 0

        hr("全部断言通过：钉锁/双入口核销写/批头/批明细/日 sealed/总览已钉计数 同成同败，无游离回执")
    finally:
        stop_server(proc)
        stop_server(fail_proc)
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
