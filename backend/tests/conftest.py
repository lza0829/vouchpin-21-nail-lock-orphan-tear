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
    import uuid

    n = uuid.uuid4().int % 10_000_000
    d = f"2099-{n % 12 + 1:02d}-{(n // 12) % 28 + 1:02d}"
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
