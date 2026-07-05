# Tool Contracts

## Tool Categories

| Category | Tools | Purpose |
|---|---|---|
| shell | PowerShell | file discovery, command orchestration, environment variables |
| pdf_extract | PyMuPDF `get_text("dict")`, `get_text("text")` | text, bbox, font, page geometry |
| pdf_render | PyMuPDF `get_pixmap`, Poppler if available | source/output PNG evidence |
| pdf_modify | PyMuPDF redaction and insertion | remove source-language units and insert target-language text |
| translation_provider | model/API/human-reviewed translation adapter | produce semantic target-language translations with coverage metadata |
| visual_review | `view_image`, source-vs-output PNG comparison | human/model visual adjudication |
| edit | `apply_patch` | minimal repair edits to scripts/docs |
| validation | Python validators | process, product gate, semantic coverage, and anti-overfit validation |
| change_tracking | `collect_change_manifest.py` | before/after file hashes and changed-file delta for adaptive rounds |
| optional | OCR, ReportLab, Poppler | only when explicitly justified |

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

- exact page background sample;
- table cell background sample;
- explicit white only for white regions;
- transparent/no-fill only if the library supports it safely.

## Generator Mode Contract

| Generator | Allowed modes | Product-quality status |
|---|---|---|
| `generate_minimal_candidate.py` | process smoke only | never product-quality evidence |
| `generate_backfill_candidate.py` | `backfill_candidate_validation` | proves redaction/insertion mechanics only; fails semantic product quality |
| `validate_semantic_translations.py` | `product_quality` | validates complete real translation input before generation |
| `build_layout_policy.py` | `product_quality` and validation runs | derives a run-local layout policy from current extraction statistics; may be revised by D4 model judgement |
| `generate_semantic_backfill.py` | `product_quality` | consumes validated semantic translations and explicit layout policy, then performs redaction/backfill with region-level target-language reflow |

`product_quality` must not silently fall back to `generate_backfill_candidate.py`. If semantic translations are missing or fail validation, return `S_FAIL_CAPABILITY` before creating a product candidate.

`validate_semantic_translations.py` must reject both literal placeholders and metadata-style pseudo translations. A target string such as `This line reports...`, `This line describes...`, `本行说明...`, `本行列示...`, `当前页的财务报告、治理或业务信息`, or leaked instruction text such as `保留数值与标记...` is not a semantic translation and must block `S7_GenerateCandidate`.

## Semantic Backfill Layout Contract

`generate_semantic_backfill.py` must not insert target-language text one source line at a time for paragraph-like content. That behavior over-preserves source line breaks and produces short target-language lines.

Required behavior:

| Step | Contract |
|---|---|
| redaction | redact every extractable source text unit by its original bbox |
| layout policy | read `layout_policy.json`; do not hardcode role thresholds, font scales, shrink arrays, or fallback lengths in generator logic |
| grouping | derive block/region groups from current-run extraction metadata such as `unit_id`, bbox, font size, page geometry, and policy thresholds |
| source separators | do not merge translated units across visible source lines that are not translation units, such as years, numeric headings, bullets, or separator labels; the policy field is `source_separator_policy.split_on_untranslated_visible_line_gap=true` |
| reflow | insert target-language text once per paragraph/body/footnote/heading region when the policy marks the region kind as `region_reflow` |
| body flow | merge aligned wide body paragraph regions only when `layout_policy.flow_grouping.body` permits it and current-run x/width/y-gap statistics prove one continuous article column |
| body flow line joining | inside one `body_flow`, use the policy's `paragraph_gap_pt` to decide whether adjacent source regions are same-paragraph continuations or new paragraphs; do not always join with `\n\n` |
| table notes | classify wide `Note:` / `Notes:` blocks as `table_note`; do not merge them into `body_flow`; keep note/body font hierarchy close to the source |
| table cells | on dense table/chart pages, classify constrained labels and cells as `table_cell`; use explicit `table_cell_zh/table_cell_en` or compact variants from D2, and do not merge these cells into `body_flow` |
| dense page guard | when page extraction says `table_or_chart_dense` or `chart_or_dashboard`, `body_flow` is disabled unless D4 records a contrary page-local justification |
| rotated navigation | execute `layout_policy.draw_modes.vertical_nav=rotated_horizontal_text_image` for narrow side navigation; target text must be laid out horizontally first and rotated as one unit, not inserted as one-character vertical writing |
| preserve-line | preserve line-level insertion only for policy-defined compact labels, legends, vertical navigation, chart ticks, or single-line regions |
| compact labels | consume explicit `layout_variants` from translation input; never invent document-specific abbreviations inside the generator; never reintroduce source-language residue such as `n/m` to make text fit |
| evidence | report both `inserted_unit_count` and `inserted_region_count`; unit count proves coverage, region count proves reflow happened; every insertion must also report `source_block_ids` and `source_line_indexes` so validators can detect cross-separator reflow |

Required generation evidence:

```json
{
  "strategy": "redact_extractable_<source_language>_lines_and_insert_semantic_<target_language>_regions",
  "layout_policy_json": "docs/reports/.../layout_policy.json",
  "layout_policy_sha256": "...",
  "layout_policy_version": "...",
  "layout_policy_source": "...",
  "redacted_line_count": 209,
  "inserted_unit_count": 209,
  "inserted_region_count": 127,
  "fit_warning_count": 0,
  "insertions": [
    {
      "region_id": "region_p0_b2_018",
      "unit_ids": ["p0_b2_l3", "p0_b2_l4"],
      "source_block_ids": ["2"],
      "source_line_indexes": [3, 4]
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
It scans the reusable core using a run-local token list stored outside the core and fails if sample-specific tokens appear in `tools`, `contracts`, or `prompts`.

Required output:

```text
anti_overfit_scan.json
```

Acceptance rule:

```text
blocking_hit_count == 0
```
