# Round22 State Machine

## States

| State | Purpose | Tool | Required Input | Required Output | Failure State |
|---|---|---|---|---|---|
| S0_Request | Confirm package boundary and input paths | `run_round22_workflow.py` | source PDF, translations JSON | `run_request.json` | S_FAIL_PROCESS |
| S1_ToolProbe | Verify local runtime dependencies | `tools/probes/probe_runtime.py` | none | `tool_probe.json` | S_FAIL_TOOLING |
| S2_SourceExtract | Extract page geometry, text spans, colors, font hierarchy | `tools/probes/extract_source_structure.py` | source PDF | `source_structure.json` | S_FAIL_TOOLING |
| S3_RolePlan | Classify source blocks into layout roles using current-page statistics | `tools/planners/plan_roles.py` | `source_structure.json`, translations JSON | `role_plan.json` | S_FAIL_PROCESS |
| S4_LayoutPlan | Decide target rectangles, font policy, erase areas, and fit attempts | `tools/planners/plan_layout.py` | `role_plan.json` | `layout_plan.json` | S_FAIL_PROCESS |
| S5_GenerateCandidate | Render translated candidate PDF | `tools/generators/generate_candidate.py` | source PDF, `layout_plan.json` | candidate PDF, `generation_evidence.json` | S_FAIL_TOOLING |
| S6_QualityGate | Compare source-derived layout expectations with candidate evidence and previews | `tools/validators/validate_quality.py` | `generation_evidence.json`, previews | `quality_gates.json` | S_FAIL_QUALITY |
| Lx_RepairLoop | Select and apply exactly one repair family for each blocking failure class | `tools/repairs/plan_repairs.py` + relevant planner/generator | `quality_gates.json` | `repair_plan_<n>.json` | S_FAIL_QUALITY |
| S7_ProcessAudit | Verify traces, input purity, anti-overfit, and package boundary | `tools/validators/validate_process.py` | all reports | `process_audit.json` | S_FAIL_PROCESS |
| S_SUCCESS | Candidate passes process and product gates | none | `quality_gates.json`, `process_audit.json` | final PDF | none |

## Loop Semantics

`Lx_RepairLoop` is not a serial state after all work is complete. It wraps the local state sequence:

S3_RolePlan -> S4_LayoutPlan -> S5_GenerateCandidate -> S6_QualityGate.

If S6 reports blocking failures, one repair family is selected, applied, and the wrapped sequence repeats. Every loop must record:

- loop index
- blocking gate ids
- selected repair family
- reason for rejecting other repair families
- changed parameters or changed tool behavior
- output candidate path
- post-repair gate result

Round22 implementation note: the current runner implements repair-family selection and logging only. It does not yet auto-apply the repair or repeat S3-S6. Therefore this package can validate extraction, planning, generation, gates, and repair selection, but it cannot claim visual closure when product gates fail.

## Terminal Rules

- `S_SUCCESS` requires both product gate PASS and process audit PASS.
- A candidate PDF with `product_quality_verdict=FAIL` is an experiment artifact, not a deliverable.
- Runtime tools may not read `offline_reference_compare/`.
