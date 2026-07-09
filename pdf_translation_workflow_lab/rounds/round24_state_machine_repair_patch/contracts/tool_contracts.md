# Round22 Tool Contracts

## Common Contract

Every tool must:

- run from the `docs/output/round22` package root or receive absolute paths under it;
- write artifacts only under `reports/`, `output/`, or `previews/`;
- emit JSON evidence for every decision that affects layout;
- avoid importing `pdf_translation_workflow_core`;
- avoid filename, exact page number, exact source phrase, or exact sample numeric-value branching.

## Tool Table

| Tool | Role | Reads | Writes | Notes |
|---|---|---|---|---|
| `tools/probes/probe_runtime.py` | dependency check | environment | `reports/tool_probe.json` | Checks Python, PyMuPDF, PIL. |
| `tools/probes/extract_source_structure.py` | source extraction | source PDF | `reports/source_structure.json` | Must record text, bbox, span colors, dominant non-symbol span, page stats. |
| `tools/planners/plan_roles.py` | role classification | source structure, translations | `reports/role_plan.json` | Uses current-page relative features only. Dense source-derived financial/table grids become per-line `table_cell` groups; adjacent small-font table headers are bound to the same table treatment. |
| `tools/planners/plan_layout.py` | layout planning | role plan, source PDF drawing geometry | `reports/layout_plan.json` | Computes erase areas, target boxes, text variant preference, font bounds, adjacent source-line-grid container relayout, source graphic boundary limits, filled-panel compact stacks, local metric-stack relayout, text-column vertical flow with draw-height reservation, section pushdown after source rules, source-column bounded translation-growth slots, metric text width growth, red-heading guardrails, and table-region obstacle packing. |
| `tools/generators/generate_candidate.py` | candidate rendering | source PDF, layout plan | candidate PDF, `reports/generation_evidence.json`, previews | Uses two-phase render: erase all planned regions first, then insert translated text. |
| `tools/validators/validate_quality.py` | product gates | generation evidence, previews | `reports/quality_gates.json` | Must fail on overflow, tiny text, source-relative overlap growth, visible residue, clipped labels. |
| `tools/repairs/plan_repairs.py` | repair selection | quality gates | `reports/repair_plan_<n>.json` | Maps gate failures to one repair family per loop. |
| `tools/validators/validate_process.py` | process audit | all reports | `reports/process_audit.json` | Verifies boundary, trace completeness, no reference input use. |
| `run_round22_workflow.py` | orchestrator | input paths | final trace and artifacts | Executes state machine and loop. |

## Evidence Log Contract

The orchestrator must produce:

| Artifact | Required Content |
|---|---|
| `reports/operation_log.jsonl` | every command, state, return code, stdout tail, stderr tail |
| `reports/decision_log.jsonl` | boundary decision, tool dispatch decisions, quality verdict, repair-family selection |
| `reports/model_interactions.jsonl` | every model call; if no model is called, one explicit `not_invoked` record |
| `reports/state_trace.json` | ordered state execution records |

## Role Contract

Roles are not fixed point-size buckets. They must be derived from current-page statistics.

Allowed source-derived dimensions:

- relative font rank within the page;
- current-page dominant body color and saturated accent colors;
- text width distribution;
- reading position relative to current page text distribution;
- symbol-font presence;
- generic unit/currency/percentage token presence.
- adjacent source drawing-line grids, filled source rectangles, long source section rules, and current-page column geometry.

Forbidden dimensions:

- exact font point thresholds such as `>= 24`;
- exact page ids or file names;
- exact sample text strings;
- exact sample values such as `13.3%`.

## Gate To Repair Mapping

| Gate Failure | Repair Family | Expected Change |
|---|---|---|
| `overflow_after_fit` | `expand_or_reflow_slot` | expand within detected column/card, use compact variant, or split into local lines |
| `source_relative_font_floor` | `reflow_before_shrink` | increase source-derived container/section space before shrinking font |
| `local_text_overlap` | `vertical_flow_relayout` | compare output overlap against source overlap baseline, then stack, container-reflow, or push down only the affected local flow; reserve possible generator draw-height expansion |
| `chart_or_panel_boundary_intrusion` | `source_graphic_boundary_limit` | limit expanded text at source drawing edges and recompute target height for wrapping |
| `filled_panel_text_collision` | `filled_panel_compact_stack` | stack compact labels inside the detected filled rectangle and erase with the panel fill color |
| `visible_background_residue` | `background_resample` | sample surrounding ring/background region, not text interior |
| `wrong_text_color` | `span_color_split` | separate symbol color from dominant text color |
| `wrong_role_classification` | `role_stats_adjust` | change relative feature logic, not page-specific values |
| `nav_footer_pollution` | `band_role_isolation` | classify repeated header/footer as `nav_footer` and exclude from body gates |
| `symbol_font_leakage` | `text_sanitization` | remove extracted one-letter symbol artifacts while preserving source-derived bullet/color rendering |
| `accent_heading_misclassified_as_title` | `red_heading_role_precedence` | classify accent-color non-symbol text as red heading before broad title rules |
| `single_line_title_false_overlap` | `single_line_title_height_reduce` | reduce title target height when target text fits one line; expand only when target length and width imply wrapping |
| `container_heading_unreadably_small` | `source_grid_container_heading_expand` | use detected source container width and target single-line capacity before shrinking heading text |
| `translated_text_expands_with_local_space` | `translation_growth_slot_expand` | expand the target slot only inside the same source-column boundary before shrinking text |
| `metric_value_contains_words` | `metric_text_width_growth` | widen word-bearing metric values using target text length and page margin, without exact value branching |
| `upstream_text_covers_red_heading` | `section_heading_guardrail` | protect later source-derived red headings from upstream text expansion in the same column |
| `table_block_merged_into_paragraph` | `table_cell_split` | split dense wide numeric/table-like source blocks into per-line `table_cell` groups before layout |
| `body_text_intrudes_later_table` | `table_region_obstacle_pack` | derive later table bands from source `table_cell` rectangles and pack same-column flow text above the table |

Current round22 implements repair selection only. A repair plan is evidence for the next engineering change; it is not proof that the candidate was repaired.
