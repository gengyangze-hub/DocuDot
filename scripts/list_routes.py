#!/usr/bin/env python3
"""打印全部 HTTP 路由（用于生成 docs/api.md，也可用来快速核对接口面）。

    python3 scripts/list_routes.py

说明：FastAPI 0.142 / Starlette 1.7 起 ``include_router`` 不再把子路由摊平到
``app.routes``（会包一层 ``_IncludedRouter``），因此这里改用 OpenAPI schema，
既稳定又能顺带拿到摘要与权限标签。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.main import create_app  # noqa: E402

SKIP_PATHS = {"/", "/health"}


def main() -> int:
    app = create_app()
    schema = app.openapi()

    rows: list[tuple[str, str, str]] = []
    for path, operations in schema.get("paths", {}).items():
        if path in SKIP_PATHS:
            continue
        for method, spec in operations.items():
            if method.lower() not in {"get", "post", "patch", "put", "delete"}:
                continue
            rows.append((method.upper(), path, (spec.get("summary") or "").strip()))

    rows.sort(key=lambda item: (item[1], item[0]))
    width = max(len(r[1]) for r in rows) + 2
    print(f"{'方法':<7}{'路径':<{width}}说明")
    print("-" * (7 + width + 40))
    for method, path, summary in rows:
        print(f"{method:<7}{path:<{width}}{summary}")
    print(f"\n共 {len(rows)} 个业务端点（另有 GET / 与 GET /health 两个无鉴权端点）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
