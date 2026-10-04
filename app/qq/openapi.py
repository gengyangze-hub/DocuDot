"""QQ 开放平台 HTTP 接口封装：AccessToken 获取与消息下发。

只依赖标准库 + ``aiohttp``。所有网络错误与非 2xx 响应统一包装为
:class:`~app.errors.UpstreamError`，由上层决定如何降级。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import aiohttp

from ..errors import UpstreamError
from .protocol import resolve_api_base, resolve_token_url

logger = logging.getLogger(__name__)

#: AccessToken 提前刷新窗口（秒）：避免踩在过期边界上。
TOKEN_REFRESH_MARGIN = 60.0

#: 单次 HTTP 请求超时（秒）。
REQUEST_TIMEOUT = 15.0


def _extract_error(payload: object) -> str | None:
    """从响应体中提取平台错误描述；没有错误则返回 ``None``。"""
    if not isinstance(payload, dict):
        return None
    for key in ("code", "errcode"):
        code = payload.get(key)
        if isinstance(code, bool) or not isinstance(code, int):
            continue
        if code != 0:
            message = payload.get("message") or payload.get("msg") or ""
            return f"code={code} message={message}"
    error = payload.get("error")
    if error:
        description = payload.get("error_description") or payload.get("error_msg") or ""
        return f"error={error} {description}".strip()
    return None


class QQOpenAPI:
    """QQ 机器人开放接口客户端（按需懒创建 ``aiohttp.ClientSession``）。

    典型用法::

        api = QQOpenAPI(settings)
        try:
            await api.send_c2c_message(openid, "库存查询结果……", msg_id=msg_id)
        finally:
            await api.close()
    """

    def __init__(self, settings: Any) -> None:
        self._settings = settings
        self._session: aiohttp.ClientSession | None = None
        self._token: str = ""
        self._token_expire_at: float = 0.0
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # 基础
    # ------------------------------------------------------------------ #

    @property
    def base_url(self) -> str:
        """开放接口基址（受 ``settings.qq_sandbox`` 影响）。"""
        return resolve_api_base(bool(getattr(self._settings, "qq_sandbox", False)))

    @property
    def app_id(self) -> str:
        return str(getattr(self._settings, "qq_app_id", "") or "")

    async def session(self) -> aiohttp.ClientSession:
        """返回可复用的 ``ClientSession``（懒创建）。"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
            )
        return self._session

    async def close(self) -> None:
        """关闭底层连接池。可重复调用。"""
        session, self._session = self._session, None
        if session is not None and not session.closed:
            try:
                await session.close()
            except Exception:  # pragma: no cover - 关闭失败不应影响主流程
                logger.debug("关闭 QQ HTTP 会话时出错", exc_info=True)

    # ------------------------------------------------------------------ #
    # AccessToken
    # ------------------------------------------------------------------ #

    async def access_token(self) -> str:
        """获取（并缓存）AppAccessToken。

        * ``qq_app_secret`` 为空但 ``qq_bot_token`` 有值：直接使用后者，不发请求；
        * 否则 ``POST`` token 地址换取 token，按 ``expires_in`` 提前
          :data:`TOKEN_REFRESH_MARGIN` 秒刷新；
        * 任何失败都抛 :class:`~app.errors.UpstreamError`。
        """
        app_secret = str(getattr(self._settings, "qq_app_secret", "") or "")
        bot_token = str(getattr(self._settings, "qq_bot_token", "") or "")

        if not app_secret:
            if bot_token:
                # 已内置 token 的场景：不请求，直接复用。
                return bot_token
            raise UpstreamError("QQ 机器人未配置凭证：qq_app_secret 与 qq_bot_token 均为空")

        now = time.monotonic()
        if self._token and now < self._token_expire_at - TOKEN_REFRESH_MARGIN:
            return self._token

        async with self._lock:
            # 双重检查：等锁期间可能已被其它协程刷新。
            now = time.monotonic()
            if self._token and now < self._token_expire_at - TOKEN_REFRESH_MARGIN:
                return self._token

            app_id = self.app_id
            if not app_id:
                raise UpstreamError("QQ 机器人未配置 qq_app_id，无法获取 AccessToken")

            payload = await self._post_json(
                resolve_token_url(),
                {"appId": app_id, "clientSecret": app_secret},
                headers={"Content-Type": "application/json"},
                context="获取 QQ AccessToken",
            )

            token = payload.get("access_token") if isinstance(payload, dict) else None
            if not token:
                raise UpstreamError(f"QQ AccessToken 响应缺少 access_token：{payload!r}")

            try:
                expires_in = float(payload.get("expires_in") or 0)
            except (TypeError, ValueError):
                expires_in = 0.0
            if expires_in <= 0:
                expires_in = 7200.0

            self._token = str(token)
            self._token_expire_at = time.monotonic() + expires_in
            logger.info("已获取 QQ AccessToken，有效期约 %.0f 秒", expires_in)
            return self._token

    # ------------------------------------------------------------------ #
    # 消息下发
    # ------------------------------------------------------------------ #

    async def send_c2c_message(
        self,
        openid: str,
        content: str,
        *,
        msg_id: str | None = None,
        msg_seq: int = 1,
        msg_type: int = 0,
    ) -> dict:
        """发送单聊（C2C）消息。"""
        return await self._send_message(
            f"/v2/users/{openid}/messages", content, msg_id=msg_id, msg_seq=msg_seq, msg_type=msg_type
        )

    async def send_group_message(
        self,
        group_openid: str,
        content: str,
        *,
        msg_id: str | None = None,
        msg_seq: int = 1,
        msg_type: int = 0,
    ) -> dict:
        """发送群聊消息。"""
        return await self._send_message(
            f"/v2/groups/{group_openid}/messages",
            content,
            msg_id=msg_id,
            msg_seq=msg_seq,
            msg_type=msg_type,
        )

    async def _send_message(
        self,
        path: str,
        content: str,
        *,
        msg_id: str | None,
        msg_seq: int,
        msg_type: int = 0,
    ) -> dict:
        body: dict[str, Any] = {
            "content": content,
            "msg_type": int(msg_type),
            "msg_seq": int(msg_seq),
        }
        if msg_id:
            body["msg_id"] = msg_id
        return await self._post_json(
            f"{self.base_url}{path}",
            body,
            headers=await self._auth_headers(),
            context=f"发送 QQ 消息 {path}",
        )

    # ------------------------------------------------------------------ #
    # 内部请求
    # ------------------------------------------------------------------ #

    async def _auth_headers(self) -> dict[str, str]:
        """构造带鉴权的请求头。"""
        token = await self.access_token()
        return {
            "Authorization": f"QQBot {token}",
            "X-Union-Appid": self.app_id,
            "Content-Type": "application/json",
        }

    async def _post_json(
        self,
        url: str,
        body: dict,
        *,
        headers: dict[str, str],
        context: str,
    ) -> dict:
        """发送 JSON POST 请求并返回解析后的响应体。

        非 2xx、响应体含平台错误码、JSON 解析失败、网络异常——全部抛
        :class:`~app.errors.UpstreamError`，且错误信息里带上响应内容。
        """
        session = await self.session()
        try:
            async with session.post(url, json=body, headers=headers) as resp:
                text = await resp.text()
                status = resp.status
        except asyncio.TimeoutError as exc:
            raise UpstreamError(f"{context} 超时（{REQUEST_TIMEOUT:.0f} 秒）") from exc
        except aiohttp.ClientError as exc:
            raise UpstreamError(f"{context} 网络错误：{exc}") from exc

        payload = self._parse_body(text, context, status)

        if not 200 <= status < 300:
            raise UpstreamError(f"{context} 失败：HTTP {status} 响应 {text[:500]}")

        error = _extract_error(payload)
        if error:
            raise UpstreamError(f"{context} 失败：{error}（响应 {text[:500]}）")

        return payload if isinstance(payload, dict) else {"data": payload}

    @staticmethod
    def _parse_body(text: str, context: str, status: int) -> object:
        """解析响应体：空响应体返回空字典，非法 JSON 抛 ``UpstreamError``。"""
        if not text or not text.strip():
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise UpstreamError(
                f"{context} 返回了非 JSON 响应：HTTP {status} 内容 {text[:500]}"
            ) from exc


__all__ = ["QQOpenAPI", "REQUEST_TIMEOUT", "TOKEN_REFRESH_MARGIN"]
