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
)


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
        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or os.environ.get("https_proxy") or os.environ.get("http_proxy")
        if proxy:
            os.environ.setdefault("HTTP_PROXY", proxy)
            os.environ.setdefault("HTTPS_PROXY", proxy)
            os.environ.setdefault("http_proxy", proxy)
            os.environ.setdefault("https_proxy", proxy)

        http_opts: dict[str, Any] = {"headers": dict(DEFAULT_HEADERS)}
        effective_base_url = self.base_url or os.environ.get("GEMINI_BASE_URL")
        if effective_base_url:
            http_opts["base_url"] = effective_base_url

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
            eff = kwargs.get("thinking_effort", kwargs.get("effort", self.effort))
            if eff and eff not in ("none", "off", "disabled"):
                budget_map = {"low": 1024, "medium": 4096, "high": 8192, "xhigh": 16384, "max": 32768}
                budget = budget_map.get(eff, 4096)
                try:
                    config_args["thinking_config"] = types.ThinkingConfig(thinking_budget=budget)
                except Exception:
                    pass
            converted = self.convert_tools(tools)
            if converted:
                config_args["tools"] = converted

            sys_instruction, contents = self.convert_messages(messages)
            effective_sys = kwargs.get("system_instruction") or sys_instruction
            if effective_sys:
                config_args["system_instruction"] = effective_sys

            config = types.GenerateContentConfig(**config_args)
            try:
                response = client.models.generate_content(model=self.model_id, contents=contents, config=config)
            except Exception as exc:
                raise self._map_error(exc) from exc
            result = self.parse_response(response)
            self.account_usage(result.token_usage)
            return result

        return self.run_with_retry(_call)

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
                    fn = tc.get("function", {})
                    fn_name = fn.get("name", "")
                    fn_args = fn.get("arguments", {})
                    if isinstance(fn_args, str):
                        try:
                            fn_args = json.loads(fn_args)
                        except Exception:
                            fn_args = {}
                    parts.append(types.Part.from_function_call(name=fn_name, args=fn_args))
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
        for candidate in getattr(response, "candidates", None) or []:
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
            token_usage = {
                "prompt_tokens": getattr(usage, "prompt_token_count", 0) or 0,
                "completion_tokens": getattr(usage, "candidates_token_count", 0) or 0,
                "total_tokens": getattr(usage, "total_token_count", 0) or 0,
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
