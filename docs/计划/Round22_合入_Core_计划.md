# Round22 合入 Core 计划

## 1. 目标

把 `pdf_translation_workflow_lab/rounds/round22_table_layout` 中已经证明有价值的布局能力，合入 `pdf_translation_workflow_core` 的稳定流程，同时保证：

- 旧样本不退化。
- 新增表格、面板、局部容器和中译英长文本场景能生效。
- 运行时不依赖样本文档、页码、固定文字、固定数值或人工对照 PDF。
- `docs/业务流程/PDF_语义翻译回填_标准流程设计.md` 与 core 真实工具、契约、状态机保持一致。
- 合入后有明确回归证据、独立 spike 验证证据和可删除内容清单。

## 2. 明确假设

1. Round22 是实验能力来源，不是产品质量通过的最终基线。
2. Core 的主状态机方向正确，问题主要在 `S4/S6/S7/Lx` 的接口深度和 repair 执行能力不足。
3. 不能直接复制 round22 runner 到 core；需要把能力拆成稳定 Module。
4. 旧 core 生成路径必须保留到新路径通过回归和 spike 验证。
5. 人工对照 PDF 只能用于运行后的结果性评估，不能进入运行时输入、提示词槽位或工具裁决。
6. `RepairPatch` 机制闭环不等于 round22 视觉效果等价；视觉等价必须用同输入、同语义译文、同页集单独验证。

## 3. 非目标

- 不重写 S5 语义翻译物化链路。
- 不把 round22 的 `generate_candidate.py` 作为 core 产品生成器。
- 不把 `offline_reference_compare`、样本文字、样本页码、样本数值迁入 core。
- 不以单个 PDF 的肉眼效果作为唯一通过标准。
- 不在合入前删除旧输出、旧实验包或回归证据。

## 4. 合入原则

| 原则 | 要求 |
|---|---|
| 接口隔离 | 新能力通过 `RolePlan`、`LayoutPlan`、`RepairPatch` 接口进入 core，不散落到 generator 内部 |
| 向后兼容 | 旧 artifact 字段不删除、不改语义；新增字段只追加 |
| 可回退 | 新 planner 可以关闭；失败时可回退 legacy candidate 或诚实 `S_FAIL_QUALITY` |
| 证据驱动 | 所有新决策必须引用当前页 geometry、font stats、bbox adjacency、drawing objects 或 gate evidence |
| 反过拟合 | 禁止文件名、页码、样本文字、样本数字、人工对照路径参与运行时判断 |
| 闭环优先 | repair atom 必须能应用并重新生成、重新裁决；只写 repair plan 不算修复成功 |

## 5. 目标 Module 与 Interface

### 5.1 RolePlanPlanner

来源能力：

- round22 `plan_roles.py`
- round22 `generate_round22_layout_candidate.py` 中的 `Line`、`Group`、role classification 相关逻辑

目标位置：

- `pdf_translation_workflow_core/tools/planners/build_role_plan.py`

输入：

- `source_extraction.json`
- `semantic_translations.json`
- `layout_policy.json`

输出：

- `role_plan.json`

最小 schema：

```json
{
  "tool": "build_role_plan",
  "policy_version": "role_plan_v1",
  "source_extraction": "...",
  "semantic_translations": "...",
  "pages": [
    {
      "page_index": 0,
      "page_rect": [0, 0, 0, 0],
      "page_stats": {},
      "groups": [
        {
          "group_id": "string",
          "line_ids": ["string"],
          "role": "body|body_flow|heading|red_heading|red_note|metric_value|compact_panel|table_cell|legend|footnote|nav_footer|vertical_nav",
          "source_rect": [0, 0, 0, 0],
          "target_text": "string",
          "source_font_size": 0,
          "role_evidence": {
            "source_relative_features": {},
            "decision_reason": "string",
            "anti_overfit": "current page evidence only"
          }
        }
      ]
    }
  ]
}
```

验收：

- 角色判断不能依赖文件名、页码、样本文字。
- 表格密集块必须拆成 `table_cell` 或等价硬槽位，不能被合并成大段正文。
- 红色提示/红色标题必须按颜色、字体、位置、邻接关系综合判断，不能只按字号判断。

### 5.2 LayoutPlanPlanner

来源能力：

- round22 `plan_layout.py`
- 局部 line-grid container 推断
- filled panel compact layout
- table region obstacle pack
- translation growth slots
- section pushdown
- source graphic boundary limits

目标位置：

- 强化 `pdf_translation_workflow_core/tools/planners/build_layout_policy.py`
- 新增 `pdf_translation_workflow_core/tools/planners/build_layout_plan.py`

输入：

- `source_extraction.json`
- `role_plan.json`
- `layout_policy.json`

输出：

- `layout_plan.json`

原则：

- `layout_policy.json` 负责通用规则和运行时参数来源。
- `layout_plan.json` 负责当前 PDF 当前页当前 region 的具体 target rect、erase rect、font profile、draw mode。
- generator 只消费 `layout_plan.json` 和 semantic translations，不再隐藏布局猜测。

验收：

- 同一页内 target rect 不能出现不可解释重叠。
- 表格/图表/图片/侧栏/页眉页脚必须作为 obstacle 参与布局。
- 中译英长文本优先扩框、重排、避障；不能默认强塞进原 bbox。

### 5.3 ProductQualityGate

来源能力：

- round22 `validate_quality.py` 的 `all_groups_fit`
- `source_relative_font_floor`
- `local_text_overlap`

目标位置：

- 强化 `pdf_translation_workflow_core/tools/validators/collect_visual_region_metrics.py`
- 强化 `pdf_translation_workflow_core/tools/validators/evaluate_pdf_quality.py`

新增或固化 gate：

| gate_id | 判断依据 | 阻断条件 |
|---|---|---|
| `all_groups_fit` | `candidate_generation_evidence.insertions[*].status` | 非允许 fallback 或 overflow |
| `source_relative_font_floor` | source font stats + output font size | 关键角色输出字号低于源相对下限 |
| `local_text_overlap` | output insertion bbox 与 source overlap baseline 对比 | 输出新增实质重叠 |
| `table_region_intrusion` | table_cell region 与 body/heading target rect | 正文或标题侵入表格区域 |
| `role_plan_coverage` | role_plan group 与 source text unit linkage | 关键文本无 role 或无法回溯 |

验收：

- gate 必须输出 sample regions、bbox、role、source baseline、repair atom candidate。
- gate 失败必须进入 `Lx_RepairLoop` 或诚实 `S_FAIL_QUALITY`，不能直接 `PASS_WITH_WARN`。

### 5.4 RepairPatchPlanner

现状问题：

- core 的 `apply_policy_repair_overrides` 里存在固定阈值式 override。
- 这类 override 虽不一定是样本过拟合，但接口太浅，难以解释为什么对新 PDF 有效。

目标：

- 新增 `repair_patch.json`，由质量失败和当前页统计量推导。
- `Lx_RepairLoop` 不直接写固定参数，而是选择 repair atom 并生成 current-run repair patch。

目标位置：

- `pdf_translation_workflow_core/tools/repairs/build_repair_patch.py`
- `pdf_translation_workflow_core/tools/repairs/apply_repair_patch.py`

输入：

- `product_quality_gates.json`
- `visual_region_metrics.json`
- `role_plan.json`
- `layout_plan.json`
- `layout_policy.json`

输出：

- `repair_patch_<n>.json`
- `layout_policy.repair<n>.json`
- `layout_plan.repair<n>.json`

最小 schema：

```json
{
  "repair_patch_id": "string",
  "selected_gate_id": "local_text_overlap",
  "repair_atom": "table_region_obstacle_pack",
  "target_state": "S6_LayoutPlan",
  "target_scope": [{"page_index": 0, "group_id": "string"}],
  "current_page_evidence": {
    "font_stats_ref": "...",
    "bbox_stats_ref": "...",
    "obstacle_refs": []
  },
  "policy_delta": {},
  "layout_delta": {},
  "anti_overfit_statement": "No filename, page number, literal text, sample value, or reference PDF was used."
}
```

验收：

- patch 必须引用当前运行证据。
- patch 不能只有固定常量。
- patch 应用后必须重新 S7/S8。

## 6. 状态机调整

不推翻现有主状态机，只把 `S4/S6/Lx` 的内部接口补深。

### 6.1 调整后的关键流

```text
S3_SourceExtract
  -> source_extraction.json
S4_PageStrategy
  -> page_strategy.json
  -> role_plan.json
S5_TranslationPlan
  -> semantic_translations.json
S6_LayoutPlan
  -> layout_policy.json
  -> layout_plan.json
S7_GenerateCandidate
  -> candidate PDF
  -> candidate_generation_evidence.json
S8_VerifyProductQuality
  -> visual_region_metrics.json
  -> product_quality_gates.json
Lx_RepairLoop
  -> repair_patch_<n>.json
  -> back to S6 or S7
```

### 6.2 兼容规则

- 如果 `role_plan.json` 不存在，legacy 路径仍可运行，但必须记录 `planner_adapter=legacy`.
- 如果 `layout_plan.json` 不存在，generator 可从 `layout_policy.json` 构建 legacy regions，但必须记录 `layout_plan_version=legacy`.
- 新路径通过回归前，默认不移除 legacy 支持。

## 7. 合入阶段

### 当前执行状态（2026-07-08）

本节记录当前计划执行位置。若下方阶段定义与本节状态冲突，以本节为准。

| 阶段 | 状态 | 当前证据 | 结论 |
|---|---|---|---|
| 阶段 0：冻结基线 | 已完成历史基线动作 | 既有 regression/baseline 与合入前 tag | 不再重复执行 |
| 阶段 1：引入接口，不启用新能力 | 已完成历史接口阶段 | core 已有 `role_plan.json`、`layout_plan.json`、工具/契约/提示词接口 | 当前不是 Phase 1；Phase 1 shadow 说明只作为历史背景 |
| 阶段 2：迁入 RolePlanPlanner | 已完成 | `build_role_plan.py` 已进入 S6 链路 | 可进入阶段 3 |
| 阶段 3：迁入 LayoutPlanPlanner | 已完成最小验证 | `09_phase23_round22_single` 中 `layout_plan.json` 被 S7 消费，`layout_plan_consumed_by_generator=true` | S6/S7 布局计划消费链路通过 |
| 阶段 4A：迁入质量 gate 和 RepairPatch 机制 | 已完成流程闭环最小验证 | `13_phase4a_round22_single_after_runner_cleanup` 中生成 `repair_patch_0001.json.operation_count=1`，`repair_loop_0001.json.execution_status=applied_and_rejudged`，process PASS，product FAIL，anti-overfit PASS | RepairPatch 链路已进入 core 且 runner 已收敛到单一 RepairPatch 机制；这只证明闭环机制，不证明 round22 视觉效果已合入 |
| 阶段 4B：Round22 visual parity 能力迁入 | 未完成 | core 的 `11_phase4_round22_single_noop_skip` 与 `pdf_translation_workflow_lab/rounds/round22_table_layout` 同输入效果仍有明显差距 | 下一步必须逐项迁入 round22 的泛化排版能力，并用同输入对比 |
| 阶段 4C：回归矩阵和独立 spike 验证 | 入口预检阻塞 | `14_phase4c_entry_preflight` 显示 4B 仍未达到 visual parity；最新 core 证据仍为 process PASS、product FAIL；anti-overfit PASS | 不执行完整回归矩阵和独立 spike；先回到阶段 4B 迁入视觉等价能力 |
| 阶段 5：默认启用新路径 | 未开始 | 4B/4C 未完成 | 默认启用前必须完成视觉等价、回归矩阵和独立 spike 验证 |

正式阶段 2/3 证据报告：

```text
pdf_translation_workflow_regression/reports/09_phase23_round22_single_plan_execution_report.md
```

正式验证 run：

```text
pdf_translation_workflow_regression/runs/09_phase23_round22_single
```

该 run 的最终状态：

```json
{
  "process_contract_verdict": "PASS",
  "semantic_translation_verdict": "PASS",
  "generation_verdict": "PASS",
  "product_quality_verdict": "FAIL",
  "terminal_state": "S_FAIL_QUALITY"
}
```

阶段 4A 证据：

```text
pdf_translation_workflow_regression/runs/10_phase4_round22_single
pdf_translation_workflow_regression/runs/11_phase4_round22_single_noop_skip
pdf_translation_workflow_regression/runs/12_phase4a_round22_single_contract_sync
pdf_translation_workflow_regression/runs/13_phase4a_round22_single_after_runner_cleanup
```

`10_phase4_round22_single` 证明 `build_repair_patch.py -> apply_repair_patch.py -> S6/S7/S8` 链路能执行，但首选 `metric_value_font_hierarchy_repair` 对当前 policy 产生 `operation_count=0`，因此只能作为 no-op 暴露证据。

`11_phase4_round22_single_noop_skip` 修正调度后跳过 no-op repair atom，选择 `table_text_legibility -> constrained_slot_layout_fit_repair`，产出 `repair_patch_0001.json.operation_count=1`，`repair_loop_0001.json.execution_status=applied_and_rejudged`，`process_contract_verdict=PASS`，`product_quality_verdict=FAIL`。

`12_phase4a_round22_single_contract_sync` 证明工具契约、状态机、提示词绑定与 RepairPatch 证据同步后仍然闭环；`13_phase4a_round22_single_after_runner_cleanup` 在移除 runner 内部重复修补逻辑后重新验证，结果仍为 process PASS、product FAIL，并且 `scan_core_overfit.py` 使用 run-local 样本敏感 token 扫描 core 后 PASS。

后续正式 regression 编号必须继续递增；`13_phase4a_round22_single_after_runner_cleanup` 之后应从 `14_...` 开始。临时 `08_*` 不作为证据。

### 阶段 0：冻结基线

动作：

1. 记录当前 `main` commit。
2. 固定回归输入清单。
3. 跑一次 legacy core 基线。

输出：

- `pdf_translation_workflow_regression/runs/baseline_before_round22_merge_<timestamp>/`
- baseline verdict JSON
- baseline candidate PDFs
- baseline visual metrics

验收：

- baseline 能复现当前已知表现。
- baseline 失败项必须如实记录，不要求全 PASS。

### 阶段 1：引入接口，不启用新能力

动作：

1. 增加 `role_plan.json` 和 `layout_plan.json` schema 文档。
2. 增加工具壳或 adapter，但默认输出与 legacy 等价。
3. 更新流程文档和工具表。

验证：

- 跑 AIA/01_source/HSBC 小集。
- candidate 与 legacy 相比不应出现质量门禁新增阻断。

通过条件：

- legacy behavior 基本不变。
- process contract 仍 PASS。

### 阶段 2：迁入 RolePlanPlanner

动作：

1. 从 round22 抽取 role classification 能力。
2. 迁入 table_cell split、red_heading、compact_panel、nav_footer、metric_value 的通用分类。
3. 输出 `role_plan.json`，但 generator 仍可走旧 `layout_policy`。

验证：

- role_plan coverage。
- table/dense page 角色分布。
- anti-overfit scan。

通过条件：

- 表格密集页不再把大块表格合成一个 body/red paragraph。
- 旧 AIA timeline、dashboard、body 页面 role 不明显劣化。

### 阶段 3：迁入 LayoutPlanPlanner

动作：

1. 新增 `build_layout_plan.py`。
2. 迁入 line-grid container、filled panel、translation growth slots、table obstacle pack。
3. `generate_semantic_backfill.py` 优先消费 `layout_plan.json`。

验证：

- 中译英 00005 前 20 页。
- AIA en->zh / zh->en 前 20 页。
- 01_source timeline。

通过条件：

- 新增 `local_text_overlap` 不高于 legacy，重点页应下降。
- `fit_warning_count` 不增加。
- 图片/背景残留不退化。

### 阶段 4A：迁入质量 gate 和 RepairPatch 机制

入口条件（当前已满足）：

| 条件 | 证据 | 状态 |
|---|---|---|
| S6/S7 新链路已跑通 | `candidate_generation_evidence.json.layout_plan_consumed_by_generator=true`，`layout_execution.json.layout_plan_consumed_by_generator=true` | PASS |
| 候选 PDF 已生成 | `pdf_translation_workflow_regression/runs/09_phase23_round22_single/output/P23R22_src_candidate.pdf` | PASS |
| 产品质量 gate 已执行 | `product_quality_gates.json.product_quality_verdict=FAIL`，`blocking_failure_count=8` | PASS |
| 失败边界不是流程契约 | `P23R22_final_verdict.json.process_contract_verdict=PASS`，`process_validation_errors=[]` | PASS |
| 失败 gate 已映射 repair atom | `visual_repair_plan.json` 和 `repair_loop_0001.json` 已给出 target state 与 repair atom | PASS |
| repair loop 真实执行器存在 | `11_phase4_round22_single_noop_skip/reports/P4R22B_src/repair_loop_0001.json.execution_status=applied_and_rejudged` | PASS |

阶段 4A 的起点不是重新证明 `layout_plan.json` 能否被消费，而是把下列失败 gate 转成可执行 repair patch，并重跑 S6/S7/S8：

| failed gate | 当前 repair atom | target state |
|---|---|---|
| `metric_value_hierarchy` | `metric_value_font_hierarchy_repair` | `S6_LayoutPlan` |
| `table_text_legibility` | `constrained_slot_layout_fit_repair` | `S7_GenerateCandidate` |
| `insertion_collision` | `region_collision_layout_repair` | `S6_LayoutPlan` |
| `background_residue_artifact` | `background_residue_fill_resample` | `S7_GenerateCandidate` |
| `matrix_diagram_integrity` | `matrix_diagram_table_cell_preserve_repair` | `S6_LayoutPlan` |

阶段 4A 的第一轮原建议以 `metric_value_hierarchy` 为主修复对象，因为 `repair_loop_0001.json` 已选择该 gate 作为首个 failure class。实盘验证后发现该 atom 对当前 `layout_policy.json` 已无可追加操作，必须记录为 no-op 并跳过，不能把“无变更重跑”算作有效修复。当前通过证据为 `13_phase4a_round22_single_after_runner_cleanup`：它跳过 no-op 的 `metric_value_hierarchy -> metric_value_font_hierarchy_repair`，选择 `table_text_legibility -> constrained_slot_layout_fit_repair`，生成并应用 `repair_patch_0001.json.operation_count=1`，重建 `layout_plan.repair01.json`，重新生成候选 PDF，再执行 S8 gate。只有出现 `repair_loop_<n>.json.execution_status=applied_and_rejudged` 且 `repair_patch_operation_count>0`，才算阶段 4A repair loop 执行通过。

动作：

1. 固化 `local_text_overlap`、`source_relative_font_floor`、`table_region_intrusion`。
2. 新增 `build_repair_patch.py` 和 `apply_repair_patch.py`。
3. 改造 runner：repair atom -> repair patch -> S6/S7/S8 重跑。
4. 若首选 repair atom 生成 `operation_count=0`，必须写入 `skipped_noop_repairs` 并选择下一个可执行、非 no-op 的 repair atom。
5. runner 内不得保留第二套内联 policy 修补分支；兼容入口只能委托 `repair_policy_patch.py` 生成和应用通用 operations。

验证：

- 至少跑 `--max-repair-loops 2`。
- 确认第二轮不是只写 repair plan，而是真的生成新 candidate 和新 gate。

通过条件：

- `repair_loop_<n>.json.execution_status=applied_and_rejudged`。
- `repair_patch_<n>.json.operation_count>0`，或报告中明确 `skipped_noop_repairs` 后选择了下一个非 no-op atom。
- 新旧 gate 差异可解释。
- 若仍失败，必须是明确剩余 failure，不是 process contract 失败。

### 阶段 4B：Round22 visual parity 能力迁入

阶段 4B 的目标是回答一个更强的问题：同一个输入 PDF 和同一份语义译文，core 输出是否接近 `pdf_translation_workflow_lab/rounds/round22_table_layout` 的实验输出。阶段 4A 不能替代阶段 4B。

入口条件：

| 条件 | 证据 | 状态 |
|---|---|---|
| 4A RepairPatch 链路可执行 | `repair_loop_0001.json.execution_status=applied_and_rejudged` | PASS |
| 同输入源 PDF 固定 | `round22_table_layout/input/source_pdfs/00005_2025_annual_report_zh_pages_001_020.pdf` | PASS |
| 同语义译文固定 | `round22_table_layout/input/semantic_translations/R22_GEN_ZH_TO_EN_00005_pages_001_020.translations.json` | PASS |
| round22 lab 输出存在 | `round22_table_layout/output/` 下候选 PDF 和报告 | PASS |
| core 同输入输出存在 | `11_phase4_round22_single_noop_skip/output/P4R22B_src_repair01_candidate.pdf` | PASS |

必须先做能力差异清单，而不是直接继续调参：

| round22 能力 | 当前 core 风险 | 迁入方式 |
|---|---|---|
| source-line-grid container 推断 | core 仍可能把相邻表格/面板文本混成大块 body 或错误 reflow | 迁入 `build_role_plan.py`/`build_layout_plan.py` 的 current-run drawing-line/grid grouping |
| same-row cross-column split | 中译英长文本会跨列挤压或覆盖邻列 | 在 role plan 中用 bbox 行带、列带和相邻障碍拆组 |
| table/cell local obstacle pack | 表格、卡片、图例局部槽位无法合理扩展 | 在 layout plan 中为 table_cell/compact_label/legend 生成局部可用空间和避障约束 |
| source-relative font floor | core 可能为了塞入 bbox 把字体压得过小 | 将 gate 与 layout planner 绑定，先扩框/重排，再缩字 |
| local_text_overlap source-baseline gate | core 当前只有部分插入碰撞/视觉相似 gate，无法表达 round22 局部拥挤基线 | 迁入 validator 维度并映射到 `region_collision_layout_repair` 或 vertical-flow repair |
| two-phase erase then insert | 局部背景和文字擦除顺序可能导致白块或残影 | 保持 generator 先统一擦除计划区，再按 layout plan 插入 |
| filled panel/background-aware erase | 灰色面板/有色区域容易出现白条或色差 | 使用 current-run panel fill/drawing evidence，不使用固定颜色 |
| text-column vertical flow and section pushdown | 中文到英文变长时局部区域无法整体下推 | 迁入可解释的 column flow/pushdown 策略，边界由当前页空白、横线、图片、表格和页底容量决定 |

执行动作：

1. 对 `round22_table_layout` 的 `reports/role_plan.json`、`reports/layout_plan.json`、`reports/quality_gates.json` 与 core 的 `role_plan.json`、`layout_plan.json`、`visual_region_metrics.json` 做结构差异报告。
2. 只迁入差异清单中可泛化的能力；禁止复制 round22 的样本页码、样本文字、样本数值、固定坐标或人工对照路径。
3. 每迁入一类能力，都必须更新 core 工具、契约、提示词绑定和本计划。
4. 每次迁入后只用同一输入跑一个编号递增的 regression，例如 `12_round22_visual_parity_<capability>`。
5. 用 `scan_core_overfit.py` 扫描新增工具、契约、提示词和 profile。

防过拟合硬门禁：

| 类别 | 允许 | 禁止 |
|---|---|---|
| 文档身份 | 读取当前输入 PDF 的结构、字体、颜色、bbox、drawing、image、文本语言统计 | 根据文件名、公司名、报告年份、已知样本 ID 分支 |
| 页信息 | 使用当前页的几何分布、列带、行带、字体分位数、对象邻接关系 | 写死页码、页序、固定页面类型列表 |
| 文本信息 | 使用通用字符类别、语言方向、数字/货币/百分比 token shape、标题/正文/表格角色证据 | 写死原文句子、译文句子、专有段落、特定数值 |
| 坐标/尺寸 | 使用 page ratio、source-relative font ratio、当前页 quantile、局部 bbox adjacency | 写死绝对坐标、固定点号、固定颜色 RGB、固定 crop |
| 对照样本 | 只在运行后做 posthoc 评估，不进入 runtime 输入 | 让人工英文/中文对照 PDF 参与 D2/D4/D7、layout policy 或 repair patch |
| RepairPatch | 由 failed gate、repair atom、target language、region role 和 current-run policy 推导 | 针对某个 PDF/页/文本定制 patch |

每个能力迁入前必须写一条“泛化来源说明”：

```json
{
  "capability_id": "string",
  "source_round22_evidence": ["role_plan/layout_plan/quality gate paths"],
  "generic_runtime_evidence": ["geometry/font/color/drawing/text-shape fields"],
  "forbidden_sample_facts_checked": ["filename", "page_number", "literal_text", "literal_value", "fixed_coordinate", "reference_pdf"],
  "anti_overfit_verdict": "PASS|FAIL"
}
```

每个能力迁入后必须做两类防过拟合验证：

1. 静态扫描：把当前样本文件名、公司名、报告年份、关键页码、肉眼发现的特定文本和特定数值写入 run-local token file，执行 `scan_core_overfit.py`，结果必须 PASS。
2. 动态变形：至少换一个非 round22 的 PDF 或同 PDF 不同页集运行；若新能力只在 round22 页集有效、换页后失效或误判，不能进入 core 默认路径。

验证：

- 用同一输入和同一语义译文同时保留三份输出：round22 lab、core 迁入前、core 迁入后。
- 比较 `role_plan` 中的 group/role 数量、table_cell/compact_label/legend/body_flow 分布。
- 比较 `layout_plan` 中的目标 bbox、局部扩框、避障记录、font profile 和 draw mode。
- 比较产品 gate：至少覆盖 `text_fit`、`source_anchor_order`、`local_text_overlap`、`source_relative_font_floor`、`table_text_legibility`、`insertion_collision`、`background_residue_artifact`。
- 视觉抽检必须使用当前 source 与 candidate 的 crop，不得使用人工英文对照作为 runtime 输入。

通过条件：

- core 同输入输出在关键页面的表格/面板/局部容器排版接近 round22 lab，不再出现明显比 round22 更差的大块挤压、跨列覆盖、白条、局部缺字。
- 若产品质量仍 FAIL，失败类别必须是 round22 也未解决或新 validator 暴露的真实问题，而不是缺少已声明迁入能力。
- `repair_loop` 若进入，必须继续符合阶段 4A 的 `repair_patch_<n>.json.operation_count>0` 或 `skipped_noop_repairs` 规则。
- 旧 AIA/01_source 小样不出现明显退化。

### 阶段 4C：回归矩阵和独立 spike 验证

阶段 4C 只在 4B 达到 visual parity 后启动。

当前入口预检证据：

```text
pdf_translation_workflow_regression/runs/14_phase4c_entry_preflight
pdf_translation_workflow_regression/reports/14_phase4c_entry_preflight_report.md
```

预检结论：

```json
{
  "entry_check": "FAIL",
  "terminal_state": "S_BLOCKED_PHASE_PREREQUISITE",
  "core_mutation": "NONE"
}
```

阻塞原因：阶段 4B 仍未完成，最新阶段 4A core 证据仍为 `product_quality_verdict=FAIL`。因此本阶段的完整回归矩阵和独立 spike 包不能启动；否则会违反本计划的状态迁移。

动作：

1. 跑完整 regression matrix：AIA en->zh/zh->en、01_source timeline、00005 zh->en、临时测试 PDF。
2. 每个 regression run 使用递增编号，保留 `final_verdict.json`、candidate PDF、render previews、visual metrics、repair loop records。
3. 构建独立 spike 包，只给当前 core、流程文档、测试提示词和输入 PDF；不提供 round22 lab 工具作为运行依赖。
4. spike 指令要求不改 framework；如因契约缺失必须小改，必须在报告中记录。

通过条件：

- 独立 spike 能复现 core 状态机和工具调度，不需要复制 round22 实验 runner。
- `process_contract_verdict=PASS`。
- 产品质量若 FAIL，失败边界可映射到明确 gate 和 repair atom。
- regression 报告证明新能力没有让旧样本明显退化。

### 阶段 5：默认启用新路径

动作：

1. 对命中表格/面板/局部容器/中译英长文本扩展证据的页面启用新 planner。
2. 保留 `--layout-planner legacy|v2|auto`。
3. 默认 `auto`，但 regression 可以强制 legacy 做对比。

通过条件：

- 回归矩阵通过。
- spike 验证不需要改 framework。

## 8. 回归测试矩阵

### 8.1 必跑回归

| 编号 | 输入 | 方向 | 目的 | 通过条件 |
|---|---|---|---|---|
| R-AIA-ENZH-01 | `样本/英文/AIA_2020_Annual_Report_en.pdf` 1-20 页 | en->zh | 保护既有 AIA 英译中效果 | 关键 gate 不退化；图片、背景、侧栏、表格不劣化 |
| R-AIA-ZHEN-01 | `样本/中文/AIA_2020_Annual_Report_zh.pdf` 1-20 页 | zh->en | 保护 AIA 中译英方向 | 正文字号比例、侧栏方向、表格结构不劣化 |
| R-01-TIMELINE | `01_source.pdf` | en->zh | 保护最早 timeline 单页 | 年份层级、列布局、图片位置不劣化 |
| R-AIA-SLICE | `测试数据/AIA_2020_Annual_Report_en_pages_08_09_24_25.pdf` | en->zh | 保护图表/表格/正文混合页 | 表格、图例、正文不退化 |
| R-HSBC-ZHEN-20 | `样本/测试数据/00005_2025_annual_report_zh.pdf` 1-20 页 | zh->en | 验证 round22 新能力 | 页 3/5/6/16 重点问题改善；整体不出现大面积重叠 |
| R-HSBC-ENZH-20 | `样本/测试数据/00005_2025_annual_report_en.pdf` 1-20 页 | en->zh | 验证反向兼容 | 不能因 zh->en 新能力破坏 en->zh |

### 8.2 评价维度

每个 regression run 必须记录：

- process verdict
- product verdict
- terminal state
- candidate path
- `source_extraction.json`
- `role_plan.json`
- `layout_policy.json`
- `layout_plan.json`
- `candidate_generation_evidence.json`
- `visual_region_metrics.json`
- `product_quality_gates.json`
- `repair_loop_<n>.json`
- `anti_overfit_scan.json`

### 8.3 不退化判定

新路径相对 legacy 不得出现：

- 页数变化。
- 页面尺寸变化。
- semantic coverage 下降。
- `fit_warning_count` 增加。
- `source_anchor_order` 从 pass 变 fail。
- `image_color_integrity` 从 pass/warn 变 fail。
- `background_residue_artifact` 阻断数量明显增加。
- AIA/01_source 人眼可见的布局大幅劣化。

允许：

- 旧路径本来失败，新路径仍失败，但失败类别更具体。
- 新路径为了保留可读性导致局部 bbox 变大，只要不破坏障碍物、图像、表格和字体层级。

## 9. 反过拟合验证

### 9.1 静态扫描

必须扫描：

- `pdf_translation_workflow_core/tools`
- `pdf_translation_workflow_core/contracts`
- `pdf_translation_workflow_core/prompts`
- `pdf_translation_workflow_core/profiles`

禁止出现：

- 样本文档文件名。
- 固定页码组合。
- 人工对照路径。
- round22 输出路径。
- 样本文字片段。
- 样本财务数字。
- 为某页单独设置的 absolute bbox。

### 9.2 动态变形测试

至少执行：

1. 同一输入 PDF 改名后运行，role/layout 决策不应依赖文件名。
2. 抽不同页组合运行，不能依赖固定页码。
3. 删除人工参考 PDF 后运行，runtime 不应失败。
4. 更换输出目录运行，workspace boundary 仍 PASS。

### 9.3 数值规则要求

允许：

- 来自当前页统计量的比例。
- 来自当前 bbox、font quantile、line height、page size 的派生值。
- 通用排版经验比例，但必须写明是全局默认，并能被当前页统计覆盖。

不允许：

- 从某个样本页手调出来且无来源说明的固定值。
- 在 repair override 中无证据地写死具体阈值。
- profile 中出现 `min_pt`、`max_pt`、`min_insert_pt` 等绝对字号。

## 10. 独立 Spike 验证

合入 core 后必须构建新 spike 包。

建议命名：

- `spikes/spike_round22_core_merge_validation`

包内只放：

- 当前 `pdf_translation_workflow_core`
- 当前 `docs/业务流程/PDF_语义翻译回填_标准流程设计.md`
- 当前测试提示词
- 指定输入 PDF
- 空 `docs/output`
- 空 `docs/reports`

新会话要求：

- 不修改 framework。
- 按流程文档状态机执行。
- 记录所有工具调用、状态迁移、质量 gate、repair loop。
- 若失败，必须定位是 process contract、tooling、capability 还是 product quality。

通过条件：

- 至少一个 zh->en 表格/面板 case 生成候选 PDF。
- 状态机和工具调度与流程文档一致。
- 若产品质量失败，失败原因必须可映射到 repair atom。
- 不出现缺契约、缺工具说明、缺提示词边界导致的早期失败。

## 11. 文档同步清单

必须更新：

- `docs/业务流程/PDF_语义翻译回填_标准流程设计.md`
- `pdf_translation_workflow_core/docs/process/PDF_语义翻译回填_标准流程设计.md`
- `pdf_translation_workflow_core/contracts/state_machine.md`
- `pdf_translation_workflow_core/contracts/tool_contracts.md`
- `pdf_translation_workflow_core/contracts/product_quality_contract.md`
- `pdf_translation_workflow_core/contracts/page_type_repair_matrix.md`
- `pdf_translation_workflow_core/prompts/templates/D4_layout_plan.prompt.json`
- `pdf_translation_workflow_core/prompts/templates/D8_repair_selection.prompt.json`
- `pdf_translation_workflow_core/tools/README.md`
- `pdf_translation_workflow_core/docs/promotion/ROUND22_TABLE_LAYOUT_PROMOTION.md`

文档必须说明：

- 新增状态内子步骤。
- 新增 artifact schema。
- 新增工具输入输出。
- 新增 gate 到 repair atom 映射。
- 新 planner 的启用条件和回退条件。
- 哪些内容来自 round22，哪些没有合入。

## 12. 可删除内容与删除条件

### 12.1 可以考虑删除或归档的内容

| 路径 | 处理方式 | 删除或归档条件 |
|---|---|---|
| `docs/output/round22` | 删除或归档 | 已确认完整快照存在于 `pdf_translation_workflow_lab/rounds/round22_table_layout`，且合入提交已打 tag |
| `pdf_translation_workflow_lab/rounds/round22_table_layout/output` | 可归档，不建议立即删 | core 合入、regression 通过、spike 通过后，保留报告和 promotion manifest 即可 |
| `pdf_translation_workflow_lab/rounds/round22_table_layout/previews` | 可归档或压缩 | regression 中已有新 candidate previews，且人工不再需要逐图对照 |
| `pdf_translation_workflow_lab/rounds/round22_table_layout/offline_reference_compare` | 可移到 regression/posthoc 区 | 对照评估工具和样本已明确不参与 runtime，且有 posthoc 评估记录 |
| `pdf_translation_workflow_lab/rounds/round22_table_layout/tools` | 保留到一个版本后再删 | 对应能力已在 core 有稳定工具、回归和 spike 证据 |
| 旧 regression 临时 run | 删除 | 已有 baseline/latest 两组代表性 run，且旧 run 未被计划或报告引用 |

### 12.2 不能删除的内容

合入期间不能删除：

- `pdf_translation_workflow_lab/rounds/round22_table_layout/reports/round22_experiment_report.md`
- `pdf_translation_workflow_lab/rounds/round22_table_layout/promotion/promotion_manifest.json`
- `pdf_translation_workflow_lab/rounds/round22_table_layout/contracts/*`
- `docs/业务流程/PDF_语义翻译回填_标准流程设计.md`
- 本计划文档
- 合入前 baseline regression run
- 合入后 latest regression run

### 12.3 删除前检查

删除前必须确认：

1. Git 已有合入前 tag。
2. Git 已有合入后 tag。
3. regression 和 spike 报告中不再引用待删除路径。
4. 删除清单写入一份 cleanup report。
5. 删除后 `git status` 只包含预期删除项。

## 13. 回滚策略

如果新能力导致旧样本退化：

1. 不回滚整个 core。
2. 先关闭 `--layout-planner auto`，强制 legacy。
3. 保留新工具但标记 `experimental_disabled`。
4. 将失败 case 写入 regression。
5. 修复 `RolePlan/LayoutPlan/RepairPatch` 后再启用。

如果发现过拟合：

1. 立即阻断合入。
2. 删除样本事实分支。
3. 把规则改成 current-run geometry derivation。
4. 增加对应 anti-overfit token。
5. 重新跑静态扫描和动态变形测试。

## 14. 成功标准

本次合入完成必须同时满足：

- Core 可以在 legacy 和 v2/auto planner 之间切换。
- 新 planner 在 HSBC zh->en 表格/面板场景有可观察改善。
- AIA、01_source、AIA slice 回归不明显退化。
- repair loop 至少对一个布局失败执行 `applied_and_rejudged`。
- 产品质量失败仍能诚实失败，不会伪装成通过。
- 所有新增工具、契约、提示词、流程文档保持一致。
- anti-overfit 静态扫描 PASS。
- 动态变形测试 PASS。
- 独立 spike 不改 framework 能跑通到候选生成和质量裁决。
- 可删除内容有 cleanup report，不提前删除仍被引用的证据。

## 15. 建议执行顺序

1. 建立 regression baseline。
2. 合入接口 schema，不启用新行为。
3. 合入 `RolePlanPlanner`。
4. 合入 `LayoutPlanPlanner`。
5. 合入质量 gate。
6. 合入 `RepairPatchPlanner`。
7. 开启 `--layout-planner auto`。
8. 跑完整回归矩阵。
9. 构建独立 spike 验证。
10. 更新流程设计文档和 core 文档副本。
11. 打 tag。
12. 按 cleanup report 清理可删除内容。
