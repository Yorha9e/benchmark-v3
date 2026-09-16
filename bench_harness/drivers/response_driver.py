"""OpenAI Responses API driver (for Response-only models).

Covers models that are only served through ``client.responses.create``
(no ``/v1/chat/completions`` endpoint), e.g. newer reasoning tiers behind
third-party gateways.

Protocol notes (openai SDK >= 1.x, ``responses.create``):
- ``input`` accepts the same role-based messages as chat history, plus
  first-class ``function_call`` / ``function_call_output`` items that keep
  the multi-turn tool-call linkage intact (stateless re-send each turn,
  mirroring :class:`OpenAIDriver` semantics so snapshot resume keeps working).
- ``tools`` use the flat Responses shape
  ``{"type": "function", "name": ..., "description": ..., "parameters": ...}``
  (no ``function`` wrapper unlike Chat Completions).
- Reasoning budget is ``reasoning={"effort": ...}``; reasoning models reject
  ``temperature``, which is dropped whenever an effort level is active.
- Token budget is ``max_output_tokens`` (``max_tokens`` from callers is
  translated automatically).
"""

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

#: Harness effort -> Responses reasoning effort mapping.
#: The Responses API only accepts low/medium/high (plus minimal on some
#: tiers); xhigh/max saturate at high, mirroring OpenAIDriver behaviour.
_EFFORT_MAP = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "high",
    "max": "high",
}


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
        self._client = OpenAI(
            api_key=self.api_key or os.environ.get("OPENAI_API_KEY", "mock-key"),
            base_url=self.base_url or os.environ.get("OPENAI_BASE_URL"),
            max_retries=0,
        )
        return self._client

    # -- protocol translation -------------------------------------------

    @staticmethod
    def convert_tools_to_response_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Flatten Chat Completions tools into the Responses flat shape."""
        converted: list[dict[str, Any]] = []
        for tool in tools or []:
            if not isinstance(tool, dict):
                continue
            inner = tool.get("function") if isinstance(tool.get("function"), dict) else None
            if inner is not None:
                converted.append(
                    {
                        "type": "function",
                        "name": inner.get("name", ""),
                        "description": inner.get("description", ""),
                        "parameters": inner.get("parameters", {"type": "object", "properties": {}}),
                    }
                )
            elif tool.get("type") == "function" and tool.get("name"):
                converted.append(
                    {
                        "type": "function",
                        "name": tool.get("name", ""),
                        "description": tool.get("description", ""),
                        "parameters": tool.get("parameters", {"type": "object", "properties": {}}),
                    }
                )
        return converted

    @staticmethod
    def convert_messages_to_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Translate harness chat history into Responses ``input`` items.

        Preserves the tool-call linkage: assistant ``tool_calls`` become
        ``function_call`` items and harness ``tool`` results become
        ``function_call_output`` items (matched by ``call_id``).
        """
        converted: list[dict[str, Any]] = []
        for message in messages or []:
            if not isinstance(message, dict):
                continue
            role = message.get("role", "user")
            if role == "tool":
                converted.append(
                    {
                        "type": "function_call_output",
                        "call_id": str(message.get("tool_call_id", "")),
                        "output": str(message.get("content", "")),
                    }
                )
                continue
            if role == "assistant":
                content = message.get("content")
                if content:
                    converted.append({"role": "assistant", "content": str(content)})
                for call in message.get("tool_calls") or []:
                    if not isinstance(call, dict):
                        continue
                    fn = call.get("function") if isinstance(call.get("function"), dict) else {}
                    name = fn.get("name", "") if fn else call.get("name", "")
                    arguments = fn.get("arguments", "") if fn else call.get("arguments", "")
                    if not isinstance(arguments, str):
                        try:
                            arguments = json.dumps(arguments, ensure_ascii=False)
                        except (TypeError, ValueError):
                            arguments = str(arguments)
                    converted.append(
                        {
                            "type": "function_call",
                            "call_id": str(call.get("id", "")),
                            "name": str(name),
                            "arguments": arguments or "{}",
                        }
                    )
                continue
            if role not in ("system", "developer", "user"):
                role = "user"
            converted.append({"role": role, "content": str(message.get("content", ""))})
        return converted

    def _reasoning_param(self, kwargs: dict[str, Any]) -> dict[str, Any] | None:
        eff = kwargs.get("reasoning_effort", kwargs.get("effort", self.effort))
        if isinstance(eff, str):
            eff = eff.lower()
        if not eff or eff in ("none", "off", "disabled", "default"):
            return None
        return {"effort": _EFFORT_MAP.get(eff, "medium")}

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
            reasoning = self._reasoning_param(kwargs)
            if reasoning is not None:
                create_kwargs["reasoning"] = reasoning
            else:
                # Non-reasoning path keeps the explicit temperature default.
                create_kwargs["temperature"] = kwargs.get("temperature", self.temperature)
            if tools:
                create_kwargs["tools"] = self.convert_tools_to_response_tools(tools)
                create_kwargs["tool_choice"] = kwargs.get("tool_choice", "auto")
            if "max_output_tokens" in kwargs:
                create_kwargs["max_output_tokens"] = kwargs["max_output_tokens"]
            elif "max_tokens" in kwargs:
                create_kwargs["max_output_tokens"] = kwargs["max_tokens"]
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
            if item_type == "message":
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
                    text = getattr(summary, "text", "") or ""
                    if text:
                        thought_parts.append(text)
        if not text_parts:
            # SDK convenience accessor fallback (some gateways omit blocks).
            fallback = getattr(response, "output_text", "") or ""
            if fallback:
                text_parts.append(str(fallback))
        usage = getattr(response, "usage", None)
        if usage is not None:
            details = getattr(usage, "output_tokens_details", None)
            reasoning_tokens = int(getattr(details, "reasoning_tokens", 0) or 0) if details is not None else 0
            token_usage = {
                "prompt_tokens": getattr(usage, "input_tokens", 0) or 0,
                "completion_tokens": getattr(usage, "output_tokens", 0) or 0,
                "total_tokens": getattr(usage, "total_tokens", 0) or 0,
                "reasoning_tokens": reasoning_tokens,
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
