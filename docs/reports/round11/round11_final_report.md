# Round11 Final Report

Generated: 2026-07-06T18:23:34
Updated: 2026-07-06T19:12:43+08:00 after background-cover draw-mode follow-up

## Runtime Boundary

- Workspace root: `D:\项目\开源项目\MerqFin\spikes\独立测试`
- Outputs: `docs\output\round11`
- Backend model calls by this session: `false`
- Semantic translations: reused existing round11 semantic translation inputs; this pass focused on layout, redaction fill, and quality gates.
- Runtime did not use official bilingual reference PDFs as generation input.

## Defect Tracked

The EN->ZH blue-background candidate still showed faint horizontal bands in empty areas. The old checks mainly inspected generated insertion regions, so source text redaction rectangles that were not covered by translated text could pass even when they left visible wipe bands.

During regression, ZH->EN also exposed a table-header case where over-weighted outside-ring sampling could choose the page background instead of a gray header-cell background.

After that fix, the EN->ZH blue-background page exposed a second-order defect: large `background_covers` drawn as `solid_vector_fill` could create faint pale/dark rectangular blocks on saturated backgrounds. The root cause was not the page type itself; the cover draw mode flattened a colored background area that should have been rebuilt from current-source local pixels.

## Generic Fix

- Redaction fill sampling now uses `outside_ring_median_pixel_cluster`.
- Sampling priority is near ring > inner grid > far outer ring. This keeps flat page backgrounds stable while preventing table/header cells from being overwritten by distant page background.
- `generate_semantic_backfill.py` now records and draws `background_covers`:
  - `region_background_cover` for semantic region source-union boxes.
  - `residual_wide_line_background_cover` for remaining wide colored source-line groups.
- `collect_visual_region_metrics.py` now emits `redaction_metrics` and routes uncovered colored wide-line redactions to `background_residue_artifact`.
- Large saturated/color `background_covers` now draw with `row_sampled_image_patch`: the generator renders the unmodified source page, samples same-row left/right background neighborhoods with top/bottom fallback, inserts an opaque PNG background patch, and records `draw_mode`, `sample_zoom`, `patch_size_px`, and `fallback_rgb`.
- `collect_visual_region_metrics.py` now emits `background_cover_metrics`; large saturated/color covers drawn as `solid_vector_fill` fail `background_residue_artifact`.
- D7/D8 prompts, tool contracts, quality contracts, and the standard process document now include `background_covers`, `redaction_metrics`, and `wide_line_patch_risk`.

## Outputs

### R11_EN_TO_ZH_AIA_pages_081_152_214_220_231

- PDF: `docs/output/round11/R11_EN_TO_ZH_AIA_pages_081_152_214_220_231_candidate.pdf`
- SHA256: `BACE5C1A2677BA1ECF02F3AB6A7FFFC1C4CA595EF8285AEAE38C008BEBC4A9FF`
- Product quality: `PASS`; blocking failures: `0`
- D7 visual adjudication: `PASS_WITH_WARN`; warning dimensions: `4`
- Regions: 208 total, 0 fail, 5 warn
- Redactions: 271 total, 0 fail, 0 warn
- Background residue gate: `pass`; fail/warn: 0/0
- Background covers: 14 total; `region_background_cover=7`, `residual_wide_line_background_cover=7`
- Background cover draw modes: `row_sampled_image_patch=12`, `solid_vector_fill=2`; `background_cover_metrics` pass/warn/fail: 14/0/0
- Max inner/background/text-image-background delta: 5.0/31.0/0.0
- Text-image regions: 10; missing image background evidence: 0
- Fit warnings: 0; inserted units/regions: 271/208
- Fill sampling methods: `outside_ring_median_pixel_cluster`

### R11_ZH_TO_EN_AIA_pages_034_036_144_228_261

- PDF: `docs/output/round11/R11_ZH_TO_EN_AIA_pages_034_036_144_228_261_candidate.pdf`
- SHA256: `1F9C35F5606B2B4BD6946442B4DDC1E0086EDBC913A2FF907ADFAF84EC2AF95D`
- Product quality: `PASS`; blocking failures: `0`
- D7 visual adjudication: `PASS_WITH_WARN`; warning dimensions: `5`
- Regions: 233 total, 0 fail, 96 warn
- Redactions: 252 total, 0 fail, 0 warn
- Background residue gate: `pass`; fail/warn: 0/0
- Background covers: 6 total; `region_background_cover=3`, `residual_wide_line_background_cover=3`
- Background cover draw modes: `row_sampled_image_patch=5`, `solid_vector_fill=1`; `background_cover_metrics` pass/warn/fail: 6/0/0
- Max inner/background/text-image-background delta: 5.0/0.0/4.667
- Text-image regions: 44; missing image background evidence: 0
- Fit warnings: 0; inserted units/regions: 252/233
- Fill sampling methods: `outside_ring_median_pixel_cluster`

## Verification

- Python compile check: PASS
- Candidate rendering: PASS, previews under each case's `candidate_previews`
- Visual metrics: PASS with non-blocking warnings only
- Visual adjudication: `PASS_WITH_WARN`, blocking_failure_count=0
- Product quality gates: PASS for both candidates
- Anti-overfit scan: `docs\reports\round11\anti_overfit_scan_after_row_sampled_patch.json` -> PASS
- Focus crop for reported blue-band issue: `docs\reports\round11\diagnostic_page02_bands_crop_after_residual_cover.png`
- Focus crop for reported blue cover-block issue: `docs\reports\round11\diagnostic_blue_patch_crop_after_row_sampled_patch.png`
- Pixel check for the blue cover area: old output dominant color inside cover was `(202,221,242)` while source background was `(202,221,243)`; current output dominant color is `(202,221,243)`.

## Remaining Risk

The ZH->EN dense table pages still carry many non-blocking readability warnings because the source table is highly compact. This is not the same failure class as the redaction-band defect; it remains a visual-density improvement area, not a blocking background-residue failure in this run.
