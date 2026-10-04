"""大模型驱动的分析与导入规范化。

对应需求里的两处「AI」：

* ``/api/v1/analysis/ai`` —— 把库存快照交给大模型出洞察与补货建议
* ``/api/v1/import/ai``   —— 把任意格式的原始文本交给大模型整理成
  规范化 Markdown，再走普通的 Markdown 导入流程
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Sequence

from ..config import Settings
from ..core.categories import DEFAULT_CATEGORY, match_category, normalize_category_code
from ..core.normalize import fold, tokenize_query
from ..errors import UpstreamError, ValidationFailed
from ..models import (
    AIAnalysisResponse,
    AIStockParse,
    AnalyzeSummary,
    DetailPrompts,
    ImportPreviewOut,
    IntentPlan,
    ItemOut,
    ItemUpdate,
    TidyChange,
    TidyPlan,
)
from ..repository import Repository
from ..utils import fmt_qty
from .analysis import AnalysisService
from .categorize import category_code, category_label
from .importer import ImportService, parse_markdown
from .inventory import InventoryService
from .llm import LLMClient

logger = logging.getLogger(__name__)

#: 规范化 Markdown 的格式约定，同时给人和模型看
MARKDOWN_SPEC = """\
输出必须是 Markdown，结构如下（不要输出任何解释文字、不要用代码块包裹整篇）：

# 库存清单

## STM元器件

| 名称 | 数量 | 位置 | 规格 | 别名 | 备注 |
| --- | --- | --- | --- | --- | --- |
| STM32F103C8T6 | 25 | A柜-1层-盒3 | LQFP48 | F103C8, STM32F103 | 蓝药丸板用 |

## Steam游戏卡

| 名称 | 数量 | 位置 | 规格 | 别名 | 备注 |
| --- | --- | --- | --- | --- | --- |
| Steam 50元充值卡 | 10 | B柜-抽屉1 | 50元 | 50元卡 | |

规则：
1. 一级分类**不预设**：优先复用【已有分类】；都不合适才新建一个简短的中文分类名。
   不要用「其他」兜底 —— 实在判断不了才写「未分类」。没有对应内容就省略该章节。
2. 「名称」必填，保持原始型号写法（如 STM32F103C8T6、1N4148、LM358），不要翻译、不要改大小写。
3. 「数量」是纯数字。原文写「25个」「25 pcs」「一盒(约25)」都归一成 25；确实没有数量的填 0。
4. 「位置」原样保留（如 A柜-1层-盒3）。原文没写的留空。
5. 「规格/封装」如 LQFP48、0805、SOIC-8、50元；没有就留空。
6. 「别名」用英文逗号分隔，把原文里出现的同义写法、料号、简称都放进去（上限 6 个）。
7. 表格单元格里不要出现竖线 `|`，需要时用 `/` 代替。
8. 只输出你确实从原文读到的内容，**不要凭空添加物品**。
"""


def _loads_json(text: str) -> dict:
    """尽量从模型输出里抠出 JSON（兼容代码块包裹、前后夹带解释文字的情况）。"""
    cleaned = _strip_code_fence(text)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            raise UpstreamError(f"大模型没有返回合法 JSON：{cleaned[:200]}") from None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise UpstreamError(f"大模型返回的 JSON 解析失败：{exc}") from exc
    if not isinstance(data, dict):
        raise UpstreamError("大模型返回的 JSON 顶层不是对象")
    return data


# --------------------------------------------------------------------------- #
# 提示词
# --------------------------------------------------------------------------- #

STOCK_PARSE_SYSTEM = """\
你是仓储机器人的语义解析器。用户会用口语描述库存变更，你要把它解析成结构化指令。

只输出一个 JSON 对象，不要任何解释文字、不要 markdown 代码块。

字段：
- action: "in"（入库/进货/收了/增加）、"out"（出库/领用/用了/拿走了/减少）、
          "set"（盘点/更正为/清点后是）、"unknown"（没说清楚要干什么）
- name: 物品名称。保持原始型号写法（STM32F103C8T6、1N4148、Steam 50元充值卡），
        不要翻译、不要补全、不要改大小写。用户说的别名（比如「电容」）也照原样放这里。
- quantity: 数量，纯数字。中文数字要换算（二十五 → 25）。原文没提数量就填 0。
- location: 位置，原样保留（A柜-1层-盒3、C库）。没提就填空字符串。
- spec: 规格/封装（LQFP48、0805、DO-35、50元）。没提就填空字符串。
- category: 分类。**优先复用给出的已有分类**；都不合适时用一个简短的中文分类名自己新建
            （例如「开发板」「传感器」「耗材」）。完全判断不了才填「未分类」。
- aliases: 原文里出现的同义写法或别称（最多 6 个），没有就给空数组。
- confidence: 0~1，你对这次解析的把握。
- reason: 一句话说明依据（中文，不超过 30 字）。

注意：
- 「还有多少」「放哪了」这类是**查询**不是变更，action 填 "unknown"。
- 不要把数量单位当成规格；不要把型号里的数字当成数量。
- 拿不准就降低 confidence，绝对不要编造。
"""

INTENT_SYSTEM = """\
你在理解用户对一个「仓库管理机器人」说的话，判断它对应哪条指令。
规则匹配已经失败过一次，所以现在轮到你来判断 —— 但**拿不准就填 unknown，不要硬猜**。

可选指令（command 字段填左边的英文）：
- out_all      把库存**全部**出库清零（「清除全部」「全部清掉」「这些都不要了」）
- out_last     把**上一批列出的**物品出库清零（「把这些用掉」「清单里的东西都不要了」）
- delete_last  把**上一批列出的**物品记录**删掉**（「把采购清单里的物品删除」「这些删掉」）
- zero_list    看已清零的条目（「零库存」「哪些已经没了」）
- zero_clean   删除已清零的条目（「清理零库存」）
- list         列出在库物品（「库存」「都有些什么」）
- overview     数量概览 / 统计（「总共有多少」「统计一下」）
- low          库存偏低的物品（「快没了的有哪些」）
- search       按名称查找（name 填关键词）
- category     看某个分类（name 填分类名）
- location     看某个位置（name 填位置名）
- tidy         让 AI 整理数据（「整理」「规整一下」）
- import       导入清单（「导入」「我要批量录入」）
- help         用法说明（「你能干什么」）
- delete_all   清空全部记录（「删除全部」「记录都删了」）
- stock_in     **入库**（「进了 200 个杜邦线放 A 柜」）
- stock_out    **出库**（「用掉 50 个」「拿了 10 个走」）
- stock_set    **盘点 / 改数量**（「杜邦线现在只剩 30 个」）
- unknown      判断不出来

要求：
- stock_in / stock_out / stock_set 必须能确定 name；**数量说不清就填 0**，不要猜
- 只有「确实在描述一次库存变动」时才选 stock_*；模糊的疑问句多半是 search 或 unknown
- **out_last / delete_last 只在用户明确指代「刚才列出的那批」时才用**（「这些」「清单里的」
  「采购清单里的」）；说的是「全部」「所有」一律用 out_all
- confidence 是你对这个判断的可信度（0~1）；犹豫就给低分或 unknown

示例：
「清除全部」        → {"command": "out_all", "confidence": 0.95, "reason": "要清空库存"}
「从库存里清除这些」  → {"command": "out_last", "confidence": 0.9, "reason": "处置刚列出的那批"}
「把采购清单里的物品删掉」→ {"command": "delete_last", "confidence": 0.9, "reason": "删掉刚对比的那批"}
「快没了的还有哪些」  → {"command": "low", "confidence": 0.9, "reason": "问库存偏低"}
「进了两盒杜邦线放A柜」→ {"command": "stock_in", "name": "杜邦线", "quantity": 2, "location": "A柜", "confidence": 0.85}
「帮我把记录都清掉」  → {"command": "delete_all", "confidence": 0.8, "reason": "要删记录"}
「那个东西呢」        → {"command": "unknown", "confidence": 0.2, "reason": "指代不明"}

只输出 JSON：
{"command": "...", "name": "", "quantity": 0, "location": "", "confidence": 0.9, "reason": "一句话理由"}
"""

DETAIL_PROMPT_SYSTEM = """\
你在为一个个人仓库机器人做「要不要追问」的判断。用户刚录入/更新了一条记录，
机器人可以追问两个字段，但**每次追问都会打断用户**，所以要能不问就不问。

请判断这两问是否值得问：
- ask_spec：问「封装/规格」是否值得？
  · 值得：有型号的电子元件（STM32F103C8T6、NE555、1N4148、AMS1117），封装会影响选料与搜索
  · 不值得：游戏卡带、书籍、日用品、耗材等本来就没有「封装」概念的东西
- ask_alias：问「还有别的叫法吗」是否值得？
  · 值得：名字有明显异写/简称（100欧姆电阻 ↔ 100Ω电阻、杜邦线 ↔ 跳线、ESP32-C3 ↔ ESP32C3）
  · 不值得：名字已经足够独特、或本来就不靠搜索找（主角、巫师三、排针）

宁可少问：只有确实能提升后续搜索准确率时才设 true。

只输出 JSON：
{"ask_spec": true/false, "ask_alias": true/false, "reason": "一句话理由"}
"""

IMPORT_ANALYZE_SYSTEM = """\
你在为一个个人仓库整理**即将导入**的物品数据。对每一条做两件事：规范命名 + 归类。

【规范命名 name】
- 名称只写「身份」：**型号 / 数值带单位 / 介质**
- 封装与规格（0805、DIP-8、SOT-223、直插、2.54）**不要写进名称** —— 它们已经在单独的「规格」字段里
- 例子：100nF 50V MLCC（规格 0805）、10kΩ（规格 0805）、51Ω 0.25W（规格 直插）、
  STM32F103C8T6（规格 LQFP48）
- 元件中文名统一用：电阻 / 电容 / 电感 / 发光二极管 / 排针 / 排母 / 轻触开关 / 晶振
- **不要写类型字母前缀**（R、C、L）—— 单位已经说明了类型
- 单位规范写法：uF / nF / pF / Ω / kΩ / MΩ / uH / mH / V / W
- 大小写规范：LED、MLCC、SMD
- **完整型号原文照抄**，只把字母统一大写：stm32f103c8t6 → STM32F103C8T6
- 名称里不要出现数量、位置、别名、备注
- **只调整格式，绝不臆测原件里没有的信息**；本来就已经规范的名称原样返回
- 分隔符统一成单个半角空格

【归类 category】
- 优先复用已有分类；装不下就新建一个简短通用的中文分类（2~5 个汉字）
- 目的是以后按分类能找到东西，**不要把互不相关的东西全塞进「其他」**
- 同一批里同类物品必须同名同类

只输出 JSON，index 与输入序号一一对应，一条都不能漏：
{"items": [{"index": 1, "name": "10kΩ 0805", "category": "STM元器件", "reason": "去掉 R 前缀"}]}
"""

#: AI 改名护栏：出现这些字符说明它把数量/位置/别名混进了名称
_NAME_BAD_CHARS = re.compile(r"[@#|\r\n\t]")
_NAME_HAS_CONTENT = re.compile(r"[0-9A-Za-z\u4e00-\u9fff]")


def _name_is_acceptable(old: str, new: str) -> bool:
    """判断 AI 给的新名称是否可以接受。

    宁可少改也不要改坏：改名必须**保留原名里至少一个「有信息量」的片段**
    （含数字，或折叠后长度 ≥2），否则 ``10kΩ 0805`` 可能被改成「电阻」这种丢信息的写法。
    """
    candidate = (new or "").strip()
    original = (old or "").strip()
    if not candidate or candidate == original:
        return False
    if len(candidate) > 60 or not _NAME_HAS_CONTENT.search(candidate):
        return False
    if _NAME_BAD_CHARS.search(candidate):
        return False

    folded = fold(candidate)
    for token in tokenize_query(original):
        piece = fold(token)
        if not piece:
            continue
        if len(piece) < 2 and not any(char.isdigit() for char in token):
            continue
        if piece in folded:
            return True
    return False


TIDY_SYSTEM = """\
你是仓储数据整理助手。给你一份库存条目清单，你要指出哪些字段可以规范化。

只输出一个 JSON 对象，不要任何解释文字、不要 markdown 代码块。

{
  "summary": "一句话总结这次整理的主要动作",
  "changes": [
    {"item_id": 3, "category": "开发板", "spec": "LQFP48", "aliases": ["F103"],
     "name": "规范后的名称", "reason": "为什么这么改"}
  ],
  "duplicates": [[1, 5]]
}

规则：
- **只列出确实需要改的字段**，不需要改的直接省略，不要原样回填。
- category：已有分类码优先；确实不合适可以新建一个简短中文分类名。
- spec：能从名称里看出来的封装/规格补齐（LQFP48、0805、SOT-223、50元）。
- aliases：把名称里的同义写法、常见简称补进去（每条最多 4 个），已有的不要重复。
- name：只有当名称明显不规范（多余空格、混进位置或数量）时才给，否则省略。
- duplicates：只把**确信是同一种东西**的条目 id 分组（如 100欧姆电阻 与 100Ω电阻）。
  拿不准就不要放进来。
- 不要编造型号、数量或分类；没把握的条目直接跳过。
"""


class AIService:
    def __init__(
        self,
        repo: Repository,
        settings: Settings,
        llm: LLMClient,
        analysis: AnalysisService,
        importer: ImportService,
        inventory: InventoryService,
    ) -> None:
        self.repo = repo
        self.settings = settings
        self.llm = llm
        self.analysis = analysis
        self.importer = importer
        self.inventory = inventory

    # ------------------------------------------------------------------ 分析
    async def analyze(
        self,
        question: str,
        *,
        category: str | None = None,
        location: str | None = None,
        include_items: bool = True,
        item_limit: int = 200,
    ) -> AIAnalysisResponse:
        if not self.llm.ready:
            raise UpstreamError("大模型未配置：请在 .env 里设置 LLM_ENABLED=true 与 LLM_API_KEY")

        records = self.repo.all_items(category=category)
        if location:
            from ..core.normalize import fold

            key = fold(location)
            records = [r for r in records if fold(r.location) == key]
        if not records:
            raise ValidationFailed("没有可分析的库存数据（筛选后为空）")

        digest = "\n".join(
            f"- {r.name}{f'（{r.spec}）' if r.spec else ''} × {fmt_qty(r.quantity)}"
            f" @ {r.location or '未指定位置'} [{'/'.join(r.aliases) if r.aliases else '无别名'}]"
            for r in records[:item_limit]
        )
        stats = self.analysis.overview_text()

        messages = [
            {
                "role": "system",
                "content": (
                    "你是一名资深的电子元器件与数字资产仓储管理顾问。"
                    "只能依据给定数据作答，禁止编造型号或数量。"
                    "输出用简体中文，结构清晰，适合在聊天窗口阅读：先给结论，再给分点建议。"
                    "涉及补货建议时，要指出具体型号和当前数量。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"【统计概览】\n{stats}\n\n【库存明细】\n{digest}\n\n"
                    f"【问题】\n{question}"
                ),
            },
        ]
        result = await self.llm.chat(messages, temperature=0.3, max_tokens=1600)
        return AIAnalysisResponse(
            question=question,
            answer=result["content"].strip(),
            model=result["model"],
            item_count=len(records),
            prompt_tokens=result.get("prompt_tokens", 0),
            completion_tokens=result.get("completion_tokens", 0),
        )

    # ------------------------------------------------------------------ 导入规范化
    async def normalize_to_markdown(self, raw_text: str, *, filename: str = "ai-import") -> str:
        """把任意格式的原始文本整理成规范化 Markdown。"""
        if not self.llm.ready:
            raise UpstreamError("AI 规范化需要大模型：请在 .env 里设置 LLM_ENABLED=true 与 LLM_API_KEY")
        if not raw_text.strip():
            raise ValidationFailed("原始内容为空")

        # 太长的内容先截断，避免超出上下文
        limit = 12000
        clipped = raw_text[:limit]
        note = "" if len(raw_text) <= limit else f"\n\n（注意：原文过长，只提供了前 {limit} 个字符）"

        messages = [
            {"role": "system", "content": MARKDOWN_SPEC},
            {
                "role": "user",
                "content": (
                    f"下面是仓库管理员提供的原始库存资料（可能来自 Excel 复制、聊天记录或随手记），"
                    f"请整理成规范化 Markdown 表格。\n\n---\n{clipped}{note}"
                ),
            },
        ]
        result = await self.llm.chat(messages, temperature=0.0, max_tokens=3000)
        content = result["content"].strip()
        if "|" not in content:
            logger.warning("大模型返回的内容不含表格，原样返回供人工检查")
        return _strip_code_fence(content)

    async def ai_import(
        self,
        raw_text: str,
        *,
        filename: str = "ai-import",
        mode: str = "merge",
        operator: str = "",
        dry_run: bool = False,
    ) -> tuple[str, ImportPreviewOut, object]:
        markdown = await self.normalize_to_markdown(raw_text, filename=filename)
        preview = parse_markdown(markdown)
        preview.filename = f"{filename}（AI 规范化）"
        result = self.importer.commit(preview, mode=mode, operator=operator, source="import", dry_run=dry_run)
        return markdown, preview, result

    # ------------------------------------------------------------------ 语义解析
    def known_categories(self) -> list[str]:
        """库里出现过的分类 —— **没有内置分类**，全库的分类就是全部词汇表。"""
        return [row["category"] for row in self.repo.category_stats() if row["category"]]

    async def classify_intent(self, text: str) -> IntentPlan:
        """规则认不出时，让大模型判断用户想执行哪条指令。"""
        if not self.llm.ready:
            raise UpstreamError(
                "AI 意图识别需要大模型：请在 .env 里设置 LLM_ENABLED=true 与 LLM_API_KEY"
            )
        if not (text or "").strip():
            raise ValidationFailed("待识别的消息为空")

        messages = [
            {"role": "system", "content": INTENT_SYSTEM},
            {"role": "user", "content": text[:1000]},
        ]
        result = await self.llm.chat(messages, temperature=0.0, max_tokens=300)
        data = _loads_json(result["content"])

        try:
            quantity = max(0.0, float(data.get("quantity") or 0))
        except (TypeError, ValueError):
            quantity = 0.0
        try:
            confidence = max(0.0, min(1.0, float(data.get("confidence") or 0)))
        except (TypeError, ValueError):
            confidence = 0.0

        return IntentPlan(
            command=str(data.get("command") or "unknown").strip().lower(),
            name=str(data.get("name") or "").strip(),
            quantity=quantity,
            location=str(data.get("location") or "").strip(),
            confidence=confidence,
            reason=str(data.get("reason") or "")[:120],
        )

    async def analyze_rows(
        self,
        rows: Sequence[Any],
        *,
        normalize: bool = True,
        classify: bool = True,
        chunk: int = 40,
    ) -> dict[int, dict[str, str]]:
        """**一次调用**同时拿到「规范名称 + 分类」。

        返回 ``{行下标(0-based): {"name": 规范名, "category": 分类码}}``。
        AI 不可用、返回不合法或调用失败时返回空 dict —— 调用方保留原值。
        """
        if not self.llm.ready or not rows or not (normalize or classify):
            return {}

        existing = self.known_categories()
        result: dict[int, dict[str, str]] = {}

        for start in range(0, len(rows), chunk):
            batch = rows[start : start + chunk]
            listing = "\n".join(
                f"{start + offset + 1}. {row.name}"
                + (f"（规格 {row.spec}）" if getattr(row, "spec", "") else "")
                + (f"（当前分类 {category_label(row.category)}）" if getattr(row, "category", "") else "")
                for offset, row in enumerate(batch)
            )
            messages = [
                {"role": "system", "content": IMPORT_ANALYZE_SYSTEM},
                {
                    "role": "user",
                    "content": f"【已有分类】{', '.join(existing)}\n【物品】\n{listing}",
                },
            ]
            try:
                response = await self.llm.chat(messages, temperature=0.0, max_tokens=3000)
                data = _loads_json(response["content"])
            except Exception:  # noqa: BLE001 - 预处理失败不能挡住导入
                logger.warning("AI 导入预处理失败（第 %d 批），保留原值", start // chunk + 1, exc_info=True)
                continue

            # 兼容模型把键写成 items / categories
            for item in data.get("items") or data.get("categories") or []:
                if not isinstance(item, dict):
                    continue
                try:
                    number = int(item.get("index"))
                except (TypeError, ValueError):
                    continue
                if not 1 <= number <= len(rows):
                    continue

                plan: dict[str, str] = {}
                if normalize:
                    name = str(item.get("name") or "").strip()
                    if name:
                        plan["name"] = name
                if classify:
                    label = str(item.get("category") or "").strip()
                    if label:
                        plan["category"] = match_category(label) or normalize_category_code(label)
                if plan:
                    result[number - 1] = plan

        return result

    async def analyze_preview(
        self,
        preview: ImportPreviewOut,
        *,
        normalize: bool = True,
        classify: bool = True,
    ) -> AnalyzeSummary:
        """对导入预览做 AI 预处理：规范命名 + 归类。

        改名很危险，所以有两道护栏：

        * :func:`_name_is_acceptable` —— 新名必须保留原名里至少一个有信息量的片段
        * **原名会同时存成别名** —— 就算改得不合你意，搜旧写法照样找得到
        """
        summary = AnalyzeSummary()
        plans = await self.analyze_rows(
            list(preview.rows), normalize=normalize, classify=classify
        )
        if not plans:
            return summary

        known = set(self.known_categories()) | {DEFAULT_CATEGORY}
        for position, plan in plans.items():
            if not 0 <= position < len(preview.rows):
                continue
            row = preview.rows[position]

            new_name = plan.get("name", "")
            if normalize and new_name and _name_is_acceptable(row.name, new_name):
                if fold(row.name) not in {fold(alias) for alias in row.aliases}:
                    row.aliases = [*row.aliases, row.name]   # 原名保留为别名
                row.original_name = row.name
                row.name = new_name
                summary.renamed += 1

            code = plan.get("category", "")
            if classify and code:
                row.category = code  # type: ignore[assignment]
                summary.categorized += 1
                if code not in known and code not in summary.new_categories:
                    summary.new_categories.append(code)

        preview.new_categories = summary.new_categories
        return summary

    async def judge_detail_prompts(self, record: Any) -> DetailPrompts:
        """判断这条记录是否值得追问封装/别名（不值得就直接入库，别打扰用户）。"""
        if not self.llm.ready:
            raise UpstreamError("AI 判断需要大模型：请在 .env 里设置 LLM_ENABLED=true 与 LLM_API_KEY")

        aliases = ", ".join(record.aliases or []) or "（无）"
        messages = [
            {"role": "system", "content": DETAIL_PROMPT_SYSTEM},
            {
                "role": "user",
                "content": (
                    f"名称：{record.name}\n"
                    f"分类：{category_label(record.category)}\n"
                    f"现有规格：{record.spec or '（空）'}\n"
                    f"现有别名：{aliases}\n"
                    f"存放位置：{record.location or '（未填写）'}"
                ),
            },
        ]
        result = await self.llm.chat(messages, temperature=0.0, max_tokens=200)
        data = _loads_json(result["content"])
        return DetailPrompts(
            ask_spec=bool(data.get("ask_spec")),
            ask_alias=bool(data.get("ask_alias")),
            reason=str(data.get("reason") or "")[:120],
        )

    async def parse_stock(self, text: str) -> AIStockParse:
        """把口语化的库存变更描述解析成结构化指令（AI 优先路径）。"""
        if not self.llm.ready:
            raise UpstreamError("AI 解析需要大模型：请在 .env 里设置 LLM_ENABLED=true 与 LLM_API_KEY")
        if not text.strip():
            raise ValidationFailed("待解析的内容为空")

        messages = [
            {"role": "system", "content": STOCK_PARSE_SYSTEM},
            {
                "role": "user",
                "content": f"【已有分类】{', '.join(self.known_categories())}\n【用户消息】\n{text[:2000]}",
            },
        ]
        result = await self.llm.chat(messages, temperature=0.0, max_tokens=600)
        data = _loads_json(result["content"])

        action = str(data.get("action", "unknown")).strip().lower()
        if action not in {"in", "out", "set", "unknown"}:
            action = "unknown"
        try:
            quantity = max(0.0, float(data.get("quantity") or 0))
        except (TypeError, ValueError):
            quantity = 0.0
        try:
            confidence = max(0.0, min(1.0, float(data.get("confidence") or 0)))
        except (TypeError, ValueError):
            confidence = 0.0

        return AIStockParse(
            action=action,  # type: ignore[arg-type]
            name=str(data.get("name", "")).strip()[:200],
            quantity=quantity,
            location=str(data.get("location", "")).strip()[:200],
            spec=str(data.get("spec", "")).strip()[:200],
            category=normalize_category_code(data.get("category")),
            aliases=_clean_alias_list(data.get("aliases")),
            confidence=confidence,
            reason=str(data.get("reason", "")).strip()[:120],
            model=result.get("model", ""),
        )

    # ------------------------------------------------------------------ 整理
    async def propose_tidy(self, item_limit: int = 120) -> TidyPlan:
        """让大模型给出一份「归纳整理」方案（不落库）。"""
        if not self.llm.ready:
            raise UpstreamError("AI 整理需要大模型：请在 .env 里设置 LLM_ENABLED=true 与 LLM_API_KEY")
        records = self.analysis.in_stock_records()
        if not records:
            raise ValidationFailed("库存是空的，没有可整理的内容")

        digest = "\n".join(
            f"- id={record.id} | 名称={record.name} | 规格={record.spec or '（空）'}"
            f" | 分类={record.category} | 数量={fmt_qty(record.quantity)}"
            f" | 位置={record.location or '（空）'}"
            f" | 别名={','.join(record.aliases) if record.aliases else '（无）'}"
            for record in records[:item_limit]
        )
        messages = [
            {"role": "system", "content": TIDY_SYSTEM},
            {
                "role": "user",
                "content": f"【已有分类】{', '.join(self.known_categories())}\n【库存条目】\n{digest}",
            },
        ]
        result = await self.llm.chat(messages, temperature=0.0, max_tokens=2500)
        data = _loads_json(result["content"])

        by_id = {record.id: record for record in records}
        changes: list[TidyChange] = []
        for raw in data.get("changes") or []:
            if not isinstance(raw, dict):
                continue
            try:
                item_id = int(raw.get("item_id"))
            except (TypeError, ValueError):
                continue
            record = by_id.get(item_id)
            if record is None:
                continue

            after: dict[str, Any] = {}
            new_name = str(raw.get("name") or "").strip()
            if new_name and new_name != record.name:
                after["name"] = new_name[:200]
            new_spec = str(raw.get("spec") or "").strip()
            if new_spec and new_spec != record.spec:
                after["spec"] = new_spec[:200]
            if raw.get("category"):
                code = normalize_category_code(raw["category"])
                if code != record.category:
                    after["category"] = code
            incoming = _clean_alias_list(raw.get("aliases"))
            if incoming:
                merged = list(dict.fromkeys([*record.aliases, *incoming]))
                if len(merged) > len(record.aliases):
                    after["aliases"] = merged
            if not after:
                continue
            changes.append(
                TidyChange(
                    item_id=item_id,
                    name=record.name,
                    before={key: getattr(record, key, None) for key in after},
                    after=after,
                    reason=str(raw.get("reason", "")).strip()[:120],
                )
            )

        duplicates: list[list[int]] = []
        for group in data.get("duplicates") or []:
            if not isinstance(group, (list, tuple)):
                continue
            ids: list[int] = []
            for value in group:
                try:
                    candidate = int(value)
                except (TypeError, ValueError):
                    continue
                if candidate in by_id and candidate not in ids:
                    ids.append(candidate)
            if len(ids) >= 2:
                duplicates.append(ids)

        return TidyPlan(
            summary=str(data.get("summary", "")).strip()[:200],
            changes=changes,
            duplicates=duplicates,
            model=result.get("model", ""),
        )

    def apply_tidy(self, plan: TidyPlan, *, operator: str = "") -> int:
        """执行整理方案，返回成功修改的条目数。"""
        applied = 0
        for change in plan.changes:
            # 只传「要改的字段」，避免把 None 当成新值写进库
            allowed = {"name", "spec", "category", "aliases"}
            payload = ItemUpdate(
                **{key: value for key, value in change.after.items() if key in allowed},
                operator=operator,
            )
            try:
                self.inventory.update_item(change.item_id, payload)
                applied += 1
            except Exception:  # noqa: BLE001 - 单条失败不影响整体
                logger.exception("整理条目 #%s 失败", change.item_id)
        if applied:
            self.repo.audit(
                action="item.tidy",
                actor=operator,
                actor_kind="ai",
                target_type="inventory",
                detail={"applied": applied, "summary": plan.summary},
            )
        return applied


def _clean_alias_list(values) -> list[str]:
    if isinstance(values, str):
        values = re.split(r"[,，、/\s]+", values)
    result: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        text = str(value).strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text[:60])
    return result[:6]


def _strip_code_fence(text: str) -> str:
    """模型有时会把整篇包在 ```markdown ... ``` 里，去掉它。"""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def items_to_markdown(items: list[ItemOut], limit: int = 500) -> str:
    """导出为规范化 Markdown（与导入格式闭环）。"""
    groups: dict[str, list[ItemOut]] = {}
    for item in items:
        groups.setdefault(category_code(item.category), []).append(item)

    lines = ["# 库存清单", ""]
    for category in sorted(groups):
        group = groups[category]
        lines.append(f"## {category_label(category)}")
        lines.append("")
        lines.append("| 名称 | 数量 | 位置 | 规格 | 别名 | 备注 |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for item in group[:limit]:
            aliases = ", ".join(item.aliases)
            lines.append(
                f"| {item.name} | {fmt_qty(item.quantity)} | {item.location} | "
                f"{item.spec} | {aliases} | {item.note} |"
            )
        lines.append("")
    return "\n".join(lines)
