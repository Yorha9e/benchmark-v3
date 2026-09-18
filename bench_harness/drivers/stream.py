"""Assemble vendor SSE streams into the same objects non-stream parsers expect.

Harness turns still wait for a full model reply before running tools.
Streaming is only on the wire so idle HTTP read timeouts do not kill a
thinking model that has not yet emitted the first token.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Iterable, Iterator


def is_stream_unsupported(exc: BaseException) -> bool:
    """True when the vendor/gateway rejected streaming (safe to retry once)."""
    if type(exc).__name__ == "TypeError":
        return True
    status = getattr(exc, "status_code", None)
    if status not in (400, 422):
        return False
    msg = str(exc).lower()
    return any(
        tok in msg
        for tok in ("stream", "stream_options", "include_usage", "sse", "event-stream")
    )


def close_stream(stream: Any) -> None:
    closer = getattr(stream, "close", None)
    if not callable(closer):
        return
    try:
        closer()
    except Exception:
        pass


def iter_stream(stream: Any) -> Iterator[Any]:
    """Iterate an SDK stream/context-manager, always closing it."""
    if stream is None:
        return
    inner = stream
    entered = False
    try:
        if hasattr(stream, "__enter__") and hasattr(stream, "__exit__"):
            inner = stream.__enter__()
            entered = True
        if inner is None:
            return
        yield from inner
    finally:
        if entered:
            try:
                stream.__exit__(None, None, None)
            except Exception:
                pass
        else:
            close_stream(stream)


def looks_like_chat_completion(response: Any) -> bool:
    choices = getattr(response, "choices", None)
    if not choices:
        return False
    choice0 = choices[0]
    return hasattr(choice0, "message") and getattr(choice0, "delta", None) is None


def looks_like_anthropic_message(response: Any) -> bool:
    return getattr(response, "content", None) is not None and hasattr(response, "stop_reason")


def looks_like_responses_object(response: Any) -> bool:
    return getattr(response, "output", None) is not None or getattr(response, "output_text", None) not in (None, "")


def _delta_text(delta: Any, *names: str) -> str:
    for name in names:
        value = getattr(delta, name, None)
        if value is None and isinstance(delta, dict):
            value = delta.get(name)
        if isinstance(value, str) and value:
            return value
        if value is not None and not isinstance(value, (str, bytes, dict, list)):
            nested = getattr(value, "content", None) or getattr(value, "text", None)
            if isinstance(nested, str) and nested:
                return nested
    return ""


def assemble_chat_completion(stream: Any) -> Any:
    """Fold Chat Completions SSE chunks into a completion-shaped namespace.

    If ``stream`` is already a non-stream completion, it is returned as-is.
    """
    if looks_like_chat_completion(stream):
        return stream

    content_parts: list[str] = []
    thought_parts: list[str] = []
    tool_acc: dict[int, dict[str, str]] = {}
    finish_reason = ""
    usage = None
    saw_chunk = False

    for chunk in iter_stream(stream):
        saw_chunk = True
        chunk_usage = getattr(chunk, "usage", None)
        if chunk_usage is not None:
            usage = chunk_usage
        if looks_like_chat_completion(chunk):
            return chunk
        choices = getattr(chunk, "choices", None) or []
        if not choices:
            continue
        choice = choices[0]
        fr = getattr(choice, "finish_reason", None)
        if fr:
            finish_reason = str(fr)
        delta = getattr(choice, "delta", None)
        if delta is None:
            message = getattr(choice, "message", None)
            if message is not None:
                return chunk
            continue
        text = _delta_text(delta, "content")
        if text:
            content_parts.append(text)
        thought = _delta_text(delta, "reasoning_content", "reasoning")
        if thought:
            thought_parts.append(thought)
        for tc in getattr(delta, "tool_calls", None) or []:
            idx = int(getattr(tc, "index", 0) or 0)
            slot = tool_acc.setdefault(idx, {"id": "", "name": "", "arguments": ""})
            tc_id = getattr(tc, "id", None)
            if tc_id:
                slot["id"] = str(tc_id)
            fn = getattr(tc, "function", None)
            if fn is None and isinstance(tc, dict):
                fn = SimpleNamespace(**(tc.get("function") or {}))
                if not slot["id"] and tc.get("id"):
                    slot["id"] = str(tc["id"])
                if "index" in tc:
                    idx = int(tc["index"] or 0)
                    slot = tool_acc.setdefault(idx, slot)
            if fn is not None:
                name = getattr(fn, "name", None)
                if name:
                    slot["name"] = str(name)
                args = getattr(fn, "arguments", None)
                if args:
                    slot["arguments"] += str(args)

    if not saw_chunk:
        return stream

    tool_calls = []
    for idx in sorted(tool_acc):
        slot = tool_acc[idx]
        tool_calls.append(
            SimpleNamespace(
                id=slot["id"],
                function=SimpleNamespace(name=slot["name"], arguments=slot["arguments"]),
            )
        )
    message = SimpleNamespace(
        content="".join(content_parts) or None,
        reasoning_content="".join(thought_parts),
        tool_calls=tool_calls or None,
    )
    choice = SimpleNamespace(message=message, finish_reason=finish_reason or "stop")
    return SimpleNamespace(choices=[choice], usage=usage)


def finalize_anthropic_stream(stream: Any) -> Any:
    """Return a Message object from ``messages.stream`` / streamed create."""
    if looks_like_anthropic_message(stream):
        return stream
    if hasattr(stream, "get_final_message") and not hasattr(stream, "__enter__"):
        return stream.get_final_message()
    if hasattr(stream, "__enter__") and hasattr(stream, "__exit__"):
        with stream as inner:
            getter = getattr(inner, "get_final_message", None)
            if callable(getter):
                return getter()
            if looks_like_anthropic_message(inner):
                return inner
            return assemble_anthropic_events(inner)
    return assemble_anthropic_events(stream)


def assemble_anthropic_events(stream: Any) -> Any:
    """Best-effort fold of raw Anthropic SSE events into a Message-like object."""
    text_parts: list[str] = []
    thought = ""
    tool_calls: list[Any] = []
    stop_reason = ""
    usage = None
    final_message = None
    tool_acc: dict[int, dict[str, Any]] = {}

    for event in iter_stream(stream):
        if looks_like_anthropic_message(event):
            final_message = event
            continue
        etype = getattr(event, "type", "") or ""
        if etype == "message_start":
            msg = getattr(event, "message", None)
            if msg is not None and getattr(msg, "usage", None) is not None:
                usage = msg.usage
        elif etype == "content_block_delta":
            delta = getattr(event, "delta", None)
            dtype = getattr(delta, "type", "") if delta is not None else ""
            if dtype == "text_delta":
                text_parts.append(getattr(delta, "text", "") or "")
            elif dtype == "thinking_delta":
                thought += getattr(delta, "thinking", "") or ""
            elif dtype == "input_json_delta":
                idx = int(getattr(event, "index", 0) or 0)
                slot = tool_acc.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                slot["arguments"] += getattr(delta, "partial_json", "") or ""
        elif etype == "content_block_start":
            block = getattr(event, "content_block", None)
            btype = getattr(block, "type", "") if block is not None else ""
            idx = int(getattr(event, "index", 0) or 0)
            if btype == "tool_use" and block is not None:
                tool_acc[idx] = {
                    "id": getattr(block, "id", "") or "",
                    "name": getattr(block, "name", "") or "",
                    "arguments": "",
                }
            elif btype == "thinking" and block is not None:
                thought += getattr(block, "thinking", "") or ""
        elif etype == "message_delta":
            delta = getattr(event, "delta", None)
            if delta is not None and getattr(delta, "stop_reason", None):
                stop_reason = str(delta.stop_reason)
            ev_usage = getattr(event, "usage", None)
            if ev_usage is not None:
                usage = ev_usage
        elif etype == "message_stop":
            msg = getattr(event, "message", None)
            if looks_like_anthropic_message(msg):
                final_message = msg

    if final_message is not None:
        return final_message

    content: list[Any] = []
    if thought:
        content.append(SimpleNamespace(type="thinking", thinking=thought))
    if text_parts:
        content.append(SimpleNamespace(type="text", text="".join(text_parts)))
    for idx in sorted(tool_acc):
        slot = tool_acc[idx]
        raw_args = slot.get("arguments") or "{}"
        try:
            parsed = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args or {})
        except (json.JSONDecodeError, TypeError, ValueError):
            parsed = {"_raw": str(raw_args)}
        content.append(
            SimpleNamespace(type="tool_use", id=slot["id"], name=slot["name"], input=parsed)
        )
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason or "end_turn",
        usage=usage,
    )


def finalize_responses_stream(stream: Any) -> Any:
    """Return a Responses object from ``responses.stream`` / streamed create."""
    if looks_like_responses_object(stream):
        return stream
    getter = getattr(stream, "get_final_response", None)
    if callable(getter) and not hasattr(stream, "__enter__"):
        try:
            return getter()
        except Exception:
            pass
    if hasattr(stream, "__enter__") and hasattr(stream, "__exit__"):
        with stream as inner:
            inner_getter = getattr(inner, "get_final_response", None)
            if callable(inner_getter):
                try:
                    return inner_getter()
                except Exception:
                    pass
            if looks_like_responses_object(inner):
                return inner
            return assemble_responses_events(inner)
    return assemble_responses_events(stream)


def assemble_responses_events(stream: Any) -> Any:
    final = None
    text_parts: list[str] = []
    thought_parts: list[str] = []
    calls: dict[str, dict[str, str]] = {}
    usage = None
    incomplete = None

    for event in iter_stream(stream):
        if looks_like_responses_object(event):
            final = event
            continue
        etype = str(getattr(event, "type", "") or "")
        if etype == "response.completed":
            final = getattr(event, "response", None) or event
        elif etype in ("response.output_text.delta", "response.text.delta"):
            delta = getattr(event, "delta", None)
            if isinstance(delta, str) and delta:
                text_parts.append(delta)
        elif etype == "response.reasoning_text.delta":
            delta = getattr(event, "delta", None)
            if isinstance(delta, str) and delta:
                thought_parts.append(delta)
        elif etype == "response.output_item.added":
            item = getattr(event, "item", None)
            if item is not None and getattr(item, "type", "") == "function_call":
                call_id = str(getattr(item, "call_id", None) or getattr(item, "id", "") or "")
                calls[call_id] = {
                    "id": call_id,
                    "name": str(getattr(item, "name", "") or ""),
                    "arguments": str(getattr(item, "arguments", "") or ""),
                }
        elif etype == "response.function_call_arguments.delta":
            call_id = str(getattr(event, "call_id", "") or "")
            slot = calls.setdefault(call_id, {"id": call_id, "name": "", "arguments": ""})
            delta = getattr(event, "delta", None)
            if isinstance(delta, str) and delta:
                slot["arguments"] += delta
        elif etype == "response.incomplete":
            incomplete = getattr(event, "response", None)

    if final is not None:
        return final
    output: list[Any] = []
    if thought_parts:
        output.append(
            SimpleNamespace(
                type="reasoning",
                summary=[SimpleNamespace(text="".join(thought_parts))],
            )
        )
    if text_parts:
        output.append(
            SimpleNamespace(
                type="message",
                content=[SimpleNamespace(type="output_text", text="".join(text_parts))],
            )
        )
    for call_id, slot in calls.items():
        output.append(
            SimpleNamespace(
                type="function_call",
                call_id=slot["id"],
                id=slot["id"],
                name=slot["name"],
                arguments=slot["arguments"] or "{}",
            )
        )
    return SimpleNamespace(
        output=output,
        output_text="".join(text_parts),
        usage=usage,
        incomplete_details=getattr(incomplete, "incomplete_details", None) if incomplete else None,
    )


def merge_gemini_chunks(chunks: Iterable[Any]) -> Any:
    """Concatenate generate_content_stream chunks into one response-shaped object."""
    text_parts: list[Any] = []
    tool_parts: list[Any] = []
    thought = None
    truncated = False
    usage = None
    last = None
    n = 0
    for chunk in chunks:
        n += 1
        last = chunk
        usage = getattr(chunk, "usage_metadata", None) or usage
        for candidate in getattr(chunk, "candidates", None) or []:
            finish = getattr(candidate, "finish_reason", "")
            if str(getattr(finish, "name", finish)) == "MAX_TOKENS":
                truncated = True
            content = getattr(candidate, "content", None)
            for part in getattr(content, "parts", None) or []:
                if getattr(part, "function_call", None):
                    tool_parts.append(part)
                elif getattr(part, "text", None):
                    text_parts.append(part)
                if getattr(part, "thought", None):
                    thought = part.thought
    if n == 1 and last is not None:
        return last
    parts = list(text_parts) + list(tool_parts)
    if thought is not None and not any(getattr(p, "thought", None) for p in parts):
        parts.append(SimpleNamespace(text="", thought=thought, function_call=None))
    candidate = SimpleNamespace(
        finish_reason="MAX_TOKENS" if truncated else "STOP",
        content=SimpleNamespace(parts=parts),
    )
    return SimpleNamespace(candidates=[candidate], usage_metadata=usage)


def self_test() -> tuple[int, int]:
    counts = [0, 0]

    def check(name: str, cond: bool) -> None:
        counts[0 if cond else 1] += 1
        print(f"{'PASS' if cond else 'FAIL'} stream::{name}", flush=True)

    complete = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="hi"), finish_reason="stop")],
        usage=None,
    )
    check("chat_complete_passthrough", assemble_chat_completion(complete) is complete)

    chunks = [
        SimpleNamespace(
            choices=[SimpleNamespace(
                delta=SimpleNamespace(content="hel", reasoning_content="th", tool_calls=None),
                finish_reason=None,
            )],
            usage=None,
        ),
        SimpleNamespace(
            choices=[SimpleNamespace(
                delta=SimpleNamespace(
                    content="lo",
                    reasoning_content="ink",
                    tool_calls=[SimpleNamespace(
                        index=0, id="c1",
                        function=SimpleNamespace(name="run_cmd", arguments='{"x":'),
                    )],
                ),
                finish_reason=None,
            )],
            usage=None,
        ),
        SimpleNamespace(
            choices=[SimpleNamespace(
                delta=SimpleNamespace(
                    content=None,
                    tool_calls=[SimpleNamespace(
                        index=0, id=None,
                        function=SimpleNamespace(name=None, arguments="1}"),
                    )],
                ),
                finish_reason="tool_calls",
            )],
            usage=SimpleNamespace(prompt_tokens=3, completion_tokens=2, total_tokens=5),
        ),
    ]
    assembled = assemble_chat_completion(chunks)
    check("chat_stream_text", assembled.choices[0].message.content == "hello")
    check("chat_stream_thought", assembled.choices[0].message.reasoning_content == "think")
    check("chat_stream_tool_name", assembled.choices[0].message.tool_calls[0].function.name == "run_cmd")
    check("chat_stream_tool_args", assembled.choices[0].message.tool_calls[0].function.arguments == '{"x":1}')
    check("chat_stream_finish", assembled.choices[0].finish_reason == "tool_calls")
    check("chat_stream_usage", assembled.usage.total_tokens == 5)

    class _Mgr:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get_final_message(self):
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text="ok")],
                stop_reason="end_turn",
                usage=SimpleNamespace(input_tokens=1, output_tokens=1),
            )

    check("anthropic_manager", finalize_anthropic_stream(_Mgr()).content[0].text == "ok")

    events = [
        SimpleNamespace(type="response.output_text.delta", delta="ab"),
        SimpleNamespace(type="response.output_text.delta", delta="c"),
        SimpleNamespace(
            type="response.output_item.added",
            item=SimpleNamespace(type="function_call", call_id="x", id="x", name="fn", arguments=""),
        ),
        SimpleNamespace(type="response.function_call_arguments.delta", call_id="x", delta='{"a":1}'),
    ]
    resp = assemble_responses_events(events)
    check("responses_text", resp.output_text == "abc")
    check("responses_tool", resp.output[-1].name == "fn" and resp.output[-1].arguments == '{"a":1}')

    gem_chunks = [
        SimpleNamespace(
            candidates=[SimpleNamespace(
                finish_reason="STOP",
                content=SimpleNamespace(parts=[SimpleNamespace(text="A", function_call=None, thought=None)]),
            )],
            usage_metadata=None,
        ),
        SimpleNamespace(
            candidates=[SimpleNamespace(
                finish_reason="STOP",
                content=SimpleNamespace(parts=[SimpleNamespace(text="B", function_call=None, thought=None)]),
            )],
            usage_metadata=SimpleNamespace(prompt_token_count=1, candidates_token_count=2, total_token_count=3),
        ),
    ]
    merged = merge_gemini_chunks(gem_chunks)
    texts = [getattr(p, "text", "") for p in merged.candidates[0].content.parts]
    check("gemini_merge_text", texts == ["A", "B"])
    check("gemini_merge_usage", merged.usage_metadata.total_token_count == 3)

    class _E(Exception):
        def __init__(self, status_code, msg):
            super().__init__(msg)
            self.status_code = status_code

    check("unsupported_stream_options", is_stream_unsupported(_E(400, "unknown field stream_options")))
    check("timeout_not_unsupported", not is_stream_unsupported(_E(None, "ReadTimeout")))
    check("typeerror_fallback", is_stream_unsupported(TypeError("unexpected keyword stream")))
    from benchmark_v3.bench_harness.drivers.base import (
        default_read_timeout,
        httpx_limits,
        httpx_timeout,
    )
    check("read_timeout_default_unlimited", default_read_timeout() is None)
    try:
        t = httpx_timeout()
        check("httpx_read_unlimited", t.read is None and float(t.connect) <= 30.0)
        limits = httpx_limits()
        check("keepalive_not_five_seconds", float(limits.keepalive_expiry) >= 60.0)
    except Exception:
        check("httpx_read_unlimited", False)
        check("keepalive_not_five_seconds", False)
    return counts[0], counts[1]


if __name__ == "__main__":
    passed, failed = self_test()
    raise SystemExit(0 if failed == 0 else 1)
