"""bench-run: unified CLI entry point for all benchmark_v3 suites.

Usage::

    bench-run --suite {short,long,reviewer,critic,all} --model <model_id>
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
    parser.add_argument("--suite", default="all",
                        choices=["short", "long", "reviewer", "critic", "all"])
    parser.add_argument("--model", required=False, default=None,
                        help="Model id (e.g. k3-max).")
    parser.add_argument("--driver", default="openai",
                        choices=["openai", "response", "google", "anthropic", "cli", "mock"])
    parser.add_argument("--base-url", default=None,
                        help="Base URL for the model under test (supports OpenAI, Response, Google, Anthropic).")
    parser.add_argument("--api-key", default=None,
                        help="API key for the model under test.")
    parser.add_argument("--effort", default=None,
                        choices=["none", "low", "medium", "high", "xhigh", "max"],
                        help="Reasoning effort level (for o1/o3/Claude 3.7/Gemini 2.0).")
    parser.add_argument("--task", default=None, help="Run a single task id only.")
    parser.add_argument("--resume", action="store_true",
                        help="Replay stalled snapshot turns instead of restarting.")
    parser.add_argument("--output", default=None,
                        help="Output root (default: ./bench_runs/<timestamp>).")
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
                        choices=["none", "low", "medium", "high", "xhigh", "max"],
                        help="Reasoning effort for the expert judge model.")
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
        from benchmark_v3.bench_harness.suites.base import ScriptedDriver

        return ScriptedDriver(model_id, effort=effort)
    raise ValueError("unknown driver %r" % (driver_name,))


def _default_output() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path("bench_runs") / stamp


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
    from benchmark_v3.bench_harness.suites import SUITE_REGISTRY

    args = build_arg_parser().parse_args(argv)
    if not args.model:
        print("error: the following arguments are required: --model (or run with -i for interactive mode)",
              file=sys.stderr)
        return 2

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
        driver = build_driver(
            args.driver,
            args.model,
            effort=args.effort,
            api_key=args.api_key,
            base_url=args.base_url,
        )
    except Exception as exc:
        print("error: cannot build driver: %s" % exc, file=sys.stderr)
        return 2

    reports: list[Any] = []
    started = time.monotonic()

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

    for suite_name in suite_names:
        if suite_name == "critic" and judge_driver is not None:
            suite = SUITE_REGISTRY[suite_name](judge_driver=judge_driver)
        else:
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

    # 自动增量更新全局总榜 (LEADERBOARD.md & bench_runs/leaderboard.json)
    from benchmark_v3.bench_harness.core.report import MasterLeaderboard
    try:
        MasterLeaderboard.update_leaderboard(
            reports,
            model_id=args.model,
            driver=args.driver,
            effort=args.effort,
            output_dir=output,
            wall_time=time.monotonic() - started,
        )
    except Exception as exc:
        print("warning: failed to update master leaderboard: %s" % exc, file=sys.stderr)

    if (args.export_sft or args.export_dpo) and reports:
        _export_datasets(output, reports, args.export_sft, args.export_dpo)

    reporter.finish("bench-run done: %d/%d passed"
                    % (sum(1 for r in reports if r.passed), len(reports)))
    _print_table(reports, output=output, model_id=args.model, driver=args.driver, quiet=args.quiet)
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


SUITE_SECTIONS = [
    ("critic", "[Critic] 代码盲审 (Adversarial Code Audit)", "🛡️ Critic 代码盲审", ["audit_bundle"]),
    ("reviewer", "[Reviewer] 调试修复 (Micro-debugging Range)", "🔧 Reviewer 调试修复", ["lock_ordering", "api_drift", "bait_guard"]),
    ("short", "[Short] 次世代短任务 · 微引擎 (Micro-Engine)", "⚡ 次世代短任务 · 微引擎", ["varint_parser", "timing_wheel", "lexer_state_machine"]),
    ("long", "[Long] 次世代长任务 · 分布式混沌 (Distributed Chaos)", "🌐 次世代长任务 · 分布式混沌", ["raft_cluster", "saga_coordinator"]),
]


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

    by_task_id = {r.task_id: r for r in reports}

    # 2. 控制台渲染各维度分组表格
    if not quiet:
        console.print()

    for suite_key, cli_title, md_title, tasks in SUITE_SECTIONS:
        suite_reports = [by_task_id[t] for t in tasks if t in by_task_id]
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

    # 4. 总体统计面板 (基于全量细粒度评分点与里程碑)
    all_milestones = [m for r in reports for m in r.milestones]
    total_scoring_points = len(all_milestones)
    passed_scoring_points = sum(1 for m in all_milestones if m.passed)
    points_pass_rate_pct = (passed_scoring_points / total_scoring_points * 100.0) if total_scoring_points else 0.0

    normalized_scores = [
        r.final_reward if r.task_id == "audit_bundle" else r.final_reward * 100.0
        for r in reports
    ]
    capability_index = (sum(normalized_scores) / len(normalized_scores)) if normalized_scores else 0.0

    tasks_passed = sum(1 for r in reports if r.passed)
    tasks_total = len(reports)
    tasks_pass_rate_pct = (tasks_passed / tasks_total * 100.0) if tasks_total else 0.0

    summary_text = (
        f"[bold white]被测模型 (Model):[/bold white] [bold yellow]{model_id}[/bold yellow]  |  "
        f"[bold white]协议驱动 (Driver):[/bold white] [bold cyan]{driver}[/bold cyan]\n"
        f"[bold white]全量评分点通过率 (Scoring Points):[/bold white] [bold {'green' if points_pass_rate_pct >= 50 else 'yellow'}]{passed_scoring_points} / {total_scoring_points} ({points_pass_rate_pct:.1f}%)[/bold {'green' if points_pass_rate_pct >= 50 else 'yellow'}]\n"
        f"[bold white]综合能力指数 (Capability Index):[/bold white] [bold cyan]{capability_index:.1f} / 100[/bold cyan] [dim](全维度归一化综合得分)[/dim]\n"
        f"[bold white]大任务全通数 (Completed Tasks):[/bold white] [dim]{tasks_passed} / {tasks_total} ({tasks_pass_rate_pct:.1f}%)[/dim]\n"
        f"[bold white]总 Token 消耗:[/bold white] [bold magenta]{total_tokens:,}[/bold magenta] "
        f"[dim](Prompt: {total_prompt:,} | Completion: {total_completion:,} | Reasoning: {total_reasoning:,})[/dim]\n"
        f"[bold white]全流程耗时 (纯遥测):[/bold white] [dim]{total_wall_time:.1f}s[/dim]\n"
        f"[bold white]报告与制品目录:[/bold white] [dim]{output}[/dim]"
    )

    if not quiet:
        console.print(Panel(summary_text, title="[bold green]Benchmark v3 全维度评测总评看板[/bold green]", border_style="bright_blue"))
        console.print()

    # 4. 写入 Markdown 汇总报告
    md_lines.append("## 📊 全维度总评看板")
    md_lines.append(f"- **全量评分点通过率 (Scoring Points Pass Rate)**: `{passed_scoring_points} / {total_scoring_points}` (`{points_pass_rate_pct:.1f}%`) *(基于全套件细粒度测试里程碑)*")
    md_lines.append(f"- **综合能力指数 (Overall Capability Index)**: `{capability_index:.1f} / 100` *(全维度归一化综合得分)*")
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
