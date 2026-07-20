# Round27 Execution Procedure

## Command

```powershell
python -B run_round27_batch.py
```

## State And Tool Flow

| Stage | Purpose | Tool or file | Required evidence |
|---|---|---|---|
| S0 Request | Declare source PDF, language pair, run mode | `run_round27_contract_case.py` | `reports/run_request.json` |
| S1 Contract Load | Load contracts, prompts, tool binding map, workspace boundary | `tools/validators/build_tool_binding_map.py` | `reports/contract_load_record.json`, `reports/tool_binding_map.json` |
| S2 Tool Probe | Check runtime and PDF libraries | `tools/probes/probe_runtime.py` | `reports/tool_probe.json` |
| S3 Source Extract | Extract text, bbox, color, font, page stats | `tools/probes/extract_source_structure.py` | `reports/source_structure.json` |
| S4 Page Strategy | Build source-derived page strategy | embedded in runner | `reports/page_strategy.json` |
| S5 Translation Plan | Materialize or validate translations | `tools/translators/materialize_google_gtx_translations.py` or embedded validator | `reports/semantic_translation_validation.json` |
| S6 Layout Plan | Build role plan and layout plan | `tools/planners/plan_roles.py`, `tools/planners/plan_layout.py` | `reports/role_plan.json`, `reports/layout_plan.json` |
| S7 Generate Candidate | Render candidate PDF | `tools/generators/generate_candidate.py` | `reports/generation_evidence.json`, `output/*_candidate.pdf` |
| S8 Quality Judge | Collect quality signals and triage | `tools/validators/validate_quality.py`, `tools/judges/compare_source_candidate.py` | `reports/quality_signals.json`, `reports/visual_adjudication.json` |
| Loop1 Repair | Apply selected primary repair | `tools/repairs/build_repair_patch.py`, `tools/repairs/apply_repair_patch.py` | `reports/repair_loop_0001.json` |
| Loop2 Promotion | Promote hard regression from memory and repair geometry | `tools/repairs/obstacle_aware_reflow.py`, `tools/repairs/apply_repair_patch.py` | `reports/repair_patch_0002.json`, `reports/repair_loop_0002.json` |
| S9 Process Audit | Materialize decision artifacts and validate process | `tools/validators/materialize_round27_artifacts.py`, `tools/validators/validate_decision_graph.py`, `tools/validators/validate_process.py` | `reports/process_audit.json`, `reports/round27_final_verdict.json` |

## Loop Rule

Loop1 chooses the primary failure from the first S8 triage.

Loop2 is entered only when loop1 fixes the target failure but creates or worsens a hard non-target failure. The failed `(issue_key, repair_atom)` is written to `repair_memory_ledger.json`; the regressed failure class is promoted to the new primary failure.

Current loop2 promotion implemented in this round:

- from `text_fit_overflow + expand_or_reflow_slot` rollback;
- to `cross_slot_overlap + obstacle_aware_reflow`.

## Acceptance Rule

Loop repair is accepted only when:

- selected failure improves;
- total blocking failures do not violate the loop baseline;
- non-selected hard failures do not regress;
- same issue+atom is not retried.

Passing the process audit is not the same as passing product quality.
