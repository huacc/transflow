# Round05 Sidebar Glyph Orientation Validation Report

## Scope

This run validates the user's correction that the side-navigation issue is about font/glyph direction, not merely whether a sidebar label is placed vertically.

Inputs:

```text
01_source.pdf
测试数据\AIA_2020_Annual_Report_en_pages_08_09_24_25.pdf
spikes\round09\docs\input\semantic_translations\R1_01_source_single_timeline.translations.json
spikes\round09\docs\input\semantic_translations\R2_AIA_pages_08_09_24_25.translations.json
```

Outputs:

```text
docs\output\round05\R1_01_source_single_timeline_round05_sidebar_glyph_orientation_candidate.pdf
docs\output\round05\R2_AIA_pages_08_09_24_25_round05_sidebar_glyph_orientation_candidate.pdf
```

## Mechanism

The side-navigation policy now uses:

```json
{
  "draw_modes": {
    "vertical_nav": {
      "mode": "rotated_horizontal_text_image",
      "writing_mode": "horizontal_line_rotated_as_unit",
      "glyph_orientation": "rotate_glyphs_with_line"
    }
  }
}
```

This means Chinese text is laid out as one horizontal label, then rotated as a single unit into the source slot. It is not one-character vertical writing.

## Evidence

Key artifacts:

```text
docs\reports\round05_sidebar_glyph_orientation_validation\compare\R2_page04_sidebar_source_vs_round05.png
docs\reports\round05_sidebar_glyph_orientation_validation\compare\R2_page04_sidebar_round05_backrotated.png
docs\reports\round05_sidebar_glyph_orientation_validation\R2\visual_adjudication.json
docs\reports\round05_sidebar_glyph_orientation_validation\R2\product_quality_gates.json
```

The back-rotated crop proves the red active sidebar label becomes readable horizontal Chinese: `财务及营运回顾`.

## Result

| Regression | Fit warnings | Product verdict | Notes |
|---|---:|---|---|
| R1 | 0 | FAIL | still blocked by broader visual similarity |
| R2 | 0 | FAIL | `sidebar_orientation_group_consistency=pass`, `sidebar_glyph_orientation=pass`, still blocked by broader visual similarity |

This round fixes the specific side-navigation glyph-direction mechanism. It does not claim final product-quality acceptance.
