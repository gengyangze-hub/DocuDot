"""QQ 机器人网关协议层：**纯函数**，不做任何网络 IO，可完整单测。

包含三部分：

1. 网关 OP 码与事件订阅 Intent 常量；
2. 握手/心跳报文构造（IDENTIFY / RESUME / HEARTBEAT）；
3. 平台事件 → :class:`~app.qq.types.IncomingMessage` 的解析与白名单判定。

参考：QQ 机器人开放平台「WebSocket 网关」与「事件订阅 Intents」文档。
"""

from __future__ import annotations

import re

from .types import SCENE_C2C, SCENE_GUILD, SCENE_GROUP, Attachment, IncomingMessage

# --------------------------------------------------------------------------- #
# OP 码
# --------------------------------------------------------------------------- #

OP_DISPATCH = 0
"""服务端事件推送（``t`` 字段标识事件类型）。"""

OP_HEARTBEAT = 1
"""客户端心跳。"""

OP_IDENTIFY = 2
"""客户端鉴权（首次连接）。"""

OP_RESUME = 6
"""客户端恢复会话（断线重连）。"""

OP_RECONNECT = 7
"""服务端要求客户端重连（可尝试 RESUME）。"""

OP_INVALID_SESSION = 9
"""会话失效，必须清空 session 并重新 IDENTIFY。"""

OP_HELLO = 10
"""服务端握手包，下发 ``heartbeat_interval``。"""

OP_HEARTBEAT_ACK = 11
"""服务端心跳回执。"""

# --------------------------------------------------------------------------- #
# Intent 常量（按位或组合）
# --------------------------------------------------------------------------- #

INTENT_GUILD_MESSAGES = 1 << 9
"""频道消息（私域，不含私信）。"""

INTENT_DIRECT_MESSAGE = 1 << 12
"""频道私信。"""

INTENT_GROUP_AND_C2C_EVENT = 1 << 25
"""群聊 @机器人 与单聊消息。"""

INTENT_PUBLIC_GUILD_MESSAGES = 1 << 30
"""频道公开消息（公域）。"""

DEFAULT_INTENTS = INTENT_PUBLIC_GUILD_MESSAGES | INTENT_GROUP_AND_C2C_EVENT
"""默认订阅：公域频道消息 + 群聊/C2C 消息。"""

# --------------------------------------------------------------------------- #
# 事件类型
# --------------------------------------------------------------------------- #

EVENT_C2C_MESSAGE_CREATE = "C2C_MESSAGE_CREATE"
"""单聊消息。"""

EVENT_GROUP_AT_MESSAGE_CREATE = "GROUP_AT_MESSAGE_CREATE"
"""群聊 @机器人 消息。"""

EVENT_AT_MESSAGE_CREATE = "AT_MESSAGE_CREATE"
"""频道内 @机器人 的消息。"""

EVENT_MESSAGE_CREATE = "MESSAGE_CREATE"
"""频道内普通消息（需私域权限）。"""

# --------------------------------------------------------------------------- #
# 网络地址
# --------------------------------------------------------------------------- #

API_BASE_PRODUCTION = "https://api.sgroup.qq.com"
API_BASE_SANDBOX = "https://sandbox.api.sgroup.qq.com"
TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"

DEFAULT_HEARTBEAT_INTERVAL_MS = 41250
"""HELLO 未下发心跳间隔时的兜底值（官方推荐值的近似）。"""

_MENTION_RE = re.compile(r"<@!?\d+>")
"""@提及 形态：``<@123456>`` 或 ``<@!123456>``（频道消息带 ``!``）。"""


# --------------------------------------------------------------------------- #
# 报文构造
# --------------------------------------------------------------------------- #


def build_identify(token: str, intents: int, shard: tuple[int, int] = (0, 1)) -> dict:
    """构造 IDENTIFY（OP 2）报文。

    :param token: 机器人 Token（裸 token，函数内部补 ``QQBot `` 前缀）。
    :param intents: 事件订阅位掩码。
    :param shard: 分片 ``(分片序号, 分片总数)``，单分片用 ``(0, 1)``。
    """
    index, total = shard
    return {
        "op": OP_IDENTIFY,
        "d": {
            "token": f"QQBot {token}",
            "intents": int(intents),
            "shard": [int(index), int(total)],
            "properties": {
                "$os": "windows",
                "$browser": "DocuDot",
                "$device": "DocuDot",
            },
        },
    }


def build_resume(token: str, session_id: str, seq: int) -> dict:
    """构造 RESUME（OP 6）报文，用于断线后恢复会话。"""
    return {
        "op": OP_RESUME,
        "d": {
            "token": f"QQBot {token}",
            "session_id": str(session_id),
            "seq": int(seq),
        },
    }


def build_heartbeat(seq: int | None) -> dict:
    """构造 HEARTBEAT（OP 1）报文；``seq`` 为 ``None`` 时 ``d`` 传 ``null``。"""
    return {"op": OP_HEARTBEAT, "d": seq}


def is_hello(payload: object) -> bool:
    """判断报文是否为 HELLO（OP 10）。"""
    return isinstance(payload, dict) and payload.get("op") == OP_HELLO


def heartbeat_interval(payload: object, default_ms: int = DEFAULT_HEARTBEAT_INTERVAL_MS) -> int:
    """从 HELLO 报文中取出心跳间隔（毫秒）。

    任何异常输入（非字典、缺字段、非数字、非正数）都回退到 ``default_ms``，
    保证网关永远不会因为一个畸形握手包而拿到非法 sleep 值。
    """
    if not isinstance(payload, dict):
        return default_ms
    data = payload.get("d")
    if not isinstance(data, dict):
        return default_ms
    raw = data.get("heartbeat_interval")
    if isinstance(raw, bool):  # bool 是 int 的子类，单独挡掉
        return default_ms
    try:
        value = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default_ms
    return value if value > 0 else default_ms


# --------------------------------------------------------------------------- #
# 文本与事件解析
# --------------------------------------------------------------------------- #


def normalize_text_content(raw: str | None) -> str:
    """去掉 ``<@!123>`` / ``<@123>`` 形式的 @提及 以及首尾空白。"""
    if not raw:
        return ""
    return _MENTION_RE.sub("", str(raw)).strip()


def _as_str(value: object) -> str:
    """把平台的任意标量安全地转成字符串（``None`` → 空串）。"""
    return "" if value is None else str(value)


def _author_field(data: dict, key: str) -> str:
    """安全读取 ``data["author"][key]``，缺失或类型异常时返回空串。"""
    author = data.get("author")
    if not isinstance(author, dict):
        return ""
    return _as_str(author.get(key))


def parse_attachments(data: dict) -> tuple[Attachment, ...]:
    """解析事件里的 ``attachments``。

    QQ 的图片/语音/视频/文件消息正文为空，真正的内容挂在这里。
    字段缺失、类型异常一律健壮处理。
    """
    raw = data.get("attachments")
    if not isinstance(raw, list):
        return ()

    result: list[Attachment] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        url = _as_str(item.get("url"))
        if not url:
            continue
        try:
            size = int(item.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        result.append(
            Attachment(
                content_type=_as_str(item.get("content_type")) or "file",
                filename=_as_str(item.get("filename")),
                url=url,
                size=size,
            )
        )
    return tuple(result)


def parse_event(event_type: str, data: dict) -> IncomingMessage | None:
    """把平台事件解析成 :class:`IncomingMessage`。

    支持 ``C2C_MESSAGE_CREATE`` / ``GROUP_AT_MESSAGE_CREATE`` /
    ``AT_MESSAGE_CREATE`` / ``MESSAGE_CREATE``；其他事件类型与缺失字段
    一律健壮处理（不抛 ``KeyError``），无法识别时返回 ``None``。
    """
    if not isinstance(data, dict):
        return None

    raw_content = _as_str(data.get("content"))
    common = {
        "message_id": _as_str(data.get("id")),
        "content": normalize_text_content(raw_content),
        "raw_content": raw_content,
        "timestamp": _as_str(data.get("timestamp")),
        "raw": data,
        "attachments": parse_attachments(data),
    }

    if event_type == EVENT_C2C_MESSAGE_CREATE:
        return IncomingMessage(
            **common,
            user_openid=_author_field(data, "user_openid"),
            group_openid=None,
            guild_id=None,
            channel_id=None,
            scene=SCENE_C2C,
        )

    if event_type == EVENT_GROUP_AT_MESSAGE_CREATE:
        return IncomingMessage(
            **common,
            user_openid=_author_field(data, "member_openid"),
            group_openid=_as_str(data.get("group_openid")) or None,
            guild_id=None,
            channel_id=None,
            scene=SCENE_GROUP,
        )

    if event_type in (EVENT_AT_MESSAGE_CREATE, EVENT_MESSAGE_CREATE):
        return IncomingMessage(
            **common,
            user_openid=_author_field(data, "id"),
            group_openid=None,
            guild_id=_as_str(data.get("guild_id")) or None,
            channel_id=_as_str(data.get("channel_id")) or None,
            scene=SCENE_GUILD,
        )

    return None


# --------------------------------------------------------------------------- #
# 地址与白名单
# --------------------------------------------------------------------------- #


def resolve_api_base(sandbox: bool) -> str:
    """返回 HTTP 开放接口基址（沙箱 / 正式）。"""
    return API_BASE_SANDBOX if sandbox else API_BASE_PRODUCTION


def resolve_token_url() -> str:
    """返回获取 AppAccessToken 的地址。"""
    return TOKEN_URL


def is_allowed(msg: IncomingMessage, allowed_users: set[str], allowed_groups: set[str]) -> bool:
    """白名单判定。

    规则：两个白名单都为空 → 全部放行；否则用户命中 ``allowed_users``
    或群命中 ``allowed_groups`` 即放行。
    """
    if not allowed_users and not allowed_groups:
        return True
    if msg.user_openid and msg.user_openid in allowed_users:
        return True
    if msg.group_openid and msg.group_openid in allowed_groups:
        return True
    return False


__all__ = [
    "API_BASE_PRODUCTION",
    "API_BASE_SANDBOX",
    "DEFAULT_HEARTBEAT_INTERVAL_MS",
    "DEFAULT_INTENTS",
    "EVENT_AT_MESSAGE_CREATE",
    "EVENT_C2C_MESSAGE_CREATE",
    "EVENT_GROUP_AT_MESSAGE_CREATE",
    "EVENT_MESSAGE_CREATE",
    "INTENT_DIRECT_MESSAGE",
    "INTENT_GROUP_AND_C2C_EVENT",
    "INTENT_GUILD_MESSAGES",
    "INTENT_PUBLIC_GUILD_MESSAGES",
    "OP_DISPATCH",
    "OP_HEARTBEAT",
    "OP_HEARTBEAT_ACK",
    "OP_HELLO",
    "OP_IDENTIFY",
    "OP_INVALID_SESSION",
    "OP_RECONNECT",
    "OP_RESUME",
    "TOKEN_URL",
    "build_heartbeat",
    "build_identify",
    "build_resume",
    "heartbeat_interval",
    "is_allowed",
    "is_hello",
    "parse_attachments",
    "normalize_text_content",
    "parse_event",
    "resolve_api_base",
    "resolve_token_url",
]
