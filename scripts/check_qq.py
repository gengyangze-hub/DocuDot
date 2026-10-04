#!/usr/bin/env python3
"""QQ 适配层离线自检：不联网，验证协议纯函数与消息编排是否可用。

    python3 scripts/check_qq.py

只做「能加载 + 关键行为正确」的检查；真实的鉴权/心跳/收发需要
填好 QQ_APP_ID / QQ_APP_SECRET 并跑真实机器人才能验证。
"""

from __future__ import annotations

import asyncio
import dataclasses
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings  # noqa: E402
from app.qq import protocol  # noqa: E402
from app.qq.bot import QQBot  # noqa: E402
from app.qq.gateway import classify_payload, next_backoff  # noqa: E402
from app.qq.openapi import QQOpenAPI  # noqa: E402
from app.qq.types import IncomingMessage  # noqa: E402

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    (PASSED if condition else FAILED).append(label if condition else (label, detail))


def main() -> int:
    settings = Settings(_env_file=None, qq_bot_enabled=False)  # type: ignore[call-arg]

    # ---- 数据结构 ----
    fields = {f.name for f in dataclasses.fields(IncomingMessage)}
    check(
        "IncomingMessage 字段齐全",
        {"message_id", "content", "user_openid", "scene", "group_openid"} <= fields,
        f"实际 {sorted(fields)}",
    )

    # ---- 协议 ----
    identify = protocol.build_identify("tok", protocol.INTENT_GROUP_AND_C2C_EVENT)
    check("IDENTIFY op=2", identify.get("op") == 2, str(identify))
    check("IDENTIFY token 带 QQBot 前缀", str(identify["d"]["token"]).startswith("QQBot "))
    check("心跳报文 d 可为 null", protocol.build_heartbeat(None) == {"op": 1, "d": None})
    check("心跳报文带 seq", protocol.build_heartbeat(7) == {"op": 1, "d": 7})
    check("HELLO 识别", protocol.is_hello({"op": 10, "d": {"heartbeat_interval": 30000}}))
    check("心跳间隔读取", protocol.heartbeat_interval({"op": 10, "d": {"heartbeat_interval": 30000}}) == 30000)
    check("心跳间隔缺省兜底", protocol.heartbeat_interval({"op": 10, "d": {}}) > 0)
    check(
        "@ 提及清洗",
        protocol.normalize_text_content("<@!123456> 查 STM32") == "查 STM32",
        repr(protocol.normalize_text_content("<@!123456> 查 STM32")),
    )

    # ---- 事件解析 ----
    c2c = protocol.parse_event(
        "C2C_MESSAGE_CREATE",
        {"id": "m1", "content": "查 电容", "author": {"user_openid": "u1"}, "timestamp": "t"},
    )
    check("C2C 事件解析", c2c is not None and c2c.scene == "c2c" and c2c.user_openid == "u1")
    group = protocol.parse_event(
        "GROUP_AT_MESSAGE_CREATE",
        {"id": "m2", "content": "库存", "author": {"member_openid": "u2"}, "group_openid": "g1"},
    )
    check("群事件解析", group is not None and group.scene == "group" and group.group_openid == "g1")
    check("未知事件返回 None", protocol.parse_event("GUILD_CREATE", {}) is None)
    check("缺字段不抛异常", protocol.parse_event("C2C_MESSAGE_CREATE", {}) is not None)

    # ---- 白名单 ----
    msg = IncomingMessage("m", "库存", "库存", "u1", "g1", None, None, "group", "", {})
    check("白名单为空时放行", protocol.is_allowed(msg, set(), set()))
    check("用户白名单命中", protocol.is_allowed(msg, {"u1"}, set()))
    check("群白名单命中", protocol.is_allowed(msg, set(), {"g1"}))
    check("不在白名单则拒绝", not protocol.is_allowed(msg, {"other"}, {"other-group"}))

    # ---- 网关纯函数 ----
    check("OP0 分发识别", classify_payload({"op": 0, "t": "C2C_MESSAGE_CREATE", "d": {}})[0] == "dispatch")
    check("退避递增且有上限", next_backoff(0) <= next_backoff(3) and next_backoff(20) <= 60)

    # ---- 编排 ----
    async def scenario() -> None:
        seen: list[str] = []

        async def handler(message: IncomingMessage) -> str:
            seen.append(message.content)
            return f"收到：{message.content}"

        bot = QQBot(settings, handler)
        check("未启用时 status.running 为假", bot.status().get("running") is False)
        reply = await bot.handle_incoming(msg)
        check("handle_incoming 调用 handler 并返回回复", reply == "收到：库存", repr(reply))
        check("传入 handler 的正文正确", seen == ["库存"], repr(seen))

        async def boom(_: IncomingMessage) -> str:
            raise RuntimeError("故意炸")

        bot2 = QQBot(settings, boom)
        survivable = await bot2.handle_incoming(msg)
        check("handler 异常被吞掉并返回友好文本", isinstance(survivable, str) and "出错" in survivable, repr(survivable))

        strict = Settings(_env_file=None, qq_bot_enabled=False, qq_allowed_users="someone-else")  # type: ignore[call-arg]
        bot3 = QQBot(strict, handler)
        check("白名单外消息不回复", await bot3.handle_incoming(msg) is None)

        api = QQOpenAPI(settings)
        check("QQOpenAPI 可实例化", api is not None)
        await api.close()

    asyncio.run(scenario())

    total = len(PASSED) + len(FAILED)
    print(f"QQ 适配层自检：{len(PASSED)}/{total} 项通过")
    for label, detail in FAILED:
        print(f"  ✗ {label}")
        if detail:
            print(f"      {detail}")
    if FAILED:
        return 1
    print("全部通过 ✓（注意：真实鉴权与 WebSocket 收发仍需线上凭证验证）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
