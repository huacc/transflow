# Round26 Contract-Driven Self-Engine Run Report

## 1. 运行目标

本轮验证新的分层状态机是否能做到：源/候选对比研判 -> 七产物物化 -> Triage -> Dispatch -> RepairPatch 绑定 -> Patch 应用 -> 再生成 -> 再研判。

## 2. 最终结论

- 过程契约：`PASS`
- 决策图契约：`PASS`
- 产品质量：`FAIL`
- 终态：`S_FAIL_QUALITY`
- 修复前阻塞数：`145`
- 修复候选阻塞数：`125`
- 当前接受候选阻塞数：`117`
- Loop 结果：`REJECTED_ROLLBACK`
- 修复候选是否接受：`False`
- 目标 failure 前后：`{'failure_class': 'text_fit_overflow', 'before': 120, 'after': 15}`
- 非目标硬 failure 回退：`{'cross_slot_overlap': {'before': 25, 'after': 110}}`
- 应用 RepairPatch 操作数：`120`
- Triage failure class：`text_fit_overflow`
- Dispatch repair family：`expand_or_reflow_slot`
- Deferred failure classes：`{'font_size_regression': 1, 'cross_slot_overlap': 8}`

## 3. 人可读研判结果

- 初始研判：候选译文与原文视觉关系存在阻塞差异，需要进入 RepairLoop。
- 初始工具选择：先由 Triage 按因果优先级选出 text_fit_overflow，再由静态 Dispatch 表映射到 expand_or_reflow_slot。
- Dispatch 结果：{'dispatch_table': 'contracts/failure_dispatch_table.json', 'selected_failure_class': 'text_fit_overflow', 'selected_repair_family': 'expand_or_reflow_slot', 'target_state': 'S6_LayoutPlan', 'allowed_operation_types': ['expand_slot'], 'tool': 'tools/repairs/build_repair_patch.py'}
- 修复后研判：候选译文与原文视觉关系存在阻塞差异，需要进入 RepairLoop。
- 当前接受候选研判：候选译文与原文视觉关系存在阻塞差异，需要进入 RepairLoop。
- 修复闭环：修复前阻塞 145 个，修复候选阻塞 125 个；闭环结果为 REJECTED_ROLLBACK。

## 3.1 本轮发现的能力缺口

- `expand_or_reflow_slot` 本轮被回测拒绝：目标 failure 或总阻塞数没有形成可接受改善，或非目标硬 failure 出现回退。hard_failure_regressions={'cross_slot_overlap': {'before': 25, 'after': 110}}。后续应将该 repair atom 改为 obstacle-aware repair，或在下一轮 loop 中尝试更局部的 RepairPatch。

## 4. 分层提示词模板

| 状态 | 模板 | 输入槽位 | 输出维度 | 本轮后端模型 |
|---|---|---|---|---|
| S8A | `S8A_quality_signal_normalization.prompt.json` | source_structure, generation_evidence, quality_gates | QualitySignal, page_signal_summary | not_invoked，使用本地确定性工具 |
| S8B | `S8B_quality_triage.prompt.json` | quality_signals, page_strategy, layout_plan | failure_class, selected_failure_class, needs_more_evidence | not_invoked，使用本地确定性工具 |
| S8C | `S8C_repair_patch_binding.prompt.json` | visual_adjudication, failure_dispatch_table, quality_signals, layout_plan | RepairPatch operations | not_invoked，使用本地确定性工具 |
| Lx | `Lx_repair_loop_execution.prompt.json` | repair_patch, layout_plan, quality_before | loop_verdict, before/after delta | not_invoked，使用本地确定性工具 |

## 5. 关键证据文件

- `reports/quality_signals.json`：修复前源/候选对比信号
- `reports/visual_adjudication.json`：修复前人可读裁决
- `reports/repair_patch_0001.json`：可执行 RepairPatch
- `reports/repair_patch_application_0001.json`：Patch 应用结果
- `reports/quality_signals.repair0001.json`：修复后源/候选对比信号
- `reports/repair_loop_0001.json`：闭环前后差异
- `reports/evidence_basket.json`：证据篮
- `reports/quality_signal_ledger.json`：质量信号账本
- `reports/problem_domain_buckets.json`：问题域桶
- `reports/triage_result.json`：分诊结果
- `reports/dispatch_result.json`：分发结果和 seed/registry 冲突裁决
- `reports/repair_acceptance.json`：修复接受/回滚裁决
- `reports/repair_memory_ledger.json`：跨轮修复记忆台账
- `reports/decision_graph_validation.json`：阶段 A 最小决策图校验
- `reports/state_trace.json`：完整状态迁移
- `reports/model_interactions.jsonl`：提示词模板及模型调用记录

## 6. 反过拟合边界

本轮运行未读取 `offline_reference_compare`，未使用人工对照 PDF，RepairPatch 只引用当前运行的 group_id、bbox、字号、fit 状态和重叠增量。
