"""``app/qq/bot.py`` 的行为单测。

原则：**不联网**。所有外部依赖（QQOpenAPI / QQGateway）都用注入或
``unittest.mock`` 打桩；``QQBot.start()`` 只在「未启用」分支下被验证，
不会真的去建连。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.qq.bot import HANDLER_ERROR_REPLY, QQBot
from app.qq.types import SCENE_C2C, SCENE_GROUP, IncomingMessage


# --------------------------------------------------------------------------- #
# 测试脚手架
# --------------------------------------------------------------------------- #


def make_settings(**overrides: object) -> SimpleNamespace:
    """构造一个与 ``app.config.Settings`` 字段兼容的轻量配置对象。"""
    defaults: dict[str, object] = {
        "qq_bot_enabled": True,
        "qq_app_id": "102000000",
        "qq_app_secret": "secret",
        "qq_bot_token": "",
        "qq_sandbox": True,
        "qq_intents": (1 << 30) | (1 << 25),
        "qq_allowed_users": "",
        "qq_allowed_groups": "",
        "qq_command_prefix": "",
        "allowed_users": set(),
        "allowed_groups": set(),
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def make_msg(
    *,
    content: str = "查 STM32",
    user_openid: str = "user-1",
    group_openid: str | None = None,
    scene: str = SCENE_C2C,
    message_id: str = "msg-1",
) -> IncomingMessage:
    return IncomingMessage(
        message_id=message_id,
        content=content,
        raw_content=content,
        user_openid=user_openid,
        group_openid=group_openid,
        guild_id=None,
        channel_id=None,
        scene=scene,
        timestamp="1700000000",
        raw={},
    )


def run(coro):
    """在同步测试里跑一个协程。"""
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# start / stop / status
# --------------------------------------------------------------------------- #


def test_start_when_disabled_does_not_raise_and_status_is_correct() -> None:
    settings = make_settings(qq_bot_enabled=False)
    bot = QQBot(settings, AsyncMock(return_value=None))

    run(bot.start())  # 不应抛异常，也不应创建后台任务

    assert bot._task is None  # noqa: SLF001 - 断言未启动
    status = bot.status()
    assert status == {
        "enabled": False,
        "running": False,
        "app_id": "102000000",
        "sandbox": True,
        "scene_stats": {"c2c": 0, "group": 0, "guild": 0},
    }


def test_start_enabled_but_missing_credentials_does_not_raise() -> None:
    settings = make_settings(qq_app_id="", qq_app_secret="", qq_bot_token="")
    bot = QQBot(settings, AsyncMock(return_value=None))

    run(bot.start())

    assert bot._task is None  # noqa: SLF001
    assert bot.status()["enabled"] is True
    assert bot.status()["running"] is False


def test_start_with_bot_token_only_is_considered_ready() -> None:
    settings = make_settings(qq_app_secret="", qq_bot_token="prebuilt-token")
    bot = QQBot(settings, AsyncMock(return_value=None))
    assert bot.has_credentials is True


def test_stop_without_start_is_safe() -> None:
    bot = QQBot(make_settings(), AsyncMock(return_value=None))
    run(bot.stop())
    assert bot.status()["running"] is False


def test_status_scene_stats_is_a_copy() -> None:
    bot = QQBot(make_settings(), AsyncMock(return_value=None))
    stats = bot.status()["scene_stats"]
    stats["c2c"] = 999
    assert bot.status()["scene_stats"]["c2c"] == 0


# --------------------------------------------------------------------------- #
# handle_incoming：白名单
# --------------------------------------------------------------------------- #


def test_handle_incoming_blocks_user_not_in_whitelist() -> None:
    handler = AsyncMock(return_value="OK")
    bot = QQBot(make_settings(allowed_users={"u-allowed"}), handler)

    result = run(bot.handle_incoming(make_msg(user_openid="u-other")))

    assert result is None
    handler.assert_not_awaited()


def test_handle_incoming_allows_user_in_whitelist() -> None:
    handler = AsyncMock(return_value="OK")
    bot = QQBot(make_settings(allowed_users={"u1"}), handler)

    result = run(bot.handle_incoming(make_msg(user_openid="u1")))

    assert result == "OK"
    handler.assert_awaited_once()


def test_handle_incoming_allows_group_in_whitelist() -> None:
    handler = AsyncMock(return_value="OK")
    bot = QQBot(make_settings(allowed_groups={"g1"}), handler)

    msg = make_msg(user_openid="u9", group_openid="g1", scene=SCENE_GROUP)
    assert run(bot.handle_incoming(msg)) == "OK"


def test_handle_incoming_blocks_group_not_in_whitelist() -> None:
    handler = AsyncMock(return_value="OK")
    bot = QQBot(make_settings(allowed_groups={"g1"}), handler)

    msg = make_msg(user_openid="u9", group_openid="g2", scene=SCENE_GROUP)
    assert run(bot.handle_incoming(msg)) is None
    handler.assert_not_awaited()


def test_handle_incoming_allows_everything_when_whitelists_empty() -> None:
    handler = AsyncMock(return_value="OK")
    bot = QQBot(make_settings(allowed_users=set(), allowed_groups=set()), handler)
    assert run(bot.handle_incoming(make_msg())) == "OK"


# --------------------------------------------------------------------------- #
# handle_incoming：前缀
# --------------------------------------------------------------------------- #


def test_handle_incoming_strips_prefix_before_calling_handler() -> None:
    seen: list[str] = []

    async def handler(msg: IncomingMessage) -> str:
        seen.append(msg.content)
        return "OK"

    bot = QQBot(make_settings(qq_command_prefix="/"), handler)
    result = run(bot.handle_incoming(make_msg(content="/查 STM32")))

    assert result == "OK"
    assert seen == ["查 STM32"]


def test_handle_incoming_ignores_message_without_prefix() -> None:
    handler = AsyncMock(return_value="OK")
    bot = QQBot(make_settings(qq_command_prefix="/"), handler)

    assert run(bot.handle_incoming(make_msg(content="查 STM32"))) is None
    handler.assert_not_awaited()


def test_handle_incoming_prefix_alone_is_passed_as_empty_content() -> None:
    seen: list[str] = []

    async def handler(msg: IncomingMessage) -> str | None:
        seen.append(msg.content)
        return None

    bot = QQBot(make_settings(qq_command_prefix="/"), handler)
    assert run(bot.handle_incoming(make_msg(content="/"))) is None
    assert seen == [""]


def test_handle_incoming_without_prefix_keeps_content() -> None:
    seen: list[str] = []

    async def handler(msg: IncomingMessage) -> str:
        seen.append(msg.content)
        return "OK"

    bot = QQBot(make_settings(qq_command_prefix=""), handler)
    assert run(bot.handle_incoming(make_msg(content="  查 STM32  "))) == "OK"
    assert seen == ["  查 STM32  "]


def test_handle_incoming_original_message_is_not_mutated() -> None:
    msg = make_msg(content="/查 STM32")
    bot = QQBot(make_settings(qq_command_prefix="/"), AsyncMock(return_value="OK"))

    run(bot.handle_incoming(msg))

    assert msg.content == "/查 STM32"


# --------------------------------------------------------------------------- #
# handle_incoming：handler 异常与空回复
# --------------------------------------------------------------------------- #


def test_handle_incoming_swallows_handler_exception() -> None:
    async def boom(_msg: IncomingMessage) -> str | None:
        raise ValueError("业务炸了")

    bot = QQBot(make_settings(), boom)

    result = run(bot.handle_incoming(make_msg()))

    assert result == HANDLER_ERROR_REPLY


@pytest.mark.parametrize("exc", [RuntimeError("x"), KeyError("k"), Exception("y")])
def test_handle_incoming_swallows_various_exceptions(exc: Exception) -> None:
    async def boom(_msg: IncomingMessage) -> str | None:
        raise exc

    bot = QQBot(make_settings(), boom)
    assert run(bot.handle_incoming(make_msg())) == HANDLER_ERROR_REPLY


def test_handle_incoming_returns_none_when_handler_returns_none() -> None:
    bot = QQBot(make_settings(), AsyncMock(return_value=None))
    assert run(bot.handle_incoming(make_msg())) is None


def test_handle_incoming_returns_none_when_handler_returns_empty() -> None:
    bot = QQBot(make_settings(), AsyncMock(return_value=""))
    assert run(bot.handle_incoming(make_msg())) is None


# --------------------------------------------------------------------------- #
# handle_event：解析 + 按场景下发
# --------------------------------------------------------------------------- #


def make_openapi_stub() -> SimpleNamespace:
    return SimpleNamespace(
        send_c2c_message=AsyncMock(return_value={"id": "r1"}),
        send_group_message=AsyncMock(return_value={"id": "r2"}),
    )


def test_handle_event_c2c_sends_c2c_message() -> None:
    bot = QQBot(make_settings(), AsyncMock(return_value="库存 3 件"))
    stub = make_openapi_stub()
    bot._openapi = stub  # noqa: SLF001 - 注入替身，避免真实网络

    run(
        bot.handle_event(
            "C2C_MESSAGE_CREATE",
            {
                "id": "m-1",
                "content": "<@!1> 查 STM32",
                "author": {"user_openid": "u-1"},
            },
        )
    )

    stub.send_c2c_message.assert_awaited_once_with("u-1", "库存 3 件", msg_id="m-1")
    stub.send_group_message.assert_not_awaited()
    assert bot.scene_stats["c2c"] == 1


def test_handle_event_group_sends_group_message() -> None:
    bot = QQBot(make_settings(), AsyncMock(return_value="已出库"))
    stub = make_openapi_stub()
    bot._openapi = stub  # noqa: SLF001

    run(
        bot.handle_event(
            "GROUP_AT_MESSAGE_CREATE",
            {
                "id": "m-2",
                "content": "<@!1> 出库 A100",
                "group_openid": "g-1",
                "author": {"member_openid": "mem-1"},
            },
        )
    )

    stub.send_group_message.assert_awaited_once_with("g-1", "已出库", msg_id="m-2")
    stub.send_c2c_message.assert_not_awaited()
    assert bot.scene_stats["group"] == 1


def test_handle_event_guild_does_not_reply() -> None:
    bot = QQBot(make_settings(), AsyncMock(return_value="频道回复"))
    stub = make_openapi_stub()
    bot._openapi = stub  # noqa: SLF001

    run(
        bot.handle_event(
            "AT_MESSAGE_CREATE",
            {
                "id": "m-3",
                "content": "<@!1> 库存",
                "channel_id": "c-1",
                "guild_id": "gd-1",
                "author": {"id": "a-1"},
            },
        )
    )

    stub.send_c2c_message.assert_not_awaited()
    stub.send_group_message.assert_not_awaited()
    assert bot.scene_stats["guild"] == 1


def test_handle_event_unknown_type_is_ignored() -> None:
    handler = AsyncMock(return_value="X")
    bot = QQBot(make_settings(), handler)

    run(bot.handle_event("FRIEND_ADD", {"id": "1"}))

    handler.assert_not_awaited()
    assert bot.scene_stats == {"c2c": 0, "group": 0, "guild": 0}


def test_handle_event_whitelist_blocked_does_not_send() -> None:
    bot = QQBot(make_settings(allowed_users={"only-me"}), AsyncMock(return_value="X"))
    stub = make_openapi_stub()
    bot._openapi = stub  # noqa: SLF001

    run(
        bot.handle_event(
            "C2C_MESSAGE_CREATE",
            {"id": "m-4", "content": "hi", "author": {"user_openid": "intruder"}},
        )
    )

    stub.send_c2c_message.assert_not_awaited()


def test_handle_event_send_failure_is_swallowed() -> None:
    """下发接口抛异常时不能冒泡（否则会打断网关循环）。"""
    bot = QQBot(make_settings(), AsyncMock(return_value="OK"))
    stub = SimpleNamespace(
        send_c2c_message=AsyncMock(side_effect=RuntimeError("网络炸了")),
        send_group_message=AsyncMock(),
    )
    bot._openapi = stub  # noqa: SLF001

    run(
        bot.handle_event(
            "C2C_MESSAGE_CREATE",
            {"id": "m-5", "content": "hi", "author": {"user_openid": "u-1"}},
        )
    )

    stub.send_c2c_message.assert_awaited_once()


def test_handle_event_without_openapi_does_not_raise() -> None:
    bot = QQBot(make_settings(), AsyncMock(return_value="OK"))

    run(
        bot.handle_event(
            "C2C_MESSAGE_CREATE",
            {"id": "m-6", "content": "hi", "author": {"user_openid": "u-1"}},
        )
    )

    assert bot.scene_stats["c2c"] == 1


def test_handle_event_group_without_group_openid_does_not_send() -> None:
    bot = QQBot(make_settings(), AsyncMock(return_value="OK"))
    stub = make_openapi_stub()
    bot._openapi = stub  # noqa: SLF001

    run(bot.handle_event("GROUP_AT_MESSAGE_CREATE", {"id": "m-7", "content": "x"}))

    stub.send_group_message.assert_not_awaited()
