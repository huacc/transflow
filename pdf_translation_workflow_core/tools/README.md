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

## Executable Tools

| Tool | Category | Input contract | Output contract | Failure signal | Current role |
|---|---|---|---|---|---|
| `probes\tool_probe.py` | probe | output JSON path | Python/package/font/executable capability JSON | required package/font unavailable | records environment facts before a run |
| `probes\extract_pdf_structure.py` | probe | input PDF path, output JSON path | pages, line bboxes, fonts, drawing counts, image counts, page-type guess | unreadable PDF or empty extraction | provides source/output structural evidence |
| `planners\build_layout_policy.py` | planner | source extraction JSON, optional semantic translations JSON, output policy path | run-local layout policy JSON with statistics, classification rules, font profiles, reflow and fallback policy | missing extraction, empty translatable units, invalid JSON | creates explicit D4 policy input so generators do not hide hardcoded visual constants |
| `renderers\render_pdf.py` | renderer | input PDF, output directory, prefix, zoom, manifest path | per-page PNG files and render manifest | missing images or render exception | provides source/output visual evidence |
| `renderers\render_source_output_crop.py` | renderer | source PDF, output PDF, page index, crop rectangle, output PNG, manifest path | source-vs-output crop contact sheet plus manifest | invalid page/crop or render exception | provides focused visual evidence for D7 dimensions such as font hierarchy, paragraph gaps, and sidebar orientation |
| `generators\generate_backfill_candidate.py` | generator | input PDF, source extraction JSON, output PDF, translations/layout/evidence JSON paths | low-fidelity Chinese backfill PDF plus translations/layout/evidence JSON | missing input/font/output failure | backfill-candidate generator; proves real backfill mechanics but not semantic quality |
| `generators\generate_semantic_backfill.py` | generator | input PDF, source extraction JSON, semantic translations JSON, layout policy JSON, output PDF, translations/layout/evidence JSON paths | semantic target-language backfill PDF plus translations/layout/evidence JSON | missing/invalid semantic translations, missing policy, font/output failure | product-quality candidate generator; redacts source-language lines and executes explicit region-reflow policy; never falls back to placeholder text |
| `generators\generate_minimal_candidate.py` | generator | input PDF, output PDF, evidence JSON path | candidate PDF plus evidence JSON | missing/unreadable input | debug-only smoke stub; copies source to prove quality gates can fail |
| `validators\evaluate_pdf_quality.py` | validator | source PDF, candidate PDF, output JSON, optional generation evidence, optional visual adjudication JSON | blocking gate verdict plus structural metrics | PDF open or metric exception | automated partial product gate; records visual gate result when adjudication artifact is supplied |
| `validators\validate_semantic_translations.py` | validator | source extraction JSON, semantic translations JSON, output JSON | translation coverage/authenticity verdict | missing units, placeholder text, pseudo-translation text, token preservation failure | blocks product-quality generation before candidate PDF creation |
| `validators\validate_process_artifacts.py` | validator | run directory, output JSON | state/operation/decision/evidence contract verdict | missing required trace artifacts | process-contract gate |
| `validators\scan_core_overfit.py` | validator | core root directory, run-local token file outside core, output JSON | anti-overfit scan verdict with blocking/warning hits | sample-specific token found in tools/contracts/prompts | proves sample facts did not enter reusable core logic |

## Known Automation Boundary

`evaluate_pdf_quality.py` is not a complete visual judge. It currently blocks on page count, page geometry, source-language text residue based on target language, generation authenticity when evidence is supplied, source anchor order, semantic translation preflight, text-fit warnings, placeholder semantic coverage, and visual adjudication failures when supplied. It records text-density and font-hierarchy metrics for review. Line fragmentation, paragraph density, internal paragraph gaps, sidebar orientation, table-cell damage, chart-label readability, redaction patch visibility, and perceived typographic rhythm still require PNG review plus a recorded model/human adjudication until those checks are promoted into deterministic validators.

`generate_semantic_backfill.py` evidence must be read carefully:

- `inserted_unit_count` proves all source text units were covered.
- `inserted_region_count` is allowed to be lower than `inserted_unit_count` because multi-line source-language blocks should be reflowed into fewer target-language regions.
- `fit_warning_count` must be `0` for product-quality acceptance.
- `source_block_ids` and `source_line_indexes` must prove that a reflow region did not cross visible untranslated anchors inside one source block.
- `strategy` should be `redact_extractable_<source_language>_lines_and_insert_semantic_<target_language>_regions`.
- `layout_policy_json`, `layout_policy_sha256`, `layout_policy_version`, and `layout_policy_source` prove layout parameters came from an explicit run-local policy instead of hidden constants in the generator.
- `layout_plan.json` region kinds must distinguish `body`, `body_flow`, `table_cell`, `table_note`, `legend`, `footnote`, `vertical_nav`, `compact_label`, and `heading` when those roles exist.
- `body_flow` text must use policy-controlled y-gap joining. Same-paragraph source-wrapped lines should use the language-specific line joiner, while only larger paragraph gaps use `paragraph_separator`.
- Dense table/chart pages should keep compact labels as `table_cell`/`legend` preserve-line regions instead of merging them into `body_flow`.
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
