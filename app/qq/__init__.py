"""QQ 官方机器人开放平台适配层（WebSocket 网关 + HTTP 开放接口）。

本包只负责「传输层」：鉴权、网关连接、心跳、事件解析与消息下发，
不含任何库存业务逻辑。业务逻辑通过 :data:`app.qq.types.MessageHandler`
注入，由 :class:`app.qq.bot.QQBot` 负责编排。

模块划分：

* :mod:`app.qq.types`    —— 数据结构（:class:`IncomingMessage` / :class:`BotReply`）
* :mod:`app.qq.protocol` —— 纯函数：OP 常量、握手包构造、事件解析（无网络，可单测）
* :mod:`app.qq.openapi`  —— HTTP 开放接口：access_token、C2C/群消息下发
* :mod:`app.qq.gateway`  —— WebSocket 网关常驻循环：连接、心跳、重连、分发
* :mod:`app.qq.bot`      —— 上层编排：白名单、前缀、调用业务 handler、按场景回复
"""

from __future__ import annotations

from .bot import QQBot
from .gateway import QQGateway
from .openapi import QQOpenAPI
from .types import Attachment, AttachmentHandler, BotReply, IncomingMessage, MessageHandler

__all__ = [
    "Attachment",
    "AttachmentHandler",
    "BotReply",
    "IncomingMessage",
    "MessageHandler",
    "QQBot",
    "QQGateway",
    "QQOpenAPI",
]
