# ROUND09 Semantic Backfill Product Validation Prompt

## 0. Execution Boundary

Root workspace is the current round directory.

All commands must run from the round root. Use relative paths for inputs, outputs, logs, and reports.

This round validates real translated-text backfill, not placeholder backfill mechanics.

## 1. Goal

Produce and audit semantic Chinese backfill candidates for the regression inputs using:

```powershell
python pdf_translation_workflow_core\tools\run_state_machine_selftest.py `
  --modes product_quality `
  --generator semantic_backfill `
  --semantic-translations-dir docs\input\semantic_translations
```

Expected published PDF names use:

```text
<regression_id>_semantic_backfill_candidate.pdf
```

Any output name containing `placeholder` is not an acceptable product candidate for this round.

## 2. Required Package Files

Read these before acting:

```text
docs\业务流程\01_source_pdf_中文回填_详细流程记录.md
pdf_translation_workflow_core\README.md
pdf_translation_workflow_core\contracts\run_modes.md
pdf_translation_workflow_core\contracts\state_machine.md
pdf_translation_workflow_core\contracts\tool_contracts.md
pdf_translation_workflow_core\contracts\decision_contracts.md
pdf_translation_workflow_core\contracts\semantic_translation_contract.md
pdf_translation_workflow_core\contracts\product_quality_contract.md
pdf_translation_workflow_core\prompts\model_tool_orchestration_contract.md
pdf_translation_workflow_core\prompts\prompt_manifest.json
pdf_translation_workflow_core\prompts\prompt_tool_bindings.json
pdf_translation_workflow_core\prompts\templates\D2_translation.prompt.json
```

## 3. Mandatory State Path

Use this state path:

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
Lx_RepairLoop or S_DONE_PRODUCT_ACCEPTED or S_FAIL_QUALITY
S9_VerifyProcessContract
```

If semantic translations are missing or invalid, stop the product branch at:

```text
S_FAIL_CAPABILITY
```

Do not fall back to:

```text
backfill_candidate_validation
backfill_placeholder
generate_backfill_candidate.py
中文回填
中文标题
中文标签
```

## 4. Translation Preparation

Before product-quality generation, create one file per regression input:

```text
docs\input\semantic_translations\<regression_id>.translations.json
```

Use the current run's extracted English units as input. Every extracted line with `ascii_tokens` requires one corresponding translation unit.

The translation JSON must satisfy:

```json
{
  "translation_provider": "codex_gpt5_manual_semantic_translation",
  "translation_quality": "semantic_translation",
  "semantic_coverage": "full_semantic_translation",
  "prompt_artifacts": [
    {
      "prompt_instance": "docs/reports/translation_prompts/<regression_id>/prompt_instance.json",
      "slot_values": "docs/reports/translation_prompts/<regression_id>/slot_values.json",
      "model_output": "docs/reports/translation_prompts/<regression_id>/model_output.json",
      "decision_record": "docs/reports/translation_prompts/<regression_id>/decision_record.json"
    }
  ],
  "coverage": {
    "source_unit_count": 0,
    "translated_unit_count": 0,
    "missing_unit_ids": []
  },
  "units": []
}
```

For every unit, include:

```json
{
  "unit_id": "...",
  "page_index": 0,
  "source_text": "...",
  "translation_zh": "...",
  "preserve_tokens": ["numbers/dates/footnotes/currency from source"],
  "term_decisions": [],
  "layout_risk": "low|medium|high"
}
```

## 5. Prompt Evidence Requirements

For each regression input, persist:

```text
docs\reports\translation_prompts\<regression_id>\prompt_instance.json
docs\reports\translation_prompts\<regression_id>\slot_values.json
docs\reports\translation_prompts\<regression_id>\model_output.json
docs\reports\translation_prompts\<regression_id>\decision_record.json
```

`prompt_instance.json` must include:

- system prompt;
- filled user prompt;
- provider/model identifier;
- timestamp.

`slot_values.json` must include:

- regression id;
- source PDF ref;
- source extraction ref;
- all translation units sent for translation.

`model_output.json` must include:

- raw structured translation output.

`decision_record.json` must include:

- verdict;
- provider;
- source unit count;
- translated unit count;
- missing unit ids;
- invalid unit ids if any;
- next state.

## 6. Mandatory Validation Commands

After each semantic translation JSON is created, run:

```powershell
python pdf_translation_workflow_core\tools\validators\validate_semantic_translations.py `
  --source-extraction <current_source_extraction.json> `
  --translations docs\input\semantic_translations\<regression_id>.translations.json `
  --out docs\reports\translation_prompts\<regression_id>\semantic_translation_validation.json
```

Only `translation_validation_verdict: PASS` can proceed to semantic backfill generation.

Then run:

```powershell
python pdf_translation_workflow_core\tools\run_state_machine_selftest.py `
  --modes product_quality `
  --generator semantic_backfill `
  --semantic-translations-dir docs\input\semantic_translations
```

## 7. Quality Interpretation

A round can fail visual/product quality and still be useful.

However, it must not fail because of:

- placeholder translations;
- missing semantic translation JSON;
- `translation_provider: deterministic_placeholder`;
- `semantic_coverage: placeholder_not_semantic`;
- output PDF generated by `backfill_placeholder`.

If those occur, mark:

```json
{
  "semantic_backfill_target_verdict": "FAIL",
  "failure_boundary": "translation_preparation_or_wrong_generator"
}
```

If semantic translations are valid and candidate PDFs are generated, but text fit or visual gates fail, mark:

```json
{
  "semantic_backfill_target_verdict": "PASS",
  "product_quality_verdict": "FAIL",
  "failure_boundary": "layout_or_visual_quality"
}
```

`visual_similarity` is blocking. If no source-vs-output PNG adjudication record says `PASS`, product quality must remain `FAIL` even when semantic backfill succeeded.

## 8. Final Audit Report

Write:

```text
docs\reports\round09_execution_audit.md
```

Required final JSON block:

```json
{
  "round": "round09",
  "run_mode": "product_quality",
  "generator": "semantic_backfill",
  "semantic_translation_files_created": "PASS|FAIL",
  "semantic_translation_validation": "PASS|FAIL",
  "semantic_candidate_generation": "PASS|FAIL",
  "placeholder_absence": "PASS|FAIL",
  "process_contract_verdict": "PASS|FAIL",
  "product_quality_verdict": "PASS|FAIL|NOT_ATTEMPTED",
  "semantic_backfill_target_verdict": "PASS|FAIL",
  "terminal_state": "S_DONE_PRODUCT_ACCEPTED|S_FAIL_QUALITY|S_FAIL_CAPABILITY|S_FAIL_PROCESS_CONTRACT|S_FAIL_TOOLING",
  "requires_core_revision": true
}
```

## 9. Failure Honesty

Do not claim high-quality final delivery unless `product_quality_verdict: PASS`.

Do claim semantic translated-text backfill only if:

- semantic translation JSON exists for each target input;
- semantic validation passes;
- candidate generation uses `generate_semantic_backfill.py`;
- candidate PDF is published;
- generation evidence says `translation_quality: semantic_translation`;
- generation evidence says `semantic_coverage: full_semantic_translation`.
