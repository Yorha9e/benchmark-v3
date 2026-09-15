# 模型分维度能力评价 — spec-conformant-v2

评价对象：short benchmark 14 个模型，B 条件严格榜顺序。

评价方法：逐模型逐维度独立分析，所有结论回溯到 solution 代码、正式评分 JSON、audit 或 events。不生成综合总分，不引用榜单名次作为证据。

---

## B13 — mimo/mimo-v2.5

**正式评分**：`B13.json` — Gate 通过、4/4 任务、16/16 criterion、2/2 extension、2/2 resource、342,149 tokens
**Audit**：passed=true, external_workspace_write=false, protected_files_ok=true, signature_ok=true, reasons=[]
**执行轨迹**：10 次 LLM 请求（turnStep 0.1→0.10），messageCount 1→30，10 个 turn 全部有效利用

### 维度 1：指令遵循

**评级：优秀**

证据：

- `B13.json` audit 字段：`passed: true`，`reasons: []`，`external_workspace_write: false`，`protected_files_ok: true`，`signature_ok: true`（第 9-14 行）
- `instruction_ack.json`：`{"completed_tasks":["recursive_patch","dependency_layers","ttl_set","duration"],"ack":"I followed TASKS.md and changed only solution files."}` — 与 TASKS.md 模板完全一致
- events.jsonl 执行流：先读 `TASKS.md`（[12]）、`public_smoke_tests.py`（[13]）、`frozen-phi3-plan.md`（[14]），再检查目录结构（[24][25]），读四个 stub（[33]-[36]），然后一次性写入四个 solution（[58]-[61]），运行 smoke 测试验证（[80]），最后写 ack（[88]）
- 仅修改 `solutions/` 下 4 个文件 + `instruction_ack.json`，无范围外写入
- 公开对象名称未改动：`DELETE`、`apply_patch`、`DependencyCycleError`、`dependency_layers`、`BoundedTTLSet`、`DurationParseError`、`parse_duration`、`normalize_duration`
- 仅使用标准库：`copy`、`math`、`collections.OrderedDict` — 无第三方依赖
- 完成后运行了 `python -B public_smoke_tests.py -v`，4/4 通过后才写 ack

分析：模型完整执行了 TASKS.md 的全部显式约束——修改范围、公开 API、stdlib 限制、ack 文件格式、交付验证。执行顺序合理：先理解任务→查看现有代码→批量实现→验证→交付。无遗漏、无禁止行为、无声明与实际不符。

### 维度 2：核心业务语义

**评级：优秀**

证据：

- 16/16 隐藏 criterion 全部通过（`B13.json` 第 52-133 行），远超 smoke 测试覆盖范围
- smoke 测试仅覆盖最基础 happy path（各 1 个断言），但模型实现了完整业务规则：

**recursive_patch**（`recursive_patch.py`）：
- 删除仅在 `patch_value is delete` 时触发（第 31 行）— 严格身份判定，非等值
- 递归合并仅在双方均为 plain dict 时发生（第 37 行 `type(old_value) is dict and type(patch_value) is dict`）
- 其余情况整体替换（第 41 行 `copy.deepcopy(patch_value)`）

**dependency_layers**（`dependency_layers.py`）：
- 一次性消费 edges（第 63 行 `for item in edges:`）
- 仅依赖位置出现的节点也被包含（通过 `get_or_create_id` 注册所有对象）
- 重复边去重（第 69-71 行 `seen_edges`）
- 真实有向环检测（Kahn 算法剩余节点，第 104-106 行）

**ttl_set**（`ttl_set.py`）：
- 过期判定 `now >= deadline`（第 42 行）— 精确边界
- 容量淘汰最旧存活项（第 72-73 行 `popitem(last=False)`）
- 重复 key 替换：先删旧项再追加（第 63-69 行）
- 所有可观测操作前先清理过期（add/discard/\_\_contains\_\_/\_\_len\_\_ 均调用 `_purge_expired`）

**duration**（`duration.py`）：
- 严格降序单位校验（第 72 行 `rank <= last_rank`）
- 从属范围校验 h<24, m<60, s<60, ms<1000（第 86-90 行）
- normalize 接受超范围从属字段并进位（第 103-131 行）
- 返回固定顺序五键 dict（第 93 行）

分析：模型不是仅让 smoke 通过——它从 TASKS.md 契约文本推导出全部隐藏规则并正确实现。每个模块的核心业务逻辑（合并/删除、分层/环检测、过期/淘汰、解析/规范化）均符合语义定义。无"针对样例通过但未实现规则"的情况。

### 维度 3：类型与边界精度

**评级：优秀**

证据：

- `ttl_strict_types` 通过 — `ttl_set.py` 第 9-18 行：`type(value) is bool` 拒绝 bool，`isinstance(value, (int, float))` 接受数值，`math.isinf/math.isnan` 拒绝非有限值
- `ttl_set.py` 第 25-26 行：`type(capacity) is bool or not isinstance(capacity, int)` — capacity 拒绝 bool 且必须为 int
- `rp_plain_dict` 通过 — `recursive_patch.py` 第 22-25 行：`type(base) is not dict` — 拒绝 dict 子类
- `du_strict_syntax` 通过 — `duration.py` 第 43-44 行：非数字字符在 position 处报 syntax 错误
- `duration.py` 第 52-53 行：前导零检测（`len(num_str) > 1 and num_str[0] == '0'`），但单值 "0" 被允许
- `duration.py` 第 59-64 行：`ms` 单位在单字符单位之前检测，避免 `m` 被误匹配
- `dependency_layers.py` 第 50 行：`type(existing_obj) is type(obj)` — unhashable 比较时检查类型同一性
- `recursive_patch.py` 第 31 行：`patch_value is delete` — 身份判定而非等值

分析：模型在所有四个模块中都精确区分了类型继承关系（bool vs int、dict 子类 vs plain dict）和身份/等值语义。边界值处理到位：capacity=0 被拒绝（`<= 0`）、ttl 负值被拒绝、空字符串报 empty 错误。错误位置均为零基 position。无"主体逻辑正确但边界处理不够严格"的情况。

### 维度 4：数据完整性与生命周期

**评级：优秀**

证据：

- `rp_isolation` 通过 — `recursive_patch.py` 第 27 行 `result = copy.deepcopy(base)` — 结果与输入完全分离
- 新键和替换值均 deepcopy（第 41、44 行 `copy.deepcopy(patch_value)`）— 无浅复制别名
- DELETE sentinel 的 `__deepcopy__` 和 `__copy__` 返回自身（第 11-15 行）— deepcopy 不会创建第二个 sentinel，保持唯一性
- `ttl_gc_release` 通过 — `ttl_set.py` 第 63-65 行：重复 key 先 `del self._data[existing_key]` 再追加，旧对象引用被释放
- `_purge_expired`（第 39-44 行）：过期项通过 `del self._data[k]` 删除，不留 stale 引用
- `dependency_layers.py`：节点对象存储在 `node_objects` 字典中，返回时直接引用原始对象——但 edges 是一次性 iterable，消费后不保留输入引用

分析：recursive_patch 通过 deepcopy 实现完全隔离，输入不变性有保证。ttl_set 在替换、删除和过期三个路径上都正确释放了旧引用。DELETE sentinel 的 deepcopy 行为被特殊处理，避免了 sentinel 被复制后失去唯一性。无浅复制、隐藏别名或 stale 引用残留。

### 维度 5：确定性与协议纪律

**评级：优秀**

证据：

- `rp_order_boundary` 通过 — `recursive_patch.py` 保留 base key 位置（在原 dict 上 deepcopy），新 key 按 patch 遇遇顺序追加（第 44 行）
- `dl_stable_order_cycle` 通过 — `dependency_layers.py` 第 83 行 `zero_indegree = sorted([...])` 和第 100 行 `next_layer.sort()` — 层内按首次出现顺序（id 顺序）排序
- `dl_one_shot` 通过 — 第 63 行 `for item in edges:` 严格一次性消费
- `ttl_set.py`：OrderedDict 维持插入顺序，淘汰用 `popitem(last=False)` — 确定性最旧优先
- `duration.py`：`_OUTPUT_KEYS` 固定顺序（第 18 行），`_UNIT_RANK` 严格降序（第 15 行），parse 输出 dict 按固定键顺序构建（第 93 行）
- `du_structured_errors` 通过 — DurationParseError 携带稳定 `.code` 和 `.position` 属性（第 7-10 行）

分析：所有模块的输出顺序都是确定性的。dependency_layers 不依赖 set 遍历顺序——用 sorted id 列表保证层内顺序。duration 的错误码和位置属性稳定可复现。recursive_patch 的 dict 顺序在替换边界处也保持正确（旧 key 保留位置，新 key 追加到末尾）。无依赖 hash 或 set 偶然顺序的行为。

### 维度 6：可扩展性与性能

**评级：良好**

证据：

- `res_dependency_20000` 通过 — `dependency_layers.py` 使用迭代 Kahn 算法（第 81-101 行），无递归，可处理 20,000 节点
- `dl_deep_iterative` 通过 — 同上，深层输入不触发递归上限
- `res_ttl_expiry_sweep` 通过 — `ttl_set.py` `_purge_expired` 每次 O(n) 扫描全部条目
- `recursive_patch.py`：deepcopy 为 O(n)，但为隔离所必需
- `duration.py`：解析和规范化均为 O(n)，n 为字符串长度

分析：迭代算法保证了深层和大规模输入的可扩展性。ttl_set 的全量扫描策略在每次操作时 O(n)，对于非常大的集合可能有性能压力，但 resource 测试通过说明在测试规模内可接受。无随输入增长失控的内存结构。性能未以牺牲边界正确性为代价。

不足：ttl_set 的 `_purge_expired` 对所有操作都做全量扫描，如果集合非常大且过期项很少，存在不必要的遍历开销。可考虑按 deadline 排序的堆结构优化，但当前实现已通过 resource 测试。

### 维度 7：泛化能力

**评级：优秀**

证据：

- `ext_recursive_patch_nested_tuple` 通过 — `recursive_patch.py` 对 tuple 值使用 `copy.deepcopy`，递归保留 tuple 的不可变结构
- `ext_duration_large_carry` 通过 — `duration.py` normalize_duration 使用 divmod 逐级进位（第 122-129 行），可处理任意大数值
- `dependency_layers.py` 支持不可哈希节点（第 45-57 行线性扫描回退），不限于字符串/数字
- `ttl_set.py` 使用 `k == value` 比较（非 hash 查找），理论上支持自定义 \_\_eq\_\_ 对象（但 OrderedDict 存储仍需可哈希 key）
- `duration.py` 的 normalize 对任意合法输入幂等（先转毫秒再分解）

分析：两个 extension 测试均通过，证明模型将公开规则推广到了未直接展示的组合场景。duration 的大数进位、recursive_patch 的嵌套不可变类型、dependency_layers 的不可哈希对象处理，都说明实现没有硬编码样例或固定阈值。normalize 的幂等性通过数学分解保证，而非模式匹配。

### 维度 8：Token 效率

**评级：良好**

证据：

- 总 token：342,149（input 36,813 + output 5,880 + cache 299,456）
- 每 criterion 成本：21,384 tokens
- 每严格任务成本：85,537 tokens
- 10 次 LLM 请求，messageCount 从 1 增长到 30
- cache 占比：87.5%（299,456 / 342,149）— 上下文缓存利用率高
- 执行流无重试、无返工：读取→实现→测试→交付，一步到位
- output token 仅 5,880 — 代码产出精炼，无冗余解释

token 分布（per-turn）：
| Turn | input | output | cache |
|------|-------|--------|-------|
| 0.1  | 17,689 | 384 | 2,048 |
| 0.2  | 10,677 | 166 | 19,712 |
| 0.3  | 1,168 | 253 | 30,336 |
| 0.4  | 1,020 | 759 | 31,488 |
| 0.5  | 1,024 | 3,181 | 32,448 |
| 0.6  | 3,610 | 146 | 33,408 |
| 0.7  | 377 | 84 | 36,992 |
| 0.8  | 316 | 238 | 37,312 |
| 0.9  | 588 | 129 | 37,568 |
| 0.10 | 344 | 540 | 38,144 |

分析：342K tokens 换取 16/16 criterion + 4/4 capability probe，效率合理。高 cache 占比（87.5%）表明上下文结构良好，重复读取被缓存。无重试和返工意味着没有浪费。output token 极低（5,880），说明代码产出直接、无冗余。

不足：前两 turn 的 input token 较高（17,689 + 10,677 = 28,366，占总 input 的 77%），主要来自系统提示和任务文件的首次加载。这是一次性成本，无法避免。后续 turn 的 input 极低（377-3,610），说明增量上下文管理高效。

---

## B08 - volcano/doubao-seed-2.1-turbo

**正式评分**：`B08.json` - Gate 通过、4/4 任务、16/16 criterion、2/2 extension、2/2 resource、602,565 tokens
**Audit**：passed=true, external_workspace_write=false, protected_files_ok=true, signature_ok=true, reasons=[]
**执行轨迹**：19 次 LLM 请求（turnStep 0.1->0.19），messageCount 1->递增，37 条 usage record

### 维度 1：指令遵循

**评级：优秀**

证据：

- `B08.json` audit 字段：`passed: true`，`reasons: []`，`external_workspace_write: false`，`protected_files_ok: true`，`signature_ok: true`（第 9-14 行）
- `instruction_ack.json`：与 TASKS.md 模板完全一致
- events.jsonl 执行流：读 TASKS.md（[12]）、smoke tests（[13]）、plan（[14]），Glob 检查目录（[23]），读四个 stub（[30]-[33]），写入四个 solution（[44][51][58][65]），运行 smoke 测试（[73]），然后主动编写自定义边界测试（[81][88][103][116]），最终再跑一次 smoke 确认（[137]），写 ack（[130]）
- 仅修改 `solutions/` 下 4 个文件 + `instruction_ack.json`
- 公开对象名称未改动
- 仅使用标准库：`copy`、`math`、`collections.OrderedDict`

分析：与 B13 一样，指令遵循完全合规。额外值得注意的是 B08 在 smoke 测试通过后主动编写了自定义边界测试（含 weakref/gc 验证），这体现了更高的交付验证意识，但也消耗了更多 token。

### 维度 2：核心业务语义

**评级：优秀**

证据：

- 16/16 隐藏 criterion 全部通过
- 四个模块的核心业务逻辑与 B13 等价正确：

**recursive_patch**（`recursive_patch.py`）：
- 使用辅助函数 `_merge_into` 原地修改 deepcopy 后的 target（第 29-39 行）
- 删除仅 `patch_value is delete` 时触发（第 31 行）
- 递归合并仅 plain dict 对（第 36 行 `type(target[key]) is dict and type(patch_value) is dict`）

**dependency_layers**（`dependency_layers.py`）：
- 一次性消费（第 42 行 `for node, dependency in edges:`）
- Kahn 算法迭代分层（第 63-79 行）
- 重复边去重（第 56-59 行 `seen_edges`）
- 环检测（第 81-84 行）

**ttl_set**（`ttl_set.py`）：
- 过期判定 `now >= deadline`（第 42 行）
- 容量淘汰 `popitem(last=False)`（第 65 行）
- 重复 key 先删后加（第 57-61 行）
- 所有操作前 purge（第 53、69、76、81 行）

**duration**（`duration.py`）：
- 数据驱动设计：`_UNITS` 列表包含 rank、key、max_val（第 12-18 行）
- 严格降序（第 77 行 `rank <= last_rank`）
- normalize 使用 divmod 逐级进位（第 126-144 行）

分析：核心业务语义全部正确。与 B13 实现策略略有不同（如 recursive_patch 使用辅助函数、duration 使用数据驱动表），但结果等价。模型还主动通过自定义测试验证了 GC 释放和边界行为，说明对业务语义的理解是主动验证而非被动假设。

### 维度 3：类型与边界精度

**评级：优秀**

证据：

- `ttl_strict_types` 通过 - `ttl_set.py` 第 8 行 `isinstance(value, bool)` 拒绝 bool，第 10 行 `isinstance(value, (int, float))` 接受数值，第 12 行 `math.isfinite` 拒绝 inf/nan
- capacity 校验：第 21 行 `isinstance(capacity, bool)` 先拒绝 bool，第 23 行 `isinstance(capacity, int)` 再验证 int
- `rp_plain_dict` 通过 - `recursive_patch.py` 第 19-22 行 `type(base) is not dict`
- `du_strict_syntax` 通过 - `duration.py` 第 52-53 行非数字字符报 syntax
- 前导零检测（第 56-57 行 `digit_count > 1 and text[digit_start] == '0'`）
- `ms` 优先匹配（第 65-72 行）
- `dependency_layers.py` 第 30-31 行：unhashable 比较 `existing_node == node` - 注意未检查 type 同一性

分析：类型精度与 B13 基本等价。一个细微差异：B08 的 unhashable 节点比较使用 `==`（第 31 行），而 B13 使用 `type(existing_obj) is type(obj) and existing_obj == obj`。B08 的写法在极端情况下可能跨类型匹配（如 `1 == 1.0`），但所有 criterion 通过说明在实际测试中未触发此问题。

### 维度 4：数据完整性与生命周期

**评级：优秀**

证据：

- `rp_isolation` 通过 - `recursive_patch.py` 第 24 行 `copy.deepcopy(base)` 后原地修改
- 新值 deepcopy（第 39 行 `target[key] = copy.deepcopy(patch_value)`）
- DELETE sentinel 的 `__deepcopy__` 返回自身（第 8-9 行）
- `ttl_gc_release` 通过 - `ttl_set.py` 第 57-58 行重复 key 先 `del`，第 43-44 行过期项 `del`
- B08 主动编写了 weakref + gc 测试验证对象释放（events [81] Bash 命令包含 `weakref, gc`）

分析：数据完整性处理与 B13 等价正确。B08 额外通过自定义 weakref/gc 测试验证了生命周期，这提供了比 B13 更强的证据支撑。deepcopy 隔离、stale 引用清理、过期释放均正确实现。

### 维度 5：确定性与协议纪律

**评级：优秀**

证据：

- `rp_order_boundary` 通过 - deepcopy 保留 base key 位置，新 key 按 patch 顺序追加
- `dl_stable_order_cycle` 通过 - `dependency_layers.py` 第 65 行 `sorted(...)` 和第 79 行 `sorted(next_layer)` - 按 id 顺序
- `dl_one_shot` 通过 - 第 42 行一次性 for 循环
- `ttl_set.py`：OrderedDict + `popitem(last=False)` 确定性淘汰
- `duration.py`：`_OUTPUT_KEYS` 固定顺序（第 24 行），`_UNIT_MAP` 严格 rank（第 21 行）
- `du_structured_errors` 通过 - DurationParseError `.code` 和 `.position` 稳定（第 5-8 行）

分析：确定性与 B13 等价。所有输出顺序稳定，不依赖 set/hash 偶然行为。dependency_layers 的 sorted id 列表保证层内确定性。

### 维度 6：可扩展性与性能

**评级：良好**

证据：

- `res_dependency_20000` 通过 - 迭代 Kahn 算法（第 63-79 行）
- `dl_deep_iterative` 通过 - 同上
- `res_ttl_expiry_sweep` 通过 - `_purge` O(n) 全量扫描（第 40-44 行）
- `duration.py`：解析和规范化均 O(n)

分析：与 B13 策略相同，迭代算法保证可扩展性。ttl_set 的全量扫描在 resource 测试规模内可接受。无递归上限风险。

### 维度 7：泛化能力

**评级：优秀**

证据：

- `ext_recursive_patch_nested_tuple` 通过 - deepcopy 保留 tuple 结构
- `ext_duration_large_carry` 通过 - divmod 逐级进位（第 126-144 行）
- `dependency_layers.py` 支持 unhashable 节点（第 28-36 行线性扫描回退）
- `duration.py` normalize 幂等（数学分解）

分析：泛化能力与 B13 等价。两个 extension 测试通过，实现无硬编码样例。

### 维度 8：Token 效率

**评级：一般**

证据：

- 总 token：602,565（input 101,726 + output 10,671 + cache 490,168）
- 每 criterion 成本：37,660 tokens（B13 为 21,384，B08 高出 76%）
- 每严格任务成本：150,641 tokens（B13 为 85,537，B08 高出 76%）
- 19 次 LLM 请求（B13 为 10 次）
- 6 次 Bash 命令（B13 为 1 次），其中 4 次为自定义边界测试
- cache 占比：81.3%（490,168 / 602,565）

token 分布（关键 turn）：
| Turn | input | output | cache |
|------|-------|--------|-------|
| 0.1  | 20,434 | 337 | 0 |
| 0.4  | 26,699 | 421 | 0 |
| 0.10 | 11,970 | 131 | 20,280 |
| 0.12 | 1,975 | 1,448 | 30,520 |
| 0.14 | 1,714 | 1,667 | 32,568 |

分析：B08 以 602K tokens 取得与 B13 相同的 16/16 + 4/4 结果，但 token 成本高出 76%。主要原因：

1. **过度验证**：smoke 测试通过后主动编写了 4 组自定义边界测试（含 weakref/gc），消耗了 8 个额外 turn。这些测试未发现任何缺陷--代码首次写入即正确。
2. **input token 偏高**：101,726（B13 为 36,813），部分原因是 19 次请求的累积上下文膨胀。
3. **cache 利用率略低**：81.3% vs B13 的 87.5%。

正面：output token 10,671 虽高于 B13 的 5,880，但考虑到代码量和测试脚本，产出比合理。代码质量未因高 token 而额外提升--B08 和 B13 的实现质量等价。

结论：B08 的 token 效率低于 B13。额外的验证工作提高了信心但未改变结果，属于"过度工程"的成本。如果任务是高安全场景，这种验证投入有合理性；在 benchmark 场景下属于冗余。

---

## B14 - volcano/glm-5.2

**正式评分**：`B14.json` - Gate 通过、4/4 任务、16/16 criterion、2/2 extension、2/2 resource、624,991 tokens
**Audit**：passed=true, external_workspace_write=false, protected_files_ok=true, signature_ok=true, reasons=[]
**执行轨迹**：13 次 LLM 请求，25 条 usage record

### 维度 1：指令遵循

**评级：优秀**

证据：

- `B14.json` audit 字段：`passed: true`，`reasons: []`，`external_workspace_write: false`，`protected_files_ok: true`，`signature_ok: true`（第 9-14 行）
- `instruction_ack.json`：与 TASKS.md 模板完全一致
- events.jsonl 执行流：ls 目录（[11]），读 TASKS.md（[17]）、smoke tests（[18]）、plan（[19]）、cell.json（[20]）、ack 模板（[21]），ls solutions（[22]），读四个 stub（[35]-[38]），TodoList（[49]），一次性写入四个 solution（[58]-[61]），TodoList 更新（[71]），运行 smoke 测试（[79]），自定义边界测试含 weakref/gc（[87]），文件卫生检查（[95]），写 ack（[103]），最终验证（[111]）
- 仅修改 `solutions/` 下 4 个文件 + `instruction_ack.json`
- 公开对象名称未改动
- 仅使用标准库：`copy`、`math`、`collections.OrderedDict`
- 额外检查了 `__pycache__` 和 `*.pyc` 文件卫生（[95]）

分析：指令遵循完全合规，且比 B13/B08 更细致--主动检查了文件卫生（无 `__pycache__` 残留）。读取了 `cell.json` 和 `instruction_ack.json` 模板后再写 ack，确保格式精确。交付验证流程完整：smoke -> 自定义边界测试 -> 文件卫生 -> 最终确认。

### 维度 2：核心业务语义

**评级：优秀**

证据：

- 16/16 隐藏 criterion 全部通过
- 四个模块的核心业务逻辑正确，且实现架构最为成熟：

**recursive_patch**（`recursive_patch.py`）：
- 辅助函数 `_merge` 原地修改 deepcopy 结果（第 23-33 行）
- 删除仅 `patch_value is delete`（第 25 行）
- plain dict 递归合并（第 29 行 `type(target[key]) is dict and type(patch_value) is dict`）

**dependency_layers**（`dependency_layers.py`）：
- 封装为 `_NodeRegistry` 类（第 19-67 行），含 `__slots__` 优化
- hash bucket 快速路径 + unhashable 线性扫描回退（第 34-61 行）
- **cross-path equality 检查**：hashable 节点也会检查 unhashable 列表（第 51-54 行），确保 `1` 和 `1.0` 等跨路径等值对象共享同一 id
- 迭代 Kahn 算法（第 93-108 行）

**ttl_set**（`ttl_set.py`）：
- 过期 `now >= deadline`（第 42 行）
- 容量淘汰 `popitem(last=False)`（第 53 行）
- 重复 key 先删后加（第 49-51 行）
- 所有操作前 purge

**duration**（`duration.py`）：
- 命名常量 `_RANK_D` 等（第 16-20 行）替代魔法数字
- `_scan` 函数明确注释"不为超范围从属值报错"（第 47-48 行），精确区分 parse 和 normalize 的语义边界
- normalize 使用 divmod 逐级分解（第 127-144 行）

分析：核心业务语义全部正确，且实现质量在三个模型中最高。`_NodeRegistry` 的 cross-path equality 检查是一个 B13/B08 都没有的细节--它确保了 hashable 和 unhashable 对象之间的等值关系被正确识别。duration 的 `_scan` 函数通过注释明确区分了 parse（报 range 错误）和 normalize（接受超范围）的语义差异。

### 维度 3：类型与边界精度

**评级：优秀**

证据：

- `ttl_strict_types` 通过 - `ttl_set.py` 第 8 行 `isinstance(value, bool) or not isinstance(value, (int, float))` 同时拒绝 bool 和非数值
- capacity 校验：第 19 行 `isinstance(capacity, bool) or not isinstance(capacity, int)` - 先 bool 后 int
- `rp_plain_dict` 通过 - `recursive_patch.py` 第 37-40 行 `type(base) is not dict`
- `du_strict_syntax` 通过 - `duration.py` 第 61-62 行非数字字符报 syntax
- 前导零检测（第 67-68 行）
- `ms` 优先匹配（第 75-76 行）
- `_NodeRegistry.id_of`：hash 失败后走 TypeError 回退（第 37-45 行），不依赖 try-except 以外的类型假设

分析：类型精度与 B13/B08 等价正确。bool 拒绝、plain dict 校验、前导零、ms 优先匹配均到位。`_NodeRegistry` 的 hash bucket + 线性扫描回退在类型处理上更健壮--不假设节点一定可哈希或一定不可哈希，而是运行时动态选择路径。

### 维度 4：数据完整性与生命周期

**评级：优秀**

证据：

- `rp_isolation` 通过 - `recursive_patch.py` 第 41 行 `copy.deepcopy(base)` 后原地修改
- 新值 deepcopy（第 32 行）
- DELETE sentinel `__deepcopy__` 返回自身（第 13-14 行），且有 docstring 说明原因（第 7-11 行）
- `ttl_gc_release` 通过 - `ttl_set.py` 第 49-50 行重复 key 先 `del`，第 43-44 行过期项 `del`
- B14 主动编写了 weakref + gc 测试验证对象释放（events [87]）

分析：数据完整性处理与 B13/B08 等价正确。B14 额外通过自定义 weakref/gc 测试验证了生命周期。DELETE sentinel 的 docstring 明确解释了 `__deepcopy__` 返回自身的原因，体现了对数据完整性的主动意识。

### 维度 5：确定性与协议纪律

**评级：优秀**

证据：

- `rp_order_boundary` 通过 - deepcopy 保留 base key 位置，新 key 按 patch 顺序追加
- `dl_stable_order_cycle` 通过 - `dependency_layers.py` 第 96 行 `current.sort()` 和第 107 行 `nxt.sort()` - 按 id 顺序
- `dl_one_shot` 通过 - 第 76 行一次性 for 循环
- `ttl_set.py`：OrderedDict + `popitem(last=False)`
- `duration.py`：`_KEY_BY_RANK` 固定顺序（第 34-40 行），result 按固定键顺序构建（第 108-114 行）
- `du_structured_errors` 通过 - DurationParseError `.code` 和 `.position` 稳定（第 7-12 行），额外接受可选 message 参数但不影响 code/position 稳定性

分析：确定性与 B13/B08 等价。`_NodeRegistry` 的 id 分配严格按首次出现顺序（`len(ids)` 作为新 id），层内排序按 id（即首次出现顺序），保证确定性。DurationParseError 的 code/position 属性稳定且不可变。

### 维度 6：可扩展性与性能

**评级：良好**

证据：

- `res_dependency_20000` 通过 - 迭代 Kahn 算法（第 93-108 行）
- `dl_deep_iterative` 通过
- `res_ttl_expiry_sweep` 通过 - `_purge` O(n) 全量扫描（第 41-44 行）
- `_NodeRegistry` 使用 `__slots__`（第 27 行）减少内存开销
- hash bucket 快速路径减少可哈希节点的查找开销

分析：与 B13/B08 策略相同，迭代算法保证可扩展性。`_NodeRegistry` 的 hash bucket 设计在大量可哈希节点时比 B13/B08 的单一字典查找更高效（冲突时仅扫描 bucket 内 id，而非全量扫描）。`__slots__` 减少了 registry 对象的内存占用。ttl_set 的全量扫描在 resource 测试规模内可接受。

### 维度 7：泛化能力

**评级：优秀**

证据：

- `ext_recursive_patch_nested_tuple` 通过 - deepcopy 保留 tuple 结构
- `ext_duration_large_carry` 通过 - divmod 逐级进位（第 127-144 行）
- `_NodeRegistry` 支持 hashable 和 unhashable 混合节点（第 34-61 行），且 cross-path equality 确保跨类型等值对象正确识别
- `duration.py` normalize 幂等（数学分解）

分析：泛化能力与 B13/B08 等价。cross-path equality 检查是一个额外的泛化优势--当输入混合了可哈希和不可哈希但等值的对象时，B14 能正确识别为同一节点，而 B13/B08 可能将其视为不同节点。但在实际 criterion 测试中这一差异未导致不同结果。

### 维度 8：Token 效率

**评级：一般**

证据：

- 总 token：624,991（input 59,007 + output 33,376 + cache 532,608）
- 每 criterion 成本：39,062 tokens（B13 为 21,384，B14 高出 83%）
- 每严格任务成本：156,248 tokens
- 13 次 LLM 请求（B13 为 10 次，B08 为 19 次）
- output token 33,376 远高于 B13（5,880）和 B08（10,671）
- cache 占比：85.2%（532,608 / 624,991）

token 分布（关键 turn）：
| Turn | input | output | cache |
|------|-------|--------|-------|
| 0.1  | 18,623 | 107 | 0 |
| 0.4  | 686 | **25,511** | 24,640 |
| 0.5  | 25,672 | 2,760 | 25,280 |
| 0.7  | 424 | 69 | 53,888 |
| 0.8  | 217 | 2,722 | 54,272 |

分析：B14 以 625K tokens 取得 16/16 + 4/4 结果，token 成本高出 B13 约 83%。主要原因：

1. **单次大输出**：turn [53]（turnStep 0.4）单次输出 25,511 tokens，包含全部四个模块的详细推理和实现规划。这是 B14 独有的模式--在一个 turn 内完成所有模块的思考，而非分步实现。
2. **output token 偏高**：33,376（B13 为 5,880，B08 为 10,671）。部分原因是大段 thinking 内容和详细的代码注释/docstring。
3. **验证开销**：4 次 Bash 命令（smoke + 自定义边界测试 + 文件卫生 + 最终确认），与 B08 类似但少于 B08 的 6 次。

正面：代码质量在三个模型中最高（`_NodeRegistry` 类、cross-path equality、`__slots__`、命名常量、详细 docstring）。高 output token 部分转化为代码质量提升--但 16/16 结果与 B13 相同，说明额外质量在当前 criterion 集合下未被检测到。

结论：B14 的 token 效率低于 B13，与 B08 接近。高 output token 主要来自单次大推理和更精细的代码编写。如果评价标准包含代码可维护性和架构质量，B14 的额外成本有合理性；在纯 criterion 通过率视角下属于冗余。

---

## B04 - kimi-code/kimi-for-coding

**正式评分**：`B04.json` - Gate 通过、4/4 任务、16/16 criterion、2/2 extension、2/2 resource、803,531 tokens
**Audit**：passed=true, external_workspace_write=false, protected_files_ok=true, signature_ok=true, reasons=[]
**执行轨迹**：21 次 LLM 请求，41 条 usage record，15 次 Bash（14 次自定义测试 + 1 次 smoke）

### 维度 1：指令遵循

**评级：优秀**

证据：

- audit 全项通过，reasons=[]
- `instruction_ack.json` 格式正确
- 执行流：ls 目录 -> 读 TASKS.md/smoke/plan -> ls solutions -> 读 4 stub -> 写 4 solution -> smoke 测试 -> 14 次自定义测试 -> 最终 smoke 确认 -> 写 ack -> git status 检查 -> 读 ack 确认
- 仅修改 4 个 solution 文件 + ack
- 仅使用标准库

分析：指令遵循完全合规。B04 是所有模型中验证最密集的--14 次自定义测试脚本覆盖了各类边界场景。额外的 `git status` 检查表明模型对工作区状态有主动确认意识。

### 维度 2：核心业务语义

**评级：优秀**

证据：

- 16/16 criterion 全过
- 四模块核心逻辑正确：

**recursive_patch**：`_is_plain_dict` 辅助函数（第 17-18 行），删除 `is delete`（第 30 行），递归合并 plain dict 对（第 34 行），deepcopy 隔离（第 27 行）

**dependency_layers**：一次性消费（第 38 行），Kahn 算法迭代（第 57-71 行），边去重（第 42-44 行），环检测（第 73-75 行）

**ttl_set**：过期 `now >= deadline`（第 44 行），淘汰 `popitem(last=False)`（第 58 行），重复 key 先删后加（第 52-55 行），静态验证方法 `_validate_positive_int`/`_validate_finite_nonnegative_number`（第 16-37 行）

**duration**：数据驱动 `_UNITS`/`_UNIT_TO_RANK`/`_RANGES`（第 13-16 行），`_scan_fields` 共享解析（第 19-67 行），normalize 使用模运算分解（第 104-111 行）

分析：核心业务语义全部正确。B04 的 normalize 实现使用模运算（`total_ms % 1000` 等）而非 divmod 链，结果等价但风格不同。ttl_set 使用静态方法封装验证逻辑，提高了代码可读性。

### 维度 3：类型与边界精度

**评级：优秀**

证据：

- `ttl_strict_types` 通过 - `ttl_set.py` 第 18 行 `isinstance(value, bool) or not isinstance(value, int)` 拒绝 bool
- `_validate_finite_number`（第 25-30 行）：bool 拒绝 + `math.isfinite` 拒绝 inf/nan
- `rp_plain_dict` 通过 - `recursive_patch.py` 第 17-18 行 `type(value) is dict`
- `du_strict_syntax` 通过 - `duration.py` 第 33-34 行非数字字符报 syntax
- 前导零检测（第 40-41 行）
- `ms` 优先匹配使用 `text.startswith("ms", pos)`（第 47 行）- 更 Pythonic 的写法

分析：类型精度与前三模型等价正确。`text.startswith("ms", pos)` 是比字符切片比较更简洁的 ms 检测方式。静态验证方法使类型检查逻辑集中且可测试。

### 维度 4：数据完整性与生命周期

**评级：优秀**

证据：

- `rp_isolation` 通过 - deepcopy 隔离（第 27 行）
- DELETE sentinel `__deepcopy__` 返回自身（第 10-11 行）
- `ttl_gc_release` 通过 - 重复 key 先 `del`（第 52-53 行），过期项 `del`（第 45-46 行）
- 14 次自定义测试中包含隔离和 GC 验证

分析：数据完整性处理与前三模型等价正确。B04 的验证最为充分--14 次自定义测试覆盖了各类生命周期场景。

### 维度 5：确定性与协议纪律

**评级：良好**

证据：

- `rp_order_boundary` 通过
- `dl_stable_order_cycle` 通过 - 但 **未显式排序层内节点**
- `dl_one_shot` 通过
- `ttl_set.py`：OrderedDict + `popitem(last=False)`
- `duration.py`：`_OUTPUT_KEYS` 固定顺序，result 按固定键构建（第 78-82 行）
- `du_structured_errors` 通过

分析：确定性结果正确，但 B04 的 `dependency_layers` 未对层内节点显式排序（第 71 行 `current = next_layer` 无 sort）。层内顺序依赖 `next_layer` 的追加顺序，即 dependents 列表的顺序，即 edge_list 的顺序，即首次出现顺序。虽然结果正确（id 按首次出现分配），但缺少显式排序使确定性依赖实现细节而非显式保证。与 B13（`sorted`）、B08（`sorted`）、B14（`sort()`）相比，这是一个风格上的不足。

### 维度 6：可扩展性与性能

**评级：良好**

证据：

- `res_dependency_20000` 通过 - 迭代 Kahn 算法
- `dl_deep_iterative` 通过
- `res_ttl_expiry_sweep` 通过 - `_purge` O(n) 全量扫描
- duration 解析和规范化均 O(n)

分析：与前三模型策略相同。迭代算法保证可扩展性，无递归风险。

### 维度 7：泛化能力

**评级：优秀**

证据：

- `ext_recursive_patch_nested_tuple` 通过
- `ext_duration_large_carry` 通过
- `dependency_layers` 支持 unhashable 节点（第 22-36 行 try-except 回退）
- normalize 使用模运算分解，可处理任意大数值

分析：泛化能力与前三模型等价。无硬编码样例或固定阈值。

### 维度 8：Token 效率

**评级：较差**

证据：

- 总 token：803,531（input 54,031 + output 27,068 + cache 722,432）
- 每 criterion 成本：50,221 tokens（B13 为 21,384，B04 高出 135%）
- 每严格任务成本：200,883 tokens
- 21 次 LLM 请求（最多：B13=10, B08=19, B14=13）
- 15 次 Bash 命令（最多：B13=1, B08=6, B14=4），其中 14 次为自定义测试
- cache 占比：89.9%（722,432 / 803,531）- 最高

分析：B04 以 804K tokens 取得 16/16 + 4/4 结果，token 成本为所有 16/16 模型中最高。主要原因：

1. **过度测试**：14 次自定义测试脚本，远超 B08（4 次）和 B14（3 次）。这些测试未发现任何缺陷--代码首次写入即正确。
2. **LLM 请求次数最多**：21 次，每次请求的累积上下文膨胀导致 cache token 高达 722K。
3. **output token 偏高**：27,068，部分来自大段测试脚本内容。

正面：cache 占比 89.9% 为最高，说明上下文结构良好。代码质量与 B08 等价，但验证投入显著过度。

结论：B04 的 token 效率是所有满分模型中最低的。14 次自定义测试在代码已正确的情况下属于显著冗余。如果减少到 2-3 次关键验证（如 B14），token 成本可降低约 40%。

---

## B06 - qwen/qwen3.8-max-preview

**正式评分**：`B06.json` - Gate 通过、4/4 任务、16/16 criterion、2/2 extension、2/2 resource、863,422 tokens
**Audit**：passed=true, external_workspace_write=false, protected_files_ok=true, signature_ok=true, reasons=[]
**执行轨迹**：18 次 LLM 请求，9 次 Bash（7 次自定义测试 + 1 次 smoke + 1 次最终确认），无返工

### 维度 1：指令遵循

**评级：优秀**

证据：

- audit 全项通过
- 执行流：读 TASKS.md/smoke/plan -> ls 目录 -> 读 4 stub + ack 模板 -> TodoList -> 写 4 solution -> TodoList -> 7 次自定义测试 -> 写 ack -> 最终 smoke + cat ack 确认
- 仅修改 4 个 solution 文件 + ack
- 仅使用标准库
- 主动读取了 `instruction_ack.json` 模板（[34]）确保格式正确

分析：指令遵循完全合规。B06 额外读取了 ack 模板文件，确保交付格式精确。最终验证包含 smoke 测试和 ack 内容确认。

### 维度 2：核心业务语义

**评级：优秀**

证据：

- 16/16 criterion 全过
- 四模块核心逻辑正确，且设计有独到之处：

**recursive_patch**（`recursive_patch.py`）：
- `_merge` 辅助函数 deepcopy 后原地修改（第 30-44 行）
- 注释解释 key 顺序保留逻辑（第 31-33 行）："assigning to an existing key preserves its position while genuinely new keys are appended"
- 替换值 deepcopy 注释（第 42-43 行）："replacement dict keeps patch's own order"

**dependency_layers**（`dependency_layers.py`）：
- `intern` 函数含 **cross-path equality 检查**（第 33-36、42-44 行）- unhashable 节点会检查 hashable 索引，hashable 节点也会检查 unhashable 索引
- 注释（第 64 行）："order by first appearance, not adjacency accident"

**ttl_set**（`ttl_set.py`）：
- 注释解释设计决策（第 31-33 行）："No heaps, tombstones, or history queues that could retain stale object references"
- 注释解释非单调 clock 处理（第 40-41 行）："scan every live entry instead of stopping at the first unexpired one"
- 注释解释重复 key 处理（第 49-51 行）："a plain assignment would keep the old key object"

**duration**（`duration.py`）：
- **`_scan(text, check_range)` 参数化设计**（第 23、56-60 行）：parse 使用 `check_range=True`，normalize 使用 `check_range=False`。这比其他模型的"scan 不检查 range"设计更精确--显式控制是否执行范围检查
- `_UNIT_MILLIS` 使用下划线分隔符（`86_400_000`）提升可读性（第 13 行）

分析：核心业务语义全部正确，且代码质量在已评价模型中与 B14 并列最高。`check_range` 参数化设计是独有的--它精确地表达了 parse 和 normalize 对范围检查的不同需求，而非通过注释或分离逻辑来处理。cross-path equality 检查与 B14 等价。代码注释密度高且信息量大，解释了"为什么"而非"做什么"。

### 维度 3：类型与边界精度

**评级：优秀**

证据：

- `ttl_strict_types` 通过 - `ttl_set.py` 第 8 行 `isinstance(value, bool)` 拒绝 bool，第 12 行 `math.isfinite` 拒绝 inf/nan
- capacity 校验（第 19-20 行）：bool 拒绝 + int 验证
- `rp_plain_dict` 通过 - `type(base) is not dict`（第 23 行）
- `du_strict_syntax` 通过 - 第 36-37 行非数字字符报 syntax
- 前导零检测（第 39-40 行）
- `ms` 优先匹配使用 `text.startswith("ms", pos)`（第 42 行）
- `_DeleteSentinel` 使用 `__slots__ = ()`（第 9 行）- 防止属性注入

分析：类型精度与已评价模型等价正确。`__slots__` 是额外的安全措施，防止 sentinel 被意外添加属性。

### 维度 4：数据完整性与生命周期

**评级：优秀**

证据：

- `rp_isolation` 通过 - deepcopy 隔离（第 34 行）
- DELETE sentinel `__deepcopy__` 返回自身（第 14-16 行），注释说明原因
- `ttl_gc_release` 通过 - 重复 key 先 `del`（第 52-53 行），注释解释"plain assignment would keep the old key object"
- ttl_set 注释明确说明"No heaps, tombstones, or history queues"避免 stale 引用（第 31-33 行）
- 7 次自定义测试包含 weakref/gc 验证

分析：数据完整性处理正确，且代码注释主动解释了避免 stale 引用的设计决策。B06 的注释质量在已评价模型中最高--不仅实现正确，还解释了为什么选择当前设计而非其他看似合理的替代方案。

### 维度 5：确定性与协议纪律

**评级：优秀**

证据：

- `rp_order_boundary` 通过 - 注释解释 key 顺序保留逻辑
- `dl_stable_order_cycle` 通过 - 第 64 行 `current.sort()` 显式排序，注释"order by first appearance, not adjacency accident"
- `dl_one_shot` 通过 - 第 50 行一次性 for 循环，注释"Consume the one-shot iterable exactly once"
- `ttl_set.py`：OrderedDict + `popitem(last=False)`
- `duration.py`：`_KEY_ORDER` 固定顺序（第 12 行），result 按固定键构建（第 69 行）
- `du_structured_errors` 通过 - DurationParseError `.code`/`.position` 稳定（第 17-20 行）

分析：确定性优秀。B06 显式排序层内节点（与 B13/B08/B14 一致，优于 B04）。注释"order by first appearance, not adjacency accident"说明模型理解确定性的重要性并主动保证。

### 维度 6：可扩展性与性能

**评级：良好**

证据：

- `res_dependency_20000` 通过 - 迭代 Kahn 算法
- `dl_deep_iterative` 通过
- `res_ttl_expiry_sweep` 通过 - `_purge` O(n) 全量扫描，注释解释非单调 clock 需要
- duration 解析和规范化均 O(n)

分析：与已评价模型策略相同。注释解释了全量扫描的必要性（非单调 clock），说明性能选择是有意识的权衡。

### 维度 7：泛化能力

**评级：优秀**

证据：

- `ext_recursive_patch_nested_tuple` 通过
- `ext_duration_large_carry` 通过 - divmod 逐级进位（第 77-80 行）
- `dependency_layers` 支持 cross-path equality（第 33-44 行）
- normalize 使用 `check_range=False` 接受超范围从属字段（第 73 行）
- `_UNIT_MILLIS` 数据驱动，无硬编码

分析：泛化能力与 B14 等价。`check_range` 参数化设计使 normalize 能正确接受超范围输入，这是对"normalize 接受超范围从属字段"契约的精确泛化。

### 维度 8：Token 效率

**评级：较差**

证据：

- 总 token：863,422（input 108 + output 33,799 + cache 829,515）
- 每 criterion 成本：53,964 tokens（B13 为 21,384，B06 高出 152%）
- 每严格任务成本：215,856 tokens
- 18 次 LLM 请求
- 9 次 Bash 命令（7 次自定义测试）
- cache 占比：96.1%（829,515 / 863,422）- **最高**
- input token 仅 108 - **极低**，几乎全部走 cache

分析：B06 以 863K tokens 取得 16/16 + 4/4 结果，token 成本为所有模型中最高。特征极为特殊：

1. **input=108**：几乎所有输入都通过 cache 提供。这说明 B06 的 provider 缓存命中率极高，但也意味着 cache token（829K）构成了成本主体。
2. **cache 占比 96.1%**：远超 B13（87.5%）、B08（81.3%）、B14（85.2%）、B04（89.9%）。这可能是 provider 端的缓存策略差异，而非模型行为差异。
3. **7 次自定义测试**：与 B04（14 次）相比较少，但仍多于 B14（3 次）和 B08（4 次）。
4. **output 33,799**：与 B14（33,376）几乎相同，高于 B08（10,671）和 B13（5,880）。

正面：代码质量与 B14 并列最高（cross-path equality、`check_range` 参数化、`__slots__`、高质量注释）。无返工，首次写入即正确。

结论：B06 的 token 成本最高，但代码质量也最高。高 cache token 主要是 provider 缓存策略的结果，不完全由模型行为决定。如果排除 cache 成本（仅看 input+output = 33,907），B06 的实际推理成本极低。但在按总 token 计费的场景下，B06 是最贵的。

---

## B01 - MT/LongCat-2.0

**正式评分**：`B01.json` - Gate 通过、4/4 任务、16/16 criterion、2/2 extension、2/2 resource、869,279 tokens
**Audit**：passed=true, external_workspace_write=false, protected_files_ok=true, signature_ok=true, reasons=[]
**执行轨迹**：20 次 LLM 请求，14 次 Bash（1 次 smoke + 13 次自定义测试），1 次 Edit（duration.py 修正）

### 维度 1：指令遵循

**评级：优秀**

证据：

- audit 全项通过
- 执行流：读 TASKS.md/smoke/plan -> ls -> 读 4 stub + ack + cell.json -> 写 4 solution -> 写 ack -> smoke 测试 -> Edit duration.py 修正 -> 13 次自定义测试
- 仅修改 4 个 solution 文件 + ack
- 仅使用标准库

分析：指令遵循完全合规。B01 是已评价模型中唯一有 Edit 修正的--duration.py 写入后通过测试发现需要小幅重构（将 `_scan` 返回的 fields 从 key 名改为 unit 字符串），随后修正并重新测试。这体现了"测试-发现-修正"的完整闭环。

### 维度 2：核心业务语义

**评级：优秀**

证据：

- 16/16 criterion 全过
- 四模块核心逻辑正确：

**recursive_patch**：`type(base) is not dict` 校验（第 20 行），删除 `is delete`（第 26 行），递归合并 plain dict 对（第 30 行），deepcopy 隔离（第 24 行）

**dependency_layers**：hash bucket 注册（第 14-42 行），一次性消费（第 48 行），Kahn 算法迭代（第 62-72 行），边去重（第 51-53 行），环检测（第 74-76 行）

**ttl_set**：`_validate_finite_number` 静态方法含 `nonnegative` 参数（第 22-29 行），过期 `now >= deadline`（第 37 行），淘汰 `popitem(last=False)`（第 48 行），重复 key 先删后加（第 44-46 行）

**duration**：模块级常量 `UNITS`/`UNIT_RANK`/`PARSE_CAP`/`MS_PER_UNIT`（第 4-15 行），`_scan` 共享解析（第 25-58 行），normalize divmod 逐级分解（第 83-86 行）

分析：核心业务语义全部正确。B01 的实现风格中规中矩--hash bucket 注册、静态验证方法、数据驱动常量都是合理的工程选择，但没有 B14/B06 的 cross-path equality 或 `check_range` 参数化等独特设计。

### 维度 3：类型与边界精度

**评级：优秀**

证据：

- `ttl_strict_types` 通过 - 第 9 行 `isinstance(capacity, bool)` 拒绝 bool，第 23 行 `isinstance(value, bool)` 拒绝 bool
- `_validate_finite_number`（第 23-28 行）：bool 拒绝 + `math.isfinite` 拒绝 inf/nan + nonnegative 检查
- `rp_plain_dict` 通过 - `type(base) is not dict`（第 20 行）
- `du_strict_syntax` 通过 - 第 35-36 行非数字字符报 syntax
- 前导零检测（第 41-42 行）
- `ms` 优先匹配 `text[i:i+2] == "ms"`（第 45 行）

分析：类型精度与已评价模型等价正确。`_validate_finite_number` 的 `nonnegative` 参数是一个简洁的设计，将 ttl（nonnegative=True）和 clock 结果（nonnegative=False）的验证统一到一个方法。

### 维度 4：数据完整性与生命周期

**评级：优秀**

证据：

- `rp_isolation` 通过 - deepcopy 隔离（第 24 行）
- DELETE sentinel `__deepcopy__` 返回自身（第 9-10 行）
- `ttl_gc_release` 通过 - 重复 key 先 `del`（第 44-45 行），过期项 `del`（第 38-39 行）
- 13 次自定义测试含隔离和 GC 验证

分析：数据完整性处理与已评价模型等价正确。

### 维度 5：确定性与协议纪律

**评级：优秀**

证据：

- `rp_order_boundary` 通过
- `dl_stable_order_cycle` 通过 - 第 58 行 `sorted(...)` 和第 71 行 `next_layer.sort()` - 显式排序
- `dl_one_shot` 通过 - 第 48 行一次性 for 循环
- `ttl_set.py`：OrderedDict + `popitem(last=False)`
- `duration.py`：`OUTPUT_KEYS` 固定顺序（第 13 行），result 按固定键构建（第 63-69 行）
- `du_structured_errors` 通过 - DurationParseError `.code`/`.position` 稳定（第 18-22 行）

分析：确定性优秀。B01 显式排序层内节点，与 B13/B08/B14/B06 一致。

### 维度 6：可扩展性与性能

**评级：良好**

证据：

- `res_dependency_20000` 通过 - 迭代 Kahn 算法
- `dl_deep_iterative` 通过
- `res_ttl_expiry_sweep` 通过 - `_purge` O(n) 全量扫描
- hash bucket 注册减少可哈希节点查找开销

分析：与已评价模型策略相同。hash bucket 设计在大量可哈希节点时效率良好。

### 维度 7：泛化能力

**评级：良好**

证据：

- `ext_recursive_patch_nested_tuple` 通过
- `ext_duration_large_carry` 通过 - divmod 逐级进位
- `dependency_layers` 支持 unhashable 节点（第 33-42 行线性扫描回退）
- normalize 使用数学分解，可处理任意大数值

分析：泛化能力正确，但缺少 cross-path equality 检查（B14/B06 有）。在混合 hashable/unhashable 等值节点的极端场景下，B01 可能将等值对象视为不同节点。但在实际 criterion 测试中未触发此问题。

### 维度 8：Token 效率

**评级：较差**

证据：

- 总 token：869,279（input 838,314 + output 30,965 + cache **0**）
- 每 criterion 成本：54,330 tokens
- 每严格任务成本：217,320 tokens
- 20 次 LLM 请求
- 14 次 Bash 命令
- **cache_tokens = 0**：provider 不支持缓存，全部 input 按原价计费
- 1 次 Edit 修正

分析：B01 以 869K tokens 取得 16/16 + 4/4 结果。token 成本与 B06（863K）接近，但构成截然不同：

- B01：input 838K（96.5%）+ output 31K（3.5%）+ cache 0
- B06：input 108（0.01%）+ output 34K（3.9%）+ cache 829K（96.1%）

B01 的 input 极高是因为 **无缓存**--20 次 LLM 请求每次都包含完整上下文。如果 provider 支持缓存，B01 的有效成本可能降低 80%+。

output 30,965 适中，与 B14（33,376）和 B06（33,799）接近。1 次 Edit 修正消耗了少量额外 token，但影响不大。

正面：14 次自定义测试虽然多，但 1 次 Edit 说明测试确实发现了需要修正的问题--与 B04（14 次测试但 0 次修正）相比，B01 的测试投入有实际回报。

结论：B01 的 token 效率受 provider 无缓存影响严重。代码质量中上，测试投入有效（发现并修正了 1 个问题）。如果排除 provider 差异（假设有缓存），B01 的实际效率可能接近 B08 水平。

---

## B07 - GPT/gpt-5.6-luna

**正式评分**：`B07.json` - Gate 通过、4/4 任务、16/16 criterion、2/2 extension、2/2 resource、880,477 tokens
**Audit**：passed=true, external_workspace_write=false, protected_files_ok=true, signature_ok=true, reasons=[]
**执行轨迹**：22 次 LLM 请求，11 次 Bash（9 次自定义测试 + 2 次 smoke），无返工

### 维度 1：指令遵循

**评级：优秀**

证据：

- audit 全项通过
- 执行流：读 TASKS.md/smoke/plan -> 读 4 stub -> 写 4 solution -> smoke 测试 -> 9 次自定义测试 -> Glob 检查 -> 读 ack 模板 -> 3 次额外测试 -> 最终 smoke -> 写 ack -> 读 ack 确认
- 仅修改 4 个 solution 文件 + ack
- 仅使用标准库

分析：指令遵循完全合规。B07 的验证流程最为完整--在 9 次自定义测试后还额外做了 3 次测试和文件检查，然后才写 ack。22 次 LLM 请求是所有模型中最多的。

### 维度 2：核心业务语义

**评级：优秀**

证据：

- 16/16 criterion 全过
- 四模块核心逻辑正确，且包含多项独特的防御性实现：

**recursive_patch**（`recursive_patch.py`）：
- 共享 `memo` 字典优化 deepcopy（第 23-24 行 `memo = {}; result = deepcopy(base, memo)`）- 避免重复拷贝同一对象
- 闭包式 `merge` 函数（第 26-42 行）

**dependency_layers**（`dependency_layers.py`）：
- `_nodes_equal` 辅助函数（第 10-20 行）：**异常安全**的等值比较，处理 `__eq__` 可能抛出异常的对象
- **cross-path equality**（第 44-47 行）：hashable 值也检查 unhashable_ids
- hash bucket + unhashable 列表双路径注册（第 31-64 行）

**ttl_set**（`ttl_set.py`）：
- `_deadline` 方法（第 40-44 行）：处理 `now + ttl` 的 **OverflowError**，返回 `math.inf` - 唯一处理此边界情况的模型
- `_purge` 使用 `list(self._entries.items())`（第 36 行）- 安全迭代，避免删除时修改字典

**duration**（`duration.py`）：
- `_decimal_value` 函数（第 28-32 行）：手动 `ord` 转换数字，不依赖 `int()`
- `_decimal` 函数（第 95-102 行）：**任意精度大数格式化**，使用 10^9 分块处理 - 专为 `ext_duration_large_carry` 设计
- `_error` 工厂函数（第 24-25 行）统一错误创建

分析：核心业务语义全部正确，且 B07 的实现是已评价模型中**防御性最强**的。`_nodes_equal` 的异常安全处理、`_deadline` 的溢出处理、`_decimal` 的大数格式化都是其他模型没有的独特边界处理。这些处理在当前 criterion 集合下未被直接测试，但体现了对极端边界场景的深度考虑。

### 维度 3：类型与边界精度

**评级：优秀**

证据：

- `ttl_strict_types` 通过 - 第 8 行 `isinstance(value, bool)` 拒绝 bool，第 10 行 `math.isfinite` 拒绝 inf/nan
- `rp_plain_dict` 通过 - `type(base) is not dict`（第 18 行）
- `du_strict_syntax` 通过 - 第 47-48 行非数字字符报 syntax
- 前导零检测（第 54-55 行）
- `ms` 优先匹配 `text.startswith("ms", position)`（第 59 行）
- `_DeleteSentinel` 使用 `__slots__ = ()`（第 7 行）
- `_deadline` 溢出处理（第 40-44 行）- 额外的数值边界保护

分析：类型精度优秀，且有额外的边界保护（OverflowError 处理）。`_nodes_equal` 的异常安全设计在类型精度上也提供了额外保障--即使 `__eq__` 实现有缺陷，也不会导致注册逻辑崩溃。

### 维度 4：数据完整性与生命周期

**评级：优秀**

证据：

- `rp_isolation` 通过 - 共享 memo deepcopy 隔离（第 23-24 行）
- DELETE sentinel `__deepcopy__` 返回自身（第 9-10 行），`__slots__` 防止属性注入
- `ttl_gc_release` 通过 - 重复 key 先 `del`（第 50-51 行），过期项 `del`（第 36-38 行）
- `_purge` 使用 `list()` 快照避免迭代时修改（第 36 行）
- 9 次自定义测试含 GC 验证

分析：数据完整性处理正确，且 `_purge` 的 `list()` 快照是比列表推导更安全的迭代删除方式。共享 memo 的 deepcopy 在处理有循环引用的输入时也能正确工作。

### 维度 5：确定性与协议纪律

**评级：优秀**

证据：

- `rp_order_boundary` 通过
- `dl_stable_order_cycle` 通过 - 第 82 行 `ready.sort()` 显式排序
- `dl_one_shot` 通过 - 第 67 行一次性 for 循环
- `ttl_set.py`：OrderedDict + `popitem(last=False)`
- `duration.py`：固定键顺序构建（第 86-92 行）
- `du_structured_errors` 通过 - DurationParseError `.code`/`.position` 稳定（第 16-21 行），可选 message 不影响稳定性

分析：确定性优秀。显式排序、固定键顺序、稳定错误属性均到位。

### 维度 6：可扩展性与性能

**评级：良好**

证据：

- `res_dependency_20000` 通过 - 迭代 Kahn 算法
- `dl_deep_iterative` 通过
- `res_ttl_expiry_sweep` 通过 - `_purge` O(n) 全量扫描
- 共享 memo deepcopy 优化（第 23 行）- 减少重复对象拷贝
- hash bucket 快速路径

分析：与已评价模型策略相同。共享 memo deepcopy 是额外的性能优化，在 base 包含重复引用的对象时可以减少拷贝次数。

### 维度 7：泛化能力

**评级：优秀**

证据：

- `ext_recursive_patch_nested_tuple` 通过
- `ext_duration_large_carry` 通过 - `_decimal` 函数使用 10^9 分块处理任意大数（第 95-102 行），是所有模型中唯一不依赖 Python int 自动转换的实现
- cross-path equality（第 44-47 行）
- `_nodes_equal` 异常安全（第 10-20 行）- 可处理 `__eq__` 抛异常的对象
- `_deadline` 溢出处理（第 40-44 行）- 可处理极端时间值

分析：泛化能力在已评价模型中**最强**。`_decimal` 的大数分块格式化、`_nodes_equal` 的异常安全比较、`_deadline` 的溢出处理，都是对极端输入形态的主动泛化。这些实现在当前 criterion 集合下未被直接测试，但在更极端的测试场景下会展现出优势。

### 维度 8：Token 效率

**评级：较差**

证据：

- 总 token：880,477（input 67,448 + output 33,765 + cache 779,264）
- 每 criterion 成本：55,030 tokens
- 每严格任务成本：220,119 tokens
- 22 次 LLM 请求（最多）
- 11 次 Bash 命令
- cache 占比：88.5%

分析：B07 以 880K tokens 取得 16/16 + 4/4 结果，token 成本最高（与 B06 的 863K 接近）。主要原因：

1. **LLM 请求最多**：22 次，超过 B04（21 次）和 B06（18 次）。每次请求的累积上下文导致 cache token 高达 779K。
2. **验证最密集**：9 次自定义测试 + 3 次额外测试 + 2 次 smoke = 14 次有效验证操作。但这些测试未发现任何缺陷。
3. **output 33,765**：与 B14（33,376）和 B06（33,799）接近。高 output 部分来自 `_decimal`、`_nodes_equal` 等额外函数的代码量。

正面：代码质量在已评价模型中**防御性最强**，包含多项独特的边界处理。无返工，首次写入即正确。

结论：B07 的 token 成本最高，但代码的防御性和泛化能力也最强。额外的 token 主要投入在更密集的验证和更防御性的代码编写上。如果评价标准包含对极端边界场景的鲁棒性，B07 的额外成本有合理性；在当前 criterion 集合下，这些额外防御未被检测到。

---

## B02 - deepseek/deepseek-v4-flash

**正式评分**：`B02.json` - Gate 通过、4/4 任务、16/16 criterion、2/2 extension、2/2 resource、900,470 tokens
**Audit**：passed=true, reasons=[], external_workspace_write=false, protected_files_ok=true, signature_ok=true
**执行轨迹**：25 次 LLM 请求（最多），8 次 Bash，0 次 Edit

### 维度 1：指令遵循

**评级：优秀**

audit 全项通过，ack 格式正确，仅修改 solution 文件 + ack，仅使用标准库。25 次 LLM 请求表明执行过程较长，但无范围外操作。

### 维度 2：核心业务语义

**评级：优秀**

16/16 criterion 全过。四模块核心逻辑正确：

- recursive_patch：嵌套 `_merge` 函数，deepcopy 隔离，`is delete` 身份判定，plain dict 递归合并
- dependency_layers：`_node_id` 模块级函数，hash bucket 注册，Kahn 算法迭代，`remaining` set 跟踪未处理节点，自环注释（第 46-48 行）
- ttl_set：`_validate_numeric` 含 `allow_zero`/`positive_only` 参数，过期 `now >= deadline`，淘汰 `popitem(last=False)`
- duration：`_UNITS` 元组列表含 (unit, rank, ms_factor)，`_PARSE_MAX` 用 `value > max_val` 表达边界（等价于 `value >= limit`），normalize divmod 逐级进位

### 维度 3：类型与边界精度

**评级：优秀**

`type(value) is bool` 拒绝 bool（ttl_set 第 9 行、第 23 行），`type(base) is not dict` 拒绝 dict 子类（recursive_patch 第 15 行），`math.isfinite` 拒绝 inf/nan，前导零检测，`ms` 优先匹配。`_PARSE_MAX` 使用 `>` 而非 `>=` 表达边界是等价但不同风格的写法。

### 维度 4：数据完整性与生命周期

**评级：优秀**

deepcopy 隔离，DELETE sentinel `__deepcopy__` 返回自身，重复 key 先删后加（ttl_set 第 55-60 行），过期项 `del`。

不足：`add` 方法中先检查 `if value in self._data`（第 55 行），再做线性扫描 `for k in list(self._data.keys()): if k == value`（第 57-60 行）-- 这两步是冗余的，`value in self._data` 已确认存在，线性扫描只是为了获取实际存储的 key 对象。但 `del self._data[value]` 可以直接用 value 删除，不需要先找到 k。这是一个不必要的复杂化。

### 维度 5：确定性与协议纪律

**评级：优秀**

`ready.sort()`（第 66 行）和 `ready.sort()`（第 81 行）显式排序层内节点。一次性消费 edges。`_OUTPUT_KEYS` 固定顺序。DurationParseError `.code`/`.position` 稳定。

### 维度 6：可扩展性与性能

**评级：良好**

迭代 Kahn 算法，hash bucket 快速路径，`_purge` O(n) 全量扫描。`remaining` set 的 `discard` 操作 O(1)。

### 维度 7：泛化能力

**评级：良好**

两个 extension 通过。支持 unhashable 节点（线性扫描回退）。normalize 数学分解可处理大数。

不足：无 cross-path equality 检查（B14/B06/B07 有）。normalize 中 `carrier` 列表（第 120-126 行）是**死代码**--定义后未使用，实际进位由 divmod 完成。这是代码质量问题，不影响正确性但表明模型在编写时有过未完成的重构。

### 维度 8：Token 效率

**评级：较差**

- 总 token：900,470（input 25,501 + output 24,537 + cache 850,432）- **最高**
- 每 criterion：56,279 tokens
- 25 次 LLM 请求（最多）
- cache 占比：94.4%

B02 以 900K tokens 取得 16/16，token 成本为所有模型中最高。25 次 LLM 请求导致 cache 累积至 850K。output 24,537 适中。代码质量中上，但存在死代码和冗余逻辑（`carrier` 未使用、`add` 方法冗余扫描）。无返工但验证投入较多。

---

## B05 - stepfun/step-3.7-flash

**正式评分**：`B05.json` - Gate 通过、4/4 任务、16/16 criterion、2/2 extension、**1/2 resource**（`res_dependency_20000` timeout）、326,756 tokens
**Audit**：passed=true, reasons=[], external_workspace_write=false, protected_files_ok=true, signature_ok=true
**执行轨迹**：9 次 LLM 请求（最少），1 次 Bash（仅 smoke），0 次 Edit

### 维度 1：指令遵循

**评级：优秀**

audit 全项通过，ack 格式正确，仅修改 solution 文件 + ack，仅使用标准库。执行极为高效--9 次 LLM 请求和 1 次 smoke 测试即完成全部工作，无自定义测试。

### 维度 2：核心业务语义

**评级：优秀**

16/16 criterion 全过。四模块核心逻辑正确：

- recursive_patch：`type(base) is not dict` 校验（第 19 行），`is delete` 身份判定（第 25 行），plain dict 递归合并（第 27 行），deepcopy 隔离
- dependency_layers：节点注册含 hashable/unhashable 双路径（第 12-34 行），边去重（第 50-52 行），环检测（第 61-63 行）
- ttl_set：`_is_finite_number` 辅助函数（第 5-10 行），过期 `now >= deadline`（第 33 行），淘汰 `popitem(last=False)`（第 50 行），重复 key 先删后加（第 44-45 行）
- duration：`_parse_fields` 共享解析（第 21-59 行），`ord` 手动转换数字（第 36 行），normalize divmod 逐级分解

### 维度 3：类型与边界精度

**评级：良好**

`isinstance(capacity, bool)` 拒绝 bool（第 15 行），`_is_finite_number` 拒绝 bool/inf/nan（第 6-9 行），`type(base) is not dict` 拒绝 dict 子类，前导零检测，`ms` 优先匹配。

不足：ttl_set 对类型错误使用 `ValueError` 而非 `TypeError`（第 15-16 行 capacity、第 18 行 ttl、第 20 行 clock）。语义上，类型不匹配应抛 `TypeError`，值不合法应抛 `ValueError`。B05 将两者混用为 `ValueError`。`ttl_strict_types` criterion 通过说明当前检查可能不区分异常类型，但这是一个语义精度问题。

### 维度 4：数据完整性与生命周期

**评级：良好**

deepcopy 隔离，DELETE sentinel `__deepcopy__` 返回自身，重复 key 先 `del`，过期项 `del`。

不足：`_purge` 方法（第 27-36 行）内部调用 `self._clock()` 获取时间，但 `add`/`discard`/`__contains__`/`__len__` 也在调用 `_purge` 前先调用 `self._clock()`（如第 39 行）。这意味着 **clock 被调用两次**--一次在方法体中，一次在 `_purge` 中。如果 clock 是非单调的，两次调用可能返回不同值，导致 purge 和操作使用不一致的时间戳。`ttl_exact_expiry` 通过说明在测试场景下 clock 是单调的，但这是一个潜在的正确性隐患。

### 维度 5：确定性与协议纪律

**评级：良好**

`layer.sort()`（第 64 行）显式排序层内节点。`_KEYS` 固定顺序。DurationParseError `.code`/`.position` 稳定。

不足：`dependency_layers` 第 36 行 `edges_list = list(edges)` 将一次性 iterable 转为列表，然后遍历两次（第 38-40 和 47-54）。虽然技术上仍是一次性消费（转为列表后不再访问原始 iterable），但这将全部边物化到内存，对于超大 edge 流不够友好。

### 维度 6：可扩展性与性能

**评级：不合格**

**关键缺陷**：`res_dependency_20000` **timeout**。

根因分析（`dependency_layers.py` 第 57-69 行）：

```python
remaining = set(range(n))
while remaining:
    layer = [i for i in remaining if indegree[i] == 0]  # O(|remaining|)
    ...
    for i in layer:
        remaining.remove(i)
        for dep_id in dependents[i]:
            indegree[dep_id] -= 1
```

每次迭代扫描 **全部剩余节点** 查找 `indegree == 0` 的节点。对于 20,000 个节点的深链（每层 1 个节点），时间复杂度为 O(n²) = O(20000²) = 4 亿次比较，导致 timeout。

其他所有模型使用标准 Kahn 算法：维护 `current_layer` 列表，仅扫描当前层节点的 dependents，复杂度 O(n + e)。B05 的实现虽然逻辑正确（结果等价），但算法复杂度从 O(n+e) 退化为 O(n²)。

`res_ttl_expiry_sweep` 通过 - ttl_set 的 `_purge` 全量扫描在测试规模内可接受。

### 维度 7：泛化能力

**评级：良好**

`ext_recursive_patch_nested_tuple` 通过，`ext_duration_large_carry` 通过。`_parse_fields` 共享解析，normalize 不检查 range（正确接受超范围从属字段）。支持 unhashable 节点。

不足：无 cross-path equality 检查。`get_id` 函数在通过线性扫描找到 unhashable 节点后，尝试将其加入 `node_to_id` 字典（第 22-24 行），但 `hash(obj)` 会再次抛出 `TypeError`，所以这个 try-except 是无效的--unhashable 对象永远不会被加入 hash 字典。这段代码虽然不影响正确性，但逻辑冗余。

### 维度 8：Token 效率

**评级：优秀**

- 总 token：326,756（input 41,651 + output 18,289 + cache 266,816）- **最低**
- 每 criterion：20,422 tokens - **最低**
- 9 次 LLM 请求（最少）
- 1 次 Bash（最少，仅 smoke 测试）
- cache 占比：81.6%

B05 以 326K tokens 取得 16/16 + 2/2 extension + 1/2 resource，token 效率为所有模型中**最优**。执行极为精炼--无自定义测试、无返工、无冗余探索。output 18,289 适中。

但低 token 的代价是**缺乏验证**--仅运行了 1 次 smoke 测试，未发现 O(n²) 算法缺陷。如果运行 1 次 20,000 节点的性能测试（如 B08/B14/B07 所做），timeout 问题可能在提交前被发现并修复。这是一个"低 token 以低验证为代价"的典型案例。

---

## B03 - deepseek/deepseek-v4-pro

**正式评分**：`B03.json` - Gate 通过、4/4 任务、16/16 criterion、2/2 extension、**1/2 resource**（`res_dependency_20000` timeout）、1,407,484 tokens
**Audit**：passed=true, reasons=[], external_workspace_write=false, protected_files_ok=true, signature_ok=true
**执行轨迹**：31 次 LLM 请求（最多），10 次 Bash，6 次 Write，2 次 Edit

### 维度 1：指令遵循

**评级：优秀**

audit 全项通过，ack 格式正确，仅修改 solution 文件 + ack，仅使用标准库。31 次 LLM 请求和 2 次 Edit 表明执行过程最为曲折，但最终交付正确。

### 维度 2：核心业务语义

**评级：优秀**

16/16 criterion 全过。四模块核心逻辑正确，且有独特设计：

- recursive_patch：标准实现，含详细 docstring（第 17-22 行）
- dependency_layers：`_register` 函数含 hashable 快速路径 + 线性扫描回退（第 25-50 行），Kahn 算法迭代（第 73-85 行）
- ttl_set：**使用 list 而非 OrderedDict**（`self._entries = []`，第 41 行），**双向等值检查** `v == value or value == v`（第 59、71、78 行）- 处理非对称 `__eq__`
- duration：`_scan` 共享解析，`ord` 手动转换（第 53 行），normalize 模运算分解

### 维度 3：类型与边界精度

**评级：良好**

`isinstance(value, bool)` 拒绝 bool，`math.isfinite` 拒绝 inf/nan，`type(base) is not dict` 拒绝 dict 子类，前导零检测，`ms` 优先匹配。

不足：capacity 验证顺序不佳--先调用 `_check_num(capacity, "capacity")`（第 25 行）检查 bool/int/float，再检查 `isinstance(capacity, int)`（第 28 行）。如果传入 `capacity=3.5`，`_check_num` 通过（float 合法），然后 `isinstance(capacity, int)` 失败抛 TypeError。逻辑正确但验证顺序冗余。双向等值检查 `v == value or value == v` 是额外的类型精度保护。

### 维度 4：数据完整性与生命周期

**评级：良好**

deepcopy 隔离，DELETE sentinel `__deepcopy__` 返回自身，重复 key 先删除（第 58-61 行），过期项通过列表重建删除（第 51 行）。

不足：`add` 方法调用 `_purge()`（第 54 行，内部调用 `_now()`），然后再调用 `_now()`（第 62 行）--clock 被调用两次，与 B05 相同的问题。双向等值检查在 `add` 中只检查第一个匹配项即 break（第 61 行），如果存在多个等值项（理论上不应该），后续项不会被清除。

### 维度 5：确定性与协议纪律

**评级：优秀**

`queue.sort()`（第 74 行）显式排序层内节点。一次性消费 edges（第 53 行）。`_KEY_ORDER` 固定顺序。DurationParseError `.code`/`.position` 稳定。

### 维度 6：可扩展性与性能

**评级：不合格**

**关键缺陷**：`res_dependency_20000` **timeout**。

根因分析（`dependency_layers.py` 第 25-50 行）：

```python
def _register(value):
    try:
        hash(value)
        if value in hash_map:
            return hash_map[value]
    except TypeError:
        pass
    # Falls through to linear scan for ALL new hashable nodes!
    for i, existing in enumerate(all_nodes):
        if existing == value:
            return i
    ...
```

对于不在 `hash_map` 中的新 hashable 节点，`hash(value)` 成功且 `value in hash_map` 返回 False，try 块正常结束（无 return），执行**落入线性扫描**。这意味着每个新节点都扫描全部已注册节点。

对于 20,000 个唯一节点：O(n²) = ~200M 次比较，导致 timeout。

与 B05 不同，B05 的 O(n²) 在 Kahn 循环中，B03 的 O(n²) 在注册阶段。两者都导致相同结果。

ttl_set 使用 list 而非 OrderedDict，所有操作（add/discard/contains/evict）均为 O(n)，但 `res_ttl_expiry_sweep` 在测试规模内通过。

### 维度 7：泛化能力

**评级：良好**

两个 extension 通过。`_scan` 共享解析，normalize 不检查 range。支持 unhashable 节点。

双向等值检查 `v == value or value == v` 是独特的泛化--可处理非对称 `__eq__` 实现。但无 cross-path equality 检查。

### 维度 8：Token 效率

**评级：极差**

- 总 token：1,407,484（input 28,601 + output 36,419 + cache 1,342,464）- **最高，唯一超过 100 万**
- 每 criterion：87,968 tokens - **最高**
- 31 次 LLM 请求（最多）
- 2 次 Edit（有返工）
- cache 占比：95.4%

B03 以 1.4M tokens 取得 16/16 + 2/2 extension + 1/2 resource，token 成本为所有模型中**最高**，且结果不如满分模型。31 次 LLM 请求导致 cache 累积至 1.34M。2 次 Edit 表明代码首次写入有缺陷需要修正。

这是 token 效率与结果质量**同时最差**的案例--最高成本 + 最低 resource 通过率。代码质量在功能正确性上达标，但在性能（O(n²) 注册）和数据结构选择（list vs OrderedDict）上存在根本缺陷。

---

## B09 - volcano/doubao-seed-2.0-pro

**正式评分**：`B09.json` - Gate 通过、3/4 严格任务、**15/16 criterion**（`ttl_strict_types` fail）、2/2 extension、2/2 resource、276,081 tokens
**Audit**：passed=true, reasons=[], external_workspace_write=false, protected_files_ok=true, signature_ok=true
**执行轨迹**：10 次 LLM 请求，1 次 Bash（仅 smoke），0 次 Edit

### 维度 1：指令遵循

**评级：优秀**

audit 全项通过，ack 格式正确，仅修改 solution 文件 + ack，仅使用标准库。执行极为高效--10 次 LLM 和 1 次 smoke 测试。

### 维度 2：核心业务语义

**评级：良好**

15/16 criterion，`ttl_strict_types` 失败。四模块核心逻辑基本正确：

- recursive_patch：标准实现，`is delete` 身份判定，plain dict 递归合并
- dependency_layers：hash bucket 注册（第 13、22-26 行），Kahn 算法迭代，`current_layer.sort()` 显式排序
- ttl_set：`_purge` 返回 `now`（第 43 行）避免重复调用 clock -- 独特设计，优于 B05/B03 的双调用模式
- duration：`_UNITS` 逆序列表（ms 在前，第 11-17 行），`_UNIT_RANKS` 动态计算（第 18 行），normalize 使用 reversed 迭代 + 除法（第 109-113 行）

### 维度 3：类型与边界精度

**评级：不合格**

**关键缺陷**：`ttl_strict_types` 失败，`error_type: AssertionError`，`message: "expected TypeError or ValueError"`。

根因（`ttl_set.py` 第 6-16 行）：

```python
def _validate_numeric(value, name, allow_zero=True, non_negative=True):
    if type(value) is bool:
        raise TypeError(...)
    if not isinstance(value, (int, float)):
        raise TypeError(...)
```

`_validate_numeric` 接受 float 作为 capacity，但 TASKS.md 明确要求 "capacity must be an int but not bool"。B09 调用 `_validate_numeric(capacity, "capacity", allow_zero=False)`（第 21 行），未额外检查 `isinstance(capacity, int)`。因此 `BoundedTTLSet(3.5, 5, lambda: 0)` 不会抛出异常，违反了 capacity 必须为 int 的约束。

其他类型检查正确：bool 拒绝、`math.isfinite` 拒绝 inf/nan、`type(base) is not dict` 拒绝 dict 子类、前导零检测、`ms` 优先匹配。

### 维度 4：数据完整性与生命周期

**评级：优秀**

deepcopy 隔离，DELETE sentinel `__deepcopy__` 返回自身，重复 key 先 `del`（第 48-49 行），过期项 `del`（第 40-41 行）。`_purge` 返回 `now` 供调用方复用，避免 clock 双调用--这是比大多数模型更优雅的设计。

### 维度 5：确定性与协议纪律

**评级：优秀**

`current_layer.sort()`（第 56 行）显式排序。一次性消费 edges。`_OUTPUT_KEYS` 固定顺序。DurationParseError `.code`/`.position` 稳定。

### 维度 6：可扩展性与性能

**评级：良好**

`res_dependency_20000` 通过 - hash bucket O(1) 注册 + Kahn 算法 O(n+e)。`res_ttl_expiry_sweep` 通过 - `_purge` O(n) 全量扫描。`_purge` 返回 `now` 避免重复 clock 调用，性能优于 B05/B03。

### 维度 7：泛化能力

**评级：良好**

两个 extension 通过。`_scan_fields` 共享解析，normalize 不检查 range。支持 unhashable 节点。无 cross-path equality。

### 维度 8：Token 效率

**评级：优秀**

- 总 token：276,081（input 58,782 + output 3,859 + cache 213,440）- **最低**
- 每 criterion：18,405 tokens - **最低**
- 10 次 LLM 请求
- 1 次 Bash（仅 smoke）
- output 3,859 - **最低**

B09 以 276K tokens 取得 15/16 + 2/2 extension + 2/2 resource，token 效率为所有模型中**最优**（比 B05 的 327K 更低，且 resource 全过）。output 仅 3,859 表明代码产出极为精炼。无返工、无自定义测试。

但低 token 的代价与 B05 相同--仅 1 次 smoke 测试，未发现 `ttl_strict_types` 的类型验证缺陷。如果运行 1 次类型边界测试，capacity=float 的问题可能被发现。

---

## B10 - volcano/doubao-seed-2.0-code

**正式评分**：`B10.json` - Gate 通过、3/4 严格任务、**15/16 criterion**（`dl_deep_iterative` timeout）、2/2 extension、**1/2 resource**（`res_dependency_20000` timeout）、**ExecutionCompleted: 0**、469,287 tokens
**Audit**：passed=true, reasons=[], external_workspace_write=false, protected_files_ok=true, signature_ok=true
**执行轨迹**：16 次 LLM 请求，3 次 Bash，0 次 Edit

### 维度 1：指令遵循

**评级：良好**

audit 全项通过，ack 格式正确，仅修改 solution 文件 + ack，仅使用标准库。但 `ExecutionCompleted: 0` 表明执行未在时间限制内完成--`dl_deep_iterative` 和 `res_dependency_20000` 均 timeout。

### 维度 2：核心业务语义

**评级：良好**

15/16 criterion，`dl_deep_iterative` timeout。四模块核心逻辑基本正确：

- recursive_patch：`__slots__` sentinel，`is delete` 身份判定，plain dict 递归合并
- dependency_layers：hashable/unhashable 双路径注册（第 20-67 行），Kahn 算法迭代（第 76-107 行）
- ttl_set：`type(capacity) is bool or not isinstance(capacity, int)` 正确验证 int（第 18 行），`_get_now` + `_purge` 分离调用
- duration：`_scan` 共享解析，`parse_duration` 使用 if-elif 链（第 86-104 行），normalize divmod 分解

### 维度 3：类型与边界精度

**评级：优秀**

`ttl_strict_types` 通过 - 第 18 行 `type(capacity) is bool or not isinstance(capacity, int) or capacity <= 0` 正确拒绝 bool/float/非正 int。`_check_numeric` 拒绝 bool/inf/nan。`type(base) is not dict` 拒绝 dict 子类。前导零检测。`ms` 优先匹配。

### 维度 4：数据完整性与生命周期

**评级：优秀**

deepcopy 隔离，`__slots__` sentinel `__deepcopy__` 返回自身，重复 key 先 `del`（第 50-51 行），过期项 `del`（第 37-38 行）。`_get_now` 单独调用 clock 后传给 `_purge`，避免双调用。

### 维度 5：确定性与协议纪律

**评级：良好**

`current_layer_ids` 按 `range(total_nodes)` 顺序收集（第 83-86 行），即按 id 顺序。一次性消费 edges。`_OUTPUT_KEYS` 固定顺序。DurationParseError `.code`/`.position` 稳定。

不足：层内排序依赖 `range()` 遍历顺序而非显式 `sort()`，虽然结果等价（id 按首次出现分配），但缺少显式排序保证。

### 维度 6：可扩展性与性能

**评级：不合格**

**关键缺陷**：`dl_deep_iterative` 和 `res_dependency_20000` 均 **timeout**。

根因（`dependency_layers.py` 第 81-99 行）：

```python
while True:
    current_layer_ids = [
        node_id for node_id in range(total_nodes)
        if indegree.get(node_id, 0) == 0
    ]
    if not current_layer_ids:
        break
    ...
    for node_id in current_layer_ids:
        ...
        indegree[node_id] = -1  # mark as processed
```

每次迭代扫描 **全部 total_nodes** 查找 `indegree == 0` 的节点，已处理节点通过 `indegree = -1` 标记跳过。对于 20,000 节点的深链，每层 1 个节点，共 20,000 次迭代 × 20,000 次扫描 = 4 亿次比较，导致 timeout。

与 B05 相同的 O(n²) 模式，但实现方式不同（B05 用 `remaining` set，B10 用 `indegree = -1` 标记）。

### 维度 7：泛化能力

**评级：良好**

两个 extension 通过。支持 unhashable 节点。normalize 不检查 range。无 cross-path equality。

### 维度 8：Token 效率

**评级：良好**

- 总 token：469,287（input 79,073 + output 5,942 + cache 384,272）
- 每 criterion：31,286 tokens
- 16 次 LLM 请求，3 次 Bash
- output 5,942 极低

B10 以 469K tokens 取得 15/16 + 2/2 extension + 1/2 resource。token 效率中等偏低。output 极低（5,942）表明代码产出精炼。无返工。但两个 timeout 导致 `ExecutionCompleted: 0`，实际有效产出低于名义值。

不足：使用 public 属性（`self.capacity`、`self.ttl`、`self.clock`，第 26-28 行）而非 private（`_capacity` 等），违反了封装惯例。

---

## B11 - volcano/minimax-m3

**正式评分**：`B11.json` - Gate 通过、3/4 严格任务、**12/16 criterion**（dependency_layers 全部 4 项失败）、2/2 extension、**1/2 resource**（`res_dependency_20000` fail）、647,000 tokens
**Audit**：passed=true, reasons=[], external_workspace_write=false, protected_files_ok=true, signature_ok=true
**执行轨迹**：31 次 LLM 请求（从 usage record 推断），3 次 Bash

### 维度 1：指令遵循

**评级：优秀**

audit 全项通过，ack 格式正确，仅修改 solution 文件 + ack，仅使用标准库。

### 维度 2：核心业务语义

**评级：不合格**

**关键缺陷**：dependency_layers 任务 0/4 criterion 全部失败。

根因分析（`dependency_layers.py` 第 96-113 行 Kahn's 算法）：

```python
while remaining:
    current = [nid for nid in remaining if indegree[nid] == 0]
    ...
    for nid in remaining:           # 遍历 remaining，不是 current
        if indegree[nid] == 0:     # 检查时 indegree 可能已被修改
            for dep_id in dependents[nid]:
                indegree[dep_id] -= 1  # 递减依赖者的 indegree
        else:
            next_remaining.append(nid)  # 仅非零节点进入下一轮
```

**静默节点丢失 bug**：当 current 层节点的处理递减了 `remaining` 中后续节点的 indegree 至 0 时，该后续节点会进入 `if indegree[nid] == 0` 分支（递减其 dependents），但**不会被加入 `next_remaining`**（因为 `else` 分支被跳过），也**不在 `current` 中**（current 在循环开始前已计算）。该节点被静默丢弃--不出现在任何层中，也不进入下一轮。

示例：输入 `[("a", "b"), ("c", "b")]`，期望 `[["b"], ["a", "c"]]`，实际 `[["b"], ["a"]]`（"c" 被丢弃）。

其他三个模块（recursive_patch、ttl_set、duration）核心逻辑正确，12/12 criterion 全过。

### 维度 3：类型与边界精度

**评级：优秀**

`ttl_strict_types` 通过 - `isinstance(capacity, bool) or not isinstance(capacity, int)` 正确拒绝 bool/float（第 23 行）。`_validate_finite_number` 分别处理 int/float，拒绝 bool/inf/nan（第 7-17 行）。`type(base) is not dict` 拒绝 dict 子类。前导零检测。`ms` 优先匹配使用字符比较 `text[i] == "m" and text[i + 1] == "s"`（第 71 行）。

### 维度 4：数据完整性与生命周期

**评级：优秀**

deepcopy 隔离，`__slots__` sentinel `__deepcopy__` 返回自身，重复 key 先 `del`（第 65-66 行），过期项 `del`（第 56-57 行）。`__init__` 中主动调用 `_purge()` 初始化状态（第 44 行）--独特的早期清理设计。

### 维度 5：确定性与协议纪律

**评级：不合格**

dependency_layers 的 `current.sort()`（第 104 行）显式排序，但由于静默节点丢失 bug，输出不完整且不确定--哪些节点被丢弃取决于它们在 `remaining` 中的位置和 indegree 被递减的时机。

其他模块确定性正常：`_OUTPUT_KEYS` 固定顺序，DurationParseError `.code`/`.position` 稳定。

### 维度 6：可扩展性与性能

**评级：不合格**

`res_dependency_20000` 失败（AssertionError，非 timeout）-- 由于静默节点丢失 bug，大规模输入会产生错误结果。

此外，Kahn's 算法使用 `remaining` 列表扫描（第 97 行 `[nid for nid in remaining if indegree[nid] == 0]`），每次迭代扫描全部剩余节点，O(n²)。

### 维度 7：泛化能力

**评级：不合格**

`ext_recursive_patch_nested_tuple` 和 `ext_duration_large_carry` 通过（其他模块泛化正确）。但 dependency_layers 的静默节点丢失 bug 意味着该模块无法正确处理任何多节点共享依赖的输入--这是最基本的泛化场景。

### 维度 8：Token 效率

**评级：一般**

- 总 token：647,000（input 27,482 + output 28,881 + cache 590,637）
- 每 criterion：53,917 tokens
- 31 条 usage record

B11 以 647K tokens 取得 12/16 + 2/2 extension + 1/2 resource。token 成本中等偏高，且结果质量最差（12/16）。代码质量在其他三个模块上很高（优秀 docstring、`__slots__`、`_format_components` 辅助函数、类型注释），但 dependency_layers 的算法缺陷导致整个模块失效。

正面：recursive_patch、ttl_set、duration 三个模块的代码质量优秀，docstring 详细解释了设计决策。`_validate_finite_number` 的分类型处理（int/float 分别验证）是清晰的验证设计。

---

## B12 - mimo/mimo-v2.5-pro

**正式评分**：`B12.json` - **Gate 不通过**（`.pytest_cache` audit 失败）、4/4 raw 任务（3 个 all_passed_raw=true，1 个 false）、**15/16 raw criterion**（`ttl_strict_types` fail）、2/2 extension、2/2 resource、**严格榜全归零**（OfficialCriterionCount=0, StrictTaskCount=0）、190,320 tokens
**Audit**：**passed=false**，`reasons: ["unexpected workspace entry .pytest_cache"]`，external_workspace_write=false, protected_files_ok=true, signature_ok=true
**执行轨迹**：6 次 LLM 请求（最少！），1 次 Bash，0 次 Edit

### 维度 1：指令遵循

**评级：不合格**

**关键缺陷**：audit 失败，原因 `unexpected workspace entry .pytest_cache`。

根因：B12 运行了 `python -B -m pytest public_smoke_tests.py -v`（events Bash 命令），而非 `python -B public_smoke_tests.py`。`-m pytest` 调用会在工作目录创建 `.pytest_cache/` 目录，这违反了 TASKS.md "changed only solution files" 的约束。

虽然模型使用了 `-B` 标志（禁用 `.pyc` 字节码），但未意识到 pytest 自身会创建缓存目录。这是一个细微但致命的指令遵循错误--严格榜全部归零，`InstructionComplianceRate: 0.0`。

其他指令遵循正确：ack 格式正确，仅修改 solution 文件 + ack（`.pytest_cache` 是工具产物，非主动创建的文件），仅使用标准库，公开对象名称未改动。

### 维度 2：核心业务语义

**评级：优秀**

15/16 raw criterion（`ttl_strict_types` 失败）。四模块核心逻辑正确：

- recursive_patch：`_merge` 辅助函数，详细 docstring（第 21-33 行），`is delete` 身份判定，plain dict 递归合并
- dependency_layers：**正确的 deque-based Kahn 算法**（第 107-125 行）- 使用 `deque` + `sorted(next_queue)`，O(n+e) 复杂度，无静默节点丢失
- ttl_set：`_validate_number` 验证，`_purge(now)` 接受参数避免双调用，重复 key 先删后加
- duration：`_raise` 辅助函数，`_check_ranges` 分离，normalize divmod 分解

### 维度 3：类型与边界精度

**评级：不合格**

**关键缺陷**：`ttl_strict_types` 失败，`message: "expected TypeError or ValueError"`。

根因（`ttl_set.py` 第 30 行）：

```python
_validate_number(capacity, allow_zero=False, label="capacity")
if isinstance(capacity, bool):
    raise TypeError("capacity must be int, not bool")
```

`_validate_number` 接受 float 作为 capacity（第 11 行 `isinstance(val, (int, float))`），但 TASKS.md 要求 "capacity must be an int but not bool"。`isinstance(capacity, bool)` 检查在 `_validate_number` 之后（第 31 行），但即使 bool 检查在前，float 仍然会被 `_validate_number` 接受。

与 B09 相同的缺陷：未验证 capacity 必须为 int（而非 float）。

其他类型检查正确：bool 拒绝、`math.isfinite` 拒绝 inf/nan、`type(base) is not dict` 拒绝 dict 子类、前导零检测、`ms` 优先匹配。

### 维度 4：数据完整性与生命周期

**评级：优秀**

deepcopy 隔离，DELETE sentinel `__deepcopy__` 返回自身（第 12-14 行），重复 key 先 `del`（第 62-63 行），过期项 `del`（第 53-54 行）。`_purge(now)` 接受 `now` 参数，由调用方通过 `_get_now()` 获取后传入，避免 clock 双调用。

### 维度 5：确定性与协议纪律

**评级：优秀**

dependency_layers 使用 `deque(sorted(next_queue))`（第 125 行）- 显式排序层内节点。`_OUT_KEYS` 固定顺序。DurationParseError `.code`/`.position` 稳定。一次性消费 edges（第 83 行 `edge_list = list(edges)`）。

### 维度 6：可扩展性与性能

**评级：优秀**

`res_dependency_20000` 通过 - **正确的 deque-based Kahn 算法**，O(n+e) 复杂度。`dl_deep_iterative` 通过。`res_ttl_expiry_sweep` 通过 - `_purge` O(n) 全量扫描。

B12 是少数在 dependency_layers 上使用正确 O(n+e) 算法且 resource 全过的模型。

### 维度 7：泛化能力

**评级：优秀**

两个 extension 通过。`_scan` 共享解析，`_check_ranges` 分离（normalize 不调用）。支持 unhashable 节点（第 67-76 行线性扫描回退）。normalize 使用 divmod 分解，可处理大数。

### 维度 8：Token 效率

**评级：优秀**

- 总 token：190,320（input 33,638 + output 4,618 + cache 152,064）- **最低！**
- 6 次 LLM 请求（最少！）
- 1 次 Bash
- output 4,618 - **最低！**

B12 以 190K tokens 取得 15/16 raw criterion + 2/2 extension + 2/2 resource，token 效率为所有模型中**最优**。比 B09（276K）低 31%，比 B13（342K）低 44%。6 次 LLM 请求和 4,618 output token 表明执行极为精炼。

**悲剧性**：B12 拥有所有模型中最优的 token 效率、正确的算法实现、2/2 resource 通过，但因 `.pytest_cache` 导致 audit 失败，严格榜全归零。如果使用 `python -B public_smoke_tests.py` 而非 `python -B -m pytest`，B12 将是严格榜前三的有力竞争者（15/16 + 2/2 + 2/2 + 190K tokens）。

如果同时修复 `ttl_strict_types`（capacity 验证为 int），B12 将是 16/16 + 2/2 + 2/2 + 190K tokens--**所有模型中性价比最高**。


## B15 - composer/composer-2.5（Cursor harness，有计划）

**正式评分**：`B15.json` - Gate 通过、4/4 严格任务、**16/16 criterion**、2/2 extension、1/2 resource（timeout）、Token N/A
**Audit**：passed=true, reasons=[], external_workspace_write=false, protected_files_ok=true, signature_ok=true
**Harness**：Cursor（无 events.jsonl，token 不可用）

### 维度 1：指令遵循

**评级：优秀**

audit 全项通过，ack 格式正确，仅修改 4 个 solution 文件 + ack，仅使用标准库。工作区无 `.pytest_cache` 或 `__pycache__`。计划文件被正确读取并遵循--B15 修复了 A15 的全部 3 个 criterion 失败。

### 维度 2：核心业务语义

**评级：优秀**

16/16 criterion 全过。四模块核心逻辑正确，且相比 A15 有显著提升：

**recursive_patch**（`recursive_patch.py`）：
- 使用 `copy.deepcopy`（第 20 行）替代 A15 的自写 `_deep_copy`--修复了 `ext_recursive_patch_nested_tuple`
- 添加 `type(base) is not dict` 校验（第 15-18 行）--修复了 `rp_plain_dict`
- `_DeleteSentinel` 含 `__deepcopy__` 返回自身（第 7-8 行）
- 标准 merge 逻辑：删除 `is delete`（第 23 行），plain dict 递归合并（第 25-30 行）

**dependency_layers**（`dependency_layers.py`）：
- `get_id` 函数线性扫描注册（第 17-25 行）--无 hash bucket，但逻辑正确
- Kahn 算法正确（第 42-54 行）：`current_layer` + `next_layer` + `sort()`，O(n+e) 的图遍历
- 边去重（第 31-33 行），环检测（第 56-58 行）

**ttl_set**（`ttl_set.py`）：
- `type(capacity) is not int or isinstance(capacity, bool)`（第 16 行）--正确拒绝 float 和 bool，修复了 `ttl_strict_types`
- `_purge(now)` 接受参数（第 36 行），`_now()` 获取后传入--避免 A15 的双 clock 调用
- `_remove_equal` 方法（第 41-45 行）用线性扫描删除等值 key

**duration**（`duration.py`）：
- 使用 `'0' <= text[position] <= '9'`（第 40 行）--正确 ASCII 检查，修复了 A15 的 `isdigit()` 问题
- `unit_start = position` 保存后用于 `order` 错误位置（第 52、65 行）--修复了 A15 的位置错误
- `_scan_fields(text, check_range)` 参数化设计（第 29 行）--与 B06 相同的优雅架构

### 维度 3：类型与边界精度

**评级：优秀**

`ttl_strict_types` 通过--`type(capacity) is not int` 拒绝 float，`isinstance(capacity, bool)` 拒绝 bool（第 16 行）。`_validate_finite_number` 用 `type(value) not in (int, float)` 拒绝非数值（第 8 行）。`type(base) is not dict` 拒绝 dict 子类。前导零检测（第 48-49 行）。`ms` 优先匹配 `text[position:position+2] == "ms"`（第 54 行）。ASCII 数字检查（第 40 行）。

B15 修复了 A15 的全部 3 个类型精度缺陷。

### 维度 4：数据完整性与生命周期

**评级：优秀**

`copy.deepcopy` 隔离（第 20 行），DELETE sentinel `__deepcopy__` 返回自身（第 7-8 行）。`_purge(now)` 接受参数避免双 clock 调用（第 36 行）--优于 A15 和 B05/B03。`_remove_equal` 确保旧 key 对象被删除（第 41-45 行），过期项 `del`（第 38-39 行）。

### 维度 5：确定性与协议纪律

**评级：优秀**

`current_layer.sort()`（第 39 行）和 `next_layer.sort()`（第 53 行）显式排序。一次性消费 edges（第 27 行）。`OUTPUT_KEYS` 固定顺序（第 3 行），result 按固定键构建（第 84 行）。`order` 错误指向 `unit_start`（第 65 行）--符合计划约定。DurationParseError `.code`/`.position` 稳定（第 21-26 行）。

### 维度 6：可扩展性与性能

**评级：不合格**

**`res_dependency_20000` timeout**。

根因（`dependency_layers.py` 第 17-25 行）：

```python
def get_id(obj):
    for index, existing in enumerate(nodes):
        if existing == obj:
            return index
    ...
```

`get_id` 对每个新节点做**全量线性扫描**，无 hash bucket 快速路径。对于 20,000 个唯一节点，注册阶段为 O(n²) = ~200M 次比较，导致 timeout。

与 B03 相同的根因（O(n²) 注册）。但 B15 的 Kahn 算法本身是正确的 O(n+e)--如果修复 `get_id` 添加 hash bucket，B15 可通过 resource 测试。

### 维度 7：泛化能力

**评级：优秀**

两个 extension 全过。`_scan_fields(text, check_range)` 参数化设计正确区分 parse/normalize。normalize 使用 total_ms + divmod 链（第 106-109 行），可处理大数。`_remove_equal` 的线性扫描支持等值但非同一对象的 key。

### 维度 8：Token 效率

**评级：待验证**

Token 数据不可用（Cursor harness 不产生 events.jsonl）。代码行数：recursive_patch 34 行、dependency_layers 60 行、ttl_set 71 行、duration 122 行，总计约 287 行。B15 的代码量与 A15 接近，但质量显著更高。无法与其他模型进行 token 效率对比。

### A15 -> B15 计划收益分析

| 维度 | A15 | B15 | 变化 |
|------|-----|-----|------|
| 指令遵循 | 优秀 | 优秀 | 不变 |
| 核心业务语义 | 良好 | 优秀 | **提升** |
| 类型与边界精度 | 不合格 | 优秀 | **显著提升** |
| 数据完整性 | 良好 | 优秀 | **提升** |
| 确定性 | 良好 | 优秀 | **提升** |
| 可扩展性 | 不合格 | 不合格 | 不变（不同根因） |
| 泛化能力 | 良好 | 优秀 | **提升** |
| Token 效率 | 待验证 | 待验证 | 不可比 |

计划文件帮助 Composer 2.5 修复了：
1. `rp_plain_dict`：添加 `type(base) is not dict` 校验
2. `ttl_strict_types`：用 `type(capacity) is not int` 拒绝 float
3. `ext_recursive_patch_nested_tuple`：改用 `copy.deepcopy`
4. `du_strict_syntax`：改用 ASCII 范围检查替代 `isdigit()`
5. `order` 错误位置：改指向 unit_start 而非 digit_start
6. clock 双调用：改为 `_purge(now)` 参数传递

唯一未修复的是 `res_dependency_20000`--A15 的 O(n²) 在 Kahn 循环，B15 的 O(n²) 在注册阶段。两者都需要添加 hash bucket 才能解决。

---

## 补充分析：B12 与 B06 的性价比对照（严格榜 gate 一票否决制的盲区）

> 本节是对 B12 `mimo/mimo-v2.5-pro` 与 B06 `qwen/qwen3.8-max-preview` 的横向对照，用于揭示严格榜 InstructionGate 一票否决制可能埋没高性价比候选的风险。结论不改变原榜单排名，仅作为评价方法论的补充视角。

### 核心对照表

| 指标 | B06 qwen3.8-max | B12 mimo-v2.5-pro |
|---|---|---|
| 严格榜 criterion | 16/16 | 0（gate 清零） |
| raw criterion | 16/16 | 15/16 |
| resource capability | 2/2 | 2/2 |
| extension capability | 2/2 | 2/2 |
| 总 token | 863,422 | **190,320**（低 78%） |
| dependency_layers 算法 | Kahn O(n+e) | **deque-based Kahn O(n+e)**（正确） |
| InstructionGate | PASS | **FAIL**（`.pytest_cache`） |
| LLM 请求数 | 18 | **6**（最少） |
| output token | 33,799 | **4,618**（最低） |

### 关键判断：B12 用 B06 22% 的 token 达到其 94% 的 raw 完成度

B12 在 raw criterion（15/16 vs 16/16）、resource（2/2 vs 2/2）、extension（2/2 vs 2/2）、算法正确性（均为 O(n+e) Kahn）四个维度上与 B06 几乎等价，但 token 成本仅为 B06 的 22%。两者的唯一实质差异是：

1. **B12 的两个缺陷都是"可修复的工程问题"**：
   - `.pytest_cache`：源于运行 `python -B -m pytest` 而非 `python -B public_smoke_tests.py`，是命令选择错误，与代码能力无关。
   - `ttl_strict_types`：capacity 未验证为 int（接受 float），是一行类型检查缺失。
   
   两者都不反映模型的算法理解或业务语义能力缺陷。

2. **B06 的高 token 是"模型行为特性"**：
   - 18 次 LLM 请求、7 次自定义测试、33,799 output token 构成了高成本主体。
   - cache 占比 96.1%（829K）部分源于 provider 缓存策略，但即便排除 cache，input+output 仍达 33,907，是 B12（38,256）的 89%。
   - 高 token 换来了代码质量提升（cross-path equality、`check_range` 参数化、高质量注释），但这些质量在当前 criterion 集合下未被额外检测--16/16 与 B12 的 15/16 仅差 1 个 capacity 检查。

### 评价方法论启示：严格榜 gate 一票否决制的盲区

严格榜的 `InstructionGate` 设计将任何指令违规清零，这保证了"指令遵循"的硬性门槛，但也带来一个副作用：**一个在代码能力、算法正确性、token 效率、resource 通过率上全面优秀的候选，会因一个与代码能力无关的工程失误（如 `.pytest_cache`）被完全排除出排名**。

B12 是典型案例：

- **严格榜视角**：B12 排名末位（OfficialCriterionCount=0），看似"什么都没做对"。
- **宽松榜视角**：B12 raw 15/16、2/2 extension、2/2 resource、190K token，是性价比最优的候选之一。
- **维度评估视角**：B12 在指令遵循外的 7 个维度（业务语义、类型精度、数据完整性、确定性、可扩展性、泛化、token 效率）均达到优秀或良好，dependency_layers 使用了正确的 deque-based O(n+e) 算法（与 B06 等价，优于 B05/B03/B10 的 O(n²)）。

### 如果 B12 修复两个缺陷的假想结果

| 指标 | 当前 | 修复后（假想） |
|---|---|---|
| InstructionGate | FAIL（`.pytest_cache`） | PASS |
| raw criterion | 15/16（`ttl_strict_types` fail） | 16/16 |
| 严格榜排名 | 末位（清零） | **前三竞争者**（16/16 + 2/2 + 2/2 + 190K token） |
| 每 criterion token | 严格榜清零无法计算 | **11,895 tokens**（全场最低，B06 为 53,964） |

修复后 B12 的每 criterion token（11,895）将是 B06（53,964）的 22%、B13（21,384）的 56%。在"相同正确性下成本最低"的效率视角下，B12 修复后将显著领先所有满分模型。

### 工程选型启示

如果将这两个模型视为工程候选：

- **选 B06（qwen）**：当代码质量、可维护性、防御性实现是首要考量，且 token 成本不敏感时。B06 的 cross-path equality、`check_range` 参数化、高质量注释在长期维护中有价值。
- **选 B12（mimo-v2.5-pro）**：当 token 效率是首要考量，且能在工程流程中规避 `.pytest_cache` 问题（如改用 `python -B public_smoke_tests.py` 或在 CI 中清理缓存）时。B12 的缺陷是流程可控的，而其性价比优势是模型固有的。

**核心结论**：B12 的 case 表明，严格榜排名不应作为工程选型的唯一依据。一个因工程失误被 gate 清零的候选，可能在代码能力与性价比上全面优于严格榜排名靠前的候选。宽松榜与维度评估提供了严格榜缺失的视角，三者结合才能形成对模型能力的完整判断。

---

