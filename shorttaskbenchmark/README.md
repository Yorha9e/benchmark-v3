# Reusable short-task benchmark package

这是一个可复用的短任务测试包，包含题目、候选模板、Instruction Gate、criterion evaluator、运行 harness 和评分说明，供后续模型进行新的 A/B 测试。

## 重要原则

- `manifest.json` 是唯一权威协议；不要以旧文档中的模型数量描述覆盖 manifest。
- 本包只包含静态测试系统，不包含历史模型 workspace、旧榜单、旧 evaluation、`usage.json`、`events.jsonl` 或其他历史运行结果。
- 原冻结 benchmark 和历史归档仍保留在项目根目录的 `short/`、`shorttaskresult/` 及对应 supplemental 目录中。
- 候选模型只能看到 public task materials；不要把 `evaluator/`、隐藏 criterion 或 harness 内部检查文件放入候选 workspace。

## 内容

- `manifest.json`：A/B cells、4 个任务、16 个 official criteria、capability probes、资源预算、Instruction Gate 和 strict/lenient 排名轴。
- `TASKS.md`：公开任务说明。
- `template/`：候选 workspace 模板和 public smoke tests。
- `evaluator/`：Instruction Gate、official criteria 和 capability probe evaluator。
- `harness/prepare_runs.py`：根据 manifest 创建全新的 prepared workspaces。
- `harness/evaluate.py`：评测单个 candidate cell，输出 evaluation JSON。
- `harness/build_report.py`：从 evaluation JSON 重建 A/B 排名和 delta。
- `harness/freeze_assets.py`：验证静态资产 hash。
- `harness/common.py`：harness 共享工具，包括 usage 读取和原子 JSON 写入。
- `harness/tests/`：harness 自身测试和 usage fixtures。
- `validation/`：reference implementation、targeted mutants 和 evaluator 敏感性验证脚本；仅供 benchmark 维护者自测，严禁暴露给候选模型。
- `results/frozen-phi3-plan.md`：B 条件额外输入的共享实施计划。
- `docs/`：评分维度、模型能力和 evaluator 契约审计说明。
- `asset-hashes.json`：来源 benchmark 的冻结静态资产 hash 记录。

## 建议的外部测试流程

不要在本包目录内写 runs/results。为每个新模型或每次重跑创建独立 benchmark root，例如：

```text
my-short-run/
├── manifest.json       # 从本包复制后按需要建立新的 benchmark_id/cells
├── TASKS.md
├── template/
├── evaluator/
├── harness/
├── results/
└── runs/
```

准备 workspace：

```bash
python path/to/my-short-run/harness/prepare_runs.py \
  --output path/to/my-short-run/runs/prepared
```

执行单个 cell 的评测：

```bash
python path/to/my-short-run/harness/evaluate.py \
  --workspace path/to/my-short-run/runs/prepared/A/A01 \
  --condition A \
  --slot A01 \
  --output path/to/my-short-run/results/A01.json
```

生成报告：

```bash
python path/to/my-short-run/harness/build_report.py \
  --input path/to/my-short-run/results \
  --manifest path/to/my-short-run/manifest.json \
  --output path/to/my-short-run/results/report.json
```

A 条件只向候选提供 `TASKS.md` 和 `public_smoke_tests.py`；B 条件才额外提供 `results/frozen-phi3-plan.md`。每个候选只能修改四个 solution 文件和 `instruction_ack.json`，不得修改题目、cell metadata 或包外文件。

## 评分要点

- Strict ranking 首先看 Instruction Gate、完整任务数和 official criteria，再看 extension/resource probes 及 token efficiency。
- Lenient ranking 保留 raw criteria，用于区分“代码实现了多少”和“是否遵守交付协议”。
- Gate 失败不会删除 raw criterion 结果，但会将 strict task/official 分数清零。
- 缺失 token usage 必须保持 unavailable/null，不能当作零 token 或高效率。
- A/B 结果按匹配模型计算 B-minus-A delta，不折叠为单一加权分数。

## 冻结与版本

`asset-hashes.json` 是来源 `short/` 的旧冻结记录。当前来源后来加入 A15/B15 Composer cells，因此该记录中的 `manifest.json` hash 与当前 15 模型 manifest 不一致；其余 47 个冻结资产仍逐项匹配。本包使用 `package-manifest.json` 对当前复制内容进行完整 SHA-256 校验，后续复用应以当前 `manifest.json` 和 `package-manifest.json` 为准，不应声称旧 `asset-hashes.json` 已完整验证当前 manifest。

如果修改题目、evaluator、harness、criteria、ranking axes 或其他评分资产，必须建立新的 `benchmark_id` 和新的静态资产 hash；不要把新旧评测 JSON 混入同一正式榜单。
