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


def configure_judge_wizard() -> None:
    """交互向导：配置/编辑全局持久化的专家裁判模型"""
    current = load_judge_config()
    console.print("\n[bold cyan]>>> 配置默认全局专家裁判模型 (Global Default Judge)[/bold cyan]")
    console.print("[dim]设置后，所有评测运行将默认自动由该模型对 Critic 盲审深度进行 L1~L4 严格复核，无需每次重复输入。[/dim]\n")

    status_str = f"当前已启用: {current.get('model')} ({current.get('driver')})" if current.get("model") else "当前未配置 (默认使用严格启发式量表复核)"
    console.print(f"[bold white]状态:[/bold white] [bold green]{status_str}[/bold green]\n")

    enable = questionary.confirm(
        "是否启用全局默认专家裁判模型?",
        default=bool(current.get("model")),
        style=CUSTOM_STYLE,
    ).ask()

    if not enable:
        save_judge_config({})
        console.print("[yellow]✔ 已清空全局专家裁判配置，后续将默认使用严格启发式量表复核。[/yellow]\n")
        try:
            questionary.press_any_key_to_continue("按任意键返回主菜单...").ask()
        except (KeyboardInterrupt, EOFError):
            pass
        return

    driver = questionary.select(
        "选择裁判模型协议驱动 (Judge Protocol Driver):",
        choices=[
            Choice("Google Gemini (Gemini 2.0 Flash / Pro 官方协议)", value="google"),
            Choice("OpenAI 兼容协议 (DeepSeek / Qwen / OpenAI / Kimi)", value="openai"),
            Choice("OpenAI Responses 协议 (仅 Response 接口的推理模型)", value="response"),
            Choice("Anthropic Claude (Claude 3.7 Sonnet 官方协议)", value="anthropic"),
        ],
        default=current.get("driver", "google"),
        style=CUSTOM_STYLE,
    ).ask()

    default_model = current.get("model")
    if not default_model:
        if driver == "google":
            default_model = "gemini-3.7-flash-tiered"
        elif driver == "openai":
            default_model = "k3-max"
        elif driver == "anthropic":
            default_model = "claude-3-7-sonnet-20250219"

    model_id = questionary.text(
        "裁判模型标识 (Model ID):",
        default=default_model,
        style=CUSTOM_STYLE,
    ).ask().strip()

    default_j_base_url = current.get("base_url")
    if default_j_base_url is None:
        if driver == "google":
            default_j_base_url = os.environ.get("GEMINI_BASE_URL", "")
        elif driver in ("openai", "response"):
            default_j_base_url = os.environ.get("OPENAI_BASE_URL", "")
        elif driver == "anthropic":
            default_j_base_url = os.environ.get("ANTHROPIC_BASE_URL", "")

    base_url = questionary.text(
        "裁判模型 Base URL (留空使用官方默认，支持自定义反代/中转):",
        default=default_j_base_url or "",
        style=CUSTOM_STYLE,
    ).ask().strip()

    current_key = current.get("api_key", "")
    key_prompt = "裁判模型独立 API Key (留空沿用环境变量/主配置):"
    if current_key:
        key_prompt = f"裁判模型独立 API Key (已配置，直接回车沿用 {mask_key(current_key)}):"

    entered_key = questionary.password(
        key_prompt,
        style=CUSTOM_STYLE,
    ).ask()
    api_key = entered_key.strip() if entered_key.strip() else current_key

    proxy = questionary.text(
        "裁判网络代理 (留空不走代理):",
        default=current.get("proxy", "http://127.0.0.1:10808" if driver in ("google", "anthropic") else ""),
        style=CUSTOM_STYLE,
    ).ask().strip()

    effort = questionary.select(
        "裁判模型推理思考强度 (Reasoning Effort):",
        choices=[
            Choice("高 (High - 推荐深度思考评判)", value="high"),
            Choice("超高 (XHigh)", value="xhigh"),
            Choice("极限 (Max)", value="max"),
            Choice("中 (Medium)", value="medium"),
            Choice("默认 (Default / None)", value=None),
        ],
        default=current.get("effort", "high"),
        style=CUSTOM_STYLE,
    ).ask()

    judge_cfg = {
        "driver": driver,
        "model": model_id,
        "base_url": base_url,
        "api_key": api_key,
        "proxy": proxy,
        "effort": effort,
    }
    save_judge_config(judge_cfg)
    console.print(f"\n[green]✔ 全局专家裁判配置已持久化保存！[当前: {model_id} ({driver})][/green]")
    console.print("[dim]该配置已保存在本地 .bench_judge.json，后续所有评测将默认自动挂载该裁判模型。[/dim]\n")
    try:
        questionary.press_any_key_to_continue("按任意键返回主菜单...").ask()
    except (KeyboardInterrupt, EOFError):
        pass

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


def view_latest_report() -> None:
    """在终端中直接用 Rich Markdown 优雅渲染 LATEST_SUMMARY.md"""
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
        questionary.press_any_key_to_continue("按任意键返回主菜单...").ask()
    except (KeyboardInterrupt, EOFError):
        pass


def view_leaderboard() -> None:
    """在终端中直接用 Rich Markdown 优雅渲染全局权威总榜 LEADERBOARD.md"""
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
        questionary.press_any_key_to_continue("按任意键返回主菜单...").ask()
    except (KeyboardInterrupt, EOFError):
        pass


def profile_picker() -> dict[str, Any] | None:
    """首页：选择现有配置或创建新配置"""
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
            return configure_wizard(initial_config=profiles[edit_target], profile_name=edit_target)
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
    return configure_wizard()


def configure_wizard(
    initial_config: dict[str, Any] | None = None,
    profile_name: str | None = None,
) -> dict[str, Any]:
    """交互向导：配置厂商协议、密钥、套件、专家裁判和运行参数 (支持已有预设编辑更新)"""
    init = initial_config or {}
    is_editing = initial_config is not None

    if is_editing:
        console.print(f"\n[bold yellow]>>> 正在编辑预设: {profile_name} (直接回车保留原有值)[/bold yellow]")

    console.print("\n[bold cyan]>>> 第 1 步：选择模型协议驱动[/bold cyan]")

    driver_choices = [
        Choice("OpenAI 兼容协议 (DeepSeek / Qwen / Moonshot / OpenAI / vLLM)", value="openai"),
        Choice("OpenAI Responses 协议 (仅 Response 接口的推理模型)", value="response"),
        Choice("Google Gemini (官方 google-genai 2.x SDK 原生协议)", value="google"),
        Choice("Anthropic Claude (官方 anthropic SDK 原生协议)", value="anthropic"),
        Choice("本地 Agent CLI (调起本地命令行 Subagent 子进程)", value="cli"),
        Choice("本地离线 Mock (无需网络和密钥，用于流水线校验)", value="mock"),
    ]

    default_driver = init.get("driver", "openai")
    driver = questionary.select(
        "请选择被测协议驱动 (Protocol Driver):",
        choices=driver_choices,
        default=default_driver,
        style=CUSTOM_STYLE,
    ).ask()

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
        ],
        default=default_effort,
        style=CUSTOM_STYLE,
    ).ask()

    console.print("\n[bold cyan]>>> 第 3 步：勾选本次运行的评测套件[/bold cyan]")

    default_suites = set(init.get("suites", ["short", "long"]))
    selected_suites = questionary.checkbox(
        "选择要评测的维度 (空格选择，Enter 确认):",
        choices=[
            Choice("次世代短任务 (零拷贝Varint解析 / 分层时间轮 / 容错Lexer)", value="short", checked="short" in default_suites),
            Choice("次世代长任务 (三节点Raft脑裂断电 / Saga分布式事务)", value="long", checked="long" in default_suites),
            Choice("Reviewer 调试靶场 (并发死锁 / 跨模块语义漂移 / 诱饵防误报)", value="reviewer", checked="reviewer" in default_suites),
            Choice("Critic 代码盲审 (去标签化高危缺陷 / 无锁环形诱饵)", value="critic", checked="critic" in default_suites),
        ],
        style=CUSTOM_STYLE,
    ).ask()

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
            ],
            default=init.get("judge_driver", driver),
            style=CUSTOM_STYLE,
        ).ask()

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
            ],
            default=init.get("judge_effort", "high"),
            style=CUSTOM_STYLE,
        ).ask()

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
                view_leaderboard()
            elif post_action == "view":
                view_latest_report()
            elif post_action == "exit" or post_action is None:
                console.print("\n[bold cyan]👋 感谢使用 Benchmark v3，再见！[/bold cyan]\n")
                return exit_code

        except KeyboardInterrupt:
            console.print("\n[yellow]检测到 Ctrl+C 中断信号。[/yellow]")
            try:
                should_exit = questionary.confirm("确定要退出评测应用吗？", default=True, style=CUSTOM_STYLE).ask()
                if should_exit:
                    console.print("\n[bold cyan]👋 评测应用已安全退出。[/bold cyan]\n")
                    return 0
            except (KeyboardInterrupt, EOFError):
                console.print("\n[bold cyan]👋 评测应用已安全退出。[/bold cyan]\n")
                return 0


if __name__ == "__main__":
    sys.exit(run_tui())
