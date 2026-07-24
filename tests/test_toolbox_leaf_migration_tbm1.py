"""Verify the TBM1 atomic-leaf production wrapper and thin-gate contracts."""

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
from transflow.toolboxes.leaves.cover import CoverToolbox
from transflow.toolboxes.leaves.factory import build_p8_toolbox_factories
from transflow.toolboxes.leaves.policy import load_p8_toolbox_policy

REPO_ROOT = Path(__file__).resolve().parent.parent
CLASSIFICATION_ROOT = REPO_ROOT / "spikes/page_classification_engine_puncture_v1"
COVER_SAMPLE_ROOT = next(CLASSIFICATION_ROOT.glob("*/cover"))
COVER_SOURCE = COVER_SAMPLE_ROOT / "S2P0302.pdf"
OFFSET_CROPBOX_SOURCE = COVER_SAMPLE_ROOT / "S2P0041.pdf"
DEFAULT_CATALOG = REPO_ROOT / "resources/catalogs/page_toolbox_catalog_v4.json"
FONT_MANIFEST = REPO_ROOT / "resources/manifests/font_manifest.json"
POLICY_PATH = REPO_ROOT / "resources/manifests/p8_toolbox_policy.json"


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


def _cover_toolbox(source: Path) -> CoverToolbox:
    policy = load_p8_toolbox_policy(POLICY_PATH)
    font_path = _fonts().resolve(policy.font_id).path
    return CoverToolbox(policy, font_path, source)


def _cover_translation(source_text: str) -> str:
    recorded_equivalent = {
        "ANNUAL REPORT": "年度报告",
        "ChinaAMC RMB Money Market ETF": "华夏人民币货币市场ETF（RMB）",
        "(Stock Code: 3161 (HKD counter) and 83161 (RMB counter))": (
            "（股票代码：3161（HKD港元柜台）及83161（RMB人民币柜台））"
        ),
        "(a Sub-Fund of ChinaAMC Global ETF Series)": "（华夏全球ETF系列之子基金）",
        "For the year ended 31 December 2025": "截至2025年12月31日止年度",
    }
    return recorded_equivalent[source_text]


def _fixed_translations(work_items: tuple[ToolboxPageWork, ...]) -> dict[str, str]:
    translations: dict[str, str] = {}
    for work in work_items:
        template = work.toolbox.prepare(work.context, work.facts)
        batch = work.toolbox.build_translation_request(template)
        assert batch is not None
        translations.update(
            {unit.unit_id: _cover_translation(unit.source_text) for unit in batch.units}
        )
    return translations


def _work_items(source: Path, run_id: str) -> tuple[ToolboxPageWork, ...]:
    pages = DocumentCoordinator(PageFactsExtractor()).enumerate_pages(_request(source, run_id))
    return tuple(
        ToolboxPageWork(
            page.context,
            page.facts,
            _cover_toolbox(source),
        )
        for page in pages
    )


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


def test_tbm1_cover_lifted_core_produces_replayable_real_candidate(
    tmp_path: Path,
) -> None:
    """The lifted cover core must emit a bound declarative patch for a real page."""

    default_hash_before = _sha256_file(DEFAULT_CATALOG)
    work = _work_items(COVER_SOURCE, "tbm1-cover-real")[0]
    translations = _fixed_translations((work,))

    result = ToolboxPageCoordinator(FixedTranslationAdapter(translations)).execute(work)

    assert result.patch is not None
    assert result.patch.owner == "cover"
    assert len(result.patch.operations) == 5
    assert not result.findings
    protected_ids = {item.object_id for item in work.facts.objects if item.protected}
    for operation in result.patch.operations:
        assert operation.owner == "cover"
        assert operation.target_object_ids
        assert not protected_ids.intersection(operation.target_object_ids)
        assert operation.redaction_rects

    candidate = tmp_path / "cover-candidate.pdf"
    with pymupdf.open(COVER_SOURCE) as document:
        application = PagePatchInterpreter(_fonts()).apply(
            document,
            work.context,
            work.facts,
            result.patch,
            "cover",
        )
        assert application.fits
        assert application.applied_count == len(result.patch.operations)
        document.save(candidate)
    with pymupdf.open(candidate) as document:
        assert document.page_count == 1
        assert "度报告" in document[0].get_text()
    assert _sha256_file(DEFAULT_CATALOG) == default_hash_before


def test_tbm1_cover_failure_keeps_machine_finding_and_viewable_diagnostic(
    tmp_path: Path,
) -> None:
    """An unfit translation must fall back with an honest failure diagnostic."""

    work = _work_items(COVER_SOURCE, "tbm1-cover-diagnostic")[0]
    template = work.toolbox.prepare(work.context, work.facts)
    batch = work.toolbox.build_translation_request(template)
    assert batch is not None
    translations = {
        unit.unit_id: (
            "用于验证封面布局失败的超长译文" * 80
            + " "
            + " ".join(extract_required_literals(unit.source_text))
        ).strip()
        for unit in batch.units
    }

    result = ToolboxPageCoordinator(FixedTranslationAdapter(translations)).execute(work)

    assert result.patch is None
    assert result.proposed_patch is not None
    assert result.outcome.fallback is Fallback.PAGE_PASSTHROUGH
    assert "COVER_TEXT_OVERFLOW" in {finding.code for finding in result.findings}

    with pymupdf.open(COVER_SOURCE) as materialization_probe:
        application = PagePatchInterpreter(_fonts()).apply(
            materialization_probe,
            work.context,
            work.facts,
            result.proposed_patch,
            "cover",
        )
        assert not application.fits

    diagnostic = tmp_path / "cover-layout-failure-diagnostic.pdf"
    with pymupdf.open(COVER_SOURCE) as document:
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
            "TBM1 FAILURE DIAGNOSTIC: COVER_TEXT_OVERFLOW",
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
        assert document.page_count == 1
        assert document.metadata.get("subject") == "FAILURE DIAGNOSTIC - NOT A TRANSLATED CANDIDATE"


def test_tbm1_cover_run_private_catalog_enable_and_disable_are_deterministic(
    tmp_path: Path,
) -> None:
    """Only a run-private overlay may enable cover; the default remains disabled."""

    default_bytes = DEFAULT_CATALOG.read_bytes()
    payload = json.loads(default_bytes.decode("utf-8"))
    cover_entry = next(item for item in payload["entries"] if item["route"] == "cover")
    cover_entry.update(
        {
            "enabled": True,
            "evidence_state": "PASS_ENABLE",
            "evidence_attestation_hash": "b" * 64,
            "disabled_reason": None,
        }
    )
    overlay = tmp_path / "page_toolbox_catalog_tbm1_cover.json"
    overlay.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    factories = build_p8_toolbox_factories(
        POLICY_PATH,
        FONT_MANIFEST,
        REPO_ROOT,
    )
    factories["cover"] = lambda: _cover_toolbox(COVER_SOURCE)

    enabled_catalog = load_toolbox_catalog(overlay, factories)
    assert enabled_catalog.validate_startup().ready
    enabled = enabled_catalog.resolve_enabled("cover", 1)
    assert enabled.toolbox is not None
    assert enabled.toolbox.descriptor.route == "cover"
    assert enabled.toolbox.descriptor.toolbox_id == "cover"
    assert enabled.finding is None

    disabled_catalog = load_toolbox_catalog(
        DEFAULT_CATALOG,
        build_p8_toolbox_factories(
            POLICY_PATH,
            FONT_MANIFEST,
            REPO_ROOT,
        ),
    )
    disabled = disabled_catalog.resolve_enabled("cover", 1)
    assert disabled.toolbox is None
    assert disabled.finding is not None
    assert disabled.finding.code == "TOOLBOX_DISABLED"
    assert disabled.outcome is not None
    assert _sha256_file(DEFAULT_CATALOG) == hashlib.sha256(default_bytes).hexdigest()


def test_tbm1_cover_shared_page_concurrency_is_equivalent(tmp_path: Path) -> None:
    """Fresh factory instances must yield identical results at concurrency 1 and 2."""

    two_page_source = tmp_path / "two-cover-pages.pdf"
    with pymupdf.open() as target, pymupdf.open(COVER_SOURCE) as source:
        target.insert_pdf(source)
        target.insert_pdf(source)
        target.save(two_page_source)

    sequential_work = _work_items(two_page_source, "tbm1-cover-concurrency")
    parallel_work = _work_items(two_page_source, "tbm1-cover-concurrency")
    translations = _fixed_translations(sequential_work)
    translations.update(_fixed_translations(parallel_work))
    sequential = ToolboxPageCoordinator(FixedTranslationAdapter(translations)).execute_many(
        sequential_work, 1
    )
    parallel = ToolboxPageCoordinator(FixedTranslationAdapter(translations)).execute_many(
        parallel_work, 2
    )

    assert tuple(item.page_no for item in sequential) == (1, 2)
    assert tuple(item.page_no for item in parallel) == (1, 2)
    assert tuple(map(_execution_identity, sequential)) == tuple(map(_execution_identity, parallel))
    assert all(item.patch is not None for item in parallel)
    assert all(item.patch.owner == "cover" for item in parallel if item.patch)


def test_tbm1_cover_offset_cropbox_is_an_explicit_shared_kernel_regression() -> None:
    """Keep the real non-zero CropBox case visible until shared replay is corrected."""

    facts = PageFactsExtractor().extract_page(
        OFFSET_CROPBOX_SOURCE,
        _sha256_file(OFFSET_CROPBOX_SOURCE),
        1,
    )

    assert facts.crop_box[0] > 0
    assert facts.text_spans
    assert all(item.bbox[0] < facts.crop_box[0] for item in facts.text_spans)
