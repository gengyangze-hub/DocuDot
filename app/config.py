"""集中式配置：从环境变量 / .env 读取。"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录（app/ 的上一级）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"


class Settings(BaseSettings):
    """全部配置项。字段名即环境变量名（大小写不敏感）。"""

    model_config = SettingsConfigDict(
        env_file=str(DEFAULT_ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------------- 服务 ----------------
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "info"
    database_path: str = "./data/inventory.db"
    data_dir: str = "./data"
    bootstrap_api_key: str = ""

    # ---------------- 模糊匹配 ----------------
    fuzzy_threshold: float = 0.55
    fuzzy_max_candidates: int = 10

    # ---------------- 大模型 ----------------
    llm_enabled: bool = False
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"
    llm_timeout: int = 60

    # ---------------- QQ 官方机器人 ----------------
    qq_bot_enabled: bool = False
    qq_app_id: str = ""
    qq_app_secret: str = ""
    qq_bot_token: str = ""
    qq_sandbox: bool = False
    qq_intents: int = Field(default=(1 << 30) | (1 << 25))
    qq_allowed_users: str = ""
    qq_allowed_groups: str = ""
    qq_command_prefix: str = ""
    #: 入库后是否追问缺失的规格/别名（回「跳过」即不再追问）
    qq_ask_on_stock_in: bool = True
    #: 追问策略：auto=规则优先、判不了才问 AI，不值得问就直接完成；always=永远问；never=从不问
    qq_detail_prompts: str = "auto"
    #: 导入（文件 / 粘贴）时让 AI 通读整批自动归类，没有的分类自动新建
    ai_classify_on_import: bool = True
    #: 导入时让 AI 规范命名（原名会保留为别名）；与归类合并成同一次 LLM 调用
    ai_normalize_on_import: bool = True
    #: 规则认不出用户意图时，让 AI 做模糊指令匹配（如「清除全部」→ 整批出库）
    qq_ai_intent: bool = True
    #: 命令解析模式：auto=规则优先、失败时问大模型；rules=只用规则；ai=入库/出库一律问大模型
    qq_parse_mode: str = "auto"
    #: AI 解析结果的可信阈值，低于它就退回规则解析或让用户确认
    qq_ai_confidence: float = 0.5
    #: QQ 里直接发文件时的大小上限（MB）
    qq_max_file_mb: int = 20

    # ---------------- 派生路径 ----------------
    @property
    def database_file(self) -> Path:
        return self._resolve(self.database_path)

    @property
    def data_path(self) -> Path:
        p = self._resolve(self.data_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def upload_path(self) -> Path:
        p = self.data_path / "uploads"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @staticmethod
    def _resolve(raw: str) -> Path:
        p = Path(raw).expanduser()
        return p if p.is_absolute() else (PROJECT_ROOT / p).resolve()

    # ---------------- 辅助 ----------------
    @property
    def allowed_users(self) -> set[str]:
        return _split_csv(self.qq_allowed_users)

    @property
    def allowed_groups(self) -> set[str]:
        return _split_csv(self.qq_allowed_groups)

    @property
    def llm_ready(self) -> bool:
        return bool(self.llm_enabled and self.llm_api_key)

    @field_validator("qq_intents", mode="before")
    @classmethod
    def _parse_intents(cls, v: object) -> object:
        """允许写 `1<<25 | 1<<30` 这种表达式。"""
        if isinstance(v, str):
            s = v.strip()
            if not s:
                return (1 << 30) | (1 << 25)
            try:
                return int(s, 0)
            except ValueError:
                allowed = set("0123456789()|& <>\t")
                if not set(s) <= allowed:
                    raise ValueError(f"无法解析的 QQ_INTENTS: {v!r}") from None
                return int(eval(s, {"__builtins__": {}}, {}))  # noqa: S307 - 仅允许数字与位运算
        return v


def _split_csv(raw: str) -> set[str]:
    return {item.strip() for item in (raw or "").split(",") if item.strip()}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程级单例配置。"""
    return Settings()


def reset_settings_cache() -> None:
    """测试用：清掉配置缓存。"""
    get_settings.cache_clear()


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
