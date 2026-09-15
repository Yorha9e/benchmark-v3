"""Anthropic Claude native driver (``anthropic`` SDK).

Based on ``benchmark_v3/references/api_clients/anthropic_tool_call.py``:
``tools`` with ``input_schema``, ``tool_use`` / ``thinking`` block parsing
and ``tool_result`` replies.
"""

from __future__ import annotations

import os
from typing import Any

from benchmark_v3.bench_harness.drivers.base import (
    BaseDriver,
    DriverResponse,
    PermanentDriverError,
    TransientDriverError,
)


class AnthropicDriver(BaseDriver):
    """Claude Messages API driver with extended-thinking extraction."""

    def __init__(
        self,
        model_id: str,
        api_key: str | None = None,
        client: Any | None = None,
        max_tokens: int = 4096,
        **kwargs: Any,
    ) -> None:
        super().__init__(model_id, api_key=api_key, **kwargs)
        self.max_tokens = max_tokens
        self._client = client

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from anthropic import Anthropic
        except ImportError as exc:
            raise PermanentDriverError(f"anthropic SDK not installed: {exc}") from exc
        self._client = Anthropic(
            api_key=self.api_key or os.environ.get("ANTHROPIC_API_KEY", "mock-key"),
            max_retries=0,  # retries managed by the harness
        )
        return self._client

    @staticmethod
    def convert_tools(standard_tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
        if not standard_tools:
            return None
        converted = []
        for tool_def in standard_tools:
            fn = tool_def.get("function", tool_def)
            converted.append(
                {
                    "name": fn["name"],
                    "description": fn.get("description", ""),
                    "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
                }
            )
        return converted

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system_prompt: str = "",
        **kwargs: Any,
    ) -> DriverResponse:
        def _call() -> DriverResponse:
            client = self._get_client()
            create_kwargs: dict[str, Any] = {
                "model": self.model_id,
                "messages": messages,
                "max_tokens": kwargs.get("max_tokens", self.max_tokens),
            }
            system = kwargs.get("system", system_prompt)
            if system:
                create_kwargs["system"] = system
            converted = self.convert_tools(tools)
            if converted:
                create_kwargs["tools"] = converted
            try:
                response = client.messages.create(**create_kwargs)
            except Exception as exc:
                raise self._map_error(exc) from exc
            result = self.parse_response(response)
            self.account_usage(result.token_usage)
            return result

        return self.run_with_retry(_call)

    def parse_response(self, response: Any) -> DriverResponse:
        tool_calls: list[dict[str, Any]] = []
        text_parts: list[str] = []
        thought = ""
        for block in getattr(response, "content", None) or []:
            block_type = getattr(block, "type", "")
            if block_type == "text":
                text_parts.append(getattr(block, "text", "") or "")
            elif block_type == "tool_use":
                tool_calls.append(
                    {"id": block.id, "name": block.name, "arguments": dict(block.input or {})}
                )
            elif block_type == "thinking":
                thought = getattr(block, "thinking", "") or ""
        usage = getattr(response, "usage", None)
        if usage is not None:
            input_tokens = getattr(usage, "input_tokens", 0) or 0
            output_tokens = getattr(usage, "output_tokens", 0) or 0
            token_usage = {
                "prompt_tokens": input_tokens,
                "completion_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
            }
        else:
            token_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        token_usage.setdefault("reasoning_tokens", 0)
        return DriverResponse(
            content="\n".join(text_parts),
            thought=thought,
            tool_calls=self.normalize_tool_calls(tool_calls),
            token_usage=self.extract_token_usage(token_usage),
            raw=response,
        )

    def _map_error(self, exc: Exception) -> Exception:
        name = type(exc).__name__
        status = getattr(exc, "status_code", None)
        if name in ("RateLimitError", "InternalServerError", "APIConnectionError", "APITimeoutError"):
            return TransientDriverError(str(exc), status_code=status or (429 if "RateLimit" in name else 500))
        if status is not None:
            return self.classify_http_error(status, str(exc))
        return TransientDriverError(f"{name}: {exc}")

    @staticmethod
    def build_tool_result(tool_use_id: str, content: str, is_error: bool = False) -> dict[str, Any]:
        """Build the ``user``-role ``tool_result`` block for Claude."""
        return {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": content,
                    "is_error": is_error,
                }
            ],
        }
