# Benchmark v3 工作总表

> **顺序锁死**：修 bug → 方法论 → 容器探索 → fable 讨论 B 组 prompt。  
> **题目增减 / A·B 编排**：本表 Phase 2 已拍板，不再等外部定题。  
> **B 组 prompt 正文**：不在本阶段手写，留给 Phase 4 与 fable 对稿。  
> Bug 细节见 [`KNOWN_BUGS.md`](KNOWN_BUGS.md)。

---

## 总待办

- [x] **Phase 1** 修 `bench_harness` 已确认实现 bug（B1–B7）
- [x] **Phase 2** 推进方法论：冻结题单、A/B 条件开关（`short_b` / `long_b`）
- [ ] **Phase 3** 容器探索：只做可行性结论，默认不落地
- [ ] **Phase 4** 启用 fable，共同制定 **B 条件** 的 prompt / frozen-plan 方案

本目录外的 v2 旧包不在本表。测试榜 `0/9` 不修。

---

## Phase 1 — 修 bug

| ID | 项 | 状态 |
| :--- | :--- | :--- |
| B1 | CLI 部分失败仍 exit 0 | 已改 |
| B2 | 快照写 / 清 / 续跑 / self_test 路径不一致 | 已改 |
| B3 | `prepare_task` 之后补 Git baseline（空仓 init 无效） | 已改 |
| B4 | Critic `bitpack.py` 金标准与 fixture 对齐 | 已改 |
| B5 | SPEC / docstring 断言计数与实现对齐 | 已改 |
| B6 | `get_suite()` 转发 `judge_driver` | 已改 |
| B7 | Response driver 补齐代理与默认头 | 已改 |

---

## Phase 2 — 方法论

**已落地**：TUI / CLI 在四个 A 套件之外增加 `short_b`、`long_b`。同一套题、同一套隐藏断言；B 多一份 `PLAN.md`。`bench-run --suite all` 仍只跑四个 A 条件，避免默认定价翻倍。B 成绩写入 `task@b` 槽。总榜评分点把 A（最多 66）和 B（最多 50）**加进同一总数**（满测 116）；**综合指数 = 已得评分点 / 总数 × 100**。覆盖列写成 `A n/9 · B m/5`。

可选入口：

```text
--suite short | short_b | long | long_b | reviewer | critic | all
```

### 横向扩展（先不拆大架构）

当前 **不必** 做成插件式微服务。已经够用的接缝：

| 要加的东西 | 怎么加 | 先别做 |
| :--- | :--- | :--- |
| 新题 | 家族 suite 里加 `TASK_IDS` + prepare/evaluate | 新仓库、新进程 |
| 新条件（如 reviewer_b） | `catalog.py` 加一行 `Runnable` | 复制一整份 suite 类 |
| 新套件家族 | 新 `SuiteAdapter` 子类 + registry + catalog 一行 | 改生命周期基类 |
| 容器 | 以后只包 `ProcessRunner` / `bash` 工具，suite 不改 | 现在上 Docker |

可选项、汇总分组、主榜 A 题单、TUI 勾选都读 `suites/catalog.py`。以后加 `reviewer_b` 只改 catalog 一行 +（如有）registry 类。容器要等 Phase 3 结论，再抽 `Runtime` 接口，不提前拆。

---

### 题单：冻结 9 题，现阶段不增不减

| 套件 | 保留 | 不加 | 不砍 |
| :--- | :--- | :--- | :--- |
| short | `varint_parser` / `timing_wheel` / `lexer_state_machine` | 不再加第四道算法题 | 三轴（内存 / 均摊复杂度 / 容错）各一，砍掉会缺维 |
| long | `raft_cluster` / `saga_coordinator` | 不加第三服务 | 共识 vs 分布式事务成对；ENOSPC / PID 复用是**旧题加故障**，不是新题 |
| reviewer | `lock_ordering` / `api_drift` / `bait_guard` | 不加第四靶 | 死锁 / 漂移 / 诱饵已经覆盖 Reviewer 三维 |
| critic | `audit_bundle`（4 缺陷 + 2 诱饵） | 不拆成 6 道微题、不加带安全标签的 CWE 题 | 拆题会爆成本；带标签安全题会把拒答和能力不足搅在一起 |

v2 四套件题目（`recursive_patch`、`order_fulfillment` 等）**不迁入** v3 harness。对照包只作过渡期并行评测。

饱和或地板效应出现后，再开「加减题」修订，不在校准前改题单。

### A/B：同一题单上的实验条件，不是两套题

| 条件 | 模型看到的 | 隐藏 oracle |
| :--- | :--- | :--- |
| **A** | 行为契约 `TASK.md` + 工作区脚手架 | 与 B 完全相同 |
| **B** | A 的全部内容 + 一份 **frozen plan / B prompt** | 与 A 完全相同 |

区分度必须来自「知道做什么 ≠ 做对」。B 计划只写施工顺序，硬约束只指向 `TASK.md`（模型用已有 `read` 工具读规格）；**禁止**复述契约、禁止贴参考实现、禁止泄露隐藏断言 / 种子 / 行号 GT。

套件差异（Phase 4 再写成具体 prompt）：

- short / long / reviewer：实施计划（步骤、不变量、资源门限）
- critic：审查清单（怎么读、什么算缺陷、什么算诱饵），不是实现计划

默认实验：同一模型 A、B 各跑 Pass@1，先比执行分，再看 B−A 增量。B 不是「送分通道」。

### 本阶段还要写进方法论文档的口径

- Critic：judge ≠ 被测模型；无 judge 的 run 标 `provisional`，不进主榜
- 时间继续只做遥测；Token 只做次级排序
- Pass@k / 多次 trial：方法论里预留开关，默认仍 Pass@1
- 锚点模型与 v2 分数映射：题单和 A/B 开关稳定后再做

---

## Phase 3 — 容器探索（默认不落地）

只回答三个问题，写成短结论，不默认开工：

1. Windows 宿主机 + Docker Desktop 是否值得为当前「可信 API 模型写 Python」威胁模型付钱
2. 只隔离 `evaluate()`，还是整场 session（含 `bash` 工具）进容器
3. Jepsen SIGKILL / 三进程 Raft 是否必须和评测同容器

倾向：威胁模型不变就不做；若要做，粒度是「一题一 Linux 容器、`network=none`」，工期见此前评估（0.5–2 天 + Jepsen 约 1 天）。

---

## Phase 4 — fable 讨论 B 组 prompt

题目与 A/B 规则按 Phase 2 冻结后，拉 fable 共同起草：

- 各套件 B 条件的 prompt / frozen-plan 模板
- 泄露检查清单（不得出现参考代码、种子、隐藏断言名）
- 与 A 条件的 diff 应只有「多一份计划」
- Critic B 的审查清单语气（去安全词汇、不要求 PoC）

本阶段产出是方案，不是先改评分器。

---

## 修订记录

| 日期 | 说明 |
| :--- | :--- |
| 2026-09-17 | 初稿：四阶段顺序；冻结 9 题；A/B 定为同题条件 |
