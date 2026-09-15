"""Atomic SQLite-backed order fulfillment service."""

from __future__ import annotations

import json as _json
import math as _math
import sqlite3 as _sqlite3
import threading as _threading
from contextlib import contextmanager as _contextmanager
from typing import Iterator as _Iterator


_SCHEMA_VERSION = 1
_REQUIRED_TABLES = {"stock", "orders", "order_items"}


class OrderError(Exception):
    """A validation, domain, dependency, schema, or storage failure."""

    def __init__(
        self,
        code: str,
        message: str,
        details: dict[str, object] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.details = {} if details is None else details
        super().__init__(message)


class OrderService:
    """Provide atomic stock reservation and order state transitions."""

    def __init__(self, db_path, *, clock, id_factory):
        if not callable(clock):
            raise OrderError("INVALID_INPUT", "clock must be callable")
        if not callable(id_factory):
            raise OrderError("INVALID_INPUT", "id_factory must be callable")

        self._clock = clock
        self._id_factory = id_factory
        self._lock = _threading.RLock()
        try:
            self._connection = _sqlite3.connect(
                db_path,
                timeout=10.0,
                isolation_level=None,
                check_same_thread=False,
            )
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA busy_timeout = 10000")
            self._initialize_or_validate_schema()
        except OrderError:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
            raise
        except (_sqlite3.Error, OSError, TypeError, ValueError) as exc:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
            raise self._storage_error(exc) from None

    def set_stock(self, sku: str, quantity: int) -> dict:
        self._validate_identifier("sku", sku)
        self._validate_nonnegative_integer("quantity", quantity)
        try:
            with self._transaction(immediate=True):
                self._connection.execute(
                    """
                    INSERT INTO stock (sku, available) VALUES (?, ?)
                    ON CONFLICT(sku) DO UPDATE SET available = excluded.available
                    """,
                    (sku, str(quantity)),
                )
            return {"sku": sku, "available": quantity}
        except OrderError:
            raise
        except (_sqlite3.Error, OSError) as exc:
            raise self._storage_error(exc) from None

    def create_order(self, idempotency_key: str, items: list[dict]) -> dict:
        self._validate_identifier("idempotency_key", idempotency_key)
        canonical_items = self._validate_items(items)
        total_cents = sum(quantity * unit_price for _, quantity, unit_price in canonical_items)

        try:
            with self._transaction(immediate=True):
                existing = self._connection.execute(
                    "SELECT order_id FROM orders WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if existing is not None:
                    order = self._load_order(existing[0])
                    existing_items = tuple(
                        (item["sku"], item["quantity"], item["unit_price_cents"])
                        for item in order["items"]
                    )
                    if existing_items != canonical_items:
                        raise OrderError(
                            "IDEMPOTENCY_CONFLICT",
                            "idempotency key was already used for different items",
                            {"idempotency_key": idempotency_key},
                        )
                    return order

                unavailable: list[str] = []
                for sku, quantity, _ in canonical_items:
                    row = self._connection.execute(
                        "SELECT available FROM stock WHERE sku = ?", (sku,)
                    ).fetchone()
                    if row is None or self._stored_int(row[0], "stock.available") < quantity:
                        unavailable.append(sku)
                if unavailable:
                    raise OrderError(
                        "INSUFFICIENT_STOCK",
                        "one or more items have insufficient stock",
                        {"skus": unavailable},
                    )

                order_id = self._new_order_id()
                now = self._read_clock()
                encoded_now = self._encode_number(now)
                self._connection.execute(
                    """
                    INSERT INTO orders (
                        order_id, idempotency_key, status, total_cents,
                        payment_id, payment_amount, created_at, updated_at
                    ) VALUES (?, ?, 'pending', ?, NULL, NULL, ?, ?)
                    """,
                    (order_id, idempotency_key, str(total_cents), encoded_now, encoded_now),
                )
                for sku, quantity, unit_price in canonical_items:
                    self._connection.execute(
                        """
                        INSERT INTO order_items (
                            order_id, sku, quantity, unit_price_cents
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (order_id, sku, str(quantity), str(unit_price)),
                    )
                    available_row = self._connection.execute(
                        "SELECT available FROM stock WHERE sku = ?", (sku,)
                    ).fetchone()
                    if available_row is None:
                        raise self._corrupt_storage("reserved stock row disappeared")
                    available = self._stored_int(available_row[0], "stock.available")
                    self._connection.execute(
                        "UPDATE stock SET available = ? WHERE sku = ?",
                        (str(available - quantity), sku),
                    )
                return self._load_order(order_id)
        except OrderError:
            raise
        except (_sqlite3.Error, OSError) as exc:
            raise self._storage_error(exc) from None

    def pay_order(self, order_id: str, payment_id: str, amount_cents: int) -> dict:
        self._validate_identifier("order_id", order_id)
        self._validate_identifier("payment_id", payment_id)
        self._validate_nonnegative_integer("amount_cents", amount_cents)

        try:
            with self._transaction(immediate=True):
                order = self._load_order(order_id)
                if order["payment_id"] == payment_id and amount_cents != order["total_cents"]:
                    raise OrderError(
                        "PAYMENT_CONFLICT",
                        "payment ID was already used with a different amount",
                        {"payment_id": payment_id},
                    )
                if amount_cents != order["total_cents"]:
                    raise OrderError(
                        "AMOUNT_MISMATCH",
                        "payment amount does not match the order total",
                        {"expected": order["total_cents"], "received": amount_cents},
                    )

                payment_owner = self._connection.execute(
                    """
                    SELECT order_id, payment_amount
                    FROM orders WHERE payment_id = ?
                    """,
                    (payment_id,),
                ).fetchone()
                if payment_owner is not None:
                    recorded_amount = self._stored_int(
                        payment_owner[1], "orders.payment_amount"
                    )
                    if payment_owner[0] != order_id or recorded_amount != amount_cents:
                        raise OrderError(
                            "PAYMENT_CONFLICT",
                            "payment ID was already used for a different payment",
                            {"payment_id": payment_id},
                        )

                if order["payment_id"] is not None:
                    if order["payment_id"] == payment_id:
                        return order
                    raise OrderError(
                        "PAYMENT_CONFLICT",
                        "order already has a different payment",
                        {"order_id": order_id},
                    )
                if order["status"] != "pending":
                    raise OrderError(
                        "INVALID_STATE",
                        "only a pending order can be paid",
                        {"order_id": order_id, "status": order["status"]},
                    )

                now = self._encode_number(self._read_clock())
                self._connection.execute(
                    """
                    UPDATE orders
                    SET status = 'paid', payment_id = ?, payment_amount = ?, updated_at = ?
                    WHERE order_id = ?
                    """,
                    (payment_id, str(amount_cents), now, order_id),
                )
                return self._load_order(order_id)
        except OrderError:
            raise
        except (_sqlite3.Error, OSError) as exc:
            raise self._storage_error(exc) from None

    def cancel_order(self, order_id: str) -> dict:
        self._validate_identifier("order_id", order_id)
        try:
            with self._transaction(immediate=True):
                order = self._load_order(order_id)
                if order["status"] == "cancelled":
                    return order
                if order["status"] == "shipped":
                    raise OrderError(
                        "INVALID_STATE",
                        "a shipped order cannot be cancelled",
                        {"order_id": order_id, "status": "shipped"},
                    )
                if order["status"] not in {"pending", "paid"}:
                    raise self._corrupt_storage("order has an invalid status")

                now = self._encode_number(self._read_clock())
                for item in order["items"]:
                    row = self._connection.execute(
                        "SELECT available FROM stock WHERE sku = ?", (item["sku"],)
                    ).fetchone()
                    if row is None:
                        raise self._corrupt_storage("reserved stock row is missing")
                    available = self._stored_int(row[0], "stock.available")
                    self._connection.execute(
                        "UPDATE stock SET available = ? WHERE sku = ?",
                        (str(available + item["quantity"]), item["sku"]),
                    )
                self._connection.execute(
                    "UPDATE orders SET status = 'cancelled', updated_at = ? WHERE order_id = ?",
                    (now, order_id),
                )
                return self._load_order(order_id)
        except OrderError:
            raise
        except (_sqlite3.Error, OSError) as exc:
            raise self._storage_error(exc) from None

    def ship_order(self, order_id: str) -> dict:
        self._validate_identifier("order_id", order_id)
        try:
            with self._transaction(immediate=True):
                order = self._load_order(order_id)
                if order["status"] == "shipped":
                    return order
                if order["status"] != "paid":
                    raise OrderError(
                        "INVALID_STATE",
                        "only a paid order can be shipped",
                        {"order_id": order_id, "status": order["status"]},
                    )
                now = self._encode_number(self._read_clock())
                self._connection.execute(
                    "UPDATE orders SET status = 'shipped', updated_at = ? WHERE order_id = ?",
                    (now, order_id),
                )
                return self._load_order(order_id)
        except OrderError:
            raise
        except (_sqlite3.Error, OSError) as exc:
            raise self._storage_error(exc) from None

    def get_order(self, order_id: str) -> dict:
        self._validate_identifier("order_id", order_id)
        try:
            with self._transaction(immediate=False):
                return self._load_order(order_id)
        except OrderError:
            raise
        except (_sqlite3.Error, OSError) as exc:
            raise self._storage_error(exc) from None

    def _initialize_or_validate_schema(self) -> None:
        with self._transaction(immediate=True):
            table_rows = self._connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name NOT GLOB 'sqlite_*'
                """
            ).fetchall()
            tables = {row[0] for row in table_rows}
            version = self._connection.execute("PRAGMA user_version").fetchone()[0]
            if not tables and version == 0:
                statements = (
                    """
                    CREATE TABLE stock (
                        sku TEXT NOT NULL PRIMARY KEY,
                        available TEXT NOT NULL
                            CHECK (available <> '' AND available NOT GLOB '*[^0-9]*')
                    )
                    """,
                    """
                    CREATE TABLE orders (
                        order_id TEXT NOT NULL PRIMARY KEY,
                        idempotency_key TEXT NOT NULL UNIQUE,
                        status TEXT NOT NULL
                            CHECK (status IN ('pending', 'paid', 'shipped', 'cancelled')),
                        total_cents TEXT NOT NULL
                            CHECK (total_cents <> '' AND total_cents NOT GLOB '*[^0-9]*'),
                        payment_id TEXT,
                        payment_amount TEXT
                            CHECK (
                                payment_amount IS NULL OR
                                (payment_amount <> '' AND payment_amount NOT GLOB '*[^0-9]*')
                            ),
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        CHECK (
                            (status = 'pending' AND payment_id IS NULL AND payment_amount IS NULL) OR
                            (status IN ('paid', 'shipped') AND payment_id IS NOT NULL AND payment_amount IS NOT NULL) OR
                            (status = 'cancelled' AND
                                ((payment_id IS NULL AND payment_amount IS NULL) OR
                                 (payment_id IS NOT NULL AND payment_amount IS NOT NULL)))
                        )
                    )
                    """,
                    """
                    CREATE UNIQUE INDEX idx_orders_payment_id
                        ON orders(payment_id) WHERE payment_id IS NOT NULL
                    """,
                    """
                    CREATE TABLE order_items (
                        order_id TEXT NOT NULL,
                        sku TEXT NOT NULL,
                        quantity TEXT NOT NULL
                            CHECK (quantity <> '' AND quantity NOT GLOB '*[^0-9]*'),
                        unit_price_cents TEXT NOT NULL
                            CHECK (unit_price_cents <> '' AND unit_price_cents NOT GLOB '*[^0-9]*'),
                        PRIMARY KEY (order_id, sku),
                        FOREIGN KEY (order_id) REFERENCES orders(order_id) ON DELETE CASCADE
                    )
                    """,
                )
                for statement in statements:
                    self._connection.execute(statement)
                self._connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
                return
            if version != _SCHEMA_VERSION or tables != _REQUIRED_TABLES:
                raise OrderError(
                    "SCHEMA_MISMATCH",
                    "database schema is not compatible",
                    {"expected_version": _SCHEMA_VERSION, "found_version": version},
                )
            self._validate_schema_columns()

    def _validate_schema_columns(self) -> None:
        expected = {
            "stock": ["sku", "available"],
            "orders": [
                "order_id",
                "idempotency_key",
                "status",
                "total_cents",
                "payment_id",
                "payment_amount",
                "created_at",
                "updated_at",
            ],
            "order_items": ["order_id", "sku", "quantity", "unit_price_cents"],
        }
        for table, expected_columns in expected.items():
            rows = self._connection.execute(f"PRAGMA table_info({table})").fetchall()
            if [row[1] for row in rows] != expected_columns:
                raise OrderError(
                    "SCHEMA_MISMATCH",
                    "database schema is not compatible",
                    {"table": table},
                )
        indexes = self._connection.execute("PRAGMA index_list(orders)").fetchall()
        if not any(row[1] == "idx_orders_payment_id" and row[2] == 1 for row in indexes):
            raise OrderError(
                "SCHEMA_MISMATCH",
                "database schema is not compatible",
                {"table": "orders"},
            )
        foreign_keys = self._connection.execute(
            "PRAGMA foreign_key_list(order_items)"
        ).fetchall()
        if not any(row[2] == "orders" and row[3] == "order_id" and row[4] == "order_id" for row in foreign_keys):
            raise OrderError(
                "SCHEMA_MISMATCH",
                "database schema is not compatible",
                {"table": "order_items"},
            )

    @_contextmanager
    def _transaction(self, *, immediate: bool) -> _Iterator[None]:
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
                yield
                self._connection.execute("COMMIT")
            except BaseException:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise

    def _load_order(self, order_id: str) -> dict:
        row = self._connection.execute(
            """
            SELECT order_id, idempotency_key, status, total_cents,
                   payment_id, payment_amount, created_at, updated_at
            FROM orders WHERE order_id = ?
            """,
            (order_id,),
        ).fetchone()
        if row is None:
            raise OrderError(
                "NOT_FOUND", "order was not found", {"order_id": order_id}
            )
        item_rows = self._connection.execute(
            """
            SELECT sku, quantity, unit_price_cents
            FROM order_items WHERE order_id = ? ORDER BY sku
            """,
            (order_id,),
        ).fetchall()
        if not item_rows:
            raise self._corrupt_storage("order has no items")
        items = [
            {
                "sku": item[0],
                "quantity": self._stored_positive_int(item[1], "order_items.quantity"),
                "unit_price_cents": self._stored_int(
                    item[2], "order_items.unit_price_cents"
                ),
            }
            for item in item_rows
        ]
        total = self._stored_int(row[3], "orders.total_cents")
        if total != sum(item["quantity"] * item["unit_price_cents"] for item in items):
            raise self._corrupt_storage("order total is inconsistent with its items")
        if row[4] is None:
            if row[5] is not None:
                raise self._corrupt_storage("order payment fields are inconsistent")
        else:
            if row[5] is None:
                raise self._corrupt_storage("order payment fields are inconsistent")
            self._stored_int(row[5], "orders.payment_amount")
        return {
            "order_id": row[0],
            "idempotency_key": row[1],
            "status": row[2],
            "items": items,
            "total_cents": total,
            "payment_id": row[4],
            "created_at": self._stored_number(row[6], "orders.created_at"),
            "updated_at": self._stored_number(row[7], "orders.updated_at"),
        }

    def _new_order_id(self) -> str:
        for _ in range(3):
            try:
                order_id = self._id_factory()
            except Exception as exc:
                raise OrderError(
                    "INVALID_DEPENDENCY",
                    "id_factory raised an exception",
                    {"dependency": "id_factory", "exception_type": type(exc).__name__},
                ) from None
            if not self._is_identifier(order_id):
                raise OrderError(
                    "INVALID_DEPENDENCY",
                    "id_factory returned an invalid order ID",
                    {"dependency": "id_factory"},
                )
            exists = self._connection.execute(
                "SELECT 1 FROM orders WHERE order_id = ?", (order_id,)
            ).fetchone()
            if exists is None:
                return order_id
        raise OrderError(
            "ID_COLLISION",
            "id_factory produced duplicate order IDs three times",
            {"attempts": 3},
        )

    def _read_clock(self) -> int | float:
        try:
            value = self._clock()
        except Exception as exc:
            raise OrderError(
                "INVALID_DEPENDENCY",
                "clock raised an exception",
                {"dependency": "clock", "exception_type": type(exc).__name__},
            ) from None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise OrderError(
                "INVALID_DEPENDENCY",
                "clock returned a non-numeric value",
                {"dependency": "clock"},
            )
        if isinstance(value, float) and not _math.isfinite(value):
            raise OrderError(
                "INVALID_DEPENDENCY",
                "clock returned a non-finite value",
                {"dependency": "clock"},
            )
        return value

    @staticmethod
    def _validate_items(items: list[dict]) -> tuple[tuple[str, int, int], ...]:
        if not isinstance(items, list) or not items:
            raise OrderError("INVALID_INPUT", "items must be a non-empty list")
        canonical: list[tuple[str, int, int]] = []
        seen: set[str] = set()
        expected_fields = {"sku", "quantity", "unit_price_cents"}
        for index, item in enumerate(items):
            if not isinstance(item, dict) or set(item) != expected_fields:
                raise OrderError(
                    "INVALID_INPUT",
                    "each item must contain exactly sku, quantity, and unit_price_cents",
                    {"index": index},
                )
            sku = item["sku"]
            quantity = item["quantity"]
            unit_price = item["unit_price_cents"]
            OrderService._validate_identifier("sku", sku, index=index)
            OrderService._validate_positive_integer("quantity", quantity, index=index)
            OrderService._validate_nonnegative_integer(
                "unit_price_cents", unit_price, index=index
            )
            if sku in seen:
                raise OrderError(
                    "INVALID_INPUT", "duplicate SKU in items", {"sku": sku}
                )
            seen.add(sku)
            canonical.append((sku, quantity, unit_price))
        return tuple(sorted(canonical, key=lambda item: item[0]))

    @staticmethod
    def _is_identifier(value: object) -> bool:
        return isinstance(value, str) and value != "" and not value.isspace()

    @staticmethod
    def _validate_identifier(name: str, value: object, **details: object) -> None:
        if not OrderService._is_identifier(value):
            raise OrderError(
                "INVALID_INPUT",
                f"{name} must be a non-empty, non-whitespace string",
                {"field": name, **details},
            )

    @staticmethod
    def _validate_nonnegative_integer(
        name: str, value: object, **details: object
    ) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise OrderError(
                "INVALID_INPUT",
                f"{name} must be an integer greater than or equal to zero",
                {"field": name, **details},
            )

    @staticmethod
    def _validate_positive_integer(name: str, value: object, **details: object) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise OrderError(
                "INVALID_INPUT",
                f"{name} must be a positive integer",
                {"field": name, **details},
            )

    @staticmethod
    def _encode_number(value: int | float) -> str:
        return _json.dumps(value, allow_nan=False, separators=(",", ":"))

    @staticmethod
    def _stored_number(value: object, field: str) -> int | float:
        if not isinstance(value, str):
            raise OrderService._corrupt_storage(f"{field} has an invalid type")
        try:
            decoded = _json.loads(value)
        except (_json.JSONDecodeError, TypeError, ValueError):
            raise OrderService._corrupt_storage(f"{field} is invalid") from None
        if isinstance(decoded, bool) or not isinstance(decoded, (int, float)):
            raise OrderService._corrupt_storage(f"{field} is invalid")
        if isinstance(decoded, float) and not _math.isfinite(decoded):
            raise OrderService._corrupt_storage(f"{field} is invalid")
        return decoded

    @staticmethod
    def _stored_int(value: object, field: str) -> int:
        if not isinstance(value, str) or not value or not value.isascii() or not value.isdigit():
            raise OrderService._corrupt_storage(f"{field} is invalid")
        return int(value)

    @staticmethod
    def _stored_positive_int(value: object, field: str) -> int:
        result = OrderService._stored_int(value, field)
        if result <= 0:
            raise OrderService._corrupt_storage(f"{field} is invalid")
        return result

    @staticmethod
    def _corrupt_storage(message: str) -> OrderError:
        return OrderError("STORAGE_ERROR", message)

    @staticmethod
    def _storage_error(exc: BaseException) -> OrderError:
        return OrderError(
            "STORAGE_ERROR",
            "SQLite storage operation failed",
            {"exception_type": type(exc).__name__},
        )


__all__ = ["OrderError", "OrderService"]
