from __future__ import annotations

import json
import math
from pathlib import Path
import unittest

from src.delivery_spool import Limits, Spool, SpoolError

from closed_loop_v2.evaluator._support import (
    FakeClock,
    assert_message_schema,
    assert_spool_root_boundary,
    expect_domain_error,
    run_spawn_race,
    spool_claim_worker,
    temporary_workspace,
)


def make_spool(
    root: Path,
    clock: FakeClock,
    *,
    limits: Limits | None = None,
    failpoint=None,
    lock_timeout: float = 5.0,
) -> Spool:
    return Spool(
        root,
        clock=clock,
        limits=limits if limits is not None else Limits(),
        lock_timeout=lock_timeout,
        failpoint=failpoint,
    )


class SpoolPersistenceTests(unittest.TestCase):
    def test_initialize_reopen_and_fail_closed(self) -> None:
        with temporary_workspace("spool-persistence-") as workspace:
            root = workspace / "spool"
            clock = FakeClock(10)
            spool = make_spool(root, clock)
            self.assertFalse(root.exists(), "constructor validation must not write")
            initialized = spool.initialize()
            self.assertIs(type(initialized), dict)
            self.assertTrue(root.is_dir())
            expected_state = '{"messages":{},"next_sequence":1,"schema_version":1}\n'
            self.assertEqual((root / "state.json").read_text(encoding="utf-8"), expected_state)
            assert_spool_root_boundary(self, root)
            with expect_domain_error(self, SpoolError, "ALREADY_INITIALIZED"):
                spool.initialize()
            self.assertEqual((root / "state.json").read_text(encoding="utf-8"), expected_state)

            reopened = make_spool(root, FakeClock(500))
            self.assertEqual(reopened.list_messages(), [])
            with expect_domain_error(self, SpoolError, "NOT_FOUND"):
                reopened.get("missing")

            uninitialized_root = workspace / "not-initialized"
            uninitialized = make_spool(uninitialized_root, FakeClock())
            with expect_domain_error(self, SpoolError, "NOT_INITIALIZED"):
                uninitialized.list_messages()
            with expect_domain_error(self, SpoolError, "NOT_INITIALIZED"):
                uninitialized.enqueue("m", {})

            corrupt_cases = [
                ("malformed", "{broken\n", "CORRUPT_STATE"),
                (
                    "non-canonical",
                    '{"schema_version": 1, "next_sequence": 1, "messages": {}}\n',
                    "CORRUPT_STATE",
                ),
                (
                    "duplicate-key",
                    '{"schema_version":1,"schema_version":1,"next_sequence":1,"messages":{}}\n',
                    "CORRUPT_STATE",
                ),
                (
                    "unknown-field",
                    '{"messages":{},"next_sequence":1,"schema_version":1,"extra":true}\n',
                    "CORRUPT_STATE",
                ),
                (
                    "invalid-next-sequence",
                    '{"messages":{},"next_sequence":true,"schema_version":1}\n',
                    "CORRUPT_STATE",
                ),
                (
                    "unknown-schema",
                    '{"messages":{},"next_sequence":1,"schema_version":2}\n',
                    "SCHEMA_MISMATCH",
                ),
            ]
            for name, contents, code in corrupt_cases:
                with self.subTest(corruption=name):
                    corrupt_root = workspace / name
                    corrupt_root.mkdir()
                    state_path = corrupt_root / "state.json"
                    state_path.write_text(contents, encoding="utf-8")
                    corrupt = make_spool(corrupt_root, FakeClock())
                    with expect_domain_error(self, SpoolError, code):
                        corrupt.list_messages()
                    self.assertEqual(state_path.read_text(encoding="utf-8"), contents)
                    with expect_domain_error(self, SpoolError, code):
                        corrupt.initialize()
                    self.assertEqual(state_path.read_text(encoding="utf-8"), contents)

            valid_message_root = workspace / "valid-message-source"
            valid_message_spool = make_spool(valid_message_root, FakeClock())
            valid_message_spool.initialize()
            valid_message_spool.enqueue("message", {}, available_at=0)
            valid_message_state = json.loads(
                (valid_message_root / "state.json").read_text(encoding="utf-8")
            )

            message_corruptions = []
            unknown_field = json.loads(json.dumps(valid_message_state))
            unknown_field["messages"]["message"]["unexpected_field"] = "must fail closed"
            message_corruptions.append(("unknown-message-field", unknown_field))
            missing_field = json.loads(json.dumps(valid_message_state))
            del missing_field["messages"]["message"]["last_error"]
            message_corruptions.append(("missing-message-field", missing_field))
            boolean_attempts = json.loads(json.dumps(valid_message_state))
            boolean_attempts["messages"]["message"]["attempts"] = True
            message_corruptions.append(("boolean-attempts", boolean_attempts))
            unknown_status = json.loads(json.dumps(valid_message_state))
            unknown_status["messages"]["message"]["status"] = "unknown"
            message_corruptions.append(("unknown-status", unknown_status))
            inconsistent_sequence = json.loads(json.dumps(valid_message_state))
            inconsistent_sequence["next_sequence"] = 1
            message_corruptions.append(("inconsistent-next-sequence", inconsistent_sequence))
            mismatched_key = json.loads(json.dumps(valid_message_state))
            mismatched_key["messages"]["other-key"] = mismatched_key["messages"].pop("message")
            message_corruptions.append(("message-key-mismatch", mismatched_key))

            leased_root = workspace / "valid-leased-source"
            leased_spool = make_spool(leased_root, FakeClock(0))
            leased_spool.initialize()
            leased_spool.enqueue("leased", {}, available_at=0)
            leased_spool.claim("worker")
            leased_state = json.loads((leased_root / "state.json").read_text(encoding="utf-8"))
            del leased_state["messages"]["leased"]["lease_token"]
            message_corruptions.append(("leased-without-token", leased_state))

            for name, message_state in message_corruptions:
                with self.subTest(message_corruption=name):
                    message_corrupt_root = workspace / name
                    message_corrupt_root.mkdir()
                    message_state_path = message_corrupt_root / "state.json"
                    message_contents = json.dumps(
                        message_state,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                        allow_nan=False,
                    ) + "\n"
                    message_state_path.write_text(message_contents, encoding="utf-8")
                    message_corrupt = make_spool(message_corrupt_root, FakeClock())
                    with expect_domain_error(self, SpoolError, "CORRUPT_STATE"):
                        message_corrupt.list_messages()
                    self.assertEqual(message_state_path.read_text(encoding="utf-8"), message_contents)

            invalid_limit_fields = [
                {"max_messages": True},
                {"max_messages": 0},
                {"max_payload_bytes": 0},
                {"max_attempts": False},
                {"lease_seconds": 0},
                {"lease_seconds": math.inf},
                {"retry_delay": -1},
            ]
            for index, fields in enumerate(invalid_limit_fields):
                with self.subTest(limit_fields=fields):
                    bad_root = workspace / f"invalid-limits-{index}"
                    with expect_domain_error(self, SpoolError, "INVALID_INPUT"):
                        limits = Limits(**fields)
                        Spool(
                            bad_root,
                            clock=FakeClock(),
                            limits=limits,
                            lock_timeout=1.0,
                            failpoint=None,
                        )
                    self.assertFalse(bad_root.exists())

            invalid_constructors = [
                {"limits": object()},
                {"clock": 1},
                {"failpoint": "not-callable"},
                {"lock_timeout": True},
                {"lock_timeout": -1},
                {"lock_timeout": math.nan},
            ]
            for index, overrides in enumerate(invalid_constructors):
                with self.subTest(constructor=overrides):
                    bad_root = workspace / f"invalid-constructor-{index}"
                    arguments = {
                        "clock": FakeClock(),
                        "limits": Limits(),
                        "lock_timeout": 1.0,
                        "failpoint": None,
                    }
                    arguments.update(overrides)
                    with expect_domain_error(self, SpoolError, "INVALID_INPUT"):
                        Spool(bad_root, **arguments)
                    self.assertFalse(bad_root.exists())

    def test_canonical_payload_idempotency_and_limits(self) -> None:
        with temporary_workspace("spool-payload-") as workspace:
            clock = FakeClock(7)
            root = workspace / "main"
            spool = make_spool(root, clock, limits=Limits(max_messages=3, max_payload_bytes=100))
            spool.initialize()
            payload = {"z": [1, {"nested": True}], "a": "é"}
            original = spool.enqueue("m1", payload, available_at=1)
            original = assert_message_schema(self, original, status="pending")
            self.assertEqual(original["sequence"], 1)
            self.assertEqual(original["attempts"], 0)
            self.assertEqual(original["last_error"], None)
            self.assertEqual(clock.calls, 0)
            payload["z"][1]["nested"] = False
            payload["z"].append(99)
            self.assertEqual(spool.get("m1"), original)
            original["payload"]["z"].append("mutated returned value")
            persisted = spool.get("m1")
            self.assertEqual(persisted["payload"], {"a": "é", "z": [1, {"nested": True}]})
            original = persisted

            reordered = {"a": "é", "z": [1, {"nested": True}]}
            repeated = spool.enqueue("m1", reordered, available_at=1.0)
            self.assertEqual(repeated, original)
            self.assertEqual(clock.calls, 0)
            with expect_domain_error(self, SpoolError, "IDEMPOTENCY_CONFLICT"):
                spool.enqueue("m1", {"a": "different"}, available_at=1)
            with expect_domain_error(self, SpoolError, "IDEMPOTENCY_CONFLICT"):
                spool.enqueue("m1", reordered, available_at=2)
            self.assertEqual(clock.calls, 0)

            defaulted = spool.enqueue("m2", None)
            self.assertEqual(defaulted["available_at"], 7)
            self.assertEqual(clock.calls, 1)
            clock.set(999)
            self.assertEqual(spool.enqueue("m2", None), defaulted)
            self.assertEqual(clock.calls, 1, "an omitted idempotent retry must not read the clock")
            spool.enqueue("m3", False, available_at=0)
            with expect_domain_error(self, SpoolError, "CAPACITY_EXCEEDED"):
                spool.enqueue("m4", {}, available_at=0)
            self.assertEqual(len(spool.list_messages()), 3)

            terminal_capacity = make_spool(
                workspace / "terminal-capacity",
                FakeClock(0),
                limits=Limits(max_messages=1),
            )
            terminal_capacity.initialize()
            terminal_capacity.enqueue("terminal", {}, available_at=0)
            terminal_claim = terminal_capacity.claim("worker")
            terminal_capacity.ack("terminal", terminal_claim["lease_token"])
            with expect_domain_error(self, SpoolError, "CAPACITY_EXCEEDED"):
                terminal_capacity.enqueue("still-full", {}, available_at=0)

            validation = make_spool(workspace / "validation", FakeClock())
            validation.initialize()
            invalid_requests = [
                ("", {}, 0),
                ("   ", {}, 0),
                (3, {}, 0),
                ("bad-nan", math.nan, 0),
                ("bad-inf", math.inf, 0),
                ("bad-key", {1: "not-a-string-key"}, 0),
                ("bad-tuple", (1, 2), 0),
                ("bad-set", {1, 2}, 0),
                ("bad-time", {}, True),
                ("bad-time-2", {}, math.nan),
            ]
            for message_id, value, available_at in invalid_requests:
                with self.subTest(message_id=message_id, payload=value, available_at=available_at):
                    with expect_domain_error(self, SpoolError, "INVALID_INPUT"):
                        validation.enqueue(message_id, value, available_at=available_at)

            invalid_clock = make_spool(workspace / "invalid-clock", FakeClock(math.nan))
            invalid_clock.initialize()
            with expect_domain_error(self, SpoolError, "INVALID_DEPENDENCY"):
                invalid_clock.enqueue("clocked", {})
            self.assertEqual(invalid_clock.list_messages(), [])

            exact_root = workspace / "exact-bytes"
            exact = make_spool(exact_root, FakeClock(), limits=Limits(max_payload_bytes=4))
            exact.initialize()
            accepted = exact.enqueue("unicode", "é", available_at=0)
            self.assertEqual(accepted["payload"], "é")
            too_small_root = workspace / "too-small"
            too_small = make_spool(too_small_root, FakeClock(), limits=Limits(max_payload_bytes=3))
            too_small.initialize()
            with expect_domain_error(self, SpoolError, "INVALID_INPUT"):
                too_small.enqueue("unicode", "é", available_at=0)


class SpoolClaimTests(unittest.TestCase):
    def test_claim_order_attempt_and_token_privacy(self) -> None:
        with temporary_workspace("spool-claim-") as workspace:
            clock = FakeClock(10)
            root = workspace / "spool"
            spool = make_spool(root, clock, limits=Limits(lease_seconds=4))
            spool.initialize()
            spool.enqueue("sequence-first", {"n": 1}, available_at=5)
            spool.enqueue("sequence-second", {"n": 2}, available_at=5)
            spool.enqueue("earliest", {"n": 3}, available_at=4)
            spool.enqueue("future", {"n": 4}, available_at=11)

            expected = ["earliest", "sequence-first", "sequence-second"]
            tokens: set[str] = set()
            for message_id in expected:
                claimed = spool.claim(" worker ")
                claimed = assert_message_schema(self, claimed, status="leased", claimed=True)
                self.assertEqual(claimed["message_id"], message_id)
                self.assertEqual(claimed["attempts"], 1)
                tokens.add(claimed["lease_token"])
                public = assert_message_schema(self, spool.get(message_id), status="leased")
                self.assertNotIn("lease_token", public)
                self.assertNotIn("worker_id", public)
                self.assertNotIn("expires_at", public)
            self.assertEqual(len(tokens), 3)

            state_before = (root / "state.json").read_bytes()
            self.assertIsNone(spool.claim("worker-none"))
            self.assertEqual((root / "state.json").read_bytes(), state_before)
            before_calls = clock.calls
            for worker in ("", "\t", 5):
                with self.subTest(worker=worker):
                    with expect_domain_error(self, SpoolError, "INVALID_INPUT"):
                        spool.claim(worker)
            self.assertEqual(clock.calls, before_calls)

    def test_concurrent_claim_has_single_winner(self) -> None:
        with temporary_workspace("spool-concurrency-") as workspace:
            root = workspace / "shared"
            spool = make_spool(root, FakeClock(50), limits=Limits(lease_seconds=30))
            spool.initialize()
            spool.enqueue("only-message", {"work": True}, available_at=0)
            rows = [(str(root), f"worker-{index}") for index in range(6)]
            results = run_spawn_race(spool_claim_worker, rows)
            self.assertEqual([result for result in results if result[0] == "worker_error"], [])
            lock_timeouts = [result for result in results if result[0] == "error"]
            self.assertLessEqual({result[1] for result in lock_timeouts}, {"LOCK_TIMEOUT"})
            successful_claims = [result[1] for result in results if result[0] == "ok" and result[1] is not None]
            empty_claims = [result for result in results if result[0] == "ok" and result[1] is None]
            self.assertEqual(len(successful_claims), 1, results)
            self.assertEqual(len(empty_claims) + len(lock_timeouts), 5, results)
            winner = assert_message_schema(self, successful_claims[0], status="leased", claimed=True)
            self.assertEqual(winner["message_id"], "only-message")
            self.assertEqual(winner["attempts"], 1)
            persisted = assert_message_schema(self, make_spool(root, FakeClock(50)).get("only-message"), status="leased")
            self.assertEqual(persisted["attempts"], 1)


class SpoolOutcomeTests(unittest.TestCase):
    def test_ack_and_fail_require_exact_token(self) -> None:
        with temporary_workspace("spool-auth-") as workspace:
            root = workspace / "spool"
            clock = FakeClock(0)
            spool = make_spool(root, clock)
            spool.initialize()
            spool.enqueue("ack-me", {"kind": "ack"}, available_at=0)
            spool.enqueue("fail-me", {"kind": "fail"}, available_at=0)
            ack_claim = spool.claim("worker-a")
            ack_token = ack_claim["lease_token"]
            state_before = (root / "state.json").read_bytes()
            calls_before = clock.calls
            for token in ("", "wrong", ack_token + "x"):
                with self.subTest(ack_token=token):
                    with expect_domain_error(self, SpoolError, "UNAUTHORIZED"):
                        spool.ack("ack-me", token)
                    self.assertEqual((root / "state.json").read_bytes(), state_before)
                    self.assertEqual(clock.calls, calls_before)
            acked = assert_message_schema(self, spool.ack("ack-me", ack_token), status="acked")
            self.assertEqual(acked["last_error"], None)
            with expect_domain_error(self, SpoolError, "INVALID_STATE"):
                spool.ack("ack-me", ack_token)

            fail_claim = spool.claim("worker-b")
            fail_token = fail_claim["lease_token"]
            state_before = (root / "state.json").read_bytes()
            calls_before = clock.calls
            with expect_domain_error(self, SpoolError, "UNAUTHORIZED"):
                spool.fail("fail-me", "not-the-token", "delivery failed")
            self.assertEqual((root / "state.json").read_bytes(), state_before)
            self.assertEqual(clock.calls, calls_before)
            for bad_error in ("", 5):
                with self.subTest(error=bad_error):
                    with expect_domain_error(self, SpoolError, "INVALID_INPUT"):
                        spool.fail("fail-me", fail_token, bad_error)
            failed = assert_message_schema(
                self,
                spool.fail("fail-me", fail_token, " delivery failed "),
                status="pending",
            )
            self.assertEqual(failed["last_error"], " delivery failed ")
            self.assertNotIn("lease_token", failed)
            with expect_domain_error(self, SpoolError, "NOT_FOUND"):
                spool.ack("missing", "anything")

    def test_retry_delay_and_dead_transition(self) -> None:
        with temporary_workspace("spool-retry-") as workspace:
            root = workspace / "spool"
            clock = FakeClock(0)
            limits = Limits(max_attempts=2, lease_seconds=10, retry_delay=5)
            spool = make_spool(root, clock, limits=limits)
            spool.initialize()
            spool.enqueue("retry", {"job": 1}, available_at=0)

            first = assert_message_schema(self, spool.claim("worker"), status="leased", claimed=True)
            self.assertEqual(first["attempts"], 1)
            failed_once = assert_message_schema(
                self,
                spool.fail("retry", first["lease_token"], "first failure"),
                status="pending",
            )
            self.assertEqual(failed_once["available_at"], 5)
            self.assertEqual(failed_once["last_error"], "first failure")
            clock.set(4.999)
            state_before = (root / "state.json").read_bytes()
            self.assertIsNone(spool.claim("worker"))
            self.assertEqual((root / "state.json").read_bytes(), state_before)
            clock.set(5)
            second = assert_message_schema(self, spool.claim("worker"), status="leased", claimed=True)
            self.assertEqual(second["attempts"], 2)
            calls_before_dead = clock.calls
            dead = assert_message_schema(
                self,
                spool.fail("retry", second["lease_token"], "final failure"),
                status="dead",
            )
            self.assertEqual(dead["attempts"], 2)
            self.assertEqual(dead["last_error"], "final failure")
            self.assertEqual(clock.calls, calls_before_dead)
            self.assertIsNone(spool.claim("worker"))


class SpoolRecoveryTests(unittest.TestCase):
    def test_recover_boundary_and_idempotency(self) -> None:
        with temporary_workspace("spool-recovery-") as workspace:
            root = workspace / "spool"
            clock = FakeClock(10)
            spool = make_spool(
                root,
                clock,
                limits=Limits(max_attempts=2, lease_seconds=5, retry_delay=100),
            )
            spool.initialize()
            spool.enqueue("one", 1, available_at=0)
            spool.enqueue("two", 2, available_at=0)
            spool.claim("worker-one")
            spool.claim("worker-two")

            clock.set(14.999)
            self.assertEqual(spool.recover(), {"recovered": 0})
            self.assertEqual(spool.get("one")["status"], "leased")
            clock.set(15)
            self.assertEqual(spool.recover(), {"recovered": 2})
            for message_id in ("one", "two"):
                recovered = assert_message_schema(self, spool.get(message_id), status="pending")
                self.assertEqual(recovered["available_at"], 15)
                self.assertEqual(recovered["attempts"], 1)
            state_after = (root / "state.json").read_bytes()
            self.assertEqual(spool.recover(), {"recovered": 0})
            self.assertEqual((root / "state.json").read_bytes(), state_after)

            spool.claim("worker-one-again")
            spool.claim("worker-two-again")
            clock.set(19.999)
            self.assertEqual(spool.recover(), {"recovered": 0})
            clock.set(20)
            self.assertEqual(spool.recover(), {"recovered": 2})
            for message_id in ("one", "two"):
                dead = assert_message_schema(self, spool.get(message_id), status="dead")
                self.assertEqual(dead["attempts"], 2)
            self.assertEqual(spool.recover(), {"recovered": 0})

    def test_reopen_get_and_sorted_list(self) -> None:
        with temporary_workspace("spool-list-") as workspace:
            root = workspace / "spool"
            clock = FakeClock(5)
            spool = make_spool(root, clock)
            spool.initialize()
            late = spool.enqueue("late", {"position": 3}, available_at=3)
            first_sequence = spool.enqueue("z-at-one", {"position": 1}, available_at=1)
            second_sequence = spool.enqueue("a-at-one", {"position": 2}, available_at=1)
            claim = spool.claim("worker")
            self.assertEqual(claim["message_id"], "z-at-one")
            acked = spool.ack("z-at-one", claim["lease_token"])

            expected = [acked, second_sequence, late]
            self.assertEqual(spool.list_messages(), expected)
            self.assertEqual(spool.list_messages("pending"), [second_sequence, late])
            self.assertEqual(spool.list_messages("acked"), [acked])
            reopened = make_spool(root, FakeClock(999))
            self.assertEqual(reopened.list_messages(), expected)
            for message in expected:
                self.assertEqual(reopened.get(message["message_id"]), message)
                assert_message_schema(self, message, status=message["status"])

            for status in ("bogus", "", 1, True):
                with self.subTest(status=status):
                    with expect_domain_error(self, SpoolError, "INVALID_INPUT"):
                        reopened.list_messages(status)
            with expect_domain_error(self, SpoolError, "NOT_FOUND"):
                reopened.get("unknown")
            for message_id in ("", "  ", 1):
                with self.subTest(message_id=message_id):
                    with expect_domain_error(self, SpoolError, "INVALID_INPUT"):
                        reopened.get(message_id)


if __name__ == "__main__":
    unittest.main()
