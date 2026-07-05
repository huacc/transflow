# Page Type Repair Matrix

## Purpose

This matrix prevents the workflow from treating all PDF pages as timeline pages.

Page type and region type must drive quality gates and repair atoms.

## Page Types

| Page type | Typical regions | Required special gates |
|---|---|---|
| `timeline` | year labels, body text, images, badges | column rhythm, image avoidance, year hierarchy |
| `bar_chart_dashboard` | chart title, axis labels, bars, value labels, growth tiles | chart geometry preservation, label readability |
| `pie_chart_legend_footnote` | pie labels, legend, notes, side navigation | legend mapping, footnote density, side nav |
| `table_body` | table header, rows, footnotes, body paragraphs | grid preservation, cell fit, numeric alignment |
| `body_nav` | paragraphs, side navigation, footer | paragraph rhythm, nav preservation, body text area |

## Repair Atoms

| Failure class | Applicable page/region | Repair atom | Tools | Verification |
|---|---|---|---|---|
| `ascii_residue` | all | retranslate_or_cover_residue | PyMuPDF text extraction, regex, render | no ASCII tokens unless allowed |
| `text_fit_overflow` | all text slots | reduce_font_or_reflow | PyMuPDF insertion, font metrics | fit result non-negative; no clipping |
| `visual_density_low` | body/notes/footnotes | expand_translation_or_adjust_line_height | translation plan, layout plan | text_area_ratio/y_span_ratio recover |
| `paragraph_density_mismatch` | body paragraphs | body_flow_grouping | layout policy, source x/width statistics, source-vs-output crop | active paragraph gaps shrink without creating overlap; end blank may remain |
| `internal_paragraph_gap` | body paragraphs | body_flow_grouping_or_line_height_adjust | layout policy, render crop | gaps between active paragraphs approach source rhythm |
| `font_hierarchy_ratio_mismatch` | notes/body/headings/table labels | role_font_profile_or_region_classification | layout policy, font hierarchy metrics, source-vs-output crop | note/body/title ratios remain close to source |
| `sidebar_orientation_fail` | annual report side navigation | rotated_horizontal_text_image_draw_mode | layout policy draw_modes, rotated horizontal label image insertion, source-vs-output crop | rotated source labels remain rotated, not stacked character-by-character |
| `sidebar_glyph_orientation_fail` | annual report side navigation | backrotated_crop_glyph_check | source-vs-output crop, back-rotated output crop | back-rotated output crop is readable horizontal Chinese |
| `side_nav_group_consistency_fail` | annual report side navigation | side_nav_group_writing_mode_policy | source region grouping, layout policy draw modes | all labels in one side-nav group share the same writing mode |
| `over_narrow_lines` | body/notes | increase_wrap_width_or_reduce_target_lines | layout plan | no near-vertical short lines |
| `background_patch_visible` | colored/table/chart backgrounds | resample_fill_color_or_split_redaction | image sampling, PyMuPDF redaction | background_delta below threshold |
| `table_cell_collision` | tables | cell_reflow | bbox, font metrics | no cell overlap; numeric alignment preserved |
| `table_grid_damage` | tables | preserve_or_redraw_grid | PyMuPDF drawing inspection/render | grid lines continuous |
| `chart_label_overlap` | charts | local_label_reflow | bbox, render | labels readable; chart geometry intact |
| `legend_mismatch` | pies/charts | legend_item_mapping_repair | extraction + visual review | colors and labels aligned |
| `footnote_unreadable` | notes | footnote_microtype_adjust | font size/line height/wrap | minimum readable font and no overflow |
| `side_nav_corruption` | annual report nav | side_nav_region_strategy | visual-only handling | nav preserved or translated consistently |
| `image_collision` | image-adjacent text | avoid_region_reflow | image bbox + layout slots | no text-image collision |

## Generic Repair Loop

1. Select exactly one failure class.
2. Select one repair atom.
3. Patch only affected slots/regions.
4. Regenerate candidate.
5. Re-run only relevant gates plus global residue/page geometry gates.
6. Record result and next state.

Do not repair unrelated regions during the same loop.
