# Round23 State-Machine Flow Run Report

## Input

- Source PDF: `input/source_pdfs/00005_2025_annual_report_zh_pages_001_020.pdf`
- Semantic translations: `input/semantic_translations/R22_GEN_ZH_TO_EN_00005_pages_001_020.translations.json`
- Case ID: `R23_GEN_ZH_TO_EN_00005_pages_001_020`

## Verdict

- Process contract: `PASS`
- Product quality: `FAIL`
- Terminal state: `S_FAIL_QUALITY`
- Blocking failure count: `80`
- Selected repair family: `vertical_flow_relayout`

## Notes

- This run uses the new state-machine design document as orchestration guidance.
- Runtime layout/generation tools are inherited from round22.
- Translation model and visual model were not invoked; this is explicitly recorded in `model_interactions.jsonl`.
- Because the inherited round22 package has repair selection but no generic RepairPatch executor, a product-quality failure enters `Lx_RepairLoop` and terminates honestly at `S_FAIL_QUALITY`.
