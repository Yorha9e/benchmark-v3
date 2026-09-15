# Benchmark 通用简易 Harness 架构与接口规范 (v3)

> **版本**：v3.0 Specification  
> **定位**：跨短任务、长任务、Reviewer、Critic 四维度的统一轻量评测底座  
> **核心原则**：
> 1. **Aider 式极简底盘**（轻量无包袱、Git 跟踪、单次隔离运行、Pass@1 循环、统一原子结果）
> 2. **Jepsen 式混沌内核**（声明式故障时间线、外部真实 SIGKILL、网络分区、不变量验证）
> 3. **一等公民微调数据管线**（流式捕获 Agent 交互轨迹，对齐评测标签，直出 SFT 黄金集与 RL 偏好对）
> 4. **全 Python 进程内集成**（Python 3.11+ 标准库优先，多协议驱动进程内原生接入，零 Node/零外部边车服务）
> 5. **透明人机交互反馈**（终端实时多行状态渲染，旁路 `live_status.json` 原子监控，告别黑屏盲跑）
> 6. **单轮 Prompt 快照续跑与自适应重试**（原子维护上一轮请求快照，断网/故障时一键重放自愈；Driver 级指数退避）
> 7. **时间纯遥测与 Token 效率审计**（彻底去除时间计分，耗时仅供复盘筛选；Token 全量审计但不扣除功能分，设 10M 熔断线）

---

## 1. 架构总览与目录结构

```text
bench_harness/
├── __init__.py
├── cli.py                     # 统一命令行入口：bench-run --suite long --model k3-max [--resume]
│
├── core/                      # [Aider 风格] 基础沙箱、执行控制、静态分析与交互反馈
│   ├── __init__.py
│   ├── types.py               # 全局数据模型与类型注解 (DataClasses)
│   ├── workspace.py           # 工作区环境准备、Git 跟踪与变更提取
│   ├── runner.py              # 子进程安全调用、超时熔断与跨平台进程树终结
│   ├── ast_diff.py            # AST 语法树级改动度量（严惩推倒重写，奖励微创修补）
│   ├── memory_probe.py        # 基于 tracemalloc 的常驻内存峰值探针
│   ├── snapshot.py            # 单轮 Prompt 快照管理器 (原子写入、断点续跑重放)
│   ├── reporter.py            # 终端交互式实时状态行与 live_status.json 旁路更新
│   └── report.py              # 评测结果原子持久化与榜单聚合
│
├── drivers/                   # [多协议驱动] 进程内原生大模型与 Agent 适配器 (含重试退避)
│   ├── __init__.py
│   ├── base.py                # BaseDriver 统一抽象基类 (内置指数退避 + 抖动重试)
│   ├── openai_driver.py       # OpenAI / ChatCompletions 协议 (覆盖大部分模型)
│   ├── response_driver.py     # OpenAI Responses / 结构化长输出协议
│   ├── google_driver.py       # Google Gemini 原生 REST / google-genai 协议
│   ├── anthropic_driver.py    # Anthropic Messages 原生协议
│   └── agent_cli_driver.py    # 外部黑盒 Agent CLI 驱动器 (如 Kimi Code CLI / Aider)
│
├── jepsen/                    # [Jepsen 风格] 分布式与混沌故障注入引擎
│   ├── __init__.py
│   ├── supervisor.py          # 多节点进程生命周期托管 (含外部真实 SIGKILL)
│   ├── broker.py              # 文件消息总线与链路故障注入 (丢包/延迟/乱序/分区)
│   ├── nemesis.py             # 混沌调度器 (按声明式时间线执行故障计划)
│   └── checker.py             # 系统不变量分析器 (单Leader/已提交不丢/最终一致)
│
├── trace/                     # [微调管线] Agent 交互轨迹采集、标注与训练集导出
│   ├── __init__.py
│   ├── collector.py           # 会话线缆（wire.jsonl）实时流式捕获
│   ├── normalizer.py          # 轨迹格式标准化（ChatML / OpenAI Tool Calls 格式）
│   ├── annotator.py           # 奖励与里程碑精准对齐标注（Reward / 失败步归因）
│   └── exporter.py            # 训练集导出器（SFT 黄金集 / RL/DPO 偏好对 JSONL）
│
└── suites/                    # [套件适配] 四大评测维度具体任务实现
    ├── __init__.py
    ├── base.py                # SuiteAdapter 统一抽象基类
    ├── short_task.py          # 次世代短任务适配器 (微引擎、零拷贝、内存硬限制)
    ├── reviewer.py            # Reviewer 适配器 (死锁修复、AST 最小改动、防诱饵误报)
    ├── critic.py              # Critic 盲审适配器 (去标签化缺陷、格式校验、拒答拦截)
    └── long_task.py           # 次世代长任务适配器 (Raft 节点协同、Saga 分布式事务)
```

---

## 2. 全局生命周期与数据流图

```text
                  ┌────────────────────────────────────────┐
                  │ 评测任务启动 (SuiteAdapter.run_session) │
                  └───────────────────┬────────────────────┘
                                      │ 1. prepare()
                                      ▼
                      ┌───────────────────────────────┐
                      │ Workspace 初始化 (Git baseline) │
                      │ - 检查是否存在未完成的快照     │
                      └───────────────┬───────────────┘
                                      │ 2. execute()
                                      ▼
                      ┌───────────────────────────────┐
                      │ Agent 执行循环 (Tool-Use Loop) │
                      │  - 原子落盘: last_prompt.json │
                      │  - Driver: 指数退避重试 (429/5xx)
                      │  - Tools: Read/Write/Edit/Bash│
                      │  - TraceCollector: 流式捕获    │
                      │  - ProgressReporter: 实时进度  │
                      └───────────────┬───────────────┘
                                      │ 3. evaluate()
                                      ▼
                      ┌───────────────────────────────┐
                      │ 评测与混沌检验 (Evaluator)     │
                      │  - 里程碑增量落盘 (Checkpoint)│
                      │  - 短任务/Reviewer: 断言+探针  │
                      │  - 长任务: Jepsen 混沌时间线   │
                      └───────────────┬───────────────┘
                                      │ 4. finalize()
                                      ▼
        ┌─────────────────────────────┴─────────────────────────────┐
        ▼                                                           ▼
┌───────────────────────────────┐           ┌───────────────────────────────┐
│ 评测报告 (Summary Report)     │           │ 微调数据集 (Annotated Trace)   │
│ - 纯功能打分 (100% 剥离时间)  │           │ - SFT 黄金轨迹 (Pass@1)        │
│ - 完整 Token 计量与帕累托前沿 │           │ - RL / DPO 偏好对 (带失败归因) │
│ - 耗时纯遥测展示 (供筛选)     │           │ - 过滤高信息密度样本           │
└───────────────────────────────┘           └───────────────────────────────┘
```

---

## 3. 核心数据模型 (`bench_harness/core/types.py`)

```python
from dataclasses import dataclass, field
from typing import Any, Literal

# --- 1. Agent 交互轨迹 (微调核心资产) ---

@dataclass(frozen=True)
class ToolCallRecord:
    call_id: str
    tool_name: str
    arguments: dict[str, Any]      # 结构化调用入参

@dataclass(frozen=True)
class ToolResultRecord:
    call_id: str
    tool_name: str
    stdout: str
    stderr: str = ""
    exit_code: int = 0
    duration_ms: float = 0.0

@dataclass
class TrajectoryTurn:
    turn_index: int
    role: Literal["user", "assistant", "tool"]
    content: str = ""              # 纯文本内容
    thought: str = ""              # 模型 CoT 思考链 (Reasoning Content)
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    tool_results: list[ToolResultRecord] = field(default_factory=list)
    tokens: dict[str, int] = field(default_factory=dict)  # prompt, completion, reasoning

@dataclass
class AgentTrajectory:
    session_id: str
    task_id: str
    model_id: str
    turns: list[TrajectoryTurn] = field(default_factory=list)
    total_tokens: int = 0
    wall_time_seconds: float = 0.0


# --- 2. 评测打分、Token审计与遥测模型 ---

@dataclass(frozen=True)
class TokenAuditMetrics:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    tokens_per_passed_milestone: float = 0.0
    budget_exceeded: bool = False  # 是否触碰 10M 熔断红线

@dataclass(frozen=True)
class TelemetryMetrics:
    wall_time_seconds: float = 0.0      # 纯遥测元数据，不计入得分
    reasoning_time_seconds: float = 0.0 # 纯思考链生成耗时
    network_retry_count: int = 0        # 429/5xx 重试次数

@dataclass(frozen=True)
class MilestoneResult:
    milestone_id: str              # 如 "M7_split_brain"
    name: str                      # 如 "脑裂分区容灾验证"
    passed: bool
    score: float                   # 0.0 ~ 1.0
    failure_reason: str | None = None
    diagnostics: str = ""          # 详细错误堆栈/Trace上下文

@dataclass
class EvaluationReport:
    task_id: str
    model_id: str
    timestamp: str
    passed: bool                   # Pass@1 全局通过状态
    final_reward: float            # 综合功能得分 (0.0 ~ 1.0 或百分制, 彻底脱离时间)
    milestones: list[MilestoneResult]
    
    # 效率与安全审计（独立于得分）
    token_metrics: TokenAuditMetrics = field(default_factory=TokenAuditMetrics)
    telemetry: TelemetryMetrics = field(default_factory=TelemetryMetrics)
    
    # 扩展度量指标
    ast_diff_penalty: float = 1.0  # 1.0=微创, 越低表示推倒重写越严重
    peak_memory_bytes: int = 0     # 短任务 tracemalloc 采样峰值
    safety_refusal: bool = False   # 是否被安全围栏拦截拒答
```

---

## 4. 单轮 Prompt 快照重放与自适应重试机制 (`core/snapshot.py` & `drivers/base.py`)

### 1. 单轮 Prompt 快照原理 (`core/snapshot.py`)
- 在每次向底层 Driver 发送 API 请求前，将当前轮次的完整请求上下文**原子覆写写入**：
  `workspace/last_prompt_snapshot.json`：
  ```json
  {
    "turn_index": 5,
    "timestamp": "2026-09-15T10:30:00Z",
    "request": {
      "messages": [ /* 全量历史上下文 */ ],
      "tools": [ /* 当前工具声明 */ ]
    }
  }
  ```
- **断点自愈**：若会话因断网、超时、宿主断电崩溃，执行 `bench-run --resume` 时，Harness 检测到快照直接读取并重发该轮请求，成功后继续正常循环，**无需重放前序 N 轮**。

### 2. Driver 自适应重试 (`drivers/base.py`)
- 捕获瞬态错误：`HTTP 429 (Rate Limit)`、`500/502/503/504`、网络连接超时。
- 策略：指数退避加随机抖动，若有 `Retry-After` 则严格遵从；确定性错误（400/401）立刻熔断报错。

---

## 5. 评分原则与 Token 审计规范

1. **绝对脱钩时间**：
   - 彻底移除任何基于纯运行耗时的计分项。
   - 评测得分 100% 由功能正确性、并发死锁消除、不变量校验、AST 最小修改度、去标签化缺陷召回决定。
2. **Token 效率审计与帕累托前沿**：
   - 完整计量输入、输出、思考链及每里程碑 Token 单耗；
   - 作为榜单的**次级排序（Tie-breaker）**与**企业选型性价比参考**；
   - 设定单任务 10,000,000 Tokens 软熔断阈值，杜绝死循环账单爆炸。
3. **微调高质量数据过滤**：
   - 导出 SFT 黄金数据集时，自动按 `Tokens / Reward` 过滤，剔除低信噪比冗长轨迹。

---

## 6. 四大套件最终评分体系映射

| 套件维度 | 核心考点 | 计分标准 (满分) | 时间与Token处理 |
| :--- | :--- | :--- | :--- |
| **次世代短任务** (3题) | 零拷贝Varint解析、分层时间轮、容错Lexer | **12 项断言** (每题4项)，含 `tracemalloc` $\le 4\text{MB}$ 内存探针 | 耗时仅记录，超 10s 判超时；Token 审计 |
| **次世代长任务** (2题) | Raft 3节点共识复制、Saga 分布式事务协调 | **20 个里程碑** (40 项严格断言)，含外部真实 SIGKILL 与 Jepsen 分区 | 耗时仅记录；Token 审计与 10M 熔断 |
| **Reviewer 靶场** | 1% 低概率死锁、跨模块语义漂移、无锁诱饵代码 | **8 项压测断言** $\times$ **AST 微创系数** (推倒重写惩罚，诱饵误改判负) | 耗时仅记录；Token 审计 |
| **Critic 盲审** | 去标签化真实漏洞 (时序/路径/越界/性能) + 诱饵代码 | **百分制**：召回 55 + 诱饵防误报 20 + 深度 20 + 格式 5 | **0 分速度分**；耗时纯展示；安全拒答单独打标 |
