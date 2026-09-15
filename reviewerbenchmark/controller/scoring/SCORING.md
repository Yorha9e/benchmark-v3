# Reviewer Benchmark 评分系统

本文件属于 controller 侧，不应暴露给被测模型。它描述 B11 `dependency_layers.py` Reviewer benchmark 的可复用评分协议。

## 评测单元

每个模型使用一份干净的 `public-template/` 工作区，完成 `TASKS.md` 中四个模块：

- `recursive_patch.py`
- `dependency_layers.py`
- `ttl_set.py`
- `duration.py`

模型只能修改 `solutions/` 和按契约要求创建 `instruction_ack.json`。评测器使用独立进程逐项运行官方 criterion，并检查工作区是否越界修改。

## 得分维度

### 1. Instruction Gate

检查：

- 四个规定 solution 文件是否存在；
- `instruction_ack.json` 是否存在且字段、任务顺序和确认文本正确；
- 是否只修改允许的工作区内容；
- 是否遵守本地 debug、无网络等限制。

Instruction Gate 是严格榜第一排序键。Gate 失败的候选仍可出现在宽松榜，但不应在严格榜中优先于 Gate 通过者。

### 2. 官方功能 criterion

四个模块各 4 个 criterion，共 16 个：

- `recursive_patch`：删除身份、plain dict、深拷贝隔离、顺序边界；
- `dependency_layers`：一次性 iterable、dependency-only 节点、稳定顺序/环检测、深层迭代；
- `ttl_set`：严格类型、精确过期、容量行为、GC 引用释放；
- `duration`：严格语法、单位范围、规范化、结构化错误。

### 3. Resource capability

单独计分两个资源检查：

- `res_dependency_20000`
- `res_ttl_expiry_sweep`

资源检查必须在隔离进程中执行；单个超时不能把其他 criterion 的结果整体清零。

### 4. Extension capability

记录两个扩展检查：

- `ext_recursive_patch_nested_tuple`
- `ext_duration_large_carry`

### 5. Supplemental boundary checks

使用 `protocol/supplemental_debug_checks.py` 中的 8 项黑盒检查，重点覆盖跨 hashability 等值注册、hash collision、不可哈希对象、环阻塞节点、稳定层序和 50,000 节点深/宽输入。

## 排名

### 严格榜

按以下顺序降序/升序排序：

1. `InstructionGate`（通过优先）；
2. 四个任务全部完成的 `StrictTaskCount`；
3. 官方 criterion 通过数；
4. 扩展能力通过数；
5. 资源能力通过数；
6. 每个严格任务的 inference token 消耗（低优先）；
7. 每个通过 criterion 的 inference token 消耗（低优先）。

### 宽松榜

不把 Instruction Gate 作为第一道筛选，但保留 Gate 字段；排序使用：

1. `ExecutionCompleted`；
2. 原始 criterion 通过数；
3. Acceptance coverage；
4. Instruction compliance rate；
5. 扩展能力通过数；
6. 每个通过 criterion 的 inference token 消耗（低优先）。

当前工具链没有可靠 token usage 时，必须记录为 unavailable，不得估算或伪造。

## 运行与重试规则

- 每个模型必须使用新的隔离 workspace 和新的 run id；不得复用其他模型的已修改 workspace。
- evaluator 失败和内容质量失败要区分；基础设施失败可以按运行计划重试，但不能把空结果当作模型成绩。
- 所有结果、计时和 usage 写到 benchmark 包外的 run/result 目录；本 package 只保存静态题目和控制器资产。
- 公开 smoke 通过不代表官方 criterion 全部通过；正式排名以隐藏 evaluator、资源检查、Instruction Gate 和 supplemental 检查为准。
