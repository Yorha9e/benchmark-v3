"""Command-line interface for :mod:`delivery_spool`."""

from __future__ import annotations

import json
import sys
import time
from typing import Callable

from . import Limits, Spool, SpoolError


_COMMAND_FIELDS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "init": (frozenset(), frozenset()),
    "enqueue": (frozenset({"message_id", "payload"}), frozenset({"available_at"})),
    "claim": (frozenset({"worker_id"}), frozenset()),
    "ack": (frozenset({"message_id", "lease_token"}), frozenset()),
    "fail": (frozenset({"message_id", "lease_token", "error"}), frozenset()),
    "recover": (frozenset(), frozenset()),
    "show": (frozenset({"message_id"}), frozenset()),
    "list": (frozenset(), frozenset({"status"})),
}
_OPTIONAL_STDIN = frozenset({"init", "recover", "list"})
_EXIT_THREE_CODES = frozenset({"CORRUPT_STATE", "LOCK_TIMEOUT", "INTERNAL_ERROR"})


class _CliInputError(ValueError):
    """Raised for malformed CLI JSON input."""


def _without_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _CliInputError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise _CliInputError(f"invalid JSON constant: {value}")


def _invalid_input(message: str, details: dict[str, object] | None = None) -> SpoolError:
    return SpoolError("INVALID_INPUT", message, details)


def _parse_argv(argv: list[str]) -> tuple[str, str]:
    if len(argv) != 3 or argv[0] != "--root" or not argv[1]:
        raise _invalid_input(
            "usage: python -m src.delivery_spool --root <path> <command>"
        )
    command = argv[2]
    if command not in _COMMAND_FIELDS:
        raise _invalid_input("unknown command", {"command": command})
    return argv[1], command


def _read_request(command: str) -> dict[str, object]:
    try:
        raw = sys.stdin.buffer.read().decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise _invalid_input("could not read UTF-8 stdin") from exc

    if raw == "":
        if command in _OPTIONAL_STDIN:
            request: object = {}
        else:
            raise _invalid_input("command requires one JSON object on stdin")
    else:
        lines = raw.splitlines()
        if len(lines) != 1 or not lines[0].strip():
            raise _invalid_input("stdin must contain exactly one non-empty JSON line")
        try:
            request = json.loads(
                lines[0],
                object_pairs_hook=_without_duplicate_keys,
                parse_constant=_reject_constant,
            )
        except (TypeError, ValueError, json.JSONDecodeError, RecursionError) as exc:
            raise _invalid_input("stdin is not valid JSON") from exc

    if type(request) is not dict:
        raise _invalid_input("stdin JSON value must be an object")

    required, optional = _COMMAND_FIELDS[command]
    fields = frozenset(request)
    missing = sorted(required - fields)
    unknown = sorted(fields - required - optional)
    if missing or unknown:
        details: dict[str, object] = {}
        if missing:
            details["missing"] = missing
        if unknown:
            details["unknown"] = unknown
        raise _invalid_input("request fields do not match command", details)
    return request


def _dispatch(spool: Spool, command: str, request: dict[str, object]) -> object:
    if command == "enqueue" and "available_at" in request and request["available_at"] is None:
        raise _invalid_input(
            "available_at must be a finite number when provided",
            {"field": "available_at"},
        )
    operations: dict[str, Callable[[], object]] = {
        "init": spool.initialize,
        "enqueue": lambda: spool.enqueue(
            request["message_id"],
            request["payload"],
            request.get("available_at"),
        ),
        "claim": lambda: spool.claim(request["worker_id"]),
        "ack": lambda: spool.ack(request["message_id"], request["lease_token"]),
        "fail": lambda: spool.fail(
            request["message_id"], request["lease_token"], request["error"]
        ),
        "recover": spool.recover,
        "show": lambda: spool.get(request["message_id"]),
        "list": lambda: spool.list_messages(request.get("status")),
    }
    return operations[command]()


def _error_envelope(error: SpoolError) -> dict[str, object]:
    return {
        "ok": False,
        "error": {
            "code": error.code,
            "message": error.message,
            "details": error.details,
        },
    }


def _emit(value: object) -> None:
    encoded = (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


def main() -> None:
    exit_code = 0
    try:
        root, command = _parse_argv(sys.argv[1:])
        request = _read_request(command)
        spool = Spool(
            root,
            clock=time.time,
            limits=Limits(),
            lock_timeout=10.0,
            failpoint=None,
        )
        envelope: dict[str, object] = {
            "ok": True,
            "result": _dispatch(spool, command, request),
        }
    except SpoolError as error:
        envelope = _error_envelope(error)
        exit_code = 3 if error.code in _EXIT_THREE_CODES else 2
    except Exception:
        error = SpoolError("INTERNAL_ERROR", "internal spool error")
        envelope = _error_envelope(error)
        exit_code = 3

    try:
        _emit(envelope)
    except Exception:
        fallback = (
            '{"error":{"code":"INTERNAL_ERROR","details":{},'
            '"message":"internal spool error"},"ok":false}\n'
        ).encode("utf-8")
        try:
            sys.stdout.buffer.write(fallback)
            sys.stdout.buffer.flush()
        except Exception:
            exit_code = 3
        exit_code = 3
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
