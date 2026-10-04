"""统计报表、自然语言问答、大模型分析路由。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from ..api.deps import AuthInfo, get_context, require
from ..errors import PermissionDenied
from ..models import (
    AIAnalysisRequest,
    AIAnalysisResponse,
    AIStockParse,
    AIStockParseRequest,
    CategoryStat,
    ItemOut,
    LocationStat,
    NLQueryRequest,
    NLQueryResponse,
    OverviewOut,
    TidyRequest,
    TidyResult,
)

router = APIRouter(prefix="/api/v1/analysis", tags=["analysis"])


@router.get("/overview", response_model=OverviewOut, summary="库存总览")
async def overview(
    request: Request,
    _: AuthInfo = Depends(require("read")),
    low_stock_threshold: float = Query(0, ge=0, description=">0 时附带低库存清单"),
    recent_limit: int = Query(10, ge=0, le=100),
) -> OverviewOut:
    context = get_context(request)
    return context.analysis.overview(low_stock_threshold=low_stock_threshold, recent_limit=recent_limit)


@router.get("/categories", response_model=list[CategoryStat], summary="按分类统计")
async def categories(request: Request, _: AuthInfo = Depends(require("read"))) -> list[CategoryStat]:
    return get_context(request).analysis.categories()


@router.get("/locations", response_model=list[LocationStat], summary="按存储位置统计")
async def locations(
    request: Request,
    _: AuthInfo = Depends(require("read")),
    limit: int = Query(50, ge=1, le=500),
) -> list[LocationStat]:
    return get_context(request).analysis.locations(limit=limit)


@router.get("/low-stock", response_model=list[ItemOut], summary="低库存清单")
async def low_stock(
    request: Request,
    _: AuthInfo = Depends(require("read")),
    threshold: float = Query(5, ge=0),
    limit: int = Query(50, ge=1, le=500),
) -> list[ItemOut]:
    return get_context(request).analysis.low_stock(threshold, limit=limit)


@router.post("/nlq", response_model=NLQueryResponse, summary="自然语言问答（规则优先）")
async def nlq(
    request: Request, payload: NLQueryRequest, _: AuthInfo = Depends(require("read"))
) -> NLQueryResponse:
    context = get_context(request)
    return await context.nlq.answer(
        payload.question, use_llm=payload.use_llm, low_stock_threshold=payload.low_stock_threshold
    )


@router.post("/ai", response_model=AIAnalysisResponse, summary="大模型分析（需 analyze 权限 + LLM Key）")
async def ai_analyze(
    request: Request, payload: AIAnalysisRequest, _: AuthInfo = Depends(require("analyze"))
) -> AIAnalysisResponse:
    context = get_context(request)
    return await context.ai.analyze(
        payload.question,
        category=payload.category,
        location=payload.location,
        include_items=payload.include_items,
    )


@router.post("/ai/parse-stock", response_model=AIStockParse, summary="用大模型解析一句库存变更描述")
async def ai_parse_stock(
    request: Request, payload: AIStockParseRequest, _: AuthInfo = Depends(require("analyze"))
) -> AIStockParse:
    """只做解析、不落库，方便机器人层或外部系统复用。"""
    return await get_context(request).ai.parse_stock(payload.text)


@router.post("/tidy", response_model=TidyResult, summary="AI 归类整理：先给建议，可选直接应用")
async def ai_tidy(
    request: Request, payload: TidyRequest, auth: AuthInfo = Depends(require("analyze"))
) -> TidyResult:
    """``apply=true`` 会真的改库 —— 所以那一步要求 ``write`` 权限。

    ``analyze`` 的语义是「只读 + 调大模型」，不能顺带写数据。
    """
    if payload.apply and not (auth.scopes & {"write", "admin"}):
        raise PermissionDenied("应用整理建议会修改库存，需要 write 权限；不带 apply 可只看建议")
    context = get_context(request)
    plan = await context.ai.propose_tidy()
    applied = context.ai.apply_tidy(plan, operator=payload.operator) if payload.apply else 0
    message = f"提出 {len(plan.changes)} 处建议" + ("，已应用" if payload.apply else "（未应用，确认后再调 apply=true）")
    return TidyResult(applied=applied, plan=plan, message=message)
