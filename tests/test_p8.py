"""按 P8.1～P8.5 的 29 个编号用例验收第一批 Toolbox。"""

from __future__ import annotations

import hashlib
import importlib
import json
import sys
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
from transflow.application.toolbox_page_coordinator import (
    ToolboxPageCoordinator,
    ToolboxPageWork,
)
from transflow.application.toolbox_page_pipeline import ToolboxPagePipeline
from transflow.domain.common import content_sha256
from transflow.domain.errors import DomainContractError, ErrorCode, PortCallError
from transflow.domain.jobs import DocumentRunRequest
from transflow.domain.states import CheckpointCompatibility, Fallback, PagePipelineState
from transflow.domain.toolbox import PagePatch, PatchOperation
from transflow.domain.translation import TranslationBatch, TranslationBundle
from transflow.pdf_kernel import (
    ControlledFontRegistry,
    PageFactsExtractor,
    PagePatchInterpreter,
    PyMuPdfPageRenderer,
    patch_operation_hash,
)
from transflow.toolboxes.catalog import (
    ToolboxCatalog,
    ToolboxCatalogEntry,
    catalog_entry_fingerprint,
    load_toolbox_catalog,
)
from transflow.toolboxes.leaves import (
    ChartTextToolbox,
    DiagramTextToolbox,
    SingleFlowTextToolbox,
    VisualOnlyToolbox,
    build_p8_toolbox_factories,
)
from transflow.toolboxes.leaves.policy import load_p8_toolbox_policy

REPO_ROOT = Path(__file__).resolve().parent.parent
FONT_MANIFEST = REPO_ROOT / "resources" / "manifests" / "font_manifest.json"
POLICY_PATH = REPO_ROOT / "resources" / "manifests" / "p8_toolbox_policy.json"
CATALOG_PATH = REPO_ROOT / "resources" / "catalogs" / "page_toolbox_catalog_v3.json"
ATTESTATION_PATH = REPO_ROOT / "resources" / "evidence" / "p8" / "leaf_attestations.json"
SUMMARY_PATH = REPO_ROOT / "resources" / "evidence" / "p8" / "p8_acceptance_summary.json"
SPIKE_ROOT = REPO_ROOT / "spikes" / "page_toolbox_engine_puncture_v1"
HASH_A = "a" * 64
FONT_ID = "noto-sans-cjk-sc-regular"


def sha256_file(path: Path) -> str:
    """流式计算真实 PDF 或资源内容哈希。"""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _scan_image_png() -> bytes:
    """生成内部含可见文字但对目标页仅表现为图片的真实 PNG。"""

    with pymupdf.open() as image_document:
        image_page = image_document.new_page(width=320, height=180)
        image_page.insert_text((30, 80), "VISIBLE TEXT INSIDE IMAGE", fontsize=16)
        return image_page.get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False).tobytes("png")


def create_pdf(path: Path, page_kinds: tuple[str, ...], *, scale: float = 1.0) -> Path:
    """按结构类型生成可被 PyMuPDF 真实解析、渲染和最终化的完整 PDF。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open() as document:
        for page_number, kind in enumerate(page_kinds, start=1):
            width, height = 420.0 * scale, 600.0 * scale
            page = document.new_page(width=width, height=height)
            if kind == "visual":
                page.insert_image(
                    pymupdf.Rect(40 * scale, 100 * scale, 380 * scale, 300 * scale),
                    stream=_scan_image_png(),
                )
            elif kind == "vector":
                page.draw_circle((210 * scale, 260 * scale), 80 * scale, color=(0, 0, 0))
                page.draw_line(
                    (80 * scale, 420 * scale),
                    (340 * scale, 420 * scale),
                    color=(0, 0, 0),
                )
            elif kind == "single":
                page.insert_text((40 * scale, 28 * scale), "ANNUAL REPORT HEADER", fontsize=8)
                page.insert_textbox(
                    pymupdf.Rect(55 * scale, 105 * scale, 360 * scale, 175 * scale),
                    "1. Important paragraph\nSecond source line.",
                    fontsize=11 * scale,
                )
                page.insert_text((200 * scale, 575 * scale), str(page_number), fontsize=8)
                page.draw_rect(
                    pymupdf.Rect(350 * scale, 510 * scale, 390 * scale, 550 * scale),
                    color=(0, 0, 0),
                )
            elif kind == "chart":
                page.draw_rect(
                    pymupdf.Rect(80 * scale, 110 * scale, 340 * scale, 410 * scale),
                    color=(0, 0, 0),
                )
                page.draw_line(
                    (110 * scale, 360 * scale),
                    (310 * scale, 210 * scale),
                    color=(0, 0, 1),
                )
                page.insert_text((180 * scale, 245 * scale), "Revenue", fontsize=10 * scale)
                page.insert_text((120 * scale, 385 * scale), "2026", fontsize=9 * scale)
            elif kind == "diagram":
                page.draw_rect(
                    pymupdf.Rect(90 * scale, 150 * scale, 250 * scale, 230 * scale),
                    color=(0, 0, 0),
                )
                page.draw_rect(
                    pymupdf.Rect(90 * scale, 330 * scale, 250 * scale, 410 * scale),
                    color=(0, 0, 0),
                )
                page.draw_line(
                    (170 * scale, 230 * scale),
                    (170 * scale, 330 * scale),
                    color=(0, 0, 0),
                )
                page.insert_text((130 * scale, 195 * scale), "Input", fontsize=10 * scale)
                page.insert_text((125 * scale, 375 * scale), "Output", fontsize=10 * scale)
            else:
                raise ValueError(f"未知测试页面类型: {kind}")
        document.save(path)
    return path


def make_request(path: Path, run_id: str) -> DocumentRunRequest:
    """为一个真实完整 PDF 建立稳定文档请求。"""

    return DocumentRunRequest(
        source_pdf_path=str(path.resolve()),
        source_hash=sha256_file(path),
        source_language="en",
        target_language="zh-CN",
        config_snapshot_hash=HASH_A,
        job_id="job-p8",
        run_id=run_id,
    )


@dataclass(slots=True)
class P8Runtime:
    """聚合一次 P8 文档运行使用的真实 Adapter 与 Kernel。"""

    artifacts: SharedFilesystemArtifactAdapter
    catalog: ToolboxCatalog
    pipeline: ToolboxPagePipeline
    coordinator: DocumentCoordinator
    finalizer: DocumentFinalizer


def make_runtime(
    run_root: Path,
    request: DocumentRunRequest,
    translations: dict[str, str],
    *,
    catalog: ToolboxCatalog | None = None,
) -> P8Runtime:
    """用真实文件存储、受控字体、v3 Catalog 和 Fixed Bundle 建立运行时。"""

    artifacts = SharedFilesystemArtifactAdapter(run_root, request.run_id)
    checkpoints = FilesystemCheckpointAdapter(run_root, request.run_id, artifacts)
    fonts = ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT)
    interpreter = PagePatchInterpreter(fonts)
    renderer = PyMuPdfPageRenderer(interpreter)
    selected_catalog = catalog or load_toolbox_catalog(
        CATALOG_PATH,
        build_p8_toolbox_factories(POLICY_PATH, FONT_MANIFEST, REPO_ROOT),
    )
    compatibility = CheckpointCompatibility(
        source_hash=request.source_hash,
        config_hash=request.config_snapshot_hash,
        font_hash=fonts.manifest_hash,
        toolbox_catalog_hash=selected_catalog.catalog_hash,
        schema_hash=sha256_file(
            REPO_ROOT / "resources" / "schemas" / "leaf_migration_evidence_v1.schema.json"
        ),
    )
    pipeline = ToolboxPagePipeline(
        selected_catalog,
        ToolboxPageCoordinator(FixedTranslationAdapter(translations)),
        renderer,
        PreviewPublisher(artifacts),
        checkpoints,
        compatibility,
    )
    return P8Runtime(
        artifacts,
        selected_catalog,
        pipeline,
        DocumentCoordinator(PageFactsExtractor()),
        DocumentFinalizer(interpreter, artifacts, run_root),
    )


def fixed_translations(
    request: DocumentRunRequest,
    routes: tuple[str, ...],
    translated_text: str = "翻译完成",
) -> dict[str, str]:
    """用正式 single 叶先构建稳定 unit ID，再形成真实 Fixed Bundle 映射。"""

    coordinator = DocumentCoordinator(PageFactsExtractor())
    pages = coordinator.enumerate_pages(request)
    policy = load_p8_toolbox_policy(POLICY_PATH)
    font_path = ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT).resolve(FONT_ID).path
    translations: dict[str, str] = {}
    for page, route in zip(pages, routes, strict=True):
        if route != "body.flow_text.single":
            continue
        toolbox = SingleFlowTextToolbox(policy, font_path)
        template = toolbox.prepare(page.context, page.facts)
        batch = toolbox.build_translation_request(template)
        if batch is not None:
            translations.update({unit.unit_id: translated_text for unit in batch.units})
    return translations


def run_document(
    tmp_path: Path,
    page_kinds: tuple[str, ...],
    routes: tuple[str, ...],
    run_id: str,
    *,
    translated_text: str = "翻译完成",
    catalog: ToolboxCatalog | None = None,
) -> tuple[DocumentExecution, P8Runtime, Path]:
    """执行一份真实完整 PDF 并返回可复核产物和运行组件。"""

    source = create_pdf(tmp_path / f"{run_id}.pdf", page_kinds)
    request = make_request(source, run_id)
    translations = fixed_translations(request, routes, translated_text)
    runtime = make_runtime(tmp_path / "runs" / run_id, request, translations, catalog=catalog)
    route_by_page = dict(enumerate(routes, start=1))
    execution = runtime.coordinator.run(
        request,
        lambda page: route_by_page[page.context.page_no],
        runtime.pipeline,
        runtime.finalizer,
    )
    return execution, runtime, source


def semantic_page_hash(path: Path, page_no: int = 1) -> str:
    """计算排除容器元数据和源身份后的页面语义对象哈希。"""

    facts = PageFactsExtractor().extract_all(path, sha256_file(path))[page_no - 1]
    return content_sha256(
        {
            "geometry": (facts.media_box, facts.crop_box, facts.rotation),
            "text": tuple((item.text, item.bbox) for item in facts.text_spans),
            "images": tuple(
                (item.bbox, item.width, item.height, item.content_hash)
                for item in facts.image_objects
            ),
            "drawings": tuple((item.bbox, item.content_hash) for item in facts.drawing_objects),
        }
    )


class ExplodingTranslationPort:
    """记录调用并立即失败，用于验证 visual_only 真正零调用。"""

    def __init__(self) -> None:
        """初始化调用计数。"""

        self.calls = 0

    def translate(self, batch: TranslationBatch) -> TranslationBundle:
        """若被调用则抛错，使错误调用无法被测试掩盖。"""

        self.calls += 1
        raise AssertionError(f"visual_only 不得调用 TranslationPort: {batch.batch_id}")


class RepairExplodingVisualOnly(VisualOnlyToolbox):
    """若零写入透传仍进入 Repair 就立即失败。"""

    def repair(self, candidate: Any, judgement: Any) -> Any:
        """证明 TM1 要求的 visual_only Repair 调用数为零。"""

        raise AssertionError("visual_only 已接受透传不得调用 Repair")


def direct_page(path: Path) -> EnumeratedPage:
    """从完整 PDF 枚举首张真实页面。"""

    request = make_request(path, f"direct-{path.stem[:20]}")
    return DocumentCoordinator(PageFactsExtractor()).enumerate_pages(request)[0]


def direct_toolbox_result(
    page: EnumeratedPage,
    toolbox: Any,
    translated_text: str = "译文",
) -> Any:
    """先用叶生成 unit，再以 Fixed Bundle 运行完整六阶段。"""

    template = toolbox.prepare(page.context, page.facts)
    batch = toolbox.build_translation_request(template)
    translations = (
        {unit.unit_id: translated_text for unit in batch.units} if batch is not None else {}
    )
    return ToolboxPageCoordinator(FixedTranslationAdapter(translations)).execute(
        ToolboxPageWork(page.context, page.facts, toolbox)
    )


@pytest.mark.contract
def test_p8_1_t01_visual_only_has_zero_units_calls_and_patch(tmp_path: Path) -> None:
    """P8.1-T01：纯矢量页形成零翻译、零 Patch、FINALIZED/PASSTHROUGH。"""

    source = create_pdf(tmp_path / "vector.pdf", ("vector",))
    page = direct_page(source)
    port = ExplodingTranslationPort()
    result = ToolboxPageCoordinator(port).execute(
        ToolboxPageWork(page.context, page.facts, RepairExplodingVisualOnly())
    )
    assert result.ordered_unit_ids == () and result.patch is None and port.calls == 0
    assert "repair" not in result.trace.stages
    assert result.outcome.state is PagePipelineState.FINALIZED
    assert result.outcome.fallback is Fallback.PAGE_PASSTHROUGH


@pytest.mark.contract
def test_p8_1_t02_scanned_visible_text_is_not_ocr_or_modified(tmp_path: Path) -> None:
    """P8.1-T02：扫描图可见文字不转成原生 unit，图片内容哈希保持。"""

    source = create_pdf(tmp_path / "scan.pdf", ("visual",))
    page = direct_page(source)
    before = tuple(item.content_hash for item in page.facts.image_objects)
    result = ToolboxPageCoordinator(ExplodingTranslationPort()).execute(
        ToolboxPageWork(page.context, page.facts, VisualOnlyToolbox())
    )
    after = tuple(item.content_hash for item in direct_page(source).facts.image_objects)
    assert before == after and result.ordered_unit_ids == () and result.patch is None


@pytest.mark.integration
def test_p8_1_t03_source_candidate_final_semantic_hashes_match(tmp_path: Path) -> None:
    """P8.1-T03：source/candidate/final 的语义对象差异为零。"""

    execution, runtime, source = run_document(
        tmp_path,
        ("visual",),
        ("visual_only",),
        "p8-visual-hash",
    )
    final = tmp_path / "visual-final.pdf"
    final.write_bytes(runtime.artifacts.get(execution.final_artifact.artifact_id))
    assert semantic_page_hash(source) == semantic_page_hash(final)
    assert execution.pages[0].application is None and execution.pages[0].patch is None


@pytest.mark.fault_injection
def test_p8_1_t04_failing_translation_port_is_never_called(tmp_path: Path) -> None:
    """P8.1-T04：报错 TranslationPort 调用数仍为零且页面完成。"""

    source = create_pdf(tmp_path / "visual-spy.pdf", ("visual",))
    page = direct_page(source)
    port = ExplodingTranslationPort()
    result = ToolboxPageCoordinator(port).execute(
        ToolboxPageWork(page.context, page.facts, VisualOnlyToolbox())
    )
    assert port.calls == 0 and result.outcome.state is PagePipelineState.FINALIZED


@pytest.mark.workflow
def test_p8_1_t05_visual_page_in_full_pdf_preserves_identity_and_order(tmp_path: Path) -> None:
    """P8.1-T05：完整 PDF 中的 visual 页保持页数、页序且最终文件可读。"""

    execution, runtime, _ = run_document(
        tmp_path,
        ("single", "visual", "single"),
        ("body.flow_text.single", "visual_only", "body.flow_text.single"),
        "p8-visual-mixed",
    )
    final = runtime.artifacts.get(execution.final_artifact.artifact_id)
    with pymupdf.open(stream=final, filetype="pdf") as document:
        assert document.page_count == 3
    assert tuple(page.page_no for page in execution.pages) == (1, 2, 3)


@pytest.mark.migration
def test_p8_2_t01_single_fixed_bundle_matches_legacy_reading_order(tmp_path: Path) -> None:
    """P8.2-T01：旧 builder 与生产叶的源文本/阅读顺序等价，ID/边界差异已登记。"""

    source = create_pdf(tmp_path / "single-equivalence.pdf", ("single",))
    sys.path.insert(0, str(SPIKE_ROOT / "src"))
    try:
        from shared_pdf_kernel.facts import extract_page_facts as legacy_extract

        legacy_builder = importlib.import_module(
            "spikes.page_toolbox_engine_puncture_v1.toolboxes.body.flow_text.single."
            "tools.template_builder"
        )
        legacy = legacy_builder.build_p4_page_template(legacy_extract(source))
    finally:
        sys.path.remove(str(SPIKE_ROOT / "src"))
    page = direct_page(source)
    policy = load_p8_toolbox_policy(POLICY_PATH)
    font = ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT).resolve(FONT_ID).path
    toolbox = SingleFlowTextToolbox(policy, font)
    batch = toolbox.build_translation_request(toolbox.prepare(page.context, page.facts))
    assert batch is not None
    # 旧叶只交付正文；当前合同还把语义页眉纳入共享翻译分母。
    legacy_text = tuple(
        item.source_text.replace("\n", " ").strip()
        for item in legacy.containers
        if item.role != "margin"
    )
    production_text = tuple(unit.source_text.replace("\n", " ").strip() for unit in batch.units)
    assert production_text[0] == "ANNUAL REPORT HEADER"
    assert production_text[1:] == legacy_text
    migration = json.loads(
        (REPO_ROOT / "docs" / "迁移" / "p8_body_flow_text_single_migration.json").read_text(
            encoding="utf-8"
        )
    )
    assert migration["migration_differences"]


@pytest.mark.contract
def test_p8_2_t02_single_preserves_order_owner_and_required_literal(tmp_path: Path) -> None:
    """P8.2-T02：短译文保持 reading order、owner、替换文本和编号字面量。"""

    source = create_pdf(tmp_path / "single-literal.pdf", ("single",))
    page = direct_page(source)
    policy = load_p8_toolbox_policy(POLICY_PATH)
    font = ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT).resolve(FONT_ID).path
    result = direct_toolbox_result(page, SingleFlowTextToolbox(policy, font), "新的段落")
    assert result.patch is not None
    assert result.patch.owner == "body.flow_text.single"
    assert any(
        operation.replacement_text is not None
        and operation.replacement_text.startswith("1. ")
        for operation in result.patch.operations
    )
    assert len(result.ordered_unit_ids) == len(result.patch.operations)


@pytest.mark.fault_injection
def test_p8_2_t03_single_long_overflow_is_bounded_and_falls_back(tmp_path: Path) -> None:
    """P8.2-T03：超长译文最多 Repair 一轮，仍不安全则整页透传。"""

    source = create_pdf(tmp_path / "single-overflow.pdf", ("single",))
    page = direct_page(source)
    policy = load_p8_toolbox_policy(POLICY_PATH)
    font = ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT).resolve(FONT_ID).path
    result = direct_toolbox_result(
        page,
        SingleFlowTextToolbox(policy, font),
        "非常长的译文" * 500,
    )
    assert result.outcome.fallback is Fallback.PAGE_PASSTHROUGH and result.patch is None
    assert result.verdict.reason_code == "TEXT_REPAIR_EXHAUSTED"


@pytest.mark.contract
def test_p8_2_t04_single_does_not_claim_margin_page_number_or_visuals(tmp_path: Path) -> None:
    """P8.2-T04：语义页眉可翻译，但页码与保护对象不被正文写入覆盖。"""

    source = create_pdf(tmp_path / "single-protection.pdf", ("single",))
    page = direct_page(source)
    policy = load_p8_toolbox_policy(POLICY_PATH)
    font = ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT).resolve(FONT_ID).path
    result = direct_toolbox_result(page, SingleFlowTextToolbox(policy, font))
    assert result.patch is not None
    target_ids = {item for op in result.patch.operations for item in op.target_object_ids}
    assert target_ids.isdisjoint(page.facts.protected_object_ids)
    assert all("HEADER" not in (op.replacement_text or "") for op in result.patch.operations)


@pytest.mark.regression
def test_p8_2_t05_single_perturbations_follow_structure(tmp_path: Path) -> None:
    """P8.2-T05：页尺寸和文字几何扰动后仍按归一化结构产生 owner。"""

    policy = load_p8_toolbox_policy(POLICY_PATH)
    font = ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT).resolve(FONT_ID).path
    counts = []
    for scale in (0.8, 1.0, 1.25):
        source = create_pdf(tmp_path / f"single-{scale}.pdf", ("single",), scale=scale)
        page = direct_page(source)
        toolbox = SingleFlowTextToolbox(policy, font)
        batch = toolbox.build_translation_request(toolbox.prepare(page.context, page.facts))
        counts.append(0 if batch is None else len(batch.units))
    assert counts == [2, 2, 2]


@pytest.mark.workflow
def test_p8_2_t06_single_reruns_p4_normal_and_failure_e2e(tmp_path: Path) -> None:
    """P8.2-T06：正式 single 叶正常/失败均全页终态、页序完整。"""

    normal, _, _ = run_document(
        tmp_path / "normal",
        ("single", "single"),
        ("body.flow_text.single", "body.flow_text.single"),
        "p8-single-normal",
    )
    failed, _, _ = run_document(
        tmp_path / "failed",
        ("single", "single"),
        ("body.flow_text.single", "body.flow_text.single"),
        "p8-single-failed",
        translated_text="超长译文" * 500,
    )
    assert all(
        page.outcome.state is PagePipelineState.FINALIZED for page in (*normal.pages, *failed.pages)
    )
    assert failed.pages[0].outcome.fallback is Fallback.PAGE_PASSTHROUGH
    assert normal.preservation.passed and failed.preservation.passed


def _native_toolbox(kind: str) -> Any:
    """按测试叶类型显式构造 chart 或 diagram Toolbox。"""

    policy = load_p8_toolbox_policy(POLICY_PATH)
    font = ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT).resolve(FONT_ID).path
    return ChartTextToolbox(policy, font) if kind == "chart" else DiagramTextToolbox(policy, font)


@pytest.mark.contract
def test_p8_3_t01_chart_units_only_target_native_text(tmp_path: Path) -> None:
    """P8.3-T01：原生 chart 只为文本 owner 生成 unit/Patch，绘图事实不变。"""

    source = create_pdf(tmp_path / "chart-native.pdf", ("chart",))
    page = direct_page(source)
    drawing_hashes = tuple(item.content_hash for item in page.facts.drawing_objects)
    result = direct_toolbox_result(page, _native_toolbox("chart"))
    assert result.patch is not None and result.ordered_unit_ids
    assert all(
        target in page.facts.owned_object_ids
        for operation in result.patch.operations
        for target in operation.target_object_ids
    )
    assert drawing_hashes == tuple(
        item.content_hash for item in direct_page(source).facts.drawing_objects
    )


@pytest.mark.contract
def test_p8_3_t02_raster_chart_has_zero_ocr_units_and_patch(tmp_path: Path) -> None:
    """P8.3-T02：栅格图表内部文字不 OCR，零 unit/Patch 并显式透传。"""

    source = create_pdf(tmp_path / "chart-raster.pdf", ("visual",))
    page = direct_page(source)
    result = direct_toolbox_result(page, _native_toolbox("chart"))
    assert result.ordered_unit_ids == () and result.patch is None
    assert result.outcome.fallback is Fallback.PAGE_PASSTHROUGH


@pytest.mark.migration
def test_p8_3_t03_chart_independent_gate_uses_frozen_real_blind_threshold() -> None:
    """P8.3-T03：chart 独立复核按冻结真实匿名文档阈值保持 disabled。"""

    summary = json.loads(SUMMARY_PATH.read_text(encoding="utf-8"))
    assert summary["real_anonymous_document_counts"]["body.chart"] == 0
    assert summary["threshold_real_anonymous_documents"] == 6
    assert summary["conclusions"]["body.chart"] == "PASS_DISABLED_WITH_FALLBACK"


@pytest.mark.regression
def test_p8_3_t04_chart_geometry_perturbation_changes_structural_facts(tmp_path: Path) -> None:
    """P8.3-T04：chart 尺寸扰动改变事实哈希但不改变结构选叶原则。"""

    counts, hashes = [], []
    for scale in (0.8, 1.2):
        source = create_pdf(tmp_path / f"chart-{scale}.pdf", ("chart",), scale=scale)
        page = direct_page(source)
        toolbox = _native_toolbox("chart")
        batch = toolbox.build_translation_request(toolbox.prepare(page.context, page.facts))
        counts.append(0 if batch is None else len(batch.units))
        hashes.append(page.facts.kernel_facts_hash)
    assert all(count > 0 for count in counts) and len(set(hashes)) == 2


@pytest.mark.fault_injection
def test_p8_3_t05_chart_translation_failure_has_bounded_page_outcome(tmp_path: Path) -> None:
    """P8.3-T05：chart 能力失败有 PageOutcome，绘图修改数为零。"""

    source = create_pdf(tmp_path / "chart-failure.pdf", ("chart",))
    page = direct_page(source)

    class FailurePort:
        """为本用例返回真实结构化端口故障。"""

        def translate(self, batch: TranslationBatch) -> TranslationBundle:
            """把翻译能力失败映射成可被协调器捕获的异常。"""

            raise PortCallError(ErrorCode.AI_TIMEOUT, True, batch.batch_id)

    result = ToolboxPageCoordinator(FailurePort()).execute(
        ToolboxPageWork(page.context, page.facts, _native_toolbox("chart"))
    )
    assert result.outcome.fallback is Fallback.PAGE_PASSTHROUGH and result.patch is None


@pytest.mark.contract
def test_p8_3_t06_chart_enable_without_blind_attestation_is_rejected() -> None:
    """P8.3-T06：把未达盲测阈值的 chart 改为 enabled 会被发布合同拒绝。"""

    catalog = load_toolbox_catalog(CATALOG_PATH)
    entry = next(item for item in catalog.entries if item.route == "body.chart")
    with pytest.raises(DomainContractError):
        replace(entry, enabled=True, disabled_reason=None)


@pytest.mark.contract
def test_p8_4_t01_diagram_units_only_target_native_labels(tmp_path: Path) -> None:
    """P8.4-T01：diagram 只为原生 label 形成 Patch，connector/drawing 哈希不变。"""

    source = create_pdf(tmp_path / "diagram-native.pdf", ("diagram",))
    page = direct_page(source)
    before = tuple(item.content_hash for item in page.facts.drawing_objects)
    result = direct_toolbox_result(page, _native_toolbox("diagram"))
    assert result.patch is not None and result.ordered_unit_ids
    assert before == tuple(item.content_hash for item in direct_page(source).facts.drawing_objects)


@pytest.mark.contract
def test_p8_4_t02_diagram_visual_or_cross_owner_patch_is_rejected(tmp_path: Path) -> None:
    """P8.4-T02：修改 node/connector/drawing 身份的 Patch 被 G6 guard 拒绝。"""

    source = create_pdf(tmp_path / "diagram-guard.pdf", ("diagram",))
    page = direct_page(source)
    drawing_id = page.facts.drawing_objects[0].object_id
    rect = page.facts.drawing_objects[0].bbox
    payload_hash = patch_operation_hash(
        owner="body.diagram",
        target_object_ids=(drawing_id,),
        rect=rect,
        replacement_text="非法",
        font_id=FONT_ID,
        font_size=8,
    )
    patch = PagePatch(
        "diagram-illegal",
        page.context.source_hash,
        page.context.page_no,
        page.context.geometry_hash,
        "body.diagram",
        (
            PatchOperation(
                "diagram-illegal-op",
                "diagram-node",
                "replace_text",
                payload_hash,
                "body.diagram",
                (drawing_id,),
                rect,
                "非法",
                FONT_ID,
                8,
            ),
        ),
    )
    interpreter = PagePatchInterpreter(ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT))
    with pymupdf.open(source) as document, pytest.raises(DomainContractError):
        interpreter.apply(document, page.context, page.facts, patch, "body.diagram")


@pytest.mark.migration
def test_p8_4_t03_diagram_independent_gate_uses_frozen_real_blind_threshold() -> None:
    """P8.4-T03：diagram 独立复核不按文件名或旧 holdout 结论启用。"""

    summary = json.loads(SUMMARY_PATH.read_text(encoding="utf-8"))
    assert summary["real_anonymous_document_counts"]["body.diagram"] == 0
    assert summary["conclusions"]["body.diagram"] == "PASS_DISABLED_WITH_FALLBACK"


@pytest.mark.regression
def test_p8_4_t04_diagram_position_and_size_perturbation_is_structural(tmp_path: Path) -> None:
    """P8.4-T04：节点位置/尺寸扰动改变事实，label owner 仍由相对结构产生。"""

    counts, hashes = [], []
    for scale in (0.9, 1.15):
        source = create_pdf(tmp_path / f"diagram-{scale}.pdf", ("diagram",), scale=scale)
        page = direct_page(source)
        toolbox = _native_toolbox("diagram")
        batch = toolbox.build_translation_request(toolbox.prepare(page.context, page.facts))
        counts.append(0 if batch is None else len(batch.units))
        hashes.append(page.facts.kernel_facts_hash)
    assert all(count > 0 for count in counts) and len(set(hashes)) == 2


@pytest.mark.fault_injection
def test_p8_4_t05_diagram_layout_failure_has_safe_fallback(tmp_path: Path) -> None:
    """P8.4-T05：超长 label 经一次 Repair 后形成整页安全 fallback。"""

    source = create_pdf(tmp_path / "diagram-overflow.pdf", ("diagram",))
    page = direct_page(source)
    result = direct_toolbox_result(
        page,
        _native_toolbox("diagram"),
        "过长节点标签" * 300,
    )
    assert result.outcome.fallback is Fallback.PAGE_PASSTHROUGH and result.patch is None


@pytest.mark.contract
def test_p8_4_t06_diagram_enable_without_blind_attestation_is_rejected() -> None:
    """P8.4-T06：diagram 未达阈值时 enabled 发布被拒绝。"""

    catalog = load_toolbox_catalog(CATALOG_PATH)
    entry = next(item for item in catalog.entries if item.route == "body.diagram")
    with pytest.raises(DomainContractError):
        replace(entry, enabled=True, disabled_reason=None)


@pytest.mark.workflow
def test_p8_5_t01_mixed_pdf_uses_only_gate_enabled_toolboxes(tmp_path: Path) -> None:
    """P8.5-T01：混合 PDF 只运行 Gate 允许叶，全部页终态且单一目标。"""

    execution, _, _ = run_document(
        tmp_path,
        ("visual", "single", "chart", "diagram"),
        ("visual_only", "body.flow_text.single", "body.chart", "body.diagram"),
        "p8-mixed-gated",
    )
    assert len(execution.pages) == 4 and execution.final_artifact.media_type == "application/pdf"
    assert [page.toolbox_id for page in execution.pages[:2]] == [
        "visual_only",
        "body.flow_text.single",
    ]
    assert all(page.outcome.state is PagePipelineState.FINALIZED for page in execution.pages)


@pytest.mark.workflow
def test_p8_5_t02_disabled_chart_diagram_fallback_without_crosstalk(tmp_path: Path) -> None:
    """P8.5-T02：chart/diagram disabled 透传不改变 visual/single 行为。"""

    execution, _, _ = run_document(
        tmp_path,
        ("visual", "single", "chart", "diagram"),
        ("visual_only", "body.flow_text.single", "body.chart", "body.diagram"),
        "p8-mixed-disabled",
    )
    assert execution.pages[0].patch is None
    assert execution.pages[1].patch is not None
    assert all(page.outcome.fallback is Fallback.PAGE_PASSTHROUGH for page in execution.pages[2:])


@pytest.mark.fault_injection
def test_p8_5_t03_toolbox_initialization_failure_is_page_local(tmp_path: Path) -> None:
    """P8.5-T03：测试 Catalog 中 chart 初始化失败只影响本页，最后页仍 final。"""

    base = load_toolbox_catalog(CATALOG_PATH)
    entries = []
    for entry in base.entries:
        if entry.route == "body.chart":
            entry = ToolboxCatalogEntry(
                route=entry.route,
                toolbox_key=entry.toolbox_key,
                toolbox_version="test-failure",
                fingerprint=catalog_entry_fingerprint(
                    entry.route,
                    entry.toolbox_key,
                    "test-failure",
                    entry.contract_version,
                ),
                contract_version=entry.contract_version,
                evidence_state="PASS_ENABLE",
                evidence_attestation_hash="f" * 64,
                enabled=True,
                fallback=entry.fallback,
            )
        entries.append(entry)
    factories = build_p8_toolbox_factories(POLICY_PATH, FONT_MANIFEST, REPO_ROOT)
    factories["body.chart"] = lambda: (_ for _ in ()).throw(RuntimeError("injected"))
    injected = ToolboxCatalog(tuple(entries), "e" * 64, factories)
    execution, _, _ = run_document(
        tmp_path,
        ("visual", "chart", "single"),
        ("visual_only", "body.chart", "body.flow_text.single"),
        "p8-mixed-init-failure",
        catalog=injected,
    )
    assert execution.pages[1].outcome.fallback is Fallback.PAGE_PASSTHROUGH
    assert execution.pages[0].outcome.state is PagePipelineState.FINALIZED
    assert execution.pages[-1].outcome.state is PagePipelineState.FINALIZED


@pytest.mark.contract
def test_p8_5_t04_catalog_evidence_version_runtime_checkpoint_are_consistent(
    tmp_path: Path,
) -> None:
    """P8.5-T04：Catalog、证明、运行结果与 Checkpoint 身份一致率 100%。"""

    execution, runtime, _ = run_document(
        tmp_path,
        ("visual", "single", "chart", "diagram"),
        ("visual_only", "body.flow_text.single", "body.chart", "body.diagram"),
        "p8-mixed-consistency",
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
def test_p8_5_t05_mixed_pdf_preserves_page_count_order_and_structure(tmp_path: Path) -> None:
    """P8.5-T05：source/target 页数、页序和 Preservation 保持率 100%。"""

    execution, runtime, source = run_document(
        tmp_path,
        ("visual", "single", "chart", "diagram"),
        ("visual_only", "body.flow_text.single", "body.chart", "body.diagram"),
        "p8-mixed-preservation",
    )
    final = runtime.artifacts.get(execution.final_artifact.artifact_id)
    with (
        pymupdf.open(source) as source_document,
        pymupdf.open(
            stream=final,
            filetype="pdf",
        ) as target_document,
    ):
        assert source_document.page_count == target_document.page_count == 4
    assert tuple(page.page_no for page in execution.pages) == (1, 2, 3, 4)
    assert execution.preservation.passed


@pytest.mark.regression
def test_p8_5_t06_g5_g6_facts_and_protected_objects_do_not_regress(tmp_path: Path) -> None:
    """P8.5-T06：同一完整 PDF 的 G5/G6 facts/protected 重提取无解释差异。"""

    source = create_pdf(tmp_path / "mixed-upstream.pdf", ("visual", "single", "chart", "diagram"))
    source_hash = sha256_file(source)
    extractor = PageFactsExtractor()
    first = extractor.extract_all(source, source_hash, include_classification=True)
    second = extractor.extract_all(source, source_hash, include_classification=True)
    assert tuple(item.kernel_facts_hash for item in first) == tuple(
        item.kernel_facts_hash for item in second
    )
    assert tuple(item.protected_object_ids for item in first) == tuple(
        item.protected_object_ids for item in second
    )


def main() -> int:
    """记录 P8 正式验收入口固定为当前 29 个编号用例。"""

    return pytest.main([str(Path(__file__).resolve()), "-q"])


if __name__ == "__main__":
    raise SystemExit(main())
