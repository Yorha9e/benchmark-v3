# Reviewer Benchmark Package

这是一个可复用的 Reviewer benchmark 静态包，供后续模型执行 B11 `dependency_layers.py` 初步 debug 任务。包内分为公开测试材料和 controller 侧隐藏评分资产，不包含任何历史模型运行结果或排行榜。

## 目录结构

```text
reviewerbenchmark/
├── README.md
├── package-manifest.json
├── public-template/
│   ├── DEBUG-TASK.md
│   ├── TASKS.md
│   ├── public_smoke_tests.py
│   └── solutions/
├── protocol/
│   ├── B11-bug-report.json
│   ├── DEBUG-TASK.md
│   ├── TASKS.md
│   ├── frozen-phi3-plan.md
│   ├── public_smoke_tests.py
│   └── supplemental_debug_checks.py
└── controller/
    ├── scoring/SCORING.md
    ├── reviewer_evaluator/
    └── short/
        ├── manifest.json
        ├── harness/
        ├── evaluator/
        └── validation/reference/
```

## 给被测模型公开的内容

被测模型只应接收 `public-template/` 中的内容：

- `TASKS.md`：四个模块的完整公开契约；
- `DEBUG-TASK.md`：B11 背景、失败项和建议测试场景；
- `public_smoke_tests.py`：基础 smoke 测试；
- `solutions/` 下的初始候选实现。

模型应只修改 `public-template/solutions/` 中的四个文件，并按照 `TASKS.md` 创建 `instruction_ack.json`。不得读取 `controller/` 或其他 benchmark 目录。

## Controller 侧资产

`controller/` 属于评测器侧，不应复制到模型可读 workspace：

- `controller/scoring/SCORING.md`：Instruction Gate、16 个官方 criterion、资源/扩展检查、8 项 supplemental 检查及严格/宽松榜排序规则；
- `controller/reviewer_evaluator/`：外置 Reviewer evaluator、校验器、排名器和 schema；
- `controller/short/`：冻结的官方 short benchmark manifest、criterion runner、Instruction Gate、官方 checks、reference solution 和资源检查所需的评测实现。

`controller/short/validation/reference/solutions/` 是 ground truth，必须保持隐藏。公开包不把这些文件暴露给被测模型。

## 推荐测试流程

1. 为每个模型创建新的包外 workspace，并复制 `public-template/`；不要直接修改本 package。
2. 为该模型写入独立的 `cell.json`/run metadata，绑定模型和 slot。
3. 只把 `public-template/` 内容提供给模型，要求完成四个模块并生成 `instruction_ack.json`。
4. 运行 `python -B public_smoke_tests.py -v` 作为基础检查。
5. 由 controller 侧 evaluator 逐项运行 16 个官方 criterion、2 个资源检查、2 个扩展检查、Instruction Gate 和 8 项 supplemental 检查。
6. 将模型输出、workspace 快照、计时和 usage 保存到包外独立 run/result 目录。
7. 使用 `controller/scoring/SCORING.md` 的严格榜或宽松榜规则生成排名。

不要因为公开 smoke 通过就判定任务完成；`dependency_layers.py` 的隐藏边界、复杂度和跨 hashability 行为必须通过正式 evaluator 验证。token usage 若不可用，记录 `unavailable`，不要估算。

## 冻结与可复用边界

本包只保存静态题目、初始模板、协议和 controller evaluator，不包含：

- `subtest_1`–`subtest_15` 或 `phi_2` 的历史结果；
- reviews、leaderboards、score summaries、timing events；
- 历史 workspace、原始对话和 Provider failure 日志；
- Python cache、`.pyc` 或 `.pytest_cache`。

后续新增模型应使用新的 run id 和包外结果目录，以免污染 benchmark 静态资产。
