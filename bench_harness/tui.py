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
        console.print("[dim]设置后，所有评测运行将默认自动由该模型对 Critic 盲审深度进行 L1~L4 严格复核，无需每次重复输入。[/dim]\n")

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


def view_leaderboard(back_label: str = "返回主菜单") -> None:
    """在终端中直接用 Rich Markdown 优雅渲染全局权威总榜 LEADERBOARD.md"""
    nav_push("权威总榜")
    board_path = Path("LEADERBOARD.md")
    if not board_path.is_file():
        console.print("\n[yellow]尚未检测到 LEADERBOARD.md 全局总榜，请先运行一次评测。[/yellow]\n")
    else:
        try:
            content = board_path.read_text(encoding="utf-8")
            from rich.markdown import Markdown

            console.print()
            console.print(
                Panel(
                    Markdown(content),
                    title="[bold gold1]🏆 LEADERBOARD.md 全维度权威总榜[/bold gold1]",
                    border_style="yellow",
                )
            )
            console.print()
        except Exception as exc:
            console.print(f"[red]读取总榜失败: {exc}[/red]")
    try:
        questionary.press_any_key_to_continue(f"按任意键{back_label}...").ask()
    except (KeyboardInterrupt, EOFError):
        pass
    nav_pop()


def profile_picker() -> dict[str, Any] | None:
    """首页：选择现有配置或创建新配置"""
    nav_reset("主菜单")
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

    global_judge = load_judge_config()
    j_tag = f" [当前: {global_judge['model']} ({global_judge.get('driver')})]" if global_judge.get("model") else " [未配置/默认启发式]"
    choices.append(Choice(f"[J] 配置全局专家裁判模型 (Default Judge){j_tag}", value=("judge", None)))

    choices.append(Choice("[V] 查看最近一次评测汇总报告 (View Latest Report)", value=("view", None)))
    choices.append(Choice("[L] 查看全局权威总榜 (View Master Leaderboard)", value=("leaderboard", None)))
    if profiles:
        choices.append(Choice("[-] 管理/删除已有预设 (Manage Profiles)", value=("manage", None)))
    choices.append(Choice("[Q] 退出评测应用 (Exit Application)", value=("exit", None)))

    action, target = questionary.select(
        "请选择操作:",
        choices=choices,
        style=CUSTOM_STYLE,
    ).ask()

    if action == "exit" or action is None:
        return None

    if action == "judge":
        configure_judge_wizard()
        return profile_picker()

    if action == "view":
        view_latest_report()
        return profile_picker()

    if action == "leaderboard":
        view_leaderboard()
        return profile_picker()

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
            if updated is None:
                return profile_picker()
            profiles[edit_target] = updated
            save_profiles(profiles)
            console.print(f"[green]✔ 已更新保存预设配置: {edit_target}[/green]")
            return updated
        return profile_picker()

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
        return profile_picker()

    # action == "new"
    created = configure_wizard()
    return created if created else profile_picker()


#: 新建向导中"放弃本次配置"哨兵值（select/checkbox 无取消键，用显式选项实现返回）。
WIZARD_ABORT = "__abort__"
WIZARD_ABORT_CHOICE = Choice("↩ 放弃本次配置，返回主菜单", value="__abort__")


#: 表格编辑器字段定义：(key, 显示名, 编辑器类型, 补充说明)
#: kind: driver|text|password|effort|suites|bool|path_opt|judge_driver
_CONFIG_FIELDS: tuple[tuple[str, str, str, str], ...] = (
    ("driver", "协议驱动", "driver", "被测模型协议：openai/response/google/anthropic/cli/mock"),
    ("model", "模型标识", "text", "模型唯一 ID，如 deepseek-chat / gemini-2.0-flash"),
    ("base_url", "Base URL", "text", "留空用官方默认；过长内容选中后展开全文"),
    ("api_key", "API Key", "password", "回车保留原值；展示时脱敏"),
    ("proxy", "网络代理", "text", "留空不走代理，如 http://127.0.0.1:10808"),
    ("effort", "思考强度", "effort", "low/medium/high/xhigh/max，留空=厂商默认"),
    ("suites", "评测套件", "suites", "空格多选：short/long/reviewer/critic"),
    ("resume", "断点续跑", "bool", "崩溃时从单轮快照原地恢复"),
    ("export_sft", "SFT 导出", "path_opt", "选中后可开关 + 修改导出路径"),
    ("export_dpo", "DPO 导出", "path_opt", "选中后可开关 + 修改导出路径"),
    ("judge_model", "裁判模型", "text", "留空=沿用全局默认裁判；可单独清空"),
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
    "low": "低 Low", "medium": "中 Medium", "high": "高 High",
    "xhigh": "超高 XHigh", "max": "极限 Max",
}

_SUITE_LABELS = {
    "short": "短任务", "long": "长任务",
    "reviewer": "Reviewer", "critic": "Critic",
}


def _shorten(value: str, width: int = 44) -> str:
    """表格单元格截断（完整值在详情面板展示）。"""
    text = str(value)
    return text if len(text) <= width else text[: width - 1] + "…"


def _field_display(config: dict[str, Any], key: str, kind: str) -> str:
    """字段当前值的展示串（rich markup，密钥脱敏）。"""
    val = config.get(key)
    if kind == "password":
        return mask_key(val)
    if val is None or val == "" or val == []:
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
    if kind == "bool":
        return "[green]✔ 开启[/green]" if val else "[dim]✖ 关闭[/dim]"
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
    if val is None or val == "" or val == []:
        return "(未配置/默认)"
    if kind == "suites":
        return ",".join(str(s) for s in val)
    if kind == "bool":
        return "开启" if val else "关闭"
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
    clearable = kind in ("text", "password", "effort", "path_opt", "judge_driver") and key.startswith(
        ("judge_", "export_", "base_url", "proxy", "api_key")
    )
    if clearable:
        actions.insert(1, Choice("🧹 清空该项（恢复默认/沿用全局）", value="clear"))

    op = questionary.select(f"如何处理 [{label}]?", choices=actions, style=CUSTOM_STYLE).ask()
    if op != "edit" and op != "clear":
        return False
    if op == "clear":
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
    if kind == "effort":
        opts = [("", "默认 (Default / None)")] + [(v, _EFFORT_LABELS[v]) for v in ("low", "medium", "high", "xhigh", "max")]
        new = questionary.select("思考强度:", choices=[Choice(t, value=v) for v, t in opts],
                                 default=current, style=CUSTOM_STYLE).ask()
        if new is not None:
            config[key] = new or None
            return True
        return False
    if kind == "suites":
        picked = questionary.checkbox(
            "评测套件 (空格选择):",
            choices=[Choice(f"{_SUITE_LABELS[s]} ({s})", value=s, checked=s in (current or []))
                     for s in ("short", "long", "reviewer", "critic")],
            style=CUSTOM_STYLE).ask() or []
        if not picked:
            picked = ["short"]
        config[key] = picked
        return True
    if kind == "bool":
        new = questionary.confirm(f"是否开启 [{label}]?", default=bool(current), style=CUSTOM_STYLE).ask()
        config[key] = bool(new)
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


def edit_config_table(profile_name: str, config: dict[str, Any]) -> dict[str, Any] | None:
    """表格化 master-detail 预设编辑器。

    总览表 + 选中行展开详情面板 + 原地修改，替代逐项 wizard 重走。
    返回更新后的 config；放弃修改返回 None。
    """
    nav_push(f"编辑预设:{profile_name}")
    try:
        working = json.loads(json.dumps(config))  # 深拷贝，放弃时不污染原配置
        while True:
            table = Table(title=f"[bold green]⚙️ 预设 [{profile_name}] · 表格化编辑[/bold green]",
                          border_style="cyan", show_lines=False)
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
            _edit_field_value(working, spec[0], spec[2], spec[1])
    finally:
        nav_pop()


def configure_wizard(
    initial_config: dict[str, Any] | None = None,
    profile_name: str | None = None,
) -> dict[str, Any] | None:
    """交互向导：配置厂商协议、密钥、套件、专家裁判和运行参数。

    每个 select/checkbox 步骤都提供"放弃本次配置"返回项；
    文本输入步骤回车保留默认；全程可 Ctrl+C 安全回主菜单。
    放弃时返回 None（调用方负责落回上级菜单）。
    """
    init = initial_config or {}
    is_editing = initial_config is not None

    if is_editing:
        console.print(f"\n[bold yellow]>>> 正在编辑预设: {profile_name} (直接回车保留原有值)[/bold yellow]")
    else:
        console.print("\n[dim]💡 新建向导：每个选择步骤都可随时放弃返回主菜单；文本输入回车保留默认值；全程可 Ctrl+C 安全回主菜单。[/dim]")

    console.print("\n[bold cyan]>>> 第 1 步：选择模型协议驱动[/bold cyan]")

    driver_choices = [
        Choice("OpenAI 兼容协议 (DeepSeek / Qwen / Moonshot / OpenAI / vLLM)", value="openai"),
        Choice("OpenAI Responses 协议 (仅 Response 接口的推理模型)", value="response"),
        Choice("Google Gemini (官方 google-genai 2.x SDK 原生协议)", value="google"),
        Choice("Anthropic Claude (官方 anthropic SDK 原生协议)", value="anthropic"),
        Choice("本地 Agent CLI (调起本地命令行 Subagent 子进程)", value="cli"),
        Choice("本地离线 Mock (无需网络和密钥，用于流水线校验)", value="mock"),
        WIZARD_ABORT_CHOICE,
    ]

    default_driver = init.get("driver", "openai")
    driver = questionary.select(
        "请选择被测协议驱动 (Protocol Driver):",
        choices=driver_choices,
        default=default_driver,
        style=CUSTOM_STYLE,
    ).ask()
    if driver == WIZARD_ABORT or driver is None:
        return None

    console.print("\n[bold cyan]>>> 第 2 步：配置模型标识与网络连接[/bold cyan]")

    default_model = init.get("model")
    if not default_model:
        if driver == "openai":
            default_model = "deepseek-chat"
        elif driver == "response":
            default_model = "gpt-5-mini"
        elif driver == "google":
            default_model = "gemini-2.0-flash"
        elif driver == "anthropic":
            default_model = "claude-3-7-sonnet-20250219"
        elif driver == "mock":
            default_model = "mock-model"
        elif driver == "cli":
            default_model = "local-agent"

    model_id = questionary.text(
        "输入模型唯一标识 (Model ID):",
        default=default_model,
        style=CUSTOM_STYLE,
    ).ask().strip()

    base_url = ""
    api_key = ""
    proxy = ""

    default_base_url = init.get("base_url")
    if default_base_url is None:
        if driver in ("openai", "response"):
            default_base_url = os.environ.get("OPENAI_BASE_URL", "https://api.deepseek.com/v1")
        elif driver == "anthropic":
            default_base_url = os.environ.get("ANTHROPIC_BASE_URL", "")
        elif driver == "google":
            default_base_url = os.environ.get("GEMINI_BASE_URL", "")

    if driver in ("openai", "response", "google", "anthropic"):
        base_url = questionary.text(
            "API Base URL (留空使用官方默认，支持自定义反代/中转):",
            default=default_base_url or "",
            style=CUSTOM_STYLE,
        ).ask().strip()

    if driver in ("openai", "response", "google", "anthropic"):
        current_env_key = init.get("api_key") or ""
        if not current_env_key:
            if driver in ("openai", "response"):
                current_env_key = os.environ.get("OPENAI_API_KEY", "")
            elif driver == "google":
                current_env_key = os.environ.get("GEMINI_API_KEY", "")
            elif driver == "anthropic":
                current_env_key = os.environ.get("ANTHROPIC_API_KEY", "")

        key_prompt = "输入 API Key (输入时隐藏):"
        if current_env_key:
            key_prompt = f"输入 API Key (已检测到现有密钥，回车沿用 {mask_key(current_env_key)}):"

        entered_key = questionary.password(
            key_prompt,
            style=CUSTOM_STYLE,
        ).ask()

        api_key = entered_key.strip() if entered_key.strip() else current_env_key

        default_proxy = init.get("proxy")
        if default_proxy is None:
            default_proxy = "http://127.0.0.1:10808" if driver in ("google", "anthropic") else ""

        proxy = questionary.text(
            "HTTP 网络代理 (留空不走代理，如本地科学上网):",
            default=default_proxy or "",
            style=CUSTOM_STYLE,
        ).ask().strip()

    # 推理思考强度选择 (Reasoning Effort / Thinking Budget)
    default_effort = init.get("effort")
    effort = questionary.select(
        "思考链推理强度 (Reasoning Effort / Thinking Budget):",
        choices=[
            Choice("默认 / 厂商默认 (Default / None)", value=None),
            Choice("低 (Low ~1k-2k tokens, 适合轻量调试与快修)", value="low"),
            Choice("中 (Medium ~4k-8k tokens, 适合常规任务)", value="medium"),
            Choice("高 (High ~8k-16k tokens, 适合算法与长任务)", value="high"),
            Choice("超高 (XHigh ~16k-32k tokens, 深度推理与死锁破除)", value="xhigh"),
            Choice("极限 (Max ~32k-64k tokens, 最大思考预算上限)", value="max"),
            WIZARD_ABORT_CHOICE,
        ],
        default=default_effort,
        style=CUSTOM_STYLE,
    ).ask()
    if effort == WIZARD_ABORT:
        return None

    console.print("\n[bold cyan]>>> 第 3 步：勾选本次运行的评测套件[/bold cyan]")

    default_suites = set(init.get("suites", ["short", "long"]))
    selected_suites = questionary.checkbox(
        "选择要评测的维度 (空格选择，Enter 确认):",
        choices=[
            Choice("次世代短任务 (零拷贝Varint解析 / 分层时间轮 / 容错Lexer)", value="short", checked="short" in default_suites),
            Choice("次世代长任务 (三节点Raft脑裂断电 / Saga分布式事务)", value="long", checked="long" in default_suites),
            Choice("Reviewer 调试靶场 (并发死锁 / 跨模块语义漂移 / 诱饵防误报)", value="reviewer", checked="reviewer" in default_suites),
            Choice("Critic 代码盲审 (去标签化高危缺陷 / 无锁环形诱饵)", value="critic", checked="critic" in default_suites),
            WIZARD_ABORT_CHOICE,
        ],
        style=CUSTOM_STYLE,
    ).ask()

    if selected_suites and WIZARD_ABORT in selected_suites:
        return None
    if not selected_suites:
        selected_suites = ["short"]

    console.print("\n[bold cyan]>>> 第 4 步：运行容灾与微调数据集导出[/bold cyan]")

    enable_resume = questionary.confirm(
        "是否启用断点续跑 (--resume，崩溃时从单轮快照原地恢复)?",
        default=init.get("resume", True),
        style=CUSTOM_STYLE,
    ).ask()

    export_sft = questionary.confirm(
        "是否自动导出 SFT 黄金微调数据集 (Pass@1 满分样本)?",
        default=bool(init.get("export_sft", True)),
        style=CUSTOM_STYLE,
    ).ask()

    sft_path = ""
    if export_sft:
        sft_path = questionary.text(
            "SFT 数据集导出路径:",
            default=init.get("export_sft") or f"./datasets/sft_{model_id}.jsonl",
            style=CUSTOM_STYLE,
        ).ask().strip()

    export_dpo = questionary.confirm(
        "是否导出 RL/DPO 偏好对数据集 (含失败里程碑归因)?",
        default=bool(init.get("export_dpo", False)),
        style=CUSTOM_STYLE,
    ).ask()

    dpo_path = ""
    if export_dpo:
        dpo_path = questionary.text(
            "DPO 偏好对数据集导出路径:",
            default=init.get("export_dpo") or f"./datasets/dpo_{model_id}.jsonl",
            style=CUSTOM_STYLE,
        ).ask().strip()

    console.print("\n[bold cyan]>>> 第 5 步：配置独立专家裁判模型 (Judge Model for Critic)[/bold cyan]")
    global_judge_cfg = load_judge_config() or {}
    global_judge_model = global_judge_cfg.get("model", "")
    use_global_judge = False
    if global_judge_model and not init.get("judge_model"):
        g_driver = global_judge_cfg.get("driver", "")
        g_effort = global_judge_cfg.get("effort", "")
        use_global_judge = questionary.confirm(
            f"检测到全局默认裁判 [{g_driver} -> {global_judge_model}"
            f"{' | effort=' + g_effort if g_effort else ''}]，是否直接沿用 (跳过手动填写)?",
            default=True,
            style=CUSTOM_STYLE,
        ).ask()

    has_judge = bool(init.get("judge_model"))
    enable_judge = questionary.confirm(
        "是否启用独立专家裁判模型对 Critic 盲审报告进行 L1~L4 深度复核 (防止水军关键词作弊)?",
        default=has_judge or use_global_judge,
        style=CUSTOM_STYLE,
    ).ask()

    judge_model = ""
    judge_driver = ""
    judge_base_url = ""
    judge_api_key = ""
    judge_effort = None

    if enable_judge and use_global_judge and not init.get("judge_model"):
        # 沿用全局裁判：任务级留空，运行时自动回退到全局配置
        console.print("[dim]✔ 本次运行将沿用全局默认裁判配置 (见预检清单 [全局默认])。[/dim]")
    elif enable_judge:
        judge_driver = questionary.select(
            "裁判模型协议驱动 (Judge Protocol Driver):",
            choices=[
                Choice("沿用当前被测驱动 (Same as Tested Driver)", value=driver),
                Choice("OpenAI 兼容协议 (DeepSeek / Qwen / OpenAI / Kimi)", value="openai"),
                Choice("OpenAI Responses 协议 (仅 Response 接口)", value="response"),
                Choice("Google Gemini (Gemini 2.0 Flash / Pro)", value="google"),
                Choice("Anthropic Claude (Claude 3.7 Sonnet)", value="anthropic"),
                WIZARD_ABORT_CHOICE,
            ],
            default=init.get("judge_driver", driver),
            style=CUSTOM_STYLE,
        ).ask()
        if judge_driver == WIZARD_ABORT:
            return None

        judge_model = questionary.text(
            "裁判模型标识 (Judge Model ID, 建议使用强推理模型如 k3-max / o3-mini / claude-3-7-sonnet):",
            default=init.get("judge_model", "k3-max"),
            style=CUSTOM_STYLE,
        ).ask().strip()

        diff_creds = questionary.confirm(
            "裁判模型是否使用独立的 Base URL / API Key (回车默认沿用主配置)?",
            default=bool(init.get("judge_base_url") or init.get("judge_api_key")),
            style=CUSTOM_STYLE,
        ).ask()

        if diff_creds:
            judge_base_url = questionary.text(
                "裁判模型 Base URL:",
                default=init.get("judge_base_url", ""),
                style=CUSTOM_STYLE,
            ).ask().strip()
            judge_api_key = questionary.password(
                "裁判模型 API Key:",
                style=CUSTOM_STYLE,
            ).ask().strip()

        judge_effort = questionary.select(
            "裁判模型思考强度 (Judge Reasoning Effort):",
            choices=[
                Choice("高 (High - 建议深度思考)", value="high"),
                Choice("超高 (XHigh)", value="xhigh"),
                Choice("中 (Medium)", value="medium"),
                Choice("默认 (Default / None)", value=None),
                WIZARD_ABORT_CHOICE,
            ],
            default=init.get("judge_effort", "high"),
            style=CUSTOM_STYLE,
        ).ask()
        if judge_effort == WIZARD_ABORT:
            return None

    config = {
        "driver": driver,
        "model": model_id,
        "base_url": base_url,
        "api_key": api_key,
        "proxy": proxy,
        "effort": effort,
        "suites": selected_suites,
        "resume": enable_resume,
        "export_sft": sft_path,
        "export_dpo": dpo_path,
        "judge_model": judge_model,
        "judge_driver": judge_driver,
        "judge_base_url": judge_base_url,
        "judge_api_key": judge_api_key,
        "judge_effort": judge_effort,
    }

    # 询问是否保存或更新预设
    save_prompt = "是否将本次配置更新保存到预设?" if is_editing else "是否将本次配置保存为本地预设 (下次可直接一键载入)?"
    save_it = questionary.confirm(
        save_prompt,
        default=True,
        style=CUSTOM_STYLE,
    ).ask()

    if save_it:
        default_pname = profile_name or f"{driver}_{model_id.replace(':', '_')}"
        saved_name = questionary.text(
            "输入预设名称 (例如 deepseek_official / gemini_local):",
            default=default_pname,
            style=CUSTOM_STYLE,
        ).ask().strip()
        if saved_name:
            profiles = load_profiles()
            profiles[saved_name] = config
            save_profiles(profiles)
            console.print(f"[green]✔ 已更新保存预设配置: {saved_name} (安全存储于 .bench_profiles.json)[/green]")

    return config


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
    table.add_row("断点自愈 (Resume)", "✔ 已开启 (启用 Prompt 快照重放)" if config.get("resume") else "✖ 未开启")

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

    # 构建 CLI 参数
    suites = config.get("suites", ["all"])
    suite_arg = "all" if len(suites) >= 4 else suites[0] if len(suites) == 1 else "all"

    cli_argv = [
        "--suite", suite_arg,
        "--model", config["model"],
        "--driver", driver,
    ]

    if base_url:
        cli_argv.extend(["--base-url", base_url])
    if api_key:
        cli_argv.extend(["--api-key", api_key])

    if config.get("effort"):
        cli_argv.extend(["--effort", config["effort"]])

    if config.get("resume"):
        cli_argv.append("--resume")

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

    # 如果选了多个但不是全部，按选中的逐个跑
    try:
        from benchmark_v3.bench_harness.cli import main as cli_main
    except ImportError:
        from bench_harness.cli import main as cli_main

    console.print("[bold green]✔ 正在初始化执行环境，切入实时评测渲染流...[/bold green]\n")

    if len(suites) > 1 and len(suites) < 4:
        # 多套件串行执行
        exit_code = 0
        for s in suites:
            sub_argv = list(cli_argv)
            sub_argv[1] = s
            code = cli_main(sub_argv)
            if code != 0:
                exit_code = code
        return exit_code
    else:
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
