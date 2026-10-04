"""发布前验收：授权覆盖、输入上限、对抗性输入、缓存失效、并发写入。

这些不是功能测试，而是「上线前必须成立」的性质：

* **每个业务端点都必须有鉴权**（结构上检查，不靠人工核对）
* 上传不能无限大（不能被一个超大请求体打爆）
* 畸形/超长/含控制字符的输入不能让接口 500
* 检索语料缓存在写入后必须立刻失效（陈旧结果 = 报错库存）
* 并发写入不能丢数据、不能损坏
"""

from __future__ import annotations

import asyncio
import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.models import ItemCreate, ItemUpdate, StockChangeRequest
from tests.conftest import ADMIN_KEY, READ_KEY, WRITE_KEY

# --------------------------------------------------------------------------- #
# 1. 授权覆盖
# --------------------------------------------------------------------------- #
def _iter_api_routes(app) -> list[APIRoute]:
    """展开出所有业务路由。

    ⚠️ **不能直接遍历 ``app.routes``**：Starlette 1.7 起 ``include_router`` 的结果
    不再展开，业务路由包在 ``_IncludedRouter`` 里 —— 直接遍历一条业务路由都看不到，
    「每条路由都有鉴权」这种断言就会**空转通过**（这正是它之前的毛病）。
    """
    found: list[APIRoute] = []
    for route in app.routes:
        if isinstance(route, APIRoute):
            found.append(route)
            continue
        inner = getattr(route, "original_router", None)
        if inner is not None:
            found.extend(sub for sub in inner.routes if isinstance(sub, APIRoute))
    return found


def _required_scope_names(route: APIRoute) -> set[str]:
    """路由依赖树里 ``require_*`` 依赖对应的权限名集合（``require_write`` → ``{"write"}``）。"""
    found: set[str] = set()
    stack = list(route.dependant.dependencies)
    while stack:
        dep = stack.pop()
        name = getattr(dep.call, "__name__", "")
        if name.startswith("require_"):
            found.update(part for part in name[len("require_") :].split("_") if part)
        stack.extend(dep.dependencies)
    return found


def test_every_business_route_requires_auth(app) -> None:
    """``/api/v1`` 下每个端点都必须挂鉴权依赖 —— 漏一个就是未授权访问。"""
    routes = [r for r in _iter_api_routes(app) if r.path.startswith("/api/v1")]
    # 先钉住数量：路由枚举方式一旦变化（比如又变回不展开），这条测试不至于空转
    assert len(routes) == 39, f"业务路由数变了（{len(routes)}），先确认枚举方式仍然有效"

    missing = [f"{sorted(r.methods)} {r.path}" for r in routes if not _required_scope_names(r)]
    assert not missing, f"这些端点没有鉴权：{missing}"


#: 用 POST 传参但**不改数据**的端点，允许只用 read/analyze：
#: 检索和 NLQ 是只读查询；tidy 的写操作（``apply=true``）在处理器内部另有 write 校验，
#: 下面 test_tidy_apply_requires_write_scope 会真的跑一遍验证。
_READ_ONLY_POST_OK = {
    ("POST", "/api/v1/analysis/ai"),
    ("POST", "/api/v1/analysis/ai/parse-stock"),
    ("POST", "/api/v1/analysis/nlq"),
    ("POST", "/api/v1/analysis/tidy"),
    ("POST", "/api/v1/search/match"),
}


def test_mutating_routes_do_not_accept_read_or_analyze(app) -> None:
    """**会改数据的端点不能只要 read/analyze。**

    这条是补上来的：原先只检查「挂没挂 require」，于是
    ``/bot/command``（只要 read 却能清库）、``/import/ai``（只要 analyze 却直接落库）
    这类越权端点全都能通过验收。现在按方法 + 权限逐条卡。
    """
    wrong: list[str] = []
    for route in _iter_api_routes(app):
        methods = {m.upper() for m in route.methods} - {"HEAD", "OPTIONS"}
        if not (methods & {"POST", "PUT", "PATCH", "DELETE"}):
            continue
        scopes = _required_scope_names(route)
        if any((method, route.path) in _READ_ONLY_POST_OK for method in methods):
            continue
        if not scopes & {"write", "admin"}:
            wrong.append(f"{sorted(methods)} {route.path} → {sorted(scopes)}")
    assert not wrong, f"这些写端点权限过低：{wrong}"


def test_tidy_apply_requires_write_scope(client: TestClient, admin: dict[str, str]) -> None:
    """``/analysis/tidy`` 只挂 analyze，但 ``apply=true`` 必须在运行时被挡住。"""
    analyzer = client.post(
        "/api/v1/admin/keys", json={"label": "analyzer", "scopes": ["analyze"]}, headers=admin
    ).json()["key"]
    headers = {"X-API-Key": analyzer}

    # 真正的应用必须 403（权限检查排在调大模型之前，所以不依赖 LLM 可用）
    response = client.post("/api/v1/analysis/tidy", json={"apply": True}, headers=headers)
    assert response.status_code == 403, response.text
    assert "write" in response.json()["message"]


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        # 曾经只要求 read，实际能改库/删库
        ("POST", "/api/v1/bot/command", {"write"}),
        # 曾经只要求 analyze，dry_run 默认 False 直接落库
        ("POST", "/api/v1/import/ai", {"write"}),
        ("POST", "/api/v1/import/commit", {"write"}),
        ("POST", "/api/v1/import/commit-file", {"write"}),
        # 删除类必须是 admin
        ("DELETE", "/api/v1/items/{item_id}", {"admin"}),
        ("POST", "/api/v1/admin/keys", {"admin"}),
    ],
)
def test_critical_routes_have_expected_scope(
    app, method: str, path: str, expected: set[str]
) -> None:
    for route in _iter_api_routes(app):
        if route.path == path and method in route.methods:
            actual = _required_scope_names(route)
            assert actual == expected, (path, actual)
            return
    raise AssertionError(f"没找到路由 {method} {path}")


@pytest.mark.parametrize("method", ["get", "post", "delete"])
def test_unauthenticated_requests_are_rejected(client: TestClient, method: str) -> None:
    """不带 Key 访问应得 401，不能泄露任何数据。"""
    for path in ("/api/v1/items", "/api/v1/analysis/overview", "/api/v1/admin/status"):
        response = getattr(client, method)(path)
        assert response.status_code in (401, 405), (method, path, response.status_code)


def test_read_key_cannot_write(client: TestClient, reader: dict[str, str]) -> None:
    """只有 read 权限的 Key 不能写。"""
    response = client.post(
        "/api/v1/items", json={"name": "越权物品", "quantity": 1}, headers=reader
    )
    assert response.status_code == 403


def test_write_key_cannot_admin(client: TestClient, writer: dict[str, str]) -> None:
    """write 权限不能管 Key、不能删条目。"""
    assert client.post("/api/v1/admin/keys", json={"label": "x"}, headers=writer).status_code == 403


def test_invalid_and_revoked_keys_are_rejected(client: TestClient, admin: dict[str, str]) -> None:
    assert client.get("/api/v1/items", headers={"X-API-Key": "nope"}).status_code == 401
    assert client.get("/api/v1/items", headers={"X-API-Key": ""}).status_code == 401

    created = client.post("/api/v1/admin/keys", json={"label": "temp"}, headers=admin).json()
    raw = created["key"]
    assert client.get("/api/v1/items", headers={"X-API-Key": raw}).status_code == 200
    client.delete(f"/api/v1/admin/keys/{created['id']}", headers=admin)
    assert client.get("/api/v1/items", headers={"X-API-Key": raw}).status_code == 401


def test_bearer_token_also_accepted(client: TestClient, admin: dict[str, str]) -> None:
    response = client.get("/api/v1/items", headers={"Authorization": f"Bearer {ADMIN_KEY}"})
    assert response.status_code == 200


# --------------------------------------------------------------------------- #
# 2. 上传上限
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", ["/api/v1/import/preview", "/api/v1/import/commit-file"])
def test_oversized_upload_is_rejected(client: TestClient, admin: dict[str, str], path: str) -> None:
    """两个上传端点都必须有大小上限 —— 之前 commit-file 完全没有检查。"""
    from app.api.routes_import import MAX_UPLOAD_BYTES

    blob = "| 名称 | 数量 |\n| --- | --- |\n".encode() + b"| X | 1 |\n" * (
        (MAX_UPLOAD_BYTES // 10) + 1
    )
    response = client.post(
        path, files={"file": ("big.md", io.BytesIO(blob), "text/markdown")}, headers=admin
    )
    assert response.status_code == 422, response.status_code
    assert "MB" in response.json()["message"]


@pytest.mark.parametrize("path", ["/api/v1/import/preview", "/api/v1/import/commit-file"])
def test_empty_upload_is_rejected(client: TestClient, admin: dict[str, str], path: str) -> None:
    response = client.post(
        path, files={"file": ("empty.md", io.BytesIO(b""), "text/markdown")}, headers=admin
    )
    assert response.status_code == 422


# --------------------------------------------------------------------------- #
# 3. 对抗性输入
# --------------------------------------------------------------------------- #
ADVERSARIAL = [
    "",                                  # 空
    " " * 5000,                          # 全空白
    "×" * 2000,                          # 纯符号
    "\x00\x01\x02",                      # 控制字符
    "🎉" * 500,                          # emoji
    "入库 " + "A" * 5000,                # 超长名称
    "采购\n" + "|名称|数量|\n" * 500,     # 残缺表格
    "{%s}" % ("x" * 1000),               # 类 JSON 垃圾
    "🙂" ,                               # 单个 emoji
    "\u202e倒序覆盖",                     # 双向控制字符
    "入库 -1",                           # 负数数量
    "入库 0",                            # 零
    "出库 " + "1" * 100,                 # 巨大数字
    "删除全部" + "!" * 500,              # 危险指令+噪声
]


@pytest.mark.parametrize("text", ADVERSARIAL)
def test_adversarial_bot_input_never_500(app, text: str) -> None:
    """任何畸形输入都只能得到一条回复，不能抛异常。"""
    router = app.state.ctx.commands
    reply = asyncio.run(
        router.handle(text, operator="fuzz", scene="qq-c2c", conversation="fuzz")
    ).reply
    assert isinstance(reply, str) and reply


@pytest.mark.parametrize(
    "filename,payload",
    [
        ("x.md", b"\x00\x01\x02\x03 binary junk"),
        ("x.xlsx", b"not really an xlsx"),
        ("x.json", b"{ broken json"),
        ("x.csv", "名称,数量\n".encode() + b"\xff\xfe bad bytes"),
        ("", "| 名称 | 数量 |\n| --- | --- |\n| A | 1 |".encode()),
    ],
)
def test_garbage_uploads_do_not_crash(
    client: TestClient, admin: dict[str, str], filename: str, payload: bytes
) -> None:
    response = client.post(
        "/api/v1/import/preview",
        files={"file": (filename or "u", io.BytesIO(payload), "application/octet-stream")},
        headers=admin,
    )
    assert response.status_code < 500, response.text


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 10**9},
        {"limit": -1},
        {"offset": -5},
        {"keyword": "%" * 100},
        {"keyword": "_" * 100},
        {"category": "' OR 1=1 --"},
    ],
)
def test_extreme_query_params_are_safe(
    client: TestClient, admin: dict[str, str], seeded: list[dict], params: dict
) -> None:
    """参数边界与 SQL 特殊字符不能让接口 500 或越权返回。"""
    response = client.get("/api/v1/items", params=params, headers=admin)
    assert response.status_code < 500, response.text
    if response.status_code == 200:
        assert isinstance(response.json()["items"], list)


def test_sql_metacharacters_do_not_inject(
    client: TestClient, admin: dict[str, str], seeded: list[dict]
) -> None:
    """注入尝试要么查不到，要么被参数化挡住 —— 绝不能返回全部或报 500。"""
    for keyword in ["' OR 1=1 --", "'; DROP TABLE items; --", "%' UNION SELECT 1 --"]:
        response = client.get("/api/v1/items", params={"keyword": keyword}, headers=admin)
        assert response.status_code == 200, response.text
        assert response.json()["total"] == 0
    # 表还在
    assert client.get("/api/v1/items", headers=admin).json()["total"] == 3


# --------------------------------------------------------------------------- #
# 4. 缓存失效（性能优化引入的检索语料缓存的正确性）
# --------------------------------------------------------------------------- #
def test_search_cache_invalidates_on_create(app) -> None:
    inventory = app.state.ctx.inventory

    assert inventory.search("缓存测试品").total == 0
    inventory.create_item(ItemCreate(name="缓存测试品", quantity=5, location="A柜"))
    assert inventory.search("缓存测试品").total == 1


def test_search_cache_invalidates_on_update(app) -> None:
    inventory = app.state.ctx.inventory
    record = inventory.create_item(ItemCreate(name="旧名字", quantity=5, location="A柜"))
    assert inventory.search("全新名字").total == 0

    inventory.update_item(record.id, ItemUpdate(name="全新名字", operator="t"))
    assert inventory.search("全新名字").total == 1
    assert inventory.search("旧名字").total == 0


def test_search_cache_invalidates_on_delete(app) -> None:
    inventory = app.state.ctx.inventory
    record = inventory.create_item(ItemCreate(name="待删物品", quantity=5, location="A柜"))
    assert inventory.search("待删物品").total == 1
    inventory.delete_item(record.id, operator="t")
    assert inventory.search("待删物品").total == 0


def test_search_cache_invalidates_on_alias_change(app) -> None:
    inventory = app.state.ctx.inventory
    record = inventory.create_item(ItemCreate(name="有别名", quantity=5, location="A柜"))
    assert inventory.search("新别名ABC").total == 0
    inventory.update_item(record.id, ItemUpdate(aliases=["新别名ABC"], operator="t"))
    assert inventory.search("新别名ABC").total >= 1


# --------------------------------------------------------------------------- #
# 5. 并发
# --------------------------------------------------------------------------- #
def test_concurrent_stock_in_does_not_lose_updates(app) -> None:
    """并发入库同一物品：数量必须等于总和，不能丢更新。"""
    inventory = app.state.ctx.inventory
    inventory.create_item(ItemCreate(name="并发物品", quantity=0, location="A柜"))

    errors: list[Exception] = []

    def worker() -> None:
        try:
            for _ in range(5):
                inventory.change_stock(
                    StockChangeRequest(
                        action="in", name="并发物品", quantity=1, location="A柜", operator="w"
                    )
                )
        except Exception as exc:  # noqa: BLE001 - 收集起来统一断言
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, errors
    record = next(r for r in inventory.repo.all_items() if r.name == "并发物品")
    assert record.quantity == 30.0        # 6 线程 × 5 次


def test_concurrent_api_writes_are_serialized(client: TestClient, admin: dict[str, str]) -> None:
    """并发 HTTP 写入不能出现 500（SQLite 写锁要正确排队）。"""
    def create(index: int) -> int:
        response = client.post(
            "/api/v1/items",
            json={"name": f"并发{index}", "quantity": 1, "location": "Z柜"},
            headers=admin,
        )
        return response.status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(create, range(24)))
    assert all(code == 201 for code in codes), codes
    assert client.get("/api/v1/items", headers=admin).json()["total"] == 24


def test_session_store_is_bounded(app) -> None:
    """会话表必须有上限，否则长期运行会内存泄漏。"""
    router = app.state.ctx.commands
    for index in range(1200):
        asyncio.run(
            router.handle("帮助", operator="u", scene="qq-c2c", conversation=f"conv-{index}")
        )
    sessions = getattr(router.sessions, "_sessions", None)
    if sessions is not None:
        assert len(sessions) <= 1000, len(sessions)


# --------------------------------------------------------------------------- #
# 6. 首次启动的引导 Key 不能是可猜的默认口令
# --------------------------------------------------------------------------- #
def _bootstrapped_repo(tmp_path, value: str):
    """启动一次应用（lifespan 里才建引导 Key），返回 repo。"""
    from fastapi.testclient import TestClient

    from app.config import Settings
    from app.main import create_app

    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_path=str(tmp_path / "boot.db"),
        data_dir=str(tmp_path),
        bootstrap_api_key=value,
        llm_enabled=False,
        qq_bot_enabled=False,
    )
    app = create_app(settings)
    with TestClient(app):
        return app.state.ctx.repo


def test_bootstrap_placeholder_is_replaced(tmp_path) -> None:
    """照 .env.example 抄下来的占位符绝不能被当成真 Key —— 那等于没有防护。"""
    repo = _bootstrapped_repo(tmp_path, "dev-admin-key-change-me")
    assert repo.get_api_key("dev-admin-key-change-me") is None, "占位符被当成了真 Key"
    assert repo.count_api_keys() == 1, "应改为自动生成一个管理员 Key"


def test_bootstrap_empty_generates_key(tmp_path) -> None:
    """留空 → 自动生成一个（而不是不建 Key 导致根本进不去）。"""
    assert _bootstrapped_repo(tmp_path, "").count_api_keys() == 1


def test_bootstrap_explicit_key_is_honored(tmp_path) -> None:
    """用户自己填的长 Key 要照用。"""
    repo = _bootstrapped_repo(tmp_path, "a-very-long-self-chosen-key-123456")
    assert repo.get_api_key("a-very-long-self-chosen-key-123456") is not None


# --------------------------------------------------------------------------- #
# 7. 错误响应不泄露内部信息
# --------------------------------------------------------------------------- #
def test_alias_view_actually_lists_aliases(app) -> None:
    """``别名 <名称>`` 要真的列出别名 —— 以前只回一句用法提示。"""
    app.state.ctx.inventory.create_item(
        ItemCreate(name="NE555", quantity=30, location="C柜", aliases=["555定时器"])
    )
    reply = asyncio.run(
        app.state.ctx.commands.handle(
            "别名 NE555", operator="t", scene="qq-c2c", conversation="alias"
        )
    ).reply
    assert "555定时器" in reply
    assert "用法" not in reply


def test_error_payload_has_no_internals(client: TestClient, admin: dict[str, str]) -> None:
    response = client.get("/api/v1/items/999999", headers=admin)
    assert response.status_code == 404
    body = json.dumps(response.json(), ensure_ascii=False)
    for leak in ("Traceback", "sqlite3", "/home/", "SELECT ", "WarehouseError"):
        assert leak not in body, body
