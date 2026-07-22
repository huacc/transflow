"""验证 RV2 分类规则不抢占正确 Route，并保持正文与页边家具分离。"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from transflow.classification.decision_adapter import BoundedDecisionRunner
from transflow.classification.engine import ClassificationEngine
from transflow.classification.evidence import build_evidence
from transflow.classification.rules import (
    decide_composite_kind,
    decide_flow_topology,
    decide_layout_owner,
    estimate_text_columns,
)
from transflow.domain.classification import ModelDecision, ModelDecisionRequest
from transflow.pdf_kernel.facts import PageFactsExtractor

REPO_ROOT = Path(__file__).resolve().parent.parent
RV0_SOURCE = (
    REPO_ROOT
    / "runs"
    / "critical_chain_revalidation"
    / "RV0"
    / "01-baseline-20260721-164419"
    / "input"
    / "source_document.pdf"
)
GOLD_ROOT = REPO_ROOT / "spikes" / "page_classification_engine_puncture_v1" / "分类结果"


def sha256_file(path: Path) -> str:
    """计算真实输入哈希。"""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def evidence_for(path: Path, page_no: int = 1, page_count: int = 1) -> dict[str, Any]:
    """经生产 Kernel 构造匿名分类证据。"""

    facts = PageFactsExtractor().extract_page(
        path,
        sha256_file(path),
        page_no,
        include_classification=True,
    )
    return build_evidence(facts, page_count)


class BodyRoleOnlyPort:
    """只允许一级页面角色调用，证明确定表格节点不会浪费模型调用。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def decide(self, request: ModelDecisionRequest) -> ModelDecision:
        node_key = str(request.node_spec["node_key"])
        stage = str(request.node_spec["stage"])
        self.calls.append((node_key, stage))
        if node_key != "page.role":
            raise AssertionError(f"确定结构节点不应调用模型:{node_key}")
        return ModelDecision(
            decision_id=request.decision_id,
            decision_kind=request.decision_kind,
            result_code="body",
            evidence_ids=("TEXT1", "TABLE1"),
            confidence=0.95,
            reason_summary="页面包含实质正文与表格",
        )


def test_rv2_p151_is_flow_text_table_and_skips_determined_node_models() -> None:
    """p0151 的正文与窄列语义表格必须共同拥有页面，确定节点不得再问模型。"""

    facts = PageFactsExtractor().extract_page(
        RV0_SOURCE,
        sha256_file(RV0_SOURCE),
        151,
        include_classification=True,
    )
    evidence = build_evidence(facts, 187)
    owner = decide_layout_owner(evidence)
    kind = decide_composite_kind(evidence)
    assert (owner.selected_child, owner.confidence) == ("composite", 0.9)
    assert (kind.selected_child, kind.confidence) == ("flow_text_table", 0.9)

    port = BodyRoleOnlyPort()
    result = ClassificationEngine(BoundedDecisionRunner(port)).classify_page(facts, 187)
    assert result.route.route == "body.composite.flow_text_table"
    assert port.calls == [("page.role", "PRIMARY")]


def test_rv2_two_row_decoration_does_not_force_composite() -> None:
    """两行装饰框即使被 PDF 库报成候选表格，也不能高置信抢走正文 Route。"""

    source = GOLD_ROOT / "body" / "flow_text" / "single" / "S2P0311.pdf"
    judgement = decide_layout_owner(evidence_for(source))
    assert not (
        judgement.selected_child == "composite" and judgement.confidence >= 0.9
    )


def test_rv2_chart_grid_does_not_force_flow_text_table() -> None:
    """图表内部的规则网格不是正文加表格的直接证据。"""

    source = (
        GOLD_ROOT
        / "body"
        / "composite"
        / "flow_text_chart"
        / "EN_15_03988_p0155.pdf"
    )
    judgement = decide_composite_kind(evidence_for(source))
    assert not (
        judgement.selected_child == "flow_text_table" and judgement.confidence >= 0.9
    )


def test_rv2_borderless_card_grid_defers_instead_of_becoming_table() -> None:
    """少量卡片文字形成的对齐关系不能直接冒充无边框表格。"""

    source = GOLD_ROOT / "body" / "anchored_blocks" / "AB_ZH_06_01596_p003.pdf"
    judgement = decide_layout_owner(evidence_for(source))
    assert not (judgement.selected_child == "table" and judgement.confidence >= 0.9)


def test_rv2_two_column_information_cards_do_not_skip_model_as_table() -> None:
    """双栏公司资料卡片的重复对齐关系不足以直接判定为无边框表格。"""

    source = GOLD_ROOT / "body" / "anchored_blocks" / "AB_EN_09_02571_p004.pdf"
    judgement = decide_layout_owner(evidence_for(source))
    assert judgement.status == "INCONCLUSIVE"


def test_rv2_table_dominance_wins_over_incidental_outside_text() -> None:
    """主体文字已绑定大表格时，页边和少量说明文字不能把 table 改成 composite。"""

    source = (
        GOLD_ROOT
        / "body"
        / "table"
        / "00005_2025_interim_report_zh_p002_body_table.pdf"
    )
    judgement = decide_layout_owner(evidence_for(source))
    assert judgement.selected_child == "table"
    assert judgement.confidence >= 0.9


def test_rv2_partial_native_table_detection_defers_instead_of_claiming_composite() -> None:
    """只命中财务表局部时，规则不得把整页表格高置信改成正文加小表。"""

    source = (
        GOLD_ROOT
        / "body"
        / "table"
        / "01425_JUSTIN ALLEN H_英文_2025_p050_body_table.pdf"
    )
    judgement = decide_layout_owner(evidence_for(source))
    assert not (
        judgement.selected_child == "composite" and judgement.confidence >= 0.9
    )


def test_rv2_margin_furniture_does_not_change_body_columns() -> None:
    """页眉页脚和纯页码属于横切家具，不参与正文单栏/多栏 Route。"""

    base: dict[str, Any] = {
        "page": {"width": 595.0, "height": 842.0},
        "blocks": [
            {"bbox": [80.0, 120.0, 430.0, 220.0], "char_count": 220},
            {"bbox": [80.0, 240.0, 430.0, 340.0], "char_count": 220},
            {"bbox": [80.0, 360.0, 430.0, 460.0], "char_count": 220},
        ],
    }
    with_margin = copy.deepcopy(base)
    with_margin["blocks"].extend(
        [
            {"bbox": [20.0, 18.0, 140.0, 30.0], "char_count": 40},
            {"bbox": [20.0, 34.0, 140.0, 46.0], "char_count": 40},
            {"bbox": [420.0, 18.0, 570.0, 30.0], "char_count": 40},
            {"bbox": [420.0, 34.0, 570.0, 46.0], "char_count": 40},
            {"bbox": [20.0, 800.0, 140.0, 812.0], "char_count": 40},
            {"bbox": [420.0, 800.0, 570.0, 812.0], "char_count": 40},
        ]
    )
    assert estimate_text_columns(base) == 1
    assert estimate_text_columns(with_margin) == 1


def test_rv2_p151_structure_perturbations_keep_direct_route() -> None:
    """缩放、平移、等长替换和文字块拆分不能改变 p0151 的结构 Route。"""

    original = evidence_for(RV0_SOURCE, 151, 187)
    variants: list[dict[str, Any]] = []

    scaled = copy.deepcopy(original)
    scaled["page"]["width"] *= 1.25
    scaled["page"]["height"] *= 1.25
    for block in scaled["blocks"]:
        block["bbox"] = [float(value) * 1.25 for value in block["bbox"]]
    scaled["tables"]["bboxes"] = [
        [float(value) * 1.25 for value in bbox] for bbox in scaled["tables"]["bboxes"]
    ]
    variants.append(scaled)

    translated = copy.deepcopy(original)
    for block in translated["blocks"]:
        x0, y0, x1, y1 = (float(value) for value in block["bbox"])
        block["bbox"] = [x0 + 11.0, y0 + 17.0, x1 + 11.0, y1 + 17.0]
    translated["tables"]["bboxes"] = [
        [bbox[0] + 11.0, bbox[1] + 17.0, bbox[2] + 11.0, bbox[3] + 17.0]
        for bbox in translated["tables"]["bboxes"]
    ]
    variants.append(translated)

    replaced = copy.deepcopy(original)
    replaced["native_text"] = "替" * len(str(replaced["native_text"]))
    for block in replaced["blocks"]:
        block["text"] = "替" * len(str(block["text"]))
    variants.append(replaced)

    fragmented = copy.deepcopy(original)
    target = max(fragmented["blocks"], key=lambda item: int(item["char_count"]))
    fragmented["blocks"].remove(target)
    x0, y0, x1, y1 = (float(value) for value in target["bbox"])
    midpoint = (y0 + y1) / 2
    source_text = str(target["text"])
    split_at = len(source_text) // 2
    first = {**target, "bbox": [x0, y0, x1, midpoint], "text": source_text[:split_at]}
    second = {**target, "bbox": [x0, midpoint, x1, y1], "text": source_text[split_at:]}
    first["char_count"] = len(str(first["text"]))
    second["char_count"] = len(str(second["text"]))
    fragmented["blocks"].extend((first, second))
    fragmented["text"]["block_count"] += 1
    variants.append(fragmented)

    for evidence in (original, *variants):
        assert decide_layout_owner(evidence).selected_child == "composite"
        assert decide_composite_kind(evidence).selected_child == "flow_text_table"


def test_rv2_concrete_routes_have_one_matching_toolbox_key() -> None:
    """16 个正文/页面叶 Route 必须与 Toolbox Catalog 一一对应。"""

    taxonomy = json.loads(
        (REPO_ROOT / "resources" / "taxonomy" / "page_classification_routes_v1.json").read_text(
            encoding="utf-8"
        )
    )
    catalog = json.loads(
        (REPO_ROOT / "resources" / "catalogs" / "page_toolbox_catalog_v4.json").read_text(
            encoding="utf-8"
        )
    )
    routes = {
        str(item["route"])
        for item in taxonomy["routes"]
        if item["route"] != "body.freeform"
    }
    entries = [item for item in catalog["entries"] if item["route"] in routes]
    assert len(routes) == len(entries) == 16
    assert {str(item["route"]) for item in entries} == routes
    assert all(item["route"] == item["toolbox_key"] for item in entries)


def test_rv2_no_sample_identity_special_case_in_runtime_rules() -> None:
    """生产规则不得读取样本号、计划页码、文件名或绝对路径。"""

    runtime_text = "\n".join(
        (REPO_ROOT / relative).read_text(encoding="utf-8")
        for relative in (
            "src/transflow/classification/rules.py",
            "src/transflow/classification/engine.py",
            "src/transflow/classification/evidence.py",
        )
    )
    forbidden = ("S2P", "p0151", "source_path", "file_name", "filename")
    assert all(token not in runtime_text for token in forbidden)


def test_rv2_margin_perturbation_keeps_flow_topology_decision() -> None:
    """正文拓扑裁决只看正文带，添加页边家具后仍为单栏。"""

    evidence: dict[str, Any] = {
        "page": {"width": 595.0, "height": 842.0},
        "blocks": [
            {"bbox": [80.0, 120.0, 430.0, 220.0], "char_count": 220},
            {"bbox": [80.0, 240.0, 430.0, 340.0], "char_count": 220},
        ],
        "images": {"area_ratio": 0.0},
        "text": {"text_area_ratio": 0.35},
    }
    assert decide_flow_topology(evidence).selected_child == "single"


def test_rv2_large_visual_interrupting_a_column_defers_multi_to_visual_anchor() -> None:
    """大图占据栏道上半部时，正文不能按普通多栏跨视觉对象扩容。"""

    source = GOLD_ROOT / "body" / "flow_text" / "visual_anchored" / "ZH_00050_p0015.pdf"
    judgement = decide_flow_topology(evidence_for(source))
    assert judgement.selected_child == "visual_anchored"


def test_rv2_full_page_visual_with_lower_text_slots_is_visual_anchored() -> None:
    """整页视觉把正文压入下方局部槽位时，不因槽位呈两栏而判普通多栏。"""

    source = GOLD_ROOT / "body" / "flow_text" / "visual_anchored" / "ZH_03366_p0008.pdf"
    judgement = decide_flow_topology(evidence_for(source))
    assert judgement.selected_child == "visual_anchored"


def test_rv2_ordinary_multi_column_page_with_visuals_remains_multi() -> None:
    """视觉对象未锁死正文安全带时，普通多栏不能被新规则抢走。"""

    source = GOLD_ROOT / "body" / "flow_text" / "multi" / "S2P0869.pdf"
    judgement = decide_flow_topology(evidence_for(source))
    assert judgement.selected_child == "multi"


def test_rv2_substantial_drawing_topology_prevents_weak_flow_text_shortcut() -> None:
    """正文很多但另有成组流程节点时，弱正文规则必须交给模型复核所有权。"""

    source = (
        GOLD_ROOT
        / "body"
        / "composite"
        / "flow_text_diagram"
        / "EN_14_06996_p0099.pdf"
    )
    judgement = decide_layout_owner(evidence_for(source))
    assert judgement.status == "INCONCLUSIVE"


def test_rv2_prompts_match_existing_toolbox_ownership_boundaries() -> None:
    """模型节点必须按既有 Toolbox owner 判断，不能把相邻视觉误扩成新类别。"""

    prompt_root = REPO_ROOT / "resources" / "prompts" / "classification"
    page_role = "\n".join(
        (prompt_root / "page_role" / name).read_text(encoding="utf-8")
        for name in ("decide.zh-CN.md", "review.zh-CN.md")
    )
    layout_owner = "\n".join(
        (prompt_root / "body_layout_owner" / name).read_text(encoding="utf-8")
        for name in ("decide.zh-CN.md", "review.zh-CN.md")
    )
    flow_topology = "\n".join(
        (prompt_root / "body_flow_topology" / name).read_text(encoding="utf-8")
        for name in ("decide.zh-CN.md", "review.zh-CN.md")
    )
    composite_kind = "\n".join(
        (prompt_root / "body_composite_kind" / name).read_text(encoding="utf-8")
        for name in ("decide.zh-CN.md", "review.zh-CN.md")
    )

    assert "同时为 true 时位置没有方向信息" in page_role
    assert "第三方认证标记" in page_role
    assert "微型页眉页脚" in page_role
    assert "对齐置信度不能单独证明表格语义" in layout_owner
    assert "flow_text + anchored_blocks 不是允许的复合组合" in layout_owner
    assert "解释同一组指标的段落仍归 chart owner" in layout_owner
    assert "主体节点或箭头不能降为正文附件" in layout_owner
    assert "理由同时确认实质正文与主体结构图" in layout_owner
    assert "公司资料块没有跨区块共享的行表头" in layout_owner
    assert "里程碑节点" in layout_owner
    assert "多栏槽位仍可属于 visual_anchored" in flow_topology
    assert "整页背景或渐隐主视觉" in flow_topology
    assert "多个图表面板仍属于 chart" in composite_kind
    assert "分类议题清单" in composite_kind
