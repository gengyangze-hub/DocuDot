"""FastAPI 应用装配入口。

    # 开发
    uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

    # 等价于
    python -m app.main
"""

from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import __version__
from .api import routes_admin, routes_analysis, routes_import, routes_inventory
from .api.deps import AppContext
from .bot.commands import CommandRouter
from .config import Settings, get_settings
from .db import Database
from .errors import WarehouseError
from .models import HealthOut
from .repository import Repository
from .services.ai import AIService
from .services.analysis import AnalysisService
from .services.importer import ImportService
from .services.inventory import InventoryService
from .services.llm import LLMClient
from .services.nlq import NLQService
from .utils import now_iso

logger = logging.getLogger(__name__)


def build_context(settings: Settings) -> AppContext:
    """装配全部服务（不启动任何后台任务）。"""
    db = Database(settings.database_file)
    db.initialize()
    repo = Repository(db)
    llm = LLMClient(settings)

    inventory = InventoryService(repo, settings)
    analysis = AnalysisService(repo, settings)
    importer = ImportService(repo, settings)
    nlq = NLQService(inventory, analysis, repo, settings, llm)
    ai = AIService(repo, settings, llm, analysis, importer, inventory)
    commands = CommandRouter(inventory, analysis, nlq, settings, ai=ai, importer=importer)

    return AppContext(
        settings=settings,
        db=db,
        repo=repo,
        inventory=inventory,
        analysis=analysis,
        importer=importer,
        llm=llm,
        nlq=nlq,
        ai=ai,
        commands=commands,
        started_at=now_iso(),
    )


def _configure_logging(settings: Settings) -> None:
    """确保应用自己的 INFO 日志可见。

    ``uvicorn app.main:app`` 启动时，uvicorn 只给自己那几个 logger 装了 handler，
    根 logger 没有 handler，于是 ``app.*`` 的 INFO 会被直接丢弃 ——
    结果就是「QQ 机器人到底连上没有」这种关键信息看不见。这里补上。
    """
    root = logging.getLogger()
    if root.handlers:
        return
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    _configure_logging(settings)
    context = build_context(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        _bootstrap_api_key(context)
        await _start_qq_bot(context)
        logger.info(
            "DocuDot 启动完成：库=%s，物品=%s 种，QQ 机器人=%s，LLM=%s",
            settings.database_file,
            context.repo.overview_counts()["item_count"],
            "开" if settings.qq_bot_enabled else "关",
            "就绪" if settings.llm_ready else "未配置",
        )
        try:
            yield
        finally:
            if context.qq_bot is not None:
                try:
                    await context.qq_bot.stop()
                except Exception:  # noqa: BLE001
                    logger.exception("关闭 QQ 机器人失败")
            await context.llm.close()
            logger.info("DocuDot 已停止")

    app = FastAPI(
        title="DocuDot 仓储助手",
        description=(
            "QQ 机器人仓储库存后端：库存变更接收与持久化、API Key 鉴权、"
            "标签式模糊匹配检索、统计报表、自然语言问答、LLM 分析、"
            "Excel/TXT/Markdown 导入。"
        ),
        version=__version__,
        lifespan=lifespan,
    )
    app.state.ctx = context
    app.state.settings = settings

    _register_error_handlers(app)

    app.include_router(routes_inventory.router)
    app.include_router(routes_analysis.router)
    app.include_router(routes_import.router)
    app.include_router(routes_admin.router)

    @app.get("/", include_in_schema=False)
    async def index() -> dict:
        return {
            "service": "DocuDot",
            "version": __version__,
            "docs": "/docs",
            "health": "/health",
        }

    @app.get("/health", response_model=HealthOut, tags=["meta"], summary="健康检查（无需鉴权）")
    async def health() -> HealthOut:
        counts = context.repo.overview_counts()
        qq_status = {"enabled": settings.qq_bot_enabled, "running": False}
        if context.qq_bot is not None:
            try:
                qq_status = context.qq_bot.status()
            except Exception:  # noqa: BLE001
                logger.exception("获取 QQ 机器人状态失败")
        return HealthOut(
            status="ok",
            version=__version__,
            database=str(settings.database_file),
            items=counts["item_count"],
            qq_bot=qq_status,
            llm_ready=settings.llm_ready,
        )

    return app


#: 已知的「占位符」引导 Key —— 照 .env.example 抄下来就会是这个，必须拦住
_PLACEHOLDER_KEYS = frozenset({"dev-admin-key", "dev-admin-key-change-me", "changeme", "password"})


def _random_api_key() -> str:
    return secrets.token_urlsafe(24)


def _bootstrap_api_key(context: AppContext) -> None:
    """首次启动且没有任何 Key 时，建一个管理员 Key。

    约定：``BOOTSTRAP_API_KEY`` 留空就**自动生成一个强随机 Key 并在日志里打印一次**。
    这样「clone 下来直接跑」的人不会拿到一个能猜到的管理员口令
    （以前的模板是 ``dev-admin-key-change-me``，照抄即中招，而且默认还绑 0.0.0.0）。
    """
    settings = context.settings
    raw = (settings.bootstrap_api_key or "").strip()

    if raw.lower() in _PLACEHOLDER_KEYS:
        logger.error(
            "BOOTSTRAP_API_KEY 用的是示例占位符「%s」，这种口令等于没有防护 —— "
            "本次已改为自动生成随机 Key。请到 QQ 开放平台/你的密钥库换成真凭证。",
            raw,
        )
        raw = ""

    if not raw:
        if context.repo.count_api_keys() > 0:
            return
        raw = _random_api_key()
        context.repo.create_api_key(
            label="bootstrap", scopes=["admin"], created_by="system", raw_key=raw
        )
        logger.warning(
            "\n"
            "============================================================\n"
            "  首次启动，已自动生成管理员 API Key（只显示这一次）：\n"
            "      %s\n"
            "  把它填进 .env 的 BOOTSTRAP_API_KEY 并妥善保存；\n"
            "  调接口时放在 X-API-Key 头里。\n"
            "============================================================",
            raw,
        )
        return

    if context.repo.get_api_key(raw):
        return
    if context.repo.count_api_keys() > 0:
        logger.info("已存在其它 API Key，跳过引导 Key 创建")
        return
    context.repo.create_api_key(
        label="bootstrap",
        scopes=["admin"],
        created_by="system",
        raw_key=raw,
    )
    if len(raw) < 16:
        logger.error(
            "BOOTSTRAP_API_KEY 只有 %d 位，太短了 —— 建议至少 16 位随机字符。",
            len(raw),
        )
    else:
        logger.info("已用 .env 里的 BOOTSTRAP_API_KEY 创建管理员 Key（前缀 %s）", raw[:6])


async def _start_qq_bot(context: AppContext) -> None:
    if not context.settings.qq_bot_enabled:
        return
    try:
        from .qq.bot import QQBot  # 延迟导入：没装 aiohttp 也能跑核心服务
    except ImportError:
        logger.warning("QQ 机器人已启用但适配层不可用（缺少 aiohttp？），跳过启动")
        return

    async def handler(message) -> str | None:  # noqa: ANN001
        response = await context.commands.handle(
            message.content,
            operator=message.user_openid,
            scene=f"qq-{message.scene}",
            # 群聊里上下文按「群」共享，单聊按用户 —— 这样回「2」能接上机器人刚列出的清单
            conversation=message.group_openid or message.user_openid,
        )
        return response.reply

    async def attachment_handler(filename: str, data: bytes, message) -> str | None:  # noqa: ANN001
        """QQ 里直接发文件（.xlsx / .csv / .txt / .md）→ 走同一套导入器。"""
        response = await context.commands.handle_attachment(
            filename,
            data,
            operator=message.user_openid,
            scene=f"qq-{message.scene}",
            conversation=message.group_openid or message.user_openid,
        )
        return response.reply

    bot = QQBot(context.settings, handler, attachment_handler)
    try:
        await bot.start()
        context.qq_bot = bot
    except Exception:  # noqa: BLE001
        logger.exception("启动 QQ 机器人失败（不影响 HTTP 接口）")


def _register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(WarehouseError)
    async def _warehouse_error(request: Request, exc: WarehouseError) -> JSONResponse:  # noqa: ARG001
        return JSONResponse(status_code=exc.status_code, content=exc.to_payload())

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:  # noqa: ARG001
        return JSONResponse(
            status_code=422,
            content={
                "error": "validation_failed",
                "message": "请求参数不合法",
                "detail": exc.errors(),
            },
        )


app = create_app()


def main() -> None:
    import uvicorn

    settings = get_settings()
    _configure_logging(settings)
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        log_level=settings.log_level,
    )


if __name__ == "__main__":
    main()
