"""按关键链路重新验收计划执行 RV1：PageFacts、PdfKernel 与 Preservation。"""

from __future__ import annotations

import hashlib
import shutil
import unicodedata
from pathlib import Path

import pymupdf
import pytest

from transflow.domain.common import content_sha256
from transflow.domain.pages import PageExecutionContext
from transflow.domain.text_inventory import InventoryDisposition
from transflow.domain.toolbox import PagePatch, PatchOperation
from transflow.pdf_kernel import (
    FACTS_SCHEMA_VERSION,
    ControlledFontRegistry,
    PageFactsExtractor,
    PagePatchInterpreter,
    ReplayPage,
    capture_document_structure,
    patch_operation_hash,
    validate_preservation,
)
from transflow.pdf_kernel.preservation import load_support_matrix
from transflow.pdf_kernel.text_inventory import freeze_page_text_inventory

REPO_ROOT = Path(__file__).resolve().parent.parent
RV0_SOURCE = (
    REPO_ROOT
    / "runs"
    / "critical_chain_revalidation"
    / "RV0"
    / "01-baseline-20260721-164419"
    / "input"
    / "source_document.pdf"
)
VISUAL_ONLY_ROOT = (
    REPO_ROOT
    / "spikes"
    / "page_classification_engine_puncture_v1"
    / "分类结果"
    / "visual_only"
)
FONT_MANIFEST = REPO_ROOT / "resources" / "manifests" / "font_manifest.json"
FONT_ID = "noto-sans-cjk-sc-regular"
OWNER = "shared.margin.footer"
CONFIG_HASH = "a" * 64


def sha256_file(path: Path) -> str:
    """流式计算真实 PDF 哈希。"""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def extract_real_page(page_no: int):
    """从 RV0 冻结的未拆分年报提取指定页面。"""

    source_hash = sha256_file(RV0_SOURCE)
    return PageFactsExtractor().extract_page(RV0_SOURCE, source_hash, page_no)


@pytest.mark.e2e
def test_rv1_t01_full_document_enumerates_twice_without_facts_drift() -> None:
    """RV1-T01：整本年报双跑的页序、身份与 Kernel Facts 哈希必须稳定。"""

    source_hash = sha256_file(RV0_SOURCE)
    extractor = PageFactsExtractor()
    first = extractor.extract_all(RV0_SOURCE, source_hash)
    second = extractor.extract_all(RV0_SOURCE, source_hash)

    with pymupdf.open(RV0_SOURCE) as document:
        expected_page_count = document.page_count
    assert tuple(item.page.page_no for item in first) == tuple(
        range(1, expected_page_count + 1)
    )
    assert tuple(
        (
            item.page.page_no,
            item.page_identity,
            item.media_box,
            item.crop_box,
            item.rotation,
            item.kernel_facts_hash,
        )
        for item in first
    ) == tuple(
        (
            item.page.page_no,
            item.page_identity,
            item.media_box,
            item.crop_box,
            item.rotation,
            item.kernel_facts_hash,
        )
        for item in second
    )


@pytest.mark.integration
def test_rv1_t02_p151_table_text_and_outside_text_are_traceable() -> None:
    """RV1-T02：p151 小表格、表内文字、正文及语义页脚均可独立追溯。"""

    facts = extract_real_page(151)
    assert len(facts.table_objects) == 1
    table_ids = set(facts.table_text_object_ids)
    outside_ids = set(facts.outside_table_text_object_ids)
    all_ids = {item.object_id for item in facts.text_spans}
    assert table_ids
    assert outside_ids
    assert not table_ids & outside_ids
    assert table_ids | outside_ids == all_ids

    text_by_id = {item.object_id: item.text for item in facts.text_spans}
    table_text = " ".join(text_by_id[item_id] for item_id in table_ids)
    outside_text = " ".join(text_by_id[item_id] for item_id in outside_ids)
    assert "At 31 Dec 2025" in table_text
    assert "Financial" in table_text and "years" in table_text
    assert "Notes to the Consolidated Financial Statements" in outside_text


@pytest.mark.integration
def test_rv1_t03_p101_semantic_footer_translates_but_page_number_stays() -> None:
    """RV1-T03：自然语言页脚进入翻译分母，页码继续机械保留。"""

    facts = extract_real_page(101)
    inventory = freeze_page_text_inventory(facts)
    items_by_id = {item.object_id: item for item in inventory.items}
    spans_by_text = {item.text.strip(): item for item in facts.text_spans}

    semantic_footer = items_by_id[spans_by_text["Corporate Governance Report"].object_id]
    page_number = items_by_id[spans_by_text["99"].object_id]
    assert semantic_footer.disposition is InventoryDisposition.TRANSLATE
    assert semantic_footer.keep_source_reason is None
    assert page_number.disposition is InventoryDisposition.KEEP_SOURCE
    assert page_number.keep_source_reason == "PAGE_NUMBER"


@pytest.mark.integration
def test_rv1_t04_page_facts_cover_links_annotations_fonts_and_protection(
    tmp_path: Path,
) -> None:
    """RV1-T04：链接、注释和字体进入稳定 PageFacts，前两者属于保护对象。"""

    source = tmp_path / "features.pdf"
    with pymupdf.open() as document:
        first = document.new_page(width=420, height=600)
        first.insert_text((40, 60), "Feature page one", fontsize=11)
        second = document.new_page(width=420, height=600)
        second.insert_text((40, 60), "Feature page two", fontsize=11)
        first = document[0]
        first.insert_link(
            {
                "kind": pymupdf.LINK_GOTO,
                "from": pymupdf.Rect(40, 80, 160, 100),
                "page": 1,
            }
        )
        first.add_text_annot((180, 90), "RV1 annotation")
        document.save(source)

    source_hash = sha256_file(source)
    extractor = PageFactsExtractor()
    first = extractor.extract_page(source, source_hash, 1)
    second = extractor.extract_page(source, source_hash, 1)

    assert FACTS_SCHEMA_VERSION == "transflow.pdf-kernel.facts/v2"
    assert len(first.link_objects) == 1
    assert len(first.annotation_objects) == 1
    assert first.font_objects
    assert set(item.object_id for item in first.link_objects) <= set(
        first.protected_object_ids
    )
    assert set(item.object_id for item in first.annotation_objects) <= set(
        first.protected_object_ids
    )
    assert first.link_objects == second.link_objects
    assert first.annotation_objects == second.annotation_objects
    assert first.font_objects == second.font_objects


@pytest.mark.integration
def test_rv1_t05_visual_only_references_have_no_editable_text() -> None:
    """visual_only 强制集应覆盖图片、矢量与混合对象，且翻译分母为零。"""

    summaries: list[tuple[int, int, int]] = []
    for source in sorted(VISUAL_ONLY_ROOT.glob("*.pdf")):
        facts = PageFactsExtractor().extract_page(source, sha256_file(source), 1)
        summaries.append(
            (len(facts.text_spans), len(facts.image_objects), len(facts.drawing_objects))
        )
        assert not facts.text_spans
        assert not freeze_page_text_inventory(facts).items

    assert summaries
    assert any(images and not drawings for _, images, drawings in summaries)
    assert any(drawings and not images for _, images, drawings in summaries)
    assert any(images and drawings for _, images, drawings in summaries)


@pytest.mark.e2e
def test_rv1_t06_real_candidate_preserves_structure_and_writes_extractable_chinese(
    tmp_path: Path,
) -> None:
    """RV1-T05：真实年报技术候选保持受保护事实，并写入可提取中文字体。"""

    source_hash = sha256_file(RV0_SOURCE)
    source_facts = PageFactsExtractor().extract_page(RV0_SOURCE, source_hash, 101)
    footer = next(
        item
        for item in source_facts.text_spans
        if item.text.strip() == "Corporate Governance Report"
    )
    replacement = "公司治理报告"
    operation_hash = patch_operation_hash(
        owner=OWNER,
        target_object_ids=(footer.object_id,),
        rect=footer.bbox,
        replacement_text=replacement,
        font_id=FONT_ID,
        font_size=5.5,
        redaction_rects=(footer.bbox,),
        color_srgb=footer.color_srgb,
        preserve_drawing_overlap=True,
    )
    operation = PatchOperation(
        operation_id="rv1-p101-semantic-footer",
        region_id="shared.margin.footer.p101",
        kind="replace_text",
        payload_hash=operation_hash,
        owner=OWNER,
        target_object_ids=(footer.object_id,),
        rect=footer.bbox,
        replacement_text=replacement,
        font_id=FONT_ID,
        font_size=5.5,
        redaction_rects=(footer.bbox,),
        color_srgb=footer.color_srgb,
        preserve_drawing_overlap=True,
    )
    patch = PagePatch(
        patch_id="rv1-p101-technical-candidate",
        source_hash=source_hash,
        page_no=101,
        geometry_hash=source_facts.page.geometry_hash,
        owner=OWNER,
        operations=(operation,),
    )
    context = PageExecutionContext(
        job_id="critical-chain-rv1",
        run_id="rv1-directed-test",
        source_hash=source_hash,
        page_no=101,
        geometry_hash=source_facts.page.geometry_hash,
        config_snapshot_hash=CONFIG_HASH,
    )
    candidate = tmp_path / "candidate.pdf"
    shutil.copy2(RV0_SOURCE, candidate)
    source_structure = capture_document_structure(RV0_SOURCE)
    PagePatchInterpreter(ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT)).replay_document(
        candidate,
        (ReplayPage(context, source_facts, patch, OWNER),),
        diagnostic=True,
    )

    candidate_hash = sha256_file(candidate)
    candidate_facts = PageFactsExtractor().extract_page(candidate, candidate_hash, 101)
    candidate_structure = capture_document_structure(candidate)
    preservation = validate_preservation(
        source_structure,
        candidate_structure,
        frozenset({101}),
        load_support_matrix(),
    )
    assert preservation.passed, preservation.failure_codes
    assert candidate_facts.locked_objects_hash == source_facts.locked_objects_hash
    assert candidate_facts.media_box == source_facts.media_box
    assert candidate_facts.crop_box == source_facts.crop_box
    assert candidate_facts.rotation == source_facts.rotation
    with pymupdf.open(candidate) as document:
        assert document.page_count == source_structure.page_count
        extracted_text = unicodedata.normalize("NFKC", document[100].get_text())
        assert replacement in extracted_text
    assert any(
        item.embedded
        and item.has_to_unicode
        and "NotoSans" in item.base_font.replace(" ", "")
        for item in candidate_facts.font_objects
    )
    assert content_sha256(candidate_facts.annotation_objects) == content_sha256(
        source_facts.annotation_objects
    )
    assert content_sha256(candidate_facts.link_objects) == content_sha256(
        source_facts.link_objects
    )
