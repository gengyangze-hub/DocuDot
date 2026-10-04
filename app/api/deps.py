"""API 依赖：应用上下文与 API Key 鉴权。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine

from fastapi import Request

from ..config import Settings
from ..db import Database
from ..errors import AuthError, PermissionDenied
from ..repository import Repository
from ..services.ai import AIService
from ..services.analysis import AnalysisService
from ..services.importer import ImportService
from ..services.inventory import InventoryService
from ..services.llm import LLMClient
from ..services.nlq import NLQService
from ..bot.commands import CommandRouter

logger = logging.getLogger(__name__)

#: 权限范围：admin 隐式包含其它全部
ALL_SCOPES = ("read", "write", "analyze", "admin")


@dataclass
class AppContext:
    """一次进程内共享的服务容器。"""

    settings: Settings
    db: Database
    repo: Repository
    inventory: InventoryService
    analysis: AnalysisService
    importer: ImportService
    llm: LLMClient
    nlq: NLQService
    ai: AIService
    commands: CommandRouter
    qq_bot: Any | None = None
    started_at: str = ""

    def shutdown_hooks(self) -> list[Any]:
        return [self.llm.close()]


@dataclass
class AuthInfo:
    key_id: int | None
    label: str
    scopes: set[str] = field(default_factory=set)

    @property
    def is_admin(self) -> bool:
        return "admin" in self.scopes


def get_context(request: Request) -> AppContext:
    context = getattr(request.app.state, "ctx", None)
    if context is None:  # pragma: no cover - 说明应用没按 create_app 装配
        raise RuntimeError("应用上下文未初始化")
    return context


def extract_api_key(request: Request) -> str | None:
    """从 ``X-API-Key`` 或 ``Authorization: Bearer`` 里取 Key。"""
    header = request.headers.get("x-api-key")
    if header:
        return header.strip()
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


#: ``last_used_at`` 的写入节流窗口（秒）。够精确到「最近用过」，又不会每次请求都写库。
TOUCH_INTERVAL_SECONDS = 60


def _needs_touch(raw: str | None) -> bool:
    """是否需要刷新 ``last_used_at``（距上次超过节流窗口）。"""
    if not raw:
        return True
    try:
        last = datetime.fromisoformat(raw)
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - last).total_seconds() >= TOUCH_INTERVAL_SECONDS


def _is_expired(raw: str | None) -> bool:
    if not raw:
        return False
    try:
        expires = datetime.fromisoformat(raw)
    except ValueError:
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires < datetime.now(timezone.utc)


def authenticate(request: Request, required: tuple[str, ...] = ()) -> AuthInfo:
    """校验 API Key 并检查权限范围。"""
    context = get_context(request)
    raw = extract_api_key(request)
    if not raw:
        raise AuthError("缺少 API Key：请用 X-API-Key 头或 Authorization: Bearer <key>")

    row = context.repo.get_api_key(raw)
    if not row:
        raise AuthError("API Key 无效")
    if not row["enabled"] or row["revoked_at"]:
        raise AuthError("API Key 已被吊销")
    if _is_expired(row["expires_at"]):
        raise AuthError("API Key 已过期")

    granted = {scope.strip() for scope in (row["scopes"] or "").split(",") if scope.strip()}
    needed = set(required)
    if needed and not (granted & {"admin"} or needed <= granted):
        raise PermissionDenied(f"该 Key 缺少权限：需要 {'/'.join(sorted(needed))}，当前 {'/'.join(sorted(granted)) or '无'}")

    # last_used_at 节流：每个请求都写一次的话，连纯读请求都会产生写事务 ——
    # 既拖慢响应，又会让检索语料缓存每次都被判为失效。
    if _needs_touch(row["last_used_at"]):
        context.repo.touch_api_key(int(row["id"]))
    return AuthInfo(key_id=int(row["id"]), label=row["label"] or "", scopes=granted)


def require(*scopes: str) -> Callable[..., Coroutine[Any, Any, AuthInfo]]:
    """生成一个 FastAPI 依赖，要求调用方具备指定权限。"""

    async def dependency(request: Request) -> AuthInfo:
        return authenticate(request, scopes)

    dependency.__name__ = f"require_{'_'.join(scopes) or 'auth'}"
    return dependency
