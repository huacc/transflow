# Round11 Background Cover Draw Mode Follow-up

Generated: 2026-07-06T19:12:43+08:00

## Trigger

User reported that blue-background candidate pages showed pale/whitish rectangular patches after the redaction-band fix, while cream/white pages did not show the same issue.

## Assumption Checked

The defect was treated as a background-cover drawing artifact, not as a translation issue. The prior fix used `background_covers` to suppress horizontal redaction bands, but large covers on saturated backgrounds were drawn as `solid_vector_fill`.

## Evidence

- Candidate evidence page 2 contained large `region_background_cover` and `residual_wide_line_background_cover` rectangles with fill around RGB `(202,221,243)`.
- Pixel inspection before this follow-up showed the source blue background dominant color was `(202,221,243)`, while the old output cover interior often rendered as `(202,221,242)`.
- The average color delta was small, so the old quality gate passed; visually, the continuous large rectangle was still perceptible on blue/saturated backgrounds.

## Tool / Contract Changes

- `pdf_translation_workflow_core/tools/generators/generate_semantic_backfill.py`
  - Added `draw_mode` for `background_covers`.
  - Large saturated/color covers now use `row_sampled_image_patch`.
  - The generator renders the unmodified source page before redaction, samples left/right same-row background pixels with top/bottom fallback, creates an opaque PNG patch, inserts it over the cover rect, and records `sample_zoom`, `patch_size_px`, and `fallback_rgb`.
  - Neutral or small covers can still use `solid_vector_fill`.

- `pdf_translation_workflow_core/tools/validators/collect_visual_region_metrics.py`
  - Added `background_cover_metrics`.
  - Large saturated/color covers drawn as `solid_vector_fill` fail or warn through `background_residue_artifact`.
  - This catches cover-created visual blocks that whole-page similarity or average background delta can miss.

- Contract / prompt updates
  - `docs/业务流程/PDF_语义翻译回填_标准流程设计.md`
  - `pdf_translation_workflow_core/contracts/tool_contracts.md`
  - `pdf_translation_workflow_core/contracts/product_quality_contract.md`
  - `pdf_translation_workflow_core/contracts/page_type_repair_matrix.md`
  - `pdf_translation_workflow_core/tools/README.md`
  - `pdf_translation_workflow_core/prompts/templates/D5_D7_quality_gate.prompt.json`
  - `pdf_translation_workflow_core/prompts/templates/D8_repair_selection.prompt.json`

## Backend Model Calls

None in this follow-up. The work used deterministic local tools and existing round11 semantic translation inputs.

## Regeneration

Outputs were regenerated in `docs/output/round11`:

- `R11_EN_TO_ZH_AIA_pages_081_152_214_220_231_candidate.pdf`
  - SHA256: `BACE5C1A2677BA1ECF02F3AB6A7FFFC1C4CA595EF8285AEAE38C008BEBC4A9FF`
  - `background_covers`: 14 total
  - `draw_mode`: `row_sampled_image_patch=12`, `solid_vector_fill=2`
  - `background_cover_metrics`: pass/warn/fail = 14/0/0
  - `background_residue_artifact`: pass, fail/warn = 0/0
  - `product_quality_verdict`: PASS

- `R11_ZH_TO_EN_AIA_pages_034_036_144_228_261_candidate.pdf`
  - SHA256: `1F9C35F5606B2B4BD6946442B4DDC1E0086EDBC913A2FF907ADFAF84EC2AF95D`
  - `background_covers`: 6 total
  - `draw_mode`: `row_sampled_image_patch=5`, `solid_vector_fill=1`
  - `background_cover_metrics`: pass/warn/fail = 6/0/0
  - `background_residue_artifact`: pass, fail/warn = 0/0
  - `product_quality_verdict`: PASS

## Verification Commands

```powershell
python -m py_compile pdf_translation_workflow_core\tools\generators\generate_semantic_backfill.py pdf_translation_workflow_core\tools\validators\collect_visual_region_metrics.py
python -m json.tool pdf_translation_workflow_core\prompts\templates\D5_D7_quality_gate.prompt.json
python -m json.tool pdf_translation_workflow_core\prompts\templates\D8_repair_selection.prompt.json
python pdf_translation_workflow_core\tools\validators\collect_visual_region_metrics.py --source docs\input\round11\source_pdfs\AIA_2020_Annual_Report_en_pages_081_152_214_220_231.pdf --output docs\output\round11\R11_EN_TO_ZH_AIA_pages_081_152_214_220_231_candidate.pdf --generation-evidence docs\reports\round11\R11_EN_TO_ZH_AIA_pages_081_152_214_220_231\candidate_generation_evidence.json --source-extraction docs\reports\round11\R11_EN_TO_ZH_AIA_pages_081_152_214_220_231\source_extraction.json --out docs\reports\round11\R11_EN_TO_ZH_AIA_pages_081_152_214_220_231\visual_region_metrics.json --crop-dir docs\reports\round11\R11_EN_TO_ZH_AIA_pages_081_152_214_220_231\visual_region_crops --zoom 3
python pdf_translation_workflow_core\tools\validators\evaluate_pdf_quality.py --source docs\input\round11\source_pdfs\AIA_2020_Annual_Report_en_pages_081_152_214_220_231.pdf --output docs\output\round11\R11_EN_TO_ZH_AIA_pages_081_152_214_220_231_candidate.pdf --generation-evidence docs\reports\round11\R11_EN_TO_ZH_AIA_pages_081_152_214_220_231\candidate_generation_evidence.json --visual-adjudication docs\reports\round11\R11_EN_TO_ZH_AIA_pages_081_152_214_220_231\visual_adjudication.json --visual-region-metrics docs\reports\round11\R11_EN_TO_ZH_AIA_pages_081_152_214_220_231\visual_region_metrics.json --out docs\reports\round11\R11_EN_TO_ZH_AIA_pages_081_152_214_220_231\product_quality_gates.json
```

## Focus Evidence

- Crop: `docs/reports/round11/diagnostic_blue_patch_crop_after_row_sampled_patch.png`
- Pixel check after fix: source and output cover-area dominant color both render as `(202,221,243)`.
