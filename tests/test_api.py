"""HTTP 接口端到端测试。"""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient


# --------------------------------------------------------------------------- #
# 基础与鉴权
# --------------------------------------------------------------------------- #
def test_health_needs_no_key(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"

    root = client.get("/")
    assert root.status_code == 200
    assert root.json()["service"] == "DocuDot"


def test_missing_key_is_401(client: TestClient) -> None:
    response = client.get("/api/v1/items")
    assert response.status_code == 401
    assert response.json()["error"] == "unauthorized"


def test_wrong_key_is_401(client: TestClient) -> None:
    response = client.get("/api/v1/items", headers={"X-API-Key": "nope"})
    assert response.status_code == 401


def test_bearer_header_also_works(client: TestClient, admin: dict[str, str]) -> None:
    key = admin["X-API-Key"]
    response = client.get("/api/v1/items", headers={"Authorization": f"Bearer {key}"})
    assert response.status_code == 200


def test_read_key_cannot_write(client: TestClient, reader: dict[str, str]) -> None:
    response = client.post(
        "/api/v1/items",
        json={"name": "X", "quantity": 1},
        headers=reader,
    )
    assert response.status_code == 403
    assert response.json()["error"] == "forbidden"


def test_read_key_cannot_manage_keys(client: TestClient, reader: dict[str, str]) -> None:
    assert client.get("/api/v1/admin/keys", headers=reader).status_code == 403


# --------------------------------------------------------------------------- #
# 物品 CRUD
# --------------------------------------------------------------------------- #
def test_create_and_get_item(client: TestClient, admin: dict[str, str]) -> None:
    response = client.post(
        "/api/v1/items",
        json={
            "name": "STM32F103C8T6",
            "category": "stm_component",
            "quantity": 25,
            "location": "A柜-1层-盒3",
            "spec": "LQFP48",
            "aliases": ["F103C8", "STM32F103"],
            "operator": "tester",
        },
        headers=admin,
    )
    assert response.status_code == 201, response.text
    item = response.json()
    assert item["quantity"] == 25
    assert item["aliases"] == ["F103C8", "STM32F103"]
    assert item["category"] == "STM元器件"

    fetched = client.get(f"/api/v1/items/{item['id']}", headers=admin).json()
    assert fetched["name"] == "STM32F103C8T6"


def test_duplicate_item_rejected(client: TestClient, admin: dict[str, str], seeded: list[dict]) -> None:
    response = client.post(
        "/api/v1/items",
        json={"name": "STM32F103C8T6", "quantity": 1, "location": "A柜-1层-盒3", "spec": "LQFP48"},
        headers=admin,
    )
    assert response.status_code == 422
    assert "已存在" in response.json()["message"]


def test_list_and_filter(client: TestClient, admin: dict[str, str], seeded: list[dict]) -> None:
    body = client.get("/api/v1/items", headers=admin).json()
    assert body["total"] == 3

    only_cards = client.get("/api/v1/items", params={"category": "Steam游戏卡"}, headers=admin).json()
    assert only_cards["total"] == 1
    assert only_cards["items"][0]["name"] == "Steam 50元充值卡"

    by_location = client.get("/api/v1/items", params={"location": "A柜-2层-盒1"}, headers=admin).json()
    assert by_location["total"] == 1


def test_update_and_delete(client: TestClient, admin: dict[str, str], seeded: list[dict]) -> None:
    item_id = seeded[0]["id"]
    updated = client.patch(
        f"/api/v1/items/{item_id}",
        json={"location": "A柜-9层", "note": "挪柜了"},
        headers=admin,
    )
    assert updated.status_code == 200
    assert updated.json()["location"] == "A柜-9层"
    assert updated.json()["note"] == "挪柜了"

    removed = client.delete(f"/api/v1/items/{item_id}", headers=admin)
    assert removed.status_code == 200
    assert client.get(f"/api/v1/items/{item_id}", headers=admin).status_code == 404


def test_alias_add_and_remove(client: TestClient, admin: dict[str, str], seeded: list[dict]) -> None:
    item_id = seeded[0]["id"]
    added = client.post(f"/api/v1/items/{item_id}/aliases", json={"aliases": ["蓝药丸"]}, headers=admin)
    assert added.status_code == 200
    assert "蓝药丸" in added.json()["aliases"]

    removed = client.delete(f"/api/v1/items/{item_id}/aliases/蓝药丸", headers=admin)
    assert removed.status_code == 200
    assert "蓝药丸" not in removed.json()["aliases"]


# --------------------------------------------------------------------------- #
# 出入库
# --------------------------------------------------------------------------- #
def test_stock_in_out_flow(client: TestClient, admin: dict[str, str], seeded: list[dict]) -> None:
    response = client.post(
        "/api/v1/stock/in",
        json={"name": "STM32F103C8T6", "quantity": 5, "operator": "u1"},
        headers=admin,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["quantity_before"] == 25
    assert body["quantity_after"] == 30
    assert body["delta"] == 5
    assert body["matched_by"] == "exact-name"

    response = client.post(
        "/api/v1/stock/out",
        json={"name": "F103C8", "quantity": 10, "operator": "u1"},
        headers=admin,
    )
    assert response.json()["quantity_after"] == 20
    assert response.json()["matched_by"] == "exact-alias"


def test_stock_out_insufficient(client: TestClient, admin: dict[str, str], seeded: list[dict]) -> None:
    response = client.post(
        "/api/v1/stock/out",
        json={"name": "STM32F103C8T6", "quantity": 9999},
        headers=admin,
    )
    assert response.status_code == 422
    assert "库存不足" in response.json()["message"]


def test_stock_set(client: TestClient, admin: dict[str, str], seeded: list[dict]) -> None:
    response = client.post(
        "/api/v1/stock/set",
        json={"name": "0.1uF 50V MLCC", "quantity": 480, "operator": "盘点员"},
        headers=admin,
    )
    assert response.status_code == 200
    assert response.json()["quantity_after"] == 480


def test_stock_in_auto_create(client: TestClient, admin: dict[str, str]) -> None:
    response = client.post(
        "/api/v1/stock/change",
        json={
            "action": "in",
            "name": "1N4148",
            "quantity": 100,
            "location": "C柜-1层",
            "auto_create": True,
            "operator": "u2",
        },
        headers=admin,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["created"] is True
    assert body["quantity_after"] == 100
    assert body["item"]["category"] == "未分类"  # 没有内置分类了，等 AI 归类


def test_stock_in_without_auto_create_fails(client: TestClient, admin: dict[str, str]) -> None:
    response = client.post(
        "/api/v1/stock/change",
        json={"action": "in", "name": "不存在的东西", "quantity": 1},
        headers=admin,
    )
    assert response.status_code == 404


def test_movements_recorded(client: TestClient, admin: dict[str, str], seeded: list[dict]) -> None:
    client.post("/api/v1/stock/in", json={"name": "STM32F103C8T6", "quantity": 5}, headers=admin)
    body = client.get("/api/v1/stock/movements", headers=admin).json()
    assert body["total"] >= 4  # 3 条 create + 1 条 in
    assert body["movements"][0]["action"] == "in"


def test_ambiguous_requires_disambiguation(client: TestClient, admin: dict[str, str]) -> None:
    for location in ("D柜-1层", "D柜-2层"):
        client.post(
            "/api/v1/items",
            json={"name": "AMS1117-3.3", "quantity": 10, "location": location},
            headers=admin,
        )
    response = client.post("/api/v1/stock/out", json={"name": "AMS1117-3.3", "quantity": 1}, headers=admin)
    assert response.status_code == 409
    assert response.json()["error"] == "ambiguous"
    assert len(response.json()["detail"]["candidates"]) == 2

    forced = client.post(
        "/api/v1/stock/out",
        json={"name": "AMS1117-3.3", "quantity": 1, "allow_ambiguous": True},
        headers=admin,
    )
    assert forced.status_code == 200


# --------------------------------------------------------------------------- #
# 检索
# --------------------------------------------------------------------------- #
def test_search_fuzzy_equivalence(client: TestClient, admin: dict[str, str], seeded: list[dict]) -> None:
    body = client.get("/api/v1/search", params={"q": "100nF"}, headers=admin).json()
    assert body["total"] == 1
    assert body["hits"][0]["name"] == "0.1uF 50V MLCC"

    body = client.get("/api/v1/search", params={"q": "电容"}, headers=admin).json()
    assert any(hit["name"] == "0.1uF 50V MLCC" for hit in body["hits"])

    body = client.get("/api/v1/search", params={"q": "游戏卡"}, headers=admin).json()
    assert [hit["name"] for hit in body["hits"]] == ["Steam 50元充值卡"]


def test_search_match_batch(client: TestClient, admin: dict[str, str], seeded: list[dict]) -> None:
    body = client.post(
        "/api/v1/search/match",
        json={"queries": ["F103C8", "不存在的料号"]},
        headers=admin,
    ).json()
    assert body["results"][0]["matched"] is True
    assert body["results"][0]["item"]["name"] == "STM32F103C8T6"
    assert body["results"][1]["matched"] is False


# --------------------------------------------------------------------------- #
# 统计
# --------------------------------------------------------------------------- #
def test_analysis_endpoints(client: TestClient, admin: dict[str, str], seeded: list[dict]) -> None:
    overview = client.get("/api/v1/analysis/overview", headers=admin).json()
    assert overview["item_count"] == 3
    assert overview["total_quantity"] == 535
    assert overview["location_count"] == 3

    categories = client.get("/api/v1/analysis/categories", headers=admin).json()
    labels = {row["category"]: row["item_count"] for row in categories}
    assert labels["STM元器件"] == 2
    assert labels["Steam游戏卡"] == 1

    locations = client.get("/api/v1/analysis/locations", headers=admin).json()
    assert len(locations) == 3

    low = client.get("/api/v1/analysis/low-stock", params={"threshold": 20}, headers=admin).json()
    assert [item["name"] for item in low] == ["Steam 50元充值卡"]


# --------------------------------------------------------------------------- #
# 自然语言问答
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("question", "expected_intent", "must_contain"),
    [
        ("总共多少种物品", "total", "共 3 种物品"),
        ("库存总览", "overview", "库存总览"),
        ("STM32 还有多少", "count", "25"),
        ("F103C8 放在哪", "where", "A柜-1层-盒3"),
        ("A柜-2层-盒1里有什么", "location_list", "0.1uF"),
        ("电容的别名", "alias", "104"),
        ("分类统计", "category_stats", "STM元器件"),
    ],
)
def test_nlq(
    client: TestClient,
    admin: dict[str, str],
    seeded: list[dict],
    question: str,
    expected_intent: str,
    must_contain: str,
) -> None:
    body = client.post("/api/v1/analysis/nlq", json={"question": question}, headers=admin).json()
    assert body["intent"] == expected_intent, body
    assert must_contain in body["answer"], body


def test_nlq_low_stock_uses_threshold(client: TestClient, admin: dict[str, str], seeded: list[dict]) -> None:
    body = client.post(
        "/api/v1/analysis/nlq",
        json={"question": "库存不足", "low_stock_threshold": 20},
        headers=admin,
    ).json()
    assert body["intent"] == "low_stock"
    assert "Steam 50元充值卡" in body["answer"]


def test_ai_analysis_without_key_is_502(client: TestClient, admin: dict[str, str], seeded: list[dict]) -> None:
    response = client.post("/api/v1/analysis/ai", json={"question": "分析一下"}, headers=admin)
    assert response.status_code == 502
    assert "大模型未配置" in response.json()["message"]


# --------------------------------------------------------------------------- #
# 导入 / 导出
# --------------------------------------------------------------------------- #
MARKDOWN = """\
# 库存清单

## STM元器件

| 名称 | 数量 | 位置 | 规格 | 别名 | 备注 |
| --- | --- | --- | --- | --- | --- |
| STM32F103C8T6 | 25 | A柜-1层-盒3 | LQFP48 | F103C8, STM32F103 | 蓝药丸 |
| 0.1uF 50V MLCC | 500 | A柜-2层-盒1 | 0805 | 104, 100nF | |

## Steam游戏卡

| 名称 | 数量 | 位置 | 规格 | 别名 | 备注 |
| --- | --- | --- | --- | --- | --- |
| Steam 50元充值卡 | 10 | B柜-抽屉1 | 50元 | 50元卡 | |
"""


def test_import_preview_and_commit(client: TestClient, admin: dict[str, str]) -> None:
    preview = client.post(
        "/api/v1/import/preview-text",
        params={"filename": "stock.md", "content": MARKDOWN},
        headers=admin,
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["total"] == 3
    assert body["rows"][0]["name"] == "STM32F103C8T6"
    assert body["rows"][0]["category"] == "STM元器件"
    assert body["rows"][2]["category"] == "Steam游戏卡"

    result = client.post(
        "/api/v1/import/commit",
        json={"filename": "stock.md", "content": MARKDOWN, "mode": "merge", "operator": "importer"},
        headers=admin,
    )
    assert result.status_code == 200, result.text
    assert result.json()["created"] == 3

    items = client.get("/api/v1/items", headers=admin).json()
    assert items["total"] == 3


def test_import_merge_updates_quantity(client: TestClient, admin: dict[str, str]) -> None:
    client.post(
        "/api/v1/import/commit",
        json={"filename": "a.md", "content": MARKDOWN, "mode": "merge"},
        headers=admin,
    )
    changed = MARKDOWN.replace("| 25 |", "| 40 |")
    result = client.post(
        "/api/v1/import/commit",
        json={"filename": "b.md", "content": changed, "mode": "merge"},
        headers=admin,
    ).json()
    assert result["created"] == 0
    assert result["updated"] == 3
    item = client.get("/api/v1/search", params={"q": "STM32F103C8T6"}, headers=admin).json()["hits"][0]
    assert item["quantity"] == 40


def test_import_upload_excel(client: TestClient, admin: dict[str, str]) -> None:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "元件"
    sheet.append(["名称", "数量", "位置", "封装", "别名"])
    sheet.append(["NE555", 30, "C柜-1层", "DIP-8", "555"])
    sheet.append(["LM358 双运放", 12, "C柜-2层", "SOIC-8", "LM358"])
    buffer = io.BytesIO()
    workbook.save(buffer)
    buffer.seek(0)

    response = client.post(
        "/api/v1/import/commit-file",
        files={"file": ("stock.xlsx", buffer.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={"mode": "merge"},
        headers=admin,
    )
    assert response.status_code == 200, response.text
    assert response.json()["created"] == 2
    search = client.get("/api/v1/search", params={"q": "555"}, headers=admin).json()
    assert search["hits"][0]["name"] == "NE555"


def test_import_dry_run_changes_nothing(client: TestClient, admin: dict[str, str]) -> None:
    result = client.post(
        "/api/v1/import/commit",
        json={"filename": "dry.md", "content": MARKDOWN, "dry_run": True},
        headers=admin,
    ).json()
    assert result["status"] == "dry-run"
    assert client.get("/api/v1/items", headers=admin).json()["total"] == 0


def test_import_template(client: TestClient, admin: dict[str, str]) -> None:
    body = client.get("/api/v1/import/template", headers=admin).json()
    assert "markdown_template" in body
    assert "STM元器件" in body["markdown_template"]
    assert len(body["usage"]) == 3


def test_export_markdown_roundtrip(client: TestClient, admin: dict[str, str], seeded: list[dict]) -> None:
    markdown = client.get("/api/v1/export/markdown", headers=admin).text
    assert "| 名称 | 数量 | 位置 |" in markdown
    assert "STM32F103C8T6" in markdown
    assert "## Steam游戏卡" in markdown

    preview = client.post(
        "/api/v1/import/preview-text",
        params={"filename": "roundtrip.md", "content": markdown},
        headers=admin,
    ).json()
    assert preview["total"] == 3


def test_export_xlsx(client: TestClient, admin: dict[str, str], seeded: list[dict]) -> None:
    response = client.get("/api/v1/export/xlsx", headers=admin)
    assert response.status_code == 200
    assert response.content[:2] == b"PK"


# --------------------------------------------------------------------------- #
# 机器人命令
# --------------------------------------------------------------------------- #
def test_bot_command_help_and_stock(client: TestClient, admin: dict[str, str]) -> None:
    help_body = client.post("/api/v1/bot/command", json={"text": "帮助"}, headers=admin).json()
    assert help_body["command"] == "help"
    assert "入库" in help_body["reply"]

    created = client.post(
        "/api/v1/bot/command",
        json={"text": "入库 STM32F103C8T6 25 @A柜-1层-盒3 #F103C8", "operator": "qq:10001", "scene": "qq-group"},
        headers=admin,
    ).json()
    assert created["handled"] is True
    assert "入库成功" in created["reply"]

    queried = client.post(
        "/api/v1/bot/command", json={"text": "STM32 还有多少"}, headers=admin
    ).json()
    assert "25" in queried["reply"]

    out = client.post(
        "/api/v1/bot/command", json={"text": "出库 F103C8 5"}, headers=admin
    ).json()
    assert "20" in out["reply"]


def test_bot_command_alias_and_location(client: TestClient, admin: dict[str, str], seeded: list[dict]) -> None:
    alias = client.post(
        "/api/v1/bot/command", json={"text": "别名 STM32F103C8T6 +蓝药丸"}, headers=admin
    ).json()
    assert "蓝药丸" in alias["reply"]

    location = client.post(
        "/api/v1/bot/command", json={"text": "位置 A柜-2层-盒1"}, headers=admin
    ).json()
    assert "0.1uF" in location["reply"]


# --------------------------------------------------------------------------- #
# 管理
# --------------------------------------------------------------------------- #
def test_api_key_lifecycle(client: TestClient, admin: dict[str, str]) -> None:
    created = client.post(
        "/api/v1/admin/keys",
        json={"label": "ci", "scopes": ["read", "analyze"]},
        headers=admin,
    )
    assert created.status_code == 201
    body = created.json()
    assert body["key"].startswith("whk_")

    new_headers = {"X-API-Key": body["key"]}
    assert client.get("/api/v1/items", headers=new_headers).status_code == 200

    listed = client.get("/api/v1/admin/keys", headers=admin).json()
    assert len(listed) >= 2

    assert client.delete(f"/api/v1/admin/keys/{body['id']}", headers=admin).status_code == 200
    assert client.get("/api/v1/items", headers=new_headers).status_code == 401


def test_audit_log(client: TestClient, admin: dict[str, str], seeded: list[dict]) -> None:
    body = client.get("/api/v1/admin/audit", headers=admin).json()
    assert body["total"] >= 3
    assert any(row["action"] == "item.create" for row in body["logs"])


def test_status_endpoint(client: TestClient, admin: dict[str, str]) -> None:
    body = client.get("/api/v1/admin/status", headers=admin).json()
    assert body["version"]
    assert body["database"]


def test_validation_error_shape(client: TestClient, admin: dict[str, str]) -> None:
    response = client.post("/api/v1/items", json={"name": ""}, headers=admin)
    assert response.status_code == 422
    assert response.json()["error"] == "validation_failed"
