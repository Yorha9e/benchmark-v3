"""Anthropic Claude native driver (``anthropic`` SDK).

Based on ``benchmark_v3/references/api_clients/anthropic_tool_call.py``:
``tools`` with ``input_schema``, ``tool_use`` / ``thinking`` block parsing
and ``tool_result`` replies.
"""

from __future__ import annotations

import os
from typing import Any

from benchmark_v3.bench_harness.drivers.base import (
    DEFAULT_HEADERS,
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
        base_url: str | None = None,
        effort: str | None = None,
        client: Any | None = None,
        max_tokens: int = 4096,
        **kwargs: Any,
    ) -> None:
        super().__init__(model_id, api_key=api_key, base_url=base_url, effort=effort, **kwargs)
        self.max_tokens = max_tokens
        self._client = client

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import httpx
            from anthropic import Anthropic
        except ImportError as exc:
            raise PermanentDriverError(f"anthropic SDK not installed: {exc}") from exc

        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or os.environ.get("https_proxy") or os.environ.get("http_proxy")
        http_client = httpx.Client(
            headers=dict(DEFAULT_HEADERS),
            proxy=proxy,
            timeout=httpx.Timeout(120.0, connect=30.0),
        )
        self._client = Anthropic(
            api_key=self.api_key or os.environ.get("ANTHROPIC_API_KEY", "mock-key"),
            base_url=self.base_url or os.environ.get("ANTHROPIC_BASE_URL"),
            http_client=http_client,
            default_headers=dict(DEFAULT_HEADERS),
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

    @staticmethod
    def convert_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
        """Convert standard messages into Anthropic (system_prompt, messages).

        Preserves:
        - System prompt extraction to top-level parameter
        - Claude 3.7 Thinking blocks in assistant turns
        - Tool use and tool results blocks
        """
        import json
        system_parts: list[str] = []
        anthropic_msgs: list[dict[str, Any]] = []

        for m in messages:
            role = m.get("role", "user")
            content = m.get("content")
            tool_calls = m.get("tool_calls", [])
            reasoning = m.get("reasoning_content") or m.get("thought", "")

            if role == "system":
                if content:
                    system_parts.append(str(content))
                continue

            if role == "assistant":
                content_blocks: list[dict[str, Any]] = []
                # 回传思考链 (Anthropic 严格要求保留 thinking block)
                if reasoning:
                    content_blocks.append({"type": "thinking", "thinking": str(reasoning)})
                if content:
                    content_blocks.append({"type": "text", "text": str(content)})
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    args = fn.get("arguments", {})
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except Exception:
                            args = {}
                    content_blocks.append({
                        "type": "tool_use",
                        "id": str(tc.get("id", "")),
                        "name": str(fn.get("name", "")),
                        "input": args if isinstance(args, dict) else {},
                    })
                anthropic_msgs.append({"role": "assistant", "content": content_blocks})

            elif role == "tool":
                tool_result_block = {
                    "type": "tool_result",
                    "tool_use_id": str(m.get("tool_call_id", "")),
                    "content": str(content or ""),
                }
                # Anthropic 规定上一轮并发的所有 tool_result 必须合并在同一个 user 消息中
                if anthropic_msgs and anthropic_msgs[-1]["role"] == "user" and isinstance(anthropic_msgs[-1]["content"], list):
                    anthropic_msgs[-1]["content"].append(tool_result_block)
                else:
                    anthropic_msgs.append({"role": "user", "content": [tool_result_block]})

            elif role == "user":
                anthropic_msgs.append({"role": "user", "content": str(content or "")})

        return "\n\n".join(system_parts), anthropic_msgs

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system_prompt: str = "",
        **kwargs: Any,
    ) -> DriverResponse:
        def _call() -> DriverResponse:
            client = self._get_client()
            sys_prompt, converted_msgs = self.convert_messages(messages)
            create_kwargs: dict[str, Any] = {
                "model": self.model_id,
                "messages": converted_msgs,
                "max_tokens": kwargs.get("max_tokens", self.max_tokens),
            }
            effective_system = kwargs.get("system", system_prompt) or sys_prompt
            if effective_system:
                create_kwargs["system"] = effective_system
            converted = self.convert_tools(tools)
            if converted:
                create_kwargs["tools"] = converted

            eff = kwargs.get("thinking_effort", kwargs.get("effort", self.effort))
            if eff and eff not in ("none", "off", "disabled"):
                budget_map = {"low": 2048, "medium": 8192, "high": 16384, "xhigh": 32768, "max": 64000}
                budget = budget_map.get(eff, 8192)
                create_kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}
                current_max = create_kwargs.get("max_tokens", self.max_tokens)
                create_kwargs["max_tokens"] = max(current_max, budget + 4096)
                create_kwargs.pop("temperature", None)

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
