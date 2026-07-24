"""Lock the TM3 body.chart migration boundary before implementing the leaf."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest

from scripts.run_toolbox_leaf_migration import (
    DRIVER_FACTORIES,
    MigrationContractError,
    _forbidden_production_dependencies,
    load_leaf_input_manifest,
)
from scripts.toolbox_leaf_migration_chart import build_chart_catalog_overlay
from transflow.adapters.ai.fixed import FixedTranslationAdapter
from transflow.application.document_coordinator import DocumentCoordinator
from transflow.application.toolbox_page_coordinator import (
    ToolboxPageCoordinator,
    ToolboxPageWork,
)
from transflow.application.translation_completeness import (
    build_semantic_unit_map,
    extract_required_literals,
)
from transflow.domain.jobs import DocumentRunRequest
from transflow.domain.pages import PageExecutionContext
from transflow.domain.text_inventory import InventoryDisposition
from transflow.pdf_kernel import (
    ControlledFontRegistry,
    PageFactsExtractor,
    PagePatchInterpreter,
)
from transflow.pdf_kernel.text_inventory import freeze_page_text_inventory
from transflow.toolboxes.leaves.body_chart.template import build_chart_template
from transflow.toolboxes.leaves.body_chart.toolbox import ChartToolbox
from transflow.toolboxes.leaves.policy import load_p8_toolbox_policy

REPO_ROOT = Path(__file__).resolve().parent.parent
FONT_MANIFEST = REPO_ROOT / "resources/manifests/font_manifest.json"
POLICY_PATH = REPO_ROOT / "resources/manifests/p8_toolbox_policy.json"
CATALOG_PATH = REPO_ROOT / "resources/catalogs/page_toolbox_catalog_v4.json"
FONT_ID = "noto-sans-cjk-sc-regular"


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _request(path: Path) -> DocumentRunRequest:
    return DocumentRunRequest(
        source_pdf_path=str(path.resolve()),
        source_hash=_sha256_file(path),
        source_language="en",
        target_language="zh-CN",
        config_snapshot_hash="b" * 64,
        job_id="job-tm3-test",
        run_id="tm3-test",
    )


def _chart_source_pdf(path: Path) -> Path:
    """Create a native vector chart with semantic labels and protected values."""

    with pymupdf.open() as document:
        page = document.new_page(width=420, height=300)
        page.insert_text((15, 18), "ANNUAL REPORT", fontsize=7)
        page.insert_text((52, 48), "Revenue by Segment", fontsize=16)
        page.draw_line((55, 235), (365, 235), color=(0.1, 0.1, 0.1), width=1)
        page.draw_line((55, 85), (55, 235), color=(0.1, 0.1, 0.1), width=1)
        page.draw_rect(
            pymupdf.Rect(90, 135, 145, 235),
            color=(0.1, 0.4, 0.8),
            fill=(0.1, 0.4, 0.8),
        )
        page.draw_rect(
            pymupdf.Rect(190, 105, 245, 235),
            color=(0.9, 0.4, 0.1),
            fill=(0.9, 0.4, 0.1),
        )
        page.draw_rect(
            pymupdf.Rect(300, 68, 312, 80),
            color=(0.1, 0.4, 0.8),
            fill=(0.1, 0.4, 0.8),
        )
        page.insert_text((318, 79), "Technology", fontsize=9)
        page.insert_text((94, 254), "Asia Pacific", fontsize=9)
        page.insert_text((194, 254), "Europe", fontsize=9)
        page.insert_text((103, 130), "20%", fontsize=8)
        page.insert_text((204, 100), "35%", fontsize=8)
        page.insert_text((39, 238), "0", fontsize=8)
        page.insert_text((32, 166), "50", fontsize=8)
        page.insert_text((25, 92), "100", fontsize=8)
        document.save(path)
    return path


def _facts(path: Path):
    return DocumentCoordinator(PageFactsExtractor()).enumerate_pages(_request(path))[0]


def _chart_pool_source(sample_id: str) -> Path:
    manifest = (
        REPO_ROOT
        / "spikes/page_toolbox_engine_puncture_v1/toolboxes/body/chart"
        / "samples/manifest.jsonl"
    )
    record = next(
        json.loads(line)
        for line in manifest.read_text(encoding="utf-8").splitlines()
        if json.loads(line)["sample_id"] == sample_id
    )
    return manifest.parent.parent / record["source_ref"]


def _intersection_area(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> float:
    return max(0.0, min(left[2], right[2]) - max(left[0], right[0])) * max(
        0.0, min(left[3], right[3]) - max(left[1], right[1])
    )


def _toolbox() -> ChartToolbox:
    fonts = ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT)
    return ChartToolbox(
        load_p8_toolbox_policy(POLICY_PATH),
        fonts.resolve(FONT_ID).path,
    )


def test_tm3_template_has_total_disjoint_text_ownership(tmp_path: Path) -> None:
    """Every native text span has exactly one editable/protected owner."""

    page = _facts(_chart_source_pdf(tmp_path / "chart.pdf"))
    template = build_chart_template(page.facts)
    editable_ids = [
        object_id
        for container in template.containers
        for object_id in container.source_object_ids
    ]
    owned_ids = [*editable_ids, *template.protected_object_ids]
    expected_ids = [item.object_id for item in page.facts.text_spans]

    assert sorted(owned_ids) == sorted(expected_ids)
    assert len(owned_ids) == len(set(owned_ids))
    assert {
        item.text
        for item in page.facts.text_spans
        if item.object_id in set(template.protected_object_ids)
    } >= {"20%", "35%", "0", "50", "100"}
    assert {"Revenue by Segment", "Technology", "Asia Pacific", "Europe"} <= {
        container.source_text for container in template.containers
    }
    assert next(
        item for item in template.containers if item.source_text == "ANNUAL REPORT"
    ).role == "PAGE_HEADER"
    assert {"TITLE", "LEGEND_LABEL", "AXIS_OR_CATEGORY_LABEL"} <= {
        container.role for container in template.containers
    }
    for index, left in enumerate(template.containers):
        for right in template.containers[index + 1 :]:
            assert _intersection_area(left.allowed_bbox, right.allowed_bbox) <= 0.05


def test_tm3_right_value_table_keeps_each_visual_row_separate() -> None:
    """A chart-embedded table must not merge labels from different rows."""

    page = _facts(_chart_pool_source("CH_EN_03700_p073"))
    template = build_chart_template(page.facts)
    by_source_text = {item.source_text: item for item in template.containers}
    expected_cells = {
        "Total number of employees (Full-time)",
        "Number of employees by rank",
        "Management staff",
        "General staff",
        "Male",
        "Female",
        "Under 30 years old",
        "30-40 years old",
        "Above 40 years old",
        "Chinese Mainland",
        "Hong Kong, Macau and Taiwan regions",
    }

    assert expected_cells <= set(by_source_text)
    assert all(by_source_text[text].role == "TABLE_CELL" for text in expected_cells)
    assert all(
        by_source_text[text].source_bbox[3] - by_source_text[text].source_bbox[1]
        < 12
        for text in expected_cells
    )
    note = by_source_text["Note:"]
    assert note.role == "ANNOTATION"
    assert note.alignment == "LEFT"


def test_tm3_prose_before_local_table_keeps_one_left_anchored_semantic_flow() -> None:
    """Nearby table geometry must not turn ordinary paragraph lines into headers."""

    page = _facts(_chart_pool_source("CH_EN_01596_p108"))
    template = build_chart_template(page.facts)
    paragraph = next(
        item
        for item in template.containers
        if item.source_text.startswith("During the Reporting Period")
    )

    assert paragraph.source_text.endswith(
        "categorised by gender and age as shown in the following table:"
    )
    assert paragraph.role == "ANNOTATION"
    assert paragraph.alignment == "LEFT"
    assert paragraph.source_bbox[0] == pytest.approx(70.8661, abs=0.01)
    assert next(
        item
        for item in template.containers
        if item.source_text == "Employee turnover"
    ).source_object_ids != paragraph.source_object_ids
    assert not any(
        item.role == "TABLE_HEADER"
        and item.source_text.startswith("registering a turnover rate")
        for item in template.containers
    )


def test_tm3_title_anchor_follows_source_axis_not_title_role_alone() -> None:
    """Centred visual titles keep the owner axis; offset titles remain left."""

    from transflow.toolboxes.leaves.body_chart.models import ChartVisualRegion
    from transflow.toolboxes.leaves.body_chart.template import (
        _alignment,
        _Line,
    )

    association = ChartVisualRegion(
        "visual",
        "DRAWING_CLUSTER",
        (50.0, 80.0, 250.0, 230.0),
        ("visual-object",),
    )

    def line(
        object_id: str,
        text: str,
        bbox: tuple[float, float, float, float],
    ) -> object:
        return _Line(
            objects=(SimpleNamespace(object_id=object_id),),
            text=text,
            bbox=bbox,
            font_size=12.0,
            color_srgb=0,
            font_name="ExampleSans-Bold",
        )

    centred_above = [line("centered", "Chart title", (110.0, 55.0, 190.0, 70.0))]
    offset_above = [line("offset", "Chart title", (55.0, 55.0, 135.0, 70.0))]

    assert (
        _alignment(
            centred_above,
            centred_above[0].bbox,
            association,
            (),
            300.0 * 300.0,
            300.0,
            None,
            "TITLE",
        )
        == "CENTER"
    )
    assert (
        _alignment(
            offset_above,
            offset_above[0].bbox,
            association,
            (),
            300.0 * 300.0,
            300.0,
            None,
            "TITLE",
        )
        == "LEFT"
    )
    assert (
        _alignment(
            centred_above,
            centred_above[0].bbox,
            association,
            (),
            300.0 * 300.0,
            300.0,
            None,
            "ANNOTATION",
        )
        == "LEFT"
    )


def test_tm3_real_chart_titles_preserve_centered_and_left_source_modes() -> None:
    """Real titles use owner-axis evidence without a page-specific branch."""

    centred = build_chart_template(
        _facts(_chart_pool_source("CH_EN_00397_p066")).facts
    )
    centred_by_text = {
        item.source_text: item.alignment for item in centred.containers
    }
    assert centred_by_text["BY GENDER"] == "CENTER"
    assert centred_by_text["BY EMPLOYMENT TYPE"] == "CENTER"
    assert centred_by_text["BY AGE"] == "CENTER"
    assert centred_by_text["BY RANK"] == "CENTER"

    left = build_chart_template(
        _facts(_chart_pool_source("CH_EN_00405_p073")).facts
    )
    left_by_text = {item.source_text: item.alignment for item in left.containers}
    assert (
        left_by_text[
            "TOP 10 TENANTS BY RENTAL INCOME (AS AT 31 DECEMBER 2025)"
        ]
        == "LEFT"
    )


def test_tm3_table_header_exclusion_uses_flow_and_style_not_page_identity() -> None:
    """A shifted prose continuation is excluded, while a true header stack remains."""

    from transflow.toolboxes.leaves.body_chart.template import (
        _continues_non_table_flow,
        _font_style,
        _Line,
        _line_key,
    )

    def line(
        object_id: str,
        text: str,
        bbox: tuple[float, float, float, float],
        font_name: str = "ExampleSans-Regular",
    ) -> object:
        return _Line(
            objects=(SimpleNamespace(object_id=object_id),),
            text=text,
            bbox=bbox,
            font_size=11.0,
            color_srgb=0,
            font_name=font_name,
        )

    prose = [
        line("lead", "A paragraph starts above the inferred table header band.", (42, 80, 360, 91)),
        line("continued", "It continues with the same semantic left anchor.", (42, 95, 330, 106)),
        line("tail", "The final line still belongs to that paragraph.", (42, 110, 310, 121)),
    ]
    prose_header_keys = {_line_key(item) for item in prose[1:]}
    assert _continues_non_table_flow(prose[1], prose, prose_header_keys)
    assert _continues_non_table_flow(prose[2], prose, prose_header_keys)

    true_header = [
        line("header-1", "Number of", (310, 150, 365, 161), "ExampleSans-Bd"),
        line("header-2", "employees", (310, 165, 370, 176), "ExampleSans-Bd"),
    ]
    true_header_keys = {_line_key(item) for item in true_header}
    assert not _continues_non_table_flow(
        true_header[1],
        true_header,
        true_header_keys,
    )
    assert _font_style("ExampleSans-It") == (False, True)
    assert _font_style("ExampleSans-Bd") == (True, False)


def test_tm3_right_value_table_rule_is_geometry_scoped_not_sample_scoped() -> None:
    """The row rule accepts a shifted table class and rejects unstable chart pairs."""

    from transflow.toolboxes.leaves.body_chart.template import (
        _Line,
        _right_value_table_cells,
    )

    def line(
        object_id: str,
        text: str,
        bbox: tuple[float, float, float, float],
    ) -> object:
        return _Line(
            objects=(SimpleNamespace(object_id=object_id),),
            text=text,
            bbox=bbox,
            font_size=10.0,
            color_srgb=0,
            font_name="Regular",
        )

    table_lines = [
        line("label-1", "Domestic sales", (72.0, 220.0, 160.0, 230.0)),
        line("value-1", "1,200", (500.0, 220.0, 550.0, 230.0)),
        line("label-2", "Overseas sales", (72.0, 238.0, 165.0, 248.0)),
        line("value-2", "800", (520.0, 238.0, 550.0, 248.0)),
        line("label-3", "Retail", (72.0, 256.0, 112.0, 266.0)),
        line("detail-3", "Online", (280.0, 256.0, 325.0, 266.0)),
        line("value-3", "450", (520.0, 256.0, 550.0, 266.0)),
        line("label-4", "Wholesale", (72.0, 274.0, 132.0, 284.0)),
        line("value-4", "350", (520.0, 274.0, 550.0, 284.0)),
    ]
    table_cells = _right_value_table_cells(table_lines, 600.0, 800.0)

    assert {
        ("label-1",),
        ("label-2",),
        ("label-3",),
        ("detail-3",),
        ("label-4",),
    } == set(table_cells)
    assert all(cell.role == "TABLE_CELL" for cell in table_cells.values())

    unstable_pairs = [
        line("axis-1", "North", (72.0, 220.0, 110.0, 230.0)),
        line("tick-1", "10", (520.0, 220.0, 550.0, 230.0)),
        line("axis-2", "South", (72.0, 275.0, 110.0, 285.0)),
        line("tick-2", "20", (460.0, 275.0, 490.0, 285.0)),
        line("axis-3", "East", (72.0, 330.0, 102.0, 340.0)),
        line("tick-3", "30", (400.0, 330.0, 430.0, 340.0)),
        line("axis-4", "West", (72.0, 385.0, 102.0, 395.0)),
        line("tick-4", "40", (340.0, 385.0, 370.0, 395.0)),
    ]

    assert _right_value_table_cells(unstable_pairs, 600.0, 800.0) == {}


def test_tm3_toolbox_builds_one_page_batch_and_visual_safe_patch(
    tmp_path: Path,
) -> None:
    """The production leaf uses one page batch and never targets chart visuals."""

    source = _chart_source_pdf(tmp_path / "chart.pdf")
    page = _facts(source)
    toolbox = _toolbox()
    template = toolbox.prepare(page.context, page.facts)
    batch = toolbox.build_translation_request(template)
    assert batch is not None
    assert len(batch.units) == len(template.object_ids)
    translated = {
        unit.unit_id: {
            "ANNUAL REPORT": "年度报告",
            "Revenue by Segment": "分部收入",
            "Technology": "科技",
            "Asia Pacific": "亚太地区",
            "Europe": "欧洲",
        }[unit.source_text]
        for unit in batch.units
    }
    result = ToolboxPageCoordinator(FixedTranslationAdapter(translated)).execute(
        ToolboxPageWork(page.context, page.facts, toolbox)
    )

    assert result.patch is not None
    assert len(result.patch.operations) == len(batch.units)
    visual_ids = {
        *(item.object_id for item in page.facts.image_objects),
        *(item.object_id for item in page.facts.drawing_objects),
    }
    assert not visual_ids.intersection(
        object_id
        for operation in result.patch.operations
        for object_id in operation.target_object_ids
    )
    assert all(operation.preserve_drawing_overlap for operation in result.patch.operations)
    assert all(
        len(operation.redaction_rects) == len(operation.target_object_ids)
        for operation in result.patch.operations
    )
    assert result.semantic_unit_map is not None
    header_entry = next(
        item
        for item in result.semantic_unit_map.entries
        if item.source_text == "ANNUAL REPORT"
    )
    assert header_entry.owner == "shared.margin.header"

    fonts = ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT)
    target = tmp_path / "chart-translated.pdf"
    with pymupdf.open(source) as document:
        applied = PagePatchInterpreter(fonts).apply(
            document,
            page.context,
            page.facts,
            result.patch,
            "body.chart",
        )
        assert applied.fits
        document.save(target)
    candidate = PageFactsExtractor().extract_page(target, _sha256_file(target), 1)
    assert [
        (item.bbox, item.content_hash) for item in candidate.image_objects
    ] == [(item.bbox, item.content_hash) for item in page.facts.image_objects]
    assert [
        (item.bbox, item.content_hash) for item in candidate.drawing_objects
    ] == [(item.bbox, item.content_hash) for item in page.facts.drawing_objects]


def test_tm3_chart_persists_managed_layout_rule_trace(tmp_path: Path) -> None:
    """Each chart placement must expose its scoped rule and production dispatch."""

    dispatch_path = (
        REPO_ROOT
        / "resources/manifests/toolbox_leaf_migration"
        / "body_chart_failure_dispatch.json"
    )
    dispatch = json.loads(dispatch_path.read_text(encoding="utf-8"))
    source = _chart_source_pdf(tmp_path / "chart.pdf")
    page = _facts(source)
    toolbox = _toolbox()
    template = toolbox.prepare(page.context, page.facts)
    batch = toolbox.build_translation_request(template)
    assert batch is not None
    translated = {
        unit.unit_id: {
            "ANNUAL REPORT": "年度报告",
            "Revenue by Segment": "分部收入",
            "Technology": "科技",
            "Asia Pacific": "亚太地区",
            "Europe": "欧洲",
        }[unit.source_text]
        for unit in batch.units
    }

    result = ToolboxPageCoordinator(
        FixedTranslationAdapter(translated)
    ).execute(ToolboxPageWork(page.context, page.facts, toolbox))
    trace = toolbox.rule_trace(f"plan-{template.template_id}")

    assert result.patch is not None
    assert len(trace) == len(result.patch.operations)
    assert dispatch["schema_version"] == "body.chart.failure-dispatch/v1"
    bindings = {
        item["failure_class"]: item for item in dispatch["bindings"]
    }
    for record in trace:
        assert record["schema_version"] == "p13-chart-layout-rule/v1"
        assert record["scope"] == "body.chart"
        assert record["dispatch_result"]["dispatch_table"] == str(
            dispatch_path.relative_to(REPO_ROOT)
        ).replace("\\", "/")
        failure_class = record["selected_failure_class"]
        repair_atom = record["dispatch_result"]["selected_repair_atom"]
        if failure_class is not None:
            assert bindings[failure_class]["repair_atom"] == repair_atom
        assert record["evidence"]["source_glyph_bbox_is_not_a_hard_width_boundary"]


def test_tm3_chart_can_materialize_rejected_translation_as_diagnostic_pdf(
    tmp_path: Path,
) -> None:
    """A product FAIL still needs a translated, explicitly non-product PDF."""

    from transflow.domain.translation import TranslatedUnit, TranslationBundle

    source = _chart_source_pdf(tmp_path / "chart.pdf")
    output = tmp_path / "chart-diagnostic.pdf"
    page = _facts(source)
    toolbox = _toolbox()
    template = toolbox.prepare(page.context, page.facts)
    batch = toolbox.build_translation_request(template)
    assert batch is not None
    bundle = TranslationBundle.from_batch(
        batch,
        tuple(
            TranslatedUnit(unit.unit_id, "重复诊断译文")
            for unit in batch.units
        ),
    )

    patch, records = toolbox.build_diagnostic_patch(
        template,
        batch,
        bundle,
    )

    assert patch is not None
    assert len(patch.operations) == len(batch.units)
    assert all(
        operation.replacement_text == "重复诊断译文"
        for operation in patch.operations
    )
    assert all(record["product_acceptance"] is False for record in records)
    fonts = ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT)
    with pymupdf.open(source) as document:
        applied = PagePatchInterpreter(fonts).apply(
            document,
            page.context,
            page.facts,
            patch,
            "body.chart",
        )
        assert applied.fits
        document.save(output)
    assert output.is_file() and output.stat().st_size > 0


def test_tm3_localized_year_notation_preserves_the_same_year_literal() -> None:
    """Y2024 may become 2024年, but a different or unmarked year must still fail."""

    from transflow.toolboxes.leaves.body_chart.toolbox import (
        _validate_translations,
    )

    container = SimpleNamespace(
        container_id="year-label",
        association_id="chart",
        required_literals=("Y2024",),
        source_text="VS Y2024",
    )

    accepted = _validate_translations(
        "plan",
        (container,),
        {"year-label": "较2024年"},
        "zh-CN",
    )
    wrong_year = _validate_translations(
        "plan",
        (container,),
        {"year-label": "较2023年"},
        "zh-CN",
    )
    unmarked_year = _validate_translations(
        "plan",
        (container,),
        {"year-label": "较2024"},
        "zh-CN",
    )

    assert accepted == ()
    assert [item.code for item in wrong_year] == [
        "TRANSLATION_REQUIRED_LITERAL_MISSING"
    ]
    assert [item.code for item in unmarked_year] == [
        "TRANSLATION_REQUIRED_LITERAL_MISSING"
    ]


def test_tm3_left_table_label_uses_its_semantic_row_width_first() -> None:
    """Expanded labels use safe row width, without widening numeric or prose slots."""

    from transflow.toolboxes.leaves.body_chart.layout import _slot_profiles

    label = SimpleNamespace(
        role="TABLE_CELL",
        alignment="LEFT",
        source_bbox=(10.0, 10.0, 35.0, 20.0),
        allowed_bbox=(10.0, 10.0, 100.0, 32.0),
    )
    numeric = SimpleNamespace(**{**vars(label), "alignment": "RIGHT"})
    prose = SimpleNamespace(**{**vars(label), "role": "ANNOTATION"})

    label_slots = _slot_profiles(label, 9.0)
    numeric_slots = _slot_profiles(numeric, 9.0)
    prose_slots = _slot_profiles(prose, 9.0)

    assert label_slots[0] == (
        "safe-horizontal",
        (10.0, 10.0, 100.0, 23.05),
    )
    assert numeric_slots[0][0] == "source-box"
    assert prose_slots[0][0] == "source-box"


def test_tm3_kept_numeric_prefix_uses_natural_english_postfix() -> None:
    """A locked row number keeps its anchor and receives a natural suffix."""

    from transflow.toolboxes.leaves.body_chart.models import ChartTextContainer
    from transflow.toolboxes.leaves.body_chart.toolbox import (
        _normalize_kept_numeric_prefix_translation,
        _translation_projection,
    )

    container = ChartTextContainer(
        container_id="age-row",
        role="TABLE_CELL",
        association_id="table",
        source_object_ids=("number", "suffix"),
        semantic_object_id="number",
        source_text="30 \u5c81\u4ee5\u4e0b",
        source_bbox=(10.0, 10.0, 35.0, 20.0),
        allowed_bbox=(10.0, 10.0, 100.0, 32.0),
        anchor_object_ids=(),
        anchor_relation="INSIDE",
        reading_order=0,
        required_literals=("30",),
        font_name="NotoSans",
        font_size=9.0,
        color_srgb=0,
        alignment="LEFT",
    )
    facts = SimpleNamespace(
        text_spans=(
            SimpleNamespace(
                object_id="number",
                text="30",
                bbox=(10.0, 10.0, 20.0, 20.0),
            ),
            SimpleNamespace(
                object_id="suffix",
                text="\u5c81\u4ee5\u4e0b",
                bbox=(20.0, 10.0, 35.0, 20.0),
            ),
        )
    )
    inventory = {
        "number": SimpleNamespace(
            disposition=InventoryDisposition.KEEP_SOURCE
        ),
        "suffix": SimpleNamespace(
            disposition=InventoryDisposition.TRANSLATE
        ),
    }

    table_projection = _translation_projection(
        container,
        facts,
        inventory,
        "en",
    )
    prose_projection = _translation_projection(
        replace(container, role="ANNOTATION"),
        facts,
        inventory,
        "en",
    )

    assert table_projection is not None
    assert table_projection.source_object_ids == ("suffix",)
    assert table_projection.source_text == "岁以下"
    assert table_projection.source_bbox == (20.0, 10.0, 35.0, 20.0)
    assert table_projection.allowed_bbox[0] == pytest.approx(23.15)
    assert table_projection.allowed_bbox[2] == 100.0
    assert (
        _normalize_kept_numeric_prefix_translation("Under", "30", "en")
        == "or below"
    )
    assert (
        _normalize_kept_numeric_prefix_translation("Over", "50", "en")
        == "or above"
    )
    assert (
        _normalize_kept_numeric_prefix_translation("Years", "30-50", "en")
        == "years"
    )
    assert (
        _normalize_kept_numeric_prefix_translation("Under", None, "en")
        == "Under"
    )
    assert prose_projection is not None
    assert prose_projection.source_object_ids == ("suffix",)
    assert prose_projection.allowed_bbox[0] == 20.0


def test_tm3_materialized_chart_gate_uses_actual_glyphs_and_anchor(
    tmp_path: Path,
) -> None:
    """A generated PDF is not PASS until its real glyphs retain their anchors."""

    from scripts.run_tm3_chart_pool_regression import _materialized_layout_gate

    source = _chart_source_pdf(tmp_path / "chart.pdf")
    output = tmp_path / "chart-translated.pdf"
    page = _facts(source)
    chart_template = build_chart_template(page.facts)
    toolbox = _toolbox()
    template = toolbox.prepare(page.context, page.facts)
    batch = toolbox.build_translation_request(template)
    assert batch is not None
    translated = {
        unit.unit_id: {
            "ANNUAL REPORT": "年度报告",
            "Revenue by Segment": "分部收入",
            "Technology": "科技",
            "Asia Pacific": "亚太地区",
            "Europe": "欧洲",
        }[unit.source_text]
        for unit in batch.units
    }
    result = ToolboxPageCoordinator(
        FixedTranslationAdapter(translated)
    ).execute(ToolboxPageWork(page.context, page.facts, toolbox))
    assert result.patch is not None
    fonts = ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT)
    with pymupdf.open(source) as document:
        PagePatchInterpreter(fonts).apply(
            document,
            page.context,
            page.facts,
            result.patch,
            "body.chart",
        )
        document.save(output)

    gate = _materialized_layout_gate(
        output,
        chart_template,
        page.facts,
        result.patch,
    )

    assert gate["materialized_operation_count"] == len(result.patch.operations)
    assert gate["missing_operation_ids"] == []
    assert gate["horizontal_anchor_failures"] == []
    assert gate["actual_font_failures"] == []
    assert gate["line_spacing_failures"] == []
    assert gate["translated_glyph_collision_pairs"] == []
    assert gate["passed"] is True

    first = chart_template.containers[0]
    shifted_template = replace(
        chart_template,
        containers=(
            replace(
                first,
                source_bbox=(
                    first.source_bbox[0] - 24.0,
                    first.source_bbox[1],
                    first.source_bbox[2] - 24.0,
                    first.source_bbox[3],
                ),
            ),
            *chart_template.containers[1:],
        ),
    )
    protected_suffix_geometry = _materialized_layout_gate(
        output,
        shifted_template,
        page.facts,
        result.patch,
    )

    assert protected_suffix_geometry["horizontal_anchor_failures"] == []
    assert protected_suffix_geometry["passed"] is True

    reordered_facts = replace(
        page.facts,
        drawing_objects=tuple(reversed(page.facts.drawing_objects)),
        locked_objects_hash="0" * 64,
    )
    reordered_visuals = _materialized_layout_gate(
        output,
        chart_template,
        reordered_facts,
        result.patch,
    )
    assert reordered_visuals["locked_objects_changed"] is False
    assert reordered_visuals["passed"] is True

    redetected_tables = _materialized_layout_gate(
        output,
        chart_template,
        replace(
            page.facts,
            table_objects=(
                SimpleNamespace(
                    bbox=(10.0, 10.0, 40.0, 30.0),
                    cell_bboxes=((10.0, 10.0, 40.0, 30.0),),
                ),
            ),
        ),
        result.patch,
    )
    assert redetected_tables["locked_objects_changed"] is False
    assert redetected_tables["passed"] is True

    changed_drawing = replace(
        reordered_facts.drawing_objects[0],
        content_hash="f" * 64,
    )
    changed_visuals = _materialized_layout_gate(
        output,
        chart_template,
        replace(
            reordered_facts,
            drawing_objects=(
                changed_drawing,
                *reordered_facts.drawing_objects[1:],
            ),
        ),
        result.patch,
    )
    assert changed_visuals["locked_objects_changed"] is True
    assert changed_visuals["passed"] is False

    first_operation = result.patch.operations[0]
    shifted_patch = replace(
        result.patch,
        operations=(
            replace(
                first_operation,
                rect=(
                    first_operation.rect[0] + 24.0,
                    first_operation.rect[1],
                    first_operation.rect[2],
                    first_operation.rect[3],
                ),
            ),
            *result.patch.operations[1:],
        ),
    )
    drift = _materialized_layout_gate(
        output,
        chart_template,
        page.facts,
        shifted_patch,
    )

    assert first_operation.operation_id in {
        *drift["horizontal_anchor_failures"],
        *drift["missing_operation_ids"],
    }
    assert drift["passed"] is False


def test_tm3_materialized_match_uses_geometry_for_duplicate_labels() -> None:
    """Compatibility dashes and repeated labels must still bind to the right row."""

    from scripts.run_tm3_chart_pool_regression import (
        _matching_materialized_lines,
    )

    lines = (
        {
            "bbox": (10.0, 10.0, 60.0, 20.0),
            "font_size": 8.0,
            "text": "North\u2011South",
        },
        {
            "bbox": (10.0, 30.0, 60.0, 40.0),
            "font_size": 8.0,
            "text": "North\u2011South",
        },
    )

    matched = _matching_materialized_lines(
        lines,
        "North-South",
        (10.0, 30.0, 80.0, 42.0),
    )

    assert matched == (lines[1],)

    bullet_line = {
        "bbox": (10.0, 50.0, 160.0, 60.0),
        "font_size": 8.0,
        "text": "COMPANY ・ 2025年年度报告",
    }
    assert _matching_materialized_lines(
        (bullet_line,),
        "COMPANY • 2025年年度报告",
        (10.0, 50.0, 160.0, 60.0),
    ) == (bullet_line,)


def test_tm3_existing_image_overlap_is_preserved_but_cannot_expand() -> None:
    """A source-associated visual is allowed only at its existing overlap depth."""

    from transflow.pdf_kernel.patch import _preserves_source_image_overlap

    image = (0.0, 0.0, 45.0, 100.0)
    source_rects = ((40.0, 20.0, 100.0, 30.0),)
    same_left_anchor = (40.0, 21.0, 120.0, 29.0)
    expanded_into_image = (30.0, 21.0, 120.0, 29.0)
    unrelated_source = ((50.0, 20.0, 100.0, 30.0),)

    assert _preserves_source_image_overlap(
        same_left_anchor,
        source_rects,
        image,
    )
    assert not _preserves_source_image_overlap(
        expanded_into_image,
        source_rects,
        image,
    )
    assert not _preserves_source_image_overlap(
        same_left_anchor,
        unrelated_source,
        image,
    )


def test_tm3_duplicate_native_aliases_share_one_redaction_rect() -> None:
    """Coincident extraction aliases stay semantic evidence, but erase only once."""

    from transflow.toolboxes.leaves.body_chart.toolbox import (
        _patch_target_object_ids,
    )

    page = _facts(_chart_pool_source("CH_EN_01978_p071"))
    template = build_chart_template(page.facts)
    bbox_by_id = {
        item.object_id: item.bbox for item in page.facts.text_spans
    }
    aliased = [
        item
        for item in template.containers
        if len(
            {
                bbox_by_id[object_id]
                for object_id in item.source_object_ids
            }
        )
        < len(item.source_object_ids)
    ]

    assert aliased
    for container in aliased:
        targets = _patch_target_object_ids(
            container.source_object_ids,
            bbox_by_id,
        )
        assert set(targets) <= set(container.source_object_ids)
        assert len(
            {bbox_by_id[object_id] for object_id in targets}
        ) == len(targets)


def test_tm3_real_chart_mixed_literals_keep_total_semantic_ownership() -> None:
    """A kept literal beside translatable text must not orphan that text."""

    manifest = json.loads(
        (
            REPO_ROOT
            / "resources/manifests/toolbox_leaf_migration/chart.json"
        ).read_text(encoding="utf-8")
    )
    source = REPO_ROOT / manifest["source_document"]["path"]
    source_hash = manifest["source_document"]["sha256"]
    facts = PageFactsExtractor().extract_page(source, source_hash, 48)
    context = PageExecutionContext(
        job_id="job-tm3-mixed-literal-test",
        run_id="tm3-mixed-literal-test",
        source_hash=source_hash,
        page_no=48,
        geometry_hash=facts.page.geometry_hash,
        config_snapshot_hash="c" * 64,
    )
    inventory = freeze_page_text_inventory(facts)
    disposition_by_id = {
        item.object_id: item.disposition for item in inventory.items
    }
    chart_template = build_chart_template(facts)
    mixed_containers = tuple(
        container
        for container in chart_template.containers
        if {
            disposition_by_id[object_id]
            for object_id in container.source_object_ids
        }
        == {
            InventoryDisposition.TRANSLATE,
            InventoryDisposition.KEEP_SOURCE,
        }
    )
    assert {container.source_text for container in mixed_containers} >= {
        "Total *",
        (
            "* Total revenue and other income is not presented in the same scale "
            "as segmental results, but is proportionately resized."
        ),
    }

    toolbox = _toolbox()
    template = toolbox.prepare(context, facts)
    batch = toolbox.build_translation_request(template)
    assert batch is not None
    requested_object_ids = {
        object_id
        for unit in batch.units
        for object_id in unit.source_object_ids
    }
    for container in mixed_containers:
        assert {
            object_id
            for object_id in container.source_object_ids
            if disposition_by_id[object_id] is InventoryDisposition.TRANSLATE
        } <= requested_object_ids
        assert not {
            object_id
            for object_id in container.source_object_ids
            if disposition_by_id[object_id] is InventoryDisposition.KEEP_SOURCE
        }.intersection(requested_object_ids)

    semantic_map = build_semantic_unit_map(template, batch, facts, inventory)
    assert semantic_map.unresolved_unit_ids == ()


def test_tm3_chart_projection_preserves_native_text_hash_whitespace() -> None:
    """A chart unit must retain exact native whitespace through semantic mapping."""

    page = _facts(_chart_pool_source("CH_EN_00050_p215"))
    toolbox = _toolbox()
    template = toolbox.prepare(page.context, page.facts)
    batch = toolbox.build_translation_request(template)
    inventory = freeze_page_text_inventory(
        page.facts,
        target_language="zh-CN",
    )

    assert batch is not None
    semantic_map = build_semantic_unit_map(
        template,
        batch,
        page.facts,
        inventory,
    )

    native_text_by_id = {
        item.object_id: item.text for item in page.facts.text_spans
    }
    assert all(
        entry.source_hash
        == hashlib.sha256(
            native_text_by_id[entry.source_object_ids[0]].encode("utf-8")
        ).hexdigest()
        for entry in semantic_map.entries
        if len(entry.source_object_ids) == 1
    )


def test_tm3_runner_reports_route_mismatch_before_bundle_identity() -> None:
    """The acceptance runner must preserve the first failing contract."""

    from scripts.toolbox_leaf_migration_chart_run import (
        _require_full_bundle_identity,
    )

    execution = SimpleNamespace(
        translation_bundle=None,
        route_capability_mismatch=SimpleNamespace(
            reason_code="semantic_unit_owner_unresolved"
        ),
    )
    with pytest.raises(MigrationContractError) as captured:
        _require_full_bundle_identity(execution, object(), 48)
    assert captured.value.code == "TM3_ROUTE_CAPABILITY_MISMATCH"
    assert captured.value.detail == "48:semantic_unit_owner_unresolved"


def test_tm3_required_number_literals_exclude_sentence_punctuation() -> None:
    """A sentence comma after a year is grammar, not a preserved literal."""

    source = (
        "As at 31 December 2025, the Company had 1,334 employees."
    )

    assert extract_required_literals(source) == ("31", "2025", "1,334")


def test_tm3_currency_units_are_keep_source_not_unresolved_chart_text() -> None:
    """Country-prefixed currency units are mechanics, not orphan translations."""

    page = _facts(_chart_pool_source("CH_EN_00050_p011"))
    inventory = freeze_page_text_inventory(
        page.facts,
        target_language="zh-CN",
    )
    native_text_by_id = {
        item.object_id: item.text for item in page.facts.text_spans
    }
    currency_items = [
        item
        for item in inventory.items
        if native_text_by_id[item.object_id] in {"HK$M", "HK$"}
    ]

    assert currency_items
    assert all(
        item.disposition is InventoryDisposition.KEEP_SOURCE
        for item in currency_items
    )
    toolbox = _toolbox()
    template = toolbox.prepare(page.context, page.facts)
    batch = toolbox.build_translation_request(template)
    assert batch is not None
    semantic_map = build_semantic_unit_map(
        template,
        batch,
        page.facts,
        inventory,
    )
    assert semantic_map.unresolved_unit_ids == ()


def test_tm3_chart_numeric_suffix_units_remain_mechanical_inventory() -> None:
    """A short unit is mechanical only inside one native numeric line."""

    page = _facts(_chart_pool_source("CH_EN_01717_p007"))
    inventory = freeze_page_text_inventory(
        page.facts,
        target_language="zh-CN",
    )
    inventory_by_id = {
        item.object_id: item for item in inventory.items
    }
    spans_by_text = {}
    for span in page.facts.text_spans:
        spans_by_text.setdefault(span.text.strip(), []).append(span)

    assert all(
        inventory_by_id[item.object_id].disposition
        is InventoryDisposition.KEEP_SOURCE
        for item in spans_by_text["(RMB’M)"]
    )
    assert all(
        inventory_by_id[item.object_id].disposition
        is InventoryDisposition.KEEP_SOURCE
        for item in spans_by_text["pps"]
    )
    toolbox = _toolbox()
    template = toolbox.prepare(page.context, page.facts)
    batch = toolbox.build_translation_request(template)
    assert batch is not None
    semantic_map = build_semantic_unit_map(
        template,
        batch,
        page.facts,
        inventory,
    )
    assert semantic_map.unresolved_unit_ids == ()


def test_tm3_currency_amount_literal_keeps_number_not_prefix_fragment() -> None:
    """RMB86,640 is one amount; the translated currency order may legitimately move."""

    template = build_chart_template(
        _facts(_chart_pool_source("CH_EN_00405_p152")).facts
    )
    container = next(
        item
        for item in template.containers
        if "RMB86,640" in item.source_text
    )

    assert "86,640" in container.required_literals
    assert "56,878" in container.required_literals
    assert "RMB86" not in container.required_literals
    assert "RMB56" not in container.required_literals
    decimal_container = next(
        item
        for item in build_chart_template(
            _facts(_chart_pool_source("CH_EN_00405_p153")).facts
        ).containers
        if "RMB949.9" in item.source_text
    )
    assert "949.9" in decimal_container.required_literals
    assert "RMB949.9" not in decimal_container.required_literals


def test_tm3_short_axis_label_recovers_height_only_when_space_is_safe() -> None:
    """A glyph bbox is not a textbox cage, but recovery cannot consume a next row."""

    from transflow.toolboxes.leaves.body_chart.template import (
        _restore_minimum_textbox_heights,
    )

    template = build_chart_template(
        _facts(_chart_pool_source("CH_EN_00405_p153")).facts
    )
    label = next(
        item
        for item in template.containers
        if item.source_text == "Retail sales growth (RHS)"
    )
    assert label.allowed_bbox[3] - label.allowed_bbox[1] >= (
        label.font_size * 1.40
    )

    short = replace(
        label,
        container_id="short-label",
        source_bbox=(10.0, 10.0, 60.0, 15.0),
        allowed_bbox=(10.0, 10.0, 60.0, 15.0),
        font_size=6.0,
    )
    next_row = replace(
        label,
        container_id="next-row",
        source_bbox=(10.0, 15.5, 60.0, 22.0),
        allowed_bbox=(10.0, 15.5, 60.0, 22.0),
        font_size=6.0,
    )
    restored = _restore_minimum_textbox_heights(
        [short, next_row],
        (),
        100.0,
    )
    assert restored[0].allowed_bbox == short.allowed_bbox


def test_tm3_runtime_layout_gate_rejects_row_drift_and_global_font_collapse() -> None:
    """Programmatic PASS must reject both semantic-row drift and global 6pt text."""

    from scripts.run_tm3_chart_pool_regression import _layout_gate

    template = SimpleNamespace(
        containers=(
            SimpleNamespace(
                container_id="row-1",
                role="TABLE_CELL",
                source_text="Employees",
                source_bbox=(10.0, 10.0, 90.0, 20.0),
                allowed_bbox=(10.0, 10.0, 190.0, 20.0),
                source_object_ids=("text-1",),
            ),
            SimpleNamespace(
                container_id="row-2",
                role="TABLE_CELL",
                source_text="1,334",
                source_bbox=(150.0, 30.0, 190.0, 40.0),
                allowed_bbox=(10.0, 30.0, 190.0, 40.0),
                source_object_ids=("text-2",),
            ),
        )
    )
    patch = SimpleNamespace(
        operations=(
            SimpleNamespace(
                operation_id="operation-1",
                target_object_ids=("text-1",),
                font_size=6.0,
                rect=(10.0, 10.0, 190.0, 20.0),
            ),
            SimpleNamespace(
                operation_id="operation-2",
                target_object_ids=("text-2",),
                font_size=6.0,
                rect=(10.0, 10.0, 190.0, 20.0),
            ),
        )
    )
    facts = SimpleNamespace(
        text_spans=(
            SimpleNamespace(object_id="text-1", font_size=8.5),
            SimpleNamespace(object_id="text-2", font_size=8.5),
        )
    )

    result = _layout_gate(template, patch, facts, minimum_font_size=6.0)

    assert result["table_row_binding_failures"] == ["operation-2"]
    assert result["global_minimum_font_degradation"] is True


def test_tm3_long_translation_never_silently_accepts_overflow(tmp_path: Path) -> None:
    """A non-fitting chart label must repair within bounds or fall back."""

    source = _chart_source_pdf(tmp_path / "chart.pdf")
    page = _facts(source)
    toolbox = _toolbox()
    template = toolbox.prepare(page.context, page.facts)
    batch = toolbox.build_translation_request(template)
    assert batch is not None
    long_text = "这是一个用于验证图表标签安全区域和有界修复行为的超长译文" * 20
    result = ToolboxPageCoordinator(
        FixedTranslationAdapter(
            {
                unit.unit_id: f"{long_text}{'甲乙丙丁'[index]}"
                if index < 4
                else f"{long_text}戊"
                for index, unit in enumerate(batch.units)
            }
        )
    ).execute(ToolboxPageWork(page.context, page.facts, toolbox))

    if result.patch is None:
        assert result.outcome.fallback.value == "PAGE_PASSTHROUGH"
        assert {
            "CHART_TEXT_SLOT_OVERFLOW",
            "TEXT_LAYOUT_OVERFLOW",
            "CHART_REPAIR_EXHAUSTED",
        }.intersection(result.outcome.finding_codes)
    else:
        assert result.outcome.quality.value == "PASS"
        assert result.repair_attempt_count <= load_p8_toolbox_policy(
            POLICY_PATH
        ).repair_limit
        assert all(
            operation.font_size
            >= load_p8_toolbox_policy(POLICY_PATH).minimum_font_size
            for operation in result.patch.operations
            if operation.font_size is not None
        )


def test_tm3_one_overflow_cannot_globally_shrink_unrelated_text(
    tmp_path: Path,
) -> None:
    """One unfit label may repair only itself, never the whole page."""

    source = _chart_source_pdf(tmp_path / "chart.pdf")
    page = _facts(source)
    toolbox = _toolbox()
    template = toolbox.prepare(page.context, page.facts)
    batch = toolbox.build_translation_request(template)
    assert batch is not None
    translations = {
        unit.unit_id: (
            "\u8d85\u957f\u6587\u672c\u5185\u5bb9" * 3
            if index == 2
            else f"\u6709\u6548\u5185\u5bb9{index}"
        )
        for index, unit in enumerate(batch.units)
    }

    result = ToolboxPageCoordinator(
        FixedTranslationAdapter(translations)
    ).execute(ToolboxPageWork(page.context, page.facts, toolbox))

    assert result.patch is not None
    assert result.outcome.quality.value == "PASS"
    minimum = load_p8_toolbox_policy(POLICY_PATH).minimum_font_size
    assert result.patch.operations[2].font_size == minimum
    assert all(
        operation.font_size is not None and operation.font_size > minimum
        for index, operation in enumerate(result.patch.operations)
        if index != 2
    )


def test_tm3_registration_is_explicit_and_catalog_overlay_is_private() -> None:
    """TM3 registers one driver but leaves the repository catalog unchanged."""

    catalog_bytes = CATALOG_PATH.read_bytes()
    catalog = json.loads(catalog_bytes)
    overlay = build_chart_catalog_overlay(catalog)
    before = {item["route"]: item for item in catalog["entries"]}
    after = {item["route"]: item for item in overlay["entries"]}
    changed = [route for route in before if before[route] != after[route]]

    assert "body.chart" in DRIVER_FACTORIES
    assert changed == ["body.chart"]
    assert before["body.chart"]["enabled"] is False
    assert after["body.chart"]["enabled"] is True
    assert CATALOG_PATH.read_bytes() == catalog_bytes


def test_tm3_full_document_regression_executes_all_accepted_text_routes() -> None:
    """TM3 full-document evidence must not pass through the accepted single leaf."""

    from scripts.toolbox_leaf_migration_chart_run import (
        _translation_prompt_for_route,
    )

    chart_prompt = _translation_prompt_for_route("body.chart")
    single_prompt = _translation_prompt_for_route("body.flow_text.single")

    assert chart_prompt is not None
    assert single_prompt is not None
    assert chart_prompt != single_prompt
    assert _translation_prompt_for_route("visual_only") is None
    assert _translation_prompt_for_route("body.flow_text.multi") is None


def test_tm3_round06_freezes_two_distinct_complete_pdf_inputs() -> None:
    """Round06 must use two immutable full PDFs that naturally contain chart pages."""

    manifests = (
        REPO_ROOT
        / "resources/manifests/toolbox_leaf_migration/chart_01717_full.json",
        REPO_ROOT
        / "resources/manifests/toolbox_leaf_migration/chart_03337_full.json",
    )
    loaded = tuple(
        load_leaf_input_manifest(path, stage="TM3", route="body.chart")
        for path in manifests
    )

    assert {item.page_count for item in loaded} == {167}
    assert {item.page_no for item in loaded} == {7, 8}
    assert len({item.source_hash for item in loaded}) == 2
    assert all(item.source_language == "en" for item in loaded)
    assert all(item.target_language == "zh-CN" for item in loaded)


def test_tm3_chart_pool_freezes_30_real_bidirectional_cases() -> None:
    """The chart pool must translate every page in its declared direction."""

    from scripts.run_tm3_chart_pool_regression import _load_cases

    cases = _load_cases()
    directions = [
        (item["source_language"], item["target_language"]) for item in cases
    ]

    assert len(cases) == 30
    assert directions.count(("en", "zh-CN")) == 15
    assert directions.count(("zh-CN", "en")) == 15


def test_tm3_chinese_chart_inventory_uses_declared_english_target() -> None:
    """Chinese chart text must not be frozen as target text for zh-CN->en."""

    facts = _facts(_chart_pool_source("CH_ZH_00050_p011")).facts
    inventory = freeze_page_text_inventory(facts, target_language="en")
    translated_ids = {
        item.object_id
        for item in inventory.items
        if item.disposition is InventoryDisposition.TRANSLATE
    }
    policy = replace(
        load_p8_toolbox_policy(POLICY_PATH),
        source_language="zh-CN",
        target_language="en",
    )
    fonts = ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT)
    toolbox = ChartToolbox(policy, fonts.resolve(FONT_ID).path)
    page = _facts(_chart_pool_source("CH_ZH_00050_p011"))
    template = toolbox.prepare(page.context, page.facts)
    batch = toolbox.build_translation_request(template)

    assert translated_ids
    assert any(
        any("\u3400" <= character <= "\u9fff" for character in item.text)
        for item in facts.text_spans
        if item.object_id in translated_ids
    )
    assert batch is not None
    assert batch.source_language == "zh-CN"
    assert batch.target_language == "en"


def test_tm3_chart_pool_creates_case_input_directory(tmp_path: Path) -> None:
    """A new case must create its nested input directory before copying."""

    from scripts.run_tm3_chart_pool_regression import _copy_case_input

    source = tmp_path / "source.pdf"
    source.write_bytes(b"pdf-placeholder")
    target = tmp_path / "case/input/source.pdf"

    _copy_case_input(source, target)

    assert target.read_bytes() == source.read_bytes()


def test_tm3_failed_case_still_produces_reviewable_pdf(tmp_path: Path) -> None:
    """A FAIL without a usable translation must retain an explicit source fallback."""

    from scripts.run_tm3_chart_pool_regression import _ensure_failure_artifacts

    source = _chart_source_pdf(tmp_path / "source.pdf")
    run_root = tmp_path / "run"
    case_root = run_root / "cases/01-test"

    artifact = _ensure_failure_artifacts(
        case_root=case_root,
        source=source,
        run_root=run_root,
        sample_id="test",
        error=MigrationContractError("TEST_FAILURE", "test"),
    )

    output = case_root / "output/transflow.pdf"
    review = case_root / "review/source_vs_transflow.png"
    process = json.loads(
        (case_root / "process/failure_manifest.json").read_text(encoding="utf-8")
    )
    assert output.is_file() and review.is_file()
    assert _sha256_file(output) == _sha256_file(source)
    assert artifact["artifact_mode"] == "SOURCE_FALLBACK"
    assert process["status"] == "FAIL"
    assert process["product_acceptance"] is False
    assert process["artifact_mode"] == "SOURCE_FALLBACK"


def test_tm3_classification_chart_pool_maps_one_to_one_to_spike_toolbox() -> None:
    """The authoritative classification PDFs and chart samples are identical."""

    chart_root = (
        REPO_ROOT
        / "spikes/page_toolbox_engine_puncture_v1/toolboxes/body/chart"
    )
    manifest = chart_root / "samples/manifest.jsonl"
    records = [
        json.loads(line)
        for line in manifest.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(records) == 30
    for record in records:
        toolbox_pdf = chart_root / record["source_ref"]
        classified_pdf = REPO_ROOT / "spikes" / record["upstream_ref"]
        assert toolbox_pdf.is_file() and classified_pdf.is_file()
        assert _sha256_file(toolbox_pdf) == record["sha256"]
        assert _sha256_file(classified_pdf) == record["sha256"]
        facts = PageFactsExtractor().extract_page(
            classified_pdf,
            record["sha256"],
            1,
        )
        template = build_chart_template(facts)
        owned_ids = [
            *(
                object_id
                for container in template.containers
                for object_id in container.source_object_ids
            ),
            *template.protected_object_ids,
        ]
        assert sorted(owned_ids) == sorted(
            item.object_id for item in facts.text_spans
        )
        assert len(owned_ids) == len(set(owned_ids))


def test_tm3_production_code_has_no_spike_runtime_dependency() -> None:
    """Lift-and-Wrap copies the core behind production DTOs; it never imports Spike."""

    violations = _forbidden_production_dependencies()
    assert violations == ()
    driver_source = (
        REPO_ROOT / "scripts/toolbox_leaf_migration_chart.py"
    ).read_text(encoding="utf-8")
    assert "spikes." not in driver_source
    assert "tests." not in driver_source


def test_tm3_rejected_chart_patch_cannot_hide_unmaterialized_formula(
    tmp_path: Path,
) -> None:
    """A rejected candidate must fall through to a readable translated diagnostic."""

    from scripts.run_tm3_chart_pool_regression import (
        _materialized_layout_gate,
        _write_patch_pdf,
    )
    from transflow.domain.translation import TranslatedUnit, TranslationBundle
    from transflow.toolboxes.leaves.body_chart.toolbox import (
        _restore_diagnostic_source_geometry,
    )

    source = _chart_pool_source("CH_ZH_02131_p123")
    page = _facts(source)
    fonts = ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT)
    policy = replace(
        load_p8_toolbox_policy(POLICY_PATH),
        source_language="zh-CN",
        target_language="en",
    )
    toolbox = ChartToolbox(policy, fonts.resolve(FONT_ID).path)
    template = toolbox.prepare(page.context, page.facts)
    batch = toolbox.build_translation_request(template)
    assert batch is not None
    requested = toolbox._snapshots[template.template_id].requested_containers
    translations = {
        unit.unit_id: (
            {
                22: "Under",
                23: "Years",
                24: "Over",
                28: (
                    "Employee Turnover Rate = Number of Leavers in the Category "
                    "(excluding part-time, interns and contract staff) / Total "
                    "Number of Employees at the End of the Year in the Category "
                    "(excluding part-time, interns and contract staff)"
                ),
            }.get(
                unit.ordinal,
                " ".join(
                    (
                        f"English label {unit.ordinal}",
                        *container.required_literals,
                    )
                ),
            )
        )
        for unit, container in zip(batch.units, requested, strict=True)
    }
    bundle = TranslationBundle.from_batch(
        batch,
        tuple(
            TranslatedUnit(unit.unit_id, translations[unit.unit_id])
            for unit in batch.units
        ),
    )
    result = ToolboxPageCoordinator(
        FixedTranslationAdapter(translations)
    ).execute(
        ToolboxPageWork(
            page.context,
            page.facts,
            toolbox,
            target_language="en",
        )
    )
    assert result.patch is None
    assert result.proposed_patch is not None

    rejected_output = tmp_path / "rejected.pdf"
    with pytest.raises(
        MigrationContractError,
        match="TM3_CHART_PATCH_TEXT_NOT_MATERIALIZED",
    ):
        _write_patch_pdf(
            source,
            rejected_output,
            page,
            result.proposed_patch,
            PagePatchInterpreter(fonts),
        )
    assert not rejected_output.exists()

    snapshot = toolbox._snapshots[template.template_id]
    diagnostic_template = _restore_diagnostic_source_geometry(snapshot)
    diagnostic_by_id = {
        container.container_id: container
        for container in diagnostic_template.containers
    }
    projected_by_id = {
        container.container_id: container
        for container in snapshot.template.containers
    }
    original_by_id = {
        container.container_id: container
        for container in build_chart_template(page.facts).containers
    }
    assert (
        diagnostic_by_id["chart-text-028"].allowed_bbox
        == original_by_id["chart-text-028"].allowed_bbox
    )
    assert (
        diagnostic_by_id["chart-text-022"].allowed_bbox
        == projected_by_id["chart-text-022"].allowed_bbox
    )

    diagnostic_patch, _ = toolbox.build_diagnostic_patch(
        template,
        batch,
        bundle,
    )
    assert diagnostic_patch is not None
    output = tmp_path / "translated-diagnostic.pdf"
    _write_patch_pdf(
        source,
        output,
        page,
        diagnostic_patch,
        PagePatchInterpreter(fonts),
        diagnostic=True,
    )

    with pymupdf.open(output) as document:
        spans = [
            span
            for block in document[0].get_text("dict").get("blocks", [])
            for line in block.get("lines", [])
            for span in line.get("spans", [])
            if str(span.get("text", "")).strip()
        ]
    normalized_text = " ".join(
        str(span["text"]).replace("\xa0", " ").replace("\u2011", "-")
        for span in spans
    )
    assert "Number of Leavers in the Category" in normalized_text
    formula_spans = [
        span
        for span in spans
        if "Employee" in str(span["text"]) and float(span["bbox"][1]) > 700
    ]
    assert formula_spans
    assert min(float(span["size"]) for span in formula_spans) >= 6.75
    assert min(float(span["bbox"][1]) for span in formula_spans) >= 710.0

    age_number = next(
        span
        for span in spans
        if str(span["text"]).strip() == "30"
        and 560.0 <= float(span["bbox"][1]) <= 580.0
        and float(span["bbox"][0]) < 100.0
    )
    age_suffix = next(
        span
        for span in spans
        if str(span["text"]).replace("\xa0", " ").strip() == "or below"
    )
    assert abs(
        float(age_number["origin"][1]) - float(age_suffix["origin"][1])
    ) <= 0.9

    gate = _materialized_layout_gate(
        output,
        build_chart_template(page.facts),
        page.facts,
        diagnostic_patch,
    )
    assert gate["semantic_row_baseline_failures"] == []
    formula_operation = diagnostic_patch.operations[28]
    assert formula_operation.operation_id not in gate["missing_operation_ids"]
