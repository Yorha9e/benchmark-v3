# Closed-loop v2 长任务 Benchmark 可复用包

本目录是从 `closed_loop_v2/` 提取的独立、只读基线包，用于之后在新 session、新 workspace 中测试其他模型。它不覆盖、不引用本目录之外的历史候选 workspace、agent wire 或冻结结果。

## 目录内容

- `template/`
  - `BRIEFING.md`：候选最终响应 schema 与任务边界。
  - `TASKS.md`：订单履约和 durable delivery spool 的完整题目契约。
  - `src/`：干净的候选初始源码模板。
  - `tests_public/`：公开 smoke tests。
- `evaluator/`
  - `criteria.json`：20 个二元验收 criterion、10 个 milestone、2 个项目。
  - `test_*.py`：隐藏机械验收测试。
- `harness/`
  - `prepare_runs.py`：从模板创建干净 workspace，并拒绝覆盖已有运行目录。
  - `evaluate.py` / `evaluate_all.py`：隔离执行 evaluator 并生成原始结果。
  - `instruction_audit.py`：InstructionGate 和修改边界检查。
  - `extract_usage.py`：从候选 agent wire 汇总 usage。
  - `freeze_assets.py`、`build_report.py`、`compare_rounds.py`：冻结、报告和比较辅助工具。
- `validation/`
  - `run_validation.py`：reference 20/20、一致性和 targeted mutant validation。
  - `reference/`、`mutants/`：验证用参考实现和变异定义，不应复制进候选 workspace。
- `results/`
  - `BRIEFING.md`、`frozen-phi3-plan.md`：preflight 所需的冻结输入。
  - `posthoc/`：补充候选记录/重评分所需的脚本依赖。
- `supplemental/`
  - `record_candidate.py`：以 descriptive-only 方式登记补充候选。
  - `build_comparison.py`：生成严格/宽松比较表；需要先提供对应的原始评测和 usage 输入。
  - `results/repair-ranking-protocol.md`、`overall-leaderboard.md`：排名规则和结果解释。
- `manifest.json`、`asset-hashes.json`、`design.md`、`SINGLE_MODEL_LONG_TASK_PROTOCOL.md`：版本、完整性和实验协议。

没有复制以下历史或运行时内容：`runs/`、历史 `supplemental/runs/`、历史候选 `results/*/evaluation*.json`、agent wire、缓存、`.pyc` 和 pytest 产物。这样不会把既有冻结实验数据混入新的测试基线。

## 复用流程

在本目录根目录执行。Python 要求为 `>=3.11`。

### 1. 先做基线验证

```bash
python -B validation/run_validation.py
```

该命令应验证参考实现的 20 个 criterion、两次输出一致性，以及至少 10 个 targeted mutants 被检测。失败时不要继续派发模型。

### 2. 创建新运行 workspace

默认 manifest 中保留了原始十个示例 slot。创建 workspace 前，必须复制本目录为新的 session-specific benchmark copy，或制作一份新的 manifest；不要修改这个基线包的冻结文件。

```bash
python -B harness/prepare_runs.py
```

如果只测一个新模型，推荐：

1. 复制整个 `longtaskbenchmark/` 为新的实验目录；
2. 在实验副本的 `manifest.json` 中新增唯一 slot 和准确的 `provider/model` 身份；
3. 按 `SINGLE_MODEL_LONG_TASK_PROTOCOL.md` 登记 `single-run-manifest.json`；
4. 从 `template/` 新建 `runs/<slot>/workspace/`，不得复用历史 workspace。

候选只能读取/修改新 workspace 中的：

```text
BRIEFING.md
TASKS.md
src/order_fulfillment/
src/delivery_spool/
```

实际允许修改的源码边界只有两个 package；不得把 evaluator、harness、reference、mutation、results 或其他候选目录放进候选 workspace。

### 3. 评测单个候选

```bash
python -B harness/evaluate.py \
  --workspace runs/<slot>/workspace \
  --slot <slot> \
  --output results/<slot>-evaluation.json
```

评测前冻结 workspace 的 before tree，评测后确认候选没有被修改。不要为了修复结果修改已经评测过的 workspace；需要重跑时建立新实验目录。

### 4. 记录 usage

候选 token 只从该候选的原始 agent wire 汇总：

```text
inputOther + inputCacheRead + inputCacheCreation + output = inference_tokens
```

缺失 usage、JSONL 损坏或 reported model 与 expected model 不完全一致时，必须记录 `indeterminate`，不能当作 0。

### 5. 评分含义

- `InstructionGate`：是否遵守文件边界、依赖、禁止行为、签名和报告约束。
- `criterion_pass_count`：20 个 criterion 中通过的数量。
- `AcceptanceCoverage`：`criterion_pass_count / 20`。
- `RawMilestoneCount`：每个 milestone 的两个 criterion 都通过的数量，不考虑 Gate。
- `MilestoneStrictCount`：同时满足两个 criterion 和 Gate 的 milestone 数量。
- `ClosedLoopProjectCount`：五个 milestone 都 strict-success 的项目数量。

严格榜使用：

```text
InstructionGate ↓
ClosedLoopProjectCount ↓
MilestoneStrictCount ↓
AcceptanceCoverage ↓
TokensPerStrictMilestone ↑效率优先
```

严格榜中 Gate FAIL 或 token 无法确认的记录不排名。宽松榜使用：

```text
RawMilestoneCount ↓
AcceptanceCoverage ↓
InstructionComplianceRate ↓
TokensPerPassedCriterion ↑效率优先
```

宽松榜展示 Gate，但不把 Gate 作为硬归零条件。两者都不使用主观 LLM 质量分，也不把主代理/计划编排 token 混入候选 token。

## 补充候选和比较

` supplemental/record_candidate.py` 只适用于按协议保留完整 raw evidence 的 descriptive-only 补充结果。它依赖实验副本中的 `harness/`、`results/posthoc/` 和准确的 surrogate slot，不应把一个模型静默改名成另一个模型。

当实验副本已经准备好原始 post-hoc evaluation、usage 和 supplemental result 目录后，可运行：

```bash
python -B supplemental/build_comparison.py
```

该脚本生成 supplemental 严格/宽松 CSV 和比较报告；它不应覆盖正式冻结实验结果。正式榜单发布前应另外审阅 `SINGLE_MODEL_LONG_TASK_PROTOCOL.md` 的停止规则和模型身份证据。

## 冻结原则

- 本目录是可复用基线，不在此直接追加模型结果。
- 新模型使用新 slot、新 session-specific copy 和新 runs/results 目录。
- 不覆盖历史 `closed_loop_v2/`、`prototype/finalresultpdf/longtaskresult` 或其他冻结数据。
- 如果 frozen plan、briefing、reference validation、asset hash、workspace 初始状态或 model identity 无法核验，应标记 blocked/indeterminate，而不是放宽评分规则。
