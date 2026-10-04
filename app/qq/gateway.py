"""QQ 机器人 WebSocket 网关常驻循环。

职责：拿到网关地址 → 建立 WebSocket → 处理 HELLO → IDENTIFY / RESUME →
后台心跳 → 分发事件给 ``on_event`` 回调 → 断线指数退避重连。

设计上把「报文分类」等纯逻辑拆成模块级函数（:func:`classify_payload` 等），
网络部分集中在 :meth:`QQGateway.run_once` / :meth:`QQGateway.run_forever`，
便于单元测试时绕开真实连接。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable

import aiohttp

from .openapi import QQOpenAPI
from .protocol import (
    OP_DISPATCH,
    OP_HEARTBEAT,
    OP_HEARTBEAT_ACK,
    OP_HELLO,
    OP_INVALID_SESSION,
    OP_RECONNECT,
    build_heartbeat,
    build_identify,
    build_resume,
    heartbeat_interval,
    is_hello,
    resolve_api_base,
)

logger = logging.getLogger(__name__)

#: 单次连接的 WebSocket 心跳由我们自己发，故关掉 aiohttp 的内置心跳。
WS_HEARTBEAT = None

#: 重连退避参数。
BACKOFF_INITIAL = 1.0
BACKOFF_FACTOR = 2.0
BACKOFF_MAX = 60.0

#: 等待 HELLO 的超时（秒）：半开连接不会永远卡住主循环。
HELLO_TIMEOUT = 30.0

#: 获取网关地址 / 关闭连接的超时（秒）。
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=15.0, sock_connect=10.0)

# 报文分类结果
KIND_DISPATCH = "dispatch"
KIND_HELLO = "hello"
KIND_HEARTBEAT = "heartbeat"
KIND_HEARTBEAT_ACK = "heartbeat_ack"
KIND_RECONNECT = "reconnect"
KIND_INVALID_SESSION = "invalid_session"
KIND_UNKNOWN = "unknown"

# 单次连接结束原因
REASON_CLOSED = "closed"
REASON_RECONNECT = "reconnect"
REASON_INVALID_SESSION = "invalid_session"


# --------------------------------------------------------------------------- #
# 纯函数：报文分类与退避计算（可单测）
# --------------------------------------------------------------------------- #


def classify_payload(payload: object) -> tuple[str, dict | None]:
    """按 OP 码对网关报文分类。

    返回 ``(kind, data)``：

    * ``dispatch`` —— ``data`` 为事件体 ``d``（事件名需由调用方从原报文取 ``t``）；
    * ``hello`` —— ``data`` 为 HELLO 的 ``d``（含 ``heartbeat_interval``）；
    * ``heartbeat_ack`` / ``reconnect`` / ``invalid_session`` / ``heartbeat`` —— ``data`` 为 ``None``；
    * ``unknown`` —— 无法识别的报文（含非字典输入）。
    """
    if not isinstance(payload, dict):
        return KIND_UNKNOWN, None

    op = payload.get("op")
    data = payload.get("d")

    if op == OP_DISPATCH:
        return KIND_DISPATCH, data if isinstance(data, dict) else {}
    if op == OP_HELLO:
        return KIND_HELLO, data if isinstance(data, dict) else {}
    if op == OP_HEARTBEAT_ACK:
        return KIND_HEARTBEAT_ACK, None
    if op == OP_HEARTBEAT:
        return KIND_HEARTBEAT, None
    if op == OP_RECONNECT:
        return KIND_RECONNECT, None
    if op == OP_INVALID_SESSION:
        return KIND_INVALID_SESSION, None
    return KIND_UNKNOWN, None


def event_type_of(payload: object) -> str:
    """取出 DISPATCH 报文的事件名 ``t``（缺失时返回空串）。"""
    if not isinstance(payload, dict):
        return ""
    value = payload.get("t")
    return value if isinstance(value, str) else ""


def sequence_of(payload: object) -> int | None:
    """取出报文序号 ``s``；非整数（含 ``bool``）返回 ``None``。"""
    if not isinstance(payload, dict):
        return None
    value = payload.get("s")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def next_backoff(
    current: float,
    *,
    initial: float = BACKOFF_INITIAL,
    factor: float = BACKOFF_FACTOR,
    maximum: float = BACKOFF_MAX,
) -> float:
    """计算下一次重连退避时长：1s → 2s → 4s …… 上限 60s。"""
    if current <= 0:
        current = initial
    return min(current * factor, maximum)


# --------------------------------------------------------------------------- #
# 网关
# --------------------------------------------------------------------------- #


class QQGateway:
    """QQ WebSocket 网关客户端。

    :param settings: 配置对象（读取 ``qq_app_id`` / ``qq_app_secret`` /
        ``qq_bot_token`` / ``qq_sandbox`` / ``qq_intents``）。
    :param on_event: 事件回调 ``(event_type, data)``，异常会被吞掉并记日志，
        不会中断网关循环。
    :param session: 可选的 ``aiohttp.ClientSession``；传入时由调用方负责关闭，
        便于测试注入。
    """

    def __init__(
        self,
        settings: Any,
        on_event: Callable[[str, dict], Awaitable[None]],
        *,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._settings = settings
        self._on_event = on_event
        self._session = session
        self._own_session = session is None
        self._openapi = QQOpenAPI(settings)

        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._connected = False
        self._stop_event = asyncio.Event()
        self._heartbeat_task: asyncio.Task | None = None
        self._last_ack: float | None = None

        # 会话状态：重连时用于 RESUME
        self.session_id: str | None = None
        self.seq: int | None = None

    # ------------------------------------------------------------------ #
    # 状态
    # ------------------------------------------------------------------ #

    @property
    def connected(self) -> bool:
        """当前是否处于已连接状态。"""
        return self._connected

    @property
    def heartbeat_acked(self) -> bool:
        """自上次心跳后是否收到过 ACK（从未心跳时返回 ``False``）。"""
        return self._last_ack is not None

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    async def run_forever(self) -> None:
        """常驻主循环：断线后指数退避重连，``stop()`` 之后不再重连。

        任何异常都被捕获并记录日志，绝不向外抛出（CancelledError 除外，
        它用于响应 :meth:`stop` 的取消）。
        """
        backoff = BACKOFF_INITIAL
        while not self._stop_event.is_set():
            try:
                reason = await self.run_once()
                backoff = BACKOFF_INITIAL  # 成功连上过就重置退避
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 网关卡不能因异常退出
                logger.warning("QQ 网关连接异常：%s", exc, exc_info=True)
                reason = "error"

            if self._stop_event.is_set():
                break

            if reason == REASON_INVALID_SESSION:
                # 会话已失效：_read_loop 已清空 session_id/seq，这里重置退避，
                # 但仍走一次短延迟（1 秒）重连，避免服务端持续失效时打满 CPU。
                logger.info("QQ 网关会话失效，将重新鉴权连接")
                backoff = BACKOFF_INITIAL

            delay = backoff
            backoff = next_backoff(backoff)
            logger.info("QQ 网关将在 %.1f 秒后重连（原因：%s）", delay, reason)
            await self._sleep_or_stop(delay)

        logger.info("QQ 网关主循环已退出")

    async def stop(self) -> None:
        """优雅停止：置位停止标记、取消心跳、关闭连接与自建会话。"""
        self._stop_event.set()
        await self._cancel_heartbeat()
        await self._close_ws()
        if self._own_session:
            session, self._session = self._session, None
            if session is not None and not session.closed:
                try:
                    await session.close()
                except Exception:  # pragma: no cover
                    logger.debug("关闭网关 HTTP 会话出错", exc_info=True)
        await self._openapi.close()
        self._connected = False

    # ------------------------------------------------------------------ #
    # 单次连接
    # ------------------------------------------------------------------ #

    async def run_once(self) -> str:
        """建立一次连接并处理消息，直到连接结束。

        返回结束原因：``closed`` / ``reconnect`` / ``invalid_session``。
        """
        gateway_url = await self._fetch_gateway_url()
        session = await self._ensure_session()
        logger.info("正在连接 QQ 网关：%s", gateway_url)

        async with session.ws_connect(gateway_url, heartbeat=WS_HEARTBEAT) as ws:
            self._ws = ws
            self._connected = True
            reason = REASON_CLOSED
            try:
                reason = await self._handshake(ws)
                if reason is None:
                    reason = await self._read_loop(ws)
            finally:
                await self._cancel_heartbeat()
                self._connected = False
                self._ws = None
                logger.info("QQ 网关连接结束（原因：%s）", reason)
            return reason

    async def _handshake(self, ws: aiohttp.ClientWebSocketResponse) -> str | None:
        """等待 HELLO → 发送 IDENTIFY 或 RESUME → 启动心跳任务。

        返回 ``None`` 表示可以进入读循环；返回字符串表示这次连接已经结束。
        """
        interval_ms = heartbeat_interval(None)  # 兜底值，HELLO 到达后覆盖
        try:
            # 加超时：TCP 半开时 receive() 会永久阻塞（receive_timeout 默认 None）。
            raw = await asyncio.wait_for(ws.receive(), timeout=HELLO_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            logger.warning("等待 QQ 网关 HELLO 超时（%.0f 秒），将重连", HELLO_TIMEOUT)
            return REASON_CLOSED
        except Exception as exc:  # noqa: BLE001
            logger.warning("等待 QQ 网关 HELLO 失败：%s", exc)
            return REASON_CLOSED

        payload = self._decode_ws_message(raw)
        if payload is None:
            logger.warning("QQ 网关未返回 HELLO，将重连")
            return REASON_CLOSED

        if is_hello(payload):
            interval_ms = heartbeat_interval(payload)
        else:
            logger.warning("QQ 网关首个报文不是 HELLO：%s", payload)

        await self._send_handshake(ws)
        self._start_heartbeat(ws, interval_ms)
        return None

    async def _send_handshake(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """按当前会话状态发送 RESUME（有 session）或 IDENTIFY（无 session）。"""
        token = await self._token()
        if self.session_id and self.seq is not None:
            logger.info("使用 RESUME 恢复会话 %s（seq=%s）", self.session_id, self.seq)
            await ws.send_json(build_resume(token, self.session_id, self.seq))
        else:
            intents = int(getattr(self._settings, "qq_intents", 0) or 0)
            logger.info("使用 IDENTIFY 建立新会话（intents=%s）", intents)
            await ws.send_json(build_identify(token, intents))

    async def _read_loop(self, ws: aiohttp.ClientWebSocketResponse) -> str:
        """读取并处理网关报文，返回结束原因。"""
        async for message in ws:
            payload = self._decode_ws_message(message)
            if payload is None:
                if getattr(message, "type", None) in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.CLOSED,
                ):
                    return REASON_CLOSED
                if getattr(message, "type", None) == aiohttp.WSMsgType.ERROR:
                    logger.warning("QQ 网关 WebSocket 报错：%s", ws.exception())
                    return REASON_CLOSED
                # 心跳 ACK 等非 JSON 文本/二进制帧：忽略。
                continue

            seq = sequence_of(payload)
            if seq is not None:
                self.seq = seq

            kind, data = classify_payload(payload)

            if kind == KIND_DISPATCH:
                event_type = event_type_of(payload)
                await self._dispatch(event_type, data or {})
            elif kind == KIND_HELLO:
                # 连接中途再次下发 HELLO：按新间隔重启心跳。
                self._start_heartbeat(ws, heartbeat_interval(payload))
            elif kind == KIND_HEARTBEAT_ACK:
                self._last_ack = asyncio.get_running_loop().time()
            elif kind == KIND_HEARTBEAT:
                # 服务端要求立刻心跳一次。
                await self._safe_send(ws, build_heartbeat(self.seq))
            elif kind == KIND_RECONNECT:
                logger.info("QQ 网关要求重连（OP 7）")
                return REASON_RECONNECT
            elif kind == KIND_INVALID_SESSION:
                logger.warning("QQ 网关会话失效（OP 9），清空 session 后重新鉴权")
                self.session_id = None
                self.seq = None
                return REASON_INVALID_SESSION
            else:
                logger.debug("忽略未知网关报文：%s", payload)

        return REASON_CLOSED

    async def _dispatch(self, event_type: str, data: dict) -> None:
        """把事件交给回调；回调异常被吞掉，保证网关不中断。"""
        if event_type == "READY":
            session_id = data.get("session_id")
            if isinstance(session_id, str) and session_id:
                self.session_id = session_id
                logger.info("QQ 网关会话就绪：session_id=%s", session_id)
            return
        if event_type == "RESUMED":
            logger.info("QQ 网关会话已恢复")
            return
        if not event_type:
            logger.debug("收到无名事件，已忽略")
            return
        try:
            await self._on_event(event_type, data)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 业务回调异常不得影响网关
            logger.exception("处理 QQ 事件 %s 时出错", event_type)

    # ------------------------------------------------------------------ #
    # 心跳
    # ------------------------------------------------------------------ #

    def _start_heartbeat(self, ws: aiohttp.ClientWebSocketResponse, interval_ms: int) -> None:
        """（重新）启动后台心跳任务。"""
        self._cancel_heartbeat_sync()
        interval = max(interval_ms, 1000) / 1000.0
        self._last_ack = None
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(ws, interval))
        logger.debug("QQ 网关心跳间隔 %.1f 秒", interval)

    async def _heartbeat_loop(self, ws: aiohttp.ClientWebSocketResponse, interval: float) -> None:
        """周期性发送心跳，直到连接关闭或任务被取消。"""
        try:
            while True:
                await asyncio.sleep(interval)
                if ws.closed:
                    return
                await self._safe_send(ws, build_heartbeat(self.seq))
                logger.debug("已发送 QQ 网关心跳（seq=%s）", self.seq)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.warning("QQ 网关心跳任务异常退出", exc_info=True)

    async def _cancel_heartbeat(self) -> None:
        task, self._heartbeat_task = self._heartbeat_task, None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # pragma: no cover
            logger.debug("取消心跳任务时出错", exc_info=True)

    def _cancel_heartbeat_sync(self) -> None:
        task, self._heartbeat_task = self._heartbeat_task, None
        if task is not None and not task.done():
            task.cancel()

    # ------------------------------------------------------------------ #
    # 底层辅助
    # ------------------------------------------------------------------ #

    async def _fetch_gateway_url(self) -> str:
        """``GET {base}/gateway`` 获取 WebSocket 地址。"""
        token = await self._token()
        session = await self._ensure_session()
        url = f"{resolve_api_base(bool(getattr(self._settings, 'qq_sandbox', False)))}/gateway"
        headers = {
            "Authorization": f"QQBot {token}",
            "X-Union-Appid": str(getattr(self._settings, "qq_app_id", "") or ""),
        }
        async with session.get(url, headers=headers, timeout=HTTP_TIMEOUT) as resp:
            text = await resp.text()
            if resp.status != 200:
                raise RuntimeError(f"获取 QQ 网关地址失败：HTTP {resp.status} 响应 {text[:300]}")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"QQ 网关地址响应不是合法 JSON：{text[:300]}") from exc
        gateway_url = payload.get("url") if isinstance(payload, dict) else None
        if not gateway_url:
            raise RuntimeError(f"QQ 网关地址响应缺少 url 字段：{text[:300]}")
        return str(gateway_url)

    async def _token(self) -> str:
        """获取 AccessToken（复用 :class:`QQOpenAPI` 的缓存逻辑）。"""
        return await self._openapi.access_token()

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
            self._own_session = True
        return self._session

    @staticmethod
    async def _safe_send(ws: aiohttp.ClientWebSocketResponse, payload: dict) -> None:
        """发送报文；连接已关闭或发送失败时只记日志。"""
        if ws.closed:
            return
        try:
            await ws.send_json(payload)
        except Exception:  # noqa: BLE001 - 发送失败交由读循环感知断线
            logger.debug("发送 QQ 网关报文失败", exc_info=True)

    @staticmethod
    def _decode_ws_message(message: Any) -> dict | None:
        """把 WebSocket 帧解析成字典；非文本/非 JSON 返回 ``None``。"""
        if getattr(message, "type", None) != aiohttp.WSMsgType.TEXT:
            return None
        try:
            payload = json.loads(message.data)
        except (TypeError, ValueError):
            logger.debug("忽略无法解析的网关报文：%r", getattr(message, "data", None))
            return None
        return payload if isinstance(payload, dict) else None

    async def _sleep_or_stop(self, delay: float) -> None:
        """可被 ``stop()`` 立即打断的 sleep。"""
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass

    async def _close_ws(self) -> None:
        ws, self._ws = self._ws, None
        if ws is None or ws.closed:
            return
        try:
            await ws.close()
        except Exception:  # pragma: no cover
            logger.debug("关闭 QQ 网关 WebSocket 出错", exc_info=True)


__all__ = [
    "BACKOFF_INITIAL",
    "BACKOFF_MAX",
    "HELLO_TIMEOUT",
    "HTTP_TIMEOUT",
    "KIND_DISPATCH",
    "KIND_HELLO",
    "KIND_HEARTBEAT",
    "KIND_HEARTBEAT_ACK",
    "KIND_INVALID_SESSION",
    "KIND_RECONNECT",
    "KIND_UNKNOWN",
    "QQGateway",
    "REASON_CLOSED",
    "REASON_INVALID_SESSION",
    "REASON_RECONNECT",
    "classify_payload",
    "event_type_of",
    "next_backoff",
    "sequence_of",
]
