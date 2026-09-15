# 长任务单模型测试协议（Closed-loop v2）

版本：`closed-loop-v2-single-model-v1`

本文把原十模型长任务测试流程收缩为**单个模型、单次 Pass@1、两个中型后端闭环**，用于在另一个 session 独立测试一个模型。它不产生模型排名，也不把主代理/计划编排的消耗计入候选模型结果。

## 1. 测试目标和边界

本测试观察一个模型在同一 session 内完成两个相关但独立的后端闭环时的：

- 跨模块实现能力；
- 持久化与重新打开后的状态保持；
- 事务、幂等、状态机和并发语义；
- 失败恢复、锁和原子文件提交；
- JSON CLI 的端到端完成度；
- 指令遵循、资源消耗和验证覆盖。

本协议不测：

- 多轮修复能力；
- best-of-k 或人工补丁后的最好结果；
- 代码风格、注释、命名或可读性主观分；
- 与其他模型的排名；
- 模型的一般软件工程能力；
- 真实网络、支付服务或第三方依赖。

实验单位是：

```text
一个 model × 一个新 session × 一个干净 workspace × 两个项目 × 一次 Pass@1
```

## 2. 固定测试条件

### 2.1 候选模型

在另一个 session 中只选择一个 slot。slot 必须使用 `closed_loop_v2/manifest.json` 中已经登记的模型身份，不要临时改名或更改 Provider 前缀。

当前可用的 executor slot：

| Slot | 模型 |
|---|---|
| `subtest_1` | `MT/LongCat-2.0` |
| `subtest_2` | `deepseek/deepseek-v4-flash` |
| `subtest_3` | `deepseek/deepseek-v4-pro` |
| `subtest_4` | `kimi-code/kimi-for-coding` |
| `subtest_5` | `stepfun/step-3.7-flash` |
| `subtest_6` | `qwen/qwen3.8-max-preview` |
| `subtest_7` | `GPT/gpt-5.6-luna` |
| `subtest_8` | `volcano/doubao-seed-2.1-turbo` |
| `subtest_9` | `volcano/doubao-seed-2.0-pro` |
| `subtest_10` | `volcano/doubao-seed-2.0-code` |

如果测试的是其他模型，必须先建立新的 benchmark version 和新的 manifest，不要复用现有 slot 的 expected model 身份。

### 2.2 固定资源

- Python：`>=3.11`；
- 只允许 Python standard library；
- 候选 executor inference token 软上限：`10,000,000`；
- 该上限是事后资源审计，不要求模型刻意耗尽；
- 主代理、调度、计划制定、评测和报告 token 不计入候选 token；
- 不使用真实墙钟 sleep 作为正确性判定；
- evaluator 每个 criterion 默认安全超时：60 秒；
- 单个 workspace 只允许修改：
  - `src/order_fulfillment/`
  - `src/delivery_spool/`
- 禁止网络、第三方依赖、`subprocess`、动态 import、`eval`、`exec`、nested subagent；
- 禁止读取 evaluator、harness、reference、mutation、其他候选 workspace 或隐藏结果。

## 3. 两个待完成项目

候选必须在同一个 workspace、同一个 session 中依次完成两个项目。

### 项目 A：订单履约与库存预留

路径：`src/order_fulfillment/`

实现一个 Python service API 和 JSON CLI，核心业务流为：

1. 初始化或重新打开 SQLite 数据库；
2. 设置库存；
3. 通过 idempotency key 创建多商品订单；
4. 在一个事务中校验并预留全部库存，任一 SKU 不足时全部回滚；
5. 支付订单，处理 payment id 幂等与冲突；
6. 执行合法取消、发货状态迁移；
7. 取消时准确释放预留库存，已发货订单不可取消；
8. 关闭并重新打开 service 后状态仍然正确；
9. 多个 service/process 竞争最后库存时不得 oversell；
10. 通过 JSON CLI 完成真实端到端流程。

必须实现的公开对象：

```python
class OrderError(Exception): ...

class OrderService:
    def __init__(self, db_path, *, clock, id_factory): ...
    def set_stock(self, sku, quantity): ...
    def create_order(self, idempotency_key, items): ...
    def pay_order(self, order_id, payment_id, amount_cents): ...
    def cancel_order(self, order_id): ...
    def ship_order(self, order_id): ...
    def get_order(self, order_id): ...
```

CLI：

```text
python -m src.order_fulfillment --db PATH stock
python -m src.order_fulfillment --db PATH create
python -m src.order_fulfillment --db PATH pay
python -m src.order_fulfillment --db PATH cancel
python -m src.order_fulfillment --db PATH ship
python -m src.order_fulfillment --db PATH get
```

### 项目 B：耐久化 Delivery Spool

路径：`src/delivery_spool/`

实现一个基于本地文件的 durable outbox/worker lease 队列，核心业务流为：

1. 初始化调用方指定的 root；
2. 按 message id 幂等入队，冲突请求必须拒绝；
3. worker 按 `(available_at, sequence, message_id)` 顺序原子 claim；
4. 成功 claim 才增加 attempts，并返回 lease token；
5. 正确 token 可 ack，错误 token 不得泄露内部 token；
6. fail 按 retry delay 回到 pending，达到 max attempts 后进入 dead；
7. worker 崩溃后，过期 lease 可 recover，重复 recover 必须幂等；
8. 进程重开后状态、sequence 和排序保持一致；
9. 多进程竞争不能 double claim 或 lost update；
10. 使用同目录临时文件、flush/fsync、原子 replace 和 failpoint；
11. 损坏状态必须 fail closed，不能静默重置；
12. 通过 JSON CLI 完成 init/enqueue/claim/ack/fail/recover/show/list。

必须实现的公开对象：

```python
@dataclasses.dataclass(frozen=True)
class Limits: ...

class SpoolError(Exception): ...

class Spool:
    def __init__(self, root, *, clock, limits, lock_timeout, failpoint): ...
    def initialize(self): ...
    def enqueue(self, message_id, payload, available_at=None): ...
    def claim(self, worker_id): ...
    def ack(self, message_id, lease_token): ...
    def fail(self, message_id, lease_token, error): ...
    def recover(self): ...
    def get(self, message_id): ...
    def list_messages(self, status=None): ...
```

CLI：

```text
python -m src.delivery_spool --root PATH init
python -m src.delivery_spool --root PATH enqueue
python -m src.delivery_spool --root PATH claim
python -m src.delivery_spool --root PATH ack
python -m src.delivery_spool --root PATH fail
python -m src.delivery_spool --root PATH recover
python -m src.delivery_spool --root PATH show
python -m src.delivery_spool --root PATH list
```

完整公开契约以该版本的 `closed_loop_v2/template/TASKS.md` 为准；本文件不替代它。

## 4. 评分体系

不计算加权总分，不使用 LLM 主观打分。单模型只输出独立维度。

### 4.1 Instruction Gate

机械检查以下项目：

- 只修改两个允许的 source package；
- 未修改 TASKS、BRIEFING、public tests 或其他 workspace 文件；
- required public exports 和签名正确；
- 只使用标准库；
- 没有网络、process、动态代码或 nested delegation；
- 没有读取 hidden evaluator、harness、reference、mutation 或其他 workspace 的证据；
- 运行期间 workspace 非源码文件保持不变；
- 最终报告符合固定 JSON schema。

Gate 失败不会删除原始 criterion 结果，但所有严格 milestone 和闭环项目数归零。

### 4.2 20 个二元 criterion

两个项目各 5 个 milestone，每个 milestone 2 个 criterion，共 20 个 criterion：

| 项目 | Milestone | 覆盖内容 |
|---|---|---|
| Orders | OF1 | schema/migration/reopen |
| Orders | OF2 | 原子库存预留/create 幂等 |
| Orders | OF3 | payment 幂等/状态迁移 |
| Orders | OF4 | 取消释放/并发不 oversell |
| Orders | OF5 | JSON CLI/E2E/批量资源行为 |
| Spool | DS1 | initialize/enqueue/idempotency/reopen |
| Spool | DS2 | ordering/claim/attempt/token 授权脱敏 |
| Spool | DS3 | ack/fail/retry/dead |
| Spool | DS4 | lease recover/failpoint/corruption fail-closed |
| Spool | DS5 | 多进程/CLI/锁/文件边界 |

每个 criterion 只有 `pass` 或 `fail`；一个 milestone 的两个 criterion 全通过且 Gate 通过，才算 strict-success。

### 4.3 输出指标

记录以下原始指标：

```text
InstructionGate              true / false / indeterminate
ClosedLoopProjectCount       0–2
MilestoneStrictCount         0–10
AcceptanceCoverage           passed_criteria / 20
criterion_pass_count         0–20
inference_tokens             有效 usage 或 indeterminate
wall_time                    审计值，不用于正确性
model_calls / tool_calls     若 wire 数据可得
retry / timeout              若 wire 数据可得
```

定义：

```text
ClosedLoopProjectCount = 两个项目中所有 5 个 milestone 均 strict-success 的项目数
MilestoneStrictCount = strict-success milestone 总数
AcceptanceCoverage = 20 个 criterion 中通过数 / 20
```

单模型报告只描述结果，不宣布“最佳模型”。如果以后与其他模型比较，使用固定词典序轴：

```text
InstructionGate ↓
ClosedLoopProjectCount ↓
MilestoneStrictCount ↓
AcceptanceCoverage ↓
InferenceTokensPerMilestoneStrictSuccess ↑效率优先
```

严格并列必须保留；不存在人工 tie-break。

## 5. 跨 session 准备流程

### 5.1 不要覆盖十模型正式目录

当前 `closed_loop_v2/` 可能包含其他测试的 runs/results。单模型测试应使用一个新的 session-specific benchmark copy，例如：

```text
closed_loop_v2_single_<slot>_<date>/
```

不要直接删除或覆盖已有 `closed_loop_v2/runs/`、`results/`、workspace 或历史 agent wire 文件。

推荐使用一个新的工作副本，并保留以下冻结资产：

```text
manifest.json
asset-hashes.json
design.md
template/
evaluator/
harness/
validation/
results/frozen-phi3-plan.md
results/BRIEFING.md
```

`runs/` 和动态结果目录必须是本次测试的新目录。若 `results/frozen-phi3-plan.md`、`results/BRIEFING.md` 或 `validation/reference-validation.json` 不存在，停止测试，不要自行补写或绕过 preflight。

### 5.2 记录实验身份

在新 session 开始时记录一份 `single-run-manifest.json`，至少包含：

```json
{
  "protocol": "closed-loop-v2-single-model-v1",
  "benchmark_version": "closed-loop-v2",
  "slot": "subtest_N",
  "expected_model": "provider/model",
  "session_id": "...",
  "agent_id": "...",
  "binding_slot": "subtest_N",
  "workspace": "...",
  "started_at_utc": "...",
  "completed_at_utc": null,
  "replacement_count": 0,
  "replacement_reason": null,
  "frozen_asset_hashes_verified": false,
  "candidate_usage_status": "pending"
}
```

`expected_model` 必须与 manifest 一致。实际模型身份若不一致，token 和模型结果记为 `indeterminate`，不能静默改名。

### 5.3 单 workspace 创建

不要把十模型历史 workspace 当作初始目录。单模型 workspace 必须从冻结的 `template/` 全新复制：

```python
from pathlib import Path
import shutil

root = Path("closed_loop_v2_single_subtest_N")
workspace = root / "runs" / "subtest_N" / "workspace"
if workspace.exists():
    raise SystemExit(f"refusing to overwrite: {workspace}
")
shutil.copytree(root / "template", workspace)
print(workspace)
```

实际使用时，把 `root` 替换为当前 session 的 benchmark copy；不要把候选代码预先写入 workspace。

然后确认：

- `BRIEFING.md` 与冻结 briefing 字节一致；
- workspace 初始 tree hash 已记录；
- 两个 source package 是唯一允许变更区域；
- evaluator/harness 不在候选 workspace 可读路径中（运行评测时由 harness 单独提供）；
- 候选 workspace 是全新的、无旧 `.pyc`、无旧测试产物、无旧数据库/状态文件。

## 6. 候选 Agent 派发

### 6.1 派发约束

只派发一个 coder session，不并行同一候选，不进行 repair pass：

```text
subagent_type = coder
binding_slot = 选定的 subtest_N
rounds = 1
```

如果平台支持 usage wire 记录，确保该 session 的 `wire.jsonl` 保留在独立的 agent 目录中。

### 6.2 推荐派发 prompt

将以下 prompt 中的路径、slot 和模型替换为实际值：

```text
你是本次 closed-loop-v2 单模型 Pass@1 测试的唯一执行 coder。

只允许读取和修改：
<ABSOLUTE_WORKSPACE>/BRIEFING.md
<ABSOLUTE_WORKSPACE>/TASKS.md
<ABSOLUTE_WORKSPACE>/src/order_fulfillment/
<ABSOLUTE_WORKSPACE>/src/delivery_spool/
以及你在该 workspace 中自行创建的测试临时文件。

严格禁止：
- 读取 evaluator、harness、reference、mutation、results、其他候选或工作区之外的项目文件；
- 网络、第三方依赖、subprocess、动态 import、eval、exec；
- nested subagent 或把任务委托给其他 agent；
- 修改 BRIEFING.md、TASKS.md、public tests 或允许路径外的文件；
- 把测试、数据库、状态文件写到允许路径之外。

请在同一个 session 内依次完成订单履约和 delivery spool 两个项目。以 TASKS.md 为唯一业务契约，先检查现状，再实现两个 package，并自行运行公开 smoke、API、reopen、CLI、错误边界和并发/恢复验证。不要等待后续修复轮，也不要只实现公开 smoke 的最小路径。

完成后返回且只返回一个符合 BRIEFING.md 末尾 schema 的 JSON 对象：
{
  "schema_version": 1,
  "status": "completed|blocked",
  "projects": [
    {
      "id": "order_fulfillment|delivery_spool",
      "changed_files": ["workspace-relative/path"],
      "verification": [{"command": "exact command", "result": "pass|fail|not_run"}]
    }
  ],
  "constraints_respected": true,
  "unresolved": []
}

不能声称未执行的测试已经执行；遇到阻塞时使用 status=blocked 并列出具体 unresolved。
```

### 6.3 派发与结束计时

单模型测试可以串行精确记录总 wall-clock：

1. 父控在 dispatch 前记录 `started_at_utc` 和 `time.perf_counter()`；
2. 发起唯一 coder；
3. Agent 返回后立即记录 `completed_at_utc` 和 `time.perf_counter()`；
4. 计算 `executor_wall_time_seconds`；
5. 该时间包含模型等待、生成、工具调用和调度开销；不包含后续 evaluator。

不要把“报告写入时间”或后续评测时间算入 executor wall-clock。若平台只提供通知时间而没有真实 Agent 完成时间，字段标为 `notification_wall_time`，不要伪称精确模型耗时。

响应已经开始后不重试。只有 provider/调度在模型首个响应前失败，才允许一次 replacement；保留原失败 evidence 和 replacement 关系。

## 7. 候选完成后的冻结和评测

### 7.1 先冻结候选 workspace

Agent 返回后立即保存：

- final tree hash；
- changed paths；
- 原始 final response；
- agent wire 文件路径；
- start/end timestamp；
- session/agent/model identity；
- 候选 workspace 的完整压缩归档或只读快照。

评测开始后不得修改候选 workspace。

### 7.2 单候选机械评测

在 benchmark copy 根目录执行：

```bash
python -B closed_loop_v2/harness/evaluate.py \
  --workspace closed_loop_v2_single_subtest_N/runs/subtest_N/workspace \
  --slot subtest_N \
  --output closed_loop_v2_single_subtest_N/results/evaluation.json
```

评测将：

- 对每个 criterion 使用独立子进程；
- 使用固定 timeout；
- 清理 `PYTHONPATH` 等继承环境；
- 检查 workspace 在评测期间没有变化；
- 输出每个项目、milestone、criterion 的原始状态和诊断；
- 将 evaluator/harness 读取边界与候选 workspace 隔离。

如果出现 `infrastructure_indeterminate`，不要把它直接当成模型失败；单模型结果应标记为基础设施不确定，后续不能发布模型能力结论。

### 7.3 token usage 提取

候选 token 只能从该候选 agent 的 wire usage events 汇总：

```text
inputOther
+ inputCacheRead
+ inputCacheCreation
+ output
= inference_tokens
```

必须同时记录：

- `input_other`；
- `input_cache_read`；
- `input_cache_creation`；
- `input_context_tokens`；
- `output_tokens`；
- `fresh_tokens`；
- `inference_tokens`；
- usage record 数；
- wire 中 reported model 是否严格等于 expected model。

缺少 usage、JSONL 损坏、模型身份混杂或字段非法时：

```text
token_measurement_status = indeterminate
inference_tokens = null
```

不要把缺失 token 当成 0，也不要用无限大伪造效率值。

单模型可直接复用 `closed_loop_v2/harness/extract_usage.py` 的 `aggregate_wire()`，但不要为了单模型伪造包含其他 9 个 slot 的完整 usage.jsonl；那会破坏 manifest 的证据语义。

### 7.4 生成单模型结果摘要

单模型不运行十模型 `build_report.py` 生成排行榜，因为该脚本要求全部 manifest slots 的 evaluation/usage 记录。应保留原始：

```text
results/evaluation.json
results/single-run-manifest.json
results/candidate-final-response.json
results/candidate-usage.json
results/timing.json
results/tree-before.json
results/tree-after.json
results/agent-wire.jsonl 或其只读归档引用
```

另生成 `results/single-model-report.md`，至少包含：

- benchmark/version/slot/model/session/agent；
- frozen asset hashes；
- workspace before/after hash；
- `InstructionGate`；
- `ClosedLoopProjectCount`；
- `MilestoneStrictCount`；
- `AcceptanceCoverage`；
- 每个项目的每个 milestone/criterion 状态；
- 每个失败 criterion 的诊断；
- executor wall-clock；
- token 四分量和 measurement status；
- 是否超过 10M soft SLA；
- replacement、provider、evaluator 或 infrastructure blocker；
- 候选最终报告是否符合 schema。

## 8. 单模型结果判读

推荐按以下顺序查看，不用单一总分掩盖问题：

1. `InstructionGate`：是否遵守边界；
2. 两个项目是否都闭环；
3. 卡在哪个 milestone；
4. 20 个 criterion 的通过分布；
5. 是否存在并发、恢复、幂等或 CLI 类系统性失败；
6. token 消耗是否有效、是否超软上限；
7. wall-clock 仅作为效率诊断；
8. 是否存在 evaluator/harness 基础设施不确定性。

解释示例：

- `ClosedLoopProjectCount=2` 且 Gate 通过：两个闭环在机械验收上均完成；仍需查看所有 criterion 和边界失败。
- `ClosedLoopProjectCount=1`：一个项目完成，另一个项目至少有一个 milestone 未完成；不能称为“部分闭环都完成”。
- Gate 失败但 criterion 有通过：说明产物可能有功能完成度，但不满足严格交付约束；严格 milestone 按 0 处理。
- token indeterminate：只能报告功能结果，不能报告 token 效率。
- infrastructure indeterminate：不能把该次结果用于模型能力结论，必要时在全新 workspace 按预注册规则重跑。

## 9. 完成检查表

### 派发前

- [ ] 使用了新的 benchmark copy/session；
- [ ] 没有覆盖历史 runs/results；
- [ ] manifest、TASKS、frozen plan、briefing、evaluator、validation hash 已核验；
- [ ] 选择的 slot 与 expected model 一致；
- [ ] 新 workspace 从 template 创建；
- [ ] before tree hash 已保存；
- [ ] `single-run-manifest.json` 已登记；
- [ ] 候选 token 预算和主代理排除规则已冻结。

### 候选执行中

- [ ] 只有一个 coder session；
- [ ] 没有 repair pass；
- [ ] 没有读取禁止路径；
- [ ] 只修改两个允许 source package；
- [ ] start/end 时间已记录；
- [ ] wire usage 原始记录已保存。

### 评测后

- [ ] final tree hash 已保存且与候选返回前一致；
- [ ] `evaluate.py` 只运行一次；
- [ ] evaluation 原始 JSON 已保存；
- [ ] token usage 未混入主代理；
- [ ] model identity 与 expected model 匹配；
- [ ] 20 个 criterion 均有状态；
- [ ] evaluator 没有 infrastructure-indeterminate；
- [ ] 单模型报告没有伪造未运行的验证命令；
- [ ] 没有生成误导性的“总排名”或普遍能力结论。

## 10. 停止规则

遇到以下任一情况，停止并报告 `blocked` 或 `experiment_incomplete`，不要临时放宽规则：

- frozen plan、briefing、reference validation 或 asset hash 缺失；
- benchmark-controlled 文件在执行前后变化；
- workspace 初始状态不是干净 template；
- 实际模型身份无法确认；
- evaluator 关键 sentinel 失败或基础设施不确定；
- 候选修改了禁止路径且无法从证据确定影响；
- wire usage 无法可靠解析；
- 评测期间 workspace 被修改；
- provider 在首个响应后中断；
- 需要为了“完成”而人工修复候选产物。

正确做法是保留所有 raw evidence，标记阻塞原因，另建新的 run/version；不要覆盖原结果。

## 11. 推荐目录结构

```text
closed_loop_v2_single_subtest_N/
├── manifest.json
├── design.md
├── asset-hashes.json
├── template/
├── evaluator/
├── harness/
├── validation/
├── results/
│   ├── frozen-phi3-plan.md
│   ├── BRIEFING.md
│   ├── single-run-manifest.json
│   ├── evaluation.json
│   ├── candidate-final-response.json
│   ├── candidate-usage.json
│   ├── timing.json
│   ├── tree-before.json
│   ├── tree-after.json
│   └── single-model-report.md
└── runs/
    └── subtest_N/
        └── workspace/
            ├── BRIEFING.md
            ├── TASKS.md
            └── src/
                ├── order_fulfillment/
                └── delivery_spool/
```

单模型测试完成后，将整个 `closed_loop_v2_single_<slot>_<date>/` 作为独立实验归档；不要把它的 evaluation、usage 或报告混入十模型正式结果目录。
