"""bench-run: unified CLI entry point for all benchmark_v3 suites.

Usage::

    bench-run --suite {catalog-key|all} [--suite another] --model <model_id>
        [--driver openai|response|google|anthropic|cli|mock] [--task <task_id>]
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
    parser.add_argument("-i", "--interactive", action="store_true",
                        help="Launch interactive TUI wizard.")
    from benchmark_v3.bench_harness.suites.catalog import CLI_SUITE_CHOICES

    parser.add_argument(
        "--suite",
        action="append",
        choices=list(CLI_SUITE_CHOICES),
        metavar="KEY",
        help="Runnable to execute. Repeat to combine in one run "
             "(default: all A-condition families).",
    )
    parser.add_argument("--model", required=False, default=None,
                        help="Model id (e.g. k3-max).")
    parser.add_argument("--driver", default="openai",
                        choices=["openai", "response", "google", "anthropic", "cli", "mock"])
    parser.add_argument("--base-url", default=None,
                        help="Base URL for the model under test (supports OpenAI, Response, Google, Anthropic).")
    parser.add_argument("--api-key", default=None,
                        help="API key for the model under test.")
    parser.add_argument("--effort", default=None,
                        choices=["none", "minimal", "low", "medium", "high", "xhigh", "max"],
                        help="Reasoning effort (protocol-native: none/minimal/low/medium/high/xhigh/max).")
    parser.add_argument("--task", default=None,
                        help="Run a single task id only. Must belong to one of the "
                             "selected --suite keys (an unknown pairing is an error, "
                             "not a silent no-op).")
    parser.add_argument("--tasks", default=None, metavar="ID[,ID...]",
                        help="Run several task ids in one go (comma-separated). "
                             "Each must belong to a selected --suite.")
    parser.add_argument("--on-upstream-error", default="pause",
                        choices=["pause", "continue"],
                        help="What to do when the model upstream fails (5xx / key "
                             "cooldown / timeout) and a task aborts before any turn: "
                             "'pause' (default) stops the sweep and keeps the run "
                             "resumable, 'continue' grinds through the remaining "
                             "tasks (each such task is recorded as ABORTED, not 0).")
    parser.add_argument("--resume", action="store_true",
                        help="Replay stalled snapshot turns instead of restarting.")
    parser.add_argument("--output", default=None,
                        help="Output root (default: ./bench_runs/<timestamp>).")
    parser.add_argument(
        "--continue-run",
        action="store_true",
        help="Resume an L1-paused run in --output: skip tasks that already have "
             "evaluation.json and keep the saved run_manifest.json queue.",
    )
    parser.add_argument("--export-sft", default=None, metavar="PATH",
                        help="Export SFT golden JSONL to PATH.")
    parser.add_argument("--export-dpo", default=None, metavar="PATH",
                        help="Export DPO preference-pair JSONL to PATH.")
    parser.add_argument("--judge-model", default=None,
                        help="Expert judge model id for Critic depth rubric (e.g. k3-max, gpt-4o).")
    parser.add_argument("--judge-driver", default=None,
                        choices=["openai", "response", "google", "anthropic", "mock"],
                        help="Expert judge protocol driver (defaults to --driver).")
    parser.add_argument("--judge-base-url", default=None,
                        help="Base URL for the expert judge model.")
    parser.add_argument("--judge-api-key", default=None,
                        help="API key for the expert judge model.")
    parser.add_argument("--judge-effort", default=None,
                        choices=["none", "minimal", "low", "medium", "high", "xhigh", "max"],
                        help="Reasoning effort for the expert judge model.")
    parser.add_argument("--on-regress", default="keep-best",
                        choices=["keep-best", "overwrite", "ask"],
                        help="Leaderboard slot policy when a rerun scores below the stored "
                             "best: keep-best (default), overwrite, or ask interactively.")
    parser.add_argument("--quiet", action="store_true", help="Suppress terminal rendering.")
    return parser


def build_driver(
    driver_name: str,
    model_id: str,
    effort: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
):  # noqa: ANN202
    """Instantiate a model driver (lazy imports; no hard SDK dependency)."""
    if driver_name == "openai":
        from benchmark_v3.bench_harness.drivers.openai_driver import OpenAIDriver

        return OpenAIDriver(model_id, api_key=api_key, base_url=base_url, effort=effort)
    if driver_name == "response":
        from benchmark_v3.bench_harness.drivers.response_driver import ResponseDriver

        return ResponseDriver(model_id, api_key=api_key, base_url=base_url, effort=effort)
    if driver_name == "google":
        from benchmark_v3.bench_harness.drivers.google_driver import GoogleGenAIDriver

        return GoogleGenAIDriver(model_id, api_key=api_key, base_url=base_url, effort=effort)
    if driver_name == "anthropic":
        from benchmark_v3.bench_harness.drivers.anthropic_driver import AnthropicDriver

        return AnthropicDriver(model_id, api_key=api_key, base_url=base_url, effort=effort)
    if driver_name == "cli":
        from benchmark_v3.bench_harness.drivers.agent_cli_driver import AgentCLIDriver

        return AgentCLIDriver(model_id, effort=effort)
    if driver_name == "mock":
        from benchmark_v3.bench_harness.suites.base import ScriptedDriver, scripted_finish

        # Default mock ends via `finish` so unbounded agent loops cannot hang
        # offline self-tests after max_turns was removed.
        return ScriptedDriver(
            model_id,
            effort=effort,
            script=[
                scripted_finish(
                    "Mock driver finished without implementing the deliverable."
                )
            ],
        )
    raise ValueError("unknown driver %r" % (driver_name,))


def _default_output() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path("bench_runs") / ("%s-%s" % (stamp, os.getpid()))


def main(argv: list[str] | None = None) -> int:
    raw_argv = sys.argv[1:] if argv is None else argv
    if len(raw_argv) == 0 or "-i" in raw_argv or "--interactive" in raw_argv:
        try:
            from benchmark_v3.bench_harness.tui import run_tui
        except ImportError:
            from bench_harness.tui import run_tui
        return run_tui()

    from benchmark_v3.bench_harness.core.report import ReportManager
    from benchmark_v3.bench_harness.core.reporter import ProgressReporter
    from benchmark_v3.bench_harness.suites import get_suite, resolve_suite_keys

    args = build_arg_parser().parse_args(argv)

    if getattr(args, "list_tasks", False):
        from benchmark_v3.bench_harness.core.run_manifest import known_task_ids
        from benchmark_v3.bench_harness.suites.catalog import SUITE_LABELS

        print("selectable suite -> task ids:")
        for key, task_ids in known_task_ids().items():
            print("  %-10s %-38s %s" % (key, "(" + SUITE_LABELS.get(key, "") + ")", ", ".join(task_ids)))
        print("\nexamples:")
        print("  --suite long --task saga_coordinator")
        print("  --suite short_b --tasks varint_parser,timing_wheel")
        return 0

    if not args.model:
        print("error: the following arguments are required: --model (or run with -i for interactive mode)",
              file=sys.stderr)
        return 2

    output = Path(args.output) if args.output else _default_output()
    output.mkdir(parents=True, exist_ok=True)

    from benchmark_v3.bench_harness.core.run_manifest import (
        TaskAbandoned,
        abandon_incomplete_task,
        build_planned_queue,
        clear_pause_request,
        known_task_ids,
        load_manifest,
        mark_task_completed,
        new_manifest,
        pause_requested,
        save_manifest,
        set_manifest_status,
        task_is_complete,
    )

    # L1 pause: Ctrl+C aborts the *current* task immediately and freezes the run.
    pause_flag = {"armed": False}

    def _on_sigint(_signum: int, _frame: Any) -> None:  # noqa: ANN401
        if pause_flag["armed"]:
            raise KeyboardInterrupt
        pause_flag["armed"] = True
        try:
            from benchmark_v3.bench_harness.core.run_manifest import request_pause

            request_pause(output)
        except Exception:
            pass
        msg = (
            "\n[pause] Ctrl+C — abandoning the current task (if any) and "
            f"freezing the run. Prior completed tasks are kept.\n"
            f"[pause] Sentinel: {output / 'PAUSE.request'}\n"
            "[pause] Press Ctrl+C again to hard-abort the process.\n"
        )
        sys.stderr.write(msg)
        sys.stderr.flush()
        raise KeyboardInterrupt

    try:
        import signal

        signal.signal(signal.SIGINT, _on_sigint)
    except Exception:
        pass

    reporter = ProgressReporter(output / "live_status.json", enabled=not args.quiet)
    suite_names = resolve_suite_keys(args.suite or ["all"])
    extra_tasks = [t.strip() for t in (args.tasks or "").split(",") if t.strip()]
    planned = build_planned_queue(
        suite_names, task_filter=args.task, task_filters=extra_tasks,
    )
    requested = ([args.task] if args.task else []) + extra_tasks
    if requested and not planned:
        # Explicit failure: never silently run nothing (or a wider sweep).
        available = known_task_ids()
        hint = ", ".join(
            "%s: %s" % (k, "/".join(v)) for k, v in available.items()
            if k in suite_names
        ) or "(no suites selected)"
        print(
            "error: task(s) %s do not belong to the selected suite(s) %s\n"
            "       available for this selection -> %s"
            % (", ".join(repr(t) for t in requested), suite_names, hint),
            file=sys.stderr,
        )
        return 2
    # A subset selection is legitimate but must never be mistaken for a full run.
    if requested and len(planned) < len(build_planned_queue(suite_names)):
        planned_ids = {p["task_id"] for p in planned}
        missing = sorted({
            t for s in suite_names
            for t in known_task_ids().get(s, [])
            if t not in planned_ids
        })
        print(
            "note: partial run — executing %d/%d task(s) of %s; not selected: %s"
            % (len(planned), len(build_planned_queue(suite_names)),
               "/".join(suite_names), ", ".join(missing) or "-"),
            file=sys.stderr if not args.quiet else sys.stderr,
        )
    total_tasks = len(planned)
    reporter.start_session("bench-run/%s" % args.model, total_tasks=total_tasks)

    launch_cfg = {
        "model": args.model,
        "model_id": args.model,
        "driver": args.driver,
        "effort": args.effort,
        "suites": list(suite_names),
        "task": args.task,
        "resume": bool(args.resume or args.continue_run),
        "base_url": args.base_url or "",
        "api_key": args.api_key or "",
        "judge_model": args.judge_model or "",
        "judge_driver": args.judge_driver or "",
        "judge_base_url": args.judge_base_url or "",
        "judge_api_key": args.judge_api_key or "",
        "judge_effort": args.judge_effort,
        "export_sft": args.export_sft or "",
        "export_dpo": args.export_dpo or "",
        "on_regress": args.on_regress,
    }

    existing = load_manifest(output) if (args.continue_run or args.resume) else None
    if existing and args.continue_run:
        # Keep original launch credentials/suites; refresh remaining from disk.
        launch_cfg = {**dict(existing.get("launch") or {}), **{
            k: v for k, v in launch_cfg.items() if v not in ("", None, [])
        }}
        # Prefer suites from existing planned order when CLI didn't narrow.
        if existing.get("planned") and not args.task and not args.suite:
            planned = [
                {"suite": str(i["suite"]), "task_id": str(i["task_id"])}
                for i in existing["planned"]
                if isinstance(i, dict) and i.get("suite") and i.get("task_id")
            ]
        manifest = new_manifest(output_dir=output, planned=planned, launch=launch_cfg)
        # Preserve created_at from prior pause.
        if existing.get("created_at"):
            manifest["created_at"] = existing["created_at"]
            save_manifest(output, manifest)
    else:
        manifest = new_manifest(output_dir=output, planned=planned, launch=launch_cfg)

    # Drop a leftover sentinel only when explicitly continuing a paused run.
    if args.continue_run:
        clear_pause_request(output)
    if not args.quiet:
        print(
            "L1 pause: Ctrl+C (or create %s) abandons the current task and "
            "keeps prior completed ones."
            % (output / "PAUSE.request",),
            flush=True,
        )

    try:
        # 当从中断目录恢复时，优先采用原有的 launch 参数，防止回退到 CLI 默认值
        effective_driver = args.driver if not args.continue_run else (launch_cfg.get("driver") or args.driver)
        effective_effort = args.effort if not args.continue_run else (launch_cfg.get("effort") or args.effort)
        effective_api_key = args.api_key if not args.continue_run else (launch_cfg.get("api_key") or args.api_key)
        effective_base_url = args.base_url if not args.continue_run else (launch_cfg.get("base_url") or args.base_url)
        driver = build_driver(
            effective_driver,
            args.model,
            effort=effective_effort,
            api_key=effective_api_key,
            base_url=effective_base_url,
        )
    except Exception as exc:
        print("error: cannot build driver: %s" % exc, file=sys.stderr)
        set_manifest_status(output, manifest, "interrupted", reason="driver_build_failed")
        return 2

    reports: list[Any] = []

    # 若继续未完成的评测，将已有完成任务的 evaluation.json 回载至 reports，保证全量统计与数据集导出不丢失
    if args.continue_run:
        from benchmark_v3.bench_harness.core.types import EvaluationReport
        for item in (manifest.get("planned") or []):
            s_name = str(item.get("suite") or "")
            t_name = str(item.get("task_id") or "")
            eval_file = output / s_name / t_name / "evaluation.json"
            if eval_file.is_file():
                try:
                    loaded_rep = EvaluationReport.from_dict(json.loads(eval_file.read_text(encoding="utf-8")))
                    reports.append(loaded_rep)
                except Exception:
                    pass
    started = time.monotonic()
    paused = False
    pause_reason = ""
    aborted_any = False

    judge_driver = None
    judge_model = args.judge_model
    judge_driver_name = args.judge_driver or args.driver
    judge_base_url = args.judge_base_url
    judge_api_key = args.judge_api_key
    judge_effort = args.judge_effort

    if not judge_model:
        # 尝试自动载入全局持久化的默认裁判配置 (.bench_judge.json)
        try:
            j_file = Path(".bench_judge.json")
            if j_file.is_file():
                j_cfg = json.loads(j_file.read_text(encoding="utf-8"))
                if j_cfg.get("model"):
                    judge_model = j_cfg["model"]
                    judge_driver_name = args.judge_driver or j_cfg.get("driver") or args.driver
                    judge_base_url = args.judge_base_url or j_cfg.get("base_url")
                    judge_api_key = args.judge_api_key or j_cfg.get("api_key")
                    judge_effort = args.judge_effort or j_cfg.get("effort")
        except Exception:
            pass

    if judge_model:
        try:
            judge_driver = build_driver(
                judge_driver_name,
                judge_model,
                effort=judge_effort,
                api_key=judge_api_key,
                base_url=judge_base_url,
            )
        except Exception as exc:
            print("warning: cannot build judge driver: %s; using strict heuristic rubric" % exc, file=sys.stderr)

    from benchmark_v3.bench_harness.core.report import MasterLeaderboard
    _skip_board = args.driver == "mock"

    def _build_regress_ask() -> Any:
        """questionary 交互确认：新分低于表内最高分时问存不存。"""
        try:
            import questionary  # type: ignore
        except Exception:
            return None
        try:
            import sys as _sys
            if not _sys.stdin.isatty():
                return None
        except Exception:
            return None

        def _ask(scope: str, old_slot: dict, new_slot: dict) -> bool:
            try:
                from rich.console import Console as _Console
                from rich.panel import Panel as _Panel
                _Console().print(_Panel(
                    f"[bold white]{scope}[/bold white]\n"
                    f"表内最高分: [green]{old_slot.get('reward')} "
                    f"({old_slot.get('milestones_passed')}/{old_slot.get('milestones_total')}, "
                    f"{old_slot.get('total_tokens', 0):,} tokens)[/green]\n"
                    f"本次得分: [yellow]{new_slot.get('reward')} "
                    f"({new_slot.get('milestones_passed')}/{new_slot.get('milestones_total')}, "
                    f"{new_slot.get('total_tokens', 0):,} tokens)[/yellow]",
                    title="[bold yellow]⚠️ 本次退步：是否覆盖表内最高分？[/bold yellow]",
                    border_style="yellow",
                ))
                ans = questionary.confirm("用本次较低分覆盖表内最高分吗？(选否则保留最高分)", default=False).ask()
                return bool(ans)
            except Exception:
                return False

        return _ask

    ask_fn = _build_regress_ask() if args.on_regress == "ask" else None
    work_queue = list(manifest.get("remaining") or planned)

    for item in work_queue:
        suite_name = str(item.get("suite") or "")
        task_id = str(item.get("task_id") or "")
        if not suite_name or not task_id:
            continue

        if pause_requested(output, pause_flag):
            paused = True
            pause_reason = "PAUSE.request" if (output / "PAUSE.request").is_file() else "SIGINT"
            break

        if task_is_complete(output, suite_name, task_id):
            mark_task_completed(manifest, suite=suite_name, task_id=task_id)
            save_manifest(output, manifest)
            reporter.update(task_id, "skip", "already has evaluation.json")
            continue

        suite = get_suite(
            suite_name,
            judge_driver=judge_driver if suite_name == "critic" else None,
        )
        reporter.update(task_id, "running", "suite=%s model=%s" % (suite_name, args.model))
        try:
            report = suite.run_session(
                task_id,
                args.model,
                driver,
                output,
                resume=bool(args.resume or args.continue_run),
            )
        except (KeyboardInterrupt, TaskAbandoned) as exc:
            paused = True
            if isinstance(exc, TaskAbandoned):
                pause_reason = str(getattr(exc, "reason", "") or "pause")
            else:
                pause_reason = (
                    "PAUSE.request" if (output / "PAUSE.request").is_file() else "SIGINT"
                )
            abandon_incomplete_task(output, suite_name, task_id)
            # Keep this task in remaining (never mark completed).
            save_manifest(output, manifest)
            if not args.quiet:
                print(
                    "abandoned in-progress task %s/%s — will retry on continue"
                    % (suite_name, task_id),
                    flush=True,
                )
            break
        except Exception as exc:  # never let one task kill the sweep
            print("task %s crashed: %r" % (task_id, exc), file=sys.stderr)
            mark_task_completed(manifest, suite=suite_name, task_id=task_id, passed=False)
            save_manifest(output, manifest)
            continue

        reports.append(report)
        mark_task_completed(
            manifest,
            suite=suite_name,
            task_id=task_id,
            passed=bool(report.passed),
            reward=float(getattr(report, "final_reward", 0.0) or 0.0),
        )
        save_manifest(output, manifest)
        reporter.complete_task(
            "%s/%s" % (suite_name, task_id), report.passed,
            "reward=%s" % (report.final_reward,),
        )

        # A session that died before any completed turn (upstream outage, key
        # cooldown) produced no evidence about the model — never let it reach
        # the leaderboard as a 0-score. By default this PAUSES the sweep so a
        # flaky upstream cannot burn through the whole queue.
        if (output / suite_name / task_id / "ABORTED.json").is_file():
            aborted_any = True
            if args.on_upstream_error == "pause":
                paused = True
                pause_reason = "upstream_error"
                print(
                    "task %s/%s ABORTED before any turn (upstream unavailable)."
                    % (suite_name, task_id),
                    file=sys.stderr,
                    flush=True,
                )
                print(
                    "pausing the sweep (--on-upstream-error pause). Nothing was "
                    "scored for this task; resume with the same --output to "
                    "retry the remaining queue once the upstream recovers.",
                    file=sys.stderr,
                    flush=True,
                )
                manifest["remaining"] = [
                    {"suite": suite_name, "task_id": task_id},
                    *[i for i in work_queue[work_queue.index(item) + 1:]],
                ]
                save_manifest(output, manifest)
                break
            if not args.quiet:
                print(
                    "task %s/%s ABORTED before any turn (upstream unavailable) — "
                    "excluded from leaderboard; continuing with the next task"
                    % (suite_name, task_id),
                    file=sys.stderr,
                    flush=True,
                )
            continue

        # Incremental leaderboard: completed tasks land even if we pause later.
        try:
            if not _skip_board:
                MasterLeaderboard.update_leaderboard(
                    [report],
                    model_id=args.model,
                    driver=args.driver,
                    effort=args.effort,
                    output_dir=output,
                    wall_time=time.monotonic() - started,
                    on_regress=args.on_regress,
                    ask_fn=ask_fn,
                )
        except Exception as exc:
            print("warning: failed to update master leaderboard: %s" % exc, file=sys.stderr)

        if pause_requested(output, pause_flag):
            paused = True
            pause_reason = "PAUSE.request" if (output / "PAUSE.request").is_file() else "SIGINT"
            break

    manager = ReportManager(output)
    summary = ReportManager.build_summary(reports)
    summary["model_id"] = args.model
    summary["driver"] = args.driver
    summary["wall_time_seconds"] = time.monotonic() - started  # telemetry only
    summary["paused"] = paused
    manager.save_summary(summary)

    if paused:
        set_manifest_status(output, manifest, "paused", reason=pause_reason or "pause")
        clear_pause_request(output)
        if not args.quiet:
            rem = len(manifest.get("remaining") or [])
            print(
                "run paused (%s): %d task(s) remaining in %s — continue from TUI "
                "or: bench-run --continue-run --output %s ..."
                % (pause_reason or "pause", rem, output, output),
                flush=True,
            )
        reporter.finish("bench-run paused: %d new report(s), %d remaining"
                        % (len(reports), len(manifest.get("remaining") or [])))
        if reports:
            _print_table(reports, output=output, model_id=args.model, driver=args.driver, quiet=args.quiet)
        return 0

    set_manifest_status(output, manifest, "completed")
    clear_pause_request(output)

    if aborted_any:
        print(
            "warning: at least one task ABORTED before any completed turn "
            "(upstream unavailable) — excluded from the leaderboard; rerun "
            "those tasks when the upstream recovers.",
            file=sys.stderr,
            flush=True,
        )

    if (args.export_sft or args.export_dpo) and reports:
        _export_datasets(output, reports, args.export_sft, args.export_dpo)

    reporter.finish("bench-run done: %d/%d passed"
                    % (sum(1 for r in reports if r.passed), len(reports)))
    _print_table(reports, output=output, model_id=args.model, driver=args.driver, quiet=args.quiet)
    if not _skip_board and reports:
        _print_post_run_board(reports, quiet=args.quiet)
    # 如果本次没有新跑任何任务（例如全部任务此前均已执行完成），直接返回成功 (0)
    if args.continue_run and not (manifest.get("remaining") or []):
        return 0
    return 0 if reports and all(r.passed for r in reports) else (0 if not reports else 1)


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


def _suite_sections() -> list[tuple[str, str, str, list[str]]]:
    from benchmark_v3.bench_harness.suites.catalog import summary_sections

    return summary_sections()


def _print_table(
    reports: list[Any],
    output: Path | None = None,
    model_id: str = "",
    driver: str = "",
    quiet: bool = False,
) -> None:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    # legacy_windows=False 强制使用 ANSI 序列，防止 GBK 编码崩溃
    console = Console(legacy_windows=False)

    # 1. 组织 Markdown 与控制台数据结构
    md_lines: list[str] = [
        f"# Benchmark v3 评测汇总报告 (Model: `{model_id}`)",
        f"- **评测驱动 (Driver)**: `{driver}`",
        f"- **生成时间 (Timestamp)**: `{datetime.now(timezone.utc).isoformat()}`",
        f"- **输出目录 (Run Dir)**: `{output}`",
        "",
        "---",
        "",
    ]

    total_passed = sum(1 for r in reports if r.passed)
    total_tasks = len(reports)
    total_tokens = sum(r.token_metrics.total_tokens for r in reports)
    total_prompt = sum(r.token_metrics.prompt_tokens for r in reports)
    total_completion = sum(r.token_metrics.completion_tokens for r in reports)
    total_reasoning = sum(r.token_metrics.reasoning_tokens for r in reports)
    total_wall_time = sum(r.telemetry.wall_time_seconds for r in reports)

    # 2. 控制台渲染各维度分组表格
    if not quiet:
        console.print()

    for suite_key, cli_title, md_title, tasks in _suite_sections():
        want_cond = "b" if suite_key.endswith("_b") else "a"
        suite_reports = [
            r for r in reports
            if r.task_id in tasks and (getattr(r, "condition", "a") or "a") == want_cond
        ]
        if not suite_reports:
            continue

        suite_passed = sum(1 for r in suite_reports if r.passed)
        suite_tokens = sum(r.token_metrics.total_tokens for r in suite_reports)
        pass_rate_pct = (suite_passed / len(suite_reports)) * 100.0

        # Markdown 节段
        md_lines.append(f"## {md_title}")
        md_lines.append(f"> **小计**: 通过 `{suite_passed}/{len(suite_reports)}` ({pass_rate_pct:.1f}%) | Token 消耗: `{suite_tokens:,}`\n")
        md_lines.append("| 任务标识 (Task) | 状态 (Status) | 得分 (Reward) | Token消耗 (Total) | 耗时 (Wall Time) |")
        md_lines.append("| :--- | :---: | :---: | ---: | ---: |")

        # Rich 表格 (终端展示)
        table = Table(title=f"[bold cyan]{cli_title}[/bold cyan]", border_style="cyan", header_style="bold white")
        table.add_column("Task ID", style="white", width=24)
        table.add_column("Status", justify="center", width=12)
        table.add_column("Reward", justify="right", width=14)
        table.add_column("Tokens", justify="right", width=14)
        table.add_column("Wall Time", justify="right", width=14)

        for r in suite_reports:
            status_display = "[bold green]PASS[/bold green]" if r.passed else "[bold red]FAIL[/bold red]"
            md_status = "✔ **PASSED**" if r.passed else "✖ FAILED"

            reward_str = f"{r.final_reward:.1f} / 100" if suite_key == "critic" else f"{r.final_reward:.2f} / 1.00"
            token_str = f"{r.token_metrics.total_tokens:,}"
            time_str = f"{r.telemetry.wall_time_seconds:.1f}s"

            table.add_row(r.task_id, status_display, reward_str, token_str, time_str)
            md_lines.append(f"| `{r.task_id}` | {md_status} | `{reward_str}` | `{token_str}` | `{time_str}` |")

        # 套件小计行
        subtotal_label = f"[dim]Subtotal ({suite_passed}/{len(suite_reports)} passed)[/dim]"
        subtotal_token = f"[dim]{suite_tokens:,}[/dim]"
        table.add_section()
        table.add_row(subtotal_label, "", f"[bold]{pass_rate_pct:.1f}%[/bold]", subtotal_token, "")

        if not quiet:
            console.print(table)
            console.print()

        md_lines.append("")

    # 3. 渲染专家裁判复核详情 (若存在 judge_verdict.json)
    judge_files = list(output.glob("**/judge_verdict.json")) if output is not None else []
    for jf in judge_files:
        try:
            jdata = json.loads(jf.read_text(encoding="utf-8"))
            verdicts = jdata.get("verdicts", {})
            jmodel = jdata.get("judge_model", "Unknown Judge")
            total_d = jdata.get("total_depth", 0.0)

            md_lines.append(f"### 👨‍⚖️ Critic 专家裁判复核详情 (Judge: `{jmodel}` | 深度总分: `{total_d:.1f}/20.0`)")
            md_lines.append("| 待审文件 (File) | 判定等级 (Level) | 得分 (Score) | 裁判裁决理由 (Rationale) |")
            md_lines.append("| :--- | :---: | :---: | :--- |")

            j_table = Table(title=f"[bold green]👨‍⚖️ Critic 专家裁判复核详情 (Judge: {jmodel} | 深度得分: {total_d:.1f}/20)[/bold green]", border_style="green")
            j_table.add_column("待审文件 (File)", style="cyan", width=22)
            j_table.add_column("等级", justify="center", width=8)
            j_table.add_column("得分", justify="right", width=12)
            j_table.add_column("裁判裁决理由与评分依据", style="white")

            for f_name, v_info in verdicts.items():
                lvl = str(v_info.get("level", "L0"))
                sc = float(v_info.get("score", 0.0))
                rsn = str(v_info.get("reasoning", ""))
                md_lines.append(f"| `{f_name}` | **{lvl}** | `{sc:.1f} / 5.0` | {rsn} |")
                lvl_colored = f"[bold green]{lvl}[/bold green]" if lvl in ("L3", "L4") else f"[yellow]{lvl}[/yellow]"
                j_table.add_row(f_name, lvl_colored, f"{sc:.1f} / 5.0", rsn)

            md_lines.append("")
            if not quiet:
                console.print(j_table)
                console.print()

            # 额外有效发现加分 (Novel Findings Bonus)
            novel_v = jdata.get("novel_verdicts", [])
            novel_b = jdata.get("novel_bonus", 0.0)
            if novel_v:
                md_lines.append(f"### 🎁 额外有效缺陷加分 (Novel Findings Bonus: `+{novel_b:.1f}` 分)")
                md_lines.append("| 涉及文件 (File) | 位置 (Line) | 奖励分值 (Bonus) | 裁判认可的额外缺陷依据 (Rationale) |")
                md_lines.append("| :--- | :---: | :---: | :--- |")
                n_table = Table(title=f"[bold magenta]🎁 Critic 额外有效缺陷奖励加分 (Bonus: +{novel_b:.1f}分)[/bold magenta]", border_style="magenta")
                n_table.add_column("涉及文件 (File)", style="cyan", width=22)
                n_table.add_column("位置", justify="center", width=8)
                n_table.add_column("奖励分值", justify="right", width=12)
                n_table.add_column("裁判认可依据与隐患类型", style="white")
                for nv in novel_v:
                    nf = nv.get("file", "?")
                    nl = str(nv.get("line", "?"))
                    nb = float(nv.get("bonus_score", 0.0))
                    nr = str(nv.get("reasoning", ""))
                    md_lines.append(f"| `{nf}` | 行 {nl} | `+{nb:.1f}` | {nr} |")
                    n_table.add_row(nf, f"行 {nl}", f"[bold green]+{nb:.1f}[/bold green]", nr)
                md_lines.append("")
                if not quiet:
                    console.print(n_table)
                    console.print()
        except Exception:
            pass

    # 4. 总体统计面板：B 里程碑加进同一总数；综合指数 = 已得 / 总数 × 100。
    def _run_cond(r) -> str:
        return getattr(r, "condition", "a") or "a"

    a_reports = [r for r in reports if _run_cond(r) == "a"]
    all_milestones = [m for r in reports for m in r.milestones]
    passed_scoring_points = sum(1 for m in all_milestones if m.passed)
    total_scoring_points = len(all_milestones)
    points_pass_rate_pct = (passed_scoring_points / total_scoring_points * 100.0) if total_scoring_points else 0.0
    capability_index = points_pass_rate_pct

    tasks_passed = sum(1 for r in reports if r.passed)
    tasks_total = len(reports)
    tasks_pass_rate_pct = (tasks_passed / tasks_total * 100.0) if tasks_total else 0.0

    unique_a = {r.task_id for r in a_reports}
    unique_pairs = {(r.task_id, _run_cond(r)) for r in reports}
    try:
        from benchmark_v3.bench_harness.core.report import MasterLeaderboard as _Board
        _Board._bind_catalog()
        covers_all_a = unique_a >= set(_Board.CANONICAL_TASKS)
    except Exception:
        covers_all_a = False
    if len(unique_pairs) == 1:
        panel_title = "本轮单项评测小结"
        md_heading = "## 本轮单项评测小结"
        cap_note = "(本轮已得评分点 / 总数，非正式总榜)"
    elif covers_all_a:
        panel_title = "Benchmark v3 全维度评测总评看板"
        md_heading = "## 📊 全维度总评看板"
        cap_note = "(已得评分点 / 总数 × 100，A+B 同一池)"
    else:
        panel_title = "本轮评测小结"
        md_heading = "## 本轮评测小结"
        cap_note = "(本轮已得评分点 / 总数，非正式总榜)"

    points_color = "green" if points_pass_rate_pct >= 50 else "yellow"
    summary_text = (
        f"[bold white]被测模型 (Model):[/bold white] [bold yellow]{model_id}[/bold yellow]  |  "
        f"[bold white]协议驱动 (Driver):[/bold white] [bold cyan]{driver}[/bold cyan]\n"
        f"[bold white]评分点 (Scoring Points):[/bold white] [bold {points_color}]{passed_scoring_points} / {total_scoring_points} ({points_pass_rate_pct:.1f}%)[/bold {points_color}]\n"
        f"[bold white]综合能力指数 (Capability Index):[/bold white] [bold cyan]{capability_index:.1f} / 100[/bold cyan] [dim]{cap_note}[/dim]\n"
        f"[bold white]大任务全通数 (Completed Tasks):[/bold white] [dim]{tasks_passed} / {tasks_total} ({tasks_pass_rate_pct:.1f}%)[/dim]\n"
        f"[bold white]总 Token 消耗:[/bold white] [bold magenta]{total_tokens:,}[/bold magenta] "
        f"[dim](Prompt: {total_prompt:,} | Completion: {total_completion:,} | Reasoning: {total_reasoning:,})[/dim]\n"
        f"[bold white]全流程耗时 (纯遥测):[/bold white] [dim]{total_wall_time:.1f}s[/dim]\n"
        f"[bold white]报告与制品目录:[/bold white] [dim]{output}[/dim]"
    )

    if not quiet:
        console.print(Panel(summary_text, title=f"[bold green]{panel_title}[/bold green]", border_style="bright_blue"))
        console.print()

    # 4. 写入 Markdown 汇总报告
    md_lines.append(md_heading)
    md_lines.append(f"- **评分点 (Scoring Points)**: `{passed_scoring_points} / {total_scoring_points}` (`{points_pass_rate_pct:.1f}%`) *(A+B 里程碑同一池)*")
    md_lines.append(f"- **综合能力指数 (Overall Capability Index)**: `{capability_index:.1f} / 100` *{cap_note}*")
    md_lines.append(f"- **全通大任务数 (Completed Task Packages)**: `{tasks_passed} / {tasks_total}` (`{tasks_pass_rate_pct:.1f}%`)")
    md_lines.append(f"- **总 Token 消耗**: `{total_tokens:,}` (Prompt: `{total_prompt:,}` | Completion: `{total_completion:,}` | Reasoning: `{total_reasoning:,}`)")
    md_lines.append(f"- **全流程累计耗时**: `{total_wall_time:.1f}s` (纯遥测指标，不计入得分)")
    md_lines.append("")

    if output is not None:
        try:
            (output / "SUMMARY.md").write_text("\n".join(md_lines), encoding="utf-8")
        except OSError:
            pass
    try:
        Path("LATEST_SUMMARY.md").write_text("\n".join(md_lines), encoding="utf-8")
    except OSError:
        pass


_MEDALS = ["👑 1", "🥈 2", "🥉 3"]


def _print_post_run_board(reports: list[Any], quiet: bool = False) -> None:
    """After the run finishes, print one ranking table derived from leaderboard.json.

    Multi-task runs get the master table. A single (task, condition) run gets
    that task's sort view of the same JSON — not a separately scored board.
    """
    if quiet or not reports:
        return

    from rich.console import Console
    from rich.table import Table

    from benchmark_v3.bench_harness.core.report import (
        MasterLeaderboard,
        _fmt_tokens_short,
    )

    console = Console(legacy_windows=False)
    unique = {(r.task_id, getattr(r, "condition", "a") or "a") for r in reports}

    if len(unique) == 1:
        task_id, cond = next(iter(unique))
        rows = MasterLeaderboard.task_board(task_id, condition=cond)
        label = task_id if cond == "a" else f"{task_id}@{cond}"
        table = Table(
            title=(
                f"[bold green]分任务榜 · {label}[/bold green]\n"
                "[dim]同源总榜槽位，按该任务得分排序（非独立计分）[/dim]"
            ),
            border_style="green",
        )
        table.add_column("排名", style="bold yellow", width=6, justify="center")
        table.add_column("模型", style="bold white")
        table.add_column("驱动·强度", style="cyan")
        table.add_column("该任务得分", justify="right")
        table.add_column("通过", justify="center")
        table.add_column("综合指数(同行)", justify="right")
        table.add_column("Token", justify="right")
        if not rows:
            console.print(f"\n[yellow]总榜尚无 `{label}` 槽位，本轮结果已写入 leaderboard.json。[/yellow]\n")
            return
        for i, row in enumerate(rows):
            tokens = row["total_tokens"]
            table.add_row(
                _MEDALS[i] if i < 3 else str(i + 1),
                str(row["model_id"]),
                f"{row['driver']}·{row['effort']}",
                MasterLeaderboard.format_slot_reward(task_id, row["reward"]),
                "✔" if row["passed"] else "✖",
                f"{row['capability_index']:.1f}",
                f"{tokens:,}" if tokens is not None else "-",
            )
        console.print()
        console.print(table)
        console.print()
        return

    entries = MasterLeaderboard.sorted_entries()
    entries = [e for e in entries if e.get("coverage_full")]
    table = Table(
        title=(
            "[bold green]🏆 全维度权威总榜（仅列全量模型）[/bold green]\n"
            "[dim]调整指数 = 能力分 / clamp(成本C,0.5,3)^0.5；能力分为同源参考列[/dim]"
        ),
        border_style="yellow",
    )
    table.add_column("排名", style="bold yellow", width=6, justify="center")
    table.add_column("模型", style="bold white")
    table.add_column("驱动·强度", style="cyan")
    table.add_column("调整指数", justify="right")
    table.add_column("能力分", justify="right")
    table.add_column("成本C", justify="right")
    table.add_column("生成TPS", justify="right")
    table.add_column("tok/断言", justify="right")
    table.add_column("Token", justify="right")
    if not entries:
        console.print("\n[yellow]总榜暂无全量模型数据。[/yellow]\n")
        return
    for i, item in enumerate(entries):
        adj = float(item.get("adjusted_index", 0.0) or 0.0)
        cap = float(item.get("capability_index", 0.0) or 0.0)
        tokens = item.get("total_tokens", 0)
        cost = float(item.get("cost_ratio", 1.0) or 1.0)
        tps = float(item.get("gen_tps", 0.0) or 0.0)
        tpa = item.get("tokens_per_assertion", 0)
        table.add_row(
            _MEDALS[i] if i < 3 else str(i + 1),
            str(item.get("model_id", "?")),
            f"{item.get('driver', '?')}·{item.get('effort', 'default')}",
            f"{adj:.1f} / 100",
            f"{cap:.1f}",
            f"{cost:.2f}",
            f"{tps:,.0f}",
            _fmt_tokens_short(tpa),
            f"{tokens:,}",
        )
    console.print()
    console.print(table)
    console.print()


def self_test() -> tuple[int, int]:
    """Run module self-tests. Returns ``(passed, failed)`` counts."""
    import tempfile

    counts = [0, 0]

    def check(name: str, cond: bool) -> None:
        counts[0 if cond else 1] += 1
        print(f"{'PASS' if cond else 'FAIL'} cli::{name}", flush=True)

    parser = build_arg_parser()
    args = parser.parse_args(["--suite", "critic", "--model", "t",
                              "--driver", "mock", "--task", "audit_bundle",
                              "--effort", "max"])
    check("arg_parse", args.suite == ["critic"] and args.task == "audit_bundle"
          and args.model == "t" and not args.resume)
    cont = parser.parse_args(["--model", "t", "--continue-run", "--output", "x"])
    check("arg_parse_continue_run", cont.continue_run is True and cont.output == "x")
    multi = parser.parse_args(["--suite", "short", "--suite", "long", "--model", "t"])
    check("arg_parse_multi_suite", multi.suite == ["short", "long"])
    defaulted = parser.parse_args(["--model", "t"])
    check("arg_parse_suite_default_none", defaulted.suite is None)
    check("effort_max_choice", args.effort == "max")
    from benchmark_v3.bench_harness.drivers.effort import openai_reasoning_effort
    check("effort_max_passthrough", openai_reasoning_effort("max") == "max")

    driver = build_driver("mock", "t")
    check("mock_driver", type(driver).__name__ == "ScriptedDriver")
    from benchmark_v3.bench_harness.suites import get_suite
    critic_suite = get_suite("critic", judge_driver=driver)
    check("get_suite_forwards_judge", getattr(critic_suite, "judge_driver", None) is driver)
    check("get_suite_short_ignores_judge", type(get_suite("short", judge_driver=driver)).__name__ == "ShortTaskSuite")
    short_b = get_suite("short_b")
    check("get_suite_short_b", short_b.condition == "b" and short_b.run_key == "short_b")
    try:
        build_driver("nope", "t")
        check("bad_driver_rejected", False)
    except ValueError:
        check("bad_driver_rejected", True)

    from benchmark_v3.bench_harness.core import run_manifest as _rm

    rm_p, rm_f = _rm.self_test()
    check("run_manifest_module", rm_f == 0 and rm_p > 0)

    # -- end-to-end offline sweep (mock writes nothing; pipeline must
    #    complete honestly with atomic reports + dataset exports) --
    with tempfile.TemporaryDirectory(prefix="cli-e2e-") as tmp:
        out = str(Path(tmp) / "runs")
        code = main(["--suite", "reviewer", "--task", "bait_guard",
                     "--model", "mock-model", "--driver", "mock",
                     "--output", out, "--quiet",
                     "--export-sft", str(Path(tmp) / "sft.jsonl"),
                     "--export-dpo", str(Path(tmp) / "dpo.jsonl")])
        check("e2e_exit", code == 1)
        check("e2e_evaluation", (Path(out) / "reviewer" / "bait_guard" / "evaluation.json").exists())
        check("e2e_summary", (Path(out) / "summary.json").exists())
        check("e2e_live", (Path(out) / "live_status.json").exists())
        check("e2e_manifest", (Path(out) / "run_manifest.json").exists())
        man = json.loads((Path(out) / "run_manifest.json").read_text(encoding="utf-8"))
        check("e2e_manifest_completed", man.get("status") == "completed")
        check("e2e_sft", (Path(tmp) / "sft.jsonl").exists())
        check("e2e_dpo", (Path(tmp) / "dpo.jsonl").exists())
        try:
            summary = json.loads((Path(out) / "summary.json").read_text(encoding="utf-8"))
            check("e2e_summary_shape", summary.get("n_reports") == 1 and "entries" in summary)
        except (OSError, ValueError):
            check("e2e_summary_shape", False)

        # continue-run should skip already-complete task (exit 0, no crash)
        code2 = main(["--suite", "reviewer", "--task", "bait_guard",
                      "--model", "mock-model", "--driver", "mock",
                      "--output", out, "--quiet", "--continue-run", "--resume"])
        check("e2e_continue_skip", code2 == 0)
        man2 = json.loads((Path(out) / "run_manifest.json").read_text(encoding="utf-8"))
        check("e2e_continue_still_complete", man2.get("status") == "completed")

    # L1 pause before first task via sentinel
    with tempfile.TemporaryDirectory(prefix="cli-pause-") as tmp:
        out = Path(tmp) / "runs"
        out.mkdir(parents=True)
        (out / "PAUSE.request").write_text("pause\n", encoding="utf-8")
        code3 = main(["--suite", "reviewer", "--model", "mock-model", "--driver", "mock",
                      "--output", str(out), "--quiet"])
        man3 = json.loads((out / "run_manifest.json").read_text(encoding="utf-8"))
        check("e2e_pause_exit", code3 == 0)
        check("e2e_pause_status", man3.get("status") == "paused")
        check("e2e_pause_remaining", len(man3.get("remaining") or []) >= 1)

    return counts[0], counts[1]


if __name__ == "__main__":
    raise SystemExit(main())
