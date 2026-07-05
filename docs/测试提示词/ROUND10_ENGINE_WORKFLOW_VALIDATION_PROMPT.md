# ROUND10 Engine Workflow Validation Prompt

## 0. Role And Root Boundary

You are a fresh Codex session acting as an execution engine for the packaged workflow.

Root workspace:

```text
spikes\round10
```

Run all commands from this round root. Use relative paths inside this package. Do not use output PDFs, reports, gate files, or screenshots from earlier rounds as acceptance evidence.

## 1. Mission

Validate whether the workflow design in:

```text
docs\业务流程\PDF_中文回填_标准流程设计.md
```

is executable and sufficiently complete to guide:

```text
state transitions
tool scheduling
model-judgement prompts
layout repair loops
product-quality gates
anti-overfit checks
final audit reporting
```

Product-quality success is not required. Honest failure with complete traceability is acceptable.

## 2. Mandatory Contract Load

Before generating or judging any PDF, read these files:

```text
ROUND10_PACKAGE_MANIFEST.md
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

The state-to-tool orchestration source of truth is:

```text
pdf_translation_workflow_core\prompts\prompt_tool_bindings.json
docs\业务流程\PDF_中文回填_标准流程设计.md
```

Before executing `S0_Request`, inspect these process-design sections and use them as the transition source of truth:

```text
docs\业务流程\PDF_中文回填_标准流程设计.md section 4.1 主状态机图
docs\业务流程\PDF_中文回填_标准流程设计.md section 4.2 Loop 与主状态机调用关系图
docs\业务流程\PDF_中文回填_标准流程设计.md section 4.3 Loop 内部状态图
docs\业务流程\PDF_中文回填_标准流程设计.md section 15 状态机详细设计
docs\业务流程\PDF_中文回填_标准流程设计.md section 16 活动流详细设计
```

The table in section 4 is an index. The diagrams and section 15 define the transition semantics. If a table row, tool instruction, or local judgement conflicts with the diagrams, follow the diagrams/section 15 and record the conflict as a process-contract issue.

## 3. Prompt Boundary

Do not invent a new judgement prompt framework.

Use the existing prompt templates:

```text
pdf_translation_workflow_core\prompts\templates\D1_page_strategy.prompt.json
pdf_translation_workflow_core\prompts\templates\D2_translation.prompt.json
pdf_translation_workflow_core\prompts\templates\D4_layout_plan.prompt.json
pdf_translation_workflow_core\prompts\templates\D5_D7_quality_gate.prompt.json
pdf_translation_workflow_core\prompts\templates\D8_repair_selection.prompt.json
pdf_translation_workflow_core\prompts\templates\D9_final_acceptance.prompt.json
```

Allowed adjustments:

```text
round-local file paths
run_id and regression_id values
page indexes and crop references
current metrics and gate summaries
JSON wrapper/field-order adaptation needed to read tool output
```

Forbidden adjustments:

```text
replace D4/D7/D8/D9 with new prompt text
delete required quality dimensions
turn blocking gates into non-blocking gates
accept placeholder translations as product-quality success
pass sidebar_glyph_orientation without back-rotated crop evidence
encode sample filenames, fixed page numbers, fixed coordinates, exact text, colors, or known document identity as rules
```

If a template is insufficient, enter `Ax_AdaptiveChange`; do not silently change it.

## 4. Required State Path

Execute and record this state path:

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
Lx_RepairLoop or S9_VerifyProcessContract or terminal failure
S9_VerifyProcessContract
```

This list is a required coverage path, not a flat serial script. Follow the transition rules in process-design sections 4.1, 4.2, 4.3, 15 and 16:

```text
S8_VerifyProductQuality is the only normal entry into Lx_RepairLoop.
Lx_RepairLoop is a composite loop, not a one-step state.
Each Lx iteration must classify one blocking failure, select one repair atom, write one repair_loop_<n>.json, apply a minimal change, and verify the target gate.
Lx exits to S6 when layout policy must change.
Lx exits to S7 when the existing policy is still valid and only candidate regeneration is needed.
Lx exits to Ax_AdaptiveChange only when the current tools/contracts/prompts cannot express the required repair.
Ax_AdaptiveChange must return to S6 or S8 only after before/after manifests and verification are recorded.
Any terminal tooling/capability/quality failure still flows into S9 for final process audit.
```

Every transition must append to:

```text
state_trace.json
operation_log.jsonl
```

Each entry must include:

```json
{
  "state": "...",
  "tool": "...",
  "input_artifacts": ["..."],
  "output_artifacts": ["..."],
  "decision_record_ids": ["..."],
  "status": "pass|fail|warn|skipped",
  "next_state": "...",
  "reason": "..."
}
```

## 5. Inputs

Target inputs:

```text
01_source.pdf
测试数据\AIA_2020_Annual_Report_en_pages_08_09_24_25.pdf
```

Semantic translation inputs:

```text
docs\input\semantic_translations\R1_01_source_single_timeline.translations.json
docs\input\semantic_translations\R2_AIA_pages_08_09_24_25.translations.json
```

Validate these translation files before using them. If validation fails, stop that product branch at `S_FAIL_CAPABILITY` unless a minimal round-local correction is made and recorded.

## 6. Mandatory Tool Sequence

Use the workflow tools in this order unless a documented state transition says otherwise.

### S2 Tool Probe

```powershell
python pdf_translation_workflow_core\tools\probes\tool_probe.py `
  --out docs\reports\tool_probe.json
```

### S3 Source Extract And Render

For each regression input, run extraction and render. Use run-specific report directories, for example:

```powershell
python pdf_translation_workflow_core\tools\probes\extract_pdf_structure.py `
  --input 01_source.pdf `
  --out docs\reports\R1_01_source_single_timeline\source_extraction.json

python pdf_translation_workflow_core\tools\renderers\render_pdf.py `
  --input 01_source.pdf `
  --out-dir docs\reports\R1_01_source_single_timeline\source_previews `
  --prefix source `
  --manifest docs\reports\R1_01_source_single_timeline\source_render_manifest.json
```

Repeat for:

```text
测试数据\AIA_2020_Annual_Report_en_pages_08_09_24_25.pdf
```

### S5 Semantic Translation Validation

For each regression input:

```powershell
python pdf_translation_workflow_core\tools\validators\validate_semantic_translations.py `
  --source-extraction docs\reports\<regression_id>\source_extraction.json `
  --translations docs\input\semantic_translations\<regression_id>.translations.json `
  --out docs\reports\<regression_id>\semantic_translation_validation.json
```

Only `translation_validation_verdict=PASS` may continue into product-quality generation.

### S6 Layout Policy

For each regression input:

```powershell
python pdf_translation_workflow_core\tools\planners\build_layout_policy.py `
  --source-extraction docs\reports\<regression_id>\source_extraction.json `
  --semantic-translations docs\input\semantic_translations\<regression_id>.translations.json `
  --out docs\reports\<regression_id>\layout_policy.json
```

If D4 judgement revises layout policy, write:

```text
docs\reports\<regression_id>\D4_layout_plan_decision.json
docs\reports\<regression_id>\layout_policy.revised.json
```

The generator must consume the final policy file used for generation.

### S7 Semantic Candidate Generation

Use product-quality semantic generation only:

```powershell
python pdf_translation_workflow_core\tools\generators\generate_semantic_backfill.py `
  --input <source_pdf> `
  --source-extraction docs\reports\<regression_id>\source_extraction.json `
  --semantic-translations docs\input\semantic_translations\<regression_id>.translations.json `
  --layout-policy docs\reports\<regression_id>\layout_policy.json `
  --output docs\output\<regression_id>_round10_semantic_backfill_candidate.pdf `
  --translations docs\reports\<regression_id>\translations.used.json `
  --layout-plan docs\reports\<regression_id>\layout_plan.json `
  --evidence docs\reports\<regression_id>\candidate_generation_evidence.json
```

Do not use `generate_backfill_candidate.py` for product-quality success.

### S8 Product Quality

Render candidate:

```powershell
python pdf_translation_workflow_core\tools\renderers\render_pdf.py `
  --input docs\output\<regression_id>_round10_semantic_backfill_candidate.pdf `
  --out-dir docs\reports\<regression_id>\candidate_previews `
  --prefix candidate `
  --manifest docs\reports\<regression_id>\candidate_render_manifest.json
```

Run quality gates:

```powershell
python pdf_translation_workflow_core\tools\validators\evaluate_pdf_quality.py `
  --source <source_pdf> `
  --output docs\output\<regression_id>_round10_semantic_backfill_candidate.pdf `
  --out docs\reports\<regression_id>\product_quality_gates.json `
  --generation-evidence docs\reports\<regression_id>\candidate_generation_evidence.json
```

If a visual gate is suspected or required, create focused source-vs-output crops using:

```powershell
python pdf_translation_workflow_core\tools\renderers\render_source_output_crop.py `
  --source <source_pdf> `
  --output docs\output\<regression_id>_round10_semantic_backfill_candidate.pdf `
  --page-index <zero_based_page_index> `
  --crop "<x0,y0,x1,y1>" `
  --out docs\reports\<regression_id>\compare\<crop_name>_source_vs_output.png `
  --manifest docs\reports\<regression_id>\compare\<crop_name>_source_vs_output.json
```

For side navigation glyph-orientation checks, also include:

```powershell
  --backrotate-output-degrees -90 `
  --backrotate-output-out docs\reports\<regression_id>\compare\<crop_name>_backrotated_output.png
```

Then write:

```text
docs\reports\<regression_id>\visual_adjudication.json
```

and rerun `evaluate_pdf_quality.py` with:

```powershell
  --visual-adjudication docs\reports\<regression_id>\visual_adjudication.json
```

## 7. Repair Loop Rules

If `product_quality_gates.json` reports blocking failures and the failure is repairable, enter `Lx_RepairLoop`.

Use:

```text
pdf_translation_workflow_core\contracts\page_type_repair_matrix.md
docs\业务流程\PDF_中文回填_标准流程设计.md sections 4.2, 4.3, 9, 10 and 15.2
```

Each loop must create:

```text
docs\reports\<regression_id>\repair_loop_<n>.json
```

Required fields:

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

If you modify tools/contracts/prompts/process docs to keep execution moving, this is `Ax_AdaptiveChange`.

Create:

```text
docs\reports\adaptive_change_record.json
docs\reports\change_manifest_before.json
docs\reports\change_manifest_after.json
```

and describe the change in the final audit report. Small changes are allowed only when they preserve existing required dimensions and state semantics.

## 8. Anti-Overfit Check

Before final acceptance, run:

```powershell
python pdf_translation_workflow_core\tools\validators\scan_core_overfit.py `
  --root pdf_translation_workflow_core `
  --out docs\reports\anti_overfit_scan.json
```

Acceptance requirement:

```text
blocking_hit_count == 0
```

Hits under `regression` are evidence-only. Hits in production `tools`, `contracts`, or `prompts` are process-contract failures unless fixed and recorded.

## 9. Process Validation

Run:

```powershell
python pdf_translation_workflow_core\tools\validators\validate_process_artifacts.py `
  --run-dir . `
  --out docs\reports\process_validation.json
```

If `run_state_machine_selftest.py` is used, treat it as a selftest harness, not as the full execution engine:

```powershell
python pdf_translation_workflow_core\tools\run_state_machine_selftest.py `
  --modes product_quality `
  --generator semantic_backfill `
  --semantic-translations-dir docs\input\semantic_translations `
  --process-doc docs\业务流程\PDF_中文回填_标准流程设计.md `
  --out-dir docs\reports\selftest
```

The selftest may support evidence, but it does not replace the state trace and final audit you must write.

## 10. Final Report

Write:

```text
docs\reports\round10_execution_audit.md
```

The final report must include:

```text
executed_state_sequence
tool_invocation_summary
prompt_templates_used
prompt_slot_values_summary
model_decision_records
state_machine_diagram_conformance
loop_transition_summary
candidate_pdf_paths
quality_gate_summary
repair_loop_summary
adaptive_changes
anti_overfit_scan_summary
process_contract_verdict
product_quality_verdict
terminal_state
requires_core_revision
```

Required final JSON block:

```json
{
  "round": "round10",
  "run_mode": "product_quality",
  "state_machine_followed": "PASS|FAIL",
  "state_machine_diagram_conformance": "PASS|FAIL",
  "loop_transition_conformance": "PASS|FAIL|NOT_ENTERED",
  "tool_orchestration_followed": "PASS|FAIL",
  "prompt_template_boundary_followed": "PASS|FAIL",
  "semantic_translation_validation": "PASS|FAIL",
  "semantic_candidate_generation": "PASS|FAIL",
  "anti_overfit_scan": "PASS|FAIL",
  "process_contract_verdict": "PASS|FAIL",
  "product_quality_verdict": "PASS|FAIL|NOT_ATTEMPTED",
  "terminal_state": "S_DONE_PRODUCT_ACCEPTED|S_DONE_PROCESS_VALIDATED|S_FAIL_QUALITY|S_FAIL_CAPABILITY|S_FAIL_PROCESS_CONTRACT|S_FAIL_TOOLING",
  "adaptive_changes_made": true,
  "adaptive_change_summary": [],
  "design_gaps_found": [],
  "requires_core_revision": true
}
```

## 11. Honesty Rules

Do not claim product-quality success unless product gates and visual adjudication support it.

Do not hide failures by changing the terminal state.

Do not say a tool was followed unless the artifact it should produce exists.

Do not mark `sidebar_glyph_orientation=PASS` without a back-rotated output crop when side navigation is in scope.

Do not mark anti-overfit PASS unless `docs\reports\anti_overfit_scan.json` exists and has zero blocking hits.

If you had to adjust anything, report exactly what changed, why it changed, and whether the core workflow should be revised.
