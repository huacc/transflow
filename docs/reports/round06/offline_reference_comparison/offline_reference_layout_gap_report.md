# Round06 Offline Official-English Layout Comparison

Boundary: this is an offline result comparison only. The official English PDF was not used as runtime translation input, layout input, visual adjudication input, prompt slot data, coordinate source, or terminology source.

Candidate PDF: `docs/output/round06/R06_R11_AIA_zh_pages_03_08_09_24_25_zh_to_en_candidate.pdf`

Official English PDF: `D:/项目/开源项目/MerqFin/spikes/独立测试/样本/英文/AIA_2020_Annual_Report_en.pdf`

Page mapping is fixed by filename digits:

- candidate p1 -> official English PDF page 3
- candidate p2 -> official English PDF page 8
- candidate p3 -> official English PDF page 9
- candidate p4 -> official English PDF page 24
- candidate p5 -> official English PDF page 25

## Metric Snapshot

| Candidate | Official EN | Median font cand/ref | Text area cand/ref | Evidence |
|---|---:|---:|---:|---|
| p1 | 3 | 6.19 / 8.0 | 0.1308 / 0.1912 | `docs/reports/round06/offline_reference_comparison/candidate_p01_vs_official_en_p003.png` |
| p2 | 8 | 5.0 / 6.5 | 0.0656 / 0.0672 | `docs/reports/round06/offline_reference_comparison/candidate_p02_vs_official_en_p008.png` |
| p3 | 9 | 5.8 / 7.0 | 0.118 / 0.1566 | `docs/reports/round06/offline_reference_comparison/candidate_p03_vs_official_en_p009.png` |
| p4 | 24 | 6.8 / 8.5 | 0.1455 / 0.2263 | `docs/reports/round06/offline_reference_comparison/candidate_p04_vs_official_en_p024.png` |
| p5 | 25 | 5.99 / 9.5 | 0.1743 / 0.2688 | `docs/reports/round06/offline_reference_comparison/candidate_p05_vs_official_en_p025.png` |

## Visual Findings

### Candidate p1 vs official page 3

- Candidate title copy is much shorter and lower hierarchy than the official hero title. Official uses a large three-line hero statement; candidate uses a smaller phrase placed in the same red panel.
- The timeline area is generally recognizable, but candidate text is more fragmented and in several cells smaller than the official English layout.
- This shows that title/hero regions need a target-language headline hierarchy policy, not just block-level semantic replacement.

### Candidate p2 vs official page 8

- Chart positions and numeric graphics are close, because this page has mostly graphic content.
- Chart headings in the candidate are too small and too sentence-like; official headings are uppercase, compact, and aligned to chart widths.
- Footer and top section hierarchy are weaker in the candidate.

### Candidate p3 vs official page 9

- Pie chart layout is broadly close, but chart headings are truncated/small in candidate.
- Notes are denser and smaller in candidate, especially footnotes. Official notes have clearer two-column rhythm and stronger readability.
- Side navigation is closer than earlier broken vertical-glyph output, but candidate labels still look smaller and less balanced than official.

### Candidate p4 vs official page 24

- This is a major failure. Candidate body text is much smaller than official body text: median font 6.8 vs 8.5.
- Candidate text area ratio is 0.1455 vs official 0.2263, meaning it occupies less visual weight despite containing more characters.
- Body paragraphs are over-compressed because generated English is fitted into source Chinese paragraph boxes and fallback shrink rules.
- Table is visually recognizable but smaller and top-left shifted; official table typography is stronger and section title hierarchy is much clearer.

### Candidate p5 vs official page 25

- This is also a major failure. Candidate body median font is 5.99 vs official 9.5.
- Candidate side navigation text is split into tiny fragments and lacks official vertical rotated-label balance.
- Candidate leaves excessive empty visual weight after compressed text, while official uses larger body type and consistent paragraph leading.
- The failure is layout policy, not only translation quality.

## Engineering Implication

The current zh-to-en backfill cannot rely only on source-language bboxes. For target languages whose text expands or whose official design uses different typographic rhythm, D4 must choose a target-language layout mode:

```text
same-layout backfill: preserve source bboxes when target text fits and official-like rhythm remains plausible
language-reflow backfill: preserve page bands, graphics, tables, side navigation, and page anchors, but recompute paragraph text frames and font hierarchy from current-page available space
```

The repair loop should therefore add a generic D4/D7 rule:

- If `target_language=en` and candidate body median font falls below source/reference-like body band, or text area ratio is much lower than expected while bottom whitespace remains available, do not shrink further.
- Expand body text frames within the same page band and reflow paragraphs with larger body font and source-like leading.
- For dense chart/table pages, keep graphics fixed but apply title/label style normalization: uppercase compact headings, width-aligned headings, and table-cell font hierarchy.
- For side navigation, render each nav label as a rotated horizontal label with group-level alignment, not as independently tiny text fragments.

These are generic layout rules derived from observed source-vs-output and offline result comparison. They must not encode page numbers, fixed coordinates, official phrases, or document-specific terms into `pdf_translation_workflow_core`.
