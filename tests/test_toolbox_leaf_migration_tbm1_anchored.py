"""Verify the TBM1 anchored-block lift and explicit shared blockers."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pymupdf

from transflow.adapters.ai.fixed import FixedTranslationAdapter
from transflow.application.document_coordinator import DocumentCoordinator
from transflow.application.toolbox_page_coordinator import (
    ToolboxPageCoordinator,
    ToolboxPageWork,
)
from transflow.application.translation_completeness import (
    extract_required_literals,
)
from transflow.domain.jobs import DocumentRunRequest
from transflow.domain.states import Fallback
from transflow.pdf_kernel import (
    ControlledFontRegistry,
    PageFactsExtractor,
    PagePatchInterpreter,
)
from transflow.toolboxes.catalog import load_toolbox_catalog
from transflow.toolboxes.contracts import ToolboxExecutionResult
from transflow.toolboxes.leaves.body_anchored_blocks import (
    AnchoredBlocksToolbox,
)
from transflow.toolboxes.leaves.body_anchored_blocks.template import (
    build_anchored_blocks_template,
)
from transflow.toolboxes.leaves.factory import build_p8_toolbox_factories
from transflow.toolboxes.leaves.policy import load_p8_toolbox_policy

REPO_ROOT = Path(__file__).resolve().parent.parent
CLASSIFICATION_ROOT = REPO_ROOT / "spikes/page_classification_engine_puncture_v1"
ANCHORED_SAMPLE_ROOT = next(
    CLASSIFICATION_ROOT.glob("*/body/anchored_blocks")
)
ANCHORED_SOURCE = ANCHORED_SAMPLE_ROOT / "AB_EN_12_01978_p068.pdf"
PROTECTED_SOURCE = ANCHORED_SAMPLE_ROOT / "AB_EN_05_01795_p085.pdf"
DEFAULT_CATALOG = REPO_ROOT / "resources/catalogs/page_toolbox_catalog_v4.json"
FONT_MANIFEST = REPO_ROOT / "resources/manifests/font_manifest.json"
POLICY_PATH = REPO_ROOT / "resources/manifests/p8_toolbox_policy.json"
RECORDED_EQUIVALENT = "用于验证锚定文本块边界与排版的中文内容。"


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _request(source: Path, run_id: str) -> DocumentRunRequest:
    return DocumentRunRequest(
        source_pdf_path=str(source.resolve()),
        source_hash=_sha256_file(source),
        source_language="en",
        target_language="zh-CN",
        config_snapshot_hash="a" * 64,
        job_id=f"job-{run_id}",
        run_id=run_id,
    )


def _fonts() -> ControlledFontRegistry:
    return ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT)


def _toolbox() -> AnchoredBlocksToolbox:
    policy = load_p8_toolbox_policy(POLICY_PATH)
    font_path = _fonts().resolve(policy.font_id).path
    return AnchoredBlocksToolbox(policy, font_path)


def _work_items(
    source: Path,
    run_id: str,
) -> tuple[ToolboxPageWork, ...]:
    pages = DocumentCoordinator(PageFactsExtractor()).enumerate_pages(
        _request(source, run_id)
    )
    return tuple(
        ToolboxPageWork(page.context, page.facts, _toolbox())
        for page in pages
    )


def _fixed_translations(
    work_items: tuple[ToolboxPageWork, ...],
) -> dict[str, str]:
    translations: dict[str, str] = {}
    for work in work_items:
        template = work.toolbox.prepare(work.context, work.facts)
        batch = work.toolbox.build_translation_request(template)
        assert batch is not None
        for unit in batch.units:
            required = " ".join(extract_required_literals(unit.source_text))
            translations[unit.unit_id] = (
                f"{RECORDED_EQUIVALENT} {required}".strip()
            )
    return translations


def _execution_identity(
    result: ToolboxExecutionResult,
) -> tuple[object, ...]:
    patch = result.patch
    return (
        result.page_no,
        result.verdict.disposition,
        result.outcome,
        result.ordered_unit_ids,
        None if patch is None else patch.owner,
        () if patch is None else tuple(
            item.operation_id for item in patch.operations
        ),
        tuple(item.code for item in result.findings),
    )


def _contains(
    outer: tuple[float, float, float, float],
    inner: tuple[float, float, float, float],
    tolerance: float = 0.05,
) -> bool:
    return (
        inner[0] >= outer[0] - tolerance
        and inner[1] >= outer[1] - tolerance
        and inner[2] <= outer[2] + tolerance
        and inner[3] <= outer[3] + tolerance
    )


def test_tbm1_anchored_lift_produces_owner_bound_real_candidate(
    tmp_path: Path,
) -> None:
    """The real page must preserve visual owners, slots, and style groups."""

    default_hash_before = _sha256_file(DEFAULT_CATALOG)
    work = _work_items(ANCHORED_SOURCE, "tbm1-anchored-real")[0]
    policy = load_p8_toolbox_policy(POLICY_PATH)
    core = build_anchored_blocks_template(work.facts, policy)
    translations = _fixed_translations((work,))

    result = ToolboxPageCoordinator(
        FixedTranslationAdapter(translations)
    ).execute(work)

    visual_owners = tuple(
        item
        for item in core.block_owners
        if item.boundary_source == "kernel_visual_bbox"
    )
    assert visual_owners
    assert all(item.background_object_ids for item in visual_owners)
    assert not core.ambiguous_container_ids
    assert sorted(
        object_id
        for item in core.containers
        for object_id in item.source_object_ids
    ) == sorted(item.object_id for item in work.facts.text_spans)
    assert result.patch is not None
    assert result.patch.owner == "body.anchored_blocks"
    assert len(result.patch.operations) == len(core.translatable_containers)
    assert not result.findings

    container_by_id = {
        item.container_id: item for item in core.translatable_containers
    }
    operation_by_id = {
        item.region_id: item for item in result.patch.operations
    }
    for operation in result.patch.operations:
        assert operation.rect is not None
        container = container_by_id[operation.region_id]
        assert _contains(container.allowed_bbox, operation.rect)
        assert set(operation.target_object_ids) == set(
            container.source_object_ids
        )

    style_groups: dict[
        tuple[str, float, int, str],
        list[str],
    ] = {}
    for container in core.translatable_containers:
        style_groups.setdefault(
            (
                container.font_name,
                container.font_size,
                container.color_srgb,
                container.role,
            ),
            [],
        ).append(container.container_id)
    repeated = next(
        values
        for values in style_groups.values()
        if len(values) >= 2
        and len(
            {
                container_by_id[value].block_owner_id
                for value in values
            }
        )
        >= 2
    )
    assert len(
        {
            operation_by_id[container_id].font_size
            for container_id in repeated
        }
    ) == 1

    candidate = tmp_path / "anchored-candidate.pdf"
    with pymupdf.open(ANCHORED_SOURCE) as document:
        application = PagePatchInterpreter(_fonts()).apply(
            document,
            work.context,
            work.facts,
            result.patch,
            "body.anchored_blocks",
        )
        assert application.fits
        assert application.applied_count == len(result.patch.operations)
        document.save(candidate)
    with pymupdf.open(candidate) as document:
        assert document.page_count == 1
        assert RECORDED_EQUIVALENT in document[0].get_text()
    assert _sha256_file(DEFAULT_CATALOG) == default_hash_before


def test_tbm1_anchored_inventory_protection_is_preauthorized() -> None:
    """Pure numeric/card labels stay source; mixed literals remain attached."""

    work = _work_items(PROTECTED_SOURCE, "tbm1-anchored-protected")[0]
    policy = load_p8_toolbox_policy(POLICY_PATH)
    core = build_anchored_blocks_template(work.facts, policy)
    protected = tuple(
        item
        for item in core.containers
        if item.inline_keep_source_object_ids
        and not item.translation_object_ids
    )
    assert protected

    template = work.toolbox.prepare(work.context, work.facts)
    batch = work.toolbox.build_translation_request(template)
    assert batch is not None
    requested_ids = {
        object_id
        for unit in batch.units
        for object_id in (
            *unit.source_object_ids,
            *unit.inline_keep_source_object_ids,
        )
    }
    assert not {
        object_id
        for item in protected
        for object_id in item.source_object_ids
    }.intersection(requested_ids)
    assert any(unit.inline_keep_source_object_ids for unit in batch.units)


def test_tbm1_anchored_failure_keeps_translated_diagnostic(
    tmp_path: Path,
) -> None:
    """Unfit block text remains a materialized, marked non-candidate."""

    work = _work_items(ANCHORED_SOURCE, "tbm1-anchored-diagnostic")[0]
    template = work.toolbox.prepare(work.context, work.facts)
    batch = work.toolbox.build_translation_request(template)
    assert batch is not None
    long_text = "用于验证锚定文本块溢出诊断的超长中文内容。" * 100
    translations = {
        unit.unit_id: (
            f"{long_text} "
            f"{' '.join(extract_required_literals(unit.source_text))}"
        ).strip()
        for unit in batch.units
    }

    result = ToolboxPageCoordinator(
        FixedTranslationAdapter(translations)
    ).execute(work)

    assert result.patch is None
    assert result.proposed_patch is not None
    assert result.outcome.fallback is Fallback.PAGE_PASSTHROUGH
    assert "ANCHORED_BLOCK_TEXT_OVERFLOW" in {
        item.code for item in result.findings
    }

    diagnostic = tmp_path / "anchored-layout-failure-diagnostic.pdf"
    with pymupdf.open(ANCHORED_SOURCE) as document:
        application = PagePatchInterpreter(_fonts()).apply(
            document,
            work.context,
            work.facts,
            result.proposed_patch,
            "body.anchored_blocks",
            diagnostic=True,
        )
        assert application.fits
        page = document[0]
        for operation in result.proposed_patch.operations:
            assert operation.rect is not None
            page.draw_rect(
                pymupdf.Rect(operation.rect),
                color=(1, 0, 0),
                width=1.5,
                overlay=True,
            )
        page.insert_text(
            (18, 24),
            "TBM1 FAILURE DIAGNOSTIC: ANCHORED_BLOCK_TEXT_OVERFLOW",
            fontname="helv",
            fontsize=8,
            color=(1, 0, 0),
            overlay=True,
        )
        metadata = document.metadata
        metadata["subject"] = (
            "FAILURE DIAGNOSTIC - NOT A TRANSLATED CANDIDATE"
        )
        document.set_metadata(metadata)
        document.save(diagnostic)
    with pymupdf.open(diagnostic) as document:
        assert document.metadata.get("subject") == (
            "FAILURE DIAGNOSTIC - NOT A TRANSLATED CANDIDATE"
        )
        assert long_text[:12] in document[0].get_text()


def test_tbm1_anchored_catalog_is_run_private_and_default_disabled(
    tmp_path: Path,
) -> None:
    """The dedicated factory is reachable only through a run-private overlay."""

    default_bytes = DEFAULT_CATALOG.read_bytes()
    payload = json.loads(default_bytes.decode("utf-8"))
    entry = next(
        item
        for item in payload["entries"]
        if item["route"] == "body.anchored_blocks"
    )
    entry.update(
        {
            "enabled": True,
            "evidence_state": "PASS_ENABLE",
            "evidence_attestation_hash": "d" * 64,
            "disabled_reason": None,
        }
    )
    overlay = tmp_path / "page_toolbox_catalog_tbm1_anchored.json"
    overlay.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    factories = build_p8_toolbox_factories(
        POLICY_PATH,
        FONT_MANIFEST,
        REPO_ROOT,
    )
    factories["body.anchored_blocks"] = _toolbox

    enabled_catalog = load_toolbox_catalog(overlay, factories)
    assert enabled_catalog.validate_startup().ready
    enabled = enabled_catalog.resolve_enabled("body.anchored_blocks", 1)
    assert enabled.toolbox is not None
    assert enabled.toolbox.descriptor.route == "body.anchored_blocks"
    assert enabled.toolbox.descriptor.toolbox_id == "body.anchored_blocks"

    disabled_catalog = load_toolbox_catalog(
        DEFAULT_CATALOG,
        build_p8_toolbox_factories(
            POLICY_PATH,
            FONT_MANIFEST,
            REPO_ROOT,
        ),
    )
    disabled = disabled_catalog.resolve_enabled("body.anchored_blocks", 1)
    assert disabled.toolbox is None
    assert disabled.finding is not None
    assert disabled.finding.code == "TOOLBOX_DISABLED"
    assert _sha256_file(DEFAULT_CATALOG) == hashlib.sha256(
        default_bytes
    ).hexdigest()


def test_tbm1_anchored_shared_page_concurrency_is_equivalent(
    tmp_path: Path,
) -> None:
    """Fresh anchored instances remain stable at concurrency one and two."""

    two_page_source = tmp_path / "two-anchored-pages.pdf"
    with pymupdf.open() as target, pymupdf.open(ANCHORED_SOURCE) as source:
        target.insert_pdf(source)
        target.insert_pdf(source)
        target.save(two_page_source)

    sequential_work = _work_items(
        two_page_source,
        "tbm1-anchored-concurrency",
    )
    parallel_work = _work_items(
        two_page_source,
        "tbm1-anchored-concurrency",
    )
    translations = _fixed_translations(sequential_work)
    translations.update(_fixed_translations(parallel_work))
    coordinator = ToolboxPageCoordinator(FixedTranslationAdapter(translations))

    sequential = coordinator.execute_many(sequential_work, 1)
    parallel = coordinator.execute_many(parallel_work, 2)

    assert tuple(item.page_no for item in sequential) == (1, 2)
    assert tuple(item.page_no for item in parallel) == (1, 2)
    assert tuple(map(_execution_identity, sequential)) == tuple(
        map(_execution_identity, parallel)
    )
    assert all(item.patch is not None for item in parallel)
    assert all(
        item.patch.owner == "body.anchored_blocks"
        for item in parallel
        if item.patch
    )


def test_tbm1_anchored_shared_margin_owner_gap_remains_explicit() -> None:
    """Global margin semantics remain visible despite the leaf-owned Patch."""

    work = _work_items(ANCHORED_SOURCE, "tbm1-anchored-margin-owner")[0]
    result = ToolboxPageCoordinator(
        FixedTranslationAdapter(_fixed_translations((work,)))
    ).execute(work)

    assert result.patch is not None
    assert result.semantic_unit_map is not None
    shared_margin_units = tuple(
        item
        for item in result.semantic_unit_map.entries
        if item.owner.startswith("shared.margin.")
        and item.disposition.value == "TRANSLATE"
    )
    assert shared_margin_units
    margin_ids = {
        object_id
        for unit in shared_margin_units
        for object_id in (
            *unit.source_object_ids,
            *unit.inline_keep_source_object_ids,
        )
    }
    leaf_owned = tuple(
        item
        for item in result.patch.operations
        if margin_ids.intersection(item.target_object_ids)
    )
    assert leaf_owned
    assert all(item.owner == "body.anchored_blocks" for item in leaf_owned)


def test_tbm1_anchored_production_leaf_has_no_spike_or_provider_runtime() -> None:
    """The production package consumes contracts, never Spike orchestration."""

    package = (
        REPO_ROOT
        / "src/transflow/toolboxes/leaves/body_anchored_blocks"
    )
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(package.glob("*.py"))
    )
    forbidden = (
        "spikes.",
        "tests.",
        "runs.",
        "HttpAiCapabilityAdapter",
        "TranslationProvider",
        "httpx",
        "ThreadPoolExecutor",
        "source_pdf_path",
    )
    assert not any(value in source for value in forbidden)
