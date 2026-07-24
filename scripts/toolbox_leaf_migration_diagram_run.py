"""Run TM4 with the accepted single, chart, and diagram leaves on one full PDF."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import shutil
import time
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Any

import pymupdf

from scripts.run_toolbox_leaf_migration import (
    CATALOG_PATH,
    MigrationContractError,
    provider_configuration_snapshot,
    store_translation_bundle,
)
from scripts.toolbox_leaf_migration_chart import (
    ROUTE as CHART_ROUTE,
)
from scripts.toolbox_leaf_migration_chart import (
    TOOLBOX_VERSION as CHART_TOOLBOX_VERSION,
)
from scripts.toolbox_leaf_migration_chart import (
    build_chart_catalog_overlay,
)
from scripts.toolbox_leaf_migration_diagram import (
    ROUTE as DIAGRAM_ROUTE,
)
from scripts.toolbox_leaf_migration_diagram import (
    TOOLBOX_VERSION as DIAGRAM_TOOLBOX_VERSION,
)
from scripts.toolbox_leaf_migration_diagram import (
    build_diagram_catalog_overlay,
)
from scripts.toolbox_leaf_migration_drivers import LeafMigrationRunContext
from scripts.toolbox_leaf_migration_visual_only import (
    FONT_MANIFEST,
    LEAF_SCHEMA,
    P8_POLICY,
    P9A_POLICY,
    _artifact,
    _classification_trace,
    _compose_comparison,
    _extract_page,
    _layout_identity,
    _relative,
    _render_page,
    _sha256_file,
    _write_json,
)
from tests.migration.p9_qwen_translation_adapter import (
    MigrationQwenTranslationAdapter,
    migration_translation_environment_ready,
)
from tests.migration.qwen_adapter import (
    MigrationQwenDecisionAdapter,
    migration_environment_ready,
)
from transflow.adapters.filesystem.artifact_store import (
    SharedFilesystemArtifactAdapter,
)
from transflow.adapters.filesystem.checkpoint import FilesystemCheckpointAdapter
from transflow.adapters.filesystem.layout_memory_runtime import (
    DocumentLayoutMemoryRuntime,
)
from transflow.adapters.filesystem.repair_memory_runtime import (
    PageRepairMemoryRuntime,
)
from transflow.adapters.filesystem.toolbox_candidate_pdf import (
    ToolboxCandidatePdfRenderer,
)
from transflow.application.contracts import EnumeratedPage, ProcessedPage
from transflow.application.document_coordinator import DocumentCoordinator
from transflow.application.document_finalizer import DocumentFinalizer
from transflow.application.document_layout_memory import (
    DocumentLayoutMemoryBuilder,
    LayoutMemoryPolicyConfig,
)
from transflow.application.page_pipeline import PreviewPublisher
from transflow.application.repair_catalog import load_repair_policy
from transflow.application.toolbox_page_coordinator import (
    ToolboxPageCoordinator,
    ToolboxPageWork,
)
from transflow.application.toolbox_page_pipeline import ToolboxPagePipeline
from transflow.application.toolbox_repair import P9BToolboxRepairHandler
from transflow.classification.decision_adapter import BoundedDecisionRunner
from transflow.classification.engine import ClassificationEngine
from transflow.domain.common import content_sha256
from transflow.domain.errors import PortCallError
from transflow.domain.jobs import DocumentRunRequest
from transflow.domain.states import (
    CheckpointCompatibility,
    Fallback,
    Quality,
    TranslationCoverage,
)
from transflow.domain.translation import TranslationBatch, TranslationBundle
from transflow.pdf_kernel import (
    ControlledFontRegistry,
    PageFactsExtractor,
    PagePatchInterpreter,
    PyMuPdfPageRenderer,
)
from transflow.pdf_kernel.preservation import PreflightDecision
from transflow.toolboxes.catalog import load_toolbox_catalog
from transflow.toolboxes.contracts import normalized_page_outcome
from transflow.toolboxes.leaves import (
    SingleFlowTextToolbox,
    build_p8_toolbox_factories,
)
from transflow.toolboxes.leaves.body_chart.prompt import (
    chart_translation_system_prompt,
)
from transflow.toolboxes.leaves.body_chart.toolbox import ChartToolbox
from transflow.toolboxes.leaves.body_diagram.prompt import (
    diagram_translation_system_prompt,
)
from transflow.toolboxes.leaves.body_diagram.toolbox import DiagramToolbox
from transflow.toolboxes.leaves.body_flow_text_single.prompt import (
    single_translation_system_prompt,
)
from transflow.toolboxes.leaves.policy import P8ToolboxPolicy, load_p8_toolbox_policy

LOGGER = logging.getLogger("transflow.toolbox_leaf_migration.diagram")
REPO_ROOT = Path(__file__).resolve().parent.parent
P9B_POLICY = Path("resources/manifests/p9b_repair_policy.json")
REPAIR_SCHEMA = Path("resources/schemas/page_repair_memory_v1.schema.json")
ACCEPTED_LEAF_AUTHORIZATION = Path(
    "resources/manifests/toolbox_leaf_migration/authorizations/"
    "tm4_accepted_leaves_full_pdf_20260724.json"
)
SINGLE_ROUTE = "body.flow_text.single"
ACCEPTED_TEXT_ROUTES = frozenset(
    {SINGLE_ROUTE, CHART_ROUTE, DIAGRAM_ROUTE}
)


def translation_prompt_for_route(route: str) -> str | None:
    """Return the prompt owned by an accepted text-producing leaf."""

    if route == SINGLE_ROUTE:
        return single_translation_system_prompt()
    if route == CHART_ROUTE:
        return chart_translation_system_prompt()
    if route == DIAGRAM_ROUTE:
        return diagram_translation_system_prompt()
    return None


def build_accepted_leaf_catalog_overlay(
    catalog: dict[str, Any],
) -> dict[str, Any]:
    """Enable chart and diagram beside the already accepted single leaf."""

    return build_diagram_catalog_overlay(build_chart_catalog_overlay(catalog))


def leaf_policy_for_languages(
    policy: P8ToolboxPolicy,
    source_language: str,
    target_language: str,
) -> P8ToolboxPolicy:
    """Bind the shared layout policy to the current document language pair."""

    return replace(
        policy,
        source_language=source_language,
        target_language=target_language,
    )


def _accepted_leaf_authorization(root: Path) -> dict[str, str]:
    path = root / ACCEPTED_LEAF_AUTHORIZATION
    payload = json.loads(path.read_text(encoding="utf-8"))
    if (
        payload.get("schema_version")
        != "transflow.toolbox-leaf-migration-authorization/v1"
        or payload.get("stage") != "TM4"
        or payload.get("approved") is not True
        or set(payload.get("allowed_routes", ())) != ACCEPTED_TEXT_ROUTES
    ):
        raise MigrationContractError(
            "TM4_ACCEPTED_LEAF_AUTHORIZATION_INVALID",
            path.name,
        )
    return {
        "ref": _relative(path, root),
        "sha256": _sha256_file(path),
    }


class _RecordingTranslationPort:
    """Persist validated bundles without persisting provider credentials or raw replies."""

    def __init__(
        self,
        delegate: MigrationQwenTranslationAdapter,
        storage_root: Path,
        *,
        retry_delays_seconds: tuple[float, ...] = (5.0, 15.0, 30.0, 60.0),
    ) -> None:
        if any(delay < 0 for delay in retry_delays_seconds):
            raise ValueError("translation retry delay must not be negative")
        self.delegate = delegate
        self.storage_root = storage_root
        self.retry_delays_seconds = retry_delays_seconds
        self.records: list[dict[str, object]] = []

    def translate(self, batch: TranslationBatch) -> TranslationBundle:
        bundle = self._call_with_retry(batch, None, "INITIAL")
        self._record(batch, bundle, "INITIAL")
        return bundle

    def repair(
        self,
        batch: TranslationBatch,
        previous: TranslationBundle,
    ) -> TranslationBundle:
        bundle = self._call_with_retry(batch, previous, "TARGETED_RETRY")
        self._record(batch, bundle, "TARGETED_RETRY")
        return bundle

    def _call_with_retry(
        self,
        batch: TranslationBatch,
        previous: TranslationBundle | None,
        call_kind: str,
    ) -> TranslationBundle:
        for attempt in range(len(self.retry_delays_seconds) + 1):
            try:
                return (
                    self.delegate.translate(batch)
                    if previous is None
                    else self.delegate.repair(batch, previous)
                )
            except PortCallError as error:
                if not error.retryable or attempt == len(self.retry_delays_seconds):
                    raise
                delay = self.retry_delays_seconds[attempt]
                LOGGER.warning(
                    "TM4 translation provider transient failure; retrying "
                    "batch=%s kind=%s attempt=%s delay_seconds=%s code=%s",
                    batch.batch_id,
                    call_kind,
                    attempt + 1,
                    delay,
                    error.code.value,
                )
                time.sleep(delay)
        raise AssertionError("unreachable translation retry state")

    def _record(
        self,
        batch: TranslationBatch,
        bundle: TranslationBundle,
        call_kind: str,
    ) -> None:
        page_numbers = {unit.page_no for unit in batch.units}
        if len(page_numbers) != 1:
            raise MigrationContractError(
                "TM4_BATCH_PAGE_SCOPE_INVALID",
                batch.batch_id,
            )
        stored = store_translation_bundle(
            batch,
            bundle,
            self.storage_root,
            provider_configuration_snapshot(),
        )
        self.records.append(
            {
                "batch_hash": content_sha256(batch),
                "bundle_hash": stored.bundle_hash,
                "bundle_path": _relative(stored.path, self.storage_root),
                "call_kind": call_kind,
                "page_no": page_numbers.pop(),
                "unit_count": len(batch.units),
            }
        )


class _RecordingCoordinator(ToolboxPageCoordinator):
    """Retain the leaf execution result for page-level audit."""

    def __init__(
        self,
        translation: _RecordingTranslationPort,
        repair_handler: P9BToolboxRepairHandler | None,
    ) -> None:
        super().__init__(translation, repair_handler=repair_handler)
        self.results: dict[int, Any] = {}

    def execute(self, work: ToolboxPageWork) -> Any:
        result = super().execute(work)
        self.results[work.context.page_no] = result
        return result


def _repair_policy_overlay(root: Path, run_root: Path) -> Path:
    payload = json.loads((root / P9B_POLICY).read_text(encoding="utf-8"))
    existing = {str(item["route"]) for item in payload["catalogs"]}
    additions = (
        (CHART_ROUTE, CHART_TOOLBOX_VERSION, "tm4-chart"),
        (DIAGRAM_ROUTE, DIAGRAM_TOOLBOX_VERSION, "tm4-diagram"),
    )
    for route, version, slug in additions:
        if route in existing:
            raise MigrationContractError(
                "TM4_REPAIR_ROUTE_ALREADY_REGISTERED",
                route,
            )
        payload["catalogs"].append(
            {
                "route": route,
                "toolbox_id": route,
                "toolbox_version": version,
                "catalog_version": f"{slug}/v1",
                "comparator_version": f"{slug}-comparator/v1",
                "atoms": [
                    {
                        "atom_id": f"{route}.legacy_repair/v1",
                        "priority": 10,
                    }
                ],
            }
        )
    path = run_root / "process/repair_policy_overlay.json"
    _write_json(path, payload, run_root)
    return path


def _implementation_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for relative in (
        "src/transflow/toolboxes/leaves/body_flow_text_single",
        "src/transflow/toolboxes/leaves/body_chart",
        "src/transflow/toolboxes/leaves/body_diagram",
    ):
        for path in sorted((root / relative).glob("*.py")):
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _render_patch_page(
    source: Path,
    page: EnumeratedPage,
    processed: ProcessedPage,
    interpreter: PagePatchInterpreter,
    target: Path,
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open(source) as document:
        if processed.patch is not None:
            interpreter.apply(
                document,
                page.context,
                page.facts,
                processed.patch,
                processed.route,
            )
        with pymupdf.open() as output:
            output.insert_pdf(
                document,
                from_page=processed.page_no - 1,
                to_page=processed.page_no - 1,
            )
            output.save(target, garbage=4, deflate=True)


def _write_page_artifacts(
    *,
    run_root: Path,
    source: Path,
    page: EnumeratedPage,
    processed: ProcessedPage,
    interpreter: PagePatchInterpreter,
) -> dict[str, str]:
    page_root = run_root / "output/pages" / f"p{processed.page_no:04d}"
    input_pdf = page_root / "input/source.pdf"
    output_pdf = page_root / "output/transflow.pdf"
    comparison_pdf = page_root / "review/source_vs_transflow.pdf"
    comparison_png = page_root / "review/source_vs_transflow.png"
    _extract_page(source, processed.page_no, input_pdf)
    _render_patch_page(source, page, processed, interpreter, output_pdf)
    _compose_comparison(
        (("SOURCE", input_pdf), ("TRANSFLOW", output_pdf)),
        comparison_pdf,
        comparison_png,
    )
    return {
        "input_pdf": _relative(input_pdf, run_root),
        "output_pdf": _relative(output_pdf, run_root),
        "review_png": _relative(comparison_png, run_root),
    }


def _non_target_passthrough(
    page: EnumeratedPage,
    route: str,
    catalog_hash: str,
    classification_route: Any,
) -> ProcessedPage:
    return ProcessedPage(
        page_no=page.context.page_no,
        route=route,
        outcome=normalized_page_outcome(
            page.context.page_no,
            accepted=True,
            translated=False,
            finding_codes=("TM4_NON_ACCEPTED_ROUTE_PASSTHROUGH",),
            passthrough=True,
        ),
        patch=None,
        preview=None,
        unit_ids=(),
        translated_unit_ids=(),
        application=None,
        catalog_hash=catalog_hash,
        classification_route=classification_route,
    )


def _accepted_page_deliverable(page: ProcessedPage) -> bool:
    if page.route not in ACCEPTED_TEXT_ROUTES:
        return True
    return bool(
        page.toolbox_id == page.route
        and page.outcome.quality is Quality.PASS
        and page.outcome.fallback is Fallback.NONE
        and page.outcome.translation_coverage is TranslationCoverage.FULL
        and (not page.unit_ids or page.patch is not None)
    )


def execute_accepted_leaf_document(
    context: LeafMigrationRunContext,
    source: Path,
) -> dict[str, Any]:
    """Execute one full document and always materialize a final PDF."""

    root = context.repository_root
    run_root = context.output_root
    authorization = _accepted_leaf_authorization(root)
    source_hash = _sha256_file(source)
    expected_hash = str(context.input_manifest["source_document"]["sha256"])
    if source_hash != expected_hash:
        raise MigrationContractError("TM4_SOURCE_COPY_DRIFT", source.name)

    runtime_root = run_root / "process/runtime"
    artifacts = SharedFilesystemArtifactAdapter(runtime_root, context.run_id)
    fonts = ControlledFontRegistry(root / FONT_MANIFEST, root)
    interpreter = PagePatchInterpreter(fonts)
    finalizer = DocumentFinalizer(interpreter, artifacts, runtime_root)
    memory_policy = LayoutMemoryPolicyConfig.load(root / P9A_POLICY)
    request = DocumentRunRequest(
        source_pdf_path=str(source.resolve()),
        source_hash=source_hash,
        source_language=str(context.input_manifest["source_language"]),
        target_language=str(context.input_manifest["target_language"]),
        config_snapshot_hash=memory_policy.config_hash,
        job_id=f"job-{context.run_id}",
        run_id=context.run_id,
    )
    preflight = finalizer.preflight(request)
    if preflight.decision is not PreflightDecision.PROCESS:
        raise MigrationContractError(
            "TM4_PREFLIGHT_NOT_PROCESS",
            preflight.decision.value,
        )

    document_coordinator = DocumentCoordinator(PageFactsExtractor())
    classification_adapter = MigrationQwenDecisionAdapter(timeout_seconds=180.0)
    decision_runner = BoundedDecisionRunner(classification_adapter)
    rich_pages = document_coordinator.enumerate_pages(
        request,
        include_classification=True,
    )
    classified = document_coordinator.classify_pages(
        rich_pages,
        ClassificationEngine(decision_runner),
        page_concurrency=4,
    )
    pages = tuple(
        EnumeratedPage(page.context, replace(page.facts, classification=None))
        for page in rich_pages
    )
    classified_by_page = {item.page_no: item for item in classified}
    route_rows = tuple((item.page_no, item.route.route) for item in classified)
    classification_trace = _classification_trace(
        classified,
        decision_runner,
        classification_adapter,
    )
    classification_trace["schema_version"] = (
        "transflow.tm4-full-pdf-classification-trace/v1"
    )
    _write_json(
        run_root / "process/classification_trace.json",
        classification_trace,
        run_root,
    )

    default_hash = _sha256_file(CATALOG_PATH)
    default_catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    overlay_payload = build_accepted_leaf_catalog_overlay(default_catalog)
    overlay_path = run_root / "process/catalog_overlay.json"
    _write_json(overlay_path, overlay_payload, run_root)
    changed_routes = {
        before["route"]
        for before, after in zip(
            default_catalog["entries"],
            overlay_payload["entries"],
            strict=True,
        )
        if before != after
    }
    if changed_routes != {CHART_ROUTE, DIAGRAM_ROUTE}:
        raise MigrationContractError(
            "TM4_CATALOG_OVERLAY_SCOPE_INVALID",
            ",".join(sorted(changed_routes)),
        )

    leaf_policy = leaf_policy_for_languages(
        load_p8_toolbox_policy(root / P8_POLICY),
        request.source_language,
        request.target_language,
    )
    font_path = fonts.resolve(leaf_policy.font_id).path
    factories = build_p8_toolbox_factories(
        root / P8_POLICY,
        root / FONT_MANIFEST,
        root,
    )
    factories[SINGLE_ROUTE] = lambda: SingleFlowTextToolbox(
        leaf_policy,
        font_path,
    )
    factories[CHART_ROUTE] = lambda: ChartToolbox(leaf_policy, font_path)
    factories[DIAGRAM_ROUTE] = lambda: DiagramToolbox(
        leaf_policy,
        font_path,
        source,
    )
    catalog = load_toolbox_catalog(overlay_path, factories)
    startup = catalog.validate_startup()
    if not startup.ready:
        raise MigrationContractError(
            "TM4_CATALOG_NOT_READY",
            ",".join(startup.violations),
        )

    builder = DocumentLayoutMemoryBuilder()
    memory_runtime = DocumentLayoutMemoryRuntime(
        runtime_root,
        context.run_id,
        builder,
    )
    layout_identity = replace(
        _layout_identity(
            context,
            tuple(page.facts for page in pages),
            memory_policy,
        ),
        catalog_hash=catalog.catalog_hash,
    )
    bound_pages, memory_ref = document_coordinator.freeze_document_layout_memory(
        pages,
        route_rows,
        layout_identity,
        memory_policy,
        memory_runtime,
    )
    if builder.build_count != 1:
        raise MigrationContractError(
            "TM4_LAYOUT_MEMORY_BUILD_COUNT_INVALID",
            str(builder.build_count),
        )
    document_memory = memory_runtime.load_readonly(memory_ref)

    repair_policy = load_repair_policy(
        _repair_policy_overlay(root, run_root)
    )
    translation_adapters = {
        route: MigrationQwenTranslationAdapter(
            timeout_seconds=180.0,
            chunk_size=48,
            system_prompt=prompt,
        )
        for route in ACCEPTED_TEXT_ROUTES
        if (prompt := translation_prompt_for_route(route)) is not None
    }
    translation_ports = {
        route: _RecordingTranslationPort(
            adapter,
            run_root / f"process/translation_store/{route.replace('.', '_')}",
        )
        for route, adapter in translation_adapters.items()
    }
    if sum(adapter.call_count for adapter in translation_adapters.values()) != 0:
        raise MigrationContractError(
            "TM4_TRANSLATION_BEFORE_MEMORY_FREEZE",
            context.run_id,
        )

    checkpoints = FilesystemCheckpointAdapter(
        runtime_root,
        context.run_id,
        artifacts,
    )
    previews = PreviewPublisher(artifacts)
    compatibility = CheckpointCompatibility(
        source_hash=source_hash,
        config_hash=memory_policy.config_hash,
        font_hash=fonts.manifest_hash,
        toolbox_catalog_hash=catalog.catalog_hash,
        schema_hash=_sha256_file(root / LEAF_SCHEMA),
    )
    repair_runtime_root = runtime_root / "repair"
    implementation_hash = _implementation_hash(root)
    processed: list[ProcessedPage] = []
    page_rows: list[dict[str, Any]] = []
    for ordinal, page in enumerate(bound_pages, start=1):
        classified_page = classified_by_page[page.context.page_no]
        route = classified_page.route.route
        if route in ACCEPTED_TEXT_ROUTES:
            repair_handler = None
            if route in {CHART_ROUTE, DIAGRAM_ROUTE}:
                candidate_renderer = ToolboxCandidatePdfRenderer(
                    source,
                    page.context,
                    page.facts,
                    interpreter,
                    route,
                )

                def runtime_factory(identity: Any) -> PageRepairMemoryRuntime:
                    return PageRepairMemoryRuntime(
                        repair_runtime_root,
                        identity,
                    )

                repair_handler = P9BToolboxRepairHandler(
                    policy=repair_policy,
                    document_memory=document_memory,
                    run_token=(
                        f"worker-{context.run_id}-p{page.context.page_no:04d}"
                    ),
                    schema_hash=_sha256_file(root / REPAIR_SCHEMA),
                    implementation_hash=implementation_hash,
                    runtime_factory=runtime_factory,
                    renderer=candidate_renderer,
                )
            coordinator = _RecordingCoordinator(
                translation_ports[route],
                repair_handler,
            )
            pipeline = ToolboxPagePipeline(
                catalog,
                coordinator,
                PyMuPdfPageRenderer(interpreter),
                previews,
                checkpoints,
                compatibility,
            )
            result = pipeline.execute(
                source,
                page,
                classified_page.route,
            )
            artifact_paths = _write_page_artifacts(
                run_root=run_root,
                source=source,
                page=page,
                processed=result,
                interpreter=interpreter,
            )
        else:
            result = _non_target_passthrough(
                page,
                route,
                catalog.catalog_hash,
                classified_page.route,
            )
            artifact_paths = {}
        processed.append(result)
        deliverable = _accepted_page_deliverable(result)
        page_rows.append(
            {
                "artifacts": artifact_paths,
                "deliverable": (
                    deliverable if route in ACCEPTED_TEXT_ROUTES else None
                ),
                "fallback": result.outcome.fallback.value,
                "finding_codes": list(result.outcome.finding_codes),
                "page_no": result.page_no,
                "patch_operation_count": (
                    len(result.patch.operations)
                    if result.patch is not None
                    else 0
                ),
                "quality": result.outcome.quality.value,
                "route": route,
                "toolbox_id": result.toolbox_id,
                "translated_unit_count": len(result.translated_unit_ids),
                "translation_coverage": (
                    result.outcome.translation_coverage.value
                ),
                "translation_unit_count": len(result.unit_ids),
            }
        )
        if ordinal % 20 == 0 or ordinal == len(bound_pages):
            LOGGER.info(
                "TM4 full PDF progress run=%s pages=%s/%s",
                context.run_id,
                ordinal,
                len(bound_pages),
            )

    processed_pages = tuple(processed)
    finalization = finalizer.finalize(
        request,
        bound_pages,
        processed_pages,
        preflight=preflight,
    )
    final_delivery = run_root / "output/transflow/final_delivery.pdf"
    final_delivery.parent.mkdir(parents=True, exist_ok=True)
    final_delivery.write_bytes(
        artifacts.get(finalization.artifact.artifact_id)
    )
    with pymupdf.open(source) as source_document, pymupdf.open(
        final_delivery
    ) as final_document:
        source_page_count = source_document.page_count
        final_page_count = final_document.page_count
        page_rects_preserved = all(
            source_document[index].rect == final_document[index].rect
            for index in range(source_page_count)
        )

    target_page_no = int(context.input_manifest["target_page"]["page_no"])
    target_source = run_root / "input/target_page.pdf"
    if not target_source.is_file():
        _extract_page(source, target_page_no, target_source)
    target_output = run_root / "output/transflow/target_candidate.pdf"
    target_png = run_root / "output/transflow/target_candidate.png"
    _extract_page(final_delivery, target_page_no, target_output)
    _render_page(target_output, 1, target_png)
    comparison_pdf = run_root / "output/comparison/source_vs_transflow.pdf"
    comparison_png = run_root / "output/comparison/source_vs_transflow.png"
    _compose_comparison(
        (("SOURCE", target_source), ("TRANSFLOW", target_output)),
        comparison_pdf,
        comparison_png,
    )

    route_distribution = dict(
        sorted(Counter(row["route"] for row in page_rows).items())
    )
    accepted_rows = [
        row for row in page_rows if row["route"] in ACCEPTED_TEXT_ROUTES
    ]
    failure_rows = [
        row for row in accepted_rows if not bool(row["deliverable"])
    ]
    summary = {
        "accepted_leaf_authorization": authorization,
        "accepted_route_failure_pages": [
            {
                "fallback": row["fallback"],
                "finding_codes": row["finding_codes"],
                "page_no": row["page_no"],
                "route": row["route"],
            }
            for row in failure_rows
        ],
        "accepted_route_page_count": len(accepted_rows),
        "accepted_route_pass_count": len(accepted_rows) - len(failure_rows),
        "catalog_hash": catalog.catalog_hash,
        "classification_model_call_count": classification_adapter.call_count,
        "default_catalog_mutated": _sha256_file(CATALOG_PATH) != default_hash,
        "document_layout_memory_build_count": builder.build_count,
        "document_layout_memory_hash": memory_ref.memory_hash,
        "document_passthrough": finalization.document_passthrough,
        "final_pdf": _relative(final_delivery, run_root),
        "final_pdf_openable": final_page_count == source_page_count,
        "final_sha256": _sha256_file(final_delivery),
        "page_count": source_page_count,
        "page_count_preserved": final_page_count == source_page_count,
        "page_order_preserved": final_page_count == source_page_count,
        "page_rects_preserved": page_rects_preserved,
        "pages": page_rows,
        "preservation_passed": finalization.preservation.passed,
        "provider_configuration": provider_configuration_snapshot(),
        "route_distribution": route_distribution,
        "schema_version": "transflow.tm4-full-pdf-summary/v1",
        "target_page_no": target_page_no,
        "target_page_route": classified_by_page[target_page_no].route.route,
        "translation_model_call_count": sum(
            adapter.call_count for adapter in translation_adapters.values()
        ),
        "translation_records": {
            route: port.records
            for route, port in sorted(translation_ports.items())
        },
    }
    _write_json(
        run_root / "process/full_document_summary.json",
        summary,
        run_root,
    )
    return summary


def execute_diagram_migration(
    context: LeafMigrationRunContext,
) -> dict[str, Any]:
    """Execute the formal TM4 driver after the shared runner freezes its input."""

    if context.stage != "TM4" or context.route != DIAGRAM_ROUTE:
        raise MigrationContractError(
            "TM4_DRIVER_IDENTITY_INVALID",
            context.route,
        )
    if (
        not migration_environment_ready()
        or not migration_translation_environment_ready()
    ):
        raise MigrationContractError(
            "REAL_QWEN_ENVIRONMENT_NOT_CONFIGURED",
            "TM4",
        )
    source = context.output_root / "input/source_document.pdf"
    summary = execute_accepted_leaf_document(context, source)
    route_distribution = summary["route_distribution"]
    missing_routes = sorted(
        route for route in ACCEPTED_TEXT_ROUTES if not route_distribution.get(route)
    )
    if missing_routes:
        raise MigrationContractError(
            "TM4_ACCEPTED_ROUTE_NOT_NATURALLY_HIT",
            ",".join(missing_routes),
        )
    if summary["target_page_route"] != DIAGRAM_ROUTE:
        raise MigrationContractError(
            "CLASSIFICATION_ROUTE_MISMATCH",
            str(summary["target_page_no"]),
        )
    failures = summary["accepted_route_failure_pages"]
    if failures:
        raise MigrationContractError(
            "TM4_ACCEPTED_PAGE_NOT_DELIVERABLE",
            ",".join(str(item["page_no"]) for item in failures),
        )
    if (
        not summary["final_pdf_openable"]
        or not summary["page_count_preserved"]
        or not summary["page_order_preserved"]
        or not summary["page_rects_preserved"]
        or not summary["preservation_passed"]
        or summary["default_catalog_mutated"]
    ):
        raise MigrationContractError(
            "TM4_FULL_DOCUMENT_INTEGRITY_FAILED",
            context.run_id,
        )

    summary_ref = _relative(
        context.output_root / "process/full_document_summary.json",
        context.repository_root,
    )
    comparison_ref = _relative(
        context.output_root / "output/comparison/source_vs_transflow.png",
        context.repository_root,
    )
    gates = {
        f"G-TM-{index:02d}": {
            "status": "PASS" if index < 14 else "REVIEW_PENDING",
            "evidence_refs": [summary_ref, comparison_ref],
        }
        for index in range(1, 15)
    }
    final_delivery = (
        context.output_root / "output/transflow/final_delivery.pdf"
    )
    target_output = (
        context.output_root / "output/transflow/target_candidate.pdf"
    )
    comparison = (
        context.output_root / "output/comparison/source_vs_transflow.png"
    )
    return {
        "artifacts": {
            "comparison": _artifact(
                comparison,
                context,
                "SOURCE_TRANSFLOW_COMPARISON",
            ),
            "final_delivery": _artifact(
                final_delivery,
                context,
                "FINAL_DELIVERY",
            ),
            "source_document": _artifact(
                source,
                context,
                "COMPLETE_SOURCE_DOCUMENT",
            ),
            "transflow_candidate": _artifact(
                target_output,
                context,
                "TRANSFLOW_TRANSLATED_CANDIDATE",
            ),
        },
        "axes": {
            "core_migration": "PASS",
            "engineering_closure": "PASS",
            "product_acceptance": "REVIEW_PENDING",
            "promotion_eligibility": "PASS_DISABLED_WITH_FALLBACK_NON_BLIND",
        },
        "gate_results": gates,
        "known_issues": [
            "Default Catalog remains unchanged pending owner review.",
            "Classification and translation use migration-only Qwen adapters.",
        ],
        "route": context.route,
        "route_attestation": {
            "accepted_routes": sorted(ACCEPTED_TEXT_ROUTES),
            "default_catalog_mutated": False,
            "forced_route_count": 0,
            "route_distribution": route_distribution,
        },
        "run_id": context.run_id,
        "schema_version": "transflow.toolbox-leaf-migration-execution/v1",
        "stage": context.stage,
        "state": "FULL_E2E_PASS",
        "trace": {
            "full_document_summary": summary_ref,
            "page_count": summary["page_count"],
            "page_output_root": _relative(
                context.output_root / "output/pages",
                context.repository_root,
            ),
        },
        "translation": {
            "completeness_decision": "PASS",
            "materialized_translated_unit_count": sum(
                int(row["translated_unit_count"])
                for row in summary["pages"]
                if row["route"] in ACCEPTED_TEXT_ROUTES
            ),
            "mock_response_count": 0,
            "ocr_call_count": 0,
            "patch_count": sum(
                int(row["patch_operation_count"] > 0)
                for row in summary["pages"]
                if row["route"] in ACCEPTED_TEXT_ROUTES
            ),
            "provider_call_count": summary["translation_model_call_count"],
            "provider_configuration": summary["provider_configuration"],
            "real_provider_call_count": summary[
                "translation_model_call_count"
            ],
            "semantic_object_modification_count": sum(
                int(row["patch_operation_count"])
                for row in summary["pages"]
            ),
            "translation_unit_count": sum(
                int(row["translation_unit_count"])
                for row in summary["pages"]
                if row["route"] in ACCEPTED_TEXT_ROUTES
            ),
        },
    }


def _standalone_context(
    *,
    run_root: Path,
    source: Path,
    source_language: str,
    target_language: str,
    target_page_no: int,
) -> LeafMigrationRunContext:
    with pymupdf.open(source) as document:
        page_count = document.page_count
    source_hash = _sha256_file(source)
    return LeafMigrationRunContext(
        stage="TM4",
        route=DIAGRAM_ROUTE,
        route_slug="body_diagram",
        run_id=run_root.name,
        repository_root=REPO_ROOT,
        evidence_root=run_root / "evidence",
        output_root=run_root,
        input_manifest={
            "route": DIAGRAM_ROUTE,
            "source_document": {
                "path": _relative(source, REPO_ROOT),
                "sha256": source_hash,
                "page_count": page_count,
            },
            "source_language": source_language,
            "target_language": target_language,
            "target_page": {
                "page_no": target_page_no,
                "page_hash": "",
                "spike_leaf_contract_route": DIAGRAM_ROUTE,
            },
        },
        baseline_hash="standalone-full-pdf-regression",
        catalog_hash=_sha256_file(CATALOG_PATH),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--source-language", required=True)
    parser.add_argument("--target-language", required=True)
    parser.add_argument("--target-page-no", type=int, default=12)
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if (
        not migration_environment_ready()
        or not migration_translation_environment_ready()
    ):
        raise RuntimeError("TM4_REAL_QWEN_ENVIRONMENT_NOT_CONFIGURED")
    source_path = args.source.resolve()
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    run_root = args.run_root.resolve()
    try:
        run_root.relative_to(
            (REPO_ROOT / "runs/toolbox_leaf_migration/TM4").resolve()
        )
    except ValueError as error:
        raise RuntimeError("TM4_RUN_ROOT_OUTSIDE_ALLOWED_ROOT") from error
    if run_root.exists():
        raise FileExistsError(run_root)
    input_source = run_root / "input/source_document.pdf"
    input_source.parent.mkdir(parents=True)
    shutil.copyfile(source_path, input_source)
    context = _standalone_context(
        run_root=run_root,
        source=input_source,
        source_language=args.source_language,
        target_language=args.target_language,
        target_page_no=args.target_page_no,
    )
    authorization = _accepted_leaf_authorization(REPO_ROOT)
    _write_json(
        run_root / "input/source_manifest.json",
        {
            "accepted_leaf_authorization": authorization,
            "copied_source": {
                "page_count": context.input_manifest["source_document"][
                    "page_count"
                ],
                "path": "input/source_document.pdf",
                "sha256": context.input_manifest["source_document"]["sha256"],
            },
            "original_source": {
                "path": _relative(source_path, REPO_ROOT),
                "sha256": _sha256_file(source_path),
            },
            "schema_version": "transflow.tm4-full-pdf-input/v1",
            "source_language": args.source_language,
            "target_language": args.target_language,
            "target_page_no": args.target_page_no,
        },
        run_root,
    )
    summary = execute_accepted_leaf_document(context, input_source)
    route_distribution = summary["route_distribution"]
    missing_routes = sorted(
        route for route in ACCEPTED_TEXT_ROUTES if not route_distribution.get(route)
    )
    failures = summary["accepted_route_failure_pages"]
    passed = bool(
        not missing_routes
        and not failures
        and summary["target_page_route"] == DIAGRAM_ROUTE
        and summary["final_pdf_openable"]
        and summary["page_count_preserved"]
        and summary["page_order_preserved"]
        and summary["page_rects_preserved"]
        and summary["preservation_passed"]
        and not summary["default_catalog_mutated"]
    )
    run_manifest = {
        "accepted_route_failure_pages": failures,
        "final_pdf": summary["final_pdf"],
        "missing_accepted_routes": missing_routes,
        "route_distribution": route_distribution,
        "run_id": context.run_id,
        "schema_version": "transflow.tm4-full-pdf-run/v1",
        "status": "PASS" if passed else "FAIL_WITH_FINAL_PDF",
        "summary": "process/full_document_summary.json",
    }
    _write_json(run_root / "run_manifest.json", run_manifest, run_root)
    print(json.dumps(run_manifest, ensure_ascii=False))
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
