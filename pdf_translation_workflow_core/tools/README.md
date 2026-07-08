# Tool Directory

This directory classifies tool roles. It is not a dump for sample-specific scripts.

## Subdirectories

| Directory | Intended contents |
|---|---|
| `probes` | environment and PDF capability probes |
| `planners` | run-local policy planners that convert extraction evidence into explicit generator inputs |
| `renderers` | source/output rendering helpers |
| `generators` | candidate PDF generation templates |
| `validators` | process and product quality validators |
| root-level `run_*.py` | workflow orchestrators that call the atomic tools and write state/operation/decision evidence |

## Executable Tools

| Tool | Category | Input contract | Output contract | Failure signal | Current role |
|---|---|---|---|---|---|
| `run_semantic_product_quality_round.py` | orchestrator | source PDF directory, semantic translation JSON directory, round input/output/report directories | candidate PDFs, per-case visual artifacts, `state_trace.json`, `operation_log.jsonl`, `decision_log.jsonl`, `repair_loop_<n>.json`, process validation and final verdict | missing translations, failed atomic tool, failed process contract, product quality fail | generic product-quality runner that enforces the state machine and records real repair-loop execution boundaries |
| `probes\tool_probe.py` | probe | output JSON path | Python/package/font/executable capability JSON | required package/font unavailable | records environment facts before a run |
| `probes\extract_pdf_structure.py` | probe | input PDF path, output JSON path | pages, line bboxes, fonts, drawing counts, image counts, page-type guess | unreadable PDF or empty extraction | provides source/output structural evidence |
| `planners\build_layout_policy.py` | planner | source extraction JSON, optional semantic translations JSON, optional language profile JSON, output policy path | run-local layout policy JSON with statistics, language_pair_profile, classification rules, source-relative font profiles, reflow and fallback policy | missing extraction, empty translatable units, invalid JSON, profile/language mismatch | creates explicit D4 policy input so generators do not hide hardcoded visual constants; reusable profiles express role sizes as ratios/current-page quantiles rather than sample fixed point sizes |
| `planners\build_role_plan.py` | planner | source extraction JSON, semantic translations JSON, optional layout policy JSON, output role plan path | `role_plan.json` with page/group roles, line ids, source rects, target text, source-relative role evidence | missing translations for required source units, empty required units, invalid JSON | creates an auditable D4 role layer from current-run geometry/font/color/page statistics before layout planning; this is the promotion target for round22 role ideas, not a copy of round22 sample code |
| `planners\build_layout_plan.py` | planner | `role_plan.json`, `layout_policy.json`, output layout plan path | generator-consumable `layout_plan.json` with projected target rects, erase rects, draw modes, source-relative font profiles, overlap hints, and `layout_plan_consumable_by_generator=true` | missing role plan/policy, invalid rects, empty groups | S6 planner that makes current-run target layout decisions explicit before S7; `generate_semantic_backfill.py --planned-layout` consumes this plan in product-quality v2 runs |
| `planners\build_translation_batch_manifest.py` | planner | source extraction JSON, case id, source/target language metadata, batch dir, output manifest path | D2 translation batch manifest plus per-batch slot_values JSON files | missing extraction, empty source units, invalid language metadata | creates bounded S5/D2 batch inputs so translation materialization is executable and auditable |
| `renderers\render_pdf.py` | renderer | input PDF, output directory, prefix, zoom, manifest path | per-page PNG files and render manifest | missing images or render exception | provides source/output visual evidence |
| `renderers\render_source_output_crop.py` | renderer | source PDF, output PDF, page index, crop rectangle, output PNG, manifest path | source-vs-output crop contact sheet plus manifest | invalid page/crop or render exception | provides focused visual evidence for D7 dimensions such as font hierarchy, paragraph gaps, and sidebar orientation |
| `generators\generate_backfill_candidate.py` | generator | input PDF, source extraction JSON, output PDF, translations/layout/evidence JSON paths | low-fidelity Chinese backfill PDF plus translations/layout/evidence JSON | missing input/font/output failure | backfill-candidate generator; proves real backfill mechanics but not semantic quality |
| `generators\generate_semantic_backfill.py` | generator | input PDF, source extraction JSON, semantic translations JSON, layout policy JSON, output PDF, translations/layout/evidence JSON paths | semantic target-language backfill PDF plus translations/layout/evidence JSON | missing/invalid semantic translations, missing policy, font/output failure | product-quality candidate generator; redacts source-language lines and executes explicit region-reflow policy; never falls back to placeholder text |
| `generators\materialize_d2_translation_batches.py` | generator | translation batch manifest, D2 prompt template, per-batch slot values, workspace-boundary reports, optional translation cache, optional chunk size limits | per-batch prompt_instance/model_output/decision_record JSON plus materialization summary and chunk evidence | missing workspace boundary PASS, provider failure, marker split failure after fallback, target-script validation failure after retry | runtime D2 batch materializer for real semantic translations; normalizes request text with NFKC, translates bounded chunks with stable unit markers, falls back to per-unit requests only when marker split fails, validates cache hits, records provider, and never creates placeholders |
| `generators\assemble_semantic_translations.py` | generator | translation batch manifest and per-batch D2 model output JSON files | assembled semantic translations JSON plus assembly evidence | missing batch output, duplicate unit, missing unit, incomplete coverage | materializes the final S5 semantic translation input after batch outputs validate; never invents translations |
| `generators\generate_minimal_candidate.py` | generator | input PDF, output PDF, evidence JSON path | candidate PDF plus evidence JSON | missing/unreadable input | debug-only smoke stub; copies source to prove quality gates can fail |
| `validators\evaluate_pdf_quality.py` | validator | source PDF, candidate PDF, output JSON, optional generation evidence, optional visual adjudication JSON | blocking gate verdict plus structural metrics | PDF open or metric exception | automated partial product gate; records visual gate result when adjudication artifact is supplied |
| `validators\collect_visual_region_metrics.py` | validator | source PDF, candidate PDF, candidate generation evidence JSON, source extraction JSON, output JSON, optional crop directory | source-relative role-level title/metric_value/body/table/footnote/sidebar/image/background metrics plus crop evidence | missing PDFs/evidence/source baseline or render/crop error | promotes visual review from page-level similarity into source-vs-output role gates such as source baseline coverage, hero banner title readability, metric value hierarchy, and image color integrity |
| `validators\write_visual_adjudication.py` | validator | `visual_region_metrics.json`, optional render manifest and repair plan refs | D7 `visual_adjudication.json` with `PASS`, `PASS_WITH_WARN`, or `FAIL` | missing/invalid visual metrics | materializes current-run deterministic role gates into the visual adjudication artifact consumed by product quality |
| `validators\validate_translation_batch.py` | validator | one D2 batch slot_values JSON and its model_output JSON | batch-level coverage/authenticity verdict | missing units, placeholder text, pseudo-translation text, token preservation failure | blocks assembly until every D2 batch is real semantic translation output |
| `validators\validate_semantic_translations.py` | validator | source extraction JSON, semantic translations JSON, output JSON | translation coverage/authenticity verdict | missing units, placeholder text, pseudo-translation text, token preservation failure | blocks product-quality generation before candidate PDF creation |
| `validators\validate_workspace_boundary.py` | validator | workspace root plus planned or observed artifact paths | resolved path containment report with `workspace_boundary_verdict` | any artifact path resolves outside workspace root, or required existing artifact is missing | guards every runtime write/read set before the state continues; process-contract failure if it fails |
| `validators\validate_translation_manifest_boundaries.py` | validator | one `translation_batch_manifest.json` and workspace root | one `workspace_boundary_ref` JSON per D2 batch plus a summary JSON | any planned D2 batch artifact resolves outside workspace root, or required `slot_values_ref` is missing | automates S5B workspace-boundary reports for large documents without weakening the per-batch boundary contract |
| `validators\validate_process_artifacts.py` | validator | run directory, output JSON | state/operation/decision/evidence contract verdict | missing required trace artifacts | process-contract gate |
| `validators\scan_core_overfit.py` | validator | core root directory, run-local token file outside core, output JSON | anti-overfit scan verdict with blocking/warning hits | sample-specific token found in tools/contracts/prompts | proves sample facts did not enter reusable core logic |
| `repairs\plan_visual_region_repairs.py` | repair planner | `visual_region_metrics.json`, output JSON | repair atoms grouped by failed role gate | missing metrics or no actionable failed gates | maps failed role gates to generic repair atoms without rewriting PDFs or inventing translations |
| `repairs\build_repair_patch.py` | repair planner | `layout_policy.json`, `visual_repair_plan.json`, optional `product_quality_gates.json`, selected gate/repair atom | `repair_patch_<n>.json` with selected failure, deferred failures, policy operations, and anti-overfit statement | no executable repair atom, selected repair not found, invalid JSON | materializes D8 repair selection as an auditable current-run patch before any policy or PDF regeneration happens |
| `repairs\apply_repair_patch.py` | repair executor | `layout_policy.json` plus `repair_patch_<n>.json` | repaired `layout_policy.<loop>.json` with `repair_overrides` and before/after change records | missing patch, unsupported operation, invalid policy JSON | applies declarative RepairPatch operations only; it does not inspect sample identity or rewrite PDFs |

## Known Automation Boundary

Runtime evidence writers must be workspace-boundary guarded. `validate_workspace_boundary.py` resolves planned input/output paths against the current execution root and fails before a write if a target escapes that root. This guard is required for model batch materialization because relative-path writes in nested spike directories can otherwise land in the parent project. Runtime artifacts should be written by tools or shell/Python commands anchored to the execution root; `apply_patch` is reserved for framework edits and must not be used as the normal writer for batch model outputs or reports.

For large documents, `validate_translation_manifest_boundaries.py` may be used immediately after `build_translation_batch_manifest.py`. It writes the same per-batch `workspace_boundary_ref` files that the state machine requires, but does so from the manifest in one deterministic pass. This is a scale optimization, not a relaxation: every batch still has its own boundary report, and any failed batch report remains `S_FAIL_PROCESS_CONTRACT`.

`materialize_d2_translation_batches.py` uses chunked provider calls by default because full annual reports can contain tens of thousands of source units. Chunking is bounded by `--chunk-units` and `--chunk-chars`; each unit is wrapped with a stable neutral marker, and the tool records `chunk_translation` evidence in every model output. If a provider drops or mangles markers, the affected units fall back to per-unit requests. The tool must still pass `validate_translation_batch.py`; chunking cannot be used to bypass semantic coverage, target-script checks, token preservation, or placeholder rejection.

`build_translation_batch_manifest.py` owns the shared `line_is_translatable` semantics. `validate_semantic_translations.py`, `build_layout_policy.py`, and `generate_semantic_backfill.py` must follow that same required-unit boundary so neutral identifiers, already-target-language visible text, URLs, codes, ratings, legal entity names, and numeric/code-only fragments are not accidentally counted as D2 semantic obligations or erased as translated regions. For `target_language=en`, `materialize_d2_translation_batches.py` may remove residual CJK only from Latin-dominant mixed identifiers/entities by generic character-ratio and token-shape rules.

`run_semantic_product_quality_round.py` must use idempotent copy behavior for source PDFs and semantic translation JSONs. If the resolved source and target paths are identical, it records a skipped copy instead of failing on Windows same-path or file-lock behavior; if they differ, normal workspace-boundary guarding still applies.

`evaluate_pdf_quality.py` is not a complete visual judge by itself. It currently blocks on page count, page geometry, source-language text residue based on target language, generation authenticity when evidence is supplied, source anchor order, semantic translation preflight, text-fit warnings, placeholder semantic coverage, visual adjudication failures when supplied, and source-relative role gates from `visual_region_metrics.json` when supplied. For Chinese targets, Latin tokens are allowed only when they appear in validated target-language evidence, match generic neutral identifier/code patterns such as CUSIP, ISIN, SEDOL, RIC, LEI, single-letter rating codes, ticker-like codes, and uppercase alphanumeric identifiers, or are PDF-extractor fragments of an allowed brand/code token from the validated target evidence. Other Latin fragments remain source-residue failures. It records text-density and font-hierarchy metrics for review. Line fragmentation, paragraph density, internal paragraph gaps, table-grid damage, chart-label semantics, and perceived typographic rhythm still require PNG review plus a recorded model/human adjudication until those checks are promoted into deterministic validators.

In `product_quality`, once a semantic candidate PDF exists, the visual-closure tool chain is mandatory: render candidate pages, run `collect_visual_region_metrics.py`, run `plan_visual_region_repairs.py`, persist `visual_adjudication.json` by model/human review or `write_visual_adjudication.py`, then run `evaluate_pdf_quality.py` with both visual artifacts. `PASS_WITH_WARN` adjudication is acceptable when no dimension has `blocking=true`; `FAIL` remains blocking. Missing any of these artifacts is a process-contract failure, not merely a product-quality failure.

`collect_visual_region_metrics.py` is the deterministic bridge between rendered evidence and D7. It does not decide from document identity. It uses current-run source extraction, generation evidence, region kind, bbox geometry, source/output font ratio, fit status, rendered background/color samples, image counts, and crop contact sheets to classify quality roles. For redaction artifacts it compares both outside-edge background and inner-bbox dominant background, so a table header, colored band, or blue page background cannot pass just because the full-page dominant color is similar. For compact labels, event cards, legends, and sidebars, `background_delta` alone is not a blocking failure when inner background, text-image background, and residue evidence are clean; this prevents normal translated glyph/color changes from being misclassified as background patches. The `source_relative_visual_baseline` gate must pass before role gates can be trusted:

- `hero_banner_title`
- `title`
- `body`
- `table_text`
- `footnote`
- `legend`
- `sidebar`
- `event_card`
- `short_label`

Critical role failures block product acceptance even when full-page similarity is mostly acceptable.

`generate_semantic_backfill.py` evidence must be read carefully:

- `inserted_unit_count` proves all source text units were covered.
- `inserted_region_count` is allowed to be lower than `inserted_unit_count` because multi-line source-language blocks should be reflowed into fewer target-language regions.
- `fit_warning_count` must be `0` for product-quality acceptance.
- `source_block_ids` and `source_line_indexes` must prove that a reflow region did not cross visible untranslated anchors inside one source block.
- `strategy` should be `redact_extractable_<source_language>_lines_and_insert_semantic_<target_language>_regions`.
- `layout_policy_json`, `layout_policy_sha256`, `layout_policy_version`, and `layout_policy_source` prove layout parameters came from an explicit run-local policy instead of hidden constants in the generator.
- `role_plan.json` and `layout_plan.json` are S6 planner artifacts. In product-quality v2 runs, `generate_semantic_backfill.py --planned-layout <layout_plan.json>` must consume the plan and write runtime insertion evidence to `layout_execution.json` through `--layout-plan`.
- `candidate_generation_evidence.json` and `layout_execution.json` must record `layout_plan_input_json`, `layout_plan_consumed_by_generator`, and `layout_plan_consumed_page_indexes`. Missing or false consumption evidence is a process-contract failure for the v2 path, even if a candidate PDF was generated.
- `language_pair_profile`, `language_profile_json`, `language_profile_sha256`, and `layout_strategy` prove language-direction behavior came from an explicit generic profile, not sample-specific code.
- `layout_plan.json` region kinds must distinguish `body`, `body_flow`, `table_cell`, `table_note`, `legend`, `footnote`, `vertical_nav`, `compact_label`, and `heading` when those roles exist.
- `decorative_numeric_merge_repair_count` records generic repairs where extraction merged a text line with a trailing decorative numeral/section marker and abnormal bbox height; the repair must be based on current-page geometry and neighbor text rhythm, never on a known page number or title string.
- `body_flow` text must use policy-controlled y-gap joining. Same-paragraph source-wrapped lines should use the language-specific line joiner, while only larger paragraph gaps use `paragraph_separator`.
- Short same-column continuation lines may join an active `body_flow` only when the policy enables `allow_short_continuation_lines`, x/y geometry matches the active flow, and width exceeds `min_continuation_width_page_ratio`.
- Dense table/chart pages should keep compact labels as `table_cell`/`legend` preserve-line regions instead of merging them into `body_flow`; body copy bands below `allow_dense_page_body_below_y_ratio` may re-enter `body_flow` only when current-run geometry proves same-column article text.
- Matrix/table-diagram pages are hard-disabled for normal `body_flow`, `target_composition`, and target-language frame expansion. On these pages, wide table rows must not be classified as `table_note` or `footnote` unless they are explicit note markers or bottom-page notes under the policy's dense-page y-ratio thresholds. Page-top large headings and page-top lead body can bypass this hard-disable only when current geometry proves they are outside the table/diagram body.
- `target_language_reflow` may expand only declared region kinds and must obey `overlap_guard` so expanded target text frames do not invade the next same-column region.
- `expandable_text_slots` may expand declared headings and explanatory `short_label` regions before font shrink when current-run geometry proves target text is longer and right/down whitespace is available. Dense table/chart/matrix labels, legends, TOC items, and side navigation remain constrained slots.
- `generate_semantic_backfill.py` must probe textbox fit on a temporary page first. Failed font-size attempts must not be drawn on the real candidate page; the real page receives only the successful attempt or explicit fallback.
- After textbox probing fails, `constrained_text_image_fit` may be used only for policy-declared roles. Constrained table/legend/hard-slot short-label failures are layout-fit repairs first; they must not be routed to retranslation unless semantic validation proves a compact variant is missing. Ordinary explanatory short-label failures route first to `expandable_text_slot_reflow_repair`. `table_note`, `footnote`, and zh-to-en `body`/`body_flow`/`heading`/`short_label`/`event_card` can use wrapped target text images so the full semantic text is preserved without falling back to tiny point insertion.
- Redaction fill provenance must come from `outside_ring_median_pixel_cluster`: sample bbox inner grid points plus near/outer ring points, weight near-ring samples highest, inner samples second, and far outer-ring samples lowest, then use the selected cluster median as the fill color. This prevents glyph anti-aliasing from polluting flat backgrounds while still preserving table/header cell fills. `candidate_generation_evidence.json` must record selected cluster counts by source.
- Multiline or wide source redactions on colored backgrounds must be normalized by `background_covers` before target text insertion. `region_background_cover` covers semantic region source-union boxes; `residual_wide_line_background_cover` covers remaining wide colored line groups that do not belong to a reflow region. Large saturated/color covers must be drawn with `draw_mode=row_sampled_image_patch`, which samples the unmodified source page neighborhood and inserts an opaque background PNG patch; `solid_vector_fill` is reserved for neutral or small covers.
- Extractable foreground text over a non-plain image/photo background is still source text and must be translated. `generate_semantic_backfill.py` classifies the local image background from current-page image block overlap and pixel evidence, then records `image_overlay_background_decision` and uses text-only redaction/background protection instead of solid wipe blocks. Text baked into the image pixels is not extractable and remains part of the image unless an OCR workflow is explicitly authorized.
- `collect_visual_region_metrics.py` must expose `source_inner_background_rgb`, `output_inner_background_rgb`, `inner_background_delta`, `redaction_metrics`, `covered_by_background_cover`, `wide_line_patch_risk`, and `background_cover_metrics`, and route visible inner-background mismatches, uncovered wide colored redaction bands, or large saturated `solid_vector_fill` cover blocks to `background_residue_fill_resample`.
- Constrained text-image drawing must use the current region's redaction fill color as an opaque image background, not a transparent white image. `generate_semantic_backfill.py` records `image_background_color`; `collect_visual_region_metrics.py` validates it with `text_image_background_delta` so subtle per-word background patches on colored pages cannot pass unnoticed.
- `vertical_nav` slots should use `draw_mode=rotated_horizontal_text_image`; this means horizontal target-language text is rendered as one label and then rotated, which is different from stacked one-character vertical writing.
- For side navigation, `render_source_output_crop.py` may emit a back-rotated output crop. If the back-rotated crop is not readable horizontal target-language text, `sidebar_glyph_orientation` must fail.

## Tool Promotion Rule

A script can be promoted into this directory only if it is generic.

It must not depend on:

- a specific PDF filename;
- a specific page number;
- a specific known text string;
- hardcoded sample coordinates;
- exact sample colors;
- known document identity.

Sample-specific scripts, historical replay harnesses, and offline bilingual-reference tools belong outside this core directory, for example under `docs\offline_reference_evaluation` or a run-specific report directory.

## Required Tool Header

Every reusable script should start with a header describing:

```text
tool_name:
category:
input_contract:
output_contract:
failure_signals:
fallback:
anti_overfit_statement:
```
