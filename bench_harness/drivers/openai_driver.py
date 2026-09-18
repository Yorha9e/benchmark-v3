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
    build_httpx_client,
    env_proxy,
    httpx_timeout,
)
from benchmark_v3.bench_harness.drivers.effort import openai_reasoning_effort
from benchmark_v3.bench_harness.drivers.stream import (
    assemble_chat_completion,
    is_stream_unsupported,
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
            from openai import OpenAI
        except ImportError as exc:
            raise PermanentDriverError(f"openai SDK not installed: {exc}") from exc

        timeout = httpx_timeout()
        http_client = build_httpx_client(env_proxy())
        self._client = OpenAI(
            api_key=self.api_key or os.environ.get("OPENAI_API_KEY", "mock-key"),
            base_url=self.base_url or os.environ.get("OPENAI_BASE_URL"),
            http_client=http_client,
            timeout=timeout,
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
            eff = openai_reasoning_effort(
                kwargs.get("reasoning_effort", kwargs.get("effort", self.effort))
            )
            if eff is not None:
                create_kwargs["reasoning_effort"] = eff
                create_kwargs.pop("temperature", None)
            if tools:
                create_kwargs["tools"] = tools
                create_kwargs["tool_choice"] = kwargs.get("tool_choice", "auto")
            if "max_completion_tokens" in kwargs:
                create_kwargs["max_completion_tokens"] = kwargs["max_completion_tokens"]
            try:
                response = self._create_completion(client, create_kwargs)
            except Exception as exc:
                raise self._map_error(exc) from exc
            result = self.parse_response(assemble_chat_completion(response))
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

    def _create_completion(self, client: Any, create_kwargs: dict[str, Any]) -> Any:
        """Prefer SSE streaming; fall back only when the gateway rejects stream."""
        create = client.chat.completions.create
        attempts: list[dict[str, Any]] = [
            {**create_kwargs, "stream": True, "stream_options": {"include_usage": True}},
            {**create_kwargs, "stream": True},
        ]
        last_exc: Exception | None = None
        for kwargs in attempts:
            try:
                return create(**kwargs)
            except Exception as exc:
                last_exc = exc
                if not is_stream_unsupported(exc):
                    raise
        try:
            return create(**create_kwargs)
        except Exception:
            if last_exc is not None:
                raise last_exc
            raise

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


def self_test() -> tuple[int, int]:
    from types import SimpleNamespace

    counts = [0, 0]

    def check(name: str, cond: bool) -> None:
        counts[0 if cond else 1] += 1
        print(f"{'PASS' if cond else 'FAIL'} openai_driver::{name}", flush=True)

    chunks = [
        SimpleNamespace(
            choices=[SimpleNamespace(
                delta=SimpleNamespace(content="he", reasoning_content="r", tool_calls=None),
                finish_reason=None,
            )],
            usage=None,
        ),
        SimpleNamespace(
            choices=[SimpleNamespace(
                delta=SimpleNamespace(content="y", reasoning_content=None, tool_calls=None),
                finish_reason="stop",
            )],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=2, total_tokens=3),
        ),
    ]

    class _Completions:
        def create(self, **kwargs):
            assert kwargs.get("stream") is True
            return list(chunks)

    client = SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))
    driver = OpenAIDriver("mock-model", client=client)
    result = driver.chat([{"role": "user", "content": "hi"}])
    check("stream_chat_content", result.content == "hey")
    check("stream_chat_thought", result.thought == "r")
    check("stream_chat_tokens", result.token_usage.get("total_tokens") == 3)

    complete = SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content="ok", reasoning_content="", tool_calls=None),
            finish_reason="stop",
        )],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
    )

    class _NonStream:
        def create(self, **kwargs):
            return complete

    driver2 = OpenAIDriver("mock-model", client=SimpleNamespace(chat=SimpleNamespace(completions=_NonStream())))
    result2 = driver2.chat([{"role": "user", "content": "hi"}])
    check("complete_still_works", result2.content == "ok")
    return counts[0], counts[1]


if __name__ == "__main__":
    passed, failed = self_test()
    raise SystemExit(0 if failed == 0 else 1)
