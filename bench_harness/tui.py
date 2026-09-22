"""
Benchmark v3 Interactive Terminal UI (TUI) Launcher.

Provides a visually polished, guided terminal wizard for:
1. Selecting model drivers (OpenAI-compatible, Google Gemini, Anthropic Claude, CLI, Mock)
2. Managing connection credentials, base URLs, and network proxies safely
3. Persistent profile presets in local gitignored `.bench_profiles.json`
4. Interactive multi-suite and task selection
5. Pre-flight launch card and real-time execution handoff
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

# Ensure both repo root and benchmark_v3 parent are in sys.path
_this_dir = os.path.dirname(os.path.abspath(__file__))
_bench_v3_dir = os.path.dirname(_this_dir)
_repo_root = os.path.dirname(_bench_v3_dir)
for _p in [_repo_root, _bench_v3_dir]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

import questionary
from questionary import Choice, Style
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from benchmark_v3.bench_harness.suites.catalog import (
    DEFAULT_ALL_KEYS,
    DEFAULT_TUI_KEYS,
    SELECTABLE_KEYS,
    SUITE_LABELS,
)

if sys.platform == "win32":
    # 确保 Windows 终端支持 UTF-8 输出，防止 GBK 编码报错
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if hasattr(sys.stderr, "reconfigure"):
        try:
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

console = Console()

#: rich markup 标签（用于 _shorten 的纯文本测量）
_MARKUP_RE = re.compile(r"\[/?[a-z#][a-z0-9_ .#,/]*\]")

PROFILES_FILE = Path(".bench_profiles.json")
JUDGE_CONFIG_FILE = Path(".bench_judge.json")


def load_judge_config() -> dict[str, Any]:
    """读取全局持久化的专家裁判模型配置"""
    if not JUDGE_CONFIG_FILE.is_file():
        return {}
    try:
        data = json.loads(JUDGE_CONFIG_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_judge_config(config: dict[str, Any]) -> None:
    """持久化保存全局专家裁判模型配置到 .bench_judge.json"""
    JUDGE_CONFIG_FILE.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")


_JUDGE_PROFILE_KEYS = (
    "judge_model",
    "judge_driver",
    "judge_base_url",
    "judge_api_key",
    "judge_effort",
)


def global_judge_summary() -> str:
    """一行摘要，如 ``google -> gemini-x | effort=high``；未配置则空串。"""
    cfg = load_judge_config()
    model = str(cfg.get("model") or "").strip()
    if not model:
        return ""
    driver = str(cfg.get("driver") or "").strip() or "?"
    effort = str(cfg.get("effort") or "").strip()
    tag = f"{driver} -> {model}"
    if effort:
        tag += f" | effort={effort}"
    return tag


def apply_profile_global_judge(config: dict[str, Any]) -> None:
    """清空任务级裁判字段，运行时回退到 ``.bench_judge.json``。"""
    for key in _JUDGE_PROFILE_KEYS:
        config[key] = None if key == "judge_effort" else ""


#: 全局裁判表格字段：(key, 显示名, kind, 说明)
_JUDGE_FIELDS: tuple[tuple[str, str, str, str], ...] = (
    ("enabled", "启用开关", "bool", "关闭=清空全局裁判，回退严格启发式量表"),
    ("driver", "协议驱动", "driver", "裁判模型协议"),
    ("model", "模型标识", "text", "如 gemini-3.7-flash-tiered / k3-max"),
    ("base_url", "Base URL", "text", "留空用官方默认，支持反代/中转"),
    ("api_key", "API Key", "password", "留空沿用环境变量/主配置"),
    ("proxy", "网络代理", "text", "留空不走代理"),
    ("effort", "思考强度", "effort", "推荐 high 深度思考评判"),
)

_JUDGE_DEFAULT_MODEL = {
    "google": "gemini-3.7-flash-tiered",
    "openai": "k3-max",
    "response": "gpt-5-mini",
    "anthropic": "claude-3-7-sonnet-20250219",
}


def configure_judge_wizard() -> None:
    """表格化全局裁判配置（复用预设 master-detail 交互，含放弃返回）。"""
    current = load_judge_config()
    nav_push("全局裁判")
    try:
        console.print("\n[bold cyan]>>> 配置默认全局专家裁判模型 (Global Default Judge)[/bold cyan]")
        console.print("[dim]设置后，所有评测运行将默认自动由该模型对 critic 报告进行 L1~L4 严格复核，无需每次重复输入。[/dim]\n")

        working: dict[str, Any] = {
            "enabled": bool(current.get("model")),
            "driver": current.get("driver", "google"),
            "model": current.get("model", ""),
            "base_url": current.get("base_url", ""),
            "api_key": current.get("api_key", ""),
            "proxy": current.get("proxy", ""),
            "effort": current.get("effort", "high"),
        }
        while True:
            table = Table(title="[bold green]⚖️ 全局专家裁判 · 表格化配置[/bold green]",
                          border_style="cyan", show_lines=False)
            table.add_column("#", style="dim", width=4, justify="right")
            table.add_column("属性", style="bold white", width=14)
            table.add_column("当前值", style="cyan", overflow="fold")
            for idx, (key, label, kind, _hint) in enumerate(_JUDGE_FIELDS, start=1):
                if kind == "bool":
                    shown = "[green]✔ 启用[/green]" if working["enabled"] else "[dim]✖ 关闭[/dim]"
                elif kind == "password":
                    shown = mask_key(working.get(key))
                elif not working.get(key):
                    shown = "[dim cyan](未配置/默认)[/dim cyan]"
                else:
                    shown = str(working.get(key))
                table.add_row(str(idx), label, _shorten(shown, 58))
            console.print()
            console.print(table)
            console.print("[dim]提示：选中某行先展开完整值详情，再决定是否修改。[/dim]\n")

            menu: list[Choice | questionary.Separator] = []
            for idx, (key, label, kind, _hint) in enumerate(_JUDGE_FIELDS, start=1):
                if kind == "bool":
                    plain = "启用" if working["enabled"] else "关闭"
                elif kind == "password":
                    plain = _field_plain(working, key, kind)
                else:
                    plain = str(working.get(key) or "(未配置/默认)")
                menu.append(Choice(f"{idx:>2}. {label}: {_shorten(plain, 40)}", value=("field", key)))
            menu.append(questionary.Separator("── 完成 ──"))
            menu.append(Choice("💾 保存并返回主菜单", value=("done", None)))
            menu.append(Choice("↩ 放弃修改返回主菜单", value=("abort", None)))

            picked = questionary.select("选择要查看/修改的属性:", choices=menu, style=CUSTOM_STYLE).ask()
            if picked is None:
                return
            action, target = picked
            if action == "abort":
                return
            if action == "done":
                if not working["enabled"]:
                    save_judge_config({})
                    console.print("[yellow]✔ 已清空全局专家裁判配置，后续将默认使用严格启发式量表复核。[/yellow]\n")
                else:
                    if not str(working.get("model") or "").strip():
                        working["model"] = _JUDGE_DEFAULT_MODEL.get(str(working.get("driver")), "")
                    save_judge_config({
                        "driver": working["driver"],
                        "model": working["model"],
                        "base_url": working["base_url"],
                        "api_key": working["api_key"],
                        "proxy": working["proxy"],
                        "effort": working["effort"],
                    })
                    console.print(f"\n[green]✔ 全局专家裁判配置已持久化保存！[当前: {working['model']} ({working['driver']})][/green]")
                    console.print("[dim]该配置已保存在本地 .bench_judge.json，后续所有评测将默认自动挂载该裁判模型。[/dim]\n")
                return
            spec = next(s for s in _JUDGE_FIELDS if s[0] == target)
            if spec[2] == "bool":
                working["enabled"] = questionary.confirm(
                    "是否启用全局默认专家裁判模型?",
                    default=bool(working["enabled"]), style=CUSTOM_STYLE).ask()
                continue
            _edit_field_value(working, spec[0], spec[2], spec[1],
                              drivers=("openai", "response", "google", "anthropic", "mock"))
    finally:
        nav_pop()

# 优雅的 Questionary 交互配色
CUSTOM_STYLE = Style(
    [
        ("qmark", "fg:#00d7af bold"),           # 绿青色标志
        ("question", "fg:#ffffff bold"),        # 白色粗体问题
        ("answer", "fg:#5fffff bold"),          # 亮青色回答
        ("pointer", "fg:#ff5faf bold"),         # 洋红色指示器
        ("highlighted", "fg:#5fffff bold"),     # 选中的高亮项
        ("selected", "fg:#00d787"),             # 多选标记绿色
        ("separator", "fg:#6c6c6c"),            # 分隔符灰色
        ("instruction", "fg:#8a8a8a italic"),   # 辅助提示弱化
        ("text", "fg:#ffffff"),
        ("disabled", "fg:#858585 italic"),
    ]
)

BANNER = r"""
[bold cyan] ██████╗ ███████╗███╗   ██╗ ██████╗██╗  ██╗███╗   ███╗ █████╗ ██████╗ ██╗  ██╗[/bold cyan]
[bold cyan] ██╔══██╗██╔════╝████╗  ██║██╔════╝██║  ██║████╗ ████║██╔══██╗██╔══██╗██║ ██╔╝[/bold cyan]
[bold cyan] ██████╔╝█████╗  ██╔██╗ ██║██║     ███████║██╔████╔██║███████║██████╔╝█████╔╝ [/bold cyan]
[bold cyan] ██╔══██╗██╔══╝  ██║╚██╗██║██║     ██╔══██║██║╚██╔╝██║██╔══██║██╔══██╗██╔═██╗ [/bold cyan]
[bold cyan] ██████╔╝███████╗██║ ╚████║╚██████╗██║  ██║██║ ╚═╝ ██║██║  ██║██║  ██║██║  ██╗[/bold cyan]
[bold cyan] ╚═════╝ ╚══════╝╚═╝  ╚═══╝ ╚═════╝╚═╝  ╚═╝╚═╝     ╚═╝╚═╝  ╚═╝╚═╝  ╚═╝╚═╝  ╚═╝[/bold cyan]
[bold magenta]                       v3.0 Next-Gen Benchmark Harness                       [/bold magenta]
[dim]     Micro-Engines · Distributed Chaos · De-labeled Auditing · Fine-Tuning Trace    [/dim]
"""


def load_profiles() -> dict[str, Any]:
    """读取本地已保存的预设配置列表"""
    if not PROFILES_FILE.is_file():
        return {}
    try:
        data = json.loads(PROFILES_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_profiles(profiles: dict[str, Any]) -> None:
    """持久化保存预设到 gitignored 的本地文件"""
    PROFILES_FILE.write_text(json.dumps(profiles, indent=2, ensure_ascii=False), encoding="utf-8")


def mask_key(key: str | None) -> str:
    """脱敏遮蔽密钥，便于安全展示"""
    if not key:
        return "[dim cyan](未配置)[/dim cyan]"
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:3]}********{key[-4:]}"


def print_banner() -> None:
    console.print(BANNER)


#: 导航栈：记录用户当前所处界面层级，实现"返回上层"而非一律回主菜单。
#: questionary 是顺序 prompt 库，做不了真全屏，用显式栈 + 面包屑是标准做法。
_NAV_STACK: list[str] = []


def nav_reset(label: str = "主菜单") -> None:
    """回到顶层（主菜单入口调用）。"""
    _NAV_STACK.clear()
    _NAV_STACK.append(label)


def nav_push(label: str) -> None:
    """进入子界面时压栈并打印面包屑。"""
    _NAV_STACK.append(label)
    nav_show()


def nav_pop() -> None:
    """从子界面返回时弹栈并打印面包屑（栈空时保持顶层语义）。"""
    if _NAV_STACK:
        _NAV_STACK.pop()
    nav_show()


def nav_bar() -> str:
    """当前面包屑路径，如 `主菜单 › 运行后 › 总榜`。"""
    return " › ".join(_NAV_STACK) if _NAV_STACK else "主菜单"


def nav_show() -> None:
    console.print(f"[dim]📍 {nav_bar()}[/dim]\n")


def view_latest_report(back_label: str = "返回主菜单") -> None:
    """在终端中直接用 Rich Markdown 优雅渲染 LATEST_SUMMARY.md"""
    nav_push("汇总报告")
    report_path = Path("LATEST_SUMMARY.md")
    if not report_path.is_file():
        console.print("\n[yellow]尚未检测到 LATEST_SUMMARY.md，请先运行一次评测。[/yellow]\n")
    else:
        try:
            content = report_path.read_text(encoding="utf-8")
            from rich.markdown import Markdown

            console.print()
            console.print(
                Panel(
                    Markdown(content),
                    title="[bold green]📄 LATEST_SUMMARY.md 评测汇总报告[/bold green]",
                    border_style="cyan",
                )
            )
            console.print()
        except Exception as exc:
            console.print(f"[red]读取报告失败: {exc}[/red]")
    try:
        questionary.press_any_key_to_continue(f"按任意键{back_label}...").ask()
    except (KeyboardInterrupt, EOFError):
        pass
    nav_pop()


def _load_master_board() -> Any:
    """懒加载 MasterLeaderboard（与 cli_main 同款双路径兼容）。"""
    try:
        from benchmark_v3.bench_harness.core.report import MasterLeaderboard
    except ImportError:
        from bench_harness.core.report import MasterLeaderboard
    return MasterLeaderboard


def _board_medals() -> list[str]:
    return ["👑 1", "🥈 2", "🥉 3"]


def _fmt_tokens_short(value: Any) -> str:
    """Compact token count for dense tables (mirrors report._fmt_tokens_short)."""
    try:
        v = float(value or 0)
    except (TypeError, ValueError):
        return "-"
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"{v / 1_000:.0f}k"
    return f"{v:.0f}"


def render_master_board() -> None:
    """Render the master ranking from leaderboard.json (same source as MD)."""
    MasterLeaderboard = _load_master_board()
    ranked = MasterLeaderboard.sorted_entries()
    entries = [e for e in ranked if e.get("coverage_full")]
    partial = [e for e in ranked if not e.get("coverage_full")]
    console.print()
    if not entries and not partial:
        console.print("[yellow]总榜暂无数据，请先运行一次评测。[/yellow]\n")
        return
    medals = _board_medals()

    def _suite_cols(item: Any) -> tuple[str, ...]:
        return tuple(str(item.get(f, "-")) for f in (
            "short_score", "short_b_score", "reviewer_score",
            "long_score", "long_b_score", "critic_score"))

    table = Table(
        title=(
            "[bold gold1]🏆 全维度权威总榜（仅列全量模型）[/bold gold1]\n"
            "[dim]调整指数 = 能力分 / clamp(成本C,0.5,3)^0.5（能力×效率几何平均）；能力分为同源 v2 口径参考列[/dim]\n"
            "[dim]TPS = 总token/真实总耗时；tok/断言 = 总token/通过断言数（力大飞砖证据）；同槽多次运行取均值[/dim]"
        ),
        border_style="yellow",
    )
    table.add_column("排名", style="bold yellow", width=6, justify="center")
    table.add_column("模型", style="bold white")
    table.add_column("驱动·强度", style="cyan")
    table.add_column("调整指数", justify="right")
    table.add_column("能力分", justify="right")
    for name in ("short", "short_b", "reviewer", "long", "long_b", "critic"):
        table.add_column(name, justify="right")
    table.add_column("成本C", justify="right")
    table.add_column("TPS", justify="right")
    table.add_column("tok/断言", justify="right")
    table.add_column("Token", justify="right")
    table.add_column("Succ/Mtok", justify="right", style="green")
    for i, item in enumerate(entries):
        adj = float(item.get("adjusted_index", 0.0) or 0.0)
        cap = float(item.get("capability_index", 0.0) or 0.0)
        tokens = item.get("total_tokens", 0)
        succ = float(item.get("succ_per_mtok", 0.0) or 0.0)
        cost = float(item.get("cost_ratio", 1.0) or 1.0)
        tps = float(item.get("tps", 0.0) or 0.0)
        tpa = item.get("tokens_per_assertion", 0)
        table.add_row(
            medals[i] if i < 3 else str(i + 1),
            str(item.get("model_id", "?")),
            f"{item.get('driver', '?')}·{item.get('effort', 'default')}",
            f"{adj:.1f} / 100",
            f"{cap:.1f}",
            *_suite_cols(item),
            f"{cost:.2f}",
            f"{tps:,.0f}",
            _fmt_tokens_short(tpa),
            f"{tokens:,}",
            f"{succ:.2f}" if succ else "-",
        )
    if entries:
        console.print(table)
    if partial:
        console.print()
        ptable = Table(
            title=(
                "[bold yellow]⚠️ 未完成模型（缺槽，暂不参与综合排名）[/bold yellow]\n"
                "[dim]缺跑的往往是难题套件，按均分掺入会系统性虚高；跑齐后自动进入上方总榜[/dim]"
            ),
            border_style="yellow",
        )
        ptable.add_column("模型", style="bold white")
        ptable.add_column("驱动·强度", style="cyan")
        for name in ("short", "short_b", "reviewer", "long", "long_b", "critic"):
            ptable.add_column(name, justify="right")
        ptable.add_column("成本C", justify="right")
        ptable.add_column("TPS", justify="right")
        ptable.add_column("tok/断言", justify="right")
        ptable.add_column("缺失槽位", style="dim")
        for item in partial:
            missing = item.get("coverage_missing") or []
            miss_s = "、".join(str(m) for m in missing[:4])
            if len(missing) > 4:
                miss_s += f" 等{len(missing)}项"
            ptable.add_row(
                str(item.get("model_id", "?")),
                f"{item.get('driver', '?')}·{item.get('effort', 'default')}",
                *_suite_cols(item),
                f"{float(item.get('cost_ratio', 1.0) or 1.0):.2f}",
                f"{float(item.get('tps', 0.0) or 0.0):,.0f}",
                _fmt_tokens_short(item.get("tokens_per_assertion", 0)),
                miss_s or "-",
            )
        console.print(ptable)
    console.print("[dim]完整 Markdown（含各任务重排表）见 LEADERBOARD.md[/dim]\n")


def render_task_board(task_id: str, condition: str = "a") -> None:
    """Render one task ranking: same JSON, primary key = that task's stored slot."""
    MasterLeaderboard = _load_master_board()
    rows = MasterLeaderboard.task_board(task_id, condition=condition)
    label = task_id if condition == "a" else f"{task_id}@{condition}"
    nav_push(f"task:{label}")
    try:
        console.print()
        if not rows:
            console.print(f"[yellow]任务 [{label}] 暂无槽位。跑完该题即入榜。[/yellow]\n")
        else:
            table = Table(
                title=(
                    f"[bold green]📊 分任务榜 · {label}[/bold green]\n"
                    "[dim]同源总榜槽位，按该任务得分排序（非独立计分）[/dim]"
                ),
                border_style="green",
            )
            table.add_column("排名", style="bold yellow", width=6, justify="center")
            table.add_column("模型", style="bold white")
            table.add_column("驱动·强度", style="cyan")
            table.add_column("该任务得分", justify="right")
            table.add_column("通过", justify="center")
            table.add_column("运行数", justify="center")
            table.add_column("综合指数(同行)", justify="right")
            table.add_column("Token", justify="right")
            table.add_column("更新时间", style="dim")
            medals = _board_medals()
            for i, r in enumerate(rows):
                tokens = r["total_tokens"]
                rc = r.get("run_count", 1) or 1
                table.add_row(
                    medals[i] if i < 3 else str(i + 1),
                    str(r["model_id"]),
                    f"{r['driver']}·{r['effort']}",
                    MasterLeaderboard.format_slot_reward(task_id, r["reward"]),
                    "✔" if r["passed"] else "✖",
                    str(rc) if rc > 1 else "-",
                    f"{r['capability_index']:.1f}",
                    f"{tokens:,}" if tokens is not None else "-",
                    str(r.get("updated_at") or "")[:10],
                )
            console.print(table)
            console.print()
        try:
            questionary.press_any_key_to_continue("按任意键返回总榜...").ask()
        except (KeyboardInterrupt, EOFError):
            pass
    finally:
        nav_pop()


def render_suite_board(suite: str) -> None:
    """渲染套件重排榜：同一份总榜 JSON，主键换成该套件已存槽位合计。"""
    MasterLeaderboard = _load_master_board()
    rows = MasterLeaderboard.suite_board(suite)
    scale = "/100" if suite == "critic" else f"/{len(MasterLeaderboard.SUITE_TASKS.get(suite, ()))}"
    nav_push(f"suite:{suite}")
    try:
        console.print()
        if not rows:
            console.print(f"[yellow]维度 [{suite}] 暂无数据，新跑一次即入榜。[/yellow]\n")
        else:
            table = Table(
                title=(
                    f"[bold green]📊 suite board · {suite}（槽位合计 {scale}）[/bold green]\n"
                    "[dim]同源总榜，按该套件已存槽位求和后重排；均分列 = 该套件任务均分（综合指数同口径）[/dim]"
                ),
                border_style="green",
            )
            table.add_column("排名", style="bold yellow", width=6, justify="center")
            table.add_column("模型", style="bold white")
            table.add_column("驱动·强度", style="cyan")
            table.add_column("槽位合计", style="bold green", justify="right")
            table.add_column("均分", justify="right")
            table.add_column("里程碑", justify="center")
            table.add_column("综合指数(同行)", justify="right")
            table.add_column("Token", justify="right")
            table.add_column("更新时间", style="dim")
            medals = _board_medals()
            for i, r in enumerate(rows):
                name = f"{r['model_id']} [dim](legacy)[/dim]" if r.get("legacy") else r["model_id"]
                cap = r.get("capability_index")
                cap_s = f"{float(cap):.1f}" if cap is not None else "-"
                pct = r.get("suite_pct")
                pct_s = f"{float(pct):.1f}" if pct is not None else "-"
                table.add_row(
                    medals[i] if i < 3 else str(i + 1),
                    name,
                    f"{r['driver']}·{r['effort']}",
                    f"{'✔' if r['passed'] else '✖'} {r['reward']}",
                    pct_s,
                    r["milestones"],
                    cap_s,
                    f"{r['total_tokens']:,}" if r["total_tokens"] is not None else "-",
                    str(r["updated_at"])[:10],
                )
            console.print(table)
            console.print()
        try:
            questionary.press_any_key_to_continue("按任意键返回总榜...").ask()
        except (KeyboardInterrupt, EOFError):
            pass
    finally:
        nav_pop()


def view_leaderboard(back_label: str = "返回主菜单") -> None:
    """总榜浏览：总表 + 同源分任务/套件重排钻取。"""
    MasterLeaderboard = _load_master_board()
    MasterLeaderboard._bind_catalog()
    nav_push("权威总榜")
    try:
        while True:
            render_master_board()
            action = questionary.select(
                "总榜操作:",
                choices=[
                    Choice("📊 按任务查看分榜（同源总榜，按该任务得分排序）", value="task"),
                    Choice("📊 按套件查看分榜（同源总榜，按该套件槽位合计排序）", value="suite"),
                    Choice(f"↩ {back_label}", value="back"),
                ],
                style=CUSTOM_STYLE,
            ).ask()
            if action == "task":
                data = MasterLeaderboard.load_data()
                task_choices: list[Choice] = []
                for task_id in MasterLeaderboard.CANONICAL_TASKS:
                    n_a = len(MasterLeaderboard.task_board(task_id, data=data, condition="a"))
                    task_choices.append(
                        Choice(f"{task_id}  (A, {n_a} 条)", value=(task_id, "a"))
                    )
                    n_b = len(MasterLeaderboard.task_board(task_id, data=data, condition="b"))
                    if n_b:
                        task_choices.append(
                            Choice(f"{task_id}@b  (B, {n_b} 条)", value=(task_id, "b"))
                        )
                task_choices.append(Choice("↩ 返回总榜", value=None))
                picked = questionary.select(
                    "选择任务（排序主键 = 该任务已存槽位）:",
                    choices=task_choices,
                    style=CUSTOM_STYLE,
                ).ask()
                if picked:
                    render_task_board(picked[0], picked[1])
                continue
            if action == "suite":
                suite_choices = [
                    Choice(f"{s} ({len(MasterLeaderboard.SUITE_TASKS.get(s, ()))} tasks)", value=s)
                    for s in MasterLeaderboard.SUITES
                ] + [Choice("↩ 返回总榜", value=None)]
                picked = questionary.select("选择维度:", choices=suite_choices, style=CUSTOM_STYLE).ask()
                if picked:
                    render_suite_board(picked)
                continue
            return
    finally:
        nav_pop()


def _delete_run_dir(run_dir: Path) -> bool:
    """Double-confirmed recursive delete, fenced inside bench_runs.

    Returns True only when the directory was actually removed. Refuses
    anything outside the ``bench_runs`` tree (symlink/typo safety).
    """
    import shutil

    try:
        target = Path(run_dir).resolve()
        fence = Path("bench_runs").resolve()
        if target == fence or fence not in target.parents:
            console.print(f"[red]拒绝删除：{run_dir} 不在 bench_runs 目录树内。[/red]")
            return False
        if not target.is_dir():
            console.print(f"[red]目录不存在：{run_dir}。[/red]")
            return False
    except OSError as exc:
        console.print(f"[red]路径检查失败：{exc}[/red]")
        return False
    sure = questionary.confirm(
        f"确认彻底删除 {target} 吗？全部日志与轨迹将丢失！",
        default=False,
        style=CUSTOM_STYLE,
    ).ask()
    if not sure:
        console.print("[dim]已取消删除。[/dim]")
        return False
    try:
        shutil.rmtree(target)
    except OSError as exc:
        console.print(f"[red]删除失败：{exc}[/red]")
        return False
    return True


def continue_paused_run_picker() -> dict[str, Any] | None:
    """主菜单：从 bench_runs 中选择一个 L1 暂停/未完成的测评继续跑。"""
    from benchmark_v3.bench_harness.core.run_manifest import (
        list_incomplete_runs,
        manifest_to_launch_config,
    )

    nav_push("继续未完成")
    try:
        runs = list_incomplete_runs()
        if not runs:
            console.print(
                "\n[yellow]当前没有可继续的未完成测评。"
                "（跑测中 Ctrl+C 会放弃当前题并冻结；或创建 run 目录下的 PAUSE.request）[/yellow]\n"
            )
            return None

        choices: list[Choice] = []
        for item in runs:
            model = item.get("model_id") or "?"
            driver = item.get("driver") or "?"
            effort = item.get("effort")
            effort_tag = f" | effort={effort}" if effort else ""
            prog = f"{item.get('completed_n', 0)}/{item.get('planned_n', 0)}"
            rem = item.get("remaining_n", 0)
            status = item.get("status") or "?"
            reason = item.get("pause_reason") or ""
            reason_tag = f" · {reason}" if reason else ""
            label = (
                f"[{status}] {model} [{driver}{effort_tag}]  "
                f"进度 {prog} · 剩余 {rem}  ·  {item.get('run_dir')}"
                f"{reason_tag}"
            )
            choices.append(Choice(label, value=item["run_dir"]))
        choices.append(Choice("↩ 返回主菜单", value=None))

        picked = questionary.select(
            "选择要继续的未完成测评:",
            choices=choices,
            style=CUSTOM_STYLE,
        ).ask()
        if not picked:
            return None

        from benchmark_v3.bench_harness.core.run_manifest import (
            discard_run,
            load_manifest,
        )

        manifest = load_manifest(Path(picked))
        if not manifest:
            console.print("[red]无法读取 run_manifest.json。[/red]")
            return None
        action = questionary.select(
            f"对 {picked} 执行：",
            choices=[
                Choice("▶ 继续跑剩余任务", value="resume"),
                Choice("🗑 移出列表（保留目录文件，仅不再显示）", value="discard"),
                Choice("☠ 删除整个 run 目录（含全部日志轨迹，不可恢复）", value="delete"),
                Choice("↩ 返回", value=None),
            ],
            style=CUSTOM_STYLE,
        ).ask()
        if action == "discard":
            if discard_run(Path(picked)):
                console.print(f"[yellow]✔ 已将 {picked} 移出继续列表（文件保留）。[/yellow]")
            else:
                console.print("[red]移出失败：manifest 不存在。[/red]")
            return None
        if action == "delete":
            if _delete_run_dir(Path(picked)):
                console.print(f"[yellow]✔ 已删除 run 目录 {picked}。[/yellow]")
            return None
        if action != "resume":
            return None
        config = manifest_to_launch_config(manifest, picked)
        if not config.get("model"):
            console.print("[red]manifest 缺少 model，无法继续。[/red]")
            return None
        console.print(
            f"\n[green]✔ 将继续 [/green][bold]{config['model']}[/bold]"
            f" 于 [cyan]{picked}[/cyan]"
            f" （剩余 {len(manifest.get('remaining') or [])} 题）\n"
        )
        return config
    finally:
        nav_pop()


def profile_picker() -> dict[str, Any] | None:
    """首页：循环选择现有配置或创建新配置（迭代避免尾递归）"""
    nav_reset("主菜单")
    while True:
        nav_show()
        profiles = load_profiles()
        choices: list[Choice | questionary.Separator] = []

        if profiles:
            choices.append(questionary.Separator("── 已保存的模型预设 ──"))
            for name, cfg in profiles.items():
                driver = cfg.get("driver", "openai")
                model = cfg.get("model", "unknown")
                effort_tag = f" | effort={cfg['effort']}" if cfg.get("effort") else ""
                choices.append(Choice(f"[Preset] {name}  [{driver} -> {model}{effort_tag}]", value=("load", name)))
            choices.append(questionary.Separator("── 任务与操作 ──"))

        choices.append(Choice("[+] 新建运行配置 (Create New Configuration)", value=("new", None)))
        if profiles:
            choices.append(Choice("[E] 编辑已有预设 (Edit Existing Profile)", value=("edit", None)))

        from benchmark_v3.bench_harness.core.run_manifest import list_incomplete_runs

        incomplete_n = len(list_incomplete_runs())
        cont_tag = f" [{incomplete_n} 个未完成]" if incomplete_n else ""
        choices.append(Choice(f"[C] 继续未完成的测评 (Continue Paused Run){cont_tag}", value=("continue", None)))

        global_judge = load_judge_config()
        j_tag = f" [当前: {global_judge['model']} ({global_judge.get('driver')})]" if global_judge.get("model") else " [未配置/默认启发式]"
        choices.append(Choice(f"[J] 配置全局专家裁判模型 (Default Judge){j_tag}", value=("judge", None)))

        choices.append(Choice("[V] 查看最近一次评测汇总报告 (View Latest Report)", value=("view", None)))
        choices.append(Choice("[L] 查看全局权威总榜 (View Master Leaderboard)", value=("leaderboard", None)))
        if profiles:
            choices.append(Choice("[-] 管理/删除已有预设 (Manage Profiles)", value=("manage", None)))
        choices.append(Choice("[Q] 退出评测应用 (Exit Application)", value=("exit", None)))

        picked_action = questionary.select(
            "请选择操作:",
            choices=choices,
            style=CUSTOM_STYLE,
        ).ask()

        if picked_action is None or picked_action[0] == "exit":
            return None

        action, target = picked_action

        if action == "judge":
            configure_judge_wizard()
            continue

        if action == "continue":
            continued = continue_paused_run_picker()
            if continued is None:
                continue
            return continued

        if action == "view":
            view_latest_report()
            continue

        if action == "leaderboard":
            view_leaderboard()
            continue

        if action == "load":
            return profiles.get(target)

        if action == "edit":
            edit_target = questionary.select(
                "选择要编辑修改的预设配置:",
                choices=[Choice(f"编辑: {k}", value=k) for k in profiles.keys()] + [Choice("返回上级", value=None)],
                style=CUSTOM_STYLE,
            ).ask()
            if edit_target and edit_target in profiles:
                updated = edit_config_table(edit_target, profiles[edit_target])
                if updated is not None:
                    profiles[edit_target] = updated
                    save_profiles(profiles)
                    console.print(f"[green]✔ 已更新保存预设配置: {edit_target}[/green]")
                    return updated
            continue

        if action == "manage":
            del_target = questionary.select(
                "选择要删除的预设配置:",
                choices=[Choice(f"删除: {k}", value=k) for k in profiles.keys()] + [Choice("返回上级", value=None)],
                style=CUSTOM_STYLE,
            ).ask()
            if del_target and del_target in profiles:
                del profiles[del_target]
                save_profiles(profiles)
                console.print(f"[green]✔ 已删除预设: {del_target}[/green]")
            continue

        # action == "new"
        created = edit_config_table(None, _seed_new_config(), create_mode=True)
        if created:
            profiles = load_profiles()
            default_name = "%s_%s" % (
                created.get("driver", "openai"),
                str(created.get("model", "model")).replace(":", "_").replace("/", "_"),
            )
            saved_name = questionary.text(
                "输入预设名称（保存后可一键载入；Esc 跳过保存直接启动）:",
                default=default_name,
                style=CUSTOM_STYLE,
            ).ask()
            if saved_name and saved_name.strip():
                saved_name = saved_name.strip()
                if saved_name in profiles:
                    overwrite = questionary.confirm(
                        f"预设 [{saved_name}] 已存在，覆盖它吗？",
                        default=False,
                        style=CUSTOM_STYLE,
                    ).ask()
                    if not overwrite:
                        console.print("[yellow]已取消覆盖；本次配置不落盘，直接进入启动卡。[/yellow]")
                        return created
                profiles[saved_name] = created
                save_profiles(profiles)
                console.print(f"[green]✔ 已保存预设配置: {saved_name}（安全存储于 .bench_profiles.json）[/green]")
            return created
        continue


#: 新建向导中"放弃本次配置"哨兵值（select/checkbox 无取消键，用显式选项实现返回）。

#: 表格编辑器字段定义：(key, 显示名, 编辑器类型, 补充说明)
#: kind: driver|text|password|effort|suites|bool|path_opt|judge_driver|judge_model
_CONFIG_FIELDS: tuple[tuple[str, str, str, str], ...] = (
    ("driver", "协议驱动", "driver", "被测模型协议：openai/response/google/anthropic/cli/mock"),
    ("model", "模型标识", "text", "模型唯一 ID，如 deepseek-chat / gemini-2.0-flash"),
    ("base_url", "Base URL", "text", "留空用官方默认；过长内容选中后展开全文"),
    ("api_key", "API Key", "password", "回车保留原值；展示时脱敏"),
    ("proxy", "网络代理", "text", "留空不走代理，如 http://127.0.0.1:10808"),
    ("effort", "思考强度", "effort", "none/minimal/low/medium/high/xhigh/max，留空=厂商默认"),
    ("suites", "评测套件", "suites", "空格多选：" + "/".join(SELECTABLE_KEYS)),
    ("tasks", "限定任务", "task_subset", "留空=所选套件全跑；选中后只跑这些题（部分运行）"),
    ("resume", "断点续跑", "bool", "崩溃时从单轮快照原地恢复"),
    ("on_upstream_error", "上游故障策略", "upstream", "pause=暂停队列可续跑 / continue=跳过记 ABORTED"),
    ("export_sft", "SFT 导出", "path_opt", "选中后可开关 + 修改导出路径"),
    ("export_dpo", "DPO 导出", "path_opt", "选中后可开关 + 修改导出路径"),
    ("judge_model", "裁判模型", "judge_model", "可选用全局专家裁判，或为本预设单独填写"),
    ("judge_driver", "裁判驱动", "judge_driver", "留空=跟随全局/被测驱动"),
    ("judge_base_url", "裁判 Base URL", "text", "留空=沿用全局或主配置"),
    ("judge_api_key", "裁判 Key", "password", "留空=沿用全局或主配置"),
    ("judge_effort", "裁判强度", "effort", "留空=沿用全局默认"),
)

_DRIVER_LABELS = {
    "openai": "OpenAI 兼容协议",
    "response": "OpenAI Responses 协议",
    "google": "Google Gemini 官方协议",
    "anthropic": "Anthropic Claude 官方协议",
    "cli": "本地 Agent CLI",
    "mock": "本地离线 Mock",
}

_EFFORT_LABELS = {
    "none": "关闭 None", "minimal": "最小 Minimal",
    "low": "低 Low", "medium": "中 Medium", "high": "高 High",
    "xhigh": "超高 XHigh", "max": "极限 Max",
}

_SUITE_LABELS = dict(SUITE_LABELS)


def _default_model_for(driver: str) -> str:
    """Per-driver default model id (create-mode seeding)."""
    return {
        "openai": "deepseek-chat",
        "response": "gpt-5-mini",
        "google": "gemini-2.0-flash",
        "anthropic": "claude-3-7-sonnet-20250219",
        "mock": "mock-model",
        "cli": "local-agent",
    }.get(driver, "deepseek-chat")


def _driver_env_defaults(driver: str) -> dict[str, str]:
    """Per-driver connection defaults (env-aware): base_url / api_key / proxy."""
    base_url = ""
    if driver in ("openai", "response"):
        base_url = os.environ.get("OPENAI_BASE_URL", "https://api.deepseek.com/v1")
    elif driver == "anthropic":
        base_url = os.environ.get("ANTHROPIC_BASE_URL", "")
    elif driver == "google":
        base_url = os.environ.get("GEMINI_BASE_URL", "")
    api_key = ""
    if driver in ("openai", "response"):
        api_key = os.environ.get("OPENAI_API_KEY", "")
    elif driver == "google":
        api_key = os.environ.get("GEMINI_API_KEY", "")
    elif driver == "anthropic":
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    proxy = "http://127.0.0.1:10808" if driver in ("google", "anthropic") else ""
    return {"base_url": base_url, "api_key": api_key, "proxy": proxy}


def _seed_new_config() -> dict[str, Any]:
    """Create-mode starting config: openai driver + env-aware defaults."""
    driver = "openai"
    cfg: dict[str, Any] = {
        "driver": driver,
        "model": _default_model_for(driver),
        "proxy": "",
        "effort": None,
        "suites": list(DEFAULT_TUI_KEYS),
        "tasks": [],
        "resume": True,
        "on_upstream_error": "pause",
        "export_sft": f"./datasets/sft_{_default_model_for(driver)}.jsonl",
        "export_dpo": "",
        "judge_model": "",
        "judge_driver": "",
        "judge_base_url": "",
        "judge_api_key": "",
        "judge_effort": None,
    }
    cfg.update(_driver_env_defaults(driver))
    return cfg


def _swap_driver_defaults(cfg: dict[str, Any], old_driver: str, new_driver: str) -> None:
    """Create-mode convenience: after a driver change, refresh connection
    fields that still hold the OLD driver's untouched defaults."""
    if not old_driver or old_driver == new_driver:
        return
    old_def = _driver_env_defaults(old_driver)
    new_def = _driver_env_defaults(new_driver)
    for k in ("base_url", "api_key", "proxy"):
        if str(cfg.get(k) or "") == str(old_def.get(k) or ""):
            cfg[k] = new_def.get(k, "")
    if str(cfg.get("model") or "") == _default_model_for(old_driver):
        cfg["model"] = _default_model_for(new_driver)
        old_sft = f"./datasets/sft_{_default_model_for(old_driver)}.jsonl"
        if str(cfg.get("export_sft") or "") in (old_sft, ""):
            cfg["export_sft"] = f"./datasets/sft_{cfg['model']}.jsonl"


def _shorten(value: str, width: int = 44) -> str:
    """表格单元格截断（完整值在详情面板展示）。

    markup 安全：超宽时先剥掉 rich 标签再截断，避免把 ``[/dim]`` 之类
    的闭合标签截一半导致终端里漏出裸标记。
    """
    text = str(value)
    if len(text) <= width:
        return text
    plain = _MARKUP_RE.sub("", text)
    if len(plain) <= width:
        return plain  # 长度都来自标签：展示纯文本但内容完整
    return plain[: width - 1] + "…"


def _field_display(config: dict[str, Any], key: str, kind: str) -> str:
    """字段当前值的展示串（rich markup，密钥脱敏）。"""
    val = config.get(key)
    if kind == "password":
        return mask_key(val)
    if kind == "judge_model":
        if val:
            return str(val)
        g_tag = global_judge_summary()
        if g_tag:
            return f"[dim cyan](沿用全局专家裁判: {g_tag})[/dim cyan]"
        return "[dim cyan](未配置/启发式)[/dim cyan]"
    if val is None or val == "" or val == []:
        if key.startswith("judge_"):
            if key == "judge_model":
                g_tag = global_judge_summary()
                if g_tag:
                    return f"[dim cyan](沿用全局专家裁判: {g_tag})[/dim cyan]"
            elif global_judge_summary():
                return "[dim cyan](沿用全局)[/dim cyan]"
        return "[dim cyan](未配置/默认)[/dim cyan]"
    if kind == "driver":
        return f"{val} [dim]({_DRIVER_LABELS.get(str(val), '')})[/dim]"
    if kind == "judge_driver":
        return f"{val} [dim]({_DRIVER_LABELS.get(str(val), '')})[/dim]"
    if kind == "effort":
        return f"{val} [dim]({_EFFORT_LABELS.get(str(val), '')})[/dim]"
    if kind == "suites":
        suites = [str(s) for s in (val or [])]
        return ", ".join(f"[green]{_SUITE_LABELS.get(s, s)}[/green]" for s in suites)
    if kind == "task_subset":
        tasks = [str(t) for t in (val or [])]
        return ("[yellow]" + ", ".join(tasks) + "[/yellow] [dim](部分运行)[/dim]") if tasks else "[dim](全部任务)[/dim]"
    if kind == "bool":
        return "[green]✔ 开启[/green]" if val else "[dim]✖ 关闭[/dim]"
    if kind == "upstream":
        if str(val) == "continue":
            return "[yellow]跳过继续[/yellow] [dim](中断题记 ABORTED，不记 0 分)[/dim]"
        return "[green]暂停队列[/green] [dim](中断题不计分，可 --continue-run 续跑)[/dim]"
    if kind == "path_opt":
        return str(val)
    return str(val)


def _field_plain(config: dict[str, Any], key: str, kind: str) -> str:
    """字段当前值的纯文本串（用于 questionary 选项标题）。"""
    val = config.get(key)
    if kind == "password":
        if not val:
            return "(未配置)"
        s = str(val)
        return s[:3] + "********" + s[-4:] if len(s) > 8 else "*" * len(s)
    if kind == "judge_model":
        if val:
            return str(val)
        g_tag = global_judge_summary()
        return f"(沿用全局专家裁判: {g_tag})" if g_tag else "(未配置/启发式)"
    if kind == "task_subset":
        tasks = [str(t) for t in (val or [])]
        return ",".join(tasks) if tasks else "(全部任务)"
    if val is None or val == "" or val == []:
        if key.startswith("judge_"):
            if key == "judge_model":
                g_tag = global_judge_summary()
                if g_tag:
                    return f"(沿用全局专家裁判: {g_tag})"
            elif global_judge_summary():
                return "(沿用全局)"
        return "(未配置/默认)"
    if kind == "suites":
        return ",".join(str(s) for s in val)
    if kind == "bool":
        return "开启" if val else "关闭"
    if kind == "upstream":
        return "跳过继续 (continue)" if str(val) == "continue" else "暂停队列 (pause)"
    return str(val)


def _edit_field_value(config: dict[str, Any], key: str, kind: str, label: str,
                      drivers: tuple[str, ...] | None = None) -> bool:
    """编辑单个字段；返回 True 表示值被修改。"""
    current = config.get(key)
    detail_value = _field_plain(config, key, kind)
    console.print()
    console.print(Panel(
        f"[bold white]{detail_value}[/bold white]",
        title=f"[bold cyan]🔍 {label} · 当前完整值[/bold cyan]",
        border_style="cyan",
    ))

    actions = [Choice("✏️  修改该项", value="edit"), Choice("↩ 返回（不改）", value="back")]
    clearable = kind in ("text", "password", "effort", "path_opt", "judge_driver", "judge_model") and (
        key.startswith(("judge_", "export_", "base_url", "proxy", "api_key"))
    )
    if clearable:
        actions.insert(1, Choice("🧹 清空该项（恢复默认/沿用全局）", value="clear"))
    g_tag = global_judge_summary()
    if key == "judge_model" and g_tag:
        actions.insert(
            0,
            Choice(f"🌐 使用当前全局专家裁判 ({g_tag})", value="use_global"),
        )

    op = questionary.select(f"如何处理 [{label}]?", choices=actions, style=CUSTOM_STYLE).ask()
    if op == "use_global":
        apply_profile_global_judge(config)
        console.print(f"[green]✔ [{label}] 已改为沿用全局专家裁判: {g_tag}[/green]")
        return True
    if op != "edit" and op != "clear":
        return False
    if op == "clear":
        if key == "judge_model":
            apply_profile_global_judge(config)
        else:
            config[key] = "" if kind != "effort" else None
            if kind == "judge_driver":
                config[key] = ""
        console.print(f"[yellow]已清空 [{label}]。[/yellow]")
        return True

    if kind == "driver":
        allowed = drivers or ("openai", "response", "google", "anthropic", "cli", "mock")
        choices = [Choice(f"{v} ({_DRIVER_LABELS[v]})", value=v) for v in allowed]
        default = current if current in allowed else allowed[0]
        new = questionary.select("协议驱动:", choices=choices,
                                 default=default,
                                 style=CUSTOM_STYLE).ask()
        if new:
            config[key] = new
            return True
        return False
    if kind == "judge_driver":
        drivers = [("", "跟随全局/被测驱动"), ("openai", "OpenAI 兼容"), ("response", "Responses"),
                   ("google", "Gemini"), ("anthropic", "Claude")]
        new = questionary.select("裁判驱动:", choices=[Choice(t, value=v) for v, t in drivers],
                                 default=current or "", style=CUSTOM_STYLE).ask()
        if new is not None:
            config[key] = new
            return True
        return False
    if kind == "judge_model":
        new = questionary.text(
            "裁判模型标识 (本预设独立配置，留空=沿用全局):",
            default=str(current or ""),
            style=CUSTOM_STYLE,
        ).ask()
        if new is None:
            return False
        config[key] = new.strip()
        return True
    if kind == "effort":
        opts = [("", "默认 (厂商默认)")] + [
            (v, _EFFORT_LABELS[v])
            for v in ("none", "minimal", "low", "medium", "high", "xhigh", "max")
        ]
        new = questionary.select("思考强度:", choices=[Choice(t, value=v) for v, t in opts],
                                 default=current, style=CUSTOM_STYLE).ask()
        if new is not None:
            config[key] = new or None
            return True
        return False
    if kind == "suites":
        picked = questionary.checkbox(
            "评测套件 (空格选择):",
            choices=[Choice(SUITE_LABELS.get(s, s), value=s, checked=s in (current or []))
                     for s in SELECTABLE_KEYS],
            style=CUSTOM_STYLE).ask() or []
        if not picked:
            picked = [DEFAULT_ALL_KEYS[0]]
        config[key] = picked
        return True
    if kind == "task_subset":
        from benchmark_v3.bench_harness.core.run_manifest import known_task_ids

        _avail = known_task_ids()
        _suites = [s for s in (config.get("suites") or list(DEFAULT_ALL_KEYS)) if s in _avail]
        _choices = [
            Choice(f"{s} / {t}", value=t, checked=t in (current or []))
            for s in _suites for t in _avail[s]
        ]
        if not _choices:
            console.print("[yellow]当前未选中任何可用套件，无法选择任务。[/yellow]")
            return False
        picked = questionary.checkbox(
            "限定任务 (空格多选；全部不选=该套件全跑):",
            choices=_choices,
            style=CUSTOM_STYLE,
        ).ask()
        if picked is None:
            return False
        config[key] = sorted(set(picked))
        return True
    if kind == "bool":
        new = questionary.confirm(f"是否开启 [{label}]?", default=bool(current), style=CUSTOM_STYLE).ask()
        if new is None:
            return False
        config[key] = bool(new)
        return True
    if kind == "upstream":
        new = questionary.select(
            "上游故障（5xx / 密钥冷却 / 超时）中断任务时如何处理?",
            choices=[
                Choice("暂停整个队列（推荐：中断题不计分，可 --continue-run 续跑）", value="pause"),
                Choice("跳过继续跑后续任务（中断题记入 ABORTED，不记 0 分）", value="continue"),
            ],
            default=str(current or "pause"),
            style=CUSTOM_STYLE,
        ).ask()
        if new is None:
            return False
        config[key] = new
        return True
    if kind == "path_opt":
        enable = questionary.confirm(f"是否启用 [{label}]?", default=bool(current), style=CUSTOM_STYLE).ask()
        if not enable:
            config[key] = ""
            return True
        new = questionary.text(f"{label}路径:", default=str(current or ""), style=CUSTOM_STYLE).ask()
        if new is None:
            return False
        new = new.strip()
        if new:
            config[key] = new
            return True
        return False
    if kind == "password":
        entered = questionary.password(f"{label} (回车保留原值):", style=CUSTOM_STYLE).ask()
        if entered is None:
            return False
        if entered.strip():
            config[key] = entered.strip()
            return True
        return False
    # text
    new = questionary.text(f"{label}:", default=str(current or ""), style=CUSTOM_STYLE).ask()
    if new is None:
        return False
    config[key] = new.strip()
    return True


def edit_config_table(
    profile_name: str | None,
    config: dict[str, Any],
    *,
    create_mode: bool = False,
) -> dict[str, Any] | None:
    """表格化 master-detail 配置编辑器（新建与编辑共用同一入口）。

    总览表 + 选中行展开详情面板 + 原地修改，替代逐项 wizard 重走。
    返回更新后的 config；放弃修改返回 None。create_mode 下驱动切换会
    联动刷新仍处于旧驱动默认值的连接字段；预设命名与落盘由调用方负责。
    """
    nav_push("新建配置" if create_mode else f"编辑配置:{profile_name}")
    try:
        working = json.loads(json.dumps(config))  # 深拷贝，放弃时不污染原配置
        while True:
            title = (
                "[bold green]🆕 新建运行配置 · 表格化编辑[/bold green]"
                if create_mode else
                f"[bold green]⚙️ 预设 [{profile_name}] · 表格化编辑[/bold green]"
            )
            table = Table(title=title, border_style="cyan", show_lines=False)
            table.add_column("#", style="dim", width=4, justify="right")
            table.add_column("属性", style="bold white", width=14)
            table.add_column("当前值", style="cyan", overflow="fold")
            for idx, (key, label, kind, _hint) in enumerate(_CONFIG_FIELDS, start=1):
                table.add_row(str(idx), label, _shorten(_field_display(working, key, kind), 58))
            console.print()
            console.print(table)
            console.print("[dim]提示：选中某行先展开完整值详情，再决定是否修改。[/dim]\n")

            menu: list[Choice | questionary.Separator] = []
            for idx, (key, label, kind, _hint) in enumerate(_CONFIG_FIELDS, start=1):
                menu.append(Choice(f"{idx:>2}. {label}: {_shorten(_field_plain(working, key, kind), 40)}",
                                   value=("field", key)))
            menu.append(questionary.Separator("── 完成 ──"))
            menu.append(Choice("💾 保存并返回", value=("done", None)))
            menu.append(Choice("↩ 放弃修改返回", value=("abort", None)))

            picked = questionary.select("选择要查看/修改的属性:", choices=menu, style=CUSTOM_STYLE).ask()
            if picked is None:
                return None
            action, target = picked
            if action == "abort":
                return None
            if action == "done":
                return working
            # field
            spec = next(s for s in _CONFIG_FIELDS if s[0] == target)
            prev_driver = working.get("driver")
            _edit_field_value(working, spec[0], spec[2], spec[1])
            if create_mode and spec[0] == "driver":
                _swap_driver_defaults(working, str(prev_driver or ""), str(working.get("driver") or ""))
    finally:
        nav_pop()




def display_launch_card(config: dict[str, Any]) -> bool:
    """展示格式化启动卡片并做最后确认"""
    table = Table(title="[bold green]🚀 评测任务预检就绪清单 (Pre-flight Summary)[/bold green]", border_style="cyan")
    table.add_column("配置属性", style="bold white", width=22)
    table.add_column("当前参数值", style="cyan")

    table.add_row("被测模型 (Model)", f"[bold yellow]{config.get('model')}[/bold yellow]")
    table.add_row("协议驱动 (Driver)", config.get("driver", "openai"))
    effort_val = config.get("effort")
    table.add_row("思考强度 (Effort)", f"[bold magenta]{effort_val}[/bold magenta]" if effort_val else "[dim cyan]默认 (Default)[/dim cyan]")
    if config.get("base_url"):
        table.add_row("Base URL", config.get("base_url"))
    table.add_row("API Key 状态", mask_key(config.get("api_key")))
    if config.get("proxy"):
        table.add_row("网络代理 (Proxy)", config.get("proxy"))

    suites_display = ", ".join([f"[bold green]{s}[/bold green]" for s in config.get("suites", [])])
    table.add_row("运行维度 (Suites)", suites_display)
    _tasks_sel = [t for t in (config.get("tasks") or []) if t]
    table.add_row(
        "运行任务 (Tasks)",
        ("[bold yellow]仅 " + ", ".join(_tasks_sel) + "[/bold yellow] [dim](部分运行)[/dim]")
        if _tasks_sel else "[dim]该套件全部任务[/dim]",
    )
    table.add_row("断点自愈 (Resume)", "✔ 已开启 (启用 Prompt 快照重放)" if config.get("resume") or config.get("continue_run") else "✖ 未开启")
    _oue = config.get("on_upstream_error") or "pause"
    table.add_row(
        "上游故障策略",
        "[green]✔ 暂停队列[/green] [dim](中断题不计分，可续跑)[/dim]"
        if _oue == "pause" else
        "[yellow]⚠ 跳过继续[/yellow] [dim](中断题记入 ABORTED，不记 0 分)[/dim]",
    )
    if config.get("continue_run") and config.get("output"):
        table.add_row("继续未完成", f"[bold yellow]{config['output']}[/bold yellow]")
    elif config.get("output"):
        table.add_row("输出目录", str(config["output"]))

    global_judge = load_judge_config()
    j_model = config.get("judge_model") or global_judge.get("model")
    j_driver = config.get("judge_driver") or global_judge.get("driver", config.get("driver", "openai"))
    j_effort = config.get("judge_effort") or global_judge.get("effort")

    if j_model:
        j_eff_str = f" | effort={j_effort}" if j_effort else ""
        j_src = "任务独立配置" if config.get("judge_model") else "全局默认"
        table.add_row("专家裁判 (Judge)", f"[bold green]{j_model}[/bold green] [{j_driver}{j_eff_str}] [dim]({j_src})[/dim]")
    else:
        table.add_row("专家裁判 (Judge)", "[dim]严格启发式量表复核 (未配置模型)[/dim]")

    if config.get("export_sft"):
        table.add_row("SFT 导出路径", config.get("export_sft"))
    if config.get("export_dpo"):
        table.add_row("DPO 导出路径", config.get("export_dpo"))

    console.print()
    console.print(Panel(table, border_style="bright_blue", padding=(1, 2)))
    console.print()

    ready = questionary.confirm(
        "确认无误，是否立即点火启动评测？",
        default=True,
        style=CUSTOM_STYLE,
    ).ask()

    return bool(ready)


def launch_harness(config: dict[str, Any]) -> int:
    """设置环境变量并调起 CLI 主程序执行评测"""
    driver = config.get("driver", "openai")
    api_key = config.get("api_key", "")
    base_url = config.get("base_url", "")
    proxy = config.get("proxy", "")

    # 注入环境参数 (仅限于当前进程生命周期)
    if api_key:
        if driver in ("openai", "response"):
            os.environ["OPENAI_API_KEY"] = api_key
        elif driver == "google":
            os.environ["GEMINI_API_KEY"] = api_key
        elif driver == "anthropic":
            os.environ["ANTHROPIC_API_KEY"] = api_key

    if base_url:
        if driver in ("openai", "response"):
            os.environ["OPENAI_BASE_URL"] = base_url
        elif driver == "anthropic":
            os.environ["ANTHROPIC_BASE_URL"] = base_url
        elif driver == "google":
            os.environ["GEMINI_BASE_URL"] = base_url

    if proxy:
        os.environ["HTTP_PROXY"] = proxy
        os.environ["HTTPS_PROXY"] = proxy
        os.environ["http_proxy"] = proxy
        os.environ["https_proxy"] = proxy

    # 构建 CLI 参数。多选套件并入同一次 CLI 调用，评测结束后才出总表/分任务表。
    suites = [s for s in config.get("suites", list(DEFAULT_TUI_KEYS)) if s]
    if not suites:
        suites = [DEFAULT_ALL_KEYS[0]]

    cli_argv: list[str] = []
    for s in suites:
        cli_argv.extend(["--suite", s])
    _task_sel = [t for t in (config.get("tasks") or []) if t]
    if _task_sel:
        cli_argv.extend(["--tasks", ",".join(_task_sel)])
    # 上游故障策略：默认暂停，避免网络问题烧穿整个队列
    cli_argv.extend(["--on-upstream-error", config.get("on_upstream_error") or "pause"])
    cli_argv.extend([
        "--model", config["model"],
        "--driver", driver,
    ])

    if base_url:
        cli_argv.extend(["--base-url", base_url])
    if api_key:
        cli_argv.extend(["--api-key", api_key])

    if config.get("effort"):
        cli_argv.extend(["--effort", config["effort"]])

    # TUI 全程可交互：退步槽位一律弹窗确认（非 TTY 自动回退保留最高分）
    cli_argv.extend(["--on-regress", "ask"])

    if config.get("resume") or config.get("continue_run"):
        cli_argv.append("--resume")
    if config.get("continue_run"):
        cli_argv.append("--continue-run")
    if config.get("output"):
        cli_argv.extend(["--output", str(config["output"])])

    global_judge = load_judge_config()
    active_j_model = config.get("judge_model") or global_judge.get("model")
    active_j_driver = config.get("judge_driver") or global_judge.get("driver")
    active_j_base_url = config.get("judge_base_url") or global_judge.get("base_url")
    active_j_api_key = config.get("judge_api_key") or global_judge.get("api_key")
    active_j_effort = config.get("judge_effort") or global_judge.get("effort")

    if active_j_model:
        cli_argv.extend(["--judge-model", active_j_model])
    if active_j_driver:
        cli_argv.extend(["--judge-driver", active_j_driver])
    if active_j_base_url:
        cli_argv.extend(["--judge-base-url", active_j_base_url])
    if active_j_api_key:
        cli_argv.extend(["--judge-api-key", active_j_api_key])
    if active_j_effort:
        cli_argv.extend(["--judge-effort", active_j_effort])

    if config.get("export_sft"):
        Path(config["export_sft"]).parent.mkdir(parents=True, exist_ok=True)
        cli_argv.extend(["--export-sft", config["export_sft"]])

    if config.get("export_dpo"):
        Path(config["export_dpo"]).parent.mkdir(parents=True, exist_ok=True)
        cli_argv.extend(["--export-dpo", config["export_dpo"]])

    try:
        from benchmark_v3.bench_harness.cli import main as cli_main
    except ImportError:
        from bench_harness.cli import main as cli_main

    console.print("[bold green]✔ 正在初始化执行环境，切入实时评测渲染流...[/bold green]\n")

    return cli_main(cli_argv)


def run_tui() -> int:
    """TUI 主入口：常驻交互式应用程序循环"""
    while True:
        print_banner()

        try:
            config = profile_picker()
            if not config:
                console.print("\n[bold cyan]👋 感谢使用 Benchmark v3，再见！[/bold cyan]\n")
                return 0

            proceed = display_launch_card(config)
            if not proceed:
                console.print("[yellow]操作已取消。[/yellow]")
                continue

            exit_code = launch_harness(config)
            console.print("\n[bold green]✔ 本轮评测执行完毕！已在当前目录同步更新 LATEST_SUMMARY.md[/bold green]\n")

            # 运行后子菜单：查看动作返回后落回本层，而不是主菜单
            nav_push("运行后")
            try:
                while True:
                    post_action = questionary.select(
                        "请选择下一步操作:",
                        choices=[
                            Choice("🏆 查看全维度全局权威总榜 (View Master Leaderboard)", value="leaderboard"),
                            Choice("📄 立即在终端查看本次评测完整汇总报告 (View Summary)", value="view"),
                            Choice("↩️  返回主菜单 (Return to Main Menu)", value="menu"),
                            Choice("❌ 退出评测应用 (Exit Application)", value="exit"),
                        ],
                        style=CUSTOM_STYLE,
                    ).ask()

                    if post_action == "leaderboard":
                        view_leaderboard(back_label="返回运行后菜单")
                        continue
                    if post_action == "view":
                        view_latest_report(back_label="返回运行后菜单")
                        continue
                    if post_action == "exit" or post_action is None:
                        console.print("\n[bold cyan]👋 感谢使用 Benchmark v3，再见！[/bold cyan]\n")
                        return exit_code
                    break  # menu -> 回主菜单
            finally:
                nav_pop()

        except KeyboardInterrupt:
            console.print("\n[yellow]检测到 Ctrl+C 中断信号（已安全接管，可放心回退）。[/yellow]")
            try:
                should_exit = questionary.confirm("确定要退出评测应用吗？(选否则返回主菜单)", default=False, style=CUSTOM_STYLE).ask()
                if should_exit:
                    console.print("\n[bold cyan]👋 评测应用已安全退出。[/bold cyan]\n")
                    return 0
            except (KeyboardInterrupt, EOFError):
                console.print("\n[bold cyan]👋 评测应用已安全退出。[/bold cyan]\n")
                return 0


if __name__ == "__main__":
    sys.exit(run_tui())
