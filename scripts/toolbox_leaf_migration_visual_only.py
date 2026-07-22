"""执行 TM1 visual_only 的真实分类、零写入主链和可视校准。"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pymupdf

from scripts.run_toolbox_leaf_migration import (
    CATALOG_PATH,
    MigrationContractError,
    provider_configuration_snapshot,
)
from scripts.toolbox_leaf_migration_drivers import LeafMigrationRunContext
from tests.migration.qwen_adapter import (
    MigrationQwenDecisionAdapter,
    migration_environment_ready,
)
from transflow.adapters.filesystem.artifact_store import SharedFilesystemArtifactAdapter
from transflow.adapters.filesystem.checkpoint import FilesystemCheckpointAdapter
from transflow.adapters.filesystem.layout_memory_runtime import DocumentLayoutMemoryRuntime
from transflow.application.contracts import ProcessedPage
from transflow.application.document_coordinator import DocumentCoordinator
from transflow.application.document_finalizer import DocumentFinalizer
from transflow.application.document_layout_memory import (
    DocumentLayoutMemoryBuilder,
    LayoutMemoryPolicyConfig,
    derive_page_geometry_hash,
)
from transflow.application.page_pipeline import PreviewPublisher
from transflow.application.toolbox_page_coordinator import (
    ToolboxPageCoordinator,
    ToolboxPageWork,
)
from transflow.application.toolbox_page_pipeline import ToolboxPagePipeline
from transflow.classification.decision_adapter import BoundedDecisionRunner
from transflow.classification.engine import ClassificationEngine, ClassifiedPage
from transflow.domain.common import content_sha256, json_ready
from transflow.domain.jobs import DocumentRunRequest
from transflow.domain.layout_memory import DocumentLayoutMemoryIdentity
from transflow.domain.states import CheckpointCompatibility, PagePipelineState
from transflow.domain.translation import TranslationBatch, TranslationBundle
from transflow.pdf_kernel import (
    ControlledFontRegistry,
    PageFactsExtractor,
    PagePatchInterpreter,
    PyMuPdfPageRenderer,
)
from transflow.pdf_kernel.facts import ExtractedPageFacts
from transflow.pdf_kernel.preservation import PreflightDecision
from transflow.toolboxes.catalog import load_toolbox_catalog
from transflow.toolboxes.contracts import normalized_page_outcome
from transflow.toolboxes.leaves import VisualOnlyToolbox, build_p8_toolbox_factories

LOGGER = logging.getLogger("transflow.toolbox_leaf_migration.visual_only")
ROUTE = "visual_only"
FONT_MANIFEST = Path("resources/manifests/font_manifest.json")
P8_POLICY = Path("resources/manifests/p8_toolbox_policy.json")
P9A_POLICY = Path("resources/manifests/p9a_layout_policy.json")
LAYOUT_SCHEMA = Path("resources/schemas/document_layout_memory_v1.schema.json")
LEAF_SCHEMA = Path("resources/schemas/leaf_migration_evidence_v1.schema.json")


class _ZeroTranslationPort:
    """把任何意外翻译调用转成可见失败，并保留调用计数。"""

    def __init__(self) -> None:
        self.call_count = 0

    def translate(self, batch: TranslationBatch) -> TranslationBundle:
        self.call_count += 1
        raise AssertionError(f"visual_only 禁止调用 TranslationPort: {batch.batch_id}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative(path: Path, repository_root: Path) -> str:
    return path.resolve().relative_to(repository_root.resolve()).as_posix()


def _write_json(path: Path, payload: object, run_root: Path) -> None:
    """只在当前轮目录内写原子 JSON。"""

    try:
        path.resolve().relative_to(run_root.resolve())
    except ValueError as error:
        raise MigrationContractError("TM1_OUTPUT_PATH_INVALID", path.name) from error
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(json_ready(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _extract_page(source: Path, page_no: int, target: Path) -> None:
    """从完整文档提取一页作为诊断副本，不作为产品输入。"""

    target.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open(source) as document, pymupdf.open() as output:
        output.insert_pdf(document, from_page=page_no - 1, to_page=page_no - 1)
        output.save(target, garbage=4, deflate=True)


def _render_page(source: Path, page_no: int, target: Path) -> None:
    """用 PyMuPDF 渲染真实 PDF 页供人工检查。"""

    target.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open(source) as document:
        pixmap = document[page_no - 1].get_pixmap(
            matrix=pymupdf.Matrix(1.5, 1.5),
            alpha=False,
        )
        pixmap.save(target)


def _compose_comparison(
    panels: tuple[tuple[str, Path], ...],
    output_pdf: Path,
    output_png: Path,
) -> None:
    """把若干单页 PDF 按原比例并排，并渲染一张复核 PNG。"""

    opened = [pymupdf.open(path) for _, path in panels]
    try:
        widths = [document[0].rect.width for document in opened]
        heights = [document[0].rect.height for document in opened]
        gap = 18.0
        header = 28.0
        with pymupdf.open() as target:
            page = target.new_page(
                width=sum(widths) + gap * (len(widths) - 1),
                height=max(heights) + header,
            )
            left = 0.0
            for (label, _), document, width, height in zip(
                panels,
                opened,
                widths,
                heights,
                strict=True,
            ):
                page.insert_text((left + 6, 17), label, fontsize=9)
                page.show_pdf_page(
                    pymupdf.Rect(left, header, left + width, header + height),
                    document,
                    0,
                )
                left += width + gap
            output_pdf.parent.mkdir(parents=True, exist_ok=True)
            target.save(output_pdf, garbage=4, deflate=True)
        _render_page(output_pdf, 1, output_png)
    finally:
        for document in opened:
            document.close()


def _pixel_change_metrics(source: Path, candidate: Path) -> dict[str, object]:
    """逐像素比较两份单页 PDF 的固定倍率渲染。"""

    with pymupdf.open(source) as left, pymupdf.open(candidate) as right:
        left_pixmap = left[0].get_pixmap(matrix=pymupdf.Matrix(1.25, 1.25), alpha=False)
        right_pixmap = right[0].get_pixmap(matrix=pymupdf.Matrix(1.25, 1.25), alpha=False)
    if (
        left_pixmap.width != right_pixmap.width
        or left_pixmap.height != right_pixmap.height
        or left_pixmap.n != right_pixmap.n
    ):
        raise MigrationContractError("TM1_RENDER_SHAPE_DRIFT", "源页与透传页渲染尺寸不同")
    changed = sum(
        left_value != right_value
        for left_value, right_value in zip(
            left_pixmap.samples,
            right_pixmap.samples,
            strict=True,
        )
    )
    total = len(left_pixmap.samples)
    return {
        "changed_channel_count": changed,
        "changed_channel_ratio": changed / total if total else 0.0,
        "height": left_pixmap.height,
        "width": left_pixmap.width,
    }


def _write_zero_diff_preview(reference_page: Path, target: Path) -> None:
    """写出明确的零差异图；若真实差异非零，调用方不会进入这里。"""

    with pymupdf.open(reference_page) as source, pymupdf.open() as document:
        rect = source[0].rect
        page = document.new_page(width=rect.width, height=rect.height)
        page.insert_text((24, 36), "NO CHANGED PIXELS - RATIO 0.0", fontsize=12)
        pixmap = page.get_pixmap(matrix=pymupdf.Matrix(1.25, 1.25), alpha=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        pixmap.save(target)


def _semantic_signature(facts: ExtractedPageFacts) -> str:
    """计算文本、图片、drawing 和页面几何的稳定语义签名。"""

    return content_sha256(
        {
            "geometry": (facts.media_box, facts.crop_box, facts.rotation),
            "text": tuple((item.text, item.bbox) for item in facts.text_spans),
            "images": tuple(
                (item.bbox, item.width, item.height, item.content_hash)
                for item in facts.image_objects
            ),
            "drawings": tuple(
                (item.bbox, item.content_hash) for item in facts.drawing_objects
            ),
        }
    )


def _layout_identity(
    context: LeafMigrationRunContext,
    facts: tuple[ExtractedPageFacts, ...],
    policy: LayoutMemoryPolicyConfig,
) -> DocumentLayoutMemoryIdentity:
    """使用本轮实际代码、资源和源文档冻结 P9A 失效身份。"""

    root = context.repository_root
    return DocumentLayoutMemoryIdentity(
        source_hash=facts[0].page.source_hash,
        source_language=str(context.input_manifest["source_language"]),
        target_language=str(context.input_manifest["target_language"]),
        page_geometry_hash=derive_page_geometry_hash(facts),
        config_hash=policy.config_hash,
        builder_hash=_sha256_file(
            root / "src/transflow/application/document_layout_memory.py"
        ),
        classifier_hash=_sha256_file(root / "src/transflow/classification/engine.py"),
        catalog_hash=_sha256_file(CATALOG_PATH),
        kernel_hash=_sha256_file(root / "src/transflow/pdf_kernel/facts.py"),
        patch_interpreter_hash=_sha256_file(root / "src/transflow/pdf_kernel/patch.py"),
        font_hash=_sha256_file(root / FONT_MANIFEST),
        schema_hash=_sha256_file(root / LAYOUT_SCHEMA),
    )


def _artifact(path: Path, context: LeafMigrationRunContext, role: str) -> dict[str, object]:
    return {
        "path": _relative(path, context.repository_root),
        "present": True,
        "role": role,
        "sha256": _sha256_file(path),
    }


def _classification_trace(
    classified: tuple[ClassifiedPage, ...],
    runner: BoundedDecisionRunner,
    adapter: MigrationQwenDecisionAdapter,
) -> dict[str, object]:
    """只保存分类身份、哈希和调用审计，不保存图片或原始模型响应。"""

    return {
        "schema_version": "transflow.tm1-classification-trace/v1",
        "adapter": "tests.migration.qwen_adapter.MigrationQwenDecisionAdapter",
        "model_call_count": adapter.call_count,
        "raw_provider_response_persisted": False,
        "route_distribution": dict(
            sorted(Counter(item.route.route for item in classified).items())
        ),
        "pages": [
            {
                "classification_evidence_hash": content_sha256(json_ready(item.resolutions)),
                "failed_node": item.route.failed_node,
                "page_identity": item.page_identity,
                "page_no": item.page_no,
                "resolution_count": len(item.resolutions),
                "route": item.route.route,
            }
            for item in classified
        ],
        "audits": [
            {
                "decision_id": item.decision_id,
                "error_code": item.error_code,
                "input_sha256": item.input_sha256,
                "latency_ms": item.latency_ms,
                "node_key": item.node_key,
                "output_sha256": item.output_sha256,
                "prompt_sha256": item.prompt_sha256,
                "stage": item.stage,
                "status": item.status,
            }
            for item in runner.audits
        ],
    }


def _source_inventory(context: LeafMigrationRunContext) -> dict[str, object]:
    """登记 visual_only 无 Spike 私有核心以及当前生产落点。"""

    root = context.repository_root
    files = (
        root / "src/transflow/toolboxes/leaves/visual_only.py",
        root / "src/transflow/toolboxes/leaves/factory.py",
        root / "src/transflow/toolboxes/catalog.py",
        root / "src/transflow/application/toolbox_page_coordinator.py",
        root / "src/transflow/application/toolbox_page_pipeline.py",
        root / "src/transflow/application/document_coordinator.py",
        root / "src/transflow/application/document_finalizer.py",
    )
    catalog_payload = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    entry = next(item for item in catalog_payload["entries"] if item["route"] == ROUTE)
    return {
        "schema_version": "transflow.toolbox-leaf-source-inventory/v1",
        "route": ROUTE,
        "spike_core": {
            "present": False,
            "reason": "NO_INDEPENDENT_SPIKE_VISUAL_ONLY_CORE",
            "mapping_coverage_percent": 100,
            "reachable_asset_count": 0,
        },
        "migration_strategy": "NOT_USED_WITH_REASON",
        "production_files": [
            {"path": _relative(path, root), "sha256": _sha256_file(path)} for path in files
        ],
        "catalog_entry": {
            "enabled": entry["enabled"],
            "evidence_state": entry["evidence_state"],
            "fingerprint": entry["fingerprint"],
            "toolbox_key": entry["toolbox_key"],
            "toolbox_version": entry["toolbox_version"],
        },
        "allowed_differences": [],
    }


def _run_regressions(context: LeafMigrationRunContext) -> dict[str, object]:
    """重跑当前叶、分类、Kernel、P9A/P9B 和既有叶回归。"""

    commands = (
        (
            "TM1_CONTRACT",
            (
                "tests/test_toolbox_leaf_migration.py",
                "tests/test_toolbox_leaf_migration_tm1.py",
            ),
        ),
        (
            "CLASSIFICATION_KERNEL_P8",
            (
                "tests/test_p4.py",
                "tests/test_p5.py",
                "tests/test_p6.py",
                "tests/test_p7.py",
                "tests/test_p8.py",
            ),
        ),
        (
            "P9_P9A_P9B",
            (
                "tests/test_p9.py",
                "tests/test_p9a.py",
                "tests/test_p9b.py",
                "tests/test_p9c.py::test_p9c_2_t02_real_single_multi_table_anchor_maps_cover_native_text",
                "tests/test_p9c.py::test_p9c_2_t03_invalid_bundle_content_never_enters_layout_or_full",
                "tests/test_p9c.py::test_p9c_4_t02_real_misroutes_fallback_without_runtime_route_mutation",
            ),
        ),
    )
    records: list[dict[str, object]] = []
    repository_text = str(context.repository_root.resolve())
    repository_posix = context.repository_root.resolve().as_posix()
    for command_id, tests in commands:
        process = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", *tests],
            cwd=context.repository_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
            check=False,
        )
        stdout = process.stdout.replace(repository_text, ".").replace(repository_posix, ".")
        stderr = process.stderr.replace(repository_text, ".").replace(repository_posix, ".")
        records.append(
            {
                "command": ["python", "-m", "pytest", "-q", *tests],
                "command_id": command_id,
                "return_code": process.returncode,
                "stderr": stderr[-4000:],
                "stdout": stdout[-4000:],
            }
        )
        if process.returncode != 0:
            payload = {
                "schema_version": "transflow.tm1-regression-results/v1",
                "status": "FAIL",
                "commands": records,
            }
            _write_json(
                context.output_root / "process/regression_results.json",
                payload,
                context.output_root,
            )
            raise MigrationContractError("TM1_REGRESSION_FAILED", command_id)
    return {
        "schema_version": "transflow.tm1-regression-results/v1",
        "status": "PASS",
        "commands": records,
    }


class VisualOnlyMigrationDriver:
    """只校准既有 visual_only 透传，不复制不存在的 Spike 核心。"""

    def execute(self, context: LeafMigrationRunContext) -> dict[str, Any]:
        if context.stage != "TM1" or context.route != ROUTE:
            raise MigrationContractError("TM1_DRIVER_IDENTITY_INVALID", context.route)
        if not migration_environment_ready():
            raise MigrationContractError("REAL_QWEN_ENVIRONMENT_NOT_CONFIGURED", "分类环境未就绪")

        root = context.repository_root
        run_root = context.output_root
        source = run_root / "input/source_document.pdf"
        source_hash = _sha256_file(source)
        source_payload = context.input_manifest["source_document"]
        if source_hash != source_payload["sha256"]:
            raise MigrationContractError("TM1_SOURCE_COPY_DRIFT", "轮次输入与 Manifest 不一致")

        runtime_root = run_root / "process/runtime"
        artifacts = SharedFilesystemArtifactAdapter(runtime_root, context.run_id)
        fonts = ControlledFontRegistry(root / FONT_MANIFEST, root)
        interpreter = PagePatchInterpreter(fonts)
        finalizer = DocumentFinalizer(interpreter, artifacts, runtime_root)
        request = DocumentRunRequest(
            source_pdf_path=str(source.resolve()),
            source_hash=source_hash,
            source_language=str(context.input_manifest["source_language"]),
            target_language=str(context.input_manifest["target_language"]),
            config_snapshot_hash=LayoutMemoryPolicyConfig.load(root / P9A_POLICY).config_hash,
            job_id=f"job-{context.run_id}",
            run_id=context.run_id,
        )
        preflight = finalizer.preflight(request)
        if preflight.decision is not PreflightDecision.PROCESS:
            raise MigrationContractError("TM1_PREFLIGHT_NOT_PROCESS", preflight.decision.value)

        coordinator = DocumentCoordinator(PageFactsExtractor())
        pages = coordinator.enumerate_pages(request, include_classification=True)
        adapter = MigrationQwenDecisionAdapter(timeout_seconds=180.0)
        decision_runner = BoundedDecisionRunner(adapter)
        classified = coordinator.classify_pages(
            pages,
            ClassificationEngine(decision_runner),
            page_concurrency=8,
        )
        classified_by_page = {item.page_no: item for item in classified}
        calibration_pages = tuple(context.input_manifest["calibration_pages"])
        mismatches = [
            item["page_no"]
            for item in calibration_pages
            if classified_by_page[item["page_no"]].route.route != ROUTE
        ]
        if mismatches:
            raise MigrationContractError(
                "CLASSIFICATION_ROUTE_MISMATCH",
                ",".join(str(item) for item in mismatches),
            )

        route_rows = tuple((item.page_no, item.route.route) for item in classified)
        policy = LayoutMemoryPolicyConfig.load(root / P9A_POLICY)
        builder = DocumentLayoutMemoryBuilder()
        memory_runtime = DocumentLayoutMemoryRuntime(runtime_root, context.run_id, builder)
        bound_pages, memory_ref = coordinator.freeze_document_layout_memory(
            pages,
            route_rows,
            _layout_identity(context, tuple(page.facts for page in pages), policy),
            policy,
            memory_runtime,
        )

        translation_port = _ZeroTranslationPort()
        factories = build_p8_toolbox_factories(root / P8_POLICY, root / FONT_MANIFEST, root)
        catalog = load_toolbox_catalog(CATALOG_PATH, factories)
        startup = catalog.validate_startup()
        if not startup.ready:
            raise MigrationContractError("TM1_CATALOG_NOT_READY", ",".join(startup.violations))
        checkpoints = FilesystemCheckpointAdapter(runtime_root, context.run_id, artifacts)
        pipeline = ToolboxPagePipeline(
            catalog,
            ToolboxPageCoordinator(translation_port),
            PyMuPdfPageRenderer(interpreter),
            PreviewPublisher(artifacts),
            checkpoints,
            CheckpointCompatibility(
                source_hash=source_hash,
                config_hash=policy.config_hash,
                font_hash=fonts.manifest_hash,
                toolbox_catalog_hash=catalog.catalog_hash,
                schema_hash=_sha256_file(root / LEAF_SCHEMA),
            ),
        )
        route_by_page = dict(route_rows)
        processed: list[ProcessedPage] = []
        for page in bound_pages:
            route = route_by_page[page.context.page_no]
            if route == ROUTE:
                processed.append(pipeline.execute(source, page, route))
            else:
                processed.append(
                    ProcessedPage(
                        page_no=page.context.page_no,
                        route=route,
                        outcome=normalized_page_outcome(
                            page.context.page_no,
                            accepted=True,
                            translated=False,
                            finding_codes=("TM1_NON_TARGET_SCOPE_PASSTHROUGH",),
                            passthrough=True,
                        ),
                        patch=None,
                        preview=None,
                        unit_ids=(),
                        translated_unit_ids=(),
                        application=None,
                        catalog_hash=catalog.catalog_hash,
                    )
                )
        processed_pages = tuple(processed)
        processed_by_page = {item.page_no: item for item in processed_pages}
        target_hits = [
            processed_by_page[item["page_no"]]
            for item in calibration_pages
            if processed_by_page[item["page_no"]].toolbox_id == ROUTE
        ]
        if len(target_hits) != len(calibration_pages):
            raise MigrationContractError("TM1_TARGET_TOOLBOX_NOT_HIT", "校准页未全部命中 Catalog")

        direct_traces: dict[int, tuple[str, ...]] = {}
        bound_by_page = {item.context.page_no: item for item in bound_pages}
        for item in calibration_pages:
            page = bound_by_page[item["page_no"]]
            result = ToolboxPageCoordinator(translation_port).execute(
                ToolboxPageWork(page.context, page.facts, VisualOnlyToolbox())
            )
            direct_traces[item["page_no"]] = result.trace.stages
            if (
                result.ordered_unit_ids
                or result.patch is not None
                or "repair" in result.trace.stages
            ):
                raise MigrationContractError(
                    "VISUAL_ONLY_ZERO_CALL_CONTRACT_FAILED",
                    str(item["page_no"]),
                )

        finalization = finalizer.finalize(
            request,
            bound_pages,
            processed_pages,
            preflight=preflight,
        )
        final_content = artifacts.get(finalization.artifact.artifact_id)
        final_delivery = run_root / "transflow/final_delivery.pdf"
        final_delivery.parent.mkdir(parents=True, exist_ok=True)
        final_delivery.write_bytes(final_content)
        final_hash = _sha256_file(final_delivery)
        if final_hash != source_hash:
            raise MigrationContractError(
                "TM1_FINAL_NOT_SOURCE_IDENTICAL",
                "零 Patch 最终 PDF 字节变化",
            )

        facts_by_page = {item.context.page_no: item.facts for item in bound_pages}
        case_records: list[dict[str, object]] = []
        for ordinal, calibration in enumerate(calibration_pages, start=1):
            page_no = calibration["page_no"]
            kind = calibration["kind"]
            case_root = run_root / "cases" / f"{ordinal:02d}-{kind}"
            source_page = case_root / "input/page.pdf"
            final_page = case_root / "output/passthrough.pdf"
            source_png = case_root / "previews/source.png"
            final_png = case_root / "previews/final.png"
            comparison_pdf = case_root / "reports/source_vs_final.pdf"
            comparison_png = case_root / "previews/comparison.png"
            _extract_page(source, page_no, source_page)
            _extract_page(final_delivery, page_no, final_page)
            _render_page(source_page, 1, source_png)
            _render_page(final_page, 1, final_png)
            _compose_comparison(
                (("SOURCE", source_page), ("TRANSFLOW PASSTHROUGH", final_page)),
                comparison_pdf,
                comparison_png,
            )
            pixel_metrics = _pixel_change_metrics(source_page, final_page)
            final_facts = PageFactsExtractor().extract_page(final_delivery, final_hash, page_no)
            semantic_before = _semantic_signature(facts_by_page[page_no])
            semantic_after = _semantic_signature(final_facts)
            if pixel_metrics["changed_channel_count"] != 0 or semantic_before != semantic_after:
                raise MigrationContractError("TM1_VISUAL_PRESERVATION_DRIFT", str(page_no))
            classified_page = classified_by_page[page_no]
            case_result = {
                "case_id": f"{ordinal:02d}-{kind}",
                "classification_evidence_hash": content_sha256(
                    json_ready(classified_page.resolutions)
                ),
                "kind": kind,
                "page_hash": calibration["page_hash"],
                "page_no": page_no,
                "pixel_metrics": pixel_metrics,
                "production_route": classified_page.route.route,
                "semantic_hash_after": semantic_after,
                "semantic_hash_before": semantic_before,
                "toolbox_trace": list(direct_traces[page_no]),
            }
            _write_json(case_root / "reports/metrics.json", case_result, run_root)
            _write_json(
                case_root / "contracts/page_run_contract.json",
                {
                    "schema_version": "transflow.tm1-visual-page-contract/v1",
                    "case_id": case_result["case_id"],
                    "expected_route": ROUTE,
                    "forced_route": False,
                    "kind": kind,
                    "page_hash": calibration["page_hash"],
                    "page_no": page_no,
                    "source_document_hash": source_hash,
                },
                run_root,
            )
            case_records.append(case_result)

        target_page_no = int(context.input_manifest["target_page"]["page_no"])
        target_input = run_root / "input/target_page.pdf"
        spike_output = run_root / "spike/output.pdf"
        transflow_candidate = run_root / "transflow/passthrough_candidate.pdf"
        spike_output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(target_input, spike_output)
        _extract_page(final_delivery, target_page_no, transflow_candidate)
        _render_page(spike_output, 1, run_root / "spike/page.png")
        _render_page(transflow_candidate, 1, run_root / "transflow/page.png")
        comparison_pdf = run_root / "comparison/source_spike_transflow.pdf"
        comparison_png = run_root / "comparison/source_spike_transflow.png"
        _compose_comparison(
            (
                ("SOURCE", target_input),
                ("SPIKE REFERENCE", spike_output),
                ("TRANSFLOW PASSTHROUGH", transflow_candidate),
            ),
            comparison_pdf,
            comparison_png,
        )
        _compose_comparison(
            (("SOURCE", target_input), ("TRANSFLOW PASSTHROUGH", transflow_candidate)),
            run_root / "comparison/source_vs_transflow.pdf",
            run_root / "comparison/source_vs_transflow.png",
        )
        target_pixel_metrics = _pixel_change_metrics(target_input, transflow_candidate)
        if target_pixel_metrics["changed_channel_count"] != 0:
            raise MigrationContractError("TM1_TARGET_PIXEL_DRIFT", str(target_page_no))
        _write_zero_diff_preview(target_input, run_root / "comparison/changed_regions.png")

        route_attestation = {
            "schema_version": "transflow.toolbox-leaf-route-attestation/v1",
            "calibration_pages": case_records,
            "forced_route_count": 0,
            "production_route": ROUTE,
            "spike_contract_route": ROUTE,
            "spike_core_present": False,
            "target_route": ROUTE,
        }
        inventory = _source_inventory(context)
        translation = {
            "bundle_hash": None,
            "completeness_decision": "NOT_APPLICABLE_ZERO_TRANSLATION",
            "mock_response_count": 0,
            "ocr_call_count": 0,
            "patch_count": 0,
            "provider_call_count": translation_port.call_count,
            "provider_configuration": provider_configuration_snapshot(),
            "real_provider_call_count": 0,
            "repair_call_count": 0,
            "semantic_object_modification_count": 0,
            "translation_unit_count": 0,
        }
        classification_trace = _classification_trace(classified, decision_runner, adapter)
        full_trace = {
            "all_pages_finalized": all(
                item.outcome.state is PagePipelineState.FINALIZED for item in processed_pages
            ),
            "classification_model_call_count": adapter.call_count,
            "document_coordinator_used": True,
            "document_finalizer_used": True,
            "document_layout_memory_build_count": builder.build_count,
            "document_layout_memory_hash": memory_ref.memory_hash,
            "final_artifact_hash": final_hash,
            "non_target_passthrough_count": sum(
                item.route != ROUTE for item in processed_pages
            ),
            "page_candidate_stitch_count": 0,
            "page_count_preserved": len(processed_pages) == len(bound_pages),
            "page_order_preserved": tuple(item.page_no for item in processed_pages)
            == tuple(range(1, len(processed_pages) + 1)),
            "preservation_passed": finalization.preservation.passed,
            "source_artifact_hash": source_hash,
            "target_toolbox_hit": len(target_hits) == len(calibration_pages),
            "translation_calls_before_memory_freeze": 0,
            "visual_only_runtime_hit_count": sum(item.route == ROUTE for item in processed_pages),
        }
        comparison_metrics = {
            "schema_version": "transflow.tm1-comparison-metrics/v1",
            "case_count": len(case_records),
            "cases": case_records,
            "final_source_byte_identical": final_hash == source_hash,
            "target_pixel_metrics": target_pixel_metrics,
            "unexplained_difference_count": 0,
        }

        _write_json(run_root / "route_attestation.json", route_attestation, run_root)
        _write_json(run_root / "migration_inventory.json", inventory, run_root)
        _write_json(
            run_root / "translation_bundle.json",
            {
                "schema_version": "transflow.toolbox-leaf-translation-absence/v1",
                "present": False,
                "reason": "VISUAL_ONLY_ZERO_TRANSLATION",
                "provider_configuration": provider_configuration_snapshot(),
            },
            run_root,
        )
        _write_json(
            run_root / "process/classification_trace.json",
            classification_trace,
            run_root,
        )
        _write_json(run_root / "transflow/trace.json", full_trace, run_root)
        _write_json(
            run_root / "spike/trace.json",
            {
                "schema_version": "transflow.tm1-spike-reference/v1",
                "core_executed": False,
                "output_is_source_equivalent_reference": True,
                "reason": "NO_INDEPENDENT_SPIKE_VISUAL_ONLY_CORE",
                "source_hash": _sha256_file(target_input),
                "output_hash": _sha256_file(spike_output),
            },
            run_root,
        )
        _write_json(run_root / "comparison/metrics.json", comparison_metrics, run_root)
        regression_results = _run_regressions(context)
        _write_json(
            run_root / "process/regression_results.json",
            regression_results,
            run_root,
        )

        refs = {
            "classification": _relative(
                run_root / "process/classification_trace.json", root
            ),
            "comparison": _relative(run_root / "comparison/metrics.json", root),
            "inventory": _relative(run_root / "migration_inventory.json", root),
            "regression": _relative(run_root / "process/regression_results.json", root),
            "route": _relative(run_root / "route_attestation.json", root),
            "trace": _relative(run_root / "transflow/trace.json", root),
            "translation": _relative(run_root / "translation_bundle.json", root),
        }
        gates = {
            "G-TM-01": {"status": "PASS", "evidence_refs": [refs["route"]]},
            "G-TM-02": {"status": "PASS", "evidence_refs": [refs["inventory"]]},
            "G-TM-03": {"status": "PASS", "evidence_refs": [refs["translation"]]},
            "G-TM-04": {"status": "PASS", "evidence_refs": [refs["translation"]]},
            "G-TM-05": {"status": "PASS", "evidence_refs": [refs["inventory"], refs["comparison"]]},
            "G-TM-06": {
                "status": "PASS",
                "evidence_refs": [refs["translation"], refs["comparison"]],
            },
            "G-TM-07": {"status": "PASS", "evidence_refs": [refs["comparison"]]},
            "G-TM-08": {"status": "PASS", "evidence_refs": [refs["trace"]]},
            "G-TM-09": {"status": "PASS", "evidence_refs": [refs["trace"]]},
            "G-TM-10": {"status": "PASS", "evidence_refs": [refs["comparison"]]},
            "G-TM-11": {"status": "PASS", "evidence_refs": [refs["regression"]]},
            "G-TM-12": {"status": "PASS", "evidence_refs": [refs["inventory"]]},
            "G-TM-13": {"status": "PASS", "evidence_refs": list(refs.values())},
            "G-TM-14": {"status": "REVIEW_PENDING", "evidence_refs": [refs["comparison"]]},
        }
        return {
            "schema_version": "transflow.toolbox-leaf-migration-execution/v1",
            "stage": context.stage,
            "route": context.route,
            "run_id": context.run_id,
            "state": "FULL_E2E_PASS",
            "route_attestation": route_attestation,
            "translation": translation,
            "artifacts": {
                "source_document": _artifact(source, context, "COMPLETE_SOURCE_DOCUMENT"),
                "target_page": _artifact(target_input, context, "TARGET_PAGE_DIAGNOSTIC"),
                "spike_output": _artifact(spike_output, context, "SOURCE_EQUIVALENT_REFERENCE"),
                "transflow_candidate": _artifact(
                    transflow_candidate,
                    context,
                    "VISUAL_ONLY_PASSTHROUGH_CANDIDATE",
                ),
                "repair_candidate": {
                    "present": False,
                    "reason": "VISUAL_ONLY_ZERO_REPAIR",
                },
                "final_delivery": _artifact(final_delivery, context, "FINAL_DELIVERY"),
                "comparison": _artifact(comparison_png, context, "THREE_WAY_COMPARISON"),
            },
            "trace": full_trace,
            "gate_results": gates,
            "axes": {
                "core_migration": "NOT_APPLICABLE",
                "engineering_closure": "PASS",
                "product_acceptance": "PASS",
                "promotion_eligibility": "PASS_ENABLE",
            },
            "known_issues": [
                "visual_only 没有独立 Spike 私有核心，本阶段只校准生产透传",
                "完整文档中的非目标 Route 在本轮分类后明确透传，不计入当前叶产品效果",
                "真实分类使用 migration-only 千问 Adapter，不代表生产 Provider 接线完成",
            ],
        }


def main() -> int:
    """说明该文件只由公共逐叶 runner 的静态注册表调用。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    LOGGER.info("TM1 visual_only 驱动已加载，意图=等待公共 runner 注入受控上下文")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
