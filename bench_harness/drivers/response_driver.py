"""OpenAI Responses / structured-output driver for long-form tasks."""

from __future__ import annotations

import json
import os
from typing import Any

from benchmark_v3.bench_harness.drivers.base import (
    BaseDriver,
    DriverResponse,
    PermanentDriverError,
    TransientDriverError,
)


class ResponseDriver(BaseDriver):
    """Driver for the OpenAI Responses API (``client.responses.create``).

    Accepts the same ``messages``/``tools`` shapes as :class:`OpenAIDriver`
    and translates them to the Responses ``input``/``tools`` protocol.
    """

    def __init__(
        self,
        model_id: str,
        api_key: str | None = None,
        base_url: str | None = None,
        client: Any | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(model_id, api_key=api_key, base_url=base_url, **kwargs)
        self._client = client

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise PermanentDriverError(f"openai SDK not installed: {exc}") from exc
        self._client = OpenAI(
            api_key=self.api_key or os.environ.get("OPENAI_API_KEY", "mock-key"),
            base_url=self.base_url or os.environ.get("OPENAI_BASE_URL"),
            max_retries=0,
        )
        return self._client

    @staticmethod
    def convert_messages_to_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        converted: list[dict[str, Any]] = []
        for message in messages:
            role = message.get("role", "user")
            if role == "tool":
                converted.append(
                    {
                        "role": "user",
                        "content": f"[tool_result {message.get('tool_call_id', '')}] {message.get('content', '')}",
                    }
                )
            else:
                converted.append({"role": role, "content": message.get("content", "")})
        return converted

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> DriverResponse:
        def _call() -> DriverResponse:
            client = self._get_client()
            if not hasattr(client, "responses"):
                raise PermanentDriverError("openai SDK has no responses API; upgrade the openai package")
            create_kwargs: dict[str, Any] = {
                "model": self.model_id,
                "input": self.convert_messages_to_input(messages),
            }
            if tools:
                create_kwargs["tools"] = tools
            create_kwargs.update(kwargs)
            try:
                response = client.responses.create(**create_kwargs)
            except Exception as exc:
                raise self._map_error(exc) from exc
            result = self.parse_response(response)
            self.account_usage(result.token_usage)
            return result

        return self.run_with_retry(_call)

    def parse_response(self, response: Any) -> DriverResponse:
        text_parts: list[str] = []
        thought_parts: list[str] = []
        raw_calls: list[dict[str, Any]] = []
        for item in getattr(response, "output", None) or []:
            item_type = getattr(item, "type", "")
            if item_type in ("message", "output_text"):
                for block in getattr(item, "content", None) or []:
                    if getattr(block, "type", "") in ("output_text", "text"):
                        text_parts.append(getattr(block, "text", "") or "")
            elif item_type == "function_call":
                raw_args = getattr(item, "arguments", {})
                try:
                    arguments = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args or {})
                except (json.JSONDecodeError, TypeError, ValueError):
                    arguments = {"_raw": str(raw_args)}
                raw_calls.append(
                    {
                        "id": getattr(item, "call_id", None) or getattr(item, "id", ""),
                        "name": getattr(item, "name", ""),
                        "arguments": arguments,
                    }
                )
            elif item_type == "reasoning":
                for summary in getattr(item, "summary", None) or []:
                    thought_parts.append(getattr(summary, "text", "") or "")
        usage = getattr(response, "usage", None)
        if usage is not None:
            token_usage = {
                "prompt_tokens": getattr(usage, "input_tokens", 0) or 0,
                "completion_tokens": getattr(usage, "output_tokens", 0) or 0,
                "total_tokens": getattr(usage, "total_tokens", 0) or 0,
            }
        else:
            token_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        token_usage.setdefault("reasoning_tokens", 0)
        return DriverResponse(
            content="\n".join(p for p in text_parts if p),
            thought="\n".join(p for p in thought_parts if p),
            tool_calls=self.normalize_tool_calls(raw_calls),
            token_usage=self.extract_token_usage(token_usage),
            raw=response,
        )

    def _map_error(self, exc: Exception) -> Exception:
        status = getattr(exc, "status_code", None)
        if status is not None:
            return self.classify_http_error(status, str(exc))
        return TransientDriverError(f"{type(exc).__name__}: {exc}")
