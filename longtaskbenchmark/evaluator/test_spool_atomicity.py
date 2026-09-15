from __future__ import annotations

import json
import multiprocessing
from pathlib import Path
import unittest

from src.delivery_spool import Limits, Spool, SpoolError

from closed_loop_v2.evaluator._support import (
    FakeClock,
    assert_message_schema,
    assert_spool_root_boundary,
    expect_domain_error,
    spool_hold_lock_worker,
    temporary_workspace,
)


class InjectedFailure(RuntimeError):
    """Distinct failure used to verify that failpoints are re-raised unchanged."""


class RecordingFailpoint:
    def __init__(self, target: str):
        self.target = target
        self.calls: list[str] = []

    def __call__(self, name: str) -> None:
        self.calls.append(name)
        if name == self.target:
            raise InjectedFailure(name)


class SpoolAtomicityTests(unittest.TestCase):
    def test_failpoints_cleanup_and_old_or_new_state(self) -> None:
        with temporary_workspace("spool-atomicity-") as workspace:
            outside_marker = workspace / "outside-marker.txt"
            outside_marker.write_text("unchanged", encoding="utf-8")

            expected_calls = {
                "after_lock": ["after_lock"],
                "before_replace": ["after_lock", "before_replace"],
                "after_replace": ["after_lock", "before_replace", "after_replace"],
            }
            for target in ("after_lock", "before_replace", "after_replace"):
                with self.subTest(enqueue_failpoint=target):
                    root = workspace / f"enqueue-{target}"
                    baseline_spool = Spool(
                        root,
                        clock=FakeClock(0),
                        limits=Limits(),
                        lock_timeout=5.0,
                        failpoint=None,
                    )
                    baseline_spool.initialize()
                    baseline = baseline_spool.enqueue("old", {"version": "old"}, available_at=0)
                    old_bytes = (root / "state.json").read_bytes()
                    failpoint = RecordingFailpoint(target)
                    failing = Spool(
                        root,
                        clock=FakeClock(1),
                        limits=Limits(),
                        lock_timeout=5.0,
                        failpoint=failpoint,
                    )
                    with self.assertRaisesRegex(InjectedFailure, target):
                        failing.enqueue("new", {"version": "new"}, available_at=1)
                    self.assertEqual(failpoint.calls, expected_calls[target])
                    state_bytes = (root / "state.json").read_bytes()
                    decoded = json.loads(state_bytes.decode("utf-8"))
                    self.assertEqual(set(decoded), {"schema_version", "next_sequence", "messages"})
                    self.assertEqual(decoded["schema_version"], 1)
                    self.assertTrue(state_bytes.endswith(b"\n"))
                    self.assertEqual(
                        state_bytes,
                        (
                            json.dumps(
                                decoded,
                                sort_keys=True,
                                separators=(",", ":"),
                                ensure_ascii=False,
                                allow_nan=False,
                            )
                            + "\n"
                        ).encode("utf-8"),
                    )

                    reopened = Spool(
                        root,
                        clock=FakeClock(2),
                        limits=Limits(),
                        lock_timeout=5.0,
                        failpoint=None,
                    )
                    visible = reopened.list_messages()
                    self.assertIn(len(visible), (1, 2))
                    self.assertEqual(visible[0], baseline)
                    if target in {"after_lock", "before_replace"}:
                        self.assertEqual(state_bytes, old_bytes)
                        self.assertEqual([message["message_id"] for message in visible], ["old"])
                    else:
                        self.assertNotEqual(state_bytes, old_bytes)
                        self.assertEqual([message["message_id"] for message in visible], ["old", "new"])
                        assert_message_schema(self, visible[1], status="pending")
                    assert_spool_root_boundary(self, root)

                    completed = reopened.enqueue("new", {"version": "new"}, available_at=1)
                    self.assertEqual(completed["message_id"], "new")
                    self.assertEqual([message["message_id"] for message in reopened.list_messages()], ["old", "new"])
                    assert_spool_root_boundary(self, root)

            for target in ("after_lock", "before_replace", "after_replace"):
                with self.subTest(initialize_failpoint=target):
                    root = workspace / f"initialize-{target}"
                    failpoint = RecordingFailpoint(target)
                    spool = Spool(
                        root,
                        clock=FakeClock(),
                        limits=Limits(),
                        lock_timeout=5.0,
                        failpoint=failpoint,
                    )
                    with self.assertRaisesRegex(InjectedFailure, target):
                        spool.initialize()
                    self.assertEqual(failpoint.calls, expected_calls[target])
                    state_path = root / "state.json"
                    if target == "after_replace":
                        self.assertTrue(state_path.is_file())
                        self.assertEqual(
                            state_path.read_text(encoding="utf-8"),
                            '{"messages":{},"next_sequence":1,"schema_version":1}\n',
                        )
                    else:
                        self.assertFalse(state_path.exists())
                    assert_spool_root_boundary(self, root)

                    clean = Spool(
                        root,
                        clock=FakeClock(),
                        limits=Limits(),
                        lock_timeout=5.0,
                        failpoint=None,
                    )
                    if target == "after_replace":
                        self.assertEqual(clean.list_messages(), [])
                    else:
                        self.assertIs(type(clean.initialize()), dict)
                    assert_spool_root_boundary(self, root)

            expected_statuses = {
                "claim": ("pending", "leased"),
                "ack": ("leased", "acked"),
                "fail": ("leased", "pending"),
                "recover": ("leased", "pending"),
            }
            for operation in ("claim", "ack", "fail", "recover"):
                for target in ("after_lock", "before_replace", "after_replace"):
                    with self.subTest(mutation=operation, failpoint=target):
                        root = workspace / f"{operation}-{target}"
                        limits = Limits(max_attempts=3, lease_seconds=1, retry_delay=2)
                        baseline_spool = Spool(
                            root,
                            clock=FakeClock(0),
                            limits=limits,
                            lock_timeout=5.0,
                            failpoint=None,
                        )
                        baseline_spool.initialize()
                        baseline_spool.enqueue("message", {"operation": operation}, available_at=0)
                        lease_token = None
                        if operation in {"ack", "fail", "recover"}:
                            lease_token = baseline_spool.claim("baseline-worker")["lease_token"]
                        old_bytes = (root / "state.json").read_bytes()

                        failpoint = RecordingFailpoint(target)
                        failing = Spool(
                            root,
                            clock=FakeClock(1),
                            limits=limits,
                            lock_timeout=5.0,
                            failpoint=failpoint,
                        )
                        with self.assertRaisesRegex(InjectedFailure, target):
                            if operation == "claim":
                                failing.claim("worker")
                            elif operation == "ack":
                                failing.ack("message", lease_token)
                            elif operation == "fail":
                                failing.fail("message", lease_token, "injected failure")
                            else:
                                failing.recover()
                        self.assertEqual(failpoint.calls, expected_calls[target])

                        state_bytes = (root / "state.json").read_bytes()
                        decoded = json.loads(state_bytes.decode("utf-8"))
                        self.assertEqual(
                            state_bytes,
                            (
                                json.dumps(
                                    decoded,
                                    sort_keys=True,
                                    separators=(",", ":"),
                                    ensure_ascii=False,
                                    allow_nan=False,
                                )
                                + "\n"
                            ).encode("utf-8"),
                        )
                        if target in {"after_lock", "before_replace"}:
                            self.assertEqual(state_bytes, old_bytes)
                            expected_status = expected_statuses[operation][0]
                        else:
                            self.assertNotEqual(state_bytes, old_bytes)
                            expected_status = expected_statuses[operation][1]

                        reopened = Spool(
                            root,
                            clock=FakeClock(2),
                            limits=limits,
                            lock_timeout=5.0,
                            failpoint=None,
                        )
                        visible = reopened.get("message")
                        assert_message_schema(self, visible, status=expected_status)
                        if operation == "claim" and target == "after_replace":
                            self.assertEqual(visible["attempts"], 1)
                        if operation == "fail" and target == "after_replace":
                            self.assertEqual(visible["last_error"], "injected failure")
                        if operation == "recover" and target == "after_replace":
                            self.assertEqual(visible["available_at"], 1)
                        assert_spool_root_boundary(self, root)

            lock_root = workspace / "held-lock"
            unlocked = Spool(
                lock_root,
                clock=FakeClock(),
                limits=Limits(),
                lock_timeout=5.0,
                failpoint=None,
            )
            unlocked.initialize()
            old_state = (lock_root / "state.json").read_bytes()
            context = multiprocessing.get_context("spawn")
            acquired_event = context.Event()
            release_event = context.Event()
            result_queue = context.Queue()
            holder = context.Process(
                target=spool_hold_lock_worker,
                args=(str(lock_root), acquired_event, release_event, result_queue),
            )
            holder.start()
            try:
                self.assertTrue(acquired_event.wait(timeout=15.0), "lock holder did not acquire in time")
                lock_path = lock_root / "state.lock"
                self.assertTrue(lock_path.is_file())
                lock_identity = lock_path.stat().st_ino
                contender = Spool(
                    lock_root,
                    clock=FakeClock(),
                    limits=Limits(),
                    lock_timeout=0.0,
                    failpoint=None,
                )
                with expect_domain_error(self, SpoolError, "LOCK_TIMEOUT"):
                    contender.list_messages()
                self.assertTrue(lock_path.is_file(), "a timed-out caller must not delete state.lock")
                self.assertEqual(lock_path.stat().st_ino, lock_identity)
                self.assertEqual((lock_root / "state.json").read_bytes(), old_state)
            finally:
                release_event.set()
                holder.join(timeout=15.0)
                if holder.is_alive():
                    holder.terminate()
                    holder.join(timeout=5.0)
            self.assertEqual(holder.exitcode, 0)
            self.assertEqual(result_queue.get(timeout=5.0), ("released",))
            result_queue.close()
            result_queue.join_thread()
            self.assertEqual(unlocked.list_messages(), [])
            assert_spool_root_boundary(self, lock_root)

            self.assertEqual(outside_marker.read_text(encoding="utf-8"), "unchanged")
            self.assertEqual(
                {path.name for path in workspace.iterdir() if path.is_file()},
                {"outside-marker.txt"},
            )


if __name__ == "__main__":
    unittest.main()
