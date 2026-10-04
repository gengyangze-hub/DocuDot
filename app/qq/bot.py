"""QQ 机器人上层编排：白名单 → 前缀剥离 → 调用业务 handler → 按场景回复。

这一层是传输层与业务层之间的唯一粘合点：

* 业务层只关心 :data:`~app.qq.types.MessageHandler`（``IncomingMessage`` → 文本）；
* 本层负责配置校验、权限过滤、异常兜底与「往哪儿回」的路由选择。
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from typing import Any

import aiohttp

from .gateway import QQGateway
from .openapi import QQOpenAPI
from .protocol import is_allowed, parse_event
from .types import (
    SCENE_C2C,
    SCENE_GROUP,
    SCENE_GUILD,
    AttachmentHandler,
    IncomingMessage,
    MessageHandler,
)

logger = logging.getLogger(__name__)

#: 业务 handler 抛异常时回给用户的兜底文案。
HANDLER_ERROR_REPLY = "处理出错，请稍后再试"

#: 所有会被统计的场景（保证 ``status()`` 的键稳定，即使从未收到消息）。
KNOWN_SCENES = (SCENE_C2C, SCENE_GROUP, SCENE_GUILD)


class QQBot:
    """QQ 机器人门面：启停网关、处理入站消息、按场景回发消息。

    :param settings: 配置对象（:class:`app.config.Settings` 结构即可，鸭子类型）。
    :param handler: 业务处理函数，返回要回复的文本，``None`` 表示不回复。
    """

    def __init__(
        self,
        settings: Any,
        handler: MessageHandler,
        attachment_handler: AttachmentHandler | None = None,
    ) -> None:
        self._settings = settings
        self._handler = handler
        self._attachment_handler = attachment_handler
        self._openapi: QQOpenAPI | None = None
        self._gateway: QQGateway | None = None
        self._task: asyncio.Task | None = None
        self._running = False
        self._scene_stats: dict[str, int] = {scene: 0 for scene in KNOWN_SCENES}

    # ------------------------------------------------------------------ #
    # 状态
    # ------------------------------------------------------------------ #

    @property
    def enabled(self) -> bool:
        return bool(getattr(self._settings, "qq_bot_enabled", False))

    @property
    def has_credentials(self) -> bool:
        """是否具备最小可用凭证：app_id + （app_secret 或 bot_token）。"""
        app_id = str(getattr(self._settings, "qq_app_id", "") or "")
        secret = str(getattr(self._settings, "qq_app_secret", "") or "")
        token = str(getattr(self._settings, "qq_bot_token", "") or "")
        return bool(app_id and (secret or token))

    @property
    def scene_stats(self) -> dict[str, int]:
        """各场景已处理消息条数（副本）。"""
        return dict(self._scene_stats)

    def status(self) -> dict:
        """返回运行状态快照，供健康检查接口使用。"""
        task = self._task
        running = bool(self._running and task is not None and not task.done())
        return {
            "enabled": self.enabled,
            "running": running,
            "app_id": str(getattr(self._settings, "qq_app_id", "") or ""),
            "sandbox": bool(getattr(self._settings, "qq_sandbox", False)),
            "scene_stats": self.scene_stats,
        }

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        """启动机器人。

        未启用或缺凭证时只记日志并直接返回，**不抛异常**，避免拖垮主服务。
        """
        if not self.enabled:
            logger.info("QQ 机器人未启用（qq_bot_enabled=False），跳过启动")
            return
        if not self.has_credentials:
            logger.warning("QQ 机器人已启用但缺少凭证（qq_app_id / qq_app_secret / qq_bot_token），跳过启动")
            return
        if self._running:
            logger.info("QQ 机器人已在运行，忽略重复启动")
            return

        self._openapi = QQOpenAPI(self._settings)
        self._gateway = QQGateway(self._settings, self.handle_event)
        self._task = asyncio.create_task(self._gateway.run_forever())
        self._running = True
        logger.info(
            "QQ 机器人已启动：app_id=%s sandbox=%s intents=%s",
            getattr(self._settings, "qq_app_id", ""),
            bool(getattr(self._settings, "qq_sandbox", False)),
            getattr(self._settings, "qq_intents", 0),
        )

    async def stop(self) -> None:
        """优雅停止：取消网关任务并关闭 HTTP 会话。可重复调用。"""
        self._running = False

        gateway, self._gateway = self._gateway, None
        if gateway is not None:
            try:
                await gateway.stop()
            except Exception:  # noqa: BLE001 - 停止流程不应抛出
                logger.debug("停止 QQ 网关时出错", exc_info=True)

        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                logger.debug("等待 QQ 网关任务退出时出错", exc_info=True)

        openapi, self._openapi = self._openapi, None
        if openapi is not None:
            try:
                await openapi.close()
            except Exception:  # noqa: BLE001
                logger.debug("关闭 QQ 开放接口会话时出错", exc_info=True)

        logger.info("QQ 机器人已停止")

    # ------------------------------------------------------------------ #
    # 入站处理
    # ------------------------------------------------------------------ #

    async def handle_event(self, event_type: str, data: dict) -> None:
        """网关事件回调：解析 → 统计 → 处理 → 回复。"""
        msg = parse_event(event_type, data)
        if msg is None:
            logger.debug("忽略未支持的 QQ 事件：%s", event_type)
            return

        self._scene_stats[msg.scene] = self._scene_stats.get(msg.scene, 0) + 1
        # 收到消息必须留下 INFO 日志：否则线上「机器人到底收没收到」无从判断
        logger.info(
            "收到 QQ 消息：scene=%s user=%s group=%s content=%r 附件=%d",
            msg.scene,
            msg.user_openid,
            msg.group_openid or "-",
            (msg.raw_content or "")[:120],
            len(msg.attachments),
        )

        reply = await self.handle_incoming(msg)
        if not reply:
            return
        await self._send_reply(msg, reply)

    async def handle_incoming(self, msg: IncomingMessage) -> str | None:
        """执行白名单 / 前缀过滤并调用业务 handler。

        返回要回复的文本；``None`` 表示不回复。**handler 的异常在这里被吞掉**，
        绝不向网关冒泡。
        """
        allowed_users = set(getattr(self._settings, "allowed_users", set()) or set())
        allowed_groups = set(getattr(self._settings, "allowed_groups", set()) or set())
        if not is_allowed(msg, allowed_users, allowed_groups):
            logger.info("QQ 消息被白名单拦截：scene=%s user=%s", msg.scene, msg.user_openid)
            return None

        # 附件优先：QQ 的文件消息正文为空，内容全在 attachments 里
        if msg.attachments:
            return await self._handle_attachments(msg)

        content = msg.content
        prefix = str(getattr(self._settings, "qq_command_prefix", "") or "")
        if prefix:
            if not content.startswith(prefix):
                logger.debug("QQ 消息未以指令前缀 %r 开头，忽略", prefix)
                return None
            content = content[len(prefix) :].strip()

        # 让业务层看到「已剥离前缀」的正文。
        dispatch_msg = dataclasses.replace(msg, content=content)

        try:
            reply = await self._handler(dispatch_msg)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 业务异常不得影响网关
            logger.exception("QQ 业务 handler 处理消息失败：scene=%s", msg.scene)
            return HANDLER_ERROR_REPLY

        if not reply:
            return None
        return str(reply)

    # ------------------------------------------------------------------ #
    # 附件
    # ------------------------------------------------------------------ #

    async def _handle_attachments(self, msg: IncomingMessage) -> str | None:
        """处理带附件的消息：下载文件 → 交给业务层。

        图片暂时不支持（需要视觉模型），但会给出明确提示而不是装死。
        """
        handler = self._attachment_handler
        files = msg.files
        if not files:
            if msg.images:
                return "图片我暂时看不懂，麻烦发文字清单，或者发表格文件（.xlsx / .csv / .txt / .md）。"
            if msg.attachments:
                return "语音和视频我处理不了，麻烦把清单发成文字或表格文件（.xlsx / .csv / .txt / .md）。"
            return None
        if handler is None:
            logger.warning("收到附件但未配置附件处理器，忽略：%s", files[0].filename)
            return None

        attachment = files[0]
        limit_mb = int(getattr(self._settings, "qq_max_file_mb", 20) or 20)
        limit = limit_mb * 1024 * 1024
        if attachment.size and attachment.size > limit:
            return f"文件太大了（{attachment.size / 1048576:.1f}MB），我最多只能处理 {limit_mb}MB。"

        data = await self._download(attachment.url)
        if data is None:
            return "文件下载失败了，麻烦重发一次，或者把内容直接粘成文字发我。"
        if len(data) > limit:
            return f"文件太大了（{len(data) / 1048576:.1f}MB），我最多只能处理 {limit_mb}MB。"
        if not data:
            return "这个文件是空的。"

        filename = attachment.filename or "qq-file"
        logger.info("收到 QQ 附件：%s（%.1fKB）", filename, len(data) / 1024)
        try:
            reply = await handler(filename, data, msg)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 附件异常同样不得冒泡
            logger.exception("附件处理失败：%s", filename)
            return HANDLER_ERROR_REPLY
        return str(reply) if reply else None

    async def _download(self, url: str) -> bytes | None:
        """下载附件。失败只记日志并返回 ``None``。"""
        timeout = aiohttp.ClientTimeout(total=90)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as resp:
                    if resp.status >= 400:
                        logger.warning("下载 QQ 附件失败：HTTP %s（%s）", resp.status, url[:80])
                        return None
                    return await resp.read()
        except Exception:  # noqa: BLE001 - 网络问题不能影响网关
            logger.exception("下载 QQ 附件异常：%s", url[:80])
            return None

    # ------------------------------------------------------------------ #
    # 出站
    # ------------------------------------------------------------------ #

    async def _send_reply(self, msg: IncomingMessage, content: str) -> None:
        """按场景选择下发接口；失败只记日志，不影响网关循环。"""
        openapi = self._openapi

        if msg.scene == SCENE_GUILD:
            logger.debug("频道场景暂不支持主动回复，已忽略（channel_id=%s）", msg.channel_id)
            return

        if openapi is None:
            logger.warning("QQ 开放接口客户端未初始化，无法回复消息")
            return

        try:
            if msg.scene == SCENE_C2C:
                await openapi.send_c2c_message(
                    msg.user_openid, content, msg_id=msg.message_id or None
                )
            elif msg.scene == SCENE_GROUP:
                if not msg.group_openid:
                    logger.warning("群聊消息缺少 group_openid，无法回复")
                    return
                await openapi.send_group_message(
                    msg.group_openid, content, msg_id=msg.message_id or None
                )
            else:
                logger.debug("未知场景 %r，不回复", msg.scene)
                return
            logger.info("已回复 QQ 消息：scene=%s 长度=%d", msg.scene, len(content))
        except Exception:  # noqa: BLE001 - 回复失败不影响后续消息
            logger.exception("发送 QQ 回复失败：scene=%s", msg.scene)


__all__ = ["HANDLER_ERROR_REPLY", "KNOWN_SCENES", "QQBot"]
