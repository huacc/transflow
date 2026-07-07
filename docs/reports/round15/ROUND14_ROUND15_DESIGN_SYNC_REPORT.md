# Round14/Round15 Design Sync Report

## Scope

This report records the workflow-core updates made after running the 00005 annual-report bidirectional translation tests.

Runtime sources:

- EN to ZH source: `docs/input/round14/source_pdfs/00005_2025_annual_report_en.pdf`
- ZH to EN source: `docs/input/round15/source_pdfs/00005_2025_annual_report_zh.pdf`

Offline references were used only after candidate generation for diagnosis:

- ZH reference: `样本/测试数据/00005_2025_annual_report_zh.pdf`
- EN reference: `样本/测试数据/00005_2025_annual_report_en.pdf`

## Core Changes Synchronized

The following runtime rules are now reflected in code, prompts, contracts, and the standard workflow document:

1. `build_translation_batch_manifest.py` owns the shared `line_is_translatable` semantics.
2. `validate_semantic_translations.py`, `build_layout_policy.py`, and `generate_semantic_backfill.py` must use the same required-unit boundary as the manifest.
3. `materialize_d2_translation_batches.py` may clean CJK residue only for Latin-dominant mixed identifiers/entities in EN targets by generic character-ratio and token-shape rules.
4. `generate_semantic_backfill.py` repairs abnormal high text bboxes caused by extractor-merged trailing decorative numerals using current-page geometry and neighbor rhythm.
5. Dense/matrix page hard-disable remains default for normal body flow, but geometry-proven page-top large headings and lead body may reflow.
6. `run_semantic_product_quality_round.py` uses idempotent same-path copy behavior for source PDFs and semantic translation JSON files.
7. Offline reference comparison stays outside `pdf_translation_workflow_core` under `docs/offline_reference_evaluation`.

## Updated Design Artifacts

- `docs/业务流程/PDF_语义翻译回填_标准流程设计.md`
- `pdf_translation_workflow_core/tools/README.md`
- `pdf_translation_workflow_core/prompts/prompt_tool_bindings.json`
- `pdf_translation_workflow_core/prompts/templates/D2_translation.prompt.json`
- `pdf_translation_workflow_core/prompts/templates/D4_layout_plan.prompt.json`
- `pdf_translation_workflow_core/prompts/templates/D5_D7_quality_gate.prompt.json`
- `pdf_translation_workflow_core/prompts/templates/D8_repair_selection.prompt.json`
- `pdf_translation_workflow_core/contracts/page_type_repair_matrix.md`
- `pdf_translation_workflow_core/contracts/product_quality_contract.md`
- `pdf_translation_workflow_core/contracts/state_machine.md`
- `pdf_translation_workflow_core/contracts/tool_contracts.md`

## Validation Run

- Prompt/profile JSON parse: PASS
- Core Python source compile via `compile()`: PASS
- Anti-overfit scan with run-local sample token file: PASS
  - token file: `docs/reports/round15/anti_overfit_tokens_round14_15.json`
  - scan output: `docs/reports/round15/anti_overfit_scan_round14_15.json`
  - blocking_hit_count: 0

## Candidate Outputs

- `docs/output/round14/R14_00005_2025_annual_report_en_candidate.pdf`
- `docs/output/round15/R15_00005_2025_annual_report_zh_candidate.pdf`

## Final Verdicts

Round14:

- process_contract_verdict: PASS
- semantic_translation_verdict: PASS
- generation_verdict: PASS
- product_quality_verdict: FAIL
- terminal_state: S_FAIL_QUALITY

Round15:

- process_contract_verdict: PASS
- semantic_translation_verdict: PASS
- generation_verdict: PASS
- product_quality_verdict: FAIL
- terminal_state: S_FAIL_QUALITY

## Honest Boundary

Both candidates are real semantic-backfill outputs, not placeholders. They are not accepted final products because product-quality gates still fail on full-document visual/layout dimensions. The current workflow now records the failure classes and repair mappings more completely, but it still does not include a generic executor for every possible repair atom across a full annual report.

The offline reference comparison reports are diagnostic only:

- `docs/reports/round14/posthoc_reference_layout_comparison_zh.json`
- `docs/reports/round15/posthoc_reference_layout_comparison_en.json`

They must not be used as runtime prompt input, layout input, or quality-gate input.
