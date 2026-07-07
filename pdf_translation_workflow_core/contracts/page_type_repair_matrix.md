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
| `matrix_or_table_diagram` | many short labels in rows/columns, light rules, sparse diagram lines | two-dimensional structure preservation, no body-flow merge, no fallback body insertion |
| `body_nav` | paragraphs, side navigation, footer | paragraph rhythm, nav preservation, body text area |

## Repair Atoms

| Failure class | Applicable page/region | Repair atom | Target state | Verification |
|---|---|---|---|---|
| `semantic_translation_authenticity_fail` | all | `regenerate_D2_translation_without_meta_description` | `S5_TranslationPlan` | no placeholder, metadata-style, or line-category pseudo translation patterns |
| `semantic_coverage_fail` | all | `regenerate_missing_D2_units` | `S5_TranslationPlan` | every source-language unit has one semantic target-language unit |
| `source_relative_visual_baseline_fail` | all generated text regions | `rerun_source_extraction_or_generation_evidence_linkage` | `S3_SourceExtract` or `S7_GenerateCandidate` | each generated region resolves to current-run source extraction font/line evidence |
| `source_text_residue_fail` | all | `retranslate_or_cover_residue` | `S7_GenerateCandidate` | no source-language residue except explicitly preserved target-language spans |
| `candidate_generation_fail` | all | `regenerate_semantic_backfill` | `S7_GenerateCandidate` | candidate evidence has real PDF, redaction count, insertion count, and unit linkage |
| `text_fit_overflow` | all text slots | `reduce_font_or_reflow`; constrained slots may use `constrained_slot_text_image_fit` | `S6_LayoutPlan` or `S7_GenerateCandidate` | no overflow, clipping, or fallback insertion warning |
| `source_anchor_order_mismatch` | timeline/body/notes/headings | `split_region_at_source_separator` | `S6_LayoutPlan` | no target region crosses visible skipped source anchors |
| `failed_probe_residue` | all textbox regions | `textbox_probe_isolation_repair` | `S7_GenerateCandidate` | failed fit attempts are not visible in candidate output |
| `line_fragmentation` | body paragraphs | `body_flow_region_reflow` | `S6_LayoutPlan` | target prose uses source-relative readable line lengths instead of inherited narrow bboxes |
| `paragraph_density_mismatch` | body paragraphs | `body_flow_grouping` or `font_size_and_region_density_rebalance` | `S6_LayoutPlan` | active paragraph gaps match source rhythm; final blank may remain |
| `internal_paragraph_gap` | body paragraphs | `body_flow_line_joining_or_line_height_adjust` | `S6_LayoutPlan` | same-paragraph continuations do not receive artificial paragraph breaks |
| `single_dense_paragraph` | body paragraphs | `body_flow_paragraph_gap_rebalance` | `S6_LayoutPlan` | source paragraph gaps remain readable in target language |
| `body_flow_fallback_truncation` | body paragraphs | `short_continuation_and_reflow_frame_repair` | `S6_LayoutPlan` | body flow fits without clipped point fallback |
| `dense_page_body_band_fragmentation` | table/body pages | `dense_page_body_band_flow_repair` | `S6_LayoutPlan` | table cells stay preserved while lower-page body text becomes one readable flow |
| `font_hierarchy_ratio_mismatch` | notes/body/headings/table labels | `role_font_profile_or_region_classification` | `S6_LayoutPlan` | note/body/title ratios remain close to source-relative hierarchy |
| `metric_value_hierarchy_fail` | KPI values, financial metric values, percentage/currency callouts | `metric_value_font_hierarchy_repair` | `S6_LayoutPlan` | metric values are classified by current-page source font hierarchy plus a generic numeric amount pattern, unit-only labels stay out of metric roles, and rendering uses source-relative ratios rather than fixed point-size constants |
| `hero_banner_text_readability_fail` | hero/banner titles | `heading_frame_fit_or_short_title_variant` | `S6_LayoutPlan` | banner/title text stays readable and visually proportional |
| `title_readability_fail` | titles/headings | `heading_font_fit_curve_repair` | `S6_LayoutPlan` | title font remains above role floor and source-relative ratio floor |
| `decorative_numeric_merge_bbox_fail` | page-top headings or lead body whose extracted bbox was merged with a trailing decorative numeral/section marker | `decorative_numeric_merge_repair` | `S7_GenerateCandidate` | repair uses current-page bbox height, font rhythm, and neighboring same-column lines; no fixed numeral, page, or title string |
| `body_paragraph_readability_fail` | body/body_flow | `target_composition_body_reflow_repair` | `S6_LayoutPlan` | body text uses target composition before shrinking below readable floor |
| `top_lead_body_reflow_fail` | page-top lead body on dense/matrix pages | `top_lead_body_reflow_policy_repair` | `S6_LayoutPlan` | matrix/table hard-disable keeps table body protected while geometry-proven top lead text is allowed to reflow |
| `table_text_legibility_fail` | table cells/headers | `constrained_slot_layout_fit_repair`; only use `D2_constrained_slot_layout_variants` when semantic validation proves a compact variant is missing | `S7_GenerateCandidate` by default; `S5_TranslationPlan` only for semantic gaps | table grid and numeric alignment are preserved without treating layout failure as retranslation |
| `footnote_readability_fail` | notes/footnotes | `footnote_fit_curve_repair` | `S6_LayoutPlan` | notes remain readable and source-relative to body/table text |
| `legend_label_alignment_fail` | chart/pie legends | `constrained_slot_layout_fit_repair`; only use `D2_constrained_slot_layout_variants` when semantic validation proves a compact variant is missing | `S7_GenerateCandidate` by default; `S5_TranslationPlan` only for semantic gaps | swatch-label alignment and label readability pass without unnecessary retranslation |
| `short_label_legibility_fail` | explanatory page labels / compact labels | `expandable_text_slot_reflow_repair` for explanatory page labels and profile-declared compact labels; `constrained_slot_layout_fit_repair` only for dense table/chart/matrix/legend/nav/TOC hard slots; only use `D2_constrained_slot_layout_variants` when semantic validation proves a compact variant is missing | `S6_LayoutPlan` by default; hard slots use `S7_GenerateCandidate`; `S5_TranslationPlan` only for semantic gaps | short labels are readable without hardcoded abbreviations, while structural slots still preserve table/chart/navigation geometry |
| `sidebar_navigation_legibility_fail` | side navigation | `side_navigation_rotated_image_repair` | `S6_LayoutPlan` | side labels are drawn as rotated label units, not stacked glyphs |
| `sidebar_glyph_orientation_fail` | side navigation | `rotated_horizontal_text_image_draw_mode` | `S6_LayoutPlan` | back-rotated crop is readable horizontal target-language text |
| `side_nav_group_consistency_fail` | side navigation groups | `side_nav_group_writing_mode_policy` | `S6_LayoutPlan` | all labels in one navigation group share the same writing mode |
| `event_card_readability_fail` | timeline/event cards | `event_card_local_fit_repair` | `S6_LayoutPlan` | event text remains local to its year/image anchor |
| `image_color_integrity_fail` | image/chart/photo regions | `image_redaction_exclusion_repair` | `S7_GenerateCandidate` | source images are not removed, recolored, or covered by redaction fill |
| `background_delta_fail` | colored/table/chart backgrounds | `background_fill_resample` | `S7_GenerateCandidate` | redaction fill samples local background, not glyph color or fixed color |
| `background_residue_artifact` | colored/table/chart backgrounds | `background_residue_fill_resample` | `S7_GenerateCandidate` | source-vs-output inner-bbox background remains consistent, constrained text images use local fill-color backgrounds, wide colored source-line redactions are covered by `region_background_cover` or `residual_wide_line_background_cover`, large saturated/color covers use `row_sampled_image_patch` instead of `solid_vector_fill`, and no visible rectangular wipe/fill artifacts remain around translated text or empty redacted areas |
| `insertion_collision_fail` | overlapping generated text regions, especially table/panel headings, body reflow frames, labels, and image-adjacent text | `region_collision_layout_repair` | `S6_LayoutPlan` | generated insertion bboxes for different semantic regions do not materially overlap; panel/table labels with same-row neighbors remain constrained and unsafe target-language reflow expansion is removed |
| `text_image_collision_fail` | image-adjacent text | `avoid_region_reflow` or `image_redaction_exclusion_repair` | `S6_LayoutPlan` or `S7_GenerateCandidate` | no translated text overlaps image/color regions |
| `visual_similarity_fail` | full page or crop | `visual_similarity_targeted_repair` | `S6_LayoutPlan` or `S7_GenerateCandidate` | D7 decomposes similarity failure into concrete gate evidence |
| `table_integrity_fail` | tables | `table_cell_variant_or_grid_preserve_repair` | `S5_TranslationPlan`, `S6_LayoutPlan`, or `S7_GenerateCandidate` | table grid, cells, labels, and numeric alignment remain intact |
| `matrix_diagram_integrity_fail` | matrix/table-diagram pages | `matrix_diagram_table_cell_preserve_repair` | `S6_LayoutPlan` | diagram labels stay in current-run row/column structure; no body_flow or fallback body text inside the matrix |
| `chart_integrity_fail` | charts | `chart_region_preserve_or_label_reflow` | `S6_LayoutPlan` or `S7_GenerateCandidate` | chart geometry is preserved and labels remain readable |

## Generic Repair Loop

1. Select exactly one failure class.
2. Select one repair atom.
3. Patch only affected slots/regions.
4. Regenerate candidate.
5. Re-run only relevant gates plus global residue/page geometry gates.
6. Record result and next state.

Do not repair unrelated regions during the same loop.
