"""Pydantic 数据模型（API 契约）。"""

from __future__ import annotations

import math
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

from .core.categories import DEFAULT_CATEGORY

# --------------------------------------------------------------------------- #
# 枚举
# --------------------------------------------------------------------------- #


class StockAction(str, Enum):
    IN = "in"
    OUT = "out"
    SET = "set"


class Scope(str, Enum):
    READ = "read"
    WRITE = "write"
    ANALYZE = "analyze"
    ADMIN = "admin"


SCENE_LABELS = {"api": "API", "qq": "QQ", "import": "导入", "cli": "命令行", "system": "系统"}


# --------------------------------------------------------------------------- #
# 基础
# --------------------------------------------------------------------------- #


class APIModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, str_strip_whitespace=True)


def _clean_list(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = [v for v in values.replace("，", ",").split(",")]
    seen: set[str] = set()
    result: list[str] = []
    for item in values:
        text = str(item).strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _normalize_category(value: Any) -> str:
    """分类写法归一：接受内置别名、分类码，也接受 AI 新建的分类名。"""
    from .core.categories import normalize_category_code

    return normalize_category_code(value)


def _normalize_category_optional(value: Any) -> Any:
    if value is None:
        return None
    return _normalize_category(value)


class ItemBase(APIModel):
    name: str = Field(..., min_length=1, max_length=200, description="物品名称")
    category: str = Field(
        "未分类", description="分类名（中文即可）；没有内置分类，AI 会自行创建"
    )
    quantity: float = Field(0, ge=0, description="数量")
    unit: str = Field("", max_length=20, description="计量单位，如 个/张/片")
    location: str = Field("", max_length=200, description="存储位置")
    spec: str = Field("", max_length=200, description="封装 / 规格")
    note: str = Field("", max_length=1000, description="备注")

    _norm_category = field_validator("category", mode="before")(_normalize_category)

    @field_serializer("quantity")
    def _serialize_quantity(self, value: float) -> float | int:
        return int(round(value)) if math.isclose(value, round(value), abs_tol=1e-9) else value


class ItemCreate(ItemBase):
    aliases: list[str] = Field(default_factory=list, description="别名 / 标签，用于模糊匹配")
    operator: str = Field("", max_length=100, description="操作人（QQ 号 / 用户名）")

    _norm_aliases = field_validator("aliases", mode="before")(_clean_list)


class ItemUpdate(APIModel):
    name: str | None = Field(None, min_length=1, max_length=200)
    category: str | None = None
    location: str | None = Field(None, max_length=200)
    spec: str | None = Field(None, max_length=200)
    note: str | None = Field(None, max_length=1000)
    unit: str | None = Field(None, max_length=20)
    aliases: list[str] | None = Field(None, description="传了就整体替换")
    operator: str = ""

    _norm_aliases = field_validator("aliases", mode="before")(_clean_list)
    _norm_category = field_validator("category", mode="before")(_normalize_category_optional)


class ItemOut(ItemBase):
    id: int
    aliases: list[str] = Field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    created_by: str = ""
    updated_by: str = ""

    @property
    def category_label(self) -> str:
        from .core.categories import category_label

        return category_label(self.category)


class ItemListOut(APIModel):
    total: int
    items: list[ItemOut]


# --------------------------------------------------------------------------- #
# 出入库
# --------------------------------------------------------------------------- #


class StockChangeRequest(APIModel):
    """统一的库存变更入口。

    ``item_id`` 与 ``name`` 二选一；给 ``name`` 时走模糊匹配，
    匹配不到且 ``auto_create=True`` 则自动建档。
    """

    action: StockAction
    item_id: int | None = None
    name: str | None = Field(None, max_length=200, description="名称或别名，支持模糊匹配")
    quantity: float = Field(..., ge=0, description="入/出库数量或目标数量")
    location: str | None = Field(None, description="指定/覆盖存储位置")
    spec: str | None = None
    category: str | None = None
    aliases: list[str] = Field(default_factory=list)
    operator: str = ""
    source: Literal["api", "qq", "import", "cli"] = "api"
    note: str = ""
    raw_text: str = Field("", description="触发本次变更的原始消息，便于审计")
    auto_create: bool = Field(False, description="匹配不到时自动创建")
    allow_ambiguous: bool = Field(False, description="命中多个候选时是否取最高分")
    merge_location: bool | None = Field(
        None,
        description=(
            "入库已有物品且填了新位置时："
            "true=合并（把原记录的位置改成新位置）；false=分开（在新位置另建一条）；"
            "null=由服务端返回 409 location_conflict 让你先问用户"
        ),
    )
    threshold: float | None = Field(None, ge=0, le=1, description="覆盖默认模糊匹配阈值")

    _norm_aliases = field_validator("aliases", mode="before")(_clean_list)
    _norm_category = field_validator("category", mode="before")(_normalize_category_optional)


class SearchHitOut(APIModel):
    id: int
    name: str
    score: float
    quantity: float = 0
    location: str = ""
    spec: str = ""
    aliases: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)

    @field_serializer("quantity")
    def _serialize_quantity(self, value: float) -> float | int:
        return int(round(value)) if math.isclose(value, round(value), abs_tol=1e-9) else value


class StockChangeResult(APIModel):
    ok: bool
    action: str
    message: str
    item: ItemOut | None = None
    created: bool = False
    quantity_before: float = 0
    quantity_after: float = 0
    delta: float = 0
    matched_by: str | None = Field(None, description="id / exact / fuzzy / created")
    score: float | None = None
    candidates: list[SearchHitOut] = Field(default_factory=list)

    @field_serializer("quantity_before", "quantity_after", "delta")
    def _serialize_qty(self, value: float) -> float | int:
        return int(round(value)) if math.isclose(value, round(value), abs_tol=1e-9) else value


class MovementOut(APIModel):
    id: int
    item_id: int
    item_name: str
    action: str
    delta: float
    quantity_before: float
    quantity_after: float
    location: str = ""
    operator: str = ""
    source: str = "api"
    note: str = ""
    raw_text: str = ""
    created_at: str = ""

    @field_serializer("delta", "quantity_before", "quantity_after")
    def _serialize_qty(self, value: float) -> float | int:
        return int(round(value)) if math.isclose(value, round(value), abs_tol=1e-9) else value


# --------------------------------------------------------------------------- #
# 检索
# --------------------------------------------------------------------------- #


class SearchResponse(APIModel):
    query: str
    tokens: list[str] = Field(default_factory=list)
    hits: list[SearchHitOut] = Field(default_factory=list)
    total: int = 0
    threshold: float = 0.0


class MatchRequest(APIModel):
    queries: list[str] = Field(..., min_length=1)
    limit: int = Field(5, ge=1, le=50)
    threshold: float | None = Field(None, ge=0, le=1)


class MatchItem(APIModel):
    query: str
    matched: bool
    item: SearchHitOut | None = None
    candidates: list[SearchHitOut] = Field(default_factory=list)


class MatchResponse(APIModel):
    results: list[MatchItem] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# 统计 / 分析
# --------------------------------------------------------------------------- #


class CategoryStat(APIModel):
    category: str
    label: str
    item_count: int
    total_quantity: float
    distinct_locations: int


class LocationStat(APIModel):
    location: str
    item_count: int
    total_quantity: float
    categories: dict[str, int] = Field(default_factory=dict)


class OverviewOut(APIModel):
    generated_at: str
    item_count: int
    total_quantity: float
    location_count: int
    category_count: int
    alias_count: int
    movement_count: int
    categories: list[CategoryStat] = Field(default_factory=list)
    top_locations: list[LocationStat] = Field(default_factory=list)
    recent_movements: list[MovementOut] = Field(default_factory=list)
    low_stock: list[ItemOut] = Field(default_factory=list)


class NLQueryRequest(APIModel):
    question: str = Field(..., min_length=1, max_length=500)
    operator: str = ""
    use_llm: bool = Field(False, description="规则解析失败时是否兜底调用大模型")
    low_stock_threshold: float = Field(5, ge=0, description="问「库存不足」时使用的阈值")


class NLQueryResponse(APIModel):
    question: str
    intent: str
    answer: str
    data: dict[str, Any] = Field(default_factory=dict)
    used_llm: bool = False


class AIAnalysisRequest(APIModel):
    question: str = Field("请分析当前库存状况并给出建议", max_length=1000)
    category: str | None = None
    location: str | None = None
    include_items: bool = True

    _norm_category = field_validator("category", mode="before")(_normalize_category_optional)


# --------------------------------------------------------------------------- #
# AI 解析 / 整理
# --------------------------------------------------------------------------- #


class AIStockParse(APIModel):
    """大模型从自由文本里解析出的库存变更意图。"""

    action: Literal["in", "out", "set", "unknown"] = "unknown"
    name: str = ""
    quantity: float = 0
    location: str = ""
    spec: str = ""
    category: str = "other"
    aliases: list[str] = Field(default_factory=list)
    confidence: float = Field(0, ge=0, le=1)
    reason: str = ""
    model: str = ""
    used_llm: bool = True

    _norm_category = field_validator("category", mode="before")(_normalize_category)
    _norm_aliases = field_validator("aliases", mode="before")(_clean_list)


class TidyChange(APIModel):
    """「整理」对单条物品提出的修改建议。"""

    item_id: int
    name: str
    before: dict[str, Any] = Field(default_factory=dict)
    after: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""


class TidyPlan(APIModel):
    summary: str = ""
    changes: list[TidyChange] = Field(default_factory=list)
    duplicates: list[list[int]] = Field(default_factory=list)
    model: str = ""


class MergeResult(APIModel):
    """把若干条记录并进一条的结果。"""

    target_id: int
    target_name: str
    quantity_after: float = 0
    merged_names: list[str] = Field(default_factory=list)
    aliases_added: list[str] = Field(default_factory=list)
    message: str = ""


class AnalyzeSummary(APIModel):
    """导入前 AI 预处理的成果汇总。"""

    renamed: int = 0
    categorized: int = 0
    new_categories: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class IntentPlan(APIModel):
    """规则认不出来时，大模型对「用户到底想干什么」的判断。"""

    command: str = "unknown"
    name: str = ""
    quantity: float = 0
    location: str = ""
    confidence: float = 0
    reason: str = ""


class DetailPrompts(APIModel):
    """是否值得为某条记录追问「封装」与「别名」。"""

    ask_spec: bool = False
    ask_alias: bool = False
    reason: str = ""


class TidyResult(APIModel):
    applied: int = 0
    plan: TidyPlan
    message: str = ""


class TidyRequest(APIModel):
    apply: bool = Field(False, description="true=直接应用建议；false=只返回建议")
    operator: str = ""


class AIStockParseRequest(APIModel):
    text: str = Field(..., min_length=1, max_length=2000, description="自由文本，例如「昨天进了一盒 0.1uF 电容 放在 A 柜」")


class AIAnalysisResponse(APIModel):
    question: str
    answer: str
    model: str
    item_count: int
    prompt_tokens: int = 0
    completion_tokens: int = 0


# --------------------------------------------------------------------------- #
# 导入 / 导出
# --------------------------------------------------------------------------- #


class ImportRow(APIModel):
    name: str
    quantity: float = 0
    location: str = ""
    category: str = "other"
    spec: str = ""
    unit: str = ""
    aliases: list[str] = Field(default_factory=list)
    note: str = ""
    row_number: int = 0
    issues: list[str] = Field(default_factory=list)
    original_name: str = Field("", description="被 AI 规范命名前的原名（会同时保留为别名）")

    _norm_category = field_validator("category", mode="before")(_normalize_category)

    @field_serializer("quantity")
    def _serialize_quantity(self, value: float) -> float | int:
        return int(round(value)) if math.isclose(value, round(value), abs_tol=1e-9) else value


class ImportPreviewOut(APIModel):
    filename: str
    fmt: str
    rows: list[ImportRow] = Field(default_factory=list)
    total: int = 0
    warnings: list[str] = Field(default_factory=list)
    detected_columns: list[str] = Field(default_factory=list)
    new_categories: list[str] = Field(
        default_factory=list, description="AI 归类时新建的分类（中文名）"
    )


class ImportCommitRequest(APIModel):
    filename: str = "inline"
    fmt: str = "markdown"
    content: str = Field(..., description="规范化 Markdown / TXT 文本")
    mode: Literal["merge", "replace", "add"] = Field(
        "merge", description="merge=按名称+位置+规格合并；add=一律新建；replace=先清空该分类"
    )
    operator: str = ""
    source: Literal["api", "qq", "import", "cli"] = "import"
    dry_run: bool = False
    use_ai: bool = Field(False, description="导入前让 AI 通读整批自动归类（没有的分类自动新建）")


class ImportResultOut(APIModel):
    batch_id: int | None = None
    filename: str
    fmt: str
    status: str
    total: int = 0
    created: int = 0
    updated: int = 0
    skipped: int = 0
    warnings: list[str] = Field(default_factory=list)
    items: list[ItemOut] = Field(default_factory=list)
    message: str = ""


class AIImportRequest(APIModel):
    content: str = Field(..., description="任意格式的原始文本（Excel 请先另存为 CSV/文本）")
    filename: str = "ai-import"
    mode: Literal["merge", "replace", "add"] = "merge"
    operator: str = ""
    #: 默认**只预览不落库**。AI 导入是一次「任意文本 → 结构化 → 写入」的操作，
    #: 误判代价高，所以安全默认值是不写；确认无误后再显式传 dry_run=false。
    dry_run: bool = True


# --------------------------------------------------------------------------- #
# 机器人命令
# --------------------------------------------------------------------------- #


class BotCommandRequest(APIModel):
    text: str = Field(..., min_length=1, max_length=1000)
    operator: str = Field("", description="操作人，通常是 QQ openid")
    scene: str = Field("api", description="api / qq / c2c / group")
    conversation: str = Field(
        "",
        description="会话键：群聊填 group_openid、单聊填 user_openid。相同键共享上下文（序号选择、待确认操作）",
    )


class BotCommandResponse(APIModel):
    reply: str
    command: str
    handled: bool
    data: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# API Key
# --------------------------------------------------------------------------- #


class ApiKeyCreate(APIModel):
    label: str = Field("", max_length=100)
    scopes: list[Scope] = Field(default_factory=lambda: [Scope.READ])
    expires_at: str | None = None
    created_by: str = ""

    _norm_scopes = field_validator("scopes", mode="before")(_clean_list)


class ApiKeyOut(APIModel):
    id: int
    key_prefix: str
    label: str = ""
    scopes: list[str] = Field(default_factory=list)
    enabled: bool = True
    created_at: str = ""
    created_by: str = ""
    last_used_at: str | None = None
    expires_at: str | None = None
    revoked_at: str | None = None


class ApiKeyCreated(ApiKeyOut):
    key: str = Field(..., description="明文 Key，仅此一次返回")


# --------------------------------------------------------------------------- #
# 通用
# --------------------------------------------------------------------------- #


class MessageOut(APIModel):
    ok: bool = True
    message: str = ""


class HealthOut(APIModel):
    status: str
    version: str
    database: str
    items: int
    qq_bot: dict[str, Any] = Field(default_factory=dict)
    llm_ready: bool = False
