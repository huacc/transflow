from __future__ import annotations

import re
from pathlib import Path

import fitz
import pytest

from page_classifier.config import NODE_CHOICES
from page_classifier.engine import ClassificationEngine
from page_classifier.evidence import build_evidence
from page_classifier.io_utils import read_jsonl, sha256_file
from page_classifier.models import NodeJudgement
from page_classifier.provider import parse_json_content
from page_classifier.qwen import validate_judgement
from page_classifier.resolver import resolve_node
from page_classifier.rules import decide_composite_kind, decide_flow_topology, decide_layout_owner, decide_page_role


ROOT = Path(__file__).resolve().parents[1]
LEGACY_SAMPLE_ROOT = ROOT.parent / "page_classification_dual_qwen_puncture" / "样本"


def require_local_pdf(path: Path) -> None:
    if not path.exists():
        pytest.skip("本地 PDF 测试样本未发布到 GitHub")


def judgement(node: str, source: str, status: str, child: str | None) -> NodeJudgement:
    return NodeJudgement(node, source, status, child, 0.9 if child else 0.0, ("IMG1",), "test")


def test_sample_manifest_and_pdf_set_are_aligned() -> None:
    if not (ROOT / "样本1").exists() or not LEGACY_SAMPLE_ROOT.exists():
        pytest.skip("本地 PDF 测试样本未发布到 GitHub")
    sources = read_jsonl(ROOT / "manifests" / "source_manifest.jsonl")
    gold = read_jsonl(ROOT / "manifests" / "gold_manifest.jsonl")
    pdfs = sorted((ROOT / "样本1").glob("*.pdf"))
    legacy_pdfs = sorted(LEGACY_SAMPLE_ROOT.glob("*.pdf"))
    assert len(pdfs) == len(sources) == len(gold) == len(legacy_pdfs) == 85
    assert [path.stem for path in pdfs] == [f"P{index:04d}" for index in range(1, len(pdfs) + 1)]
    source_by_id = {row["sample_id"]: row for row in sources}
    for path in pdfs:
        assert re.fullmatch(r"P\d{4}\.pdf", path.name)
        with fitz.open(path) as document:
            assert document.page_count == 1
        assert sha256_file(path) == source_by_id[path.stem]["sample_sha256"]


def test_parse_json_content_accepts_plain_and_fenced_json() -> None:
    assert parse_json_content('{"ok":true}') == {"ok": True}
    assert parse_json_content('```json\n{"ok": true}\n```') == {"ok": True}
    assert parse_json_content("not json") is None


def test_qwen_judgement_validation_rejects_unknown_evidence() -> None:
    value = {
        "node_key": "page.role",
        "status": "DECIDED",
        "selected_child": "body",
        "confidence": 0.9,
        "evidence_refs": ["UNKNOWN"],
        "reason_summary": "x",
    }
    result = validate_judgement(value, "page.role", {"IMG1"}, "QWEN_PRIMARY")
    assert result.status == "INCONCLUSIVE"


def test_resolver_direct_agreement_skips_review() -> None:
    called = False

    def review() -> NodeJudgement:
        nonlocal called
        called = True
        return judgement("page.role", "QWEN_REVIEW", "DECIDED", "cover")

    result = resolve_node(
        "page.role",
        judgement("page.role", "RULE", "DECIDED", "body"),
        judgement("page.role", "QWEN_PRIMARY", "DECIDED", "body"),
        review,
    )
    assert result.resolution == "DIRECT_AGREEMENT"
    assert result.final.selected_child == "body"
    assert not called


def test_resolver_uses_one_review_for_disagreement() -> None:
    result = resolve_node(
        "body.layout_owner",
        NodeJudgement("body.layout_owner", "RULE", "DECIDED", "table", 0.89, ("IMG1",), "weak rule"),
        judgement("body.layout_owner", "QWEN_PRIMARY", "DECIDED", "flow_text"),
        lambda: judgement("body.layout_owner", "QWEN_REVIEW", "DECIDED", "composite"),
    )
    assert result.resolution == "REVIEW_DECIDED"
    assert result.final.selected_child == "composite"


def test_contents_rule_requires_title_and_page_relationships() -> None:
    evidence = {
        "native_text": "CONTENTS\nCorporate Information 2\nChairman's Statement 5\nDirectors' Report 10\nFinancial Statements 80",
        "text": {"max_font_size": 20, "native_char_count": 110},
        "page": {"position": {"is_first": False, "is_last": False}},
        "tables": {"count": 0},
    }
    assert decide_page_role(evidence).selected_child == "contents"


def test_layout_rule_distinguishes_table_and_composite() -> None:
    table = {
        "text": {"native_char_count": 1000, "outside_table_chars": 100},
        "tables": {"count": 1, "area_ratio": 0.7},
        "images": {"area_ratio": 0.0},
    }
    composite = {
        "text": {"native_char_count": 1400, "outside_table_chars": 700},
        "tables": {"count": 1, "area_ratio": 0.3},
        "images": {"area_ratio": 0.0},
    }
    table["text"].update({"block_count": 20, "text_area_ratio": 0.6})
    composite["text"].update({"block_count": 20, "text_area_ratio": 0.6})
    assert decide_layout_owner(table).selected_child == "table"
    assert decide_layout_owner(composite).selected_child == "composite"


def test_layout_rule_keeps_local_text_on_fixed_collage_in_flow_text() -> None:
    evidence = {
        "text": {
            "native_char_count": 490,
            "outside_table_chars": 300,
            "block_count": 3,
            "text_area_ratio": 0.18,
        },
        "tables": {"count": 1, "area_ratio": 0.2},
        "images": {"area_ratio": 0.71},
    }
    assert decide_layout_owner(evidence).selected_child == "flow_text"


def test_borderless_glossary_rule_detects_real_table(tmp_path: Path) -> None:
    require_local_pdf(ROOT / "样本2" / "S2P0060.pdf")
    evidence = build_evidence(
        ROOT / "样本2" / "S2P0060.pdf",
        {"sample_id": "S2P0060", "source_page_number": 1, "source_page_count": 1},
        tmp_path / "S2P0060.png",
    )
    assert evidence["borderless_table"]["confidence"] >= 0.9
    result = decide_layout_owner(evidence)
    assert result.selected_child == "table"
    assert result.confidence >= 0.9


def test_borderless_table_with_substantial_prose_is_composite(tmp_path: Path) -> None:
    require_local_pdf(ROOT / "样本2" / "S2P0104.pdf")
    evidence = build_evidence(
        ROOT / "样本2" / "S2P0104.pdf",
        {"sample_id": "S2P0104", "source_page_number": 1, "source_page_count": 1},
        tmp_path / "S2P0104.png",
    )
    assert evidence["borderless_table"]["confidence"] >= 0.9
    result = decide_layout_owner(evidence)
    assert result.selected_child == "composite"
    assert result.confidence >= 0.9


def test_two_row_definition_fragment_does_not_trigger_hard_table_rule(tmp_path: Path) -> None:
    require_local_pdf(ROOT / "样本2" / "S2P0140.pdf")
    evidence = build_evidence(
        ROOT / "样本2" / "S2P0140.pdf",
        {"sample_id": "S2P0140", "source_page_number": 1, "source_page_count": 1},
        tmp_path / "S2P0140.png",
    )
    assert evidence["borderless_table"]["confidence"] < 0.9


def test_true_multi_column_prose_does_not_trigger_borderless_table_rule(tmp_path: Path) -> None:
    require_local_pdf(ROOT / "样本2" / "S2P0168.pdf")
    evidence = build_evidence(
        ROOT / "样本2" / "S2P0168.pdf",
        {"sample_id": "S2P0168", "source_page_number": 1, "source_page_count": 1},
        tmp_path / "S2P0168.png",
    )
    assert evidence["borderless_table"]["confidence"] < 0.9
    assert decide_layout_owner(evidence).selected_child == "flow_text"


def test_high_confidence_rule_overrides_qwen_without_review() -> None:
    called = False

    def review() -> NodeJudgement:
        nonlocal called
        called = True
        return judgement("body.layout_owner", "QWEN_REVIEW", "DECIDED", "flow_text")

    result = resolve_node(
        "body.layout_owner",
        NodeJudgement("body.layout_owner", "RULE", "DECIDED", "table", 0.94, ("TEXT1",), "stable columns"),
        NodeJudgement("body.layout_owner", "QWEN_PRIMARY", "DECIDED", "flow_text", 0.98, ("IMG1",), "looks like prose"),
        review,
    )
    assert result.resolution == "HIGH_CONFIDENCE_RULE"
    assert result.final.selected_child == "table"
    assert not called


def test_direct_table_evidence_skips_qwen_primary() -> None:
    direct = NodeJudgement(
        "body.layout_owner",
        "RULE",
        "DECIDED",
        "table",
        0.94,
        ("BTABLE1", "TEXT1"),
        "stable borderless table",
    )
    weak = NodeJudgement(
        "body.layout_owner",
        "RULE",
        "DECIDED",
        "table",
        0.89,
        ("BTABLE1", "TEXT1"),
        "weak borderless table",
    )
    assert ClassificationEngine._uses_direct_table_evidence(direct)
    assert not ClassificationEngine._uses_direct_table_evidence(weak)


def test_composite_kind_rule_detects_flow_text_table() -> None:
    evidence = {
        "text": {"outside_table_chars": 900, "text_area_ratio": 0.58},
        "tables": {"count": 1, "area_ratio": 0.11},
    }
    assert decide_composite_kind(evidence).selected_child == "flow_text_table"


def test_composite_kind_rule_leaves_card_chart_for_qwen() -> None:
    evidence = {
        "text": {"outside_table_chars": 1000, "text_area_ratio": 0.31},
        "tables": {"count": 1, "area_ratio": 0.11},
    }
    assert decide_composite_kind(evidence).status == "INCONCLUSIVE"


def test_node_choices_do_not_offer_freeform_to_qwen() -> None:
    assert all("freeform" not in choices for choices in NODE_CHOICES.values())


def test_review_uses_flow_and_card_contrast_exemplars() -> None:
    engine = ClassificationEngine.__new__(ClassificationEngine)
    assert engine._review_exemplar_labels("body.layout_owner", {"flow_text"}) == {"flow_text", "anchored_blocks"}
    assert engine._review_exemplar_labels("body.composite.kind", {"flow_text_table"}) == {
        "flow_text_table",
        "anchored_blocks_chart",
        "chart_table",
        "flow_text_chart",
        "flow_text_diagram",
    }


def test_confirmed_gold_covers_corrected_pages_and_composite_subtypes() -> None:
    gold = {row["sample_id"]: row for row in read_jsonl(ROOT / "manifests" / "gold_manifest.jsonl")}
    for sample_id in ("P0039", "P0051"):
        assert gold[sample_id]["layout_owner"] == "anchored_blocks"
        assert gold[sample_id]["layout_gold_status"] == "CONFIRMED"
    for sample_id in ("P0006", "P0016"):
        assert gold[sample_id]["layout_owner"] == "composite"
        assert gold[sample_id]["composite_kind"] == "anchored_blocks_chart"
        assert gold[sample_id]["composite_kind_gold_status"] == "CONFIRMED"
    for sample_id in ("P0025", "P0026", "P0027"):
        assert gold[sample_id]["layout_owner"] == "composite"
        assert gold[sample_id]["composite_kind"] == "flow_text_table"
        assert gold[sample_id]["composite_kind_gold_status"] == "CONFIRMED"
    for sample_id in ("P0057", "P0067"):
        assert gold[sample_id]["layout_owner"] == "flow_text"
        assert gold[sample_id]["flow_topology"] == "visual_anchored"
        assert gold[sample_id]["flow_topology_gold_status"] == "CONFIRMED"


def test_flow_topology_distinguishes_single_and_multi_lanes() -> None:
    single = {
        "page": {"width": 600, "height": 840},
        "text": {"native_char_count": 3200, "text_area_ratio": 0.45},
        "images": {"area_ratio": 0.0},
        "blocks": [
            {"bbox": [60, 100 + index * 70, 520, 150 + index * 70], "char_count": 320}
            for index in range(10)
        ],
    }
    multi_blocks = []
    for x0 in (60, 220, 380):
        for index in range(5):
            multi_blocks.append({"bbox": [x0, 300 + index * 80, x0 + 130, 350 + index * 80], "char_count": 55})
    multi = {
        "page": {"width": 600, "height": 840},
        "text": {"native_char_count": 825, "text_area_ratio": 0.3},
        "images": {"area_ratio": 0.2},
        "blocks": multi_blocks,
    }
    assert decide_flow_topology(single).selected_child == "single"
    assert decide_flow_topology(multi).selected_child == "multi"


def test_flow_rule_detects_visual_anchored_text() -> None:
    evidence = {
        "page": {"width": 600, "height": 840},
        "text": {"native_char_count": 480, "text_area_ratio": 0.18},
        "images": {"area_ratio": 0.71},
        "blocks": [
            {"bbox": [45, 100, 290, 230], "char_count": 20},
            {"bbox": [240, 520, 490, 660], "char_count": 260},
            {"bbox": [240, 675, 490, 775], "char_count": 200},
        ],
    }
    assert decide_flow_topology(evidence).selected_child == "visual_anchored"


def test_numbered_agenda_remains_single_column_topology() -> None:
    evidence = {
        "page": {"width": 600, "height": 840},
        "text": {"native_char_count": 1800, "text_area_ratio": 0.42},
        "images": {"area_ratio": 0.0},
        "native_text": "NOTICE\n1. Consider the financial statements\n2. Re-elect the directors\n3. Re-appoint the auditor",
        "blocks": [
            {"bbox": [60, 100 + index * 90, 530, 170 + index * 90], "char_count": 300}
            for index in range(6)
        ],
    }
    assert decide_flow_topology(evidence).selected_child == "single"


def test_every_classification_directory_has_definition_and_screening_mechanism() -> None:
    result_root = ROOT / "分类结果"
    directories = [result_root, *sorted(path for path in result_root.rglob("*") if path.is_dir())]
    expected = {
        ".",
        "body",
        "cover",
        "contents",
        "end",
        "visual_only",
        "body/anchored_blocks",
        "body/chart",
        "body/composite",
        "body/composite/flow_text_table",
        "body/composite/anchored_blocks_chart",
        "body/composite/chart_table",
        "body/composite/flow_text_chart",
        "body/composite/flow_text_diagram",
        "body/diagram",
        "body/freeform",
        "body/table",
        "body/flow_text",
        "body/flow_text/single",
        "body/flow_text/multi",
        "body/flow_text/visual_anchored",
    }
    actual = {
        "." if directory == result_root else directory.relative_to(result_root).as_posix()
        for directory in directories
    }
    assert actual == expected
    for directory in directories:
        content = (directory / "分类说明.md").read_text(encoding="utf-8")
        assert "定义" in content
        assert "筛选机制" in content
