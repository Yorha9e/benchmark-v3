"""Google Gemini native driver (``google-genai`` SDK).

Based on ``benchmark_v3/references/api_clients/google_genai_tool_call.py``:
``GenerateContentConfig`` + ``Tool(function_declarations=...)`` tool
declarations, native ``function_call`` part parsing and
``Part.from_function_response`` replies.
"""

from __future__ import annotations

import os
import time
from typing import Any

from benchmark_v3.bench_harness.drivers.base import (
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
        client: Any | None = None,
        temperature: float = 0.0,
        **kwargs: Any,
    ) -> None:
        super().__init__(model_id, api_key=api_key, **kwargs)
        self.temperature = temperature
        self._client = client

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from google import genai
        except ImportError as exc:
            raise PermanentDriverError(f"google-genai SDK not installed: {exc}") from exc
        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        if proxy:
            os.environ.setdefault("HTTP_PROXY", proxy)
            os.environ.setdefault("HTTPS_PROXY", proxy)
        self._client = genai.Client(api_key=self.api_key or os.environ.get("GEMINI_API_KEY", "mock-key"))
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
            config_args: dict[str, Any] = {"temperature": kwargs.get("temperature", self.temperature)}
            converted = self.convert_tools(tools)
            if converted:
                config_args["tools"] = converted
            config = types.GenerateContentConfig(**config_args)
            contents = self.convert_messages(messages)
            try:
                response = client.models.generate_content(model=self.model_id, contents=contents, config=config)
            except Exception as exc:
                raise self._map_error(exc) from exc
            result = self.parse_response(response)
            self.account_usage(result.token_usage)
            return result

        return self.run_with_retry(_call)

    @staticmethod
    def convert_messages(messages: list[Any]) -> list[Any]:
        """Pass through native contents; wrap plain dicts as user texts."""
        converted: list[Any] = []
        for message in messages:
            if isinstance(message, dict):
                converted.append({"role": message.get("role", "user"), "parts": [{"text": message.get("content", "")}]})
            else:
                converted.append(message)
        return converted

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
