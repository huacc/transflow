# Tool Contracts

## Tool Categories

| Category | Tools | Purpose |
|---|---|---|
| shell | PowerShell | file discovery, command orchestration, environment variables |
| pdf_extract | PyMuPDF `get_text("dict")`, `get_text("text")` | text, bbox, font, page geometry |
| pdf_render | PyMuPDF `get_pixmap`, Poppler if available | source/output PNG evidence |
| pdf_modify | PyMuPDF redaction and insertion | remove source-language units and insert target-language text |
| translation_provider | D2 batch prompt loop, model/API/human-reviewed translation adapter | produce semantic target-language translations with coverage metadata |
| visual_review | `view_image`, source-vs-output PNG comparison | human/model visual adjudication |
| edit | `apply_patch` | minimal repair edits to scripts/docs |
| validation | Python validators | process, product gate, semantic coverage, and anti-overfit validation |
| change_tracking | `collect_change_manifest.py` | before/after file hashes and changed-file delta for adaptive rounds |
| optional | OCR, ReportLab, Poppler | only when explicitly justified |

## Workspace Boundary Contract

Every run has one execution root. In an external validation package that root is the spike/round directory containing `pdf_translation_workflow_core`, `docs`, `run_request.json`, `state_trace.json`, `operation_log.jsonl`, and `decision_log.jsonl`. Runtime artifacts must not be read from or written to a parent directory unless the prompt explicitly defines an offline reference check that is outside the product run.

Before the executor writes any runtime artifact, it must resolve every planned artifact path against the current execution root and record a boundary check:

```powershell
python pdf_translation_workflow_core\tools\validators\validate_workspace_boundary.py `
  --workspace-root . `
  --path docs\reports `
  --path docs\output `
  --path docs\input\semantic_translations `
  --out docs\reports\workspace_boundary_preflight.json `
  --allow-missing
```

For a state-specific planned write set, pass the exact planned output paths:

```powershell
python pdf_translation_workflow_core\tools\validators\validate_workspace_boundary.py `
  --workspace-root . `
  --path docs\reports\<run_id>\translation_batches\<batch_id>.prompt_instance.json `
  --path docs\reports\<run_id>\translation_batches\<batch_id>.model_output.json `
  --path docs\reports\<run_id>\translation_batches\<batch_id>.decision_record.json `
  --path docs\reports\<run_id>\translation_batches\<batch_id>.validation.json `
  --out docs\reports\<run_id>\translation_batches\<batch_id>.workspace_boundary.json `
  --allow-missing
```

The output path passed to `--out` is itself resolved against the same root. If any planned input/output artifact resolves outside the execution root, the state must stop before the write and enter `S_FAIL_PROCESS_CONTRACT`.

`apply_patch` is for edits to reusable scripts, contracts, or prompts. It is not a runtime artifact writer. Runtime evidence such as `prompt_instance.json`, `model_output.json`, `decision_record.json`, `state_trace.json`, `operation_log.jsonl`, reports, previews, and candidate PDFs must be written by tools or shell/Python commands anchored to the execution root after a passing workspace-boundary check. If an executor uses `apply_patch` for a runtime artifact and the resolved target root cannot be proven, the run must fail process-contract validation.

Each `operation_log.jsonl` record that writes artifacts must include either `workspace_boundary_check_ref` or an inline `workspace_boundary_check` object with `workspace_boundary_verdict=PASS`.

## Mandatory Tool Record

Every tool invocation that affects the run must be represented in either state trace or operation log:

```json
{
  "operation_id": "OP012",
  "state": "S3_SourceExtract",
  "tool": "PyMuPDF.get_text(dict)",
  "input_artifacts": ["input.pdf"],
  "output_artifacts": ["tmp/pdfs/source_extraction.json"],
  "contract": "extract blocks/lines/spans with bbox/font/text; do not infer missing text",
  "status": "pass|fail|warn",
  "failure_signal": null,
  "fallback": null
}
```

## Required Tool Fallback Rules

| Failure | Fallback |
|---|---|
| Python cannot open Chinese path | pass path through environment variables or use `Path.cwd()` and relative paths |
| `pdfinfo` unavailable | use PyMuPDF for page count and geometry |
| Poppler unavailable | use PyMuPDF rendering |
| ReportLab unavailable | use PyMuPDF redaction/backfill or mark tooling failure if fresh PDF generation is required |
| Chinese font unavailable | probe fallback CJK fonts; if none, `S_FAIL_TOOLING` |
| OCR unavailable | do not pretend image text is extracted; record `visual_only_out_of_scope` or `ocr_required_but_unavailable` |

## Current Environment Probe

Probe artifact:

```text
docs\reports\pdf_translation_workflow_core\manual_aia_quality_eval\tool_probe.json
```

Observed on 2026-07-05:

| Capability | Status | Consequence |
|---|---|---|
| `fitz` / PyMuPDF | available | primary extract, render, redact/backfill route |
| `pypdf` | available | optional page copy/merge route |
| `pdfplumber` | available | optional table/text cross-check route |
| `PIL` | available | optional PNG/image inspection route |
| Microsoft YaHei / SimHei fonts | available | Chinese insertion can use local CJK fonts |
| Poppler CLI `pdfinfo` / `pdftoppm` | unavailable | use PyMuPDF rendering/page metadata unless installed later |
| `reportlab` | unavailable | do not rely on ReportLab for fresh PDF generation in this environment |

This probe is environment evidence, not a universal assumption. A new run must refresh it instead of copying this table as truth.

## Redaction Contract

For source-PDF backfill, redaction must preserve non-text graphics unless a repair atom explicitly states otherwise:

```python
page.apply_redactions(
    images=fitz.PDF_REDACT_IMAGE_NONE,
    graphics=fitz.PDF_REDACT_LINE_ART_NONE,
    text=fitz.PDF_REDACT_TEXT_REMOVE,
)
```

Fill color must have provenance:

- exact local page background sample;
- table cell background sample;
- multi-point surrounding-pixel cluster sample for normal text bboxes, so glyph color is not mistaken for the background;
- explicit white only for white regions after sampled evidence agrees;
- transparent/no-fill only if the library supports it safely.

The fill sampler is an open function over the current bbox and page pixels. It must not branch on filename, page number, exact text, known brand colors, hard-coded coordinates, or remembered samples. If a source glyph is red, blue, gray, or white, the sampler must still choose the surrounding region background unless the surrounding region itself has that color.

## Generator Mode Contract

| Generator | Allowed modes | Product-quality status |
|---|---|---|
| `generate_minimal_candidate.py` | process smoke only | never product-quality evidence |
| `generate_backfill_candidate.py` | `backfill_candidate_validation` | proves redaction/insertion mechanics only; fails semantic product quality |
| `validate_semantic_translations.py` | `product_quality` | validates complete real translation input before generation |
| `build_translation_batch_manifest.py` | `product_quality` | creates bounded D2 batch slot files from current source extraction |
| `validate_translation_batch.py` | `product_quality` | validates one D2 batch before assembly |
| `assemble_semantic_translations.py` | `product_quality` | assembles validated batch outputs into semantic translation JSON |
| `build_layout_policy.py` | `product_quality` and validation runs | derives a run-local layout policy from current extraction statistics plus the matching generic language layout profile; may be revised by D4 model judgement |
| `generate_semantic_backfill.py` | `product_quality` | consumes validated semantic translations and explicit layout policy, then performs redaction/backfill with region-level target-language reflow |

`product_quality` must not silently fall back to `generate_backfill_candidate.py`. If semantic translations are missing or fail validation, return `S_FAIL_CAPABILITY` before creating a product candidate.

`product_quality` must not treat D2 as an unbounded one-shot request. The executable S5 sequence is:

```text
build_translation_batch_manifest.py
for each batch:
  D2_translation.prompt.json
  validate_translation_batch.py
assemble_semantic_translations.py
validate_semantic_translations.py
```

The PDF generator can consume only the assembled file that passes `validate_semantic_translations.py`.

`validate_semantic_translations.py` must reject both literal placeholders and metadata-style pseudo translations. A target string such as `This line reports...`, `This line describes...`, `本行说明...`, `本行列示...`, `当前页的财务报告、治理或业务信息`, or leaked instruction text such as `保留数值与标记...` is not a semantic translation and must block `S7_GenerateCandidate`.

## Semantic Backfill Layout Contract

`generate_semantic_backfill.py` must not insert target-language text one source line at a time for paragraph-like content. That behavior over-preserves source line breaks and produces short target-language lines.

Required behavior:

| Step | Contract |
|---|---|
| redaction | redact every extractable source text unit by its original bbox |
| layout policy | read `layout_policy.json`; do not hardcode role thresholds, font scales, shrink arrays, or fallback lengths in generator logic |
| language profile | `layout_policy.json` must record `language_pair_profile`, `language_profile_json`, `language_profile_sha256`, and `layout_strategy`; language-direction behavior must come from this explicit profile, not sample branches |
| already-target spans | if a source-language PDF contains visible text that is already in the target language and that text may be covered by a recomposed target frame, the generator may mark it `preserve_already_target_language_span`, redact it, and redraw the same text; this is preservation evidence, not semantic translation evidence |
| grouping | derive block/region groups from current-run extraction metadata such as `unit_id`, bbox, font size, page geometry, and policy thresholds |
| source separators | do not merge translated units across visible source lines that are not translation units, such as years, numeric headings, bullets, or separator labels; the policy field is `source_separator_policy.split_on_untranslated_visible_line_gap=true` |
| reflow | insert target-language text once per paragraph/body/footnote/heading region when the policy marks the region kind as `region_reflow` |
| body flow | merge aligned wide body paragraph regions only when `layout_policy.flow_grouping.body` permits it and current-run x/width/y-gap statistics prove one continuous article column |
| body flow line joining | inside one `body_flow`, use the policy's `paragraph_gap_pt` to decide whether adjacent source regions are same-paragraph continuations or new paragraphs; do not always join with `\n\n` |
| target composition | for fluid `body_flow` regions, source bboxes are redaction/order/anchor evidence, not hard target containers; when `layout_policy.target_composition.enabled=true`, recompute the target frame from current-page margins, body band, source-body height expansion, bottom limit, region role, and avoid/overlap guard before font shrink; the frame must not automatically consume the whole remaining page |
| short continuation lines | a short same-column continuation may join only an already-active body flow, only when the policy enables `allow_short_continuation_lines`, and only when x/y geometry plus `min_continuation_width_page_ratio` pass |
| table notes | classify wide `Note:` / `Notes:` blocks as `table_note`; do not merge them into `body_flow`; keep note/body font hierarchy close to the source |
| table cells | on dense table/chart pages, classify constrained labels and cells as `table_cell`; use explicit `table_cell_zh/table_cell_en` or compact variants from D2, and do not merge these cells into `body_flow` |
| event cards | on mixed image/text timeline or milestone pages, classify narrow multi-line event descriptions as `event_card`; keep each event local, do not merge events into `body_flow`, and use event-card font/variant rules inside the source card slot |
| dense page guard | when page extraction says `table_or_chart_dense` or `chart_or_dashboard`, table/chart labels stay out of `body_flow`; a lower-page body copy band may re-enter `body_flow` only when policy `allow_dense_page_body_below_y_ratio` and current-run geometry prove same-column article text |
| target-language reflow | expand only declared `target_language_reflow.region_kinds`; obey `overlap_guard` so expanded frames do not invade the next same-column region |
| textbox fit probe | test each candidate font size on a temporary page first; failed attempts must not draw on the real candidate page |
| rotated navigation | execute `layout_policy.draw_modes.vertical_nav=rotated_horizontal_text_image` for narrow side navigation; target text must be laid out horizontally first and rotated as one unit, not inserted as one-character vertical writing |
| preserve-line | preserve line-level insertion only for policy-defined compact labels, legends, vertical navigation, chart ticks, or single-line regions |
| compact labels | consume explicit `layout_variants` from translation input; never invent document-specific abbreviations inside the generator; never reintroduce source-language residue such as `n/m` to make text fit |
| constrained text image fit | after normal textbox probing fails, table cells, compact labels, short labels, legends, and dense-page single-line labels may be inserted as transparent text images with full target text preserved; evidence must record `status=constrained_text_image_fit`, `font_size`, target box, and `horizontal_compression_ratio` |
| evidence | report both `inserted_unit_count` and `inserted_region_count`; unit count proves coverage, region count proves reflow happened; every insertion must also report `source_block_ids` and `source_line_indexes` so validators can detect cross-separator reflow |

Required generation evidence:

```json
{
  "strategy": "redact_extractable_<source_language>_lines_and_insert_semantic_<target_language>_regions",
  "language_pair_profile": "en_to_zh|zh_to_en|...",
  "layout_strategy": "source_anchor_preserving_region_reflow",
  "layout_policy_json": "docs/reports/.../layout_policy.json",
  "layout_policy_sha256": "...",
  "layout_policy_version": "...",
  "layout_policy_source": "...",
  "redacted_line_count": 209,
  "inserted_unit_count": 209,
  "inserted_region_count": 127,
  "semantic_translated_unit_count": 209,
  "preserved_target_language_unit_count": 0,
  "fit_warning_count": 0,
  "allowed_non_warning_insert_statuses": ["fit", "point_fit", "rotated_fit", "rotated_horizontal_image_fit", "constrained_text_image_fit"],
  "insertions": [
    {
      "region_id": "region_p0_b2_018",
      "unit_ids": ["p0_b2_l3", "p0_b2_l4"],
      "source_block_ids": ["2"],
      "source_line_indexes": [3, 4],
      "target_composition_applied": false,
      "target_composition_profile": null,
      "source_anchor_bbox": null
    }
  ]
}
```

`inserted_region_count` may be lower than `inserted_unit_count`. That is expected when multiple source lines are merged into one target-language paragraph region. Validators must compare redaction coverage against `inserted_unit_count`, not against region count.

`source_line_indexes` must be contiguous inside one source block unless the skipped line has been explicitly classified as ignorable. If one insertion jumps from line index `1` to `3` in the same block, the generator has crossed a visible source separator and product quality must fail through `source_anchor_order`.

## Visual Adjudication Tool Contract

`evaluate_pdf_quality.py` may consume a separate D7 visual adjudication artifact:

```powershell
python pdf_translation_workflow_core\tools\validators\evaluate_pdf_quality.py `
  --source <source.pdf> `
  --output <candidate.pdf> `
  --generation-evidence <candidate_generation_evidence.json> `
  --visual-adjudication <visual_adjudication.json> `
  --out <product_quality_gates.json>
```

This file is not produced by `generate_semantic_backfill.py`. It must be produced by a recorded visual review step that names input render/crop artifacts and returns dimensions such as `line_fragmentation`, `paragraph_density`, `internal_paragraph_gap`, `end_blank_allowed`, `source_anchor_order`, `region_crosses_untranslated_separator`, `font_hierarchy_ratio`, `sidebar_orientation`, `sidebar_orientation_group_consistency`, `sidebar_glyph_orientation`, `footnote_readability`, and `visual_similarity`.

If the visual adjudication verdict is missing or not `PASS`, `visual_similarity` remains a blocking failure in product-quality mode.

## Region-Level Visual Metrics Contract

Before final `evaluate_pdf_quality.py` in product-quality mode, S8 must run:

```powershell
python pdf_translation_workflow_core\tools\validators\collect_visual_region_metrics.py `
  --source <source.pdf> `
  --output <candidate.pdf> `
  --generation-evidence docs\reports\<run_id>\candidate_generation_evidence.json `
  --source-extraction docs\reports\<run_id>\source_extraction.json `
  --out docs\reports\<run_id>\visual_region_metrics.json `
  --crop-dir docs\reports\<run_id>\visual_region_crops
```

The output must contain:

- `page_metrics`: image count and page-level color/dominant-color deltas;
- `region_metrics`: one record per generated insertion with `quality_role`, `gate_id`, `font_size`, `source_median_font_size`, `output_to_source_font_ratio`, `generation_status`, `background_delta`, `crop_evidence`, reasons, and repair atoms;
- `role_gates`: aggregated gates such as `source_relative_visual_baseline`, `hero_banner_text_readability`, `title_readability`, `body_paragraph_readability`, `table_text_legibility`, `footnote_readability`, `legend_label_alignment`, `sidebar_navigation_legibility`, `event_card_readability`, and `image_color_integrity`.

`source_relative_visual_baseline` is mandatory in product-quality mode. It fails when `source_extraction.json` is missing or when too few generated regions can be tied back to source extraction font/line evidence. `evaluate_pdf_quality.py` must consume this file through `--visual-region-metrics`. A critical role gate with status `fail` is blocking even if `visual_similarity` is `PASS_WITH_WARN` or a full-page thumbnail looks acceptable.

Repair routing should be generated by:

```powershell
python pdf_translation_workflow_core\tools\repairs\plan_visual_region_repairs.py `
  --visual-region-metrics docs\reports\<run_id>\visual_region_metrics.json `
  --out docs\reports\<run_id>\visual_repair_plan.json
```

This repair planner may select repair atoms such as `heading_frame_fit_or_short_title_variant`, `D2_constrained_slot_layout_variants`, `target_composition_body_reflow_repair`, `background_fill_resample`, or `image_redaction_exclusion_repair`. It must not rewrite candidate PDFs by itself.

`evaluate_pdf_quality.py` also records per-page `source_font_hierarchy`, `output_font_hierarchy`, and `small_to_body_ratio_delta` metrics. These metrics do not replace visual judgement, but D7 must use them when deciding whether table notes, body copy, and headings still have source-like relative size.

`render_source_output_crop.py` is the required generic crop comparison helper when D7 needs source-vs-output visual evidence:

```powershell
python pdf_translation_workflow_core\tools\renderers\render_source_output_crop.py `
  --source <source.pdf> `
  --output <candidate.pdf> `
  --page-index <zero_based_page_index> `
  --crop "x0,y0,x1,y1" `
  --out <run_dir>\compare\<scope>.png `
  --manifest <run_dir>\compare\<scope>.json
```

The crop rectangle is run evidence supplied by the executor or D7 decision; the renderer itself must not contain sample-specific coordinates.

For rotated side navigation, D7 should also request `--backrotate-output-degrees` and `--backrotate-output-out`. A valid rotated horizontal target-language label must become readable horizontal target-language text in the back-rotated output crop.

## Semantic Translation Tool Contract

Required input path:

```text
docs\input\semantic_translations\<case_id>.translations.json
```

Required preflight:

```powershell
python pdf_translation_workflow_core\tools\validators\validate_semantic_translations.py `
  --source-extraction <source_extraction.json> `
  --translations docs\input\semantic_translations\<case_id>.translations.json `
  --out <run_dir>\semantic_translation_validation.json
```

Only `translation_validation_verdict: PASS` may enter:

```powershell
python pdf_translation_workflow_core\tools\generators\generate_semantic_backfill.py `
  --input <source.pdf> `
  --source-extraction <source_extraction.json> `
  --semantic-translations docs\input\semantic_translations\<case_id>.translations.json `
  --layout-policy <run_dir>\layout_policy.json `
  --output <run_dir>\outputs\candidate.pdf `
  --evidence <run_dir>\candidate_generation_evidence.json `
  --translations <run_dir>\translations.json `
  --layout-plan <run_dir>\layout_plan.json
```

## Tool Anti-Overfit Rule

Tools may read sample facts as input evidence, but tool behavior cannot branch on known filename, known page number, exact text, or fixed sample coordinates.

Official bilingual reference PDFs are not runtime evidence for translation, layout, or quality decisions. They may be used only after a run as offline evaluation data to identify generic process gaps. Reusable tools must not consume the reference pair to derive hardcoded translations, coordinates, page identities, or terminology exceptions.

`tools\validators\scan_core_overfit.py` must be run before final acceptance in `S9_VerifyProcessContract`.
It scans the reusable core using a run-local token list stored outside the core and fails if sample-specific tokens appear in `tools`, `contracts`, `prompts`, or `profiles`.

Required output:

```text
anti_overfit_scan.json
```

Acceptance rule:

```text
blocking_hit_count == 0
```
