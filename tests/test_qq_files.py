"""QQ 附件（文件消息）测试。

QQ 的图片/文件消息正文为空、内容挂在 ``attachments`` 上，
所以「直接发文件没反应」的根因是只解析了 ``content``。
"""

from __future__ import annotations

import asyncio

import pytest

from app.config import Settings
from app.qq.bot import QQBot
from app.qq.protocol import parse_attachments, parse_event
from app.qq.types import Attachment, IncomingMessage


def _message(**overrides) -> IncomingMessage:
    base = dict(
        message_id="m1",
        content="",
        raw_content="",
        user_openid="u1",
        group_openid=None,
        guild_id=None,
        channel_id=None,
        scene="c2c",
        timestamp="",
        raw={},
    )
    base.update(overrides)
    return IncomingMessage(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #
def test_parse_attachments() -> None:
    data = {
        "attachments": [
            {"content_type": "file", "filename": "库存.xlsx", "url": "https://cdn/x", "size": 1234},
            {"content_type": "image/png", "url": "https://cdn/y"},
        ]
    }
    items = parse_attachments(data)
    assert len(items) == 2
    assert items[0].filename == "库存.xlsx"
    assert items[0].is_file and not items[0].is_image
    assert items[1].is_image and not items[1].is_file


@pytest.mark.parametrize(
    "data",
    [{}, {"attachments": None}, {"attachments": "x"}, {"attachments": [1, "a", {}]}, {"attachments": [{"url": ""}]}],
)
def test_parse_attachments_is_robust(data: dict) -> None:
    assert parse_attachments(data) == ()


def test_parse_event_carries_attachments() -> None:
    msg = parse_event(
        "C2C_MESSAGE_CREATE",
        {
            "id": "m1",
            "content": "",
            "author": {"user_openid": "u1"},
            "attachments": [{"content_type": "file", "filename": "a.csv", "url": "https://cdn/a"}],
        },
    )
    assert msg is not None
    assert len(msg.attachments) == 1
    assert msg.files[0].filename == "a.csv"


def test_message_without_attachments_is_unchanged() -> None:
    msg = parse_event("C2C_MESSAGE_CREATE", {"id": "m", "content": "库存", "author": {"user_openid": "u"}})
    assert msg is not None and msg.attachments == () and msg.files == ()


# --------------------------------------------------------------------------- #
# 编排
# --------------------------------------------------------------------------- #
@pytest.fixture()
def bot_settings() -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        qq_bot_enabled=True,
        qq_app_id="1",
        qq_app_secret="s",
        qq_max_file_mb=1,
    )


def _bot(settings: Settings, download: bytes | None = b"name,qty\nNE555,30\n"):
    captured: dict = {}

    async def handler(_msg):
        return "文本回复"

    async def attachment_handler(filename, data, _msg):
        captured["filename"] = filename
        captured["data"] = data
        return f"已收到 {filename}"

    bot = QQBot(settings, handler, attachment_handler)

    async def fake_download(_url: str):
        return download

    bot._download = fake_download  # type: ignore[method-assign]
    return bot, captured


def test_file_attachment_goes_to_attachment_handler(bot_settings: Settings) -> None:
    bot, captured = _bot(bot_settings)
    msg = _message(attachments=(Attachment("file", "库存.csv", "https://cdn/a", 20),))
    reply = asyncio.run(bot.handle_incoming(msg))
    assert reply == "已收到 库存.csv"
    assert captured["data"] == b"name,qty\nNE555,30\n"


def test_image_attachment_gets_helpful_reply(bot_settings: Settings) -> None:
    bot, captured = _bot(bot_settings)
    msg = _message(attachments=(Attachment("image/png", "a.png", "https://cdn/a", 20),))
    reply = asyncio.run(bot.handle_incoming(msg))
    assert "图片" in reply and "看不懂" in reply
    assert captured == {}


@pytest.mark.parametrize("content_type", ["voice", "video/mp4"])
def test_voice_and_video_get_helpful_reply(bot_settings: Settings, content_type: str) -> None:
    bot, captured = _bot(bot_settings)
    msg = _message(attachments=(Attachment(content_type, "a", "https://cdn/a", 20),))
    reply = asyncio.run(bot.handle_incoming(msg))
    assert "语音和视频" in reply
    assert captured == {}


def test_attachment_classification() -> None:
    assert Attachment("file", "a.xlsx", "u").is_file
    assert Attachment("application/pdf", "a.pdf", "u").is_file
    assert not Attachment("image/jpeg", "a.jpg", "u").is_file
    assert not Attachment("voice", "a", "u").is_file
    assert Attachment("voice", "a", "u").is_voice_or_video
    assert not Attachment("file", "a.xlsx", "").is_file      # 没有 url 就没法下载


def test_oversized_attachment_is_rejected(bot_settings: Settings) -> None:
    bot, captured = _bot(bot_settings)
    msg = _message(attachments=(Attachment("file", "big.csv", "https://cdn/a", 99 * 1024 * 1024),))
    reply = asyncio.run(bot.handle_incoming(msg))
    assert "文件太大" in reply
    assert captured == {}


def test_download_failure_is_reported(bot_settings: Settings) -> None:
    bot, captured = _bot(bot_settings, download=None)
    msg = _message(attachments=(Attachment("file", "a.csv", "https://cdn/a", 20),))
    reply = asyncio.run(bot.handle_incoming(msg))
    assert "下载失败" in reply
    assert captured == {}


def test_attachment_handler_exception_is_swallowed(bot_settings: Settings) -> None:
    async def boom(_filename, _data, _msg):
        raise RuntimeError("炸")

    bot = QQBot(bot_settings, lambda _m: None, boom)  # type: ignore[arg-type]

    async def fake_download(_url):
        return b"x"

    bot._download = fake_download  # type: ignore[method-assign]
    msg = _message(attachments=(Attachment("file", "a.csv", "https://cdn/a", 1),))
    assert "出错" in (asyncio.run(bot.handle_incoming(msg)) or "")


def test_no_attachment_handler_is_ignored(bot_settings: Settings) -> None:
    bot = QQBot(bot_settings, lambda _m: None)  # type: ignore[arg-type]
    msg = _message(attachments=(Attachment("file", "a.csv", "https://cdn/a", 1),))
    assert asyncio.run(bot.handle_incoming(msg)) is None
