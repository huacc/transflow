"""按 RV4-T01～T06 重新验收文字分母、语义映射与翻译完整性。"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pymupdf
import pytest

from tests.migration.p9_qwen_translation_adapter import MigrationQwenTranslationAdapter
from transflow.adapters.ai.fixed import FixedTranslationAdapter
from transflow.adapters.filesystem.artifact_store import SharedFilesystemArtifactAdapter
from transflow.application.contracts import EnumeratedPage
from transflow.application.document_coordinator import DocumentCoordinator
from transflow.application.translated_diagnostic import (
    DiagnosticPageInput,
    TranslatedDiagnosticMaterializer,
)
from transflow.application.translation_completeness import (
    TranslationCompletenessGate,
    adjudicate_translation_candidates,
    build_semantic_unit_map,
    extract_required_literals,
    validate_inventory_coverage,
)
from transflow.domain.completeness import (
    SEMANTIC_MAP_SCHEMA_V2,
    CompletenessDisposition,
    CompletenessErrorCode,
    CompletenessStatus,
    KeepSourceReason,
    SemanticUnit,
    SemanticUnitDisposition,
    SemanticUnitMap,
    TranslationCandidate,
)
from transflow.domain.delivery import DiagnosticStatus
from transflow.domain.errors import DomainContractError, ErrorCode, PortCallError
from transflow.domain.jobs import DocumentRunRequest
from transflow.domain.pages import PageOutcome
from transflow.domain.result_axes import ProductAcceptance, project_page_result
from transflow.domain.states import (
    ArtifactIntegrity,
    ArtifactProduced,
    Capability,
    Fallback,
    PagePipelineState,
    Quality,
    TranslationCoverage,
)
from transflow.domain.text_inventory import InventoryDisposition
from transflow.domain.translation import (
    TranslatedUnit,
    TranslationBatch,
    TranslationBundle,
    TranslationUnit,
)
from transflow.pdf_kernel import ControlledFontRegistry, PageFactsExtractor
from transflow.pdf_kernel.patch import PagePatchInterpreter
from transflow.pdf_kernel.text_inventory import freeze_page_text_inventory
from transflow.toolboxes.contracts import PageTemplate, TranslationDispatch
from transflow.toolboxes.leaves import SingleFlowTextToolbox
from transflow.toolboxes.leaves.policy import load_p8_toolbox_policy

REPO_ROOT = Path(__file__).resolve().parent.parent
P0101 = (
    REPO_ROOT
    / "runs"
    / "toolbox_leaf_migration"
    / "TM2"
    / "05-body-flow-text-single-20260721-133143"
    / "cases"
    / "04-short-p0101"
    / "input"
    / "source.pdf"
)
P0151 = (
    REPO_ROOT
    / "runs"
    / "critical_chain_revalidation"
    / "RV3"
    / "02-routing-catalog-20260722-012551"
    / "pages"
    / "p0151"
    / "input"
    / "source.pdf"
)
FONT_MANIFEST = REPO_ROOT / "resources" / "manifests" / "font_manifest.json"
P8_POLICY = REPO_ROOT / "resources" / "manifests" / "p8_toolbox_policy.json"
FONT_ID = "noto-sans-cjk-sc-regular"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _enumerate(path: Path, run_id: str, page_no: int = 1) -> EnumeratedPage:
    assert path.is_file(), path
    request = DocumentRunRequest(
        source_pdf_path=str(path.resolve()),
        source_hash=_sha256_file(path),
        source_language="en",
        target_language="zh-CN",
        config_snapshot_hash="a" * 64,
        job_id=f"job-{run_id}",
        run_id=run_id,
    )
    return DocumentCoordinator(PageFactsExtractor()).enumerate_pages(request)[page_no - 1]


def _single(
    path: Path,
    run_id: str,
) -> tuple[
    EnumeratedPage,
    SingleFlowTextToolbox,
    PageTemplate,
    TranslationBatch,
    SemanticUnitMap,
]:
    page = _enumerate(path, run_id)
    fonts = ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT)
    toolbox = SingleFlowTextToolbox(
        load_p8_toolbox_policy(P8_POLICY),
        fonts.resolve(FONT_ID).path,
    )
    template = toolbox.prepare(page.context, page.facts)
    batch = toolbox.build_translation_request(template)
    assert batch is not None
    semantic_map = build_semantic_unit_map(template, batch, page.facts)
    return page, toolbox, template, batch, semantic_map


def _valid_translations(
    semantic_map: SemanticUnitMap,
    *,
    footer_text: str = "公司治理报告",
) -> dict[str, str]:
    entries = {item.unit_id: item for item in semantic_map.entries}
    translations: dict[str, str] = {}
    for ordinal, unit_id in enumerate(semantic_map.translated_unit_ids, start=1):
        entry = entries[unit_id]
        prefix = (
            footer_text
            if "Corporate Governance Report" in entry.source_text
            else f"译文{ordinal}"
        )
        translations[unit_id] = " ".join((prefix, *entry.required_literals)).strip()
    return translations


def _entry(
    unit_id: str,
    ordinal: int,
    source_text: str,
    *,
    disposition: SemanticUnitDisposition = SemanticUnitDisposition.TRANSLATE,
    keep_source_reason: KeepSourceReason | None = None,
    disposition_reason: str | None = None,
) -> SemanticUnit:
    return SemanticUnit(
        unit_id=unit_id,
        object_id=f"object-{unit_id}",
        container_id=f"container-{unit_id}",
        owner="body.flow_text.single",
        ordinal=ordinal,
        source_text=source_text,
        source_hash=hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
        required_literals=extract_required_literals(source_text),
        disposition=disposition,
        keep_source_reason=keep_source_reason,
        source_object_ids=(f"object-{unit_id}",),
        disposition_reason=disposition_reason,
    )


def _map(*entries: SemanticUnit) -> SemanticUnitMap:
    return SemanticUnitMap(
        "rv4-contract-map",
        1,
        "b" * 64,
        tuple(entries),
        SEMANTIC_MAP_SCHEMA_V2,
    )


@pytest.mark.integration
def test_rv4_t01_p0101_footer_is_translated_and_page_number_is_authorized() -> None:
    """p0101 语义页脚进入翻译，纯页码单独预授权，三层身份双向闭合。"""

    page, _, _, batch, semantic_map = _single(P0101, "rv4-t01")
    inventory = freeze_page_text_inventory(page.facts)
    mapped_ids = tuple(
        object_id
        for entry in semantic_map.entries
        for object_id in entry.source_object_ids
    )
    assert set(mapped_ids) == {item.object_id for item in inventory.items}
    assert len(mapped_ids) == len(set(mapped_ids)) == len(inventory.items)
    footer = next(
        item
        for item in semantic_map.entries
        if item.source_text == "Corporate Governance Report"
    )
    page_number = next(item for item in semantic_map.entries if item.source_text == "99")
    assert footer.disposition is SemanticUnitDisposition.TRANSLATE
    assert footer.owner == "shared.margin.footer"
    assert page_number.disposition is SemanticUnitDisposition.KEEP_SOURCE
    assert page_number.keep_source_reason is KeepSourceReason.PAGE_NUMBER
    assert page_number.owner == "shared.margin.footer"

    gate = TranslationCompletenessGate(maximum_targeted_retries=0).execute(
        semantic_map,
        batch,
        FixedTranslationAdapter(_valid_translations(semantic_map)),
    )
    assert gate.decision.status is CompletenessStatus.PASS
    assert gate.bundle is not None
    assert gate.bundle.requested_unit_ids == batch.ordered_unit_ids
    translated = {item.unit_id: item.translated_text for item in gate.bundle.units}
    assert translated[footer.unit_id] == "公司治理报告"

    outcome = PageOutcome(
        1,
        PagePipelineState.FINALIZED,
        ArtifactProduced.YES,
        ArtifactIntegrity.PASS,
        TranslationCoverage.FULL,
        Capability.SUPPORTED,
        Quality.PASS,
        Fallback.NONE,
    )
    axes = project_page_result(
        "rv4-t01",
        outcome,
        final_available=True,
        completeness=gate.decision,
    )
    assert axes.product_acceptance is ProductAcceptance.NOT_EVALUATED
    assert "SOURCE_TEXT_PASSTHROUGH_PRESENT" not in axes.reasons


@pytest.mark.integration
def test_rv4_t02_p0151_body_and_table_text_have_exact_bidirectional_coverage() -> None:
    """p0151 的表内与表外文字全部进入 v2 map，且没有 unresolved unit。"""

    page = _enumerate(P0151, "rv4-t02")
    inventory = freeze_page_text_inventory(page.facts)
    text_by_id = {item.object_id: item for item in page.facts.text_spans if item.text.strip()}
    inventory_by_id = {item.object_id: item for item in inventory.items}
    currency_units = tuple(
        item.object_id for item in page.facts.text_spans if item.text.strip() == "$m"
    )
    assert currency_units
    assert all(
        inventory_by_id[object_id].disposition is InventoryDisposition.KEEP_SOURCE
        and inventory_by_id[object_id].keep_source_reason
        == KeepSourceReason.NUMERIC_OR_SYMBOLIC_LITERAL.value
        for object_id in currency_units
    )
    translate_ids = tuple(
        item.object_id
        for item in inventory.items
        if item.disposition.value == "TRANSLATE"
    )
    template = PageTemplate(
        "rv4-p0151-composite",
        page.context,
        page.facts.kernel_facts_hash,
        "body.composite.flow_text_table",
        translate_ids,
    )
    batch = TranslationBatch(
        "rv4-p0151-composite-batch",
        "en",
        "zh-CN",
        tuple(
            TranslationUnit(
                hashlib.sha256(f"{page.facts.page_identity}\0{object_id}".encode()).hexdigest(),
                page.context.page_no,
                ordinal,
                text_by_id[object_id].text,
                f"rv4-p0151-r{ordinal:04d}",
            )
            for ordinal, object_id in enumerate(translate_ids)
        ),
    )
    semantic_map = build_semantic_unit_map(template, batch, page.facts, inventory)
    validate_inventory_coverage(inventory, semantic_map, page.facts)
    assert semantic_map.schema_version == SEMANTIC_MAP_SCHEMA_V2
    assert not semantic_map.unresolved_unit_ids
    assert not semantic_map.unsupported_unit_ids

    table_boxes = tuple(item.bbox for item in page.facts.table_objects)
    inside = {
        span.object_id
        for span in page.facts.text_spans
        if any(
            box[0] <= (span.bbox[0] + span.bbox[2]) / 2 <= box[2]
            and box[1] <= (span.bbox[1] + span.bbox[3]) / 2 <= box[3]
            for box in table_boxes
        )
    }
    outside = set(text_by_id) - inside
    mapped = {
        object_id
        for entry in semantic_map.entries
        for object_id in entry.source_object_ids
    }
    assert table_boxes and inside and outside
    assert inside <= mapped
    assert outside <= mapped
    assert mapped == {item.object_id for item in inventory.items}


@pytest.mark.parametrize(
    "candidates,expected_codes",
    (
        ((), {CompletenessErrorCode.MISSING_UNIT}),
        (
            (
                TranslationCandidate("unit-1", "有效译文"),
                TranslationCandidate("unit-1", "重复译文"),
            ),
            {CompletenessErrorCode.DUPLICATE_UNIT},
        ),
        (
            (
                TranslationCandidate("unit-1", "有效译文"),
                TranslationCandidate("unit-extra", "新增译文"),
            ),
            {CompletenessErrorCode.EXTRA_UNIT},
        ),
        (
            (TranslationCandidate("unit-wrong", "错配译文"),),
            {CompletenessErrorCode.EXTRA_UNIT, CompletenessErrorCode.MISSING_UNIT},
        ),
    ),
)
def test_rv4_t03_bad_unit_id_sets_are_rejected_before_layout(
    candidates: tuple[TranslationCandidate, ...],
    expected_codes: set[CompletenessErrorCode],
) -> None:
    """删除、重复、新增和错配 ID 都只能得到 FAIL，不能形成 Bundle。"""

    semantic_map = _map(_entry("unit-1", 0, "Annual report"))
    decision = adjudicate_translation_candidates(semantic_map, candidates)
    assert decision.status is CompletenessStatus.FAIL
    assert expected_codes <= {item.code for item in decision.errors}

    batch = TranslationBatch(
        "rv4-bad-id-batch",
        "en",
        "zh-CN",
        (TranslationUnit("unit-1", 1, 0, "Annual report", "region-1"),),
    )
    with pytest.raises(DomainContractError):
        TranslationBundle.from_batch(batch, ())
    with pytest.raises(DomainContractError):
        TranslationBundle.from_batch(
            batch,
            (
                TranslatedUnit("unit-1", "译文"),
                TranslatedUnit("unit-extra", "新增"),
            ),
        )


@pytest.mark.parametrize(
    "translated_text,expected_code",
    (
        ("", CompletenessErrorCode.EMPTY_TRANSLATION),
        ("[placeholder]", CompletenessErrorCode.PLACEHOLDER),
        ("ERROR: timeout", CompletenessErrorCode.ERROR_ECHO),
        ("Revenue was 12.5% in FY2025.", CompletenessErrorCode.UNJUSTIFIED_SOURCE_COPY),
        ("收入增长。", CompletenessErrorCode.REQUIRED_LITERAL_BROKEN),
        (
            "Revenue was 12.5% in FY2025. Updated",
            CompletenessErrorCode.SOURCE_LANGUAGE_RESIDUAL,
        ),
    ),
)
def test_rv4_t04_invalid_translation_content_is_rejected(
    translated_text: str,
    expected_code: CompletenessErrorCode,
) -> None:
    """空串、占位、异常、照抄、丢字面量与源语言残留都必须失败。"""

    semantic_map = _map(_entry("unit-1", 0, "Revenue was 12.5% in FY2025."))
    decision = adjudicate_translation_candidates(
        semantic_map,
        (TranslationCandidate("unit-1", translated_text),),
    )
    assert decision.status is CompletenessStatus.FAIL
    assert expected_code in {item.code for item in decision.errors}


@pytest.mark.integration
def test_rv4_t05_complete_translation_can_be_saved_only_as_diagnostic(
    tmp_path: Path,
) -> None:
    """完整译文在布局验收失败时可留诊断 PDF，但不能形成或冒充 final。"""

    source = tmp_path / "rv4-t05-source.pdf"
    with pymupdf.open() as document:
        pdf_page = document.new_page(width=420, height=600)
        pdf_page.insert_textbox(
            pymupdf.Rect(60, 120, 360, 220),
            "Revenue increased 10%",
            fontname="helv",
            fontsize=12,
        )
        document.save(source)
    page, toolbox, template, batch, semantic_map = _single(source, "rv4-t05")
    gate = TranslationCompletenessGate(maximum_targeted_retries=0).execute(
        semantic_map,
        batch,
        FixedTranslationAdapter(
            {semantic_map.translated_unit_ids[0]: "收入增长 10%"}
        ),
    )
    assert gate.bundle is not None
    plan = toolbox.consume_translation_bundle(
        template,
        TranslationDispatch(batch=batch, bundle=gate.bundle),
    )
    artifacts = SharedFilesystemArtifactAdapter(tmp_path, "rv4-t05")
    diagnostic = TranslatedDiagnosticMaterializer(
        PagePatchInterpreter(ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT)),
        artifacts,
        tmp_path,
    ).materialize_page(
        source,
        DiagnosticPageInput(
            page.context,
            page.facts,
            plan.patch,
            semantic_map,
            gate.bundle,
            gate.decision,
        ),
    )
    assert diagnostic.status is DiagnosticStatus.TRANSLATED_DIAGNOSTIC_READY
    assert diagnostic.artifact is not None
    assert diagnostic.artifact.label == "diagnostic"
    assert diagnostic.artifact.relative_path is not None
    assert (tmp_path / diagnostic.artifact.relative_path).is_file()

    failed_layout = PageOutcome(
        1,
        PagePipelineState.FINALIZED,
        ArtifactProduced.NO,
        ArtifactIntegrity.FAIL,
        TranslationCoverage.FULL,
        Capability.SUPPORTED,
        Quality.FAIL,
        Fallback.PAGE_PASSTHROUGH,
        ("TEXT_LAYOUT_OVERFLOW",),
    )
    axes = project_page_result(
        "rv4-t05",
        failed_layout,
        final_available=False,
        completeness=gate.decision,
        diagnostic=diagnostic,
    )
    assert axes.product_acceptance is ProductAcceptance.FAIL
    assert not tuple(tmp_path.rglob("final*.pdf"))


def test_rv4_t06_source_passthrough_is_never_product_pass() -> None:
    """即使源 PDF 技术上可交付，目标页无译文透传仍必须 Product FAIL。"""

    semantic_map = _map(_entry("unit-1", 0, "Annual report"))
    complete = adjudicate_translation_candidates(
        semantic_map,
        (TranslationCandidate("unit-1", "年度报告"),),
    )
    passthrough = PageOutcome(
        1,
        PagePipelineState.FINALIZED,
        ArtifactProduced.YES,
        ArtifactIntegrity.PASS,
        TranslationCoverage.NONE,
        Capability.SUPPORTED,
        Quality.PASS,
        Fallback.PAGE_PASSTHROUGH,
        ("SOURCE_PASSTHROUGH",),
    )
    axes = project_page_result(
        "rv4-t06",
        passthrough,
        final_available=True,
        completeness=complete,
        p14_evaluated=True,
        p14_threshold_passed=True,
    )
    assert axes.product_acceptance is ProductAcceptance.FAIL
    assert {
        "TRANSLATION_COVERAGE_NOT_FULL",
        "FALLBACK_PRESENT",
    } <= set(axes.reasons)


def test_rv4_unsupported_blocks_provider_while_protected_remains_auditable() -> None:
    """PROTECTED 保留对象证据；UNSUPPORTED 以能力原因阻断且不调用 Provider。"""

    protected = _entry(
        "protected",
        0,
        "Registered logo text",
        disposition=SemanticUnitDisposition.PROTECTED,
        disposition_reason="LOCKED_LOGO_OBJECT",
    )
    unsupported = _entry(
        "unsupported",
        1,
        "Text embedded in unsupported object",
        disposition=SemanticUnitDisposition.UNSUPPORTED,
        disposition_reason="OCR_BACKFILL_NOT_AVAILABLE",
    )
    semantic_map = _map(protected, unsupported)

    class CountingPort:
        def __init__(self) -> None:
            self.calls = 0

        def translate(self, batch: TranslationBatch) -> TranslationBundle:
            self.calls += 1
            return TranslationBundle.from_batch(batch, ())

    port = CountingPort()
    gate = TranslationCompletenessGate(maximum_targeted_retries=0).execute(
        semantic_map,
        None,
        port,
    )
    assert port.calls == 0
    assert gate.bundle is None
    assert gate.decision.status is CompletenessStatus.FAIL
    assert CompletenessErrorCode.UNSUPPORTED_UNIT in {
        item.code for item in gate.decision.errors
    }
    assert next(
        item for item in gate.decision.dispositions if item.unit_id == "protected"
    ).disposition is CompletenessDisposition.PROTECTED


def test_rv4_v2_coverage_rejects_duplicate_or_missing_source_object_ids() -> None:
    """v2 source_object_ids 是文字分母身份，重复和漏项都不能通过覆盖门禁。"""

    page = _enumerate(P0101, "rv4-coverage-fault")
    inventory = freeze_page_text_inventory(page.facts)
    _, _, _, _, semantic_map = _single(P0101, "rv4-coverage-map")
    first = next(item for item in semantic_map.entries if len(item.source_object_ids) > 1)
    damaged = replace(
        semantic_map,
        entries=tuple(
            replace(item, source_object_ids=item.source_object_ids[:-1])
            if item.unit_id == first.unit_id
            else item
            for item in semantic_map.entries
        ),
    )
    with pytest.raises(DomainContractError):
        validate_inventory_coverage(inventory, damaged, page.facts)


def test_rv4_qwen_invalid_multi_unit_response_is_bisected_without_id_drift() -> None:
    """模型合并多 ID 时只缩小当前分片，单 unit 身份和最终顺序保持不变。"""

    class ProbeAdapter(MigrationQwenTranslationAdapter):
        def __init__(self) -> None:
            self.seen: list[tuple[str, ...]] = []

        def _translate_chunk(
            self,
            batch: TranslationBatch,
            units: tuple[TranslationUnit, ...],
        ) -> dict[str, str]:
            del batch
            ids = tuple(item.unit_id for item in units)
            self.seen.append(ids)
            if len(units) > 1:
                raise PortCallError(
                    ErrorCode.AI_RESPONSE_INVALID,
                    False,
                    "probe_multi_id_merge",
                )
            return {units[0].unit_id: f"译文-{units[0].unit_id}"}

    batch = TranslationBatch(
        "rv4-qwen-bisect",
        "en",
        "zh-CN",
        tuple(
            TranslationUnit(f"unit-{index}", 1, index, f"Source {index}", f"r-{index}")
            for index in range(4)
        ),
    )
    adapter = ProbeAdapter()
    translated = adapter._translate_chunk_resilient(batch, batch.units)
    assert tuple(translated) == batch.ordered_unit_ids
    assert adapter.seen == [
        ("unit-0", "unit-1", "unit-2", "unit-3"),
        ("unit-0", "unit-1"),
        ("unit-0",),
        ("unit-1",),
        ("unit-2", "unit-3"),
        ("unit-2",),
        ("unit-3",),
    ]
