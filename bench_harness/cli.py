"""bench-run: unified CLI entry point for all benchmark_v3 suites.

Usage::

    bench-run --suite {short,long,reviewer,critic,all} --model <model_id>
        [--driver openai|google|anthropic|cli|mock] [--task <task_id>]
        [--resume] [--output <dir>] [--export-sft <path>]
        [--export-dpo <path>] [--quiet]

For every selected ``(suite, task)`` the CLI runs
``SuiteAdapter.run_session`` with terminal progress rendering
(:class:`ProgressReporter`), persists atomic per-task reports
(``evaluation.json`` / ``summary.json``) plus the aggregate
``summary.json`` and ``live_status.json`` at the output root, and
optionally exports SFT golden trajectories and DPO preference pairs
from the annotated traces. Wall-clock time stays pure telemetry.
"""

from __future__ import annotations

import os
import sys
# Ensure both repo root and benchmark_v3 parent are in sys.path
_this_dir = os.path.dirname(os.path.abspath(__file__))
_bench_v3_dir = os.path.dirname(_this_dir)
_repo_root = os.path.dirname(_bench_v3_dir)
for _p in [_repo_root, _bench_v3_dir]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = ["build_arg_parser", "build_driver", "main", "self_test"]


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bench-run", description="Benchmark v3 unified harness runner.")
    parser.add_argument("--suite", default="all",
                        choices=["short", "long", "reviewer", "critic", "all"])
    parser.add_argument("--model", required=True, help="Model id (e.g. k3-max).")
    parser.add_argument("--driver", default="openai",
                        choices=["openai", "google", "anthropic", "cli", "mock"])
    parser.add_argument("--task", default=None, help="Run a single task id only.")
    parser.add_argument("--resume", action="store_true",
                        help="Replay stalled snapshot turns instead of restarting.")
    parser.add_argument("--output", default=None,
                        help="Output root (default: ./bench_runs/<timestamp>).")
    parser.add_argument("--export-sft", default=None, metavar="PATH",
                        help="Export SFT golden JSONL to PATH.")
    parser.add_argument("--export-dpo", default=None, metavar="PATH",
                        help="Export DPO preference-pair JSONL to PATH.")
    parser.add_argument("--quiet", action="store_true", help="Suppress terminal rendering.")
    return parser


def build_driver(driver_name: str, model_id: str):  # noqa: ANN202
    """Instantiate a model driver (lazy imports; no hard SDK dependency)."""
    if driver_name == "openai":
        from benchmark_v3.bench_harness.drivers.openai_driver import OpenAIDriver

        return OpenAIDriver(model_id)
    if driver_name == "google":
        from benchmark_v3.bench_harness.drivers.google_driver import GoogleGenAIDriver

        return GoogleGenAIDriver(model_id)
    if driver_name == "anthropic":
        from benchmark_v3.bench_harness.drivers.anthropic_driver import AnthropicDriver

        return AnthropicDriver(model_id)
    if driver_name == "cli":
        from benchmark_v3.bench_harness.drivers.agent_cli_driver import AgentCLIDriver

        return AgentCLIDriver(model_id)
    if driver_name == "mock":
        from benchmark_v3.bench_harness.suites.base import ScriptedDriver

        return ScriptedDriver(model_id)
    raise ValueError("unknown driver %r" % (driver_name,))


def _default_output() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path("bench_runs") / stamp


def main(argv: list[str] | None = None) -> int:
    from benchmark_v3.bench_harness.core.report import ReportManager
    from benchmark_v3.bench_harness.core.reporter import ProgressReporter
    from benchmark_v3.bench_harness.suites import SUITE_REGISTRY

    args = build_arg_parser().parse_args(argv)
    output = Path(args.output) if args.output else _default_output()
    output.mkdir(parents=True, exist_ok=True)

    reporter = ProgressReporter(output / "live_status.json", enabled=not args.quiet)
    suite_names = sorted(SUITE_REGISTRY) if args.suite == "all" else [args.suite]
    total_tasks = sum(
        len([t for t in SUITE_REGISTRY[name].TASK_IDS if not args.task or t == args.task])
        for name in suite_names
    )
    reporter.start_session("bench-run/%s" % args.model, total_tasks=total_tasks)

    try:
        driver = build_driver(args.driver, args.model)
    except Exception as exc:
        print("error: cannot build driver: %s" % exc, file=sys.stderr)
        return 2

    reports: list[Any] = []
    started = time.monotonic()
    for suite_name in suite_names:
        suite = SUITE_REGISTRY[suite_name]()
        task_ids = [t for t in suite.task_ids() if not args.task or t == args.task]
        if args.task and not task_ids:
            print("error: unknown task %r for suite %s" % (args.task, suite_name),
                  file=sys.stderr)
            return 2
        for task_id in task_ids:
            reporter.update(task_id, "running", "suite=%s model=%s" % (suite_name, args.model))
            try:
                report = suite.run_session(task_id, args.model, driver, output,
                                           resume=args.resume)
            except Exception as exc:  # never let one task kill the sweep
                print("task %s crashed: %r" % (task_id, exc), file=sys.stderr)
                continue
            reports.append(report)
            reporter.complete_task(
                "%s/%s" % (suite_name, task_id), report.passed,
                "reward=%s" % (report.final_reward,))

    manager = ReportManager(output)
    summary = ReportManager.build_summary(reports)
    summary["model_id"] = args.model
    summary["driver"] = args.driver
    summary["wall_time_seconds"] = time.monotonic() - started  # telemetry only
    manager.save_summary(summary)

    if (args.export_sft or args.export_dpo) and reports:
        _export_datasets(output, reports, args.export_sft, args.export_dpo)

    reporter.finish("bench-run done: %d/%d passed"
                    % (sum(1 for r in reports if r.passed), len(reports)))
    _print_table(reports, quiet=args.quiet)
    return 0 if reports and all(r.passed for r in reports) else (0 if reports else 1)


def _export_datasets(output: Path, reports: list[Any],
                     sft_path: str | None, dpo_path: str | None) -> None:
    from benchmark_v3.bench_harness.trace.annotator import TraceAnnotator
    from benchmark_v3.bench_harness.trace.collector import TraceCollector
    from benchmark_v3.bench_harness.trace.exporter import DatasetExporter

    by_task = {(r.task_id, r.model_id): r for r in reports}
    annotated = []
    for trajectory_file in sorted(output.glob("*/*/trajectory.json")):
        try:
            trajectory = TraceCollector.load_trajectory(trajectory_file)
        except (OSError, ValueError):
            continue
        report = by_task.get((trajectory.task_id, trajectory.model_id))
        if report is None:
            continue
        try:
            annotated.append(TraceAnnotator().annotate(trajectory, report))
        except Exception:
            continue
    exporter = DatasetExporter()
    if sft_path:
        stats = exporter.export_sft_golden(annotated, sft_path)
        print("sft export: %s" % json.dumps(stats))
    if dpo_path:
        stats = exporter.export_dpo_pairs(annotated, dpo_path)
        print("dpo export: %s" % json.dumps(stats))


def _print_table(reports: list[Any], quiet: bool = False) -> None:
    if quiet:
        return
    print("%-28s %-8s %-10s %-10s" % ("task", "passed", "reward", "tokens"))
    for report in reports:
        print("%-28s %-8s %-10s %-10d" % (
            report.task_id, "yes" if report.passed else "no",
            ("%.1f" % report.final_reward) if report.final_reward <= 100 else ("%.3f" % report.final_reward),
            report.token_metrics.total_tokens))


def self_test() -> tuple[int, int]:
    """Run module self-tests. Returns ``(passed, failed)`` counts."""
    import tempfile

    counts = [0, 0]

    def check(name: str, cond: bool) -> None:
        counts[0 if cond else 1] += 1
        print(f"{'PASS' if cond else 'FAIL'} cli::{name}", flush=True)

    parser = build_arg_parser()
    args = parser.parse_args(["--suite", "critic", "--model", "t",
                              "--driver", "mock", "--task", "audit_bundle"])
    check("arg_parse", args.suite == "critic" and args.task == "audit_bundle"
          and args.model == "t" and not args.resume)

    driver = build_driver("mock", "t")
    check("mock_driver", type(driver).__name__ == "ScriptedDriver")
    try:
        build_driver("nope", "t")
        check("bad_driver_rejected", False)
    except ValueError:
        check("bad_driver_rejected", True)

    # -- end-to-end offline sweep (mock writes nothing; pipeline must
    #    complete honestly with atomic reports + dataset exports) --
    with tempfile.TemporaryDirectory(prefix="cli-e2e-") as tmp:
        out = str(Path(tmp) / "runs")
        code = main(["--suite", "reviewer", "--task", "bait_guard",
                     "--model", "mock-model", "--driver", "mock",
                     "--output", out, "--quiet",
                     "--export-sft", str(Path(tmp) / "sft.jsonl"),
                     "--export-dpo", str(Path(tmp) / "dpo.jsonl")])
        check("e2e_exit", code in (0, 1))
        check("e2e_evaluation", (Path(out) / "reviewer" / "bait_guard" / "evaluation.json").exists())
        check("e2e_summary", (Path(out) / "summary.json").exists())
        check("e2e_live", (Path(out) / "live_status.json").exists())
        check("e2e_sft", (Path(tmp) / "sft.jsonl").exists())
        check("e2e_dpo", (Path(tmp) / "dpo.jsonl").exists())
        try:
            summary = json.loads((Path(out) / "summary.json").read_text(encoding="utf-8"))
            check("e2e_summary_shape", summary.get("n_reports") == 1 and "entries" in summary)
        except (OSError, ValueError):
            check("e2e_summary_shape", False)

    return counts[0], counts[1]


if __name__ == "__main__":
    raise SystemExit(main())
