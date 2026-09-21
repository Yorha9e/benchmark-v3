"""Unified driver abstraction with exponential backoff + jitter retry."""

from __future__ import annotations

import os
import platform
import random
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

#: Official client User-Agent identifying the SubagentBenchmark Harness.
DEFAULT_USER_AGENT = os.environ.get(
    "BENCH_USER_AGENT",
    f"SubagentBenchmark-Harness/3.0.0 ({platform.system()} {platform.release()}; {platform.machine()}) Python/{platform.python_version()}",
)

#: Standard professional client headers.
DEFAULT_HEADERS: dict[str, str] = {
    "User-Agent": DEFAULT_USER_AGENT,
    "X-Benchmark-Harness": "SubagentBenchmark/3.0.0",
    "Accept": "application/json, text/event-stream, */*",
}

#: Connect vs idle-read split. A single 120s timeout used to kill long
#: reasoning turns that emit nothing until the first token. Streaming
#: resets the read timer on every SSE chunk; the read budget is the
#: allowed silence *between* chunks.
#: The read budget is deliberately generous (10 min) rather than unlimited:
#: a gateway that accepts the connection and then goes silent (hung upstream,
#: dead backend) would otherwise stall the harness forever — no error, no
#: retry, just a permanent "running" task. A ReadTimeout is raised as a
#: transient failure and goes through the normal retry/backoff path.
#: Override with BENCH_HTTP_READ_TIMEOUT (seconds, or none/0 for unlimited).
DEFAULT_CONNECT_TIMEOUT = 30.0
DEFAULT_READ_TIMEOUT = 600.0
DEFAULT_WRITE_TIMEOUT = 120.0
DEFAULT_POOL_TIMEOUT = 30.0
DEFAULT_KEEPALIVE_EXPIRY = 300.0


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def default_read_timeout() -> float | None:
    """Idle-read timeout in seconds (default 600), or None for no limit."""
    raw = os.environ.get("BENCH_HTTP_READ_TIMEOUT", str(DEFAULT_READ_TIMEOUT))
    text = str(raw).strip().lower()
    if text in ("", "none", "off", "0", "infinite", "inf"):
        return None
    try:
        value = float(text)
    except (TypeError, ValueError):
        return DEFAULT_READ_TIMEOUT
    return value if value > 0 else None


def httpx_timeout() -> Any:
    """httpx timeout: short connect, optional idle-read for streaming/thinking."""
    import httpx

    return httpx.Timeout(
        connect=DEFAULT_CONNECT_TIMEOUT,
        read=default_read_timeout(),
        write=DEFAULT_WRITE_TIMEOUT,
        pool=DEFAULT_POOL_TIMEOUT,
    )


def httpx_limits() -> Any:
    """Keep pooled sockets longer than httpx's 5s default.

    Gateways log the pool reclaim as ``client disconnected`` even after a
    successful turn; 5s expiry makes that look like a mid-request abort.
    """
    import httpx

    expiry = _env_float("BENCH_HTTP_KEEPALIVE", DEFAULT_KEEPALIVE_EXPIRY)
    if expiry <= 0:
        expiry = DEFAULT_KEEPALIVE_EXPIRY
    return httpx.Limits(
        max_connections=100,
        max_keepalive_connections=20,
        keepalive_expiry=expiry,
    )


def env_proxy() -> str | None:
    return (
        os.environ.get("HTTPS_PROXY")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("https_proxy")
        or os.environ.get("http_proxy")
    )


def build_httpx_client(proxy: str | None = None) -> Any:
    """Shared HTTP client: streaming-friendly timeouts + longer keep-alive."""
    import httpx

    kwargs: dict[str, Any] = {
        "headers": dict(DEFAULT_HEADERS),
        "timeout": httpx_timeout(),
        "limits": httpx_limits(),
        "follow_redirects": True,
    }
    if proxy:
        kwargs["proxy"] = proxy
    return httpx.Client(**kwargs)

#: HTTP statuses treated as transient (SPEC v3 Section 4).
TRANSIENT_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})

#: Deterministic client errors: fail fast, never retry.
PERMANENT_STATUS_CODES = frozenset({400, 401, 403, 404, 422})


class TransientDriverError(Exception):
    """Retryable driver failure (429/5xx, timeouts, connection errors)."""

    def __init__(self, message: str = "", *, status_code: int | None = None, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


class PermanentDriverError(Exception):
    """Non-retryable driver failure (400/401 payload or config errors)."""


@dataclass
class DriverResponse:
    """Normalized single-turn model response."""

    content: str = ""
    thought: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    token_usage: dict[str, int] = field(default_factory=dict)
    raw: Any = None
    #: True when the vendor cut generation short on its output budget
    #: (OpenAI finish_reason=length, Anthropic stop_reason=max_tokens,
    #: Gemini finishReason=MAX_TOKENS, Responses incomplete_details).
    #: Drivers retry once with a doubled budget before surfacing this.
    truncated: bool = False

    @property
    def prompt_tokens(self) -> int:
        return int(self.token_usage.get("prompt_tokens", 0))

    @property
    def completion_tokens(self) -> int:
        return int(self.token_usage.get("completion_tokens", 0))

    @property
    def total_tokens(self) -> int:
        return int(self.token_usage.get("total_tokens", self.prompt_tokens + self.completion_tokens))

    def to_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "thought": self.thought,
            "tool_calls": [dict(tc) for tc in self.tool_calls],
            "token_usage": dict(self.token_usage),
            "truncated": self.truncated,
        }


class BaseDriver(ABC):
    """Abstract model driver with adaptive retry.

    Retries transient errors (429/5xx, timeouts) with exponential backoff
    plus random jitter, strictly honoring any ``Retry-After`` hint.
    Deterministic errors (400/401) raise immediately.
    """

    transient_statuses = TRANSIENT_STATUS_CODES

    def __init__(
        self,
        model_id: str,
        api_key: str | None = None,
        base_url: str | None = None,
        effort: str | None = None,
        max_retries: int = 5,
        base_backoff: float = 1.0,
        max_backoff: float = 60.0,
        sleep_fn: Callable[[float], None] | None = None,
        **kwargs: Any,
    ) -> None:
        self.model_id = model_id
        self.api_key = api_key
        self.base_url = base_url
        self.effort = effort.lower() if isinstance(effort, str) else None
        self.max_retries = max_retries
        self.base_backoff = base_backoff
        self.max_backoff = max_backoff
        self.sleep_fn = sleep_fn if sleep_fn is not None else time.sleep
        self.retry_count = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0

    # -- interface ---------------------------------------------------------

    @abstractmethod
    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> DriverResponse:
        """Send one chat turn and return the normalized response."""

    # -- retry machinery -----------------------------------------------------

    @classmethod
    def is_transient_status(cls, status_code: int | None) -> bool:
        return status_code in cls.transient_statuses

    def compute_backoff(self, attempt: int, retry_after: float | None = None) -> float:
        backoff = min(self.max_backoff, self.base_backoff * (2**attempt) + random.uniform(0.1, 0.5))
        if retry_after is not None and retry_after > 0:
            backoff = max(backoff, min(retry_after, self.max_backoff))
        return backoff

    def run_with_retry(self, func: Callable[[], DriverResponse]) -> DriverResponse:
        """Execute ``func`` with adaptive retry; tracks ``retry_count``."""
        import sys

        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                return func()
            except PermanentDriverError as exc:
                sys.stderr.write(f"\033[31;1m[driver-error] Fatal {type(exc).__name__}: {exc}\033[0m\n")
                sys.stderr.flush()
                raise
            except TransientDriverError as exc:
                last_error = exc
                self.retry_count += 1
                delay = self.compute_backoff(attempt, exc.retry_after)
                status_str = f"HTTP {exc.status_code} " if exc.status_code else ""
                sys.stderr.write(
                    f"\033[33;1m[retry {attempt+1}/{self.max_retries}] {status_str}{type(exc).__name__}: {exc} -> sleeping {delay:.1f}s...\033[0m\n"
                )
                sys.stderr.flush()
                if attempt == self.max_retries - 1:
                    break
                self.sleep_fn(delay)
            except Exception as exc:  # network-level surprises: retry by default
                last_error = exc
                self.retry_count += 1
                delay = self.compute_backoff(attempt)
                sys.stderr.write(
                    f"\033[33;1m[retry {attempt+1}/{self.max_retries}] {type(exc).__name__}: {exc} -> sleeping {delay:.1f}s...\033[0m\n"
                )
                sys.stderr.flush()
                if attempt == self.max_retries - 1:
                    break
                self.sleep_fn(delay)
        assert last_error is not None
        sys.stderr.write(f"\033[31;1m[driver-error] Exhausted {self.max_retries} retries. Final failure: {last_error}\033[0m\n")
        sys.stderr.flush()
        raise last_error

    # -- shared helpers --------------------------------------------------------

    @staticmethod
    def extract_token_usage(usage: Any) -> dict[str, int]:
        """Normalize SDK usage payloads to prompt/completion/total (+reasoning)."""
        if usage is None:
            return {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0, "total_tokens": 0}
        if isinstance(usage, dict):
            prompt = int(usage.get("prompt_tokens", 0))
            completion = int(usage.get("completion_tokens", 0))
            reasoning = int(usage.get("reasoning_tokens", 0))
            total = int(usage.get("total_tokens", prompt + completion + reasoning))
        else:
            prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
            completion = int(getattr(usage, "completion_tokens", 0) or 0)
            reasoning = int(getattr(usage, "reasoning_tokens", 0) or 0)
            total = int(getattr(usage, "total_tokens", 0) or (prompt + completion + reasoning))
        return {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "reasoning_tokens": reasoning,
            "total_tokens": total,
        }

    def account_usage(self, token_usage: dict[str, int]) -> None:
        self.prompt_tokens += int(token_usage.get("prompt_tokens", 0))
        self.completion_tokens += int(token_usage.get("completion_tokens", 0))

    @staticmethod
    def normalize_tool_calls(raw_calls: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for call in raw_calls or []:
            normalized.append(
                {
                    "id": str(call.get("id", f"call_{uuid.uuid4().hex[:12]}")),
                    "name": str(call.get("name", "")),
                    "arguments": dict(call.get("arguments", {})),
                }
            )
        return normalized

    @staticmethod
    def new_call_id(prefix: str = "call") -> str:
        return f"{prefix}_{uuid.uuid4().hex[:12]}"

    @staticmethod
    def parse_retry_after(value: Any) -> float | None:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    @staticmethod
    def compact_http_body(message: str) -> str:
        """Keep JSON errors; collapse HTML 404 pages into one readable line."""
        text = (message or "").strip()
        if not text:
            return text
        lowered = text[:200].lower()
        if "<!doctype html" in lowered or "<html" in lowered:
            if "openrouter" in text.lower():
                return (
                    "OpenRouter returned an HTML 404 page (wrong path). "
                    "Anthropic SDK posts to {base}/v1/messages — use "
                    "https://openrouter.ai/api not https://openrouter.ai/api/v1. "
                    "OpenAI-compat driver should use https://openrouter.ai/api/v1."
                )
            return "server returned an HTML page instead of a JSON API error (wrong base URL or path)"
        if len(text) > 500:
            return text[:500] + "…"
        return text

    def classify_http_error(self, status_code: int | None, message: str = "") -> Exception:
        """Map an HTTP failure to transient vs permanent driver errors."""
        compact = self.compact_http_body(message)
        if status_code in PERMANENT_STATUS_CODES:
            return PermanentDriverError(f"HTTP {status_code}: {compact}")
        if self.is_transient_status(status_code):
            return TransientDriverError(compact or f"HTTP {status_code}", status_code=status_code)
        return PermanentDriverError(f"HTTP {status_code}: {compact}")
