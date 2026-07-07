# Round20 Overlay Experiment Report

## Scope

- Workspace: `docs/output/round20`
- Tool sandbox: `docs/output/round20/pdf_translation_workflow_core`
- Source subset: pages 3, 5, 6 from `00005_2025_annual_report_zh_pages_001_030.pdf`
- Runtime input: source PDF plus semantic translation JSON only
- Reference or human translation files: not used during generation

## Validated Fixes

1. Metric/KPI role classification now requires current-page font hierarchy plus a generic numeric amount pattern.
   - Unit labels such as "US dollars" are no longer promoted to `metric_value` only because they contain a currency word.
   - This removed the `metric_value_hierarchy` blocking failure in the round20 run.

2. Reusable font policy moved away from sample fixed point sizes.
   - Role font profiles use `source_size` ratios and current-page font quantiles.
   - Absolute point-size floors are retained only as warning/reporting dimensions unless a role explicitly declares that they are blocking.

3. Short/compact labels can use role-specific page-relative expansion.
   - `compact_label` is handled as a short-label quality role but can expand with its own page-ratio limits.
   - The rule is based on current page geometry, not literal label text or coordinates.

4. Colored small-label background sampling now supports `inner_bbox_pixel_cluster`.
   - If bbox-internal samples form a consistent non-light background cluster, redaction/text-image fill uses that inner cluster.
   - This fixed the visible white patch artifact on colored table/matrix labels.

5. Process validation now supports direct S8 PASS to S9 acceptance.
   - `D8_minimal_repair_selection` is required only when D7 failed or `Lx_RepairLoop` was entered.
   - Direct product-quality PASS is accepted through `D9_final_acceptance.next_state = S_DONE_PRODUCT_ACCEPTED`.

## Validation Evidence

- Candidate PDF: `run_pages_03_05_06/output/ROUND20P356_00005_2025_annual_report_zh_pages_003_005_006_candidate.pdf`
- Product quality: `PASS`
- Process validation rerun: `PASS`
- Visual region metrics:
  - `fail_region_count = 0`
  - `warn_region_count = 10`
  - `fail_redaction_count = 0`
  - `blocking_repair_count = 0`
- Anti-overfit scan: `anti_overfit_scan_overlay.json` => `PASS`

## Migration Decision

The validated changes are generic and should be migrated to the root `pdf_translation_workflow_core`:

- metric amount classification
- source-relative constrained image sizing
- compact-label expansion support
- inner background cluster sampling
- source-relative quality gate blocking semantics
- direct PASS process validation semantics

Do not migrate the round20 subset input/output artifacts into the core framework.

## Follow-up Collision Fix

Manual preview of the first root validation found that page 6 still had overlapping generated labels in a gray multi-column strategy panel while product gates passed. That exposed two generic gaps:

1. Narrow same-row panel headings were still allowed to behave as page-level `heading` regions.
2. S8 had no deterministic generated-insertion collision gate.

Implemented and validated follow-up fixes:

- `generate_semantic_backfill.py` now splits same-block text on current-run cross-column row gaps.
- Large-font labels on matrix/table pages with horizontally separated same-row neighbors are downgraded to constrained table/panel roles.
- `target_language_reflow.min_source_width_page_ratio_for_reflow` applies to every expandable kind, not just `body/body_flow`.
- `collect_visual_region_metrics.py` now emits `insertion_collision`.
- `plan_visual_region_repairs.py` maps `insertion_collision` to `region_collision_layout_repair`.

Final root validation:

- Candidate PDF: `root_validation_pages_03_05_06_fix2/output/R20FIX356B_00005_2025_annual_report_zh_pages_003_005_006_candidate.pdf`
- Final verdict: `process_contract_verdict=PASS`, `product_quality_verdict=PASS`, `terminal_state=S_DONE_PRODUCT_ACCEPTED`
- Final report: `root_validation_pages_03_05_06_fix2/reports/r20fix356b_final_verdict.json`
