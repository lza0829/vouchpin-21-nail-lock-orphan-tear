# Vouchpin（券钉簿）

骨架：券事件入湖 → 核销事实钉扎 → 过期/退回窗（后挖）→ 批次互证。

本地：
- API: `cd backend && python -m uvicorn app.main:app --port 10800`
- Web: `cd frontend && npm install && npm run dev`（5800）

## 钉扎 × 核销临界：全局互斥（钉扎优先）

钉扎（批次封存）与核销写入围绕同一业务日互斥，只贯彻一种策略——**钉扎优先**，
不使用「稍后重试」代替互斥本身。

### 两个核销写入口（共用同一把业务日写锁、同一服务函数）

1. 页面表单：`POST /api/events`，`event_type=redeem`
2. API 专用：`POST /api/redeem`

### 互斥与同成同败

- **业务日写闸**（`app/gating.py`）：按 `business_day` 分桶的进程内互斥锁。
  钉扎事务与两个入口的核销写，进入写事务前都必须先取得该日写锁。
- **库级写锁兜底**：写连接一律 `BEGIN IMMEDIATE`（`PRAGMA busy_timeout=5000`，
  WAL 模式）。多 worker / 多进程无共享内存时，由 SQLite 库级写锁串行化；
  锁等待触顶统一翻译成 `409`，而不是要求调用方重试。
- **钉扎持锁期间**：新核销事实阻塞在闸外，无法插入；钉扎提交释放锁后，
  核销事务内二次检查到日已 `sealed` → `409`，被拒写整体回滚，
  不留下「成功回执但既不在批内也不是 open」的中间态。
- **钉扎先扫描到的 open 集合即锁前快照**：批头（笔数/合计）、批明细
  （仅 `status='open'` 的事实可置 `sealed`，行数不符即整笔回滚）、
  日 `sealed` 标记在同一事务提交，同成同败。
- **提交前对账**：批头 `fact_count/total_amount` 必须与明细及锁前快照的
  `coupon_code` / 金额集合逐一相等，否则整笔失败回滚，事实仍为 open 可再钉。
- **总览已钉计数**：`GET /api/dashboard` 的 `sealed_fact_count` 与各批头
  `fact_count` 之和、金额之和对账，结果在 `sealed_reconciles` 给出。
- 批次明细与对账：`GET /api/batches/{id}`。

### 演示与验证

```bash
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt httpx pytest

# 单元/并发测试（线程池 + 闸内延迟钩子固定交错）
python -m pytest tests/ -v

# 真实 uvicorn 进程的 HTTP 交错演示（四种场景，含断言）
python scripts/race_demo.py
```

`race_demo.py` 覆盖的交错：

| 场景 | 交错 | 预期 |
| --- | --- | --- |
| 1 | 先开钉，钉扎持锁期间页面/API 两入口同时打核销 | 钉扎只收锁前事实；两笔核销 409，无游离回执 |
| 2 | 先打在途核销，再开钉 | 在途核销先成功为 open，随后完整进入本批明细与合计 |
| 3 | 双入口同时各写 5 笔（不钉扎） | 业务日锁串行化，10 笔全 open、无丢失重复，可一次钉入 |
| 4 | 钉扎提交前故障注入 | 整笔回滚：无批次残留、事实仍 open、写闸仍放行 |

仅测试/演示使用的环境变量（生产默认零开销）：

- `VOUCHPIN_SEAL_DELAY_MS` / `VOUCHPIN_REDEEM_DELAY_MS`：钉扎/核销事务持闸内停顿，固定交错时序。
- `VOUCHPIN_SEAL_FAIL=1`：钉扎提交前注入故障，验证同成同败回滚。
