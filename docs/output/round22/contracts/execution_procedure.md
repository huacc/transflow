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
   - Required roles include `title`, `section_heading`, `body`, `red_heading`, `red_note`, `metric_value`, `compact_panel`, `nav_footer`.
   - Emit both role and evidence dimensions for each group.

5. Run `S4_LayoutPlan`.
   - Choose translation variant: display, short, compact.
   - Plan erase rect, target rect, font size range, line wrapping, and bullet/text split.
   - Expansion must respect detected neighboring columns/cards.

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
