# Round22 Execution Document

## Scope

This document is the runnable execution guide for the isolated round22 package.

Round22 is an experiment package under `docs/output/round22`. It must not import or modify `pdf_translation_workflow_core`. The offline English reference is only for human comparison after generation and is not a runtime input.

## Assumptions

- Runtime root is this directory: `docs/output/round22`.
- Runtime input is limited to `input/source_pdfs/` and `input/semantic_translations/`.
- The translation JSON already contains semantic translations. The runner does not call an LLM for translation.
- Prompt templates are present for future model adjudication. In the current runner, model calls are explicitly logged as `not_invoked`.
- A generated PDF is only a candidate. It is accepted only if product quality and process audit both pass.

## Command

Run from the project root:

```powershell
python docs\output\round22\run_round22_workflow.py
```

Equivalent run from this directory:

```powershell
python run_round22_workflow.py
```

Optional arguments:

```powershell
python run_round22_workflow.py `
  --source-pdf input/source_pdfs/00005_2025_annual_report_zh_pages_003_005_006.pdf `
  --translations-json input/semantic_translations/R22_PAGES_03_05_06_00005_2025_annual_report_zh_pages_003_005_006.translations.json `
  --case-id R22_PAGES_03_05_06
```

## State To Tool Mapping

| State | Tool | Required Input | Required Output |
|---|---|---|---|
| S0_Request | `run_round22_workflow.py` | CLI args | `reports/run_request.json`, first decision log entry |
| S1_ToolProbe | `tools/probes/probe_runtime.py` | local Python runtime | `reports/tool_probe.json` |
| S2_SourceExtract | `tools/probes/extract_source_structure.py` | source PDF | `reports/source_structure.json` |
| S3_RolePlan | `tools/planners/plan_roles.py` | source structure, translation JSON | `reports/role_plan.json` |
| S4_LayoutPlan | `tools/planners/plan_layout.py` | role plan, source PDF | `reports/layout_plan.json` |
| S5_GenerateCandidate | `tools/generators/generate_candidate.py` | source PDF, layout plan | candidate PDF, `reports/generation_evidence.json`, PNG previews |
| S6_QualityGate | `tools/validators/validate_quality.py` | generation evidence | `reports/quality_gates.json` |
| L0_RepairSelection | `tools/repairs/plan_repairs.py` | quality gates | `reports/repair_plan_0.json` |
| S7_ProcessAudit | `tools/validators/validate_process.py` | all reports and tools | `reports/process_audit.json` |

## Required Evidence

The process audit requires these artifacts:

- `reports/run_request.json`
- `reports/tool_probe.json`
- `reports/source_structure.json`
- `reports/role_plan.json`
- `reports/layout_plan.json`
- `reports/generation_evidence.json`
- `reports/quality_gates.json`
- `reports/repair_plan_0.json`
- `reports/state_trace.json`
- `reports/decision_log.jsonl`
- `reports/operation_log.jsonl`
- `reports/model_interactions.jsonl`

The process audit also requires these static execution assets:

- `README.md`
- `EXECUTION.md`
- `contracts/state_machine.md`
- `contracts/tool_contracts.md`
- `contracts/execution_procedure.md`
- `prompts/templates/visual_quality_adjudication.prompt.json`
- `prompts/templates/repair_selection.prompt.json`
- all stage tools under `tools/probes/`, `tools/planners/`, `tools/generators/`, `tools/validators/`, and `tools/repairs/`

## Logs

- `operation_log.jsonl`: every executed command, state, return code, stdout tail, stderr tail.
- `decision_log.jsonl`: boundary decisions, tool dispatch decisions, quality verdict, selected repair family.
- `model_interactions.jsonl`: every LLM call. Current runner records one `not_invoked` entry because no LLM call is made.
- `state_trace.json`: ordered state execution trace.

## Gate And Repair Mapping

The current validator maps failures this way:

| Gate | Dimension | Repair Family |
|---|---|---|
| `all_groups_fit` | `overflow` | `expand_or_reflow_slot` |
| `source_relative_font_floor` | `font_floor` | `reflow_before_shrink` |
| `local_text_overlap` | `visual_crowding` | `vertical_flow_relayout` |

`local_text_overlap` is source-relative. It fails only when translated output overlap exceeds the source-region overlap baseline by more than the font-derived tolerance.

Planner-side `source_line_grid_container_relayout` derives local containers from the source PDF's adjacent horizontal and vertical drawing lines. It uses only minimal adjacent grid cells, never cross-column combinations. Inside each detected container, top-band heading-like groups keep heading treatment, lower groups become body flow, and long translated text receives source-relative font bounds plus container-width-derived target heights.

Planner-side `source_graphic_boundary_limit` uses source drawing-grid vertical edges as hard visual boundaries. When a left-side label, chart note, or footnote would expand into a neighboring card/table region, the planner limits the target width at the edge and recomputes the target height for natural wrapping.

Planner-side `filled_panel_compact_stack` detects non-white source-filled rectangles and stacks compact labels inside the filled panel. The erase background uses the panel fill color, not a sample from the text interior, so the renderer does not leave white bars behind compact text.

Planner-side `vertical_flow_relayout` applies only to text-flow roles (`body`, `section_heading`, `red_note`) in the same source-derived column. It excludes KPI metric/value stacks and repeated page bands. The flow calculation reserves the generator's possible draw-height expansion before placing the following block, so a box that fits only after font-size fallback does not collide with the next paragraph.

Planner-side `section_pushdown_after_source_rule` detects long source-derived horizontal section rules and may shift downstream translated groups downward when upstream translated content grows into the next section boundary and there is page-bottom capacity. This is a geometry-derived pushdown; it must not branch on page ids or text.

Text sanitization removes single-letter symbol-font leakage before an uppercase word, for example when a bullet glyph is extracted as a plain ASCII letter. The actual bullet/color rendering remains source-span-derived.

Candidate rendering is two-phase:

1. erase all planned source/target rectangles;
2. insert all translated text.

This prevents later erase rectangles from covering text that was already inserted by an earlier group.

The repair selector records the selected family in `repair_plan_0.json`. Current round22 does not yet auto-apply the repair and rerun S3-S6. Therefore a quality FAIL after L0 is still terminal for this experiment.

## Prompt Boundary

Prompt templates live under `prompts/templates/`.

Current runner behavior:

- does not call OpenAI or any other LLM;
- records this in `reports/model_interactions.jsonl`;
- uses deterministic local extraction, planning, generation, and gate tools.

If a future runner calls an LLM, it must record:

- template id;
- filled slot values;
- input artifact references;
- output dimensions;
- selected repair family;
- rejected alternatives;
- uncertainty.

## Anti-Overfit Rules

Runtime tools must not use:

- exact source page numbers as branch conditions;
- exact source text phrases as branch conditions;
- exact sample numeric values as branch conditions;
- absolute local paths;
- offline reference files;
- imports from `pdf_translation_workflow_core`.

Allowed runtime features are source-derived:

- page-local font-size ranks and quantiles;
- page-local text geometry;
- detected color families;
- symbol-font presence;
- generic currency, percentage, and unit token patterns;
- neighboring column or panel geometry extracted from the source page;
- adjacent source drawing-line grids, filled source panels, and long source section rules.

## Current Expected Outcome

As of this package version:

- process audit should pass after a clean run;
- product quality is expected to fail if any group remains `overflow_after_fit`;
- product quality is expected to fail if a group has to shrink below the configured source-relative font floor;
- a failed product gate means the candidate PDF is not acceptable for merge.
