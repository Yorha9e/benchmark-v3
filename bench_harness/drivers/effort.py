"""Map harness effort labels onto each protocol's official wire values.

Harness / TUI / CLI labels: ``none | minimal | low | medium | high | xhigh | max``
(plus omit = vendor default). Official enums as of 2026:

* OpenAI Chat Completions ``reasoning_effort`` and Responses ``reasoning.effort``:
  ``none | minimal | low | medium | high | xhigh | max`` (pass-through).
* Anthropic Messages: Claude 4.6+ uses ``thinking.type=adaptive`` plus
  ``output_config.effort`` (``low | medium | high | xhigh | max``). Older
  models still use ``thinking.type=enabled`` + ``budget_tokens``.
* Gemini 3+ ``ThinkingConfig.thinking_level``: ``minimal | low | medium | high``
  (no xhigh/max; those saturate at ``high``). Gemini 2.5 uses ``thinking_budget``.
"""

from __future__ import annotations

import re
from typing import Any

HARNESS_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")

#: Gemini 2.5 token budgets (Flash max 24576, Pro max 32768).
_GEMINI25_BUDGET = {
    "none": 0,
    "off": 0,
    "disabled": 0,
    "minimal": 512,
    "low": 1024,
    "medium": 8192,
    "high": 24576,
    "xhigh": 32768,
    "max": 32768,
}

#: Pre-4.6 Claude extended-thinking token budgets.
_ANTHROPIC_BUDGET = {
    "minimal": 1024,
    "low": 2048,
    "medium": 8192,
    "high": 16384,
    "xhigh": 32768,
    "max": 64000,
}


def normalize_effort(value: Any) -> str | None:
    """Return a lowercase effort label, or None to omit the field (vendor default)."""
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if not text or text in ("default",):
        return None
    return text


def openai_reasoning_effort(value: Any) -> str | None:
    """Chat Completions ``reasoning_effort`` / Responses ``reasoning.effort``.

    Omit when unset. ``off`` / ``disabled`` become official ``none``.
    Every other label is passed through, including ``max`` and ``xhigh``.
    """
    eff = normalize_effort(value)
    if eff is None:
        return None
    if eff in ("off", "disabled"):
        return "none"
    return eff


def responses_reasoning_param(value: Any) -> dict[str, str] | None:
    """Responses API ``reasoning={effort: ...}`` or None to omit."""
    mapped = openai_reasoning_effort(value)
    if mapped is None:
        return None
    return {"effort": mapped}


def gemini_uses_thinking_level(model_id: str) -> bool:
    """Gemini 3.x generateContent uses ``thinking_level``, not ``thinking_budget``."""
    return "gemini-3" in (model_id or "").lower()


def gemini_thinking_kwargs(model_id: str, value: Any) -> dict[str, Any] | None:
    """Arguments for ``types.ThinkingConfig``.

    Gateways differ in which ``thinking_level`` values they accept (e.g. some
    reject ``minimal`` outright with HTTP 400). Unsupported labels degrade to
    the nearest universally-accepted level instead of failing the request:
    ``minimal`` -> ``low`` (closest supported tier). If a gateway still
    rejects the value, the driver retries without the field.
    """
    eff = normalize_effort(value)
    if gemini_uses_thinking_level(model_id):
        if eff is None:
            return None
        if eff in ("none", "off", "disabled"):
            return {"thinking_level": "minimal"}
        if eff in ("xhigh", "max"):
            return {"thinking_level": "high"}
        if eff == "minimal":
            # 部分网关不支持 minimal，降级到最接近的 low
            return {"thinking_level": "low"}
        if eff in ("low", "medium", "high"):
            return {"thinking_level": eff}
        return {"thinking_level": "medium"}
    if eff is None:
        return None
    return {"thinking_budget": _GEMINI25_BUDGET.get(eff, 8192)}


def anthropic_uses_named_effort(model_id: str) -> bool:
    """Claude 4.6+ / Claude 5 use ``output_config.effort`` (adaptive thinking)."""
    text = (model_id or "").lower().replace("_", "-")
    if any(tok in text for tok in ("claude-5", "sonnet-5", "opus-5", "haiku-5", "mythos")):
        return True
    return re.search(r"(?:sonnet|opus|haiku|claude)[^a-z0-9]*4[^a-z0-9]*[6-9]", text) is not None


def anthropic_effort_wire(model_id: str, value: Any) -> tuple[str, Any] | None:
    """Return ``("adaptive", effort)`` or ``("budget", tokens)``, or None to omit thinking."""
    eff = normalize_effort(value)
    if eff is None or eff in ("none", "off", "disabled"):
        return None
    if anthropic_uses_named_effort(model_id):
        named = "low" if eff == "minimal" else eff
        return ("adaptive", named)
    return ("budget", _ANTHROPIC_BUDGET.get(eff, 8192))


def self_test() -> tuple[int, int]:
    counts = [0, 0]

    def check(name: str, cond: bool) -> None:
        counts[0 if cond else 1] += 1
        print(f"{'PASS' if cond else 'FAIL'} effort::{name}", flush=True)

    check("omit_default", openai_reasoning_effort(None) is None)
    check("omit_empty", openai_reasoning_effort("") is None)
    check("pass_max", openai_reasoning_effort("max") == "max")
    check("pass_xhigh", openai_reasoning_effort("xhigh") == "xhigh")
    check("pass_high", openai_reasoning_effort("HIGH") == "high")
    check("off_to_none", openai_reasoning_effort("off") == "none")
    check("explicit_none", openai_reasoning_effort("none") == "none")
    check("responses_max", responses_reasoning_param("max") == {"effort": "max"})
    check("responses_omit", responses_reasoning_param(None) is None)

    check("gemini3_max_is_high", gemini_thinking_kwargs("gemini-3.8-flash", "max") == {"thinking_level": "high"})
    check("gemini3_xhigh_is_high", gemini_thinking_kwargs("gemini-3.5-flash", "xhigh") == {"thinking_level": "high"})
    check("gemini3_low", gemini_thinking_kwargs("gemini-3.1-pro", "low") == {"thinking_level": "low"})
    check("gemini25_max_budget", gemini_thinking_kwargs("gemini-2.5-pro", "max") == {"thinking_budget": 32768})
    check("gemini25_high_budget", gemini_thinking_kwargs("gemini-2.5-flash", "high") == {"thinking_budget": 24576})

    check("claude37_budget", anthropic_effort_wire("claude-3-7-sonnet-20250219", "max") == ("budget", 64000))
    check("claude45_budget", anthropic_effort_wire("claude-sonnet-4-5", "high") == ("budget", 16384))
    check("claude46_named", anthropic_effort_wire("claude-sonnet-4-6", "max") == ("adaptive", "max"))
    check("claude47_named", anthropic_effort_wire("claude-opus-4-7", "xhigh") == ("adaptive", "xhigh"))
    check("claude5_named", anthropic_effort_wire("claude-sonnet-5", "high") == ("adaptive", "high"))
    check("anthropic_none_omits", anthropic_effort_wire("claude-sonnet-4-6", "none") is None)

    return counts[0], counts[1]


def main() -> int:
    passed, failed = self_test()
    print(f"effort self-test: {passed} passed, {failed} failed", flush=True)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
