# 多模型审查报告：long B 计划 + critic 金标准

> 记录时间：2026-09-24
> 方法：7 slot 并行一审（5 完成：slot1/2/4/7 + slot3 窄范围重试；slot5/6 因 Go 订阅 403 缺席）→ 5 模型二审逐条裁决
> 审查对象：① `bench_harness/suites/b_plans.py` 的 `order_fulfillment@b` 冻结计划及其判定器
> ② `docs/CRITIC_GOLD_STANDARDS.md` 金标准卷宗及 `core/judge.py` / `suites/critic.py`

---

## 一、B 计划与判定器（order_fulfillment）

### P1（必修，均已实证）

| # | 问题 | 证据 | 修法 |
|---|---|---|---|
| B1 | `business_task.py:805` 读私有属性 `svc._delivery_gateway`（死代码，`dg` 从未使用） | 契约只规定 `set_delivery_gateway()` 方法名；命名不同的正确实现 AttributeError → p6 只剩 error → **m9+m10 归零** | 删除该行 |
| B2 | 恢复探针硬编码 `ord-000001` | 契约写 "assigned by you"、计划只说 "monotonic counter"；`ord-1` 等合法格式丢 m11 两项。且 runner 的 `out_path` 是死参数（子进程从不写）——修法A的前提原本不存在 |  runner 把创建的 o1/o2 id 写入 out.json，探针动态读取（保持契约自由） |
| B8b | 计划 step9 "每个变更方法重写 journal" 未限定 `journal_path` 非 None | 8 处探针构造中 5 处无 journal；照字面实现 `open(None)` → TypeError → 全场景 fatal | 契约+计划明确 "journal_path 为 None 时不落盘" |
| — | p7 超卖探针的 `Barrier` 从未 `wait`（slot2 发现） | `_grab` 无 `barrier.wait`（`_churn` 有）→ 8 线程自然起跑，竞争判别力弱于设计意图 | 补 `barrier.wait(timeout=5)` |
| — | 探针异常被当成模型失败（slot7 系统级） | B1/B2 致命的原因：探针内异常被 except 吞成 error 键，与模型实现错误无法区分 | 探针 error 键标注 `probe_error` 前缀，milestone 记 ERROR 而非 FAIL |

### P2（应修，一次计划修订 + 小改）

- **B3**：计划 step4 未要求 reserve 校验 `state == PENDING`（m3/m8 依赖）。slot7 补充：必须与库存扣减**同一临界区**（check-then-act 在 8 线程下仍双预留）。
- **B4**：`sweep_expired`→list[str]、`deliver_next`→entry|None、`redrive_dlq`→移回+重置 attempts/last_attempt_at+返回 id 列表——契约已有，计划漏写；slot7 建议补 "二次 sweep 返回空"（幂等）。
- **B5**（一审 4/7 称会崩，**二审降级**）：slot1/slot2 实证探针中 `stock_report` 均在 join 之后调用、reserve 内部持锁 → 当前不丢分；仍应把计划措辞改为 "every public method, reads under the same lock"。
- **B6**：journal 枚举漏 `on_hand/by_idem/payments/counter`（slot1：counter 缺失会在恢复后复用 id 静默覆盖活订单——正确性隐患大于丢分）。
- **B7**：`cancel_order` 在计划中完全缺席（m8 依赖终态拒绝）。
- **B8a**：`gateway.charge(idem_key, total_qty)` 参数未指明；slot7 补第三个缺口：幂等键派生规则（每单稳定 vs 每次尝试）。
- **B10**（升 P2）：`attempts == max_attempts` 应为 `>=`；slot7：max=0 时是**不终止重试循环**，会烧评测预算。

### B9 泄露度争议 — 裁决：**适中，不过细**（4/5 明确反对"过细"）

论据：① 粒度与体系内 timing_wheel/raft 计划一致；② 未泄露断言与探针常数；③ **决定性反证：计划存在成片盲区（B3/B6/B7/B8 全踩在计划未覆盖而探针却考的位置）——送分清单不会漏评分项**。唯一保留（slot1）：step2 钉死六个私有 dict 名可改为"结构自定但必须可持久化 X"。

---

## 二、金标准与 judge（critic）

### P1（必修，均已实证）

| # | 问题 | 证据 | 修法 |
|---|---|---|---|
| G1 | `replace("../","")` 一票否决按字面量触发且**无否定豁免** | `judge.py:478` 无豁免，而 basename(:485)/startswith(:500) 都有；金标准自述陷阱就用该字面量 → 引用反例的专家答案被误判 L1。zip-diff 否决(:436)同样无豁免 | 统一补"否定/批判语境"豁免 |
| P1-B | format 闸门 severity 悬崖 | `critic.py:481-490` 要求**每条** severity ∈ (low/medium/high/critical)；金标准 :449 明文鼓励 style/info 不扣分 → **一条 info 附注毁掉 recall+depth+format 共 80 分** | 非缺陷级 severity 允许存在但不参与计分（过滤而非整份作废） |
| — | `_good_audit()` 参考答案自身含伪修复 | `critic.py:605` 对 session_tokens 的修复建议 "Accumulate mismatches with XOR and compare once at the end" 正是金标准否决的空串绕过模式（无长度守卫），却因不含 "zip(" 字面逃过启发式，且被自检当≥85 分答案 | 改为 `hmac.compare_digest` 生产修复 |
| G8 | `startswith` 启发式误杀 | `judge.py:500-503`：含 "startswith" 且无否定词即 `has_production_fix=False` → `startswith(ROOT + os.sep)` 正确修复被压 L3 | 带分隔符形态豁免 |
| G3 | L4 声称 isinstance 防 DoS 不成立 | `hmac.compare_digest` 对非 ASCII str 仍抛 TypeError（5/5 同意） | L4 示例补 `.encode()` 或 try/except |

### P2（应修）

- **G4**：新颖性加分（+2.5/项、cap +5）只在 judge.py，文档无章节；slot2 发现 `criticbenchmark/scoring/SCORING.md:22` 明写"真实缺陷也不加分"，与 judge.py **直接冲突**；slot1：+5 与 100 截断使满分≠全召回。→ 补章节 + 统一两份文档。
- **G5**：诱饵扣分判据文档比 `_is_defect_claim` 实现严格（漏三阶段豁免）→ 人工复核误扣 10 分。→ 抄入文档。
- **G6**：机械召回判据未文档化（LINE_TOLERANCE=3、≥4 字符双向前缀匹配、recall 不校验 severity）；slot1/slot2 验算 **off-by-one 的别名集 (bound/overflow/oob/index/range) 无法匹配 off-by-one 自身**（off/by/one <4 字符）→ 以 off-by-one 作 category 的正确答案丢 13.75 分，docstring 示例本身是错的。→ 补文档 + 别名集加 "offbyone"。
- **G7 逐项**（slot2 逐项核对后）：posixpath 归因同意；O_EXCL 语义变更未披露同意；绝对路径升格同意；assert 否决窄化为"仅依赖 assert 作唯一校验"；**"off-by-one 与方向写反拆分"子项撤销**（slot2 未找到该条目，证据不足）；GC 论据：slot2 反驳"循环不分配"——zip 每次迭代分配 tuple，GC 抖动有依据，但小整数缓存部分牵强 → 论据改写为"zip 迭代分配 + 解释器开销"。

### G2 争议 — 裁决：**原修法不成立，降级**

slot2 的证据链（已核实 `judge.py:436`）：否决条件是 `"zip(" in combined and "diff |=" in combined and not any(len(/length/guard)`——**已限定"移除长度保护"场景**，带长度守卫的实现不触发；且 `diff == 0` 收敛并不修复 zip 截断（空串时 0 次迭代 diff 恒 0 → True 仍是完整绕过），原修法会制造假阴性。**保留现有否决，仅并纳入 G1 的否定豁免泛化**。

---

## 三、修复顺序（按多模型收敛排序）

1. B1 + B2 + barrier + ERROR 通道（判定器硬伤，分钟级）
2. P1-B + _good_audit 伪修复 + G1 泛化 + G8（critic 判分管道硬伤）
3. B3/B4/B6/B7/B8/B10 + B5 措辞（一次 PLAN.md/契约修订）
4. G3/G4/G5/G6/G7 文档批处理（含别名集 offbyone）
5. B9 不动（裁决适中）；step2 私有结构命名改为"结构自定"（可选）
