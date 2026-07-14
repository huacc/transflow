from __future__ import annotations

import tempfile
import unittest
import re
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import fitz

from page_toolbox_puncture.contracts import (
    DrawingObjectFact,
    ImageObjectFact,
    PageFacts,
    PageTranslationBundle,
    PageTranslationRequest,
    TextObjectFact,
    TranslationResult,
    TranslationUnit,
)
from page_toolbox_puncture.sample_snapshot import sha256_file
from page_toolbox_puncture.translation import FixedTranslationProvider
from shared_pdf_kernel.facts import extract_page_facts
from toolboxes.body.composite.flow_text_table.tools.engine import run_p7_page
from toolboxes.body.composite.flow_text_table.tools.layout_planner import (
    _avoid_earlier_placement_overlaps,
    _build_table_region_transforms,
    _contains_locked_table_artwork,
    _elastic_table_region_scales,
    _expand_elastic_table_regions,
    _fit_anchored_grid_to_region,
    _fit_flow_plan_to_region,
    _protected_text_objects_in_region,
    repair_horizontal_table_rule_overlaps,
    _reserve_following_flow_space,
    plan_composite_layout,
    validate_owner_boundaries,
)
from toolboxes.body.composite.flow_text_table.tools.models import (
    CompositeFinding,
    CompositeLayoutPlan,
    TableRegionTransform,
)
from toolboxes.body.composite.flow_text_table.tools.renderer import _page_background_drawings
from toolboxes.body.composite.flow_text_table.tools.renderer import _moved_repaint_requires_residue_check
from toolboxes.body.composite.flow_text_table.tools.translation_request import (
    build_translation_request,
    split_translation_bundle,
)
from toolboxes.body.composite.flow_text_table.tools.template_builder import (
    _expand_table_region_to_owned_cells,
    _expanded_table_ranges,
    _mark_anchored_flow_containers,
    build_composite_template,
)
from toolboxes.body.composite.flow_text_table.tools.translation_guard import translate_with_targeted_guard_retry
from toolboxes.body.composite.flow_text_table.tools.vector_table_builder import (
    _TextGroup,
    _VectorRegion,
    _column_span_for_bbox,
    _split_semantic_rows_with_numeric_peers,
    _table_cells,
    _text_groups,
    prefer_vector_detection,
)
from toolboxes.body.flow_text.single.tools.models import SingleColumnTemplate, TextContainer
from toolboxes.body.flow_text.single.tools.p4_judge import judge_p4_candidate
from toolboxes.body.flow_text.single.tools.p4_layout_planner import _fixed_margin_bottom
from toolboxes.body.flow_text.single.tools.p4_models import P4LayoutPlan, P4Placement
from toolboxes.body.flow_text.single.tools.template_builder import _split_block
from toolboxes.body.table.tools.layout_planner import (
    _missing_protected_tokens,
    _vector_translation_scale_cap,
)
from toolboxes.body.table.tools.models import TableLayoutPlan, TablePlacement
from toolboxes.body.table.tools.renderer import _table_rule_segments


ROOT = Path(__file__).resolve().parents[1]
TOOLBOX = ROOT / "toolboxes" / "body" / "composite" / "flow_text_table"
SOURCE = TOOLBOX / "samples" / "development" / "S2P0055.pdf"
MULTI_TABLE_SOURCE = TOOLBOX / "samples" / "regression" / "S2P0093.pdf"
VERTICAL_REFLOW_SOURCE = TOOLBOX / "samples" / "regression" / "S2P0648.pdf"
FONT_FILE = "C:/Windows/Fonts/msyh.ttc"
BOLD_FONT_FILE = "C:/Windows/Fonts/msyhbd.ttc"


class P7BodyCompositeFlowTextTableTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.facts = extract_page_facts(SOURCE, page_id="P7-S2P0055")
        cls.template = build_composite_template(SOURCE, cls.facts)
        cls.request = build_translation_request(
            cls.template,
            source_language="en",
            target_language="zh-CN",
        )

    def test_every_source_text_object_has_exactly_one_owner(self) -> None:
        ownership = {item.object_id: item.owner for item in self.template.ownerships}
        self.assertEqual({item.object_id for item in self.facts.text_objects}, set(ownership))
        self.assertEqual(len(self.facts.text_objects), len(self.template.ownerships))
        self.assertIn("flow", set(ownership.values()))
        self.assertIn("table", set(ownership.values()))
        self.assertIn("protected", set(ownership.values()))

        flow_ids = {
            object_id
            for region in self.template.flow_regions
            for container in region.template.containers
            for object_id in container.source_object_ids
        }
        table_ids = {
            object_id
            for cell in self.template.table_template.cells
            for object_id in cell.source_object_ids
        }
        self.assertFalse(flow_ids & table_ids)
        self.assertEqual(
            ("before_table", "after_table"),
            tuple(region.relation for region in self.template.flow_regions),
        )

    def test_one_page_request_contains_both_owners_and_slices_back_to_leaf_order(self) -> None:
        owners = {item.container_id: item.owner for item in self.template.container_ownerships}
        requested_owners = {owners[unit.container_id] for unit in self.request.units}
        self.assertEqual({"flow", "table"}, requested_owners)
        self.assertEqual(list(range(len(self.request.units))), [unit.reading_order for unit in self.request.units])

        bundle = self._bundle()
        flow_bundles, table_bundle = split_translation_bundle(self.template, bundle)
        self.assertEqual(len(self.template.flow_regions), len(flow_bundles))
        for region, region_bundle in zip(self.template.flow_regions, flow_bundles):
            self.assertEqual(
                [item.container_id for item in region.template.containers],
                [item.container_id for item in region_bundle.translations],
            )
        self.assertEqual(
            [item.container_id for item in self.template.table_template.translatable_cells],
            [item.container_id for item in table_bundle.translations],
        )

    def test_two_tables_return_intervening_narrative_to_flow_owner(self) -> None:
        facts = extract_page_facts(MULTI_TABLE_SOURCE, page_id="P7-S2P0093")
        template = build_composite_template(MULTI_TABLE_SOURCE, facts)
        self.assertEqual(2, len(template.table_regions))
        middle = next(region for region in template.flow_regions if region.relation == "between_tables")
        middle_text = " ".join(container.source_text for container in middle.template.containers)
        self.assertIn("The Group as a lessor", middle_text)
        self.assertIn("investment property", middle_text)
        table_text = " ".join(cell.source_text for cell in template.table_template.translatable_cells)
        self.assertNotIn("The Group as a lessor", table_text)
        self.assertEqual(
            {item.object_id for item in facts.text_objects},
            {item.object_id for item in template.ownerships},
        )

    def test_table_ranges_keep_multiline_headers_without_swallowing_narrative(self) -> None:
        def cell(row, column, *, role="table_body", span=1):
            return SimpleNamespace(
                row_index=row,
                column_index=column,
                row_span=span,
                role=role,
            )

        cells_by_row = {
            0: [cell(0, 1, role="table_header", span=4)],
            1: [cell(1, 2, role="table_header")],
            2: [cell(2, 3, role="table_header")],
            3: [cell(3, 0, span=2)],
            4: [cell(4, 1), cell(4, 2)],
            5: [cell(5, 1), cell(5, 2)],
            6: [cell(6, 1), cell(6, 2)],
            7: [cell(7, 0)],
            8: [cell(8, 1), cell(8, 2)],
            9: [cell(9, 1), cell(9, 2)],
            10: [cell(10, 1), cell(10, 2)],
            11: [cell(11, 1), cell(11, 2)],
            12: [cell(12, 1), cell(12, 2)],
            13: [cell(13, 1), cell(13, 2)],
            14: [cell(14, 1), cell(14, 2)],
        }

        self.assertEqual(
            [(0, 6), (8, 14)],
            _expanded_table_ranges([[4, 5, 6], [12, 13, 14]], cells_by_row, 15),
        )

    def test_short_label_between_structural_rows_stays_with_the_table(self) -> None:
        def cell(row, column, text, *, translatable=True):
            return SimpleNamespace(
                row_index=row,
                column_index=column,
                row_span=1,
                role="table_body",
                source_text=text,
                translatable=translatable,
            )

        cells_by_row = {
            0: [cell(0, 1, "Header A"), cell(0, 2, "Header B")],
            1: [cell(1, 0, "Row label")],
            2: [cell(2, 0, "Value"), cell(2, 1, "100", translatable=False)],
        }

        self.assertEqual(
            [(0, 2)],
            _expanded_table_ranges([[2]], cells_by_row, 3),
        )

    def test_table_ownership_envelope_includes_owned_source_glyph_overhang(self) -> None:
        inside = SimpleNamespace(source_bbox=(40.0, 90.0, 520.0, 110.0))
        outside = SimpleNamespace(source_bbox=(20.0, 240.0, 540.0, 260.0))

        self.assertEqual(
            ((40.0, 90.0, 520.0, 200.0),),
            _expand_table_region_to_owned_cells(
                ((50.0, 100.0, 500.0, 200.0),),
                (inside, outside),
            ),
        )

    def test_partial_vector_grid_does_not_replace_a_wider_detected_table(self) -> None:
        table = SimpleNamespace(structure=SimpleNamespace(bbox=(0.0, 0.0, 100.0, 200.0)))
        partial = SimpleNamespace(
            grid_cell_count=30,
            regions=((0.0, 50.0, 60.0, 100.0),),
            template=SimpleNamespace(cells=(SimpleNamespace(font_size=8.0),)),
        )
        full_width = SimpleNamespace(
            grid_cell_count=30,
            regions=((0.0, -20.0, 100.0, 220.0),),
            template=SimpleNamespace(cells=(SimpleNamespace(font_size=8.0),)),
        )

        self.assertFalse(prefer_vector_detection(table, partial))
        self.assertTrue(prefer_vector_detection(table, full_width))

    def test_vector_group_preserves_block_line_order_after_continuation_merge(self) -> None:
        def text(object_id, value, bbox, block, span):
            return TextObjectFact(object_id, value, bbox, "Regular", 8.0, 0, block, 0, span)

        group = _TextGroup(
            0,
            0,
            0,
            [
                text("marker", "\uf0b2", (0.0, 0.0, 5.0, 8.0), 1, 0),
                text("label", "Primary phrase", (5.0, 0.0, 70.0, 8.0), 1, 1),
                text("continuation", "continuation", (10.0, 10.0, 55.0, 18.0), 2, 0),
            ],
        )

        self.assertEqual("\uf0b2Primary phrase\ncontinuation", group.text)

    def test_vector_column_assignment_ignores_small_glyph_overhang_at_gridline(self) -> None:
        boundaries = (0.0, 100.0, 200.0)
        self.assertEqual((1, 1), _column_span_for_bbox((99.3, 10.0, 160.0, 20.0), boundaries, 8.0))
        self.assertEqual((1, 1), _column_span_for_bbox((98.7, 10.0, 130.0, 20.0), boundaries, 3.6))
        self.assertEqual((0, 0), _column_span_for_bbox((20.0, 10.0, 100.7, 20.0), boundaries, 8.0))

    def test_vector_rows_in_one_pdf_block_stay_separate_by_structural_cells(self) -> None:
        region = _VectorRegion(
            (0.0, 0.0, 100.0, 40.0),
            (0.0, 100.0),
            (0.0, 20.0, 40.0),
            2,
            ((0.0, 0.0, 100.0, 20.0), (0.0, 20.0, 100.0, 40.0)),
        )
        objects = (
            TextObjectFact("first", "First row", (5.0, 5.0, 45.0, 12.0), "Regular", 8.0, 0, 1, 0, 0),
            TextObjectFact("second", "Second row", (5.0, 25.0, 50.0, 32.0), "Regular", 8.0, 0, 1, 1, 0),
        )

        groups = _text_groups((region,), objects)

        self.assertEqual(2, len(groups))
        self.assertEqual({0, 1}, {group.structural_cell_index for group in groups})

    def test_semantic_rows_inside_one_merged_cell_get_separate_vertical_bands(self) -> None:
        region = _VectorRegion(
            (0.0, 0.0, 100.0, 40.0),
            (0.0, 50.0, 100.0),
            (0.0, 40.0),
            1,
            ((0.0, 0.0, 100.0, 40.0),),
        )
        groups = [
            _TextGroup(0, 1, 1, [TextObjectFact("first", "First", (55.0, 5.0, 80.0, 12.0), "Regular", 8.0, 0, 1, 0, 0)], 0),
            _TextGroup(0, 1, 1, [TextObjectFact("second", "Second", (55.0, 25.0, 85.0, 32.0), "Regular", 8.0, 0, 2, 0, 0)], 0),
        ]

        cells = _table_cells((region,), groups)

        self.assertLessEqual(cells[0].cell_bbox[3], cells[1].cell_bbox[1])
        self.assertTrue(all(cell.cell_bbox[0] == 50.0 for cell in cells))

    def test_multiline_label_splits_when_numeric_peers_prove_distinct_rows(self) -> None:
        labels = _TextGroup(
            0,
            0,
            0,
            [
                TextObjectFact("label-1", "Long-term borrowings", (5.0, 5.0, 45.0, 12.0), "Regular", 8.0, 0, 1, 0, 0),
                TextObjectFact("label-2", "Guarantor", (5.0, 17.0, 45.0, 24.0), "Regular", 8.0, 0, 1, 1, 0),
                TextObjectFact("label-3", "Short-term borrowings", (5.0, 29.0, 45.0, 36.0), "Regular", 8.0, 0, 1, 2, 0),
                TextObjectFact("label-4", "Guarantor", (5.0, 41.0, 45.0, 48.0), "Regular", 8.0, 0, 1, 3, 0),
            ],
        )
        amounts = _TextGroup(
            0,
            1,
            1,
            [
                TextObjectFact("amount-1", "66,208", (60.0, 17.0, 90.0, 24.0), "Regular", 8.0, 0, 2, 0, 0),
                TextObjectFact("amount-2", "758,224", (60.0, 41.0, 90.0, 48.0), "Regular", 8.0, 0, 2, 1, 0),
            ],
        )

        split = _split_semantic_rows_with_numeric_peers([labels, amounts])

        label_groups = [group for group in split if group.column_start == 0]
        self.assertEqual(4, len(label_groups))
        self.assertEqual(
            ["Long-term borrowings", "Guarantor", "Short-term borrowings", "Guarantor"],
            [group.text for group in label_groups],
        )

    def test_horizontal_rule_overlap_moves_only_the_colliding_table_text_upward(self) -> None:
        target = TablePlacement(
            "target",
            "Translated heading",
            (0.0, 0.0, 100.0, 50.0),
            (0.0, 0.0, 100.0, 50.0),
            (10.0, 20.0, 90.0, 45.0),
            (10.0, 20.0),
            FONT_FILE,
            "p6table",
            10.0,
            1.05,
            0,
            "left",
            True,
        )
        neighbor = replace(target, container_id="neighbor", output_bbox=(110.0, 20.0, 190.0, 45.0))
        table_plan = TableLayoutPlan("page", "body.table", "0" * 64, (target, neighbor))
        plan = CompositeLayoutPlan("page", "body.composite.flow_text_table", "en", "zh-CN", (), (), table_plan)
        finding = CompositeFinding(
            "TABLE_LINE_TEXT_OVERLAP",
            "HARD",
            "table_quality_judge",
            None,
            "overlap",
            {
                "overlaps": [
                    {
                        "container_id": "target",
                        "glyph_bbox": [10.0, 20.0, 60.0, 32.0],
                        "orientation": "horizontal",
                        "rule_coordinate": 30.0,
                    }
                ]
            },
        )

        repaired, records = repair_horizontal_table_rule_overlaps(plan, (finding,))

        self.assertEqual((10.0, 17.5, 90.0, 42.5), repaired.table_plan.placements[0].output_bbox)
        self.assertEqual((10.0, 17.5), repaired.table_plan.placements[0].anchor)
        self.assertEqual(neighbor, repaired.table_plan.placements[1])
        self.assertEqual("target", records[0]["container_id"])

    def test_stage_numbers_do_not_split_one_multiline_clinical_label(self) -> None:
        label = _TextGroup(
            0,
            0,
            0,
            [
                TextObjectFact("target", "PD-L1 x 4-1BB", (5.0, 5.0, 45.0, 12.0), "Regular", 8.0, 0, 1, 0, 0),
                TextObjectFact("type", "Bispecific antibody", (5.0, 17.0, 45.0, 24.0), "Regular", 8.0, 0, 1, 1, 0),
            ],
        )
        stages = _TextGroup(
            0,
            1,
            1,
            [
                TextObjectFact("year", "2025", (60.0, 5.0, 90.0, 12.0), "Regular", 8.0, 0, 2, 0, 0),
                TextObjectFact("phase", "1", (60.0, 17.0, 90.0, 24.0), "Regular", 8.0, 0, 2, 1, 0),
            ],
        )

        split = _split_semantic_rows_with_numeric_peers([label, stages])

        self.assertEqual(1, len([group for group in split if group.column_start == 0]))

    def test_vector_table_uses_one_generic_translation_expansion_scale_cap(self) -> None:
        vector_template = SimpleNamespace(
            structure=SimpleNamespace(direct_evidence=("vector_grid_cells",)),
            translatable_cells=(SimpleNamespace(container_id="label", source_text="短句"),),
        )
        cap = _vector_translation_scale_cap(
            vector_template,
            {"label": "This translated sentence occupies substantially more horizontal space"},
            FONT_FILE,
        )
        self.assertGreaterEqual(cap, 0.70)
        self.assertLess(cap, 1.0)

    def test_simultaneous_spatially_separate_pdf_lines_are_not_merged(self) -> None:
        objects = [
            TextObjectFact("page", "7", (20.0, 10.0, 25.0, 20.0), "Regular", 8.0, 0, 1, 0, 0),
            TextObjectFact("header", "Header", (45.0, 10.5, 80.0, 20.5), "Regular", 8.0, 0, 1, 1, 0),
        ]

        groups = _split_block(objects)

        self.assertEqual(2, len(groups))

    def test_composite_marks_tiny_notes_and_image_labels_as_spatial_anchors(self) -> None:
        containers = (
            TextContainer("body", ("body",), "A sufficiently long ordinary paragraph for font evidence.", 0, "body", (40.0, 50.0, 300.0, 65.0), (40.0, 50.0), 10.0, 0),
            TextContainer("note", ("note",), "Small note", 1, "body", (40.0, 200.0, 100.0, 204.0), (40.0, 200.0), 3.0, 0),
            TextContainer("map", ("map",), "Map label", 2, "heading", (150.0, 300.0, 200.0, 312.0), (150.0, 300.0), 8.0, 0),
            TextContainer("heading", ("heading",), "Ordinary heading", 3, "heading", (40.0, 100.0, 140.0, 112.0), (40.0, 100.0), 8.0, 0),
            TextContainer("grid-left", ("grid-left",), "Left label", 4, "body", (40.0, 400.0, 120.0, 412.0), (40.0, 400.0), 8.0, 0),
            TextContainer("grid-right", ("grid-right",), "Right label", 5, "body", (180.0, 400.0, 260.0, 412.0), (180.0, 400.0), 8.0, 0),
        )
        template = SingleColumnTemplate("page", "body.flow_text.single", 600.0, 800.0, containers)
        facts = PageFacts(
            "page",
            "0" * 64,
            600.0,
            800.0,
            0,
            "synthetic",
            image_objects=(ImageObjectFact("image", (100.0, 250.0, 250.0, 400.0), 150, 150, "1" * 64),),
        )

        marked = _mark_anchored_flow_containers(template, facts)
        roles = {item.container_id: item.role for item in marked.containers}

        self.assertEqual("anchored", roles["note"])
        self.assertEqual("image_anchored", roles["map"])
        self.assertEqual("heading", roles["heading"])
        self.assertEqual("anchored_grid", roles["grid-left"])
        self.assertEqual("anchored_grid", roles["grid-right"])

    def test_tiled_page_backgrounds_do_not_turn_headings_into_image_labels(self) -> None:
        containers = (
            TextContainer("body", ("body",), "A sufficiently long ordinary paragraph for font evidence.", 0, "body", (40.0, 50.0, 300.0, 65.0), (40.0, 50.0), 10.0, 0),
            TextContainer("heading", ("heading",), "Ordinary heading", 1, "heading", (40.0, 100.0, 140.0, 112.0), (40.0, 100.0), 8.0, 0),
        )
        template = SingleColumnTemplate("page", "body.flow_text.single", 600.0, 800.0, containers)
        tiled_images = tuple(
            ImageObjectFact(f"tile-{index}", bbox, 300, 240, str(index) * 64)
            for index, bbox in enumerate(
                (
                    (0.0, 0.0, 300.0, 240.0),
                    (300.0, 0.0, 600.0, 240.0),
                    (0.0, 240.0, 300.0, 480.0),
                    (300.0, 240.0, 600.0, 480.0),
                ),
                start=1,
            )
        )
        facts = PageFacts("page", "0" * 64, 600.0, 800.0, 0, "synthetic", image_objects=tiled_images)

        marked = _mark_anchored_flow_containers(template, facts)
        roles = {item.container_id: item.role for item in marked.containers}

        self.assertEqual("heading", roles["heading"])

    def test_spatial_anchors_are_judged_by_rectangle_overlap_not_reading_order(self) -> None:
        containers = (
            TextContainer("body", ("body",), "Body", 0, "body", (40.0, 50.0, 200.0, 65.0), (40.0, 50.0), 8.0, 0),
            TextContainer("left", ("left",), "Note", 1, "anchored", (40.0, 100.0, 120.0, 106.0), (40.0, 100.0), 3.0, 0),
            TextContainer("right", ("right",), "Label", 2, "image_anchored", (140.0, 99.0, 200.0, 107.0), (140.0, 99.0), 5.0, 0),
        )
        template = SingleColumnTemplate("page", "body.flow_text.single", 300.0, 400.0, containers)
        placements = (
            P4Placement("body", "Translated body", "body", containers[0].source_bbox, (40.0, 50.0, 200.0, 65.0), "normal_flow_width_invariant", 8.0, 8.0, 1.0, "source_anchor_cap", 0.0, 0.0, 0, "regular", True),
            P4Placement("left", "Translated note", "anchored", containers[1].source_bbox, (40.0, 100.0, 120.0, 106.0), "semantic_left_anchor_expand", 3.0, 3.0, 1.0, "fixed_spatial_annotation", 0.0, 0.0, 0, "regular", True),
            P4Placement("right", "Translated label", "image_anchored", containers[2].source_bbox, (140.0, 99.0, 200.0, 107.0), "semantic_left_anchor_expand", 5.0, 5.0, 1.0, "fixed_spatial_annotation", 0.0, 0.0, 0, "regular", True),
        )
        plan = P4LayoutPlan("page", "body.flow_text.single", "en", "zh-CN", "test", FONT_FILE, "p4", 40.0, 200.0, 50.0, 380.0, placements)
        with tempfile.TemporaryDirectory() as temporary:
            candidate = Path(temporary) / "candidate.pdf"
            with fitz.open() as document:
                document.new_page(width=300.0, height=400.0)
                document.save(candidate)
            decision = judge_p4_candidate(candidate_pdf=candidate, template=template, plan=plan)

        self.assertFalse([item for item in decision.findings if item.severity == "HARD"])

    def test_fill_only_progress_shapes_are_not_table_rules(self) -> None:
        with fitz.open() as document:
            page = document.new_page(width=100.0, height=100.0)
            shape = page.new_shape()
            shape.draw_line((10.0, 20.0), (90.0, 20.0))
            shape.draw_line((90.0, 20.0), (90.0, 25.0))
            shape.draw_line((90.0, 25.0), (10.0, 20.0))
            shape.finish(color=None, fill=(0.0, 0.0, 1.0), closePath=True)
            shape.commit()
            page.draw_line((10.0, 40.0), (90.0, 40.0), color=(0.0, 0.0, 0.0))

            rules = _table_rule_segments(page, fitz.Rect(0.0, 0.0, 100.0, 100.0))

        self.assertTrue(any(abs(rule[1] - 40.0) < 0.1 for rule in rules))
        self.assertFalse(any(abs(rule[1] - 20.0) < 0.1 for rule in rules))

    def test_cross_owner_write_is_a_hard_finding(self) -> None:
        plan, findings, _ = plan_composite_layout(
            facts=self.facts,
            template=self.template,
            translations=self._bundle(),
            source_language="en",
            target_language="zh-CN",
            font_file=FONT_FILE,
            bold_font_file=BOLD_FONT_FILE,
        )
        self.assertFalse([item for item in findings if item.severity == "HARD"])
        first_flow = plan.flow_plans[0]
        placement = first_flow.placements[0]
        table_box = self.template.table_template.structure.bbox
        bad_placement = replace(
            placement,
            output_bbox=(table_box[0] + 2, table_box[1] + 2, table_box[0] + 80, table_box[1] + 30),
        )
        bad_flow = replace(first_flow, placements=(bad_placement,) + first_flow.placements[1:])
        bad_plan = replace(plan, flow_plans=(bad_flow,) + plan.flow_plans[1:])
        boundary_findings = validate_owner_boundaries(self.template, bad_plan, self.facts)
        self.assertTrue(any(item.code == "CROSS_REGION_WRITE" and item.severity == "HARD" for item in boundary_findings))

        protected_id = next(
            item.object_id
            for item in self.template.ownerships
            if item.owner == "protected"
            and next(source for source in self.facts.text_objects if source.object_id == item.object_id).bbox[1]
            >= self.facts.height * 0.90
        )
        protected_bbox = next(
            item.bbox for item in self.facts.text_objects if item.object_id == protected_id
        )
        margin_flow_index = next(
            index
            for index, flow_plan in enumerate(plan.flow_plans)
            if any(item.role == "margin" for item in flow_plan.placements)
        )
        margin_flow = plan.flow_plans[margin_flow_index]
        margin_index = next(
            index for index, item in enumerate(margin_flow.placements) if item.role == "margin"
        )
        margin_placements = list(margin_flow.placements)
        margin_placements[margin_index] = replace(
            margin_placements[margin_index],
            output_bbox=protected_bbox,
        )
        protected_flow_plans = list(plan.flow_plans)
        protected_flow_plans[margin_flow_index] = replace(
            margin_flow,
            placements=tuple(margin_placements),
        )
        protected_bad_plan = replace(plan, flow_plans=tuple(protected_flow_plans))

        protected_findings = validate_owner_boundaries(
            self.template,
            protected_bad_plan,
            self.facts,
        )
        self.assertTrue(
            any(
                item.code == "PROTECTED_TEXT_WRITE_OVERLAP" and item.severity == "HARD"
                for item in protected_findings
            )
        )

    def test_protected_text_inside_flow_region_is_given_to_leaf_planner(self) -> None:
        inside = TextObjectFact(
            "inside",
            "141",
            (510.0, 20.0, 525.0, 32.0),
            "font",
            8.0,
            0,
            0,
            0,
            0,
        )
        outside = replace(inside, object_id="outside", bbox=(510.0, 220.0, 525.0, 232.0))
        flow = replace(inside, object_id="flow", text="Heading", bbox=(380.0, 20.0, 430.0, 32.0))
        template = SimpleNamespace(
            ownerships=(
                SimpleNamespace(object_id="inside", owner="protected"),
                SimpleNamespace(object_id="outside", owner="protected"),
                SimpleNamespace(object_id="flow", owner="flow"),
            )
        )
        facts = SimpleNamespace(text_objects=(inside, outside, flow))

        selected = _protected_text_objects_in_region(
            template,
            facts,
            (0.0, 0.0, 595.0, 180.0),
        )

        self.assertEqual(("inside",), tuple(item.object_id for item in selected))

    def test_required_literal_violation_retries_only_that_container(self) -> None:
        request = PageTranslationRequest(
            "request",
            "page",
            "en",
            "zh-CN",
            (
                TranslationUnit("date", "31 December", 0, ("31",)),
                TranslationUnit("plain", "Notes", 1),
            ),
        )

        class RetryProvider:
            provider_name = "retry-fixture"
            model_name = "fixture"

            def __init__(self) -> None:
                self.calls = []

            def translate(self, current_request):
                self.calls.append(tuple(unit.container_id for unit in current_request.units))
                rows = []
                for unit in current_request.units:
                    text = "十二月三十一日" if len(self.calls) == 1 and unit.container_id == "date" else "12月31日"
                    if unit.container_id == "plain":
                        text = "附注"
                    rows.append(TranslationResult(unit.container_id, text))
                return PageTranslationBundle(
                    current_request.request_id,
                    current_request.page_id,
                    self.provider_name,
                    self.model_name,
                    tuple(rows),
                )

        provider = RetryProvider()
        bundle, trace = translate_with_targeted_guard_retry(provider, request)
        self.assertEqual([("date", "plain"), ("date",)], provider.calls)
        self.assertEqual("12月31日", bundle.translations[0].translated_text)
        self.assertEqual("PASS", trace[0]["verdict"])

    def test_localized_date_month_satisfies_semantic_literal_guard(self) -> None:
        request = PageTranslationRequest(
            "request",
            "page",
            "zh-CN",
            "en",
            (
                TranslationUnit(
                    "date",
                    "\u622a\u81f32025\u5e746\u670823",
                    0,
                    ("2025", "6", "23"),
                ),
            ),
        )

        class DateProvider:
            provider_name = "date-fixture"
            model_name = "fixture"

            def __init__(self) -> None:
                self.calls = 0

            def translate(self, current_request):
                self.calls += 1
                return PageTranslationBundle(
                    current_request.request_id,
                    current_request.page_id,
                    self.provider_name,
                    self.model_name,
                    (TranslationResult("date", "As at 23 June 2025"),),
                )

        provider = DateProvider()
        bundle, trace = translate_with_targeted_guard_retry(provider, request)
        self.assertEqual(1, provider.calls)
        self.assertEqual("As at 23 June 2025", bundle.translations[0].translated_text)
        self.assertFalse(trace)

    def test_localized_year_month_satisfies_semantic_literal_guard(self) -> None:
        self.assertEqual(
            (),
            _missing_protected_tokens(
                "\u622a\u81f32025\u5e7412\u6708",
                "As of December 2025",
                ("2025", "12"),
            ),
        )

    def test_currency_literals_are_restored_after_targeted_retry(self) -> None:
        request = PageTranslationRequest(
            "request",
            "page",
            "en",
            "zh-CN",
            (TranslationUnit("amount", "Amount (RMB'000)", 0, ("RMB", "000")),),
        )

        class CurrencyProvider:
            provider_name = "currency-fixture"
            model_name = "fixture"

            def __init__(self) -> None:
                self.calls = 0

            def translate(self, current_request):
                self.calls += 1
                return PageTranslationBundle(
                    current_request.request_id,
                    current_request.page_id,
                    self.provider_name,
                    self.model_name,
                    (TranslationResult("amount", "Amount in thousands"),),
                )

        provider = CurrencyProvider()
        bundle, trace = translate_with_targeted_guard_retry(provider, request)
        self.assertEqual(2, provider.calls)
        self.assertEqual("Amount in thousands (RMB 000)", bundle.translations[0].translated_text)
        self.assertTrue(any(item.get("kind") == "REQUIRED_LITERAL_RESTORED" for item in trace))

    def test_visibly_truncated_sentence_retries_only_that_container(self) -> None:
        request = PageTranslationRequest(
            "request",
            "page",
            "zh-CN",
            "en",
            (TranslationUnit("paragraph", "這是一個完整句子。", 0),),
        )

        class RetryProvider:
            provider_name = "retry-fixture"
            model_name = "fixture"

            def __init__(self) -> None:
                self.calls = 0

            def translate(self, current_request):
                self.calls += 1
                text = "This is an incomplete sentence (the" if self.calls == 1 else "This is a complete sentence."
                return PageTranslationBundle(
                    current_request.request_id,
                    current_request.page_id,
                    self.provider_name,
                    self.model_name,
                    (TranslationResult("paragraph", text),),
                )

        provider = RetryProvider()
        bundle, trace = translate_with_targeted_guard_retry(provider, request)
        self.assertEqual(2, provider.calls)
        self.assertEqual("This is a complete sentence.", bundle.translations[0].translated_text)
        self.assertIn("TERMINAL_PUNCTUATION_MISSING", trace[0]["surface_violations"])
        self.assertIn("UNBALANCED_TARGET_BRACKETS", trace[0]["surface_violations"])

    def test_inline_bullet_is_restored_to_source_line_structure(self) -> None:
        request = PageTranslationRequest(
            "request",
            "page",
            "en",
            "zh-CN",
            (TranslationUnit("list", "Metric label\n• Web-based games", 0),),
        )

        class InlineBulletProvider:
            provider_name = "inline-bullet-fixture"
            model_name = "fixture"

            def translate(self, current_request):
                return PageTranslationBundle(
                    current_request.request_id,
                    current_request.page_id,
                    self.provider_name,
                    self.model_name,
                    (TranslationResult("list", "指标标签 • 网页游戏"),),
                )

        bundle, trace = translate_with_targeted_guard_retry(InlineBulletProvider(), request)
        self.assertEqual("指标标签\n• 网页游戏", bundle.translations[0].translated_text)
        self.assertTrue(any(item.get("kind") == "BULLET_LINEBREAK_CANONICALIZED" for item in trace))

    def test_pure_structural_marker_is_preserved_instead_of_translated(self) -> None:
        request = PageTranslationRequest(
            "request",
            "page",
            "en",
            "zh-CN",
            (
                TranslationUnit("marker", "(b)", 0),
                TranslationUnit("label", "Generic label", 1),
            ),
        )

        class MarkerProvider:
            provider_name = "marker-fixture"
            model_name = "fixture"

            def translate(self, current_request):
                return PageTranslationBundle(
                    current_request.request_id,
                    current_request.page_id,
                    self.provider_name,
                    self.model_name,
                    (
                        TranslationResult("marker", "Unrelated phrase"),
                        TranslationResult("label", "通用标签"),
                    ),
                )

        bundle, trace = translate_with_targeted_guard_retry(MarkerProvider(), request)

        self.assertEqual("(b)", bundle.translations[0].translated_text)
        self.assertEqual("通用标签", bundle.translations[1].translated_text)
        self.assertTrue(any(item.get("kind") == "STRUCTURAL_MARKER_PRESERVED" for item in trace))

    def test_private_use_diamond_bullet_is_rendered_with_supported_glyph(self) -> None:
        request = PageTranslationRequest(
            "request",
            "page",
            "en",
            "zh-CN",
            (TranslationUnit("list", "\uf0b2 Item", 0),),
        )

        class DiamondProvider:
            provider_name = "diamond-fixture"
            model_name = "fixture"

            def translate(self, current_request):
                return PageTranslationBundle(
                    current_request.request_id,
                    current_request.page_id,
                    self.provider_name,
                    self.model_name,
                    (TranslationResult("list", "\uf0b2 translated item"),),
                )

        bundle, trace = translate_with_targeted_guard_retry(DiamondProvider(), request)
        self.assertEqual("◇ translated item", bundle.translations[0].translated_text)
        self.assertTrue(any(item.get("kind") == "BULLET_LINEBREAK_CANONICALIZED" for item in trace))

    def test_compact_flow_reclaims_vertical_space_without_moving_table_x_anchors(self) -> None:
        facts = extract_page_facts(VERTICAL_REFLOW_SOURCE, page_id="P7-S2P0648")
        template = build_composite_template(VERTICAL_REFLOW_SOURCE, facts)
        request = build_translation_request(template, source_language="en", target_language="zh-CN")
        bundle = self._compact_bundle(template, request)
        plan, findings, _ = plan_composite_layout(
            facts=facts,
            template=template,
            translations=bundle,
            source_language="en",
            target_language="zh-CN",
            font_file=FONT_FILE,
            bold_font_file=BOLD_FONT_FILE,
        )
        self.assertFalse([item for item in findings if item.severity == "HARD"])
        transform = plan.table_region_transforms[0]
        self.assertEqual(transform.source_bbox[0], transform.target_bbox[0])
        self.assertEqual(transform.source_bbox[2], transform.target_bbox[2])
        self.assertEqual(transform.source_bbox[3], transform.target_bbox[3])
        self.assertLess(transform.target_bbox[1], transform.source_bbox[1] - 20.0)

        region = template.flow_regions[0]
        source_bottom = max(
            item.source_bbox[3] for item in region.template.containers if item.role != "margin"
        )
        target_bottom = max(
            item.output_bbox[3] for item in plan.flow_plans[0].placements if item.role != "margin"
        )
        source_gap = transform.source_bbox[1] - source_bottom
        target_gap = transform.target_bbox[1] - target_bottom
        self.assertAlmostEqual(source_gap, target_gap, delta=0.2)
        self.assertEqual(len(template.table_template.cells), len(plan.table_plan.placements))

    def test_spatial_grid_rows_block_table_reflow_into_their_vertical_band(self) -> None:
        source_table = (50.0, 200.0, 550.0, 400.0)
        body = TextContainer("body", ("body",), "Body", 0, "body", (40.0, 50.0, 300.0, 100.0), (40.0, 50.0), 10.0, 0)
        grid = TextContainer("grid", ("grid",), "Column", 1, "anchored_grid", (300.0, 180.0, 400.0, 196.0), (300.0, 180.0), 8.0, 0)
        region = SimpleNamespace(
            region_id="flow-before_table-000",
            allowed_bbox=(0.0, 0.0, 600.0, 200.0),
            template=SimpleNamespace(containers=(body, grid)),
        )
        template = SimpleNamespace(
            table_regions=(source_table,),
            flow_regions=(region,),
        )
        flow_plan = SimpleNamespace(
            placements=(
                SimpleNamespace(role="body", output_bbox=(40.0, 50.0, 300.0, 80.0)),
                SimpleNamespace(role="anchored_grid", output_bbox=(300.0, 180.0, 400.0, 196.0)),
            )
        )
        facts = SimpleNamespace(width=600.0, height=800.0, image_objects=(), drawing_objects=())

        transform = _build_table_region_transforms(template, (flow_plan,), facts)[0]

        self.assertEqual(source_table, transform.target_bbox)

    def test_page_background_drawing_does_not_lock_table_vertical_reflow(self) -> None:
        facts = PageFacts(
            "page",
            "0" * 64,
            600.0,
            800.0,
            0,
            "synthetic",
            drawing_objects=(
                DrawingObjectFact("background", (-10.0, -10.0, 610.0, 810.0), "1" * 64),
            ),
        )

        self.assertFalse(
            _contains_locked_table_artwork((50.0, 200.0, 550.0, 500.0), facts)
        )

    def test_large_partial_drawing_still_locks_table_vertical_reflow(self) -> None:
        facts = PageFacts(
            "page", "0" * 64, 600.0, 800.0, 0, "synthetic",
            drawing_objects=(DrawingObjectFact("artwork", (0.0, 0.0, 600.0, 500.0), "1" * 64),),
        )

        self.assertTrue(
            _contains_locked_table_artwork((50.0, 200.0, 550.0, 500.0), facts)
        )

    def test_filled_page_underlay_is_identified_for_redraw(self) -> None:
        with fitz.open() as document:
            page = document.new_page(width=200.0, height=300.0)
            page.draw_rect((-5.0, -5.0, 205.0, 305.0), fill=(0.9, 0.9, 0.9), color=None, overlay=False)

            backgrounds = _page_background_drawings(page)

        self.assertEqual(1, len(backgrounds))

    def test_small_table_font_requests_vertical_elasticity(self) -> None:
        cell = SimpleNamespace(
            container_id="cell",
            translatable=True,
            font_size=10.0,
            cell_bbox=(50.0, 220.0, 250.0, 240.0),
        )
        table_template = SimpleNamespace(translatable_cells=(cell,))
        table_plan = SimpleNamespace(
            placements=(SimpleNamespace(container_id="cell", font_size=8.9),),
        )
        transform = TableRegionTransform(
            (50.0, 200.0, 550.0, 400.0),
            (50.0, 200.0, 550.0, 400.0),
            "before",
            4.0,
            4.0,
        )

        self.assertEqual(
            {0: 1.05},
            _elastic_table_region_scales(table_template, table_plan, (transform,)),
        )

    def test_elastic_table_uses_adjacent_vertical_whitespace_without_changing_x(self) -> None:
        before = SimpleNamespace(region_id="before", allowed_bbox=(0.0, 0.0, 600.0, 200.0))
        after = SimpleNamespace(region_id="after", allowed_bbox=(0.0, 400.0, 600.0, 800.0))
        template = SimpleNamespace(flow_regions=(before, after))
        flow_plans = (
            SimpleNamespace(placements=(SimpleNamespace(role="body", output_bbox=(50.0, 100.0, 550.0, 170.0)),)),
            SimpleNamespace(placements=(SimpleNamespace(role="body", output_bbox=(50.0, 440.0, 550.0, 470.0)),)),
        )
        facts = SimpleNamespace(width=600.0, height=800.0, image_objects=(), drawing_objects=())
        transform = TableRegionTransform(
            (50.0, 200.0, 550.0, 400.0),
            (50.0, 190.0, 550.0, 400.0),
            "before",
            20.0,
            20.0,
        )

        expanded = _expand_elastic_table_regions(
            template,
            flow_plans,
            facts,
            (transform,),
            {0: 1.25},
        )

        self.assertEqual((50.0, 174.0, 550.0, 424.0), expanded[0].target_bbox)
        self.assertEqual(4.0, expanded[0].target_gap)

    def test_last_elastic_table_uses_whitespace_before_locked_footer(self) -> None:
        before = SimpleNamespace(region_id="before", allowed_bbox=(0.0, 0.0, 600.0, 200.0))
        template = SimpleNamespace(
            flow_regions=(before,),
            ownerships=(SimpleNamespace(object_id="footer", owner="protected"),),
        )
        flow_plans = (
            SimpleNamespace(placements=(SimpleNamespace(role="body", output_bbox=(50.0, 100.0, 550.0, 170.0)),)),
        )
        facts = PageFacts(
            "page", "0" * 64, 600.0, 800.0, 0, "synthetic",
            text_objects=(TextObjectFact("footer", "Footer", (50.0, 740.0, 150.0, 755.0), "Regular", 8.0, 0, 0, 0, 0),),
        )
        transform = TableRegionTransform(
            (50.0, 200.0, 550.0, 600.0),
            (50.0, 190.0, 550.0, 600.0),
            "before",
            20.0,
            20.0,
        )

        expanded = _expand_elastic_table_regions(
            template,
            flow_plans,
            facts,
            (transform,),
            {0: 1.25},
        )

        self.assertEqual((50.0, 174.0, 550.0, 674.0), expanded[0].target_bbox)

    def test_nearly_stationary_bottom_repaint_is_not_source_residue(self) -> None:
        transform = TableRegionTransform(
            (50.0, 200.0, 550.0, 600.0),
            (50.0, 190.0, 550.0, 600.0),
            "before",
            4.0,
            4.0,
        )
        bottom = SimpleNamespace(source_bbox=(100.0, 590.0, 150.0, 598.0))
        upper = SimpleNamespace(source_bbox=(100.0, 210.0, 150.0, 220.0))

        self.assertFalse(_moved_repaint_requires_residue_check(bottom, (transform,)))
        self.assertTrue(_moved_repaint_requires_residue_check(upper, (transform,)))

    def test_following_flow_can_move_down_to_reserve_table_wrap_space(self) -> None:
        following = SimpleNamespace(region_id="after", allowed_bbox=(0.0, 400.0, 600.0, 800.0))
        template = SimpleNamespace(flow_regions=(following,))
        placement = P4Placement("note", "Note", "anchored_grid", (50.0, 410.0, 550.0, 430.0), (50.0, 410.0, 550.0, 430.0), "anchor", 8.0, 8.0, 1.0, "fixed", 0.0, 0.0, 0, "regular", True)
        plan = P4LayoutPlan("page", "body.flow_text.single", "en", "zh-CN", "test", FONT_FILE, "p4", 50.0, 550.0, 400.0, 750.0, (placement,))
        transform = TableRegionTransform(
            (50.0, 200.0, 550.0, 400.0),
            (50.0, 174.0, 550.0, 400.0),
            "before",
            4.0,
            4.0,
        )

        reserved = _reserve_following_flow_space(
            template,
            (plan,),
            (transform,),
            {0: 1.25},
        )

        self.assertEqual((50.0, 428.0, 550.0, 448.0), reserved[0].placements[0].output_bbox)
        self.assertIn("table_elastic_space", reserved[0].placements[0].vertical_policy)

    def test_margin_bottom_uses_the_next_semantic_anchor_even_when_source_boxes_overlap(self) -> None:
        upper = TextContainer("upper", ("upper",), "Upper", 0, "margin", (100.0, 10.0, 200.0, 60.0), (100.0, 10.0), 20.0, 0)
        lower = TextContainer("lower", ("lower",), "Lower", 1, "body", (120.0, 40.0, 220.0, 55.0), (120.0, 40.0), 8.0, 0)

        self.assertEqual(39.5, _fixed_margin_bottom(upper, (upper, lower), 250.0, 300.0))

    def test_flow_rows_move_only_downward_around_earlier_spatial_rows(self) -> None:
        spatial = P4Placement("spatial", "Spatial", "anchored_grid", (100.0, 100.0, 300.0, 150.0), (100.0, 100.0, 300.0, 150.0), "anchor", 8.0, 8.0, 1.0, "fixed", 0.0, 0.0, 0, "regular", True)
        first = P4Placement("first", "First", "body", (100.0, 130.0, 300.0, 160.0), (100.0, 130.0, 300.0, 160.0), "normal", 8.0, 8.0, 1.0, "flow", 0.0, 0.0, 0, "regular", True)
        second = P4Placement("second", "Second", "body", (100.0, 165.0, 300.0, 180.0), (100.0, 165.0, 300.0, 180.0), "normal", 8.0, 8.0, 1.0, "flow", 5.0, 5.0, 0, "regular", True)
        plan = P4LayoutPlan("page", "body.flow_text.single", "en", "zh-CN", "test", FONT_FILE, "p4", 100.0, 300.0, 100.0, 250.0, (spatial, first, second))

        resolved = _avoid_earlier_placement_overlaps(plan, (0.0, 0.0, 400.0, 250.0))

        self.assertEqual(spatial.output_bbox, resolved.placements[0].output_bbox)
        self.assertEqual((100.0, 150.5, 300.0, 180.5), resolved.placements[1].output_bbox)
        self.assertEqual((100.0, 181.0, 300.0, 196.0), resolved.placements[2].output_bbox)

    def test_spatial_row_and_following_flow_move_down_after_earlier_text(self) -> None:
        body = P4Placement("body", "Body", "body", (100.0, 100.0, 300.0, 120.0), (100.0, 100.0, 300.0, 120.0), "normal", 8.0, 8.0, 1.0, "flow", 0.0, 0.0, 0, "regular", True)
        label = P4Placement("label", "Label", "anchored_grid", (100.0, 119.0, 300.0, 140.0), (100.0, 119.0, 300.0, 140.0), "anchor", 8.0, 8.0, 1.0, "fixed", 0.0, 0.0, 0, "regular", True)
        marker = replace(label, container_id="marker", source_bbox=(70.0, 125.0, 95.0, 137.0), output_bbox=(70.0, 125.0, 95.0, 137.0))
        following = P4Placement("following", "Following", "body", (100.0, 140.5, 300.0, 160.5), (100.0, 140.5, 300.0, 160.5), "normal", 8.0, 8.0, 1.0, "flow", 0.5, 0.5, 0, "regular", True)
        plan = P4LayoutPlan("page", "body.flow_text.single", "en", "zh-CN", "test", FONT_FILE, "p4", 100.0, 300.0, 50.0, 250.0, (body, label, marker, following))

        resolved = _avoid_earlier_placement_overlaps(plan, (0.0, 0.0, 400.0, 250.0))

        self.assertEqual((100.0, 120.5, 300.0, 141.5), resolved.placements[1].output_bbox)
        self.assertEqual((70.0, 126.5, 95.0, 138.5), resolved.placements[2].output_bbox)
        self.assertEqual((100.0, 142.0, 300.0, 162.0), resolved.placements[3].output_bbox)

    def test_spatial_grid_group_moves_only_vertically_to_fit_region_bottom(self) -> None:
        body = P4Placement("body", "Body", "body", (40.0, 50.0, 200.0, 60.0), (40.0, 50.0, 200.0, 60.0), "normal", 8.0, 8.0, 1.0, "normal", 0.0, 0.0, 0, "regular", True)
        left = P4Placement("left", "Left", "anchored_grid", (40.0, 190.0, 100.0, 200.0), (40.0, 190.0, 100.0, 202.0), "anchor", 8.0, 8.0, 1.0, "fixed", 0.0, 0.0, 0, "regular", True)
        right = replace(left, container_id="right", source_bbox=(140.0, 190.0, 200.0, 200.0), output_bbox=(140.0, 190.0, 200.0, 202.0))
        plan = P4LayoutPlan("page", "body.flow_text.single", "en", "zh-CN", "test", FONT_FILE, "p4", 40.0, 200.0, 50.0, 200.0, (body, left, right))

        fitted = _fit_anchored_grid_to_region(plan, (0.0, 0.0, 300.0, 200.0))

        self.assertEqual(body.output_bbox, fitted.placements[0].output_bbox)
        self.assertEqual((40.0, 188.0, 100.0, 200.0), fitted.placements[1].output_bbox)
        self.assertEqual((140.0, 188.0, 200.0, 200.0), fitted.placements[2].output_bbox)

    def test_flow_group_moves_only_upward_to_fit_region_bottom(self) -> None:
        margin = P4Placement("margin", "Margin", "margin", (40.0, 20.0, 200.0, 40.0), (40.0, 20.0, 200.0, 40.0), "margin", 8.0, 8.0, 1.0, "fixed", 0.0, 0.0, 0, "regular", True)
        first = P4Placement("first", "First", "body", (40.0, 100.0, 200.0, 120.0), (40.0, 100.0, 200.0, 120.0), "normal", 8.0, 8.0, 1.0, "flow", 0.0, 0.0, 0, "regular", True)
        second = P4Placement("second", "Second", "body", (40.0, 190.0, 200.0, 202.0), (40.0, 190.0, 200.0, 202.0), "normal", 8.0, 8.0, 1.0, "flow", 70.0, 70.0, 0, "regular", False)
        plan = P4LayoutPlan("page", "body.flow_text.single", "en", "zh-CN", "test", FONT_FILE, "p4", 40.0, 200.0, 50.0, 200.0, (margin, first, second))

        fitted = _fit_flow_plan_to_region(plan, (0.0, 50.0, 300.0, 200.0))

        self.assertEqual(margin.output_bbox, fitted.placements[0].output_bbox)
        self.assertEqual((40.0, 98.0, 200.0, 118.0), fitted.placements[1].output_bbox)
        self.assertEqual((40.0, 188.0, 200.0, 200.0), fitted.placements[2].output_bbox)
        self.assertTrue(fitted.placements[2].fit)
        self.assertIn("region_upward_clamp", fitted.placements[2].vertical_policy)

    def test_fixed_translation_end_to_end_preserves_source(self) -> None:
        translations = {item.container_id: item.translated_text for item in self._bundle().translations}
        source_hash = sha256_file(SOURCE)
        with tempfile.TemporaryDirectory() as temporary:
            result = run_p7_page(
                source_pdf=SOURCE,
                page_id=self.template.page_id,
                run_dir=Path(temporary) / "run",
                provider=FixedTranslationProvider(translations),
                font_file=FONT_FILE,
                bold_font_file=BOLD_FONT_FILE,
                source_language="en",
                target_language="zh-CN",
            )
            self.assertEqual("PASS", result.process_verdict)
            quality_report = Path(temporary) / "run" / "reports" / "quality_decision.json"
            self.assertEqual(
                "PASS",
                result.product_verdict,
                quality_report.read_text(encoding="utf-8"),
            )
            self.assertEqual("PAGE_PASSED", result.terminal_state)
            self.assertTrue((Path(temporary) / "run" / "output" / "candidate.pdf").is_file())
            self.assertTrue((Path(temporary) / "run" / "previews" / "comparison.png").is_file())
            self.assertEqual(source_hash, sha256_file(SOURCE))

    def test_core_has_no_sample_identity_branch(self) -> None:
        source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (TOOLBOX / "tools").glob("*.py")
        )
        self.assertNotIn("S2P0055", source)
        self.assertIsNone(re.search(r"\bS\d+P\d+\b", source))

    def _bundle(self) -> PageTranslationBundle:
        by_id = {}
        for index, unit in enumerate(self.request.units):
            text = f"译文{index}"
            if unit.source_text.rstrip().endswith((".", "!", "?", "。", "！", "？", ";", "；", ":", "：")):
                text += "。"
            cell = next(
                (
                    item
                    for item in self.template.table_template.translatable_cells
                    if item.container_id == unit.container_id
                ),
                None,
            )
            if cell is not None and cell.protected_tokens:
                text += " " + " ".join(cell.protected_tokens)
            by_id[unit.container_id] = text
        return PageTranslationBundle(
            self.request.request_id,
            self.request.page_id,
            "fixed",
            "p7-test",
            tuple(TranslationResult(unit.container_id, by_id[unit.container_id]) for unit in self.request.units),
        )

    @staticmethod
    def _compact_bundle(template, request) -> PageTranslationBundle:
        rows = []
        table_ids = {cell.container_id for cell in template.table_template.translatable_cells}
        for index, unit in enumerate(request.units):
            bullet_count = unit.source_text.count("•")
            if bullet_count:
                has_label = not unit.source_text.lstrip().startswith("•")
                parts = (["标签"] if has_label else []) + ["• 项目"] * bullet_count
                text = "\n".join(parts)
            else:
                text = "译" if unit.container_id in table_ids else f"译文{index}"
            if unit.source_text.rstrip().endswith((".", "!", "?", "。", "！", "？", ";", "；", ":", "：")):
                text += "。"
            if unit.required_literals:
                text += " " + " ".join(unit.required_literals)
            rows.append(TranslationResult(unit.container_id, text))
        return PageTranslationBundle(request.request_id, request.page_id, "fixed", "compact", tuple(rows))


if __name__ == "__main__":
    unittest.main()
