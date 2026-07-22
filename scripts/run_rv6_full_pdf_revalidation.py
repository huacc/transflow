"""执行 RV6 两份完整 PDF 的当前链路重新验收。"""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import shutil
import subprocess
import sys
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pymupdf
from PIL import Image, ImageDraw

from tests.migration.p9_qwen_translation_adapter import (
    MigrationQwenTranslationAdapter,
    migration_translation_environment_ready,
)
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
from transflow.application.toolbox_page_coordinator import ToolboxPageCoordinator
from transflow.application.toolbox_page_pipeline import ToolboxPagePipeline
from transflow.classification.decision_adapter import BoundedDecisionRunner
from transflow.classification.engine import ClassificationEngine, ClassifiedPage
from transflow.domain.common import content_sha256, json_ready
from transflow.domain.jobs import DocumentRunRequest
from transflow.domain.layout_memory import DocumentLayoutMemoryIdentity
from transflow.domain.states import (
    CheckpointCompatibility,
    Fallback,
    PagePipelineState,
    Quality,
    TranslationCoverage,
)
from transflow.pdf_kernel import (
    ControlledFontRegistry,
    ExtractedPageFacts,
    PageFactsExtractor,
    PagePatchInterpreter,
    PyMuPdfPageRenderer,
)
from transflow.pdf_kernel.preservation import PreflightDecision
from transflow.toolboxes.catalog import load_toolbox_catalog
from transflow.toolboxes.leaves import build_p8_toolbox_factories
from transflow.toolboxes.leaves.body_flow_text_single.judge import (
    inspect_materialized_candidate,
)
from transflow.toolboxes.leaves.body_flow_text_single.prompt import (
    single_translation_system_prompt,
)

LOGGER = logging.getLogger("transflow.rv6.full_pdf_revalidation")
RUN_ROOT = REPO_ROOT / "runs/critical_chain_revalidation/RV6"
CATALOG_PATH = REPO_ROOT / "resources/catalogs/page_toolbox_catalog_v4.json"
FONT_MANIFEST = REPO_ROOT / "resources/manifests/font_manifest.json"
P8_POLICY = REPO_ROOT / "resources/manifests/p8_toolbox_policy.json"
P9A_POLICY = REPO_ROOT / "resources/manifests/p9a_layout_policy.json"
LAYOUT_SCHEMA = REPO_ROOT / "resources/schemas/document_layout_memory_v1.schema.json"
LEAF_SCHEMA = REPO_ROOT / "resources/schemas/leaf_migration_evidence_v1.schema.json"
CURRENT_POINTER = REPO_ROOT / "resources/manifests/rv6_gate.json"
TARGET_ROUTE = "body.flow_text.single"
VISUAL_ROUTE = "visual_only"
HAN = re.compile(r"[\u3400-\u9fff]")


@dataclass(frozen=True, slots=True)
class InputSpec:
    """声明冻结前可见的完整 PDF 身份，不包含任何页级判定。"""

    document_id: str
    source: Path
    role: str
    selection_reason: str
    focus_pages: tuple[int, ...] = ()


INPUT_SPECS = (
    InputSpec(
        "tm2-baseline",
        REPO_ROOT
        / "runs/critical_chain_revalidation/RV0/01-baseline-20260721-164419/"
        "input/source_document.pdf",
        "CURRENT_TM2_UNSPLIT_FULL_PDF",
        "RV0 已冻结的 TM2/08 权威完整输入",
        (101, 148, 149, 150, 151, 152),
    ),
    InputSpec(
        "blind-annual-08210",
        REPO_ROOT / "样本/年报/08210_DLC ASIA_英文_2025.pdf",
        "NEW_BLIND_UNSPLIT_FULL_PDF",
        "按文件大小事前冻结的 12 份最小英文候选中，满足 Preservation 可修改、英文占比"
        "超过 99% 且未命中既有关键链路文件名或 SHA-256 证据的最小页数完整年报（98 页）",
    ),
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(json_ready(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _relative(path: Path, root: Path = REPO_ROOT) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _next_run_dir() -> Path:
    RUN_ROOT.mkdir(parents=True, exist_ok=True)
    ordinals = []
    for path in RUN_ROOT.iterdir():
        match = re.match(r"^(\d+)-", path.name)
        if path.is_dir() and match:
            ordinals.append(int(match.group(1)))
    ordinal = max(ordinals, default=0) + 1
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return RUN_ROOT / f"{ordinal:02d}-full-pdf-current-chain-{stamp}"


def _document_runtime_root(run_root: Path, document_id: str) -> Path:
    """返回受 Windows 路径预算约束的 Checkpoint 与 Artifact 工作目录。"""

    return run_root / "_runtime" / document_id


def _document_run_id(run_root: Path, document_id: str) -> str:
    """以短哈希保持内部身份唯一，避免最终 Artifact 文件名突破 Windows 路径预算。"""

    run_hash = hashlib.sha256(run_root.name.encode("utf-8")).hexdigest()[:12]
    document_hash = hashlib.sha256(document_id.encode("utf-8")).hexdigest()[:8]
    return f"rv6-{run_hash}-d{document_hash}"


def _freeze_inputs(run_root: Path) -> tuple[dict[str, Any], ...]:
    """先复制和哈希两份整本输入，再允许任何页级提取或模型调用。"""

    frozen: list[dict[str, Any]] = []
    for spec in INPUT_SPECS:
        if not spec.source.is_file():
            raise FileNotFoundError(spec.source.name)
        target = run_root / "input/documents" / spec.document_id / "source.pdf"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(spec.source, target)
        source_hash = _sha256(target)
        with pymupdf.open(target) as document:
            page_count = document.page_count
        frozen.append(
            {
                "blind_frozen_before_page_inspection": spec.role.startswith("NEW_BLIND"),
                "document_id": spec.document_id,
                "focus_pages": list(spec.focus_pages),
                "page_count": page_count,
                "role": spec.role,
                "selection_reason": spec.selection_reason,
                "sha256": source_hash,
                "size_bytes": target.stat().st_size,
                "source_origin": _relative(spec.source),
                "source_pdf": _relative(target, run_root),
            }
        )
    manifest = {
        "business_input": "TWO_UNSPLIT_COMPLETE_PDFS",
        "documents": frozen,
        "frozen_before_classification": True,
        "schema_version": "transflow.rv6-input-freeze/v1",
    }
    _write_json(run_root / "input/input_freeze.json", manifest)
    return tuple(frozen)


def _layout_identity(
    facts: tuple[ExtractedPageFacts, ...],
    policy: LayoutMemoryPolicyConfig,
    catalog_hash: str,
) -> DocumentLayoutMemoryIdentity:
    return DocumentLayoutMemoryIdentity(
        source_hash=facts[0].page.source_hash,
        source_language="en",
        target_language="zh-CN",
        page_geometry_hash=derive_page_geometry_hash(facts),
        config_hash=policy.config_hash,
        builder_hash=_sha256(
            REPO_ROOT / "src/transflow/application/document_layout_memory.py"
        ),
        classifier_hash=_sha256(REPO_ROOT / "src/transflow/classification/engine.py"),
        catalog_hash=catalog_hash,
        kernel_hash=_sha256(REPO_ROOT / "src/transflow/pdf_kernel/facts.py"),
        patch_interpreter_hash=_sha256(REPO_ROOT / "src/transflow/pdf_kernel/patch.py"),
        font_hash=_sha256(FONT_MANIFEST),
        schema_hash=_sha256(LAYOUT_SCHEMA),
    )


def _classification_trace(
    classified: tuple[ClassifiedPage, ...],
    runner: BoundedDecisionRunner,
    audit_start: int,
    model_calls: int,
) -> dict[str, Any]:
    return {
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
            for item in runner.audits[audit_start:]
        ],
        "model_call_count": model_calls,
        "pages": [
            {
                "classification_evidence_hash": content_sha256(json_ready(item.resolutions)),
                "page_identity": item.page_identity,
                "page_no": item.page_no,
                "route": item.route.as_dict(),
            }
            for item in classified
        ],
        "raw_provider_response_persisted": False,
        "route_distribution": dict(
            sorted(Counter(item.route.route for item in classified).items())
        ),
        "schema_version": "transflow.rv6-classification-trace/v1",
    }


def _normalize(value: str) -> str:
    return "".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _source_residual_count(processed: ProcessedPage, final_text: str) -> int:
    checkpoint = processed.translation_checkpoint
    if not isinstance(checkpoint, dict):
        return 1
    semantic_map = checkpoint.get("semantic_map")
    if not isinstance(semantic_map, dict):
        return 1
    normalized_final = _normalize(final_text)
    count = 0
    for entry in semantic_map.get("entries", []):
        if not isinstance(entry, dict) or entry.get("disposition") != "TRANSLATE":
            continue
        source_text = str(entry.get("source_text", "")).strip()
        if len(source_text) >= 24 and re.search(r"[A-Za-z]{4}", source_text):
            count += int(_normalize(source_text) in normalized_final)
    return count


def _render_hash(page: pymupdf.Page) -> str:
    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(1.0, 1.0), alpha=False)
    return hashlib.sha256(pixmap.samples).hexdigest()


def _save_page(document: pymupdf.Document, index: int, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open() as single:
        single.insert_pdf(document, from_page=index, to_page=index)
        single.save(target, garbage=4, deflate=True)


def _render_page(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open(source) as document:
        pixmap = document[0].get_pixmap(matrix=pymupdf.Matrix(2.0, 2.0), alpha=False)
        pixmap.save(target)


def _compose_triptych(source: Path, candidate: Path, final: Path, target: Path) -> None:
    images = [Image.open(path).convert("RGB") for path in (source, candidate, final)]
    try:
        label_height = 38
        width = sum(image.width for image in images)
        height = max(image.height for image in images) + label_height
        canvas = Image.new("RGB", (width, height), "white")
        draw = ImageDraw.Draw(canvas)
        x = 0
        for label, image in zip(("SOURCE", "CANDIDATE", "FINAL"), images, strict=True):
            draw.text((x + 12, 12), label, fill="black")
            canvas.paste(image, (x, label_height))
            x += image.width
        target.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(target)
    finally:
        for image in images:
            image.close()


def _choose_visual_pages(
    pages: list[dict[str, Any]], focus_pages: tuple[int, ...]
) -> tuple[int, ...]:
    """选关键页、自然 single 页和不同 disabled Route；不按运行结果改 Gate。"""

    available = {int(item["page_no"]) for item in pages}
    selected = [page for page in focus_pages if page in available]
    targets = [int(item["page_no"]) for item in pages if item["route"] == TARGET_ROUTE]
    if targets:
        for index in (0, len(targets) // 2, len(targets) - 1):
            selected.append(targets[index])
    visuals = [int(item["page_no"]) for item in pages if item["route"] == VISUAL_ROUTE]
    selected.extend(visuals[:2])
    seen_routes: set[str] = set()
    for item in pages:
        route = str(item["route"])
        if route in {TARGET_ROUTE, VISUAL_ROUTE} or route in seen_routes:
            continue
        seen_routes.add(route)
        selected.append(int(item["page_no"]))
        if len(seen_routes) == 4:
            break
    return tuple(dict.fromkeys(selected))[:14]


def _write_visual_artifacts(
    document_root: Path,
    page_rows: list[dict[str, Any]],
    focus_pages: tuple[int, ...],
) -> tuple[str, ...]:
    artifacts: list[str] = []
    for page_no in _choose_visual_pages(page_rows, focus_pages):
        page_root = document_root / "pages" / f"p{page_no:04d}"
        source_png = page_root / "visual/source.png"
        final_png = page_root / "visual/final.png"
        triptych = page_root / "visual/source-candidate-final.png"
        _render_page(page_root / "input/source.pdf", source_png)
        _render_page(page_root / "output/final.pdf", final_png)
        _compose_triptych(
            source_png,
            page_root / "process/candidate.png",
            final_png,
            triptych,
        )
        artifacts.append(_relative(triptych, document_root))
    _write_json(
        document_root / "process/visual_review.json",
        {
            "artifacts": artifacts,
            "reviewed_page_count": 0,
            "schema_version": "transflow.rv6-visual-review/v1",
            "status": "PENDING",
        },
    )
    return tuple(artifacts)


def _execute_document(
    frozen: dict[str, Any],
    run_root: Path,
    decision_adapter: MigrationQwenDecisionAdapter,
    decision_runner: BoundedDecisionRunner,
    translation_adapter: MigrationQwenTranslationAdapter,
) -> dict[str, Any]:
    document_id = str(frozen["document_id"])
    document_root = run_root / "documents" / document_id
    source = run_root / str(frozen["source_pdf"])
    runtime_root = _document_runtime_root(run_root, document_id)
    run_id = _document_run_id(run_root, document_id)
    source_hash = _sha256(source)
    if source_hash != frozen["sha256"]:
        raise RuntimeError(f"RV6_SOURCE_COPY_DRIFT:{document_id}")

    policy = LayoutMemoryPolicyConfig.load(P9A_POLICY)
    artifacts = SharedFilesystemArtifactAdapter(runtime_root, run_id)
    fonts = ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT)
    interpreter = PagePatchInterpreter(fonts)
    finalizer = DocumentFinalizer(interpreter, artifacts, runtime_root)
    request = DocumentRunRequest(
        source_pdf_path=str(source.resolve()),
        source_hash=source_hash,
        source_language="en",
        target_language="zh-CN",
        config_snapshot_hash=policy.config_hash,
        job_id=f"job-{run_id}",
        run_id=run_id,
    )
    preflight = finalizer.preflight(request)
    if preflight.decision is not PreflightDecision.PROCESS:
        reasons = ",".join(preflight.reason_codes) or "NO_REASON"
        raise RuntimeError(
            f"RV6_PREFLIGHT_NOT_PROCESS:{document_id}:{preflight.decision.value}:{reasons}"
        )

    coordinator = DocumentCoordinator(PageFactsExtractor())
    pages = coordinator.enumerate_pages(request, include_classification=True)
    model_before = decision_adapter.call_count
    audit_before = len(decision_runner.audits)
    classified = coordinator.classify_pages(
        pages,
        ClassificationEngine(decision_runner),
        page_concurrency=8,
    )
    model_calls = decision_adapter.call_count - model_before
    _write_json(
        document_root / "process/classification_trace.json",
        _classification_trace(classified, decision_runner, audit_before, model_calls),
    )

    factories = build_p8_toolbox_factories(P8_POLICY, FONT_MANIFEST, REPO_ROOT)
    catalog = load_toolbox_catalog(CATALOG_PATH, factories)
    startup = catalog.validate_startup()
    if not startup.ready:
        raise RuntimeError("RV6_CATALOG_NOT_READY:" + ",".join(startup.violations))
    route_rows = tuple((item.page_no, item.route.route) for item in classified)
    builder = DocumentLayoutMemoryBuilder()
    memory_runtime = DocumentLayoutMemoryRuntime(runtime_root, run_id, builder)
    bound_pages, memory_ref = coordinator.freeze_document_layout_memory(
        pages,
        route_rows,
        _layout_identity(tuple(page.facts for page in pages), policy, catalog.catalog_hash),
        policy,
        memory_runtime,
    )
    memory_runtime.load_readonly(memory_ref)

    checkpoints = FilesystemCheckpointAdapter(runtime_root, run_id, artifacts)
    page_coordinator = ToolboxPageCoordinator(translation_adapter)
    pipeline = ToolboxPagePipeline(
        catalog,
        page_coordinator,
        PyMuPdfPageRenderer(interpreter),
        PreviewPublisher(artifacts),
        checkpoints,
        CheckpointCompatibility(
            source_hash=source_hash,
            config_hash=policy.config_hash,
            font_hash=fonts.manifest_hash,
            toolbox_catalog_hash=catalog.catalog_hash,
            schema_hash=_sha256(LEAF_SCHEMA),
        ),
    )
    classified_by_identity = {item.page_identity: item for item in classified}
    translation_before = translation_adapter.call_count
    processed: list[ProcessedPage] = []
    for ordinal, page in enumerate(bound_pages, start=1):
        classification_route = classified_by_identity[page.facts.page_identity].route
        processed.append(pipeline.execute(source, page, classification_route))
        if ordinal % 20 == 0 or ordinal == len(bound_pages):
            LOGGER.info(
                "RV6 完整文档逐页终态进度 document=%s pages=%s/%s",
                document_id,
                ordinal,
                len(bound_pages),
            )
    processed_pages = tuple(processed)
    translation_calls = translation_adapter.call_count - translation_before
    finalization = finalizer.finalize(
        request,
        bound_pages,
        processed_pages,
        preflight=preflight,
    )
    final_path = document_root / "output/final.pdf"
    final_path.parent.mkdir(parents=True, exist_ok=True)
    final_path.write_bytes(artifacts.get(finalization.artifact.artifact_id))

    catalog_payload = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    entries = {str(item["route"]): item for item in catalog_payload["entries"]}
    page_rows: list[dict[str, Any]] = []
    degradation_rows: list[dict[str, Any]] = []
    checkpoint_payload = json.loads(
        (runtime_root / "job/checkpoint_manifest.json").read_text(encoding="utf-8")
    )
    checkpoints_complete = len(checkpoint_payload["pages"]) == len(bound_pages)
    with pymupdf.open(source) as source_document, pymupdf.open(final_path) as final_document:
        openable = final_document.page_count == len(bound_pages)
        for index, (page, result) in enumerate(zip(bound_pages, processed_pages, strict=True)):
            page_no = page.context.page_no
            page_root = document_root / "pages" / f"p{page_no:04d}"
            source_page_path = page_root / "input/source.pdf"
            final_page_path = page_root / "output/final.pdf"
            _save_page(source_document, index, source_page_path)
            _save_page(final_document, index, final_page_path)
            candidate_path = page_root / "process/candidate.png"
            if result.preview is not None:
                candidate_path.parent.mkdir(parents=True, exist_ok=True)
                candidate_path.write_bytes(artifacts.get(result.preview.artifact_id))

            route = result.route
            entry = entries.get(route)
            enabled = bool(entry and entry["enabled"])
            source_render_hash = _render_hash(source_document[index])
            final_render_hash = _render_hash(final_document[index])
            final_text = final_document[index].get_text("text")
            residual_count = 0
            materialized: dict[str, Any] | None = None
            target_pass = False
            if route == TARGET_ROUTE:
                residual_count = _source_residual_count(result, final_text)
                if result.patch is not None:
                    inspected = inspect_materialized_candidate(final_path, page.facts, result.patch)
                    materialized = asdict(inspected)
                    materialized["materialization_rate"] = inspected.materialization_rate
                    materialized["passed"] = inspected.passed
                target_pass = bool(
                    result.patch is not None
                    and result.outcome.translation_coverage is TranslationCoverage.FULL
                    and result.outcome.quality is Quality.PASS
                    and result.outcome.fallback is Fallback.NONE
                    and materialized
                    and materialized["passed"]
                    and HAN.search(final_text)
                    and residual_count == 0
                    and source_render_hash != final_render_hash
                    and not finalization.document_passthrough
                )
            visual_pass = bool(
                route == VISUAL_ROUTE
                and enabled
                and result.patch is None
                and result.outcome.quality is Quality.PASS
                and result.preview is not None
            )
            if not enabled:
                degradation_rows.append(
                    {
                        "catalog_disabled_reason": (
                            entry.get("disabled_reason") if entry is not None else None
                        ),
                        "finding_codes": list(result.outcome.finding_codes),
                        "page_no": page_no,
                        "registered": entry is not None,
                        "route": route,
                    }
                )
            row = {
                "candidate_artifact_present": result.preview is not None,
                "classification": result.classification_route.as_dict()
                if result.classification_route is not None
                else None,
                "enabled": enabled,
                "fallback": result.outcome.fallback.value,
                "finding_codes": list(result.outcome.finding_codes),
                "final_page_sha256": _sha256(final_page_path),
                "finalized": result.outcome.state is PagePipelineState.FINALIZED,
                "materialized_judgement": materialized,
                "page_identity": page.facts.page_identity,
                "page_no": page_no,
                "patch_present": result.patch is not None,
                "quality": result.outcome.quality.value,
                "route": route,
                "source_page_sha256": _sha256(source_page_path),
                "source_residual_count": residual_count,
                "target_pass": target_pass if route == TARGET_ROUTE else None,
                "translation_coverage": result.outcome.translation_coverage.value,
                "translation_unit_count": len(result.unit_ids),
                "visual_only_pass": visual_pass if route == VISUAL_ROUTE else None,
            }
            _write_json(page_root / "process/summary.json", row)
            page_rows.append(row)

    _write_json(
        document_root / "process/degradation_manifest.json",
        {
            "pages": degradation_rows,
            "schema_version": "transflow.rv6-degradation-manifest/v1",
        },
    )
    _write_json(
        document_root / "process/pages.json",
        {"pages": page_rows, "schema_version": "transflow.rv6-pages/v1"},
    )
    visual_artifacts = _write_visual_artifacts(
        document_root,
        page_rows,
        tuple(int(item) for item in frozen["focus_pages"]),
    )

    target_rows = [item for item in page_rows if item["route"] == TARGET_ROUTE]
    visual_rows = [item for item in page_rows if item["route"] == VISUAL_ROUTE]
    disabled_rows = [item for item in page_rows if not item["enabled"]]
    unregistered = [
        item
        for item in disabled_rows
        if item["classification"]["route"] == "unclassified"
    ]
    trace_files_complete = all(
        (document_root / "pages" / f"p{int(item['page_no']):04d}" / relative).is_file()
        for item in page_rows
        for relative in (
            "input/source.pdf",
            "process/summary.json",
            "process/candidate.png",
            "output/final.pdf",
        )
    )
    return {
        "all_pages_finalized": all(item["finalized"] for item in page_rows),
        "blind_input": bool(frozen["blind_frozen_before_page_inspection"]),
        "catalog_hash": catalog.catalog_hash,
        "classification_model_call_count": model_calls,
        "degradation_disclosed_count": len(degradation_rows),
        "disabled_page_count": len(disabled_rows),
        "document_id": document_id,
        "document_layout_memory_build_count": builder.build_count,
        "document_layout_memory_hash": memory_ref.memory_hash,
        "document_passthrough": finalization.document_passthrough,
        "final_pdf": _relative(final_path, run_root),
        "final_sha256": _sha256(final_path),
        "input_process_output_trace_complete": trace_files_complete and checkpoints_complete,
        "openable": openable,
        "page_count": len(page_rows),
        "page_count_preserved": len(page_rows) == int(frozen["page_count"]),
        "page_order_preserved": tuple(item["page_no"] for item in page_rows)
        == tuple(range(1, len(page_rows) + 1)),
        "preservation_passed": finalization.preservation.passed,
        "route_distribution": dict(sorted(Counter(item["route"] for item in page_rows).items())),
        "source_passthrough_masquerade_count": sum(
            item["route"] == TARGET_ROUTE and not item["target_pass"] for item in page_rows
        ),
        "target_page_count": len(target_rows),
        "target_pass_count": sum(bool(item["target_pass"]) for item in target_rows),
        "translation_model_call_count": translation_calls,
        "unregistered_page_count": len(unregistered),
        "visual_artifacts": list(visual_artifacts),
        "visual_only_page_count": len(visual_rows),
        "visual_only_pass_count": sum(bool(item["visual_only_pass"]) for item in visual_rows),
    }


def _run_verification() -> dict[str, Any]:
    commands = (
        (
            "RV6_AND_DOCUMENT_BARRIERS",
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "tests/test_critical_chain_rv6.py",
                "tests/test_p4.py::test_p4_4_t02_finalizer_sorts_shuffled_results_and_rejects_incomplete",
                "tests/test_p4.py::test_p4_5_t02_restart_skips_all_committed_pages",
                "tests/test_p4.py::test_p4_5_t03_last_page_translation_failure_passthroughs_and_completes",
                "tests/test_p4.py::test_p4_5_t05_final_write_failure_retries_without_partial_authority",
                "tests/test_p5.py::test_p5_4_t02_out_of_order_model_responses_merge_by_page_no",
            ],
        ),
        (
            "RV5_TM2_TARGET_CHAIN",
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "tests/test_critical_chain_rv5.py",
                "tests/test_toolbox_leaf_migration_tm2.py",
            ],
        ),
        (
            "P8_P9A_P9B_P9C_CURRENT_TARGET",
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "tests/test_p8.py",
                "tests/test_p9a.py",
                "tests/test_p9b.py::test_p9b_2_t11_real_pdf_reflow_candidate_exists_before_page_finalized",
                "tests/test_p9b.py::test_p9b_3_t06_finalization_uses_last_approved_candidate_only",
                "tests/test_p9b.py::test_p9b_4_t04_full_document_crash_windows_resume_equivalently",
                "tests/test_p9c.py::test_p9c_2_t02_real_single_multi_table_anchor_maps_cover_native_text",
                "tests/test_p9c.py::test_p9c_2_t03_invalid_bundle_content_never_enters_layout_or_full",
                "tests/test_p9c.py::test_p9c_4_t02_real_misroutes_fallback_without_runtime_route_mutation",
            ],
        ),
        (
            "STATIC",
            [
                sys.executable,
                "-m",
                "ruff",
                "check",
                "scripts/run_rv6_full_pdf_revalidation.py",
                "tests/test_critical_chain_rv6.py",
                "src/transflow/toolboxes/leaves/body_flow_text_single/toolbox.py",
                "tests/test_p8.py",
            ],
        ),
        (
            "MYPY",
            [
                sys.executable,
                "-m",
                "mypy",
                "scripts/run_rv6_full_pdf_revalidation.py",
                "src/transflow/toolboxes/leaves/body_flow_text_single",
            ],
        ),
    )
    records = []
    for command_id, command in commands:
        process = subprocess.run(
            command,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
            check=False,
        )
        records.append(
            {
                "command": ["python", *command[2:]],
                "command_id": command_id,
                "return_code": process.returncode,
                "stderr": process.stderr[-4000:],
                "stdout": process.stdout[-4000:],
            }
        )
    return {
        "commands": records,
        "schema_version": "transflow.rv6-verification/v1",
        "status": "PASS" if all(item["return_code"] == 0 for item in records) else "FAIL",
    }


def _evaluate_gates(
    documents: tuple[dict[str, Any], ...], verification_pass: bool
) -> dict[str, Any]:
    page_count = sum(int(item["page_count"]) for item in documents)
    target_count = sum(int(item["target_page_count"]) for item in documents)
    target_pass = sum(int(item["target_pass_count"]) for item in documents)
    disabled_count = sum(int(item["disabled_page_count"]) for item in documents)
    disclosed_count = sum(int(item["degradation_disclosed_count"]) for item in documents)
    masquerade_count = sum(
        int(item["source_passthrough_masquerade_count"]) for item in documents
    )
    structural_pass = bool(
        len(documents) == 2
        and sum(bool(item["blind_input"]) for item in documents) == 1
        and all(
            item["all_pages_finalized"]
            and item["page_count_preserved"]
            and item["page_order_preserved"]
            and item["openable"]
            and item["preservation_passed"]
            and item["input_process_output_trace_complete"]
            for item in documents
        )
    )
    delivery_pass = bool(
        target_count > 0
        and target_count == target_pass
        and masquerade_count == 0
        and disabled_count == disclosed_count
        and all(item["unregistered_page_count"] == 0 for item in documents)
        and all(
            item["visual_only_page_count"] == item["visual_only_pass_count"]
            for item in documents
        )
    )
    return {
        "document_count": len(documents),
        "documents": list(documents),
        "full_pdf_execution_count": len(documents),
        "gates": {
            "G-RV-09": {
                "metrics": {
                    "all_page_terminal_rate": 1.0
                    if page_count
                    and all(item["all_pages_finalized"] for item in documents)
                    else 0.0,
                    "blind_document_count": sum(
                        bool(item["blind_input"]) for item in documents
                    ),
                    "input_process_output_trace_rate": 1.0
                    if page_count
                    and all(item["input_process_output_trace_complete"] for item in documents)
                    else 0.0,
                    "openable_document_count": sum(bool(item["openable"]) for item in documents),
                    "page_count": page_count,
                    "page_count_preserved_document_count": sum(
                        bool(item["page_count_preserved"]) for item in documents
                    ),
                    "page_order_preserved_document_count": sum(
                        bool(item["page_order_preserved"]) for item in documents
                    ),
                },
                "status": "PASS" if structural_pass else "FAIL",
            },
            "G-RV-10": {
                "metrics": {
                    "disabled_disclosed_count": disclosed_count,
                    "disabled_page_count": disabled_count,
                    "source_passthrough_masquerade_count": masquerade_count,
                    "target_page_count": target_count,
                    "target_pass_count": target_pass,
                    "target_translation_coverage_rate": target_pass / max(1, target_count),
                    "unregistered_page_count": sum(
                        int(item["unregistered_page_count"]) for item in documents
                    ),
                },
                "status": "PASS" if delivery_pass else "FAIL",
            },
        },
        "schema_version": "transflow.rv6-mechanical-gate/v1",
        "status": "PASS" if structural_pass and delivery_pass and verification_pass else "FAIL",
        "verification_pass": verification_pass,
        "visual_review": "PENDING",
    }


def _report_text(gate: dict[str, Any], *, final: bool) -> str:
    g9 = gate["gates"]["G-RV-09"]
    g10 = gate["gates"]["G-RV-10"]
    visual = gate.get("visual_review", "PENDING")
    lines = [
        "# RV6 完整 PDF 当前链路重新验收",
        "",
        f"- 结论：{gate['status']}",
        f"- G-RV-09：{g9['status']}",
        f"- G-RV-10：{g10['status']}",
        f"- 视觉复核：{visual}",
        f"- 完整 PDF：{gate['full_pdf_execution_count']} 本",
        f"- 总页数：{g9['metrics']['page_count']}",
        "",
        "## 完整文档结果",
        "",
    ]
    for item in gate["documents"]:
        lines.extend(
            [
                f"### {item['document_id']}",
                "",
                f"- 页数：{item['page_count']}；页数/页序/可打开："
                f"{item['page_count_preserved']}/{item['page_order_preserved']}/{item['openable']}",
                f"- 当前 single 目标页：{item['target_pass_count']}/{item['target_page_count']}",
                f"- disabled 降级披露：{item['degradation_disclosed_count']}/"
                f"{item['disabled_page_count']}",
                f"- 分类/翻译真实模型调用：{item['classification_model_call_count']}/"
                f"{item['translation_model_call_count']}",
                f"- 最终 PDF：`{item['final_pdf']}`",
                "",
            ]
        )
    lines.extend(
        [
            "## 结论边界",
            "",
            "本阶段只要求当前已启用的 body.flow_text.single 自然命中页真实翻译。"
            "visual_only 按零翻译、零写入的已启用叶验收；其余 Route 逐页经过 Catalog 后"
            "显式透传并进入降级清单。",
            "因此本报告不宣称两本年报已经整本翻译完成。尚未迁移的表格、图表、结构图、复合页和 "
            "Freeform 仍需后续逐叶迁移。",
            "",
            "## 已知但未纳入当前目标的回归",
            "",
            "body.table 仍有一条极端超长单元格旧核心回退测试失败。该 Route 在当前 Catalog 中 "
            "disabled，本轮只允许显式降级，未绕过 Catalog 执行；问题保留给对应叶迁移阶段。",
            "",
        ]
    )
    if not final:
        lines.append("机械 Gate 已生成；必须完成三联图人工复核后才能签署最终 Gate。")
    return "\n".join(lines) + "\n"


def _finalize_visual(run_root: Path, visual_status: str) -> int:
    mechanical_path = run_root / "mechanical_gate.json"
    if not mechanical_path.is_file():
        raise FileNotFoundError("RV6_MECHANICAL_GATE_MISSING")
    gate = json.loads(mechanical_path.read_text(encoding="utf-8"))
    if gate["status"] != "PASS" and visual_status == "PASS":
        raise RuntimeError("RV6_MECHANICAL_GATE_NOT_PASS")
    artifacts = [
        f"documents/{item['document_id']}/{path}"
        for item in gate["documents"]
        for path in item["visual_artifacts"]
    ]
    _write_json(
        run_root / "process/visual_review.json",
        {
            "artifacts": artifacts,
            "reviewed_page_count": len(artifacts),
            "reviewer": "codex-manual-visual-inspection",
            "schema_version": "transflow.rv6-visual-review/v1",
            "status": visual_status,
        },
    )
    gate["schema_version"] = "transflow.rv6-gate/v1"
    gate["visual_review"] = visual_status
    gate["status"] = (
        "PASS"
        if gate["gates"]["G-RV-09"]["status"] == "PASS"
        and gate["gates"]["G-RV-10"]["status"] == "PASS"
        and gate["verification_pass"]
        and visual_status == "PASS"
        else "FAIL"
    )
    gate_path = run_root / "gate.json"
    _write_json(gate_path, gate)
    (run_root / "report.md").write_text(_report_text(gate, final=True), encoding="utf-8")
    if gate["status"] == "PASS":
        _write_json(
            CURRENT_POINTER,
            {
                "gate": "G-RV-09+G-RV-10",
                "gate_sha256": _sha256(gate_path),
                "run": _relative(run_root),
                "schema_version": "transflow.current-gate-pointer/v1",
                "status": "PASS",
            },
        )
    print(gate["status"])
    return 0 if gate["status"] == "PASS" else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--finalize-visual", type=Path)
    parser.add_argument("--visual-status", choices=("PASS", "FAIL"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    if args.finalize_visual is not None:
        if args.visual_status is None:
            parser.error("--finalize-visual 必须配合 --visual-status")
        return _finalize_visual(args.finalize_visual.resolve(), args.visual_status)
    if args.visual_status is not None:
        parser.error("--visual-status 只能用于 --finalize-visual")
    if not migration_environment_ready() or not migration_translation_environment_ready():
        raise RuntimeError("RV6_REAL_QWEN_ENVIRONMENT_NOT_CONFIGURED")

    run_root = _next_run_dir()
    run_root.mkdir(parents=True, exist_ok=False)
    frozen = _freeze_inputs(run_root)
    _write_json(
        run_root / "run_manifest.json",
        {
            "run_id": run_root.name,
            "schema_version": "transflow.rv6-run/v1",
            "stage": "RV6",
            "state": "INPUT_FROZEN",
        },
    )
    decision_adapter = MigrationQwenDecisionAdapter(timeout_seconds=180.0)
    decision_runner = BoundedDecisionRunner(decision_adapter)
    translation_adapter = MigrationQwenTranslationAdapter(
        timeout_seconds=180.0,
        chunk_size=48,
        system_prompt=single_translation_system_prompt(),
    )
    documents: list[dict[str, Any]] = []
    try:
        for item in frozen:
            documents.append(
                _execute_document(
                    item,
                    run_root,
                    decision_adapter,
                    decision_runner,
                    translation_adapter,
                )
            )
        verification = _run_verification()
        _write_json(run_root / "process/verification.json", verification)
        gate = _evaluate_gates(tuple(documents), verification["status"] == "PASS")
        _write_json(run_root / "mechanical_gate.json", gate)
        (run_root / "mechanical_report.md").write_text(
            _report_text(gate, final=False), encoding="utf-8"
        )
        if gate["status"] != "PASS":
            failed_gate = dict(gate)
            failed_gate["schema_version"] = "transflow.rv6-gate/v1"
            failed_gate["visual_review"] = "NOT_STARTED"
            _write_json(run_root / "gate.json", failed_gate)
            (run_root / "report.md").write_text(
                _report_text(failed_gate, final=True), encoding="utf-8"
            )
            print(f"FAIL {run_root}")
            return 1
        print(f"MECHANICAL_PASS_VISUAL_PENDING {run_root}")
        return 0
    except Exception as error:
        _write_json(
            run_root / "failure.json",
            {
                "error_type": type(error).__name__,
                "message": str(error),
                "schema_version": "transflow.rv6-failure/v1",
                "status": "FAIL",
            },
        )
        raise


if __name__ == "__main__":
    raise SystemExit(main())
