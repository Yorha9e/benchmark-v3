# `short/` 评分器公开契约一致性审计（spec-conformant-v2）

## 1. 审计目标与证据边界

本审计逐条核对 `short/` 的 16 个 official criteria、4 个 capability probes 与 Instruction Gate。强制契约只来自候选实际可见的 `TASKS.md`；`results/frozen-phi3-plan.md` 是 B 条件额外输入，只能解释计划处理效应，不能偷偷扩展 A/B 共用的 official contract。Reference 是验证样例，不是规范来源；mutant 只证明 criterion 能捕获某类缺陷，也不能反向创造契约。

证据文件：

- 公开契约：`short/TASKS.md:3-25`
- B 条件计划：`short/results/frozen-phi3-plan.md:14-143`
- criterion 注册：`short/manifest.json:55-69`
- 实际断言：`short/evaluator/checks.py:16-207`
- Gate：`short/evaluator/instruction_audit.py:70-170`
- 隔离与计分：`short/harness/evaluate.py:124-223`
- reference：`short/validation/reference/solutions/*.py`
- mutants：`short/validation/mutants/*.py`

现有 `graphify-out/graph.json` 的查询误命中 `prototype/manifest.json` 与 `prototype/evaluator/criteria.json`，并未覆盖当前动态 `short/` 正式结果。因此下述结论全部由真实 `short/` 文件直接复核，旧图只作为“不可依赖”的审计记录。

## 2. 判定标准

- **明确支持**：实际断言可从 `TASKS.md` 直接推出；具体样例只是公开规则的实例化。
- **合理但未公开**：方向合理，但异常子类、位置锚点、调用时机、精确规模或 SLA 未向候选公开。
- **与计划冲突**：断言与 B 条件计划的明确语义不一致。
- **完全越界**：断言既无公开依据，也不是公开要求的合理实例。

A/B 公平性分两层：同一 evaluator 对两条件对称，并不自动意味着公开契约公平；若只有 B 计划给出某个精确答案，共用 evaluator 绑定该答案仍会偏向 B。

## 3. 16 个 official criteria 映射

### 3.1 `recursive_patch`

| Criterion | evaluator 实际断言 | `TASKS.md` 依据 | phi_3 计划 | reference / mutant | 判定与 v2 处理 | A/B 影响 |
|---|---|---|---|---|---|---|
| `rp_delete_identity` | equal-but-distinct 哨兵不得删除；`is delete` 才删除；自定义 delete 只替换本次哨兵；替换结果不得变成 replacement sentinel | `TASKS.md:13` 明确 “only when it is delete” 和可替换参数 | `plan:19,21,28` 明确 identity 而非 equality | reference `recursive_patch.py:8-24`；mutant `rp_delete_identity.py:8-12` 用 `==` | **明确支持**；不改 | 无偏置 |
| `rp_plain_dict` | 顶层 `dict` 子类必须被拒绝；嵌套子类整值替换、保留子类类型且不与旧 dict 合并；旧版还精确要求 `TypeError` | `TASKS.md:13` 明确 plain dict、not subclasses、非双 plain dict 时整值替换；未规定异常类 | `plan:19,30,37` 同样未规定异常类 | reference `:28-30` 选择 `TypeError`；mutant `rp_plain_dict.py:8-9` 强转为 dict | 行为 **明确支持**，精确异常类 **合理但未公开**；v2 改为捕获普通 `Exception`，不接受 `BaseException` | 修复绝对契约缺口；对两臂对称，不再要求猜异常类 |
| `rp_isolation` | 输入调用前后不变；修改结果的替换分支、未触及 base 分支、新 patch 分支均不得反向污染输入 | `TASKS.md:13` 明确 fully detached、no mutable aliases、inputs unchanged | `plan:20,31,38` | reference 全分支 deepcopy；mutant `rp_isolation.py:8-12` 给新增键保留别名 | **明确支持**；不改 | 无偏置 |
| `rp_order_boundary` | 顶层保留键维持原位、新键追加；递归层删除后保留键原位、新键追加；整值替换 dict 使用 patch 自身顺序 | `TASKS.md:13` 明确 retained positions、patch encounter order、recursively and across replacement boundaries | `plan:20,23-24,35` | reference 依赖 Python dict 插入序；mutant 反转顶层顺序 | **明确支持**；不改 | 无偏置 |

### 3.2 `dependency_layers`

| Criterion | evaluator 实际断言 | `TASKS.md` 依据 | phi_3 计划 | reference / mutant | 判定与 v2 处理 | A/B 影响 |
|---|---|---|---|---|---|---|
| `dl_one_shot` | `__iter__` 只调用一次；链 `a→b→c` 返回三层 list-of-lists | `TASKS.md:17` 明确 one-shot、consume exactly once、dependency earlier | `plan:45,63` | reference 单次 for；mutant `dl_one_shot.py:8-10` 二次迭代 | **明确支持**；不改 | 无偏置 |
| `dl_dependency_nodes` | 重复边去重；dependency-only 节点进入结果；层内首次出现序；空输入 `[]` | `TASKS.md:17` 逐项明确，空图结果是返回类型和图语义的自然实例 | `plan:56-57,63-64` | reference 用 set，额外假设节点可哈希；mutant 遇重复边伪报 cycle | **明确支持**；不改 | 无偏置；计划承诺不可哈希节点但 official 未测，属于 B 的无得分额外工作 |
| `dl_stable_order_cycle` | 层内按整个 edge stream 首次出现序；真实环抛 candidate 自己的异常；`.nodes == ("a","b","c")`，含 blocked 下游；自环是环 | `TASKS.md:17` 明确 `DependencyCycleError(ValueError)`、`.nodes` tuple、stable first appearance、cyclic/blocked nodes、real cycle | `plan:44,51-52,57,66` | reference message 未被测；mutant 反转 `.nodes` | **明确支持**；v2 补 `issubclass(DependencyCycleError, ValueError)`，覆盖原 evaluator 漏测的公开要求 | 两臂同规则，无偏置 |
| `dl_deep_iterative` | 12000 边深链返回 12001 个单节点层，首尾正确；criterion 5 秒隔离 | `TASKS.md:17` 明确 very deep、without recursion；未公开 12000 与 5 秒 | `plan:59,67` 仍无精确数值 | reference 迭代 Kahn；mutant >5000 主动抛 RecursionError | 非递归语义 **明确支持**；12000/5 秒为 **合理但未公开** 的测试实例。v2 保留 official 深链，以独立 resource probe 承担更强规模解释 | 对称；报告不得把规模值解释为公开 SLA |

附带冲突：`plan:46,65` 明确支持不可哈希节点，reference `dependency_layers.py:12-13,23,28-29,36-39` 依赖 set/dict 可哈希。现有 official criteria 不投喂不可哈希节点，因此不改变排名，但 B 计划与 reference 不一致必须保留在审计记录中。

### 3.3 `ttl_set`

| Criterion | evaluator 实际断言 | `TASKS.md` 依据 | phi_3 计划 | reference / mutant | 判定与 v2 处理 | A/B 影响 |
|---|---|---|---|---|---|---|
| `ttl_strict_types` | 拒绝 bool/非正/非 int capacity；拒绝 bool/负数/inf/nan/字符串 ttl；拒绝不可调用 clock；每次 clock 结果拒绝 bool。旧版对不可调用 clock 精确要求 TypeError，且用恒 True clock 隐式要求构造期不得调用 clock | `TASKS.md:21` 明确所有类型和值域与 callable；未规定异常类或构造期 clock 调用时机 | `plan:73-79,88,99` 未规定异常类，倾向只在公开操作读取 clock | reference 选择 TypeError/ValueError；mutant 只放行 bool capacity/ttl | 值域 **明确支持**；精确异常类和调用时机 **合理但未公开**。v2 接受 `TypeError/ValueError`，并在构造后切换 clock 值再测试 add | 消除合规 eager-validation 实现误杀；两臂同规则 |
| `ttl_exact_expiry` | deadline 前存在，`now == deadline` 消失；ttl=0 下一次同刻观察已过期 | `TASKS.md:21` 明确 `clock() >= deadline`、observable 前 purge、ttl nonnegative | `plan:82,89,95` | reference `now >= deadline`；mutant 改成 `>` | **明确支持**；不改 | 无偏置 |
| `ttl_capacity` | 超容量淘汰最老活跃插入；equal key 重加后更新对象/deadline/位置，再插入时淘汰旧的下一项 | `TASKS.md:21` 逐字明确 | `plan:80-83,90,96` | reference delete+reinsert；mutant capacity+1 | **明确支持**；不改 | 无偏置 |
| `ttl_gc_release` | equal replacement、discard、expiry 后旧对象均可被 GC，不能保留 stale refs | `TASKS.md:21` 明确 deletion/replacement/expiration must not retain stale refs | `plan:78,81,84,97` | reference 单一 OrderedDict；mutant `_stale` 保留 replace/discard 对象 | **明确支持**；不改。过期释放断言暂无定向 mutant，记录为 mutation coverage 缺口 | 无偏置 |

计划 `plan:79,91,98` 还要求非单调 clock 时全量扫描；当前 official/probe 时钟均单调，未给该额外承诺计分。这不误杀 A，但会让忠实执行 B 计划的实现承担额外复杂度。

### 3.4 `duration`

| Criterion | evaluator 实际断言 | `TASKS.md` 依据 | phi_3 计划 | reference / mutant | 判定与 v2 处理 | A/B 影响 |
|---|---|---|---|---|---|---|
| `du_strict_syntax` | 非 str 抛 `DurationParseError` 且 code=type；空白、符号、小数、坏单位、缺数字、leading zero 均拒绝 | `TASKS.md:25` 明确语法、code 集与所有 invalid input 使用该异常；并明确 `DurationParseError(ValueError)` | `plan:105,108-110,119,131` | reference `isdigit()` 接受部分 Unicode 数字，与 plan ASCII-only 冲突；mutant 放行 `+` | **明确支持**；v2 补 `issubclass(DurationParseError, ValueError)`，不新增 Unicode official 断言 | 两臂同规则；不把仅 B 计划的 ASCII 细化偷偷加入 official |
| `du_units_ranges` | 固定键序和值；边界 `24h/60m/60s/1000ms` 拒绝；乱序、重复拒绝 | `TASKS.md:25` 明确固定 plain dict、五键顺序、范围、严格降序、不可重复 | `plan:106,111-112,120-121,127` | reference dict.fromkeys；mutant允许 overflow | **明确支持**；v2 补 `type(parsed) is dict`，覆盖原 evaluator 漏测的 plain dict 要求 | 无偏置 |
| `du_normalize` | 五组 canonical carry/zero 案例与幂等 | `TASKS.md:25` 明确 carry、shortest canonical、omit zero、`0s`、idempotent | `plan:112,115,121-123,128-129` | reference 总毫秒/divmod；mutant原样返回 | **明确支持**；不改 | 无偏置 |
| `du_structured_errors` | code 分别为 empty/leading_zero/order/range/syntax；旧版 position 精确为 0/0/3/0/1 | `TASKS.md:25` 明确 code 集、stable `.code/.position`、zero-based，但没有定义 position 锚点 | `plan:113,130` 才定义 type/empty=0、syntax=意外字符、leading_zero/range=数字起点、order=单位起点 | reference 当前按 plan；mutant把 syntax position 移位 | code **明确支持**；精确 position 策略 **合理但未公开**。v2 要求 position 为非 bool int、重复调用稳定、落在对应错误 token/字段跨度内；order 接受 2（字段起点）或 3（单位起点） | 这是原 evaluator 最直接的 B 偏置；v2 消除“只有读计划才知道 3”的优势 |

v2 position 合理集合：empty `{0}`；`01s` leading_zero `{0,1}`；`1s2m` order `{2,3}`；`60s` range `{0,1,2}`；`1q` syntax `{1}`。这些集合不选定 reference 的单一锚点，同时仍拒绝负数、越界、非整数和不稳定位置。

## 4. Capability probes

| Probe | 断言 | 公开依据 | 判定 | 计分边界 |
|---|---|---|---|---|
| `ext_recursive_patch_nested_tuple` | tuple 内嵌 list/dict 也不得与 patch 别名 | `TASKS.md:13` 的 “no mutable container … aliases either input” | **明确支持** | 只进入 ExtensionCapabilityCount，不进入 16 official |
| `ext_duration_large_carry` | `1000000ms -> 16m40s` | `TASKS.md:25` 的 carry + shortest canonical | **明确支持** | 只进入 ExtensionCapabilityCount |
| `res_dependency_20000` | 20000 深链在隔离预算内完成 | only “very deep … without recursion”；精确 20000/5 秒未公开 | 方向明确、规模/SLA **合理但未公开** | 只进入 ResourceCapabilityCount，不得宣称为 official correctness |
| `res_ttl_expiry_sweep` | 1000 项同 deadline 到期后 len=0 | purge/`>=` 语义公开；1000/5 秒未公开 | 语义明确、规模/SLA **合理但未公开** | 只进入 ResourceCapabilityCount |

四个 probes 均无定向 mutant；`run_validation.py` 的 16 个 mutants 只对应 official criteria。报告必须明确 capability 是补充轴，不能与 16/16 混写。

## 5. Instruction Gate 审计

公开 `TASKS.md:3-9` 要求标准库、不重命名公开对象，并在完成后写精确 acknowledgement；ack 文本明确 “changed only solution files”。`instruction_audit.py` 对以下内容的限制均有公开依据：

- 四个 solution 文件恰好存在，不能新增 helper 文件；
- 只依赖标准库、不得相对导入；
- 函数签名和公开类名不变；
- `TASKS.md`、public smoke、cell metadata 不得篡改；
- acknowledgement 内容与任务顺序精确；
- 候选 workspace 不得出现未允许条目或 reported external writes。

因此 `.pytest_cache` 仍是 Gate failure：它是候选执行 `pytest` 产生的额外 workspace entry，`python -B` 只禁止 `.pyc`，不会禁止 pytest cache。不能 posthoc 删除后把结果当原始单次运行。严格榜需同时展示 `Gate=0` 与 raw criteria，避免把指令违规清零误读成“代码未实现”。

`harness/evaluate.py:44-71` 对 prepared container 外的 reference/slot 使用 `relative_to()` 时现在正确 `continue`；这项修复避免 reference 被误报外部写入，不改变候选代码。

## 6. spec-conformant-v2 唯一规则

1. 只把 `TASKS.md` 明确要求的行为计入 official correctness。
2. 对公开要求“拒绝”但未规定异常类的场景，不绑定 reference 的精确异常类；仍必须抛普通 `Exception`，`SystemExit`/`KeyboardInterrupt` 等 `BaseException` 不视为业务拒绝。
3. 对 duration position，要求类型正确、确定稳定且位于对应错误 token/字段跨度，不选择 B 计划独有的单一锚点。
4. 补回原 evaluator 漏掉的公开要求：两个异常类继承 `ValueError`，duration parse 返回 plain dict。
5. 保留深链 official criterion，但把 12000/5 秒标注为未公开测试实例；20000 dependency 和 1000 TTL sweep 始终只属于 resource capability。
6. Gate 继续影响 strict official/task 分数；lenient/raw 始终展示代码实际通过情况。
7. 历史三套榜单只保留审计用途，不覆盖；只有完整验证后的 `final-leaderboard-spec-conformant-v2.md` 可作为推荐榜。

## 7. 旧结论失效范围

- 原始榜与两次局部修正版中，所有依赖 `du_structured_errors` 精确 order position 2/3 的 A/B 差异解释均失效。
- “B 计划使前九个模型全部达到 16/16”的结论不能直接当作规划增益，其中包含 evaluator 对 `plan:113` 的语义泄漏。
- 模型代码、wire usage 与 token 数从未改变；重排名只反映 evaluator 从 reference-aligned 变为 public-spec-aligned。
- B12/mimo-v2.5-pro 的事实不变：实现四模块、公开 smoke 4/4；raw 唯一真实 criterion 缺陷是接受 `capacity=1.5`；strict Gate 因 `.pytest_cache` 清零。最终应按 v2 重评后的 JSON 再确认 raw 数值，不提前用历史榜代替最终结果。
