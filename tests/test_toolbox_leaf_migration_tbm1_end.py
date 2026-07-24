"""Verify the TBM1 end-leaf wrapper and its explicit contract blocker."""

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
from transflow.toolboxes.leaves.end import EndToolbox
from transflow.toolboxes.leaves.end.template import build_end_template
from transflow.toolboxes.leaves.factory import build_p8_toolbox_factories
from transflow.toolboxes.leaves.lifted_contracts import lift_page_facts
from transflow.toolboxes.leaves.policy import load_p8_toolbox_policy

REPO_ROOT = Path(__file__).resolve().parent.parent
CLASSIFICATION_ROOT = REPO_ROOT / "spikes/page_classification_engine_puncture_v1"
END_SAMPLE_ROOT = next(CLASSIFICATION_ROOT.glob("*/end"))
END_SOURCE = END_SAMPLE_ROOT / "S2P0120.pdf"
END_ZERO_TEXT_SOURCE = END_SAMPLE_ROOT / "S2P0580.pdf"
END_BILINGUAL_CONTACT_SOURCE = END_SAMPLE_ROOT / "S2P0040.pdf"
DEFAULT_CATALOG = REPO_ROOT / "resources/catalogs/page_toolbox_catalog_v4.json"
FONT_MANIFEST = REPO_ROOT / "resources/manifests/font_manifest.json"
POLICY_PATH = REPO_ROOT / "resources/manifests/p8_toolbox_policy.json"
FIXED_END_TRANSLATION = "电话：(852) 3406 5688\n香港中环康乐广场 8 号交易广场二期 2801-2803 室"


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


def _end_work_items(
    source: Path,
    run_id: str,
) -> tuple[ToolboxPageWork, ...]:
    policy = load_p8_toolbox_policy(POLICY_PATH)
    font_path = _fonts().resolve(policy.font_id).path
    pages = DocumentCoordinator(PageFactsExtractor()).enumerate_pages(_request(source, run_id))
    return tuple(
        ToolboxPageWork(
            page.context,
            page.facts,
            EndToolbox(policy, font_path),
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
        translations.update({unit.unit_id: FIXED_END_TRANSLATION for unit in batch.units})
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


def test_tbm1_end_lifted_core_produces_bound_real_candidate(
    tmp_path: Path,
) -> None:
    """The end core must translate the contact block and preserve its URL."""

    default_hash_before = _sha256_file(DEFAULT_CATALOG)
    work = _end_work_items(END_SOURCE, "tbm1-end-real")[0]
    policy = load_p8_toolbox_policy(POLICY_PATH)
    core_template = build_end_template(
        lift_page_facts(work.facts),
        policy.source_language,
        policy.target_language,
    )
    translations = _fixed_translations((work,))

    result = ToolboxPageCoordinator(FixedTranslationAdapter(translations)).execute(work)

    assert result.patch is not None
    assert result.patch.owner == "end"
    assert len(result.patch.operations) == 1
    assert not result.findings
    protected_ids = set(core_template.protected_object_ids)
    operation = result.patch.operations[0]
    assert operation.replacement_text == FIXED_END_TRANSLATION
    assert not protected_ids.intersection(operation.target_object_ids)

    candidate = tmp_path / "end-candidate.pdf"
    with pymupdf.open(END_SOURCE) as document:
        application = PagePatchInterpreter(_fonts()).apply(
            document,
            work.context,
            work.facts,
            result.patch,
            "end",
        )
        assert application.fits
        assert application.applied_count == 1
        document.save(candidate)
    with pymupdf.open(candidate) as document:
        text = document[0].get_text().replace("\xa0", " ")
        assert document.page_count == 1
        assert "www.csopasset.com" in text
        assert "Telephone" not in text
        assert "3406 5688" in text
    assert _sha256_file(DEFAULT_CATALOG) == default_hash_before


def test_tbm1_end_failure_keeps_machine_finding_and_viewable_diagnostic(
    tmp_path: Path,
) -> None:
    """An unfit end translation must fall back without posing as a candidate."""

    work = _end_work_items(END_SOURCE, "tbm1-end-diagnostic")[0]
    template = work.toolbox.prepare(work.context, work.facts)
    batch = work.toolbox.build_translation_request(template)
    assert batch is not None
    translations = {
        unit.unit_id: (
            "用于验证结束页布局失败的超长译文" * 100
            + " "
            + " ".join(extract_required_literals(unit.source_text))
        ).strip()
        for unit in batch.units
    }

    result = ToolboxPageCoordinator(FixedTranslationAdapter(translations)).execute(work)

    assert result.patch is None
    assert result.proposed_patch is not None
    assert result.outcome.fallback is Fallback.PAGE_PASSTHROUGH
    assert "END_TEXT_OVERFLOW" in {finding.code for finding in result.findings}

    with pymupdf.open(END_SOURCE) as materialization_probe:
        application = PagePatchInterpreter(_fonts()).apply(
            materialization_probe,
            work.context,
            work.facts,
            result.proposed_patch,
            "end",
        )
        assert not application.fits

    diagnostic = tmp_path / "end-layout-failure-diagnostic.pdf"
    with pymupdf.open(END_SOURCE) as document:
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
            "TBM1 FAILURE DIAGNOSTIC: END_TEXT_OVERFLOW",
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


def test_tbm1_end_zero_text_page_is_provider_free_passthrough() -> None:
    """A native-text-free end page must not fabricate a translation request."""

    work = _end_work_items(END_ZERO_TEXT_SOURCE, "tbm1-end-zero-text")[0]

    result = ToolboxPageCoordinator(FixedTranslationAdapter({})).execute(work)

    assert result.patch is None
    assert result.proposed_patch is None
    assert result.ordered_unit_ids == ()
    assert result.outcome.fallback is Fallback.PAGE_PASSTHROUGH
    assert not result.findings
    assert result.completeness_decision is not None
    assert not result.completeness_decision.errors


def test_tbm1_end_bilingual_contact_pre_authorization_gap_is_explicit() -> None:
    """Do not silently approve leaf-private KEEP_SOURCE after inventory freeze."""

    work = _end_work_items(
        END_BILINGUAL_CONTACT_SOURCE,
        "tbm1-end-bilingual-contact",
    )[0]

    result = ToolboxPageCoordinator(FixedTranslationAdapter({})).execute(work)

    assert result.patch is None
    assert result.proposed_patch is None
    assert result.outcome.fallback is Fallback.PAGE_PASSTHROUGH
    assert result.outcome.finding_codes == ("ROUTE_CAPABILITY_MISMATCH",)
    assert result.completeness_decision is not None
    assert {error.code.value for error in result.completeness_decision.errors} == {
        "UNRESOLVED_UNIT"
    }


def test_tbm1_end_run_private_catalog_enable_and_disable_are_deterministic(
    tmp_path: Path,
) -> None:
    """Only a run-private overlay may resolve the migrated end factory."""

    default_bytes = DEFAULT_CATALOG.read_bytes()
    payload = json.loads(default_bytes.decode("utf-8"))
    entry = next(item for item in payload["entries"] if item["route"] == "end")
    entry.update(
        {
            "enabled": True,
            "evidence_state": "PASS_ENABLE",
            "evidence_attestation_hash": "d" * 64,
            "disabled_reason": None,
        }
    )
    overlay = tmp_path / "page_toolbox_catalog_tbm1_end.json"
    overlay.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    factories = build_p8_toolbox_factories(
        POLICY_PATH,
        FONT_MANIFEST,
        REPO_ROOT,
    )
    policy = load_p8_toolbox_policy(POLICY_PATH)
    font_path = _fonts().resolve(policy.font_id).path
    factories["end"] = lambda: EndToolbox(policy, font_path)

    enabled_catalog = load_toolbox_catalog(overlay, factories)
    assert enabled_catalog.validate_startup().ready
    enabled = enabled_catalog.resolve_enabled("end", 1)
    assert enabled.toolbox is not None
    assert enabled.toolbox.descriptor.route == "end"
    assert enabled.toolbox.descriptor.toolbox_id == "end"
    assert enabled.finding is None

    disabled_catalog = load_toolbox_catalog(
        DEFAULT_CATALOG,
        build_p8_toolbox_factories(
            POLICY_PATH,
            FONT_MANIFEST,
            REPO_ROOT,
        ),
    )
    disabled = disabled_catalog.resolve_enabled("end", 1)
    assert disabled.toolbox is None
    assert disabled.finding is not None
    assert disabled.finding.code == "TOOLBOX_DISABLED"
    assert disabled.outcome is not None
    assert _sha256_file(DEFAULT_CATALOG) == hashlib.sha256(default_bytes).hexdigest()


def test_tbm1_end_shared_page_concurrency_is_equivalent(
    tmp_path: Path,
) -> None:
    """Fresh end instances must be stable at shared concurrency 1 and 2."""

    two_page_source = tmp_path / "two-end-pages.pdf"
    with pymupdf.open() as target, pymupdf.open(END_SOURCE) as source:
        target.insert_pdf(source)
        target.insert_pdf(source)
        target.save(two_page_source)

    sequential_work = _end_work_items(
        two_page_source,
        "tbm1-end-concurrency",
    )
    parallel_work = _end_work_items(
        two_page_source,
        "tbm1-end-concurrency",
    )
    translations = _fixed_translations(sequential_work)
    translations.update(_fixed_translations(parallel_work))
    coordinator = ToolboxPageCoordinator(FixedTranslationAdapter(translations))

    sequential = coordinator.execute_many(sequential_work, 1)
    parallel = coordinator.execute_many(parallel_work, 2)

    assert tuple(item.page_no for item in sequential) == (1, 2)
    assert tuple(item.page_no for item in parallel) == (1, 2)
    assert tuple(map(_execution_identity, sequential)) == tuple(map(_execution_identity, parallel))
    assert all(item.patch is not None for item in parallel)
    assert all(item.patch.owner == "end" for item in parallel if item.patch)
