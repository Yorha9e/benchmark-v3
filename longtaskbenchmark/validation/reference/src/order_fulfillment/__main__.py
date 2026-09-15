"""JSON command-line interface for :mod:`src.order_fulfillment`."""

from __future__ import annotations

import json
import sys
import time
import uuid

from . import OrderError, OrderService


_COMMAND_FIELDS = {
    "stock": {"sku", "quantity"},
    "create": {"idempotency_key", "items"},
    "pay": {"order_id", "payment_id", "amount_cents"},
    "cancel": {"order_id"},
    "ship": {"order_id"},
    "get": {"order_id"},
}


def _invalid(message: str, details: dict[str, object] | None = None) -> OrderError:
    return OrderError("INVALID_INPUT", message, details)


def _parse_arguments(arguments: list[str]) -> tuple[str, str]:
    if len(arguments) != 3 or arguments[0] != "--db":
        raise _invalid(
            "usage: python -m src.order_fulfillment --db <path> <command>"
        )
    db_path, command = arguments[1], arguments[2]
    if command not in _COMMAND_FIELDS:
        raise _invalid("unknown command", {"command": command})
    return db_path, command


def _read_request(expected_fields: set[str]) -> dict[str, object]:
    text = sys.stdin.read()
    nonempty_lines = [line for line in text.splitlines() if line.strip()]
    if len(nonempty_lines) != 1:
        raise _invalid("stdin must contain exactly one non-empty JSON line")
    try:
        request = json.loads(nonempty_lines[0])
    except (json.JSONDecodeError, TypeError, ValueError):
        raise _invalid("stdin contains invalid JSON") from None
    if not isinstance(request, dict):
        raise _invalid("JSON input must be an object")
    received_fields = set(request)
    if received_fields != expected_fields:
        raise _invalid(
            "JSON object has missing or unknown fields",
            {
                "missing": sorted(expected_fields - received_fields),
                "unknown": sorted(received_fields - expected_fields),
            },
        )
    return request


def _dispatch(service: OrderService, command: str, request: dict[str, object]) -> object:
    if command == "stock":
        return service.set_stock(request["sku"], request["quantity"])
    if command == "create":
        return service.create_order(request["idempotency_key"], request["items"])
    if command == "pay":
        return service.pay_order(
            request["order_id"], request["payment_id"], request["amount_cents"]
        )
    if command == "cancel":
        return service.cancel_order(request["order_id"])
    if command == "ship":
        return service.ship_order(request["order_id"])
    return service.get_order(request["order_id"])


def _emit(value: object) -> None:
    sys.stdout.write(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    )


def main() -> None:
    try:
        db_path, command = _parse_arguments(sys.argv[1:])
        request = _read_request(_COMMAND_FIELDS[command])
        service = OrderService(
            db_path,
            clock=time.time,
            id_factory=lambda: uuid.uuid4().hex,
        )
        result = _dispatch(service, command, request)
        _emit({"ok": True, "result": result})
    except OrderError as exc:
        _emit(
            {
                "ok": False,
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "details": exc.details,
                },
            }
        )
        raise SystemExit(3 if exc.code == "STORAGE_ERROR" else 2) from None
    except Exception:
        _emit(
            {
                "ok": False,
                "error": {
                    "code": "STORAGE_ERROR",
                    "message": "unexpected internal failure",
                    "details": {},
                },
            }
        )
        raise SystemExit(3) from None


if __name__ == "__main__":
    main()
