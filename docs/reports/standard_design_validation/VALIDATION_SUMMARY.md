# Standard Design Validation Summary

## Scope

This validation checks whether `docs\业务流程\PDF_语义翻译回填_标准流程设计.md` matches the current tools, contracts, and prompt templates.

It is not a round10 execution result.

## Commands Covered

The validation ran the core chain for both regression inputs:

```text
tool_probe.py
extract_pdf_structure.py
render_pdf.py
validate_semantic_translations.py
build_layout_policy.py
generate_semantic_backfill.py
evaluate_pdf_quality.py
scan_core_overfit.py
run_state_machine_selftest.py
```

## Result

```json
{
  "tool_parameter_check": "PASS",
  "prompt_json_parse": "PASS",
  "semantic_translation_validation": "PASS",
  "semantic_candidate_generation": "PASS",
  "fit_warning_count": 0,
  "anti_overfit_scan": "PASS",
  "selftest_process_contract_verdict": "PASS",
  "selftest_product_quality_verdict": "FAIL",
  "expected_product_failure_boundary": "visual_similarity_requires_visual_adjudication"
}
```

## Findings Applied

The validation found one contract mismatch: older docs referenced `layout_provider`, but the current generator emits `strategy`, `layout_policy_json`, `layout_policy_sha256`, `layout_policy_version`, and `layout_policy_source`.

The standard process design and core contracts were updated to match the actual generator evidence schema.

## Evidence

```text
docs\reports\standard_design_validation\R1_01_source_single_timeline
docs\reports\standard_design_validation\R2_AIA_pages_08_09_24_25
docs\reports\standard_design_validation\selftest\selftest_summary.json
docs\reports\standard_design_validation\anti_overfit_scan_after_contract_fix.json
```
