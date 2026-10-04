"""物品、出入库、检索相关路由。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from ..api.deps import AppContext, AuthInfo, get_context, require
from ..services.categorize import category_label
from ..models import (
    ItemCreate,
    ItemListOut,
    ItemOut,
    ItemUpdate,
    MatchItem,
    MatchRequest,
    MatchResponse,
    MessageOut,
    MovementOut,
    SearchResponse,
    StockAction,
    StockChangeRequest,
    StockChangeResult,
)
from ..models import APIModel
from pydantic import Field

router = APIRouter(prefix="/api/v1", tags=["inventory"])


class AliasPayload(APIModel):
    aliases: list[str] = Field(..., min_length=1, description="要添加的别名")
    operator: str = ""


class SimpleStockRequest(APIModel):
    name: str = Field(..., min_length=1)
    quantity: float = Field(..., ge=0)
    location: str | None = None
    spec: str | None = None
    category: str | None = None
    aliases: list[str] = Field(default_factory=list)
    operator: str = ""
    note: str = ""
    raw_text: str = ""
    auto_create: bool = False
    allow_ambiguous: bool = False
    source: str = "api"


# --------------------------------------------------------------------------- #
# 元数据
# --------------------------------------------------------------------------- #
@router.get("/meta", summary="分类 / 位置 / 规模概览")
async def get_meta(request: Request, _: AuthInfo = Depends(require("read"))) -> dict:
    context = get_context(request)
    counts = context.repo.overview_counts()
    locations = [
        {
            "location": row["location"] or "（未指定位置）",
            "item_count": int(row["item_count"]),
            "total_quantity": float(row["total_quantity"]),
        }
        for row in context.repo.location_stats(limit=200)
    ]
    return {
        # 分类不预设 —— 库里有什么分类就返回什么（由 AI / 用户创建）
        "categories": [
            {"value": row["category"], "label": category_label(row["category"])}
            for row in context.repo.category_stats()
            if row["category"]
        ],
        "locations": locations,
        "counts": counts,
        "fuzzy_threshold": context.settings.fuzzy_threshold,
        "llm_ready": context.settings.llm_ready,
        "qq_bot_enabled": context.settings.qq_bot_enabled,
    }


# --------------------------------------------------------------------------- #
# 物品
# --------------------------------------------------------------------------- #
@router.get("/items", response_model=ItemListOut, summary="列出库存（支持过滤与分页）")
async def list_items(
    request: Request,
    _: AuthInfo = Depends(require("read")),
    category: str | None = Query(None),
    location: str | None = Query(None, description="精确匹配位置（已做归一化）"),
    keyword: str | None = Query(None, description="名称/位置/规格/备注 的子串过滤"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> ItemListOut:
    context = get_context(request)
    total, records = context.inventory.list_items(
        category=category,
        location=location,
        keyword=keyword,
        limit=limit,
        offset=offset,
    )
    return ItemListOut(total=total, items=[record.to_out() for record in records])


@router.post("/items", response_model=ItemOut, status_code=201, summary="新建库存条目")
async def create_item(
    request: Request, payload: ItemCreate, _: AuthInfo = Depends(require("write"))
) -> ItemOut:
    context = get_context(request)
    record = context.inventory.create_item(payload)
    return record.to_out()


@router.get("/items/{item_id}", response_model=ItemOut, summary="查看单个条目")
async def get_item(
    request: Request, item_id: int, _: AuthInfo = Depends(require("read"))
) -> ItemOut:
    return get_context(request).inventory.get_item(item_id).to_out()


@router.patch("/items/{item_id}", response_model=ItemOut, summary="修改条目")
async def update_item(
    request: Request,
    item_id: int,
    payload: ItemUpdate,
    _: AuthInfo = Depends(require("write")),
) -> ItemOut:
    return get_context(request).inventory.update_item(item_id, payload).to_out()


@router.delete("/items/{item_id}", response_model=MessageOut, summary="删除条目（需 admin）")
async def delete_item(
    request: Request, item_id: int, auth: AuthInfo = Depends(require("admin"))
) -> MessageOut:
    context = get_context(request)
    context.inventory.delete_item(item_id, operator=auth.label or "admin")
    return MessageOut(message=f"已删除 #{item_id}")


# --------------------------------------------------------------------------- #
# 别名
# --------------------------------------------------------------------------- #
@router.get("/items/{item_id}/aliases", summary="查看别名")
async def list_aliases(
    request: Request, item_id: int, _: AuthInfo = Depends(require("read"))
) -> dict:
    context = get_context(request)
    record = context.inventory.get_item(item_id)
    return {"item_id": item_id, "name": record.name, "aliases": record.aliases}


@router.post("/items/{item_id}/aliases", response_model=ItemOut, summary="添加别名")
async def add_aliases(
    request: Request,
    item_id: int,
    payload: AliasPayload,
    _: AuthInfo = Depends(require("write")),
) -> ItemOut:
    context = get_context(request)
    record = context.inventory.add_aliases(item_id, payload.aliases, operator=payload.operator)
    return record.to_out()


@router.delete("/items/{item_id}/aliases/{alias:path}", response_model=ItemOut, summary="删除别名")
async def remove_alias(
    request: Request,
    item_id: int,
    alias: str,
    operator: str = Query(""),
    _: AuthInfo = Depends(require("write")),
) -> ItemOut:
    context = get_context(request)
    record = context.inventory.remove_alias(item_id, alias, operator=operator)
    return record.to_out()


# --------------------------------------------------------------------------- #
# 出入库
# --------------------------------------------------------------------------- #
@router.post("/stock/change", response_model=StockChangeResult, summary="统一库存变更入口")
async def change_stock(
    request: Request, payload: StockChangeRequest, _: AuthInfo = Depends(require("write"))
) -> StockChangeResult:
    return get_context(request).inventory.change_stock(payload)


def _simple(payload: SimpleStockRequest, action: StockAction) -> StockChangeRequest:
    return StockChangeRequest(
        action=action,
        name=payload.name,
        quantity=payload.quantity,
        location=payload.location,
        spec=payload.spec,
        category=payload.category,
        aliases=payload.aliases,
        operator=payload.operator,
        note=payload.note,
        raw_text=payload.raw_text,
        auto_create=payload.auto_create if action is not StockAction.SET else False,
        allow_ambiguous=payload.allow_ambiguous,
        source=payload.source if payload.source in {"api", "qq", "import", "cli"} else "api",  # type: ignore[arg-type]
    )


@router.post("/stock/in", response_model=StockChangeResult, summary="入库")
async def stock_in(
    request: Request, payload: SimpleStockRequest, _: AuthInfo = Depends(require("write"))
) -> StockChangeResult:
    return get_context(request).inventory.change_stock(_simple(payload, StockAction.IN))


@router.post("/stock/out", response_model=StockChangeResult, summary="出库")
async def stock_out(
    request: Request, payload: SimpleStockRequest, _: AuthInfo = Depends(require("write"))
) -> StockChangeResult:
    return get_context(request).inventory.change_stock(_simple(payload, StockAction.OUT))


@router.post("/stock/set", response_model=StockChangeResult, summary="盘点（直接设定数量）")
async def stock_set(
    request: Request, payload: SimpleStockRequest, _: AuthInfo = Depends(require("write"))
) -> StockChangeResult:
    return get_context(request).inventory.change_stock(_simple(payload, StockAction.SET))


@router.get("/stock/movements", summary="出入库流水")
async def list_movements(
    request: Request,
    _: AuthInfo = Depends(require("read")),
    item_id: int | None = Query(None),
    operator: str | None = Query(None),
    source: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict:
    context = get_context(request)
    total, movements = context.inventory.movements(
        item_id=item_id, operator=operator, source=source, limit=limit, offset=offset
    )
    return {"total": total, "movements": [m.model_dump() for m in movements]}


# --------------------------------------------------------------------------- #
# 检索
# --------------------------------------------------------------------------- #
@router.get("/search", response_model=SearchResponse, summary="模糊检索")
async def search(
    request: Request,
    _: AuthInfo = Depends(require("read")),
    q: str = Query(..., min_length=1, description="查询词，支持 0.1uF/100nF、4R7、电容 等写法"),
    limit: int = Query(10, ge=1, le=100),
    threshold: float | None = Query(None, ge=0, le=1),
    category: str | None = Query(None),
    any_mode: bool = Query(False, description="true=任一关键词命中即可（默认 AND）"),
) -> SearchResponse:
    context = get_context(request)
    return context.inventory.search(
        q,
        limit=limit,
        threshold=threshold,
        category=category,
        any_mode=any_mode,
    )


@router.post("/search/match", response_model=MatchResponse, summary="批量解析名称 → 唯一物品")
async def match(
    request: Request, payload: MatchRequest, _: AuthInfo = Depends(require("read"))
) -> MatchResponse:
    context = get_context(request)
    results: list[MatchItem] = []
    for query in payload.queries:
        record, score, candidates, matched_by = context.inventory.resolve(
            query, threshold=payload.threshold, allow_ambiguous=True
        )
        if record is None:
            response = context.inventory.search(query, limit=payload.limit, threshold=payload.threshold)
            results.append(MatchItem(query=query, matched=False, candidates=response.hits))
            continue
        results.append(
            MatchItem(
                query=query,
                matched=True,
                item={
                    "id": record.id,
                    "name": record.name,
                    "score": score or 1.0,
                    "quantity": record.quantity,
                    "location": record.location,
                    "spec": record.spec,
                    "aliases": record.aliases,
                    "reasons": [f"matched_by={matched_by}"],
                },
                candidates=candidates,
            )
        )
    return MatchResponse(results=results)
