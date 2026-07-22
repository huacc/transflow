"""用分类结果中的真实 PDF 执行 P9 六叶结构扫描和千问候选回归。"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
from collections.abc import Callable
from dataclasses import asdict, dataclass, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import pymupdf

from tests.migration.p9_qwen_translation_adapter import MigrationQwenTranslationAdapter
from transflow.application.contracts import EnumeratedPage
from transflow.application.document_coordinator import DocumentCoordinator
from transflow.application.toolbox_page_coordinator import ToolboxPageCoordinator, ToolboxPageWork
from transflow.domain.jobs import DocumentRunRequest
from transflow.domain.toolbox import PagePatch
from transflow.domain.translation import TranslationBatch, TranslationBundle
from transflow.pdf_kernel import ControlledFontRegistry, PageFactsExtractor, PagePatchInterpreter
from transflow.pdf_kernel.patch import ReplayPage
from transflow.pdf_kernel.renderer import outside_region_diff_ratio
from transflow.toolboxes.catalog import load_toolbox_catalog
from transflow.toolboxes.leaves import (
    AnchoredBlocksToolbox,
    ContentsToolbox,
    CoverToolbox,
    EndToolbox,
    MultiFlowTextToolbox,
    TableToolbox,
)
from transflow.toolboxes.leaves.ordinary import StructuredOrdinaryLeafToolbox
from transflow.toolboxes.leaves.ordinary_policy import (
    P9OrdinaryLeafPolicy,
    load_p9_ordinary_leaf_policy,
)

LOGGER = logging.getLogger("transflow.scripts.run_p9_real_samples")
REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_ROOT = REPO_ROOT / "spikes" / "page_classification_engine_puncture_v1" / "分类结果"
OUTPUT_ROOT = REPO_ROOT / "output" / "pdf" / "P9_real_samples"
EVIDENCE_PATH = REPO_ROOT / "resources" / "evidence" / "p9" / "real_sample_regression.json"
SUMMARY_PATH = OUTPUT_ROOT / "P9_real_samples_summary.json"
SHOWCASE_PATH = OUTPUT_ROOT / "P9_real_samples_showcase.pdf"
PRODUCTION_SHOWCASE_PATH = OUTPUT_ROOT / "P9_real_samples_production_safe_showcase.pdf"
DIAGNOSTIC_ROOT = OUTPUT_ROOT / "diagnostic_candidates"
PRODUCTION_ROOT = OUTPUT_ROOT / "production_safe"
P9_POLICY = REPO_ROOT / "resources" / "manifests" / "p9_ordinary_leaf_policy.json"
FONT_MANIFEST = REPO_ROOT / "resources" / "manifests" / "font_manifest.json"
CATALOG_V4 = REPO_ROOT / "resources" / "catalogs" / "page_toolbox_catalog_v4.json"
FONT_ID = "noto-sans-cjk-sc-regular"
SAMPLES_PER_LEAF = 2
MAX_SELECTED_UNITS = 96
CandidateToolboxFactory = Callable[[P9OrdinaryLeafPolicy, Path], StructuredOrdinaryLeafToolbox]
LEAF_SPECS: tuple[tuple[str, tuple[str, ...], CandidateToolboxFactory], ...] = (
    ("cover", ("cover",), CoverToolbox),
    ("contents", ("contents",), ContentsToolbox),
    ("end", ("end",), EndToolbox),
    ("body.flow_text.multi", ("body", "flow_text", "multi"), MultiFlowTextToolbox),
    ("body.table", ("body", "table"), TableToolbox),
    ("body.anchored_blocks", ("body", "anchored_blocks"), AnchoredBlocksToolbox),
)


@dataclass(slots=True)
class ScannedSample:
    """保存一次真实 PDF 扫描形成的候选运行上下文。"""

    route: str
    path: Path
    source_hash: str
    page: EnumeratedPage
    toolbox: StructuredOrdinaryLeafToolbox
    unit_count: int
    latin_count: int
    cjk_count: int


@dataclass(frozen=True, slots=True)
class TranslationExchange:
    """保存一次真实 TranslationPort 调用的请求与规范化响应，供诊断证据落盘。"""

    batch: TranslationBatch
    bundle: TranslationBundle


@dataclass(frozen=True, slots=True)
class DiagnosticRenderResult:
    """记录不可发布候选的实际写入状态、操作数和文本框剩余空间。"""

    status: str
    applied_count: int
    layout_remainders: tuple[float, ...]
    clipped_operation_count: int = 0
    shrink_to_fit_count: int = 0
    forced_text_count: int = 0
    rendered_font_sizes: tuple[float, ...] = ()
    error_type: str | None = None


class RecordingTranslationPort:
    """代理真实千问适配器，同时只在内存中保留已校验的请求和响应。"""

    def __init__(self, delegate: MigrationQwenTranslationAdapter) -> None:
        """绑定真实适配器；不改变请求、响应或异常的生产语义。"""

        self._delegate = delegate
        self._exchanges: list[TranslationExchange] = []

    @property
    def call_count(self) -> int:
        """返回底层真实 HTTP 调用次数，用于证明没有用固定译文替代千问。"""

        return self._delegate.call_count

    @property
    def exchange_count(self) -> int:
        """返回已经完成合同校验并可用于审计落盘的交换数量。"""

        return len(self._exchanges)

    @property
    def latest_exchange(self) -> TranslationExchange | None:
        """返回最近一次成功的真实翻译交换；没有成功响应时返回空。"""

        return self._exchanges[-1] if self._exchanges else None

    def translate(self, batch: TranslationBatch) -> TranslationBundle:
        """调用真实千问并在响应合同校验完成后保存同一份对象。"""

        LOGGER.info(
            "调用 P9 翻译证据记录，意图=保留真实请求与规范化响应 batch_id=%s",
            batch.batch_id,
        )
        bundle = self._delegate.translate(batch)
        self._exchanges.append(TranslationExchange(batch, bundle))
        return bundle


def _sha256_file(path: Path) -> str:
    """流式计算样本和产物哈希，禁止用路径或文件名代替内容身份。"""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative(path: Path) -> str:
    """把证据路径统一记录为仓库相对 POSIX 路径。"""

    return path.resolve().relative_to(REPO_ROOT.resolve()).as_posix()


def _json_default(value: object) -> object:
    """把领域数据类和枚举转换为可读 JSON，同时拒绝隐式字符串化未知对象。"""

    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"P9 诊断证据存在不可序列化类型:{type(value).__name__}")


def _write_json(path: Path, payload: object) -> None:
    """用统一 UTF-8、稳定键顺序写入一份可追溯诊断证据。"""

    LOGGER.info("写入 P9 诊断证据，意图=保留候选失败上下文 path=%s", _relative(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            payload,
            default=_json_default,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _choose_diagnostic_font_size(
    *,
    page_rect: pymupdf.Rect,
    rectangle: pymupdf.Rect,
    text: str,
    font_path: Path,
    requested_size: float,
) -> tuple[float, float | None]:
    """在空白页探测原字号余量，并为不可发布候选选择第一个可容纳字号。"""

    ratios = (1.0, 0.8, 0.65, 0.5, 0.35)
    sizes = tuple(dict.fromkeys(max(2.5, round(requested_size * ratio, 2)) for ratio in ratios))
    original_remainder = -1.0
    with pymupdf.open() as probe_document:
        probe_page = probe_document.new_page(width=page_rect.width, height=page_rect.height)
        probe_page.insert_font(fontname="TFP9DiagnosticProbe", fontfile=str(font_path))
        for index, font_size in enumerate(sizes):
            remainder = float(
                probe_page.insert_textbox(
                    rectangle,
                    text,
                    fontname="TFP9DiagnosticProbe",
                    fontsize=font_size,
                    color=(0, 0, 0),
                )
            )
            if index == 0:
                original_remainder = remainder
            if remainder >= 0:
                return original_remainder, font_size
    return original_remainder, None


def _write_diagnostic_candidate(
    *,
    source_pdf: Path,
    candidate_pdf: Path,
    sample: ScannedSample,
    patch: PagePatch | None,
    fonts: ControlledFontRegistry,
) -> DiagnosticRenderResult:
    """无条件生成诊断 PDF；有原始提案时绕过发布保护区门并真实写入译文。

    该函数只服务 ``output/pdf/P9_real_samples/diagnostic_candidates``。它故意展示
    生产保护区门拒绝的视觉后果，因此产物永远不可作为正式发布 PDF。
    """

    LOGGER.info(
        "写入 P9 不可发布候选，意图=让保护区或排版失败可视 route=%s source_hash=%s",
        sample.route,
        sample.source_hash[:12],
    )
    candidate_pdf.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source_pdf, candidate_pdf)
    if patch is None:
        return DiagnosticRenderResult("NO_PROPOSED_PATCH", 0, ())

    try:
        # 诊断候选仍严格校验 source/page/owner 绑定，只绕过生产保护区拒绝。
        patch.validate_binding(sample.page.context, sample.route)
        with pymupdf.open(candidate_pdf) as document:
            page = document[sample.page.context.page_no - 1]
            render_operations: list[tuple[Any, pymupdf.Rect, Path, str, float]] = []
            clipped_operation_count = 0
            for operation in patch.operations:
                if (
                    operation.kind != "replace_text"
                    or operation.rect is None
                    or operation.replacement_text is None
                    or operation.font_id is None
                    or operation.font_size is None
                ):
                    raise ValueError("P9_DIAGNOSTIC_PATCH_OPERATION_INVALID")
                rectangle = pymupdf.Rect(operation.rect)
                # 某些真实 PDF 的事实 CropBox 保留原始介质坐标，而 Patch 使用归一化页面坐标。
                # 诊断渲染以当前 PyMuPDF 页面的 page.rect 为准，避免混用两个坐标空间。
                crop_box = pymupdf.Rect(page.rect)
                if not crop_box.contains(rectangle):
                    rectangle = rectangle & crop_box
                    clipped_operation_count += 1
                if rectangle.is_empty or rectangle.is_infinite:
                    raise ValueError("P9_DIAGNOSTIC_RECT_OUTSIDE_CROPBOX")
                font_path = fonts.resolve(operation.font_id).path
                render_operations.append(
                    (
                        operation,
                        rectangle,
                        font_path,
                        operation.replacement_text,
                        operation.font_size,
                    )
                )
                page.add_redact_annot(rectangle, fill=(1, 1, 1))

            # 先统一清除原文，再写入译文，防止相邻区域的后续红action删除已写文本。
            if render_operations:
                page.apply_redactions(images=0, graphics=0, text=0)
            remainders: list[float] = []
            rendered_font_sizes: list[float] = []
            shrink_to_fit_count = 0
            forced_text_count = 0
            for (
                operation,
                rectangle,
                font_path,
                replacement_text,
                requested_size,
            ) in render_operations:
                font_name = f"TFP9Diag{operation.payload_hash[:8]}"
                page.insert_font(fontname=font_name, fontfile=str(font_path))
                original_remainder, fitted_size = _choose_diagnostic_font_size(
                    page_rect=page.rect,
                    rectangle=rectangle,
                    text=replacement_text,
                    font_path=font_path,
                    requested_size=requested_size,
                )
                remainders.append(original_remainder)
                if fitted_size is None:
                    # 极小区域仍放不下时强制写出开头；完整译文仍保存在相邻 bundle JSON。
                    fitted_size = 2.5
                    forced_text_count += 1
                    page.insert_text(
                        pymupdf.Point(rectangle.x0, rectangle.y0 + fitted_size),
                        replacement_text,
                        fontname=font_name,
                        fontsize=fitted_size,
                        color=(0, 0, 0),
                    )
                else:
                    page.insert_textbox(
                        rectangle,
                        replacement_text,
                        fontname=font_name,
                        fontsize=fitted_size,
                        color=(0, 0, 0),
                    )
                if fitted_size < requested_size:
                    shrink_to_fit_count += 1
                rendered_font_sizes.append(fitted_size)
            metadata = dict(document.metadata)
            metadata["subject"] = "UNSAFE DIAGNOSTIC CANDIDATE - NOT FOR PRODUCTION"
            document.set_metadata(metadata)
            if render_operations:
                document.saveIncr()
        return DiagnosticRenderResult(
            "WRITTEN_UNSAFE_DIAGNOSTIC",
            len(render_operations),
            tuple(remainders),
            clipped_operation_count,
            shrink_to_fit_count,
            forced_text_count,
            tuple(rendered_font_sizes),
        )
    except Exception as error:
        # 即使诊断渲染本身失败，也保留已经复制的可打开 PDF，并把失败类型写入证据。
        LOGGER.exception(
            "P9 不可发布候选写入失败，意图=保留源副本并报告失败类型 route=%s",
            sample.route,
        )
        return DiagnosticRenderResult(
            "RENDER_FAILED_SOURCE_PRESERVED",
            0,
            (),
            error_type=type(error).__name__,
        )


def _make_request(path: Path, route: str, ordinal: int) -> DocumentRunRequest:
    """为真实单页样本建立不含文件身份分支的文档运行请求。"""

    digest = _sha256_file(path)
    return DocumentRunRequest(
        source_pdf_path=str(path.resolve()),
        source_hash=digest,
        source_language="en",
        target_language="zh-CN",
        config_snapshot_hash="a" * 64,
        job_id="job-p9-real-corpus",
        run_id=f"run-p9-real-{route.replace('.', '-')}-{ordinal:03d}-{digest[:12]}",
    )


def _language_counts(text: str) -> tuple[int, int]:
    """仅用页面原生文字统计拉丁和中日韩字符数量，供测试选样。"""

    return (
        len(re.findall(r"[A-Za-z]", text)),
        len(re.findall(r"[\u3400-\u9fff]", text)),
    )


def _toolbox(
    toolbox_factory: CandidateToolboxFactory,
    font_path: Path,
) -> StructuredOrdinaryLeafToolbox:
    """使用集中 P9 policy 和受控字体构造一个候选叶实例。"""

    return toolbox_factory(load_p9_ordinary_leaf_policy(P9_POLICY), font_path)


def _scan_real_corpus() -> tuple[list[dict[str, Any]], dict[str, list[ScannedSample]]]:
    """让六叶逐份消费全部真实 PageFacts，并收集结构覆盖证据。"""

    LOGGER.info("调用 P9 真实语料扫描，意图=让六叶消费分类结果中的全部 PDF")
    font_path = ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT).resolve(FONT_ID).path
    coordinator = DocumentCoordinator(PageFactsExtractor())
    catalog = load_toolbox_catalog(CATALOG_V4)
    records: list[dict[str, Any]] = []
    candidates: dict[str, list[ScannedSample]] = {route: [] for route, _, _ in LEAF_SPECS}
    for route, parts, toolbox_factory in LEAF_SPECS:
        folder = SAMPLE_ROOT.joinpath(*parts)
        paths = tuple(sorted(folder.glob("*.pdf")))
        if not paths:
            raise RuntimeError(f"P9 真实样本目录为空:{route}")
        resolution = catalog.resolve_enabled(route, 1)
        if resolution.finding is None or resolution.finding.code != "TOOLBOX_DISABLED":
            raise RuntimeError(f"P9 正式 Catalog 未保持 disabled:{route}")
        for ordinal, path in enumerate(paths):
            request = _make_request(path, route, ordinal)
            pages = coordinator.enumerate_pages(request)
            if len(pages) != 1:
                raise RuntimeError(f"P9 分类样本不是单页 PDF:{_relative(path)}")
            page = pages[0]
            toolbox = _toolbox(toolbox_factory, font_path)
            template = toolbox.prepare(page.context, page.facts)
            snapshot = toolbox.audit_snapshot(template)
            batch = toolbox.build_translation_request(template)
            source_text = "\n".join(item.text for item in page.facts.text_spans)
            latin_count, cjk_count = _language_counts(source_text)
            unit_count = len(batch.units) if batch is not None else 0
            record = {
                "cjk_count": cjk_count,
                "drawing_objects": len(page.facts.drawing_objects),
                "image_objects": len(page.facts.image_objects),
                "kernel_facts_hash": page.facts.kernel_facts_hash,
                "latin_count": latin_count,
                "native_text_spans": len(page.facts.text_spans),
                "owner_coverage_complete": snapshot.owner_coverage_complete,
                "page_count": len(pages),
                "relative_path": _relative(path),
                "route": route,
                "source_hash": request.source_hash,
                "table_objects": len(page.facts.table_objects),
                "translation_units": unit_count,
            }
            records.append(record)
            if unit_count > 0 and latin_count >= 24 and latin_count > cjk_count * 2:
                candidates[route].append(
                    ScannedSample(
                        route,
                        path,
                        request.source_hash,
                        page,
                        toolbox,
                        unit_count,
                        latin_count,
                        cjk_count,
                    )
                )
    return records, candidates


def _select_candidates(candidates: dict[str, list[ScannedSample]]) -> list[ScannedSample]:
    """按结构复杂度和内容哈希选择每叶两份英文样本，不读取公司或样本名。"""

    selected: list[ScannedSample] = []
    for route, _, _ in LEAF_SPECS:
        eligible = sorted(
            (item for item in candidates[route] if item.unit_count <= MAX_SELECTED_UNITS),
            key=lambda item: (item.unit_count, item.source_hash),
        )
        if len(eligible) < SAMPLES_PER_LEAF:
            eligible = sorted(
                candidates[route], key=lambda item: (item.unit_count, item.source_hash)
            )
        if not eligible:
            raise RuntimeError(f"P9 叶没有可执行英文真实样本:{route}")
        if len(eligible) == 1 or SAMPLES_PER_LEAF == 1:
            chosen = eligible[:1]
        else:
            chosen = [eligible[0], eligible[-1]]
        selected.extend(chosen)
    return selected


def _render_preview(pdf_path: Path, preview_path: Path) -> None:
    """把最新候选 PDF 真实渲染为 PNG，并用 PyMuPDF 解码确认可读。"""

    with pymupdf.open(pdf_path) as document:
        if document.page_count != 1:
            raise RuntimeError("P9 真实候选产物页数不为一")
        pixmap = document[0].get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5), alpha=False)
        content = pixmap.tobytes("png")
    decoded = pymupdf.Pixmap(content)
    if decoded.width < 1 or decoded.height < 1:
        raise RuntimeError("P9 真实候选预览无法解码")
    preview_path.parent.mkdir(parents=True, exist_ok=True)
    preview_path.write_bytes(content)


def _run_candidate(
    sample: ScannedSample,
    adapter: RecordingTranslationPort,
    interpreter: PagePatchInterpreter,
    fonts: ControlledFontRegistry,
) -> dict[str, Any]:
    """执行真实六阶段链，同时分别落盘不可发布候选与生产安全结果。"""

    LOGGER.info(
        "调用 P9 真实候选叶，意图=形成千问译文和项目内 PDF route=%s source_hash=%s",
        sample.route,
        sample.source_hash[:12],
    )
    before_calls = adapter.call_count
    before_exchanges = adapter.exchange_count
    result = ToolboxPageCoordinator(adapter).execute(
        ToolboxPageWork(sample.page.context, sample.page.facts, sample.toolbox)
    )
    exchange = adapter.latest_exchange if adapter.exchange_count > before_exchanges else None
    route_slug = sample.route.replace(".", "_")
    artifact_name = f"{route_slug}_{sample.source_hash[:12]}"
    production_dir = PRODUCTION_ROOT / route_slug
    diagnostic_dir = DIAGNOSTIC_ROOT / route_slug / artifact_name
    production_path = production_dir / f"{artifact_name}_production_safe.pdf"
    production_preview_path = production_dir / f"{artifact_name}_production_safe.png"
    diagnostic_path = diagnostic_dir / f"{artifact_name}_diagnostic_candidate.pdf"
    diagnostic_preview_path = diagnostic_dir / f"{artifact_name}_diagnostic_candidate.png"
    production_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(sample.path, production_path)
    if result.patch is not None:
        interpreter.replay_document(
            production_path,
            (
                ReplayPage(
                    sample.page.context,
                    sample.page.facts,
                    result.patch,
                    sample.route,
                ),
            ),
        )
    _render_preview(production_path, production_preview_path)

    diagnostic_render = _write_diagnostic_candidate(
        source_pdf=sample.path,
        candidate_pdf=diagnostic_path,
        sample=sample,
        patch=result.proposed_patch,
        fonts=fonts,
    )
    _render_preview(diagnostic_path, diagnostic_preview_path)

    request_payload: object = (
        exchange.batch
        if exchange is not None
        else {"available": False, "reason": "NO_SUCCESSFUL_TRANSLATION_EXCHANGE"}
    )
    bundle_payload: object = (
        exchange.bundle
        if exchange is not None
        else {"available": False, "reason": "NO_SUCCESSFUL_TRANSLATION_EXCHANGE"}
    )
    request_path = diagnostic_dir / "translation_request.json"
    bundle_path = diagnostic_dir / "translation_bundle.json"
    plan_path = diagnostic_dir / "layout_plan.json"
    judgement_path = diagnostic_dir / "quality_judgement.json"
    _write_json(request_path, request_payload)
    _write_json(bundle_path, bundle_payload)
    _write_json(
        plan_path,
        {
            "approved_patch": result.patch,
            "proposed_patch": result.proposed_patch,
            "route": sample.route,
            "schema_version": "transflow.p9-diagnostic-layout-plan/v1",
        },
    )
    _write_json(
        judgement_path,
        {
            "diagnostic_publishable": False,
            "diagnostic_render": diagnostic_render,
            "findings": result.findings,
            "outcome": result.outcome,
            "schema_version": "transflow.p9-diagnostic-judgement/v1",
            "verdict": result.verdict,
        },
    )

    approved_operation_rects = (
        tuple(operation.rect for operation in result.patch.operations if operation.rect is not None)
        if result.patch is not None
        else ()
    )
    proposed_operation_rects = (
        tuple(
            operation.rect
            for operation in result.proposed_patch.operations
            if operation.rect is not None
        )
        if result.proposed_patch is not None
        else ()
    )
    production_outside_ratio = (
        outside_region_diff_ratio(
            sample.path,
            production_path,
            approved_operation_rects,
            page_no=1,
            scale=0.5,
        )
        if result.patch is not None
        else 0.0
    )
    diagnostic_outside_ratio = (
        outside_region_diff_ratio(
            sample.path,
            diagnostic_path,
            proposed_operation_rects,
            page_no=1,
            scale=0.5,
        )
        if result.proposed_patch is not None
        else 0.0
    )
    return {
        "approved_patch_operations": len(result.patch.operations)
        if result.patch is not None
        else 0,
        "candidate_hash": _sha256_file(diagnostic_path),
        "candidate_path": _relative(diagnostic_path),
        "catalog_enabled": False,
        "catalog_expected_fallback": "PAGE_PASSTHROUGH",
        "diagnostic_artifact_dir": _relative(diagnostic_dir),
        "diagnostic_clipped_operation_count": diagnostic_render.clipped_operation_count,
        "diagnostic_forced_text_count": diagnostic_render.forced_text_count,
        "diagnostic_layout_remainders": list(diagnostic_render.layout_remainders),
        "diagnostic_outside_declared_region_diff_ratio": diagnostic_outside_ratio,
        "diagnostic_publishable": False,
        "diagnostic_render_error_type": diagnostic_render.error_type,
        "diagnostic_rendered_font_sizes": list(diagnostic_render.rendered_font_sizes),
        "diagnostic_render_status": diagnostic_render.status,
        "diagnostic_shrink_to_fit_count": diagnostic_render.shrink_to_fit_count,
        "finding_codes": list(result.outcome.finding_codes),
        "layout_plan_path": _relative(plan_path),
        "outside_declared_region_diff_ratio": diagnostic_outside_ratio,
        "patch_operations": len(result.proposed_patch.operations)
        if result.proposed_patch is not None
        else 0,
        "preview_path": _relative(diagnostic_preview_path),
        "production_outside_declared_region_diff_ratio": production_outside_ratio,
        "production_preview_path": _relative(production_preview_path),
        "production_safe_hash": _sha256_file(production_path),
        "production_safe_path": _relative(production_path),
        "proposed_patch_operations": len(result.proposed_patch.operations)
        if result.proposed_patch is not None
        else 0,
        "quality_judgement_path": _relative(judgement_path),
        "qwen_http_calls": adapter.call_count - before_calls,
        "relative_path": _relative(sample.path),
        "route": sample.route,
        "source_hash": sample.source_hash,
        "translation_bundle_path": _relative(bundle_path),
        "translation_coverage": result.outcome.translation_coverage.value,
        "translation_request_path": _relative(request_path),
        "translation_units": sample.unit_count,
        "verdict": result.verdict.disposition.value,
    }


def _leaf_summaries(records: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """按 Route 聚合真实样本、owner、零单元和结构对象数量。"""

    summaries: dict[str, dict[str, int]] = {}
    for route, _, _ in LEAF_SPECS:
        items = [item for item in records if item["route"] == route]
        summaries[route] = {
            "owner_coverage_failures": sum(not item["owner_coverage_complete"] for item in items),
            "sample_count": len(items),
            "samples_with_translation_units": sum(item["translation_units"] > 0 for item in items),
            "table_objects": sum(item["table_objects"] for item in items),
            "translation_units": sum(item["translation_units"] for item in items),
            "zero_translation_samples": sum(item["translation_units"] == 0 for item in items),
        }
    return summaries


def _write_showcase(
    results: list[dict[str, Any]],
    *,
    artifact_key: str,
    output_path: Path,
) -> None:
    """按 Route 顺序合并指定产物，明确区分诊断候选和生产安全结果。"""

    with pymupdf.open() as showcase:
        for item in results:
            with pymupdf.open(REPO_ROOT / str(item[artifact_key])) as candidate:
                showcase.insert_pdf(candidate)
        output_path.write_bytes(showcase.tobytes(garbage=4, deflate=True))


def main() -> int:
    """扫描全部真实样本、执行真实千问候选并写入可追溯证据。"""

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")
    LOGGER.setLevel(logging.INFO)
    logging.getLogger("transflow.tests.migration.p9_qwen_translation_adapter").setLevel(
        logging.INFO
    )
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    records, candidates = _scan_real_corpus()
    selected = _select_candidates(candidates)
    adapter = RecordingTranslationPort(MigrationQwenTranslationAdapter())
    fonts = ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT)
    interpreter = PagePatchInterpreter(fonts)
    results = [_run_candidate(sample, adapter, interpreter, fonts) for sample in selected]
    _write_showcase(results, artifact_key="candidate_path", output_path=SHOWCASE_PATH)
    _write_showcase(
        results,
        artifact_key="production_safe_path",
        output_path=PRODUCTION_SHOWCASE_PATH,
    )
    summaries = _leaf_summaries(records)
    diagnostic_candidates_written = sum(
        item["diagnostic_render_status"] == "WRITTEN_UNSAFE_DIAGNOSTIC"
        and item["proposed_patch_operations"] > 0
        for item in results
    )
    fallback_with_diagnostic_candidate = sum(
        item["verdict"] == "FALLBACK" and item["proposed_patch_operations"] > 0 for item in results
    )
    payload = {
        "blind_promotion_eligible": False,
        "catalog_decision": "PASS_DISABLED_WITH_FALLBACK",
        "candidate_results": results,
        "corpus_root": _relative(SAMPLE_ROOT),
        "diagnostic_candidates_written": diagnostic_candidates_written,
        "diagnostic_showcase_hash": _sha256_file(SHOWCASE_PATH),
        "diagnostic_showcase_path": _relative(SHOWCASE_PATH),
        "fallback_with_diagnostic_candidate": fallback_with_diagnostic_candidate,
        "leaf_summaries": summaries,
        "production_safe_showcase_hash": _sha256_file(PRODUCTION_SHOWCASE_PATH),
        "production_safe_showcase_path": _relative(PRODUCTION_SHOWCASE_PATH),
        "qwen_http_calls": adapter.call_count,
        "sample_records": records,
        "schema_version": "transflow.p9-real-sample-regression/v2",
        "selected_sample_count": len(selected),
        "showcase_hash": _sha256_file(SHOWCASE_PATH),
        "showcase_path": _relative(SHOWCASE_PATH),
        "total_sample_count": len(records),
    }
    _write_json(EVIDENCE_PATH, payload)
    _write_json(SUMMARY_PATH, payload)
    print(
        json.dumps(
            {
                "blind_promotion_eligible": False,
                "catalog_decision": payload["catalog_decision"],
                "diagnostic_candidates_written": diagnostic_candidates_written,
                "fallback_with_diagnostic_candidate": fallback_with_diagnostic_candidate,
                "leaf_summaries": summaries,
                "production_safe_showcase_path": payload["production_safe_showcase_path"],
                "qwen_http_calls": adapter.call_count,
                "selected_sample_count": len(selected),
                "showcase_path": payload["showcase_path"],
                "total_sample_count": len(records),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
