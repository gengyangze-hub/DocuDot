"""导入 / 导出路由。"""

from __future__ import annotations

import io
from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import Response, StreamingResponse

from ..api.deps import AuthInfo, get_context, require
from ..errors import PermissionDenied, ValidationFailed
from ..models import AIImportRequest, ImportCommitRequest, ImportPreviewOut, ImportResultOut
from ..services.ai import MARKDOWN_SPEC, items_to_markdown
from ..services.categorize import category_label
from ..services.importer import ImportService, parse_input

router = APIRouter(prefix="/api/v1", tags=["import"])

#: 上传文件大小上限。导入用的清单不会有几十 MB，超过基本是误传或恶意。
MAX_UPLOAD_BYTES = 20 * 1024 * 1024


def _guard_destructive(mode: str, auth: AuthInfo) -> None:
    """``replace`` 会**先清空涉及分类的旧记录**再写入，属于破坏性操作。

    机器人的路径固定用 ``merge`` 并带二次确认；HTTP 这条路径没有确认环节，
    所以要求 ``admin`` —— 免得一个普通 write Key 一个请求清掉整个分类。
    """
    if mode == "replace" and not auth.is_admin:
        raise PermissionDenied(
            "replace 模式会先清空涉及分类的旧记录，需要 admin 权限；请改用 merge 或 add"
        )


async def _read_upload(file: UploadFile) -> bytes:
    """读上传文件，**带上限**。

    不能先 ``await file.read()`` 再判断大小 —— 那等于把任意大的请求体
    先整个读进内存，一个超大文件就能把服务打爆。这里先看 ``Content-Length``，
    再按上限 +1 字节读，超了立刻拒绝。
    """
    declared = getattr(file, "size", None)
    if declared is not None and declared > MAX_UPLOAD_BYTES:
        raise ValidationFailed(f"文件超过 {MAX_UPLOAD_BYTES // 1024 // 1024}MB，请拆分后再导入")
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if not data:
        raise ValidationFailed("上传的文件是空的")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValidationFailed(f"文件超过 {MAX_UPLOAD_BYTES // 1024 // 1024}MB，请拆分后再导入")
    return data

MARKDOWN_TEMPLATE = """\
# 库存清单

## STM元器件

| 名称 | 数量 | 位置 | 规格 | 别名 | 备注 |
| --- | --- | --- | --- | --- | --- |
| STM32F103C8T6 | 25 | A柜-1层-盒3 | LQFP48 | F103C8, STM32F103 | 蓝药丸板用 |
| 0.1uF 50V MLCC | 500 | A柜-2层-盒1 | 0805 | 104, 100nF | |

## Steam游戏卡

| 名称 | 数量 | 位置 | 规格 | 别名 | 备注 |
| --- | --- | --- | --- | --- | --- |
| Steam 50元充值卡 | 10 | B柜-抽屉1 | 50元 | 50元卡 | |
"""


# --------------------------------------------------------------------------- #
# 预览
# --------------------------------------------------------------------------- #
@router.post("/import/preview", response_model=ImportPreviewOut, summary="上传文件预览解析结果（不落库）")
async def preview_upload(
    request: Request,
    _: AuthInfo = Depends(require("write")),
    file: UploadFile = File(..., description=".md / .txt / .csv / .json / .xlsx"),
    default_category: str | None = Form(None),
    use_ai: bool = Form(False, description="让 AI 通读整批自动归类（没有的分类自动新建）"),
) -> ImportPreviewOut:
    context = get_context(request)
    data = await _read_upload(file)
    preview = parse_input(file.filename or "upload", data=data, default_category=default_category)
    if use_ai:
        await context.ai.analyze_preview(preview)
    return preview


@router.post("/import/preview-text", response_model=ImportPreviewOut, summary="直接提交文本预览（便于 AI 预处理流程）")
async def preview_text(
    request: Request,
    _: AuthInfo = Depends(require("write")),
    filename: str = Query("inline.md"),
    content: str = Query(..., description="Markdown / TXT / CSV / JSON 内容"),
    default_category: str | None = Query(None),
    use_ai: bool = Query(False, description="让 AI 通读整批自动归类"),
) -> ImportPreviewOut:
    context = get_context(request)
    preview = parse_input(filename, text=content, default_category=default_category)
    if use_ai:
        await context.ai.analyze_preview(preview)
    return preview


# --------------------------------------------------------------------------- #
# 提交
# --------------------------------------------------------------------------- #
@router.post("/import/commit", response_model=ImportResultOut, summary="提交导入（Markdown/TXT 文本）")
async def commit_import(
    request: Request, payload: ImportCommitRequest, auth: AuthInfo = Depends(require("write"))
) -> ImportResultOut:
    _guard_destructive(payload.mode, auth)
    context = get_context(request)
    preview = parse_input(payload.filename, text=payload.content)
    preview.filename = payload.filename
    if payload.use_ai:
        await context.ai.analyze_preview(preview)
    return context.importer.commit(
        preview,
        mode=payload.mode,
        operator=payload.operator,
        source=payload.source,
        dry_run=payload.dry_run,
    )


@router.post("/import/commit-file", response_model=ImportResultOut, summary="提交导入（上传文件）")
async def commit_file(
    request: Request,
    auth: AuthInfo = Depends(require("write")),
    file: UploadFile = File(...),
    mode: str = Form("merge"),
    operator: str = Form(""),
    dry_run: bool = Form(False),
    use_ai: bool = Form(False, description="导入前让 AI 通读整批自动归类"),
) -> ImportResultOut:
    _guard_destructive(mode, auth)
    context = get_context(request)
    data = await _read_upload(file)
    preview = parse_input(file.filename or "upload", data=data)
    if use_ai:
        await context.ai.analyze_preview(preview)
    return context.importer.commit(preview, mode=mode, operator=operator, source="import", dry_run=dry_run)


@router.post(
    "/import/ai",
    summary="AI 规范化导入：任意文本 → 大模型整理成 Markdown → 导入",
)
async def ai_import(
    request: Request, payload: AIImportRequest, auth: AuthInfo = Depends(require("write"))
) -> dict:
    # AI 导入会写库（除非 dry_run），且 replace 会清分类 —— scope 与守护都对上
    _guard_destructive(payload.mode, auth)
    context = get_context(request)
    markdown, preview, result = await context.ai.ai_import(
        payload.content,
        filename=payload.filename,
        mode=payload.mode,
        operator=payload.operator,
        dry_run=payload.dry_run,
    )
    return {
        "normalized_markdown": markdown,
        "preview": preview.model_dump(),
        "result": result.model_dump(),
    }


# --------------------------------------------------------------------------- #
# 模板与批次
# --------------------------------------------------------------------------- #
@router.get("/import/template", summary="下载规范化 Markdown 模板与 AI 提示词")
async def import_template(_: AuthInfo = Depends(require("read"))) -> dict:
    return {
        "markdown_template": MARKDOWN_TEMPLATE,
        "ai_prompt": MARKDOWN_SPEC,
        "usage": [
            "方式一（推荐）：把 Excel/聊天记录原文贴给任意大模型，附上 ai_prompt，让它输出 Markdown，"
            "然后 POST /api/v1/import/commit 提交。",
            "方式二：直接 POST /api/v1/import/ai，由本服务调用配置好的大模型完成规范化与导入。",
            "方式三：Excel/CSV/TXT 直接上传 /api/v1/import/commit-file，服务内置表头识别。",
        ],
    }


@router.get("/import/batches", summary="导入批次历史")
async def import_batches(
    request: Request, _: AuthInfo = Depends(require("read")), limit: int = Query(20, ge=1, le=200)
) -> dict:
    context = get_context(request)
    rows = context.repo.list_import_batches(limit=limit)
    return {"total": len(rows), "batches": [dict(row) for row in rows]}


# --------------------------------------------------------------------------- #
# 导出
# --------------------------------------------------------------------------- #
@router.get("/export/markdown", summary="导出为规范化 Markdown（可直接再导入）")
async def export_markdown(
    request: Request,
    _: AuthInfo = Depends(require("read")),
    category: str | None = Query(None),
    download: bool = Query(False),
) -> Response:
    context = get_context(request)
    total, records = context.inventory.list_items(category=category, limit=5000)
    markdown = items_to_markdown([record.to_out() for record in records])
    if download:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return Response(
            content=markdown.encode("utf-8"),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="inventory-{stamp}.md"'},
        )
    return Response(content=markdown, media_type="text/markdown; charset=utf-8")


@router.get("/export/xlsx", summary="导出为 Excel")
async def export_xlsx(
    request: Request,
    _: AuthInfo = Depends(require("read")),
    category: str | None = Query(None),
) -> StreamingResponse:
    from openpyxl import Workbook

    context = get_context(request)
    _, records = context.inventory.list_items(category=category, limit=10000)

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "库存"
    headers = ["名称", "数量", "位置", "分类", "规格", "别名", "备注", "更新时间"]
    sheet.append(headers)
    for record in records:
        sheet.append(
            [
                record.name,
                record.quantity,
                record.location,
                category_label(record.category),
                record.spec,
                ", ".join(record.aliases),
                record.note,
                record.updated_at,
            ]
        )
    for index, width in enumerate([28, 10, 18, 12, 14, 24, 26, 22], start=1):
        sheet.column_dimensions[chr(64 + index)].width = width

    buffer = io.BytesIO()
    workbook.save(buffer)
    buffer.seek(0)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="inventory-{stamp}.xlsx"'},
    )
