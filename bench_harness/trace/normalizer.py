"""Trajectory format normalization for training and replay.

:class:`TrajectoryNormalizer` converts a raw :class:`AgentTrajectory` into
the three standard message formats used across the harness and its
fine-tuning exports:

* :meth:`to_chatml` — ChatML envelope: a list of ``{"role", "content"}``
  dicts.  Chain-of-thought is preserved inline with ``<think>`` tags
  (DeepSeek-R1 distillation convention) and structured tool calls ride
  along under a ``tool_calls`` key using the OpenAI shape.
* :meth:`to_openai_tools` — OpenAI ChatCompletions messages: assistant
  turns carry ``tool_calls: [{id, type: function, function: {name,
  arguments}}]`` (arguments JSON-encoded) and tool outputs use
  ``role="tool"`` with ``tool_call_id`` (cf. ``references/api_clients/
  openai_tool_call.py``).
* :meth:`to_anthropic` — ``{"system", "messages"}`` with ``tool_use`` and
  ``tool_result`` content blocks (cf. ``references/api_clients/
  anthropic_tool_call.py``).  Consecutive same-role messages are merged so
  the alternating user/assistant invariant holds.

Only the Python standard library is used.
"""

from __future__ import annotations

import json
from typing import Any

from ._compat import AgentTrajectory, ToolResultRecord

__all__ = ["TrajectoryNormalizer"]


def _tool_result_text(result: ToolResultRecord) -> str:
    """Render a tool result as message content."""
    parts = [result.stdout or ""]
    if result.stderr:
        parts.append(f"[stderr]\n{result.stderr}")
    if result.exit_code != 0:
        parts.append(
            f"[exit_code={result.exit_code} duration_ms={result.duration_ms}]"
        )
    return "\n".join(p for p in parts if p)


def _openai_tool_calls(turn: Any) -> list[dict[str, Any]]:
    return [
        {
            "id": c.call_id,
            "type": "function",
            "function": {
                "name": c.tool_name,
                "arguments": json.dumps(c.arguments, ensure_ascii=False),
            },
        }
        for c in (turn.tool_calls or [])
    ]


class TrajectoryNormalizer:
    """Stateless converter from :class:`AgentTrajectory` to chat formats."""

    # -- ChatML ------------------------------------------------------------

    @staticmethod
    def to_chatml(trajectory: AgentTrajectory) -> list[dict[str, Any]]:
        """Normalize to ChatML ``[{"role", "content", ...}]``.

        Assistant chain-of-thought is preserved inline as
        ``<think>{thought}</think>`` ahead of the text content; structured
        tool calls are attached under ``tool_calls`` (OpenAI shape) and
        tool outputs use ``role="tool"`` with ``tool_call_id``/``name``.
        """
        messages: list[dict[str, Any]] = []
        for turn in trajectory.turns or []:
            role = turn.role
            if role == "user":
                messages.append({"role": "user", "content": turn.content})
            elif role == "assistant":
                content = turn.content or ""
                if turn.thought:
                    content = f"<think>\n{turn.thought}\n</think>\n\n{content}"
                message: dict[str, Any] = {
                    "role": "assistant",
                    "content": content,
                }
                if turn.tool_calls:
                    message["tool_calls"] = _openai_tool_calls(turn)
                messages.append(message)
                for result in turn.tool_results or []:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": result.call_id,
                            "name": result.tool_name,
                            "content": _tool_result_text(result),
                        }
                    )
            elif role == "tool":
                for result in turn.tool_results or []:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": result.call_id,
                            "name": result.tool_name,
                            "content": _tool_result_text(result),
                        }
                    )
                if turn.content:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": f"turn_{turn.turn_index}",
                            "content": turn.content,
                        }
                    )
            else:  # forward-compatible: keep unknown roles verbatim
                messages.append({"role": role, "content": turn.content})
        return messages

    # -- OpenAI tools -------------------------------------------------------

    @staticmethod
    def to_openai_tools(trajectory: AgentTrajectory) -> list[dict[str, Any]]:
        """Normalize to OpenAI ChatCompletions messages with tool calls.

        Assistant ``content`` is ``None`` when the turn carries tool calls
        but no text (the API's canonical shape); chain-of-thought travels
        in ``reasoning_content`` (DeepSeek-R1 / o-series convention).
        """
        messages: list[dict[str, Any]] = []
        for turn in trajectory.turns or []:
            if turn.role == "user":
                messages.append({"role": "user", "content": turn.content})
            elif turn.role == "assistant":
                tool_calls = _openai_tool_calls(turn)
                message = {
                    "role": "assistant",
                    "content": turn.content if turn.content else None,
                }
                if turn.thought:
                    message["reasoning_content"] = turn.thought
                if tool_calls:
                    message["tool_calls"] = tool_calls
                messages.append(message)
                for result in turn.tool_results or []:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": result.call_id,
                            "content": _tool_result_text(result),
                        }
                    )
            elif turn.role == "tool":
                for result in turn.tool_results or []:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": result.call_id,
                            "content": _tool_result_text(result),
                        }
                    )
                if turn.content:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": f"turn_{turn.turn_index}",
                            "content": turn.content,
                        }
                    )
            else:
                messages.append({"role": turn.role, "content": turn.content})
        return messages

    # -- Anthropic ----------------------------------------------------------

    @staticmethod
    def to_anthropic(trajectory: AgentTrajectory) -> dict[str, Any]:
        """Normalize to ``{"system", "messages"}`` Anthropic Messages shape.

        Assistant turns become ``text`` (+ ``thinking`` + ``tool_use``)
        blocks; tool outputs become ``tool_result`` blocks inside the next
        ``user`` message.  Same-role neighbours are merged and a leading
        placeholder user turn is inserted only when the trajectory does not
        start with ``user``, keeping the alternating invariant.
        """
        raw: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []

        def flush_pending() -> None:
            if pending:
                raw.append({"role": "user", "content": list(pending)})
                pending.clear()

        for turn in trajectory.turns or []:
            if turn.role == "user":
                flush_pending()
                raw.append({"role": "user", "content": turn.content or ""})
            elif turn.role == "assistant":
                flush_pending()
                blocks: list[dict[str, Any]] = []
                if turn.thought:
                    blocks.append(
                        {"type": "thinking", "thinking": turn.thought}
                    )
                if turn.content:
                    blocks.append({"type": "text", "text": turn.content})
                for call in turn.tool_calls or []:
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": call.call_id,
                            "name": call.tool_name,
                            "input": call.arguments,
                        }
                    )
                if not blocks:
                    blocks.append({"type": "text", "text": ""})
                raw.append({"role": "assistant", "content": blocks})
                for result in turn.tool_results or []:
                    pending.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": result.call_id,
                            "content": _tool_result_text(result),
                            "is_error": result.exit_code != 0,
                        }
                    )
            elif turn.role == "tool":
                for result in turn.tool_results or []:
                    pending.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": result.call_id,
                            "content": _tool_result_text(result),
                            "is_error": result.exit_code != 0,
                        }
                    )
                if turn.content:
                    flush_pending()
                    raw.append(
                        {"role": "user", "content": turn.content or ""}
                    )
            else:
                flush_pending()
                raw.append(
                    {
                        "role": "user",
                        "content": f"[{turn.role}]\n{turn.content or ''}",
                    }
                )
        flush_pending()

        # Merge consecutive same-role messages (blocks concatenate).
        merged: list[dict[str, Any]] = []
        for message in raw:
            if merged and merged[-1]["role"] == message["role"]:
                prev_content = merged[-1]["content"]
                cur_content = message["content"]
                prev_blocks = (
                    prev_content
                    if isinstance(prev_content, list)
                    else [{"type": "text", "text": prev_content}]
                )
                cur_blocks = (
                    cur_content
                    if isinstance(cur_content, list)
                    else [{"type": "text", "text": cur_content}]
                )
                merged[-1]["content"] = prev_blocks + cur_blocks
            else:
                content = message["content"]
                merged.append(
                    {
                        "role": message["role"],
                        "content": list(content)
                        if isinstance(content, list)
                        else content,
                    }
                )
        if merged and merged[0]["role"] != "user":
            merged.insert(0, {"role": "user", "content": "(session start)"})
        return {"system": "", "messages": merged}

    # -- dispatcher ----------------------------------------------------------

    @staticmethod
    def normalize(
        trajectory: AgentTrajectory, fmt: str = "openai"
    ) -> Any:
        """Dispatch to a named format: ``chatml`` | ``openai`` | ``anthropic``."""
        key = (fmt or "").lower().replace("-", "_")
        if key == "chatml":
            return TrajectoryNormalizer.to_chatml(trajectory)
        if key in ("openai", "openai_tools", "openai_tool_calls"):
            return TrajectoryNormalizer.to_openai_tools(trajectory)
        if key == "anthropic":
            return TrajectoryNormalizer.to_anthropic(trajectory)
        raise ValueError(
            f"unknown trajectory format {fmt!r}; "
            "expected 'chatml', 'openai' or 'anthropic'"
        )
