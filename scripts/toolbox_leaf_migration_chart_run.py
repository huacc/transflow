"""Execute the TM3 body.chart full-document acceptance run."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import unicodedata
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Any

import pymupdf

from scripts.run_toolbox_leaf_migration import (
    CATALOG_PATH,
    MigrationContractError,
    _forbidden_production_dependencies,
    provider_configuration_snapshot,
    store_translation_bundle,
)
from scripts.toolbox_leaf_migration_chart import (
    ROUTE,
    TOOLBOX_VERSION,
    build_chart_catalog_overlay,
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
    _pixel_change_metrics,
    _relative,
    _render_page,
    _semantic_signature,
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
from transflow.adapters.filesystem.artifact_store import SharedFilesystemArtifactAdapter
from transflow.adapters.filesystem.checkpoint import FilesystemCheckpointAdapter
from transflow.adapters.filesystem.layout_memory_runtime import DocumentLayoutMemoryRuntime
from transflow.adapters.filesystem.repair_memory_runtime import PageRepairMemoryRuntime
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
from transflow.domain.completeness import CompletenessStatus
from transflow.domain.jobs import DocumentRunRequest
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
from transflow.pdf_kernel.renderer import outside_region_diff_ratio
from transflow.toolboxes.catalog import load_toolbox_catalog
from transflow.toolboxes.contracts import normalized_page_outcome
from transflow.toolboxes.leaves import build_p8_toolbox_factories
from transflow.toolboxes.leaves.body_chart.models import (
    ChartTemplate,
    ChartTextContainer,
)
from transflow.toolboxes.leaves.body_chart.prompt import (
    chart_translation_system_prompt,
)
from transflow.toolboxes.leaves.body_chart.template import build_chart_template
from transflow.toolboxes.leaves.body_chart.toolbox import ChartToolbox
from transflow.toolboxes.leaves.body_flow_text_single.prompt import (
    single_translation_system_prompt,
)
from transflow.toolboxes.leaves.policy import load_p8_toolbox_policy

LOGGER = logging.getLogger("transflow.toolbox_leaf_migration.chart")
HAN = re.compile(r"[\u3400-\u9fff]")
P9B_POLICY = Path("resources/manifests/p9b_repair_policy.json")
REPAIR_SCHEMA = Path("resources/schemas/page_repair_memory_v1.schema.json")
ACCEPTED_LEAF_AUTHORIZATION = Path(
    "resources/manifests/toolbox_leaf_migration/authorizations/"
    "tm3_accepted_leaves_round06.json"
)
SINGLE_ROUTE = "body.flow_text.single"
ACCEPTED_TEXT_ROUTES = frozenset({ROUTE, SINGLE_ROUTE})


def _translation_prompt_for_route(route: str) -> str | None:
    """Return the leaf-owned prompt only for accepted text-producing routes."""

    if route == ROUTE:
        return chart_translation_system_prompt()
    if route == SINGLE_ROUTE:
        return single_translation_system_prompt()
    return None


def _accepted_leaf_authorization(root: Path) -> dict[str, str]:
    """Validate the explicit Round06 authority before executing accepted leaves."""

    path = root / ACCEPTED_LEAF_AUTHORIZATION
    payload = json.loads(path.read_text(encoding="utf-8"))
    if (
        payload.get("schema_version")
        != "transflow.toolbox-leaf-migration-authorization/v1"
        or payload.get("stage") != "TM3"
        or payload.get("approved") is not True
        or set(payload.get("allowed_routes", ())) != ACCEPTED_TEXT_ROUTES
    ):
        raise MigrationContractError("TM3_ACCEPTED_LEAF_AUTHORIZATION_INVALID", path.name)
    return {
        "ref": _relative(path, root),
        "sha256": _sha256_file(path),
    }


class _RecordingTranslationPort:
    """Call the real provider and persist only validated, content-addressed bundles."""

    def __init__(
        self,
        delegate: MigrationQwenTranslationAdapter,
        context: LeafMigrationRunContext,
    ) -> None:
        self.delegate = delegate
        self.context = context
        self.records: dict[
            int,
            list[tuple[TranslationBatch, TranslationBundle, str, Path, str]],
        ] = {}

    def translate(self, batch: TranslationBatch) -> TranslationBundle:
        bundle = self.delegate.translate(batch)
        self._record(batch, bundle, "INITIAL")
        return bundle

    def repair(
        self,
        batch: TranslationBatch,
        previous: TranslationBundle,
    ) -> TranslationBundle:
        bundle = self.delegate.repair(batch, previous)
        self._record(batch, bundle, "TARGETED_RETRY")
        return bundle

    def _record(
        self,
        batch: TranslationBatch,
        bundle: TranslationBundle,
        call_kind: str,
    ) -> None:
        stored = store_translation_bundle(
            batch,
            bundle,
            self.context.output_root / "process/translation_store/provider",
            provider_configuration_snapshot(),
        )
        page_numbers = {unit.page_no for unit in batch.units}
        if len(page_numbers) != 1:
            raise MigrationContractError("TM3_BATCH_PAGE_SCOPE_INVALID", batch.batch_id)
        page_no = page_numbers.pop()
        self.records.setdefault(page_no, []).append(
            (batch, bundle, stored.bundle_hash, stored.path, call_kind)
        )


class _RecordingCoordinator(ToolboxPageCoordinator):
    """Retain the six-stage result while a page-scoped P9B handler executes."""

    def __init__(
        self,
        translation: _RecordingTranslationPort,
        repair_handler: P9BToolboxRepairHandler | None = None,
    ) -> None:
        super().__init__(translation, repair_handler=repair_handler)
        self.results: dict[int, Any] = {}

    def execute(self, work: ToolboxPageWork) -> Any:
        result = super().execute(work)
        self.results[work.context.page_no] = result
        return result


def _normalized(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value)).casefold()


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


def _commit_page_output(
    *,
    run_root: Path,
    source: Path,
    page: EnumeratedPage,
    processed: ProcessedPage,
    interpreter: PagePatchInterpreter,
) -> dict[str, object]:
    page_root = run_root / "output/pages" / f"p{processed.page_no:04d}"
    input_pdf = page_root / "input/source.pdf"
    input_png = page_root / "input/source.png"
    output_pdf = page_root / "output/transflow.pdf"
    output_png = page_root / "output/transflow.png"
    comparison_pdf = page_root / "review/source_vs_transflow.pdf"
    comparison_png = page_root / "review/source_vs_transflow.png"
    _extract_page(source, processed.page_no, input_pdf)
    _render_page(input_pdf, 1, input_png)
    _render_patch_page(source, page, processed, interpreter, output_pdf)
    _render_page(output_pdf, 1, output_png)
    _compose_comparison(
        (("SOURCE", input_pdf), ("TRANSFLOW", output_pdf)),
        comparison_pdf,
        comparison_png,
    )
    payload = {
        "schema_version": "transflow.tm3-incremental-page-output/v1",
        "page_no": processed.page_no,
        "route": processed.route,
        "terminal_state": processed.outcome.state.value,
        "translation_coverage": processed.outcome.translation_coverage.value,
        "fallback": processed.outcome.fallback.value,
        "input": {
            "path": _relative(input_pdf, run_root),
            "sha256": _sha256_file(input_pdf),
        },
        "output": {
            "path": _relative(output_pdf, run_root),
            "preview_path": _relative(output_png, run_root),
            "sha256": _sha256_file(output_pdf),
        },
        "review": {
            "path": _relative(comparison_png, run_root),
            "sha256": _sha256_file(comparison_png),
        },
        "processed_page": processed.as_checkpoint_payload(),
    }
    completed = page_root / "process/completed.json"
    _write_json(completed, payload, run_root)
    return {
        "completed_path": _relative(completed, run_root),
        "output_path": _relative(output_pdf, run_root),
        "page_no": processed.page_no,
        "review_path": _relative(comparison_png, run_root),
    }


def _chart_repair_policy_overlay(root: Path, run_root: Path) -> Path:
    payload = json.loads((root / P9B_POLICY).read_text(encoding="utf-8"))
    if any(item["route"] == ROUTE for item in payload["catalogs"]):
        raise MigrationContractError("TM3_REPAIR_ROUTE_ALREADY_REGISTERED", ROUTE)
    payload["catalogs"].append(
        {
            "route": ROUTE,
            "toolbox_id": ROUTE,
            "toolbox_version": TOOLBOX_VERSION,
            "catalog_version": "tm3-chart/v1",
            "comparator_version": "tm3-chart-comparator/v1",
            "atoms": [{"atom_id": "body.chart.legacy_repair/v1", "priority": 10}],
        }
    )
    path = run_root / "process/repair_policy_overlay.json"
    _write_json(path, payload, run_root)
    return path


def _implementation_hash(root: Path) -> str:
    paths = (
        *sorted((root / "src/transflow/toolboxes/leaves/body_chart").glob("*.py")),
        *sorted(
            (root / "src/transflow/toolboxes/leaves/body_flow_text_single").glob("*.py")
        ),
        root / "src/transflow/application/repair_coordinator.py",
        root / "src/transflow/application/toolbox_repair.py",
        root / "src/transflow/pdf_kernel/patch.py",
    )
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _rebuild_chart_batch(
    page: EnumeratedPage,
    policy: Any,
    font_path: Path,
) -> tuple[ChartTemplate, TranslationBatch | None]:
    toolbox = ChartToolbox(policy, font_path)
    template = toolbox.prepare(page.context, page.facts)
    return build_chart_template(page.facts), toolbox.build_translation_request(template)


def _require_full_bundle_identity(
    execution: Any,
    full_batch: TranslationBatch,
    page_no: int,
) -> TranslationBundle:
    bundle = execution.translation_bundle
    if bundle is None:
        mismatch = execution.route_capability_mismatch
        if mismatch is not None:
            raise MigrationContractError(
                "TM3_ROUTE_CAPABILITY_MISMATCH",
                f"{page_no}:{mismatch.reason_code}",
            )
        raise MigrationContractError("TM3_FULL_BUNDLE_MISSING", str(page_no))
    if bundle.requested_unit_ids != full_batch.ordered_unit_ids:
        raise MigrationContractError(
            "TM3_FULL_BUNDLE_IDENTITY_MISMATCH",
            str(page_no),
        )
    return bundle


def _match_spike_containers(
    production_template: ChartTemplate,
    batch: TranslationBatch,
    spike_template: Any,
) -> tuple[
    tuple[tuple[ChartTextContainer, Any, Any], ...],
    tuple[dict[str, object], ...],
]:
    production_by_objects = {
        container.source_object_ids: container
        for container in production_template.containers
    }
    available = list(enumerate(spike_template.containers))
    matches: list[tuple[ChartTextContainer, Any, Any]] = []
    differences: list[dict[str, object]] = []
    last_spike_index = -1
    for unit in batch.units:
        production = production_by_objects.get(unit.source_object_ids)
        if production is None:
            raise MigrationContractError(
                "TM3_PRODUCTION_CONTAINER_MAPPING_FAILED",
                unit.unit_id,
            )
        selected = next(
            (
                (index, container)
                for index, container in available
                if index > last_spike_index
                and _normalized(container.source_text)
                == _normalized(unit.source_text)
            ),
            None,
        )
        if selected is None:
            raise MigrationContractError(
                "TM3_SPIKE_CONTAINER_MAPPING_FAILED",
                unit.unit_id,
            )
        spike_index, spike = selected
        available.remove(selected)
        last_spike_index = spike_index
        matches.append((production, spike, unit))
        if (
            production.role != spike.role
            or production.association_id != spike.association_id
            or production.alignment != spike.alignment
            or production.rotation != spike.rotation
        ):
            raise MigrationContractError(
                "TM3_SPIKE_SEMANTIC_STRUCTURE_MISMATCH",
                unit.unit_id,
            )
        bbox_delta = max(
            abs(left - right)
            for left, right in zip(
                production.allowed_bbox,
                spike.allowed_bbox,
                strict=True,
            )
        )
        if bbox_delta > 0.01:
            if not (
                _contains(production.allowed_bbox, production.source_bbox)
                and _contains(spike.allowed_bbox, spike.source_bbox)
            ):
                raise MigrationContractError(
                    "TM3_SAFE_LANE_DIFFERENCE_UNBOUNDED",
                    unit.unit_id,
                )
            differences.append(
                {
                    "code": "CURRENT_KERNEL_SAFE_LANE_ADAPTATION",
                    "approval": "TM3_ENGINEERING_CONTRACT",
                    "container_source_hash": content_sha256(production.source_text),
                    "maximum_coordinate_delta_points": round(bbox_delta, 4),
                    "reason": (
                        "Transflow uses current Kernel span/drawing facts while preserving "
                        "the same role, association, anchor, alignment and source slot."
                    ),
                }
            )
    return tuple(matches), tuple(differences)


def _run_spike_reference(
    *,
    context: LeafMigrationRunContext,
    source_page: Path,
    production_template: ChartTemplate,
    batch: TranslationBatch,
    bundle: TranslationBundle,
    bundle_hash: str,
    font_path: Path,
) -> tuple[Path, dict[str, object]]:
    spike_root = context.repository_root / "spikes/page_toolbox_engine_puncture_v1"
    for path in (spike_root, spike_root / "src"):
        resolved = str(path.resolve())
        if resolved not in sys.path:
            sys.path.insert(0, resolved)
    from page_toolbox_puncture.contracts import (  # type: ignore[import-not-found]
        PageTranslationBundle,
        TranslationResult,
    )
    from shared_pdf_kernel.facts import (  # type: ignore[import-not-found]
        extract_page_facts,
    )
    from toolboxes.body.chart.tools.engine import (  # type: ignore[import-not-found]
        _decision,
    )
    from toolboxes.body.chart.tools.layout_planner import (  # type: ignore[import-not-found]
        plan_chart_layout,
    )
    from toolboxes.body.chart.tools.renderer import (  # type: ignore[import-not-found]
        render_chart_candidate,
    )
    from toolboxes.body.chart.tools.template_builder import (  # type: ignore[import-not-found]
        build_chart_template as build_spike_template,
    )

    spike_facts = extract_page_facts(source_page, page_id="tm3-target")
    spike_template = build_spike_template(spike_facts)
    matches, allowed_differences = _match_spike_containers(
        production_template,
        batch,
        spike_template,
    )
    translated_by_id = {
        item.unit_id: item.translated_text for item in bundle.units
    }
    spike_bundle = PageTranslationBundle(
        request_id=batch.batch_id,
        page_id=spike_template.page_id,
        provider="REAL_QWEN_SHARED_BUNDLE_ADAPTER",
        model="PROCESS_ENVIRONMENT_MODEL",
        translations=tuple(
            TranslationResult(spike.container_id, translated_by_id[unit.unit_id])
            for _, spike, unit in matches
        ),
        response_sha256=bundle_hash,
    )
    plan, layout_findings = plan_chart_layout(
        spike_template,
        spike_bundle,
        font_file=str(font_path),
        bold_font_file=str(font_path),
    )
    if any(not placement.fit for placement in plan.placements):
        raise MigrationContractError(
            "TM3_SPIKE_LAYOUT_FAILED",
            ",".join(item.code for item in layout_findings),
        )
    output = context.output_root / "output/spike/output.pdf"
    render_findings, render_evidence = render_chart_candidate(
        source_pdf=source_page,
        candidate_pdf=output,
        facts=spike_facts,
        template=spike_template,
        plan=plan,
        evidence_dir=context.output_root / "output/spike/previews",
    )
    decision = _decision(
        spike_template.page_id,
        tuple((*layout_findings, *render_findings)),
    )
    with pymupdf.open(output) as spike_document:
        extracted_text = spike_document[0].get_text("text")
    compatibility_only = (
        decision.product_verdict != "PASS"
        and bool(decision.findings)
        and all(
            item.code == "CHART_TRANSLATION_MISSING"
            for item in decision.findings
        )
        and all(
            _normalized(item.translated_text) in _normalized(extracted_text)
            for item in spike_bundle.translations
        )
    )
    if decision.product_verdict != "PASS" and not compatibility_only:
        raise MigrationContractError(
            "TM3_SPIKE_PRODUCT_FAILED",
            ",".join(item.code for item in decision.findings),
        )
    if compatibility_only:
        allowed_differences = (
            *allowed_differences,
            {
                "code": "SPIKE_UNICODE_COMPATIBILITY_EXTRACTION",
                "approval": "TM3_ENGINEERING_CONTRACT",
                "reason": (
                    "Spike text extraction reports CJK compatibility code points; "
                    "NFKC-normalized glyph text equals the shared TranslationBundle."
                ),
            },
        )
    for key in ("source_png", "candidate_png", "comparison_png"):
        value = render_evidence.get(key)
        if isinstance(value, str):
            render_evidence[key] = _relative(Path(value), context.repository_root)
    trace = {
        "schema_version": "transflow.tm3-spike-reference/v1",
        "allowed_differences": list(allowed_differences),
        "bundle_hash_consumed": bundle_hash,
        "core_executed": True,
        "layout_finding_codes": [item.code for item in layout_findings],
        "matched_container_count": len(matches),
        "process_verdict": decision.process_verdict,
        "original_product_verdict": decision.product_verdict,
        "product_verdict": "PASS" if compatibility_only else decision.product_verdict,
        "protected_object_count": len(spike_template.protected_object_ids),
        "render_evidence": render_evidence,
        "source_text_alignment": "EXACT_NORMALIZED_ORDER",
        "spike_container_count": len(spike_template.containers),
        "spike_structure_hash": spike_template.structure_sha256,
        "toolbox_route": ROUTE,
        "unicode_compatibility_normalization_applied": compatibility_only,
        "unexplained_difference_count": 0,
    }
    _write_json(
        context.output_root / "process/spike_trace.json",
        trace,
        context.output_root,
    )
    return output, trace


def _contains(
    outer: tuple[float, float, float, float],
    inner: tuple[float, float, float, float],
    tolerance: float = 0.1,
) -> bool:
    return (
        outer[0] <= inner[0] + tolerance
        and outer[1] <= inner[1] + tolerance
        and outer[2] + tolerance >= inner[2]
        and outer[3] + tolerance >= inner[3]
    )


def _protected_visual_signature(facts: ExtractedPageFacts) -> str:
    return content_sha256(
        {
            "images": tuple(
                sorted(
                    (item.bbox, item.width, item.height, item.content_hash)
                    for item in facts.image_objects
                )
            ),
            "drawings": tuple(
                sorted(
                    (item.bbox, item.content_hash)
                    for item in facts.drawing_objects
                )
            ),
        }
    )


def _missing_protected_text(
    source_facts: ExtractedPageFacts,
    final_facts: ExtractedPageFacts,
    protected_ids: tuple[str, ...],
) -> tuple[dict[str, object], ...]:
    source = [
        item for item in source_facts.text_spans if item.object_id in set(protected_ids)
    ]
    target = list(final_facts.text_spans)
    missing: list[dict[str, object]] = []
    used: set[int] = set()
    for item in source:
        match = next(
            (
                index
                for index, candidate in enumerate(target)
                if index not in used
                and _normalized(candidate.text) == _normalized(item.text)
                and max(
                    abs(left - right)
                    for left, right in zip(item.bbox, candidate.bbox, strict=True)
                )
                <= 0.75
            ),
            None,
        )
        if match is None:
            missing.append(
                {
                    "bbox": item.bbox,
                    "source_text_hash": content_sha256(item.text),
                }
            )
        else:
            used.add(match)
    return tuple(missing)


def _classification_pool_audit(
    context: LeafMigrationRunContext,
) -> dict[str, object]:
    root = context.repository_root
    chart_root = root / "spikes/page_toolbox_engine_puncture_v1/toolboxes/body/chart"
    records = [
        json.loads(line)
        for line in (chart_root / "samples/manifest.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    rows: list[dict[str, object]] = []
    for record in records:
        toolbox_pdf = chart_root / record["source_ref"]
        classified_pdf = root / "spikes" / record["upstream_ref"]
        toolbox_hash = _sha256_file(toolbox_pdf)
        classified_hash = _sha256_file(classified_pdf)
        if toolbox_hash != record["sha256"] or classified_hash != record["sha256"]:
            raise MigrationContractError(
                "TM3_CLASSIFICATION_TOOLBOX_PAIR_DRIFT",
                str(record["source_ref"]),
            )
        facts = PageFactsExtractor().extract_page(classified_pdf, classified_hash, 1)
        template = build_chart_template(facts)
        owned = [
            *(
                object_id
                for container in template.containers
                for object_id in container.source_object_ids
            ),
            *template.protected_object_ids,
        ]
        expected = [item.object_id for item in facts.text_spans]
        if sorted(owned) != sorted(expected) or len(owned) != len(set(owned)):
            raise MigrationContractError(
                "TM3_POOL_TEXT_OWNERSHIP_INVALID",
                str(record["source_ref"]),
            )
        rows.append(
            {
                "classification_ref": str(record["upstream_ref"]),
                "container_count": len(template.containers),
                "protected_text_count": len(template.protected_object_ids),
                "role_distribution": dict(
                    sorted(Counter(item.role for item in template.containers).items())
                ),
                "sha256": classified_hash,
                "toolbox_ref": _relative(toolbox_pdf, root),
                "total_text_ownership": "PASS",
            }
        )
    return {
        "schema_version": "transflow.tm3-classification-toolbox-pairing/v1",
        "blind_status": "KNOWN_NON_BLIND_REGRESSION_POOL",
        "one_to_one_match_count": len(rows),
        "pair_count": len(rows),
        "records": rows,
        "status": "PASS",
    }


def _source_inventory(context: LeafMigrationRunContext) -> dict[str, object]:
    root = context.repository_root
    chart_root = root / "spikes/page_toolbox_engine_puncture_v1/toolboxes/body/chart"
    selected = tuple(
        path
        for path in sorted(chart_root.rglob("*"), key=lambda item: item.as_posix())
        if path.is_file() and "__pycache__" not in path.parts
    )
    direct_map = {
        "engine.py": "src/transflow/toolboxes/leaves/body_chart/toolbox.py",
        "layout_planner.py": "src/transflow/toolboxes/leaves/body_chart/layout.py",
        "models.py": "src/transflow/toolboxes/leaves/body_chart/models.py",
        "renderer.py": "src/transflow/pdf_kernel/patch.py",
        "template_builder.py": "src/transflow/toolboxes/leaves/body_chart/template.py",
        "page_translation.en-zh.zh-CN.md": (
            "src/transflow/toolboxes/leaves/body_chart/prompt.py"
        ),
    }
    rows: list[dict[str, object]] = []
    for path in selected:
        if path.name in direct_map:
            strategy = "ADAPT_CONTRACT"
            target: str | None = direct_map[path.name]
            reason = "CHART_CORE_MIGRATED"
        elif "runs" in path.parts or "reports" in path.parts:
            strategy = "EVIDENCE_ONLY"
            target = "runs/toolbox_leaf_migration/TM3"
            reason = "HISTORICAL_OR_NON_BLIND_EVIDENCE_NOT_RUNTIME"
        elif "tests" in path.parts or "fixtures" in path.parts:
            strategy = "TEST_ONLY"
            target = "tests/test_toolbox_leaf_migration_tm3.py"
            reason = "REGRESSION_INPUT_NOT_PRODUCTION_RUNTIME"
        elif "samples" in path.parts:
            strategy = "TEST_ONLY"
            target = "process/classification_toolbox_pairing.json"
            reason = "KNOWN_CLASSIFICATION_POOL_USED_ONLY_FOR_REGRESSION"
        elif path.suffix in {".md", ".json"}:
            strategy = "EVIDENCE_ONLY"
            target = "process/migration_inventory.json"
            reason = "CONTRACT_OR_LESSON_RECORDED_AS_MIGRATION_CONSTRAINT"
        else:
            strategy = "NOT_USED_WITH_REASON"
            target = None
            reason = "SPIKE_ORCHESTRATION_NOT_PRODUCTION_RUNTIME"
        rows.append(
            {
                "path": _relative(path, root),
                "reason": reason,
                "sha256": _sha256_file(path),
                "strategy": strategy,
                "target": target,
            }
        )
    experience_root = root / "spikes/page_toolbox_engine_puncture_v1/docs/经验"
    experience = [
        {
            "path": _relative(path, root),
            "sha256": _sha256_file(path),
            "treatment": (
                "SEMANTIC_OWNER_GLOBAL_LAYOUT_STILL_NARROW_ADAPTER"
                if "chart" in path.name.casefold()
                else "REGRESSION_CONSTRAINT"
            ),
        }
        for path in sorted(experience_root.glob("*"), key=lambda item: item.as_posix())
        if path.is_file()
    ]
    production_files = tuple(
        sorted(
            (
                *(root / "src/transflow/toolboxes/leaves/body_chart").glob("*.py"),
                root / "src/transflow/application/translation_completeness.py",
                root / "src/transflow/domain/toolbox.py",
                root / "src/transflow/domain/translation.py",
                root / "src/transflow/pdf_kernel/patch.py",
                root / "src/transflow/pdf_kernel/repair.py",
            ),
            key=lambda item: item.as_posix(),
        )
    )
    forbidden = _forbidden_production_dependencies()
    return {
        "schema_version": "transflow.toolbox-leaf-source-inventory/v1",
        "route": ROUTE,
        "mapping_coverage_percent": 100 if rows else 0,
        "mapped_asset_count": len(rows),
        "selected_asset_count": len(rows),
        "unmapped_assets": [],
        "spike_assets": rows,
        "experience_constraints": experience,
        "production_files": [
            {"path": _relative(path, root), "sha256": _sha256_file(path)}
            for path in production_files
        ],
        "production_dependency_scan": {
            "forbidden_count": len(forbidden),
            "violations": forbidden,
        },
        "allowed_differences": [
            "Current Kernel object identities replace Spike-local object identities.",
            (
                "PageTextInventory preauthorizes numeric, currency-scale and acronym "
                "keep-source units before provider dispatch."
            ),
            (
                "Margin semantics use shared.margin.header/footer owners; the narrow "
                "chart adapter retains Spike geometry until a shared margin executor exists."
            ),
            (
                "DocumentFinalizer replays approved PagePatch operations on one full "
                "source-document copy."
            ),
        ],
    }


def _run_regressions(context: LeafMigrationRunContext) -> dict[str, object]:
    commands = (
        (
            "TM0_TM3_CONTRACTS",
            (
                "tests/test_toolbox_leaf_migration.py",
                "tests/test_toolbox_leaf_migration_tm1.py",
                "tests/test_toolbox_leaf_migration_tm2.py",
                "tests/test_toolbox_leaf_migration_tm3.py",
            ),
        ),
        ("PDF_KERNEL_AND_P8", ("tests/test_p4.py", "tests/test_p8.py")),
        ("P9A_P9B_MEMORY", ("tests/test_p9a.py", "tests/test_p9b.py")),
        ("RV4_COMPLETENESS", ("tests/test_critical_chain_rv4.py",)),
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        ("src", ".", environment.get("PYTHONPATH", ""))
    )
    records: list[dict[str, object]] = []
    repository_text = str(context.repository_root.resolve())
    repository_posix = context.repository_root.resolve().as_posix()
    for command_id, tests in commands:
        process = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", *tests],
            cwd=context.repository_root,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=1200,
            check=False,
        )
        stdout = process.stdout.replace(repository_text, ".").replace(
            repository_posix,
            ".",
        )
        stderr = process.stderr.replace(repository_text, ".").replace(
            repository_posix,
            ".",
        )
        records.append(
            {
                "command": ["python", "-m", "pytest", "-q", *tests],
                "command_id": command_id,
                "return_code": process.returncode,
                "stderr": stderr[-6000:],
                "stdout": stdout[-6000:],
            }
        )
        if process.returncode != 0:
            payload = {
                "schema_version": "transflow.tm3-regression-results/v1",
                "status": "FAIL",
                "commands": records,
            }
            _write_json(
                context.output_root / "process/regression_results.json",
                payload,
                context.output_root,
            )
            raise MigrationContractError("TM3_REGRESSION_FAILED", command_id)
    return {
        "schema_version": "transflow.tm3-regression-results/v1",
        "status": "PASS",
        "commands": records,
    }


def _write_report(
    context: LeafMigrationRunContext,
    *,
    chart_pages: tuple[int, ...],
    single_pages: tuple[int, ...],
    target_page_no: int,
    final_hash: str,
    comparison_path: Path,
    axes: dict[str, str],
    gates: dict[str, dict[str, object]],
) -> Path:
    report = context.output_root / "report.md"
    gate_lines = "\n".join(
        f"- `{gate}`: `{value['status']}`"
        for gate, value in gates.items()
    )
    page_count = context.input_manifest["source_document"]["page_count"]
    content = f"""# TM3 `body.chart` 核心迁移与全流程验收

- Run：`{context.run_id}`
- 输入：完整 {page_count} 页 PDF；目标页：`{target_page_no}`
- 当前真实分类 chart 命中页：`{", ".join(map(str, chart_pages))}`
- 已接受 single 综合回归页：`{", ".join(map(str, single_pages)) or "none"}`
- 强制 Route：`0`
- 默认 Catalog 修改：`0`
- 最终 PDF SHA-256：`{final_hash}`
- 三联对比：`{_relative(comparison_path, context.repository_root)}`

## 四轴结论

- CoreMigration：`{axes["core_migration"]}`
- EngineeringClosure：`{axes["engineering_closure"]}`
- ProductAcceptance：`{axes["product_acceptance"]}`
- PromotionEligibility：`{axes["promotion_eligibility"]}`

分类目录与 Toolbox 的 30 个一对一页面全部作为已知回归池执行所有权检查；
它们已参与既有 Spike，不能冒充匿名未知盲测，因此默认 Catalog 继续 disabled。
页眉页脚已进入 `shared.margin.header/footer` 语义 owner；本阶段不扩大为跨 Route 的共享布局执行器，
避免在 TM3 中改动全部叶。

## Gate

{gate_lines}

`G-TM-14` 保持人工硬停；负责人查看对比并明确处理前，不启动 TM4。
"""
    report.write_text(content, encoding="utf-8")
    return report


def execute_chart_migration(context: LeafMigrationRunContext) -> dict[str, Any]:
    """Run current classification, shared-bundle A/B, P9B and full PDF finalization."""

    if context.stage != "TM3" or context.route != ROUTE:
        raise MigrationContractError("TM3_DRIVER_IDENTITY_INVALID", context.route)
    if not migration_environment_ready() or not migration_translation_environment_ready():
        raise MigrationContractError(
            "REAL_QWEN_ENVIRONMENT_NOT_CONFIGURED",
            "TM3 环境未就绪",
        )

    root = context.repository_root
    run_root = context.output_root
    accepted_leaf_authorization = _accepted_leaf_authorization(root)
    source = run_root / "input/source_document.pdf"
    source_hash = _sha256_file(source)
    if source_hash != context.input_manifest["source_document"]["sha256"]:
        raise MigrationContractError("TM3_SOURCE_COPY_DRIFT", "source_document")

    runtime_root = run_root / "process/runtime"
    artifacts = SharedFilesystemArtifactAdapter(runtime_root, context.run_id)
    fonts = ControlledFontRegistry(root / FONT_MANIFEST, root)
    interpreter = PagePatchInterpreter(fonts)
    finalizer = DocumentFinalizer(interpreter, artifacts, runtime_root)
    policy = LayoutMemoryPolicyConfig.load(root / P9A_POLICY)
    request = DocumentRunRequest(
        source_pdf_path=str(source.resolve()),
        source_hash=source_hash,
        source_language=str(context.input_manifest["source_language"]),
        target_language=str(context.input_manifest["target_language"]),
        config_snapshot_hash=policy.config_hash,
        job_id=f"job-{context.run_id}",
        run_id=context.run_id,
    )
    preflight = finalizer.preflight(request)
    if preflight.decision is not PreflightDecision.PROCESS:
        raise MigrationContractError("TM3_PREFLIGHT_NOT_PROCESS", preflight.decision.value)

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
    target_page_no = int(context.input_manifest["target_page"]["page_no"])
    if classified_by_page[target_page_no].route.route != ROUTE:
        raise MigrationContractError(
            "CLASSIFICATION_ROUTE_MISMATCH",
            str(target_page_no),
        )
    route_rows = tuple((item.page_no, item.route.route) for item in classified)

    default_catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    overlay_payload = build_chart_catalog_overlay(default_catalog)
    overlay_path = run_root / "process/catalog_overlay.json"
    _write_json(overlay_path, overlay_payload, run_root)
    changed_routes = [
        before["route"]
        for before, after in zip(
            default_catalog["entries"],
            overlay_payload["entries"],
            strict=True,
        )
        if before != after
    ]
    if changed_routes != [ROUTE]:
        raise MigrationContractError(
            "TM3_CATALOG_OVERLAY_SCOPE_INVALID",
            ",".join(changed_routes),
        )

    chart_policy = load_p8_toolbox_policy(root / P8_POLICY)
    font_path = fonts.resolve(chart_policy.font_id).path
    factories = build_p8_toolbox_factories(
        root / P8_POLICY,
        root / FONT_MANIFEST,
        root,
    )
    factories[ROUTE] = lambda: ChartToolbox(chart_policy, font_path)
    catalog = load_toolbox_catalog(overlay_path, factories)
    startup = catalog.validate_startup()
    if not startup.ready:
        raise MigrationContractError(
            "TM3_CATALOG_NOT_READY",
            ",".join(startup.violations),
        )

    builder = DocumentLayoutMemoryBuilder()
    memory_runtime = DocumentLayoutMemoryRuntime(runtime_root, context.run_id, builder)
    layout_identity = replace(
        _layout_identity(context, tuple(page.facts for page in pages), policy),
        catalog_hash=catalog.catalog_hash,
    )
    bound_pages, memory_ref = document_coordinator.freeze_document_layout_memory(
        pages,
        route_rows,
        layout_identity,
        policy,
        memory_runtime,
    )
    if builder.build_count != 1:
        raise MigrationContractError("TM3_LAYOUT_MEMORY_BUILD_COUNT_INVALID", "not-one")
    document_memory = memory_runtime.load_readonly(memory_ref)

    repair_overlay = _chart_repair_policy_overlay(root, run_root)
    repair_policy = load_repair_policy(repair_overlay)
    translation_adapters = {
        route: MigrationQwenTranslationAdapter(
            timeout_seconds=180.0,
            chunk_size=48,
            system_prompt=prompt,
        )
        for route in ACCEPTED_TEXT_ROUTES
        if (prompt := _translation_prompt_for_route(route)) is not None
    }
    translation_ports = {
        route: _RecordingTranslationPort(adapter, context)
        for route, adapter in translation_adapters.items()
    }
    calls_before_memory_freeze = sum(
        adapter.call_count for adapter in translation_adapters.values()
    )
    if calls_before_memory_freeze != 0:
        raise MigrationContractError(
            "TM3_TRANSLATION_BEFORE_MEMORY_FREEZE",
            str(calls_before_memory_freeze),
        )

    checkpoints = FilesystemCheckpointAdapter(runtime_root, context.run_id, artifacts)
    previews = PreviewPublisher(artifacts)
    compatibility = CheckpointCompatibility(
        source_hash=source_hash,
        config_hash=policy.config_hash,
        font_hash=fonts.manifest_hash,
        toolbox_catalog_hash=catalog.catalog_hash,
        schema_hash=_sha256_file(root / LEAF_SCHEMA),
    )
    repair_runtime_root = runtime_root / "repair"
    implementation_hash = _implementation_hash(root)
    processed: list[ProcessedPage] = []
    execution_by_page: dict[int, Any] = {}
    incremental_outputs: list[dict[str, object]] = []
    skipped_pages: list[dict[str, object]] = []
    full_bundle_records: dict[
        int,
        tuple[TranslationBatch, TranslationBundle, str, Path, ChartTemplate],
    ] = {}
    bound_by_page = {item.context.page_no: item for item in bound_pages}
    for page in bound_pages:
        classified_page = classified_by_page[page.context.page_no]
        route = classified_page.route.route
        if route in translation_ports:
            repair_handler = None
            if route == ROUTE:
                candidate_renderer = ToolboxCandidatePdfRenderer(
                    source,
                    page.context,
                    page.facts,
                    interpreter,
                    route,
                )

                def runtime_factory(identity: Any) -> PageRepairMemoryRuntime:
                    return PageRepairMemoryRuntime(repair_runtime_root, identity)

                repair_handler = P9BToolboxRepairHandler(
                    policy=repair_policy,
                    document_memory=document_memory,
                    run_token=f"worker-{context.run_id}-p{page.context.page_no:04d}",
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
            processed_page = pipeline.execute(source, page, classified_page.route)
            execution = coordinator.results.get(page.context.page_no)
            if execution is None:
                raise MigrationContractError(
                    "TM3_TARGET_EXECUTION_MISSING",
                    str(page.context.page_no),
                )
            execution_by_page[page.context.page_no] = execution
            incremental_outputs.append(
                _commit_page_output(
                    run_root=run_root,
                    source=source,
                    page=page,
                    processed=processed_page,
                    interpreter=interpreter,
                )
            )
            if route == ROUTE:
                production_template, full_batch = _rebuild_chart_batch(
                    page,
                    chart_policy,
                    font_path,
                )
                if full_batch is not None:
                    full_bundle = _require_full_bundle_identity(
                        execution,
                        full_batch,
                        page.context.page_no,
                    )
                    stored = store_translation_bundle(
                        full_batch,
                        full_bundle,
                        run_root / "process/translation_store/full",
                        provider_configuration_snapshot(),
                    )
                    full_bundle_records[page.context.page_no] = (
                        full_batch,
                        full_bundle,
                        stored.bundle_hash,
                        stored.path,
                        production_template,
                    )
        else:
            processed_page = ProcessedPage(
                page_no=page.context.page_no,
                route=route,
                outcome=normalized_page_outcome(
                    page.context.page_no,
                    accepted=True,
                    translated=False,
                    finding_codes=("TM3_NON_TARGET_SCOPE_PASSTHROUGH",),
                    passthrough=True,
                ),
                patch=None,
                preview=None,
                unit_ids=(),
                translated_unit_ids=(),
                application=None,
                catalog_hash=catalog.catalog_hash,
                classification_route=classified_page.route,
            )
            skipped_pages.append(
                {
                    "page_no": page.context.page_no,
                    "reason": "TM3_NON_TARGET_SCOPE_PASSTHROUGH",
                    "route": route,
                }
            )
        processed.append(processed_page)

    processed_pages = tuple(processed)
    chart_pages = tuple(
        item.page_no for item in classified if item.route.route == ROUTE
    )
    single_pages = tuple(
        item.page_no for item in classified if item.route.route == SINGLE_ROUTE
    )
    if not chart_pages or target_page_no not in chart_pages:
        raise MigrationContractError("TM3_NATURAL_ROUTE_TARGET_MISSING", str(target_page_no))
    if not single_pages:
        raise MigrationContractError("TM3_ACCEPTED_SINGLE_ROUTE_MISSING", context.run_id)
    failed_chart_pages = tuple(
        item.page_no
        for item in processed_pages
        if item.route == ROUTE
        and (
            item.toolbox_id != ROUTE
            or item.outcome.quality.value != "PASS"
            or item.outcome.fallback.value != "NONE"
        )
    )
    if failed_chart_pages:
        raise MigrationContractError(
            "TM3_TARGET_PAGE_NOT_DELIVERABLE",
            ",".join(map(str, failed_chart_pages)),
        )
    failed_single_pages = tuple(
        item.page_no
        for item in processed_pages
        if item.route == SINGLE_ROUTE
        and (
            item.toolbox_id != SINGLE_ROUTE
            or item.outcome.quality.value != "PASS"
            or item.outcome.fallback.value != "NONE"
            or item.patch is None
        )
    )
    if failed_single_pages:
        raise MigrationContractError(
            "TM3_ACCEPTED_SINGLE_PAGE_NOT_DELIVERABLE",
            ",".join(map(str, failed_single_pages)),
        )
    skipped_accepted_pages = tuple(
        int(item["page_no"])
        for item in skipped_pages
        if item["route"] in ACCEPTED_TEXT_ROUTES
    )
    if skipped_accepted_pages:
        raise MigrationContractError(
            "TM3_ACCEPTED_ROUTE_PASSTHROUGH",
            ",".join(map(str, skipped_accepted_pages)),
        )
    target_processed = next(
        item for item in processed_pages if item.page_no == target_page_no
    )
    target_execution = execution_by_page[target_page_no]
    target_record = full_bundle_records.get(target_page_no)
    if (
        target_processed.patch is None
        or target_record is None
        or target_execution.completeness_decision is None
        or target_execution.completeness_decision.status is not CompletenessStatus.PASS
    ):
        raise MigrationContractError(
            "TM3_TARGET_TRANSLATED_PATCH_MISSING",
            str(target_page_no),
        )

    _write_json(
        run_root / "process/skipped_pages.json",
        {
            "schema_version": "transflow.tm3-skipped-pages/v1",
            "pages": skipped_pages,
            "reason": "ONLY_DISABLED_OR_NON_TEXT_ROUTES_PASSTHROUGH",
        },
        run_root,
    )
    finalization = finalizer.finalize(
        request,
        bound_pages,
        processed_pages,
        preflight=preflight,
    )
    final_content = artifacts.get(finalization.artifact.artifact_id)
    final_delivery = run_root / "output/transflow/final_delivery.pdf"
    final_delivery.parent.mkdir(parents=True, exist_ok=True)
    final_delivery.write_bytes(final_content)
    final_hash = _sha256_file(final_delivery)

    target_input = run_root / "input/target_page.pdf"
    transflow_candidate = run_root / "output/transflow/target_candidate.pdf"
    _extract_page(final_delivery, target_page_no, transflow_candidate)
    _render_page(
        transflow_candidate,
        1,
        run_root / "output/transflow/target_candidate.png",
    )
    repair_seed_full = (
        repair_runtime_root
        / f"pages/{target_page_no:04d}/repair/candidate-0.pdf"
    )
    repair_candidate = run_root / "output/transflow/repair_candidate_round_00.pdf"
    if repair_seed_full.is_file():
        _extract_page(repair_seed_full, target_page_no, repair_candidate)
        _render_page(
            repair_candidate,
            1,
            run_root / "output/transflow/repair_candidate_round_00.png",
        )
    else:
        repair_candidate = None

    target_batch, target_bundle, target_bundle_hash, target_bundle_path, target_template = (
        target_record
    )
    spike_output, spike_trace = _run_spike_reference(
        context=context,
        source_page=target_input,
        production_template=target_template,
        batch=target_batch,
        bundle=target_bundle,
        bundle_hash=target_bundle_hash,
        font_path=font_path,
    )
    comparison_pdf = run_root / "output/comparison/source_spike_transflow.pdf"
    comparison_png = run_root / "output/comparison/source_spike_transflow.png"
    _compose_comparison(
        (
            ("SOURCE", target_input),
            ("SPIKE", spike_output),
            ("TRANSFLOW", transflow_candidate),
        ),
        comparison_pdf,
        comparison_png,
    )
    _compose_comparison(
        (("SOURCE", target_input), ("TRANSFLOW", transflow_candidate)),
        run_root / "output/comparison/source_vs_transflow.pdf",
        run_root / "output/comparison/source_vs_transflow.png",
    )

    target_bound = bound_by_page[target_page_no]
    final_facts = PageFactsExtractor().extract_page(
        final_delivery,
        final_hash,
        target_page_no,
    )
    protected_before = _protected_visual_signature(target_bound.facts)
    protected_after = _protected_visual_signature(final_facts)
    if protected_before != protected_after:
        raise MigrationContractError(
            "TM3_LOCKED_VISUAL_CHANGED",
            str(target_page_no),
        )
    missing_protected = _missing_protected_text(
        target_bound.facts,
        final_facts,
        target_template.protected_object_ids,
    )
    if missing_protected:
        raise MigrationContractError(
            "TM3_PROTECTED_TEXT_CHANGED",
            str(len(missing_protected)),
        )
    with pymupdf.open(transflow_candidate) as target_document:
        target_text = target_document[0].get_text("text")
    source_residue = sum(
        _normalized(unit.source_text) in _normalized(target_text)
        for unit in target_batch.units
        if len(_normalized(unit.source_text)) >= 8
        and _normalized(
            next(
                item.translated_text
                for item in target_bundle.units
                if item.unit_id == unit.unit_id
            )
        )
        != _normalized(unit.source_text)
    )
    han_count = len(HAN.findall(target_text))
    if han_count < 1 or source_residue:
        raise MigrationContractError(
            "TM3_REAL_TRANSLATION_NOT_MATERIALIZED",
            f"han={han_count} residue={source_residue}",
        )
    allowed_regions = tuple(
        operation.rect
        for operation in target_processed.patch.operations
        if operation.rect is not None
    ) + tuple(
        rect
        for operation in target_processed.patch.operations
        for rect in operation.redaction_rects
    )
    outside_ratio = outside_region_diff_ratio(
        source,
        final_delivery,
        allowed_regions,
        page_no=target_page_no,
    )
    if outside_ratio > 0.012:
        raise MigrationContractError(
            "TM3_OUTSIDE_REGION_DRIFT",
            str(outside_ratio),
        )
    pixel_metrics = _pixel_change_metrics(target_input, transflow_candidate)
    if pixel_metrics["changed_channel_count"] == 0:
        raise MigrationContractError(
            "TM3_TARGET_SOURCE_PASSTHROUGH",
            str(target_page_no),
        )

    classification_trace = _classification_trace(
        classified,
        decision_runner,
        classification_adapter,
    )
    classification_trace["schema_version"] = "transflow.tm3-classification-trace/v1"
    _write_json(
        run_root / "process/classification_trace.json",
        classification_trace,
        run_root,
    )
    pool_audit = _classification_pool_audit(context)
    _write_json(
        run_root / "process/classification_toolbox_pairing.json",
        pool_audit,
        run_root,
    )
    inventory = _source_inventory(context)
    dependency_scan = inventory["production_dependency_scan"]
    if (
        inventory["mapping_coverage_percent"] != 100
        or dependency_scan["forbidden_count"] != 0
    ):
        raise MigrationContractError("TM3_SOURCE_MAPPING_INCOMPLETE", ROUTE)
    _write_json(run_root / "process/migration_inventory.json", inventory, run_root)

    provider_records = [
        {
            "batch_hash": content_sha256(batch),
            "bundle_hash": bundle_hash,
            "bundle_path": _relative(path, root),
            "call_kind": call_kind,
            "page_no": page_no,
            "route": route,
            "unit_count": len(batch.units),
        }
        for route, translation_port in sorted(translation_ports.items())
        for page_no, records in sorted(translation_port.records.items())
        for batch, _, bundle_hash, path, call_kind in records
    ]
    full_records = [
        {
            "batch_hash": content_sha256(batch),
            "bundle_hash": bundle_hash,
            "bundle_path": _relative(path, root),
            "page_no": page_no,
            "unit_count": len(batch.units),
        }
        for page_no, (batch, _, bundle_hash, path, _) in sorted(
            full_bundle_records.items()
        )
    ]
    translation_index = {
        "schema_version": "transflow.tm3-translation-bundle-index/v1",
        "prompt_hashes": {
            route: content_sha256(prompt)
            for route in sorted(ACCEPTED_TEXT_ROUTES)
            if (prompt := _translation_prompt_for_route(route)) is not None
        },
        "provider_configuration": provider_configuration_snapshot(),
        "provider_records": provider_records,
        "full_bundle_records": full_records,
        "raw_provider_response_persisted": False,
        "target_bundle_hash": target_bundle_hash,
        "target_bundle_path": _relative(target_bundle_path, root),
    }
    _write_json(
        run_root / "process/translation_bundle.json",
        translation_index,
        run_root,
    )

    comparison_metrics = {
        "schema_version": "transflow.tm3-comparison-metrics/v1",
        "allowed_differences": spike_trace["allowed_differences"],
        "outside_allowed_changed_pixel_ratio": outside_ratio,
        "protected_text_missing_count": 0,
        "protected_visual_hash_after": protected_after,
        "protected_visual_hash_before": protected_before,
        "source_semantic_hash": _semantic_signature(target_bound.facts),
        "spike_bundle_hash": spike_trace["bundle_hash_consumed"],
        "target_han_character_count": han_count,
        "target_pixel_metrics": pixel_metrics,
        "target_source_residue_count": source_residue,
        "transflow_semantic_hash": _semantic_signature(final_facts),
        "unexplained_difference_count": 0,
    }
    _write_json(
        run_root / "process/comparison_metrics.json",
        comparison_metrics,
        run_root,
    )
    page_outputs_by_no = {
        int(item["page_no"]): item for item in incremental_outputs
    }
    accepted_leaf_regression = {
        "schema_version": "transflow.tm3-accepted-leaf-regression/v1",
        "status": "PASS",
        "accepted_routes": sorted(ACCEPTED_TEXT_ROUTES),
        "authorization": accepted_leaf_authorization,
        "pages": [
            {
                "fallback": item.outcome.fallback.value,
                "output_path": page_outputs_by_no[item.page_no]["output_path"],
                "page_no": item.page_no,
                "patch_operation_count": len(item.patch.operations)
                if item.patch is not None
                else 0,
                "provider_record_count": len(
                    translation_ports[item.route].records.get(item.page_no, ())
                ),
                "quality": item.outcome.quality.value,
                "review_path": page_outputs_by_no[item.page_no]["review_path"],
                "route": item.route,
                "translated_unit_count": len(item.translated_unit_ids),
                "translation_coverage": item.outcome.translation_coverage.value,
            }
            for item in processed_pages
            if item.route in ACCEPTED_TEXT_ROUTES
        ],
        "accepted_route_passthrough_count": 0,
    }
    _write_json(
        run_root / "process/accepted_leaf_regression.json",
        accepted_leaf_regression,
        run_root,
    )

    route_attestation = {
        "schema_version": "transflow.toolbox-leaf-route-attestation/v1",
        "forced_route_count": 0,
        "natural_target_page_count": len(chart_pages),
        "natural_target_pages": list(chart_pages),
        "natural_accepted_single_page_count": len(single_pages),
        "natural_accepted_single_pages": list(single_pages),
        "production_route": ROUTE,
        "spike_contract_route": ROUTE,
        "target_page_no": target_page_no,
        "target_route": classified_by_page[target_page_no].route.route,
        "classification_pool_pair_count": pool_audit["pair_count"],
        "classification_pool_blind_status": pool_audit["blind_status"],
    }
    _write_json(
        run_root / "process/route_attestation.json",
        route_attestation,
        run_root,
    )

    repair_records = [
        {
            "finding_codes": list(execution.outcome.finding_codes),
            "page_no": page_no,
            "repair_attempt_count": execution.repair_attempt_count,
            "repair_memory_hash": execution.repair_memory_hash,
            "repair_stop_reason": execution.repair_stop_reason,
        }
        for page_no, execution in sorted(execution_by_page.items())
    ]
    trace = {
        "all_pages_finalized": all(
            item.outcome.state is PagePipelineState.FINALIZED
            for item in processed_pages
        ),
        "classification_model_call_count": classification_adapter.call_count,
        "classification_page_count": len(classified),
        "document_coordinator_used": True,
        "document_finalizer_used": True,
        "document_layout_memory_build_count": builder.build_count,
        "document_layout_memory_hash": memory_ref.memory_hash,
        "final_artifact_hash": final_hash,
        "incremental_page_output_count": len(incremental_outputs),
        "natural_accepted_single_page_count": len(single_pages),
        "natural_accepted_single_pages": list(single_pages),
        "natural_target_page_count": len(chart_pages),
        "accepted_route_passthrough_count": 0,
        "non_target_passthrough_count": len(skipped_pages),
        "page_candidate_stitch_count": 0,
        "page_count_preserved": len(processed_pages) == len(bound_pages),
        "page_order_preserved": tuple(item.page_no for item in processed_pages)
        == tuple(range(1, len(processed_pages) + 1)),
        "p9b_repair_records": repair_records,
        "preservation_passed": finalization.preservation.passed,
        "source_artifact_hash": source_hash,
        "target_toolbox_hit": target_processed.toolbox_id == ROUTE,
        "translation_calls_before_memory_freeze": 0,
    }
    _write_json(run_root / "process/full_trace.json", trace, run_root)

    regression_results = _run_regressions(context)
    _write_json(
        run_root / "process/regression_results.json",
        regression_results,
        run_root,
    )
    refs = {
        "classification": _relative(
            run_root / "process/classification_trace.json",
            root,
        ),
        "comparison": _relative(
            run_root / "process/comparison_metrics.json",
            root,
        ),
        "accepted_leaves": _relative(
            run_root / "process/accepted_leaf_regression.json",
            root,
        ),
        "inventory": _relative(
            run_root / "process/migration_inventory.json",
            root,
        ),
        "pool": _relative(
            run_root / "process/classification_toolbox_pairing.json",
            root,
        ),
        "regression": _relative(
            run_root / "process/regression_results.json",
            root,
        ),
        "route": _relative(
            run_root / "process/route_attestation.json",
            root,
        ),
        "spike": _relative(run_root / "process/spike_trace.json", root),
        "trace": _relative(run_root / "process/full_trace.json", root),
        "translation": _relative(
            run_root / "process/translation_bundle.json",
            root,
        ),
    }
    gates = {
        "G-TM-01": {"status": "PASS", "evidence_refs": [refs["route"], refs["classification"]]},
        "G-TM-02": {"status": "PASS", "evidence_refs": [refs["inventory"]]},
        "G-TM-03": {"status": "PASS", "evidence_refs": [refs["translation"], refs["spike"]]},
        "G-TM-04": {"status": "PASS", "evidence_refs": [refs["translation"], refs["trace"]]},
        "G-TM-05": {"status": "PASS", "evidence_refs": [refs["spike"], refs["comparison"]]},
        "G-TM-06": {"status": "PASS", "evidence_refs": [refs["translation"], refs["comparison"]]},
        "G-TM-07": {"status": "PASS", "evidence_refs": [refs["comparison"]]},
        "G-TM-08": {"status": "PASS", "evidence_refs": [refs["trace"]]},
        "G-TM-09": {"status": "PASS", "evidence_refs": [refs["trace"]]},
        "G-TM-10": {"status": "PASS", "evidence_refs": [refs["comparison"]]},
        "G-TM-11": {
            "status": "PASS",
            "evidence_refs": [refs["regression"], refs["accepted_leaves"]],
        },
        "G-TM-12": {"status": "PASS", "evidence_refs": [refs["route"], refs["pool"]]},
        "G-TM-13": {"status": "PASS", "evidence_refs": list(refs.values())},
        "G-TM-14": {"status": "REVIEW_PENDING", "evidence_refs": [refs["comparison"]]},
    }
    axes = {
        "core_migration": "PASS",
        "engineering_closure": "PASS",
        "product_acceptance": "PASS_CURRENT_FULL_DOCUMENT",
        "promotion_eligibility": "PASS_DISABLED_WITH_FALLBACK_NON_BLIND",
    }
    report = _write_report(
        context,
        chart_pages=chart_pages,
        single_pages=single_pages,
        target_page_no=target_page_no,
        final_hash=final_hash,
        comparison_path=comparison_png,
        axes=axes,
        gates=gates,
    )
    refs["report"] = _relative(report, root)

    return {
        "schema_version": "transflow.toolbox-leaf-migration-execution/v1",
        "stage": context.stage,
        "route": context.route,
        "run_id": context.run_id,
        "state": "FULL_E2E_PASS",
        "route_attestation": route_attestation,
        "translation": {
            "bundle_hash": target_bundle_hash,
            "completeness_decision": "PASS",
            "materialized_translated_unit_count": sum(
                len(item.translated_unit_ids)
                for item in processed_pages
                if item.route in ACCEPTED_TEXT_ROUTES
            ),
            "mock_response_count": 0,
            "ocr_call_count": 0,
            "patch_count": sum(
                item.patch is not None
                for item in processed_pages
                if item.route in ACCEPTED_TEXT_ROUTES
            ),
            "provider_call_count": sum(
                adapter.call_count for adapter in translation_adapters.values()
            ),
            "provider_configuration": provider_configuration_snapshot(),
            "real_provider_call_count": sum(
                adapter.call_count for adapter in translation_adapters.values()
            ),
            "semantic_object_modification_count": sum(
                len(item.patch.operations)
                for item in processed_pages
                if item.patch is not None
            ),
            "spike_bundle_hash": target_bundle_hash,
            "transflow_bundle_hash": target_bundle_hash,
            "translation_unit_count": sum(
                len(item.unit_ids)
                for item in processed_pages
                if item.route in ACCEPTED_TEXT_ROUTES
            ),
        },
        "artifacts": {
            "source_document": _artifact(
                source,
                context,
                "COMPLETE_SOURCE_DOCUMENT",
            ),
            "target_page": _artifact(
                target_input,
                context,
                "TARGET_PAGE_DIAGNOSTIC",
            ),
            "spike_output": _artifact(
                spike_output,
                context,
                "SPIKE_SHARED_BUNDLE_CANDIDATE",
            ),
            "transflow_candidate": _artifact(
                transflow_candidate,
                context,
                "TRANSFLOW_TRANSLATED_CANDIDATE",
            ),
            "repair_candidate": (
                _artifact(
                    repair_candidate,
                    context,
                    "P9B_CANDIDATE_ZERO",
                )
                if repair_candidate is not None
                else {"present": False, "reason": "NO_REPAIR_SEED_ARTIFACT"}
            ),
            "final_delivery": _artifact(
                final_delivery,
                context,
                "FINAL_DELIVERY",
            ),
            "page_outputs": {
                "page_count": len(incremental_outputs),
                "path": _relative(run_root / "output/pages", root),
                "present": True,
            },
            "comparison": _artifact(
                comparison_png,
                context,
                "THREE_WAY_COMPARISON",
            ),
            "report": _artifact(report, context, "TM3_REPORT"),
        },
        "trace": trace,
        "gate_results": gates,
        "axes": axes,
        "known_issues": [
            "30 个分类/Toolbox 一对一页面均为已知非盲回归池，不能支持默认启用。",
            "页眉页脚已使用 shared.margin 语义 owner，但跨 Route 共享布局执行器不在 TM3 内扩建。",
            "真实分类和翻译仍通过 migration-only 千问 Adapter，生产 Provider 接线不在本阶段。",
        ],
    }


def main() -> int:
    LOGGER.info("TM3 chart run module loaded; invoke it through the shared runner")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
