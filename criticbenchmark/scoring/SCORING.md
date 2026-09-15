# 评分体系 — critic-model-select-v1

15 个 Critic 候选（subtest_1..subtest_14 + phi_2）；本轮 14 个完成可比评分，subtest_14 因两侧 Provider 失败被排除。每个单轮只读审查两个固定盲化目标：

- candidate-A（subtest_12 产物，含已确认缺陷）
- candidate-B（subtest_7 产物，正确性干净，仅资源缺陷）

同一 prompt、同一 critic profile、仅换 binding_slot；所有 A/B 调用严格串行，避免并发造成 Provider 竞争。每个 Agent 的 `started_at`/`ended_at` 由任务元数据提供，使用 `agent_task_metadata_wall_clock` 计时；速度使用两次 Agent 时长之和，包含任务排队、生成和工具调用。

## 总分 100

### 1. 召回 Recall — 50 分
| GT | 目标 | 类型 | 分值 |
|---|---|---|---|
| GT1 跨哈希域相等值去重缺失 | A | 正确性 | 30 |
| GT2 unhashable 注册表 O(N²) | A | 资源 | 10 |
| GT3 双向跨域扫描 O(N²) | B | 资源 | 10 |

命中判定看根因描述是否与 GT 一致（行号不作硬性要求）；同一 GT 的近重复 finding 折叠为一次命中。

### 2. 误报纪律 Precision — 20 分
起始 20 分，每个 FP −5，扣完为止。FP = 主控经验复现无法确认的 claim；GT 之外的新 claim 先验证再分类（真实缺陷不算 FP，也不加分）。干净目标 B 上的正确性指控是重点误报源。

### 3. 深度 Depth — 15 分
取两目标全部**已确认** finding 的最高 depth_level：L4=15，L3=12，L2=8，L1=4，L0=1，无确认项=0。

### 4. 速度 Speed — 10 分
按 A/B 两次 Agent 任务元数据时长之和在完赛模型中排名：`10 × (N − rank) / (N − 1)`，最快 10 分。每次时长均为 `ended_at − started_at`，包含该任务自身排队、生成和工具调用；这是端到端墙钟时间，不等同于纯模型生成时间。

### 5. 输出契约 Format — 5 分
每目标：严格 JSON 且字段完整 = 2.5；可解析但有偏差（围栏/缺字段）= 1；不可解析 = 0。两目标求和。

## 决胜顺序
GT1 命中 > 召回总分 > FP 更少 > 总耗时更短。

## 其他规则
- 基础设施失败（工具报错/429）重试一次；内容差不重试，按实际输出计分。
- 彻底失败的 cell 各维度记 0 并标注。
- 自审标记（subtest_12→A、subtest_7→B）仅记录用于分析，不调分。
- token 用量工具层不可见，记 unavailable。
- 若某模型的 A/B 两个 cell 在基础设施重试后均无输出，则记录失败元数据并从可比总榜排除，不把无内容伪装成有效审查分数。
