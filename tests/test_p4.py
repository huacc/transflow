"""按 P4.0～P4.5 验收首条完整 PDF 纵向闭环。"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pymupdf
import pytest

from scripts import verify_p4
from tests.support.fixed_routes import FixedRouteFixture
from transflow.adapters.ai.fixed import DeterministicTranslationAdapter, FixedTranslationAdapter
from transflow.adapters.filesystem.artifact_store import SharedFilesystemArtifactAdapter
from transflow.adapters.filesystem.checkpoint import FilesystemCheckpointAdapter
from transflow.adapters.filesystem.common import InjectedCrash
from transflow.application.contracts import EnumeratedPage, ProcessedPage
from transflow.application.document_coordinator import DocumentCoordinator
from transflow.application.document_finalizer import DocumentFinalizer
from transflow.application.page_pipeline import (
    ROUTE_PASSTHROUGH,
    ROUTE_SINGLE,
    ROUTE_VISUAL_ONLY,
    MinimalPagePipeline,
    PreviewPublisher,
    build_unit_id,
)
from transflow.domain.errors import DomainContractError, ErrorCode, PortCallError
from transflow.domain.jobs import DocumentRunRequest
from transflow.domain.pages import PageOutcome
from transflow.domain.states import (
    ArtifactIntegrity,
    ArtifactProduced,
    Capability,
    CheckpointCompatibility,
    DocumentOutcome,
    Fallback,
    PagePipelineState,
    Quality,
    TranslationCoverage,
)
from transflow.domain.toolbox import PagePatch, PatchOperation
from transflow.pdf_kernel import (
    INTERPRETER_ID,
    ControlledFontRegistry,
    PageFactsExtractor,
    PagePatchInterpreter,
    PyMuPdfPageRenderer,
    patch_operation_hash,
)
from transflow.ports.translation import TranslationPort

REPO_ROOT = Path(__file__).resolve().parent.parent
FONT_MANIFEST = REPO_ROOT / "resources" / "manifests" / "font_manifest.json"
ANNUAL_FIXTURE_MANIFEST = REPO_ROOT / "resources" / "manifests" / "p4_e2e_fixture.json"
FONT_ID = "noto-sans-cjk-sc-regular"
HASH_A = "a" * 64
HASH_B = "b" * 64


def sha256_file(path: Path) -> str:
    """流式计算测试 PDF 或 manifest 的真实 SHA-256。"""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_pdf(
    path: Path,
    page_specs: tuple[dict[str, Any], ...],
) -> Path:
    """根据输入规格生成真实多页 PDF，可包含 rotation、CropBox 和保护绘图。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open() as document:
        for index, spec in enumerate(page_specs, start=1):
            width = float(spec.get("width", 420.0))
            height = float(spec.get("height", 600.0))
            page = document.new_page(width=width, height=height)
            text = str(spec.get("text", f"Page {index} source text"))
            page.insert_textbox(
                pymupdf.Rect(40, 50, width - 40, 130),
                text,
                fontsize=11,
                fontname="helv",
            )
            if spec.get("drawing"):
                page.draw_rect(
                    pymupdf.Rect(width - 100, height - 100, width - 40, height - 40),
                    color=(0, 0, 0),
                )
            crop = spec.get("crop")
            if crop is not None:
                page.set_cropbox(pymupdf.Rect(crop))
            page.set_rotation(int(spec.get("rotation", 0)))
        document.save(path)
    return path


def make_request(path: Path, run_id: str = "run-p4") -> DocumentRunRequest:
    """为一份真实完整 PDF 构造稳定的 DocumentRunRequest。"""

    return DocumentRunRequest(
        source_pdf_path=str(path.resolve()),
        source_hash=sha256_file(path),
        source_language="en",
        target_language="zh-CN",
        config_snapshot_hash=HASH_A,
        job_id="job-p4",
        run_id=run_id,
    )


@dataclass(slots=True)
class RuntimeHarness:
    """聚合一次 P4 测试运行所需的真实 Adapter、Kernel 和应用组件。"""

    run_root: Path
    artifacts: SharedFilesystemArtifactAdapter
    checkpoints: FilesystemCheckpointAdapter
    fonts: ControlledFontRegistry
    interpreter: PagePatchInterpreter
    renderer: PyMuPdfPageRenderer
    pipeline: MinimalPagePipeline
    finalizer: DocumentFinalizer
    coordinator: DocumentCoordinator


def make_runtime(
    tmp_path: Path,
    request: DocumentRunRequest,
    translation: TranslationPort,
) -> RuntimeHarness:
    """用真实文件 Adapter 和受控字体建立当前 Run 的最小纵向运行时。"""

    run_root = tmp_path / "runs" / request.run_id
    artifacts = SharedFilesystemArtifactAdapter(run_root, request.run_id)
    checkpoints = FilesystemCheckpointAdapter(run_root, request.run_id, artifacts)
    fonts = ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT)
    interpreter = PagePatchInterpreter(fonts)
    renderer = PyMuPdfPageRenderer(interpreter)
    compatibility = CheckpointCompatibility(
        source_hash=request.source_hash,
        config_hash=request.config_snapshot_hash,
        font_hash=sha256_file(FONT_MANIFEST),
        toolbox_catalog_hash=HASH_B,
        schema_hash=HASH_B,
    )
    pipeline = MinimalPagePipeline(
        translation,
        renderer,
        interpreter,
        PreviewPublisher(artifacts),
        checkpoints,
        compatibility,
        FONT_ID,
    )
    return RuntimeHarness(
        run_root,
        artifacts,
        checkpoints,
        fonts,
        interpreter,
        renderer,
        pipeline,
        DocumentFinalizer(interpreter, artifacts, run_root),
        DocumentCoordinator(PageFactsExtractor()),
    )


def make_short_translation(page: EnumeratedPage, text: str = "测试译文") -> dict[str, str]:
    """为页面首个真实文本对象建立 FixedTranslationAdapter 映射。"""

    text_object = next(item for item in page.facts.objects if not item.protected and item.text)
    return {build_unit_id(page, text_object.object_id): text}


def make_patch(page: EnumeratedPage, replacement_text: str = "测试译文") -> PagePatch:
    """基于页面首个真实文本对象构造可由唯一解释器执行的声明式 Patch。"""

    text_object = next(item for item in page.facts.objects if not item.protected and item.text)
    font_size = 10.0
    payload_hash = patch_operation_hash(
        owner=ROUTE_SINGLE,
        target_object_ids=(text_object.object_id,),
        rect=text_object.bbox,
        replacement_text=replacement_text,
        font_id=FONT_ID,
        font_size=font_size,
    )
    operation = PatchOperation(
        operation_id="operation-p4",
        region_id="region-p4",
        kind="replace_text",
        payload_hash=payload_hash,
        owner=ROUTE_SINGLE,
        target_object_ids=(text_object.object_id,),
        rect=text_object.bbox,
        replacement_text=replacement_text,
        font_id=FONT_ID,
        font_size=font_size,
    )
    return PagePatch(
        patch_id="patch-p4",
        source_hash=page.context.source_hash,
        page_no=page.context.page_no,
        geometry_hash=page.context.geometry_hash,
        owner=ROUTE_SINGLE,
        operations=(operation,),
    )


def content_streams(document: pymupdf.Document, page_no: int) -> tuple[bytes, ...]:
    """读取指定页全部真实内容流，用于证明拒绝前没有发生部分写入。"""

    page = document[page_no - 1]
    return tuple(document.xref_stream(xref) for xref in page.get_contents())


def final_path(harness: RuntimeHarness, execution: Any) -> Path:
    """把最终 Artifact 的 Run 相对路径解析为真实文件路径。"""

    relative = execution.final_artifact.relative_path
    assert relative is not None
    return harness.run_root / relative


@pytest.mark.contract
def test_p4_0_t01_candidate_and_final_use_same_interpreter_and_order(tmp_path: Path) -> None:
    """P4.0-T01：candidate 与 final 对同一 Patch 返回同一解释器、顺序和 owner。"""

    source = create_pdf(tmp_path / "source.pdf", ({"text": "Revenue increased"},))
    request = make_request(source)
    coordinator = DocumentCoordinator(PageFactsExtractor())
    page = coordinator.enumerate_pages(request)[0]
    harness = make_runtime(tmp_path, request, FixedTranslationAdapter(make_short_translation(page)))
    patch = make_patch(page)
    candidate = harness.renderer.render_candidate(
        source, page.context, page.facts, patch, ROUTE_SINGLE
    )
    replay_path = tmp_path / "replay.pdf"
    shutil.copyfile(source, replay_path)
    with pymupdf.open(replay_path) as document:
        replay = harness.interpreter.apply(document, page.context, page.facts, patch, ROUTE_SINGLE)
        document.saveIncr()
    assert candidate.application == replay
    assert replay.interpreter_id == INTERPRETER_ID
    assert replay.operation_ids == ("operation-p4",)
    PyMuPdfPageRenderer.validate_png(candidate.png_bytes)


@pytest.mark.contract
def test_p4_0_t02_all_binding_and_protected_failures_write_zero_operations(
    tmp_path: Path,
) -> None:
    """P4.0-T02：跨 owner、保护对象、错误源哈希和几何均在写入前拒绝。"""

    source = create_pdf(
        tmp_path / "guard.pdf",
        ({"text": "Guarded source", "drawing": True},),
    )
    request = make_request(source)
    page = DocumentCoordinator(PageFactsExtractor()).enumerate_pages(request)[0]
    harness = make_runtime(tmp_path, request, DeterministicTranslationAdapter())
    patch = make_patch(page)
    protected = next(item for item in page.facts.objects if item.protected)
    protected_hash = patch_operation_hash(
        owner=ROUTE_SINGLE,
        target_object_ids=(protected.object_id,),
        rect=protected.bbox,
        replacement_text="禁止写入",
        font_id=FONT_ID,
        font_size=8.0,
    )
    protected_operation = replace(
        patch.operations[0],
        target_object_ids=(protected.object_id,),
        rect=protected.bbox,
        replacement_text="禁止写入",
        font_size=8.0,
        payload_hash=protected_hash,
    )
    candidates = (
        (replace(patch, owner="body.table"), ROUTE_SINGLE),
        (replace(patch, operations=(protected_operation,)), ROUTE_SINGLE),
        (replace(patch, source_hash="c" * 64), ROUTE_SINGLE),
        (replace(patch, geometry_hash="d" * 64), ROUTE_SINGLE),
    )
    with pymupdf.open(source) as document:
        before = content_streams(document, 1)
        for candidate, expected_owner in candidates:
            with pytest.raises(DomainContractError):
                harness.interpreter.apply(
                    document,
                    page.context,
                    page.facts,
                    candidate,
                    expected_owner,
                )
            assert content_streams(document, 1) == before


@pytest.mark.contract
def test_p4_0_t03_only_manifest_font_is_usable_without_system_probe(tmp_path: Path) -> None:
    """P4.0-T03：登记字体可加载，未登记字体被拒绝且系统探测保持为零。"""

    registry = ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT)
    assert registry.resolve(FONT_ID).path.is_file()
    with pytest.raises(PortCallError) as captured:
        registry.resolve("host-system-font")
    assert captured.value.code is ErrorCode.FONT_NOT_REGISTERED
    assert registry.system_probe_count == 0


@pytest.mark.integration
def test_p4_0_t04_mixed_replay_preserves_all_page_structure(tmp_path: Path) -> None:
    """P4.0-T04：Patch/透传混合回放保持页数、页序、框和旋转百分之百。"""

    source = create_pdf(
        tmp_path / "mixed.pdf",
        (
            {"text": "Page one"},
            {"text": "Page two", "rotation": 90},
            {"text": "Page three", "crop": (15, 20, 390, 560)},
        ),
    )
    request = make_request(source)
    coordinator = DocumentCoordinator(PageFactsExtractor())
    pages = coordinator.enumerate_pages(request)
    harness = make_runtime(
        tmp_path, request, FixedTranslationAdapter(make_short_translation(pages[0]))
    )
    router = FixedRouteFixture(
        {
            pages[0].facts.page_identity: ROUTE_SINGLE,
            pages[1].facts.page_identity: ROUTE_VISUAL_ONLY,
        }
    )
    execution = coordinator.run(request, router, harness.pipeline, harness.finalizer)
    assert execution.preservation.passed
    assert execution.preservation.page_count_rate == 1.0
    assert execution.preservation.page_order_rate == 1.0
    assert execution.preservation.geometry_rate == 1.0
    with pymupdf.open(final_path(harness, execution)) as document:
        assert document.page_count == 3


@pytest.mark.contract
def test_p4_0_t05_kernel_and_runtime_forbidden_implementation_count_is_zero() -> None:
    """P4.0-T05：临时内核、第二解释器和禁止渲染/合并调用扫描命中为零。"""

    assert verify_p4.kernel_boundary_violations() == []


@pytest.mark.contract
def test_p4_1_t01_three_pages_are_one_based_ordered_and_geometry_exact(tmp_path: Path) -> None:
    """P4.1-T01：三页 PDF 产生 1/2/3 上下文且页面尺寸与源一致。"""

    source = create_pdf(
        tmp_path / "three.pdf",
        ({"width": 400}, {"width": 410}, {"width": 420}),
    )
    request = make_request(source)
    pages = DocumentCoordinator(PageFactsExtractor()).enumerate_pages(request)
    assert tuple(item.context.page_no for item in pages) == (1, 2, 3)
    with pymupdf.open(source) as document:
        assert tuple(item.facts.page.width_points for item in pages) == tuple(
            float(page.rect.width) for page in document
        )


@pytest.mark.contract
def test_p4_1_t02_rotation_and_cropbox_are_preserved_in_context(tmp_path: Path) -> None:
    """P4.1-T02：旋转与非默认 CropBox 完整进入可序列化页面事实。"""

    source = create_pdf(
        tmp_path / "geometry.pdf",
        ({"rotation": 90, "crop": (10, 20, 390, 560)},),
    )
    page = DocumentCoordinator(PageFactsExtractor()).enumerate_pages(make_request(source))[0]
    assert page.facts.rotation == 90
    assert page.facts.crop_box == (10.0, 20.0, 390.0, 560.0)
    assert page.context.geometry_hash == page.facts.page.geometry_hash


@pytest.mark.fault_injection
def test_p4_1_t03_source_change_during_enumeration_aborts_run(tmp_path: Path) -> None:
    """P4.1-T03：枚举真实结果后源文件字节变化会被后置哈希复核中止。"""

    source = create_pdf(tmp_path / "changing.pdf", ({}, {}))
    request = make_request(source)

    class MutatingExtractor(PageFactsExtractor):
        """在返回真实提取结果前修改输入文件，用于触发竞态窗口。"""

        def extract_all(self, source_path: Path, expected_hash: str) -> tuple[Any, ...]:
            """调用真实提取器后追加字节，使 Coordinator 的后置检查失败。"""

            result = super().extract_all(source_path, expected_hash)
            with source_path.open("ab") as stream:
                stream.write(b"source-changed")
            return result

    with pytest.raises(PortCallError) as captured:
        DocumentCoordinator(MutatingExtractor()).enumerate_pages(request)
    assert captured.value.code is ErrorCode.SOURCE_CHANGED_DURING_RUN


@pytest.mark.contract
def test_p4_2_t01_explicit_visual_and_single_routes_hit_only_declared_pages(
    tmp_path: Path,
) -> None:
    """P4.2-T01：fixture 声明的 visual_only/single 只命中对应稳定页面身份。"""

    source = create_pdf(tmp_path / "routes.pdf", ({}, {}, {}))
    pages = DocumentCoordinator(PageFactsExtractor()).enumerate_pages(make_request(source))
    router = FixedRouteFixture(
        {
            pages[0].facts.page_identity: ROUTE_VISUAL_ONLY,
            pages[1].facts.page_identity: ROUTE_SINGLE,
        }
    )
    assert tuple(router(page) for page in pages) == (
        ROUTE_VISUAL_ONLY,
        ROUTE_SINGLE,
        ROUTE_PASSTHROUGH,
    )


@pytest.mark.contract
def test_p4_2_t02_undeclared_page_is_always_passthrough(tmp_path: Path) -> None:
    """P4.2-T02：空 fixture 对每个未声明页面都返回透传。"""

    source = create_pdf(tmp_path / "default.pdf", ({}, {}))
    pages = DocumentCoordinator(PageFactsExtractor()).enumerate_pages(make_request(source))
    router = FixedRouteFixture({})
    assert all(router(page) == ROUTE_PASSTHROUGH for page in pages)


@pytest.mark.contract
def test_p4_2_t03_rename_and_identity_labels_do_not_change_routes(tmp_path: Path) -> None:
    """P4.2-T03：改文件名、公司标签和 sample 标签不会改变基于内容的 Route。"""

    source = create_pdf(tmp_path / "company-a-sample-1.pdf", ({}, {}))
    renamed = tmp_path / "company-b-sample-999.pdf"
    shutil.copyfile(source, renamed)
    first_pages = DocumentCoordinator(PageFactsExtractor()).enumerate_pages(make_request(source))
    second_pages = DocumentCoordinator(PageFactsExtractor()).enumerate_pages(
        make_request(renamed, "run-renamed")
    )
    router = FixedRouteFixture({first_pages[0].facts.page_identity: ROUTE_SINGLE})
    assert tuple(router(page) for page in first_pages) == tuple(
        router(page) for page in second_pages
    )


@pytest.mark.contract
def test_p4_2_t04_production_wiring_has_no_fixed_route_fixture() -> None:
    """P4.2-T04：production 源码不存在测试固定路由入口。"""

    assert verify_p4.production_route_violations() == []


@pytest.mark.workflow
def test_p4_3_t01_single_page_completes_unit_bundle_patch_judge_and_preview(
    tmp_path: Path,
) -> None:
    """P4.3-T01：single 页完整产生 Unit、Bundle、Patch、Judge 和 FINALIZED 预览。"""

    source = create_pdf(tmp_path / "single.pdf", ({"text": "Short source"},))
    request = make_request(source)
    page = DocumentCoordinator(PageFactsExtractor()).enumerate_pages(request)[0]
    harness = make_runtime(tmp_path, request, FixedTranslationAdapter(make_short_translation(page)))
    result = harness.pipeline.execute(source, page, ROUTE_SINGLE)
    assert result.outcome.state is PagePipelineState.FINALIZED
    assert result.unit_ids == result.translated_unit_ids
    assert result.patch is not None and result.application is not None
    assert result.application.fits and result.outcome.fallback is Fallback.NONE
    assert result.preview is not None and harness.artifacts.verify(result.preview)
    PyMuPdfPageRenderer.validate_png(harness.artifacts.get(result.preview.artifact_id))


@pytest.mark.workflow
def test_p4_3_t02_visual_only_has_no_units_and_passthroughs_source(tmp_path: Path) -> None:
    """P4.3-T02：visual_only 不构造 TranslationUnit，以原页预览进入 FINALIZED。"""

    source = create_pdf(tmp_path / "visual.pdf", ({},))
    request = make_request(source)
    page = DocumentCoordinator(PageFactsExtractor()).enumerate_pages(request)[0]
    harness = make_runtime(tmp_path, request, DeterministicTranslationAdapter())
    result = harness.pipeline.execute(source, page, ROUTE_VISUAL_ONLY)
    assert result.unit_ids == () and result.patch is None
    assert result.outcome.state is PagePipelineState.FINALIZED
    assert result.outcome.fallback is Fallback.PAGE_PASSTHROUGH
    assert result.preview is not None


@pytest.mark.workflow
def test_p4_3_t03_translation_missing_and_judge_failure_both_finalize(
    tmp_path: Path,
) -> None:
    """P4.3-T03：译文缺失和文本框 Judge 失败均降级为完整原页终态。"""

    for run_id, translations in (
        ("run-missing", {}),
        ("run-overflow", None),
    ):
        source = create_pdf(tmp_path / f"{run_id}.pdf", ({"text": "Compact"},))
        request = make_request(source, run_id)
        page = DocumentCoordinator(PageFactsExtractor()).enumerate_pages(request)[0]
        mapping = (
            make_short_translation(page, "超长译文" * 1000)
            if translations is None
            else translations
        )
        harness = make_runtime(tmp_path, request, FixedTranslationAdapter(mapping))
        result = harness.pipeline.execute(source, page, ROUTE_SINGLE)
        assert result.outcome.state is PagePipelineState.FINALIZED
        assert result.patch is None
        assert result.outcome.fallback is Fallback.PAGE_PASSTHROUGH


@pytest.mark.fault_injection
def test_p4_3_t04_invalid_or_partial_png_never_commits_preview_pointer(tmp_path: Path) -> None:
    """P4.3-T04：PNG 解码失败或 rename 前崩溃均不提交预览指针。"""

    source = create_pdf(tmp_path / "preview.pdf", ({},))
    request = make_request(source)
    harness = make_runtime(tmp_path, request, DeterministicTranslationAdapter())
    publisher = PreviewPublisher(harness.artifacts)
    assert publisher.publish(1, b"not-a-png") is None
    valid_png = harness.renderer.render_passthrough(source, 1).png_bytes
    assert publisher.publish(1, valid_png, crash_at="before_artifact_rename") is None
    assert list(harness.run_root.rglob("*.partial")) == []
    manifest = json.loads(
        (harness.run_root / "job" / "artifact_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["entries"] == {}


@pytest.mark.integration
def test_p4_4_t01_mixed_patch_and_passthrough_produce_one_complete_pdf(tmp_path: Path) -> None:
    """P4.4-T01：混合批准 Patch 与透传页最终化为单一结构完整 PDF。"""

    source = create_pdf(tmp_path / "final-mixed.pdf", ({}, {}, {}))
    request = make_request(source)
    coordinator = DocumentCoordinator(PageFactsExtractor())
    pages = coordinator.enumerate_pages(request)
    harness = make_runtime(
        tmp_path, request, FixedTranslationAdapter(make_short_translation(pages[0]))
    )
    router = FixedRouteFixture({pages[0].facts.page_identity: ROUTE_SINGLE})
    execution = coordinator.run(request, router, harness.pipeline, harness.finalizer)
    assert execution.preservation.passed
    assert execution.final_artifact.media_type == "application/pdf"
    with pymupdf.open(final_path(harness, execution)) as document:
        assert document.page_count == len(pages)


@pytest.mark.integration
def test_p4_4_t02_finalizer_sorts_shuffled_results_and_rejects_incomplete(
    tmp_path: Path,
) -> None:
    """P4.4-T02：乱序结果按 page_no 回放，缺页清单在写入前拒绝。"""

    source = create_pdf(tmp_path / "shuffled.pdf", ({}, {}, {}))
    request = make_request(source)
    coordinator = DocumentCoordinator(PageFactsExtractor())
    pages = coordinator.enumerate_pages(request)
    harness = make_runtime(tmp_path, request, DeterministicTranslationAdapter())
    results = tuple(harness.pipeline.execute(source, page, ROUTE_PASSTHROUGH) for page in pages)
    finalization = harness.finalizer.finalize(request, pages, tuple(reversed(results)))
    assert finalization.preservation.passed
    second_request = replace(request, run_id="run-incomplete")
    second_harness = make_runtime(tmp_path, second_request, DeterministicTranslationAdapter())
    with pytest.raises(DomainContractError) as captured:
        second_harness.finalizer.finalize(second_request, pages, results[:-1])
    assert captured.value.code is ErrorCode.DOCUMENT_NOT_FINALIZABLE


@pytest.mark.fault_injection
def test_p4_4_t03_patch_replay_failure_publishes_valid_source_copy(tmp_path: Path) -> None:
    """P4.4-T03：Patch 回放失败时发布经校验源副本并标记整本降级。"""

    source = create_pdf(tmp_path / "fallback-final.pdf", ({},))
    request = make_request(source)
    coordinator = DocumentCoordinator(PageFactsExtractor())
    page = coordinator.enumerate_pages(request)[0]
    harness = make_runtime(tmp_path, request, DeterministicTranslationAdapter())
    invalid_patch = replace(make_patch(page), source_hash="c" * 64)
    outcome = PageOutcome(
        1,
        PagePipelineState.FINALIZED,
        ArtifactProduced.YES,
        ArtifactIntegrity.PASS,
        TranslationCoverage.FULL,
        Capability.SUPPORTED,
        Quality.PASS,
        Fallback.NONE,
    )
    processed = ProcessedPage(1, ROUTE_SINGLE, outcome, invalid_patch, None, (), (), None)
    finalization = harness.finalizer.finalize(request, (page,), (processed,))
    assert finalization.document_passthrough
    assert finalization.preservation.passed
    relative = finalization.artifact.relative_path
    assert relative is not None
    assert sha256_file(harness.run_root / relative) == sha256_file(source)


@pytest.mark.contract
def test_p4_4_t04_finalizer_has_no_page_merge_or_browser_render_calls() -> None:
    """P4.4-T04：最终化源码与运行扫描不含页级合并或浏览器渲染调用。"""

    assert verify_p4.finalizer_boundary_violations() == []


@pytest.mark.e2e
def test_p4_5_t01_complete_real_annual_report_produces_single_pdf(tmp_path: Path) -> None:
    """P4.5-T01：登记的完整真实年报全页执行并产出单一完整 PDF。"""

    fixture = json.loads(ANNUAL_FIXTURE_MANIFEST.read_text(encoding="utf-8"))
    source = REPO_ROOT / fixture["relative_source"]
    assert source.is_file() and sha256_file(source) == fixture["source_sha256"]
    request = make_request(source, "run-annual-e2e")
    harness = make_runtime(tmp_path, request, DeterministicTranslationAdapter())
    pages = harness.coordinator.enumerate_pages(request)
    assert len(pages) == fixture["page_count"]
    router = FixedRouteFixture(dict(fixture["routes_by_page_identity"]))
    execution = harness.coordinator.run(request, router, harness.pipeline, harness.finalizer)
    target = final_path(harness, execution)
    with pymupdf.open(source) as source_document, pymupdf.open(target) as target_document:
        assert target_document.page_count == source_document.page_count
    assert execution.preservation.page_count_rate == 1.0
    assert execution.preservation.page_order_rate == 1.0
    assert len(tuple((harness.run_root / "final").glob("*.pdf"))) == 1


@pytest.mark.workflow
def test_p4_5_t02_restart_skips_all_committed_pages(tmp_path: Path) -> None:
    """P4.5-T02：同一 Run 重启后全部已提交页面从 Checkpoint 恢复且不重跑。"""

    source = create_pdf(tmp_path / "restart.pdf", ({}, {}, {}))
    request = make_request(source, "run-restart")
    harness = make_runtime(tmp_path, request, DeterministicTranslationAdapter())
    router = FixedRouteFixture({})
    first = harness.coordinator.run(request, router, harness.pipeline, harness.finalizer)
    second = harness.coordinator.run(request, router, harness.pipeline, harness.finalizer)
    assert first.resumed_page_count == 0
    assert second.resumed_page_count == 3
    assert second.final_artifact == first.final_artifact


@pytest.mark.fault_injection
def test_p4_5_t03_last_page_translation_failure_passthroughs_and_completes(
    tmp_path: Path,
) -> None:
    """P4.5-T03：最后页翻译失败后原页透传且完整 PDF 仍降级完成。"""

    source = create_pdf(tmp_path / "last-page.pdf", ({}, {}, {}))
    request = make_request(source, "run-last-page")
    coordinator = DocumentCoordinator(PageFactsExtractor())
    pages = coordinator.enumerate_pages(request)
    harness = make_runtime(tmp_path, request, FixedTranslationAdapter({}))
    router = FixedRouteFixture({pages[-1].facts.page_identity: ROUTE_SINGLE})
    execution = coordinator.run(request, router, harness.pipeline, harness.finalizer)
    assert execution.pages[-1].outcome.fallback is Fallback.PAGE_PASSTHROUGH
    assert execution.result.outcome is DocumentOutcome.COMPLETED_WITH_DEGRADATION
    with pymupdf.open(final_path(harness, execution)) as document:
        assert document.page_count == 3


@pytest.mark.workflow
def test_p4_5_t04_all_passthrough_preserves_structure_and_marks_degradation(
    tmp_path: Path,
) -> None:
    """P4.5-T04：全页透传保持结构完整并明确标记降级结果。"""

    source = create_pdf(tmp_path / "all-pass.pdf", ({}, {}, {}))
    request = make_request(source, "run-all-pass")
    harness = make_runtime(tmp_path, request, DeterministicTranslationAdapter())
    execution = harness.coordinator.run(
        request,
        FixedRouteFixture({}),
        harness.pipeline,
        harness.finalizer,
    )
    assert all(page.outcome.fallback is Fallback.PAGE_PASSTHROUGH for page in execution.pages)
    assert execution.result.outcome is DocumentOutcome.COMPLETED_WITH_DEGRADATION
    assert execution.preservation.passed


@pytest.mark.fault_injection
def test_p4_5_t05_final_write_failure_retries_without_partial_authority(tmp_path: Path) -> None:
    """P4.5-T05：最终 rename 前写失败无发布权威，恢复后重试产生完整结果。"""

    source = create_pdf(tmp_path / "retry-final.pdf", ({}, {}))
    request = make_request(source, "run-final-retry")
    harness = make_runtime(tmp_path, request, DeterministicTranslationAdapter())
    router = FixedRouteFixture({})
    with pytest.raises(InjectedCrash):
        harness.coordinator.run(
            request,
            router,
            harness.pipeline,
            harness.finalizer,
            final_crash_at="before_artifact_rename",
        )
    assert harness.artifacts.published_final() is None
    harness.artifacts.recover()
    assert list(harness.run_root.rglob("*.partial")) == []
    recovered = harness.coordinator.run(request, router, harness.pipeline, harness.finalizer)
    assert recovered.final_artifact == harness.artifacts.published_final()
    assert recovered.resumed_page_count == 2
    with pymupdf.open(final_path(harness, recovered)) as document:
        assert document.page_count == 2
