from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import shutil

import fitz

from page_toolbox_puncture.contracts import PageFacts, PageTranslationBundle, PageTranslationRequest, TextObjectFact, TranslationResult, TranslationUnit
from page_toolbox_puncture.sample_snapshot import sha256_file
from page_toolbox_puncture.translation import FixedTranslationProvider, ProviderError
from shared_pdf_kernel.facts import extract_page_facts
from toolbox_cadence.models import SampleSplit, ToolboxSampleRecord
from toolboxes.body.flow_text.single.tools.engine import run_page
from toolboxes.body.flow_text.single.tools.layout_planner import plan_layout
from toolboxes.body.flow_text.single.tools.models import SingleColumnTemplate, TextContainer
from toolboxes.body.flow_text.single.tools.p4_engine import _canonicalize_with_targeted_retry, run_p4_page
from toolboxes.body.flow_text.single.tools.p4_layout_planner import build_best_p4_plan
from toolboxes.body.flow_text.single.tools.p4_models import P4LayoutPlan, P4Placement
from toolboxes.body.flow_text.single.tools.orchestrator.repair_loop import apply_deterministic_layout_repairs
from toolboxes.body.flow_text.single.tools.repairs.section_spacing_reflow import apply_section_spacing_reflow
from toolboxes.body.flow_text.single.tools.validators.semantic_paragraph_spacing_rule import evaluate_semantic_paragraph_spacing
from toolboxes.body.flow_text.single.tools.validators.inline_graphic_control_alignment_rule import evaluate_inline_graphic_control_alignment
from toolboxes.body.flow_text.single.tools.template_builder import _split_block, build_page_template
from toolboxes.body.flow_text.single.tools.run_package import initialize_batch_package, publish_case_outputs, write_artifact_index


FONT_FILE = "C:/Windows/Fonts/simhei.ttf"


class P3BodyFlowTextSingleTests(unittest.TestCase):
    def test_template_groups_native_blocks_and_orders_top_to_bottom(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = self._source_pdf(Path(temporary))
            template = build_page_template(extract_page_facts(source, page_id="synthetic"))
            self.assertGreaterEqual(len(template.containers), 3)
            self.assertEqual(sorted(item.reading_order for item in template.containers), list(range(len(template.containers))))
            self.assertEqual(sorted(item.source_bbox[1] for item in template.containers), [item.source_bbox[1] for item in template.containers])

    def test_layout_preserves_every_left_top_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = self._source_pdf(Path(temporary))
            template = build_page_template(extract_page_facts(source, page_id="synthetic"))
            bundle = self._bundle(template, "简短译文")
            plan, findings = plan_layout(template, bundle, font_file=FONT_FILE)
            self.assertFalse(findings)
            self.assertEqual(
                [item.anchor for item in template.containers],
                [item.output_bbox[:2] for item in plan.placements],
            )

    def test_layout_reports_overflow_instead_of_moving_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = self._source_pdf(Path(temporary))
            template = build_page_template(extract_page_facts(source, page_id="synthetic"))
            bundle = self._bundle(template, "过长译文" * 4000)
            plan, findings = plan_layout(template, bundle, font_file=FONT_FILE)
            self.assertTrue(findings)
            self.assertTrue(any(item.code == "LAYOUT_TEXT_OVERFLOW" for item in findings))
            self.assertEqual(template.containers[0].anchor, plan.placements[0].output_bbox[:2])

    def test_fixed_translation_runs_through_candidate_and_quality_decision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source_pdf(root)
            source_hash = sha256_file(source)
            template = build_page_template(extract_page_facts(source, page_id="synthetic"))
            translations = {
                item.container_id: item.source_text if item.role == "margin" else f"译文{item.reading_order}"
                for item in template.containers
            }
            result = run_page(
                source_pdf=source,
                page_id="synthetic",
                run_dir=root / "run",
                provider=FixedTranslationProvider(translations),
                font_file=FONT_FILE,
            )
            self.assertEqual("PASS", result.process_verdict)
            self.assertEqual("PASS", result.product_verdict)
            self.assertTrue(Path(result.candidate_pdf or "").is_file())
            self.assertEqual(source_hash, sha256_file(source))
            self.assertEqual(source_hash, sha256_file(root / "run" / "input" / "source.pdf"))
            for relative in (
                "contracts/page_run_contract.json",
                "docs/README.md",
                "input/page_facts.json",
                "input/page_template.json",
                "input/translation_request.json",
                "output/translation_bundle.json",
                "output/layout_plan.json",
                "output/candidate.pdf",
                "previews/comparison.png",
                "reports/quality_decision.json",
                "reports/run_result.json",
            ):
                self.assertTrue((root / "run" / relative).is_file(), relative)

    def test_translation_contract_failure_is_attributed_to_provider(self) -> None:
        class BrokenProvider:
            provider_name = "broken"
            model_name = "broken"

            def translate(self, request):
                raise ProviderError("BROKEN_TRANSLATION")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = run_page(
                source_pdf=self._source_pdf(root),
                page_id="synthetic",
                run_dir=root / "run",
                provider=BrokenProvider(),
                font_file=FONT_FILE,
            )
            self.assertEqual("FAIL", result.process_verdict)
            self.assertEqual("translation_provider", result.failure_owner)

    def test_suspicious_duplicate_translation_retries_conflicting_containers_separately(self) -> None:
        units = (
            TranslationUnit("first", "甲" * 130, 0),
            TranslationUnit("second", "乙" * 130, 1),
        )
        request = PageTranslationRequest("request", "page", "zh", "en", units)
        template = SingleColumnTemplate(
            "page",
            "body.flow_text.single",
            420,
            595,
            (
                TextContainer("first", ("a",), units[0].source_text, 0, "body", (40, 80, 360, 120), (40, 80), 10, 0),
                TextContainer("second", ("b",), units[1].source_text, 1, "body", (40, 130, 360, 170), (40, 130), 10, 0),
            ),
        )
        duplicate = "duplicated translation " * 20
        initial = PageTranslationBundle(
            "request",
            "page",
            "qwen",
            "model",
            (TranslationResult("first", duplicate), TranslationResult("second", duplicate)),
            response_sha256="a" * 64,
        )

        class TargetedRetryProvider:
            provider_name = "qwen"
            model_name = "model"
            requested_ids = []

            def translate(self, retry_request):
                target_id = retry_request.units[0].container_id
                self.requested_ids.append([target_id])
                return PageTranslationBundle(
                    retry_request.request_id,
                    retry_request.page_id,
                    self.provider_name,
                    self.model_name,
                    (TranslationResult(target_id, f"independent corrected translation for {target_id} " * 10),),
                    response_sha256="b" * 64,
                )

        provider = TargetedRetryProvider()
        repaired, retries = _canonicalize_with_targeted_retry(
            request=request,
            translation=initial,
            template=template,
            provider=provider,
        )
        self.assertEqual([["first"], ["second"]], provider.requested_ids)
        self.assertEqual(2, len(retries))
        self.assertNotEqual(repaired.translations[0].translated_text, repaired.translations[1].translated_text)

    def test_toolbox_code_has_no_sample_identity_branch(self) -> None:
        tools = Path(__file__).resolve().parents[1] / "toolboxes" / "body" / "flow_text" / "single" / "tools"
        source = "\n".join(path.read_text(encoding="utf-8") for path in tools.glob("*.py"))
        for forbidden in ("S2P0043", "S2P0044", "S2P0103", "S2P0106"):
            self.assertNotIn(forbidden, source)

    def test_template_distinguishes_heading_body_and_margin(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            template = build_page_template(extract_page_facts(self._source_pdf(Path(temporary)), page_id="synthetic"))
            roles = {item.role for item in template.containers}
            self.assertIn("heading", roles)
            self.assertIn("body", roles)
            self.assertIn("margin", roles)

    def test_overlaid_duplicate_text_is_translated_once_with_topmost_style(self) -> None:
        lower = TextObjectFact("lower", "REPORT TITLE", (220.0, 24.0, 360.0, 44.0), "Helvetica-Bold", 18.0, 0xFFFFFF, 0, 0, 0)
        upper = TextObjectFact("upper", "REPORT TITLE", (220.0, 24.0, 360.0, 44.0), "Helvetica-Bold", 18.0, 0x00B185, 0, 1, 0)
        facts = PageFacts("overlay", "0" * 64, 420.0, 595.0, 2, "test", text_objects=(lower, upper))

        container = build_page_template(facts).containers[0]

        self.assertEqual("REPORT TITLE", container.source_text)
        self.assertEqual(("lower", "upper"), container.source_object_ids)
        self.assertEqual(0x00B185, container.color_srgb)

    def test_native_block_is_split_on_paragraph_gaps_and_new_bullets(self) -> None:
        paragraph_lines = [
            self._text_fact(0, "HEADING", 20.0, 30.0),
            self._text_fact(2, "Paragraph line one", 45.0, 55.0),
            self._text_fact(3, "paragraph line two", 55.5, 65.5),
            self._text_fact(5, "Signature", 100.0, 110.0),
        ]
        self.assertEqual(3, len(_split_block(paragraph_lines)))
        bullet_lines = [
            self._text_fact(0, "•", 20.0, 30.0),
            self._text_fact(1, "First item", 21.0, 31.0),
            self._text_fact(2, "continued", 31.5, 41.5),
            self._text_fact(3, "•", 41.6, 51.6),
            self._text_fact(4, "Second item", 42.6, 52.6),
        ]
        self.assertEqual(2, len(_split_block(bullet_lines)))

    def test_list_marker_is_preserved_outside_translation_container(self) -> None:
        marker = self._text_fact(0, "•", 20.0, 30.0)
        body = TextObjectFact("body", "First item", (62.0, 21.0, 300.0, 31.0), "TimesNewRomanPSMT", 10.0, 0, 0, 1, 0)
        facts = PageFacts("list", "0" * 64, 420.0, 595.0, 2, "test", text_objects=(marker, body))
        template = build_page_template(facts)
        self.assertEqual(1, len(template.containers))
        self.assertEqual("list", template.containers[0].role)
        self.assertEqual("•", template.containers[0].preserved_prefix)
        self.assertEqual(("body",), template.containers[0].source_object_ids)
        self.assertEqual("First item", template.containers[0].source_text)

    def test_batch_package_carries_source_candidate_contracts_and_index(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            toolbox = root / "toolbox"
            source = self._source_pdf(root)
            sample = toolbox / "samples" / "development" / "sample.pdf"
            sample.parent.mkdir(parents=True)
            shutil.copy2(source, sample)
            (toolbox / "samples" / "manifest.jsonl").write_text("{}\n", encoding="utf-8")
            prompt = toolbox / "prompts" / "page_translation.zh-CN.md"
            prompt.parent.mkdir(parents=True)
            prompt.write_text("prompt\n", encoding="utf-8")
            dispatch = toolbox / "docs" / "单列正文工具箱调度流程.md"
            dispatch.parent.mkdir(parents=True)
            dispatch.write_text("dispatch\n", encoding="utf-8")
            record = ToolboxSampleRecord("sample", "body.flow_text.single", SampleSplit.DEVELOPMENT, "samples/development/sample.pdf", sha256_file(sample), "doc", 1)
            run = root / "run"
            initialize_batch_package(run_root=run, toolbox_root=toolbox, run_id="run", records=(record,), prompt_path=prompt, model="qwen")
            case = run / "cases" / "sample"
            (case / "output").mkdir(parents=True)
            (case / "previews").mkdir()
            shutil.copy2(sample, case / "output" / "candidate.pdf")
            (case / "previews" / "comparison.png").write_bytes(b"png")
            publish_case_outputs(run_root=run, page_id="sample", case_root=case)
            write_artifact_index(run)
            for relative in (
                "contracts/batch_run_contract.json",
                "docs/单列正文工具箱调度流程.md",
                "input/source_pdfs/sample_source.pdf",
                "output/sample_candidate.pdf",
                "previews/sample_comparison.png",
                "reports/artifact_index.json",
            ):
                self.assertTrue((run / relative).is_file(), relative)

    def test_p4_normal_flow_keeps_source_bbox_width_and_reflows_vertically(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = self._flow_source_pdf(Path(temporary))
            facts = extract_page_facts(source, page_id="flow")
            template = build_page_template(facts)
            bundle = self._bundle(template, "这是一个完整但更紧凑的中文段落。")
            plan, attempts = build_best_p4_plan(
                facts=facts,
                template=template,
                translations=bundle,
                source_language="en",
                target_language="zh-CN",
                font_file=FONT_FILE,
            )
            self.assertIsNotNone(plan)
            self.assertTrue(attempts[-1].fit)
            main = [item for item in plan.placements if item.role != "margin"]
            for item in main:
                self.assertEqual(item.source_bbox[0], item.output_bbox[0])
                if item.horizontal_policy == "normal_flow_width_invariant":
                    self.assertEqual(item.source_bbox[2], item.output_bbox[2])
            self.assertLessEqual(main[-1].output_bbox[1], main[-1].source_bbox[1])

    def test_p4_extreme_translation_exhausts_profiles_without_horizontal_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = self._flow_source_pdf(Path(temporary))
            facts = extract_page_facts(source, page_id="flow")
            template = build_page_template(facts)
            bundle = self._bundle(template, "very long translation " * 5000)
            plan, attempts = build_best_p4_plan(
                facts=facts,
                template=template,
                translations=bundle,
                source_language="zh",
                target_language="en",
                font_file=FONT_FILE,
            )
            self.assertIsNotNone(plan)
            self.assertFalse(attempts[-1].fit)
            self.assertTrue(any(finding.code == "P4_VERTICAL_PAGE_ESCAPE" for finding in attempts[-1].findings))
            self.assertTrue(all(item.output_bbox[2] <= plan.column_right + 0.01 for item in plan.placements if item.role != "margin"))

    def test_p4_fixed_provider_generates_complete_page_package(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._flow_source_pdf(root)
            template = build_page_template(extract_page_facts(source, page_id="flow"))
            translations = {item.container_id: item.source_text if item.role == "margin" else f"译文{item.reading_order}" for item in template.containers}
            result = run_p4_page(
                source_pdf=source,
                page_id="flow",
                run_dir=root / "p4",
                provider=FixedTranslationProvider(translations),
                font_file=FONT_FILE,
                source_language="en",
                target_language="zh-CN",
            )
            self.assertEqual("PASS", result.process_verdict)
            self.assertEqual("PASS", result.product_verdict)
            for relative in ("input/source.pdf", "output/candidate.pdf", "output/layout_plan.json", "reports/repair_trace.json", "reports/quality_decision.json", "previews/comparison.png"):
                self.assertTrue((root / "p4" / relative).is_file(), relative)

    def test_section_spacing_reflow_closes_one_transition_without_horizontal_change(self) -> None:
        placements = (
            P4Placement("a", "甲", "body", (40, 100, 300, 120), (40, 100, 300, 120), "normal_flow_width_invariant", 10, 10, 1.2, "source_anchor_cap", 6, 6, 0, "regular", True),
            P4Placement("b", "乙", "list", (40, 126, 300, 146), (40, 160, 300, 180), "normal_flow_width_invariant", 10, 10, 1.2, "source_anchor_cap", 6, 6, 0, "bold", True),
            P4Placement("c", "丙", "body", (70, 152, 300, 172), (70, 186, 300, 206), "normal_flow_width_invariant", 10, 10, 1.2, "source_anchor_cap", 6, 6, 0, "regular", True),
        )
        plan = P4LayoutPlan("p", "body.flow_text.single", "en", "zh-CN", "test", FONT_FILE, "font", 40, 300, 40, 560, placements)
        repaired, evidence = apply_section_spacing_reflow(plan, previous_container_id="a", next_container_id="b", target_gap_pt=6)
        self.assertEqual("applied", evidence["status"])
        self.assertAlmostEqual(6, repaired.placements[1].output_bbox[1] - repaired.placements[0].output_bbox[3])
        self.assertEqual((40, 300), (repaired.placements[1].output_bbox[0], repaired.placements[1].output_bbox[2]))
        self.assertEqual(152, repaired.placements[2].output_bbox[1])

    def test_semantic_paragraph_spacing_rule_is_source_relative_and_idempotent(self) -> None:
        failed = evaluate_semantic_paragraph_spacing(
            previous_container_id="a",
            next_container_id="b",
            previous_role="body",
            next_role="body",
            source_visible_gap_pt=3.25,
            candidate_visible_gap_pt=5.16,
            previous_candidate_bottom_inset_pt=1.91,
            next_candidate_top_inset_pt=0.0,
            source_typographic_scale_pt=8.0,
            candidate_typographic_scale_pt=8.0,
        )
        self.assertEqual("FAIL", failed["rule_verdict"])
        self.assertEqual("section_spacing_regression", failed["selected_failure_class"])
        self.assertAlmostEqual(1.34, failed["target_plan_gap_pt"], places=2)

        passed = evaluate_semantic_paragraph_spacing(
            previous_container_id="a",
            next_container_id="b",
            previous_role="body",
            next_role="body",
            source_visible_gap_pt=3.25,
            candidate_visible_gap_pt=3.25,
            previous_candidate_bottom_inset_pt=1.91,
            next_candidate_top_inset_pt=0.0,
            source_typographic_scale_pt=8.0,
            candidate_typographic_scale_pt=8.0,
        )
        self.assertEqual("PASS", passed["rule_verdict"])
        self.assertIsNone(passed["repair_atom"])

        not_applicable = evaluate_semantic_paragraph_spacing(
            previous_container_id="a",
            next_container_id="b",
            previous_role="list",
            next_role="body",
            source_visible_gap_pt=3.25,
            candidate_visible_gap_pt=8.0,
            previous_candidate_bottom_inset_pt=0.0,
            next_candidate_top_inset_pt=0.0,
            source_typographic_scale_pt=8.0,
            candidate_typographic_scale_pt=8.0,
        )
        self.assertEqual("NOT_APPLICABLE", not_applicable["rule_verdict"])

    def test_deterministic_layout_repair_loop_closes_only_page_gap_outlier(self) -> None:
        placements = []
        cursor = 40.0
        source_gap = 8.0
        for index, gap in enumerate((8.0, 8.0, 8.0, 48.0, 8.0, 8.0)):
            y0 = cursor if index == 0 else cursor + gap
            placements.append(
                P4Placement(
                    f"c{index}", "正文", "body" if index % 2 else "list",
                    (40, y0, 300, y0 + 12), (40, y0, 300, y0 + 12),
                    "normal_flow_width_invariant", 10, 10, 1.2,
                    "source_anchor_cap", 0 if index == 0 else source_gap,
                    0 if index == 0 else source_gap, 0, "regular", True,
                )
            )
            cursor = y0 + 12
        plan = P4LayoutPlan("p", "body.flow_text.single", "en", "zh-CN", "test", FONT_FILE, "font", 40, 300, 40, 560, tuple(placements))
        repaired, records = apply_deterministic_layout_repairs(plan)
        self.assertEqual(1, len(records))
        self.assertEqual("section_spacing_regression", records[0]["repair_patch"]["selected_failure_class"])
        self.assertAlmostEqual(source_gap, repaired.placements[3].output_bbox[1] - repaired.placements[2].output_bbox[3])
        rerun, second_records = apply_deterministic_layout_repairs(repaired)
        self.assertEqual(repaired, rerun)
        self.assertEqual((), second_records)

    def test_inline_graphic_control_alignment_rule_is_idempotent(self) -> None:
        probe = {
            "container_id": "runtime-container",
            "source_position_hit_count": 2,
            "target_position_hit_count": 0,
            "control_count": 2,
            "normalized_container_shift": 4.0,
        }
        failed = evaluate_inline_graphic_control_alignment(probe)
        self.assertEqual("icon_label_misalignment", failed["selected_failure_class"])

        probe["source_position_hit_count"] = 0
        probe["target_position_hit_count"] = 2
        passed = evaluate_inline_graphic_control_alignment(probe)
        self.assertEqual("PASS", passed["rule_verdict"])
        self.assertIsNone(passed["repair_atom"])

    @staticmethod
    def _bundle(template, translated_text: str) -> PageTranslationBundle:
        return PageTranslationBundle(
            "request",
            template.page_id,
            "fixed",
            "fixed",
            tuple(
                TranslationResult(item.container_id, item.source_text if item.role == "margin" else translated_text)
                for item in template.containers
            ),
        )

    @staticmethod
    def _source_pdf(root: Path) -> Path:
        path = root / "source.pdf"
        with fitz.open() as document:
            page = document.new_page(width=420, height=595)
            page.insert_text((42, 28), "REPORT 2026", fontsize=8, fontname="helv", color=(0.3, 0.3, 0.3))
            page.insert_text((42, 80), "MANAGEMENT REPORT", fontsize=16, fontname="hebo", color=(0.0, 0.2, 0.7))
            page.insert_textbox(
                fitz.Rect(42, 120, 378, 210),
                "The Group maintained stable operations during the year. Revenue quality improved and risk remained controlled.",
                fontsize=10,
                fontname="helv",
                color=(0.1, 0.1, 0.1),
            )
            page.draw_rect(fitz.Rect(36, 112, 384, 218), color=(0.8, 0.8, 0.8), width=0.5)
            page.insert_text((205, 570), "12", fontsize=8, fontname="helv")
            document.save(path)
        return path

    @staticmethod
    def _flow_source_pdf(root: Path) -> Path:
        path = root / "flow-source.pdf"
        with fitz.open() as document:
            page = document.new_page(width=420, height=595)
            page.insert_text((42, 30), "REPORT 2026", fontsize=8, fontname="helv")
            page.insert_text((42, 80), "OPERATING REVIEW", fontsize=15, fontname="hebo")
            page.insert_textbox(fitz.Rect(42, 120, 378, 175), "The Group maintained stable operations during the year and continued to improve service quality.", fontsize=10, fontname="helv")
            page.insert_textbox(fitz.Rect(42, 230, 378, 285), "Management reviewed the market environment and implemented prudent risk controls throughout the reporting period.", fontsize=10, fontname="helv")
            page.insert_text((205, 570), "12", fontsize=8, fontname="helv")
            document.save(path)
        return path

    @staticmethod
    def _text_fact(line_index: int, text: str, y0: float, y1: float) -> TextObjectFact:
        return TextObjectFact(
            f"line-{line_index}",
            text,
            (42.0, y0, 300.0, y1),
            "TimesNewRomanPSMT",
            10.0,
            0,
            0,
            line_index,
            0,
        )


if __name__ == "__main__":
    unittest.main()
