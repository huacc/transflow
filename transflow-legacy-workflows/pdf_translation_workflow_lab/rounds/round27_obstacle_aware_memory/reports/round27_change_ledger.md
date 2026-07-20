# Round27 Change Ledger

| What | Why | Before | After | Evidence |
|---|---|---|---|---|
| Copied execution contract into round27 workspace | Keep global contract read-only while allowing round-local fixes | Global contract was the only contract document | Round27 has its own `docs/设计/PDF_语义翻译回填_执行契约.md` copy | `ROUND26` §1.1 plus round27 changes |
| Added registry snapshot under `contracts/registry` | Decision graph validation needs a local truth source without importing core | round25 package had no registry snapshot | round27 validates IDs against local registry JSON | `contracts/registry/*.json` |
| Added multi-loop artifact materializer | round25/26 reports were flat or single-loop and did not expose problem-domain state | `quality_signals.json` and one repair loop only | independent evidence basket, signal ledger, domain buckets, triage, dispatch, patch, acceptance, multi-attempt memory ledger | `tools/validators/materialize_round27_artifacts.py` |
| Added decision graph validator | Lock artifact chain and dispatch/capability consistency before trusting a run | no validator | `validate_decision_graph.py` phase-A minimum validator | `reports/decision_graph_validation.json` |
| Clarified RepairPatch operation schema in round27 tool contract | Prevent arbitrary repair operations and overfitted patch shape | schema was implicit | allowed operation types and required fields are explicit | `contracts/tool_contracts.md` |
| Treat round25 dispatch table as seed | round25 maps `cross_slot_overlap` to a risky partial repair while registry points to missing `obstacle_aware_reflow` | seed dispatch could be mistaken for authority | dispatch conflict is recorded and registry is treated as normative future target | `reports/dispatch_result.json` |
| Added second-loop obstacle-aware repair | round26 stopped after one rejected repair and did not promote hard regressions | rejected expand repair ended the loop | cross-slot hard regression is promoted to `obstacle_aware_reflow`, recorded in memory, and revalidated | `reports/repair_loop_0002.json` |
