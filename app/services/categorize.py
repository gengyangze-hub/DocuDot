"""分类推断：现在只是 :mod:`app.core.categories` 的转发层。

**不再有任何内置分类** —— 分类由 AI 判断或用户指定，
``guess_category`` 只返回 :data:`~app.core.categories.DEFAULT_CATEGORY`。
保留这个模块是为了不打断既有 import 路径。
"""

from ..core.categories import (
    DEFAULT_CATEGORY,
    LEGACY_CATEGORY_CODES,
    category_code,
    category_from_heading,
    category_label,
    detect_category_hint,
    guess_category,
    match_category,
    normalize_category_code,
)

__all__ = [
    "DEFAULT_CATEGORY",
    "LEGACY_CATEGORY_CODES",
    "category_code",
    "category_from_heading",
    "category_label",
    "detect_category_hint",
    "guess_category",
    "match_category",
    "normalize_category_code",
]
