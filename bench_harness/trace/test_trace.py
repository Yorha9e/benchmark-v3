"""Unit self-tests for the bench_harness trace pipeline.

Run from anywhere::

    python test_trace.py
    python -m unittest test_trace -v

Only the Python standard library is used.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(
    0,
    os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ),
)

from bench_harness.trace import (  # noqa: E402
    AgentTrajectory,
    DatasetExporter,
    EvaluationReport,
    MilestoneResult,
    TraceAnnotator,
    TraceCollector,
    TrajectoryNormalizer,
)


def _make_session(collector: TraceCollector) -> AgentTrajectory:
    collector.start_session("sess-1", "raft-3node", "k3-max")
    collector.record_user_turn("Implement a 3-node Raft log replicator.")
    collector.record_assistant_turn(
        content="I will create the node supervisors first.",
        thought="Need leader election before log replication.",
        tool_calls=[
            {"id": "call_1", "name": "write_file",
             "arguments": {"path": "node.py", "content": "..."}},
        ],
        tokens={"prompt": 100, "completion": 50, "reasoning": 20},
    )
    collector.record_tool_result(
        "call_1", "write_file", "wrote 120 bytes", "", 0, 12.5
    )
    collector.record_assistant_turn(
        content="Now restart the cluster.",
        thought="",
        tool_calls=[
            {"call_id": "call_2", "tool_name": "bash",
             "arguments": {"command": "pkill -9 node"}},
        ],
        tokens={"prompt_tokens": 200, "completion_tokens": 30},
    )
    collector.record_tool_result(
        "call_2", "bash", "", "kill: no such process", 1, 3.0
    )
    return collector.finish(wall_time_seconds=7.5)


def _passed_report() -> EvaluationReport:
    return EvaluationReport(
        task_id="raft-3node",
        model_id="k3-max",
        timestamp="2026-09-15T00:00:00Z",
        passed=True,
        final_reward=1.0,
        milestones=[
            MilestoneResult("M1", "elect leader", True, 1.0),
            MilestoneResult("M2", "replicate log", True, 1.0),
        ],
    )


def _failed_report() -> EvaluationReport:
    return EvaluationReport(
        task_id="raft-3node",
        model_id="k3-max",
        timestamp="2026-09-15T00:00:00Z",
        passed=False,
        final_reward=0.25,
        milestones=[
            MilestoneResult("M1", "elect leader", True, 1.0),
            MilestoneResult(
                "M2", "replicate log", False, 0.0,
                failure_reason="log mismatch after restart",
                diagnostics="replica diverged: kill -9 lost unflushed log",
            ),
        ],
    )


class CollectorTests(unittest.TestCase):
    def test_ingestion_builds_trajectory(self):
        collector = TraceCollector()
        trajectory = _make_session(collector)
        self.assertEqual(trajectory.session_id, "sess-1")
        self.assertEqual(trajectory.task_id, "raft-3node")
        self.assertEqual(trajectory.model_id, "k3-max")
        self.assertEqual(len(trajectory.turns), 3)
        roles = [t.role for t in trajectory.turns]
        self.assertEqual(roles[0], "user")
        self.assertEqual(roles[1], "assistant")
        self.assertEqual(roles[2], "assistant")
        # tool results attach to the issuing assistant turns
        self.assertEqual(len(trajectory.turns[1].tool_results), 1)
        self.assertEqual(trajectory.turns[1].tool_results[0].exit_code, 0)
        self.assertEqual(len(trajectory.turns[2].tool_results), 1)
        self.assertEqual(trajectory.turns[2].tool_results[0].exit_code, 1)
        # total = prompt + completion + reasoning across assistant turns
        self.assertEqual(trajectory.total_tokens, 100 + 50 + 20 + 200 + 30)
        self.assertAlmostEqual(trajectory.wall_time_seconds, 7.5)
        collector.close()

    def test_requires_start_session(self):
        collector = TraceCollector()
        with self.assertRaises(RuntimeError):
            collector.record_user_turn("hello")
        collector.close()

    def test_orphan_tool_result_creates_tool_turn(self):
        collector = TraceCollector()
        collector.start_session("s", "t", "m")
        record = collector.record_tool_result("ghost", "probe", "out")
        self.assertEqual(record.call_id, "ghost")
        trajectory = collector.get_trajectory()
        self.assertEqual(trajectory.turns[-1].role, "tool")
        collector.close()

    def test_wire_streaming_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            wire = os.path.join(tmp, "wire.jsonl")
            collector = TraceCollector(wire_path=wire)
            live = _make_session(collector)
            collector.close()
            self.assertTrue(os.path.exists(wire))
            replayed = TraceCollector.load_wire(wire)
            self.assertEqual(replayed.session_id, live.session_id)
            self.assertEqual(replayed.task_id, live.task_id)
            self.assertEqual(replayed.model_id, live.model_id)
            self.assertEqual(len(replayed.turns), len(live.turns))
            self.assertEqual(replayed.total_tokens, live.total_tokens)
            self.assertEqual(
                replayed.turns[2].tool_results[0].stderr,
                "kill: no such process",
            )

    def test_trajectory_save_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "traj.json")
            collector = TraceCollector()
            live = _make_session(collector)
            collector.save_trajectory(path)
            loaded = TraceCollector.load_trajectory(path)
            self.assertEqual(loaded.session_id, live.session_id)
            self.assertEqual(len(loaded.turns), len(live.turns))
            collector.close()


class NormalizerTests(unittest.TestCase):
    def setUp(self):
        collector = TraceCollector()
        self.trajectory = _make_session(collector)
        collector.close()

    def test_openai_tools_shape(self):
        messages = TrajectoryNormalizer.to_openai_tools(self.trajectory)
        self.assertEqual(messages[0]["role"], "user")
        assistant = messages[1]
        self.assertEqual(assistant["role"], "assistant")
        self.assertIn("reasoning_content", assistant)
        self.assertEqual(
            assistant["tool_calls"][0]["function"]["name"], "write_file"
        )
        args = json.loads(
            assistant["tool_calls"][0]["function"]["arguments"]
        )
        self.assertEqual(args["path"], "node.py")
        tool_msg = messages[2]
        self.assertEqual(tool_msg["role"], "tool")
        self.assertEqual(tool_msg["tool_call_id"], "call_1")

    def test_chatml_envelope(self):
        messages = TrajectoryNormalizer.to_chatml(self.trajectory)
        for message in messages:
            self.assertIn("role", message)
            self.assertIn("content", message)
        assistant = messages[1]
        self.assertIn("<think>", assistant["content"])
        self.assertIn("Need leader election", assistant["content"])

    def test_anthropic_shape(self):
        payload = TrajectoryNormalizer.to_anthropic(self.trajectory)
        self.assertIn("system", payload)
        self.assertIn("messages", payload)
        messages = payload["messages"]
        self.assertEqual(messages[0]["role"], "user")
        kinds = [
            block.get("type")
            for message in messages
            if isinstance(message.get("content"), list)
            for block in message["content"]
        ]
        self.assertIn("tool_use", kinds)
        self.assertIn("tool_result", kinds)
        self.assertIn("thinking", kinds)
        roles = [m["role"] for m in messages]
        for first, second in zip(roles, roles[1:]):
            self.assertNotEqual(first, second)

    def test_dispatcher_rejects_unknown_format(self):
        with self.assertRaises(ValueError):
            TrajectoryNormalizer.normalize(self.trajectory, fmt="nope")


class AnnotatorTests(unittest.TestCase):
    def setUp(self):
        collector = TraceCollector()
        self.trajectory = _make_session(collector)
        collector.close()
        self.annotator = TraceAnnotator()

    def test_passed_run_credit_assignment(self):
        annotated = self.annotator.annotate(
            self.trajectory, _passed_report()
        )
        self.assertTrue(annotated.passed)
        self.assertIsNone(annotated.failure_attribution_step)
        self.assertIsNone(annotated.failure_reason)
        self.assertEqual(
            len(annotated.turn_rewards), len(self.trajectory.turns)
        )
        # user turn gets no credit; scored turns split the reward
        self.assertEqual(annotated.turn_rewards[0], 0.0)
        self.assertAlmostEqual(sum(annotated.turn_rewards), 1.0)
        self.assertEqual(annotated.reward_tags[0], "context")
        self.assertIn("credit", annotated.reward_tags)
        self.assertEqual(
            annotated.milestone_binding["M1"]["bound_turn"], None
        )

    def test_failed_run_failure_attribution(self):
        annotated = self.annotator.annotate(
            self.trajectory, _failed_report()
        )
        self.assertFalse(annotated.passed)
        # call_2 failed with exit 1 on turn index 2
        self.assertEqual(annotated.failure_attribution_step, 2)
        self.assertIsNotNone(annotated.failure_reason)
        self.assertIn("call_2", annotated.failure_reason or "")
        self.assertEqual(annotated.reward_tags[2], "blame")
        self.assertEqual(
            annotated.milestone_binding["M2"]["bound_turn"], 2
        )
        self.assertIsNone(
            annotated.milestone_binding["M1"]["bound_turn"]
        )


class ExporterTests(unittest.TestCase):
    def setUp(self):
        collector = TraceCollector()
        trajectory = _make_session(collector)
        collector.close()
        annotator = TraceAnnotator()
        self.good = annotator.annotate(trajectory, _passed_report())
        self.bad = annotator.annotate(trajectory, _failed_report())
        self.exporter = DatasetExporter()

    def _read_jsonl(self, path):
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def test_sft_golden_filters(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "sft.jsonl")
            stats = self.exporter.export_sft_golden(
                [self.good, self.bad], out
            )
            self.assertEqual(stats["written"], 1)
            self.assertEqual(stats["skipped_low_reward"], 1)
            rows = self._read_jsonl(out)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["session_id"], "sess-1")
            self.assertTrue(
                any(m["role"] == "tool" for m in rows[0]["messages"])
            )

    def test_sft_golden_bloat_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "sft.jsonl")
            stats = self.exporter.export_sft_golden(
                [self.good], out, max_tokens_per_milestone=1.0
            )
            self.assertEqual(stats["written"], 0)
            self.assertEqual(stats["skipped_bloated"], 1)
            self.assertEqual(self._read_jsonl(out), [])

    def test_dpo_explicit_pairs(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "dpo.jsonl")
            stats = self.exporter.export_dpo_pairs(
                [(self.good, self.bad)], out
            )
            self.assertEqual(stats["written"], 1)
            (row,) = self._read_jsonl(out)
            self.assertEqual(row["task_id"], "raft-3node")
            self.assertIn("prompt", row)
            self.assertIn("chosen", row)
            self.assertIn("rejected", row)
            self.assertEqual(
                row["failure_attribution"]["step"],
                self.bad.failure_attribution_step,
            )
            # prompt is the common message prefix of both sides
            self.assertTrue(len(row["prompt"]) >= 1)
            self.assertEqual(
                row["chosen"][: len(row["prompt"])], row["prompt"]
            )

    def test_dpo_auto_pairing(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "dpo.jsonl")
            stats = self.exporter.export_dpo_pairs(
                [self.good, self.bad], out
            )
            self.assertEqual(stats["written"], 1)
            (row,) = self._read_jsonl(out)
            self.assertGreater(
                row["chosen_reward"], row["rejected_reward"]
            )

    def test_dpo_skips_ties(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "dpo.jsonl")
            stats = self.exporter.export_dpo_pairs(
                [self.good, self.good], out
            )
            self.assertEqual(stats["written"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
