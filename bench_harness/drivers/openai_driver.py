"""OpenAI / ChatCompletions driver (covers most OpenAI-compatible models).

Based on ``benchmark_v3/references/api_clients/openai_tool_call.py``:
standard ``tools`` declarations, ``reasoning_content`` extraction,
parallel tool calls and ``role="tool"`` result messages.
"""

from __future__ import annotations

import json
import os
from typing import Any

from benchmark_v3.bench_harness.drivers.base import (
    DEFAULT_HEADERS,
    BaseDriver,
    DriverResponse,
    PermanentDriverError,
    TransientDriverError,
)


class OpenAIDriver(BaseDriver):
    """ChatCompletions driver for OpenAI, DeepSeek, Qwen, Kimi, GLM, ..."""

    def __init__(
        self,
        model_id: str,
        api_key: str | None = None,
        base_url: str | None = None,
        effort: str | None = None,
        temperature: float = 0.0,
        client: Any | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(model_id, api_key=api_key, base_url=base_url, effort=effort, **kwargs)
        self.temperature = temperature
        self._client = client

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import httpx
            from openai import OpenAI
        except ImportError as exc:
            raise PermanentDriverError(f"openai SDK not installed: {exc}") from exc

        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or os.environ.get("https_proxy") or os.environ.get("http_proxy")
        http_client = httpx.Client(
            headers=dict(DEFAULT_HEADERS),
            proxy=proxy,
            timeout=httpx.Timeout(120.0, connect=30.0),
        )
        self._client = OpenAI(
            api_key=self.api_key or os.environ.get("OPENAI_API_KEY", "mock-key"),
            base_url=self.base_url or os.environ.get("OPENAI_BASE_URL"),
            http_client=http_client,
            default_headers=dict(DEFAULT_HEADERS),
            max_retries=0,  # retries managed by the harness
        )
        return self._client

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> DriverResponse:
        def _call() -> DriverResponse:
            client = self._get_client()
            create_kwargs: dict[str, Any] = {
                "model": self.model_id,
                "messages": messages,
                "temperature": kwargs.get("temperature", self.temperature),
            }
            eff = kwargs.get("reasoning_effort", kwargs.get("effort", self.effort))
            if eff and eff not in ("none", "off", "disabled"):
                openai_effort = "high" if eff in ("xhigh", "max") else eff
                create_kwargs["reasoning_effort"] = openai_effort
                create_kwargs.pop("temperature", None)
            if tools:
                create_kwargs["tools"] = tools
                create_kwargs["tool_choice"] = kwargs.get("tool_choice", "auto")
            if "max_completion_tokens" in kwargs:
                create_kwargs["max_completion_tokens"] = kwargs["max_completion_tokens"]
            try:
                response = client.chat.completions.create(**create_kwargs)
            except Exception as exc:
                raise self._map_error(exc) from exc
            result = self.parse_response(response)
            self.account_usage(result.token_usage)
            if result.truncated and not kwargs.get("_trunc_retry"):
                # Output budget hit mid-turn (often inside tool-call JSON):
                # retry once with a doubled budget instead of failing silently.
                # (This attempt's tokens are already accounted above.)
                retry_kwargs = dict(kwargs, _trunc_retry=True)
                retry_kwargs["max_completion_tokens"] = int(
                    kwargs.get("max_completion_tokens", 8192)
                ) * 2
                return self.chat(messages, tools, **retry_kwargs)
            return result

        return self.run_with_retry(_call)

    def parse_response(self, response: Any) -> DriverResponse:
        choice = response.choices[0]
        message = choice.message
        thought = getattr(message, "reasoning_content", "") or ""
        raw_calls: list[dict[str, Any]] = []
        for tool_call in getattr(message, "tool_calls", None) or []:
            function = tool_call.function
            raw_args = function.arguments
            try:
                arguments = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args or {})
            except (json.JSONDecodeError, TypeError, ValueError):
                arguments = {"_raw": str(raw_args)}
            raw_calls.append({"id": tool_call.id, "name": function.name, "arguments": arguments})
        usage = getattr(response, "usage", None)
        token_usage = {
            "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0 if usage else 0,
            "completion_tokens": getattr(usage, "completion_tokens", 0) or 0 if usage else 0,
            "total_tokens": getattr(usage, "total_tokens", 0) or 0 if usage else 0,
        }
        token_usage.setdefault("reasoning_tokens", 0)
        truncated = getattr(choice, "finish_reason", "") == "length"
        return DriverResponse(
            content=message.content or "",
            thought=thought,
            tool_calls=self.normalize_tool_calls(raw_calls),
            token_usage=self.extract_token_usage(token_usage),
            raw=response,
            truncated=truncated,
        )

    def _map_error(self, exc: Exception) -> Exception:
        name = type(exc).__name__
        status = getattr(exc, "status_code", None)
        retry_after = self.parse_retry_after(
            getattr(getattr(exc, "response", None), "headers", {}).get("retry-after")
            if getattr(exc, "response", None) is not None
            else None
        )
        if name in ("RateLimitError", "InternalServerError", "APIConnectionError", "APITimeoutError"):
            if name == "RateLimitError":
                return TransientDriverError(str(exc), status_code=status or 429, retry_after=retry_after)
            return TransientDriverError(str(exc), status_code=status or 500)
        if status is not None:
            return self.classify_http_error(status, str(exc))
        return TransientDriverError(f"{name}: {exc}")

    @staticmethod
    def build_tool_result_message(tool_call_id: str, tool_name: str, result_content: str) -> dict[str, Any]:
        """Build the ``role="tool"`` message returning a tool result."""
        _ = tool_name  # kept for interface symmetry; ChatCompletions keys on the call id
        return {"role": "tool", "tool_call_id": tool_call_id, "content": result_content}
