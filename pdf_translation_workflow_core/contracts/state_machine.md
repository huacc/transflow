# State Machine Contract

## Generic States

| State | Purpose | Required artifacts | Exit gate |
|---|---|---|---|
| `S0_Request` | Restate goal, mode, inputs, non-goals | run header | Mode declared |
| `S1_ContractLoad` | Load process docs and core contracts | contract load record | Sections and core files present |
| `S2_ToolProbe` | Probe local tools, fonts, renderers, extraction libraries | tool probe JSON | Tool availability known |
| `S3_SourceExtract` | Extract page geometry, text, images/drawings, source renders | source extraction JSON, source PNGs | All pages represented |
| `S4_PageStrategy` | Classify page types and region roles | page strategy records | D1/D3 decisions recorded |
| `S5_TranslationPlan` | Build translation units, materialize D2 batch translations, assemble semantic translation JSON, and validate coverage | `translation_batch_manifest.json`, per-batch slot/model/validation records, semantic translations JSON, `semantic_translation_validation.json` in product-quality mode | All D2 batches validated and final semantic translation validation passes, or capability failure is recorded |
| `S6_LayoutPlan` | Build or revise explicit region-aware layout policy | `layout_policy.json`, optional LayoutSlot/RegionSlot plan | D4 decisions recorded, including constrained-slot vs fluid-body split, policy source, target composition, reflow-vs-preserve-line decisions, font profiles, and fallback policy |
| `S7_GenerateCandidate` | Generate a candidate PDF with a real backfill attempt if feasible | candidate PDF, candidate PNGs, generation evidence, translations/layout artifacts, semantic translation validation, or explicit generation failure | Candidate contains backfilled target-language text or explicit failure; fluid body uses target composition when policy requires it |
| `S8_VerifyProductQuality` | Evaluate machine and visual quality gates | quality gates JSON | D5/D7 decisions recorded, including target composition, font hierarchy, overlap residue, and constrained-slot integrity |
| `Lx_RepairLoop` | Repair one documented failure class at a time | repair decision, patch record, verification result | Failure fixed, deferred, or terminal |
| `Ax_AdaptiveChange` | Modify round-local docs/tools when the package is insufficient | change log, before/after manifests, verification result | Change recorded and verified |
| `S9_VerifyProcessContract` | Validate state trace, decision log, artifacts, and anti-overfit evidence | process validator output, `anti_overfit_scan.json` | Process pass/fail known and reusable core has no blocking sample-specific hits |
| `S_DONE_PROCESS_VALIDATED` | Process validation success | audit report | Only for process-validation mode |
| `S_DONE_PRODUCT_ACCEPTED` | Product-quality success | final PDF, final previews, final report | Only when all product gates pass |
| `S_FAIL_PROCESS_CONTRACT` | Missing process evidence or invalid contract execution | failure report | Terminal |
| `S_FAIL_QUALITY` | Product-quality mode cannot meet quality gates within budget | failure report | Terminal |
| `S_FAIL_TOOLING` | Required tool unavailable and no valid fallback | failure report | Terminal |
| `S_FAIL_CAPABILITY` | Requested product capability is not implemented or not wired | failure report | Terminal |

## Composite State Semantics

`Lx_RepairLoop` is a composite state, not a linear list of one-off states.

The top-level state machine enters `Lx_RepairLoop` from `S8_VerifyProductQuality` only when a blocking quality failure is repairable. Inside the loop, the workflow repeats:

```text
classify failure -> select repair atom -> apply repair -> regenerate candidate -> rejudge
```

The loop exits only through one of these outcomes:

| Outcome | Exit target |
|---|---|
| repair atom changes source extraction, OCR boundary, or generation evidence linkage | `S3_SourceExtract` or `S7_GenerateCandidate` |
| repair atom changes semantic translation, compact variant, or terminology choice | `S5_TranslationPlan` |
| repair atom changes layout policy, region role, font profile, or reflow mode | `S6_LayoutPlan` |
| repair atom only requires candidate regeneration | `S7_GenerateCandidate` |
| target gates already pass after rejudge | `S9_VerifyProcessContract` |
| still failing and repairable | repeat inside `Lx_RepairLoop` |
| no valid repair | `S_FAIL_QUALITY` |
| required capability missing | `S_FAIL_CAPABILITY` |
| tooling failure | `S_FAIL_TOOLING` |

Do not model historical repair attempts such as `L1 -> L2 -> L3` as the generic state machine. Those are execution trace entries inside or around the composite repair loop.

`Ax_AdaptiveChange` is a separate composite state for methodology/tooling changes. It must not be confused with product-quality repair. Adaptive changes operate on round-local docs, prompts, contracts, or tools and must produce change manifests.

## State Trace Schema

Every transition must be recorded:

```json
{
  "transition_id": "T07",
  "from": "S6_LayoutPlan",
  "to": "S7_GenerateCandidate",
  "entry_condition": "layout slots and render patches exist",
  "run_mode": "product_quality",
  "tools": ["Python", "PyMuPDF", "Codex/OpenAI model"],
  "input_artifacts": ["tmp/pdfs/source_extraction.json"],
  "output_artifacts": ["tmp/pdfs/render_patches.json"],
  "decision_record_ids": ["D4_layout_plan"],
  "gates": [
    {"gate_id": "render_patches_complete", "status": "pass", "evidence": "..."}
  ],
  "next_state_rule": "generate candidate or fail tooling",
  "timestamp_local": "YYYY-MM-DD HH:MM:SS"
}
```

## Repair Loop Schema

Each repair iteration must be recorded. `loop_iteration` increases on every pass through the composite state.

```json
{
  "loop_id": "L2",
  "loop_iteration": 1,
  "entered_from_state": "S8_VerifyProductQuality",
  "failure_class": "text_fit_overflow",
  "failed_gate_ids": ["text_fit", "visual_similarity"],
  "repair_atom": "reduce_font_or_reflow",
  "target_state": "S6_LayoutPlan",
  "patch_scope": ["page_index", "region_id", "slot_id"],
  "tools": ["apply_patch", "Python", "PyMuPDF"],
  "expected_effect": "reduce overflow without lowering text-area ratio below threshold",
  "verification_to_run": ["render_png", "extract_text", "quality_metrics"],
  "exit_condition": "gate pass or max attempts reached"
}
```

## Adaptive Change Schema

```json
{
  "change_id": "C01",
  "entered_from_state": "S8_VerifyProductQuality",
  "trigger_failure": "missing required evidence or insufficient tool behavior",
  "hypothesis": "changing this contract/tool will make the failure explicit or resolve it",
  "files_changed": ["relative/path"],
  "change_type": "doc_contract|prompt|tool|test_or_validator|report_only",
  "before_evidence": ["docs/reports/round##_change_manifest_before.json"],
  "after_evidence": ["docs/reports/round##_change_manifest_after.json"],
  "verification_to_run": ["command"],
  "result": "pass|fail|partial",
  "core_backport_recommendation": true
}
```

Adaptive changes are round-local. A later maintainer must decide whether to backport them into the root workflow core.

## S5 Translation Materialization Loop

`S5_TranslationPlan` is also a composite state. It is not satisfied by a single broad instruction such as "translate all units".

In `product_quality`, S5 must run this bounded loop:

```text
build translation batch manifest
for each batch:
  fill D2_translation prompt slots from that batch slot_values JSON
  persist prompt_instance.json
  persist raw model_output.json
  validate batch with validate_translation_batch.py
assemble validated batch outputs into docs/input/semantic_translations/<case_id>.translations.json
validate assembled translations with validate_semantic_translations.py
```

Required tools:

| Step | Tool | Required output |
|---|---|---|
| build batches | `tools/planners/build_translation_batch_manifest.py` | `translation_batch_manifest.json`, `translation_batches/<batch_id>.slot_values.json` |
| batch judgement | `templates/D2_translation.prompt.json` | `translation_batches/<batch_id>.prompt_instance.json`, `translation_batches/<batch_id>.model_output.json`, `translation_batches/<batch_id>.decision_record.json` |
| validate batch | `tools/validators/validate_translation_batch.py` | `translation_batches/<batch_id>.validation.json` |
| assemble batches | `tools/generators/assemble_semantic_translations.py` | `docs/input/semantic_translations/<case_id>.translations.json`, `translation_assembly_evidence.json` |
| final semantic gate | `tools/validators/validate_semantic_translations.py` | `semantic_translation_validation.json` |

S5 exits only through these outcomes:

| Outcome | Exit target |
|---|---|
| every required batch output exists, every batch validation passes, assembled coverage is complete, and final semantic validation passes | `S6_LayoutPlan` |
| model/API/human translation capacity is unavailable or cannot materialize current-run units | `S_FAIL_CAPABILITY` |
| batch outputs contain placeholders, metadata-style pseudo translations, missing units, invalid target language, or token preservation failures | repair inside S5 if bounded and documented; otherwise `S_FAIL_CAPABILITY` |
| state trace, prompt artifacts, batch refs, or validation artifacts are missing | `S_FAIL_PROCESS_CONTRACT` |

The executor may choose a smaller batch size to stay within model context. It must not silently drop units or generate placeholder text to satisfy coverage.

## Mode-Specific State Rule

In `process_validation`, quality failures may go to `S_DONE_PROCESS_VALIDATED` only if they are explicitly recorded as observations.

In `backfill_candidate_validation`, a placeholder candidate may be generated to prove redaction/backfill mechanics. Semantic quality must still fail unless a real semantic provider is wired.

In `product_quality`, quality failures must go to `Lx_RepairLoop`, `S_FAIL_QUALITY`, or `S_FAIL_TOOLING`. They cannot go to a done state.

In `product_quality`, missing or invalid semantic translations must go to `S_FAIL_CAPABILITY` before placeholder candidate generation. The workflow must not create a product candidate by falling back to `backfill_candidate_validation`.

## Candidate Generation Authenticity Rule

In `product_quality` mode, `S7_GenerateCandidate` must not be satisfied by copying the source PDF.

Minimum acceptable candidate evidence for `backfill_candidate_validation`:

```json
{
  "real_backfill_pdf": true,
  "translations_json": "...",
  "layout_plan_json": "...",
  "layout_policy_json": "...",
  "layout_policy_sha256": "...",
  "redacted_line_count": 1,
  "inserted_line_count": 1
}
```

A low-fidelity placeholder translation is allowed only for `backfill_candidate_validation`, but it must fail `semantic_coverage` and cannot reach `S_DONE_PRODUCT_ACCEPTED`.

Minimum acceptable candidate evidence for `product_quality`:

```json
{
  "real_backfill_pdf": true,
  "translation_provider": "semantic_provider_name",
  "translation_quality": "semantic_translation",
  "semantic_coverage": "full_semantic_translation",
  "input_semantic_translations": "docs/input/semantic_translations/<case_id>.translations.json",
  "semantic_translation_validation": "PASS",
  "strategy": "redact_extractable_<source_language>_lines_and_insert_semantic_<target_language>_regions",
  "layout_policy_json": "...",
  "layout_policy_sha256": "...",
  "layout_policy_version": "...",
  "layout_policy_source": "...",
  "translations_json": "...",
  "layout_plan_json": "...",
  "redacted_line_count": 1,
  "inserted_line_count": 1,
  "inserted_unit_count": 1,
  "semantic_translated_unit_count": 1,
  "preserved_target_language_unit_count": 0,
  "inserted_region_count": 1
}
```

If semantic translations are missing, invalid, placeholder-like, or metadata-style pseudo translations, `product_quality` must fail at `S_FAIL_CAPABILITY` before creating a product candidate. Examples of pseudo translations include `This line reports...`, `This line describes...`, `本行说明...`, `本行列示...`, and leaked preservation instructions such as `保留数值与标记...`.

## Layout Reflow State Rule

Inside `S6_LayoutPlan`, the workflow must first create a run-local `layout_policy.json`. The policy can be generated by `tools\planners\build_layout_policy.py` from current extraction statistics plus the matching generic language layout profile, then revised by D4 model judgement. The generator must consume this file; it must not hide these choices as constants in Python.

Every extracted text group must receive one of these layout modes through policy rules:

| Layout mode | Use when | Required next tool behavior |
|---|---|---|
| `region_reflow` | body paragraphs, footnote blocks, multi-line headings, or any multi-line semantic block whose target-language text should use the full region width | redact original lines individually; insert one target-language textbox across the union region |
| `region_flow` | aligned wide body paragraph regions that form one continuous article column and otherwise create large internal blank gaps | redact original lines individually; insert one flowing target-language article textbox with paragraph separators; blank space after the final paragraph is allowed |
| `event_card` | narrow multi-line event or milestone descriptions on mixed image/text pages | keep each event local, use event-card font/variant rules, and do not merge across years, images, or adjacent event cards |
| `table_note` | wide `Note:` / `Notes:` blocks near tables, especially when source font is smaller than body text | keep note/body font hierarchy and do not merge into body_flow |
| `table_cell` | compact labels or cells on dense table/chart pages | preserve line/cell geometry, use D2-supplied compact variants, and do not merge into body_flow |
| `rotated_horizontal_text_image` | narrow side navigation labels whose source text is a horizontal label rotated as one unit | render horizontal target-language text as one label image, rotate the whole label, insert it into the source slot, and verify with a back-rotated crop |
| `line_preserve` | single-line labels, chart ticks, compact table labels, legends, vertical navigation, or text whose source layout is intentionally fragmented | redact and insert per unit |
| `visual_only` | embedded image text that is not extractable and no OCR path is authorized | do not pretend it was translated; record OCR/tooling boundary |

For `fluid_body` / `body_flow`, the policy may also enable `target_composition`. In that case source bboxes are redaction, reading-order, and anchor evidence, not hard target containers. The generator must recompute the target textbox from current-page body band, page margins, bottom limit, and overlap guard before font-size shrink. This rule does not apply to constrained slots such as table cells, legends, chart labels, side navigation, page numbers, or short labels.

The transition from `S6_LayoutPlan` to `S7_GenerateCandidate` is invalid if `layout_policy.json` is missing, if `language_pair_profile` / `language_profile_json` / `language_profile_sha256` are missing in product-quality mode, if paragraph-like blocks are all marked `line_preserve` without a recorded D4 justification, or if policy numeric values cannot be traced to current-run statistics, visual adjudication, language profile, or explicit user feedback evidence.

`region_reflow` and `region_flow` must not cross visible source anchors. A visible source line that is not a translation unit, such as a year, numeric heading, bullet-only label, or separator label, splits the region. The generator must express this through `source_separator_policy` and generation evidence fields `source_block_ids` / `source_line_indexes`.

Visible source lines that are already in the target language may be redacted and redrawn as `preserve_already_target_language_span` when a recomposed target frame would otherwise overlap them. They are preservation units, not semantic translations, and must be counted in `preserved_target_language_unit_count`.

Inside `region_flow`, same-paragraph continuations and new paragraphs must be distinguished by current-run y-gap evidence. The policy fields `flow_grouping.body.paragraph_gap_pt`, `line_joiner_en`, `line_joiner_zh`, and `paragraph_separator` define this behavior. Fixed `\n\n` insertion between every merged source line is invalid because it creates artificial paragraph gaps.

Short same-column continuation lines may join an active `region_flow` only when policy fields `allow_short_continuation_lines` and `min_continuation_width_page_ratio` permit it. Dense table/chart pages must still preserve cells and legends, but lower-page article bands may re-enter `region_flow` when `allow_dense_page_body_below_y_ratio` is present and current-run geometry proves same-column body copy.

`S7_GenerateCandidate` must probe textbox fit on a temporary page before drawing. Failed font-size attempts must not render to the real candidate page. If evidence or PNG review shows failed probe residue, the run must return to `S6`/`S7` repair or fail product quality.

If `S8_VerifyProductQuality` observes short target-language lines caused by inherited source bboxes, the failure class is:

```json
{
  "failure_class": "line_fragmentation",
  "repair_atom": "region_reflow",
  "from": "S8_VerifyProductQuality",
  "to": "Lx_RepairLoop"
}
```

If `S8_VerifyProductQuality` observes translated text moved across a visible source anchor, or `evaluate_pdf_quality.py` reports `source_anchor_order=fail`, the failure class is:

```json
{
  "failure_class": "source_anchor_order_mismatch",
  "repair_atom": "split_region_at_source_separator",
  "from": "S8_VerifyProductQuality",
  "to": "Lx_RepairLoop"
}
```

If `S8_VerifyProductQuality` observes body paragraphs with large blank space between active paragraphs while the source is a continuous article column, the failure class is:

```json
{
  "failure_class": "paragraph_density_mismatch",
  "repair_atom": "body_flow_grouping",
  "from": "S8_VerifyProductQuality",
  "to": "Lx_RepairLoop"
}
```

If `S8_VerifyProductQuality` observes notes, footnotes, body copy, headings, or table labels losing their source-relative size hierarchy, the failure class is:

```json
{
  "failure_class": "font_hierarchy_ratio_mismatch",
  "repair_atom": "role_font_profile_or_region_classification",
  "from": "S8_VerifyProductQuality",
  "to": "Lx_RepairLoop"
}
```

If `S8_VerifyProductQuality` observes side navigation inserted as stacked target-language characters when the source uses rotated horizontal text, or observes inconsistent writing mode inside the same navigation group, the failure class is:

```json
{
  "failure_class": "sidebar_orientation_fail",
  "repair_atom": "rotated_horizontal_text_image_draw_mode",
  "from": "S8_VerifyProductQuality",
  "to": "Lx_RepairLoop"
}
```

The repair loop must regenerate the candidate and re-run product gates. It cannot mark the run accepted merely because semantic coverage passed.
