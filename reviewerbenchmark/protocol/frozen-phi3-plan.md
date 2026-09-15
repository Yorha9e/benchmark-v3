# Short benchmark 共享实施计划

## 范围与执行顺序

- 只使用 Python 标准库，不重命名任何公开对象，也不新增跨模块 helper 文件。
- 四个模块没有公开依赖，按 `TASKS.md` 顺序实施：
  1. `solutions/recursive_patch.py`
  2. `solutions/dependency_layers.py`
  3. `solutions/ttl_set.py`
  4. `solutions/duration.py`
- 每完成一个模块，立即用 `python -B` 运行该模块的内联断言；最后再做一次四模块联合 smoke。`-B` 可避免 smoke 生成 `.pyc`。
- 全部通过后，在 `solutions/` 同级创建契约要求的 `instruction_ack.json`。除四个 solution 文件和该确认文件外，不改动其他文件。

## 1. `recursive_patch.py`

### 实现方案

- 用私有哨兵类型创建唯一模块级 `DELETE`；可让其 `__deepcopy__` 返回自身，避免复制嵌套数据时制造第二个公共哨兵。
- `apply_patch` 入口用 `type(base) is dict` 和 `type(patch) is dict` 校验，明确拒绝 `dict` 子类；`delete` 保留可替换的默认参数语义。
- 使用 `copy.deepcopy` 切断所有内置可变容器与输入的别名。先复制完整 `base`，自然保留未修改键及其顺序，再按 `patch.items()` 的遇见顺序处理每一项：
  1. 先在复制前检查当前层的 `patch_value is delete`；是则删除已有键，不存在则无操作。
  2. 仅当对应旧值和补丁值都满足 `type(value) is dict` 时递归合并。
  3. 其余情况整值替换为补丁值的深复制；已有键赋值不移动位置，真正的新键才在末尾追加。
- 新增键也应深复制；递归分支沿用同一顺序规则。若发生整值替换，替换字典自身按补丁对象的原顺序复制，不保留被替换旧字典的内部顺序。

### 关键边界

- 删除只看身份，不看相等性；自定义 `delete` 只替代本次调用的删除哨兵。
- 只有“当前层补丁映射的直接值”可触发删除；列表或其他容器内部出现哨兵只是普通数据。
- 嵌套 `dict` 子类不递归合并，而是整个深复制后替换。
- 未被补丁触及的 base 分支也必须脱离 base；来自 patch 的替换分支必须脱离 patch；两个输入始终不变。

### 公开 smoke

1. 用 `base={"a":{"x":1,"y":2},"b":0}`、`patch={"a":{"y":3,"z":4},"c":5}`，断言顶层键序为 `a,b,c`，`a` 内键序为 `x,y,z`。
2. 覆盖“删除已有键、删除不存在键、自定义哨兵、与哨兵相等但非同一对象不删除”。
3. 覆盖“旧值非 plain dict”和“补丁值为 `dict` 子类”时均整值替换，并断言顶层 `dict` 子类输入被拒绝。
4. 在 base 未修改分支及 patch 替换分支中放入嵌套 `list`/`set`；分别修改结果和输入，断言双方互不影响且原输入内容未被调用改变。

## 2. `dependency_layers.py`

### 实现方案

- `DependencyCycleError` 继承 `ValueError`，构造时始终把传入节点固化为 `.nodes` tuple，并生成确定性消息。
- 只用一个 `for node, dependency in edges` 消费原迭代器；在每对数据中按 `node`、再 `dependency` 的顺序登记首次出现。
- 不直接依赖节点对象可哈希：为每个首次出现的等值节点分配递增整数 ID，并在 `nodes[id]` 中保留第一次出现的实际对象。可为可哈希对象维护 hash bucket 快速路径，但不可哈希对象要回退等值查找；跨路径仍要确认等值，不能把“可哈希/不可哈希”误当成不同节点。
- 图结构使用：
  - `indegree[id]`：去重后的依赖数；
  - `dependents[dependency_id]`：依赖该节点的节点 ID 列表；
  - `seen_edges`：整数 ID 对集合，用于重复边去重。
- 使用非递归、按层的 Kahn 算法：初始层是全部入度为零的 ID；处理完整层后再形成下一层。每层按整数 ID（即首次出现序）排序，不能依赖 set 或邻接遍历的偶然顺序。
- 若处理数少于节点总数，剩余 `indegree > 0` 的 ID 必然由真实环造成；按 ID 顺序构造异常 `.nodes`。这里应包含环内节点以及被环阻塞、无法剥离的下游节点，而不只是环成员。

### 关键边界

- 空流返回 `[]`；只出现在 dependency 位置的节点也必须出现在结果中。
- 重复边只贡献一次入度；自环是真实环。
- 多个独立子图共享同一全局首次出现顺序。
- 深链全程使用循环，不能使用递归 DFS。

### 公开 smoke

1. 用会记录 `__iter__` 调用次数的一次性 iterable，断言只迭代一次；空 iterable 返回 `[]`。
2. 输入 `[("a","b"),("a","b"),("c","b")]`，断言结果为 `[["b"],["a","c"]]`，同时覆盖重复边和 dependency-only 节点。
3. 输入 `[("c","a"),("d","b")]`，断言结果为 `[["a","b"],["c","d"]]`；另用列表节点做一次小例，确认实现不强制节点可哈希。
4. 输入 `[("a","b"),("b","a"),("c","a"),("d","e")]`，断言异常 `.nodes == ("a","b","c")`，且可剥离的 `d/e` 不在其中。
5. 构造长度超过 Python 递归上限的整数深链，断言成功得到单节点层序列且没有 `RecursionError`。

## 3. `ttl_set.py`

### 实现方案

- 构造器统一校验：
  - `capacity` 是 `int` 且不是 `bool`，并且 `> 0`；
  - `ttl` 是有限 `int`/`float` 且不是 `bool`，并且 `>= 0`；
  - `clock` 可调用。
- 用一个数值校验 helper 复用上述规则：拒绝非 `int`/`float`、`bool`、`NaN` 和正负无穷；每次调用 `clock()` 后都重新校验其返回值。整数本身视为有限，浮点数用 `math.isfinite`。
- 以 `collections.OrderedDict` 保存“实际 key 对象 -> deadline”，字典顺序就是活跃插入顺序。不要使用带惰性旧条目的 heap、历史队列或 tombstone，以免保留陈旧对象引用。
- 每个公开操作 `add`、`discard`、`__contains__`、`__len__` 开始时读取并校验一次当前时间，再用同一 `now` 清理所有满足 `now >= deadline` 的条目。不能假设 clock 单调，因此 purge 必须扫描全部活跃条目，不能遇到首个未过期项就停止。
- `add(value)` 在 purge 后执行：
  1. 若已有 equal key，先显式删除旧映射，再插入传入的新对象；仅赋值或 `move_to_end` 会继续保留旧 key 对象，不能使用。
  2. 以本次 `now + ttl` 设置新 deadline，并放到末尾；`ttl == 0` 在下一次公开观察（同一时刻也满足边界）时应表现为已过期。
  3. 超过容量时用 `popitem(last=False)` 淘汰最老的活跃插入。
- `discard` 对不存在值不报错；所有删除、替换、过期和容量淘汰都要从唯一存储结构中移除引用。

### 关键边界

- `True` 不能作为合法 capacity、ttl 或 clock 返回值。
- 过期比较是 `>=`，不是 `>`；purge 必须先于成员、长度、替换和容量判断。
- equal 但非 identical 的新 key 要同时更新实际存储对象、deadline 和插入位置。
- 非单调 clock 可能使较新的插入先过期，因此插入顺序不能兼作过期顺序。

### 公开 smoke

1. 使用可控 clock：在 `now=10`、`ttl=5` 时添加值，断言 `14.999` 仍存在、`15` 已不存在且长度为零；再覆盖 `ttl=0`。
2. 容量为 2 时添加 `a,b`，刷新 equal 的新版 `a` 后再添加 `c`，断言应淘汰 `b`，新版 `a` 与 `c` 保留。
3. 用两个 equal 但 identity 不同、可弱引用的 key；替换后配合 `weakref`/`gc` 断言容器不再持有旧对象。
4. 让 clock 回退，使插入顺序靠后的条目先到期；断言 purge 会越过仍存活的头部并删除后方过期项。
5. 分别断言拒绝 `capacity=True/0`、`ttl=True/NaN/inf`、不可调用 clock，以及 clock 在后续操作中返回非数字、bool 或非有限值。

## 4. `duration.py`

### 实现方案

- `DurationParseError` 继承 `ValueError`；所有抛出路径都设置稳定 `.code` 和零基 `.position`。只使用公开代码：`type`、`empty`、`syntax`、`leading_zero`、`order`、`range`。
- 建立固定元数据：单位严格顺序 `d,h,m,s,ms`，输出键顺序 `days,hours,minutes,seconds,milliseconds`，以及 parse 上限 `h<24`、`m<60`、`s<60`、`ms<1000`；days 无上限。
- 两个 API 共用一个从左到右的游标扫描器：
  1. 非字符串报 `type`；空串报 `empty`，但空白字符串仍是 `syntax`。
  2. 每个字段先读取一个或多个 ASCII 数字（显式比较 `'0' <= ch <= '9'`，不要使用会接受 Unicode 数字的 `isdigit()`）。
  3. 多位数字以 `0` 开头时报 `leading_zero`；单个 `0` 合法。
  4. 单位匹配时先尝试 `ms`，再尝试单字符单位；记录单位 rank，要求后一字段 rank 严格增大，从而同时拒绝乱序和重复。
  5. 保存字段数值、数字起点和单位起点；扫描完整字符串后，`parse_duration` 再按字段顺序做范围检查，`normalize_duration` 跳过 subordinate range 检查但不放宽任何语法规则。
- 固定错误位置策略，并让两个 API 共用：`type/empty` 为 0；`syntax` 指向首个意外字符，意外结束则为 `len(text)`；`leading_zero/range` 指向该字段数字起点；`order` 指向违规字段的单位起点。多重错误按“类型、空串、扫描期 syntax/leading_zero/order、完整语法后的 range”顺序稳定处理。
- `parse_duration` 显式按五个固定键构造普通 dict，缺失字段补 0。
- `normalize_duration` 把语法有效字段换算成总毫秒，再依次用 `divmod` 拆成 d/h/m/s/ms；按该顺序拼接并省略零字段。总值为零时唯一输出 `0s`。Python 整数可直接支持无界 days。

### 关键边界

- 字段必须完全相邻；符号、小数点、首尾或字段间空白都为语法错误。
- `ms` 是单一单位：`1s2ms` 合法，`1ms2s` 为顺序错误。
- `parse_duration("24h")` 等越界字段报 `range`；相同文本对 normalize 合法并进位，例如 `24h -> 1d`。
- `0d`、多个合法零字段等都可解析；normalize 后统一为 `0s`。
- canonical 输出再次 normalize 必须逐字符不变。

### 公开 smoke

1. 断言 `parse_duration("2d3h4m5s6ms")` 返回五个固定顺序键及对应数值；缺失字段补零。
2. 断言 `parse_duration("1h60m")` 抛 `range`，而 `normalize_duration("1h60m") == "2h"`。
3. 断言 `normalize_duration("1h120m3601s1001ms") == "4h2s1ms"`，并将输出再次 normalize 验证幂等；全零输入得到 `0s`。
4. 检查代表性错误及位置：非字符串为 `type/0`、空串为 `empty/0`、`01s` 为 `leading_zero/0`、`1h 2m` 为 `syntax/2`、`1ms2s` 为 `order/4`、`24h` 对 parse 为 `range/0`。
5. 覆盖符号、小数、重复单位、尾部残字符、Unicode 数字，断言均抛 `DurationParseError` 且 `.code/.position` 每次一致。

## 联合验证与收尾

1. 从 `short/` 目录使用 `python -B` 运行一段不落盘的内联脚本，导入四个公开模块和全部公开对象；依次执行上述断言。不要读取或调用任何非公开测试材料。
2. 再做一次输入不变性、稳定顺序、异常属性类型和 normalize 幂等性的联合检查；失败时只回到对应 solution 文件修复，不顺手重构其他模块。
3. 全部公开 smoke 通过后，在 `solutions/` 同级写入以下精确 JSON：

```json
{"completed_tasks":["recursive_patch","dependency_layers","ttl_set","duration"],"ack":"I followed TASKS.md and changed only solution files."}
```

4. 最终确认四个公开对象集合未改名、实现只依赖标准库，且除四个 solution 文件与契约要求的 `instruction_ack.json` 外没有新增或改动其他文件。
