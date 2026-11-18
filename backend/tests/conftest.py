import os
import tempfile

# 必须在导入 app 之前指定独立数据目录（DB_PATH 在导入时解析）。
_TMP = tempfile.mkdtemp(prefix="vouchpin-test-")
os.environ["DATA_DIR"] = _TMP

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture()
def day(client: TestClient) -> str:
    # 每个用例独立业务日，互不污染；业务日锁按天分桶。
    # 用大随机空间（约 9 千亿种组合）保证不与历史用例已 sealed 的日子相撞。
    import uuid

    n = int(uuid.uuid4().hex[:10], 16)
    d = f"20{n % 100:02d}-{n // 100 % 12 + 1:02d}-{n // 1200 % 28 + 1:02d}"
    r = client.put("/api/settings/day", json={"business_day": d})
    assert r.status_code == 200, r.text
    return d


def make_open_fact(client: TestClient, day: str, code: str, camp: str = "NEW10") -> dict:
    r = client.post(
        "/api/events",
        json={"event_type": "redeem", "coupon_code": code, "campaign_code": camp, "business_day": day},
    )
    assert r.status_code == 200, r.text
    return r.json()
