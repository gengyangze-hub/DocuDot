"""领域异常：API 层统一映射为 HTTP 状态码。"""

from __future__ import annotations


class WarehouseError(Exception):
    """业务异常基类。"""

    status_code = 400
    code = "bad_request"

    def __init__(self, message: str, *, detail: object | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def to_payload(self) -> dict:
        payload: dict = {"error": self.code, "message": self.message}
        if self.detail is not None:
            payload["detail"] = self.detail
        return payload


class NotFound(WarehouseError):
    status_code = 404
    code = "not_found"


class Conflict(WarehouseError):
    status_code = 409
    code = "conflict"


class ValidationFailed(WarehouseError):
    status_code = 422
    code = "validation_failed"


class AuthError(WarehouseError):
    status_code = 401
    code = "unauthorized"


class PermissionDenied(WarehouseError):
    status_code = 403
    code = "forbidden"


class RateLimited(WarehouseError):
    status_code = 429
    code = "rate_limited"


class Ambiguous(WarehouseError):
    """模糊匹配命中多个等分候选，需要调用方消歧。"""

    status_code = 409
    code = "ambiguous"


class LocationConflict(WarehouseError):
    """入库已有物品时填了新位置，需要调用方决定「合并」还是「分开」。"""

    status_code = 409
    code = "location_conflict"


class UpstreamError(WarehouseError):
    """外部依赖（LLM / QQ 开放平台）出错。"""

    status_code = 502
    code = "upstream_error"
