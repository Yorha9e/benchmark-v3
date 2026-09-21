# v3 Harness 已知缺陷与修复待办

> **总表**：阶段顺序与题单决策见 [`ROADMAP.md`](ROADMAP.md)。  
> **范围**：仅 `bench_harness/` 与其直接依赖的文档/金标准。  
> **不在本表**：测试榜覆盖率 `0/9`（后续可能清空重测）、评测方法论（题目定稿后另开计划）、容器沙箱（见文末「暂缓」）。  
> **状态**：先登记，后改。勾选表示已合入。

---

## 待办清单

- [x] **B1** CLI 部分失败仍返回 exit 0
- [x] **B2** Prompt 快照写入路径与清除/续跑路径不一致
- [x] **B3** Workspace 默认不建 Git baseline，AST 微创 diff 常为空
- [x] **B4** Critic 诱饵 `bitpack.py` 与金标准卷宗不是同一份代码
- [x] **B5** SPEC / 模块 docstring 的断言计数与实现不一致
- [x] **B6** `get_suite()` 不转发 `judge_driver`，程序化拉起 Critic 无法挂裁判
- [x] **B7** Response driver 缺少 OpenAI driver 已有的代理 / 默认头
- [ ] **B8** 进程自杀与宿主解释器内直接 `exec_module` 逃逸（主进程暴毙元凶）
- [ ] **B9** 缺少作业对象（Job Object）导致模型后台测试节点残留堆积
- [ ] **B10** `MasterLeaderboard.save_data` 未持锁导致全量覆盖与丢失更新
- [ ] **B11** Windows 下 `_read_stores` 并发瞬时句柄锁导致收敛误判（WinError 32）
- [ ] **B12** Critic 诱饵扣分实现与金标准脱节（纯 info 关键词误扣 10 分）

---

## 缺陷说明

### B1 — CLI 部分失败仍返回 0

- **文件**：`bench_harness/cli.py`（`main` 末尾 return）
- **现象**：有 `reports` 但未全部 `passed` 时仍 `return 0`。
- **影响**：接 CI / 脚本时无法用退出码判断评测失败。
- **建议**：全过返回 0；有失败返回非 0；无 report 返回非 0。

### B2 — 快照写 / 清 / 续跑路径不一致

- **文件**：`bench_harness/suites/base.py`、`bench_harness/core/snapshot.py`
- **现象**：
  - 执行循环写入 `SnapshotManager(paths.root)` → `{run}/{suite}/{task}/last_prompt_snapshot.json`
  - 成功后 `clear()` 打在 `workspace.workspace_dir`
  - SPEC 写的是 `workspace/last_prompt_snapshot.json`
  - `self_test` 检查的是 workspace 子路径，与真实写入位置不一致
- **影响**：`--resume` 与成功清理不可靠，快照可能残留或清错文件。
- **建议**：三处统一到同一路径，并改 self_test 跟着走。

### B3 — Git baseline 默认关闭

- **文件**：`bench_harness/core/workspace.py`、`bench_harness/suites/base.py`
- **现象**：SPEC 要求 Aider 式 Git 跟踪；`setup()` 若在 `prepare_task` 之前 `git init`，baseline 是空树。`record_baseline()` 常为 `None`。
- **说明**：Reviewer 的 AST 微创目前对比内存中的 buggy 原文，不读 git；baseline 仍供 diff / 复盘 / 后续 Gate。
- **修复**：`prepare_task` 之后调用 `ensure_git_baseline()`（`git -c user.*` 本地身份，不写用户 gitconfig；失败则跳过）。

### B4 — `bitpack.py` 金标准漂移

- **实现**：`bench_harness/suites/critic.py`  
  当前 fixture 是 `pack(red, green, blue)`，三通道 12-bit 打进一个 int。
- **文档**：`docs/CRITIC_GOLD_STANDARDS.md` 诱饵二仍是 `pack_pair(ch_a, ch_b)` 三字节打包。
- **影响**：Judge 卷宗、伪修复红线与真实题目对不上，depth / 诱饵误报会漂。
- **建议**：以 harness fixture 为准改卷宗，或把 fixture 改回卷宗；禁止两套并存。

### B5 — 文档计数过期

| 出处 | 写的是 | 实际 |
| :--- | :--- | :--- |
| `bench_harness/suites/short_task.py` 模块头 | 3 题 × 4 断言 = 12 | 3 题 × 10 = 30 |
| `docs/BENCHMARK_HARNESS_SPEC_V3.md` 短任务 | 12 项断言 | 30 |
| SPEC Reviewer | 8 项压测 | 3 任务 × 4 milestone = 12 |

- **建议**：实现冻结后只改文档，或文档改完再锁实现，不要两边各写各的。

### B6 — `get_suite()` 丢 Judge

- **文件**：`bench_harness/suites/__init__.py`
- **现象**：CLI 给 `CriticSuite` 传了 `judge_driver`；`get_suite()` 工厂没有该参数。
- **影响**：TUI / 脚本走工厂时 Critic depth 只能启发式兜底。

### B7 — Response driver 能力缺口

- **对照**：`openai_driver.py` 有 httpx 代理与 `DEFAULT_HEADERS`；`response_driver.py` 没有。
- **影响**：同一套本地网关 / 代理配置下，切 Responses 协议可能连不上或丢自定义头。

### B8 — 进程自杀与宿主内 `exec_module` 逃逸

- **文件**：`bench_harness/suites/base.py`、`bench_harness/suites/reviewer.py`、`bench_harness/suites/long_task.py`
- **现象**：
  1. 模型在 bash 中调用 `taskkill /F /IM python.exe` 导致评测主进程被连带秒杀；
  2. `reviewer.py:380` (`_load_module`) 与 `long_task.py:1705` 在宿主解释器直接 `import` 模型 deliverable，若代码含 `sys.exit()`，作为 `BaseException` 会逃逸 `except Exception` 导致评测器瞬间安静退出。
- **修复**：`base.py` 加 `_bulk_kill_reason` 拦截网；探针捕获 `BaseException`，长期改写为类似 `short_task.py` 的独立子进程隔离执行。

### B9 — 缺少作业对象（Job Object）导致孤儿节点堆积

- **文件**：`bench_harness/core/runner.py`、`bench_harness/core/workspace.py`
- **现象**：长任务模型后台启动的测试子进程脱离后不会被自动回收，在 Windows 下长期占用端口和文件句柄，反向逼迫模型执行全量进程扑杀。
- **修复**：引入 Windows Job Object（`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`），任务结束由操作系统内核原子擦除所有衍生孤儿进程。

### B10 — `save_data` 未持锁导致丢失更新

- **文件**：`bench_harness/core/report.py:186`、`rescore_*.py`
- **现象**：`MasterLeaderboard.save_data` 未加排他文件锁，且全量覆盖写；若离线复算与实时跑测并发执行，内存旧快照会直接冲掉实时跑出的新成绩。
- **修复**：加排他文件锁；落盘前重新 `load_data()` 执行原子增量合并（merge）。

### B11 — Windows 下 `_read_stores` 并发瞬时句柄锁导致误判

- **文件**：`bench_harness/suites/long_task.py:591` (`_read_stores`)
- **现象**：节点高频 `os.replace` 写 `state.json` 时，评测器并发读取在 Windows 下抛 `WinError 32`，直接异常降级为 `{}`，在 `_wait_converged` 最后轮询时被误判为状态分裂（扣 0.5 分）。
- **修复**：在读取时加入 3 次 30ms 的短暂休眠重试，平滑文件锁竞争。

### B12 — Critic 诱饵扣分实现与金标准脱节

- **文件**：`bench_harness/suites/critic.py:338` (`_is_defect_claim`)
- **现象**：金标准规定仅当 severity >= Low 时扣分，代码却无视 severity 直接匹配 `collision` 等词根，导致对诱饵文件的好心设计说明（info/note）被误扣 10 分。
- **修复**：限定词根匹配仅在 `severity in ("low", "medium", "high", "critical")` 时生效。

---

## 暂缓（不是本表的修复项）

| 项 | 原因 |
| :--- | :--- |
| 榜单 `tasks_covered = 0/9`、slot 迁移 | 测试榜，后续可能清空重测 |
| A/B 通道、Instruction Gate、锚点校准、Pass@k | 题目定稿后另开计划（拟用 fable 对稿） |
| 容器 / 强隔离沙箱 | 见对话结论：当前偏重，先维持进程内隔离 |
| 本目录外的 v2 旧包 | 过渡期并行评测，不在本仓库工作区改 |

---

## 修订记录

| 日期 | 说明 |
| :--- | :--- |
| 2026-09-17 | 初稿：登记 B1–B7 |
| 2026-09-17 | B1–B7 已改；cli/base/critic/reviewer self_test 全过 |
