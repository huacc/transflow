"""验证 TM2 single 独立核心、精确擦除和底图保护合同。"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from statistics import median

import pymupdf
import pytest

import scripts.toolbox_leaf_migration_single as single_runner
from scripts.run_toolbox_leaf_migration import (
    MigrationContractError,
    _forbidden_production_dependencies,
)
from scripts.toolbox_leaf_migration_single import (
    _align_spike_body_units,
    _commit_incremental_page_output,
    _normalized,
)
from transflow.adapters.ai.fixed import FixedTranslationAdapter
from transflow.application.contracts import ProcessedPage
from transflow.application.document_coordinator import DocumentCoordinator
from transflow.application.toolbox_page_coordinator import (
    ToolboxPageCoordinator,
    ToolboxPageWork,
)
from transflow.domain.common import json_ready
from transflow.domain.jobs import DocumentRunRequest
from transflow.domain.toolbox import PagePatch
from transflow.pdf_kernel import ControlledFontRegistry, PageFactsExtractor, PagePatchInterpreter
from transflow.toolboxes.contracts import normalized_page_outcome
from transflow.toolboxes.leaves import SingleFlowTextToolbox
from transflow.toolboxes.leaves.body_flow_text_single.judge import judge_placements
from transflow.toolboxes.leaves.body_flow_text_single.layout import plan_placements
from transflow.toolboxes.leaves.body_flow_text_single.models import SingleTextContainer
from transflow.toolboxes.leaves.body_flow_text_single.template import build_containers
from transflow.toolboxes.leaves.body_flow_text_single.toolbox import (
    _normalize_translated_text,
)
from transflow.toolboxes.leaves.policy import load_p8_toolbox_policy

REPO_ROOT = Path(__file__).resolve().parent.parent
FONT_MANIFEST = REPO_ROOT / "resources/manifests/font_manifest.json"
POLICY_PATH = REPO_ROOT / "resources/manifests/p8_toolbox_policy.json"
FONT_ID = "noto-sans-cjk-sc-regular"


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_pdf(path: Path) -> Path:
    """生成文字位于整页矢量底图上的真实 single 测试页。"""

    with pymupdf.open() as document:
        page = document.new_page(width=420, height=600)
        page.draw_rect(
            pymupdf.Rect(0, 0, 420, 600),
            color=(0.7, 0.8, 0.7),
            fill=(0.95, 0.98, 0.95),
        )
        page.insert_text((40, 30), "ANNUAL REPORT HEADER", fontsize=8)
        page.insert_textbox(
            pymupdf.Rect(55, 115, 360, 180),
            "Operational overview for 2026.\nThe business remained resilient.",
            fontsize=11,
            lineheight=1.33,
            color=(0.8, 0, 0),
        )
        page.insert_text((205, 575), "1", fontsize=8)
        document.save(path)
    return path


def _source_pdf_with_semantic_footer(path: Path) -> Path:
    """生成页脚标签与纯页码同属一个原生 block 的真实 single 页面。"""

    with pymupdf.open() as document:
        page = document.new_page(width=420, height=600)
        page.insert_textbox(
            pymupdf.Rect(55, 115, 360, 180),
            "Operational overview for 2026.\nThe business remained resilient.",
            fontsize=11,
            lineheight=1.33,
        )
        writer = pymupdf.TextWriter(page.rect)
        font = pymupdf.Font("helv")
        writer.append((40, 575), "Corporate Governance Report", font=font, fontsize=8)
        writer.append((365, 575), "99", font=font, fontsize=8)
        writer.write_text(page)
        document.save(path)
    return path


def _source_pdf_with_boundary_heading(path: Path) -> Path:
    """生成与 12% 正文上边界相交、但中心仍在边界外的章节标题。"""

    with pymupdf.open() as document:
        page = document.new_page(width=420, height=600)
        page.insert_textbox(
            pymupdf.Rect(55, 64, 360, 90),
            "Material Accounting Policies (continued)",
            fontsize=11,
        )
        page.insert_textbox(
            pymupdf.Rect(55, 115, 360, 180),
            "Operational overview for 2026.",
            fontsize=11,
        )
        page.insert_text((205, 575), "1", fontsize=8)
        document.save(path)
    return path


def _request(path: Path) -> DocumentRunRequest:
    return DocumentRunRequest(
        source_pdf_path=str(path.resolve()),
        source_hash=_sha256_file(path),
        source_language="en",
        target_language="zh-CN",
        config_snapshot_hash="a" * 64,
        job_id="job-tm2-test",
        run_id="tm2-test",
    )


def test_tm2_single_uses_block_semantics_and_span_redaction(tmp_path: Path) -> None:
    """一个段落只形成一个语义 unit，但擦除目标精确覆盖其全部 span。"""

    source = _source_pdf(tmp_path / "single-underlay.pdf")
    page = DocumentCoordinator(PageFactsExtractor()).enumerate_pages(_request(source))[0]
    fonts = ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT)
    toolbox = SingleFlowTextToolbox(
        load_p8_toolbox_policy(POLICY_PATH),
        fonts.resolve(FONT_ID).path,
    )
    template = toolbox.prepare(page.context, page.facts)
    batch = toolbox.build_translation_request(template)
    assert batch is not None and len(batch.units) == 2
    assert len(template.object_ids) == len(batch.units)
    body_unit = next(unit for unit in batch.units if "Operational overview" in unit.source_text)

    result = ToolboxPageCoordinator(
        FixedTranslationAdapter(
            {
                unit.unit_id: (
                    "年度报告页眉"
                    if "HEADER" in unit.source_text
                    else "2026 年运营概览，业务保持韧性。"
                )
                for unit in batch.units
            }
        )
    ).execute(ToolboxPageWork(page.context, page.facts, toolbox))
    assert result.patch is not None and len(result.patch.operations) == 2
    operation = next(
        item for item in result.patch.operations if item.region_id == body_unit.region_id
    )
    assert operation.preserve_drawing_overlap is True
    assert operation.redaction_rects
    assert len(operation.redaction_rects) == len(operation.target_object_ids)
    assert operation.color_srgb == 0xCC0000
    assert operation.line_height is not None and operation.line_height >= 1.25
    assert PagePatch.from_dict(json_ready(result.patch)) == result.patch


def test_tm2_single_uses_source_line_rhythm_in_rendered_pdf(tmp_path: Path) -> None:
    """译文的实际行距必须跟随源页节奏，不能只以“塞得下”为通过条件。"""

    source = _source_pdf(tmp_path / "single-line-rhythm.pdf")
    page = DocumentCoordinator(PageFactsExtractor()).enumerate_pages(_request(source))[0]
    fonts = ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT)
    toolbox = SingleFlowTextToolbox(
        load_p8_toolbox_policy(POLICY_PATH),
        fonts.resolve(FONT_ID).path,
    )
    template = toolbox.prepare(page.context, page.facts)
    batch = toolbox.build_translation_request(template)
    assert batch is not None and len(batch.units) == 2
    body_unit = next(unit for unit in batch.units if "Operational overview" in unit.source_text)
    translated = "2026 年运营概览显示业务保持韧性，并持续改善治理与执行效率。" * 3
    result = ToolboxPageCoordinator(
        FixedTranslationAdapter(
            {
                unit.unit_id: "年度报告页眉" if "HEADER" in unit.source_text else translated
                for unit in batch.units
            }
        )
    ).execute(ToolboxPageWork(page.context, page.facts, toolbox))
    assert result.patch is not None
    operation = next(
        item for item in result.patch.operations if item.region_id == body_unit.region_id
    )
    assert operation.line_height is not None
    assert 1.30 <= operation.line_height <= 1.35

    target = tmp_path / "single-line-rhythm-output.pdf"
    with pymupdf.open(source) as document:
        application = PagePatchInterpreter(fonts).apply(
            document,
            page.context,
            page.facts,
            result.patch,
            "body.flow_text.single",
        )
        assert application.fits
        document.save(target)
    with pymupdf.open(target) as document:
        blocks = document[0].get_text("dict")["blocks"]
    translated_lines = [
        line
        for block in blocks
        if "lines" in block
        for line in block["lines"]
        if any("\u4e00" <= char <= "\u9fff" for span in line["spans"] for char in span["text"])
    ]
    assert len(translated_lines) >= 2
    line_tops = [float(line["bbox"][1]) for line in translated_lines]
    font_sizes = [max(float(span["size"]) for span in line["spans"]) for line in translated_lines]
    observed = [
        (second_top - first_top) / first_size
        for first_top, second_top, first_size in zip(
            line_tops[:-1],
            line_tops[1:],
            font_sizes[:-1],
            strict=True,
        )
        if second_top > first_top
    ]
    assert observed and median(observed) >= 1.25


def test_tm2_single_reflows_later_body_blocks_and_keeps_margin_fixed(
    tmp_path: Path,
) -> None:
    """A growing translation pushes later body blocks instead of overflowing a source slot."""

    source = _source_pdf(tmp_path / "single-natural-reflow.pdf")
    page = DocumentCoordinator(PageFactsExtractor()).enumerate_pages(_request(source))[0]
    policy = load_p8_toolbox_policy(POLICY_PATH)
    font = ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT).resolve(FONT_ID).path
    containers = (
        SingleTextContainer(
            "first",
            "a" * 64,
            ("a" * 64,),
            ((55.0, 100.0, 360.0, 112.0),),
            "First paragraph.",
            0,
            "body",
            (55.0, 100.0, 360.0, 112.0),
            (55.0, 100.0),
            11.0,
            0,
            1.30,
        ),
        SingleTextContainer(
            "second",
            "b" * 64,
            ("b" * 64,),
            ((55.0, 125.0, 360.0, 145.0),),
            "Second paragraph.",
            1,
            "body",
            (55.0, 125.0, 360.0, 145.0),
            (55.0, 125.0),
            11.0,
            0,
            1.30,
        ),
        SingleTextContainer(
            "footer",
            "c" * 64,
            ("c" * 64,),
            ((40.0, 565.0, 150.0, 575.0),),
            "Annual Report",
            2,
            "margin",
            (40.0, 565.0, 150.0, 575.0),
            (40.0, 565.0),
            8.0,
            0,
            1.30,
            preserved_page_numbers=("1",),
        ),
    )
    translations = {
        "first": "第一段译文需要自然扩展并推动后续段落。" * 12,
        "second": "第二段译文保持在第一段之后。",
        "footer": "年度报告",
    }

    placements = plan_placements(page.facts, containers, translations, policy, font)

    assert all(item.fit for item in placements)
    assert placements[1].output_bbox[1] > containers[1].anchor[1]
    assert placements[1].output_bbox[1] >= placements[0].output_bbox[3]
    assert placements[2].output_bbox[1] == containers[2].anchor[1]
    assert placements[2].output_bbox[2] > containers[2].source_bbox[2]
    assert all(item.line_height >= 1.25 for item in placements)
    assert not {
        finding.code
        for finding in judge_placements("natural-reflow", containers, placements)
    } & {"ANCHOR_CHANGED", "TEXT_LAYOUT_OVERFLOW"}


def test_tm2_single_normalizes_visual_newlines_but_preserves_list_items() -> None:
    """Visual PDF line breaks do not force extra target lines; semantic list breaks remain."""

    assert _normalize_translated_text("2.\n重要会计政策（续）") == "2. 重要会计政策（续）"
    assert _normalize_translated_text("(i) 第一项\n(ii) 第二项") == (
        "(i) 第一项\n(ii) 第二项"
    )


def test_tm2_single_translates_semantic_footer_and_preserves_page_number(
    tmp_path: Path,
) -> None:
    """组合页脚只翻译语义标签，纯页码必须保留原生文字和原位置。"""

    source = _source_pdf_with_semantic_footer(tmp_path / "single-footer.pdf")
    page = DocumentCoordinator(PageFactsExtractor()).enumerate_pages(_request(source))[0]
    fonts = ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT)
    toolbox = SingleFlowTextToolbox(
        load_p8_toolbox_policy(POLICY_PATH),
        fonts.resolve(FONT_ID).path,
    )
    template = toolbox.prepare(page.context, page.facts)
    batch = toolbox.build_translation_request(template)
    assert batch is not None
    footer_unit = next(
        unit for unit in batch.units if "Corporate Governance Report" in unit.source_text
    )
    translations = {
        unit.unit_id: (
            "公司治理报告 99"
            if unit.unit_id == footer_unit.unit_id
            else "2026 年运营概览，业务保持韧性。"
        )
        for unit in batch.units
    }
    result = ToolboxPageCoordinator(FixedTranslationAdapter(translations)).execute(
        ToolboxPageWork(page.context, page.facts, toolbox)
    )
    assert result.patch is not None
    footer_operation = next(
        operation
        for operation in result.patch.operations
        if operation.region_id == footer_unit.region_id
    )
    source_text_by_id = {item.object_id: item.text for item in page.facts.text_spans}
    assert "99" not in {
        source_text_by_id[object_id] for object_id in footer_operation.target_object_ids
    }
    assert footer_operation.replacement_text == "公司治理报告"

    with pymupdf.open(source) as document:
        source_page_number = document[0].search_for("99")[0]
    target = tmp_path / "single-footer-output.pdf"
    with pymupdf.open(source) as document:
        application = PagePatchInterpreter(fonts).apply(
            document,
            page.context,
            page.facts,
            result.patch,
            "body.flow_text.single",
        )
        assert application.fits
        document.save(target)
    with pymupdf.open(target) as document:
        output_text = "".join(document[0].get_text("text").split())
        target_page_number = document[0].search_for("99")[0]
    assert "CorporateGovernanceReport" not in output_text
    assert _normalized("公司治理报告") in _normalized(output_text)
    assert tuple(round(value, 3) for value in source_page_number) == tuple(
        round(value, 3) for value in target_page_number
    )


def test_tm2_single_claims_text_intersecting_body_boundary(tmp_path: Path) -> None:
    """与正文边界相交的章节标题不能因中心点误差变成 unresolved。"""

    source = _source_pdf_with_boundary_heading(tmp_path / "single-boundary-heading.pdf")
    page = DocumentCoordinator(PageFactsExtractor()).enumerate_pages(_request(source))[0]
    fonts = ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT)
    toolbox = SingleFlowTextToolbox(
        load_p8_toolbox_policy(POLICY_PATH),
        fonts.resolve(FONT_ID).path,
    )
    batch = toolbox.build_translation_request(toolbox.prepare(page.context, page.facts))
    assert batch is not None and len(batch.units) == 2
    heading_unit = next(
        unit for unit in batch.units if "Material Accounting Policies" in unit.source_text
    )
    translations = {
        unit.unit_id: (
            "重大会计政策（续）"
            if unit.unit_id == heading_unit.unit_id
            else "2026 年运营概览，业务保持韧性。"
        )
        for unit in batch.units
    }
    result = ToolboxPageCoordinator(FixedTranslationAdapter(translations)).execute(
        ToolboxPageWork(page.context, page.facts, toolbox)
    )
    assert result.patch is not None

    target = tmp_path / "single-boundary-heading-output.pdf"
    with pymupdf.open(source) as document:
        PagePatchInterpreter(fonts).apply(
            document,
            page.context,
            page.facts,
            result.patch,
            "body.flow_text.single",
        )
        document.save(target)
    with pymupdf.open(target) as document:
        output_text = _normalized(document[0].get_text("text"))
    assert _normalized("重大会计政策") in output_text
    assert _normalized("Material Accounting Policies") not in output_text


def test_tm2_commits_page_input_process_and_output_at_page_terminal_state(
    tmp_path: Path,
) -> None:
    """单页一旦完成就必须拥有独立输入、过程快照、输出 PDF 和预览。"""

    source = _source_pdf(tmp_path / "single-incremental.pdf")
    page = DocumentCoordinator(PageFactsExtractor()).enumerate_pages(_request(source))[0]
    fonts = ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT)
    toolbox = SingleFlowTextToolbox(
        load_p8_toolbox_policy(POLICY_PATH),
        fonts.resolve(FONT_ID).path,
    )
    template = toolbox.prepare(page.context, page.facts)
    batch = toolbox.build_translation_request(template)
    assert batch is not None
    execution = ToolboxPageCoordinator(
        FixedTranslationAdapter(
            {
                unit.unit_id: (
                    "年度报告页眉"
                    if "HEADER" in unit.source_text
                    else "2026 年运营概览，业务保持韧性。"
                )
                for unit in batch.units
            }
        )
    ).execute(ToolboxPageWork(page.context, page.facts, toolbox))
    assert execution.patch is not None
    processed = ProcessedPage(
        page_no=1,
        route="body.flow_text.single",
        outcome=execution.outcome,
        patch=execution.patch,
        preview=None,
        unit_ids=execution.ordered_unit_ids,
        translated_unit_ids=execution.ordered_unit_ids,
        application=None,
    )
    run_root = tmp_path / "run"
    record = _commit_incremental_page_output(
        run_root=run_root,
        source=source,
        page=page,
        processed=processed,
        interpreter=PagePatchInterpreter(fonts),
    )
    assert record["page_no"] == 1
    page_root = run_root / "pages/p0001"
    assert (page_root / "input/source.pdf").is_file()
    assert (page_root / "process/completed.json").is_file()
    assert (page_root / "output/transflow.pdf").is_file()
    assert (page_root / "output/transflow.png").is_file()
    with pymupdf.open(page_root / "output/transflow.pdf") as document:
        assert _normalized("运营概览") in _normalized(document[0].get_text("text"))


def test_tm2_rejects_any_natural_target_page_passthrough() -> None:
    """自然命中当前叶的页面只要有一页未完整翻译，正式轮就必须失败。"""

    failed = ProcessedPage(
        page_no=150,
        route="body.flow_text.single",
        outcome=normalized_page_outcome(
            150,
            accepted=False,
            translated=False,
            finding_codes=("ROUTE_CAPABILITY_MISMATCH",),
            passthrough=True,
        ),
        patch=None,
        preview=None,
        unit_ids=("unit-150",),
        translated_unit_ids=(),
        application=None,
    )
    with pytest.raises(MigrationContractError) as caught:
        single_runner._assert_all_target_pages_deliverable((failed,))
    assert caught.value.code == "TM2_TARGET_PAGE_TRANSLATION_INCOMPLETE"
    assert caught.value.detail == "count=1 pages=150"


def test_tm2_spike_comparison_allows_only_explicit_semantic_footer_difference(
    tmp_path: Path,
) -> None:
    """旧 Spike 可少一个被其锁定的页脚，但共同正文仍须逐项严格等价。"""

    source = _source_pdf_with_semantic_footer(tmp_path / "single-spike-footer.pdf")
    page = DocumentCoordinator(PageFactsExtractor()).enumerate_pages(_request(source))[0]
    policy = load_p8_toolbox_policy(POLICY_PATH)
    fonts = ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT)
    toolbox = SingleFlowTextToolbox(policy, fonts.resolve(FONT_ID).path)
    batch = toolbox.build_translation_request(toolbox.prepare(page.context, page.facts))
    assert batch is not None
    containers = build_containers(page.facts, policy)
    spike_source_texts = tuple(
        item.source_text for item in containers if item.role != "margin"
    )
    body_units, allowed = _align_spike_body_units(
        spike_source_texts,
        containers,
        batch,
    )
    assert len(body_units) == len(batch.units) - 1
    assert [item["code"] for item in allowed] == [
        "TRANSFLOW_SEMANTIC_FOOTER_TRANSLATED_SPIKE_P4_EXCLUDED"
    ]

    margin_as_body = tuple(
        replace(item, role="body") if item.role == "margin" else item
        for item in containers
    )
    with pytest.raises(MigrationContractError, match="spike=1 transflow=2"):
        _align_spike_body_units(spike_source_texts, margin_as_body, batch)


def test_tm2_interpreter_preserves_vector_underlay_and_materializes_chinese(
    tmp_path: Path,
) -> None:
    """精确擦除允许文字覆盖矢量底图，但矢量内容本身必须保持。"""

    source = _source_pdf(tmp_path / "single-render.pdf")
    request = _request(source)
    page = DocumentCoordinator(PageFactsExtractor()).enumerate_pages(request)[0]
    fonts = ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT)
    toolbox = SingleFlowTextToolbox(
        load_p8_toolbox_policy(POLICY_PATH),
        fonts.resolve(FONT_ID).path,
    )
    template = toolbox.prepare(page.context, page.facts)
    batch = toolbox.build_translation_request(template)
    assert batch is not None
    result = ToolboxPageCoordinator(
        FixedTranslationAdapter(
            {
                unit.unit_id: (
                    "年度报告页眉"
                    if "HEADER" in unit.source_text
                    else "2026 年运营概览，业务保持韧性。"
                )
                for unit in batch.units
            }
        )
    ).execute(ToolboxPageWork(page.context, page.facts, toolbox))
    assert result.patch is not None

    target = tmp_path / "candidate.pdf"
    with pymupdf.open(source) as document:
        application = PagePatchInterpreter(fonts).apply(
            document,
            page.context,
            page.facts,
            result.patch,
            "body.flow_text.single",
        )
        assert application.fits
        document.save(target)
    after = PageFactsExtractor().extract_page(target, _sha256_file(target), 1)
    assert tuple(item.content_hash for item in after.drawing_objects) == tuple(
        item.content_hash for item in page.facts.drawing_objects
    )
    with pymupdf.open(target) as document:
        text = document[0].get_text("text")
    assert "运营概览" in text
    assert "Operational overview" not in text


def test_tm2_production_package_has_no_spike_test_or_run_dependency() -> None:
    """生产 src/transflow 不得导入 Spike、测试或历史运行目录。"""

    assert _forbidden_production_dependencies() == ()


def test_tm2_spike_text_review_normalizes_cjk_compatibility_glyphs() -> None:
    """Noto ToUnicode 的兼容汉字不得被误判为译文未渲染。"""

    assert _normalized("战略规划与持续发力") == _normalized("战略规划与持续发力")


def test_tm2_single_rejects_overlapping_block_to_span_ownership(tmp_path: Path) -> None:
    """表格式重叠 block 不得以重复 span owner 进入翻译。"""

    source = _source_pdf(tmp_path / "overlapping-blocks.pdf")
    page = DocumentCoordinator(PageFactsExtractor()).enumerate_pages(_request(source))[0]
    fonts = ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT)
    toolbox = SingleFlowTextToolbox(
        load_p8_toolbox_policy(POLICY_PATH),
        fonts.resolve(FONT_ID).path,
    )
    base_template = toolbox.prepare(page.context, page.facts)
    assert len(base_template.object_ids) == 2
    body_object_id = next(
        object_id
        for object_id in base_template.object_ids
        if "Operational overview"
        in next(item.text for item in page.facts.objects if item.object_id == object_id)
    )
    block_index = next(
        index
        for index, item in enumerate(page.facts.objects)
        if item.object_id == body_object_id
    )
    block = page.facts.objects[block_index]
    spans = tuple(
        item for item in page.facts.text_spans if item.block_index == block_index
    )
    assert spans
    duplicate_index = len(page.facts.objects)
    overlapping_facts = replace(
        page.facts,
        objects=(*page.facts.objects, replace(block, object_id="f" * 64)),
        text_spans=(
            *page.facts.text_spans,
            *(
                replace(
                    item,
                    object_id=f"{ordinal:064x}",
                    block_index=duplicate_index,
                )
                for ordinal, item in enumerate(spans, start=1)
            ),
        ),
    )
    toolbox = SingleFlowTextToolbox(
        load_p8_toolbox_policy(POLICY_PATH),
        fonts.resolve(FONT_ID).path,
    )
    template = toolbox.prepare(page.context, overlapping_facts)
    assert template.object_ids == ()
    assert toolbox.build_translation_request(template) is None
