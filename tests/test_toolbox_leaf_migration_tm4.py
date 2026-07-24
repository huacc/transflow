"""Lock the TM4 body.diagram migration boundary before implementing the leaf."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pymupdf

from scripts.run_tm4_diagram_pool_regression import (
    _recorded_translations,
    _RecordedTranslationPort,
)
from scripts.toolbox_leaf_migration_diagram import build_diagram_catalog_overlay
from scripts.toolbox_leaf_migration_drivers import DRIVER_FACTORIES
from transflow.application.document_coordinator import DocumentCoordinator
from transflow.application.toolbox_page_coordinator import (
    ToolboxPageCoordinator,
    ToolboxPageWork,
)
from transflow.domain.completeness import CompletenessStatus
from transflow.domain.jobs import DocumentRunRequest
from transflow.domain.toolbox import DecisionDisposition, PatchOperation
from transflow.domain.translation import TranslatedUnit, TranslationBundle
from transflow.pdf_kernel import ControlledFontRegistry, PageFactsExtractor
from transflow.pdf_kernel.patch import _preserves_source_image_owner
from transflow.toolboxes.contracts import TranslationDispatch
from transflow.toolboxes.leaves.body_diagram.judge import judge_diagram_plan
from transflow.toolboxes.leaves.body_diagram.layout import (
    _line_height_candidates,
    plan_diagram_layout,
    segment_hits_rect,
)
from transflow.toolboxes.leaves.body_diagram.models import (
    DiagramConnector,
    DiagramContainer,
    DiagramLayoutPlan,
    DiagramNode,
    DiagramPlacement,
    DiagramTemplate,
)
from transflow.toolboxes.leaves.body_diagram.template import build_diagram_template
from transflow.toolboxes.leaves.body_diagram.toolbox import (
    DiagramToolbox,
    _next_repair_font_size,
    _operation_needs_repair,
)
from transflow.toolboxes.leaves.policy import load_p8_toolbox_policy

REPO_ROOT = Path(__file__).resolve().parent.parent
FONT_MANIFEST = REPO_ROOT / "resources/manifests/font_manifest.json"
POLICY_PATH = REPO_ROOT / "resources/manifests/p8_toolbox_policy.json"
CATALOG_PATH = REPO_ROOT / "resources/catalogs/page_toolbox_catalog_v4.json"
DIAGRAM_ROOT = REPO_ROOT / "spikes/page_toolbox_engine_puncture_v1/toolboxes/body/diagram"
FONT_ID = "noto-sans-cjk-sc-regular"


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _request(
    path: Path,
    *,
    source_language: str = "en",
    target_language: str = "zh-CN",
) -> DocumentRunRequest:
    return DocumentRunRequest(
        source_pdf_path=str(path.resolve()),
        source_hash=_sha256_file(path),
        source_language=source_language,
        target_language=target_language,
        config_snapshot_hash="d" * 64,
        job_id="job-tm4-test",
        run_id="tm4-test",
    )


def _page(path: Path):
    return DocumentCoordinator(PageFactsExtractor()).enumerate_pages(_request(path))[0]


def _diagram_pool_source(sample_id: str) -> Path:
    manifest = DIAGRAM_ROOT / "samples/manifest.jsonl"
    record = next(
        json.loads(line)
        for line in manifest.read_text(encoding="utf-8").splitlines()
        if json.loads(line)["sample_id"] == sample_id
    )
    return DIAGRAM_ROOT / record["source_ref"]


def _toolbox(
    source_pdf: Path,
    *,
    source_language: str = "en",
    target_language: str = "zh-CN",
) -> DiagramToolbox:
    fonts = ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT)
    policy = replace(
        load_p8_toolbox_policy(POLICY_PATH),
        source_language=source_language,
        target_language=target_language,
    )
    return DiagramToolbox(
        policy,
        fonts.resolve(FONT_ID).path,
        source_pdf,
    )


def _intersection_area(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> float:
    return max(0.0, min(left[2], right[2]) - max(left[0], right[0])) * max(
        0.0,
        min(left[3], right[3]) - max(left[1], right[1]),
    )


def test_tm4_template_has_total_disjoint_ownership_and_topology() -> None:
    """Every native span has one owner and native diagram geometry is recovered."""

    source = _diagram_pool_source("DG_EN_00995_p052")
    page = _page(source)
    template = build_diagram_template(page.facts, source)
    editable = [
        object_id for container in template.containers for object_id in container.source_object_ids
    ]
    all_owned = [*editable, *template.protected_object_ids]
    expected = [item.object_id for item in page.facts.text_spans]

    assert sorted(all_owned) == sorted(expected)
    assert len(all_owned) == len(set(all_owned))
    assert template.nodes
    assert template.connectors
    assert all(node.source_drawing_ids for node in template.nodes)
    assert all(connector.source_drawing_id for connector in template.connectors)
    assert {container.owner_kind for container in template.containers} >= {"node", "local_label"}


def test_tm4_owner_is_resolved_before_dense_lines_are_merged(tmp_path: Path) -> None:
    """Text in adjacent nodes must never become one cross-node translation unit."""

    source = tmp_path / "dense-nodes.pdf"
    with pymupdf.open() as document:
        page = document.new_page(width=420, height=260)
        page.draw_rect(pymupdf.Rect(55, 70, 185, 125), color=(0, 0, 0))
        page.draw_rect(pymupdf.Rect(55, 129, 185, 184), color=(0, 0, 0))
        page.draw_line((120, 125), (120, 129), color=(0, 0, 0))
        page.insert_textbox(
            pymupdf.Rect(65, 82, 175, 116),
            "First owner\nfirst detail",
            fontsize=10,
        )
        page.insert_textbox(
            pymupdf.Rect(65, 141, 175, 175),
            "Second owner\nsecond detail",
            fontsize=10,
        )
        document.save(source)

    page = _page(source)
    template = build_diagram_template(page.facts, source)
    node_containers = [
        container for container in template.containers if container.owner_kind == "node"
    ]

    assert len({container.node_id for container in node_containers}) == 2
    assert not any(
        "First owner" in container.source_text and "Second owner" in container.source_text
        for container in node_containers
    )


def test_tm4_long_node_translation_stays_inside_original_owner() -> None:
    """A longer translation may reflow or shrink locally but cannot switch owner."""

    node = DiagramNode(
        "node-000",
        (40, 40, 230, 125),
        (46, 46, 224, 119),
        ("drawing-000",),
        ("node-000/text-00",),
    )
    container = DiagramContainer(
        "node-000/text-00",
        "node",
        "node-000",
        "node-000",
        ("source-000",),
        "业务流程",
        (92, 72, 150, 87),
        node.safe_text_bbox,
        0,
        (),
        "node_text",
        "NotoSansCJK",
        12.0,
        0,
        "CENTER",
    )
    template = DiagramTemplate(
        "synthetic-long-node",
        "body.diagram",
        320,
        200,
        "translated",
        (node,),
        (),
        (container,),
        (),
        "a" * 64,
        "b" * 64,
        "c" * 64,
    )
    font_path = ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT).resolve(FONT_ID).path

    plan, findings = plan_diagram_layout(
        template,
        {container.container_id: ("Business process coordination and operational responsibility")},
        font_file=font_path,
    )

    placement = plan.placements[0]
    assert placement.fit
    assert not findings
    assert placement.node_id == node.node_id
    assert _intersection_area(placement.output_bbox, node.safe_text_bbox) > 0
    assert node.safe_text_bbox[0] <= placement.output_bbox[0]
    assert placement.output_bbox[2] <= node.safe_text_bbox[2]
    assert node.safe_text_bbox[1] <= placement.output_bbox[1]
    assert placement.output_bbox[3] <= node.safe_text_bbox[3]
    assert placement.font_size >= 5.5


def test_tm4_connector_count_and_page_scale_are_not_sample_rules() -> None:
    """Connector collision logic is geometry-based across count and scale changes."""

    container = DiagramContainer(
        "label-000/text-00",
        "local_label",
        "label-000",
        None,
        ("text-000",),
        "Branch label",
        (80, 88, 150, 103),
        (70, 80, 210, 115),
        0,
        (),
        "independent_label",
        "Helvetica",
        10.0,
        0,
        "LEFT",
    )
    connectors = tuple(
        DiagramConnector(
            f"connector-{index:03d}",
            (220 + 20 * index, 76),
            (190 + 20 * index, 102),
            f"drawing-{index:03d}",
            None,
            None,
            "undirected",
        )
        for index in range(3)
    )
    template = DiagramTemplate(
        "connector-perturbation",
        "body.diagram",
        500,
        260,
        "translated",
        (),
        connectors,
        (container,),
        (),
        "d" * 64,
        "e" * 64,
        "f" * 64,
    )
    scaled = replace(
        template,
        page_id="connector-perturbation-scaled",
        width=750,
        height=390,
        connectors=tuple(
            replace(
                connector,
                start=tuple(value * 1.5 for value in connector.start),
                end=tuple(value * 1.5 for value in connector.end),
            )
            for connector in connectors
        ),
        containers=(
            replace(
                container,
                source_bbox=tuple(value * 1.5 for value in container.source_bbox),
                allowed_bbox=tuple(value * 1.5 for value in container.allowed_bbox),
                font_size=container.font_size * 1.5,
            ),
        ),
    )
    font_path = ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT).resolve(FONT_ID).path

    base_plan, base_findings = plan_diagram_layout(
        template,
        {container.container_id: "Translated branch label"},
        font_file=font_path,
    )
    scaled_plan, scaled_findings = plan_diagram_layout(
        scaled,
        {container.container_id: "Translated branch label"},
        font_file=font_path,
    )

    assert not base_findings
    assert not scaled_findings
    assert base_plan.placements[0].fit
    assert scaled_plan.placements[0].fit
    assert scaled_plan.placements[0].font_size > base_plan.placements[0].font_size


def test_tm4_global_typography_enlargement_is_capped_at_ten_percent() -> None:
    """Whitespace may improve readability without inflating one role by 25%."""

    container = DiagramContainer(
        "label-000/text-00",
        "local_label",
        "label-000",
        None,
        ("text-000",),
        "Title",
        (20, 20, 100, 40),
        (20, 20, 280, 100),
        0,
        (),
        "title",
        "Helvetica",
        10.0,
        0,
        "LEFT",
    )
    template = DiagramTemplate(
        "typography-cap",
        "body.diagram",
        300,
        180,
        "translated",
        (),
        (),
        (container,),
        (),
        "a" * 64,
        "b" * 64,
        "c" * 64,
    )
    font_path = ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT).resolve(FONT_ID).path

    plan, findings = plan_diagram_layout(
        template,
        {container.container_id: "Translated title"},
        font_file=str(font_path),
    )

    assert not findings
    assert plan.placements[0].font_size <= container.font_size * 1.10
    assert max(_line_height_candidates(container, 10.0, False)) <= 1.15
    assert max(
        _line_height_candidates(
            replace(
                container,
                role="independent_paragraph",
                source_text="Long body paragraph " * 10,
            ),
            10.0,
            False,
        )
    ) <= 1.55


def test_tm4_judge_uses_node_boundary_not_layout_search_region() -> None:
    """A node candidate remains legal outside the preferred inset but inside its owner."""

    node = DiagramNode(
        "node-000",
        (40, 40, 220, 130),
        (70, 60, 190, 110),
        ("drawing-000",),
        ("node-000/text-00",),
    )
    container = DiagramContainer(
        "node-000/text-00",
        "node",
        "node-000",
        "node-000",
        ("text-000",),
        "Source",
        (90, 75, 170, 92),
        node.safe_text_bbox,
        0,
        (),
        "node_text",
        "Helvetica",
        10.0,
        0,
        "CENTER",
    )
    template = DiagramTemplate(
        "node-boundary-contract",
        "body.diagram",
        300,
        200,
        "translated",
        (node,),
        (),
        (container,),
        (),
        "a" * 64,
        "b" * 64,
        "c" * 64,
    )
    placement = DiagramPlacement(
        container.container_id,
        container.owner_kind,
        container.owner_id,
        container.node_id,
        "Long translated node text",
        (52, 55, 208, 118),
        "font.otf",
        "font",
        9.0,
        1.0,
        0,
        "CENTER",
        "owner-boundary",
        True,
        (55, 58, 205, 115),
    )
    layout = DiagramLayoutPlan(
        template.page_id,
        template.toolbox_key,
        template.topology_sha256,
        (placement,),
    )

    assert not judge_diagram_plan("plan-node-boundary", template, layout)


def test_tm4_coordinate_locked_node_uses_source_position_as_legal_baseline() -> None:
    """An unchanged map coordinate is legal; a newly moved excursion is not."""

    node = DiagramNode(
        "node-000",
        (40, 40, 180, 120),
        (50, 50, 170, 110),
        ("drawing-000",),
        ("node-000/text-00",),
    )
    container = DiagramContainer(
        "node-000/text-00",
        "node",
        "node-000",
        "node-000",
        ("text-000",),
        "Source",
        (160, 70, 190, 90),
        (160, 70, 190, 90),
        0,
        (),
        "node_text",
        "Helvetica",
        10.0,
        0,
        "LEFT",
    )
    template = DiagramTemplate(
        "coordinate-node-baseline",
        "body.diagram",
        300,
        200,
        "translated",
        (node,),
        (),
        (container,),
        (),
        "a" * 64,
        "b" * 64,
        "c" * 64,
        "MAP_COORDINATE_LOCKED",
    )
    placement = DiagramPlacement(
        container.container_id,
        container.owner_kind,
        container.owner_id,
        container.node_id,
        "Translated",
        container.source_bbox,
        "font.otf",
        "font",
        8.0,
        1.0,
        0,
        "LEFT",
        "map-coordinate-locked",
        True,
        container.source_bbox,
    )
    unchanged = DiagramLayoutPlan(
        template.page_id,
        template.toolbox_key,
        template.topology_sha256,
        (placement,),
    )
    moved = replace(
        unchanged,
        placements=(
            replace(
                placement,
                output_bbox=(165, 70, 195, 90),
                glyph_bbox=(165, 70, 195, 90),
            ),
        ),
    )

    assert not judge_diagram_plan("plan-coordinate-baseline", template, unchanged)
    assert {
        finding.code
        for finding in judge_diagram_plan("plan-coordinate-moved", template, moved)
    } == {
        "DIAGRAM_MAP_TEXT_COORDINATE_CHANGED",
        "DIAGRAM_NODE_TEXT_OUTSIDE_NODE",
    }


def test_tm4_connector_increment_gate_applies_only_to_local_labels() -> None:
    """Shared-margin and node owners do not inherit the local-label connector gate."""

    connector = DiagramConnector(
        "connector-000",
        (120, 20),
        (120, 180),
        "drawing-000",
        None,
        None,
        "undirected",
    )
    container = DiagramContainer(
        "shared-margin-header-000/text-00",
        "shared_margin",
        "shared-margin-header-000",
        None,
        ("text-000",),
        "Header",
        (20, 10, 90, 25),
        (10, 5, 250, 35),
        0,
        (),
        "margin_header",
        "Helvetica",
        10.0,
        0,
        "LEFT",
    )
    template = DiagramTemplate(
        "shared-margin-connector-contract",
        "body.diagram",
        300,
        200,
        "translated",
        (),
        (connector,),
        (container,),
        (),
        "d" * 64,
        "e" * 64,
        "f" * 64,
    )
    placement = DiagramPlacement(
        container.container_id,
        container.owner_kind,
        container.owner_id,
        None,
        "Translated header",
        (20, 10, 160, 25),
        "font.otf",
        "font",
        10.0,
        1.0,
        0,
        "LEFT",
        "fit",
        True,
        (20, 10, 160, 25),
    )
    layout = DiagramLayoutPlan(
        template.page_id,
        template.toolbox_key,
        template.topology_sha256,
        (placement,),
    )

    assert not judge_diagram_plan("plan-shared-margin", template, layout)


def test_tm4_source_image_frame_is_a_hard_owner_not_an_internal_obstacle() -> None:
    """A map-card label may reflow inside its source frame, never beyond that frame."""

    page = (0.0, 0.0, 600.0, 800.0)
    owner = (100.0, 100.0, 220.0, 160.0)
    nested_map_image = (180.0, 130.0, 320.0, 260.0)
    source_rects = ((120.0, 110.0, 190.0, 140.0),)

    assert _preserves_source_image_owner(
        (110.0, 105.0, 215.0, 150.0),
        source_rects,
        (owner, nested_map_image),
        page,
    )
    assert not _preserves_source_image_owner(
        (110.0, 105.0, 225.0, 150.0),
        source_rects,
        (owner, nested_map_image),
        page,
    )


def test_tm4_repair_matches_container_region_and_never_enlarges_font() -> None:
    """Container findings reach their operation and one repair is a local 10% shrink."""

    operation = PatchOperation(
        "op-001",
        "body-diagram-p0001-node-000/text-00",
        "replace_text",
        "a" * 64,
        owner="body.diagram",
        target_object_ids=("source-001",),
        rect=(10, 10, 100, 30),
        replacement_text="Translated",
        font_id=FONT_ID,
        font_size=4.76,
    )

    assert _operation_needs_repair(operation, {"node-000/text-00"})
    assert not _operation_needs_repair(operation, {"node-000"})
    assert _next_repair_font_size(operation.font_size or 0.0) == 4.28
    assert _next_repair_font_size(operation.font_size or 0.0) < (operation.font_size or 0.0)


def test_tm4_toolbox_builds_real_patch_without_editing_diagram_geometry() -> None:
    """Production output targets native text only; drawings and images remain protected."""

    source = _diagram_pool_source("DG_EN_00631_p006")
    page = _page(source)
    toolbox = _toolbox(source)
    template = toolbox.prepare(page.context, page.facts)
    batch = toolbox.build_translation_request(template)
    assert batch is not None
    bundle = TranslationBundle.from_batch(
        batch,
        tuple(TranslatedUnit(unit.unit_id, f"译文 {unit.source_text}") for unit in batch.units),
    )

    plan = toolbox.consume_translation_bundle(
        template,
        TranslationDispatch(batch, bundle=bundle),
    )

    assert plan.patch is not None
    text_ids = {item.object_id for item in page.facts.text_spans}
    protected_ids = {
        *(item.object_id for item in page.facts.drawing_objects),
        *(item.object_id for item in page.facts.image_objects),
    }
    assert all(set(operation.target_object_ids) <= text_ids for operation in plan.patch.operations)
    assert all(
        not (set(operation.target_object_ids) & protected_ids)
        for operation in plan.patch.operations
    )
    assert all(operation.preserve_drawing_overlap for operation in plan.patch.operations)


def test_tm4_mixed_container_keeps_mechanical_prefix_out_of_translation() -> None:
    """Kernel-approved section markers stay native while adjacent semantics translate."""

    source = _diagram_pool_source("DG_EN_00995_p052")
    page = _page(source)
    toolbox = _toolbox(source)
    template = toolbox.prepare(page.context, page.facts)
    batch = toolbox.build_translation_request(template)
    assert batch is not None
    governance = next(
        unit for unit in batch.units if "INFORMATION ON CORPORATE GOVERNANCE" in unit.source_text
    )
    prefix = next(item for item in page.facts.text_spans if item.text.strip() == "I.")

    assert governance.source_text == "INFORMATION ON CORPORATE GOVERNANCE"
    assert prefix.object_id not in governance.source_object_ids
    snapshot = toolbox._snapshots[template.template_id]
    governance_container = next(
        container
        for container in snapshot.template.containers
        if governance.region_id.endswith(f"-{container.container_id}")
    )
    assert prefix.object_id not in getattr(
        governance_container,
        "recomposed_object_ids",
        (),
    )


def test_tm4_promotes_translatable_margin_text_without_claiming_page_number(
    tmp_path: Path,
) -> None:
    """Spike-protected margin text still receives a shared-margin translation owner."""

    source = tmp_path / "diagram-with-footer.pdf"
    with pymupdf.open() as document:
        page = document.new_page(width=420, height=500)
        page.draw_rect(pymupdf.Rect(100, 150, 320, 230), color=(0, 0, 0))
        page.insert_textbox(
            pymupdf.Rect(120, 170, 300, 210),
            "Approval workflow",
            fontsize=11,
            align=pymupdf.TEXT_ALIGN_CENTER,
        )
        page.insert_text((250, 493), "Summary footer words", fontsize=8)
        page.insert_text((390, 493), "17", fontsize=8)
        document.save(source)

    page = _page(source)
    spike_template = build_diagram_template(page.facts, source)
    footer = next(
        item for item in page.facts.text_spans if item.text == "Summary footer words"
    )
    assert footer.object_id in spike_template.protected_object_ids

    toolbox = _toolbox(source)
    template = toolbox.prepare(page.context, page.facts)
    batch = toolbox.build_translation_request(template)

    assert batch is not None
    assert any(unit.source_text == "Summary footer words" for unit in batch.units)
    assert all(unit.source_text != "17" for unit in batch.units)


def test_tm4_recomposes_inline_keep_source_literals_with_their_paragraph() -> None:
    """Inline numbers and codes keep their text but move with the translated owner."""

    source = _diagram_pool_source("DG_ZH_00631_p007")
    page = _page(source)
    toolbox = _toolbox(
        source,
        source_language="zh-CN",
        target_language="en",
    )
    template = toolbox.prepare(page.context, page.facts)
    batch = toolbox.build_translation_request(template)
    assert batch is not None
    paragraph = next(unit for unit in batch.units if "258" in unit.source_text)
    numeric = next(item for item in page.facts.text_spans if item.text.strip() == "258")
    snapshot = toolbox._snapshots[template.template_id]
    container = next(
        item
        for item in snapshot.template.containers
        if paragraph.region_id.endswith(f"-{item.container_id}")
    )

    assert numeric.object_id not in paragraph.source_object_ids
    assert numeric.object_id in getattr(container, "recomposed_object_ids", ())

    bundle = TranslationBundle.from_batch(
        batch,
        tuple(
            TranslatedUnit(unit.unit_id, f"Translated {unit.source_text}")
            for unit in batch.units
        ),
    )
    plan = toolbox.consume_translation_bundle(
        template,
        TranslationDispatch(batch, bundle=bundle),
    )
    assert plan.patch is not None
    operation = next(
        item
        for item in plan.patch.operations
        if item.region_id == paragraph.region_id
    )
    assert numeric.object_id in operation.target_object_ids


def test_tm4_inline_keep_source_brand_is_authorized_by_completeness() -> None:
    """A preauthorized Latin brand on a zh-to-en page is not a source residual."""

    sample_id = "DG_ZH_08495_p089"
    source = _diagram_pool_source(sample_id)
    page = _page(source)
    toolbox = _toolbox(
        source,
        source_language="zh-CN",
        target_language="en",
    )
    result = ToolboxPageCoordinator(
        _RecordedTranslationPort(
            _recorded_translations(sample_id),
            "en",
        )
    ).execute(
        ToolboxPageWork(
            page.context,
            page.facts,
            toolbox,
            target_language="en",
        )
    )

    assert result.completeness_decision is not None
    assert result.completeness_decision.status is CompletenessStatus.PASS


def test_tm4_connector_sensitive_paragraph_retries_before_judge() -> None:
    """A flow cohort retries together when one paragraph adds a connector hit."""

    sample_id = "DG_ZH_02400_p041"
    source = _diagram_pool_source(sample_id)
    page = _page(source)
    result = ToolboxPageCoordinator(
        _RecordedTranslationPort(
            _recorded_translations(sample_id),
            "en",
        )
    ).execute(
        ToolboxPageWork(
            page.context,
            page.facts,
            _toolbox(
                source,
                source_language="zh-CN",
                target_language="en",
            ),
            target_language="en",
        )
    )

    assert result.verdict.disposition is DecisionDisposition.ACCEPT
    assert "DIAGRAM_NEW_CONNECTOR_COLLISION" not in {
        finding.code for finding in result.findings
    }
    assert result.patch is not None
    paragraphs = [
        operation
        for operation in result.patch.operations
        if operation.region_id.endswith(
            (
                "label-005/text-00",
                "label-007/text-00",
            )
        )
    ]
    assert len(paragraphs) == 2
    assert paragraphs[0].font_size == paragraphs[1].font_size
    assert paragraphs[0].line_height == paragraphs[1].line_height


def test_tm4_connector_gate_uses_actual_glyph_boundary() -> None:
    """A visible gap is not converted into a collision by an extra safety halo."""

    assert not segment_hits_rect(
        (10.0, 28.9989),
        (500.0, 28.9989),
        (118.0542, 18.6856, 273.5942, 28.8216),
    )


def test_tm4_header_near_decoration_line_remains_deliverable() -> None:
    """A translated header may approach, but not cross, its original decoration line."""

    sample_id = "DG_EN_00631_p008"
    source = _diagram_pool_source(sample_id)
    page = _page(source)
    result = ToolboxPageCoordinator(
        _RecordedTranslationPort(
            _recorded_translations(sample_id),
            "zh-CN",
        )
    ).execute(
        ToolboxPageWork(
            page.context,
            page.facts,
            _toolbox(source),
            target_language="zh-CN",
        )
    )

    assert result.verdict.disposition is DecisionDisposition.ACCEPT
    assert "DIAGRAM_NEW_CONNECTOR_COLLISION" not in {
        finding.code for finding in result.findings
    }


def test_tm4_same_level_body_paragraphs_reflow_without_overlap() -> None:
    """Long sibling paragraphs share typography and move vertically as one flow."""

    sample_id = "DG_ZH_00995_p052"
    source = _diagram_pool_source(sample_id)
    page = _page(source)
    result = ToolboxPageCoordinator(
        _RecordedTranslationPort(
            _recorded_translations(sample_id),
            "en",
        )
    ).execute(
        ToolboxPageWork(
            page.context,
            page.facts,
            _toolbox(
                source,
                source_language="zh-CN",
                target_language="en",
            ),
            target_language="en",
        )
    )

    assert result.verdict.disposition is DecisionDisposition.ACCEPT
    assert result.patch is not None
    paragraphs = [
        operation
        for operation in result.patch.operations
        if operation.region_id.endswith(
            (
                "label-002/text-00",
                "label-003/text-00",
            )
        )
    ]
    assert len(paragraphs) == 2
    assert paragraphs[0].font_size == paragraphs[1].font_size
    assert paragraphs[0].line_height == paragraphs[1].line_height
    assert paragraphs[0].rect[3] <= paragraphs[1].rect[1]


def test_tm4_judge_rejects_inconsistent_or_overlapping_flow_paragraphs() -> None:
    """The final gate owns visual-cohort consistency, not only rectangle bounds."""

    containers = tuple(
        DiagramContainer(
            f"label-{index:03d}/text-00",
            "local_label",
            f"label-{index:03d}",
            None,
            (f"text-{index:03d}",),
            "Source paragraph",
            source_bbox,
            (60, 60, 360, 220),
            index,
            (),
            "independent_paragraph",
            "Helvetica",
            10.0,
            0,
            "LEFT",
        )
        for index, source_bbox in enumerate(
            (
                (60, 70, 340, 105),
                (60, 115, 340, 150),
            )
        )
    )
    template = DiagramTemplate(
        "flow-gate-contract",
        "body.diagram",
        420,
        280,
        "translated",
        (),
        (),
        containers,
        (),
        "a" * 64,
        "b" * 64,
        "c" * 64,
    )
    layout = DiagramLayoutPlan(
        template.page_id,
        template.toolbox_key,
        template.topology_sha256,
        (
            DiagramPlacement(
                containers[0].container_id,
                "local_label",
                containers[0].owner_id,
                None,
                "First translated paragraph",
                (60, 70, 360, 125),
                "font.otf",
                "font",
                10.0,
                1.4,
                0,
                "LEFT",
                "measured",
                True,
                (60, 70, 350, 122),
            ),
            DiagramPlacement(
                containers[1].container_id,
                "local_label",
                containers[1].owner_id,
                None,
                "Second translated paragraph",
                (60, 110, 360, 165),
                "font.otf",
                "font",
                8.0,
                1.0,
                0,
                "LEFT",
                "measured",
                True,
                (60, 112, 350, 160),
            ),
        ),
    )

    codes = {
        finding.code
        for finding in judge_diagram_plan(
            "plan-flow-gate-contract",
            template,
            layout,
        )
    }

    assert "DIAGRAM_BODY_TYPOGRAPHY_INCONSISTENT" in codes
    assert "DIAGRAM_FLOW_TEXT_COLLISION" in codes


def test_tm4_registration_is_explicit_and_catalog_overlay_is_private() -> None:
    """TM4 registers one driver without mutating the repository default Catalog."""

    before = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    overlay = build_diagram_catalog_overlay(before)
    after = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    before_entry = next(item for item in before["entries"] if item["route"] == "body.diagram")
    overlay_entry = next(item for item in overlay["entries"] if item["route"] == "body.diagram")

    assert "body.diagram" in DRIVER_FACTORIES
    assert before == after
    assert before_entry["enabled"] is False
    assert overlay_entry["enabled"] is True
    assert overlay_entry["evidence_state"] == "PASS_ENABLE"


def test_tm4_production_code_has_no_spike_runtime_or_sample_identity() -> None:
    """Migrated production code may cite provenance, never import the spike at runtime."""

    production_root = REPO_ROOT / "src/transflow/toolboxes/leaves/body_diagram"
    source = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(production_root.glob("*.py"))
    )

    assert "spikes." not in source
    assert "page_toolbox_engine_puncture_v1" not in source
    assert "DG_EN_" not in source
    assert "DG_ZH_" not in source
    assert "_p006" not in source
