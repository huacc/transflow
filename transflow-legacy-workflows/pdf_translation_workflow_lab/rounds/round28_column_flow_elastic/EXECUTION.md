# Round28 Execution Procedure

## Command

```powershell
python -B run_round28_batch.py
```

## State And Tool Flow

| Stage | Purpose | Tool or file | Required evidence |
|---|---|---|---|
| S0 Request | Declare source PDF, language pair, run mode | `run_round28_contract_case.py` | `reports/run_request.json` |
| S1 Contract Load | Load contracts, prompts, tool binding map, workspace boundary | `tools/validators/build_tool_binding_map.py` | `reports/contract_load_record.json`, `reports/tool_binding_map.json` |
| S2 Tool Probe | Check runtime and PDF libraries | `tools/probes/probe_runtime.py` | `reports/tool_probe.json` |
| S3 Source Extract | Extract text, bbox, color, font, page stats | `tools/probes/extract_source_structure.py` | `reports/source_structure.json` |
| S4 Page Strategy | Build source-derived page strategy | embedded in runner | `reports/page_strategy.json` |
| S5 Translation Plan | Materialize or validate translations | `tools/translators/materialize_google_gtx_translations.py` or embedded validator | `reports/semantic_translation_validation.json` |
| S6A Role Plan | Classify text regions | `tools/planners/plan_roles.py` | `reports/role_plan.json` |
| S6B Page Classification | Classify page role, layout flow, columns, density | `tools/planners/classify_pages.py` | `reports/page_profiles.json` |
| S6C Base Layout | Build unmodified base layout | `tools/planners/plan_layout.py` | `reports/layout_plan.raw.json` |
| S6D Column-Flow Layout | Keep column width, reflow normal text vertically, preserve background | `tools/planners/apply_column_flow_elastic.py` | `reports/layout_plan.json`, `reports/column_flow_elastic_evidence.json` |
| S7 Generate Candidate | Render candidate PDF | `tools/generators/generate_candidate.py` | `reports/generation_evidence.json`, `output/*_candidate.pdf` |
| S8 Quality Judge | Collect quality signals and triage | `tools/validators/validate_quality.py`, `tools/judges/compare_source_candidate.py` | `reports/quality_signals.json`, `reports/visual_adjudication.json` |
| Loop1 Repair | Apply selected primary repair | `tools/repairs/build_repair_patch.py`, `tools/repairs/apply_repair_patch.py` | `reports/repair_loop_0001.json` |
| Loop2 Promotion | Promote hard regression from memory and repair geometry | `tools/repairs/obstacle_aware_reflow.py`, `tools/repairs/apply_repair_patch.py` | `reports/repair_patch_0002.json`, `reports/repair_loop_0002.json` |
| S9 Process Audit | Materialize decision artifacts and validate process | `tools/validators/materialize_round28_artifacts.py`, `tools/validators/validate_decision_graph.py`, `tools/validators/validate_process.py` | `reports/process_audit.json`, `reports/round28_final_verdict.json` |

## Page Classification Rule

Each page is handled independently. The page may fall into:

- body text page;
- financial table page;
- chart or metric page;
- cover or section page;
- mixed page.

Only body-like text regions on eligible pages enter column-flow elastic layout. Tables, charts,
metric grids, visual pages, and protected footers keep their original local geometry.

## Background Rule

Text background is not a repair variable.

- Erase source text bbox only.
- Do not erase the whole target bbox.
- Do not repaint photo or textured background to cover layout mistakes.

## Acceptance Rule

Loop repair is accepted only when:

- selected failure improves;
- total blocking failures do not violate the loop baseline;
- non-selected hard failures do not regress;
- same issue and repair atom are not retried.

Passing the process audit is not the same as passing product quality.
