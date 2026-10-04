"""QQ 对话上下文。

没有上下文的时候，机器人只能对每条消息孤立作答，于是会出现：

* 机器人列出 1~5 号候选，用户回「2」—— 被当成搜「2」
* 用户说「将这些物品全部出库」—— 不知道「这些」指什么
* 危险操作没人拦，一句话就把库存清零

这里为每个会话（群 / 单聊）保留一个短生命周期的小状态，解决上面三件事：

* :attr:`Session.candidates` —— 最近一次列出的编号清单，支持回「2」选择
* :attr:`Session.candidate_intent` —— 这批候选是为了哪个操作列的（回「2」会把它执行完）
* :attr:`Session.last_item_ids` —— 「这些物品」指代的上一次结果集
* :attr:`Session.pending` —— 待二次确认的危险操作

状态只存在内存里（单进程部署足够），带 TTL 自动过期。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from ..utils import fmt_qty

#: 默认 15 分钟没交互就丢弃上下文
DEFAULT_TTL_SECONDS = 900
#: 最多同时保留多少会话，防止内存无限增长
DEFAULT_MAX_SESSIONS = 1000

AFFIRMATIVE_WORDS = {
    "确认", "确定", "是", "是的", "好", "好的", "可以", "执行", "继续", "对", "嗯",
    "yes", "y", "ok", "confirm",
}
NEGATIVE_WORDS = {
    "取消", "不", "否", "不要", "不用", "算了", "放弃", "停止", "别",
    "no", "n", "cancel",
}

#: ``2`` / ``第2个`` / ``2号``
_SINGLE_SELECTION = re.compile(r"^\s*(?:第\s*)?(\d{1,3})\s*(?:号|个|项|条)?\s*$")
#: ``1、3`` / ``2和3`` / ``第1,第2``
_MULTI_SELECTION = re.compile(
    r"^\s*(?:第\s*)?\d{1,3}\s*(?:号|个|项|条)?"
    r"(?:\s*(?:[、,，]|和|与|跟)\s*(?:第\s*)?\d{1,3}\s*(?:号|个|项|条)?)+\s*$"
)
_NUMBER_RE = re.compile(r"\d{1,3}")


def is_affirmative(text: str) -> bool:
    return (text or "").strip().casefold() in AFFIRMATIVE_WORDS


def is_negative(text: str) -> bool:
    return (text or "").strip().casefold() in NEGATIVE_WORDS


def parse_selection(text: str) -> list[int]:
    """把「2」「第2个」「1、3」解析成序号列表；不是选择就返回空列表。

    必须整条消息都是序号，避免把 ``STM32F103`` 之类的型号当成选择。
    """
    raw = (text or "").strip()
    if not raw:
        return []
    if _SINGLE_SELECTION.match(raw):
        return [int(_SINGLE_SELECTION.match(raw).group(1))]  # type: ignore[union-attr]
    if _MULTI_SELECTION.match(raw):
        return [int(n) for n in _NUMBER_RE.findall(raw)]
    return []


@dataclass
class Candidate:
    """清单里的一项。``index`` 就是用户要回的序号。"""

    index: int
    item_id: int
    name: str
    quantity: float = 0.0
    location: str = ""
    spec: str = ""
    category: str = ""

    def render(self) -> str:
        text = f"{self.index}. {self.name}"
        if self.spec:
            text += f"（{self.spec}）"
        text += f" × {fmt_qty(self.quantity)}"
        if self.quantity <= 0:
            text += "（已清零）"
        if self.location:
            text += f" @ {self.location}"
        return text

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "id": self.item_id,
            "name": self.name,
            "quantity": self.quantity,
            "location": self.location,
            "spec": self.spec,
            "category": self.category,
        }


@dataclass
class PendingIntent:
    """「这批候选是为了干什么」—— 用户回序号时接着把这个操作做完。"""

    action: str  # in / out / set
    quantity: float
    spec: str | None = None
    location: str | None = None

    def describe(self) -> str:
        verb = {"in": "入库", "out": "出库", "set": "盘点"}.get(self.action, self.action)
        return f"{verb} {fmt_qty(self.quantity)}"


@dataclass
class PendingConfirmation:
    """等待「确认 / 取消」的危险操作。"""

    description: str
    action: str
    item_ids: list[int] = field(default_factory=list)
    #: 额外的执行参数（例如待导入的原文、AI 整理方案）
    payload: dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)


@dataclass
class PromptSlot:
    """等待用户补全的一个字段（入库后追问封装、别名等）。"""

    kind: str  # "spec" | "alias"
    item_id: int
    item_name: str


@dataclass
class Session:
    key: str
    candidates: list[Candidate] = field(default_factory=list)
    candidate_intent: PendingIntent | None = None
    last_item_ids: list[int] = field(default_factory=list)
    last_label: str = ""
    pending: PendingConfirmation | None = None
    #: 待补全的字段队列（一次问一个）
    prompts: list[PromptSlot] = field(default_factory=list)
    #: 用户明确跳过过的 (item_id, kind)，同一个会话里不再重复追问
    declined: set[tuple[int, str]] = field(default_factory=set)
    #: 上一次「整理」找出的「可能是同一种东西」分组（item_id 列表），供「合并 1」使用
    merge_groups: list[list[int]] = field(default_factory=list)
    touched_at: float = field(default_factory=time.time)

    # ------------------------------------------------------------------ 更新
    def touch(self) -> None:
        self.touched_at = time.time()

    def set_candidates(
        self,
        candidates: list[Candidate],
        *,
        intent: PendingIntent | None = None,
        label: str = "",
    ) -> None:
        self.candidates = list(candidates)
        self.candidate_intent = intent
        self.last_item_ids = [c.item_id for c in candidates]
        self.last_label = label
        self.touch()

    def set_items(self, item_ids: list[int], *, label: str = "") -> None:
        self.last_item_ids = list(item_ids)
        self.last_label = label
        self.candidates = []
        self.candidate_intent = None
        self.touch()

    def consume_candidates(self) -> None:
        self.candidates = []
        self.candidate_intent = None
        self.touch()

    def set_pending(self, pending: PendingConfirmation) -> None:
        self.pending = pending
        self.touch()

    def clear_pending(self) -> None:
        self.pending = None
        self.touch()

    # ------------------------------------------------------------------ 补全
    def ask(self, slots: list[PromptSlot]) -> None:
        """开始追问；同时清掉候选，避免用户回数字时歧义。"""
        self.prompts = list(slots)
        self.candidates = []
        self.candidate_intent = None
        self.touch()

    def current_prompt(self) -> PromptSlot | None:
        return self.prompts[0] if self.prompts else None

    def pop_prompt(self) -> PromptSlot | None:
        slot = self.prompts.pop(0) if self.prompts else None
        self.touch()
        return slot

    def decline_prompt(self) -> PromptSlot | None:
        """用户回了「跳过」：记住不再重复问同一字段。"""
        slot = self.current_prompt()
        if slot is not None:
            self.declined.add((slot.item_id, slot.kind))
        return self.pop_prompt()

    def clear_prompts(self) -> None:
        self.prompts = []
        self.touch()

    def pick(self, numbers: list[int]) -> list[Candidate]:
        wanted = set(numbers)
        return [c for c in self.candidates if c.index in wanted]


class SessionStore:
    """按会话键保存上下文，带 TTL 与容量上限。"""

    def __init__(self, ttl_seconds: float = DEFAULT_TTL_SECONDS, max_sessions: int = DEFAULT_MAX_SESSIONS) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_sessions = max_sessions
        self._sessions: dict[str, Session] = {}

    def get(self, key: str) -> Session:
        self._prune()
        session = self._sessions.get(key)
        if session is None:
            session = Session(key=key)
            self._sessions[key] = session
            # 插入后再收一次：只在插入前检查的话，稳态会是 max+1 条
            self._prune()
        session.touch()
        return session

    def drop(self, key: str) -> None:
        self._sessions.pop(key, None)

    def clear(self) -> None:
        self._sessions.clear()

    def __len__(self) -> int:
        return len(self._sessions)

    def _prune(self) -> None:
        now = time.time()
        for key in [k for k, s in self._sessions.items() if now - s.touched_at > self.ttl_seconds]:
            self._sessions.pop(key, None)
        overflow = len(self._sessions) - self.max_sessions
        if overflow > 0:
            oldest = sorted(self._sessions.items(), key=lambda kv: kv[1].touched_at)[:overflow]
            for key, _ in oldest:
                self._sessions.pop(key, None)


def build_candidates(records, *, start: int = 1) -> list[Candidate]:
    """从 ``ItemRecord`` / ``ItemOut`` 列表生成带序号的候选。"""
    result: list[Candidate] = []
    for offset, record in enumerate(records):
        record_id = getattr(record, "id", None)
        if record_id is None:
            continue
        result.append(
            Candidate(
                index=start + offset,
                item_id=int(record_id),
                name=getattr(record, "name", ""),
                quantity=float(getattr(record, "quantity", 0) or 0),
                location=getattr(record, "location", "") or "",
                spec=getattr(record, "spec", "") or "",
                category=getattr(record, "category", "") or "",
            )
        )
    return result


def build_candidates_from_hits(records: list[dict]) -> list[Candidate]:
    """从接口返回的 ``items`` 字典列表生成候选。"""
    result: list[Candidate] = []
    for offset, record in enumerate(records, start=1):
        if not record.get("id"):
            continue
        result.append(
            Candidate(
                index=offset,
                item_id=int(record["id"]),
                name=str(record.get("name", "")),
                quantity=float(record.get("quantity", 0) or 0),
                location=str(record.get("location", "") or ""),
                spec=str(record.get("spec", "") or ""),
                category=str(record.get("category", "") or ""),
            )
        )
    return result
