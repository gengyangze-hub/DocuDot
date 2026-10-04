"""通用工具：时间、数值格式化、文本截断。"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIMEZONE = "Asia/Shanghai"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    """当前时间（UTC，秒级，带偏移量），用于入库。"""
    return utc_now().isoformat(timespec="seconds")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def get_zone(name: str = DEFAULT_TIMEZONE) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def fmt_time(value: str | None, tz: str = DEFAULT_TIMEZONE, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """把库里存的 UTC ISO 时间转成展示用时区。"""
    parsed = parse_iso(value)
    if not parsed:
        return value or ""
    return parsed.astimezone(get_zone(tz)).strftime(fmt)


def fmt_qty(value: float | None) -> str:
    """数量展示：整数不显示小数点。"""
    if value is None:
        return "0"
    if isinstance(value, float) and math.isclose(value, round(value), abs_tol=1e-9):
        return str(int(round(value)))
    return f"{value:g}"


def truncate(text: str, limit: int = 200, suffix: str = "…") -> str:
    text = text or ""
    return text if len(text) <= limit else text[: max(0, limit - 1)] + suffix


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
