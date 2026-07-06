# SPIKE20 round13 源 PDF 穿刺验证执行提示词

## 1. 工作根

你的根工作目录必须是：

```text
D:\项目\开源项目\MerqFin\spikes\独立测试\spikes\spike20
```

所有命令必须从这个目录执行。只能使用本目录内文件作为执行证据。禁止读取父目录、历史 round/spike 输出、`round13` 语义译文 JSON、官方双语参考、人工翻译样本或前轮译文作为运行依据。

## 2. 本轮目标

本轮是基于 `docs\input\round13\source_pdfs` 的工程完备性穿刺验证。

必须验证：

```text
当前 pdf_translation_workflow_core 是否能支撑 D2 语义译文物化
当前标准流程文档是否足以指导状态迁移和工具调度
round13 的 5 个源 PDF 能否按同一套契约执行
S8 视觉闭环是否强制执行
D7 失败后 D8/Lx 是否按契约记录
最终报告是否能诚实区分 process_contract_verdict 和 product_quality_verdict
```

## 3. 输入纯度

顶层输入目录只能有 PDF：

```text
input\00005_2025_interim_report_zh.pdf
input\00388_2026_annual_report_en.pdf
input\00992_2023_annual_report_en.pdf
input\建業新生活有限公司_c.pdf
input\建業新生活有限公司_e.pdf
```

运行前必须检查并记录：

```text
input_purity = PASS only if top-level input contains PDF files only
```

不得使用：

```text
父目录 docs\input\round13\semantic_translations
父目录 docs\input\round13\semantic_translation_pool
父目录 docs\output\round13
父目录 docs\reports\round13
任何人工双语参考或历史候选 PDF
```

## 4. 冻结边界

默认不得修改：

```text
pdf_translation_workflow_core\
docs\业务流程\
docs\测试提示词\
run_request.json
SPIKE20_PACKAGE_MANIFEST.md
```

如果不做小幅修改就无法继续执行，必须进入 `Ax_AdaptiveChange`，只允许改 `spikes\spike20` 内的本轮副本，并写出：

```text
docs\reports\adaptive_change_record.json
docs\reports\change_manifest_before.json
docs\reports\change_manifest_after.json
```

最终报告必须说明这些修改是不是说明标准流程、契约或工具仍不完备。

## 5. 必读文件

执行前必须读取并在审计报告中列出：

```text
run_request.json
SPIKE20_PACKAGE_MANIFEST.md
docs\业务流程\PDF_语义翻译回填_标准流程设计.md
pdf_translation_workflow_core\README.md
pdf_translation_workflow_core\tools\README.md
pdf_translation_workflow_core\contracts\state_machine.md
pdf_translation_workflow_core\contracts\tool_contracts.md
pdf_translation_workflow_core\contracts\decision_contracts.md
pdf_translation_workflow_core\contracts\semantic_translation_contract.md
pdf_translation_workflow_core\contracts\product_quality_contract.md
pdf_translation_workflow_core\contracts\page_type_repair_matrix.md
pdf_translation_workflow_core\contracts\change_control_contract.md
pdf_translation_workflow_core\prompts\model_tool_orchestration_contract.md
pdf_translation_workflow_core\prompts\prompt_tool_bindings.json
pdf_translation_workflow_core\prompts\templates\D2_translation.prompt.json
pdf_translation_workflow_core\prompts\templates\D4_layout_plan.prompt.json
pdf_translation_workflow_core\prompts\templates\D5_D7_quality_gate.prompt.json
pdf_translation_workflow_core\prompts\templates\D8_repair_selection.prompt.json
pdf_translation_workflow_core\prompts\templates\D9_final_acceptance.prompt.json
```

## 6. 语言识别规则

`run_request.json` 里的 `source_language` 和 `target_language` 是初始化元数据，不是唯一裁决。

每个 case 必须先通过当前 PDF 提取结果确认语言：

```text
中文源文：CJK 文本占主要可翻译文本
英文源文：Latin/ASCII 词占主要可翻译文本，且不是纯代码/数字标识
```

如果提取结果和 `run_request.json` 不一致，必须记录 `language_metadata_mismatch`，并按当前提取结果决定是否继续。不能仅靠文件名后缀 `zh/en/c/e` 裁决。

## 7. 必走状态

必须按状态机执行，不能因为生成了 PDF 就跳过后续验证：

```text
S1_ContractLoad
S2_ToolProbe
S3_SourceExtract
S5_TranslationPlan
S6_LayoutPlan
S7_GenerateCandidate
S8_VerifyProductQuality
Lx_RepairLoop when D7 has blocking failures
S9_VerifyProcessContract
```

`S5_TranslationPlan` 是有界 batch loop，不是一次性整篇翻译。每个 batch 必须记录 prompt、slot、model_output、decision_record、validation 和 workspace boundary。

## 8. 推荐工具调度

### 8.1 S1/S2 预检

```powershell
python pdf_translation_workflow_core\tools\validators\validate_workspace_boundary.py --workspace-root . --path input --path docs\input --path docs\reports --path docs\output --allow-missing --out docs\reports\workspace_boundary_preflight.json
python pdf_translation_workflow_core\tools\probes\tool_probe.py --out docs\reports\tool_probe.json
```

### 8.2 S3/S5 逐 case 物化语义译文

对 `run_request.json.regressions[]` 的每个 case 执行：

```powershell
python pdf_translation_workflow_core\tools\probes\extract_pdf_structure.py --input <input_pdf> --out docs\reports\<regression_id>\source_extraction.json
python pdf_translation_workflow_core\tools\renderers\render_pdf.py --input <input_pdf> --out-dir docs\reports\<regression_id>\source_previews --prefix source --manifest docs\reports\<regression_id>\source_render_manifest.json
python pdf_translation_workflow_core\tools\planners\build_translation_batch_manifest.py --source-extraction docs\reports\<regression_id>\source_extraction.json --case-id <regression_id> --source-language <source_language> --target-language <target_language> --target-text-field <target_text_field> --batch-dir docs\reports\<regression_id>\translation_batches --out docs\reports\<regression_id>\translation_batch_manifest.json --run-id spike20 --max-units 40
```

对 `translation_batch_manifest.json.batches[]` 的每个 batch，在写入 D2 产物前先写 workspace boundary：

```powershell
python pdf_translation_workflow_core\tools\validators\validate_workspace_boundary.py --workspace-root . --path <slot_values_ref> --path <prompt_instance_ref> --path <model_output_ref> --path <batch_validation_ref> --path <decision_record_ref> --allow-missing --out <workspace_boundary_ref>
```

再执行 D2 批量物化：

```powershell
python pdf_translation_workflow_core\tools\generators\materialize_d2_translation_batches.py --manifest docs\reports\<regression_id>\translation_batch_manifest.json --prompt-template pdf_translation_workflow_core\prompts\templates\D2_translation.prompt.json --provider google_translate_web_gtx --require-workspace-boundary --cache docs\reports\<regression_id>\translation_cache.json --out docs\reports\<regression_id>\d2_materialization.json
```

如果 `google_translate_web_gtx` 不可用，可由当前 Codex 作为翻译执行者逐 batch 写入 `model_output.json`，但必须使用 `D2_translation.prompt.json` 的槽位和输出契约，不能写 placeholder、line-category pseudo translation 或仅保留源文。所有人工/模型裁决输入和输出必须落盘。

每个 batch 物化后必须校验：

```powershell
python pdf_translation_workflow_core\tools\validators\validate_translation_batch.py --slot-values <slot_values_ref> --model-output <model_output_ref> --out <batch_validation_ref>
```

所有 batch 通过后汇总并校验：

```powershell
python pdf_translation_workflow_core\tools\generators\assemble_semantic_translations.py --manifest docs\reports\<regression_id>\translation_batch_manifest.json --out <semantic_translations_output> --evidence-out docs\reports\<regression_id>\translation_assembly_evidence.json
python pdf_translation_workflow_core\tools\validators\validate_semantic_translations.py --source-extraction docs\reports\<regression_id>\source_extraction.json --translations <semantic_translations_output> --out docs\reports\<regression_id>\semantic_translation_validation.json
```

任何缺失、伪译文、placeholder、目标语脚本失败、覆盖不完整，都必须终止到：

```text
S_FAIL_CAPABILITY
```

路径越界或缺 workspace-boundary 证据，必须终止到：

```text
S_FAIL_PROCESS_CONTRACT
```

### 8.3 S6-S9 产品质量执行器

当 semantic translation JSON 都通过后，调用当前通用执行器：

```powershell
python pdf_translation_workflow_core\tools\run_semantic_product_quality_round.py --round-id S20 --source-dir input --semantic-dir docs\input\semantic_translations --input-dir docs\input --output-dir docs\output --report-dir docs\reports --max-repair-loops 1
```

执行器的候选 PDF 应输出到：

```text
docs\output\S20_00005_2025_interim_report_zh_candidate.pdf
docs\output\S20_00388_2026_annual_report_en_candidate.pdf
docs\output\S20_00992_2023_annual_report_en_candidate.pdf
docs\output\S20_建業新生活有限公司_c_candidate.pdf
docs\output\S20_建業新生活有限公司_e_candidate.pdf
```

如果实际文件名不同，不要硬改结果；在报告中说明命名偏差，并把真实路径写入 verdict。

## 9. S8 强制产物

每个 case 在候选 PDF 生成后必须有：

```text
docs\reports\<case_id>\candidate_render_manifest.json
docs\reports\<case_id>\candidate_previews\*.png
docs\reports\<case_id>\visual_region_metrics.json
docs\reports\<case_id>\visual_repair_plan.json
docs\reports\<case_id>\visual_adjudication.json
docs\reports\<case_id>\product_quality_gates.json
```

缺任意一个，最终必须是：

```text
process_contract_verdict=FAIL
terminal_state=S_FAIL_PROCESS_CONTRACT
```

## 10. D7/D8/Lx 规则

如果 `visual_adjudication.json` 或 `product_quality_gates.json` 有 blocking failure：

```text
D8_minimal_repair_selection 必须执行
D8 不允许 skipped
D8 必须输出 repair_loop_<n>.json 或 explicit unrepairable_reason
visual_repair_plan.json 不能算作 repair loop 已执行
```

如果当前工具没有对应 repair atom 执行器，必须诚实输出 `S_FAIL_QUALITY`，不能把候选 PDF 宣称为最终合格译文。

## 11. 最终输出

必须输出：

```text
docs\reports\spike20_execution_audit.md
docs\reports\spike20_final_verdict.json
```

`spike20_final_verdict.json` 必须包含：

```json
{
  "spike": "spike20",
  "input_purity": "PASS|FAIL",
  "workspace_boundary_preflight": "PASS|FAIL",
  "s5_batch_materialization": {},
  "semantic_translation_validation": {},
  "candidate_generation": "PASS|FAIL|NOT_REACHED",
  "visual_closure": "PASS|FAIL|NOT_REACHED",
  "d8_repair_selection": "PASS|FAIL|NOT_REQUIRED|NOT_REACHED",
  "framework_immutability": "PASS|FAIL",
  "adaptive_changes_made": false,
  "adaptive_change_refs": [],
  "state_machine_followed": "PASS|FAIL",
  "tool_orchestration_followed": "PASS|FAIL",
  "prompt_template_boundary_followed": "PASS|FAIL",
  "model_interaction_records_complete": "PASS|FAIL",
  "anti_overfit_scan": "PASS|FAIL",
  "process_artifact_schema_verdict": "PASS|FAIL",
  "process_contract_verdict": "PASS|FAIL",
  "product_quality_verdict": "PASS|FAIL|NOT_REACHED",
  "terminal_state": "S_DONE_PRODUCT_ACCEPTED|S_DONE_PROCESS_VALIDATED|S_FAIL_PROCESS_CONTRACT|S_FAIL_TOOLING|S_FAIL_CAPABILITY|S_FAIL_QUALITY",
  "candidate_pdfs": [],
  "design_gaps_found": []
}
```

如果发生 adaptive change，`adaptive_changes_made` 改为 `true`，并填写改动证据路径。

## 12. 审计报告要求

`spike20_execution_audit.md` 必须记录：

```text
每一步状态迁移
每个工具调用命令、输入、输出、返回结果
每个 D2 batch 的 prompt_instance、model_output、decision_record、validation
D4/D7/D8/D9 使用的提示词模板和槽位数据
所有 PASS/FAIL 门禁及其证据路径
任何小幅改动的原因、diff 摘要、影响范围
是否存在标准流程文档和真实工具不一致
```

最终报告必须明确：失败是流程契约失败、能力失败、还是产品质量失败。
