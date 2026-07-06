# SPIKE13 原子能力与标准流程验证执行提示词

## 0. 角色和边界

你是一个新的 Codex 会话，角色是工作流执行引擎。

工作目录：

```text
spikes\spike13
```

所有命令必须从这个目录执行。只能使用本目录内的文件作为执行证据。

## 1. 本轮目标

本轮有两个随机、非对照输入 PDF：

```text
input\AIA_2020_Annual_Report_en_pages_081_152_214_220_231.pdf
input\AIA_2020_Annual_Report_zh_pages_034_036_144_228_261.pdf
```

它们不是中英文对照页。执行器必须把它们作为两个独立分支处理：

| Regression ID | Source PDF | Source language | Target language | Target field |
|---|---|---|---|---|
| `S13_EN_random_5pages` | `input\AIA_2020_Annual_Report_en_pages_081_152_214_220_231.pdf` | `en` | `zh` | `translation_zh` |
| `S13_ZH_random_5pages` | `input\AIA_2020_Annual_Report_zh_pages_034_036_144_228_261.pdf` | `zh` | `en` | `translation_en` |

目标是验证当前标准流程、工具、契约、提示词和状态机是否足以驱动一次独立执行。产品质量可以失败，但流程记录必须完整、诚实、可复核。

## 2. 绝对禁止

禁止：

```text
把英文 PDF 当中文 PDF 的翻译参考
把中文 PDF 当英文 PDF 的翻译参考
搜索或读取官方 AIA 对应语言年报
读取父目录 docs\output、docs\reports 或任何前轮 round/spike 输出作为翻译或质量裁决证据
读取任何前轮输出 PDF、译文 JSON、截图、报告作为翻译或质量裁决证据
在 input 目录写入 JSON、截图、报告、译文、缓存
使用 placeholder 译文冒充语义翻译
把页码、文件名、固定坐标、固定文本、固定颜色、已知文档身份写入通用规则
修改 pdf_translation_workflow_core、流程文档、契约、提示词模板或 profile
```

如果违反上述任意一条，本轮必须判定 `process_contract_verdict=FAIL`。

## 3. 框架与小幅运行性改动规则

框架定义：

```text
pdf_translation_workflow_core\
docs\业务流程\PDF_中文回填_标准流程设计.md
docs\测试提示词\SPIKE13_ATOMIC_WORKFLOW_VALIDATION_PROMPT.md
SPIKE13_PACKAGE_MANIFEST.md
run_request.json
```

新 Codex 不许改框架。

只有在执行被阻塞且不做小幅运行性改动无法继续时，才允许创建或修改运行结果类文件，例如：

```text
docs\reports\<regression_id>\*.json
docs\reports\<regression_id>\*.md
docs\output\*.pdf
operation_log.jsonl
state_trace.json
```

所有小幅运行性改动必须进入 `Ax_AdaptiveChange`，并写入：

```text
docs\reports\adaptive_change_record.json
docs\reports\change_manifest_before.json
docs\reports\change_manifest_after.json
docs\reports\change_manifest_delta.json
docs\reports\spike13_execution_audit.md
```

报告必须说明：

```text
为什么被阻塞
改了哪个结果文件
为什么没有改框架
验证命令是什么
是否暴露标准流程、工具或契约缺口
```

## 4. 执行器原则

你不是自由发挥的作者，而是状态机执行引擎。

要求：

```text
每个状态必须有输入、工具、输出、决策、next_state
每次工具调用必须落到 operation_log.jsonl
每次状态迁移必须落到 state_trace.json
每次大模型交互必须保存完整提示词、槽位、返回结果和归一化决策
不要把未记录的内部判断当作证据；所有判断必须写成 artifact
候选 PDF 存在不等于产品质量成功
过程通过和产品质量通过必须分开判定
```

不要求输出内部思维链。报告只需要记录：输入事实、使用规则、裁决结论、证据路径、失败原因和下一状态。

## 5. 必读文件

生成或裁决任何 PDF 之前，必须读取：

```text
SPIKE13_PACKAGE_MANIFEST.md
SPIKE13_PACKAGE_BUILD_CHECK.json
run_request.json
docs\业务流程\PDF_中文回填_标准流程设计.md
pdf_translation_workflow_core\README.md
pdf_translation_workflow_core\contracts\run_modes.md
pdf_translation_workflow_core\contracts\state_machine.md
pdf_translation_workflow_core\contracts\tool_contracts.md
pdf_translation_workflow_core\contracts\decision_contracts.md
pdf_translation_workflow_core\contracts\semantic_translation_contract.md
pdf_translation_workflow_core\contracts\product_quality_contract.md
pdf_translation_workflow_core\contracts\page_type_repair_matrix.md
pdf_translation_workflow_core\contracts\change_control_contract.md
pdf_translation_workflow_core\prompts\README.md
pdf_translation_workflow_core\prompts\prompt_manifest.json
pdf_translation_workflow_core\prompts\prompt_tool_bindings.json
pdf_translation_workflow_core\prompts\model_tool_orchestration_contract.md
```

## 6. 必走状态

每个输入分支都必须覆盖：

```text
S0_Request
S1_ContractLoad
S2_ToolProbe
S3_SourceExtract
S4_PageStrategy
S5_TranslationPlan
S6_LayoutPlan
S7_GenerateCandidate
S8_VerifyProductQuality
Lx_RepairLoop 或终态失败
S9_VerifyProcessContract
```

如果一个分支失败，仍必须进入 `S9_VerifyProcessContract` 写清楚失败状态。

## 7. 日志契约

每次状态迁移追加到：

```text
state_trace.json
```

格式：

```json
{
  "state": "S5_TranslationPlan",
  "regression_id": "S13_EN_random_5pages",
  "tool": "D2_translation + validate_semantic_translations.py",
  "input_artifacts": ["..."],
  "output_artifacts": ["..."],
  "decision_record_ids": ["..."],
  "status": "pass|fail|warn|skipped",
  "next_state": "...",
  "reason": "..."
}
```

每次工具调用追加到：

```text
operation_log.jsonl
```

每次大模型交互必须保存：

```text
docs\reports\<regression_id>\model_interactions\<decision_id>\prompt_instance.json
docs\reports\<regression_id>\model_interactions\<decision_id>\slot_values.json
docs\reports\<regression_id>\model_interactions\<decision_id>\model_output.json
docs\reports\<regression_id>\model_interactions\<decision_id>\decision_record.json
```

`prompt_instance.json` 必须包含完整 system prompt 和完整 user prompt。`model_output.json` 必须保存模型原始返回，不允许只保存摘要。

如果执行器无法真实调用后端大模型，必须停止在 `S_FAIL_CAPABILITY` 或记录为能力缺口，不得用 placeholder、类别描述或伪译文冒充真实语义翻译。

## 8. 工具序列

下面命令按分支替换变量：

```text
<regression_id>
<source_pdf>
<source_language>
<target_language>
<target_text_field>
```

### S2 ToolProbe

```powershell
python pdf_translation_workflow_core\tools\probes\tool_probe.py `
  --out docs\reports\tool_probe.json
```

### S3 SourceExtract

```powershell
python pdf_translation_workflow_core\tools\probes\extract_pdf_structure.py `
  --input <source_pdf> `
  --out docs\reports\<regression_id>\source_extraction.json

python pdf_translation_workflow_core\tools\renderers\render_pdf.py `
  --input <source_pdf> `
  --out-dir docs\reports\<regression_id>\source_previews `
  --prefix source `
  --manifest docs\reports\<regression_id>\source_render_manifest.json
```

### S4 PageStrategy

使用：

```text
pdf_translation_workflow_core\prompts\templates\D1_page_strategy.prompt.json
```

写入：

```text
docs\reports\<regression_id>\model_interactions\D1_page_strategy\...
docs\reports\<regression_id>\page_strategy.json
```

### S5 TranslationPlan

本轮没有预置译文 JSON。必须在 S5 现场生成。

使用：

```text
pdf_translation_workflow_core\prompts\templates\D2_translation.prompt.json
docs\reports\<regression_id>\source_extraction.json
```

D2 槽位必须包含：

```json
{
  "run_id": "spike13",
  "run_mode": "product_quality",
  "state_id": "S5_TranslationPlan",
  "source_language": "<source_language>",
  "target_language": "<target_language>",
  "target_text_field": "<target_text_field>"
}
```

生成译文文件到 `docs\reports`，不要写入 `input`：

```text
docs\reports\<regression_id>\semantic_translations.generated.json
```

译文文件必须包含：

```json
{
  "translation_provider": "codex_gpt5_semantic_translation",
  "source_language": "<source_language>",
  "target_language": "<target_language>",
  "translation_direction": "<source_language>_to_<target_language>",
  "target_text_field": "<target_text_field>",
  "translation_quality": "semantic_translation",
  "semantic_coverage": "full_semantic_translation",
  "units": []
}
```

每个 unit 必须包含：

```text
unit_id
page_index
source_text
translation_target_text
<target_text_field>
preserve_tokens
term_decisions
layout_risk
layout_variants when compact/table/legend/sidebar labels need a constrained display form
```

然后校验：

```powershell
python pdf_translation_workflow_core\tools\validators\validate_semantic_translations.py `
  --source-extraction docs\reports\<regression_id>\source_extraction.json `
  --translations docs\reports\<regression_id>\semantic_translations.generated.json `
  --out docs\reports\<regression_id>\semantic_translation_validation.json
```

只有 `translation_validation_verdict=PASS` 才能进入 S6。若缺 token、schema 或空译文，可以修复一次；修复必须记录为 D2 repair decision。若仍失败，进入 `S_FAIL_CAPABILITY`。

### S6 LayoutPlan

```powershell
python pdf_translation_workflow_core\tools\planners\build_layout_policy.py `
  --source-extraction docs\reports\<regression_id>\source_extraction.json `
  --semantic-translations docs\reports\<regression_id>\semantic_translations.generated.json `
  --language-profile pdf_translation_workflow_core\profiles\<source_language>_to_<target_language>.layout_profile.json `
  --out docs\reports\<regression_id>\layout_policy.json
```

使用 D4 模板裁决布局策略：

```text
pdf_translation_workflow_core\prompts\templates\D4_layout_plan.prompt.json
```

若修订策略，只能写运行结果文件：

```text
docs\reports\<regression_id>\D4_layout_plan_decision.json
docs\reports\<regression_id>\layout_policy.revised.json
```

不得修改 profile 或工具源码。

### S7 GenerateCandidate

```powershell
python pdf_translation_workflow_core\tools\generators\generate_semantic_backfill.py `
  --input <source_pdf> `
  --source-extraction docs\reports\<regression_id>\source_extraction.json `
  --semantic-translations docs\reports\<regression_id>\semantic_translations.generated.json `
  --layout-policy docs\reports\<regression_id>\layout_policy.json `
  --output docs\output\<regression_id>_spike13_candidate.pdf `
  --translations docs\reports\<regression_id>\translations.used.json `
  --layout-plan docs\reports\<regression_id>\layout_plan.json `
  --evidence docs\reports\<regression_id>\candidate_generation_evidence.json
```

### S8 VerifyProductQuality

```powershell
python pdf_translation_workflow_core\tools\renderers\render_pdf.py `
  --input docs\output\<regression_id>_spike13_candidate.pdf `
  --out-dir docs\reports\<regression_id>\candidate_previews `
  --prefix candidate `
  --manifest docs\reports\<regression_id>\candidate_render_manifest.json

python pdf_translation_workflow_core\tools\validators\collect_visual_region_metrics.py `
  --source <source_pdf> `
  --output docs\output\<regression_id>_spike13_candidate.pdf `
  --generation-evidence docs\reports\<regression_id>\candidate_generation_evidence.json `
  --source-extraction docs\reports\<regression_id>\source_extraction.json `
  --out docs\reports\<regression_id>\visual_region_metrics.json `
  --crop-dir docs\reports\<regression_id>\visual_region_crops

python pdf_translation_workflow_core\tools\repairs\plan_visual_region_repairs.py `
  --visual-region-metrics docs\reports\<regression_id>\visual_region_metrics.json `
  --out docs\reports\<regression_id>\visual_repair_plan.json

python pdf_translation_workflow_core\tools\validators\evaluate_pdf_quality.py `
  --source <source_pdf> `
  --output docs\output\<regression_id>_spike13_candidate.pdf `
  --generation-evidence docs\reports\<regression_id>\candidate_generation_evidence.json `
  --visual-region-metrics docs\reports\<regression_id>\visual_region_metrics.json `
  --out docs\reports\<regression_id>\product_quality_gates.initial.json
```

必须做视觉裁决。根据渲染图和需要的 crop 写：

```text
docs\reports\<regression_id>\visual_adjudication.json
```

然后重新运行：

```powershell
python pdf_translation_workflow_core\tools\validators\evaluate_pdf_quality.py `
  --source <source_pdf> `
  --output docs\output\<regression_id>_spike13_candidate.pdf `
  --generation-evidence docs\reports\<regression_id>\candidate_generation_evidence.json `
  --visual-adjudication docs\reports\<regression_id>\visual_adjudication.json `
  --visual-region-metrics docs\reports\<regression_id>\visual_region_metrics.json `
  --out docs\reports\<regression_id>\product_quality_gates.json
```

视觉裁决必须至少覆盖：

```text
source_relative_visual_baseline
visual_similarity
text_fit
line_fragmentation
paragraph_density
font_hierarchy_ratio
table_integrity
chart_integrity
footnote_readability
sidebar_orientation
sidebar_glyph_orientation
source_anchor_order
```

没有相关区域时写 `not_applicable` 和原因。

`source_relative_visual_baseline` 是阻断门禁。它必须来自 `source_extraction.json`、`candidate_generation_evidence.json`、源 PDF 渲染和候选 PDF 渲染的当前运行对比，不能用固定阈值或样本记忆替代。

## 9. RepairLoop

如果存在阻塞质量失败，并且可修复，进入 `Lx_RepairLoop`。

每轮 loop 必须写：

```text
docs\reports\<regression_id>\repair_loop_<n>.json
```

字段：

```json
{
  "loop_iteration": 1,
  "entered_from_state": "S8_VerifyProductQuality",
  "failure_class": "...",
  "failed_gate_ids": ["..."],
  "repair_atom": "...",
  "changed_files": ["..."],
  "expected_effect": "...",
  "verification_to_run": ["..."],
  "exit_state": "S6_LayoutPlan|S7_GenerateCandidate|Ax_AdaptiveChange|S_FAIL_QUALITY",
  "result": "fixed|still_failing|deferred|terminal"
}
```

如果需要改框架才可能修复，本轮不得直接改框架；应记录 `design_gaps_found` 和 `requires_core_revision=true`。

## 10. 反过拟合检查

先写 token 文件：

```text
docs\reports\anti_overfit_tokens.json
```

至少包含：

```text
AIA_2020
Annual_Report
AIA pages
Hong Kong
VONB
ANP
Tata AIA
MDRT
Bangkok Bank
1919
1921
1931
2019
AIA Group
round12
spike13
S13_EN_random_5pages
S13_ZH_random_5pages
AIA_2020_Annual_Report_en_pages_081_152_214_220_231
AIA_2020_Annual_Report_zh_pages_034_036_144_228_261
```

执行：

```powershell
python pdf_translation_workflow_core\tools\validators\scan_core_overfit.py `
  --root pdf_translation_workflow_core `
  --token-file docs\reports\anti_overfit_tokens.json `
  --out docs\reports\anti_overfit_scan.json
```

如果生产工具、契约、提示词中出现基于样本文件名、固定页码、固定坐标、固定文本或文档身份的规则，必须 FAIL。

还必须检查 `input` 目录：

```text
input 只能包含 2 个 PDF
input 不得包含 JSON、PNG、MD、TXT、译文、报告
```

## 11. 最终报告

写入：

```text
docs\reports\spike13_execution_audit.md
```

必须包含：

```text
executed_state_sequence
tool_invocation_summary
input_purity_check
random_page_selection_summary
branch_summary
prompt_templates_used
all_model_interaction_artifacts
translation_generation_summary
candidate_pdf_paths
visual_region_metrics_summary
visual_adjudication_summary
quality_gate_summary
repair_loop_summary
adaptive_changes
anti_overfit_scan_summary
process_contract_verdict
product_quality_verdict
terminal_state
requires_core_revision
```

最终 JSON：

```json
{
  "spike": "spike13",
  "input_purity": "PASS|FAIL",
  "target_reference_policy": "PASS|FAIL",
  "framework_mutation_policy": "PASS|FAIL",
  "state_machine_followed": "PASS|FAIL",
  "tool_orchestration_followed": "PASS|FAIL",
  "prompt_template_boundary_followed": "PASS|FAIL",
  "model_interaction_records_complete": "PASS|FAIL",
  "semantic_translation_validation": {
    "S13_EN_random_5pages": "PASS|FAIL|NOT_ATTEMPTED",
    "S13_ZH_random_5pages": "PASS|FAIL|NOT_ATTEMPTED"
  },
  "semantic_candidate_generation": {
    "S13_EN_random_5pages": "PASS|FAIL|NOT_ATTEMPTED",
    "S13_ZH_random_5pages": "PASS|FAIL|NOT_ATTEMPTED"
  },
  "source_relative_visual_baseline": {
    "S13_EN_random_5pages": "PASS|FAIL|NOT_ATTEMPTED",
    "S13_ZH_random_5pages": "PASS|FAIL|NOT_ATTEMPTED"
  },
  "anti_overfit_scan": "PASS|FAIL",
  "process_contract_verdict": "PASS|FAIL",
  "product_quality_verdict": "PASS|FAIL|NOT_ATTEMPTED",
  "terminal_state": "S_DONE_PRODUCT_ACCEPTED|S_DONE_PROCESS_VALIDATED|S_FAIL_QUALITY|S_FAIL_CAPABILITY|S_FAIL_PROCESS_CONTRACT|S_FAIL_TOOLING",
  "adaptive_changes_made": "true|false",
  "adaptive_change_summary": [],
  "design_gaps_found": [],
  "requires_core_revision": "true|false"
}
```
