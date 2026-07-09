# Round24 State-Machine RepairPatch Run Report

## 1. 运行目标

本轮验证新的分层状态机是否能做到：源/候选对比研判 -> 质量信号归一 -> Triage -> RepairPatch 绑定 -> Patch 应用 -> 再生成 -> 再研判。

## 2. 最终结论

- 过程契约：`PASS`
- 产品质量：`FAIL`
- 终态：`S_FAIL_QUALITY`
- 修复前阻塞数：`80`
- 修复候选阻塞数：`102`
- 当前接受候选阻塞数：`80`
- Loop 结果：`REJECTED_ROLLBACK`
- 修复候选是否接受：`False`
- 应用 RepairPatch 操作数：`33`
- Triage failure class：`cross_slot_overlap`
- Dispatch repair family：`vertical_flow_relayout`
- Deferred failure classes：`{'text_fit_overflow': 9}`

## 3. 人可读研判结果

- 初始研判：候选译文与原文视觉关系存在阻塞差异，需要进入 RepairLoop。
- 初始工具选择：先由 Triage 选出 cross_slot_overlap，再由静态 Dispatch 表映射到 vertical_flow_relayout。
- Dispatch 结果：{'dispatch_table': 'contracts/failure_dispatch_table.json', 'selected_failure_class': 'cross_slot_overlap', 'selected_repair_family': 'vertical_flow_relayout', 'target_state': 'S6_LayoutPlan', 'allowed_operation_types': ['vertical_flow_relayout'], 'tool': 'tools/repairs/build_repair_patch.py'}
- 修复后研判：候选译文与原文视觉关系存在阻塞差异，需要进入 RepairLoop。
- 当前接受候选研判：候选译文与原文视觉关系存在阻塞差异，需要进入 RepairLoop。
- 修复闭环：修复前阻塞 80 个，修复候选阻塞 102 个；闭环结果为 REJECTED_ROLLBACK。

## 3.1 本轮发现的能力缺口

- `vertical_flow_relayout` 本轮被回测拒绝：它按页/角色移动下游文本流，缺少障碍感知和局部列内重排，导致修复候选阻塞数上升。后续应将该 repair atom 改为 obstacle-aware flow repair，或在下一轮 loop 中把已拒绝 failure class 暂时降权并尝试 deferred failure class。

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
- `reports/state_trace.json`：完整状态迁移
- `reports/model_interactions.jsonl`：提示词模板及模型调用记录

## 6. 反过拟合边界

本轮运行未读取 `offline_reference_compare`，未使用人工对照 PDF，RepairPatch 只引用当前运行的 group_id、bbox、字号、fit 状态和重叠增量。
