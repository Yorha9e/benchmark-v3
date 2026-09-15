from __future__ import annotations

import unittest

from closed_loop_v2.evaluator._support import (
    assert_cli_error,
    assert_cli_success,
    assert_order_schema,
    run_cli,
    temporary_workspace,
)


MODULE = "src.order_fulfillment"


class OrderCliTests(unittest.TestCase):
    def test_all_commands_emit_one_json_line(self) -> None:
        with temporary_workspace("order-cli-happy-") as root:
            db_path = root / "orders.sqlite3"

            completed, decoded = run_cli(MODULE, "--db", db_path, "stock", {"sku": "sku", "quantity": 2})
            self.assertEqual(
                assert_cli_success(self, completed, decoded),
                {"sku": "sku", "available": 2},
            )

            item = {"sku": "sku", "quantity": 1, "unit_price_cents": 125}
            completed, decoded = run_cli(
                MODULE,
                "--db",
                db_path,
                "create",
                {"idempotency_key": "cli-key-one", "items": [item]},
            )
            created = assert_order_schema(self, assert_cli_success(self, completed, decoded), status="pending")
            order_id = created["order_id"]

            completed, decoded = run_cli(MODULE, "--db", db_path, "get", {"order_id": order_id})
            self.assertEqual(assert_cli_success(self, completed, decoded), created)

            completed, decoded = run_cli(
                MODULE,
                "--db",
                db_path,
                "pay",
                {"order_id": order_id, "payment_id": "cli-payment", "amount_cents": 125},
            )
            paid = assert_order_schema(self, assert_cli_success(self, completed, decoded), status="paid")
            self.assertEqual(paid["payment_id"], "cli-payment")

            completed, decoded = run_cli(MODULE, "--db", db_path, "ship", {"order_id": order_id})
            shipped = assert_order_schema(self, assert_cli_success(self, completed, decoded), status="shipped")
            self.assertEqual(shipped["payment_id"], "cli-payment")

            completed, decoded = run_cli(
                MODULE,
                "--db",
                db_path,
                "create",
                {"idempotency_key": "cli-key-two", "items": [item]},
            )
            second = assert_order_schema(self, assert_cli_success(self, completed, decoded), status="pending")
            completed, decoded = run_cli(
                MODULE,
                "--db",
                db_path,
                "cancel",
                {"order_id": second["order_id"]},
            )
            cancelled = assert_order_schema(self, assert_cli_success(self, completed, decoded), status="cancelled")
            self.assertEqual(cancelled["order_id"], second["order_id"])

    def test_error_codes_exit_codes_and_persistence(self) -> None:
        with temporary_workspace("order-cli-errors-") as root:
            db_path = root / "orders.sqlite3"
            completed, decoded = run_cli(MODULE, "--db", db_path, "stock", {"sku": "a", "quantity": 1})
            assert_cli_success(self, completed, decoded)
            completed, decoded = run_cli(
                MODULE,
                "--db",
                db_path,
                "create",
                {
                    "idempotency_key": "persistent",
                    "items": [{"sku": "a", "quantity": 1, "unit_price_cents": 9}],
                },
            )
            persisted = assert_cli_success(self, completed, decoded)

            invalid_cases = [
                ("stock", None, ""),
                ("stock", None, "   \n"),
                ("stock", None, "{not-json}\n"),
                ("stock", None, "[]\n"),
                ("stock", {}, None),
                ("stock", {"sku": "a", "quantity": 1, "extra": True}, None),
                ("stock", {"sku": "a", "quantity": True}, None),
                ("pay", {"order_id": persisted["order_id"], "payment_id": "p"}, None),
                ("get", None, None),
                ("get", None, '{"order_id":"x"}\n{"order_id":"y"}\n'),
                ("unknown-command", {}, None),
            ]
            for command, request, raw_input in invalid_cases:
                with self.subTest(command=command, request=request, raw_input=raw_input):
                    completed, decoded = run_cli(
                        MODULE,
                        "--db",
                        db_path,
                        command,
                        request,
                        raw_input=raw_input,
                    )
                    assert_cli_error(
                        self,
                        completed,
                        decoded,
                        exit_code=2,
                        code="INVALID_INPUT",
                    )

            completed, decoded = run_cli(
                MODULE,
                "--db",
                db_path,
                "create",
                {
                    "idempotency_key": "no-stock",
                    "items": [{"sku": "a", "quantity": 1, "unit_price_cents": 9}],
                },
            )
            assert_cli_error(self, completed, decoded, exit_code=2, code="INSUFFICIENT_STOCK")

            completed, decoded = run_cli(
                MODULE,
                "--db",
                db_path,
                "get",
                {"order_id": persisted["order_id"]},
            )
            self.assertEqual(assert_cli_success(self, completed, decoded), persisted)

            unusable_db = root / "database-is-a-directory"
            unusable_db.mkdir()
            completed, decoded = run_cli(
                MODULE,
                "--db",
                unusable_db,
                "stock",
                {"sku": "x", "quantity": 1},
            )
            assert_cli_error(self, completed, decoded, exit_code=3, code="STORAGE_ERROR")


if __name__ == "__main__":
    unittest.main()
