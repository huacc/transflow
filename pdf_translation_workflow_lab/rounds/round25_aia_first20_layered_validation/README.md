# Round25 AIA First-20 Layered Validation

Round25 validates the layered PDF translation-backfill workflow against three cases:

1. `AIA_2020_Annual_Report_zh.pdf` pages 1-20, zh to en.
2. `AIA_2020_Annual_Report_en.pdf` pages 1-20, en to zh.
3. `00005_2025_annual_report_zh_pages_001_020.pdf`, zh to en regression from round24.

## Boundary

- Runtime inputs are under `input/`.
- Case outputs are preserved under `case_runs/<case_id>/`.
- This package must not import from `pdf_translation_workflow_core`.
- Runtime tools must not read human reference PDFs or `offline_reference_compare`.
- Translation for AIA cases is materialized inside `S5_TranslationPlan` from current-run extracted text through `google_translate_web_gtx_public_endpoint`.
- Repair decisions must use current-run source/candidate evidence only: bbox, font size, fit status, local overlap baseline, page statistics, and generated evidence.

## Main Command

```powershell
python run_round25_batch.py
```

Single-case command:

```powershell
python run_round25_layered_case.py `
  --source-pdf input/source_pdfs/AIA_2020_Annual_Report_zh_pages_001_020.pdf `
  --translations-json AUTO `
  --source-language zh `
  --target-language en `
  --case-id R25_AIA_ZH_TO_EN_pages_001_020
```

## Layered Judgement

| Layer | State | Prompt or contract | Local tool |
|---|---|---|---|
| Translation materialization | `S5` | `S5_materialize_translation.prompt.json` | `tools/translators/materialize_google_gtx_translations.py` |
| Signal normalization | `S8A` | `S8A_quality_signal_normalization.prompt.json` | `tools/judges/compare_source_candidate.py` |
| Triage | `S8B` | `S8B_quality_triage.prompt.json` | `tools/judges/compare_source_candidate.py` |
| Static dispatch | `S8C-Dispatch` | `contracts/failure_dispatch_table.json` | deterministic table lookup |
| Patch binding | `S8C-Binding` | `S8C_repair_patch_binding.prompt.json` | `tools/repairs/build_repair_patch.py` |
| Patch execution | `Lx` | `Lx_repair_loop_execution.prompt.json` | `tools/repairs/apply_repair_patch.py` |

Triage uses causal priority before count: `text_fit_overflow`, `font_size_regression`, then `cross_slot_overlap`. Repair acceptance requires selected failure improvement, total blocking-count decrease, and no non-selected hard failure regression.

## Outputs

- Batch summary: `reports/round25_batch_summary.md`
- Case reports: `case_runs/<case_id>/reports/round25_state_machine_repair_patch_report.md`
- Case verdicts: `case_runs/<case_id>/reports/round25_final_verdict.json`
- Accepted candidate PDFs: `case_runs/<case_id>/output/...`

Product quality may still fail. A failed product verdict with complete evidence is acceptable for this experiment.
