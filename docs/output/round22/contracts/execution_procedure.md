# Round22 Execution Procedure

## Success Criteria

The round is successful only when:

- candidate PDF is generated;
- every source page has a rendered preview;
- `quality_gates.json.product_quality_verdict` is `PASS`;
- `process_audit.json.process_contract_verdict` is `PASS`;
- report lists every loop and every model/tool decision;
- no runtime tool reads `offline_reference_compare/`.

## Procedure

1. Run `S0_Request`.
   - Record source PDF, translations JSON, output paths, and package boundary.
   - Verify all runtime paths resolve under `docs/output/round22`.

2. Run `S1_ToolProbe`.
   - Probe Python version, PyMuPDF, PIL, write permission.
   - If missing required dependency, stop at `S_FAIL_TOOLING`.

3. Run `S2_SourceExtract`.
   - Extract page size, text lines, spans, bbox, font, color, symbol-font flag.
   - Compute page-level quantiles for font size, line width, line height.
   - Record dominant body color and saturated accent colors.

4. Run `S3_RolePlan`.
   - Classify roles using current-page relative features only.
   - Required roles include `title`, `section_heading`, `body`, `red_heading`, `red_note`, `metric_value`, `compact_panel`, `table_cell`, `nav_footer`.
   - Split dense, wide, numeric/table-like source blocks into `table_cell` groups before any normal paragraph grouping.
   - Bind adjacent small-font blocks that touch a detected table top into `table_cell` treatment when their source geometry overlaps the table.
   - Emit both role and evidence dimensions for each group.

5. Run `S4_LayoutPlan`.
   - Choose translation variant: display, short, compact.
   - Plan erase rect, target rect, font size range, line wrapping, and bullet/text split.
   - Expansion must respect detected neighboring columns/cards.
   - Apply source-derived text-growth slots before font shrink when translated text needs more height.
   - Apply metric text width growth only from target text length, source font size, current target width, and page margin.
   - Apply red-heading guardrails only from source/output column overlap and source-font-derived minimum remaining height.
   - Apply container long-heading expansion from detected source line-grid containers, container width, source font size, and target single-line capacity.
   - Derive table bands from `table_cell` source rectangles and pack same-column body flow above later table regions when translated text would intrude.
   - Do not branch on source page number, exact source/target phrase, exact numeric value, or offline reference content.

6. Run `S5_GenerateCandidate`.
   - Render candidate over copied source PDF.
   - Record every fit attempt and final result per group.
   - Render PNG previews.

7. Run `S6_QualityGate`.
   - Fail if any group is `overflow_after_fit`.
   - Fail if text is rendered below source-relative floor.
   - Fail if bullet text inherits bullet color instead of body color.
   - Fail if near-white background is rendered as visible gray residue.
   - Fail if repeated header/footer is counted as body failure.

8. If S6 fails, enter `Lx_RepairLoop`.
   - Select repair family from `tool_contracts.md`.
   - Current round22 runner records the repair family but does not auto-apply it.
   - Because repair is not auto-applied, `product_quality_verdict=FAIL` remains the honest terminal product state.
   - A future closed-loop runner must apply one repair family, repeat S3-S6, and stop after max loop count or repeated no-improvement failures.

9. Run `S7_ProcessAudit`.
   - Validate evidence completeness, path boundary, no reference input use, no hard-coded sample branching.

## Required Logs

- `reports/state_trace.json`
- `reports/decision_log.jsonl`
- `reports/operation_log.jsonl`
- `reports/model_interactions.jsonl`
- `reports/repair_plan_<n>.json`

## Human/Model Adjudication

Model adjudication is allowed only for ambiguous visual judgment. It must use the templates under `prompts/templates/` and must record:

- prompt template id;
- slot values;
- input artifact refs;
- output dimensions;
- selected repair family;
- uncertainty and rejected alternatives.

## Latest 1-20 Page Verification

- Source input: `input/source_pdfs/00005_2025_annual_report_zh_pages_001_020.pdf`.
- Translation input: `input/semantic_translations/R22_GEN_ZH_TO_EN_00005_pages_001_020.translations.json`.
- Candidate output: `output/R22_GEN_ZH_TO_EN_00005_pages_001_020_candidate.pdf`.
- Process verdict: `PASS`.
- Product verdict: `FAIL`.
- Blocking failures: 80 total; 9 `all_groups_fit`, 1 `source_relative_font_floor`, 70 `local_text_overlap`.
- Page-16 regression fixed: a wide financial table was no longer merged into one red paragraph; the table is now split into `table_cell` groups and later body flow is packed above the table region.
- Accepted generic changes: red-heading role precedence, source-relative red-heading floor, single-line title height reduction, container heading expansion, metric text width growth, translation-growth slots, section-heading guardrail, table-cell split, adjacent table-header binding, table-region obstacle packing.
- Rejected generic changes: global line-count ceiling across all roles and red-heading source-column cap. Both were removed because the 1-20 run showed worse product gates.
