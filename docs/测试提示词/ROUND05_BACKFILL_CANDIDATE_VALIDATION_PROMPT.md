# Round05 Backfill Candidate Validation Prompt

Work only inside this standalone directory:

```text
D:\项目\开源项目\MerqFin\spikes\独立测试\spikes\round05
```

Path rule: if copied docs contain historical absolute paths, treat those paths as audit evidence only. Execute relative paths from the round05 root.

## Goal

Validate that the workflow now produces a real low-fidelity Chinese backfill candidate PDF, not only a source-copy smoke candidate.

Round05 does not require final visual quality to pass.

Round05 does require:

1. complete process evidence;
2. state/tool/prompt binding conformance;
3. real candidate generation under `S7_GenerateCandidate`;
4. candidate PDF published under `docs\output`;
5. quality gates to fail truthfully on semantic or visual quality, not because no backfill happened.

## Required Reading

```text
docs\业务流程\01_source_pdf_中文回填_详细流程记录.md
pdf_translation_workflow_core\README.md
pdf_translation_workflow_core\contracts\run_modes.md
pdf_translation_workflow_core\contracts\state_machine.md
pdf_translation_workflow_core\contracts\tool_contracts.md
pdf_translation_workflow_core\contracts\decision_contracts.md
pdf_translation_workflow_core\contracts\product_quality_contract.md
pdf_translation_workflow_core\contracts\page_type_repair_matrix.md
pdf_translation_workflow_core\prompts\README.md
pdf_translation_workflow_core\prompts\prompt_manifest.json
pdf_translation_workflow_core\prompts\prompt_tool_bindings.json
pdf_translation_workflow_core\prompts\model_tool_orchestration_contract.md
pdf_translation_workflow_core\prompts\templates\*.prompt.json
pdf_translation_workflow_core\regression\regression_manifest.json
pdf_translation_workflow_core\regression\regression_matrix.md
```

## Required Execution

Run from the round05 root:

```powershell
python pdf_translation_workflow_core\tools\run_state_machine_selftest.py --modes product_quality
```

If `docs\reports` already contains older preparation reports, generate a fresh `selftest_*` run and base the audit on the newest run you created. Do not reuse an older report as the verdict.

Do not use:

```text
--generator smoke_copy
```

The default generator must be:

```text
backfill_placeholder
```

## Required Evidence

For each product-quality regression, verify these files exist:

```text
docs\reports\pdf_translation_workflow_core\selftest_*\product_quality\*\candidate_generation_evidence.json
docs\reports\pdf_translation_workflow_core\selftest_*\product_quality\*\translations.json
docs\reports\pdf_translation_workflow_core\selftest_*\product_quality\*\layout_plan.json
docs\reports\pdf_translation_workflow_core\selftest_*\product_quality\*\product_quality_gates.json
docs\reports\pdf_translation_workflow_core\selftest_*\product_quality\*\outputs\candidate.pdf
docs\output\*_backfill_placeholder_candidate.pdf
docs\output\previews\*_backfill_placeholder_page_*.png
```

`candidate_generation_evidence.json` must contain:

```json
{
  "tool": "generate_backfill_candidate",
  "real_backfill_pdf": true,
  "redacted_line_count": ">0",
  "inserted_line_count": ">0",
  "translations_json": "present",
  "layout_plan_json": "present",
  "semantic_coverage": "placeholder_not_semantic"
}
```

`product_quality_gates.json` should normally contain:

```text
text_residue: pass
backfill_generation: pass
semantic_coverage: fail
product_quality_verdict: FAIL
terminal_state: S_FAIL_QUALITY
```

If `text_residue` fails, record it as a generator defect. Do not hide it.

## Required Audit Report

Write:

```text
docs\reports\round05_backfill_candidate_audit.md
```

It must answer:

1. Did `S7_GenerateCandidate` use `generate_backfill_candidate.py`?
2. Were `translations.json` and `layout_plan.json` generated?
3. Was a candidate PDF published to `docs\output`?
4. Did the candidate PDF contain Chinese backfill text?
5. Did `text_residue` pass or fail?
6. Did `backfill_generation` pass?
7. Did `semantic_coverage` fail as expected for placeholder translation?
8. Did every tool invocation appear in `operation_log.jsonl`?
9. Did every decision D1-D9 appear in `decision_log.jsonl`?
10. Did final verdict split process success from product-quality failure?

## Expected Final Verdict

Expected successful Round05 methodology result:

```text
process_contract_verdict: PASS
product_quality_verdict: FAIL
anti_overfit_verdict: PASS
terminal_state: S_FAIL_QUALITY
generation_verdict: PASS
```

Round05 fails if no real backfill candidate PDF is produced, even if process logs pass.
