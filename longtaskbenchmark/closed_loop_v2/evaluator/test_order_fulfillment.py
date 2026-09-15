from __future__ import annotations

import math
from pathlib import Path
import sqlite3
import unittest

from src.order_fulfillment import OrderError, OrderService

from closed_loop_v2.evaluator._support import (
    DeterministicIds,
    FakeClock,
    assert_order_schema,
    expect_domain_error,
    order_create_worker,
    run_spawn_race,
    temporary_workspace,
)


ITEM_A = {"sku": "a", "quantity": 1, "unit_price_cents": 100}


class OrderPersistenceTests(unittest.TestCase):
    def test_schema_initialize_and_reopen(self) -> None:
        with temporary_workspace("order-persistence-") as root:
            db_path = root / "nested-name.sqlite3"
            clock = FakeClock(10.5)
            ids = DeterministicIds(["persisted-order"])
            service = OrderService(db_path, clock=clock, id_factory=ids)
            self.assertTrue(db_path.is_file())
            self.assertEqual(service.set_stock("b", 2), {"sku": "b", "available": 2})
            self.assertEqual(service.set_stock("a", 3), {"sku": "a", "available": 3})
            created = service.create_order(
                "persist-key",
                [
                    {"sku": "b", "quantity": 2, "unit_price_cents": 7},
                    {"sku": "a", "quantity": 1, "unit_price_cents": 11},
                ],
            )
            created = assert_order_schema(self, created, status="pending")
            self.assertEqual(created["order_id"], "persisted-order")
            self.assertEqual(created["items"], [
                {"sku": "a", "quantity": 1, "unit_price_cents": 11},
                {"sku": "b", "quantity": 2, "unit_price_cents": 7},
            ])
            self.assertEqual(created["total_cents"], 25)
            self.assertEqual(created["created_at"], 10.5)
            self.assertEqual(created["updated_at"], 10.5)

            reopened_clock = FakeClock(999.0)
            reopened_ids = DeterministicIds(prefix="unused")
            reopened = OrderService(str(db_path), clock=reopened_clock, id_factory=reopened_ids)
            self.assertEqual(reopened.get_order("persisted-order"), created)
            self.assertEqual(reopened_clock.calls, 0)
            self.assertEqual(reopened_ids.calls, 0)
            again = OrderService(db_path, clock=reopened_clock, id_factory=reopened_ids)
            self.assertEqual(again.get_order("persisted-order"), created)

            versioned = root / "unknown-schema.sqlite3"
            with sqlite3.connect(versioned) as connection:
                connection.execute("PRAGMA user_version = 987654")
            with expect_domain_error(self, OrderError, "SCHEMA_MISMATCH"):
                OrderService(versioned, clock=FakeClock(), id_factory=DeterministicIds())

    def test_input_and_dependency_validation(self) -> None:
        with temporary_workspace("order-validation-") as root:
            clock = FakeClock(1.0)
            ids = DeterministicIds(prefix="valid")
            service = OrderService(root / "orders.sqlite3", clock=clock, id_factory=ids)

            invalid_stock = [
                ("", 1), ("   ", 1), (1, 1), ("sku", True), ("sku", -1), ("sku", 1.0),
            ]
            for sku, quantity in invalid_stock:
                with self.subTest(stock=(sku, quantity)):
                    with expect_domain_error(self, OrderError, "INVALID_INPUT"):
                        service.set_stock(sku, quantity)
            self.assertEqual(clock.calls, 0)
            self.assertEqual(ids.calls, 0)
            self.assertEqual(service.set_stock(" sku ", 2), {"sku": " sku ", "available": 2})
            self.assertEqual(service.set_stock("a", 5), {"sku": "a", "available": 5})

            invalid_items = [
                [], {}, [ITEM_A, ITEM_A],
                [{"sku": "a", "quantity": 1}],
                [{"sku": "a", "quantity": 1, "unit_price_cents": 1, "extra": 2}],
                [{"sku": "", "quantity": 1, "unit_price_cents": 1}],
                [{"sku": "   ", "quantity": 1, "unit_price_cents": 1}],
                [{"sku": 1, "quantity": 1, "unit_price_cents": 1}],
                [{"sku": "a", "quantity": True, "unit_price_cents": 1}],
                [{"sku": "a", "quantity": 0, "unit_price_cents": 1}],
                [{"sku": "a", "quantity": 1.0, "unit_price_cents": 1}],
                [{"sku": "a", "quantity": 1, "unit_price_cents": True}],
                [{"sku": "a", "quantity": 1, "unit_price_cents": -1}],
                [{"sku": "a", "quantity": 1, "unit_price_cents": 1.0}],
            ]
            for items in invalid_items:
                with self.subTest(items=items):
                    before_clock, before_ids = clock.calls, ids.calls
                    with expect_domain_error(self, OrderError, "INVALID_INPUT"):
                        service.create_order("key", items)
                    self.assertEqual(clock.calls, before_clock)
                    self.assertEqual(ids.calls, before_ids)
            for key in ("", "\t", 3):
                with self.subTest(idempotency_key=key):
                    with expect_domain_error(self, OrderError, "INVALID_INPUT"):
                        service.create_order(key, [ITEM_A])

            for method, arguments in [
                (service.get_order, ("",)),
                (service.get_order, (1,)),
                (service.cancel_order, (None,)),
                (service.cancel_order, (True,)),
                (service.ship_order, ("  ",)),
                (service.ship_order, (3.0,)),
                (service.pay_order, ("order", "", 1)),
                (service.pay_order, ("order", 4, 1)),
                (service.pay_order, (1, "payment", 1)),
                (service.pay_order, ("order", "payment", True)),
                (service.pay_order, ("order", "payment", -1)),
                (service.pay_order, ("order", "payment", 1.0)),
            ]:
                with self.subTest(method=method.__name__, arguments=arguments):
                    with expect_domain_error(self, OrderError, "INVALID_INPUT"):
                        method(*arguments)

            for index, bad_now in enumerate((math.nan, math.inf, True, "now")):
                with self.subTest(clock_value=bad_now):
                    bad_clock = FakeClock(bad_now)
                    bad_clock_service = OrderService(
                        root / f"bad-clock-{index}.sqlite3",
                        clock=bad_clock,
                        id_factory=DeterministicIds(["unused"]),
                    )
                    bad_clock_service.set_stock("a", 1)
                    with expect_domain_error(self, OrderError, "INVALID_DEPENDENCY"):
                        bad_clock_service.create_order("bad-clock", [ITEM_A])
                    self.assertEqual(bad_clock.calls, 1)

            for index, dependencies in enumerate(((1, DeterministicIds()), (FakeClock(), 1))):
                with self.subTest(dependencies=dependencies):
                    with expect_domain_error(self, OrderError, "INVALID_INPUT"):
                        OrderService(
                            root / f"bad-dependency-{index}.sqlite3",
                            clock=dependencies[0],
                            id_factory=dependencies[1],
                        )

            for bad_id in ("", "  ", 17):
                with self.subTest(id_factory_value=bad_id):
                    candidate = OrderService(
                        root / f"bad-id-{repr(bad_id)}.sqlite3",
                        clock=FakeClock(2.0),
                        id_factory=DeterministicIds([bad_id]),
                    )
                    candidate.set_stock("a", 1)
                    with expect_domain_error(self, OrderError, "INVALID_DEPENDENCY"):
                        candidate.create_order("bad-id", [ITEM_A])

            collision_db = root / "collisions.sqlite3"
            seed = OrderService(collision_db, clock=FakeClock(3), id_factory=DeterministicIds(["same-id"]))
            seed.set_stock("a", 2)
            seed.create_order("first-key", [ITEM_A])
            colliding_ids = DeterministicIds(["same-id", "same-id", "same-id"])
            collision = OrderService(collision_db, clock=FakeClock(4), id_factory=colliding_ids)
            with expect_domain_error(self, OrderError, "ID_COLLISION"):
                collision.create_order("second-key", [ITEM_A])
            self.assertEqual(colliding_ids.calls, 3)
            successful = OrderService(
                collision_db,
                clock=FakeClock(5),
                id_factory=DeterministicIds(["different-id"]),
            ).create_order("third-key", [ITEM_A])
            self.assertEqual(successful["order_id"], "different-id")


class OrderCreationTests(unittest.TestCase):
    def test_multi_sku_reservation_is_atomic(self) -> None:
        with temporary_workspace("order-atomic-create-") as root:
            clock = FakeClock(20)
            ids = DeterministicIds(["failed-unused", "complete"])
            service = OrderService(root / "orders.sqlite3", clock=clock, id_factory=ids)
            service.set_stock("a", 2)
            service.set_stock("b", 1)
            service.set_stock("zero", 0)

            for bad_items in [
                [
                    {"sku": "a", "quantity": 1, "unit_price_cents": 3},
                    {"sku": "missing", "quantity": 1, "unit_price_cents": 4},
                ],
                [
                    {"sku": "a", "quantity": 1, "unit_price_cents": 3},
                    {"sku": "b", "quantity": 2, "unit_price_cents": 4},
                ],
                [{"sku": "zero", "quantity": 1, "unit_price_cents": 0}],
            ]:
                with self.subTest(bad_items=bad_items):
                    before_clock, before_ids = clock.calls, ids.calls
                    with expect_domain_error(self, OrderError, "INSUFFICIENT_STOCK"):
                        service.create_order(f"fail-{repr(bad_items)}", bad_items)
                    self.assertEqual(clock.calls, before_clock)
                    self.assertEqual(ids.calls, before_ids)

            result = service.create_order(
                "all-stock",
                [
                    {"sku": "b", "quantity": 1, "unit_price_cents": 5},
                    {"sku": "a", "quantity": 2, "unit_price_cents": 7},
                ],
            )
            result = assert_order_schema(self, result, status="pending")
            self.assertEqual(result["order_id"], "failed-unused")
            self.assertEqual(result["total_cents"], 19)
            with expect_domain_error(self, OrderError, "INSUFFICIENT_STOCK"):
                service.create_order("oversell", [ITEM_A])

    def test_create_idempotency_and_conflict(self) -> None:
        with temporary_workspace("order-idempotency-") as root:
            clock = FakeClock(30.0)
            ids = DeterministicIds(["idempotent-order", "second-order"])
            service = OrderService(root / "orders.sqlite3", clock=clock, id_factory=ids)
            service.set_stock("a", 2)
            service.set_stock("b", 2)
            original_items = [
                {"sku": "b", "quantity": 1, "unit_price_cents": 2},
                {"sku": "a", "quantity": 1, "unit_price_cents": 3},
            ]
            original = service.create_order("same-key", original_items)
            self.assertEqual((clock.calls, ids.calls), (1, 1))
            clock.set(999.0)
            repeated = service.create_order("same-key", list(reversed(original_items)))
            self.assertEqual(repeated, original)
            self.assertEqual((clock.calls, ids.calls), (1, 1))

            variants = [
                [{"sku": "a", "quantity": 2, "unit_price_cents": 3}],
                [{"sku": "a", "quantity": 1, "unit_price_cents": 4}],
                [{"sku": "b", "quantity": 1, "unit_price_cents": 2}],
            ]
            for variant in variants:
                with self.subTest(variant=variant):
                    with expect_domain_error(self, OrderError, "IDEMPOTENCY_CONFLICT"):
                        service.create_order("same-key", variant)
                    self.assertEqual((clock.calls, ids.calls), (1, 1))

            second = service.create_order(
                "other-key",
                [
                    {"sku": "a", "quantity": 1, "unit_price_cents": 3},
                    {"sku": "b", "quantity": 1, "unit_price_cents": 2},
                ],
            )
            self.assertEqual(second["order_id"], "second-order")
            self.assertEqual((clock.calls, ids.calls), (2, 2))


class OrderPaymentTests(unittest.TestCase):
    def test_payment_idempotency_conflicts_and_amount(self) -> None:
        with temporary_workspace("order-payment-") as root:
            clock = FakeClock(40.0)
            service = OrderService(
                root / "orders.sqlite3",
                clock=clock,
                id_factory=DeterministicIds(["one", "two"]),
            )
            service.set_stock("a", 2)
            first = service.create_order("key-one", [ITEM_A])
            second = service.create_order("key-two", [ITEM_A])
            before = service.get_order(first["order_id"])

            with expect_domain_error(self, OrderError, "AMOUNT_MISMATCH"):
                service.pay_order("one", "payment-one", 99)
            self.assertEqual(service.get_order("one"), before)
            calls_before_payment = clock.calls
            paid = service.pay_order("one", "payment-one", 100)
            paid = assert_order_schema(self, paid, status="paid")
            self.assertEqual(paid["payment_id"], "payment-one")
            self.assertEqual(clock.calls, calls_before_payment + 1)

            clock.set(500)
            repeated = service.pay_order("one", "payment-one", 100)
            self.assertEqual(repeated, paid)
            self.assertEqual(clock.calls, calls_before_payment + 1)
            with expect_domain_error(self, OrderError, "PAYMENT_CONFLICT"):
                service.pay_order("one", "different-payment", 100)
            with expect_domain_error(self, OrderError, "PAYMENT_CONFLICT"):
                service.pay_order(second["order_id"], "payment-one", 100)
            with expect_domain_error(self, OrderError, "PAYMENT_CONFLICT"):
                service.pay_order("one", "payment-one", 101)
            self.assertEqual(clock.calls, calls_before_payment + 1)
            self.assertEqual(service.get_order("two")["status"], "pending")

    def test_explicit_state_machine(self) -> None:
        with temporary_workspace("order-state-") as root:
            clock = FakeClock(50)
            service = OrderService(
                root / "orders.sqlite3",
                clock=clock,
                id_factory=DeterministicIds(["ship-me", "cancel-me"]),
            )
            service.set_stock("a", 2)
            shipping = service.create_order("shipping", [ITEM_A])
            with expect_domain_error(self, OrderError, "INVALID_STATE"):
                service.ship_order(shipping["order_id"])
            paid = service.pay_order(shipping["order_id"], "pay-ship", 100)
            shipped = service.ship_order(shipping["order_id"])
            assert_order_schema(self, shipped, status="shipped")
            self.assertEqual(shipped["payment_id"], "pay-ship")
            calls_after_ship = clock.calls
            self.assertEqual(service.ship_order(shipping["order_id"]), shipped)
            self.assertEqual(clock.calls, calls_after_ship)
            with expect_domain_error(self, OrderError, "INVALID_STATE"):
                service.cancel_order(shipping["order_id"])
            self.assertEqual(service.pay_order(shipping["order_id"], "pay-ship", 100), shipped)

            cancellation = service.create_order("cancellation", [ITEM_A])
            cancelled = service.cancel_order(cancellation["order_id"])
            assert_order_schema(self, cancelled, status="cancelled")
            calls_after_cancel = clock.calls
            self.assertEqual(service.cancel_order(cancellation["order_id"]), cancelled)
            self.assertEqual(clock.calls, calls_after_cancel)
            with expect_domain_error(self, OrderError, "INVALID_STATE"):
                service.ship_order(cancellation["order_id"])
            with expect_domain_error(self, OrderError, "NOT_FOUND"):
                service.get_order("does-not-exist")
            self.assertEqual(service.get_order("ship-me"), shipped)


class OrderInventoryTests(unittest.TestCase):
    def test_cancel_releases_once_and_shipped_cannot_cancel(self) -> None:
        with temporary_workspace("order-cancel-") as root:
            clock = FakeClock(60)
            service = OrderService(
                root / "orders.sqlite3",
                clock=clock,
                id_factory=DeterministicIds(["first", "replacement", "paid-cancel", "after-paid", "shipped"]),
            )
            service.set_stock("a", 1)
            first = service.create_order("first-key", [ITEM_A])
            cancelled = service.cancel_order(first["order_id"])
            self.assertEqual(service.cancel_order(first["order_id"]), cancelled)
            replacement = service.create_order("replacement-key", [ITEM_A])
            self.assertEqual(replacement["order_id"], "replacement")
            with expect_domain_error(self, OrderError, "INSUFFICIENT_STOCK"):
                service.create_order("would-double-release", [ITEM_A])

            service.set_stock("paid", 1)
            paid_item = {"sku": "paid", "quantity": 1, "unit_price_cents": 8}
            paid_order = service.create_order("paid-key", [paid_item])
            service.pay_order(paid_order["order_id"], "paid-token", 8)
            paid_cancelled = service.cancel_order(paid_order["order_id"])
            self.assertEqual(paid_cancelled["status"], "cancelled")
            self.assertEqual(paid_cancelled["payment_id"], "paid-token")
            after_paid = service.create_order("after-paid-key", [paid_item])
            self.assertEqual(after_paid["order_id"], "after-paid")

            service.set_stock("ship", 1)
            ship_item = {"sku": "ship", "quantity": 1, "unit_price_cents": 1}
            ship_order = service.create_order("ship-key", [ship_item])
            service.pay_order(ship_order["order_id"], "ship-payment", 1)
            service.ship_order(ship_order["order_id"])
            with expect_domain_error(self, OrderError, "INVALID_STATE"):
                service.cancel_order(ship_order["order_id"])
            with expect_domain_error(self, OrderError, "INSUFFICIENT_STOCK"):
                service.create_order("ship-stock-stays-reserved", [ship_item])

    def test_two_instances_cannot_oversell(self) -> None:
        with temporary_workspace("order-concurrency-") as root:
            db_path = root / "shared.sqlite3"
            service = OrderService(
                db_path,
                clock=FakeClock(0),
                id_factory=DeterministicIds(["initializer-unused"]),
            )
            service.set_stock("shared", 1)
            rows = [(str(db_path), f"key-{index}", f"order-{index}") for index in range(6)]
            results = run_spawn_race(order_create_worker, rows)
            worker_errors = [result for result in results if result[0] == "worker_error"]
            self.assertEqual(worker_errors, [])
            successes = [result[1] for result in results if result[0] == "ok"]
            failures = [result for result in results if result[0] == "error"]
            self.assertEqual(len(successes), 1, results)
            self.assertEqual(len(failures), 5, results)
            self.assertLessEqual(
                {result[1] for result in failures},
                {"INSUFFICIENT_STOCK", "STORAGE_ERROR"},
            )
            winner = assert_order_schema(self, successes[0], status="pending")

            reopened = OrderService(
                db_path,
                clock=FakeClock(1000),
                id_factory=DeterministicIds(["post-race"]),
            )
            self.assertEqual(reopened.get_order(winner["order_id"]), winner)
            for order_id in {f"order-{index}" for index in range(6)} - {winner["order_id"]}:
                with self.subTest(partial_order_id=order_id):
                    with expect_domain_error(self, OrderError, "NOT_FOUND"):
                        reopened.get_order(order_id)
            with expect_domain_error(self, OrderError, "INSUFFICIENT_STOCK"):
                reopened.create_order(
                    "post-race-key",
                    [{"sku": "shared", "quantity": 1, "unit_price_cents": 25}],
                )


if __name__ == "__main__":
    unittest.main()
