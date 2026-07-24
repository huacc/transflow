"""Verify the TBM1 table lift and its explicit shared-contract blockers."""

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
from transflow.toolboxes.leaves.body_table import TableToolbox
from transflow.toolboxes.leaves.body_table.template import build_table_template
from transflow.toolboxes.leaves.factory import build_p8_toolbox_factories
from transflow.toolboxes.leaves.policy import load_p8_toolbox_policy

REPO_ROOT = Path(__file__).resolve().parent.parent
CLASSIFICATION_ROOT = REPO_ROOT / "spikes/page_classification_engine_puncture_v1"
TABLE_SAMPLE_ROOT = next(CLASSIFICATION_ROOT.glob("*/body/table"))
TABLE_SOURCE = next(TABLE_SAMPLE_ROOT.glob("*00356*095*.pdf"))
UNDETECTED_BORDERLESS_SOURCE = next(TABLE_SAMPLE_ROOT.glob("*00235*080*.pdf"))
DEFAULT_CATALOG = REPO_ROOT / "resources/catalogs/page_toolbox_catalog_v4.json"
FONT_MANIFEST = REPO_ROOT / "resources/manifests/font_manifest.json"
POLICY_PATH = REPO_ROOT / "resources/manifests/p8_toolbox_policy.json"
RECORDED_EQUIVALENT = "表格中文内容"


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


def _toolbox() -> TableToolbox:
    policy = load_p8_toolbox_policy(POLICY_PATH)
    font_path = _fonts().resolve(policy.font_id).path
    return TableToolbox(policy, font_path)


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


def test_tbm1_table_lifted_core_produces_cell_bound_real_candidate(
    tmp_path: Path,
) -> None:
    """The real table must keep cell boundaries and emit a replayable Patch."""

    default_hash_before = _sha256_file(DEFAULT_CATALOG)
    work = _work_items(TABLE_SOURCE, "tbm1-table-real")[0]
    policy = load_p8_toolbox_policy(POLICY_PATH)
    core_template = build_table_template(work.facts, policy)
    translations = _fixed_translations((work,))

    result = ToolboxPageCoordinator(FixedTranslationAdapter(translations)).execute(
        work
    )

    assert len(core_template.structures) == 1
    assert core_template.structures[0].cell_bboxes
    assert not any(item.ownership_ambiguous for item in core_template.cells)
    assert result.patch is not None
    assert result.patch.owner == "body.table"
    assert len(result.patch.operations) == len(core_template.translatable_cells)
    assert not result.findings
    cell_by_id = {
        item.container_id: item for item in core_template.translatable_cells
    }
    for operation in result.patch.operations:
        assert operation.rect is not None
        cell = cell_by_id[operation.region_id]
        assert _contains(cell.hard_legal_boundary, operation.rect)
        assert set(operation.target_object_ids) == set(cell.source_object_ids)

    candidate = tmp_path / "table-candidate.pdf"
    with pymupdf.open(TABLE_SOURCE) as document:
        application = PagePatchInterpreter(_fonts()).apply(
            document,
            work.context,
            work.facts,
            result.patch,
            "body.table",
        )
        assert application.fits
        assert application.applied_count == len(result.patch.operations)
        document.save(candidate)
    with pymupdf.open(candidate) as document:
        assert document.page_count == 1
        assert RECORDED_EQUIVALENT in document[0].get_text()
    assert _sha256_file(DEFAULT_CATALOG) == default_hash_before


def test_tbm1_table_numeric_cells_and_required_literals_are_preauthorized() -> None:
    """Pure values stay source and mixed-span literals stay in translated text."""

    work = _work_items(TABLE_SOURCE, "tbm1-table-protected")[0]
    policy = load_p8_toolbox_policy(POLICY_PATH)
    core_template = build_table_template(work.facts, policy)
    pure_protected = tuple(
        item
        for item in core_template.cells
        if item.inline_keep_source_object_ids
        and not item.translation_object_ids
        and item.table_id != "page-context"
    )
    assert pure_protected

    template = work.toolbox.prepare(work.context, work.facts)
    batch = work.toolbox.build_translation_request(template)
    assert batch is not None
    assert all(
        item.container_id not in {unit.region_id for unit in batch.units}
        for item in pure_protected
    )

    translations = _fixed_translations((work,))
    result = ToolboxPageCoordinator(
        FixedTranslationAdapter(translations)
    ).execute(work)
    assert result.patch is not None
    assert not {
        object_id
        for item in pure_protected
        for object_id in item.source_object_ids
    }.intersection(
        object_id
        for operation in result.patch.operations
        for object_id in operation.target_object_ids
    )
    assert all(
        literal in translations[unit.unit_id]
        for unit in batch.units
        for literal in extract_required_literals(unit.source_text)
    )


def test_tbm1_table_failure_keeps_finding_and_viewable_diagnostic(
    tmp_path: Path,
) -> None:
    """Unfit cell text must remain a marked diagnostic, never a candidate."""

    work = _work_items(TABLE_SOURCE, "tbm1-table-diagnostic")[0]
    template = work.toolbox.prepare(work.context, work.facts)
    batch = work.toolbox.build_translation_request(template)
    assert batch is not None
    translations = {
        unit.unit_id: (
            "用于验证表格单元格布局失败的极长中文内容" * 100
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
    assert "CELL_TEXT_OVERFLOW" in {
        finding.code for finding in result.findings
    }

    diagnostic = tmp_path / "table-layout-failure-diagnostic.pdf"
    with pymupdf.open(TABLE_SOURCE) as document:
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
            "TBM1 FAILURE DIAGNOSTIC: CELL_TEXT_OVERFLOW",
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


def test_tbm1_table_missing_kernel_direct_evidence_is_not_sample_patched() -> None:
    """A classified borderless page remains a generic Kernel capability gap."""

    work = _work_items(
        UNDETECTED_BORDERLESS_SOURCE,
        "tbm1-table-undetected-borderless",
    )[0]
    policy = load_p8_toolbox_policy(POLICY_PATH)

    template = build_table_template(work.facts, policy)

    assert not work.facts.table_objects
    assert not template.structures
    assert template.translatable_cells


def test_tbm1_table_run_private_catalog_enable_and_disable_are_deterministic(
    tmp_path: Path,
) -> None:
    """The dedicated factory is reachable only through a run-private overlay."""

    default_bytes = DEFAULT_CATALOG.read_bytes()
    payload = json.loads(default_bytes.decode("utf-8"))
    entry = next(
        item for item in payload["entries"] if item["route"] == "body.table"
    )
    entry.update(
        {
            "enabled": True,
            "evidence_state": "PASS_ENABLE",
            "evidence_attestation_hash": "d" * 64,
            "disabled_reason": None,
        }
    )
    overlay = tmp_path / "page_toolbox_catalog_tbm1_table.json"
    overlay.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    factories = build_p8_toolbox_factories(
        POLICY_PATH,
        FONT_MANIFEST,
        REPO_ROOT,
    )
    factories["body.table"] = _toolbox

    enabled_catalog = load_toolbox_catalog(overlay, factories)
    assert enabled_catalog.validate_startup().ready
    enabled = enabled_catalog.resolve_enabled("body.table", 1)
    assert enabled.toolbox is not None
    assert enabled.toolbox.descriptor.route == "body.table"
    assert enabled.toolbox.descriptor.toolbox_id == "body.table"

    disabled_catalog = load_toolbox_catalog(
        DEFAULT_CATALOG,
        build_p8_toolbox_factories(
            POLICY_PATH,
            FONT_MANIFEST,
            REPO_ROOT,
        ),
    )
    disabled = disabled_catalog.resolve_enabled("body.table", 1)
    assert disabled.toolbox is None
    assert disabled.finding is not None
    assert disabled.finding.code == "TOOLBOX_DISABLED"
    assert _sha256_file(DEFAULT_CATALOG) == hashlib.sha256(default_bytes).hexdigest()


def test_tbm1_table_shared_page_concurrency_is_equivalent(
    tmp_path: Path,
) -> None:
    """Fresh table instances must be stable at concurrency one and two."""

    two_page_source = tmp_path / "two-table-pages.pdf"
    with pymupdf.open() as target, pymupdf.open(TABLE_SOURCE) as source:
        target.insert_pdf(source)
        target.insert_pdf(source)
        target.save(two_page_source)

    sequential_work = _work_items(
        two_page_source,
        "tbm1-table-concurrency",
    )
    parallel_work = _work_items(
        two_page_source,
        "tbm1-table-concurrency",
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
        item.patch.owner == "body.table"
        for item in parallel
        if item.patch
    )


def test_tbm1_table_shared_margin_owner_gap_remains_explicit() -> None:
    """Do not pretend the leaf-owned footer Patch is shared-owner ready."""

    work = _work_items(TABLE_SOURCE, "tbm1-table-margin-owner")[0]
    result = ToolboxPageCoordinator(
        FixedTranslationAdapter(_fixed_translations((work,)))
    ).execute(work)

    assert result.patch is not None
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
    assert all(item.owner == "body.table" for item in leaf_owned)
