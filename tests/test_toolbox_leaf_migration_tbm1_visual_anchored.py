"""Verify the TBM1 visual-anchored lift and its explicit blockers."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
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
from transflow.toolboxes.leaves.body_flow_text_visual_anchored import (
    VisualAnchoredToolbox,
)
from transflow.toolboxes.leaves.body_flow_text_visual_anchored.template import (
    build_visual_anchored_template,
)
from transflow.toolboxes.leaves.factory import build_p8_toolbox_factories
from transflow.toolboxes.leaves.policy import (
    P8ToolboxPolicy,
    load_p8_toolbox_policy,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
CLASSIFICATION_ROOT = REPO_ROOT / "spikes/page_classification_engine_puncture_v1"
VISUAL_SAMPLE_ROOT = next(CLASSIFICATION_ROOT.glob("*/body/flow_text/visual_anchored"))
VISUAL_SOURCE = VISUAL_SAMPLE_ROOT / "EN_00468_p0010.pdf"
BILINGUAL_SOURCE = VISUAL_SAMPLE_ROOT / "EN_02136_p0003.pdf"
LOW_CONTRAST_SOURCE = VISUAL_SAMPLE_ROOT / "ZH_02298_p0006.pdf"
DEFAULT_CATALOG = REPO_ROOT / "resources/catalogs/page_toolbox_catalog_v4.json"
FONT_MANIFEST = REPO_ROOT / "resources/manifests/font_manifest.json"
POLICY_PATH = REPO_ROOT / "resources/manifests/p8_toolbox_policy.json"
RECORDED_TRANSLATIONS = (
    "董事会主席致辞",
    "袁勋军",
    "首席执行官、董事会主席兼执行董事",
    "公司本年度稳步推进业务整合、技术创新和管理提升，持续夯实长期发展的基础。",
    "无菌包装有限公司年度报告",
)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _request(
    source: Path,
    run_id: str,
    policy: P8ToolboxPolicy,
) -> DocumentRunRequest:
    return DocumentRunRequest(
        source_pdf_path=str(source.resolve()),
        source_hash=_sha256_file(source),
        source_language=policy.source_language,
        target_language=policy.target_language,
        config_snapshot_hash="a" * 64,
        job_id=f"job-{run_id}",
        run_id=run_id,
    )


def _fonts() -> ControlledFontRegistry:
    return ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT)


def _policy(
    source_language: str = "en",
    target_language: str = "zh-CN",
) -> P8ToolboxPolicy:
    return replace(
        load_p8_toolbox_policy(POLICY_PATH),
        source_language=source_language,
        target_language=target_language,
    )


def _toolbox(
    policy: P8ToolboxPolicy | None = None,
) -> VisualAnchoredToolbox:
    selected = policy or _policy()
    font_path = _fonts().resolve(selected.font_id).path
    return VisualAnchoredToolbox(selected, font_path)


def _work_items(
    source: Path,
    run_id: str,
    policy: P8ToolboxPolicy | None = None,
) -> tuple[ToolboxPageWork, ...]:
    selected = policy or _policy()
    pages = DocumentCoordinator(PageFactsExtractor()).enumerate_pages(
        _request(source, run_id, selected)
    )
    return tuple(
        ToolboxPageWork(
            page.context,
            page.facts,
            _toolbox(selected),
            target_language=selected.target_language,
        )
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
        for ordinal, unit in enumerate(batch.units):
            required = " ".join(extract_required_literals(unit.source_text))
            text = RECORDED_TRANSLATIONS[min(ordinal, len(RECORDED_TRANSLATIONS) - 1)]
            translations[unit.unit_id] = f"{text} {required}".strip()
    return translations


def _execution_identity(
    result: ToolboxExecutionResult,
) -> tuple[object, ...]:
    proposed = result.proposed_patch
    return (
        result.page_no,
        result.verdict.disposition,
        result.outcome,
        result.ordered_unit_ids,
        None if proposed is None else proposed.owner,
        () if proposed is None else tuple(item.operation_id for item in proposed.operations),
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


def test_tbm1_visual_anchored_materializes_source_bound_candidate(
    tmp_path: Path,
) -> None:
    """The real page keeps slots and anchors while exposing evidence gaps."""

    default_hash_before = _sha256_file(DEFAULT_CATALOG)
    work = _work_items(
        VISUAL_SOURCE,
        "tbm1-visual-anchored-real",
    )[0]
    policy = _policy()
    core = build_visual_anchored_template(work.facts, policy)
    result = ToolboxPageCoordinator(FixedTranslationAdapter(_fixed_translations((work,)))).execute(
        work
    )

    container_ids = [object_id for item in core.containers for object_id in item.source_object_ids]
    assert len(container_ids) == len(set(container_ids))
    assert {item.object_id for item in work.facts.text_spans} == set(container_ids).union(
        core.protected_object_ids
    )
    assert core.locked_visual_object_ids == tuple(
        [
            *(item.object_id for item in work.facts.image_objects),
            *(item.object_id for item in work.facts.drawing_objects),
        ]
    )
    assert not core.capability_codes
    assert not core.ambiguous_container_ids
    assert {"LEFT", "RIGHT"}.issubset({item.alignment for item in core.containers})

    assert result.patch is None
    assert result.proposed_patch is not None
    assert result.proposed_patch.owner == ("body.flow_text.visual_anchored")
    assert len(result.proposed_patch.operations) == len(core.translatable_containers)
    assert {item.code for item in result.findings} == {"VISUAL_BACKGROUND_EVIDENCE_MISSING"}
    assert result.outcome.fallback is Fallback.PAGE_PASSTHROUGH

    containers = {item.container_id: item for item in core.translatable_containers}
    for operation in result.proposed_patch.operations:
        assert operation.rect is not None
        container = containers[operation.region_id]
        assert _contains(
            container.hard_boundary_bbox,
            operation.rect,
        )
        assert set(operation.target_object_ids) == set(container.source_object_ids)
        assert operation.text_align == container.alignment
        assert operation.preserve_drawing_overlap

    candidate = tmp_path / "visual-anchored-candidate.pdf"
    with pymupdf.open(VISUAL_SOURCE) as document:
        application = PagePatchInterpreter(_fonts()).apply(
            document,
            work.context,
            work.facts,
            result.proposed_patch,
            "body.flow_text.visual_anchored",
        )
        assert application.fits
        document.save(candidate)
    with pymupdf.open(candidate) as document:
        assert document.page_count == 1
        assert RECORDED_TRANSLATIONS[0] in document[0].get_text()
    assert _sha256_file(DEFAULT_CATALOG) == default_hash_before


def test_tbm1_visual_anchored_does_not_geometry_deduplicate_bilingual_text() -> None:
    """Structural companions require semantics and never suppress a Patch."""

    work = _work_items(
        BILINGUAL_SOURCE,
        "tbm1-visual-anchored-bilingual",
    )[0]
    policy = _policy()
    core = build_visual_anchored_template(work.facts, policy)
    template = work.toolbox.prepare(work.context, work.facts)
    batch = work.toolbox.build_translation_request(template)
    assert batch is not None

    result = ToolboxPageCoordinator(FixedTranslationAdapter(_fixed_translations((work,)))).execute(
        work
    )

    assert core.bilingual_candidates
    assert "VISUAL_BILINGUAL_SEMANTIC_DECISION_REQUIRED" in {item.code for item in result.findings}
    assert result.patch is None
    assert result.proposed_patch is not None
    assert len(result.proposed_patch.operations) == len(batch.units)
    requested_object_ids = {
        object_id for unit in batch.units for object_id in unit.source_object_ids
    }
    assert {
        object_id
        for item in core.containers
        if not item.translation_object_ids
        for object_id in item.source_object_ids
    }.isdisjoint(requested_object_ids)


def test_tbm1_visual_anchored_missing_background_fact_is_honest() -> None:
    """The leaf does not reopen the PDF or claim an inferred contrast PASS."""

    policy = _policy("zh-CN", "en")
    work = _work_items(
        LOW_CONTRAST_SOURCE,
        "tbm1-visual-anchored-contrast",
        policy,
    )[0]
    core = build_visual_anchored_template(work.facts, policy)
    visual_slots = tuple(
        item for item in core.visual_slots if item.background_evidence == "KERNEL_GEOMETRY_ONLY"
    )
    assert visual_slots
    assert all(item.background_rgb is None for item in visual_slots)
    assert all(item.source_contrast_ratio is None for item in visual_slots)

    template = work.toolbox.prepare(work.context, work.facts)
    batch = work.toolbox.build_translation_request(template)
    assert batch is not None
    translations = {
        unit.unit_id: (
            "The report describes the principal progress, decisions, "
            "and plans for the year. " + " ".join(extract_required_literals(unit.source_text))
        ).strip()
        for unit in batch.units
    }
    result = ToolboxPageCoordinator(FixedTranslationAdapter(translations)).execute(work)
    codes = {item.code for item in result.findings}
    assert "VISUAL_BACKGROUND_EVIDENCE_MISSING" in codes
    assert "VISUAL_CONTRAST_LOW" not in codes
    assert result.proposed_patch is not None


def test_tbm1_visual_anchored_failure_keeps_translated_diagnostic(
    tmp_path: Path,
) -> None:
    """Overflow remains materialized and marked as a non-candidate."""

    work = _work_items(
        VISUAL_SOURCE,
        "tbm1-visual-anchored-diagnostic",
    )[0]
    template = work.toolbox.prepare(work.context, work.facts)
    batch = work.toolbox.build_translation_request(template)
    assert batch is not None
    long_text = "这是一段用于检查视觉锚定槽位溢出诊断的超长中文内容。" * 20
    translations = {
        unit.unit_id: (
            f"{long_text} {' '.join(extract_required_literals(unit.source_text))}"
        ).strip()
        for unit in batch.units
    }

    result = ToolboxPageCoordinator(FixedTranslationAdapter(translations)).execute(work)

    assert result.patch is None
    assert result.proposed_patch is not None
    assert "VISUAL_SLOT_OVERFLOW" in {item.code for item in result.findings}
    diagnostic = tmp_path / "visual-anchored-failure.pdf"
    with pymupdf.open(VISUAL_SOURCE) as document:
        application = PagePatchInterpreter(_fonts()).apply(
            document,
            work.context,
            work.facts,
            result.proposed_patch,
            "body.flow_text.visual_anchored",
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
            "TBM1 FAILURE DIAGNOSTIC: VISUAL_SLOT_OVERFLOW",
            fontname="helv",
            fontsize=8,
            color=(1, 0, 0),
            overlay=True,
        )
        metadata = document.metadata
        metadata["subject"] = "FAILURE DIAGNOSTIC - NOT A TRANSLATED CANDIDATE"
        document.set_metadata(metadata)
        document.save(diagnostic)
    with pymupdf.open(diagnostic) as document:
        assert document.metadata.get("subject") == (
            "FAILURE DIAGNOSTIC - NOT A TRANSLATED CANDIDATE"
        )
        assert long_text[:12] in document[0].get_text()


def test_tbm1_visual_anchored_catalog_is_private_and_default_disabled(
    tmp_path: Path,
) -> None:
    """Only a run-private overlay can resolve the dedicated factory."""

    default_bytes = DEFAULT_CATALOG.read_bytes()
    payload = json.loads(default_bytes.decode("utf-8"))
    entry = next(
        item for item in payload["entries"] if item["route"] == "body.flow_text.visual_anchored"
    )
    entry.update(
        {
            "enabled": True,
            "evidence_state": "PASS_ENABLE",
            "evidence_attestation_hash": "e" * 64,
            "disabled_reason": None,
        }
    )
    overlay = tmp_path / "page_toolbox_catalog_tbm1_visual.json"
    overlay.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    factories = build_p8_toolbox_factories(
        POLICY_PATH,
        FONT_MANIFEST,
        REPO_ROOT,
    )
    factories["body.flow_text.visual_anchored"] = _toolbox

    enabled_catalog = load_toolbox_catalog(overlay, factories)
    assert enabled_catalog.validate_startup().ready
    enabled = enabled_catalog.resolve_enabled(
        "body.flow_text.visual_anchored",
        1,
    )
    assert enabled.toolbox is not None
    assert enabled.toolbox.descriptor.route == ("body.flow_text.visual_anchored")
    assert enabled.toolbox.descriptor.toolbox_id == ("body.flow_text.visual_anchored")

    disabled_catalog = load_toolbox_catalog(
        DEFAULT_CATALOG,
        build_p8_toolbox_factories(
            POLICY_PATH,
            FONT_MANIFEST,
            REPO_ROOT,
        ),
    )
    disabled = disabled_catalog.resolve_enabled(
        "body.flow_text.visual_anchored",
        1,
    )
    assert disabled.toolbox is None
    assert disabled.finding is not None
    assert disabled.finding.code == "TOOLBOX_DISABLED"
    assert _sha256_file(DEFAULT_CATALOG) == hashlib.sha256(default_bytes).hexdigest()


def test_tbm1_visual_anchored_shared_concurrency_is_equivalent(
    tmp_path: Path,
) -> None:
    """Fresh instances remain deterministic at concurrency one and two."""

    two_page_source = tmp_path / "two-visual-anchored-pages.pdf"
    with pymupdf.open() as target, pymupdf.open(VISUAL_SOURCE) as source:
        target.insert_pdf(source)
        target.insert_pdf(source)
        target.save(two_page_source)

    sequential_work = _work_items(
        two_page_source,
        "tbm1-visual-anchored-concurrency",
    )
    parallel_work = _work_items(
        two_page_source,
        "tbm1-visual-anchored-concurrency",
    )
    translations = _fixed_translations(sequential_work)
    translations.update(_fixed_translations(parallel_work))
    coordinator = ToolboxPageCoordinator(FixedTranslationAdapter(translations))

    sequential = coordinator.execute_many(sequential_work, 1)
    parallel = coordinator.execute_many(parallel_work, 2)

    assert tuple(item.page_no for item in sequential) == (1, 2)
    assert tuple(item.page_no for item in parallel) == (1, 2)
    assert tuple(map(_execution_identity, sequential)) == tuple(map(_execution_identity, parallel))
    assert all(item.patch is None for item in parallel)
    assert all(item.proposed_patch is not None for item in parallel)
    assert all(
        item.proposed_patch.owner == "body.flow_text.visual_anchored"
        for item in parallel
        if item.proposed_patch is not None
    )


def test_tbm1_visual_anchored_shared_margin_gap_remains_explicit() -> None:
    """Margin semantics stay global despite the current leaf-owned proposal."""

    work = _work_items(
        VISUAL_SOURCE,
        "tbm1-visual-anchored-margin",
    )[0]
    result = ToolboxPageCoordinator(FixedTranslationAdapter(_fixed_translations((work,)))).execute(
        work
    )

    assert result.proposed_patch is not None
    assert result.semantic_unit_map is not None
    shared_margin_units = tuple(
        item
        for item in result.semantic_unit_map.entries
        if item.owner.startswith("shared.margin.") and item.disposition.value == "TRANSLATE"
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
        for item in result.proposed_patch.operations
        if margin_ids.intersection(item.target_object_ids)
    )
    assert leaf_owned
    assert all(item.owner == "body.flow_text.visual_anchored" for item in leaf_owned)


def test_tbm1_visual_anchored_has_no_spike_or_provider_runtime() -> None:
    """The package consumes production facts and never Spike orchestration."""

    package = REPO_ROOT / "src/transflow/toolboxes/leaves/body_flow_text_visual_anchored"
    source = "\n".join(path.read_text(encoding="utf-8") for path in sorted(package.glob("*.py")))
    forbidden = (
        "spikes.",
        "tests.",
        "runs.",
        "HttpAiCapabilityAdapter",
        "TranslationProvider",
        "httpx",
        "ThreadPoolExecutor",
        "source_pdf_path",
        "page_image_png",
        "EN_00468",
        "EN_02136",
        "ZH_02298",
    )
    assert not any(value in source for value in forbidden)
