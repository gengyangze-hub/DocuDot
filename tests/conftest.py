"""pytest 共享 fixture。"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402

ADMIN_KEY = "test-admin-key"
READ_KEY = "test-read-key"
WRITE_KEY = "test-write-key"


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    """每个测试用独立的临时数据库，且不读取项目 .env。"""
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_path=str(tmp_path / "inventory.db"),
        data_dir=str(tmp_path),
        bootstrap_api_key=ADMIN_KEY,
        fuzzy_threshold=0.55,
        llm_enabled=False,
        qq_bot_enabled=False,
    )


@pytest.fixture()
def app(settings: Settings):
    return create_app(settings)


@pytest.fixture()
def client(app) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def admin() -> dict[str, str]:
    return {"X-API-Key": ADMIN_KEY}


@pytest.fixture()
def reader(app, client: TestClient) -> dict[str, str]:
    """只读 Key（用固定明文，方便断言）。"""
    app.state.ctx.repo.create_api_key(label="reader", scopes=["read"], raw_key=READ_KEY)
    return {"X-API-Key": READ_KEY}


@pytest.fixture()
def writer(app, client: TestClient) -> dict[str, str]:
    app.state.ctx.repo.create_api_key(label="writer", scopes=["read", "write"], raw_key=WRITE_KEY)
    return {"X-API-Key": WRITE_KEY}


@pytest.fixture()
def seeded(client: TestClient, admin: dict[str, str]) -> list[dict]:
    """一份跨两类物品的种子数据。"""
    payloads = [
        {
            "name": "STM32F103C8T6",
            "category": "stm_component",
            "quantity": 25,
            "location": "A柜-1层-盒3",
            "spec": "LQFP48",
            "aliases": ["F103C8", "STM32F103"],
            "operator": "tester",
        },
        {
            "name": "0.1uF 50V MLCC",
            "category": "stm_component",
            "quantity": 500,
            "location": "A柜-2层-盒1",
            "spec": "0805",
            "aliases": ["104", "100nF"],
            "operator": "tester",
        },
        {
            "name": "Steam 50元充值卡",
            "category": "steam_card",
            "quantity": 10,
            "location": "B柜-抽屉1",
            "spec": "50元",
            "aliases": ["50元卡"],
            "operator": "tester",
        },
    ]
    created = []
    for payload in payloads:
        response = client.post("/api/v1/items", json=payload, headers=admin)
        assert response.status_code == 201, response.text
        created.append(response.json())
    return created
