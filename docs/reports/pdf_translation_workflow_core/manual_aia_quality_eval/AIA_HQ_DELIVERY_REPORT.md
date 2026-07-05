# AIA Four-Page Chinese Backfill Delivery Report

## Scope

Input:

```text
测试数据\AIA_2020_Annual_Report_en_pages_08_09_24_25.pdf
```

Output:

```text
docs\output\AIA_2020_Annual_Report_zh_pages_08_09_24_25.hq.pdf
```

This report records the delivery candidate and evidence for the four extracted AIA pages. It does not claim that the generic PDF backfill engine is complete.

## Evidence Artifacts

| Artifact | Purpose |
|---|---|
| `tool_probe.json` | Runtime tool/font capability probe |
| `aia_source_structure.json` | Source PDF structural extraction |
| `aia_hq_structure.json` | Output PDF structural extraction |
| `aia_source_render_manifest.json` | Source preview manifest |
| `aia_output_render_manifest.json` | Output preview manifest |
| `aia_hq_quality_gates.json` | Automated gate result |
| `source_previews\*.png` | Source page renderings |
| `output_previews\*.png` | Output page renderings |

## Tool Sequence

| Step | Tool | Output |
|---|---|---|
| 1 | `tool_probe.py` | environment capability JSON |
| 2 | `extract_pdf_structure.py` on source | source structure JSON |
| 3 | `extract_pdf_structure.py` on output | output structure JSON |
| 4 | `render_pdf.py` on source | source PNGs and manifest |
| 5 | `render_pdf.py` on output | output PNGs and manifest |
| 6 | `evaluate_pdf_quality.py` | automated gate JSON |
| 7 | Codex visual review via rendered PNGs | page-level visual verdict below |

## Automated Gate Result

From `aia_hq_quality_gates.json`:

```json
{
  "product_quality_verdict": "PASS",
  "blocking_failure_count": 0,
  "blocking_gates": ["page_count", "page_geometry", "text_residue"]
}
```

Recorded structural ratios:

| Page index | text_area_ratio | line_count_ratio | y_span_ratio | font_size_ratio |
|---:|---:|---:|---:|---:|
| 0 | 0.820 | 0.994 | 1.001 | 1.000 |
| 1 | 0.391 | 0.906 | 1.002 | 0.914 |
| 2 | 0.604 | 0.942 | 1.001 | 1.000 |
| 3 | 0.578 | 1.189 | 1.001 | 0.789 |

These ratios are evidence, not full visual acceptance. Chinese text can occupy less horizontal area than English, so raw text area cannot be the only blocking rule.

## Model Adjudication Record

No separate OpenAI API call was made. The model adjudication was performed inside the Codex session using rendered source/output PNGs plus structure JSON.

Adjudication prompt exposed in this run:

```text
Compare the source PDF rendering and Chinese output rendering. Ignore language-specific glyph width differences by themselves. Judge:
1. page geometry and major region preservation;
2. chart, table, side navigation, and footer integrity;
3. readability of Chinese headings, body text, and footnotes;
4. blocking overlap, clipping, overflow, or English residue;
5. visible redaction patches, density collapse, or rhythm mismatch;
6. emit a page-level verdict: PASS, PASS_WITH_WARN, or FAIL.
```

Page-level verdicts:

| Source page | Page type | Verdict | Basis | Residual warning |
|---|---|---|---|---|
| 08 | `bar_chart_dashboard` | PASS_WITH_WARN | chart geometry, numbers, red growth blocks, footer preserved | Chinese chart titles are tighter than English |
| 09 | `pie_chart_legend_footnote` | PASS_WITH_WARN | pie charts, legend, side navigation, notes preserved | Chinese notes occupy less area, lower page looks more open |
| 24 | `table_body` | PASS_WITH_WARN | table grid, numeric columns, body paragraphs remain readable | local light patches inside table cells |
| 25 | `body_nav` | PASS_WITH_WARN | paragraphs, side navigation, and footer preserved | Chinese body copy is shorter, bottom whitespace increases |

Overall:

```json
{
  "delivery_candidate": "ACCEPTED_WITH_WARNINGS",
  "automated_blocking_gates": "PASS",
  "manual_visual_review": "PASS_WITH_WARN",
  "methodology_proof": "PARTIAL",
  "generic_engine_status": "NOT_COMPLETE"
}
```

## Honest Boundary

- The output PDF is a high-quality delivery candidate for the requested AIA four pages.
- The reusable workflow now has runnable tools, contracts, regression anchors, and selftest evidence.
- The generic generation engine is not complete: the current reusable generator is a minimal stub used to prove gate behavior.
- Automated visual quality is partial: table-cell damage, patch visibility, rhythm, and semantic coverage still need deterministic validators or recorded visual adjudication.

