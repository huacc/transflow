# Round22 Isolated Layout Experiment Report

## Boundary

Round22 is an isolated experiment under `docs/output/round22`.

The package is not part of `pdf_translation_workflow_core`. Runtime tools in this directory must not import or execute `pdf_translation_workflow_core` modules.

## Inputs

- Source PDF: `input/source_pdfs/00005_2025_annual_report_zh_pages_003_005_006.pdf`
- Translation JSON: `input/semantic_translations/R22_PAGES_03_05_06_00005_2025_annual_report_zh_pages_003_005_006.translations.json`
- Offline reference only: `offline_reference_compare/reference_00005_2025_annual_report_en.pdf`

The offline reference is only for human review. It is not consumed by runtime tools.

## Execution Entry

Primary command:

```powershell
python docs\output\round22\run_round22_workflow.py
```

Detailed procedure:

- `docs/output/round22/EXECUTION.md`
- `docs/output/round22/contracts/state_machine.md`
- `docs/output/round22/contracts/tool_contracts.md`
- `docs/output/round22/contracts/execution_procedure.md`

## Implemented Chain

Implemented files:

- `run_round22_workflow.py`
- `tools/probes/probe_runtime.py`
- `tools/probes/extract_source_structure.py`
- `tools/planners/plan_roles.py`
- `tools/planners/plan_layout.py`
- `tools/generators/generate_candidate.py`
- `tools/validators/validate_quality.py`
- `tools/repairs/plan_repairs.py`
- `tools/validators/validate_process.py`
- `prompts/templates/visual_quality_adjudication.prompt.json`
- `prompts/templates/repair_selection.prompt.json`

## Required Evidence

The runner generates:

- `reports/run_request.json`
- `reports/tool_probe.json`
- `reports/source_structure.json`
- `reports/role_plan.json`
- `reports/layout_plan.json`
- `reports/generation_evidence.json`
- `reports/quality_gates.json`
- `reports/repair_plan_0.json`
- `reports/process_audit.json`
- `reports/state_trace.json`
- `reports/decision_log.jsonl`
- `reports/operation_log.jsonl`
- `reports/model_interactions.jsonl`

The process validator also requires the root execution document, contracts, prompt templates, and all stage tools. Missing static assets now fail the process audit.

## Current Output

- Candidate PDF: `output/R22_PAGES_03_05_06_candidate.pdf`
- PNG previews: `previews/candidate_page_001.png` through `previews/candidate_page_003.png`

## Current Gate Status

The current process audit is expected to be `PASS` after a clean run.

The current product verdict is `FAIL` if `quality_gates.json` contains any `overflow_after_fit` group or any output overlap that is worse than the source-region overlap baseline. That failure is intentional and must not be hidden: the repair selector records a repair family, but round22 does not yet apply another automatic repair loop and rerun the layout sequence.

## Current Repair State

Implemented:

- map blocking failures to repair families;
- record selected repair family in `repair_plan_0.json`;
- log the repair selection decision in `decision_log.jsonl`.
- classify KPI labels as content, not nav/footer, by restricting nav/footer to real page bands;
- use short layout variants for red-note roles;
- render candidates in two phases, erasing all planned regions before inserting translated text;
- sanitize one-letter symbol-font leakage before uppercase words while preserving source-derived bullet rendering;
- split same-row cross-column source blocks into separate groups using current-page row geometry;
- infer adjacent source-line-grid containers from PDF drawing lines and relayout headings/bodies inside those local cells;
- limit chart labels and footnotes at source drawing boundaries, then recompute target height so text wraps instead of intruding into adjacent panels;
- stack compact labels inside non-white source-filled panels and erase with the panel fill color to avoid white bars;
- apply source-geometry metric-stack relayout for local KPI label/value/note groups;
- apply text-column vertical flow for `body`, `section_heading`, and `red_note`, including reserved draw-height expansion from generator font fallback;
- apply section pushdown after long source horizontal rules when upstream translated content grows and page-bottom capacity exists;
- add `local_text_overlap` gate using source overlap as the baseline.

Not implemented:

- automatically mutate layout policy from the repair plan;
- rerun S3-S6 after mutation;
- prove visual closure through a repaired candidate.

## Anti-Overfit State

Removed from runtime decisions:

- fixed point-size title thresholds;
- exact sample text tokens;
- exact sample numeric values;
- offline reference access;
- core framework imports.

Still suspicious and not ready for merge:

- some expansion policies still need a stronger source-derived decision for when to redraw or extend card/container borders instead of shrinking fonts;
- product gates cover fit, font floor, and source-relative local overlap, but do not yet cover all human-visible color/residue/card-density failures;
- repair application is not closed-loop.

## Latest Local Run

Latest verified command:

```powershell
python docs\output\round22\run_round22_workflow.py
```

Latest verified artifacts:

- Candidate PDF: `output/R22_PAGES_03_05_06_candidate.pdf`
- Preview PNGs: `previews/candidate_page_001.png`, `previews/candidate_page_002.png`, `previews/candidate_page_003.png`
- Process audit: `reports/process_audit.json`
- Product gates: `reports/quality_gates.json`

Latest verified verdict:

- `process_contract_verdict`: `PASS`
- `product_quality_verdict`: `FAIL`
- Blocking failure classes still present: `all_groups_fit`, `source_relative_font_floor`, `local_text_overlap`

Observed improvement:

- Page 3 card headings are no longer merged across columns.
- The top four-column grey panel is relaid out as adjacent source-grid cells instead of cross-column containers.
- Page 3 chart labels and footnote are bounded by source card/grid edges and wrap instead of intruding into the right-side cards.
- Page 3 bottom grey value panels stack compact text on the grey fill instead of leaving white erase bars.
- Symbol leakage such as leading extracted bullet letters is removed by a generic sanitizer.

Remaining issue:

- Some long translated headings/bodies still require either container-border expansion/redraw or downstream section movement; shrinking alone violates the source-relative font floor.

## Merge Guidance

Do not migrate round22 into `pdf_translation_workflow_core` yet.

Round22 is useful as an isolated design probe for:

- extracting source structure;
- splitting roles;
- logging state/tool/model decisions;
- validating process-boundary completeness;
- selecting repair families from product gates.

It is not yet a complete production workflow because product-quality repair loops are not executable end to end.
