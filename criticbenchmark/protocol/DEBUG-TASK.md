# Debugging Task: dependency_layers.py

## 背景

`solutions/dependency_layers.py` 是一个 Python 标准库实现的依赖分层拓扑排序模块。该代码通过了公开 smoke 测试（4/4），但在隐藏评价 criterion 中全部 4 项失败。你的任务是找出并修复所有 bug。

## 已知失败项

| Criterion | 描述 |
|-----------|------|
| `dl_one_shot` | 一次性消费 edges iterable + 基本分层正确性 |
| `dl_dependency_nodes` | 只出现在 dependency 位置的节点也出现在结果中 |
| `dl_stable_order_cycle` | 稳定排序 + 环检测 |
| `dl_deep_iterative` | 深层无环输入不触发递归上限 |

此外，`res_dependency_20000`（20,000 节点性能测试）也失败。

## 契约要求（来自 TASKS.md）

Export `DependencyCycleError(ValueError)` with a `.nodes` tuple and `dependency_layers(edges)`。

- `edges` is a one-shot iterable of `(node, dependency)` pairs; consume it exactly once.
- Return `list[list[object]]`, with dependencies in earlier layers than dependents.
- Include nodes appearing only in the dependency position.
- Deduplicate repeated edges.
- Within a layer, order nodes by first appearance anywhere in the edge stream.
- Raise `DependencyCycleError` only for a real directed cycle, with remaining cyclic/blocked nodes in stable first-appearance order.
- Handle very deep acyclic inputs without recursion.

## 验证方式

```bash
python -B public_smoke_tests.py -v
```

注意：smoke 测试会通过，但这不代表 bug 已修复。你需要自己编写更全面的测试用例来验证修复的正确性。

## 建议的测试场景

1. 多个节点依赖同一个节点：`[("a", "b"), ("c", "b")]` -> 期望 `[["b"], ["a", "c"]]`
2. 独立子图：`[("c", "a"), ("d", "b")]` -> 期望 `[["a", "b"], ["c", "d"]]`
3. 环检测：`[("a", "b"), ("b", "a"), ("c", "a"), ("d", "e")]` -> 期望异常 `.nodes == ("a", "b", "c")`
4. 深链（10000+ 节点）不超时

## 参考文件

- `TASKS.md` - 完整任务契约
- `frozen-phi3-plan.md` - 实施计划（含详细边界说明）
- `B11-bug-report.json` - 正式评分报告
