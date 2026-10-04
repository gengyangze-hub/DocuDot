"""大模型客户端（OpenAI 兼容协议）。

默认对接 DeepSeek（``https://api.deepseek.com/v1``），
任何兼容 ``/chat/completions`` 的服务都能用（通义、Kimi、本地 vLLM 等）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Sequence

import aiohttp

from ..config import Settings
from ..errors import UpstreamError

logger = logging.getLogger(__name__)


class LLMClient:
    def __init__(self, settings: Settings, session: aiohttp.ClientSession | None = None) -> None:
        self.settings = settings
        self._session = session
        self._owns_session = session is None

    @property
    def ready(self) -> bool:
        return self.settings.llm_ready

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.settings.llm_timeout)
            self._session = aiohttp.ClientSession(timeout=timeout)
            self._owns_session = True
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed and self._owns_session:
            await self._session.close()

    async def chat(
        self,
        messages: Sequence[dict[str, str]],
        *,
        temperature: float = 0.2,
        max_tokens: int = 1500,
        retries: int = 2,
    ) -> dict[str, Any]:
        """调用 ``/chat/completions``，返回 ``{content, model, prompt_tokens, completion_tokens}``。"""
        if not self.ready:
            raise UpstreamError("大模型未配置：请在 .env 里设置 LLM_ENABLED=true 和 LLM_API_KEY")

        url = self.settings.llm_base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": self.settings.llm_model,
            "messages": list(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {self.settings.llm_api_key}",
            "Content-Type": "application/json",
        }
        session = await self._get_session()

        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                async with session.post(url, json=payload, headers=headers) as resp:
                    body = await resp.text()
                    if resp.status >= 400:
                        raise UpstreamError(f"大模型返回 {resp.status}：{body[:500]}")
                    data = json.loads(body)
                choices = data.get("choices") or []
                if not choices:
                    raise UpstreamError(f"大模型返回体没有 choices：{body[:300]}")
                usage = data.get("usage") or {}
                return {
                    "content": (choices[0].get("message") or {}).get("content", ""),
                    "model": data.get("model", self.settings.llm_model),
                    "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
                    "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
                }
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = exc
                if attempt < retries:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
            except json.JSONDecodeError as exc:
                raise UpstreamError(f"大模型返回体不是合法 JSON：{exc}") from exc
        raise UpstreamError(f"大模型请求失败：{last_error}")
