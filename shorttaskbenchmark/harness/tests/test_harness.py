import copy
import json
import pathlib
import tempfile
import unittest
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
HARNESS = ROOT / "harness"
EVALUATOR = ROOT / "evaluator"
FIXTURES = ROOT / "harness" / "tests" / "fixtures"
sys.path.insert(0, str(HARNESS))
sys.path.insert(0, str(EVALUATOR))

from build_report import build_report
from common import atomic_json, load_manifest, read_usage
from evaluate import evaluate
from freeze_assets import inventory
from instruction_audit import audit_workspace
from prepare_runs import prepare


class HarnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = load_manifest(ROOT)

    def test_manifest_drives_four_tasks_sixteen_criteria_and_twenty_eight_cells(self):
        self.assertEqual(len(self.manifest["tasks"]), 4)
        self.assertEqual(sum(len(task["criteria"]) for task in self.manifest["tasks"]), 16)
        self.assertEqual(sum(len(slots) for slots in self.manifest["conditions"].values()), 28)
        self.assertEqual([wave["id"] for wave in self.manifest["waves"]], ["G1-A", "G2-B", "G1-B", "G2-A"])
        self.assertTrue(self.manifest["ranking_axes"]["strict"])
        self.assertTrue(self.manifest["ranking_axes"]["lenient"])

    def test_atomic_json_refuses_second_write(self):
        with tempfile.TemporaryDirectory(prefix="short-test-", dir=str(ROOT / "runs")) as temporary:
            path = pathlib.Path(temporary) / "value.json"
            atomic_json(path, {"value": 1})
            with self.assertRaises(FileExistsError):
                atomic_json(path, {"value": 2})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["value"], 1)

    def test_usage_aggregates_json_and_jsonl_wire_records(self):
        with tempfile.TemporaryDirectory(prefix="short-usage-", dir=str(ROOT / "runs")) as temporary:
            directory = pathlib.Path(temporary)
            (directory / "usage.json").write_bytes((FIXTURES / "usage.json").read_bytes())
            (directory / "events.jsonl").write_bytes((FIXTURES / "events.jsonl").read_bytes())
            usage = read_usage(directory)
            self.assertTrue(usage["available"])
            self.assertEqual(usage["record_count"], 2)
            self.assertEqual(usage["input_tokens"], 5)
            self.assertEqual(usage["output_tokens"], 9)
            self.assertEqual(usage["total_tokens"], 14)

    def test_usage_reads_new_wire_schema_and_dedupes_repeated_payloads(self):
        payload = {"inputOther": 10, "inputCacheRead": 5, "inputCacheCreation": 2, "output": 3}
        with tempfile.TemporaryDirectory(prefix="short-wire-usage-", dir=str(ROOT / "runs")) as temporary:
            directory = pathlib.Path(temporary)
            lines = [
                json.dumps({"event": {"usage": payload}}),
                json.dumps({"usage": payload}),
            ]
            (directory / "events.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
            usage = read_usage(directory)
            self.assertTrue(usage["available"])
            self.assertEqual(usage["record_count"], 1)
            self.assertEqual(usage["input_tokens"], 10)
            self.assertEqual(usage["output_tokens"], 3)
            self.assertEqual(usage["cache_tokens"], 7)
            self.assertEqual(usage["total_tokens"], 20)

    def test_prepare_creates_cells_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory(prefix="short-prepare-", dir=str(ROOT / "runs")) as temporary:
            target = pathlib.Path(temporary) / "prepared"
            summary = prepare(target)
            self.assertEqual(summary["cell_count"], 28)
            self.assertTrue((target / "A" / "A01" / "TASKS.md").is_file())
            metadata = json.loads((target / "A" / "A01" / "cell.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["expected_model"], "MT/LongCat-2.0")
            with self.assertRaises(FileExistsError):
                prepare(target)

    def test_reference_evaluation_is_full_and_replayable(self):
        first = evaluate(ROOT, "A", "reference")
        second = evaluate(ROOT, "A", "reference")
        self.assertTrue(first["instruction_gate"])
        self.assertEqual(first["strict"]["OfficialCriterionCount"], 16)
        self.assertEqual(first["strict"]["StrictTaskCount"], 4)
        self.assertEqual(first["strict"]["ExtensionCapabilityCount"], 2)
        self.assertEqual(first["strict"]["ResourceCapabilityCount"], 2)
        self.assertEqual(first, second)

    def test_instruction_gate_reference_passes(self):
        audit = audit_workspace(ROOT / "validation" / "reference", self.manifest, ROOT)
        self.assertTrue(audit["passed"], audit["reasons"])

    def test_report_rebuilds_rankings_and_ab_delta(self):
        entry_a = evaluate(ROOT, "A", "reference")
        entry_b = copy.deepcopy(entry_a)
        entry_a.update({"condition": "A", "slot": "A01", "model": "candidate_01"})
        entry_b.update({"condition": "B", "slot": "B01", "model": "candidate_01"})
        with tempfile.TemporaryDirectory(prefix="short-report-", dir=str(ROOT / "runs")) as temporary:
            directory = pathlib.Path(temporary)
            (directory / "a.json").write_text(json.dumps(entry_a), encoding="utf-8")
            (directory / "b.json").write_text(json.dumps(entry_b), encoding="utf-8")
            first = build_report(directory)
            second = build_report(directory)
            self.assertEqual(first, second)
            self.assertEqual(first["entry_count"], 2)
            self.assertEqual(len(first["rankings"]["strict"]["A"]), 1)
            self.assertEqual(len(first["rankings"]["lenient"]["B"]), 1)
            self.assertEqual(len(first["ab_delta"]), 1)
            self.assertIn("RawCriterionCount", first["ab_delta"][0]["lenient"])
            self.assertEqual(first["planner"]["status"], "completed")
            self.assertEqual(first["planner"]["tokens"], 155201)
            self.assertFalse(first["formal_ranking_available"])

    def test_freeze_inventory_excludes_dynamic_roots_and_caches(self):
        inventory_value = inventory(ROOT, self.manifest)
        assets = inventory_value["assets"]
        self.assertIn("results/frozen-phi3-plan.md", assets)
        self.assertNotIn("asset-hashes.json", assets)
        self.assertFalse(any(path.startswith("runs/") for path in assets))
        self.assertFalse(any("__pycache__" in path for path in assets))


if __name__ == "__main__":
    unittest.main()
