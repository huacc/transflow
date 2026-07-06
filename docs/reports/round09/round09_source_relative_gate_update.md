# Round09 Source-Relative Gate Update

## Purpose

User challenge: product-quality gates must be formed by comparing the source PDF with the translated candidate PDF, not by overfitted rules.

Decision: yes. Fixed numeric values may only be generic readability floors. Product-quality gates must first prove that every generated text region has a current-run source baseline, then judge candidate output relative to that source baseline.

## Assumptions

- Runtime input may be any PDF page set, not only the AIA samples.
- Official bilingual reports are reference-only for post-run layout comparison, not runtime inputs.
- Candidate PDF existence is not product success.
- Product-quality mode must fail when source-relative evidence is missing.

## Modified Core Artifacts

| Artifact | Change |
|---|---|
| `pdf_translation_workflow_core/tools/validators/collect_visual_region_metrics.py` | Added source-extraction-based region baseline fields and `source_relative_visual_baseline` role gate. |
| `pdf_translation_workflow_core/contracts/tool_contracts.md` | Required `--source-extraction` for region-level visual metrics. |
| `pdf_translation_workflow_core/contracts/product_quality_contract.md` | Added `source_relative_visual_baseline` as a blocking product gate. |
| `pdf_translation_workflow_core/contracts/page_type_repair_matrix.md` | Added repair atom for evidence-chain failure. |
| `pdf_translation_workflow_core/prompts/templates/D5_D7_quality_gate.prompt.json` | Required model adjudication to check `source_relative_visual_baseline` first. |
| `pdf_translation_workflow_core/prompts/templates/D8_repair_selection.prompt.json` | Routed source baseline failure to evidence-chain repair, not visual threshold tuning. |
| `pdf_translation_workflow_core/prompts/prompt_tool_bindings.json` | Added source extraction as S8 evidence and failure class. |
| `pdf_translation_workflow_core/tools/README.md` | Documented source-vs-output role gates. |
| `docs/业务流程/PDF_中文回填_标准流程设计.md` | Updated S8 commands, required gates, and blocking rules. |

## Tool Logic Contract

`collect_visual_region_metrics.py` now requires these inputs for product-quality use:

```powershell
python pdf_translation_workflow_core\tools\validators\collect_visual_region_metrics.py `
  --source <source_pdf> `
  --output <candidate_pdf> `
  --generation-evidence docs\reports\<run_id>\candidate_generation_evidence.json `
  --source-extraction docs\reports\<run_id>\source_extraction.json `
  --out docs\reports\<run_id>\visual_region_metrics.json `
  --crop-dir docs\reports\<run_id>\visual_region_crops
```

Each generated region records:

- `source_median_font_size`
- `source_font_source`
- `source_matched_line_count`
- `output_to_source_font_ratio`
- `source_union_bbox`
- `source_background_rgb`
- `output_background_rgb`
- `background_delta`

Blocking baseline rule:

- `source_relative_visual_baseline = fail` if `source_extraction.json` is absent.
- `source_relative_visual_baseline = fail` if source-extraction coverage is below the generic fail floor.
- `source_relative_visual_baseline = warn` if coverage is below the generic recommended floor.
- A baseline failure must not be repaired by tuning visual thresholds; it must repair source extraction or generation evidence linkage.

## Backend Model Prompt Changes

No external backend model call was executed during this update.

The prompt templates were changed for future model adjudication:

- `D5_D7_quality_gate.prompt.json`
  - added required dimension `source_relative_visual_baseline`;
  - instructs the model to check source baseline evidence first;
  - tells the model that product-quality judgement is invalid without current-run source font/line evidence.
- `D8_repair_selection.prompt.json`
  - added routing for `source_relative_visual_baseline_fail`;
  - directs repair to rerun S3/S7 evidence linkage rather than changing thresholds.

Expected model output dimensions remain strict JSON:

- `verdict`
- `findings[]`
- `blocking_failure_count`
- `confidence`
- `evidence_refs[]`
- `next_state`

## Verification Commands

Compile and JSON validation:

```powershell
python -m py_compile pdf_translation_workflow_core\tools\validators\collect_visual_region_metrics.py pdf_translation_workflow_core\tools\validators\evaluate_pdf_quality.py pdf_translation_workflow_core\tools\repairs\plan_visual_region_repairs.py
python -m json.tool pdf_translation_workflow_core\prompts\templates\D5_D7_quality_gate.prompt.json
python -m json.tool pdf_translation_workflow_core\prompts\templates\D8_repair_selection.prompt.json
python -m json.tool pdf_translation_workflow_core\prompts\prompt_tool_bindings.json
```

ZH to EN region metrics:

```powershell
python pdf_translation_workflow_core\tools\validators\collect_visual_region_metrics.py --source docs\input\round09\zh_to_en\source_pdfs\AIA_2020_Annual_Report_zh_pages_03_08_09_24_25.pdf --output docs\output\round09\R09_ZH_TO_EN_AIA_pages_03_08_09_24_25_candidate.pdf --generation-evidence docs\reports\round09\R09_ZH_TO_EN_AIA_pages_03_08_09_24_25\candidate_generation_evidence.json --source-extraction docs\reports\round09\R09_ZH_TO_EN_AIA_pages_03_08_09_24_25\source_extraction.json --out docs\reports\round09\R09_ZH_TO_EN_AIA_pages_03_08_09_24_25\visual_region_metrics.json --crop-dir docs\reports\round09\R09_ZH_TO_EN_AIA_pages_03_08_09_24_25\visual_region_crops --zoom 2.0
```

EN to ZH region metrics:

```powershell
python pdf_translation_workflow_core\tools\validators\collect_visual_region_metrics.py --source docs\input\round09\en_to_zh\source_pdfs\AIA_2020_Annual_Report_en_pages_08_09_24_25.pdf --output docs\output\round09\R09_EN_TO_ZH_AIA_pages_08_09_24_25_candidate.pdf --generation-evidence docs\reports\round09\R09_EN_TO_ZH_AIA_pages_08_09_24_25\candidate_generation_evidence.json --source-extraction docs\reports\round09\R09_EN_TO_ZH_AIA_pages_08_09_24_25\source_extraction.json --out docs\reports\round09\R09_EN_TO_ZH_AIA_pages_08_09_24_25\visual_region_metrics.json --crop-dir docs\reports\round09\R09_EN_TO_ZH_AIA_pages_08_09_24_25\visual_region_crops --zoom 2.0
```

Anti-overfit scan:

```powershell
python pdf_translation_workflow_core\tools\validators\scan_core_overfit.py --root pdf_translation_workflow_core --token-file docs\reports\round09\anti_overfit_tokens.json --out docs\reports\round09\anti_overfit_scan.json
```

## Verification Results

| Case | Source baseline | Visual fail regions | Visual warn regions | Blocking product failures | Main remaining failure |
|---|---:|---:|---:|---:|---|
| `R09_ZH_TO_EN_AIA_pages_03_08_09_24_25` | pass, coverage `1.0` | 23 | 55 | 4 | `footnote_readability`, `table_text_legibility`, plus existing `text_fit` and `visual_similarity` |
| `R09_EN_TO_ZH_AIA_pages_08_09_24_25` | pass, coverage `1.0` | 8 | 3 | 3 | `table_text_legibility`, plus existing `text_fit` and `visual_similarity` |

Anti-overfit scan result: `PASS`.

## Honest Boundary

This update fixes the gate design problem: visual quality now requires source-vs-output evidence before product-quality judgement.

It does not claim the round09 candidate PDFs are final product-quality deliverables. The candidates still fail product quality on table/footnote/text-fit/visual-similarity gates.
