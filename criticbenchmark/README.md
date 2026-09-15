# Critic Benchmark Package

这是一个可复用的静态 Critic benchmark 包，用于比较不同 subagent 对两个固定、盲化候选产物进行单轮代码审查的能力。它只保存题目、公开输入、评分协议、控制 manifest、evaluator 和辅助检查脚本；历史运行结果不属于本包。

## 包含内容

- `public-inputs/candidate-A/`：带有已确认正确性与资源缺陷的候选输入。
- `public-inputs/candidate-B/`：正确性干净、但包含资源缺陷的候选输入。
- `protocol/`：Critic 任务说明、公开任务契约、公开 smoke test、补充黑盒检查和冻结实施计划。
- `scoring/`：控制器侧评分协议与 ground-truth manifest。该目录不应暴露给被测 Critic。

`public-inputs/` 是最小盲化审查输入，只保留三个允许公开文件；其中复制的通用 `public_smoke_tests.py` 默认面向包含完整 `solutions/` 的候选工作区，不能直接在这个最小输入目录中执行。Critic 的候选审查应使用目录中的 `dependency_layers.py`，并由控制器运行补充黑盒检查和隐藏复现。
- `evaluator/reviewer_evaluator/`：用于校验审查输出、运行评分检查和排名的 evaluator。该目录属于控制器侧。
- `package-manifest.json`：本包的文件清单、大小和 SHA-256，用于确认归档未被意外修改。

## 推荐测试边界

被测 Critic 只应读取：

1. `public-inputs/candidate-A/` 和 `public-inputs/candidate-B/`；
2. `protocol/` 中明确作为公开材料的任务说明和测试文件。

被测 Critic 不应读取 `scoring/`、`evaluator/` 或任何 ground-truth 细节。控制器应在包外建立独立的 run/result 目录，保存模型输出、复现结果、计时元数据和最终评分；不要回写本包。

每个模型对 candidate-A、candidate-B 各执行一次单轮只读审查。A/B 调用必须严格串行，不应重叠。建议使用 Agent task metadata 的 `started_at` 与 `ended_at` 计算端到端墙钟时长，包含排队、生成和工具调用；这不是纯模型生成时间。基础设施失败最多重试一次，内容质量差不重试。

## 评分摘要

总分 100：

- 召回 Recall：50 分，GT1 30 分、GT2 10 分、GT3 10 分；
- 误报纪律 Precision：20 分，初始 20 分，每个无法经验复现的 false positive 扣 5 分；
- 深度 Depth：15 分，按已确认 finding 的最高深度等级计分；
- 速度 Speed：10 分，按 A/B 两次端到端任务时长之和在完赛模型中的排名换算；
- 输出契约 Format：5 分，按两个目标是否产生严格、可解析且字段完整的 JSON 计分。

决胜顺序为：GT1 命中、召回总分、较少误报、较短总耗时。token 用量在当前工具层不可见，不纳入本 Critic 协议的分数。审查结果应先由控制器复现 claim，再区分真实缺陷、ground truth 命中和 false positive；近重复 finding 只计算一次。

## 候选与版本说明

本包固定使用 candidate-A 和 candidate-B 的公开输入及其输入哈希。当前来源的 `scoring/SCORING.md` 开头保留了 subtest_14 当时 Provider 失败、被排除的历史描述；之后曾有 high-effort 成功重跑。这里仅复用评分维度和协议结构，不复用任何历史结果。新的测试必须使用新的 run id，并独立记录实际失败、重试和排除状态。

本包不包含 `reviews/*.json`、`score-summary.json`、`timing-events.json`、旧排行榜、Provider failure 日志、历史 workspace、`subtest_15` 的 short A17/B17 结果、Python 缓存或字节码。

## 使用原则

- 只使用 Python 标准库；不要改名公开对象。
- Critic 输出应遵守协议要求的 JSON 格式，并明确区分 finding、证据、影响、复现状态和置信度。
- candidate-A/B 必须作为两个独立 cell 运行；不能把一个候选的结论迁移到另一个候选。
- 任何模型输出、计时和评分都写到包外的新 run 目录，保证本静态 benchmark 可重复复用。

详细规则以 `protocol/` 和控制器侧的 `scoring/SCORING.md` 为准；模型测试时只向被测模型公开允许的材料。
