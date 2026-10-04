"""QQ 适配层的数据结构定义。

这里只放「跨模块共享」的轻量数据结构，刻意保持不可变（``frozen=True``），
避免网关线程与业务层互相改写同一条消息。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

# --------------------------------------------------------------------------- #
# 场景常量
# --------------------------------------------------------------------------- #

SCENE_C2C = "c2c"
"""单聊（QQ 好友 / 私信），对应官方 C2C 事件。"""

SCENE_GROUP = "group"
"""群聊 @机器人，对应官方 GROUP_AT_MESSAGE_CREATE 事件。"""

SCENE_GUILD = "guild"
"""频道（子频道）消息，对应官方 AT_MESSAGE_CREATE / MESSAGE_CREATE 事件。"""


@dataclass(frozen=True)
class Attachment:
    """消息里携带的附件（用户发的文件 / 图片）。

    QQ 的文件消息 ``content`` 是空的，内容以 ``attachments`` 数组下发，
    所以只解析 ``content`` 会把文件消息当成空消息。
    """

    content_type: str
    """平台给的类型：``file``、``image/png``、``application/pdf`` 等。"""

    filename: str
    """原始文件名（平台可能给空串）。"""

    url: str
    """下载地址。"""

    size: int = 0
    """字节数（平台可能给 0）。"""

    @property
    def is_image(self) -> bool:
        return self.content_type.casefold().startswith("image/")

    @property
    def is_voice_or_video(self) -> bool:
        """语音 / 视频 —— 官方 content_type 为 ``voice`` 或 ``video/mp4``。"""
        kind = self.content_type.casefold()
        return kind == "voice" or kind.startswith(("video/", "audio/"))

    @property
    def is_file(self) -> bool:
        """能当文档导入的附件（``content_type`` 为 ``file`` 或 application/text）。"""
        if not self.url or self.is_image or self.is_voice_or_video:
            return False
        return True


@dataclass(frozen=True)
class IncomingMessage:
    """一条「已归一化」的入站消息。

    归一化包括：剥离 @机器人 前缀、剥离首尾空白、把三种场景的字段差异
    （C2C 的 ``user_openid`` / 群聊的 ``member_openid`` / 频道的 ``author.id``）
    统一收敛到 :attr:`user_openid`。
    """

    message_id: str
    """平台消息 ID，回复时作为 ``msg_id`` 使用（被动回复必需）。"""

    content: str
    """已剥离 @机器人 前缀与首尾空白后的正文。"""

    raw_content: str
    """平台下发的原始正文（未做任何处理）。"""

    user_openid: str
    """发送者标识：C2C 用 ``user_openid``；群聊用 ``member_openid``；频道用 ``author.id``。"""

    group_openid: str | None
    """群聊场景的群标识；C2C / 频道场景为 ``None``。"""

    guild_id: str | None
    """频道场景的频道（guild）ID；其他场景为 ``None``。"""

    channel_id: str | None
    """频道场景的子频道 ID；其他场景为 ``None``。"""

    scene: str
    """场景标识，取值见 :data:`SCENE_C2C` / :data:`SCENE_GROUP` / :data:`SCENE_GUILD`。"""

    timestamp: str
    """平台时间戳（原样字符串，可能为空串）。"""

    raw: dict
    """原始事件 ``data``，供业务层取用平台的额外字段。"""

    attachments: tuple[Attachment, ...] = ()
    """随消息下发的附件；文件消息的正文为空、内容都在这里。"""

    @property
    def files(self) -> tuple[Attachment, ...]:
        return tuple(item for item in self.attachments if item.is_file)

    @property
    def images(self) -> tuple[Attachment, ...]:
        return tuple(item for item in self.attachments if item.is_image)


@dataclass(frozen=True)
class BotReply:
    """一条待下发的回复。"""

    content: str
    """回复正文。"""

    msg_type: int = 0
    """消息类型：``0`` = 纯文本，``2`` = markdown。"""


MessageHandler = Callable[[IncomingMessage], Awaitable[str | None]]
"""业务处理函数：收到消息后返回要回复的文本；返回 ``None`` 表示不回复。"""

AttachmentHandler = Callable[[str, bytes, IncomingMessage], Awaitable[str | None]]
"""附件处理函数：``(文件名, 字节内容, 原消息)`` → 回复文本；``None`` 表示不回复。"""


__all__ = [
    "SCENE_C2C",
    "SCENE_GROUP",
    "SCENE_GUILD",
    "Attachment",
    "AttachmentHandler",
    "BotReply",
    "IncomingMessage",
    "MessageHandler",
]
