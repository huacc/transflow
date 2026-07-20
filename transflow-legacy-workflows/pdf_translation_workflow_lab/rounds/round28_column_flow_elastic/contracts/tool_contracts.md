# Round28 Tool Contracts

Round28 is a lab round. It may add or change tools under this package only. It must not
modify `pdf_translation_workflow_core`.

## Common Contract

Every tool must:

- run from `pdf_translation_workflow_lab/rounds/round28_column_flow_elastic` or receive absolute paths under it;
- write artifacts only under `reports/`, `output/`, `previews/`, or `case_runs/`;
- emit JSON evidence for decisions that affect layout;
- avoid importing `pdf_translation_workflow_core`;
- avoid filename, exact page number, exact source phrase, exact target phrase, or sample numeric-value branching.

## Page-Type Contract

Each page is classified before layout repair. Classification is source-derived and uses only current-page evidence:

- text density and bbox distribution;
- detected column bands;
- table-like numeric line density;
- image/drawing coverage;
- role counts from `role_plan.json`;
- language-pair expansion prior.

Allowed page roles:

| Page role | Layout flow | Repair bias |
|---|---|---|
| `body_text_page` | `single_column_text` or `multi_column_text` | Keep column width; reflow vertically within each column. |
| `financial_table_page` | `table_grid` | Preserve table geometry; do not convert tables to paragraph flow. |
| `chart_or_metric_page` | `chart_metric_grid` | Preserve chart/metric anchors; only local label fitting is allowed. |
| `cover_or_section_page` | `visual_freeform` | Preserve visual composition and image/background regions. |
| `mixed_page` | current dominant local flow | Protect non-text obstacles before any text movement. |

## Column-Width-Invariant Flow Contract

For normal body or column pages:

- source column `x0/x1` is the legal horizontal boundary;
- target text may grow or shrink vertically;
- text groups in the same column may be pushed down as a flow;
- paragraph starts and headings should keep their source column and relative order;
- paragraph-like body text in the same page flow must use a consistent source-derived font size;
- only source-derived vertical free space can be consumed;
- if no legal vertical space exists, emit a repair failure or defer instead of shrinking text into unreadability.

For Chinese-to-English, expansion is expected. The repair target is not "fit inside original bbox"; it is:

1. keep column width stable;
2. keep source reading order stable;
3. expand down when legal;
4. preserve obstacles such as tables, figures, cards, charts, sidebars, and page footer bands;
5. shrink font only after legal reflow space is exhausted.

For English-to-Chinese, contraction is expected. The repair target is:

1. avoid excessive empty gaps inside a text flow;
2. preserve section rhythm and column alignment;
3. do not enlarge text enough to break the source hierarchy.

## Background Contract

Round28 must not treat background as a repair variable.

- Erase only the source text bbox, with small padding.
- Do not wipe the whole target bbox.
- Do not sample and repaint photo or textured background as a way to hide bad layout.
- Do not erase text that belongs to a protected image/photo unless it was extracted as live PDF text and classified as overlay text.

`apply_column_flow_elastic.py` must write `erase_policy = source_text_only` for the regions it modifies.

## Tool Table

| Tool | Role | Reads | Writes | Notes |
|---|---|---|---|---|
| `tools/probes/probe_runtime.py` | dependency check | environment | `reports/tool_probe.json` | Checks Python, PyMuPDF, PIL. |
| `tools/probes/extract_source_structure.py` | source extraction | source PDF | `reports/source_structure.json` | Records text, bbox, span colors, font, page stats. |
| `tools/planners/plan_roles.py` | role classification | source structure, translations | `reports/role_plan.json` | Uses relative current-page features. |
| `tools/planners/classify_pages.py` | page classification | role plan | `reports/page_profiles.json` | Produces page role, layout flow, density, columns, and whether normal flow is enabled. |
| `tools/planners/plan_layout.py` | base layout planning | role plan, source geometry | `reports/layout_plan.raw.json` | Preserves existing round27 base behavior before round28 postprocess. |
| `tools/planners/apply_column_flow_elastic.py` | column flow postprocess | raw layout plan, page profiles | `reports/layout_plan.json`, `reports/column_flow_elastic_evidence.json` | Applies column-width-invariant vertical flow for eligible text pages. |
| `tools/generators/generate_candidate.py` | candidate rendering | source PDF, layout plan | candidate PDF, `reports/generation_evidence.json`, previews | Erases planned source text and inserts translated text. |
| `tools/validators/validate_quality.py` | product gates | generation evidence, previews | `reports/quality_gates.json` | Fails on overflow, tiny text, overlap growth, residue, clipping. |
| `tools/judges/compare_source_candidate.py` | source-candidate comparison | source/candidate PDFs | `reports/quality_signals.json`, `reports/visual_adjudication.json` | Emits source-relative visual quality signals. |
| `tools/repairs/build_repair_patch.py` | first repair selection | quality gates, layout plan | `reports/repair_patch_0001.json` | Selects one primary repair family. |
| `tools/repairs/obstacle_aware_reflow.py` | second repair binder | layout, quality, loop memory | `reports/repair_patch_0002.json` | Avoids repeating a rejected atom and respects obstacles. |
| `tools/repairs/apply_repair_patch.py` | patch application | layout plan, RepairPatch | repaired layout plan | Applies operations only; does not judge quality. |
| `tools/validators/materialize_round28_artifacts.py` | decision artifacts | S8/Lx reports | decision files, memory ledger, trace cards | Splits evidence into process-contract artifacts. |
| `tools/validators/validate_decision_graph.py` | decision graph validator | registry snapshot, artifacts, change ledger | `reports/decision_graph_validation.json` | Checks dispatch and evidence chain. |
| `tools/validators/build_tool_binding_map.py` | binding preflight | round28 files | `reports/tool_binding_map.json` | Maps contract names to concrete round28 files. |
| `tools/validators/validate_process.py` | process audit | all reports | `reports/process_audit.json` | Verifies trace completeness, boundary, and anti-overfit evidence. |
| `run_round28_contract_case.py` | case orchestrator | one source PDF | per-case reports/output | Executes the state flow for one input. |
| `run_round28_batch.py` | batch orchestrator | configured source PDFs | batch reports | Runs both 20-page inputs. |

## Gate To Repair Mapping

| Gate failure | Primary repair family | Expected action |
|---|---|---|
| `text_fit_overflow` | `column_flow_elastic_relayout` | Keep column x-range stable and reflow text vertically before shrinking font. |
| `cross_slot_overlap` | `obstacle_aware_reflow` | Move or reflow only legal same-column groups; reject if obstacle-safe movement is unavailable. |
| `font_size_regression` | `reflow_before_shrink` | Recover font by consuming legal vertical space before using smaller text. |
| `body_font_inconsistency` | `uniform_paragraph_font_within_page_flow` | Use current-page paragraph font median for body-like text in one flow. |
| `table_block_merged_into_paragraph` | `table_cell_split` | Preserve table-grid treatment; do not apply normal paragraph flow. |
| `body_text_intrudes_later_table` | `table_region_obstacle_pack` | Keep text above protected table bands. |
| `visible_background_residue` | `source_text_only_erase_review` | Confirm erase area is source text only; do not repaint background as layout repair. |
| `wrong_role_classification` | `role_or_page_profile_adjust` | Fix relative classification logic, not page-specific constants. |

## RepairPatch Operation Schema

Repair tools must output operations only. They must not directly write PDFs.

Allowed operation types:

| Operation | Required fields | Meaning |
|---|---|---|
| `expand_slot` | `operation_id`, `operation_type`, `group_id`, `page_index`, `grow_down_pt`, `grow_right_pt`, `min_font_start`, `min_font_min`, `failure_class`, `reason` | Expand one target text region inside its legal slot. |
| `move_region_group` | `operation_id`, `operation_type`, `page_index`, `group_ids`, `delta_x_pt`, `delta_y_pt`, `failure_class`, `reason` | Move explicitly selected movable groups. |
| `flow_within_region` | `operation_id`, `operation_type`, `group_id`, `page_index`, `target_rect`, `font_start`, `font_min`, `failure_class`, `reason` | Reflow one region within a computed legal rectangle. |
| `split_flow` | `operation_id`, `operation_type`, `source_group_id`, `page_index`, `target_rects`, `failure_class`, `reason` | Split one text flow into multiple legal target rectangles. |
| `defer_unrepairable` | `operation_id`, `operation_type`, `failure_class`, `unrepairable_reason`, `evidence_ref` | Declare no legal repair under current constraints. |

## Loop Rule

The loop repairs one primary failure at a time.

1. Classify page and failure domain.
2. Select one primary blocking failure using severity and causal priority.
3. Bind one repair family.
4. Apply patch and regenerate.
5. Rejudge source-vs-candidate.
6. Accept only if the target failure improves and hard non-target failures do not regress.

If a repair improves one issue but worsens another hard issue, reject it, record the failed
`(issue_key, repair_atom)` in `repair_memory_ledger.json`, and promote the regressed issue
only if a different repair atom exists.
