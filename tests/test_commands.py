"""QQ 命令路由测试：多轮上下文、序号选择、整批确认、清单化展示。"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.bot.commands import parse_bulk_request, parse_merge_request
from app.bot.session import is_affirmative, is_negative, parse_selection
from app.models import BotCommandResponse, ItemCreate


# --------------------------------------------------------------------------- #
# 纯函数
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("text", "expected"),
    [("2", [2]), ("第2个", [2]), ("2号", [2]), ("1、3", [1, 3]), ("2和3", [2, 3])],
)
def test_parse_selection_positive(text: str, expected: list[int]) -> None:
    assert parse_selection(text) == expected


@pytest.mark.parametrize("text", ["STM32F103", "出库 2", "2个电容", "", "全部"])
def test_parse_selection_negative(text: str) -> None:
    assert parse_selection(text) == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("出库全部", ("all", None)),
        ("全部出库", ("all", None)),
        ("清空库存", ("all", None)),
        ("将这些物品全部出库", ("last", None)),
        ("这些全部出库", ("last", None)),
        # 分类不再是内置代码 —— 用户写的分类名原样带出去，是否存在由执行层查库确认
        ("STM元器件 全部出库", ("category", "STM元器件")),
        ("游戏卡全部清空", ("category", "游戏卡")),
    ],
)
def test_parse_bulk_request_positive(text: str, expected: tuple) -> None:
    assert parse_bulk_request(text) == expected


@pytest.mark.parametrize(
    "text",
    ["出库 STM32F103 5", "查 全部", "入库 NE555 10", "库存"],
)
def test_parse_bulk_request_negative(text: str) -> None:
    """这些不是整批请求 —— 否则会误清空整个库。"""
    assert parse_bulk_request(text) is None


def test_bulk_out_by_keyword_uses_search_not_whole_stock() -> None:
    """「出库 全部电容」不该被当成「清空全部」，而是一个关键词范围。"""
    assert parse_bulk_request("出库 全部电容") == ("category", "电容")


def test_affirmative_negative_words() -> None:
    assert is_affirmative("确认") and is_affirmative("好") and is_affirmative("OK")
    assert is_negative("取消") and is_negative("算了")
    assert not is_affirmative("确认一下")


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
SEED = [
    {"name": "STM32F103C8T6", "category": "STM元器件", "quantity": 25,
     "location": "A柜-1层", "spec": "LQFP48", "aliases": ["F103C8"]},
    {"name": "0.1uF 50V MLCC", "category": "STM元器件", "quantity": 500,
     "location": "A柜-2层", "spec": "0805", "aliases": ["104", "100nF"]},
    {"name": "Steam 50元充值卡", "category": "Steam游戏卡", "quantity": 10,
     "location": "B柜-抽屉1", "spec": "50元", "aliases": ["50元卡"]},
]


@pytest.fixture()
def router(app):
    for payload in SEED:
        app.state.ctx.inventory.create_item(ItemCreate(**payload))
    return app.state.ctx.commands


def say(router, text: str, *, conversation: str = "group-1", scene: str = "qq-group") -> BotCommandResponse:
    return asyncio.run(
        router.handle(text, operator="user-1", scene=scene, conversation=conversation)
    )


def quantities(app) -> dict[str, float]:
    return {record.name: record.quantity for record in app.state.ctx.repo.all_items()}


# --------------------------------------------------------------------------- #
# 清单化展示（用户反馈第 3 点：汇总要跟具体清单）
# --------------------------------------------------------------------------- #
def test_overview_includes_concrete_items(router) -> None:
    reply = say(router, "库存").reply
    assert "库存总览" in reply
    assert "物品清单" in reply
    # 每个物品都要出现，并且带规格/数量/位置（顺序按最近更新排，所以只断言内容）
    assert "STM32F103C8T6（LQFP48） × 25 @ A柜-1层" in reply
    assert "0.1uF 50V MLCC（0805） × 500 @ A柜-2层" in reply
    assert "Steam 50元充值卡（50元） × 10 @ B柜-抽屉1" in reply


def test_category_list_is_specific(router) -> None:
    reply = say(router, "STM元器件").reply
    assert "STM元器件" in reply
    assert "STM32F103C8T6" in reply and "0.1uF 50V MLCC" in reply
    assert "Steam 50元充值卡" not in reply


def test_category_command(router) -> None:
    reply = say(router, "分类 游戏卡").reply
    assert "Steam游戏卡" in reply
    assert "Steam 50元充值卡" in reply


# --------------------------------------------------------------------------- #
# 上下文：回序号
# --------------------------------------------------------------------------- #
def test_reply_with_number_shows_detail(router) -> None:
    say(router, "库存")
    reply = say(router, "2").reply
    assert "0.1uF 50V MLCC" in reply
    assert "位置：A柜-2层" in reply
    assert "别名：104、100nF" in reply


def test_ambiguous_then_number_completes_operation(app, router) -> None:
    """机器人列候选 → 用户回「2」→ 出库在选中的那条上完成。"""
    app.state.ctx.inventory.create_item(
        ItemCreate(name="AMS1117-3.3", quantity=10, location="D柜-1层")
    )
    app.state.ctx.inventory.create_item(
        ItemCreate(name="AMS1117-3.3", quantity=20, location="D柜-2层")
    )

    first = say(router, "出库 AMS1117-3.3 5")
    assert "1. AMS1117-3.3 × 10 @ D柜-1层" in first.reply
    assert "2. AMS1117-3.3 × 20 @ D柜-2层" in first.reply
    assert "回复序号" in first.reply
    assert "出库 5" in first.reply

    # 还没执行
    assert quantities(app)["AMS1117-3.3"] in {10.0, 20.0}

    done = say(router, "2")
    assert "出库成功" in done.reply
    records = [r for r in app.state.ctx.repo.all_items() if r.name == "AMS1117-3.3"]
    assert sorted(r.quantity for r in records) == [10.0, 15.0]


def test_selection_out_of_range(router) -> None:
    say(router, "库存")
    reply = say(router, "99").reply
    assert "没有第 99 项" in reply


def test_sessions_are_isolated(router) -> None:
    say(router, "库存", conversation="group-A")
    reply = say(router, "2", conversation="group-B").reply
    assert "0.1uF 50V MLCC" not in reply


# --------------------------------------------------------------------------- #
# 整批出库 + 二次确认
# --------------------------------------------------------------------------- #
def test_bulk_out_requires_confirmation(app, router) -> None:
    prompt = say(router, "出库全部").reply
    assert "整批出库" in prompt and "确认" in prompt and "取消" in prompt
    assert "全部库存" in prompt
    # 确认之前数据必须原封不动
    assert quantities(app) == {"STM32F103C8T6": 25.0, "0.1uF 50V MLCC": 500.0, "Steam 50元充值卡": 10.0}

    done = say(router, "确认").reply
    assert "已执行" in done and "清零 3 种" in done
    assert quantities(app) == {"STM32F103C8T6": 0.0, "0.1uF 50V MLCC": 0.0, "Steam 50元充值卡": 0.0}

    # 流水留痕
    _, movements = app.state.ctx.repo.list_movements(limit=50)
    assert sum(1 for m in movements if m["action"] == "out") == 3


def test_bulk_out_can_be_cancelled(app, router) -> None:
    say(router, "清空库存")
    reply = say(router, "取消").reply
    assert "已取消" in reply
    assert quantities(app)["STM32F103C8T6"] == 25.0


def test_bulk_out_with_reference_uses_last_listing(app, router) -> None:
    """「这些全部出库」只作用于上一次列出来的那批。"""
    say(router, "查 电容")           # 只列出电容
    prompt = say(router, "这些全部出库").reply
    assert "「电容」的搜索结果" in prompt

    say(router, "确认")
    state = quantities(app)
    assert state["0.1uF 50V MLCC"] == 0.0
    assert state["STM32F103C8T6"] == 25.0     # 没被牵连
    assert state["Steam 50元充值卡"] == 10.0


def test_bulk_out_by_category(app, router) -> None:
    prompt = say(router, "STM元器件 全部出库").reply
    assert "STM元器件" in prompt
    say(router, "确认")
    state = quantities(app)
    assert state["STM32F103C8T6"] == 0.0
    assert state["0.1uF 50V MLCC"] == 0.0
    assert state["Steam 50元充值卡"] == 10.0   # 游戏卡不动


def test_bulk_out_without_prior_listing(app, router) -> None:
    reply = say(router, "这些全部出库", conversation="fresh").reply
    assert "还不知道「这些」指哪些物品" in reply


def test_pending_blocks_other_commands(app, router) -> None:
    say(router, "出库全部")
    reply = say(router, "入库 NE555 10 @C柜").reply
    assert "还有一个待确认的操作" in reply
    assert quantities(app)["STM32F103C8T6"] == 25.0


# --------------------------------------------------------------------------- #
# 位置解析：容忍不写 @ （库主实测踩到的坑）
# --------------------------------------------------------------------------- #
def test_stock_accepts_location_without_at(app, router) -> None:
    """「入库 100欧姆电阻 100 C库」—— 位置没写 @ 也要能认出来。"""
    reply = say(router, "入库 100欧姆电阻 100 C库").reply
    assert "入库成功" in reply
    record = next(r for r in app.state.ctx.repo.all_items() if "欧姆电阻" in r.name)
    assert record.name == "100欧姆电阻"
    assert record.location == "C库"
    assert record.quantity == 100
    assert record.category == "未分类"      # 没有内置分类了，等 AI 归类


def test_stock_accepts_location_without_at_and_at_the_end(app, router) -> None:
    """位置写在最末尾、且没写 @ 也要能识别。"""
    reply = say(router, "入库 470uF 电解电容 50 B柜-1层").reply
    assert "入库成功" in reply
    record = next(r for r in app.state.ctx.repo.all_items() if "电解电容" in r.name)
    assert record.name == "470uF 电解电容"
    assert record.location == "B柜-1层"
    assert record.quantity == 50


def test_name_without_location_word_is_not_split(app, router) -> None:
    """「八方旅人 随身包」有空格，但尾部不是位置，不能被拆开。"""
    say(router, "入库 八方旅人 随身包 1")
    record = next(r for r in app.state.ctx.repo.all_items() if "八方旅人" in r.name)
    assert record.name == "八方旅人 随身包"
    assert record.location == ""
    assert record.category == "未分类"      # 分类交给 AI，「随身包」不再被关键词猜成游戏卡


# --------------------------------------------------------------------------- #
# 分类不能当物品操作
# --------------------------------------------------------------------------- #
def test_category_target_of_in_is_rejected(app, router) -> None:
    reply = say(router, "入库 STM元器件 100").reply
    assert "是分类" in reply
    assert len(app.state.ctx.repo.all_items()) == 3


def test_bulk_scope_does_not_match_similar_phrase(app, router) -> None:
    """「出库 全部电容」应被当成关键词范围（或普通出库），而不是清空全库。"""
    reply = say(router, "出库 全部电容").reply
    # 命中的只能是电容那一批，STM32 这类别的分类不能被波及
    if "整批出库" in reply:
        assert "STM32F103C8T6" not in reply
    assert quantities(app)["STM32F103C8T6"] == 25.0


# --------------------------------------------------------------------------- #
# 保留原有能力
# --------------------------------------------------------------------------- #
def test_help_and_normal_flow(app, router) -> None:
    assert "入库" in say(router, "帮助").reply
    assert "入库成功" in say(router, "入库 NE555 30 @C柜-1层 #555").reply
    assert "30" in say(router, "NE555 还有多少").reply
    assert "C柜-1层" in say(router, "位置 C柜-1层").reply


# --------------------------------------------------------------------------- #
# 入库后追问细节（封装 / 别名）
# --------------------------------------------------------------------------- #
def test_stock_in_asks_for_spec_then_alias(app, router) -> None:
    first = say(router, "入库 AT24C02 20 @C柜-2层")
    assert "入库成功" in first.reply
    assert "要补充规格/封装吗" in first.reply

    second = say(router, "SOP-8")
    assert "已记录规格：SOP-8" in second.reply
    assert "还有别的叫法吗" in second.reply

    third = say(router, "AT24C02N, 24C02")
    assert "已记录别名：AT24C02N、24C02" in third.reply
    assert "信息补全完成" in third.reply

    record = next(r for r in app.state.ctx.repo.all_items() if r.name == "AT24C02")
    assert record.spec == "SOP-8"
    assert set(record.aliases) == {"AT24C02N", "24C02"}


def test_steam_card_skips_detail_prompts(app, router) -> None:
    """游戏卡/充值卡不该被问「封装」—— 由 AI 判定不需要追问，入库即完成。"""
    _install_fake_llm(app, '{"ask_spec": false, "ask_alias": false, "reason": "充值卡没有封装"}')
    reply = say(router, "入库 Steam 钱包充值码 3 @B柜").reply
    assert "入库成功" in reply
    assert "补充规格" not in reply
    assert "叫法" not in reply


def test_rules_skip_ai_for_components(app, router) -> None:
    """有型号的元件由规则判定要问封装，不必额外花一次 LLM 往返。"""
    fake = _install_fake_llm(app, '{"ask_spec": false, "ask_alias": false}')
    reply = say(router, "入库 NE555 30 @C柜").reply
    assert "要补充规格/封装吗" in reply
    assert fake.calls == []


def test_ai_decides_not_to_ask(app, router) -> None:
    """名字规则判不了 → 交给 AI；AI 说不用问，就直接完成。"""
    fake = _install_fake_llm(
        app, '{"ask_spec": false, "ask_alias": false, "reason": "叫主角的东西不需要封装"}'
    )
    reply = say(router, "入库 主角 1 书架").reply
    assert "入库成功" in reply
    assert "跳过" not in reply
    assert "叫法" not in reply
    assert fake.calls, "规则判不了时应该问过 AI"


def test_ai_decides_to_ask_alias_only(app, router) -> None:
    _install_fake_llm(
        app, '{"ask_spec": false, "ask_alias": true, "reason": "杜邦线又叫跳线"}'
    )
    reply = say(router, "入库 杜邦线 10").reply
    assert "叫法" in reply
    assert "补充规格" not in reply


def test_detail_prompts_never_mode(app, router) -> None:
    app.state.ctx.settings.qq_detail_prompts = "never"
    reply = say(router, "入库 杜邦线 10").reply
    assert "入库成功" in reply
    assert "跳过" not in reply
    app.state.ctx.settings.qq_detail_prompts = "auto"


def test_detail_prompts_always_mode(app, router) -> None:
    app.state.ctx.settings.qq_detail_prompts = "always"
    reply = say(router, "入库 主角 1 书架").reply
    assert "补充规格" in reply
    app.state.ctx.settings.qq_detail_prompts = "auto"


def test_ai_failure_falls_back_to_asking(app, router) -> None:
    """AI 判断失败不能挡住入库，回退成「缺什么问什么」。"""
    class BrokenLLM(FakeLLM):
        async def chat(self, messages, **kwargs):
            raise RuntimeError("炸")

    app.state.ctx.ai.llm = BrokenLLM("{}")
    reply = say(router, "入库 主角 1 书架").reply
    assert "入库成功" in reply
    assert "补充规格" in reply


def test_game_cartridge_does_not_get_asked_for_spec(app, router) -> None:
    """实测踩到的：Switch 卡带被当成元器件追问封装。

    现在没有内置分类了 —— 由 AI 判断「游戏卡带需不需要问封装」。
    """
    _install_fake_llm(
        app,
        '{"ask_spec": false, "ask_alias": false, "reason": "卡带没有封装这回事"}',
    )
    first = say(router, "入库 Switch卡带 巫师三 1").reply
    assert "入库成功" in first
    assert "补充规格" not in first
    # 分类交给 AI，不再由「卡带」关键词猜成内置分类
    record = next(r for r in app.state.ctx.repo.all_items() if "Switch卡带" in r.name)
    assert record.category == "未分类"


def test_category_correction_is_not_stored_as_spec(app, router) -> None:
    """用户纠正分类时，那句吐槽不能被当成规格存下来。

    现在分类名**直接取自用户说法** —— 他说「这是游戏卡」，就建「游戏卡」这个分类。
    """
    say(router, "入库 神秘物件 1")                       # 猜不出分类 → 追问
    reply = say(router, "这没有封装啊，这是游戏卡").reply
    assert "已按分类处理" in reply
    assert "游戏卡" in reply
    record = next(r for r in app.state.ctx.repo.all_items() if r.name == "神秘物件")
    assert record.category == "游戏卡"
    assert record.spec == ""                             # 没被污染


def test_prompt_can_be_skipped_and_is_not_repeated(app, router) -> None:
    say(router, "入库 NE555 30 @C柜-1层")
    assert "已跳过" in say(router, "跳过").reply
    assert "信息补全完成" in say(router, "跳过").reply

    # 同一会话里不再重复追问同一个字段
    again = say(router, "入库 NE555 5 @C柜-1层")
    assert "要补充规格/封装吗" not in again.reply
    assert "入库成功" in again.reply


def test_prompt_does_not_swallow_real_command(app, router) -> None:
    """追问期间用户直接下指令，不能被当成规格记进去。"""
    say(router, "入库 NE555 30 @C柜-1层")
    reply = say(router, "库存")
    assert "库存总览" in reply.reply
    record = next(r for r in app.state.ctx.repo.all_items() if r.name == "NE555")
    assert record.spec == ""


def test_prompt_can_be_cancelled(app, router) -> None:
    say(router, "入库 NE555 30 @C柜-1层")
    assert "不再追问" in say(router, "取消").reply
    assert "库存总览" in say(router, "库存").reply


def test_ask_can_be_disabled(app) -> None:
    from app.main import create_app
    from app.services.inventory import InventoryService  # noqa: F401

    app.state.ctx.settings.qq_ask_on_stock_in = False
    reply = say(app.state.ctx.commands, "入库 NE555 30 @C柜-1层")
    assert "要补充规格/封装吗" not in reply.reply


# --------------------------------------------------------------------------- #
# 位置可以省略
# --------------------------------------------------------------------------- #
def test_stock_in_without_location(app, router) -> None:
    reply = say(router, "入库 杜邦线 100")
    assert "入库成功" in reply.reply
    record = next(r for r in app.state.ctx.repo.all_items() if r.name == "杜邦线")
    assert record.location == ""
    assert record.quantity == 100


def test_location_can_explicitly_be_skipped(app, router) -> None:
    for text in ("入库 杜邦线 100 无位置", "入库 排针 50 跳过位置", "入库 跳线 20 不指定位置"):
        assert "入库成功" in say(router, text).reply
    records = {r.name: r for r in app.state.ctx.repo.all_items()}
    assert records["杜邦线"].location == ""
    assert records["排针"].location == ""
    assert records["跳线"].location == ""


def test_pick_first_word_selects_top_candidate(app, router) -> None:
    app.state.ctx.inventory.create_item(ItemCreate(name="AMS1117-3.3", quantity=10, location="D柜-1层"))
    app.state.ctx.inventory.create_item(ItemCreate(name="AMS1117-3.3", quantity=20, location="D柜-2层"))
    say(router, "出库 AMS1117-3.3 5")
    assert "出库成功" in say(router, "随便").reply


# --------------------------------------------------------------------------- #
# 已出库（数量 0）不再占版面
# --------------------------------------------------------------------------- #
def _zero_everything(router) -> None:
    say(router, "出库全部")
    say(router, "确认")


def test_zero_quantity_hidden_from_listings(app, router) -> None:
    _zero_everything(router)
    reply = say(router, "库存").reply
    assert "当前没有在库物品" in reply
    assert "零库存" in reply
    assert "STM32F103C8T6" not in reply
    # 条目本身还在，只是不再出现在「在库」清单里
    assert len(app.state.ctx.repo.all_items()) == 3


def test_zero_list_then_clean(app, router) -> None:
    _zero_everything(router)

    listing = say(router, "零库存").reply
    assert "已清零的物品" in listing
    assert "STM32F103C8T6" in listing
    assert "已清零" in listing

    prompt = say(router, "清理零库存").reply
    assert "将删除 3 条" in prompt and "确认" in prompt
    assert len(app.state.ctx.repo.all_items()) == 3      # 未确认前不删

    done = say(router, "确认").reply
    assert "已删除 3 条" in done
    assert app.state.ctx.repo.all_items() == []


def test_zero_clean_can_be_cancelled(app, router) -> None:
    _zero_everything(router)
    say(router, "清理零库存")
    assert "已取消" in say(router, "取消").reply
    assert len(app.state.ctx.repo.all_items()) == 3


def test_search_still_finds_zero_items(app, router) -> None:
    """搜索保留已清零条目 —— 「有这个料号、只是没货了」本身有用。"""
    _zero_everything(router)
    reply = say(router, "查 STM32").reply
    assert "STM32F103C8T6" in reply
    assert "已清零" in reply


def test_category_list_marks_zero_items(app, router) -> None:
    _zero_everything(router)
    reply = say(router, "STM元器件").reply
    assert "已全部清零" in reply or "已清零" in reply


def test_overview_counts_only_live_items(app, router) -> None:
    say(router, "出库 STM32F103C8T6 25")   # 只清掉一个
    reply = say(router, "库存").reply
    assert "在库 2 种" in reply
    assert "另有 1 种已清零" in reply
    assert "STM32F103C8T6" not in reply
    assert "0.1uF 50V MLCC" in reply


# --------------------------------------------------------------------------- #
# 「100欧姆电阻」和「100Ω电阻」是同一条
# --------------------------------------------------------------------------- #
def test_ohm_spelling_variants_are_one_item(app, router) -> None:
    created = say(router, "入库 100欧姆电阻 100 @C库")
    assert "入库成功" in created.reply
    say(router, "跳过")
    say(router, "跳过")

    found = say(router, "查 100Ω电阻").reply
    assert "100欧姆电阻" in found

    out = say(router, "出库 100Ω电阻 30").reply
    assert "出库成功" in out
    records = [r for r in app.state.ctx.repo.all_items() if "100" in r.name and "电阻" in r.name]
    assert len(records) == 1                       # 没有产生第二条记录
    assert records[0].quantity == 70
    assert "100Ω电阻" in records[0].aliases         # 自动记住了另一种写法

    # 再搜一次应该走精确别名命中
    assert "100欧姆电阻" in say(router, "查 100Ω").reply


# --------------------------------------------------------------------------- #
# AI 解析（用假 LLM 打桩，不联网）
# --------------------------------------------------------------------------- #
class FakeLLM:
    """替身：``chat`` 依次返回预设的 JSON 字符串，并记录调用。"""

    def __init__(self, *responses: str, ready: bool = True) -> None:
        self.responses = list(responses) or ['{"action":"unknown"}']
        self.calls: list[list[dict]] = []
        self.ready = ready

    async def chat(self, messages, **_kwargs):
        self.calls.append(list(messages))
        content = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        return {
            "content": content,
            "model": "fake-model",
            "prompt_tokens": 1,
            "completion_tokens": 1,
        }

    async def close(self) -> None:  # pragma: no cover - 测试替身
        return None


def _install_fake_llm(app, *responses: str) -> FakeLLM:
    fake = FakeLLM(*responses)
    app.state.ctx.ai.llm = fake
    return fake


AI_IN_JSON = (
    '{"action":"in","name":"杜邦线","quantity":2,"location":"A柜","spec":"","category":"other",'
    '"aliases":["跳线"],"confidence":0.9,"reason":"用户说进了一盒杜邦线"}'
)


def test_ai_parses_vague_stock_command(app, router) -> None:
    """数量说不清时交给 AI：『入库 杜邦线 一些』。"""
    fake = _install_fake_llm(app, AI_IN_JSON)
    reply = say(router, "入库 杜邦线 一些")
    assert "入库成功" in reply.reply, reply.reply
    assert "AI 理解" in reply.reply
    assert fake.calls, "应该调用过大模型"
    record = next(r for r in app.state.ctx.repo.all_items() if r.name == "杜邦线")
    assert record.quantity == 2
    assert record.location == "A柜"
    assert "跳线" in record.aliases


def test_rules_win_when_they_succeed(app, router) -> None:
    """规则能干净解析时不该浪费一次 LLM 调用。"""
    fake = _install_fake_llm(app, AI_IN_JSON)
    reply = say(router, "入库 NE555 30 @C柜-1层")
    assert "入库成功" in reply.reply
    assert "AI 理解" not in reply.reply
    assert fake.calls == []


def test_ai_low_confidence_is_ignored(app, router) -> None:
    _install_fake_llm(
        app,
        '{"action":"in","name":"杜邦线","quantity":2,"confidence":0.1,"reason":"猜的"}',
    )
    reply = say(router, "入库 杜邦线 一些")
    assert "没解析出数量" in reply.reply
    assert not [r for r in app.state.ctx.repo.all_items() if r.name == "杜邦线"]


def test_ai_can_create_new_category(app, router) -> None:
    _install_fake_llm(
        app,
        '{"action":"in","name":"ESP32-C3 开发板","quantity":3,"location":"A柜","category":"开发板",'
        '"confidence":0.95,"reason":"这是开发板"}',
    )
    reply = say(router, "进了一块 ESP32-C3 开发板 放 A柜").reply
    assert "已归入新分类" in reply
    record = next(r for r in app.state.ctx.repo.all_items() if "ESP32-C3" in r.name)
    assert record.category == "开发板"
    # 新分类要能被分类清单找到（这条以前写成 `... or True`，等于没测）
    listing = say(router, "分类 开发板").reply
    assert "开发板" in listing
    assert "ESP32-C3 开发板" in listing


def test_ai_handles_freeform_sentence(app, router) -> None:
    """非命令语气的口语描述也要能识别。"""
    _install_fake_llm(
        app,
        '{"action":"out","name":"0.1uF 50V MLCC","quantity":20,"confidence":0.92,"reason":"用掉了一些"}',
    )
    reply = say(router, "我把 0.1uF 电容用掉了一些").reply
    assert "出库成功" in reply, reply
    record = next(r for r in app.state.ctx.repo.all_items() if "0.1uF" in r.name)
    assert record.quantity == 480


def test_ai_unknown_action_falls_back_to_nlq(app, router) -> None:
    _install_fake_llm(app, '{"action":"unknown","name":"","confidence":0.9,"reason":"这是查询"}')
    reply = say(router, "我把 0.1uF 电容用掉了一些").reply
    assert "出库成功" not in reply


def test_parse_mode_rules_disables_ai(app, router) -> None:
    fake = _install_fake_llm(app, AI_IN_JSON)
    app.state.ctx.settings.qq_parse_mode = "rules"
    say(router, "入库 杜邦线 一些")
    assert fake.calls == []
    app.state.ctx.settings.qq_parse_mode = "auto"


# --------------------------------------------------------------------------- #
# QQ 里直接粘贴清单导入
# --------------------------------------------------------------------------- #
PASTE_MD = """| 名称 | 数量 | 位置 | 规格 |
| --- | --- | --- | --- |
| AMS1117-3.3 | 50 | D柜-1层 | SOT-223 |
| NE555 | 30 | C柜-1层 | DIP-8 |"""


def test_paste_markdown_table_imports(app, router) -> None:
    before = len(app.state.ctx.repo.all_items())
    preview = say(router, PASTE_MD)
    assert "解析出 2 条记录" in preview.reply
    assert "AMS1117-3.3" in preview.reply
    assert len(app.state.ctx.repo.all_items()) == before      # 确认前不落库

    done = say(router, "确认").reply
    assert "导入完成" in done
    names = {r.name for r in app.state.ctx.repo.all_items()}
    assert {"AMS1117-3.3", "NE555"} <= names
    assert len(app.state.ctx.repo.all_items()) == before + 2
    record = next(r for r in app.state.ctx.repo.all_items() if r.name == "AMS1117-3.3")
    assert (record.quantity, record.location, record.spec) == (50, "D柜-1层", "SOT-223")


def test_paste_inline_list_imports(app, router) -> None:
    text = "STM32F103C8T6 × 25 @A柜-1层 #F103C8 (LQFP48)\nNE555 30个 @C柜-1层 #555 (DIP-8)"
    assert "解析出 2 条记录" in say(router, text).reply
    say(router, "确认")
    record = next(r for r in app.state.ctx.repo.all_items() if r.name == "NE555")
    assert record.quantity == 30
    assert "555" in record.aliases


def test_handle_attachment_imports_excel(app, router) -> None:
    """QQ 里直接发的 .xlsx 文件 → 解析预览 → 确认后入库。"""
    import io

    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["名称", "数量", "位置", "封装"])
    sheet.append(["NE555", 30, "C柜-1层", "DIP-8"])
    buffer = io.BytesIO()
    workbook.save(buffer)

    before = len(app.state.ctx.repo.all_items())
    reply = asyncio.run(
        app.state.ctx.commands.handle_attachment(
            "库存.xlsx", buffer.getvalue(), operator="u1", scene="api", conversation="file-1"
        )
    )
    assert "解析出 1 条记录" in reply.reply
    assert "NE555" in reply.reply
    assert len(app.state.ctx.repo.all_items()) == before          # 确认前不落库

    done = say(router, "确认", conversation="file-1", scene="api")
    assert "导入完成" in done.reply
    record = next(r for r in app.state.ctx.repo.all_items() if r.name == "NE555")
    assert (record.quantity, record.location, record.spec) == (30, "C柜-1层", "DIP-8")


def test_handle_attachment_txt(app, router) -> None:
    data = "STM32F103C8T6 × 25 @A柜-1层 #F103C8 (LQFP48)\n".encode()
    reply = asyncio.run(
        app.state.ctx.commands.handle_attachment(
            "清单.txt", data, operator="u1", scene="api", conversation="file-2"
        )
    )
    assert "解析出 1 条记录" in reply.reply
    say(router, "确认", conversation="file-2", scene="api")
    record = next(r for r in app.state.ctx.repo.all_items() if r.name == "STM32F103C8T6")
    assert record.quantity == 25
    assert "F103C8" in record.aliases


def test_handle_attachment_unsupported_format(app, router) -> None:
    reply = asyncio.run(
        app.state.ctx.commands.handle_attachment(
            "老表格.xls", b"\xd0\xcf\x11\xe0", operator="u1", scene="api", conversation="file-3"
        )
    )
    assert "没法解析" in reply.reply
    assert "另存" in reply.reply


def test_handle_attachment_empty_content(app, router) -> None:
    reply = asyncio.run(
        app.state.ctx.commands.handle_attachment(
            "空的.csv", b"\n\n\n", operator="u1", scene="api", conversation="file-4"
        )
    )
    assert "没解析出任何条目" in reply.reply


def test_handle_attachment_zero_bytes_does_not_crash(app, router) -> None:
    reply = asyncio.run(
        app.state.ctx.commands.handle_attachment(
            "空的.csv", b"", operator="u1", scene="api", conversation="file-5"
        )
    )
    assert reply.reply          # 有明确回复、不抛异常即可


# --------------------------------------------------------------------------- #
# 导入时 AI 自动归类
# --------------------------------------------------------------------------- #
CLASSIFY_JSON = (
    '{"categories": ['
    '{"index": 1, "category": "STM元器件", "reason": "电阻"},'
    '{"index": 2, "category": "耗材", "reason": "杜邦线属于线材耗材"},'
    '{"index": 3, "category": "耗材", "reason": "同类"}'
    "]}"
)


def test_import_uses_ai_categories(app, router) -> None:
    """导入时 AI 通读整批归类，没有的分类自动新建。"""
    app.state.ctx.ai.llm = FakeLLM(CLASSIFY_JSON)
    text = "导入\n| 名称 | 数量 |\n| --- | --- |\n| 100欧姆电阻 | 10 |\n| 杜邦线 | 50 |\n| 热缩管 | 5 |"
    reply = say(router, text, scene="api", conversation="cls-1").reply

    assert "AI 归类" in reply
    assert "耗材 2" in reply
    assert "新建分类：耗材" in reply

    say(router, "确认", scene="api", conversation="cls-1")
    by_name = {r.name: r.category for r in app.state.ctx.repo.all_items()}
    assert by_name["100欧姆电阻"] == "STM元器件"
    assert by_name["杜邦线"] == "耗材"          # 新分类已落库
    assert by_name["热缩管"] == "耗材"
    # 新分类立刻能用
    assert "杜邦线" in say(router, "分类 耗材").reply


def test_import_ai_classification_can_be_disabled(app, router) -> None:
    app.state.ctx.settings.ai_classify_on_import = False
    app.state.ctx.ai.llm = FakeLLM(CLASSIFY_JSON)
    text = "导入\n| 名称 | 数量 |\n| --- | --- |\n| 杜邦线 | 50 |"
    reply = say(router, text, scene="api", conversation="cls-2").reply
    assert "AI 归类" not in reply
    app.state.ctx.settings.ai_classify_on_import = True


def test_import_falls_back_when_ai_classification_fails(app, router) -> None:
    class BrokenLLM(FakeLLM):
        async def chat(self, messages, **kwargs):
            raise RuntimeError("炸")

    app.state.ctx.ai.llm = BrokenLLM("{}")
    text = "导入\n| 名称 | 数量 |\n| --- | --- |\n| 100欧姆电阻 | 10 |"
    reply = say(router, text, scene="api", conversation="cls-3").reply
    assert "解析出 1 条记录" in reply          # 照样能导
    say(router, "确认", scene="api", conversation="cls-3")
    record = next(r for r in app.state.ctx.repo.all_items() if r.name == "100欧姆电阻")
    assert record.category == "未分类"  # 没有内置分类，规则只保证有值


# --------------------------------------------------------------------------- #
# 导入时 AI 规范命名
# --------------------------------------------------------------------------- #
RENAME_JSON = (
    '{"items": ['
    '{"index": 1, "name": "10kΩ 0805", "category": "STM元器件", "reason": "去掉 R 前缀"},'
    '{"index": 2, "name": "杜邦线", "category": "STM元器件", "reason": "已经规范"}'
    "]}"
)


def test_import_normalizes_names_with_ai(app, router) -> None:
    """导入时 AI 规范命名；原名保留为别名，搜旧写法照样能找到。"""
    app.state.ctx.ai.llm = FakeLLM(RENAME_JSON)
    text = "导入\n| 名称 | 数量 |\n| --- | --- |\n| R 10kΩ 0805 | 200 |\n| 杜邦线 | 100 |"
    reply = say(router, text, scene="api", conversation="nm-1").reply

    assert "规范命名 1 条" in reply          # 第二条本来就规范，不算改名
    assert "R 10kΩ 0805 → 10kΩ 0805" in reply
    assert "原名称已保留为别名" in reply

    say(router, "确认", scene="api", conversation="nm-1")
    record = next(r for r in app.state.ctx.repo.all_items() if r.name == "10kΩ 0805")
    assert "R 10kΩ 0805" in record.aliases
    # 旧写法仍然搜得到
    assert "10kΩ 0805" in say(router, "查 R 10kΩ 0805").reply


def test_import_normalization_can_be_disabled(app, router) -> None:
    app.state.ctx.settings.ai_normalize_on_import = False
    app.state.ctx.ai.llm = FakeLLM(RENAME_JSON)
    text = "导入\n| 名称 | 数量 |\n| --- | --- |\n| R 10kΩ 0805 | 200 |"
    reply = say(router, text, scene="api", conversation="nm-2").reply
    assert "→" not in reply

    say(router, "确认", scene="api", conversation="nm-2")
    record = next(r for r in app.state.ctx.repo.all_items() if "10k" in r.name)
    assert record.name == "R 10kΩ 0805"      # 原样保留
    app.state.ctx.settings.ai_normalize_on_import = True


@pytest.mark.parametrize(
    ("old", "new", "ok"),
    [
        ("R 10kΩ 0805", "10kΩ 0805", True),          # 去前缀，信息保留
        ("黄 led 直插", "黄色LED 直插", True),          # 统一大小写/中文名
        ("stm32f103c8t6", "STM32F103C8T6", True),     # 型号大写
        ("0805 10kΩ", "10kΩ 0805", True),             # 调整顺序
        ("10kΩ 0805", "电阻", False),                  # 丢信息 —— 拒绝
        ("NE555", "555", False),                      # 丢型号 —— 拒绝
        ("10kΩ 0805", "", False),                     # 空名 —— 拒绝
        ("10kΩ 0805", "10kΩ 0805 @C库", False),        # 混进位置 —— 拒绝
        ("10kΩ 0805", "10kΩ 0805", False),            # 没变化
    ],
)
def test_ai_rename_guard(old: str, new: str, ok: bool) -> None:
    """改名护栏：新名必须保留原名里至少一个「有信息量」的片段。"""
    from app.services.ai import _name_is_acceptable

    assert _name_is_acceptable(old, new) is ok


def test_paste_import_can_be_cancelled(app, router) -> None:
    before = len(app.state.ctx.repo.all_items())
    say(router, PASTE_MD)
    assert "已取消" in say(router, "取消").reply
    assert len(app.state.ctx.repo.all_items()) == before


def test_single_line_is_not_treated_as_paste(app, router) -> None:
    """单行消息不该走批量导入，仍按普通指令/搜索处理。"""
    reply = say(router, "STM32F103C8T6 × 25 @A柜-1层")
    assert "解析出" not in reply.reply


def test_stock_in_with_table_routes_to_import(app, router) -> None:
    """实测 bug：『入库 + 整张表格』把表格压成了一条物品名。"""
    text = "入库\n| 名称 | 数量 |\n| --- | --- |\n| NE555 | 30 |\n| AMS1117-3.3 | 50 |"
    reply = say(router, text).reply
    assert "解析出 2 条记录" in reply
    # 绝不能生成带竖线的垃圾条目
    assert not [r for r in app.state.ctx.repo.all_items() if "|" in r.name or "名称" in r.name]

    say(router, "确认")
    names = {r.name for r in app.state.ctx.repo.all_items()}
    assert {"NE555", "AMS1117-3.3"} <= names


def test_stock_out_with_table_batch_out(app, router) -> None:
    """实测 bug：把清单粘在「出库」后面应该逐条出库，而不是被拒绝。"""
    text = "出库\n| 名称 | 数量 |\n| --- | --- |\n| STM32F103C8T6 | 5 |\n| 0.1uF 50V MLCC | 100 |"
    reply = say(router, text).reply
    assert "将按清单批量出库 2 项" in reply
    assert "25 → 20" in reply
    assert "500 → 400" in reply
    assert sum(r.quantity for r in app.state.ctx.repo.all_items()) == 535   # 未确认不动

    done = say(router, "确认").reply
    assert "已按清单批量出库 2 项" in done
    by_name = {r.name: r.quantity for r in app.state.ctx.repo.all_items()}
    assert by_name["STM32F103C8T6"] == 20
    assert by_name["0.1uF 50V MLCC"] == 400


def test_stock_out_pasted_bot_listing(app, router) -> None:
    """把机器人列出的清单原样复制回来（带括号规格和 × 数量）也要能出库。"""
    listing = say(router, "库存").reply
    lines = [line.strip() for line in listing.splitlines() if "×" in line]
    assert len(lines) >= 2, listing

    before = sum(r.quantity for r in app.state.ctx.repo.all_items())
    reply = say(router, "出库\n" + "\n".join(lines)).reply
    assert "将按清单批量出库" in reply

    say(router, "确认")
    after = sum(r.quantity for r in app.state.ctx.repo.all_items())
    assert after < before
    assert all(r.quantity == 0 for r in app.state.ctx.repo.all_items())


def test_batch_out_reports_unmatched(app, router) -> None:
    text = "出库\n| 名称 | 数量 |\n| --- | --- |\n| STM32F103C8T6 | 5 |\n| 不存在的料号 | 3 |"
    reply = say(router, text).reply
    assert "将按清单批量出库 1 项" in reply
    assert "另有 1 项没法处理" in reply
    assert "库存里没有这条" in reply


def test_batch_out_requires_quantity(app, router) -> None:
    text = "出库\n| 名称 | 数量 |\n| --- | --- |\n| STM32F103C8T6 | |"
    reply = say(router, text).reply
    assert "都没法出库" in reply
    assert "没写要出多少" in reply


def test_batch_out_insufficient_stock_fails_per_item(app, router) -> None:
    """一条库存不足不该拖累其他条目。"""
    text = "出库\n| 名称 | 数量 |\n| --- | --- |\n| STM32F103C8T6 | 9999 |\n| 0.1uF 50V MLCC | 10 |"
    say(router, text)
    done = say(router, "确认").reply
    assert "已按清单批量出库 1 项" in done
    assert "库存不足" in done
    by_name = {r.name: r.quantity for r in app.state.ctx.repo.all_items()}
    assert by_name["0.1uF 50V MLCC"] == 490     # 另一条照常执行
    assert by_name["STM32F103C8T6"] == 25       # 不足的那条没动


def test_batch_set_from_list(app, router) -> None:
    text = "盘点\n| 名称 | 数量 |\n| --- | --- |\n| STM32F103C8T6 | 7 |\n| 0.1uF 50V MLCC | 42 |"
    reply = say(router, text).reply
    assert "将按清单批量盘点 2 项" in reply
    assert "25 → 7" in reply

    say(router, "确认")
    by_name = {r.name: r.quantity for r in app.state.ctx.repo.all_items()}
    assert by_name["STM32F103C8T6"] == 7
    assert by_name["0.1uF 50V MLCC"] == 42


def test_batch_out_can_be_cancelled(app, router) -> None:
    before = sum(r.quantity for r in app.state.ctx.repo.all_items())
    text = "出库\n| 名称 | 数量 |\n| --- | --- |\n| STM32F103C8T6 | 5 |\n| 0.1uF 50V MLCC | 100 |"
    say(router, text)
    assert "已取消" in say(router, "取消").reply
    assert sum(r.quantity for r in app.state.ctx.repo.all_items()) == before


# --------------------------------------------------------------------------- #
# 归类 / 删除（实测中用户尝试过、当时不存在的命令）
# --------------------------------------------------------------------------- #
def test_categorize_natural_phrasing(app, router) -> None:
    """「杜邦线归入stm元器件」—— 大小写不同**复用**已有分类，不新建重复分类。"""
    app.state.ctx.inventory.create_item(ItemCreate(name="杜邦线", quantity=10))
    reply = say(router, "杜邦线归入stm元器件").reply
    assert "改到「STM元器件」" in reply
    record = next(r for r in app.state.ctx.repo.all_items() if r.name == "杜邦线")
    assert record.category == "STM元器件"
    # 没有造出仅大小写不同的第二个分类（种子数据只有这两个分类）
    assert {r.category for r in app.state.ctx.repo.all_items()} == {"STM元器件", "Steam游戏卡"}


def test_categorize_command_can_create_category(app, router) -> None:
    app.state.ctx.inventory.create_item(ItemCreate(name="杜邦线", quantity=10))
    reply = say(router, "归类 杜邦线 耗材").reply
    assert "新建了分类「耗材」" in reply
    record = next(r for r in app.state.ctx.repo.all_items() if r.name == "杜邦线")
    assert record.category == "耗材"
    # 新分类能用于分类清单
    assert "杜邦线" in say(router, "分类 耗材").reply


def test_categorize_command_with_connector(app, router) -> None:
    app.state.ctx.inventory.create_item(ItemCreate(name="杜邦线", quantity=10))
    assert "改到「Steam游戏卡」" in say(router, "归类 杜邦线 到 游戏卡").reply


def test_categorize_unknown_item(app, router) -> None:
    assert "没找到" in say(router, "归类 不存在的料号 耗材").reply


def test_delete_single_requires_confirmation(app, router) -> None:
    before = len(app.state.ctx.repo.all_items())
    prompt = say(router, "删除 STM32F103C8T6").reply
    assert "将删除「STM32F103C8T6」" in prompt
    assert len(app.state.ctx.repo.all_items()) == before      # 未确认不删

    assert "已删除" in say(router, "确认").reply
    names = {r.name for r in app.state.ctx.repo.all_items()}
    assert "STM32F103C8T6" not in names
    assert len(app.state.ctx.repo.all_items()) == before - 1


def test_delete_can_be_cancelled(app, router) -> None:
    before = len(app.state.ctx.repo.all_items())
    say(router, "删除 STM32F103C8T6")
    assert "已取消" in say(router, "取消").reply
    assert len(app.state.ctx.repo.all_items()) == before


def test_delete_all_requires_confirmation(app, router) -> None:
    prompt = say(router, "删除全部").reply
    assert "⚠️" in prompt and "3 条" in prompt
    assert len(app.state.ctx.repo.all_items()) == 3           # 未确认不删
    assert "已清空 3 条" in say(router, "确认").reply
    assert app.state.ctx.repo.all_items() == []


# --------------------------------------------------------------------------- #
# AI 整理
# --------------------------------------------------------------------------- #
TIDY_JSON = """\
{"summary":"补齐别名与分类","changes":[
  {"item_id": 1, "aliases": ["蓝药丸"], "reason": "常见叫法"},
  {"item_id": 3, "category": "耗材", "reason": "示例：换个分类"}
], "duplicates": [[1, 2]]}
"""


def test_tidy_proposes_then_applies(app, router) -> None:
    _install_fake_llm(app, TIDY_JSON)
    prompt = say(router, "整理").reply
    assert "建议改" in prompt
    assert "蓝药丸" in prompt
    assert "可能是同一种东西" in prompt

    # 确认前不改数据
    assert "蓝药丸" not in next(r for r in app.state.ctx.repo.all_items() if r.id == 1).aliases

    done = say(router, "确认").reply
    assert "已按建议整理" in done
    record = next(r for r in app.state.ctx.repo.all_items() if r.id == 1)
    assert "蓝药丸" in record.aliases
    other = next(r for r in app.state.ctx.repo.all_items() if r.name == "Steam 50元充值卡")
    assert other.category == "耗材"          # AI 新建的分类


def test_tidy_without_llm(app, router) -> None:
    app.state.ctx.ai.llm = FakeLLM("{}", ready=False)
    assert "需要先配置大模型" in say(router, "整理").reply


def test_tidy_can_be_cancelled(app, router) -> None:
    _install_fake_llm(app, TIDY_JSON)
    say(router, "整理")
    assert "已取消" in say(router, "取消").reply


# --------------------------------------------------------------------------- #
# 整批出库
# --------------------------------------------------------------------------- #
def test_bulk_out_all_requires_confirmation(app, router) -> None:
    reply = say(router, "出库全部").reply
    assert "整批出库" in reply
    assert "回复「确认」" in reply
    assert sum(r.quantity for r in app.state.ctx.repo.all_items()) > 0     # 未确认不动


def test_bulk_out_last_items(app, router) -> None:
    say(router, "库存")
    assert "整批出库" in say(router, "这些全部出库").reply
    say(router, "确认")
    assert all(r.quantity == 0 for r in app.state.ctx.repo.all_items())


def test_clear_these_natural_phrasing(app, router) -> None:
    """实测：『从库存里面清除这些东西，我要用掉』"""
    say(router, "库存")                                   # 先建立「这些」的指代
    reply = say(router, "从库存里面清除这些东西，我要用掉").reply
    assert "整批出库" in reply
    assert "回复「确认」" in reply
    assert sum(r.quantity for r in app.state.ctx.repo.all_items()) > 0     # 未确认不落库

    assert "实际清零 3 种" in say(router, "确认").reply
    assert all(r.quantity == 0 for r in app.state.ctx.repo.all_items())


def test_clear_these_without_context_gives_hint(app, router) -> None:
    reply = say(router, "把这些都删掉").reply
    assert "不知道「这些」指哪些" in reply


def test_clear_these_after_procure(app, router) -> None:
    """『采购』列出的清单也要能被『这些』指代。"""
    say(router, "采购 STM32F103C8T6 25")
    reply = say(router, "这些东西都用掉").reply
    assert "整批出库" in reply


@pytest.mark.parametrize(
    "text",
    [
        "出库 STM32F103C8T6 5",         # 正常单条出库
        "入库 NE555 30",
        "查 电容",
        "这些电容全部出库",              # 带分类 → 走 parse_bulk_request 的分类路径
    ],
)
def test_clear_these_does_not_hijack_other_messages(app, router, text: str) -> None:
    from app.bot.commands import parse_clear_these

    assert parse_clear_these(text) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # 实测：采购之后用户就是这么说话的
        ("好的，现在从库存里面删除这些", "delete"),
        ("从库存里面清除采购清单里面的物品", "out"),
        ("把采购清单里的东西都删了", "delete"),
        ("这些都不要了", "out"),
        ("从库存里清除这些东西，我要用掉", "out"),
        ("刚才那份清单里的物品清掉", "out"),
        ("上面的那些物品删掉", "delete"),
        # 不该误判
        ("出库 STM32 5", None),
        ("删除 NE555", None),            # 单条删除走命令，不是整批
        # 只说「处理掉」→ 歧义，要反问
        ("把这些处理掉", "ask"),
        ("采购清单里的东西处理一下", "ask"),
    ],
)
def test_parse_clear_these_covers_natural_phrasings(text: str, expected: str | None) -> None:
    from app.bot.commands import parse_clear_these

    assert parse_clear_these(text) == expected


def test_ambiguous_dispose_asks_instead_of_failing(app, router) -> None:
    """实测：「把这些处理掉」既不是清零也不是删除 —— 该反问，不该回「没找到」。"""
    say(router, "采购 STM32F103C8T6 25")
    reply = say(router, "把这些处理掉").reply
    assert "要我怎么处理" in reply
    assert "① 出库清零" in reply and "② 删除记录" in reply
    assert "STM32F103C8T6" in reply              # 说清「这些」到底指哪些

    # 回「出库」→ 走清零
    assert "整批出库" in say(router, "出库").reply
    say(router, "确认")
    record = next(r for r in app.state.ctx.repo.all_items() if r.name == "STM32F103C8T6")
    assert record.quantity == 0                  # 记录还在，只是清零


def test_ambiguous_dispose_can_delete(app, router) -> None:
    say(router, "采购 STM32F103C8T6 25")
    say(router, "把这些处理掉")
    assert "将删除" in say(router, "删除").reply
    say(router, "确认")
    names = {record.name for record in app.state.ctx.repo.all_items()}
    assert "STM32F103C8T6" not in names


def test_ambiguous_dispose_can_be_cancelled(app, router) -> None:
    before = len(app.state.ctx.repo.all_items())
    say(router, "采购 STM32F103C8T6 25")
    say(router, "把这些处理掉")
    assert "已取消" in say(router, "取消").reply
    assert len(app.state.ctx.repo.all_items()) == before


def test_ambiguous_dispose_without_reference_gives_hint(app, router) -> None:
    reply = say(router, "把这些处理掉").reply
    assert "不知道「这些」指哪些" in reply


def test_clear_purchase_list_deletes_records(app, router) -> None:
    """实测：采购之后说「从库存里面删除这些」，该真的删掉而不是没反应。"""
    say(router, "采购 STM32F103C8T6 25")            # 建立「这些」的指代
    before = len(app.state.ctx.repo.all_items())

    reply = say(router, "好的，现在从库存里面删除这些").reply
    assert "将删除" in reply
    assert "真的删掉" in reply
    assert len(app.state.ctx.repo.all_items()) == before      # 未确认不删

    done = say(router, "确认").reply
    assert "已删除" in done
    names = {record.name for record in app.state.ctx.repo.all_items()}
    assert "STM32F103C8T6" not in names


def test_clear_purchase_list_outs_instead_when_asked(app, router) -> None:
    """说「清除」就是清零（记录保留），说「删除」才是真删 —— 两者语义要分开。"""
    say(router, "采购 STM32F103C8T6 25")
    reply = say(router, "从库存里面清除采购清单里面的物品").reply
    assert "整批出库" in reply

    say(router, "确认")
    records = [r for r in app.state.ctx.repo.all_items() if r.name == "STM32F103C8T6"]
    assert records and records[0].quantity == 0      # 记录还在，只是清零


def test_normal_out_still_works(app, router) -> None:
    reply = say(router, "出库 STM32F103C8T6 5").reply
    assert "出库成功" in reply
    record = next(r for r in app.state.ctx.repo.all_items() if r.name == "STM32F103C8T6")
    assert record.quantity == 20


def test_stock_in_with_bare_package_does_not_ask_spec(app, router) -> None:
    """实测 bug：``入库 100Ω电容 0805 100`` 已经把封装写在名称里了，不该再问一次。"""
    reply = say(router, "入库 100Ω电容 0805 100").reply
    assert "入库成功" in reply
    assert "要补充规格/封装吗" not in reply          # 不再重复追问封装

    record = next(r for r in app.state.ctx.repo.all_items() if "电容" in r.name)
    assert record.name == "100Ω电容"              # 封装没有留在名称里
    assert record.spec == "0805"
    assert record.quantity == 100


def test_stock_in_with_bare_package_creates_separate_variants(app, router) -> None:
    """不同裸写封装 = 不同品种，跟括号写法行为一致。"""
    say(router, "入库 100Ω电容 0805 100")
    say(router, "跳过")
    say(router, "跳过")
    say(router, "入库 100Ω电容 0603 50")
    records = [r for r in app.state.ctx.repo.all_items() if r.name == "100Ω电容"]
    assert {r.spec: r.quantity for r in records} == {"0805": 100, "0603": 50}


# --------------------------------------------------------------------------- #
# AI 模糊指令匹配
# --------------------------------------------------------------------------- #
def _intent(app, command: str, **extra) -> None:
    payload = {"command": command, "confidence": 0.9, "reason": "测试"} | extra
    app.state.ctx.ai.llm = FakeLLM(json.dumps(payload, ensure_ascii=False))


def test_ai_intent_routes_unrecognized_phrase(app, router) -> None:
    """实测：「清除全部」这种说法规则认不出来，交给 AI 判成整批出库。"""
    _intent(app, "out_all")
    reply = say(router, "全部不要了").reply
    assert "整批出库" in reply
    assert "回复「确认」" in reply


def test_clear_all_is_handled_by_rules_without_ai(app, router) -> None:
    """「清除全部」已经把「清除」加进动词表，规则直接认，不用花 LLM。"""
    fake = _install_fake_llm(app, '{"command": "unknown", "confidence": 0.1}')
    reply = say(router, "清除全部").reply
    assert "整批出库" in reply
    assert fake.calls == []


def test_ai_intent_routes_to_list(app, router) -> None:
    _intent(app, "list")
    assert "在库共" in say(router, "给我看看家底").reply


def test_ai_intent_routes_to_low_stock(app, router) -> None:
    _intent(app, "low")
    assert say(router, "有哪些快见底了").reply


def test_ai_intent_routes_to_stock_in(app, router) -> None:
    _intent(app, "stock_in", name="杜邦线", quantity=200, location="A柜")
    reply = say(router, "进了 200 个杜邦线放 A 柜").reply
    assert "入库成功" in reply
    record = next(r for r in app.state.ctx.repo.all_items() if r.name == "杜邦线")
    assert (record.quantity, record.location) == (200, "A柜")


def test_ai_intent_routes_to_stock_out(app, router) -> None:
    # 既没有出库关键词（「拿走/用掉」会被前面的 AI 库存解析截住），
    # 也搜不到东西 → 才会走 AI 意图兜底
    _intent(app, "stock_out", name="STM32F103C8T6", quantity=5)
    reply = say(router, "把它减掉五个").reply
    assert "出库成功" in reply
    record = next(r for r in app.state.ctx.repo.all_items() if r.name == "STM32F103C8T6")
    assert record.quantity == 20


def test_ai_intent_ignores_unknown(app, router) -> None:
    _intent(app, "unknown")
    reply = say(router, "那个东西呢").reply
    assert "没找到" in reply          # 退回原来的「没找到」提示


def test_ai_intent_ignores_low_confidence(app, router) -> None:
    app.state.ctx.ai.llm = FakeLLM(
        json.dumps({"command": "delete_all", "confidence": 0.2}, ensure_ascii=False)
    )
    reply = say(router, "那个东西呢").reply
    assert "已清空" not in reply
    assert len(app.state.ctx.repo.all_items()) == 3


def test_ai_intent_stock_needs_high_confidence(app, router) -> None:
    """会改库存的意图门槛更高 —— 0.6 分的 stock_out 不该执行。"""
    app.state.ctx.ai.llm = FakeLLM(
        json.dumps(
            {"command": "stock_out", "name": "STM32F103C8T6", "quantity": 5, "confidence": 0.6},
            ensure_ascii=False,
        )
    )
    reply = say(router, "把它减掉五个").reply
    assert "出库成功" not in reply
    record = next(r for r in app.state.ctx.repo.all_items() if r.name == "STM32F103C8T6")
    assert record.quantity == 25


def test_ai_intent_out_last_accepts_moderate_confidence(app, router) -> None:
    """出库是清零（记录保留），门槛可以比删除低。

    「把这些处理掉」现在由规则直接反问了，这里用规则认不出的说法测 AI 兜底。
    """
    say(router, "采购 STM32F103C8T6 25")
    app.state.ctx.ai.llm = FakeLLM(
        json.dumps(
            {"command": "out_last", "confidence": 0.6, "reason": "指代刚列出的那批"},
            ensure_ascii=False,
        )
    )
    reply = say(router, "上一批货全部作废").reply
    assert "整批出库" in reply


def test_ai_intent_delete_last_needs_high_confidence(app, router) -> None:
    """删除不可恢复，0.6 分不够。"""
    say(router, "采购 STM32F103C8T6 25")
    before = len(app.state.ctx.repo.all_items())
    app.state.ctx.ai.llm = FakeLLM(
        json.dumps(
            {"command": "delete_last", "confidence": 0.6, "reason": "猜的"}, ensure_ascii=False
        )
    )
    reply = say(router, "上一批货全部作废").reply
    assert "将删除" not in reply
    assert len(app.state.ctx.repo.all_items()) == before


def test_ai_intent_out_last_without_reference_is_ignored(app, router) -> None:
    """没有「上一批」可指代时不能乱动。"""
    app.state.ctx.ai.llm = FakeLLM(
        json.dumps({"command": "out_last", "confidence": 0.95}, ensure_ascii=False)
    )
    reply = say(router, "上一批货全部作废").reply
    assert "整批出库" not in reply
    assert len(app.state.ctx.repo.all_items()) == 3


def test_ai_intent_fires_when_count_finds_nothing(app, router) -> None:
    """「手头还有多少东西」被规则当成 count 且一条没找到 → 该让 AI 再判一次。"""
    _intent(app, "overview")
    assert "总览" in say(router, "手头还有多少东西").reply


def test_ai_intent_not_used_for_empty_low_stock(app, router) -> None:
    """「快没了的有哪些」的空结果是有效答案，不该再去问 AI。"""
    fake = _install_fake_llm(app, '{"command": "delete_all", "confidence": 0.99}')
    reply = say(router, "快没了的有哪些").reply
    assert "没有库存" in reply
    assert fake.calls == []
    assert len(app.state.ctx.repo.all_items()) == 3


def test_ai_intent_can_be_disabled(app, router) -> None:
    app.state.ctx.settings.qq_ai_intent = False
    _intent(app, "out_all")
    reply = say(router, "全部不要了").reply
    assert "整批出库" not in reply
    app.state.ctx.settings.qq_ai_intent = True


def test_ai_intent_not_used_when_rules_match(app, router) -> None:
    """规则能认出来的说法不该多花一次 LLM。"""
    fake = _install_fake_llm(app, '{"command": "out_all", "confidence": 0.99}')
    reply = say(router, "库存").reply
    assert "物品清单" in reply
    assert fake.calls == []


# --------------------------------------------------------------------------- #
# 合并重复条目
# --------------------------------------------------------------------------- #
def _seed_duplicates(app) -> dict[str, int]:
    """造一对「同一种东西、写法不同」的记录。"""
    inventory = app.state.ctx.inventory
    inventory.create_item(
        ItemCreate(name="杜邦线", quantity=10, location="B柜", aliases=["跳线"])
    )
    inventory.create_item(
        ItemCreate(name="杜邦线", quantity=5, location="A柜", aliases=["导线"])
    )
    return {r.location: r.id for r in app.state.ctx.repo.all_items() if r.name == "杜邦线"}


def test_merge_after_tidy_duplicates(app, router) -> None:
    """实测需求：整理说「这几组可能是同一种东西」之后，要能真的合并掉。"""
    ids = _seed_duplicates(app)
    app.state.ctx.ai.llm = FakeLLM(
        json.dumps(
            {
                "summary": "有重复条目",
                "changes": [],
                "duplicates": [[ids["B柜"], ids["A柜"]]],
            },
            ensure_ascii=False,
        )
    )

    prompt = say(router, "整理").reply
    assert "可能是同一种东西" in prompt
    assert "1. " in prompt                      # 分组带序号
    assert "合并 1" in prompt                    # 并且告诉用户怎么合并

    preview = say(router, "合并 1").reply
    assert "将合并 1 组" in preview
    assert "保留：" in preview and "并入：" in preview
    assert "→ 合并后 15 件" in preview
    assert "留成别名" in preview

    done = say(router, "确认").reply
    assert "已合并" in done

    records = [r for r in app.state.ctx.repo.all_items() if r.name == "杜邦线"]
    assert len(records) == 1
    assert records[0].quantity == 15
    assert records[0].location == "B柜"          # 信息更全的那条被保留
    assert "跳线" in records[0].aliases
    assert "导线" in records[0].aliases          # 被并入那条的别名也带过来了
    assert "A柜" in records[0].note              # 另一处的位置记进备注


def test_merge_all_groups(app, router) -> None:
    ids = _seed_duplicates(app)
    app.state.ctx.ai.llm = FakeLLM(
        json.dumps(
            {"summary": "重复", "changes": [], "duplicates": [[ids["B柜"], ids["A柜"]]]},
            ensure_ascii=False,
        )
    )
    say(router, "整理")
    preview = say(router, "合并这些").reply
    assert "将合并 1 组" in preview
    say(router, "确认")
    assert len([r for r in app.state.ctx.repo.all_items() if r.name == "杜邦线"]) == 1


def test_merge_by_name(app, router) -> None:
    ids = _seed_duplicates(app)
    preview = say(router, f"合并 杜邦线 @A柜 到 杜邦线 @B柜").reply
    assert "将合并 1 组" in preview
    assert "→ 合并后 15 件" in preview
    say(router, "确认")
    records = [r for r in app.state.ctx.repo.all_items() if r.name == "杜邦线"]
    assert len(records) == 1
    assert records[0].id == ids["B柜"]            # 明确说了保留哪条


def test_merge_natural_phrasing(app, router) -> None:
    ids = _seed_duplicates(app)
    reply = say(router, "把 杜邦线 @A柜 合并到 杜邦线 @B柜").reply
    assert "将合并 1 组" in reply


def test_merge_uses_kept_side_when_named(app, router) -> None:
    ids = _seed_duplicates(app)
    say(router, "合并 杜邦线 @B柜 到 杜邦线 @A柜")
    say(router, "确认")
    records = [r for r in app.state.ctx.repo.all_items() if r.name == "杜邦线"]
    assert len(records) == 1
    assert records[0].id == ids["A柜"]            # 保留的是「到」后面那条


def test_merge_warns_on_different_spec(app, router) -> None:
    """规格不同可能真的是两种东西 —— 必须提醒。"""
    inventory = app.state.ctx.inventory
    inventory.create_item(ItemCreate(name="100欧姆电阻", quantity=200, location="C库", spec="0805"))
    inventory.create_item(ItemCreate(name="100欧姆电阻", quantity=100, location="A库", spec="0603"))

    preview = say(router, "合并 100欧姆电阻 @A库 到 100欧姆电阻 @C库").reply
    assert "规格不同" in preview
    assert "0805" in preview and "0603" in preview

    say(router, "取消")
    records = [r for r in app.state.ctx.repo.all_items() if r.name == "100欧姆电阻"]
    assert len(records) == 2                      # 取消了，没合并


def test_merge_without_candidates_gives_hint(app, router) -> None:
    reply = say(router, "合并").reply
    assert "还没有可以合并的候选" in reply
    assert "整理" in reply


def test_merge_can_be_cancelled(app, router) -> None:
    ids = _seed_duplicates(app)
    before = len(app.state.ctx.repo.all_items())
    say(router, "合并 杜邦线 @A柜 到 杜邦线 @B柜")
    assert "已取消" in say(router, "取消").reply
    assert len(app.state.ctx.repo.all_items()) == before


def test_merge_manual_usage_hint(app, router) -> None:
    reply = say(router, "合并 看不懂的东西").reply
    assert "用法" in reply
    assert "合并 <要并掉的> 到 <保留的>" in reply


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("把杜邦线合并到跳线", ("杜邦线", "跳线")),
        ("杜邦线合并到跳线", ("杜邦线", "跳线")),
        ("把杜邦线合并进跳线", ("杜邦线", "跳线")),
        ("合并杜邦线到跳线", None),        # 带命令词时走命令分支
        ("杜邦线", None),
    ],
)
def test_parse_merge_request(text: str, expected) -> None:
    assert parse_merge_request(text) == expected


# --------------------------------------------------------------------------- #
# 采购清单比对
# --------------------------------------------------------------------------- #
def test_procure_spec_ignores_unit_suffix(app, router) -> None:
    """实测：库里存 ``2.54``、清单写 ``2.54mm`` —— 严格比较会误报「库存里没有」。"""
    app.state.ctx.inventory.create_item(
        ItemCreate(name="排母 11P", quantity=200, location="B柜", spec="2.54")
    )
    reply = say(router, "采购\n1. 排母 11P（2.54mm） × 200").reply
    assert "已够 1" in reply
    assert "需要 200 / 现有 200" in reply


def test_procure_handles_numbered_list(app, router) -> None:
    """实测 bug：带序号的采购清单全部匹配不上（序号混进了名称）。"""
    inventory = app.state.ctx.inventory
    inventory.create_item(ItemCreate(name="ESP32-C3", quantity=100, location="A柜"))
    inventory.create_item(ItemCreate(name="1kΩ", quantity=200, location="A柜", spec="直插"))

    reply = say(router, "采购\n1. ESP32-C3 × 100\n2. 1kΩ（直插） × 200").reply
    assert "已够 2" in reply and "缺 0" in reply
    assert "1. ESP32-C3" not in reply          # 序号不该出现在名称里


def test_procure_respects_spec(app, router) -> None:
    """实测 bug：采购对比忽略了封装，把 0603 的库存也算进了 0805 的需求。"""
    inventory = app.state.ctx.inventory
    inventory.create_item(ItemCreate(name="100uF 25V", quantity=200, location="C库", spec="0805"))
    inventory.create_item(ItemCreate(name="100uF 25V", quantity=500, location="A库", spec="0603"))

    reply = say(router, "采购\n| 名称 | 数量 | 规格 |\n| --- | --- | --- |\n| 100uF 25V | 300 | 0805 |").reply
    assert "100uF 25V（0805）" in reply
    assert "现有 200" in reply          # 只算 0805 那 200，不含 0603 的 500
    assert "还差 100" in reply
    assert "100uF 25V（0805） × 100" in reply


def test_procure_reports_other_specs(app, router) -> None:
    """要的封装没有，但同名其它封装有 —— 要说清楚，别让人以为丢了数据。"""
    app.state.ctx.inventory.create_item(
        ItemCreate(name="100uF 25V", quantity=500, location="A库", spec="0603")
    )
    reply = say(router, "采购\n| 名称 | 数量 | 规格 |\n| --- | --- | --- |\n| 100uF 25V | 300 | 0805 |").reply
    assert "库存里没有" in reply
    assert "有同名但封装不同" in reply
    assert "0603" in reply


def test_procure_without_spec_aggregates_all(app, router) -> None:
    """不写封装就汇总所有封装 —— 这是「有没有这个东西」的粗查。"""
    inventory = app.state.ctx.inventory
    inventory.create_item(ItemCreate(name="100uF 25V", quantity=200, location="C库", spec="0805"))
    inventory.create_item(ItemCreate(name="100uF 25V", quantity=500, location="A库", spec="0603"))
    reply = say(router, "采购 100uF 25V 300").reply
    assert "现有 700" in reply
    assert "已够 1" in reply


def test_procure_matches_multiword_names(app, router) -> None:
    """实测 bug：『排母 11P』明明在库，却被报成「库存里没有」。"""
    inventory = app.state.ctx.inventory
    inventory.create_item(ItemCreate(name="排母 11P", quantity=200, location="B柜", spec="2.54"))
    inventory.create_item(ItemCreate(name="排母 22P", quantity=200, location="B柜", spec="2.54"))

    reply = say(router, "采购\n排母 11P（2.54） × 200\n排母 3P（2.54） × 200").reply
    assert "已够 1" in reply and "缺 1" in reply
    assert "排母 11P（2.54）  需要 200 / 现有 200" in reply
    assert "排母 3P（2.54）" in reply


PROCURE_TABLE = """\
采购
| 名称 | 数量 |
| --- | --- |
| STM32F103C8T6 | 25 |
| 0.1uF 50V MLCC | 1000 |
| ESP32-C3 | 5 |"""


def test_procure_usage_hint(router) -> None:
    assert "把采购清单发给我" in say(router, "采购").reply


def test_procure_table_report(app, router) -> None:
    reply = say(router, PROCURE_TABLE).reply

    assert "对照采购清单：3 项" in reply
    assert "已够 1" in reply and "不足 1" in reply and "缺 1" in reply

    # 已拥有的要给出位置和数量
    assert "@ A柜-1层" in reply
    assert "需要 25 / 现有 25" in reply
    # 不足的要算出差额
    assert "需要 1000 / 现有 500" in reply
    assert "还差 500" in reply
    # 没有的要列出
    assert "ESP32-C3" in reply

    buy = reply.split("需要额外购买：")[1]
    assert "0.1uF 50V MLCC × 500" in buy
    assert "ESP32-C3 × 5" in buy
    assert "STM32F103C8T6" not in buy          # 够了的不进购买清单
    assert "合计 2 项 / 505 件" in buy


def test_procure_inline_list(app, router) -> None:
    reply = say(router, "采购 STM32F103C8T6 25, NE555 10").reply
    assert "对照采购清单：2 项" in reply
    assert "已够 1" in reply          # STM32 够
    assert "缺 1" in reply            # NE555 没有
    assert "NE555 × 10" in reply


def test_procure_all_satisfied(app, router) -> None:
    reply = say(router, "采购 STM32F103C8T6 5").reply
    assert "不需要额外采购" in reply


def test_procure_quantity_optional(app, router) -> None:
    reply = say(router, "采购 STM32F103C8T6").reply
    assert "清单没写数量" in reply
    assert "不需要额外采购" in reply


# --------------------------------------------------------------------------- #
# 入库已有物品的位置处理
# --------------------------------------------------------------------------- #
def test_stock_in_existing_without_location_needs_no_prompt(app, router) -> None:
    """入库已有物品、不填位置 → 直接累加，不追问位置。"""
    reply = say(router, "入库 STM32F103C8T6 5").reply
    assert "入库成功" in reply
    assert "合并" not in reply and "分开" not in reply
    record = next(r for r in app.state.ctx.repo.all_items() if r.name == "STM32F103C8T6")
    assert record.quantity == 30
    assert record.location == "A柜-1层"       # 位置保持不变


def test_stock_in_existing_with_new_location_asks(app, router) -> None:
    reply = say(router, "入库 STM32F103C8T6 5 @B柜-9层").reply
    assert "已经有一条记录了" in reply
    assert "@ A柜-1层 × 25" in reply
    assert "你这次填的位置是「B柜-9层」" in reply
    assert "① 合并" in reply and "② 分开" in reply
    # 询问期间不能落库
    record = next(r for r in app.state.ctx.repo.all_items() if r.name == "STM32F103C8T6")
    assert record.quantity == 25


def test_location_choice_merge(app, router) -> None:
    say(router, "入库 STM32F103C8T6 5 @B柜-9层")
    done = say(router, "合并").reply
    assert "已合并位置" in done
    records = [r for r in app.state.ctx.repo.all_items() if r.name == "STM32F103C8T6"]
    assert len(records) == 1
    assert records[0].location == "B柜-9层"
    assert records[0].quantity == 30


def test_location_choice_split(app, router) -> None:
    say(router, "入库 STM32F103C8T6 5 @B柜-9层")
    done = say(router, "分开").reply
    assert "另建一条" in done
    records = [r for r in app.state.ctx.repo.all_items() if r.name == "STM32F103C8T6"]
    assert len(records) == 2
    by_location = {r.location: r.quantity for r in records}
    assert by_location == {"A柜-1层": 25, "B柜-9层": 5}


def test_location_choice_cancel(app, router) -> None:
    say(router, "入库 STM32F103C8T6 5 @B柜-9层")
    assert "已取消" in say(router, "取消").reply
    records = [r for r in app.state.ctx.repo.all_items() if r.name == "STM32F103C8T6"]
    assert len(records) == 1
    assert records[0].quantity == 25


def test_location_choice_asks_again_on_unclear_input(app, router) -> None:
    say(router, "入库 STM32F103C8T6 5 @B柜-9层")
    reply = say(router, "嗯……").reply
    assert "回复「合并」或「分开」" in reply


def test_same_location_does_not_ask(app, router) -> None:
    reply = say(router, "入库 STM32F103C8T6 5 @A柜-1层").reply
    assert "入库成功" in reply
    assert "合并" not in reply


# --------------------------------------------------------------------------- #
# 不同封装 = 不同的东西（实测 bug：0805 与 SOP-8 被并成一条）
# --------------------------------------------------------------------------- #
def test_different_spec_creates_separate_variant(app, router) -> None:
    reply = say(router, "入库 0.1uF 50V MLCC（DIP-8） 100").reply
    assert "入库成功" in reply

    records = [r for r in app.state.ctx.repo.all_items() if "0.1uF" in r.name]
    by_spec = {r.spec: r for r in records}
    assert set(by_spec) == {"0805", "DIP-8"}
    assert by_spec["0805"].quantity == 500          # 原来那条没被动过
    assert by_spec["DIP-8"].quantity == 100
    assert by_spec["DIP-8"].location == "A柜-2层"    # 跟随原记录的位置
    assert "104" in by_spec["DIP-8"].aliases        # 别名一起带过去


def test_same_spec_still_merges(app, router) -> None:
    reply = say(router, "入库 0.1uF 50V MLCC（0805） 100").reply
    assert "入库成功" in reply
    records = [r for r in app.state.ctx.repo.all_items() if "0.1uF" in r.name]
    assert len(records) == 1
    assert records[0].quantity == 600


def test_variant_is_reused_on_second_entry(app, router) -> None:
    say(router, "入库 0.1uF 50V MLCC（DIP-8） 100")
    say(router, "入库 0.1uF 50V MLCC（DIP-8） 50")
    records = [r for r in app.state.ctx.repo.all_items() if "0.1uF" in r.name]
    assert {r.spec: r.quantity for r in records} == {"0805": 500, "DIP-8": 150}


def test_spec_is_filled_when_record_has_none(app, router) -> None:
    """原记录没规格时，补规格 = 补全，不该新建品种。"""
    app.state.ctx.inventory.create_item(ItemCreate(name="电阻存货", quantity=10, location="C柜"))
    reply = say(router, "入库 电阻存货（0805） 5").reply
    assert "入库成功" in reply
    records = [r for r in app.state.ctx.repo.all_items() if r.name == "电阻存货"]
    assert len(records) == 1
    assert records[0].spec == "0805"
    assert records[0].quantity == 15


def test_user_reported_case(app, router) -> None:
    """用户实测：『入库 100Ω电阻 200』→『入库 100Ω电阻（SOP-8） 200』不能再合并。"""
    app.state.ctx.inventory.create_item(
        ItemCreate(name="100欧姆电阻", category="stm_component", quantity=120,
                   location="C库", spec="0805")
    )
    first = say(router, "入库 100Ω电阻 200").reply
    assert "120 → 320" in first

    second = say(router, "入库 100Ω电阻（SOP-8） 200").reply
    assert "入库成功" in second

    records = [r for r in app.state.ctx.repo.all_items() if "电阻" in r.name and "100" in r.name]
    assert {r.spec: r.quantity for r in records} == {"0805": 320, "SOP-8": 200}
