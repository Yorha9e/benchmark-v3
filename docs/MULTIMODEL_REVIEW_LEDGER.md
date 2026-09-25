# 多模型审查记录：payment_ledger（题二）

> 记录时间：2026-09-24
> 方法：7 slot 一审（5 完成：1/2/3/4/7；slot5/6 Go-403 缺席；slot3 窄范围）+ 4 模型二审裁决（slot4 网关 503）
> 对象：`LEDGER_CONTRACT` + `REFERENCE_LEDGER` + `run_ledger_scenario` / `build_ledger_milestones` + B 计划

## 一审发现的 P1（全部修复，`8850704`）

| # | 问题 | 后果 |
|---|---|---|
| C1 | 契约 transfer 借贷方向写反 `(debit source, credit destination)`，与 balance=Σdebits−Σcredits 矛盾 | 照契约实现会使**付款方余额增加**，m2/m3/m4/m6/m9 五个里程碑全挂 |
| C2 | 契约 balance() 称"负余额不可能"，探针却要求贷方账户为负（B=−70、cold=−100、m9 的 −1000） | 字面实现（全局非负）在第一笔注资就崩，p1 进 error 近乎全卷归零 |
| C3 | settle 未声明"批次本身是 journal entry"，m9 硬编码 settle 后余额（760/−1000/240） | A 条件模型无凭据推出该语义，m9 必挂 |
| C4 | entry dict 标 "your internal shape"，但 reconcile 探针直接解构 post/transfer 返回值 | 字段命名不同的正确实现 → m7+m8 归零（22% 总分） |

P2：mixed-currency 拒绝清单缺失；transfer "idem_key 已用则拒绝"与"重放同结果"自相矛盾；reconcile 语义（batch 绑定/seq 粒度/按腿报告）未定义；PROBE-ERROR 透传缺失；冲正重放是死键无探针；m6 不验证批次真实入账；b_plans 自检闸门对两份业务计划恒 FAIL 4 条；参考实现不挡"冲正一个冲正"。

## 二审裁决（`8960591`）

- **C1~C7 / J1~J3 / R1 全部裁定"修复正确"**，契约/参考/探针三层一致。
- **D1 销案**：slot3 撤回"settle 符号句互斥"异议——R2（同号累加）下"批次是 entry"与"后=前+净额"是同一句话的因与果，760/−1000/240 验算自洽（借贷各 1120 平衡）。
- **D2 裁定**：计划复述契约语义**不构成泄露**——计划头已声明"冲突以 TASK.md 为准"，B 条件考的就是遵从工作单；真正的泄露红线（断言 id/期望值/种子）一样都没有。
- **二审新抓的残局**（我 round-1 补丁自己引入/未改净，已修）：① C7 写成"seq unique per leg"与参考实现的"一 posting 一个 seq"矛盾——照做会在干净流误报 duplicate；② C6 transfer 拒绝清单残留 "idem_key already used by another operation"；③ 幂等重放先于一切校验未写明；④ settle 批次的戳日与"不参与当日 netting"未定义（有日后重算翻倍的风险）；⑤ 一 posting 同账户多腿的语义未定义（已钉死：至多一条腿/账户）。

## 仍未覆盖（文档化的宽容性条款，不影响评分）

- 空日 settle（参考返回空批次）、`open_account` 重复开户、单腿同时 debit+credit、post 重放"参数不同"的路径、reverse-of-reversal 本身（R1 一致性修复，无探针）。
- m3 并发探针对"锁外 check-then-act"的召回率为 0（CPython GIL 掩蔽，已在代码注释与 mutant 表中声明）。

## 结论

题二达到与题一相同的审查标准：契约自包含（A 条件公平）、判定器不侵入私有约定、参考实现与契约逐条一致、突变体全门控。可以进入校准。
