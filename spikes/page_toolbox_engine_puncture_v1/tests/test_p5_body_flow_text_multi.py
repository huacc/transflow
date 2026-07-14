from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import fitz

from page_toolbox_puncture.contracts import ImageObjectFact, PageFacts, PageTranslationBundle, TextObjectFact, TranslationResult
from shared_pdf_kernel.facts import extract_page_facts
from toolboxes.body.flow_text.single.tools.p4_layout_planner import _font_variant, _rendered_lines
from toolboxes.body.flow_text.multi.tools.layout_pattern import (
    build_flow_bands,
    build_layout_pattern_rule_decision,
    infer_multi_band_variant,
)
from toolboxes.body.flow_text.multi.tools.layout_planner import (
    _plan_margins,
    _reflow_repeated_content_bands,
    build_best_multi_plan,
    refresh_post_repair_planning_findings,
)
from toolboxes.body.flow_text.multi.tools.judge import judge_multi_candidate
from toolboxes.body.flow_text.multi.tools.models import (
    ColumnAssignment,
    ColumnBand,
    MultiColumnLayoutPlan,
    MultiColumnTemplate,
    ToolboxFinding,
)
from toolboxes.body.flow_text.multi.tools.orchestrator.template_repair_loop import (
    apply_deterministic_template_repairs,
)
from toolboxes.body.flow_text.multi.tools.orchestrator.layout_repair_loop import (
    apply_deterministic_multi_layout_repairs,
)
from toolboxes.body.flow_text.multi.tools.probes.structural_anchor_probe import (
    probe_horizontal_structural_anchors,
)
from toolboxes.body.flow_text.multi.tools.probes.semantic_paragraph_spacing_probe import (
    probe_semantic_paragraph_transitions,
)
from toolboxes.body.flow_text.multi.tools.repairs.rendered_semantic_spacing_reflow import (
    apply_rendered_semantic_spacing_reflow,
)
from toolboxes.body.flow_text.multi.tools.repairs.post_heading_width_vertical_reflow import (
    apply_post_heading_width_vertical_reflow,
)
from toolboxes.body.flow_text.multi.tools.repairs.semantic_paragraph_fragment_merge import (
    apply_semantic_paragraph_fragment_merge,
)
from toolboxes.body.flow_text.multi.tools.template_builder import (
    build_multi_column_template,
    build_multi_column_template_with_repairs,
)
from toolboxes.body.flow_text.multi.tools.translation_validation import (
    _is_structurally_incomplete_translation,
)
from toolboxes.body.flow_text.multi.tools.validators.rendered_semantic_spacing_rule import (
    evaluate_rendered_semantic_spacing,
)
from toolboxes.body.flow_text.multi.tools.validators.cross_column_extraction_merge_rule import (
    evaluate_cross_column_extraction_merge,
)
from toolboxes.body.flow_text.multi.tools.validators.semantic_paragraph_fragmentation_rule import (
    derive_owner_line_gap_limits,
    evaluate_semantic_paragraph_fragmentation,
    _owner_line_gap_limit,
    _same_semantic_paragraph,
)
from toolboxes.body.flow_text.multi.tools.validators.tolerant_route_rule import (
    evaluate_tolerant_multi_route,
)
from toolboxes.body.flow_text.single.tools.models import TextContainer
from toolboxes.body.flow_text.single.tools.p4_models import P4Placement
from toolboxes.body.flow_text.single.tools.template_builder import build_page_template
from toolboxes.body.flow_text.single.tools.renderer import _textbox_alignment


class P5MultiColumnTest(unittest.TestCase):
    def test_regular_han_body_is_not_mistaken_for_latin_uppercase_heading(self) -> None:
        facts = PageFacts(
            page_id="han-body",
            source_pdf_sha256="0" * 64,
            width=600,
            height=800,
            native_text_object_count=1,
            origin="synthetic-test",
            text_objects=(
                TextObjectFact(
                    "han-body-line",
                    "這是一段使用普通字體呈現的正文內容，不是全大寫英文標題。",
                    (60, 120, 420, 145),
                    "SourceHanSans-Regular",
                    10,
                    2301728,
                    1,
                    0,
                    0,
                ),
            ),
        )

        template = build_page_template(facts)

        self.assertEqual("body", template.containers[0].role)

    def test_one_extraction_block_with_per_column_headers_is_split_before_translation(self) -> None:
        facts = _facts_with_shared_cross_column_header()

        template, repairs = build_multi_column_template_with_repairs(facts)

        header_ids = {"header-left", "header-right"}
        header_containers = [
            item for item in template.containers if set(item.source_object_ids) & header_ids
        ]
        assignment = {item.container_id: item.column_id for item in template.assignments}
        self.assertEqual(2, len(header_containers))
        self.assertEqual(
            {"column-1", "column-2"},
            {assignment[item.container_id] for item in header_containers},
        )
        self.assertTrue(all(len(item.source_object_ids) == 1 for item in header_containers))
        self.assertEqual({9.5}, {item.font_size for item in header_containers})
        self.assertEqual({2301728}, {item.color_srgb for item in header_containers})
        self.assertEqual(1, len(repairs))
        self.assertEqual(
            "cross_column_extraction_merge",
            repairs[0]["rule_decision"]["selected_failure_class"],
        )

    def test_one_visual_line_crossing_the_gutter_is_not_split_into_fake_cells(self) -> None:
        facts = PageFacts(
            page_id="continuous-cross-gutter-line",
            source_pdf_sha256="0" * 64,
            width=600,
            height=800,
            native_text_object_count=2,
            origin="synthetic-test",
            text_objects=(
                TextObjectFact("line-left", "continuous sentence first half", (50, 200, 290, 212), "Helvetica", 9, 0, 10, 0, 0),
                TextObjectFact("line-right", "and its second half", (290, 200, 550, 212), "Helvetica", 9, 0, 10, 0, 1),
            ),
        )
        container = TextContainer(
            "continuous-line",
            ("line-left", "line-right"),
            "continuous sentence first half and its second half",
            0,
            "body",
            (50, 200, 550, 212),
            (50, 200),
            9,
            0,
            "regular",
            None,
        )
        template = MultiColumnTemplate(
            "continuous-cross-gutter-line",
            "body.flow_text.multi",
            600,
            800,
            (
                ColumnBand("column-1", 0, 50, 280, 180, 500),
                ColumnBand("column-2", 1, 300, 550, 180, 500),
            ),
            (container,),
            (ColumnAssignment(container.container_id, "span", 0),),
            (),
        )

        self.assertEqual(
            "PASS",
            evaluate_cross_column_extraction_merge(facts=facts, template=template)["rule_verdict"],
        )

    def test_two_column_template_has_unique_ownership_and_column_major_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "two-column.pdf"
            _write_multi_column_pdf(source, 2)
            facts = extract_page_facts(source, page_id="two-column")
            template = build_multi_column_template(facts)

        self.assertEqual(2, len(template.columns))
        assignments = {item.container_id: item.column_id for item in template.assignments}
        self.assertTrue(all(assignments[item.container_id] in {"column-1", "column-2", "span"} for item in template.containers))
        column_order = [assignments[item.container_id] for item in template.containers if assignments[item.container_id] != "span"]
        self.assertEqual(sorted(column_order), column_order)

    def test_three_column_template_is_detected_without_fixed_coordinates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "three-column.pdf"
            _write_multi_column_pdf(source, 3)
            facts = extract_page_facts(source, page_id="three-column")
            template = build_multi_column_template(facts)

        self.assertEqual(3, len(template.columns))
        self.assertEqual({"column-1", "column-2", "column-3", "span"}, {item.column_id for item in template.assignments})

    def test_local_multi_uses_repeated_narrow_heading_starts_as_fallback_evidence(self) -> None:
        facts = _facts_with_local_heading_columns()

        template = build_multi_column_template(facts)

        assignment = {item.container_id: item.column_id for item in template.assignments}
        self.assertEqual(2, len(template.columns))
        self.assertEqual("span", assignment[next(item.container_id for item in template.containers if "wide-body" in item.source_object_ids)])
        local_owners = {
            assignment[item.container_id]
            for item in template.containers
            if any(object_id.startswith("local-") for object_id in item.source_object_ids)
        }
        self.assertEqual({"column-1", "column-2"}, local_owners)

    def test_low_text_volume_label_column_is_kept_as_real_local_column(self) -> None:
        facts = _facts_with_label_content_columns()

        template = build_multi_column_template(facts)

        assignment = {item.container_id: item.column_id for item in template.assignments}
        label = next(item for item in template.containers if "local-label" in item.source_object_ids)
        intro = next(item for item in template.containers if "wide-intro" in item.source_object_ids)
        self.assertEqual(2, len(template.columns))
        self.assertEqual("column-1", assignment[label.container_id])
        self.assertEqual("span", assignment[intro.container_id])
        self.assertGreater(template.columns[0].left, 120)
        self.assertFalse(template.ambiguous_spanning_container_ids)
        route = evaluate_tolerant_multi_route(template)
        self.assertEqual("ACCEPT_TOLERANT", route["route_verdict"])
        self.assertIn("label_column_with_content_column", route["matched_tolerance_modes"])

    def test_late_spanning_note_is_planned_after_local_columns(self) -> None:
        facts = _facts_with_local_heading_columns(include_late_note=True)
        template = build_multi_column_template(facts)

        plan, _, findings = build_best_multi_plan(
            facts=facts,
            template=template,
            translations=_bundle(template, {}),
            source_language="en",
            target_language="zh-CN",
            font_file="C:/Windows/Fonts/msyh.ttc",
        )

        assignment = {item.container_id: item.column_id for item in template.assignments}
        late = next(item for item in plan.placements if "late-note" in next(value for value in template.containers if value.container_id == item.container_id).source_object_ids)
        column_bottom = max(item.output_bbox[3] for item in plan.placements if assignment[item.container_id].startswith("column-"))
        self.assertGreaterEqual(late.output_bbox[1], column_bottom)
        self.assertFalse(any(item.severity == "HARD" for item in findings))

    def test_long_late_span_uses_available_vertical_space_before_failing(self) -> None:
        facts = _facts_with_local_heading_columns(include_late_note=True)
        template = build_multi_column_template(facts)
        late = next(
            item
            for item in template.containers
            if "late-note" in item.source_object_ids
        )
        late = replace(
            late,
            source_bbox=(60, 750, 540, 770),
            anchor=(60, 750),
        )
        template = replace(
            template,
            containers=tuple(
                late if item.container_id == late.container_id else item
                for item in template.containers
            ),
        )

        plan, _, findings = build_best_multi_plan(
            facts=facts,
            template=template,
            translations=_bundle(
                template,
                {
                    late.container_id: (
                        "A substantially expanded translated closing note uses the available "
                        "lower-page space with natural wrapping. " * 4
                    ),
                },
            ),
            source_language="zh",
            target_language="en",
            font_file="C:/Windows/Fonts/msyh.ttc",
        )

        placement = next(
            item for item in plan.placements if item.container_id == late.container_id
        )
        self.assertTrue(placement.fit)
        self.assertNotIn("vertical-natural", placement.vertical_policy)
        self.assertFalse(any(item.severity == "HARD" for item in findings))

    def test_flow_bands_expose_single_multi_single_refill_pattern(self) -> None:
        facts = _facts_with_local_heading_columns(include_late_note=True)
        template = build_multi_column_template(facts)

        bands = build_flow_bands(template)
        decision = build_layout_pattern_rule_decision(template)

        self.assertEqual(
            ["single", "multi", "single"],
            [item.mode for item in bands if item.mode in {"single", "multi"}],
        )
        self.assertEqual("single_multi_single", decision["pattern"])
        self.assertEqual("multi_owned_single_vertical_reflow", bands[0].refill_strategy)
        self.assertEqual("independent_column_vertical_reflow", bands[1].refill_strategy)

    def test_repeated_paired_multi_bands_reflow_in_page_reading_order(self) -> None:
        template = _repeated_paired_multi_band_template()
        facts = PageFacts(
            template.page_id,
            "0" * 64,
            template.width,
            template.height,
            0,
            "synthetic-test",
        )
        bands = build_flow_bands(template)
        content_bands = [item for item in bands if item.mode in {"single", "multi"}]

        self.assertEqual(
            ["single", "multi", "single", "multi", "single"],
            [item.mode for item in content_bands],
        )
        self.assertEqual(
            "repeated_multi_bands",
            build_layout_pattern_rule_decision(template)["pattern"],
        )
        self.assertEqual("paired_row_columns", infer_multi_band_variant(template))

        middle = next(
            item.container_id
            for item in template.containers
            if item.container_id == "middle-span"
        )
        plan, _, findings = build_best_multi_plan(
            facts=facts,
            template=template,
            translations=_bundle(
                template,
                {
                    middle: "译文段落需要按整页阅读顺序自然增长。" * 18,
                },
            ),
            source_language="en",
            target_language="zh-CN",
            font_file="C:/Windows/Fonts/msyh.ttc",
        )

        placement = {item.container_id: item for item in plan.placements}
        for previous, current in zip(content_bands, content_bands[1:]):
            previous_bottom = max(placement[item].output_bbox[3] for item in previous.container_ids)
            current_top = min(placement[item].output_bbox[1] for item in current.container_ids)
            source_gap = max(0.0, current.top - previous.bottom)
            self.assertGreaterEqual(current_top + 0.01, previous_bottom + source_gap)
        lower_band = content_bands[3]
        self.assertGreater(
            min(placement[item].output_bbox[1] for item in lower_band.container_ids),
            lower_band.top,
        )
        self.assertFalse(any(item.severity == "HARD" for item in findings))

    def test_repeated_independent_multi_bands_keep_intervening_spans_in_order(self) -> None:
        template = _repeated_independent_multi_band_template()
        facts = PageFacts(
            template.page_id,
            "0" * 64,
            template.width,
            template.height,
            0,
            "synthetic-test",
        )
        content_bands = [
            item
            for item in build_flow_bands(template)
            if item.mode in {"single", "multi"}
        ]
        self.assertEqual("independent_columns", infer_multi_band_variant(template))

        plan, _, findings = build_best_multi_plan(
            facts=facts,
            template=template,
            translations=_bundle(
                template,
                {"middle-span": "A page-wide translated explanation grows naturally. " * 14},
            ),
            source_language="zh",
            target_language="en",
            font_file="C:/Windows/Fonts/msyh.ttc",
        )

        placement = {item.container_id: item for item in plan.placements}
        for previous, current in zip(content_bands, content_bands[1:]):
            previous_bottom = max(placement[item].output_bbox[3] for item in previous.container_ids)
            current_top = min(placement[item].output_bbox[1] for item in current.container_ids)
            source_gap = max(0.0, current.top - previous.bottom)
            self.assertGreaterEqual(current_top + 0.01, previous_bottom + source_gap)
        self.assertFalse(any(item.severity == "HARD" for item in findings))

    def test_spacing_probe_keeps_each_single_band_separate_on_mixed_page(self) -> None:
        base = _repeated_independent_multi_band_template()
        subtitle = TextContainer(
            "top-subtitle",
            ("top-subtitle",),
            "Page-wide subtitle.",
            1,
            "body",
            (50, 80, 550, 92),
            (50, 80),
            9,
            0,
            "regular",
            None,
        )
        middle_detail = TextContainer(
            "middle-detail",
            ("middle-detail",),
            "A second paragraph in the intervening page-wide section.",
            6,
            "body",
            (50, 255, 550, 267),
            (50, 255),
            9,
            0,
            "regular",
            None,
        )
        source_tops = {
            "first-left-1": 200,
            "first-right-1": 200,
            "first-right-2": 216,
            "middle-span": 230,
        }
        containers = []
        for item in base.containers:
            if item.container_id == "top-span":
                containers.extend((item, subtitle))
                continue
            top = source_tops.get(item.container_id)
            if top is not None:
                height = item.source_bbox[3] - item.source_bbox[1]
                item = replace(
                    item,
                    source_bbox=(item.source_bbox[0], top, item.source_bbox[2], top + height),
                    anchor=(item.anchor[0], top),
                )
            containers.append(item)
            if item.container_id == "middle-span":
                containers.append(middle_detail)
        assignments = []
        owner_by_id = {item.container_id: item.column_id for item in base.assignments}
        owner_by_id[subtitle.container_id] = "span"
        owner_by_id[middle_detail.container_id] = "span"
        for index, item in enumerate(containers):
            assignments.append(ColumnAssignment(item.container_id, owner_by_id[item.container_id], index))
        template = replace(
            base,
            columns=tuple(replace(column, content_top=200) for column in base.columns),
            containers=tuple(containers),
            assignments=tuple(assignments),
        )
        facts = PageFacts(
            template.page_id,
            "0" * 64,
            template.width,
            template.height,
            0,
            "synthetic-test",
        )
        plan, _, findings = build_best_multi_plan(
            facts=facts,
            template=template,
            translations=_bundle(
                template,
                {item.container_id: item.source_text for item in template.containers},
            ),
            source_language="en",
            target_language="en",
            font_file="C:/Windows/Fonts/msyh.ttc",
        )
        self.assertFalse(any(item.severity == "HARD" for item in findings))

        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / "candidate.pdf"
            document = fitz.open()
            page = document.new_page(width=template.width, height=template.height)
            for placement in plan.placements:
                result = page.insert_textbox(
                    fitz.Rect(placement.output_bbox),
                    placement.translated_text,
                    fontname="p5cjk",
                    fontfile=plan.font_file,
                    fontsize=placement.font_size,
                    lineheight=placement.line_height,
                )
                self.assertGreaterEqual(result, 0)
            document.save(candidate)
            document.close()

            transitions = probe_semantic_paragraph_transitions(
                candidate_pdf=candidate,
                facts=facts,
                template=template,
                plan=plan,
            )

        span_pairs = [
            (item["previous_container_id"], item["next_container_id"])
            for item in transitions
            if item["column_id"] == "span"
        ]
        self.assertEqual(
            [
                ("top-span", "top-subtitle"),
                ("middle-span", "middle-detail"),
            ],
            span_pairs,
        )

    def test_spacing_probe_does_not_compare_columns_across_single_band(self) -> None:
        containers = (
            TextContainer("first-left", ("first-left-1", "first-left-2"), "First left paragraph.", 0, "body", (50, 100, 280, 130), (50, 100), 10, 0, "regular", None),
            TextContainer("first-right", ("first-right-1", "first-right-2"), "First right paragraph.", 1, "body", (320, 100, 550, 130), (320, 100), 10, 0, "regular", None),
            TextContainer("middle-span", ("middle-span",), "Intervening page-wide paragraph.", 2, "body", (50, 200, 550, 230), (50, 200), 10, 0, "regular", None),
            TextContainer("second-left", ("second-left-1", "second-left-2"), "Second left paragraph.", 3, "body", (50, 300, 280, 330), (50, 300), 10, 0, "regular", None),
            TextContainer("second-right", ("second-right-1", "second-right-2"), "Second right paragraph.", 4, "body", (320, 300, 550, 330), (320, 300), 10, 0, "regular", None),
        )
        assignments = (
            ColumnAssignment("first-left", "column-1", 0),
            ColumnAssignment("first-right", "column-2", 0),
            ColumnAssignment("middle-span", "span", 0),
            ColumnAssignment("second-left", "column-1", 1),
            ColumnAssignment("second-right", "column-2", 1),
        )
        template = MultiColumnTemplate(
            "separate-multi-bands",
            "body.flow_text.multi",
            600,
            800,
            (
                ColumnBand("column-1", 0, 50, 280, 100, 500),
                ColumnBand("column-2", 1, 320, 550, 100, 500),
            ),
            containers,
            assignments,
            (),
        )
        translated = {
            "first-left": "First left line.\nSecond left line.",
            "first-right": "First right line.\nSecond right line.",
            "middle-span": "Intervening page-wide paragraph.",
            "second-left": "Second left line.\nAnother left line.",
            "second-right": "Second right line.\nAnother right line.",
        }
        placements = tuple(
            P4Placement(
                item.container_id,
                translated[item.container_id],
                item.role,
                item.source_bbox,
                item.source_bbox,
                "column_width_invariant" if "span" not in item.container_id else "spanning_width_invariant",
                item.font_size,
                item.font_size,
                1.0,
                "synthetic",
                0.0,
                0.0,
                item.color_srgb,
                item.font_weight,
                True,
            )
            for item in containers
        )
        plan = MultiColumnLayoutPlan(
            template.page_id,
            template.toolbox_key,
            "en",
            "en",
            "C:/Windows/Fonts/arial.ttf",
            "helv",
            template.columns,
            (),
            placements,
            (),
            build_flow_bands(template),
        )
        facts = PageFacts(
            template.page_id,
            "0" * 64,
            template.width,
            template.height,
            9,
            "synthetic-test",
            text_objects=(
                TextObjectFact("first-left-1", "First left source line.", (50, 100, 200, 110), "Helvetica", 10, 0, 0, 0, 0),
                TextObjectFact("first-left-2", "Second left source line.", (50, 112, 210, 122), "Helvetica", 10, 0, 0, 1, 0),
                TextObjectFact("first-right-1", "First right source line.", (320, 100, 470, 110), "Helvetica", 10, 0, 1, 0, 0),
                TextObjectFact("first-right-2", "Second right source line.", (320, 112, 480, 122), "Helvetica", 10, 0, 1, 1, 0),
                TextObjectFact("middle-span", "Intervening page-wide paragraph.", (50, 200, 250, 210), "Helvetica", 10, 0, 2, 0, 0),
                TextObjectFact("second-left-1", "Second left source line.", (50, 300, 210, 310), "Helvetica", 10, 0, 3, 0, 0),
                TextObjectFact("second-left-2", "Another left source line.", (50, 312, 205, 322), "Helvetica", 10, 0, 3, 1, 0),
                TextObjectFact("second-right-1", "Second right source line.", (320, 300, 480, 310), "Helvetica", 10, 0, 4, 0, 0),
                TextObjectFact("second-right-2", "Another right source line.", (320, 312, 475, 322), "Helvetica", 10, 0, 4, 1, 0),
            ),
        )

        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / "candidate.pdf"
            document = fitz.open()
            page = document.new_page(width=template.width, height=template.height)
            for placement in plan.placements:
                result = page.insert_textbox(
                    fitz.Rect(placement.output_bbox),
                    placement.translated_text,
                    fontname="helv",
                    fontsize=placement.font_size,
                    lineheight=placement.line_height,
                )
                self.assertGreaterEqual(result, 0)
            document.save(candidate)
            document.close()

            transitions = probe_semantic_paragraph_transitions(
                candidate_pdf=candidate,
                facts=facts,
                template=template,
                plan=plan,
            )

        content_band_by_id = {
            container_id: band.band_id
            for band in plan.flow_bands
            if band.mode in {"single", "multi"}
            for container_id in band.container_ids
        }
        self.assertTrue(
            all(
                content_band_by_id[item["previous_container_id"]]
                == content_band_by_id[item["next_container_id"]]
                for item in transitions
            )
        )

    def test_heading_width_reflow_keeps_repeated_content_band_order(self) -> None:
        template = _repeated_independent_multi_band_template()
        facts = PageFacts(
            template.page_id,
            "0" * 64,
            template.width,
            template.height,
            0,
            "synthetic-test",
        )
        plan, _, _ = build_best_multi_plan(
            facts=facts,
            template=template,
            translations=_bundle(template, {}),
            source_language="en",
            target_language="zh-CN",
            font_file="C:/Windows/Fonts/msyh.ttc",
        )
        plan = replace(
            plan,
            placements=tuple(
                replace(item, horizontal_policy="safe_heading_whitespace_expand")
                if item.container_id == "top-span"
                else item
                for item in plan.placements
            ),
        )

        repaired, _ = apply_post_heading_width_vertical_reflow(
            template=template,
            plan=plan,
        )

        placement = {item.container_id: item for item in repaired.placements}
        content_bands = [
            item for item in repaired.flow_bands if item.mode in {"single", "multi"}
        ]
        for previous, current in zip(content_bands, content_bands[1:]):
            previous_bottom = max(placement[item].output_bbox[3] for item in previous.container_ids)
            current_top = min(placement[item].output_bbox[1] for item in current.container_ids)
            self.assertGreaterEqual(current_top + 0.01, previous_bottom)

    def test_paired_row_variant_preserves_cross_column_row_starts_and_widths(self) -> None:
        facts = _facts_with_paired_rows()
        template = build_multi_column_template(facts)
        self.assertEqual("paired_row_columns", infer_multi_band_variant(template))
        plan, _, findings = build_best_multi_plan(
            facts=facts,
            template=template,
            translations=_bundle(template, {}),
            source_language="en",
            target_language="zh-CN",
            font_file="C:/Windows/Fonts/msyh.ttc",
        )

        assignment = {item.container_id: item.column_id for item in template.assignments}
        left = [item for item in plan.placements if assignment[item.container_id] == "column-1"]
        right = [item for item in plan.placements if assignment[item.container_id] == "column-2"]
        self.assertEqual(len(left), len(right))
        self.assertTrue(all(abs(a.output_bbox[1] - b.output_bbox[1]) < 0.01 for a, b in zip(left, right)))
        source = {item.container_id: item.source_bbox for item in template.containers}
        columns = {item.column_id: item for item in template.columns}
        self.assertTrue(all(item.output_bbox[0] == source[item.container_id][0] for item in (*left, *right)))
        self.assertTrue(
            all(
                item.output_bbox[2] == columns[assignment[item.container_id]].right
                for item in (*left, *right)
            )
        )
        self.assertTrue(
            all(
                -0.01 <= item.output_bbox[1] - source[item.container_id][1] <= 20.0
                for item in (*left, *right)
            )
        )
        self.assertFalse(any(item.severity == "HARD" for item in findings))

    def test_paired_row_fixed_column_width_is_not_rejected_as_width_mutation(self) -> None:
        facts = _facts_with_paired_rows()
        template = build_multi_column_template(facts)
        plan, _, findings = build_best_multi_plan(
            facts=facts,
            template=template,
            translations=_bundle(template, {}),
            source_language="en",
            target_language="zh-CN",
            font_file="C:/Windows/Fonts/msyh.ttc",
        )
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / "candidate.pdf"
            document = fitz.open()
            document.new_page(width=template.width, height=template.height)
            document.save(candidate)
            document.close()
            decision = judge_multi_candidate(
                candidate_pdf=candidate,
                template=template,
                plan=plan,
                upstream_findings=findings,
            )

        self.assertFalse(
            any(item.code == "P5_COLUMN_WIDTH_CHANGED" for item in decision.findings)
        )

    def test_margin_text_may_wrap_upward_inside_verified_footer_space(self) -> None:
        margin = TextContainer(
            "footer-company",
            ("footer-company-source",),
            "Short company name",
            0,
            "margin",
            (50, 770, 170, 782),
            (50, 770),
            8,
            0,
            "regular",
            None,
        )
        facts = PageFacts(
            "wrapped-footer",
            "0" * 64,
            600,
            800,
            3,
            "synthetic-test",
            text_objects=(
                TextObjectFact("body", "Body above footer", (50, 680, 550, 710), "Helvetica", 9, 0, 1, 0, 0),
                TextObjectFact("footer-company-source", "Short company name", margin.source_bbox, "Helvetica", 8, 0, 2, 0, 0),
                TextObjectFact("footer-page", "12", (190, 770, 210, 782), "Helvetica", 8, 0, 3, 0, 0),
                TextObjectFact("footer-locked-area", "LOCKED", (210, 770, 576, 782), "Helvetica", 8, 0, 3, 1, 0),
            ),
        )
        template = MultiColumnTemplate(
            facts.page_id,
            "body.flow_text.multi",
            facts.width,
            facts.height,
            (
                ColumnBand("column-1", 0, 50, 280, 100, 650),
                ColumnBand("column-2", 1, 320, 550, 100, 650),
            ),
            (margin,),
            (ColumnAssignment(margin.container_id, "margin", 0),),
            (),
        )

        placements, findings = _plan_margins(
            facts,
            template,
            [margin],
            {
                margin.container_id: (
                    "A translated corporate footer name that naturally needs multiple lines"
                ),
            },
            "C:/Windows/Fonts/msyh.ttc",
            "p5cjk",
        )

        self.assertLess(placements[0].output_bbox[1], margin.source_bbox[1])
        self.assertEqual(margin.source_bbox[3], placements[0].output_bbox[3])
        self.assertFalse(any(item.severity == "HARD" for item in findings))

    def test_margin_upper_guard_ignores_adjacent_text_on_the_same_baseline(self) -> None:
        margin = TextContainer(
            "footer-label",
            ("footer-label-source",),
            "Report",
            0,
            "margin",
            (212.4, 778.9, 228.3, 787.0),
            (212.4, 778.9),
            8,
            0,
            "bold",
            None,
        )
        facts = PageFacts(
            "adjacent-footer-label",
            "0" * 64,
            600,
            808,
            2,
            "synthetic-test",
            text_objects=(
                TextObjectFact("footer-year", "2024", (195.3, 778.2, 212.6, 786.9), "Helvetica", 8, 0, 1, 0, 0),
                TextObjectFact("footer-label-source", "Report", margin.source_bbox, "Helvetica", 8, 0, 1, 1, 0),
            ),
        )
        template = MultiColumnTemplate(
            facts.page_id,
            "body.flow_text.multi",
            facts.width,
            facts.height,
            (
                ColumnBand("column-1", 0, 50, 280, 100, 650),
                ColumnBand("column-2", 1, 320, 550, 100, 650),
            ),
            (margin,),
            (ColumnAssignment(margin.container_id, "margin", 0),),
            (),
        )

        placements, findings = _plan_margins(
            facts,
            template,
            [margin],
            {margin.container_id: "Annual Report"},
            "C:/Windows/Fonts/msyh.ttc",
            "p5cjk",
        )

        self.assertTrue(placements[0].fit)
        self.assertFalse(any(item.code == "P5_MARGIN_VERTICAL_ESCAPE" for item in findings))

    def test_margin_row_expands_into_safe_left_space_before_locked_year(self) -> None:
        company = TextContainer(
            "footer-company",
            ("footer-company-source",),
            "衍匯亞洲有限公司",
            0,
            "margin",
            (375.8651, 781.1910, 454.1651, 792.7200),
            (375.8651, 781.1910),
            9.0,
            7500145,
            "bold",
            None,
        )
        report = TextContainer(
            "footer-report",
            ("footer-report-source",),
            "年報",
            1,
            "margin",
            (463.1651, 783.0793, 482.0651, 792.0793),
            (463.1651, 783.0793),
            9.0,
            7500145,
            "regular",
            None,
        )
        locked_year = TextObjectFact(
            "footer-year",
            "2025/2026",
            (482.0651, 782.2620, 524.4101, 793.1611),
            "DIN-Light",
            9.0,
            7500145,
            2,
            2,
            0,
        )
        facts = PageFacts(
            "footer-row-safe-left",
            "0" * 64,
            595.276,
            807.874,
            4,
            "synthetic-test",
            text_objects=(
                TextObjectFact("footer-page", "49", (50, 781, 62, 793), "DIN-Light", 9, 7500145, 2, 0, 0),
                TextObjectFact("footer-company-source", company.source_text, company.source_bbox, "SourceHanSans-Bold", 9, 7500145, 2, 1, 0),
                TextObjectFact("footer-report-source", report.source_text, report.source_bbox, "DIN-Light", 9, 7500145, 2, 1, 1),
                locked_year,
            ),
        )
        template = MultiColumnTemplate(
            facts.page_id,
            "body.flow_text.multi",
            facts.width,
            facts.height,
            (
                ColumnBand("column-1", 0, 50, 280, 100, 650),
                ColumnBand("column-2", 1, 320, 550, 100, 650),
            ),
            (company, report),
            (
                ColumnAssignment(company.container_id, "margin", 0),
                ColumnAssignment(report.container_id, "margin", 1),
            ),
            (),
        )

        placements, findings = _plan_margins(
            facts,
            template,
            [company, report],
            {
                company.container_id: "Derivative Asia Limited",
                report.container_id: "Annual Report",
            },
            "C:/Windows/Fonts/msyh.ttc",
            "p5cjk",
        )

        by_id = {item.container_id: item for item in placements}
        self.assertFalse(any(item.severity == "HARD" for item in findings))
        self.assertLess(by_id[company.container_id].output_bbox[0], company.source_bbox[0])
        self.assertLessEqual(by_id[company.container_id].output_bbox[2], by_id[report.container_id].output_bbox[0])
        self.assertLessEqual(by_id[report.container_id].output_bbox[2], locked_year.bbox[0] - 0.5)
        for container in (company, report):
            placement = by_id[container.container_id]
            placement_font_file, placement_resource = _font_variant(
                "C:/Windows/Fonts/msyh.ttc",
                "p5cjk",
                container.font_weight,
            )
            lines = _rendered_lines(
                page_width=template.width,
                page_height=template.height,
                width=placement.output_bbox[2] - placement.output_bbox[0],
                height=placement.output_bbox[3] - placement.output_bbox[1],
                text=placement.translated_text,
                font_size=placement.font_size,
                line_height=placement.line_height,
                font_file=placement_font_file,
                font_resource=placement_resource,
                color_srgb=placement.color_srgb,
            )
            self.assertEqual(1, len(lines), (container.container_id, lines))

    def test_margin_latin_word_fragmentation_is_a_hard_failure_without_safe_space(self) -> None:
        margin = TextContainer(
            "footer-report",
            ("footer-report-source",),
            "年報",
            0,
            "margin",
            (200, 780, 220, 792),
            (200, 780),
            9,
            0,
            "regular",
            None,
        )
        facts = PageFacts(
            "footer-no-safe-space",
            "0" * 64,
            300,
            800,
            3,
            "synthetic-test",
            text_objects=(
                TextObjectFact("left-lock", "LOCK", (12, 780, 199, 792), "Helvetica", 8, 0, 1, 0, 0),
                TextObjectFact("footer-report-source", margin.source_text, margin.source_bbox, "Helvetica", 9, 0, 1, 1, 0),
                TextObjectFact("right-lock", "2025", (220, 780, 288, 792), "Helvetica", 8, 0, 1, 2, 0),
            ),
        )
        template = MultiColumnTemplate(
            facts.page_id,
            "body.flow_text.multi",
            facts.width,
            facts.height,
            (
                ColumnBand("column-1", 0, 30, 140, 100, 650),
                ColumnBand("column-2", 1, 160, 270, 100, 650),
            ),
            (margin,),
            (ColumnAssignment(margin.container_id, "margin", 0),),
            (),
        )

        _, findings = _plan_margins(
            facts,
            template,
            [margin],
            {margin.container_id: "Annual Report"},
            "C:/Windows/Fonts/msyh.ttc",
            "p5cjk",
        )

        self.assertTrue(
            any(
                item.code == "P5_MARGIN_WORD_FRAGMENTATION" and item.severity == "HARD"
                for item in findings
            )
        )

    def test_margin_locked_year_partitions_mirrored_footer_row(self) -> None:
        report = TextContainer(
            "footer-report",
            ("footer-report-source",),
            "年報",
            0,
            "margin",
            (70.8661, 783.0793, 89.7661, 792.0793),
            (70.8661, 783.0793),
            9.0,
            7500145,
            "regular",
            None,
        )
        company = TextContainer(
            "footer-company",
            ("footer-company-source",),
            "衍匯亞洲有限公司",
            1,
            "margin",
            (141.1111, 781.1910, 219.4111, 792.7200),
            (141.1111, 781.1910),
            9.0,
            7500145,
            "bold",
            None,
        )
        locked_year = TextObjectFact(
            "footer-year",
            "2025/2026",
            (89.7661, 782.2620, 132.1111, 793.1611),
            "DIN-Light",
            9.0,
            7500145,
            2,
            1,
            0,
        )
        facts = PageFacts(
            "footer-row-mirrored",
            "0" * 64,
            595.276,
            807.874,
            4,
            "synthetic-test",
            text_objects=(
                TextObjectFact("footer-page", "96", (542, 781, 553, 793), "DIN-Light", 9, 7500145, 2, 0, 0),
                TextObjectFact("footer-report-source", report.source_text, report.source_bbox, "DIN-Light", 9, 7500145, 2, 1, 0),
                locked_year,
                TextObjectFact("footer-company-source", company.source_text, company.source_bbox, "SourceHanSans-Bold", 9, 7500145, 2, 1, 2),
            ),
        )
        template = MultiColumnTemplate(
            facts.page_id,
            "body.flow_text.multi",
            facts.width,
            facts.height,
            (
                ColumnBand("column-1", 0, 50, 280, 100, 650),
                ColumnBand("column-2", 1, 320, 550, 100, 650),
            ),
            (report, company),
            (
                ColumnAssignment(report.container_id, "margin", 0),
                ColumnAssignment(company.container_id, "margin", 1),
            ),
            (),
        )

        placements, findings = _plan_margins(
            facts,
            template,
            [report, company],
            {
                report.container_id: "Annual Report",
                company.container_id: "Derivative Asia Limited",
            },
            "C:/Windows/Fonts/msyh.ttc",
            "p5cjk",
        )

        by_id = {item.container_id: item for item in placements}
        self.assertFalse(any(item.severity == "HARD" for item in findings))
        self.assertLess(by_id[report.container_id].output_bbox[0], report.source_bbox[0])
        self.assertLessEqual(by_id[report.container_id].output_bbox[2], locked_year.bbox[0] - 0.5)
        self.assertGreaterEqual(by_id[company.container_id].output_bbox[0], locked_year.bbox[2] + 0.5)
        for container in (report, company):
            placement = by_id[container.container_id]
            placement_font_file, placement_resource = _font_variant(
                "C:/Windows/Fonts/msyh.ttc",
                "p5cjk",
                container.font_weight,
            )
            lines = _rendered_lines(
                page_width=template.width,
                page_height=template.height,
                width=placement.output_bbox[2] - placement.output_bbox[0],
                height=placement.output_bbox[3] - placement.output_bbox[1],
                text=placement.translated_text,
                font_size=placement.font_size,
                line_height=placement.line_height,
                font_file=placement_font_file,
                font_resource=placement_resource,
                color_srgb=placement.color_srgb,
            )
            self.assertEqual(1, len(lines), (container.container_id, lines))

    def test_margin_row_can_move_into_neighbor_free_segment_after_locked_year(self) -> None:
        company = TextContainer(
            "footer-company",
            ("footer-company-source",),
            "云想科技控股有限公司",
            0,
            "margin",
            (92.126, 777.3549, 190.326, 787.585),
            (92.126, 777.3549),
            10.0,
            0,
            "regular",
            None,
        )
        report = TextContainer(
            "footer-report",
            ("footer-report-source",),
            "年報",
            1,
            "margin",
            (212.438, 778.8589, 228.278, 787.043),
            (212.438, 778.8589),
            8.0,
            0,
            "regular",
            None,
        )
        locked_year = TextObjectFact(
            "footer-year",
            "2024",
            (195.286, 778.2189, 212.598, 786.851),
            "Helvetica",
            8.0,
            0,
            9,
            1,
            1,
        )
        facts = PageFacts(
            "footer-neighbor-free-segment",
            "0" * 64,
            595.276,
            807.874,
            4,
            "synthetic-test",
            text_objects=(
                TextObjectFact("footer-page", "159", (56.6929, 776.673, 73.3729, 787.463), "Helvetica", 10, 0, 9, 0, 0),
                TextObjectFact("footer-company-source", company.source_text, company.source_bbox, "Helvetica", 10, 0, 9, 1, 0),
                locked_year,
                TextObjectFact("footer-report-source", report.source_text, report.source_bbox, "Helvetica", 8, 0, 9, 1, 2),
            ),
        )
        template = MultiColumnTemplate(
            facts.page_id,
            "body.flow_text.multi",
            facts.width,
            facts.height,
            (
                ColumnBand("column-1", 0, 50, 280, 100, 650),
                ColumnBand("column-2", 1, 320, 550, 100, 650),
            ),
            (company, report),
            (
                ColumnAssignment(company.container_id, "margin", 0),
                ColumnAssignment(report.container_id, "margin", 1),
            ),
            (),
        )

        placements, findings = _plan_margins(
            facts,
            template,
            [company, report],
            {
                company.container_id: "Clouds Technology Holdings Limited",
                report.container_id: "Annual Report",
            },
            "C:/Windows/Fonts/msyh.ttc",
            "p5cjk",
        )

        by_id = {item.container_id: item for item in placements}
        self.assertFalse(any(item.severity == "HARD" for item in findings))
        self.assertGreaterEqual(
            by_id[company.container_id].output_bbox[0],
            locked_year.bbox[2] + company.font_size * 0.35,
        )
        self.assertLessEqual(by_id[company.container_id].output_bbox[2], by_id[report.container_id].output_bbox[0])
        for container in (company, report):
            placement = by_id[container.container_id]
            lines = _rendered_lines(
                page_width=template.width,
                page_height=template.height,
                width=placement.output_bbox[2] - placement.output_bbox[0],
                height=placement.output_bbox[3] - placement.output_bbox[1],
                text=placement.translated_text,
                font_size=placement.font_size,
                line_height=placement.line_height,
                font_file="C:/Windows/Fonts/msyh.ttc",
                font_resource="p5cjk",
                color_srgb=placement.color_srgb,
            )
            self.assertEqual(1, len(lines), (container.container_id, lines))

    def test_margin_row_can_move_report_before_locked_year_when_middle_slot_is_narrow(self) -> None:
        company = TextContainer(
            "footer-company",
            ("footer-company-source",),
            "財華社集團有限公司",
            0,
            "margin",
            (395.9851, 808.7089, 470.4812, 819.1409),
            (395.9851, 808.7089),
            8.0,
            0,
            "regular",
            None,
        )
        report = TextContainer(
            "footer-report",
            ("footer-report-source",),
            "年報",
            1,
            "margin",
            (522.2715, 808.9089, 538.5835, 819.533),
            (522.2715, 808.9089),
            8.0,
            0,
            "regular",
            None,
        )
        locked_year = TextObjectFact(
            "footer-year",
            " - 2025/2026 ",
            (470.7859, 809.8049, 521.0579, 819.4609),
            "Helvetica",
            8.0,
            0,
            5,
            0,
            1,
        )
        facts = PageFacts(
            "footer-report-before-year",
            "0" * 64,
            595.276,
            841.89,
            4,
            "synthetic-test",
            text_objects=(
                TextObjectFact("footer-company-source", company.source_text, company.source_bbox, "Helvetica", 8, 0, 5, 0, 0),
                locked_year,
                TextObjectFact("footer-report-source", report.source_text, report.source_bbox, "Helvetica", 8, 0, 5, 0, 2),
                TextObjectFact("footer-page", "58", (555.3366, 806.017, 568.6044, 822.6769), "Helvetica", 13.2816, 0, 5, 1, 0),
            ),
        )
        template = MultiColumnTemplate(
            facts.page_id,
            "body.flow_text.multi",
            facts.width,
            facts.height,
            (
                ColumnBand("column-1", 0, 50, 280, 100, 650),
                ColumnBand("column-2", 1, 320, 550, 100, 650),
            ),
            (company, report),
            (
                ColumnAssignment(company.container_id, "margin", 0),
                ColumnAssignment(report.container_id, "margin", 1),
            ),
            (),
        )

        placements, findings = _plan_margins(
            facts,
            template,
            [company, report],
            {
                company.container_id: "Caihua Group Limited",
                report.container_id: "Annual Report",
            },
            "C:/Windows/Fonts/msyh.ttc",
            "p5cjk",
        )

        by_id = {item.container_id: item for item in placements}
        self.assertFalse(any(item.severity == "HARD" for item in findings))
        self.assertLessEqual(by_id[company.container_id].output_bbox[2], by_id[report.container_id].output_bbox[0])
        self.assertLessEqual(by_id[report.container_id].output_bbox[2], locked_year.bbox[0])
        for container in (company, report):
            placement = by_id[container.container_id]
            lines = _rendered_lines(
                page_width=template.width,
                page_height=template.height,
                width=placement.output_bbox[2] - placement.output_bbox[0],
                height=placement.output_bbox[3] - placement.output_bbox[1],
                text=placement.translated_text,
                font_size=placement.font_size,
                line_height=placement.line_height,
                font_file="C:/Windows/Fonts/msyh.ttc",
                font_resource="p5cjk",
                color_srgb=placement.color_srgb,
            )
            self.assertEqual(1, len(lines), (container.container_id, lines))

    def test_mixed_column_band_may_move_below_its_source_bottom_before_next_single_band(self) -> None:
        columns = (
            ColumnBand("column-1", 0, 50, 280, 100, 140),
            ColumnBand("column-2", 1, 320, 550, 100, 140),
        )
        containers = (
            TextContainer("top", ("top",), "Top", 0, "heading", (50, 50, 550, 70), (50, 50), 10, 0, "regular", None),
            TextContainer("left", ("left",), "Left", 1, "body", (50, 100, 280, 130), (50, 100), 9, 0, "regular", None),
            TextContainer("right", ("right",), "Right", 2, "body", (320, 100, 550, 130), (320, 100), 9, 0, "regular", None),
            TextContainer("tail", ("tail",), "Tail", 3, "body", (50, 160, 550, 180), (50, 160), 9, 0, "regular", None),
        )
        template = MultiColumnTemplate(
            "mixed-dynamic-column-bottom",
            "body.flow_text.multi",
            600,
            800,
            columns,
            containers,
            (
                ColumnAssignment("top", "span", 0),
                ColumnAssignment("left", "column-1", 1),
                ColumnAssignment("right", "column-2", 2),
                ColumnAssignment("tail", "span", 3),
            ),
            (),
        )
        placements = (
            P4Placement("top", "Top", "heading", containers[0].source_bbox, (50, 50, 550, 90), "spanning_width_invariant", 10, 10, 1.0, "test", 0, 0, 0, "regular", True),
            P4Placement("left", "Left", "body", containers[1].source_bbox, (50, 110, 280, 150), "column_width_invariant", 9, 9, 1.0, "test", 0, 0, 0, "regular", True),
            P4Placement("right", "Right", "body", containers[2].source_bbox, (320, 110, 550, 150), "column_width_invariant", 9, 9, 1.0, "test", 0, 0, 0, "regular", True),
            P4Placement("tail", "Tail", "body", containers[3].source_bbox, (50, 165, 550, 185), "spanning_width_invariant", 9, 9, 1.0, "test", 0, 0, 0, "regular", True),
        )
        plan = MultiColumnLayoutPlan(
            template.page_id,
            template.toolbox_key,
            "zh",
            "en",
            "C:/Windows/Fonts/msyh.ttc",
            "p5cjk",
            columns,
            (),
            placements,
            (),
            build_flow_bands(template),
        )

        findings = refresh_post_repair_planning_findings(
            template=template,
            plan=plan,
            findings=(),
        )

        self.assertFalse(any(item.code == "P5_COLUMN_VERTICAL_ESCAPE" for item in findings))

    def test_ordered_flow_compacts_whitespace_before_footer_without_changing_text_height(self) -> None:
        columns = (
            ColumnBand("column-1", 0, 50, 280, 140, 300),
            ColumnBand("column-2", 1, 320, 550, 140, 300),
        )
        containers = (
            TextContainer("top", ("top",), "Top", 0, "heading", (50, 50, 550, 70), (50, 50), 10, 0, "regular", None),
            TextContainer("left", ("left",), "Left", 1, "body", (50, 140, 280, 180), (50, 140), 9, 0, "regular", None),
            TextContainer("right", ("right",), "Right", 2, "body", (320, 140, 550, 180), (320, 140), 9, 0, "regular", None),
            TextContainer("tail-a", ("tail-a",), "Tail A", 3, "body", (50, 240, 550, 300), (50, 240), 9, 0, "regular", None),
            TextContainer("tail-b", ("tail-b",), "Tail B", 4, "body", (50, 330, 550, 390), (50, 330), 9, 0, "regular", None),
            TextContainer("footer", ("footer",), "Footer", 5, "margin", (50, 760, 180, 780), (50, 760), 8, 0, "regular", None),
        )
        owners = {"top": "span", "left": "column-1", "right": "column-2", "tail-a": "span", "tail-b": "span", "footer": "margin"}
        template = MultiColumnTemplate(
            "footer-safe-ordered-flow",
            "body.flow_text.multi",
            600,
            800,
            columns,
            containers,
            tuple(ColumnAssignment(item.container_id, owners[item.container_id], index) for index, item in enumerate(containers)),
            (),
        )
        boxes = {
            "top": (50, 50, 550, 120),
            "left": (50, 180, 280, 300),
            "right": (320, 180, 550, 300),
            "tail-a": (50, 360, 550, 580),
            "tail-b": (50, 640, 550, 790),
            "footer": (50, 760, 180, 780),
        }
        placements = [
            P4Placement(item.container_id, item.source_text, item.role, item.source_bbox, boxes[item.container_id], "test", item.font_size, item.font_size, 1.0, "test", 0, 50 if item.container_id == "tail-b" else 0, 0, item.font_weight, True)
            for item in containers
        ]
        before_height = {item.container_id: item.output_bbox[3] - item.output_bbox[1] for item in placements}

        repaired, findings = _reflow_repeated_content_bands(
            template=template,
            flow_bands=build_flow_bands(template),
            placements=placements,
        )

        by_id = {item.container_id: item for item in repaired}
        content_bottom = max(by_id[item].output_bbox[3] for item in ("top", "left", "right", "tail-a", "tail-b"))
        self.assertLess(content_bottom, by_id["footer"].output_bbox[1])
        self.assertTrue(all(abs((item.output_bbox[3] - item.output_bbox[1]) - before_height[item.container_id]) < 0.01 for item in repaired))
        self.assertGreaterEqual(by_id["tail-b"].output_bbox[1] - by_id["tail-a"].output_bbox[3], 50.0 - 0.01)
        self.assertEqual(boxes["footer"], by_id["footer"].output_bbox)
        self.assertFalse(any(item.severity == "HARD" for item in findings))

        tight_boxes = {
            "top": (50, 50, 550, 120),
            "left": (50, 124, 280, 244),
            "right": (320, 124, 550, 244),
            "tail-a": (50, 248, 550, 468),
            "tail-b": (50, 518, 550, 755.9),
        }
        spacing_placements = tuple(
            replace(item, output_bbox=tight_boxes[item.container_id])
            if item.container_id in tight_boxes
            else item
            for item in repaired
        )
        spacing_before = {item.container_id: item for item in spacing_placements}
        plan = MultiColumnLayoutPlan(
            template.page_id,
            template.toolbox_key,
            "zh",
            "en",
            "C:/Windows/Fonts/msyh.ttc",
            "p5cjk",
            columns,
            (),
            spacing_placements,
            (),
            build_flow_bands(template),
        )
        repaired_plan, application = apply_rendered_semantic_spacing_reflow(
            template=template,
            plan=plan,
            decision={
                "previous_container_id": "tail-a",
                "next_container_id": "tail-b",
                "source_transition_ratio": 1.5,
                "candidate_transition_ratio": 1.0,
                "candidate_line_step_pt": 8.0,
                "selected_failure_class": "semantic_paragraph_spacing_loss",
            },
        )
        after_spacing = {item.container_id: item for item in repaired_plan.placements}
        self.assertGreaterEqual(
            after_spacing["tail-b"].output_bbox[1] - after_spacing["tail-a"].output_bbox[3],
            spacing_before["tail-b"].output_bbox[1] - spacing_before["tail-a"].output_bbox[3] + 4.0 - 0.01,
        )
        self.assertLess(after_spacing["tail-b"].output_bbox[3], after_spacing["footer"].output_bbox[1])
        self.assertLess(after_spacing["tail-b"].font_size, spacing_before["tail-b"].font_size)
        self.assertIsNotNone(application["page_flow_fit_font_scale"])

    def test_top_margin_may_expand_upward_inside_the_page_header(self) -> None:
        margin = TextContainer(
            "header-report",
            ("header-report-source",),
            "Annual Report",
            0,
            "margin",
            (42, 26, 118, 33),
            (42, 26),
            8,
            0,
            "regular",
            None,
        )
        facts = PageFacts(
            "top-margin",
            "0" * 64,
            600,
            800,
            2,
            "synthetic-test",
            text_objects=(
                TextObjectFact("header-report-source", "Annual Report", margin.source_bbox, "Helvetica", 8, 0, 1, 0, 0),
                TextObjectFact("header-company", "Company", (133, 26, 250, 33), "Helvetica", 8, 0, 2, 0, 0),
            ),
            image_objects=(
                ImageObjectFact("header-decoration", (0, 0, 150, 25), 300, 50, "8" * 64),
            ),
        )
        template = MultiColumnTemplate(
            facts.page_id,
            "body.flow_text.multi",
            facts.width,
            facts.height,
            (
                ColumnBand("column-1", 0, 50, 280, 100, 650),
                ColumnBand("column-2", 1, 320, 550, 100, 650),
            ),
            (margin,),
            (ColumnAssignment(margin.container_id, "margin", 0),),
            (),
        )

        placements, findings = _plan_margins(
            facts,
            template,
            [margin],
            {margin.container_id: "2025年年度报告"},
            "C:/Windows/Fonts/msyh.ttc",
            "p5cjk",
        )

        self.assertGreaterEqual(placements[0].output_bbox[1], 0.0)
        self.assertFalse(any(item.severity == "HARD" for item in findings))

    def test_tiled_page_background_does_not_lock_ordinary_column_text(self) -> None:
        facts = _facts_with_tiled_page_background()
        template = build_multi_column_template(facts)
        assignment = {item.container_id: item.column_id for item in template.assignments}

        self.assertFalse(any(owner == "fixed" for owner in assignment.values()))
        self.assertTrue(
            all(
                any(owner == column.column_id for owner in assignment.values())
                for column in template.columns
            )
        )

    def test_short_aligned_cells_establish_the_earlier_column_activity(self) -> None:
        facts = _facts_with_short_aligned_cells_before_long_evidence()
        template = build_multi_column_template(facts)
        assignment = {item.container_id: item.column_id for item in template.assignments}

        for object_id, expected_owner in (
            ("early-left-0", "column-1"),
            ("early-right-0", "column-2"),
            ("early-left-1", "column-1"),
            ("early-right-1", "column-2"),
        ):
            container = next(item for item in template.containers if object_id in item.source_object_ids)
            self.assertEqual(expected_owner, assignment[container.container_id])
        self.assertFalse(template.ambiguous_spanning_container_ids)

    def test_paired_continuation_merges_but_aligned_next_row_stays_separate(self) -> None:
        template = _paired_template_with_numeric_visual_continuation()
        limits = derive_owner_line_gap_limits(template)

        first = evaluate_semantic_paragraph_fragmentation(
            template=template,
            owner_line_gap_limits=limits,
        )
        self.assertEqual(("left-wrap", "left-cont"), (first["previous_container_id"], first["current_container_id"]))
        repaired, _ = apply_semantic_paragraph_fragment_merge(
            template=template,
            previous_container_id=str(first["previous_container_id"]),
            current_container_id=str(first["current_container_id"]),
        )

        second = evaluate_semantic_paragraph_fragmentation(
            template=repaired,
            owner_line_gap_limits=limits,
        )
        self.assertEqual(("right-wrap", "right-numeric-cont"), (second["previous_container_id"], second["current_container_id"]))
        repaired, _ = apply_semantic_paragraph_fragment_merge(
            template=repaired,
            previous_container_id=str(second["previous_container_id"]),
            current_container_id=str(second["current_container_id"]),
        )

        merged = next(item for item in repaired.containers if item.container_id == "right-wrap")
        self.assertIn("2021–2025", merged.source_text)
        self.assertEqual(
            "PASS",
            evaluate_semantic_paragraph_fragmentation(
                template=repaired,
                owner_line_gap_limits=limits,
            )["rule_verdict"],
        )

    def test_post_repair_geometry_refresh_removes_stale_escape_finding(self) -> None:
        facts = _facts_with_local_heading_columns(include_late_note=True)
        template = build_multi_column_template(facts)
        plan, _, findings = build_best_multi_plan(
            facts=facts,
            template=template,
            translations=_bundle(template, {}),
            source_language="en",
            target_language="zh-CN",
            font_file="C:/Windows/Fonts/msyh.ttc",
        )
        stale = findings + (
            ToolboxFinding(
                "P5_SPANNING_VERTICAL_ESCAPE",
                "HARD",
                "old_planner",
                plan.placements[-1].container_id,
                "旧计划病因",
            ),
        )

        refreshed = refresh_post_repair_planning_findings(
            template=template,
            plan=plan,
            findings=stale,
        )

        self.assertFalse(any(item.code == "P5_SPANNING_VERTICAL_ESCAPE" for item in refreshed))

    def test_translation_completeness_detects_structural_truncation(self) -> None:
        self.assertTrue(
            _is_structurally_incomplete_translation(
                source_text="本公司是一间公众公司，其股份在交易所上市。",
                translated_text="The Company is a public company (the ",
                source_language="zh",
                target_language="en",
            )
        )
        self.assertTrue(
            _is_structurally_incomplete_translation(
                source_text="本集团本年度首次采用一项准则修订，该修订涉及一项重要判断，但没有提前采用其他已经发布且尚未生效的准则或修订。",
                translated_text="The Group has adopted the amendment",
                source_language="zh",
                target_language="en",
            )
        )
        self.assertFalse(
            _is_structurally_incomplete_translation(
                source_text="本集团本年度首次采用一项准则修订。",
                translated_text="The Group adopted an amendment for the first time this year.",
                source_language="zh",
                target_language="en",
            )
        )

    def test_semantic_fragment_merge_uses_current_owner_line_rhythm(self) -> None:
        lines = tuple(
            TextContainer(
                f"line-{index}",
                (f"source-{index}",),
                text,
                index,
                "body",
                (56.0, 100.0 + index * 21.2, 286.0, 110.5 + index * 21.2),
                (56.0, 100.0 + index * 21.2),
                10.5,
                2301728,
            )
            for index, text in enumerate(("第一行尚未結束", "第二行仍在繼續", "第三行完成。"))
        )
        gap_limit = _owner_line_gap_limit(list(lines))

        self.assertGreater(gap_limit, 1.0)
        self.assertTrue(
            _same_semantic_paragraph(
                lines[0],
                lines[1],
                column_left=56.0,
                paired_rows=False,
                line_gap_limit_ratio=gap_limit,
            )
        )
        self.assertFalse(
            _is_structurally_incomplete_translation(
                source_text="獲取主要客戶針對交易額的函證確認；及",
                translated_text="Obtaining confirmations from major customers regarding transaction amounts; and",
                source_language="zh",
                target_language="en",
            )
        )

    def test_semantic_fragment_merge_keeps_initial_rhythm_until_owner_is_stable(self) -> None:
        fragments = tuple(
            TextContainer(
                f"fragment-{index}",
                (f"source-{index}",),
                text,
                index,
                "body",
                (56.0, 100.0 + index * 21.2, 286.0, 110.5 + index * 21.2),
                (56.0, 100.0 + index * 21.2),
                10.5,
                2301728,
            )
            for index, text in enumerate(("第一行尚未結束", "第二行仍在繼續", "第三行仍在繼續", "第四行完成。"))
        )
        right = TextContainer("right-terminal", ("source-right",), "右栏独立段落。", 4, "body", (310.0, 100.0, 540.0, 110.5), (310.0, 100.0), 10.5, 2301728)
        template = MultiColumnTemplate(
            page_id="fragment-rhythm",
            toolbox_key="body.flow_text.multi",
            width=600.0,
            height=800.0,
            columns=(
                ColumnBand("column-1", 0, 56.0, 286.0, 100.0, 300.0),
                ColumnBand("column-2", 1, 310.0, 540.0, 100.0, 300.0),
            ),
            containers=(*fragments, right),
            assignments=(
                *(ColumnAssignment(item.container_id, "column-1", index) for index, item in enumerate(fragments)),
                ColumnAssignment(right.container_id, "column-2", 0),
            ),
        )
        facts = PageFacts("fragment-rhythm", "0" * 64, 600.0, 800.0, 0, "synthetic-test")

        repaired, _ = apply_deterministic_template_repairs(facts=facts, template=template)
        owner = {item.container_id: item.column_id for item in repaired.assignments}
        left = [item for item in repaired.containers if owner[item.container_id] == "column-1"]

        self.assertEqual(1, len(left))
        self.assertEqual("第一行尚未結束第二行仍在繼續第三行仍在繼續第四行完成。", left[0].source_text)

    def test_full_width_visual_lines_merge_before_paired_row_inference(self) -> None:
        """两栏普通正文即使源行纵向对齐，也应先恢复自然段而不是保留视觉断行。"""

        columns = (
            ColumnBand("column-1", 0, 60.0, 280.0, 100.0, 220.0),
            ColumnBand("column-2", 1, 320.0, 540.0, 100.0, 220.0),
        )
        containers: list[TextContainer] = []
        assignments: list[ColumnAssignment] = []
        for column_index, column in enumerate(columns):
            owner = column.column_id
            for line_index, text in enumerate(
                (
                    "A continuous paragraph begins on this extracted visual line",
                    "and continues with enough words to occupy the fixed column",
                    "before the target language is allowed to wrap it naturally",
                    "and this is the genuine paragraph ending.",
                )
            ):
                container_id = f"visual-{column_index}-{line_index}"
                containers.append(
                    TextContainer(
                        container_id,
                        (f"source-{column_index}-{line_index}",),
                        text,
                        len(containers),
                        "body",
                        (column.left, 100.0 + line_index * 14.0, column.right, 109.0 + line_index * 14.0),
                        (column.left, 100.0 + line_index * 14.0),
                        9.0,
                        2301728,
                    )
                )
                assignments.append(ColumnAssignment(container_id, owner, line_index))
        template = MultiColumnTemplate(
            page_id="full-width-visual-lines",
            toolbox_key="body.flow_text.multi",
            width=600.0,
            height=800.0,
            columns=columns,
            containers=tuple(containers),
            assignments=tuple(assignments),
        )
        facts = PageFacts("full-width-visual-lines", "0" * 64, 600.0, 800.0, 0, "synthetic-test")

        repaired, records = apply_deterministic_template_repairs(facts=facts, template=template)
        owner = {item.container_id: item.column_id for item in repaired.assignments}

        self.assertEqual(6, len(records))
        self.assertEqual(1, sum(owner[item.container_id] == "column-1" for item in repaired.containers))
        self.assertEqual(1, sum(owner[item.container_id] == "column-2" for item in repaired.containers))
        self.assertEqual("independent_columns", infer_multi_band_variant(repaired))
        self.assertTrue(all(item.source_bbox[0] in {60.0, 320.0} for item in repaired.containers))
        self.assertTrue(all(item.source_bbox[2] in {280.0, 540.0} for item in repaired.containers))

    def test_semantic_fragment_merge_accepts_owner_adjacent_interleaved_columns(self) -> None:
        """左右栏对象可交错存储；修复器应验证同栏相邻，而不是全页数组相邻。"""

        left_first = TextContainer("left-first", ("left-source-1",), "Left paragraph continues", 0, "body", (60.0, 100.0, 280.0, 109.0), (60.0, 100.0), 9.0, 2301728)
        right_first = TextContainer("right-first", ("right-source-1",), "Right paragraph continues", 1, "body", (320.0, 100.0, 540.0, 109.0), (320.0, 100.0), 9.0, 2301728)
        left_second = TextContainer("left-second", ("left-source-2",), "onto the next visual line.", 2, "body", (60.0, 114.0, 280.0, 123.0), (60.0, 114.0), 9.0, 2301728)
        right_second = TextContainer("right-second", ("right-source-2",), "onto its next visual line.", 3, "body", (320.0, 114.0, 540.0, 123.0), (320.0, 114.0), 9.0, 2301728)
        template = MultiColumnTemplate(
            page_id="interleaved-owner-order",
            toolbox_key="body.flow_text.multi",
            width=600.0,
            height=800.0,
            columns=(
                ColumnBand("column-1", 0, 60.0, 280.0, 100.0, 220.0),
                ColumnBand("column-2", 1, 320.0, 540.0, 100.0, 220.0),
            ),
            containers=(left_first, right_first, left_second, right_second),
            assignments=(
                ColumnAssignment("left-first", "column-1", 0),
                ColumnAssignment("right-first", "column-2", 0),
                ColumnAssignment("left-second", "column-1", 1),
                ColumnAssignment("right-second", "column-2", 1),
            ),
        )

        repaired, application = apply_semantic_paragraph_fragment_merge(
            template=template,
            previous_container_id="left-first",
            current_container_id="left-second",
        )

        self.assertEqual("applied", application["status"])
        self.assertEqual(3, len(repaired.containers))
        merged = next(item for item in repaired.containers if item.container_id == "left-first")
        self.assertEqual("Left paragraph continues onto the next visual line.", merged.source_text)
        self.assertEqual(
            ["left-first", "right-first", "right-second"],
            [item.container_id for item in repaired.containers],
        )

    def test_text_near_local_locked_image_uses_fixed_overlay_not_column_flow(self) -> None:
        facts = _facts_with_locked_signature_overlay()
        template = build_multi_column_template(facts)
        assignment = {item.container_id: item.column_id for item in template.assignments}
        overlays = [item for item in template.containers if assignment[item.container_id] == "fixed"]

        plan, _, findings = build_best_multi_plan(
            facts=facts,
            template=template,
            translations=_bundle(
                template,
                {
                    overlays[0].container_id: "Chairman",
                    overlays[1].container_id: "31 March 2026",
                },
            ),
            source_language="zh",
            target_language="en",
            font_file="C:/Windows/Fonts/msyh.ttc",
        )

        placement = {item.container_id: item for item in plan.placements}
        self.assertEqual(2, len(overlays))
        # 签名区短标签以右侧锚点为准；原框装不下完整英文时，只向已证明为空白的左侧扩展。
        self.assertTrue(all(placement[item.container_id].output_bbox[2] == item.source_bbox[2] for item in overlays))
        self.assertTrue(all(placement[item.container_id].output_bbox[0] <= item.source_bbox[0] for item in overlays))
        self.assertTrue(
            all(
                len(
                    _rendered_lines(
                        page_width=facts.width,
                        page_height=facts.height,
                        width=placement[item.container_id].output_bbox[2] - placement[item.container_id].output_bbox[0],
                        height=placement[item.container_id].output_bbox[3] - placement[item.container_id].output_bbox[1],
                        text=placement[item.container_id].translated_text,
                        font_size=placement[item.container_id].font_size,
                        line_height=placement[item.container_id].line_height,
                        font_file="C:/Windows/Fonts/msyh.ttc",
                        font_resource="p5cjk",
                        color_srgb=placement[item.container_id].color_srgb,
                    )
                )
                == 1
                for item in overlays
            )
        )
        self.assertTrue(all(placement[item.container_id].output_bbox[1] >= 340 for item in overlays))
        self.assertFalse(any(item.severity == "HARD" for item in findings))

    def test_internal_column_visual_is_an_obstacle_not_a_vertical_bottom_anchor(self) -> None:
        facts = _facts_with_internal_column_visual()
        template = build_multi_column_template(facts)
        assignment = {item.container_id: item.column_id for item in template.assignments}
        post_visual = [
            item
            for item in template.containers
            if set(item.source_object_ids) & {"right-post-heading", "right-post-body-1", "right-post-body-2"}
        ]

        self.assertTrue(post_visual)
        self.assertTrue(all(assignment[item.container_id] == "column-2" for item in post_visual))
        right_column = next(item for item in template.columns if item.column_id == "column-2")
        self.assertGreater(right_column.content_bottom, 520.0)

        pre_visual = next(
            item for item in template.containers if "right-pre-visual" in item.source_object_ids
        )
        plan, _, findings = build_best_multi_plan(
            facts=facts,
            template=template,
            translations=_bundle(
                template,
                {
                    pre_visual.container_id: (
                        "This translated paragraph uses several natural lines in the fixed column width. "
                        "It must remain readable above the locked visual without drawing across the image. "
                        "Only this locally constrained paragraph may use a tighter finite profile; later "
                        "paragraphs should keep the roomier column profile and flow into page whitespace. "
                        "This additional generic prose makes the translation longer than the source while "
                        "remaining ordinary body text. It must be fitted locally instead of shrinking every "
                        "later paragraph in the column."
                    )
                },
            ),
            source_language="zh",
            target_language="en",
            font_file="C:/Windows/Fonts/msyh.ttc",
        )

        local_visual = facts.image_objects[1].bbox
        column_placements = [
            item for item in plan.placements if assignment[item.container_id] == "column-2"
        ]
        self.assertFalse(any(_rectangles_overlap(item.output_bbox, local_visual) for item in column_placements))
        pre_placement = next(item for item in column_placements if item.container_id == pre_visual.container_id)
        later_placements = [
            item for item in column_placements if item.output_bbox[1] > local_visual[3]
        ]
        self.assertTrue(later_placements)
        self.assertIn("locked_visual_obstacle_local_fit", pre_placement.vertical_policy)
        self.assertTrue(all(item.font_size >= pre_placement.font_size for item in later_placements))
        self.assertTrue(
            any(
                item.role in {"body", "list"}
                and item.font_size > pre_placement.font_size + 0.01
                for item in later_placements
            )
        )
        colliding_placement = replace(
            pre_placement,
            output_bbox=(
                pre_placement.output_bbox[0],
                pre_placement.output_bbox[1],
                pre_placement.output_bbox[2],
                local_visual[1] + 10.0,
            ),
        )
        colliding_plan = replace(
            plan,
            placements=tuple(
                colliding_placement if item.container_id == pre_placement.container_id else item
                for item in plan.placements
            ),
        )
        refreshed = refresh_post_repair_planning_findings(
            facts=facts,
            template=template,
            plan=colliding_plan,
            findings=(),
        )
        self.assertIn(
            "P5_LOCKED_VISUAL_TEXT_COLLISION",
            {item.code for item in refreshed if item.severity == "HARD"},
        )
        self.assertFalse(any(item.severity == "HARD" for item in findings))

    def test_changing_one_column_translation_does_not_move_other_column(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "independent-column.pdf"
            _write_multi_column_pdf(source, 2)
            facts = extract_page_facts(source, page_id="independent-column")
            template = build_multi_column_template(facts)
            base = _bundle(template, {})
            plan_a, _, _ = build_best_multi_plan(facts=facts, template=template, translations=base, source_language="en", target_language="zh-CN", font_file="C:/Windows/Fonts/msyh.ttc")
            right_id = next(item.container_id for item in template.assignments if item.column_id == "column-2")
            changed = _bundle(template, {right_id: "目标文字 " * 24})
            plan_b, _, _ = build_best_multi_plan(facts=facts, template=template, translations=changed, source_language="en", target_language="zh-CN", font_file="C:/Windows/Fonts/msyh.ttc")

        assignment = {item.container_id: item.column_id for item in template.assignments}
        left_a = {item.container_id: item.output_bbox for item in plan_a.placements if assignment[item.container_id] == "column-1"}
        left_b = {item.container_id: item.output_bbox for item in plan_b.placements if assignment[item.container_id] == "column-1"}
        self.assertEqual(left_a, left_b)

    def test_safe_space_expands_wrapped_heading_but_not_body_width(self) -> None:
        facts = _facts_with_shared_cross_column_header()
        template = build_multi_column_template(facts)
        assignment = {item.container_id: item.column_id for item in template.assignments}
        left_heading = next(
            item for item in template.containers
            if assignment[item.container_id] == "column-1" and item.role == "heading"
        )
        left_body = next(
            item for item in template.containers
            if assignment[item.container_id] == "column-1" and item.role == "body"
        )
        translations = _bundle(template, {left_heading.container_id: "Concise review topic"})
        plan, _, _ = build_best_multi_plan(
            facts=facts,
            template=template,
            translations=translations,
            source_language="en",
            target_language="zh-CN",
            font_file="C:/Windows/Fonts/msyh.ttc",
        )

        repaired, records = apply_deterministic_multi_layout_repairs(
            facts=facts,
            template=template,
            plan=plan,
        )

        before = {item.container_id: item for item in plan.placements}
        after = {item.container_id: item for item in repaired.placements}
        self.assertGreater(
            after[left_heading.container_id].output_bbox[2],
            before[left_heading.container_id].output_bbox[2],
        )
        self.assertEqual(
            before[left_body.container_id].output_bbox[0::2],
            after[left_body.container_id].output_bbox[0::2],
        )
        self.assertTrue(records)
        self.assertEqual(
            "avoidable_short_line_wrap_with_safe_space",
            records[0]["rule_decision"]["selected_failure_class"],
        )

    def test_tiled_page_background_does_not_block_single_band_title_width(self) -> None:
        facts = _facts_with_tiled_page_background()
        facts = replace(
            facts,
            text_objects=tuple(
                replace(item, bbox=(50, 60, 125, 82))
                if item.object_id == "page-title"
                else item
                for item in facts.text_objects
            ),
        )
        template = build_multi_column_template(facts)
        assignment = {item.container_id: item.column_id for item in template.assignments}
        title = next(
            item for item in template.containers
            if assignment[item.container_id] == "span" and item.role == "heading"
        )
        plan, _, _ = build_best_multi_plan(
            facts=facts,
            template=template,
            translations=_bundle(template, {title.container_id: "Notes to the Financial Statements"}),
            source_language="zh",
            target_language="en",
            font_file="C:/Windows/Fonts/msyh.ttc",
        )

        repaired, records = apply_deterministic_multi_layout_repairs(
            facts=facts,
            template=template,
            plan=plan,
        )

        before = next(item for item in plan.placements if item.container_id == title.container_id)
        after = next(item for item in repaired.placements if item.container_id == title.container_id)
        self.assertGreater(after.output_bbox[2], before.output_bbox[2])
        self.assertEqual(max(item.right for item in template.columns), after.output_bbox[2])
        self.assertTrue(records)

    def test_local_raster_underlay_does_not_block_single_band_title_width(self) -> None:
        facts = _facts_with_tiled_page_background()
        facts = replace(
            facts,
            text_objects=tuple(
                replace(item, bbox=(50, 60, 125, 82))
                if item.object_id == "page-title"
                else item
                for item in facts.text_objects
            ),
        )
        template = build_multi_column_template(facts)
        facts = replace(
            facts,
            image_objects=(
                ImageObjectFact("header-underlay", (0, 0, 600, 70), 1200, 140, "9" * 64),
            ),
        )
        assignment = {item.container_id: item.column_id for item in template.assignments}
        title = next(
            item for item in template.containers
            if assignment[item.container_id] == "span" and item.role == "heading"
        )
        plan, _, _ = build_best_multi_plan(
            facts=facts,
            template=template,
            translations=_bundle(template, {title.container_id: "Notes to the Consolidated Financial Statements"}),
            source_language="zh",
            target_language="en",
            font_file="C:/Windows/Fonts/msyh.ttc",
        )

        repaired, _ = apply_deterministic_multi_layout_repairs(
            facts=facts,
            template=template,
            plan=plan,
        )

        before = next(item for item in plan.placements if item.container_id == title.container_id)
        after = next(item for item in repaired.placements if item.container_id == title.container_id)
        self.assertGreater(after.output_bbox[2], before.output_bbox[2])
        self.assertEqual(max(item.right for item in template.columns), after.output_bbox[2])

    def test_single_band_body_uses_full_text_region_before_wrapping(self) -> None:
        template = _repeated_independent_multi_band_template()
        template = replace(
            template,
            containers=tuple(
                replace(item, source_bbox=(50, 260, 170, 280))
                if item.container_id == "middle-span"
                else item
                for item in template.containers
            ),
        )
        facts = PageFacts(
            template.page_id,
            "0" * 64,
            template.width,
            template.height,
            0,
            "synthetic-test",
        )
        plan, _, _ = build_best_multi_plan(
            facts=facts,
            template=template,
            translations=_bundle(
                template,
                {"middle-span": "This complete sentence should use the full single text region before wrapping."},
            ),
            source_language="zh",
            target_language="en",
            font_file="C:/Windows/Fonts/msyh.ttc",
        )

        repaired, records = apply_deterministic_multi_layout_repairs(
            facts=facts,
            template=template,
            plan=plan,
        )

        after = next(item for item in repaired.placements if item.container_id == "middle-span")
        self.assertEqual(max(item.right for item in template.columns), after.output_bbox[2])
        self.assertEqual("safe_flow_whitespace_expand", after.horizontal_policy)
        self.assertTrue(
            any(
                item["rule_decision"]["container_id"] == "middle-span"
                for item in records
                if "container_id" in item["rule_decision"]
            )
        )

    def test_multi_band_body_uses_only_its_own_column_boundary(self) -> None:
        template = _repeated_independent_multi_band_template()
        facts = PageFacts(
            template.page_id,
            "0" * 64,
            template.width,
            template.height,
            0,
            "synthetic-test",
        )
        plan, _, _ = build_best_multi_plan(
            facts=facts,
            template=template,
            translations=_bundle(
                template,
                {"first-left-1": "Use the complete column width before wrapping."},
            ),
            source_language="zh",
            target_language="en",
            font_file="C:/Windows/Fonts/msyh.ttc",
        )

        repaired, _ = apply_deterministic_multi_layout_repairs(
            facts=facts,
            template=template,
            plan=plan,
        )

        after = next(item for item in repaired.placements if item.container_id == "first-left-1")
        left_column = next(item for item in template.columns if item.column_id == "column-1")
        self.assertEqual(left_column.right, after.output_bbox[2])
        self.assertLess(after.output_bbox[2], min(item.left for item in template.columns if item.column_id != "column-1"))
        self.assertEqual("safe_flow_whitespace_expand", after.horizontal_policy)

    def test_short_line_rule_ignores_fixed_overlay_heading(self) -> None:
        """固定覆盖层不属于 span/column 流，不能进入普通标题横向扩宽规则。"""

        facts = _facts_with_locked_signature_overlay()
        template = build_multi_column_template(facts)
        assignment = {item.container_id: item.column_id for item in template.assignments}
        fixed_id = next(
            item.container_id for item in template.containers
            if assignment[item.container_id] == "fixed"
        )
        template = replace(
            template,
            containers=tuple(
                replace(item, role="heading") if item.container_id == fixed_id else item
                for item in template.containers
            ),
        )
        plan, _, _ = build_best_multi_plan(
            facts=facts,
            template=template,
            translations=_bundle(template, {fixed_id: "A translated fixed overlay heading"}),
            source_language="zh",
            target_language="en",
            font_file="C:/Windows/Fonts/msyh.ttc",
        )

        repaired, _ = apply_deterministic_multi_layout_repairs(
            facts=facts,
            template=template,
            plan=plan,
        )

        before = next(item for item in plan.placements if item.container_id == fixed_id)
        after = next(item for item in repaired.placements if item.container_id == fixed_id)
        self.assertEqual(before.output_bbox, after.output_bbox)

    def test_stacked_prelude_bbox_overlap_is_not_treated_as_same_line_obstacle(self) -> None:
        facts = _facts_with_shared_cross_column_header()
        template = build_multi_column_template(facts)
        subtitle = next(
            item for item in template.containers if "page-subtitle" in item.source_object_ids
        )
        translations = _bundle(
            template,
            {subtitle.container_id: "For the Complete Reporting Period"},
        )
        plan, _, _ = build_best_multi_plan(
            facts=facts,
            template=template,
            translations=translations,
            source_language="en",
            target_language="zh-CN",
            font_file="C:/Windows/Fonts/msyh.ttc",
        )

        repaired, records = apply_deterministic_multi_layout_repairs(
            facts=facts,
            template=template,
            plan=plan,
        )

        before = next(item for item in plan.placements if item.container_id == subtitle.container_id)
        after = next(item for item in repaired.placements if item.container_id == subtitle.container_id)
        self.assertGreater(after.output_bbox[2], before.output_bbox[2])
        self.assertTrue(
            any(
                item["rule_decision"]["container_id"] == subtitle.container_id
                for item in records
            )
        )

    def test_right_anchored_page_heading_can_use_proven_safe_left_space(self) -> None:
        facts = _facts_with_locked_signature_overlay()
        facts = replace(
            facts,
            text_objects=(replace(facts.text_objects[0], bbox=(450, 30, 550, 50)), *facts.text_objects[1:]),
        )
        template = build_multi_column_template(facts)
        assignment = {item.container_id: item.column_id for item in template.assignments}
        title = next(
            item for item in template.containers
            if assignment[item.container_id] == "span" and item.role == "heading"
        )
        plan, _, _ = build_best_multi_plan(
            facts=facts,
            template=template,
            translations=_bundle(template, {title.container_id: "Independent Auditor's Report"}),
            source_language="zh",
            target_language="en",
            font_file="C:/Windows/Fonts/msyh.ttc",
        )

        repaired, records = apply_deterministic_multi_layout_repairs(
            facts=facts,
            template=template,
            plan=plan,
        )

        before = next(item for item in plan.placements if item.container_id == title.container_id)
        after = next(item for item in repaired.placements if item.container_id == title.container_id)
        self.assertLess(after.output_bbox[0], before.output_bbox[0])
        self.assertEqual(before.output_bbox[2], after.output_bbox[2])
        self.assertEqual("safe_heading_left_whitespace_expand", after.horizontal_policy)
        self.assertTrue(any(item["rule_decision"]["selected_failure_class"] == "avoidable_right_anchored_heading_wrap_with_safe_left_space" for item in records))

    def test_long_horizontal_rules_are_detected_as_structural_anchors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "structural-rules.pdf"
            document = fitz.open()
            page = document.new_page(width=600, height=800)
            for y in (105, 132, 164):
                page.draw_line((55, y), (545, y), color=(0.1, 0.2, 0.1), width=1.2)
            document.save(source)
            document.close()

            anchors = probe_horizontal_structural_anchors(source)

        self.assertEqual(3, len(anchors))
        self.assertTrue(all(item.anchor_kind == "horizontal_rule" for item in anchors))

    def test_body_paragraphs_preserve_source_relative_line_rhythm(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "rhythmic-columns.pdf"
            _write_rhythmic_multi_column_pdf(source)
            facts = extract_page_facts(source, page_id="rhythmic-columns")
            template = build_multi_column_template(facts)
            body_replacements = {
                item.container_id: (
                    "Translated paragraph contains enough words to wrap across the column while "
                    "remaining one semantic paragraph for rhythm verification."
                )
                for item in template.containers
                if item.role == "body"
            }
            plan, _, _ = build_best_multi_plan(
                facts=facts,
                template=template,
                translations=_bundle(template, body_replacements),
                source_language="en",
                target_language="zh-CN",
                font_file="C:/Windows/Fonts/msyh.ttc",
            )

        assignment = {item.container_id: item.column_id for item in template.assignments}
        left = [
            item for item in plan.placements
            if assignment[item.container_id] == "column-1" and item.role == "body"
        ]
        self.assertGreaterEqual(len(left), 2)
        self.assertTrue(
            all(
                "semantic_source_rhythm" in item.vertical_policy
                for item in left[1:]
            )
        )
        self.assertTrue(all(item.target_gap > 0 for item in left[1:]))
        self.assertTrue(
            all(abs(item.target_gap - item.source_gap) > 0.01 for item in left[1:])
        )

    def test_actual_glyph_overlap_is_a_hard_rendered_spacing_failure(self) -> None:
        decision = evaluate_rendered_semantic_spacing(
            (
                {
                    "previous_container_id": "paragraph-a",
                    "next_container_id": "paragraph-b",
                    "source_transition_ratio": 2.0,
                    "candidate_transition_ratio": 0.5,
                    "candidate_visible_gap_pt": -4.0,
                    "candidate_visible_overlap_pt": 4.0,
                    "candidate_typographic_scale_pt": 10.0,
                },
            )
        )

        self.assertEqual("FAIL", decision["rule_verdict"])
        self.assertEqual("rendered_text_overlap", decision["selected_failure_class"])
        self.assertEqual("paragraph-b", decision["next_container_id"])

    def test_paired_rows_ignore_relative_spacing_but_never_glyph_overlap(self) -> None:
        relative_only = {
            "previous_container_id": "left-a",
            "next_container_id": "left-b",
            "source_transition_ratio": 1.0,
            "candidate_transition_ratio": 1.8,
            "candidate_visible_gap_pt": 4.0,
            "candidate_visible_overlap_pt": 0.0,
            "candidate_typographic_scale_pt": 8.0,
            "column_id": "column-1",
        }
        decision = evaluate_rendered_semantic_spacing(
            (relative_only,),
            ignore_relative_spacing_columns=("column-1", "column-2"),
        )
        self.assertEqual("PASS", decision["rule_verdict"])

        decision = evaluate_rendered_semantic_spacing(
            ({**relative_only, "candidate_visible_overlap_pt": 2.0},),
            ignore_relative_spacing_columns=("column-1", "column-2"),
        )
        self.assertEqual("rendered_text_overlap", decision["selected_failure_class"])

    def test_excessive_semantic_group_gap_is_a_hard_spacing_failure(self) -> None:
        decision = evaluate_rendered_semantic_spacing(
            (
                {
                    "previous_container_id": "heading-a",
                    "next_container_id": "heading-b",
                    "source_transition_ratio": 0.25,
                    "candidate_transition_ratio": 2.0,
                    "candidate_visible_gap_pt": 18.0,
                    "candidate_visible_overlap_pt": 0.0,
                    "candidate_typographic_scale_pt": 9.0,
                },
            )
        )

        self.assertEqual("FAIL", decision["rule_verdict"])
        self.assertEqual("semantic_paragraph_spacing_amplification", decision["selected_failure_class"])

    def test_tiny_visible_gap_is_not_compressed_into_glyph_overlap(self) -> None:
        decision = evaluate_rendered_semantic_spacing(
            (
                {
                    "previous_container_id": "paragraph-a",
                    "next_container_id": "paragraph-b",
                    "source_transition_ratio": 1.0,
                    "candidate_transition_ratio": 1.2586,
                    "candidate_line_step_pt": 10.4257,
                    "candidate_visible_gap_pt": 0.3801,
                    "candidate_visible_overlap_pt": 0.0,
                    "candidate_typographic_scale_pt": 9.66,
                    "column_id": "column-1",
                },
            )
        )

        self.assertEqual("PASS", decision["rule_verdict"])

    def test_small_source_gap_cannot_be_visually_doubled(self) -> None:
        decision = evaluate_rendered_semantic_spacing(
            (
                {
                    "previous_container_id": "heading-a",
                    "next_container_id": "heading-b",
                    "source_transition_ratio": 0.196,
                    "candidate_transition_ratio": 0.4492,
                    "candidate_line_step_pt": 7.92,
                    "candidate_visible_gap_pt": 3.5576,
                    "candidate_visible_overlap_pt": 0.0,
                    "candidate_typographic_scale_pt": 7.92,
                },
            )
        )

        self.assertEqual("FAIL", decision["rule_verdict"])
        self.assertEqual("semantic_paragraph_spacing_amplification", decision["selected_failure_class"])

    def test_rendered_spacing_repair_moves_only_the_later_owner_local_flow(self) -> None:
        facts = _facts_with_shared_cross_column_header()
        template = build_multi_column_template(facts)
        plan, _, _ = build_best_multi_plan(
            facts=facts,
            template=template,
            translations=_bundle(template, {}),
            source_language="en",
            target_language="zh-CN",
            font_file="C:/Windows/Fonts/msyh.ttc",
        )
        assignment = {item.container_id: item.column_id for item in template.assignments}
        span_ids = [
            item.container_id for item in template.containers
            if assignment[item.container_id] == "span"
        ]
        self.assertGreaterEqual(len(span_ids), 2)
        previous_id, next_id = span_ids[:2]
        plan = replace(
            plan,
            placements=tuple(
                replace(item, target_gap=4.0)
                if item.container_id == next_id
                else item
                for item in plan.placements
            ),
        )
        before = {item.container_id: item for item in plan.placements}

        repaired, application = apply_rendered_semantic_spacing_reflow(
            template=template,
            plan=plan,
            decision={
                "previous_container_id": previous_id,
                "next_container_id": next_id,
                "source_transition_ratio": 0.20,
                "candidate_transition_ratio": 0.45,
                "candidate_line_step_pt": 8.0,
                "selected_failure_class": "semantic_paragraph_spacing_amplification",
            },
        )

        after = {item.container_id: item for item in repaired.placements}
        self.assertEqual(before[previous_id].output_bbox, after[previous_id].output_bbox)
        self.assertAlmostEqual(
            before[next_id].output_bbox[1] - 2.0,
            after[next_id].output_bbox[1],
            places=3,
        )
        first_column_id = next(
            item.container_id for item in template.containers
            if assignment[item.container_id].startswith("column-")
        )
        self.assertEqual(before[first_column_id].output_bbox, after[first_column_id].output_bbox)
        self.assertEqual("applied", application["status"])

    def test_rendered_overlap_repair_uses_glyph_overlap_when_rhythm_ratios_match(self) -> None:
        facts = _facts_with_shared_cross_column_header()
        template = build_multi_column_template(facts)
        plan, _, _ = build_best_multi_plan(
            facts=facts,
            template=template,
            translations=_bundle(template, {}),
            source_language="en",
            target_language="zh-CN",
            font_file="C:/Windows/Fonts/msyh.ttc",
        )
        assignment = {item.container_id: item.column_id for item in template.assignments}
        span_ids = [
            item.container_id for item in template.containers
            if assignment[item.container_id] == "span"
        ]
        previous_id, next_id = span_ids[:2]
        before = next(item for item in plan.placements if item.container_id == next_id)

        repaired, application = apply_rendered_semantic_spacing_reflow(
            template=template,
            plan=plan,
            decision={
                "previous_container_id": previous_id,
                "next_container_id": next_id,
                "source_transition_ratio": 1.0,
                "candidate_transition_ratio": 1.0,
                "candidate_line_step_pt": 10.0,
                "candidate_visible_overlap_pt": 0.9,
                "candidate_typographic_scale_pt": 10.0,
                "selected_failure_class": "rendered_text_overlap",
            },
        )

        after = next(item for item in repaired.placements if item.container_id == next_id)
        self.assertAlmostEqual(before.output_bbox[1] + 1.4, after.output_bbox[1], places=3)
        self.assertAlmostEqual(1.4, application["applied_shift_pt"], places=3)

    def test_rendered_spacing_repair_can_move_past_zero_bbox_gap_when_glyph_gap_is_safe(self) -> None:
        facts = _facts_with_shared_cross_column_header()
        template = build_multi_column_template(facts)
        plan, _, _ = build_best_multi_plan(
            facts=facts,
            template=template,
            translations=_bundle(template, {}),
            source_language="en",
            target_language="zh-CN",
            font_file="C:/Windows/Fonts/msyh.ttc",
        )
        assignment = {item.container_id: item.column_id for item in template.assignments}
        span_ids = [
            item.container_id for item in template.containers
            if assignment[item.container_id] == "span"
        ]
        previous_id, next_id = span_ids[:2]
        plan = replace(
            plan,
            placements=tuple(
                replace(item, target_gap=0.0)
                if item.container_id == next_id
                else item
                for item in plan.placements
            ),
        )
        before = next(item for item in plan.placements if item.container_id == next_id)

        repaired, application = apply_rendered_semantic_spacing_reflow(
            template=template,
            plan=plan,
            decision={
                "previous_container_id": previous_id,
                "next_container_id": next_id,
                "source_transition_ratio": 1.9,
                "candidate_transition_ratio": 2.6,
                "candidate_line_step_pt": 7.2,
                "selected_failure_class": "semantic_paragraph_spacing_amplification",
            },
        )

        after = next(item for item in repaired.placements if item.container_id == next_id)
        self.assertLess(after.output_bbox[1], before.output_bbox[1])
        self.assertAlmostEqual(-5.04, application["applied_shift_pt"], places=2)

    def test_top_single_band_spacing_repair_does_not_move_late_single_band(self) -> None:
        facts = _facts_with_local_heading_columns(include_late_note=True)
        template = build_multi_column_template(facts)
        plan, _, _ = build_best_multi_plan(
            facts=facts,
            template=template,
            translations=_bundle(template, {}),
            source_language="en",
            target_language="zh-CN",
            font_file="C:/Windows/Fonts/msyh.ttc",
        )
        single_bands = [item for item in plan.flow_bands if item.mode == "single"]
        self.assertEqual(2, len(single_bands))
        previous_id, next_id = single_bands[0].container_ids[:2]
        late_id = single_bands[1].container_ids[0]
        before = {item.container_id: item for item in plan.placements}

        repaired, application = apply_rendered_semantic_spacing_reflow(
            template=template,
            plan=plan,
            decision={
                "previous_container_id": previous_id,
                "next_container_id": next_id,
                "source_transition_ratio": 0.2,
                "candidate_transition_ratio": 0.5,
                "candidate_line_step_pt": 8.0,
                "selected_failure_class": "semantic_paragraph_spacing_amplification",
            },
        )

        after = {item.container_id: item for item in repaired.placements}
        self.assertNotEqual(before[next_id].output_bbox, after[next_id].output_bbox)
        self.assertEqual(before[late_id].output_bbox, after[late_id].output_bbox)
        self.assertNotIn(late_id, application["affected_container_ids"])

    def test_safe_left_heading_expansion_renders_right_aligned(self) -> None:
        self.assertEqual(
            fitz.TEXT_ALIGN_RIGHT,
            _textbox_alignment("safe_heading_left_whitespace_expand"),
        )
        self.assertEqual(
            fitz.TEXT_ALIGN_LEFT,
            _textbox_alignment("column_width_invariant"),
        )
        self.assertEqual(
            fitz.TEXT_ALIGN_RIGHT,
            _textbox_alignment("locked_visual_overlay_safe_left_expand"),
        )

    def test_translatable_footer_text_is_included_but_numeric_page_marker_is_locked(self) -> None:
        facts = _facts_with_shared_cross_column_header()
        footer_objects = (
            TextObjectFact("footer-page-number", "67", (45, 765, 62, 785), "Helvetica-Bold", 12, 2301728, 40, 0, 0),
            TextObjectFact("footer-company", "示例控股有限公司", (95, 765, 165, 786), "SourceHanSans-Medium", 7.5, 2301728, 40, 1, 0),
            TextObjectFact("footer-report", "二零二五年年報", (175, 765, 197, 786), "SourceHanSans-Bold", 7.5, 5790043, 40, 1, 1),
        )
        facts = replace(
            facts,
            text_objects=facts.text_objects + footer_objects,
            native_text_object_count=facts.native_text_object_count + len(footer_objects),
        )

        template = build_multi_column_template(facts)

        assignment = {item.container_id: item.column_id for item in template.assignments}
        margin_containers = [
            item for item in template.containers if assignment[item.container_id] == "margin"
        ]
        translated_source_ids = {
            object_id for item in margin_containers for object_id in item.source_object_ids
        }
        self.assertEqual({"footer-company", "footer-report"}, translated_source_ids)
        self.assertNotIn("footer-page-number", translated_source_ids)

        replacements = {
            item.container_id: "Example Holdings Limited" if "footer-company" in item.source_object_ids else "Annual Report"
            for item in margin_containers
        }
        plan, _, findings = build_best_multi_plan(
            facts=facts,
            template=template,
            translations=_bundle(template, replacements),
            source_language="zh",
            target_language="en",
            font_file="C:/Windows/Fonts/msyh.ttc",
        )
        margin_placements = [item for item in plan.placements if assignment[item.container_id] == "margin"]
        self.assertTrue(any(item.horizontal_policy == "safe_margin_right_whitespace_expand" for item in margin_placements))
        self.assertFalse(any(item.severity == "HARD" for item in findings))
        self.assertEqual(2, len(margin_containers))

        plan, _, _ = build_best_multi_plan(
            facts=facts,
            template=template,
            translations=_bundle(
                template,
                {
                    margin_containers[0].container_id: "Example Holdings Limited",
                    margin_containers[1].container_id: "2025 Annual Report",
                },
            ),
            source_language="zh",
            target_language="en",
            font_file="C:/Windows/Fonts/msyh.ttc",
        )
        placement = {item.container_id: item for item in plan.placements}
        self.assertTrue(all(placement[item.container_id].fit for item in margin_containers))
        self.assertTrue(
            all(
                placement[item.container_id].output_bbox[0] == item.source_bbox[0]
                and placement[item.container_id].output_bbox[2] >= item.source_bbox[2]
                for item in margin_containers
            )
        )


def _bundle(template, replacements: dict[str, str]) -> PageTranslationBundle:
    return PageTranslationBundle(
        "request",
        template.page_id,
        "test",
        "test",
        tuple(TranslationResult(item.container_id, replacements.get(item.container_id, "目标段落内容")) for item in template.containers),
    )


def _write_multi_column_pdf(path: Path, column_count: int) -> None:
    document = fitz.open()
    page = document.new_page(width=600, height=800)
    page.insert_textbox(fitz.Rect(50, 45, 550, 78), "MULTI COLUMN DEVELOPMENT PAGE", fontname="helv", fontsize=14)
    if column_count == 2:
        columns = ((50, 285), (330, 565))
    else:
        columns = ((35, 185), (225, 375), (415, 565))
    for column_index, (left, right) in enumerate(columns):
        for row_index in range(3):
            top = 120 + row_index * 145
            text = (
                f"Column {column_index + 1} section {row_index + 1}. "
                "This paragraph contains enough words to provide stable column-start evidence and a real vertical reading flow. "
                "The next sentence remains inside the same source column."
            )
            page.insert_textbox(fitz.Rect(left, top, right, top + 110), text, fontname="helv", fontsize=9, lineheight=1.15)
    document.save(path)
    document.close()


def _write_rhythmic_multi_column_pdf(path: Path) -> None:
    document = fitz.open()
    page = document.new_page(width=600, height=800)
    page.insert_textbox(fitz.Rect(50, 45, 550, 75), "RHYTHMIC MULTI COLUMN PAGE", fontname="helv", fontsize=14)
    for column_index, (left, right) in enumerate(((50, 285), (330, 565))):
        page.insert_textbox(
            fitz.Rect(left, 105, right, 125),
            f"COLUMN {column_index + 1} HEADER",
            fontname="hebo",
            fontsize=10,
        )
        for paragraph_index, top in enumerate((150, 195, 240)):
            page.insert_textbox(
                fitz.Rect(left, top, right, top + 30),
                (
                    f"Source paragraph {paragraph_index + 1} has a complete first line.\n"
                    "Its second line remains in the same semantic paragraph."
                ),
                fontname="helv",
                fontsize=9,
                lineheight=1.15,
            )
    document.save(path)
    document.close()


def _facts_with_shared_cross_column_header() -> PageFacts:
    objects: list[TextObjectFact] = [
        TextObjectFact("page-title", "Development page", (50, 45, 210, 70), "Helvetica-Bold", 14, 2301728, 1, 0, 0),
        # 上下两行的字体框可以轻微相交，但视觉基线不同，不能互相阻止安全横向扩展。
        TextObjectFact("page-subtitle", "Reporting period", (50, 62, 180, 82), "Helvetica", 8, 2301728, 3, 0, 0),
        # 两个栏标题故意共享一个 PDF block_index，复现“提取块跨栏、视觉对象分栏”的事实。
        TextObjectFact("header-left", "Left topic", (50, 100, 125, 116), "Helvetica-Bold", 9.5, 2301728, 2, 0, 0),
        TextObjectFact("header-right", "How the topic was addressed", (330, 100, 520, 116), "Helvetica-Bold", 9.5, 2301728, 2, 1, 0),
    ]
    for column_index, (left, right) in enumerate(((50, 285), (330, 565))):
        for row_index in range(3):
            block_index = 10 + column_index * 10 + row_index
            objects.append(
                TextObjectFact(
                    f"body-{column_index}-{row_index}",
                    f"Column {column_index + 1} paragraph {row_index + 1} contains stable reading-flow evidence.",
                    (left, 140 + row_index * 145, right, 230 + row_index * 145),
                    "Helvetica",
                    9,
                    2301728,
                    block_index,
                    0,
                    0,
                )
            )
    return PageFacts(
        page_id="shared-header",
        source_pdf_sha256="0" * 64,
        width=600,
        height=800,
        native_text_object_count=len(objects),
        origin="synthetic-test",
        text_objects=tuple(objects),
    )


def _facts_with_local_heading_columns(*, include_late_note: bool = False) -> PageFacts:
    objects = [
        TextObjectFact("page-title", "LOCAL MULTI DEVELOPMENT", (50, 45, 300, 70), "Helvetica-Bold", 14, 2301728, 1, 0, 0),
        TextObjectFact(
            "wide-body",
            "This full width paragraph remains above the local two-column structure and must span both regions.",
            (50, 120, 550, 150),
            "Helvetica",
            9,
            2301728,
            2,
            0,
            0,
        ),
    ]
    for row_index, top in enumerate((400, 440, 480)):
        objects.extend(
            (
                TextObjectFact(
                    f"local-left-{row_index}",
                    f"Left styled entry {row_index + 1} with repeated local-column evidence",
                    (60, top, 260, top + 16),
                    "Helvetica-Oblique",
                    9,
                    2301728,
                    10 + row_index,
                    0,
                    0,
                ),
                TextObjectFact(
                    f"local-right-{row_index}",
                    f"Right styled entry {row_index + 1} with repeated local-column evidence",
                    (330, top, 550, top + 16),
                    "Helvetica-Oblique",
                    9,
                    2301728,
                    20 + row_index,
                    0,
                    0,
                ),
            )
        )
    if include_late_note:
        objects.append(
            TextObjectFact(
                "late-note",
                "This note follows the local two-column region and remains a page-width postlude.",
                (60, 700, 540, 720),
                "Helvetica",
                8,
                2301728,
                40,
                0,
                0,
            )
        )
    return PageFacts(
        page_id="local-multi",
        source_pdf_sha256="0" * 64,
        width=600,
        height=800,
        native_text_object_count=len(objects),
        origin="synthetic-test",
        text_objects=tuple(objects),
    )


def _facts_with_paired_rows() -> PageFacts:
    objects = [
        TextObjectFact("paired-title", "PAIRED DISCLOSURE LIST", (50, 45, 300, 70), "Helvetica-Bold", 14, 2301728, 1, 0, 0),
    ]
    for row_index, top in enumerate((180, 220, 275, 330, 385)):
        objects.extend(
            (
                TextObjectFact(f"paired-left-{row_index}", f"Standard {row_index + 1}", (60, top, 180, top + 12), "Helvetica", 9, 2301728, 10 + row_index * 3, 0, 0),
                TextObjectFact(f"paired-right-{row_index}", f"Disclosure requirement {row_index + 1}", (330, top, 540, top + 12), "Helvetica", 9, 2301728, 11 + row_index * 3, 0, 0),
            )
        )
        if row_index in {1, 3}:
            objects.extend(
                (
                    TextObjectFact(f"paired-left-cont-{row_index}", "continued identifier", (69, top + 13, 190, top + 25), "Helvetica", 9, 2301728, 12 + row_index * 3, 0, 0),
                    TextObjectFact(f"paired-right-cont-{row_index}", "continued description", (339, top + 13, 520, top + 25), "Helvetica", 9, 2301728, 13 + row_index * 3, 0, 0),
                )
            )
    return PageFacts(
        page_id="paired-rows",
        source_pdf_sha256="0" * 64,
        width=600,
        height=800,
        native_text_object_count=len(objects),
        origin="synthetic-test",
        text_objects=tuple(objects),
    )


def _facts_with_label_content_columns() -> PageFacts:
    objects = [
        TextObjectFact("page-title", "LOCAL STRUCTURE", (55, 55, 500, 85), "Helvetica-Bold", 14, 2301728, 1, 0, 0),
        TextObjectFact("section", "COMMITMENT TO ENVIRONMENT", (85, 140, 320, 155), "Helvetica-Bold", 10, 2301728, 2, 0, 0),
        TextObjectFact("subsection", "Climate response", (85, 170, 235, 185), "Helvetica-Bold", 9, 2301728, 3, 0, 0),
        TextObjectFact("wide-intro", "A page-wide introduction explains the response approach before the local label and content columns begin.", (140, 225, 540, 335), "Helvetica", 9, 2301728, 4, 0, 0),
        TextObjectFact("local-label", "Response and incident management", (140, 352, 205, 392), "Helvetica", 9, 2301728, 5, 0, 0),
    ]
    for index, top in enumerate((352, 465, 550)):
        objects.append(
            TextObjectFact(
                f"right-content-{index}",
                f"Right column action {index + 1} contains enough explanatory content to dominate text volume without erasing the small label column.",
                (283, top, 542, top + 82),
                "Helvetica",
                9,
                2301728,
                10 + index,
                0,
                0,
            )
        )
    return PageFacts(
        page_id="label-content",
        source_pdf_sha256="0" * 64,
        width=600,
        height=800,
        native_text_object_count=len(objects),
        origin="synthetic-test",
        text_objects=tuple(objects),
    )


def _facts_with_locked_signature_overlay() -> PageFacts:
    objects = [
        TextObjectFact("page-title", "CHAIRMAN MESSAGE", (450, 45, 550, 70), "Helvetica-Bold", 14, 2301728, 1, 0, 0),
    ]
    for column_index, (left, right) in enumerate(((55, 285), (310, 540))):
        for row_index, top in enumerate((100, 145, 190)):
            objects.append(
                TextObjectFact(
                    f"body-{column_index}-{row_index}",
                    f"Column {column_index + 1} paragraph {row_index + 1} provides stable multi-column source evidence.",
                    (left, top, right, top + 22),
                    "Helvetica",
                    9,
                    2301728,
                    10 + column_index * 10 + row_index,
                    0,
                    0,
                )
            )
    objects.extend(
        (
            TextObjectFact("signature-label", "董事长", (506, 350, 540, 365), "SourceHanSans-Regular", 10, 2301728, 30, 0, 0),
            TextObjectFact("signature-date", "2026年3月31日", (464, 375, 540, 390), "SourceHanSans-Regular", 10, 2301728, 31, 0, 0),
        )
    )
    return PageFacts(
        page_id="locked-signature",
        source_pdf_sha256="0" * 64,
        width=600,
        height=800,
        native_text_object_count=len(objects),
        origin="synthetic-test",
        text_objects=tuple(objects),
        image_objects=(ImageObjectFact("signature-image", (430, 270, 525, 340), 300, 220, "1" * 64),),
    )


def _facts_with_internal_column_visual() -> PageFacts:
    objects = [
        TextObjectFact("page-title", "MULTI COLUMN PAGE", (55, 50, 250, 72), "Helvetica-Bold", 14, 2301728, 1, 0, 0),
        TextObjectFact("left-body-1", "Left column paragraph provides stable body evidence for the first column.", (55, 125, 285, 175), "Helvetica", 9, 2301728, 10, 0, 0),
        TextObjectFact("left-body-2", "Left column continuation keeps an independent vertical reading flow.", (55, 205, 285, 255), "Helvetica", 9, 2301728, 11, 0, 0),
        TextObjectFact("left-body-3", "Left column lower paragraph confirms that the page has ordinary trailing whitespace.", (55, 285, 285, 335), "Helvetica", 9, 2301728, 12, 0, 0),
        TextObjectFact("right-pre-visual", "Right column paragraph appears before a local visual object.", (310, 125, 540, 190), "Helvetica", 9, 2301728, 20, 0, 0),
        TextObjectFact("right-post-heading", "Later Section", (310, 365, 390, 380), "Helvetica-Bold", 11, 2301728, 21, 0, 0),
        TextObjectFact("right-post-body-1", "Ordinary body text below the image belongs to the same column flow.", (310, 390, 540, 412), "Helvetica", 9, 2301728, 22, 0, 0),
        TextObjectFact("right-post-body-2", "The following line continues naturally and may move down when translation grows.", (310, 414, 540, 436), "Helvetica", 9, 2301728, 23, 0, 0),
        TextObjectFact("right-late-body", "A later paragraph provides evidence that usable page space continues below.", (310, 475, 540, 520), "Helvetica", 9, 2301728, 24, 0, 0),
        TextObjectFact("footer", "Example Annual Report", (450, 770, 550, 782), "Helvetica", 7, 2301728, 40, 0, 0),
    ]
    return PageFacts(
        page_id="internal-column-visual",
        source_pdf_sha256="0" * 64,
        width=600,
        height=800,
        native_text_object_count=len(objects),
        origin="synthetic-test",
        text_objects=tuple(objects),
        image_objects=(
            ImageObjectFact("page-background", (0, 0, 600, 800), 1200, 1600, "2" * 64),
            ImageObjectFact("column-visual", (310, 210, 540, 340), 600, 340, "3" * 64),
        ),
    )


def _facts_with_tiled_page_background() -> PageFacts:
    objects = [
        TextObjectFact("page-title", "TILED BACKGROUND PAGE", (50, 60, 280, 82), "Helvetica-Bold", 14, 2301728, 1, 0, 0),
    ]
    for column_index, (left, right) in enumerate(((55, 280), (320, 545))):
        for row_index, top in enumerate((450, 520, 590)):
            objects.append(
                TextObjectFact(
                    f"tile-flow-{column_index}-{row_index}",
                    f"Column {column_index + 1} ordinary paragraph {row_index + 1} supplies stable flow evidence.",
                    (left, top, right, top + 42),
                    "Helvetica",
                    9,
                    2301728,
                    10 + column_index * 10 + row_index,
                    0,
                    0,
                )
            )
    return PageFacts(
        page_id="tiled-page-background",
        source_pdf_sha256="0" * 64,
        width=600,
        height=800,
        native_text_object_count=len(objects),
        origin="synthetic-test",
        text_objects=tuple(objects),
        image_objects=(
            ImageObjectFact("background-tile-1", (0, 0, 300, 400), 900, 1200, "4" * 64),
            ImageObjectFact("background-tile-2", (300, 0, 600, 400), 900, 1200, "5" * 64),
            ImageObjectFact("background-tile-3", (0, 400, 300, 800), 900, 1200, "6" * 64),
            ImageObjectFact("background-tile-4", (300, 400, 600, 800), 900, 1200, "7" * 64),
        ),
    )


def _facts_with_short_aligned_cells_before_long_evidence() -> PageFacts:
    objects = [
        TextObjectFact("page-title", "ALIGNED CELL PAGE", (50, 55, 260, 78), "Helvetica-Bold", 14, 2301728, 1, 0, 0),
    ]
    for row_index, top in enumerate((300, 330, 360)):
        objects.extend(
            (
                TextObjectFact(
                    f"early-left-{row_index}",
                    f"Left aligned row {row_index + 1} has enough text to establish the first column.",
                    (60, top, 275, top + 12),
                    "Helvetica",
                    9,
                    2301728,
                    10 + row_index * 2,
                    0,
                    0,
                ),
                TextObjectFact(
                    f"early-right-{row_index}",
                    f"Right {row_index + 1}",
                    (325, top, 400, top + 12),
                    "Helvetica",
                    9,
                    2301728,
                    11 + row_index * 2,
                    0,
                    0,
                ),
            )
        )
    objects.extend(
        (
            TextObjectFact("late-left", "Later left evidence remains in the established first column.", (60, 390, 275, 402), "Helvetica", 9, 2301728, 30, 0, 0),
            TextObjectFact("late-right", "Later right evidence is deliberately long enough to seed the second geometric cluster.", (325, 390, 550, 402), "Helvetica", 9, 2301728, 31, 0, 0),
        )
    )
    return PageFacts(
        page_id="short-aligned-cells",
        source_pdf_sha256="0" * 64,
        width=600,
        height=800,
        native_text_object_count=len(objects),
        origin="synthetic-test",
        text_objects=tuple(objects),
    )


def _paired_template_with_numeric_visual_continuation() -> MultiColumnTemplate:
    columns = (
        ColumnBand("column-1", 0, 50, 280, 100, 320),
        ColumnBand("column-2", 1, 320, 550, 100, 320),
    )
    containers = (
        TextContainer("left-0", ("left-0",), "Left row zero.", 0, "body", (50, 100, 180, 110), (50, 100), 10, 0, "regular", None),
        TextContainer("left-1", ("left-1",), "Left row one.", 1, "body", (50, 150, 180, 160), (50, 150), 10, 0, "regular", None),
        TextContainer("left-wrap", ("left-wrap",), "A wrapped cell continues", 2, "body", (50, 200, 278, 210), (50, 200), 10, 0, "regular", None),
        TextContainer("left-cont", ("left-cont",), "inside the same cell", 3, "body", (60, 214, 205, 224), (60, 214), 10, 0, "regular", None),
        TextContainer("left-next", ("left-next",), "Aligned next row.", 4, "body", (50, 228, 278, 238), (50, 228), 10, 0, "regular", None),
        TextContainer("left-last", ("left-last",), "Left final row.", 5, "body", (50, 270, 180, 280), (50, 270), 10, 0, "regular", None),
        TextContainer("right-0", ("right-0",), "Right row zero.", 6, "body", (320, 100, 460, 110), (320, 100), 10, 0, "regular", None),
        TextContainer("right-1", ("right-1",), "Right row one.", 7, "body", (320, 150, 460, 160), (320, 150), 10, 0, "regular", None),
        TextContainer("right-wrap", ("right-wrap",), "the plan for 2021–", 8, "body", (320, 200, 548, 210), (320, 200), 10, 0, "regular", None),
        TextContainer("right-numeric-cont", ("right-numeric-cont",), "2025) approved.", 9, "list", (320, 214, 525, 224), (320, 214), 10, 0, "regular", None),
        TextContainer("right-next", ("right-next",), "Aligned paired next row.", 10, "body", (320, 228, 548, 238), (320, 228), 10, 0, "regular", None),
        TextContainer("right-last", ("right-last",), "Right final row.", 11, "body", (320, 270, 460, 280), (320, 270), 10, 0, "regular", None),
    )
    assignments = tuple(
        ColumnAssignment(item.container_id, "column-1" if item.container_id.startswith("left-") else "column-2", index)
        for index, item in enumerate(containers)
    )
    return MultiColumnTemplate(
        "paired-numeric-continuation",
        "body.flow_text.multi",
        600,
        800,
        columns,
        containers,
        assignments,
        (),
    )


def _repeated_paired_multi_band_template() -> MultiColumnTemplate:
    columns = (
        ColumnBand("column-1", 0, 50, 280, 140, 750),
        ColumnBand("column-2", 1, 320, 550, 140, 750),
    )
    containers = [
        TextContainer("top-span", ("top-span",), "Page-wide introduction.", 0, "heading", (50, 50, 550, 70), (50, 50), 10, 0, "regular", None),
        TextContainer("first-left-1", ("first-left-1",), "First left row.", 1, "body", (50, 140, 180, 152), (50, 140), 9, 0, "regular", None),
        TextContainer("first-right-1", ("first-right-1",), "First right row.", 2, "body", (320, 140, 550, 152), (320, 140), 9, 0, "regular", None),
        TextContainer("first-left-2", ("first-left-2",), "Second left row.", 3, "body", (50, 180, 180, 192), (50, 180), 9, 0, "regular", None),
        TextContainer("first-right-2", ("first-right-2",), "Second right row.", 4, "body", (320, 180, 550, 192), (320, 180), 9, 0, "regular", None),
        TextContainer("middle-span", ("middle-span",), "Page-wide explanation between paired sections.", 5, "body", (50, 260, 550, 280), (50, 260), 10, 0, "regular", None),
        TextContainer("second-left-1", ("second-left-1",), "Third left row.", 6, "body", (50, 360, 180, 372), (50, 360), 9, 0, "regular", None),
        TextContainer("second-right-1", ("second-right-1",), "Third right row.", 7, "body", (320, 360, 550, 372), (320, 360), 9, 0, "regular", None),
        TextContainer("second-left-2", ("second-left-2",), "Fourth left row.", 8, "body", (50, 400, 180, 412), (50, 400), 9, 0, "regular", None),
        TextContainer("second-right-2", ("second-right-2",), "Fourth right row.", 9, "body", (320, 400, 550, 412), (320, 400), 9, 0, "regular", None),
        TextContainer("tail-span", ("tail-span",), "Page-wide closing note.", 10, "body", (50, 520, 550, 540), (50, 520), 9, 0, "regular", None),
    ]
    owners = {
        "top-span": "span",
        "middle-span": "span",
        "tail-span": "span",
    }
    assignments = tuple(
        ColumnAssignment(
            item.container_id,
            owners.get(
                item.container_id,
                "column-1" if "left" in item.container_id else "column-2",
            ),
            index,
        )
        for index, item in enumerate(containers)
    )
    return MultiColumnTemplate(
        "repeated-paired-bands",
        "body.flow_text.multi",
        600,
        800,
        columns,
        tuple(containers),
        assignments,
        (),
    )


def _repeated_independent_multi_band_template() -> MultiColumnTemplate:
    paired = _repeated_paired_multi_band_template()
    removed = {"first-left-2", "second-left-2"}
    containers = tuple(
        item for item in paired.containers if item.container_id not in removed
    )
    assignments = tuple(
        item for item in paired.assignments if item.container_id not in removed
    )
    return replace(
        paired,
        page_id="repeated-independent-bands",
        containers=containers,
        assignments=assignments,
    )


def _rectangles_overlap(first: tuple[float, float, float, float], second: tuple[float, float, float, float]) -> bool:
    return min(first[2], second[2]) > max(first[0], second[0]) and min(first[3], second[3]) > max(first[1], second[1])


if __name__ == "__main__":
    unittest.main()
