# Round22 合入 Core Phase 1 执行记录

## 范围

本次执行 `Round22_合入_Core_计划.md` 的阶段 1：引入接口，不启用新能力。

已完成：

- 新增 `build_role_plan.py`，生成 `role_plan.json`。
- 新增 `build_layout_plan.py`，生成 `layout_plan.shadow.json`。
- `run_semantic_product_quality_round.py` 在 S6 中串联：
  - `build_layout_policy.py`
  - `build_role_plan.py`
  - `build_layout_plan.py`
- 保持 `generate_semantic_backfill.py` legacy 主路径不变，仍消费 `layout_policy.json` 并输出正式 `layout_plan.json`。
- 修复 runner 旧边界问题：
  - 未找到 language profile 时返回 `None`，避免把工作区根目录当 JSON 读取。
  - 初始 `plan_visual_region_repairs.py` 记录在 `S8_VerifyProductQuality`，不再伪装成 `Lx_RepairLoop`。
  - 只有存在 `repair_loop_<n>.json` 时，汇总状态才进入 `Lx_RepairLoop`；无 loop 记录的质量失败直接从 S8 进入 `S_FAIL_QUALITY`。

未启用：

- generator 尚未消费 `layout_plan.shadow.json`。
- round22 实验工具没有直接拷贝进 core。
- 未删除 round22 输出、历史 regression run 或 spike 内容。

## 验证

### 编译验证

命令：

```powershell
python -m py_compile `
  pdf_translation_workflow_core\tools\planners\build_role_plan.py `
  pdf_translation_workflow_core\tools\planners\build_layout_plan.py `
  pdf_translation_workflow_core\tools\run_semantic_product_quality_round.py
```

结果：通过。

### planner smoke

输入：

- `01_source.pdf`
- `docs\input\semantic_translations\R1_01_source_single_timeline.translations.json`

输出目录：

```text
pdf_translation_workflow_regression\runs\merge_phase1_role_layout_smoke_20260708_195917
```

结果：

- `role_plan.json` 生成成功。
- `layout_plan.shadow.json` 生成成功。
- `role_groups=45`
- `required_units=94`
- `layout_groups=45`
- `estimated_overlaps=52`

说明：shadow layout 只作为证据，`estimated_overlaps` 不改变当前候选 PDF，也不作为产品质量通过依据。

### runner smoke

输入：

- `01_source.pdf`
- `docs\input\semantic_translations\R1_01_source_single_timeline.translations.json`

输出目录：

```text
pdf_translation_workflow_regression\runs\merge_phase1_runner_smoke_20260708_200355
```

关键结果：

```json
{
  "process_contract_verdict": "PASS",
  "semantic_translation_verdict": "PASS",
  "generation_verdict": "PASS",
  "product_quality_verdict": "FAIL",
  "terminal_state": "S_FAIL_QUALITY"
}
```

产物检查：

- `source_extraction.json`: exists
- `layout_policy.json`: exists
- `role_plan.json`: exists
- `layout_plan.shadow.json`: exists
- `layout_plan.json`: exists
- `candidate_generation_evidence.json`: exists
- `product_quality_gates.json`: exists

产品质量失败原因仍是当前候选的真实门禁：

```text
text_residue
```

这不是 Phase 1 的失败。Phase 1 的目标是接口和状态机证据链通过，而不是接受该候选 PDF。

### anti-overfit

来源：

```text
pdf_translation_workflow_regression\runs\merge_phase1_runner_smoke_20260708_200355\reports\anti_overfit_scan.json
```

结果：

```json
{
  "verdict": "PASS",
  "blocking_hit_count": 0,
  "warning_hit_count": 0
}
```

额外人工 grep：

- 新增 `build_role_plan.py`
- 新增 `build_layout_plan.py`
- 修改后的 runner

未发现样本文件名、固定页码组合、样本文字或固定坐标分支。

## 当前结论

Phase 1 可以进入代码评审/提交。

下一步不应直接启用 round22 行为，而应按计划进入 Phase 2/3：

1. 扩展 `RolePlanPlanner` 的表格/面板/图例拆分能力。
2. 扩展 `LayoutPlanPlanner` 的目标语增长、避障、下推和表格局部布局能力。
3. 在 regression 中对比 legacy `layout_plan.json` 与 shadow `layout_plan.shadow.json`。
4. 只有回归证明不退化后，才增加 generator 消费 v2 layout plan 的开关。
