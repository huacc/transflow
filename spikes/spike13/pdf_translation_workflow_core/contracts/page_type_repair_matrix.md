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
| `semantic_translation_authenticity_fail` | all | regenerate_D2_translation_without_meta_description | D2 prompt, semantic validator | no placeholder or line-category pseudo translation patterns |
| `source_relative_visual_baseline_fail` | all generated text regions | rerun_source_extraction_or_generation_evidence_linkage | source_extraction.json, generation evidence unit_ids | each generated region can be compared to current-run source font/line evidence |
| `ascii_residue` | all | retranslate_or_cover_residue | PyMuPDF text extraction, regex, render | no ASCII tokens unless allowed |
| `text_fit_overflow` | all text slots | reduce_font_or_reflow | PyMuPDF insertion, font metrics | fit result non-negative; no clipping |
| `visual_density_low` | body/notes/footnotes | expand_translation_or_adjust_line_height | translation plan, layout plan | text_area_ratio/y_span_ratio recover |
| `source_anchor_order_mismatch` | timeline/body/notes/headings | split_region_at_source_separator | source line indexes, layout policy, generation evidence | no insertion region crosses skipped source lines inside one source block |
| `paragraph_density_mismatch` | body paragraphs | body_flow_grouping | layout policy, source x/width/y-gap statistics, source-vs-output crop | active paragraph gaps shrink without creating overlap; end blank may remain |
| `internal_paragraph_gap` | body paragraphs | body_flow_line_joining_or_line_height_adjust | layout policy `paragraph_gap_pt` and line joiners, render crop | same-paragraph continuations do not receive artificial paragraph breaks |
| `single_dense_paragraph` | body paragraphs | body_flow_paragraph_gap_rebalance | layout policy `paragraph_gap_pt`, source y-gap statistics, render crop | source paragraph gaps become readable target-language paragraph gaps |
| `body_flow_fallback_truncation` | body paragraphs | short_continuation_and_reflow_frame_repair | generation evidence, `allow_short_continuation_lines`, target_language_reflow, overlap_guard | body flow fits without clipped point fallback |
| `dense_page_body_band_fragmentation` | table/body pages | dense_page_body_band_flow_repair | page type guess, lower-page y ratio, same-column x/width evidence | table cells stay preserved while lower-page body text becomes one readable flow |
| `failed_probe_residue` | all textbox regions | textbox_probe_isolation_repair | generator evidence, PNG render | failed fit attempts are not visible in candidate output |
| `font_hierarchy_ratio_mismatch` | notes/body/headings/table labels | role_font_profile_or_region_classification | layout policy, font hierarchy metrics, source-vs-output crop | note/body/title ratios remain close to source |
| `sidebar_orientation_fail` | annual report side navigation | rotated_horizontal_text_image_draw_mode | layout policy draw_modes, rotated horizontal label image insertion, source-vs-output crop | rotated source labels remain rotated, not stacked character-by-character |
| `sidebar_glyph_orientation_fail` | annual report side navigation | backrotated_crop_glyph_check | source-vs-output crop, back-rotated output crop | back-rotated output crop is readable horizontal Chinese |
| `side_nav_group_consistency_fail` | annual report side navigation | side_nav_group_writing_mode_policy | source region grouping, layout policy draw modes | all labels in one side-nav group share the same writing mode |
| `over_narrow_lines` | body/notes | increase_wrap_width_or_reduce_target_lines | layout plan | no near-vertical short lines |
| `background_patch_visible` | colored/table/chart backgrounds | resample_fill_color_or_split_redaction | image sampling, PyMuPDF redaction | background_delta below threshold |
| `table_cell_collision` | tables | table_cell_variant_or_cell_reflow | bbox, font metrics, D2 layout_variants | no cell overlap; numeric alignment preserved |
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
