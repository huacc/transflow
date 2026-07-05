# Round11/Round12 失败诊断与核心修补记录

## 结论

Round12 的首要失败不是排版，而是 D2 翻译真实性失败。旧流程把 `本行说明...`、`本行列示...`、`This line reports...`、`This line describes...` 这类元描述式伪译文当作语义译文放行，导致后续排版工具在错误输入上继续工作。

Round11 的首要问题是真实 zh->en 译文回填后的布局失败。旧布局把连续正文行固定用段落分隔连接，并且没有区分 dense table/chart 页面里的 `table_cell`，导致正文大空隙、字体比例异常、表格/图例小格 fallback。

## 根因

1. `validate_semantic_translations.py` 只校验覆盖、目标语字符和保留数字，未识别元描述式伪译文。
2. `D2_translation.prompt.json` 没明确禁止“描述文本行类别”，模型可输出“本行说明/This line reports”这类格式。
3. `build_layout_policy.py` 没有 `table_cell` 角色，也没有把 `body_flow` 的同段续行和新段落分开。
4. `generate_semantic_backfill.py` 的 `body_flow` 使用固定 `paragraph_separator`，导致连续行被错误拉成段落。
5. `D8_repair_selection` 不能把 `semantic_translation_authenticity_fail` 路由回 `S5_TranslationPlan`。

## 已修补文件

- `pdf_translation_workflow_core/tools/validators/validate_semantic_translations.py`
- `pdf_translation_workflow_core/tools/generators/generate_semantic_backfill.py`
- `pdf_translation_workflow_core/tools/planners/build_layout_policy.py`
- `pdf_translation_workflow_core/prompts/templates/D2_translation.prompt.json`
- `pdf_translation_workflow_core/prompts/templates/D4_layout_plan.prompt.json`
- `pdf_translation_workflow_core/prompts/templates/D5_D7_quality_gate.prompt.json`
- `pdf_translation_workflow_core/prompts/templates/D8_repair_selection.prompt.json`
- `pdf_translation_workflow_core/prompts/prompt_tool_bindings.json`
- `pdf_translation_workflow_core/prompts/model_tool_orchestration_contract.md`
- `pdf_translation_workflow_core/contracts/semantic_translation_contract.md`
- `pdf_translation_workflow_core/contracts/tool_contracts.md`
- `pdf_translation_workflow_core/contracts/state_machine.md`
- `pdf_translation_workflow_core/contracts/product_quality_contract.md`
- `pdf_translation_workflow_core/contracts/decision_contracts.md`
- `pdf_translation_workflow_core/contracts/page_type_repair_matrix.md`
- `pdf_translation_workflow_core/tools/README.md`
- `docs/业务流程/PDF_中文回填_标准流程设计.md`

## 新增通用规则

### S5 语义真实性阻断

以下目标文本或 `layout_variants` 必须失败：

- `本行说明...`
- `本行列示...`
- `当前页的财务报告、治理或业务信息`
- `保留数值与标记...`
- `This line reports...`
- `This line describes...`
- `preserve figures/markers...`

这类失败是 `semantic_translation_authenticity_fail`，应回到 `S5_TranslationPlan` 或进入 `S_FAIL_CAPABILITY`，不能当作排版问题。

### S6 布局策略

`layout_policy.json` 必须包含或能解释：

- `classification_rules.table_cell`
- `classification_rules.legend`
- `flow_grouping.body.max_vertical_gap_pt`
- `flow_grouping.body.paragraph_gap_pt`
- `flow_grouping.body.line_joiner_en`
- `flow_grouping.body.line_joiner_zh`
- `flow_grouping.body.disable_page_type_guesses`
- `layout_text_variants.table_cell*`
- `layout_text_variants.legend*`
- `font_profiles.table_cell`
- `font_profiles.legend`

`body_flow` 合并时，小于 `paragraph_gap_pt` 的相邻区域视为同段续行，使用目标语行连接符；大于等于该阈值才使用段落分隔。

### Dense Table/Chart 页

当当前页 `page_type_guess` 是 `table_or_chart_dense` 或 `chart_or_dashboard` 时，默认禁用 `body_flow`，紧凑单元格优先归类为 `table_cell`。如果 D4 要例外合并，必须记录当前页证据。

## 验证证据

### 语义校验回归

| Case | 验证文件 | 结果 |
|---|---|---|
| Round11 zh->en 真实译文 | `docs/reports/round11_round12_diagnosis/r11_semantic_validation_after_core_fix_final.json` | PASS，245/245，invalid 0 |
| Round12 EN random 旧伪译文 | `docs/reports/round11_round12_diagnosis/r12_en_semantic_validation_after_core_fix_final.json` | FAIL，invalid 257/271 |
| Round12 ZH random 旧伪译文 | `docs/reports/round11_round12_diagnosis/r12_zh_semantic_validation_after_core_fix_final.json` | FAIL，invalid 199/252 |

### Round11 布局探针

探针输出：

```text
docs/output/round11_round12_diagnosis/R11_after_core_fix_v2_probe.pdf
docs/output/round11_round12_diagnosis/previews_v2/
docs/reports/round11_round12_diagnosis/R11_after_core_fix_v2_candidate_generation_evidence.json
docs/reports/round11_round12_diagnosis/R11_after_core_fix_v2_product_quality_gates.json
```

对比旧 Round11 生成证据：

| 指标 | 旧 Round11 | 修补后探针 |
|---|---:|---:|
| inserted_unit_count | 245 | 245 |
| inserted_region_count | 162 | 165 |
| fit_warning_count | 23 | 7 |
| body/body_flow fallback | 23 | 0 |
| product_quality | FAIL | FAIL |

说明：修补后正文不再大量截断，但局部 `table_cell`、脚注和整体视觉节奏仍未达产品质量，因此仍应停在 `S_FAIL_QUALITY` 或继续 repair loop，不能宣称通过。

### 工具一致性

- Prompt JSON 解析：PASS
- Python 工具编译：PASS
- 反过拟合扫描：`docs/reports/round11_round12_diagnosis/anti_overfit_scan_after_core_fix.json`，PASS，blocking 0

## 下一轮执行要求

1. 先跑 S5 语义校验；如果出现元描述式伪译文，停止候选生成。
2. 只有语义译文真实通过后，才进入 S6/S7 排版回填。
3. D4 必须记录 `table_cell`、`legend`、`body_flow`、`paragraph_gap_pt` 和 `line_joiner` 的当前 PDF 证据。
4. D7 如果看到“文字存在但语义是 This line reports/本行说明”，必须归类为语义真实性失败，不归类为字体或布局失败。
