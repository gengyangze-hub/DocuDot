"""管理路由：API Key、审计日志、机器人命令入口。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from .. import __version__
from ..api.deps import AuthInfo, get_context, require
from ..models import (
    ApiKeyCreate,
    ApiKeyCreated,
    ApiKeyOut,
    BotCommandRequest,
    BotCommandResponse,
    MessageOut,
    Scope,
)

router = APIRouter(prefix="/api/v1", tags=["admin"])


# --------------------------------------------------------------------------- #
# API Key
# --------------------------------------------------------------------------- #
@router.post("/admin/keys", response_model=ApiKeyCreated, status_code=201, summary="新建 API Key")
async def create_key(
    request: Request, payload: ApiKeyCreate, auth: AuthInfo = Depends(require("admin"))
) -> ApiKeyCreated:
    context = get_context(request)
    raw, key_id = context.repo.create_api_key(
        label=payload.label,
        scopes=[scope.value for scope in payload.scopes] or [Scope.READ.value],
        created_by=payload.created_by or auth.label or "admin",
        expires_at=payload.expires_at,
    )
    context.repo.audit(
        action="apikey.create",
        actor=auth.label or "admin",
        actor_kind="admin",
        target_type="api_key",
        target_id=key_id,
        detail={"label": payload.label, "scopes": [s.value for s in payload.scopes]},
    )
    row = context.repo.db.query_one("SELECT * FROM api_keys WHERE id = ?", (key_id,))
    return ApiKeyCreated(
        id=key_id,
        key=raw,
        key_prefix=raw[:12],
        label=payload.label,
        scopes=[scope.value for scope in payload.scopes],
        enabled=True,
        created_at=row["created_at"] if row else "",
        created_by=payload.created_by or auth.label or "admin",
        expires_at=payload.expires_at,
    )


@router.get("/admin/keys", response_model=list[ApiKeyOut], summary="列出 API Key（不含明文）")
async def list_keys(request: Request, _: AuthInfo = Depends(require("admin"))) -> list[ApiKeyOut]:
    context = get_context(request)
    result: list[ApiKeyOut] = []
    for row in context.repo.list_api_keys():
        result.append(
            ApiKeyOut(
                id=int(row["id"]),
                key_prefix=row["key_prefix"],
                label=row["label"] or "",
                scopes=[s for s in (row["scopes"] or "").split(",") if s],
                enabled=bool(row["enabled"]) and not row["revoked_at"],
                created_at=row["created_at"] or "",
                created_by=row["created_by"] or "",
                last_used_at=row["last_used_at"],
                expires_at=row["expires_at"],
                revoked_at=row["revoked_at"],
            )
        )
    return result


@router.delete("/admin/keys/{key_id}", response_model=MessageOut, summary="吊销 API Key")
async def revoke_key(
    request: Request, key_id: int, auth: AuthInfo = Depends(require("admin"))
) -> MessageOut:
    context = get_context(request)
    if not context.repo.revoke_api_key(key_id):
        return MessageOut(ok=False, message=f"Key #{key_id} 不存在")
    context.repo.audit(
        action="apikey.revoke",
        actor=auth.label or "admin",
        actor_kind="admin",
        target_type="api_key",
        target_id=key_id,
    )
    return MessageOut(message=f"已吊销 Key #{key_id}")


# --------------------------------------------------------------------------- #
# 审计
# --------------------------------------------------------------------------- #
@router.get("/admin/audit", summary="审计日志")
async def audit_log(
    request: Request,
    _: AuthInfo = Depends(require("admin")),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict:
    context = get_context(request)
    total, rows = context.repo.list_audit(limit=limit, offset=offset)
    return {"total": total, "logs": [dict(row) for row in rows]}


@router.get("/admin/status", summary="服务运行状态")
async def service_status(request: Request, _: AuthInfo = Depends(require("admin"))) -> dict:
    context = get_context(request)
    counts = context.repo.overview_counts()
    return {
        "version": __version__,
        "started_at": context.started_at,
        "database": str(context.settings.database_file),
        "counts": counts,
        "api_keys": context.repo.count_api_keys(),
        "llm_ready": context.settings.llm_ready,
        "llm_model": context.settings.llm_model if context.settings.llm_ready else None,
        "qq_bot": context.qq_bot.status() if context.qq_bot else {"enabled": context.settings.qq_bot_enabled, "running": False},
    }


# --------------------------------------------------------------------------- #
# 机器人命令
# --------------------------------------------------------------------------- #
@router.post(
    "/bot/command",
    response_model=BotCommandResponse,
    summary="机器人命令入口（QQ 适配层与其它机器人框架共用）",
)
async def bot_command(
    request: Request, payload: BotCommandRequest, auth: AuthInfo = Depends(require("write"))
) -> BotCommandResponse:
    """机器人命令入口。

    ⚠️ 必须要求 ``write``：这条路径背后的命令路由器能改库存、能删记录、
    甚至能清空全库（``删除全部`` → ``确认``）。以前只要 ``read`` ——
    一个只读 Key 就能接管全部数据。
    """
    context = get_context(request)
    # 会话键按调用方隔离：否则任何拿到 Key 的人都能用自己构造的 conversation
    # 去替别人按下「确认」，二次确认就不成其为边界了。
    conversation = payload.conversation or payload.operator
    return await context.commands.handle(
        payload.text,
        operator=payload.operator,
        scene=payload.scene,
        conversation=f"key{auth.key_id or 0}:{conversation}",
    )
