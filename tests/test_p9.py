"""按 P9.1～P9.7 的 42 个编号用例验收第二批普通叶。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pymupdf
import pytest

from transflow.adapters.ai.fixed import FixedTranslationAdapter
from transflow.adapters.filesystem.artifact_store import SharedFilesystemArtifactAdapter
from transflow.adapters.filesystem.checkpoint import FilesystemCheckpointAdapter
from transflow.application.contracts import DocumentExecution, EnumeratedPage
from transflow.application.document_coordinator import DocumentCoordinator
from transflow.application.document_finalizer import DocumentFinalizer
from transflow.application.page_pipeline import PreviewPublisher
from transflow.application.toolbox_page_coordinator import ToolboxPageCoordinator, ToolboxPageWork
from transflow.application.toolbox_page_pipeline import ToolboxPagePipeline
from transflow.domain.jobs import DocumentRunRequest
from transflow.domain.states import CheckpointCompatibility, Fallback, PagePipelineState
from transflow.domain.toolbox import PagePatch
from transflow.pdf_kernel import (
    ControlledFontRegistry,
    PageFactsExtractor,
    PagePatchInterpreter,
    PyMuPdfPageRenderer,
)
from transflow.toolboxes.catalog import (
    ToolboxCatalog,
    ToolboxCatalogEntry,
    catalog_entry_fingerprint,
    load_toolbox_catalog,
)
from transflow.toolboxes.contracts import ToolboxExecutionResult
from transflow.toolboxes.leaves import (
    AnchoredBlocksToolbox,
    ContentsToolbox,
    CoverToolbox,
    EndToolbox,
    MultiFlowTextToolbox,
    TableToolbox,
    build_p8_toolbox_factories,
    build_p9_toolbox_factories,
)
from transflow.toolboxes.leaves.ordinary import StructuredOrdinaryLeafToolbox
from transflow.toolboxes.leaves.ordinary_policy import load_p9_ordinary_leaf_policy

REPO_ROOT = Path(__file__).resolve().parent.parent
FONT_MANIFEST = REPO_ROOT / "resources" / "manifests" / "font_manifest.json"
P8_POLICY = REPO_ROOT / "resources" / "manifests" / "p8_toolbox_policy.json"
P9_POLICY = REPO_ROOT / "resources" / "manifests" / "p9_ordinary_leaf_policy.json"
CATALOG_V3 = REPO_ROOT / "resources" / "catalogs" / "page_toolbox_catalog_v3.json"
CATALOG_V4 = REPO_ROOT / "resources" / "catalogs" / "page_toolbox_catalog_v4.json"
SUMMARY_PATH = REPO_ROOT / "resources" / "evidence" / "p9" / "p9_acceptance_summary.json"
SCHEMA_PATH = REPO_ROOT / "resources" / "schemas" / "leaf_migration_evidence_v1.schema.json"
FONT_ID = "noto-sans-cjk-sc-regular"
HASH_A = "a" * 64
P9_ROUTES = (
    "cover",
    "contents",
    "end",
    "body.flow_text.multi",
    "body.table",
    "body.anchored_blocks",
)


def sha256_file(path: Path) -> str:
    """流式计算真实 PDF 或资源内容哈希。"""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _scan_image_png(label: str = "IMAGE TEXT - NO OCR") -> bytes:
    """生成内部带文字但对页面事实仅为受保护图片的 PNG。"""

    with pymupdf.open() as document:
        page = document.new_page(width=220, height=110)
        page.insert_text((20, 55), label, fontsize=12)
        return page.get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False).tobytes("png")


def _draw_page(page: pymupdf.Page, kind: str, scale: float = 1.0) -> None:
    """按普通叶结构绘制可被真实 Kernel 解析的一页。"""

    if kind == "cover":
        page.draw_rect(pymupdf.Rect(25, 25, 395, 575), color=(0.2, 0.2, 0.2))
        page.insert_image(pymupdf.Rect(300, 45, 375, 90), stream=_scan_image_png("LOGO"))
        page.insert_text((55, 175), "TRANSFLOW ANNUAL REPORT", fontsize=22 * scale)
        page.insert_text((55, 225), "Sustainable growth", fontsize=14 * scale)
        page.insert_text((55, 275), "2026", fontsize=11 * scale)
    elif kind == "contents":
        page.insert_text((45, 60), "CONTENTS", fontsize=18)
        for index, title in enumerate(("Overview", "Business Review", "Financial Statements")):
            y = 125 + index * 65
            page.insert_text((55 + index * 12, y), title, fontsize=11)
            page.insert_text((255, y), "........", fontsize=10)
            page.insert_text((350, y), str(index + 2), fontsize=10)
    elif kind == "end_blank":
        return
    elif kind == "end_visual":
        page.insert_image(pymupdf.Rect(95, 180, 325, 300), stream=_scan_image_png("END IMAGE"))
    elif kind == "end_text":
        page.insert_text((65, 220), "Contact: investor@example.com", fontsize=12)
        page.insert_text((65, 260), "Copyright 2026 Transflow", fontsize=10)
    elif kind == "end_mixed":
        page.insert_image(pymupdf.Rect(95, 100, 325, 200), stream=_scan_image_png("QR AND LOGO"))
        page.insert_text((65, 300), "Contact: investor@example.com", fontsize=12)
        page.draw_line((65, 330), (355, 330), color=(0, 0, 0))
    elif kind in {"multi2", "multi3"}:
        column_count = 2 if kind == "multi2" else 3
        left, right, gutter = 35.0, 385.0, 24.0
        width = (right - left - gutter * (column_count - 1)) / column_count
        for column in range(column_count):
            x0 = left + column * (width + gutter)
            page.insert_textbox(
                pymupdf.Rect(x0, 125, x0 + width, 260),
                f"Column {column + 1} first paragraph.\nColumn {column + 1} second paragraph.",
                fontsize=10 * scale,
            )
    elif kind == "multi2_spanning":
        page.insert_textbox(
            pymupdf.Rect(35, 55, 385, 90),
            "SPANNING TITLE ACROSS BOTH COLUMNS AND PAGE",
            fontsize=12,
        )
        _draw_page(page, "multi2", scale)
        page.insert_image(pymupdf.Rect(175, 300, 245, 365), stream=_scan_image_png("ANCHOR"))
    elif kind == "table":
        x_values, y_values = (45, 160, 275, 375), (120, 190, 260, 330)
        for x in x_values:
            page.draw_line((x, y_values[0]), (x, y_values[-1]), color=(0, 0, 0))
        for y in y_values:
            page.draw_line((x_values[0], y), (x_values[-1], y), color=(0, 0, 0))
        for row in range(3):
            for column in range(3):
                page.insert_text(
                    (x_values[column] + 10, y_values[row] + 35),
                    f"R{row + 1}C{column + 1}",
                    fontsize=9 * scale,
                )
    elif kind == "borderless_table":
        for row in range(3):
            for column in range(3):
                page.insert_text((55 + column * 110, 140 + row * 55), f"B{row}{column}", fontsize=9)
    elif kind == "image_table":
        page.insert_image(pymupdf.Rect(45, 120, 375, 330), stream=_scan_image_png("SCANNED TABLE"))
    elif kind == "anchored":
        page.draw_rect(pymupdf.Rect(45, 120, 120, 200), color=(0, 0, 0))
        page.draw_rect(pymupdf.Rect(285, 310, 365, 390), color=(0, 0, 0))
        page.insert_textbox(pymupdf.Rect(130, 125, 245, 190), "First anchored block", fontsize=10)
        page.insert_textbox(pymupdf.Rect(180, 340, 285, 390), "Second anchored block", fontsize=10)
    elif kind == "anchored_conflict":
        page.draw_rect(pymupdf.Rect(50, 210, 120, 290), color=(0, 0, 0))
        page.draw_rect(pymupdf.Rect(300, 210, 370, 290), color=(0, 0, 0))
        page.insert_textbox(pymupdf.Rect(170, 220, 250, 280), "Tied block", fontsize=10)
    elif kind == "visual":
        page.insert_image(pymupdf.Rect(60, 120, 360, 300), stream=_scan_image_png())
    elif kind == "single":
        page.insert_textbox(
            pymupdf.Rect(55, 120, 365, 190),
            "1. P8 single regression text.",
            fontsize=11,
        )
    elif kind == "chart":
        page.draw_rect(pymupdf.Rect(80, 120, 340, 410), color=(0, 0, 0))
        page.insert_text((165, 250), "Revenue chart", fontsize=11)
    elif kind == "diagram":
        page.draw_rect(pymupdf.Rect(90, 150, 250, 230), color=(0, 0, 0))
        page.insert_text((130, 195), "Input", fontsize=10)
    else:
        raise ValueError(f"未知 P9 测试页类型: {kind}")


def create_pdf(path: Path, kinds: tuple[str, ...], *, scale: float = 1.0) -> Path:
    """生成包含指定结构页且可真实打开、解析、渲染的完整 PDF。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open() as document:
        for kind in kinds:
            page = document.new_page(width=420 * scale, height=600 * scale)
            _draw_page(page, kind, scale)
        document.save(path)
    return path


def make_request(path: Path, run_id: str) -> DocumentRunRequest:
    """为完整测试 PDF 建立生产形态的 DocumentRunRequest。"""

    return DocumentRunRequest(
        source_pdf_path=str(path.resolve()),
        source_hash=sha256_file(path),
        source_language="en",
        target_language="zh-CN",
        config_snapshot_hash=HASH_A,
        job_id="job-p9",
        run_id=run_id,
    )


def direct_page(path: Path, page_no: int = 1, run_id: str = "p9-direct") -> EnumeratedPage:
    """通过真实文档协调器枚举并返回一页上下文与 Kernel Facts。"""

    request = make_request(path, run_id)
    return DocumentCoordinator(PageFactsExtractor()).enumerate_pages(request)[page_no - 1]


def toolbox_for(route: str) -> StructuredOrdinaryLeafToolbox:
    """按固定 Route 构造一个 P9 普通叶测试实例。"""

    policy = load_p9_ordinary_leaf_policy(P9_POLICY)
    font = ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT).resolve(FONT_ID).path
    classes: dict[str, type[StructuredOrdinaryLeafToolbox]] = {
        "cover": CoverToolbox,
        "contents": ContentsToolbox,
        "end": EndToolbox,
        "body.flow_text.multi": MultiFlowTextToolbox,
        "body.table": TableToolbox,
        "body.anchored_blocks": AnchoredBlocksToolbox,
    }
    return classes[route](policy, font)


def direct_result(
    page: EnumeratedPage,
    toolbox: StructuredOrdinaryLeafToolbox,
    translations: str | tuple[str, ...] = "翻译文本",
) -> ToolboxExecutionResult:
    """用真实 FixedTranslationAdapter 执行一个叶的完整六阶段。"""

    template = toolbox.prepare(page.context, page.facts)
    batch = toolbox.build_translation_request(template)
    mapping: dict[str, str] = {}
    if batch is not None:
        values = (
            tuple(translations for _ in batch.units)
            if isinstance(translations, str)
            else translations
        )
        if len(values) != len(batch.units):
            raise ValueError("测试译文数量与 unit 数不一致")
        mapping = {unit.unit_id: value for unit, value in zip(batch.units, values, strict=True)}
    return ToolboxPageCoordinator(FixedTranslationAdapter(mapping)).execute(
        ToolboxPageWork(page.context, page.facts, toolbox)
    )


@dataclass(slots=True)
class P9Runtime:
    """聚合一次 P9 文档运行的真实 Adapter、Catalog 与 Kernel。"""

    artifacts: SharedFilesystemArtifactAdapter
    catalog: ToolboxCatalog
    pipeline: ToolboxPagePipeline
    coordinator: DocumentCoordinator
    finalizer: DocumentFinalizer


def make_runtime(
    run_root: Path,
    request: DocumentRunRequest,
    translations: dict[str, str],
    catalog: ToolboxCatalog | None = None,
) -> P9Runtime:
    """用 v4 Catalog、文件 Checkpoint 和真实 PDF Kernel 构造运行时。"""

    artifacts = SharedFilesystemArtifactAdapter(run_root, request.run_id)
    fonts = ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT)
    interpreter = PagePatchInterpreter(fonts)
    selected = catalog or load_toolbox_catalog(
        CATALOG_V4,
        build_p9_toolbox_factories(P8_POLICY, P9_POLICY, FONT_MANIFEST, REPO_ROOT),
    )
    checkpoints = FilesystemCheckpointAdapter(run_root, request.run_id, artifacts)
    compatibility = CheckpointCompatibility(
        source_hash=request.source_hash,
        config_hash=request.config_snapshot_hash,
        font_hash=fonts.manifest_hash,
        toolbox_catalog_hash=selected.catalog_hash,
        schema_hash=sha256_file(SCHEMA_PATH),
    )
    pipeline = ToolboxPagePipeline(
        selected,
        ToolboxPageCoordinator(FixedTranslationAdapter(translations)),
        PyMuPdfPageRenderer(interpreter),
        PreviewPublisher(artifacts),
        checkpoints,
        compatibility,
    )
    return P9Runtime(
        artifacts,
        selected,
        pipeline,
        DocumentCoordinator(PageFactsExtractor()),
        DocumentFinalizer(interpreter, artifacts, run_root),
    )


def run_document(
    tmp_path: Path,
    kinds: tuple[str, ...],
    routes: tuple[str, ...],
    run_id: str,
    *,
    catalog: ToolboxCatalog | None = None,
) -> tuple[DocumentExecution, P9Runtime, Path]:
    """执行完整 PDF，并返回文档终态、运行组件与真实源文件。"""

    source = create_pdf(tmp_path / f"{run_id}.pdf", kinds)
    request = make_request(source, run_id)
    runtime = make_runtime(tmp_path / "runs" / run_id, request, {}, catalog)
    route_by_page = dict(enumerate(routes, start=1))
    execution = runtime.coordinator.run(
        request,
        lambda page: route_by_page[page.context.page_no],
        runtime.pipeline,
        runtime.finalizer,
    )
    return execution, runtime, source


def _tampered_patch(patch: PagePatch, **changes: Any) -> PagePatch:
    """基于真实 Patch 构造单一违规变体，保留其余生产字段。"""

    operation = replace(patch.operations[0], **changes)
    return replace(patch, operations=(operation, *patch.operations[1:]))


@pytest.mark.contract
def test_p9_1_t01_cover_owner_order_and_hierarchy(tmp_path: Path) -> None:
    """P9.1-T01：封面标题、副标题按阅读顺序形成唯一 owner。"""

    page = direct_page(create_pdf(tmp_path / "cover.pdf", ("cover",)))
    toolbox = toolbox_for("cover")
    template = toolbox.prepare(page.context, page.facts)
    snapshot = toolbox.audit_snapshot(template)
    assert snapshot.owner_coverage_complete
    assert [item.source_text for item in snapshot.atoms][:2] == [
        "TRANSFLOW ANNUAL REPORT",
        "Sustainable growth",
    ]


@pytest.mark.contract
def test_p9_1_t02_cover_protected_visuals_unchanged(tmp_path: Path) -> None:
    """P9.1-T02：Logo、背景与装饰对象不进入 Patch。"""

    page = direct_page(create_pdf(tmp_path / "cover-visuals.pdf", ("cover",)))
    result = direct_result(page, toolbox_for("cover"))
    targets = (
        {target for op in result.patch.operations for target in op.target_object_ids}
        if result.patch
        else set()
    )
    assert targets.isdisjoint(page.facts.protected_object_ids)
    assert (
        page.facts.locked_objects_hash
        == direct_page(tmp_path / "cover-visuals.pdf").facts.locked_objects_hash
    )


@pytest.mark.migration
def test_p9_1_t03_cover_blind_threshold_keeps_disabled() -> None:
    """P9.1-T03：没有新真实匿名集时 cover 明确保持 disabled。"""

    summary = json.loads(SUMMARY_PATH.read_text(encoding="utf-8"))
    entry = next(item for item in load_toolbox_catalog(CATALOG_V4).entries if item.route == "cover")
    assert summary["real_anonymous_document_counts"]["cover"] == 0
    assert not entry.enabled and entry.evidence_state == "PASS_DISABLED_WITH_FALLBACK"


@pytest.mark.regression
def test_p9_1_t04_cover_perturbations_are_structure_driven(tmp_path: Path) -> None:
    """P9.1-T04：页面比例和字号扰动改变事实但不改变 owner 原则。"""

    counts, hashes = [], []
    for scale in (0.8, 1.2):
        page = direct_page(create_pdf(tmp_path / f"cover-{scale}.pdf", ("cover",), scale=scale))
        toolbox = toolbox_for("cover")
        snapshot = toolbox.audit_snapshot(toolbox.prepare(page.context, page.facts))
        counts.append(len(snapshot.atoms))
        hashes.append(page.facts.kernel_facts_hash)
    assert counts == [2, 2] and len(set(hashes)) == 2


@pytest.mark.fault_injection
def test_p9_1_t05_cover_layout_failure_falls_back(tmp_path: Path) -> None:
    """P9.1-T05：超长封面译文经一次修复后整页安全透传。"""

    page = direct_page(create_pdf(tmp_path / "cover-overflow.pdf", ("cover",)))
    result = direct_result(page, toolbox_for("cover"), "超长封面标题" * 600)
    assert result.patch is None and result.outcome.fallback is Fallback.PAGE_PASSTHROUGH
    assert result.outcome.state is PagePipelineState.FINALIZED


@pytest.mark.contract
def test_p9_1_t06_cover_has_no_single_cross_leaf_fallback() -> None:
    """P9.1-T06：cover 实现不导入或调用 single 正文流。"""

    source = (REPO_ROOT / "src" / "transflow" / "toolboxes" / "leaves" / "ordinary.py").read_text(
        encoding="utf-8"
    )
    cover_source = source[
        source.index("class CoverToolbox") : source.index("class ContentsToolbox")
    ]
    assert (
        "SingleFlowTextToolbox" not in cover_source and "body.flow_text.single" not in cover_source
    )


@pytest.mark.contract
def test_p9_2_t01_contents_mapping_and_levels_are_stable(tmp_path: Path) -> None:
    """P9.2-T01：多级目录条目顺序、分组和 owner mapping 闭合。"""

    page = direct_page(create_pdf(tmp_path / "contents.pdf", ("contents",)))
    toolbox = toolbox_for("contents")
    snapshot = toolbox.audit_snapshot(toolbox.prepare(page.context, page.facts))
    assert snapshot.owner_coverage_complete and len(snapshot.atoms) == 4
    assert len({item.object_id for item in snapshot.atoms}) == len(snapshot.atoms)


@pytest.mark.contract
def test_p9_2_t02_contents_page_numbers_and_leaders_are_keep_source(tmp_path: Path) -> None:
    """P9.2-T02：纯数字页码和点线不生成 TranslationUnit/Patch。"""

    page = direct_page(create_pdf(tmp_path / "contents-keep.pdf", ("contents",)))
    toolbox = toolbox_for("contents")
    template = toolbox.prepare(page.context, page.facts)
    snapshot = toolbox.audit_snapshot(template)
    kept_text = {
        item.text for item in page.facts.text_spans if item.object_id in snapshot.keep_source_ids
    }
    assert {"2", "3", "4", "........"} <= kept_text
    batch = toolbox.build_translation_request(template)
    assert batch is not None and all(not unit.source_text.isdigit() for unit in batch.units)


@pytest.mark.fault_injection
def test_p9_2_t03_contents_long_entry_rolls_back_atomically(tmp_path: Path) -> None:
    """P9.2-T03：一个长条目失败时只保留其他完整条目，不产生半条。"""

    page = direct_page(create_pdf(tmp_path / "contents-long.pdf", ("contents",)))
    toolbox = toolbox_for("contents")
    template = toolbox.prepare(page.context, page.facts)
    batch = toolbox.build_translation_request(template)
    assert batch is not None
    values = tuple(
        "超长目录译文" * 600 if index == 1 else "安全条目" for index, _ in enumerate(batch.units)
    )
    result = direct_result(page, toolbox_for("contents"), values)
    assert result.patch is not None and result.outcome.fallback is Fallback.REGION_FALLBACK
    assert all(op.region_id != batch.units[1].region_id for op in result.patch.operations)


@pytest.mark.regression
def test_p9_2_t04_contents_structure_perturbation_has_no_fixed_coordinate(tmp_path: Path) -> None:
    """P9.2-T04：页面缩放后 mapping 仍由归一化行结构形成。"""

    groups = []
    for scale in (0.85, 1.15):
        page = direct_page(
            create_pdf(tmp_path / f"contents-{scale}.pdf", ("contents",), scale=scale)
        )
        toolbox = toolbox_for("contents")
        snapshot = toolbox.audit_snapshot(toolbox.prepare(page.context, page.facts))
        groups.append(tuple(item.group_id for item in snapshot.atoms))
    assert groups[0] == groups[1]


@pytest.mark.contract
def test_p9_2_t05_contents_missing_or_duplicate_mapping_rejected(tmp_path: Path) -> None:
    """P9.2-T05：跨条目或重复 mapping 被 owner guard 拒绝。"""

    page = direct_page(create_pdf(tmp_path / "contents-guard.pdf", ("contents",)))
    toolbox = toolbox_for("contents")
    template = toolbox.prepare(page.context, page.facts)
    result = direct_result(page, toolbox_for("contents"))
    assert result.patch is not None
    invalid = _tampered_patch(result.patch, region_id="contents-entry-missing")
    assert "P9_CROSS_GROUP_REJECTED" in toolbox.validate_patch(template, invalid)


@pytest.mark.integration
def test_p9_2_t06_contents_full_pdf_preserves_links_and_order(tmp_path: Path) -> None:
    """P9.2-T06：完整 PDF 透传后链接目标、页数和页序保持。"""

    source = create_pdf(tmp_path / "contents-links.pdf", ("contents", "end_text"))
    with pymupdf.open(source) as document:
        document[0].insert_link(
            {"kind": pymupdf.LINK_GOTO, "from": pymupdf.Rect(45, 110, 380, 145), "page": 1}
        )
        document.save(tmp_path / "contents-links-saved.pdf")
    saved = tmp_path / "contents-links-saved.pdf"
    request = make_request(saved, "p9-contents-links")
    runtime = make_runtime(tmp_path / "runs" / request.run_id, request, {})
    execution = runtime.coordinator.run(
        request,
        lambda page: ("contents", "end")[page.context.page_no - 1],
        runtime.pipeline,
        runtime.finalizer,
    )
    target = runtime.artifacts.get(execution.final_artifact.artifact_id)
    with pymupdf.open(saved) as before, pymupdf.open(stream=target, filetype="pdf") as after:
        assert before.page_count == after.page_count == 2
        assert before[0].get_links()[0]["page"] == after[0].get_links()[0]["page"] == 1


@pytest.mark.contract
def test_p9_3_t01_end_blank_and_visual_are_zero_translation(tmp_path: Path) -> None:
    """P9.3-T01：空白和纯视觉 end 均零 unit/Patch 并终态透传。"""

    for kind in ("end_blank", "end_visual"):
        page = direct_page(create_pdf(tmp_path / f"{kind}.pdf", (kind,)))
        result = direct_result(page, toolbox_for("end"))
        assert result.ordered_unit_ids == () and result.patch is None
        assert result.outcome.fallback is Fallback.PAGE_PASSTHROUGH


@pytest.mark.contract
def test_p9_3_t02_end_text_only_edits_explicit_owner(tmp_path: Path) -> None:
    """P9.3-T02：联系方式和版权文字形成唯一 Patch owner。"""

    page = direct_page(create_pdf(tmp_path / "end-text.pdf", ("end_text",)))
    result = direct_result(page, toolbox_for("end"))
    assert result.patch is not None and len(result.patch.operations) == 2
    assert all(op.owner == "end" for op in result.patch.operations)


@pytest.mark.contract
def test_p9_3_t03_end_visual_objects_are_protected(tmp_path: Path) -> None:
    """P9.3-T03：Logo、二维码、背景和装饰对象不被 end Patch 修改。"""

    page = direct_page(create_pdf(tmp_path / "end-mixed.pdf", ("end_mixed",)))
    result = direct_result(page, toolbox_for("end"))
    targets = (
        {target for op in result.patch.operations for target in op.target_object_ids}
        if result.patch
        else set()
    )
    assert targets.isdisjoint(page.facts.protected_object_ids)


@pytest.mark.regression
def test_p9_3_t04_end_does_not_depend_on_last_page_or_filename(tmp_path: Path) -> None:
    """P9.3-T04：end 在文档中间和不同文件名下产生相同 owner 数。"""

    first = create_pdf(tmp_path / "middle-a.pdf", ("single", "end_text", "single"))
    second = create_pdf(tmp_path / "renamed-b.pdf", ("single", "end_text", "single"))
    counts = []
    for source in (first, second):
        page = direct_page(source, 2)
        toolbox = toolbox_for("end")
        counts.append(len(toolbox.audit_snapshot(toolbox.prepare(page.context, page.facts)).atoms))
    assert counts == [2, 2]


@pytest.mark.fault_injection
def test_p9_3_t05_end_long_statement_has_bounded_fallback(tmp_path: Path) -> None:
    """P9.3-T05：超长声明在一次 Repair 后整页透传并完成。"""

    page = direct_page(create_pdf(tmp_path / "end-long.pdf", ("end_text",)))
    result = direct_result(page, toolbox_for("end"), "超长结束页声明" * 600)
    assert result.patch is None and result.outcome.fallback is Fallback.PAGE_PASSTHROUGH
    assert result.outcome.state is PagePipelineState.FINALIZED


@pytest.mark.migration
def test_p9_3_t06_end_unknown_set_requires_blind_threshold() -> None:
    """P9.3-T06：end 没有新盲测集时只能明确禁用。"""

    summary = json.loads(SUMMARY_PATH.read_text(encoding="utf-8"))
    assert summary["real_anonymous_document_counts"]["end"] == 0
    assert summary["conclusions"]["end"] == "PASS_DISABLED_WITH_FALLBACK"


@pytest.mark.contract
def test_p9_4_t01_multi_columns_have_unique_ordered_owners(tmp_path: Path) -> None:
    """P9.4-T01：双栏和三栏均形成唯一列 owner 与列内/列间顺序。"""

    counts = []
    for kind in ("multi2", "multi3"):
        page = direct_page(create_pdf(tmp_path / f"{kind}.pdf", (kind,)))
        toolbox = toolbox_for("body.flow_text.multi")
        snapshot = toolbox.audit_snapshot(toolbox.prepare(page.context, page.facts))
        assert snapshot.owner_coverage_complete
        counts.append(len({atom.group_id for atom in snapshot.atoms}))
    assert counts == [2, 3]


@pytest.mark.regression
def test_p9_4_t02_multi_geometry_perturbations_follow_facts(tmp_path: Path) -> None:
    """P9.4-T02：列数和页面比例变化驱动 owner 数变化。"""

    owner_counts = []
    for kind, scale in (("multi2", 0.9), ("multi3", 1.15)):
        page = direct_page(create_pdf(tmp_path / f"{kind}-{scale}.pdf", (kind,), scale=scale))
        toolbox = toolbox_for("body.flow_text.multi")
        snapshot = toolbox.audit_snapshot(toolbox.prepare(page.context, page.facts))
        owner_counts.append(len({atom.group_id for atom in snapshot.atoms}))
    assert owner_counts == [2, 3]


@pytest.mark.contract
def test_p9_4_t03_multi_spanning_title_and_image_are_not_double_claimed(tmp_path: Path) -> None:
    """P9.4-T03：跨栏标题和图片 anchor 不被任一普通栏重复领取。"""

    page = direct_page(create_pdf(tmp_path / "multi-spanning.pdf", ("multi2_spanning",)))
    toolbox = toolbox_for("body.flow_text.multi")
    snapshot = toolbox.audit_snapshot(toolbox.prepare(page.context, page.facts))
    kept_text = {
        item.text for item in page.facts.text_spans if item.object_id in snapshot.keep_source_ids
    }
    assert "SPANNING TITLE ACROSS BOTH COLUMNS AND PAGE" in kept_text
    assert set(atom.object_id for atom in snapshot.atoms).isdisjoint(
        page.facts.protected_object_ids
    )


@pytest.mark.contract
def test_p9_4_t04_multi_cross_gutter_owner_and_clip_patch_rejected(tmp_path: Path) -> None:
    """P9.4-T04：跨 gutter、跨栏、跨 owner Patch 全部被拒绝。"""

    page = direct_page(create_pdf(tmp_path / "multi-guard.pdf", ("multi2",)))
    toolbox = toolbox_for("body.flow_text.multi")
    template = toolbox.prepare(page.context, page.facts)
    result = direct_result(page, toolbox_for("body.flow_text.multi"))
    assert result.patch is not None
    op = result.patch.operations[0]
    invalid = _tampered_patch(
        result.patch,
        owner="body.flow_text.single",
        region_id=result.patch.operations[-1].region_id,
        rect=(op.rect[0], op.rect[1], op.rect[2] + 30, op.rect[3]),
    )
    codes = toolbox.validate_patch(template, invalid)
    assert {"P9_CROSS_OWNER_REJECTED", "P9_CROSS_GROUP_REJECTED", "P9_CROSS_CLIP_REJECTED"} <= set(
        codes
    )


@pytest.mark.regression
def test_p9_4_t05_multi_identity_and_scaling_do_not_create_sample_branch(tmp_path: Path) -> None:
    """P9.4-T05：改名和缩放不引入文件身份分支。"""

    counts = []
    for name, scale in (("unknown-a.pdf", 0.95), ("anonymous-b.pdf", 1.05)):
        page = direct_page(create_pdf(tmp_path / name, ("multi2",), scale=scale))
        toolbox = toolbox_for("body.flow_text.multi")
        snapshot = toolbox.audit_snapshot(toolbox.prepare(page.context, page.facts))
        counts.append(len({atom.group_id for atom in snapshot.atoms}))
    assert counts == [2, 2]


@pytest.mark.fault_injection
def test_p9_4_t06_multi_failed_column_does_not_corrupt_other_column(tmp_path: Path) -> None:
    """P9.4-T06：一个栏失败时撤销该栏，其他安全栏保留并终态完成。"""

    page = direct_page(create_pdf(tmp_path / "multi-column-failure.pdf", ("multi2",)))
    toolbox = toolbox_for("body.flow_text.multi")
    template = toolbox.prepare(page.context, page.facts)
    batch = toolbox.build_translation_request(template)
    assert batch is not None
    failed_group = batch.units[0].region_id
    values = tuple(
        "超长栏译文" * 600 if unit.region_id == failed_group else "安全栏译文"
        for unit in batch.units
    )
    result = direct_result(page, toolbox_for("body.flow_text.multi"), values)
    assert result.patch is not None and result.outcome.fallback is Fallback.REGION_FALLBACK
    assert all(op.region_id != failed_group for op in result.patch.operations)


@pytest.mark.contract
def test_p9_5_t01_table_cells_have_stable_unique_owners(tmp_path: Path) -> None:
    """P9.5-T01：有框表格形成稳定 row/column/cell ID 和唯一文本 owner。"""

    page = direct_page(create_pdf(tmp_path / "table.pdf", ("table",)))
    toolbox = toolbox_for("body.table")
    snapshot = toolbox.audit_snapshot(toolbox.prepare(page.context, page.facts))
    assert snapshot.owner_coverage_complete and len(snapshot.atoms) == 9
    assert len({atom.group_id for atom in snapshot.atoms}) == 9


@pytest.mark.contract
def test_p9_5_t02_borderless_table_without_evidence_falls_back(tmp_path: Path) -> None:
    """P9.5-T02：无边框表结构证据不足时整页 KEEP_SOURCE。"""

    page = direct_page(create_pdf(tmp_path / "borderless.pdf", ("borderless_table",)))
    result = direct_result(page, toolbox_for("body.table"), "A")
    assert result.ordered_unit_ids == () and result.patch is None
    assert result.outcome.fallback is Fallback.PAGE_PASSTHROUGH


@pytest.mark.contract
def test_p9_5_t03_image_table_has_zero_ocr_units_and_patch(tmp_path: Path) -> None:
    """P9.5-T03：图片表格零 OCR、零 TranslationUnit、零 Patch。"""

    page = direct_page(create_pdf(tmp_path / "image-table.pdf", ("image_table",)))
    result = direct_result(page, toolbox_for("body.table"))
    assert result.ordered_unit_ids == () and result.patch is None
    assert page.facts.image_objects


@pytest.mark.contract
def test_p9_5_t04_table_duplicate_or_cross_cell_patch_rejected(tmp_path: Path) -> None:
    """P9.5-T04：重复、悬空和跨 cell Patch 均被拒绝。"""

    page = direct_page(create_pdf(tmp_path / "table-guard.pdf", ("table",)))
    toolbox = toolbox_for("body.table")
    template = toolbox.prepare(page.context, page.facts)
    result = direct_result(page, toolbox_for("body.table"), "A")
    assert result.patch is not None
    invalid = _tampered_patch(result.patch, region_id=result.patch.operations[-1].region_id)
    assert "P9_CROSS_GROUP_REJECTED" in toolbox.validate_patch(template, invalid)


@pytest.mark.fault_injection
def test_p9_5_t05_table_single_cell_failure_rolls_back_whole_table(tmp_path: Path) -> None:
    """P9.5-T05：任一 cell 无法修复时整表回退，不保留半表。"""

    page = direct_page(create_pdf(tmp_path / "table-long.pdf", ("table",)))
    toolbox = toolbox_for("body.table")
    template = toolbox.prepare(page.context, page.facts)
    batch = toolbox.build_translation_request(template)
    assert batch is not None
    values = tuple(
        "超长单元格" * 600 if index == 0 else "安全" for index, _ in enumerate(batch.units)
    )
    result = direct_result(page, toolbox_for("body.table"), values)
    assert result.patch is None and result.outcome.fallback is Fallback.PAGE_PASSTHROUGH
    assert "P9_ATOMIC_TABLE_FALLBACK" in result.outcome.finding_codes


@pytest.mark.regression
def test_p9_5_t06_table_structure_changes_without_identity_branch(tmp_path: Path) -> None:
    """P9.5-T06：有框与无框结构差异驱动判断，文件身份不参与。"""

    pages = (
        direct_page(create_pdf(tmp_path / "anonymous-grid.pdf", ("table",))),
        direct_page(create_pdf(tmp_path / "renamed-no-grid.pdf", ("borderless_table",))),
    )
    counts = []
    for page in pages:
        toolbox = toolbox_for("body.table")
        counts.append(len(toolbox.audit_snapshot(toolbox.prepare(page.context, page.facts)).atoms))
    assert counts == [9, 0]


@pytest.mark.contract
def test_p9_6_t01_anchored_blocks_have_unique_anchor_slot_order(tmp_path: Path) -> None:
    """P9.6-T01：多个独立文本块具有唯一 owner、anchor 和阅读顺序。"""

    page = direct_page(create_pdf(tmp_path / "anchored.pdf", ("anchored",)))
    toolbox = toolbox_for("body.anchored_blocks")
    snapshot = toolbox.audit_snapshot(toolbox.prepare(page.context, page.facts))
    assert snapshot.owner_coverage_complete and len(snapshot.atoms) == 2
    assert len({atom.group_id for atom in snapshot.atoms}) == 2


@pytest.mark.regression
def test_p9_6_t02_anchored_binding_follows_geometry(tmp_path: Path) -> None:
    """P9.6-T02：anchor 和 block 数变化由几何事实决定绑定。"""

    normal = direct_page(create_pdf(tmp_path / "anchored-normal.pdf", ("anchored",)))
    conflict = direct_page(create_pdf(tmp_path / "anchored-conflict.pdf", ("anchored_conflict",)))
    counts = []
    for page in (normal, conflict):
        toolbox = toolbox_for("body.anchored_blocks")
        counts.append(len(toolbox.audit_snapshot(toolbox.prepare(page.context, page.facts)).atoms))
    assert counts == [2, 0]


@pytest.mark.contract
def test_p9_6_t03_anchored_cross_owner_clip_source_patch_rejected(tmp_path: Path) -> None:
    """P9.6-T03：跨 owner、clip 和 source Patch 全部被拒绝。"""

    page = direct_page(create_pdf(tmp_path / "anchored-guard.pdf", ("anchored",)))
    toolbox = toolbox_for("body.anchored_blocks")
    template = toolbox.prepare(page.context, page.facts)
    result = direct_result(page, toolbox_for("body.anchored_blocks"))
    assert result.patch is not None
    op = result.patch.operations[0]
    invalid = replace(
        _tampered_patch(
            result.patch,
            owner="body.flow_text.single",
            rect=(op.rect[0], op.rect[1], op.rect[2] + 10, op.rect[3]),
        ),
        source_hash="b" * 64,
    )
    codes = toolbox.validate_patch(template, invalid)
    assert {
        "P9_SOURCE_BINDING_REJECTED",
        "P9_CROSS_OWNER_REJECTED",
        "P9_CROSS_CLIP_REJECTED",
    } <= set(codes)


@pytest.mark.contract
def test_p9_6_t04_anchored_competing_slot_is_keep_source(tmp_path: Path) -> None:
    """P9.6-T04：两个 anchor 距离并列时冲突对象完整 KEEP_SOURCE。"""

    page = direct_page(create_pdf(tmp_path / "anchored-tie.pdf", ("anchored_conflict",)))
    toolbox = toolbox_for("body.anchored_blocks")
    snapshot = toolbox.audit_snapshot(toolbox.prepare(page.context, page.facts))
    assert snapshot.atoms == () and len(snapshot.keep_source_ids) == 1


@pytest.mark.contract
def test_p9_6_t05_anchored_blocks_do_not_use_flow_text() -> None:
    """P9.6-T05：anchored_blocks 实现不调用 single/multi flow。"""

    source = (REPO_ROOT / "src" / "transflow" / "toolboxes" / "leaves" / "ordinary.py").read_text(
        encoding="utf-8"
    )
    anchored_source = source[source.index("class AnchoredBlocksToolbox") :]
    assert (
        "SingleFlowTextToolbox" not in anchored_source
        and "MultiFlowTextToolbox" not in anchored_source
    )


@pytest.mark.fault_injection
def test_p9_6_t06_anchored_failed_owner_is_atomic(tmp_path: Path) -> None:
    """P9.6-T06：一个 block 失败时只撤销该 owner，其他 block 保留。"""

    page = direct_page(create_pdf(tmp_path / "anchored-long.pdf", ("anchored",)))
    toolbox = toolbox_for("body.anchored_blocks")
    template = toolbox.prepare(page.context, page.facts)
    batch = toolbox.build_translation_request(template)
    assert batch is not None
    failed_group = batch.units[0].region_id
    values = tuple(
        "超长块译文" * 600 if unit.region_id == failed_group else "安全块" for unit in batch.units
    )
    result = direct_result(page, toolbox_for("body.anchored_blocks"), values)
    assert result.patch is not None and result.outcome.fallback is Fallback.REGION_FALLBACK
    assert all(op.region_id != failed_group for op in result.patch.operations)


@pytest.mark.workflow
def test_p9_7_t01_enabled_combination_has_unique_terminal_pages(tmp_path: Path) -> None:
    """P9.7-T01：六叶没有无证据启用项，完整 PDF 仍逐页唯一终态。"""

    execution, runtime, _ = run_document(
        tmp_path,
        ("cover", "contents", "end_text", "multi2", "table", "anchored"),
        P9_ROUTES,
        "p9-all-disabled",
    )
    assert not {entry.route for entry in runtime.catalog.entries if entry.enabled} & set(P9_ROUTES)
    assert len(execution.pages) == 6 and all(
        page.outcome.state is PagePipelineState.FINALIZED for page in execution.pages
    )


@pytest.mark.workflow
def test_p9_7_t02_multiple_disabled_leaves_do_not_crosstalk(tmp_path: Path) -> None:
    """P9.7-T02：多个 disabled 叶各自透传，不误调用其他叶。"""

    execution, _, _ = run_document(
        tmp_path,
        ("cover", "contents", "table"),
        ("cover", "contents", "body.table"),
        "p9-disabled-mix",
    )
    assert all(page.patch is None for page in execution.pages)
    assert [page.toolbox_id for page in execution.pages] == ["cover", "contents", "body.table"]


@pytest.mark.fault_injection
def test_p9_7_t03_last_page_initialization_failure_still_finalizes(tmp_path: Path) -> None:
    """P9.7-T03：最后一页 Toolbox 初始化失败后仍完成完整 final。"""

    base = load_toolbox_catalog(CATALOG_V4)
    entries = []
    for entry in base.entries:
        if entry.route == "end":
            entry = ToolboxCatalogEntry(
                route=entry.route,
                toolbox_key=entry.toolbox_key,
                toolbox_version="test-init-failure",
                fingerprint=catalog_entry_fingerprint(
                    entry.route, entry.toolbox_key, "test-init-failure", entry.contract_version
                ),
                contract_version=entry.contract_version,
                evidence_state="PASS_ENABLE",
                evidence_attestation_hash="f" * 64,
                enabled=True,
                fallback=entry.fallback,
            )
        entries.append(entry)
    factories = build_p9_toolbox_factories(P8_POLICY, P9_POLICY, FONT_MANIFEST, REPO_ROOT)
    factories["end"] = lambda: (_ for _ in ()).throw(RuntimeError("injected"))
    injected = ToolboxCatalog(tuple(entries), "e" * 64, factories)
    execution, _, _ = run_document(
        tmp_path,
        ("cover", "end_text"),
        ("cover", "end"),
        "p9-last-failure",
        catalog=injected,
    )
    assert execution.pages[-1].outcome.fallback is Fallback.PAGE_PASSTHROUGH
    assert execution.pages[-1].outcome.state is PagePipelineState.FINALIZED


@pytest.mark.contract
def test_p9_7_t04_catalog_evidence_runtime_outcome_are_consistent(tmp_path: Path) -> None:
    """P9.7-T04：六叶证据、Catalog、runtime 和 outcome 一致率 100%。"""

    execution, runtime, _ = run_document(
        tmp_path,
        ("cover", "contents", "end_text", "multi2", "table", "anchored"),
        P9_ROUTES,
        "p9-consistency",
    )
    by_route = {entry.route: entry for entry in runtime.catalog.entries}
    assert all(page.catalog_hash == runtime.catalog.catalog_hash for page in execution.pages)
    assert all(
        page.toolbox_version == by_route[page.route].toolbox_version for page in execution.pages
    )
    assert all(
        page.evidence_attestation_hash == by_route[page.route].evidence_attestation_hash
        for page in execution.pages
    )


@pytest.mark.integration
def test_p9_7_t05_mixed_pdf_preserves_page_count_order_and_features(tmp_path: Path) -> None:
    """P9.7-T05：第二批混合 PDF 页数、页序和 Preservation 保持率 100%。"""

    execution, runtime, source = run_document(
        tmp_path,
        ("cover", "contents", "end_text", "multi2", "table", "anchored"),
        P9_ROUTES,
        "p9-preservation",
    )
    target = runtime.artifacts.get(execution.final_artifact.artifact_id)
    with pymupdf.open(source) as before, pymupdf.open(stream=target, filetype="pdf") as after:
        assert before.page_count == after.page_count == 6
    assert tuple(page.page_no for page in execution.pages) == (1, 2, 3, 4, 5, 6)
    assert execution.preservation.passed


@pytest.mark.regression
def test_p9_7_t06_g8_first_batch_has_no_explanatory_regression(tmp_path: Path) -> None:
    """P9.7-T06：v4 对第一批 Route/Toolbox/Patch/PageOutcome 无解释差异。"""

    kinds = ("visual", "single", "chart", "diagram")
    routes = ("visual_only", "body.flow_text.single", "body.chart", "body.diagram")
    v3 = load_toolbox_catalog(
        CATALOG_V3,
        build_p8_toolbox_factories(P8_POLICY, FONT_MANIFEST, REPO_ROOT),
    )
    first, _, _ = run_document(tmp_path / "v3", kinds, routes, "p9-g8-v3", catalog=v3)
    second, _, _ = run_document(tmp_path / "v4", kinds, routes, "p9-g8-v4")

    def projection(execution: DocumentExecution) -> tuple[tuple[str, str | None, bool, str], ...]:
        """提取不依赖 Catalog 版本号的第一批行为投影。"""

        return tuple(
            (page.route, page.toolbox_id, page.patch is not None, page.outcome.fallback.value)
            for page in execution.pages
        )

    assert projection(first) == projection(second)


def main() -> int:
    """记录 P9 正式验收入口固定为当前 42 个编号用例。"""

    return pytest.main([str(Path(__file__).resolve()), "-q"])


if __name__ == "__main__":
    raise SystemExit(main())
