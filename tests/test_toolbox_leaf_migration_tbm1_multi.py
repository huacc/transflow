"""Verify the TBM1 multi-column lift and its shared-margin owner blocker."""

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
from transflow.application.translation_completeness import extract_required_literals
from transflow.domain.jobs import DocumentRunRequest
from transflow.domain.states import Fallback
from transflow.pdf_kernel import (
    ControlledFontRegistry,
    PageFactsExtractor,
    PagePatchInterpreter,
)
from transflow.toolboxes.catalog import load_toolbox_catalog
from transflow.toolboxes.leaves.body_flow_text_multi import MultiFlowTextToolbox
from transflow.toolboxes.leaves.body_flow_text_multi.template import (
    build_multi_column_template,
)
from transflow.toolboxes.leaves.factory import build_p8_toolbox_factories
from transflow.toolboxes.leaves.policy import load_p8_toolbox_policy

REPO_ROOT = Path(__file__).resolve().parent.parent
CLASSIFICATION_ROOT = REPO_ROOT / "spikes/page_classification_engine_puncture_v1"
MULTI_SAMPLE_ROOT = next(CLASSIFICATION_ROOT.glob("*/body/flow_text/multi"))
MULTI_SOURCE = MULTI_SAMPLE_ROOT / "S2P0986.pdf"
DEFAULT_CATALOG = REPO_ROOT / "resources/catalogs/page_toolbox_catalog_v4.json"
FONT_MANIFEST = REPO_ROOT / "resources/manifests/font_manifest.json"
POLICY_PATH = REPO_ROOT / "resources/manifests/p8_toolbox_policy.json"
RECORDED_EQUIVALENT = "这是用于验证多栏正文排版、阅读顺序与列间边界的中文内容。"


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


def _toolbox() -> MultiFlowTextToolbox:
    policy = load_p8_toolbox_policy(POLICY_PATH)
    font_path = _fonts().resolve(policy.font_id).path
    return MultiFlowTextToolbox(policy, font_path)


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
            translated = RECORDED_EQUIVALENT
            translations[unit.unit_id] = (
                f"{translated}\n{required}" if required else translated
            )
    return translations


def _execution_identity(result: object) -> tuple[object, ...]:
    patch = result.patch
    return (
        result.page_no,
        result.verdict.disposition,
        result.outcome,
        result.ordered_unit_ids,
        None if patch is None else patch.owner,
        () if patch is None else tuple(item.operation_id for item in patch.operations),
        tuple(item.code for item in result.findings),
    )


def test_tbm1_multi_lifted_core_produces_bound_real_candidate(
    tmp_path: Path,
) -> None:
    """The real two-column page must retain bands and emit a replayable Patch."""

    default_hash_before = _sha256_file(DEFAULT_CATALOG)
    work = _work_items(MULTI_SOURCE, "tbm1-multi-real")[0]
    policy = load_p8_toolbox_policy(POLICY_PATH)
    core_template = build_multi_column_template(work.facts, policy)
    translations = _fixed_translations((work,))

    result = ToolboxPageCoordinator(FixedTranslationAdapter(translations)).execute(
        work
    )

    assert len(core_template.columns) == 2
    assert not core_template.ambiguous_container_ids
    assert len(core_template.containers) == 14
    assert result.patch is not None
    assert result.patch.owner == "body.flow_text.multi"
    assert len(result.patch.operations) == 14
    assert not result.findings
    protected_ids = {item.object_id for item in work.facts.objects if item.protected}
    assert all(
        operation.target_object_ids
        and operation.redaction_rects
        and not protected_ids.intersection(operation.target_object_ids)
        for operation in result.patch.operations
    )

    candidate = tmp_path / "multi-candidate.pdf"
    with pymupdf.open(MULTI_SOURCE) as document:
        application = PagePatchInterpreter(_fonts()).apply(
            document,
            work.context,
            work.facts,
            result.patch,
            "body.flow_text.multi",
        )
        assert application.fits
        assert application.applied_count == len(result.patch.operations)
        document.save(candidate)
    with pymupdf.open(candidate) as document:
        assert document.page_count == 1
        assert "多栏正文排版" in document[0].get_text()
    assert _sha256_file(DEFAULT_CATALOG) == default_hash_before


def test_tbm1_multi_inline_literals_use_the_preauthorized_contract() -> None:
    """Merged paragraphs must attach numeric/acronym spans without translating them."""

    work = _work_items(MULTI_SOURCE, "tbm1-multi-inline-literals")[0]
    template = work.toolbox.prepare(work.context, work.facts)
    batch = work.toolbox.build_translation_request(template)
    assert batch is not None
    paragraph = next(
        item
        for item in batch.units
        if item.inline_keep_source_object_ids
        and len(item.source_object_ids) > 1
    )

    assert set(paragraph.source_object_ids).isdisjoint(
        paragraph.inline_keep_source_object_ids
    )
    translations = _fixed_translations((work,))
    result = ToolboxPageCoordinator(FixedTranslationAdapter(translations)).execute(
        work
    )
    assert result.patch is not None
    operation = next(
        item
        for item in result.patch.operations
        if item.region_id == paragraph.region_id
    )
    assert set(paragraph.source_object_ids) <= set(operation.target_object_ids)
    assert set(paragraph.inline_keep_source_object_ids) <= set(
        operation.target_object_ids
    )


def test_tbm1_multi_failure_keeps_finding_and_viewable_diagnostic(
    tmp_path: Path,
) -> None:
    """An unfit multi-column bundle must remain a marked diagnostic."""

    work = _work_items(MULTI_SOURCE, "tbm1-multi-diagnostic")[0]
    template = work.toolbox.prepare(work.context, work.facts)
    batch = work.toolbox.build_translation_request(template)
    assert batch is not None
    translations = {
        unit.unit_id: (
            "用于验证多栏布局失败的超长中文译文" * 100
            + " "
            + " ".join(extract_required_literals(unit.source_text))
        ).strip()
        for unit in batch.units
    }

    result = ToolboxPageCoordinator(FixedTranslationAdapter(translations)).execute(
        work
    )

    assert result.patch is None
    assert result.proposed_patch is not None
    assert result.outcome.fallback is Fallback.PAGE_PASSTHROUGH
    assert "MULTI_TEXT_OVERFLOW" in {
        finding.code for finding in result.findings
    }

    diagnostic = tmp_path / "multi-layout-failure-diagnostic.pdf"
    with pymupdf.open(MULTI_SOURCE) as document:
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
            "TBM1 FAILURE DIAGNOSTIC: MULTI_TEXT_OVERFLOW",
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


def test_tbm1_multi_run_private_catalog_enable_and_disable_are_deterministic(
    tmp_path: Path,
) -> None:
    """The dedicated factory is used only by a run-private overlay."""

    default_bytes = DEFAULT_CATALOG.read_bytes()
    payload = json.loads(default_bytes.decode("utf-8"))
    entry = next(
        item
        for item in payload["entries"]
        if item["route"] == "body.flow_text.multi"
    )
    entry.update(
        {
            "enabled": True,
            "evidence_state": "PASS_ENABLE",
            "evidence_attestation_hash": "d" * 64,
            "disabled_reason": None,
        }
    )
    overlay = tmp_path / "page_toolbox_catalog_tbm1_multi.json"
    overlay.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    factories = build_p8_toolbox_factories(
        POLICY_PATH,
        FONT_MANIFEST,
        REPO_ROOT,
    )
    factories["body.flow_text.multi"] = _toolbox

    enabled_catalog = load_toolbox_catalog(overlay, factories)
    assert enabled_catalog.validate_startup().ready
    enabled = enabled_catalog.resolve_enabled("body.flow_text.multi", 1)
    assert enabled.toolbox is not None
    assert enabled.toolbox.descriptor.route == "body.flow_text.multi"
    assert enabled.toolbox.descriptor.toolbox_id == "body.flow_text.multi"

    disabled_catalog = load_toolbox_catalog(
        DEFAULT_CATALOG,
        build_p8_toolbox_factories(
            POLICY_PATH,
            FONT_MANIFEST,
            REPO_ROOT,
        ),
    )
    disabled = disabled_catalog.resolve_enabled("body.flow_text.multi", 1)
    assert disabled.toolbox is None
    assert disabled.finding is not None
    assert disabled.finding.code == "TOOLBOX_DISABLED"
    assert _sha256_file(DEFAULT_CATALOG) == hashlib.sha256(default_bytes).hexdigest()


def test_tbm1_multi_shared_page_concurrency_is_equivalent(
    tmp_path: Path,
) -> None:
    """Fresh multi instances must be stable at concurrency one and two."""

    two_page_source = tmp_path / "two-multi-pages.pdf"
    with pymupdf.open() as target, pymupdf.open(MULTI_SOURCE) as source:
        target.insert_pdf(source)
        target.insert_pdf(source)
        target.save(two_page_source)

    sequential_work = _work_items(
        two_page_source,
        "tbm1-multi-concurrency",
    )
    parallel_work = _work_items(
        two_page_source,
        "tbm1-multi-concurrency",
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
        item.patch.owner == "body.flow_text.multi"
        for item in parallel
        if item.patch
    )


def test_tbm1_multi_shared_margin_owner_gap_remains_explicit() -> None:
    """Do not silently upgrade leaf-owned margin Patch operations to ready."""

    work = _work_items(MULTI_SOURCE, "tbm1-multi-margin-owner")[0]
    translations = _fixed_translations((work,))
    result = ToolboxPageCoordinator(FixedTranslationAdapter(translations)).execute(
        work
    )

    assert result.patch is not None
    shared_margin_units = tuple(
        item
        for item in result.semantic_unit_map.entries
        if item.owner.startswith("shared.margin.")
        and item.disposition.value == "TRANSLATE"
    )
    assert shared_margin_units
    margin_object_ids = {
        object_id
        for unit in shared_margin_units
        for object_id in (
            *unit.source_object_ids,
            *unit.inline_keep_source_object_ids,
        )
    }
    leaf_owned_margin_operations = tuple(
        item
        for item in result.patch.operations
        if margin_object_ids.intersection(item.target_object_ids)
    )
    assert leaf_owned_margin_operations
    assert all(
        item.owner == "body.flow_text.multi"
        for item in leaf_owned_margin_operations
    )
