# Round23 State-Machine Flow Package

This directory is an isolated experiment package derived from `round22_table_layout`.

## Objective

Run the round22 table/layout tools under the new state-first process design:

`docs/设计/PDF_语义翻译回填_状态机与工具编排设计.md`

The goal is to verify whether the new S0-S9/Lx/Ax orchestration can describe and audit the existing round22 execution path without mutating `pdf_translation_workflow_core`.

## Boundary

- Runtime inputs are only under `input/`.
- Runtime outputs are only under `reports/`, `output/`, and `previews/`.
- Offline references under `offline_reference_compare/` are for human review only and must not be consumed by runtime tools.
- This package must not import from `pdf_translation_workflow_core`.
- The package inherits round22 layout tools; it does not claim new layout-quality capability by itself.

## Main Command

```powershell
python run_round23_state_machine_flow.py
```

Default input:

- `input/source_pdfs/00005_2025_annual_report_zh_pages_001_020.pdf`
- `input/semantic_translations/R22_GEN_ZH_TO_EN_00005_pages_001_020.translations.json`

Default output:

- `output/R23_GEN_ZH_TO_EN_00005_pages_001_020_candidate.pdf`

## State Mapping

Round23 uses the new design's top-level state names:

- `S0_Request`
- `S1_ContractLoad`
- `S2_ToolProbe`
- `S3_SourceExtract`
- `S4_PageStrategy`
- `S5_TranslationPlan`
- `S6_LayoutPlan`
- `S7_GenerateCandidate`
- `S8_VerifyProductQuality`
- `Lx_RepairLoop`
- `S9_VerifyProcessContract`

The inherited round22 tools still provide the actual extraction, planning, generation, quality gate, and repair selection behavior.

## Current Known Limitation

The inherited tool package can select a repair family but cannot apply a generic `RepairPatch` and rerun the layout loop. Therefore a product-quality failure should enter `Lx_RepairLoop` and terminate honestly at `S_FAIL_QUALITY`.
