from __future__ import annotations

import unittest

from closed_loop_v2.evaluator._support import (
    assert_cli_error,
    assert_cli_success,
    assert_message_schema,
    assert_spool_root_boundary,
    run_cli,
    temporary_workspace,
)


MODULE = "src.delivery_spool"


class SpoolCliTests(unittest.TestCase):
    def test_cli_protocol_and_root_boundary(self) -> None:
        with temporary_workspace("spool-cli-") as workspace:
            root = workspace / "spool"
            sibling = workspace / "must-not-change.txt"
            sibling.write_text("sentinel", encoding="utf-8")

            completed, decoded = run_cli(MODULE, "--root", root, "init")
            initialized = assert_cli_success(self, completed, decoded)
            self.assertIs(type(initialized), dict)
            assert_spool_root_boundary(self, root)

            completed, decoded = run_cli(
                MODULE,
                "--root",
                root,
                "enqueue",
                {"message_id": "will-fail", "payload": {"kind": "fail"}, "available_at": 0},
            )
            enqueued = assert_message_schema(
                self,
                assert_cli_success(self, completed, decoded),
                status="pending",
            )
            self.assertEqual(enqueued["sequence"], 1)

            completed, decoded = run_cli(
                MODULE,
                "--root",
                root,
                "show",
                {"message_id": "will-fail"},
            )
            self.assertEqual(assert_cli_success(self, completed, decoded), enqueued)

            completed, decoded = run_cli(
                MODULE,
                "--root",
                root,
                "claim",
                {"worker_id": "cli-worker"},
            )
            claimed = assert_message_schema(
                self,
                assert_cli_success(self, completed, decoded),
                status="leased",
                claimed=True,
            )
            completed, decoded = run_cli(
                MODULE,
                "--root",
                root,
                "fail",
                {
                    "message_id": "will-fail",
                    "lease_token": claimed["lease_token"],
                    "error": "cli failure",
                },
            )
            failed = assert_message_schema(
                self,
                assert_cli_success(self, completed, decoded),
                status="pending",
            )
            self.assertEqual(failed["last_error"], "cli failure")

            completed, decoded = run_cli(
                MODULE,
                "--root",
                root,
                "enqueue",
                {"message_id": "will-ack", "payload": [1, 2], "available_at": 0},
            )
            assert_cli_success(self, completed, decoded)
            completed, decoded = run_cli(
                MODULE,
                "--root",
                root,
                "claim",
                {"worker_id": "cli-worker"},
            )
            ack_claim = assert_message_schema(
                self,
                assert_cli_success(self, completed, decoded),
                status="leased",
                claimed=True,
            )
            self.assertEqual(ack_claim["message_id"], "will-ack")
            completed, decoded = run_cli(
                MODULE,
                "--root",
                root,
                "ack",
                {"message_id": "will-ack", "lease_token": ack_claim["lease_token"]},
            )
            acked = assert_message_schema(
                self,
                assert_cli_success(self, completed, decoded),
                status="acked",
            )

            completed, decoded = run_cli(MODULE, "--root", root, "recover")
            self.assertEqual(assert_cli_success(self, completed, decoded), {"recovered": 0})
            completed, decoded = run_cli(MODULE, "--root", root, "list")
            listed = assert_cli_success(self, completed, decoded)
            self.assertEqual([message["message_id"] for message in listed], ["will-ack", "will-fail"])
            self.assertEqual(listed[0], acked)
            completed, decoded = run_cli(
                MODULE,
                "--root",
                root,
                "list",
                {"status": "acked"},
            )
            self.assertEqual(assert_cli_success(self, completed, decoded), [acked])

            invalid_cases = [
                ("enqueue", None, ""),
                ("enqueue", None, " \t\n"),
                ("enqueue", None, "{bad-json}\n"),
                ("enqueue", None, "[]\n"),
                ("show", None, '{"message_id":"x"}\n{"message_id":"y"}\n'),
                ("enqueue", {"message_id": "missing-payload"}, None),
                ("enqueue", {"message_id": "x", "payload": {}, "unknown": 1}, None),
                ("claim", {"worker_id": True}, None),
                ("show", {}, None),
                ("list", {"status": "not-a-status"}, None),
                ("recover", {"extra": 1}, None),
                ("unknown-command", {}, None),
            ]
            for command, request, raw_input in invalid_cases:
                with self.subTest(command=command, request=request, raw_input=raw_input):
                    completed, decoded = run_cli(
                        MODULE,
                        "--root",
                        root,
                        command,
                        request,
                        raw_input=raw_input,
                    )
                    assert_cli_error(self, completed, decoded, exit_code=2, code="INVALID_INPUT")

            not_initialized = workspace / "not-initialized"
            completed, decoded = run_cli(
                MODULE,
                "--root",
                not_initialized,
                "show",
                {"message_id": "x"},
            )
            assert_cli_error(self, completed, decoded, exit_code=2, code="NOT_INITIALIZED")

            corrupt_root = workspace / "corrupt"
            corrupt_root.mkdir()
            (corrupt_root / "state.json").write_text("{broken\n", encoding="utf-8")
            completed, decoded = run_cli(MODULE, "--root", corrupt_root, "list")
            assert_cli_error(self, completed, decoded, exit_code=3, code="CORRUPT_STATE")
            self.assertEqual((corrupt_root / "state.json").read_text(encoding="utf-8"), "{broken\n")

            self.assertEqual(sibling.read_text(encoding="utf-8"), "sentinel")
            self.assertEqual(
                {path.name for path in workspace.iterdir() if path.is_file()},
                {"must-not-change.txt"},
            )
            assert_spool_root_boundary(self, root)
            assert_spool_root_boundary(self, corrupt_root)


if __name__ == "__main__":
    unittest.main()
