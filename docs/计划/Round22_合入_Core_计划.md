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

### 阶段 4：迁入质量 gate 和 RepairPatch

动作：

1. 固化 `local_text_overlap`、`source_relative_font_floor`、`table_region_intrusion`。
2. 新增 `build_repair_patch.py` 和 `apply_repair_patch.py`。
3. 改造 runner：repair atom -> repair patch -> S6/S7/S8 重跑。

验证：

- 至少跑 `--max-repair-loops 2`。
- 确认第二轮不是只写 repair plan，而是真的生成新 candidate 和新 gate。

通过条件：

- `repair_loop_<n>.json.execution_status=applied_and_rejudged`。
- 新旧 gate 差异可解释。
- 若仍失败，必须是明确剩余 failure，不是 process contract 失败。

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

