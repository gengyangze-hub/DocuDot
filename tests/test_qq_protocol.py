"""``app/qq/protocol.py`` 的纯函数单测：**不联网**、不依赖 aiohttp。"""

from __future__ import annotations

import pytest

from app.qq import protocol
from app.qq import gateway
from app.qq.protocol import (
    OP_DISPATCH,
    OP_HEARTBEAT,
    OP_HEARTBEAT_ACK,
    OP_HELLO,
    OP_IDENTIFY,
    OP_INVALID_SESSION,
    OP_RECONNECT,
    OP_RESUME,
    build_heartbeat,
    build_identify,
    build_resume,
    heartbeat_interval,
    is_allowed,
    is_hello,
    normalize_text_content,
    parse_event,
    resolve_api_base,
    resolve_token_url,
)
from app.qq.types import SCENE_C2C, SCENE_GUILD, SCENE_GROUP, IncomingMessage


def make_msg(
    *,
    user_openid: str = "user-1",
    group_openid: str | None = None,
    scene: str = SCENE_C2C,
) -> IncomingMessage:
    """构造一条最小可用的入站消息。"""
    return IncomingMessage(
        message_id="msg-1",
        content="查 STM32",
        raw_content="查 STM32",
        user_openid=user_openid,
        group_openid=group_openid,
        guild_id=None,
        channel_id=None,
        scene=scene,
        timestamp="1700000000",
        raw={},
    )


# --------------------------------------------------------------------------- #
# OP 常量
# --------------------------------------------------------------------------- #


def test_op_constants_match_qq_spec() -> None:
    assert (OP_DISPATCH, OP_HEARTBEAT, OP_IDENTIFY) == (0, 1, 2)
    assert (OP_RESUME, OP_RECONNECT, OP_INVALID_SESSION) == (6, 7, 9)
    assert (OP_HELLO, OP_HEARTBEAT_ACK) == (10, 11)


def test_intent_constants_are_bit_flags() -> None:
    assert protocol.INTENT_PUBLIC_GUILD_MESSAGES == 1 << 30
    assert protocol.INTENT_GROUP_AND_C2C_EVENT == 1 << 25
    assert protocol.INTENT_GUILD_MESSAGES == 1 << 9
    assert protocol.INTENT_DIRECT_MESSAGE == 1 << 12


# --------------------------------------------------------------------------- #
# 报文构造
# --------------------------------------------------------------------------- #


def test_build_identify_structure() -> None:
    payload = build_identify("abc", 12345)

    assert payload["op"] == OP_IDENTIFY
    data = payload["d"]
    assert data["token"] == "QQBot abc"
    assert data["intents"] == 12345
    assert data["shard"] == [0, 1]
    assert isinstance(data["properties"], dict) and data["properties"]
    # properties 里应是 ``$`` 前缀的规范字段
    assert all(key.startswith("$") for key in data["properties"])


def test_build_identify_custom_shard() -> None:
    payload = build_identify("t", 1, shard=(2, 4))
    assert payload["d"]["shard"] == [2, 4]


def test_build_resume_structure() -> None:
    payload = build_resume("abc", "sess-9", 42)

    assert payload["op"] == OP_RESUME
    assert payload["d"] == {"token": "QQBot abc", "session_id": "sess-9", "seq": 42}


def test_build_heartbeat_with_seq() -> None:
    assert build_heartbeat(7) == {"op": OP_HEARTBEAT, "d": 7}


def test_build_heartbeat_without_seq_is_null() -> None:
    payload = build_heartbeat(None)
    assert payload["op"] == OP_HEARTBEAT
    assert payload["d"] is None


# --------------------------------------------------------------------------- #
# HELLO / heartbeat_interval
# --------------------------------------------------------------------------- #


def test_is_hello() -> None:
    assert is_hello({"op": OP_HELLO, "d": {"heartbeat_interval": 45000}}) is True
    assert is_hello({"op": OP_DISPATCH, "t": "READY", "d": {}}) is False
    assert is_hello(None) is False
    assert is_hello({"op": "10"}) is False  # 字符串不算


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"op": OP_HELLO, "d": {"heartbeat_interval": 45000}}, 45000),
        ({"op": OP_HELLO, "d": {"heartbeat_interval": "30000"}}, 30000),
        ({"op": OP_HELLO, "d": {"heartbeat_interval": 41250.0}}, 41250),
    ],
)
def test_heartbeat_interval_normal(payload: dict, expected: int) -> None:
    assert heartbeat_interval(payload) == expected


@pytest.mark.parametrize(
    "payload",
    [
        None,
        "not-a-dict",
        [],
        {},  # 无 op / 无 d
        {"op": OP_HELLO},  # 无 d
        {"op": OP_HELLO, "d": None},
        {"op": OP_HELLO, "d": {}},  # 无字段
        {"op": OP_HELLO, "d": {"heartbeat_interval": None}},
        {"op": OP_HELLO, "d": {"heartbeat_interval": "abc"}},
        {"op": OP_HELLO, "d": {"heartbeat_interval": [1, 2]}},
        {"op": OP_HELLO, "d": {"heartbeat_interval": True}},  # bool 视为非法
        {"op": OP_HELLO, "d": {"heartbeat_interval": 0}},
        {"op": OP_HELLO, "d": {"heartbeat_interval": -5}},
    ],
)
def test_heartbeat_interval_fallback_to_default(payload: object) -> None:
    assert heartbeat_interval(payload) == 41250


def test_heartbeat_interval_custom_default() -> None:
    assert heartbeat_interval({}, default_ms=1000) == 1000


# --------------------------------------------------------------------------- #
# 文本归一化
# --------------------------------------------------------------------------- #


def test_normalize_text_content_strips_mention_and_whitespace() -> None:
    assert normalize_text_content("<@!123> 查 STM32") == "查 STM32"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("<@123>查 STM32", "查 STM32"),
        ("<@!123>   查 STM32   ", "查 STM32"),
        ("查 STM32", "查 STM32"),
        ("  <@!1> <@2>  查 STM32 ", "查 STM32"),
        ("", ""),
        (None, ""),
        ("<@!123>", ""),
    ],
)
def test_normalize_text_content_variants(raw: object, expected: str) -> None:
    assert normalize_text_content(raw) == expected  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# parse_event
# --------------------------------------------------------------------------- #


def test_parse_event_c2c() -> None:
    data = {
        "id": "msg-1",
        "content": "<@!123> 查 STM32",
        "timestamp": "1700000000",
        "author": {"user_openid": "user-openid-1"},
    }
    msg = parse_event("C2C_MESSAGE_CREATE", data)

    assert msg is not None
    assert msg.scene == SCENE_C2C
    assert msg.user_openid == "user-openid-1"
    assert msg.group_openid is None
    assert msg.guild_id is None
    assert msg.channel_id is None
    assert msg.message_id == "msg-1"
    assert msg.content == "查 STM32"
    assert msg.raw_content == "<@!123> 查 STM32"
    assert msg.timestamp == "1700000000"
    assert msg.raw is data


def test_parse_event_group() -> None:
    data = {
        "id": "msg-2",
        "content": "<@!9> 出库 A100",
        "timestamp": "1700000001",
        "group_openid": "group-1",
        "author": {"member_openid": "member-1"},
    }
    msg = parse_event("GROUP_AT_MESSAGE_CREATE", data)

    assert msg is not None
    assert msg.scene == SCENE_GROUP
    assert msg.user_openid == "member-1"
    assert msg.group_openid == "group-1"
    assert msg.content == "出库 A100"


@pytest.mark.parametrize("event_type", ["AT_MESSAGE_CREATE", "MESSAGE_CREATE"])
def test_parse_event_guild(event_type: str) -> None:
    data = {
        "id": "msg-3",
        "content": "<@!1> 库存",
        "timestamp": "1700000002",
        "channel_id": "chan-1",
        "guild_id": "guild-1",
        "author": {"id": "author-1"},
    }
    msg = parse_event(event_type, data)

    assert msg is not None
    assert msg.scene == SCENE_GUILD
    assert msg.user_openid == "author-1"
    assert msg.channel_id == "chan-1"
    assert msg.guild_id == "guild-1"
    assert msg.group_openid is None


@pytest.mark.parametrize(
    "event_type",
    ["READY", "RESUMED", "GROUP_ADD_ROBOT", "FRIEND_ADD", "", "unknown"],
)
def test_parse_event_unknown_type_returns_none(event_type: str) -> None:
    assert parse_event(event_type, {"id": "x", "content": "y"}) is None


def test_parse_event_missing_fields_is_robust() -> None:
    """缺字段 / 字段类型异常都不能抛 KeyError。"""
    for event_type in ("C2C_MESSAGE_CREATE", "GROUP_AT_MESSAGE_CREATE", "AT_MESSAGE_CREATE"):
        msg = parse_event(event_type, {})
        assert msg is not None
        assert msg.message_id == ""
        assert msg.content == ""
        assert msg.raw_content == ""
        assert msg.user_openid == ""
        assert msg.timestamp == ""
        assert msg.scene in {SCENE_C2C, SCENE_GROUP, SCENE_GUILD}

    # author 类型不对
    msg = parse_event("C2C_MESSAGE_CREATE", {"author": "not-a-dict"})
    assert msg is not None and msg.user_openid == ""

    # author 存在但缺 key
    msg = parse_event("GROUP_AT_MESSAGE_CREATE", {"author": {}})
    assert msg is not None and msg.user_openid == ""

    # content 为 None
    msg = parse_event("C2C_MESSAGE_CREATE", {"content": None})
    assert msg is not None and msg.content == "" and msg.raw_content == ""


def test_parse_event_non_dict_data_returns_none() -> None:
    assert parse_event("C2C_MESSAGE_CREATE", None) is None  # type: ignore[arg-type]
    assert parse_event("C2C_MESSAGE_CREATE", "oops") is None  # type: ignore[arg-type]


def test_parse_event_group_openid_empty_becomes_none() -> None:
    msg = parse_event("GROUP_AT_MESSAGE_CREATE", {"group_openid": ""})
    assert msg is not None and msg.group_openid is None


# --------------------------------------------------------------------------- #
# 地址
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("sandbox", "expected"),
    [
        (True, "https://sandbox.api.sgroup.qq.com"),
        (False, "https://api.sgroup.qq.com"),
    ],
)
def test_resolve_api_base(sandbox: bool, expected: str) -> None:
    assert resolve_api_base(sandbox) == expected


def test_resolve_token_url() -> None:
    assert resolve_token_url() == "https://bots.qq.com/app/getAppAccessToken"


# --------------------------------------------------------------------------- #
# 白名单
# --------------------------------------------------------------------------- #


def test_is_allowed_both_empty_allows_everything() -> None:
    assert is_allowed(make_msg(), set(), set()) is True


def test_is_allowed_user_hit() -> None:
    assert is_allowed(make_msg(user_openid="u1"), {"u1"}, set()) is True


def test_is_allowed_group_hit() -> None:
    msg = make_msg(user_openid="u9", group_openid="g1", scene=SCENE_GROUP)
    assert is_allowed(msg, set(), {"g1"}) is True


def test_is_allowed_miss() -> None:
    msg = make_msg(user_openid="u9", group_openid="g9", scene=SCENE_GROUP)
    assert is_allowed(msg, {"u1"}, {"g1"}) is False


def test_is_allowed_group_whitelist_does_not_allow_c2c() -> None:
    """只有群白名单时，C2C 消息（无 group_openid）应被拒绝。"""
    assert is_allowed(make_msg(user_openid="u9"), set(), {"g1"}) is False


def test_is_allowed_user_whitelist_allows_group_member() -> None:
    """群消息里 member_openid 命中用户白名单也算放行（按规范）。"""
    msg = make_msg(user_openid="u1", group_openid="g9", scene=SCENE_GROUP)
    assert is_allowed(msg, {"u1"}, {"g1"}) is True


# --------------------------------------------------------------------------- #
# 网关纯函数（classify_payload 等，不涉及网络）
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("payload", "kind"),
    [
        ({"op": 0, "t": "X", "d": {"a": 1}}, gateway.KIND_DISPATCH),
        ({"op": 10, "d": {"heartbeat_interval": 45000}}, gateway.KIND_HELLO),
        ({"op": 11}, gateway.KIND_HEARTBEAT_ACK),
        ({"op": 1, "d": None}, gateway.KIND_HEARTBEAT),
        ({"op": 7}, gateway.KIND_RECONNECT),
        ({"op": 9, "d": True}, gateway.KIND_INVALID_SESSION),
        ({"op": 99}, gateway.KIND_UNKNOWN),
        (None, gateway.KIND_UNKNOWN),
        ("x", gateway.KIND_UNKNOWN),
    ],
)
def test_classify_payload(payload: object, kind: str) -> None:
    assert gateway.classify_payload(payload)[0] == kind


def test_classify_payload_dispatch_returns_event_data() -> None:
    assert gateway.classify_payload({"op": 0, "t": "X", "d": {"a": 1}}) == (
        gateway.KIND_DISPATCH,
        {"a": 1},
    )
    # d 不是字典时退化为空字典，避免下游 None 解包
    assert gateway.classify_payload({"op": 0, "t": "X", "d": None}) == (
        gateway.KIND_DISPATCH,
        {},
    )


def test_event_type_of() -> None:
    assert gateway.event_type_of({"op": 0, "t": "READY"}) == "READY"
    assert gateway.event_type_of({"op": 0}) == ""
    assert gateway.event_type_of({"op": 0, "t": 5}) == ""
    assert gateway.event_type_of(None) == ""


def test_sequence_of() -> None:
    assert gateway.sequence_of({"op": 0, "s": 3}) == 3
    assert gateway.sequence_of({"op": 0}) is None
    assert gateway.sequence_of({"op": 0, "s": True}) is None  # bool 不算序号
    assert gateway.sequence_of({"op": 0, "s": "3"}) is None
    assert gateway.sequence_of(None) is None


def test_next_backoff_grows_and_caps() -> None:
    values = [1.0]
    for _ in range(8):
        values.append(gateway.next_backoff(values[-1]))
    assert values[1:5] == [2.0, 4.0, 8.0, 16.0]
    assert max(values) == gateway.BACKOFF_MAX == 60.0
    assert gateway.next_backoff(0.0) == 2.0  # 非法输入按初始值处理
