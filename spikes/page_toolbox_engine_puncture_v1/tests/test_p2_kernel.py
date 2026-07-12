from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

import fitz
from PIL import Image

from page_toolbox_puncture.contracts import ContainerWrite, PagePatch
from page_toolbox_puncture.sample_snapshot import sha256_file
from shared_pdf_kernel.constraints import preflight_patch
from shared_pdf_kernel.facts import extract_page_facts
from shared_pdf_kernel.fonts import embedded_font_resources, missing_embedded_resources, probe_font
from shared_pdf_kernel.passthrough import passthrough_pdf
from shared_pdf_kernel.patch import apply_page_patch
from shared_pdf_kernel.probe import probe_tools
from shared_pdf_kernel.render import render_contact_sheet, render_page
from shared_pdf_kernel.repair import RepairController
from shared_pdf_kernel.workspace import WorkspaceBoundaryError, require_under


FONT_CJK = Path("C:/Windows/Fonts/simhei.ttf")
FONT_LATIN = Path("C:/Windows/Fonts/arial.ttf")


def create_synthetic_pdf(path: Path) -> None:
    document = fitz.open()
    page = document.new_page(width=300, height=300)
    page.draw_rect(fitz.Rect(180, 20, 270, 80), color=(0.1, 0.2, 0.8), fill=(0.8, 0.9, 1.0), width=1)
    image = Image.new("RGB", (20, 20), (220, 80, 30))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    page.insert_image(fitz.Rect(200, 180, 250, 230), stream=buffer.getvalue())
    page.insert_text((30, 50), "Hello", fontsize=12, fontname="helv", color=(0, 0, 0))
    page.insert_text((30, 110), "Other text", fontsize=12, fontname="helv", color=(0, 0, 0))
    document.save(path)
    document.close()


class P2KernelTests(unittest.TestCase):
    def _source(self, root: Path) -> tuple[Path, object]:
        source = root / "source.pdf"
        create_synthetic_pdf(source)
        return source, extract_page_facts(source, page_id="synthetic")

    def _write(self, facts, object_id: str, text: str = "你好", *, width: float = 100, height: float = 30, font: Path = FONT_CJK) -> ContainerWrite:
        source = next(item for item in facts.text_objects if item.object_id == object_id)
        x0, y0, _x1, _y1 = source.bbox
        bbox = (x0, y0, x0 + width, y0 + height)
        return ContainerWrite(object_id, text, bbox, bbox, str(font), "p2font", 10.0, 1.2)

    def test_tool_probe_reports_required_environment(self) -> None:
        result = probe_tools()
        self.assertTrue(result["required_ok"])
        self.assertTrue(result["packages"]["fitz"])

    def test_facts_are_stable_and_include_text_image_drawing_font(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source, first = self._source(Path(tmp))
            second = extract_page_facts(source, page_id="synthetic")
            self.assertEqual(first, second)
            self.assertEqual(first.width, 300)
            self.assertGreaterEqual(len(first.text_objects), 2)
            self.assertEqual(len(first.image_objects), 1)
            self.assertGreaterEqual(len(first.drawing_objects), 1)
            self.assertTrue(any(item.font_name for item in first.text_objects))

    def test_render_page_and_contact_sheet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, _facts = self._source(root)
            rendered = render_page(source, root / "page.png")
            sheet = render_contact_sheet(source, source, root / "sheet.png")
            self.assertGreater(rendered["width"], 0)
            self.assertTrue((root / "page.png").exists())
            self.assertGreater(sheet["width"], rendered["width"])

    def test_valid_patch_preserves_locked_objects_and_embeds_font(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, facts = self._source(root)
            target = next(item for item in facts.text_objects if item.text == "Hello")
            write = self._write(facts, target.object_id)
            patch = PagePatch("synthetic", "mechanical.validation", (write,), facts.source_pdf_sha256, 0)
            result = apply_page_patch(workspace_root=root, source_pdf=source, candidate_pdf=root / "candidate.pdf", facts=facts, patch=patch)
            self.assertEqual(result.status, "APPLIED", [item.code for item in result.findings])
            self.assertEqual(result.source_locked_objects_sha256, result.candidate_locked_objects_sha256)
            self.assertLessEqual(result.outside_allowed_changed_pixel_ratio or 0, 0.00001)
            self.assertIn("p2font", embedded_font_resources(root / "candidate.pdf"))
            self.assertEqual(sha256_file(source), facts.source_pdf_sha256)

    def test_out_of_bounds_overlap_and_overflow_are_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, facts = self._source(root)
            hello = next(item for item in facts.text_objects if item.text == "Hello")
            other = next(item for item in facts.text_objects if item.text == "Other text")

            x0, y0, _x1, _y1 = hello.bbox
            moved = ContainerWrite(hello.object_id, "你好", (-1.0, y0, 40.0, y0 + 20), (-1.0, y0, 40.0, y0 + 20), str(FONT_CJK), "p2font", 10.0)
            moved_patch = PagePatch("synthetic", "mechanical.validation", (moved,), facts.source_pdf_sha256)
            moved_codes = {item.code for item in preflight_patch(facts, moved_patch)}
            self.assertIn("FIXED_ORIGIN_MOVED", moved_codes)
            self.assertIn("PAGE_BOUNDS_EXCEEDED", moved_codes)

            first = self._write(facts, hello.object_id, width=120, height=90)
            second = self._write(facts, other.object_id, width=120, height=30)
            overlap_patch = PagePatch("synthetic", "mechanical.validation", (first, second), facts.source_pdf_sha256)
            overlap_codes = {item.code for item in preflight_patch(facts, overlap_patch)}
            self.assertTrue({"WRITE_OVERLAP", "NON_TARGET_TEXT_OVERLAP"} & overlap_codes)

            tiny = self._write(facts, hello.object_id, text="very long text " * 30, width=8, height=5, font=FONT_LATIN)
            overflow_patch = PagePatch("synthetic", "mechanical.validation", (tiny,), facts.source_pdf_sha256)
            result = apply_page_patch(workspace_root=root, source_pdf=source, candidate_pdf=root / "overflow.pdf", facts=facts, patch=overflow_patch)
            self.assertEqual(result.status, "REJECTED")
            self.assertIn("TEXT_FIT_OVERFLOW", {item.code for item in result.findings})
            self.assertFalse((root / "overflow.pdf").exists())

    def test_missing_glyph_is_detected(self) -> None:
        probe = probe_font(FONT_LATIN, "中文")
        self.assertTrue(probe.exists)
        self.assertFalse(probe.covers_text)
        self.assertTrue(probe.missing_codepoints)

    def test_missing_embedded_font_resource_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source, _facts = self._source(Path(tmp))
            self.assertEqual(missing_embedded_resources(source, {"p2font"}), ("p2font",))

    def test_passthrough_is_byte_equivalent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, _facts = self._source(root)
            result = passthrough_pdf(workspace_root=root, source_pdf=source, output_pdf=root / "passthrough.pdf")
            self.assertTrue(result["equivalent"])
            self.assertEqual(result["source_sha256"], result["output_sha256"])

    def test_workspace_boundary_rejects_escape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            root.mkdir()
            with self.assertRaises(WorkspaceBoundaryError):
                require_under(root.parent / "escape.pdf", root)

    def test_failed_repair_rolls_back_and_stops_after_no_improvement(self) -> None:
        controller = RepairController("baseline.pdf", max_rounds=3, max_no_improvement=2)
        first = controller.consider(
            trial_candidate_ref="trial-1.pdf",
            target_score_before=10,
            target_score_after=4,
            hard_findings_before=set(),
            hard_findings_after={"new_overlap"},
            locked_objects_unchanged=True,
        )
        self.assertFalse(first.accepted)
        self.assertEqual(first.selected_candidate_ref, "baseline.pdf")
        second = controller.consider(
            trial_candidate_ref="trial-2.pdf",
            target_score_before=10,
            target_score_after=10,
            hard_findings_before=set(),
            hard_findings_after=set(),
            locked_objects_unchanged=True,
        )
        self.assertEqual(second.outcome, "NO_IMPROVEMENT")
        self.assertEqual(second.selected_candidate_ref, "baseline.pdf")

    def test_kernel_source_has_no_leaf_routing_tokens(self) -> None:
        root = Path(__file__).resolve().parents[1] / "src" / "shared_pdf_kernel"
        source = "\n".join(path.read_text(encoding="utf-8") for path in root.glob("*.py"))
        for token in ("body.flow_text", "body.table", "body.composite", "anchored_blocks_chart", "flow_text_diagram"):
            self.assertNotIn(token, source)


if __name__ == "__main__":
    unittest.main()
