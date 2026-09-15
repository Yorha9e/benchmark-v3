# 两个中型后端闭环任务 × 十模型重测计划

## 方向更正

上一版“5 个独立 utility 题”只是在增加行为覆盖，**不完全符合用户要测长程/闭环功能构建的意图**。本版改为十个模型各自完成完全相同的两个中型后端闭环，既不会长到超出 subagent 单次任务能力，又能观察跨模块实现、持久化、状态机、失败恢复、CLI、端到端验证和资源边界。

用户已冻结的选择：

- 任务结构：**两个中型闭环**，不是一个超长项目，也不是五个小函数；
- 业务领域：**后端业务闭环**；
- 对外接口：**Python service API + JSON CLI**，不引入 HTTP 端口/进程噪声；
- 每个槽位只启动 **一个 coder 会话**，在同一上下文中依次完成两个闭环；
- token 预算：**每个候选槽位各有约 10,000,000 inference tokens 的软上限**，十槽候选总 envelope 约 100M；优先完成十槽，不要求刻意烧满；
- 当前主代理即用户指定的 `phi_3` / `GPT/gpt-5.6-sol`，由主代理制定并冻结共享计划后直接派发，不再增加单独 planner cell；
- 主代理的设计、编排和复核 token **不计入 candidate pipeline**，只审计十个 executor 各自的 usage；
- 十个候选仍用 `subtest_1..subtest_10`，每轮五个并行、单次 Pass@1。

本轮是新的条件性探索，不覆盖 `prototype/` 和 behavioral-quality-v4，也不宣称正式或普遍最佳模型。

## 独立套件

新建可导入目录 `closed_loop_v2/`，拥有独立的：

- `design.md`、`manifest.json`、`asset-hashes.json`；
- `template/`、`evaluator/`、`harness/`、`validation/`；
- `runs/subtest_N/workspace/`；
- `results/`。

原 `prototype/`、`prototype/results/`、`prototype/results/behavioral-quality-v4/` 和根目录设计文档保持只读，并在本轮前后保存 SHA-256。

## 闭环 A：订单履约与库存预留

候选在 `src/order_fulfillment/` 中实现 SQLite 持久化 service 和 JSON CLI。公开契约冻结为以下业务流：

1. 初始化/迁移数据库并设置库存；
2. 用 idempotency key 创建多商品订单；
3. 在同一事务内校验并预留全部库存，任一商品不足则全部回滚；
4. 支付订单，payment id 幂等且冲突重复必须拒绝；
5. 允许合法的取消、发货状态迁移，取消时按规则释放库存，已发货不可取消；
6. 关闭并重新打开 service 后状态仍可查询；
7. 两个 service 实例竞争最后库存时不得 oversell；
8. CLI 以 JSON stdin/stdout 完成 `stock/create/pay/cancel/ship/get` 的真实多进程闭环。

核心接口在 `TASKS.md` 精确冻结，预期包括：

- `OrderService(db_path, *, clock, id_factory)`；
- `set_stock(...)`、`create_order(...)`、`pay_order(...)`、`cancel_order(...)`、`ship_order(...)`、`get_order(...)`；
- 结构化领域异常及稳定错误码；
- `python -m src.order_fulfillment --db PATH COMMAND` 的单行 JSON 协议。

必须机械检查：事务原子性、库存守恒、状态机、幂等/冲突、SQLite reopen、并发竞争、非法输入、CLI exit/status/schema、文件描述符/连接关闭和合法规模批量输入。实现只用标准库，不做真实网络或支付调用。

## 闭环 B：耐久化 Delivery Spool

候选在 `src/delivery_spool/` 中实现文件系统持久化的本地 outbox/worker lease 队列、注入 clock/failpoint 和 JSON CLI。它与订单任务使用不同的持久化机制，避免两个任务都只测 SQLite。公开契约冻结为以下业务流：

1. 初始化调用方指定的 state root，并把 canonical JSON payload 以 message id 幂等入队；同 ID 不同内容必须 conflict；
2. worker 按 `(available_at, sequence, message_id)` 原子 claim 当前可用消息，获得不可泄漏的 lease token，attempt 只在成功 claim 时增加；
3. 正确 token 可 `ack`；`fail` 按 retry delay 回到 pending，达到 max attempts 后进入 dead；
4. worker 崩溃后，lease 在 `clock() >= expires_at` 到期，重复 `recover()` 必须幂等地回收或转 dead；
5. 新进程重新打开同一 root 后状态、history、sequence 和分页保持一致；
6. 多进程竞争同一消息时最多一个 claim 成功，不同消息并发 enqueue 不得 lost update；
7. 每次变更采用同目录临时文件、flush/fsync、原子 replace；malformed/未知 schema 必须 fail closed；
8. 可见 `failpoint(after_lock/before_replace/after_replace)` 用于验证异常后锁释放、临时文件清理，以及重开只能看到完整旧态或完整新态；
9. CLI 以单行 JSON 完成 `init/enqueue/claim/ack/fail/recover/show/list`，不访问真实网络。

核心接口在 `TASKS.md` 精确冻结，预期包括：

- `Limits` 与 `Spool(root, *, clock, limits, lock_timeout, failpoint)`；
- `initialize(...)`、`enqueue(...)`、`claim(...)`、`ack(...)`、`fail(...)`、`recover(...)`、`get(...)`、`list_messages(...)`；
- `ValidationError/ConflictError/LeaseError/BusyError/StoreCorruptionError` 等结构化异常和稳定错误码；
- `python -m src.delivery_spool --root PATH COMMAND` 的单行 JSON 协议，成功/业务错误/持久层错误使用冻结 exit code。

必须机械检查：状态机、幂等冲突、lease token 授权与脱敏、retry/dead 边界、原子 failpoint、corruption fail-closed、跨进程竞争、锁 timeout/清理、reopen、稳定分页、CLI、状态根越界和 payload/message/list 资源限制。

## 评分结构：不用主模型主观打分

两个项目各拆成 5 个严格 milestone，每个 milestone 含 2 个二元 criterion，共：

- `ClosedLoopProjectCount`: 0–2；一个项目的 5 个 milestone 全部成功才算闭环完成；
- `MilestoneStrictCount`: 0–10；
- `AcceptanceCoverage`: 20 个等权 criterion 的通过率；
- `InstructionGate`: 文件/接口/依赖/进程日志等机械门；
- `InferenceTokensPerMilestoneStrictSuccess`：最后一个效率轴，零 milestone 时为无穷。

订单 milestone：

1. schema/migration/reopen；
2. 原子库存预留与 create idempotency；
3. payment idempotency 与状态迁移；
4. 取消释放、并发无 oversell；
5. JSON CLI 端到端与批量/资源行为。

Delivery Spool milestone：

1. initialize/enqueue/idempotency/reopen 与资源验证；
2. availability ordering、claim、attempt、lease token 授权/脱敏；
3. ack/fail、retry delay 与 dead 边界；
4. expired lease recover、原子 failpoint 与 corruption fail-closed；
5. 多进程无 double claim/lost update、JSON CLI 端到端与锁/文件边界。

排序严格为降序词典序：

`InstructionGate → ClosedLoopProjectCount → MilestoneStrictCount → AcceptanceCoverage → InferenceTokensPerMilestoneStrictSuccess(升序)`

不建立加权 FinalScore，不评价注释、命名、docstring 或可读性，不用 LLM Judge 决胜。完全同轴必须保留并列。报告另列每个 criterion、E2E flow 和错误诊断。

## 每槽 10M token 软预算

`subtest_1..subtest_10` **每个候选槽位独立拥有约 10,000,000 inference tokens 的软上限**，候选执行总 envelope 约 100M；这不是十槽共享 10M，也不是要求刻意烧满。`phi_3` 计划、benchmark 构建和最终复核的 token 单独记录，不挤占任一候选额度。

执行规则：

- 优先保证十个 expected cells 都完成；某槽为完成闭环而轻微超过 10M 时继续并如实标记，不让半轮数据因硬切断失效；
- 不为了接近 10M 重复思考、增加无关需求或做 best-of-k；两个闭环完成并验证后即可结束；
- 每槽分别从自己的 `wire.jsonl usage.record` 汇总 input/cache/output 四分量，绝不将共同 `phi_3` 计划摊薄到候选；
- Agent API 没有可靠的前置 token hard-stop，且模型上下文、30 分钟 subagent 时限或 provider 限制很可能先于 10M 生效，所以 10M 只能作为事后资源审计软上限，不能承诺实际可消费到该数值；
- 超过软上限不删除该槽的完成度事实，但令 `ResourceSLA=FAIL`；不能依据第一波成绩或 token 使用量改变第二波任务、提示或评分。

## 实施步骤

1. **由当前主代理 `phi_3` 冻结实验与共享实施计划**
   - 当前主代理按用户指定身份作为 `phi_3` / `GPT/gpt-5.6-sol`，直接完成 `design.md`、精确 `TASKS.md` 与 `results/frozen-phi3-plan.md`，不再建立单独 planner expected cell。
   - 共享计划只给两个 package 的模块分解、SQLite 事务、文件原子替换/锁、状态机、CLI 与验证清单，不包含完整候选实现，也不读取隐藏 evaluator 后向候选泄漏 oracle。
   - 生成唯一 `results/BRIEFING.md = executor constraints + TASKS 原始字节 + frozen plan 原始字节 + final report schema`；复制进十个 workspace，并要求 SHA-256 完全一致。Agent dispatch prompt 只传 slot、workspace、briefing path/hash，不重复业务正文。
   - 冻结 TASKS/plan/briefing/model/hash；主代理 token 不写入 candidate `usage.jsonl`、不计入 PipelineTokens。计划冻结并完成 benchmark 预检后，主代理直接按两波派发十个 coder。

2. **实现 benchmark 契约、stubs 和隐藏 evaluator**
   - 创建 `src/order_fulfillment/` 与 `src/delivery_spool/` 两个 package stub、统一 JSON envelope/错误码和 public smoke tests。
   - `evaluator/criteria.json` 固定 10 milestone/20 criterion；测试全部使用 `TemporaryDirectory`、FakeClock、deterministic ids、filesystem failpoints 和显式并发 barrier，不依赖网络、真实墙钟、随机 sleep 或机器速度微基准。
   - E2E CLI 用独立 subprocess + 唯一 nonce JSON frame；严格校验单帧、schema、exit code、stderr 和敏感字段。进程 timeout 是 harness 安全阀：golden sentinel 同时异常则为 infrastructure-indeterminate 并阻断榜单，不能直接扣候选性能分。
   - SQLite 并发使用短事务/显式 barrier；文件 spool 使用冻结 lock_timeout、同目录临时文件与 failpoint。Windows/POSIX 子进程树均需清理，不能留下锁、临时文件或后代进程。

3. **reference + mutants 验证 evaluator**
   - `validation/reference/` 的两个完整实现必须通过 20/20 和两个闭环 E2E；normalized evaluator 连续两次结果 hash 一致。
   - 每个 milestone 至少一个 targeted mutant，总计至少 10 个；必须覆盖库存非原子/oversell、错误状态迁移/幂等、CLI 协议、spool 同 ID 覆盖、忽略 lease token/backoff/recover、非原子 state 写入、corruption 静默重置和无锁 double claim。
   - 额外验证 evaluator tampering、workspace 越界、第三方/网络/subprocess import、CLI 垃圾输出、多帧/无帧、worker failure 和状态根越界。
   - 两个独立只读 critic：一方审契约/oracle/capability 是否重复计分，一方审 SQLite/文件并发、harness 隔离、协议帧、timeout、process tree、hash 和排名。任何 golden failure、关键 mutant 漏检或协议不确定都阻止十模型调度。

4. **构建隔离 harness 并冻结资产**
   - 复制现有 `prototype/harness/` 到 `closed_loop_v2/harness/`，改为动态 task/milestone 数、原子 JSONL 发布、严格 schema/slot/model 验证、清理 Python 环境变量、完整 tree hash。
   - 新增 `evaluate_all.py`、agent wire 指令审计和 `compare_rounds.py`。
   - `freeze_assets.py` 冻结 template/evaluator/harness；冻结后不得边跑边改。任何 oracle 缺陷使整个版本 invalid，必须递增版本并对全部十槽重跑。

5. **准备十个字节一致 workspace 与 assignments**
   - 新 Wave 1 使用上一轮 Wave 2：`subtest_4, subtest_6, subtest_1, subtest_9, subtest_8`；新 Wave 2 使用上一轮 Wave 1：`subtest_10, subtest_5, subtest_3, subtest_2, subtest_7`，平衡跨两轮 time block。
   - `prepare_runs.py` 创建十个全新 workspace，均包含字节一致的 `BRIEFING.md`、TASKS、frozen plan、两个 package stubs 和 smoke tests；十份 briefing SHA-256 必须相同，仅路由 prompt 的 slot/workspace 不同。
   - 保存 expected-cell ledger、before tree hash、briefing hash、binding slot、expected model。`subtest_7` 当前 binding 是 `GPT/gpt-5.6-luna`，与旧轮 `supernb/...` 前缀差异必须在跨轮报告中保留。

6. **两波派发十个候选 coder**
   - 每波一次并行调用 5 个 `Agent(subagent_type="coder", binding_slot="subtest_N")`；每个槽位只启动一个 coder，会话内依次完成订单履约与 delivery spool 两个闭环，保持一次 Pass@1。
   - 只允许读自己的 workspace、修改 `workspace/src/order_fulfillment/` 与 `workspace/src/delivery_spool/`；禁止读 evaluator/harness/其他候选，禁止 network、第三方依赖、subprocess/dynamic-code 和 nested subagent；必须运行 public smoke 与自行 API/CLI/reopen smoke。
   - Wave 1 全部终止并记录 agent/model/usage/workspace hash 后才启动 Wave 2；不按 Wave 1 成绩、token 或失败类型改任务、提示、评分或停止第二波。
   - 模型响应开始后不重试；仅首个响应前 provider/调度失败允许一次 replacement，并保留原 expected cell/failure chain。每槽 10M 为软资源审计上限，不要求模型耗尽。

7. **机械评测、token 与指令审计**
   - 并行评测 10 workspace，输出 `evaluation.jsonl`、逐 criterion 诊断和 E2E traces。
   - Gate：冻结资产、非源码 workspace、导出/签名、stdlib-only、SQLite/CLI/状态根文件边界、无网络调用；agent wire 中可解析的 evaluator/harness/其他 workspace 读取、network、nested Agent 尝试记为违规，解析不到的历史项目标 unobservable。
   - 只从十个候选 `agents/<agent-id>/wire.jsonl` 提取 executor 原生 token 四分量；当前主代理 `phi_3` 的设计、编排与复核 usage 不进入 `usage.jsonl` 或 PipelineTokens。actual executor model 不匹配 expected 时 token/model identity 无效，不静默更名。

8. **生成 v2 与跨轮报告**
   - 输出 `results/report.md`、`leaderboard.csv`、`evaluation.jsonl`、`usage.jsonl`、`instruction-audit.jsonl`、`agent-map.json`、`audit.json` 和 `final-decision.json`。
   - `compare_rounds.md/json` 并列展示：旧 v1 三小题 completion/token、旧 v4 行为质量、v2 两闭环五轴；不把不同轴加总，不因 provider/time block 差异宣布普遍 winner。
   - 稳定性只作描述性分析：三轮 top-set overlap、tie-aware rank movement、45 个模型对的 concordance/reversal/tie-change；`subtest_7` 同时按 slot 与 exact provider identity 两种口径报告。
   - 重点回答：谁完成 0/1/2 个闭环、卡在哪个业务 milestone、token/成功 milestone、旧冠军是否稳定、v4 冠军是否在长闭环仍领先。

9. **最终独立复核**
   - 两路 critic 重算所有轴，核对 10 expected cells、20 criteria、usage、model identity、process gate、workspace/旧结果 hash 和并列。
   - 任一 decision infrastructure-indeterminate、缺槽、冻结资产变化、raw/report 不一致时拒绝发布覆盖十模型的 finalist。

## 验证命令

- `python -m unittest discover -s closed_loop_v2/harness/tests -v`
- `python closed_loop_v2/validation/run_validation.py`
- `python closed_loop_v2/harness/freeze_assets.py`
- `python closed_loop_v2/harness/prepare_runs.py`
- 候选完成后：`python closed_loop_v2/harness/evaluate_all.py`
- `python closed_loop_v2/harness/extract_usage.py ...`
- `python closed_loop_v2/harness/build_report.py --results-dir closed_loop_v2/results`
- `python closed_loop_v2/harness/compare_rounds.py`
- 最终 raw 重算与 hash assertions

## 完成标准与停止规则

完成标准：两个闭环 benchmark 在冻结前通过 reference/mutant/双 critic 验证；十个 expected cells 均有可追溯终态；20 criterion、E2E、token、指令、v2 报告、跨轮报告与完整审计全部生成并复核。

若冻结后发现契约/evaluator 错误，不补丁式继续：整轮 invalid、版本递增、全部十槽重跑。若 provider 缺失、模型 identity 无法确认、decision infrastructure-indeterminate 或预算规则触发停止，则保留所有 expected cells 和观测结果，但报告 `experiment_incomplete`，不事后删除候选、改阈值或挑选赢家。