"""Verify the conservative TBM2 composite and freeform production puncture."""

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
from transflow.domain.toolbox import Finding
from transflow.pdf_kernel import (
    ControlledFontRegistry,
    PageFactsExtractor,
    PagePatchInterpreter,
)
from transflow.toolboxes.catalog import load_toolbox_catalog
from transflow.toolboxes.composites import (
    FREEFORM_COMPONENT_ALLOWLIST,
    FlowTextChartToolbox,
    FlowTextDiagramToolbox,
    FreeformToolbox,
    build_tbm2_toolbox_factories,
)
from transflow.toolboxes.leaves.factory import build_p8_toolbox_factories
from transflow.toolboxes.leaves.policy import load_p8_toolbox_policy

REPO_ROOT = Path(__file__).resolve().parent.parent
CLASSIFICATION_ROOT = REPO_ROOT / "spikes/page_classification_engine_puncture_v1"
FLOW_CHART_SOURCE = next(
    CLASSIFICATION_ROOT.glob(
        "*/body/composite/flow_text_chart/EN_01_00050_p0077.pdf"
    )
)
FLOW_DIAGRAM_SOURCE = next(
    CLASSIFICATION_ROOT.glob(
        "*/body/composite/flow_text_diagram/EN_01_03988_p0101.pdf"
    )
)
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
        config_snapshot_hash="b" * 64,
        job_id=f"job-{run_id}",
        run_id=run_id,
    )


def _pages(source: Path, run_id: str):
    return DocumentCoordinator(PageFactsExtractor()).enumerate_pages(
        _request(source, run_id)
    )


def _fonts() -> ControlledFontRegistry:
    return ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT)


def _policy():
    return load_p8_toolbox_policy(POLICY_PATH)


def _font_path() -> Path:
    policy = _policy()
    return _fonts().resolve(policy.font_id).path


def _flow_chart_toolbox() -> FlowTextChartToolbox:
    return FlowTextChartToolbox(_policy(), _font_path())


def _flow_diagram_toolbox(source: Path) -> FlowTextDiagramToolbox:
    return FlowTextDiagramToolbox(_policy(), _font_path(), source)


def _freeform_toolbox(source: Path) -> FreeformToolbox:
    return FreeformToolbox(_policy(), _font_path(), source)


def _work(source: Path, run_id: str, toolbox) -> ToolboxPageWork:
    page = _pages(source, run_id)[0]
    return ToolboxPageWork(page.context, page.facts, toolbox)


def _translations(work: ToolboxPageWork, *, long: bool = False) -> dict[str, str]:
    template = work.toolbox.prepare(work.context, work.facts)
    batch = work.toolbox.build_translation_request(template)
    assert batch is not None
    translations = {}
    for unit in batch.units:
        required = " ".join(extract_required_literals(unit.source_text))
        body = "用于验证组合根失败收敛的超长中文内容" * 100 if long else "中文"
        translations[unit.unit_id] = f"{body} {required}".strip()
    return translations


class _CountingAdapter:
    def __init__(self, translations: dict[str, str]) -> None:
        self._delegate = FixedTranslationAdapter(translations)
        self.calls = 0

    def translate(self, batch):
        self.calls += 1
        return self._delegate.translate(batch)


def _execution_identity(result) -> tuple[object, ...]:
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


def test_tbm2_flow_text_chart_owns_one_root_request_and_replayable_patch() -> None:
    default_hash = _sha256_file(DEFAULT_CATALOG)
    work = _work(FLOW_CHART_SOURCE, "tbm2-flow-chart", _flow_chart_toolbox())
    translations = _translations(work)
    adapter = _CountingAdapter(translations)

    result = ToolboxPageCoordinator(adapter).execute(work)
    audit = work.toolbox.ownership_audit()

    assert adapter.calls == 1
    assert {item.component for item in audit} >= {"flow", "chart"}
    assert result.patch is not None
    assert result.patch.owner == "body.composite.flow_text_chart"
    assert all(item.owner == result.patch.owner for item in result.patch.operations)
    target_ids = [
        object_id
        for operation in result.patch.operations
        for object_id in operation.target_object_ids
    ]
    assert len(target_ids) == len(set(target_ids))
    with pymupdf.open(FLOW_CHART_SOURCE) as document:
        application = PagePatchInterpreter(_fonts()).apply(
            document,
            work.context,
            work.facts,
            result.patch,
            result.patch.owner,
        )
        assert application.fits
    assert _sha256_file(DEFAULT_CATALOG) == default_hash


def test_tbm2_flow_text_diagram_owns_one_root_request_and_replayable_patch() -> None:
    work = _work(
        FLOW_DIAGRAM_SOURCE,
        "tbm2-flow-diagram",
        _flow_diagram_toolbox(FLOW_DIAGRAM_SOURCE),
    )
    adapter = _CountingAdapter(_translations(work))

    result = ToolboxPageCoordinator(adapter).execute(work)
    audit = work.toolbox.ownership_audit()

    assert adapter.calls == 1
    assert {item.component for item in audit} >= {"flow", "diagram"}
    assert result.patch is not None
    assert result.patch.owner == "body.composite.flow_text_diagram"
    assert all(item.owner == result.patch.owner for item in result.patch.operations)
    with pymupdf.open(FLOW_DIAGRAM_SOURCE) as document:
        application = PagePatchInterpreter(_fonts()).apply(
            document,
            work.context,
            work.facts,
            result.patch,
            result.patch.owner,
        )
        assert application.fits


def test_tbm2_flow_text_diagram_preserves_parallel_flow_lanes() -> None:
    work = _work(
        FLOW_DIAGRAM_SOURCE,
        "tbm2-flow-diagram-lanes",
        _flow_diagram_toolbox(FLOW_DIAGRAM_SOURCE),
    )
    result = ToolboxPageCoordinator(
        FixedTranslationAdapter(_translations(work))
    ).execute(work)

    assert result.patch is not None
    component_by_object = {
        item.object_id: item.component
        for item in work.toolbox.ownership_audit()
    }
    bbox_by_id = {
        item.object_id: item.bbox for item in work.facts.text_spans
    }
    top_flow_operations = tuple(
        operation
        for operation in result.patch.operations
        if component_by_object[operation.target_object_ids[0]] == "flow"
        and min(bbox_by_id[item][1] for item in operation.target_object_ids) < 150
    )

    assert len(top_flow_operations) == 2
    left, right = sorted(top_flow_operations, key=lambda item: item.rect[0])
    assert left.rect is not None and right.rect is not None
    assert abs(left.rect[1] - right.rect[1]) <= 4.0
    assert left.rect[2] <= right.rect[0] + 1.0


def test_tbm2_flow_text_diagram_honours_leaf_hard_judge(
    monkeypatch,
) -> None:
    def hard_judge(*_args, **_kwargs):
        return (
            Finding(
                "tbm2-diagram-hard-judge",
                "DIAGRAM_FLOW_TEXT_COLLISION",
                "HARD",
                ("diagram-local-chain",),
            ),
        )

    monkeypatch.setattr(
        "transflow.toolboxes.composites.toolbox.judge_diagram_plan",
        hard_judge,
    )
    work = _work(
        FLOW_DIAGRAM_SOURCE,
        "tbm2-flow-diagram-hard-judge",
        _flow_diagram_toolbox(FLOW_DIAGRAM_SOURCE),
    )

    result = ToolboxPageCoordinator(
        FixedTranslationAdapter(_translations(work))
    ).execute(work)

    assert result.patch is None
    assert result.proposed_patch is not None
    assert result.outcome.fallback is Fallback.PAGE_PASSTHROUGH
    assert any(
        item.code == "DIAGRAM_FLOW_TEXT_COLLISION"
        for item in result.findings
    )


def test_tbm2_disjoint_diagram_local_labels_are_retained() -> None:
    work = _work(
        FLOW_DIAGRAM_SOURCE,
        "tbm2-flow-diagram-disjoint-label",
        _flow_diagram_toolbox(FLOW_DIAGRAM_SOURCE),
    )
    result = ToolboxPageCoordinator(
        FixedTranslationAdapter(_translations(work))
    ).execute(work)

    disjoint_ids = {
        item.object_id
        for item in work.facts.text_spans
        if item.text
        in {
            "Comprehensive Risk",
            "Management Committee",
            "Internal Control and ",
            "Compliance Committee",
        }
        and 460.0 <= item.bbox[1] <= 502.0
    }
    component_by_id = {
        item.object_id: item.component
        for item in work.toolbox.ownership_audit()
    }
    patched_ids = {
        object_id
        for operation in result.patch.operations
        for object_id in operation.target_object_ids
    }

    assert len(disjoint_ids) == 4
    assert {component_by_id[item] for item in disjoint_ids} == {"retained"}
    assert disjoint_ids.isdisjoint(patched_ids)


def test_tbm2_shared_margins_are_retained_for_global_layer() -> None:
    work = _work(
        FLOW_CHART_SOURCE,
        "tbm2-shared-margin",
        _flow_chart_toolbox(),
    )
    result = ToolboxPageCoordinator(
        FixedTranslationAdapter(_translations(work))
    ).execute(work)

    height = work.facts.page.height_points
    bbox_by_id = {
        item.object_id: item.bbox for item in work.facts.text_spans
    }
    margin_ids = {
        item.object_id
        for item in work.toolbox.ownership_audit()
        if (
            bbox_by_id[item.object_id][3] <= height * _policy().body_margin_top_ratio
            or bbox_by_id[item.object_id][1]
            >= height * _policy().body_margin_bottom_ratio
        )
    }
    retained_ids = {
        item.object_id
        for item in work.toolbox.ownership_audit()
        if item.component == "retained"
    }
    patched_ids = {
        object_id
        for operation in result.patch.operations
        for object_id in operation.target_object_ids
    }

    assert margin_ids
    assert margin_ids <= retained_ids
    assert margin_ids.isdisjoint(patched_ids)
    assert result.outcome.fallback is Fallback.REGION_FALLBACK


def test_tbm2_composite_failure_converges_at_root_without_partial_patch() -> None:
    work = _work(
        FLOW_CHART_SOURCE,
        "tbm2-root-failure",
        _flow_chart_toolbox(),
    )

    result = ToolboxPageCoordinator(
        FixedTranslationAdapter(_translations(work, long=True))
    ).execute(work)

    assert result.patch is None
    assert result.proposed_patch is not None
    assert result.proposed_patch.owner == "body.composite.flow_text_chart"
    assert result.outcome.fallback is Fallback.PAGE_PASSTHROUGH
    assert any(item.severity == "HARD" for item in result.findings)


def test_tbm2_freeform_is_fixed_bounded_and_only_uses_ready_components(
    tmp_path: Path,
) -> None:
    source = tmp_path / "freeform-classification-failure.pdf"
    with pymupdf.open() as document:
        page = document.new_page(width=420, height=320)
        page.insert_textbox(
            pymupdf.Rect(35, 32, 385, 90),
            (
                "This narrative paragraph represents an unclassified page and "
                "is deliberately long enough to form one deterministic flow region."
            ),
            fontsize=10,
        )
        page.draw_line((55, 270), (365, 270), color=(0, 0, 0))
        page.draw_rect(
            pymupdf.Rect(90, 180, 145, 270),
            color=(0.1, 0.4, 0.8),
            fill=(0.1, 0.4, 0.8),
        )
        page.draw_rect(
            pymupdf.Rect(205, 145, 260, 270),
            color=(0.9, 0.4, 0.1),
            fill=(0.9, 0.4, 0.1),
        )
        page.insert_text((88, 289), "Domestic market", fontsize=9)
        page.insert_text((205, 289), "Overseas market", fontsize=9)
        document.save(source)

    work = _work(source, "tbm2-freeform", _freeform_toolbox(source))
    adapter = _CountingAdapter(_translations(work))
    result = ToolboxPageCoordinator(adapter).execute(work)

    assert FREEFORM_COMPONENT_ALLOWLIST == ("diagram", "chart", "flow")
    assert work.toolbox.activation_reason == "CLASSIFICATION_FAILED"
    assert adapter.calls == 1
    assert result.patch is not None
    assert result.patch.owner == "body.freeform"
    assert {
        item.component for item in work.toolbox.ownership_audit()
    }.issubset(set(FREEFORM_COMPONENT_ALLOWLIST) | {"retained"})
    narrative_ids = {
        item.object_id
        for item in work.facts.text_spans
        if item.bbox[1] < 100.0
    }
    narrative_bbox = (
        min(
            item.bbox[0]
            for item in work.facts.text_spans
            if item.object_id in narrative_ids
        ),
        max(
            item.bbox[2]
            for item in work.facts.text_spans
            if item.object_id in narrative_ids
        ),
    )
    narrative_operation = next(
        item
        for item in result.patch.operations
        if narrative_ids.intersection(item.target_object_ids)
    )
    assert narrative_operation.rect is not None
    assert (
        narrative_operation.rect[2] - narrative_operation.rect[0]
        >= (narrative_bbox[1] - narrative_bbox[0]) * 0.75
    )
    disjoint_label_ids = {
        item.object_id
        for item in work.facts.text_spans
        if item.bbox[1] > 270.0
    }
    component_by_id = {
        item.object_id: item.component
        for item in work.toolbox.ownership_audit()
    }
    patched_ids = {
        object_id
        for operation in result.patch.operations
        for object_id in operation.target_object_ids
    }
    assert len(disjoint_label_ids) == 2
    assert {
        component_by_id[item]
        for item in disjoint_label_ids
    } == {"retained"}
    assert disjoint_label_ids.isdisjoint(patched_ids)


def test_tbm2_run_private_catalog_registers_only_ready_routes(tmp_path: Path) -> None:
    default_bytes = DEFAULT_CATALOG.read_bytes()
    payload = json.loads(default_bytes.decode("utf-8"))
    entry = next(
        item
        for item in payload["entries"]
        if item["route"] == "body.composite.flow_text_chart"
    )
    entry.update(
        {
            "enabled": True,
            "evidence_state": "PASS_ENABLE",
            "evidence_attestation_hash": "e" * 64,
            "disabled_reason": None,
        }
    )
    overlay = tmp_path / "page_toolbox_catalog_tbm2.json"
    overlay.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    factories = build_p8_toolbox_factories(
        POLICY_PATH,
        FONT_MANIFEST,
        REPO_ROOT,
    )
    factories.update(
        build_tbm2_toolbox_factories(
            POLICY_PATH,
            FONT_MANIFEST,
            REPO_ROOT,
            FLOW_CHART_SOURCE,
        )
    )

    catalog = load_toolbox_catalog(overlay, factories)
    assert catalog.validate_startup().ready
    resolved = catalog.resolve_enabled("body.composite.flow_text_chart", 1)
    assert resolved.toolbox is not None
    assert resolved.toolbox.descriptor.owner == "body.composite.flow_text_chart"
    assert {
        "body.composite.flow_text_table",
        "body.composite.chart_table",
        "body.composite.anchored_blocks_chart",
    }.isdisjoint(build_tbm2_toolbox_factories(
        POLICY_PATH,
        FONT_MANIFEST,
        REPO_ROOT,
        FLOW_CHART_SOURCE,
    ))
    assert _sha256_file(DEFAULT_CATALOG) == hashlib.sha256(default_bytes).hexdigest()


def test_tbm2_shared_page_concurrency_is_equivalent(tmp_path: Path) -> None:
    source = tmp_path / "two-flow-chart-pages.pdf"
    with pymupdf.open() as target, pymupdf.open(FLOW_CHART_SOURCE) as sample:
        target.insert_pdf(sample)
        target.insert_pdf(sample)
        target.save(source)

    sequential_pages = _pages(source, "tbm2-concurrency")
    parallel_pages = _pages(source, "tbm2-concurrency")
    sequential_work = tuple(
        ToolboxPageWork(page.context, page.facts, _flow_chart_toolbox())
        for page in sequential_pages
    )
    parallel_work = tuple(
        ToolboxPageWork(page.context, page.facts, _flow_chart_toolbox())
        for page in parallel_pages
    )
    translations = {}
    for work in (*sequential_work, *parallel_work):
        translations.update(_translations(work))
    coordinator = ToolboxPageCoordinator(FixedTranslationAdapter(translations))

    sequential = coordinator.execute_many(sequential_work, 1)
    parallel = coordinator.execute_many(parallel_work, 2)

    assert tuple(map(_execution_identity, sequential)) == tuple(
        map(_execution_identity, parallel)
    )
    assert all(item.patch is not None for item in parallel)
