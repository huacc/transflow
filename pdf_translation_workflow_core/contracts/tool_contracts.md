# Tool Contracts

## Tool Categories

| Category | Tools | Purpose |
|---|---|---|
| shell | PowerShell | file discovery, command orchestration, environment variables |
| pdf_extract | PyMuPDF `get_text("dict")`, `get_text("text")` | text, bbox, font, page geometry |
| pdf_render | PyMuPDF `get_pixmap`, Poppler if available | source/output PNG evidence |
| pdf_modify | PyMuPDF redaction and insertion | remove source English and insert Chinese |
| translation_provider | model/API/human-reviewed translation adapter | produce semantic Chinese translations with coverage metadata |
| visual_review | `view_image`, source-vs-output PNG comparison | human/model visual adjudication |
| edit | `apply_patch` | minimal repair edits to scripts/docs |
| validation | Python validators | process and product gate validation |
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
| `generate_semantic_backfill.py` | `product_quality` | consumes validated semantic translations and explicit layout policy, then performs redaction/backfill with region-level Chinese reflow |

`product_quality` must not silently fall back to `generate_backfill_candidate.py`. If semantic translations are missing or fail validation, return `S_FAIL_CAPABILITY` before creating a product candidate.

## Semantic Backfill Layout Contract

`generate_semantic_backfill.py` must not insert Chinese one source line at a time for paragraph-like content. That behavior over-preserves English line breaks and produces short Chinese lines.

Required behavior:

| Step | Contract |
|---|---|
| redaction | redact every extractable source text unit by its original bbox |
| layout policy | read `layout_policy.json`; do not hardcode role thresholds, font scales, shrink arrays, or fallback lengths in generator logic |
| grouping | derive block/region groups from current-run extraction metadata such as `unit_id`, bbox, font size, page geometry, and policy thresholds |
| reflow | insert Chinese once per paragraph/body/footnote/heading region when the policy marks the region kind as `region_reflow` |
| body flow | merge aligned wide body paragraph regions only when `layout_policy.flow_grouping.body` permits it and current-run x/width statistics prove one continuous article column |
| table notes | classify wide `Note:` / `Notes:` blocks as `table_note`; do not merge them into `body_flow`; keep note/body font hierarchy close to the source |
| rotated navigation | execute `layout_policy.draw_modes.vertical_nav=rotated_text` for narrow side navigation instead of inserting one Chinese character per source line |
| preserve-line | preserve line-level insertion only for policy-defined compact labels, legends, vertical navigation, chart ticks, or single-line regions |
| compact labels | consume explicit `layout_variants` from translation input; never invent document-specific abbreviations inside the generator; never reintroduce English residue such as `n/m` to make text fit |
| evidence | report both `inserted_unit_count` and `inserted_region_count`; unit count proves coverage, region count proves reflow happened |

Required generation evidence:

```json
{
  "layout_provider": "region_reflow_semantic_layout",
  "layout_policy_json": "docs/reports/.../layout_policy.json",
  "layout_policy_sha256": "...",
  "strategy": "redact_extractable_ascii_lines_and_insert_semantic_chinese_regions",
  "redacted_line_count": 209,
  "inserted_unit_count": 209,
  "inserted_region_count": 127,
  "fit_warning_count": 0
}
```

`inserted_region_count` may be lower than `inserted_unit_count`. That is expected when multiple English source lines are merged into one Chinese paragraph region. Validators must compare redaction coverage against `inserted_unit_count`, not against region count.

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

This file is not produced by `generate_semantic_backfill.py`. It must be produced by a recorded visual review step that names input render/crop artifacts and returns dimensions such as `line_fragmentation`, `paragraph_density`, `internal_paragraph_gap`, `end_blank_allowed`, `font_hierarchy_ratio`, `sidebar_orientation`, `footnote_readability`, and `visual_similarity`.

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

## Semantic Translation Tool Contract

Required input path:

```text
docs\input\semantic_translations\<regression_id>.translations.json
```

Required preflight:

```powershell
python pdf_translation_workflow_core\tools\validators\validate_semantic_translations.py `
  --source-extraction <source_extraction.json> `
  --translations docs\input\semantic_translations\<regression_id>.translations.json `
  --out <run_dir>\semantic_translation_validation.json
```

Only `translation_validation_verdict: PASS` may enter:

```powershell
python pdf_translation_workflow_core\tools\generators\generate_semantic_backfill.py `
  --input <source.pdf> `
  --source-extraction <source_extraction.json> `
  --semantic-translations docs\input\semantic_translations\<regression_id>.translations.json `
  --layout-policy <run_dir>\layout_policy.json `
  --output <run_dir>\outputs\candidate.pdf `
  --evidence <run_dir>\candidate_generation_evidence.json `
  --translations <run_dir>\translations.json `
  --layout-plan <run_dir>\layout_plan.json
```

## Tool Anti-Overfit Rule

Tools may read sample facts as input evidence, but tool behavior cannot branch on known filename, known page number, exact text, or fixed sample coordinates.
