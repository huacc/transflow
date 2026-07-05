# Round06 Root Validation Audit

## Verdict Split

- `process_contract_verdict`: PASS
- `anti_overfit_verdict`: PASS
- `product_quality_verdict`: FAIL
- terminal state: `S_FAIL_QUALITY`

This run proves the workflow evidence package is now structurally executable and auditable. It does not claim product-quality acceptance.

## Runtime Inputs

Runtime input root: `docs/input/round06`.

- R11 zh-to-en source PDF: `docs/input/round06/round11/source_pdfs/AIA_2020_Annual_Report_zh_pages_03_08_09_24_25.pdf`
- R11 semantic translations: `docs/input/round06/round11/semantic_translations/R11_AIA_zh_pages_03_08_09_24_25.translations.json`
- R12 English random PDF: `docs/input/round06/round12/source_pdfs/AIA_2020_Annual_Report_en_pages_081_152_214_220_231.pdf`
- R12 Chinese random PDF: `docs/input/round06/round12/source_pdfs/AIA_2020_Annual_Report_zh_pages_034_036_144_228_261.pdf`

Official bilingual/reference PDFs were not used as runtime prompt input, semantic translation input, layout input, or visual adjudication input.

## Outputs

- R11 candidate PDF: `docs/output/round06/R06_R11_AIA_zh_pages_03_08_09_24_25_zh_to_en_candidate.pdf`
- R11 generation evidence: `docs/reports/round06/R06_R11_AIA_zh_pages_03_08_09_24_25_zh_to_en/candidate_generation_evidence.json`
- R11 product gates: `docs/reports/round06/R06_R11_AIA_zh_pages_03_08_09_24_25_zh_to_en/product_quality_gates.with_visual.json`
- process validation: `docs/reports/round06/process_validation.json`
- anti-overfit scan: `docs/reports/round06/anti_overfit_scan.json`
- state trace: `docs/reports/round06/state_trace.json`
- decision log: `docs/reports/round06/decision_log.jsonl`
- operation log: `docs/reports/round06/operation_log.jsonl`

No R12 candidate PDF was generated. That is intentional: product-quality mode requires real semantic translations before generation. Missing translations route to `S_FAIL_CAPABILITY`.

## R11 Product Quality Result

R11 generation succeeded with real semantic backfill:

- source units: 245
- inserted units: 245
- inserted regions: 165
- source residue gate: pass
- semantic translation preflight: pass

Product quality failed because the current layout output still has blocking visual/layout issues:

- `text_fit`: fail, `fit_warning_count=7`
- `visual_similarity`: fail
- `font_hierarchy_ratio`: fail

This failure must go through a future repair loop before any product acceptance.

## Core Boundary Changes

The reusable core now contains only generic runtime assets:

- contracts
- prompt templates and bindings
- extract/render/layout/backfill/validation tools

Moved out of core:

- historical regression fixtures: `docs/offline_reference_evaluation/archived_core_regression`
- selftest/replay harness: `docs/offline_reference_evaluation/tools/run_state_machine_selftest.py`

`scan_core_overfit.py` no longer hardcodes sample-sensitive tokens. It accepts a run-local token file outside core: `docs/reports/round06/anti_overfit_tokens.json`.

## Prompt And Decision Evidence

No external backend model call was made in this root round. `decision_log.jsonl` records `backend_model_call_made=false` and names the prompt contract used for each D1-D9 judgement. The decisions were made by the Codex executor from persisted tool artifacts and rendered evidence.

Required decisions are present:

- D1_role_classification
- D2_translation
- D3_visual_only_text
- D4_layout_plan
- D5_initial_verification
- D6_user_feedback_adjudication
- D7_similarity_gate
- D8_minimal_repair_selection
- D9_final_acceptance

## Verification Commands

```powershell
python pdf_translation_workflow_core\tools\validators\scan_core_overfit.py --root pdf_translation_workflow_core --token-file docs\reports\round06\anti_overfit_tokens.json --out docs\reports\round06\anti_overfit_scan.json
python pdf_translation_workflow_core\tools\validators\validate_process_artifacts.py --run-dir docs\reports\round06 --out docs\reports\round06\process_validation.json
```

Additional checks performed:

- Parsed all Python files under `pdf_translation_workflow_core` with `ast.parse`: 14 files, 0 syntax errors.
- Searched `pdf_translation_workflow_core` and `docs/????/PDF_????_??????.md` for current sample-specific tokens: no matches.

## Design Documents Updated

- `docs/????/PDF_????_??????.md`
- `pdf_translation_workflow_core/README.md`
- `pdf_translation_workflow_core/tools/README.md`
- core contracts and prompt bindings under `pdf_translation_workflow_core/contracts` and `pdf_translation_workflow_core/prompts`

## Remaining Gap

The process and anti-overfit framework is now cleaner, but layout quality is still not accepted. The next engineering target should be the repair loop for text-fit, font hierarchy, and table/compact-region visual similarity using only source-vs-output evidence from the current input PDF.
