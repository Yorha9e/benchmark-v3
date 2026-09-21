"""Google Gemini native driver (``google-genai`` SDK).

Based on ``benchmark_v3/references/api_clients/google_genai_tool_call.py``:
``GenerateContentConfig`` + ``Tool(function_declarations=...)`` tool
declarations, native ``function_call`` part parsing and
``Part.from_function_response`` replies.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from benchmark_v3.bench_harness.drivers.base import (
    DEFAULT_HEADERS,
    BaseDriver,
    DriverResponse,
    PermanentDriverError,
    TransientDriverError,
    build_httpx_client,
    env_proxy,
)
from benchmark_v3.bench_harness.drivers.effort import gemini_thinking_kwargs
from benchmark_v3.bench_harness.drivers.stream import is_stream_unsupported, merge_gemini_chunks


class GoogleGenAIDriver(BaseDriver):
    """Gemini driver built on the official ``google-genai`` 2.x SDK."""

    def __init__(
        self,
        model_id: str,
        api_key: str | None = None,
        base_url: str | None = None,
        effort: str | None = None,
        client: Any | None = None,
        temperature: float = 0.0,
        **kwargs: Any,
    ) -> None:
        super().__init__(model_id, api_key=api_key, base_url=base_url, effort=effort, **kwargs)
        self.temperature = temperature
        self._client = client

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from google import genai
        except ImportError as exc:
            raise PermanentDriverError(f"google-genai SDK not installed: {exc}") from exc
        proxy = env_proxy()
        if proxy:
            os.environ.setdefault("HTTP_PROXY", proxy)
            os.environ.setdefault("HTTPS_PROXY", proxy)
            os.environ.setdefault("http_proxy", proxy)
            os.environ.setdefault("https_proxy", proxy)

        http_opts: dict[str, Any] = {"headers": dict(DEFAULT_HEADERS)}
        effective_base_url = self.base_url or os.environ.get("GEMINI_BASE_URL")
        if effective_base_url:
            http_opts["base_url"] = effective_base_url
        try:
            http_opts["httpx_client"] = build_httpx_client(proxy)
        except Exception:
            pass

        self._client = genai.Client(
            api_key=self.api_key or os.environ.get("GEMINI_API_KEY", "mock-key"),
            http_options=http_opts,
        )
        return self._client

    @staticmethod
    def convert_tools(standard_tools: list[dict[str, Any]] | None) -> Any | None:
        """Convert standard JSON-schema tools to ``types.Tool`` (lazy import)."""
        if not standard_tools:
            return None
        try:
            from google.genai import types
        except ImportError as exc:
            raise PermanentDriverError(f"google-genai SDK not installed: {exc}") from exc
        declarations = []
        for tool_def in standard_tools:
            fn = tool_def.get("function", tool_def)
            declarations.append(
                types.FunctionDeclaration(
                    name=fn["name"],
                    description=fn.get("description", ""),
                    parameters=fn.get("parameters", {}),
                )
            )
        return [types.Tool(function_declarations=declarations)]

    def chat(
        self,
        messages: list[dict[str, Any]] | list[Any],
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> DriverResponse:
        def _call() -> DriverResponse:
            client = self._get_client()
            try:
                from google.genai import types
            except ImportError as exc:
                raise PermanentDriverError(f"google-genai SDK not installed: {exc}") from exc
            config_args: dict[str, Any] = {
                "temperature": kwargs.get("temperature", self.temperature),
                "automatic_function_calling": types.AutomaticFunctionCallingConfig(disable=True),
            }
            thinking_kwargs = gemini_thinking_kwargs(
                self.model_id,
                kwargs.get("thinking_effort", kwargs.get("effort", self.effort)),
            )
            if thinking_kwargs:
                try:
                    config_args["thinking_config"] = types.ThinkingConfig(**thinking_kwargs)
                except Exception:
                    if "thinking_level" in thinking_kwargs:
                        fallback = gemini_thinking_kwargs(
                            "gemini-2.5-flash",
                            kwargs.get("thinking_effort", kwargs.get("effort", self.effort)),
                        )
                        if fallback:
                            try:
                                config_args["thinking_config"] = types.ThinkingConfig(**fallback)
                            except Exception:
                                pass
            converted = self.convert_tools(tools)
            if converted:
                config_args["tools"] = converted
            if "max_output_tokens" in kwargs:
                config_args["max_output_tokens"] = kwargs["max_output_tokens"]

            sys_instruction, contents = self.convert_messages(messages)
            effective_sys = kwargs.get("system_instruction") or sys_instruction
            if effective_sys:
                config_args["system_instruction"] = effective_sys

            config = types.GenerateContentConfig(**config_args)
            try:
                response = self._generate(client, contents, config)
            except Exception as exc:
                mapped = self._map_error(exc)
                # Gateway rejected the thinking level (some gateways only
                # accept a subset). Degrade to vendor default and retry once
                # rather than failing the whole task.
                if (
                    "thinking_config" in config_args
                    and not kwargs.get("_thinking_dropped")
                    and isinstance(mapped, PermanentDriverError)
                    and "INVALID_ARGUMENT" in str(mapped).upper()
                    and "THINKING" in str(mapped).upper()
                ):
                    # Gateway rejected the thinking level (some gateways only
                    # accept a subset). Degrade to vendor default and retry once
                    # rather than failing the whole task.
                    retry_kwargs = dict(kwargs, _thinking_dropped=True)
                    return self.chat(messages, tools, **retry_kwargs)
                raise mapped from exc
            result = self.parse_response(response)
            self.account_usage(result.token_usage)
            if result.truncated and not kwargs.get("_trunc_retry"):
                # Output budget hit mid-turn: retry once with a doubled budget.
                retry_kwargs = dict(kwargs, _trunc_retry=True)
                retry_kwargs["max_output_tokens"] = int(kwargs.get("max_output_tokens", 8192)) * 2
                return self.chat(messages, tools, **retry_kwargs)
            return result

        return self.run_with_retry(_call)

    def _generate(self, client: Any, contents: Any, config: Any) -> Any:
        """Prefer generate_content_stream so thinking idle time does not drop the socket."""
        models = client.models
        stream_fn = getattr(models, "generate_content_stream", None)
        if callable(stream_fn):
            try:
                chunks = list(stream_fn(model=self.model_id, contents=contents, config=config))
                if chunks:
                    return merge_gemini_chunks(chunks)
            except Exception as exc:
                if not is_stream_unsupported(exc):
                    if type(exc).__name__ not in ("AttributeError", "TypeError"):
                        raise
        return models.generate_content(model=self.model_id, contents=contents, config=config)

    @staticmethod
    def convert_messages(messages: list[Any]) -> tuple[str, list[Any]]:
        """Convert standard messages into (system_instruction, Google GenAI types.Content list).

        Preserves:
        - System instruction extraction
        - Thought / reasoning content parts
        - Assistant function_call parts
        - Tool function_response parts
        """
        try:
            from google.genai import types
        except ImportError:
            return "", messages

        system_parts: list[str] = []
        converted: list[Any] = []
        for m in messages:
            if not isinstance(m, dict):
                converted.append(m)
                continue

            role = m.get("role", "user")
            content = m.get("content")
            tool_calls = m.get("tool_calls", [])
            reasoning = m.get("reasoning_content") or m.get("thought", "")

            if role == "system":
                if content:
                    system_parts.append(str(content))
                continue

            if role == "assistant":
                parts: list[Any] = []
                # 回传思考链
                if reasoning:
                    parts.append(types.Part(thought=True, text=str(reasoning)))
                if content:
                    parts.append(types.Part.from_text(text=str(content)))
                for tc in tool_calls:
                    fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                    fn_name = str(fn.get("name") or tc.get("name") or "")
                    fn_args = fn.get("arguments") if "arguments" in fn else tc.get("arguments", {})
                    if isinstance(fn_args, str):
                        try:
                            fn_args = json.loads(fn_args)
                        except Exception:
                            fn_args = {}
                    parts.append(types.Part.from_function_call(name=fn_name, args=fn_args if isinstance(fn_args, dict) else {}))
                converted.append(types.Content(role="model", parts=parts))

            elif role == "tool":
                func_name = str(m.get("name") or "tool")
                resp_part = types.Part.from_function_response(
                    name=func_name,
                    response={"output": str(content or "")},
                )
                # Gemini 要求并发工具调用的多个 function_response 合并在同一个 user content 中
                if converted and getattr(converted[-1], "role", None) == "user" and hasattr(converted[-1], "parts"):
                    converted[-1].parts.append(resp_part)
                else:
                    converted.append(types.Content(role="user", parts=[resp_part]))

            elif role == "user":
                if content:
                    converted.append(types.Content(role="user", parts=[types.Part.from_text(text=str(content))]))

        return "\n\n".join(system_parts), converted

    def parse_response(self, response: Any) -> DriverResponse:
        tool_calls: list[dict[str, Any]] = []
        text_parts: list[str] = []
        thought = ""
        truncated = False
        for candidate in getattr(response, "candidates", None) or []:
            finish = getattr(candidate, "finish_reason", "")
            if str(getattr(finish, "name", finish)) == "MAX_TOKENS":
                truncated = True
            content = getattr(candidate, "content", None)
            for part in getattr(content, "parts", None) or []:
                function_call = getattr(part, "function_call", None)
                if function_call:
                    tool_calls.append(
                        {
                            "id": f"call_{function_call.name}_{int(time.time() * 1000)}",
                            "name": function_call.name,
                            "arguments": dict(function_call.args) if function_call.args else {},
                        }
                    )
                elif getattr(part, "text", None):
                    text_parts.append(part.text)
                if getattr(part, "thought", None):
                    thought = part.thought
        usage = getattr(response, "usage_metadata", None)
        if usage is not None:
            # ``thoughts_token_count`` is the vendor-native thinking budget
            # actually consumed. Surface it as reasoning_tokens so effort
            # changes are visible in telemetry even when the gateway does
            # not stream thought text back.
            reasoning = int(getattr(usage, "thoughts_token_count", 0) or 0)
            if not reasoning:
                prompt_n = int(getattr(usage, "prompt_token_count", 0) or 0)
                cand_n = int(getattr(usage, "candidates_token_count", 0) or 0)
                total_n = int(getattr(usage, "total_token_count", 0) or 0)
                reasoning = max(0, total_n - prompt_n - cand_n)
            token_usage = {
                "prompt_tokens": getattr(usage, "prompt_token_count", 0) or 0,
                "completion_tokens": getattr(usage, "candidates_token_count", 0) or 0,
                "total_tokens": getattr(usage, "total_token_count", 0) or 0,
                "reasoning_tokens": reasoning,
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
            truncated=truncated,
        )

    def _map_error(self, exc: Exception) -> Exception:
        code = getattr(exc, "code", None)
        status: int | None = None
        if isinstance(code, int):
            status = code
        elif isinstance(code, str) and code.isdigit():
            status = int(code)
        if status is not None:
            retry_after = self.parse_retry_after(getattr(exc, "retry_after", None))
            if self.is_transient_status(status):
                return TransientDriverError(str(exc), status_code=status, retry_after=retry_after)
            return self.classify_http_error(status, str(exc))
        return TransientDriverError(f"{type(exc).__name__}: {exc}")

    @staticmethod
    def build_function_response(function_name: str, response_dict: dict[str, Any]) -> Any:
        """Build a ``FunctionResponse`` part replying to a Gemini tool call."""
        try:
            from google.genai import types
        except ImportError as exc:
            raise PermanentDriverError(f"google-genai SDK not installed: {exc}") from exc
        return types.Part.from_function_response(name=function_name, response=response_dict)


def self_test() -> tuple[int, int]:
    from types import SimpleNamespace

    counts = [0, 0]

    def check(name: str, cond: bool) -> None:
        counts[0 if cond else 1] += 1
        print(f"{'PASS' if cond else 'FAIL'} google_driver::{name}", flush=True)

    chunks = [
        SimpleNamespace(
            candidates=[SimpleNamespace(
                finish_reason="STOP",
                content=SimpleNamespace(parts=[SimpleNamespace(text="x", function_call=None, thought=None)]),
            )],
            usage_metadata=None,
        ),
        SimpleNamespace(
            candidates=[SimpleNamespace(
                finish_reason="STOP",
                content=SimpleNamespace(parts=[SimpleNamespace(text="y", function_call=None, thought=None)]),
            )],
            usage_metadata=SimpleNamespace(
                prompt_token_count=1, candidates_token_count=2, total_token_count=3
            ),
        ),
    ]

    class _Models:
        def generate_content_stream(self, **kwargs):
            return chunks

        def generate_content(self, **kwargs):
            raise AssertionError("non-stream generate_content should not run")

    driver = GoogleGenAIDriver("gemini-3.8-flash", client=SimpleNamespace(models=_Models()))
    merged = driver._generate(driver._client, contents="hi", config=None)
    parsed = driver.parse_response(merged)
    check("stream_merge_content", parsed.content == "x\ny")
    check("stream_merge_tokens", parsed.total_tokens == 3)
    return counts[0], counts[1]


if __name__ == "__main__":
    passed, failed = self_test()
    raise SystemExit(0 if failed == 0 else 1)
