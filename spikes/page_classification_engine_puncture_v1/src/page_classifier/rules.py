from __future__ import annotations

import re
from statistics import median
from typing import Any

from .models import NodeJudgement


def _judgement(
    node_key: str,
    status: str,
    child: str | None,
    confidence: float,
    refs: tuple[str, ...],
    reason: str,
) -> NodeJudgement:
    return NodeJudgement(node_key, "RULE", status, child, confidence, refs, reason)


def _inconclusive(node_key: str, reason: str, refs: tuple[str, ...] = ("PAGE1", "TEXT1")) -> NodeJudgement:
    return _judgement(node_key, "INCONCLUSIVE", None, 0.0, refs, reason)


def decide_page_role(evidence: dict[str, Any]) -> NodeJudgement:
    text = str(evidence["native_text"])
    upper = text.upper()
    text_meta = evidence["text"]
    position = evidence["page"]["position"]
    page_refs = len(re.findall(r"(?m)^.{2,90}?(?:\s|\.{2,})\d{1,3}\s*$", text))
    contents_title = bool(re.search(r"(?im)^\s*(CONTENTS?|目錄|目录)\s*$", text))
    if contents_title and page_refs >= 4:
        return _judgement("page.role", "DECIDED", "contents", 0.96, ("TEXT1", "PAGE1"), "存在目录标题和重复条目页码关系")

    identity_words = bool(re.search(r"ANNUAL REPORT|INTERIM REPORT|年報|年报|年度報告|年度报告", upper))
    max_font = float(text_meta.get("max_font_size") or 0)
    chars = int(text_meta["native_char_count"])
    if position["is_first"] and identity_words and chars <= 500 and max_font >= 18:
        return _judgement("page.role", "DECIDED", "cover", 0.86, ("TEXT1", "PAGE1", "IMG1"), "首页身份标题明显且连续正文较少")

    if int(evidence["tables"]["count"]) > 0 or chars >= 700:
        return _judgement("page.role", "DECIDED", "body", 0.82, ("TEXT1", "TABLE1", "PAGE1"), "存在实质正文或表格内容")

    if chars == 0:
        return _inconclusive("page.role", "原生文字为空，规则无法区分图片封面、结束页、正文或纯视觉页", ("IMG1", "TEXT1", "PAGE1"))
    if position["is_last"] and chars <= 250:
        return _inconclusive("page.role", "末页且文字较少，但规则无法证明其承担结束功能", ("IMG1", "TEXT1", "PAGE1"))
    return _inconclusive("page.role", "规则证据不足以稳定区分页面角色", ("IMG1", "TEXT1", "PAGE1"))


def decide_layout_owner(evidence: dict[str, Any]) -> NodeJudgement:
    text_meta = evidence["text"]
    tables = evidence["tables"]
    chars = int(text_meta["native_char_count"])
    outside = int(text_meta["outside_table_chars"])
    table_count = int(tables["count"])
    table_ratio = float(tables["area_ratio"])
    image_ratio = float(evidence["images"]["area_ratio"])
    block_count = int(text_meta["block_count"])
    text_ratio = float(text_meta["text_area_ratio"])
    borderless = evidence.get("borderless_table") or {}
    borderless_confidence = float(borderless.get("confidence") or 0)
    if image_ratio >= 0.6 and block_count <= 4 and text_ratio <= 0.25:
        return _judgement(
            "body.layout_owner",
            "DECIDED",
            "flow_text",
            0.86,
            ("IMAGE1", "TEXT1", "IMG1"),
            "大面积固定视觉中只有一个局部连续正文区",
        )
    if borderless_confidence >= 0.9:
        borderless_outside = int(borderless.get("outside_chars") or 0)
        borderless_area = float(borderless.get("area_ratio") or 0)
        outside_ratio = borderless_outside / max(chars, 1)
        if borderless_outside >= 550 and borderless_area >= 0.15:
            return _judgement(
                "body.layout_owner",
                "DECIDED",
                "composite",
                borderless_confidence,
                ("BTABLE1", "TEXT1", "IMG1"),
                "稳定重复行和列锚点确认无边框表格，且表外存在实质正文",
            )
        if borderless_outside <= 150 and outside_ratio <= 0.2:
            return _judgement(
                "body.layout_owner",
                "DECIDED",
                "table",
                borderless_confidence,
                ("BTABLE1", "TEXT1", "IMG1"),
                "稳定重复行和列锚点确认无边框表格，表外只有少量附属文字",
            )
    if table_count and table_ratio >= 0.5 and outside < max(350, chars * 0.25):
        return _judgement("body.layout_owner", "DECIDED", "table", 0.9, ("TABLE1", "TEXT1", "IMG1"), "表格区域占主体且大多数文字绑定表格")
    if table_count and table_ratio >= 0.18 and outside >= 350:
        return _judgement("body.layout_owner", "DECIDED", "composite", 0.78, ("TABLE1", "TEXT1", "IMG1"), "表格和表外正文都包含实质内容")
    if chars >= 700 and table_count == 0:
        return _judgement("body.layout_owner", "DECIDED", "flow_text", 0.72, ("TEXT1", "IMG1"), "连续原生文字较多且未检测到主体表格")
    return _inconclusive("body.layout_owner", "规则不能可靠区分图表、结构图、信息块或弱结构正文", ("IMG1", "TEXT1", "TABLE1", "DRAWING1"))


def estimate_text_columns(evidence: dict[str, Any]) -> int:
    page_width = float(evidence["page"]["width"])
    page_height = float(evidence["page"]["height"])
    candidates = []
    for block in evidence["blocks"]:
        x0, y0, x1, _ = (float(value) for value in block["bbox"])
        width = x1 - x0
        if int(block["char_count"]) < 12 or y0 >= page_height * 0.94 or width >= page_width * 0.68:
            continue
        candidates.append(block)
    if not candidates:
        return 1
    clusters: list[list[dict[str, Any]]] = []
    threshold = max(24.0, page_width * 0.075)
    for block in sorted(candidates, key=lambda item: float(item["bbox"][0])):
        x0 = float(block["bbox"][0])
        cluster = next(
            (items for items in clusters if abs(x0 - median(float(item["bbox"][0]) for item in items)) <= threshold),
            None,
        )
        if cluster is None:
            clusters.append([block])
        else:
            cluster.append(block)
    substantial = [
        items
        for items in clusters
        if len(items) >= 2 or sum(int(item["char_count"]) for item in items) >= 180
    ]
    return max(1, len(substantial))


def decide_flow_topology(evidence: dict[str, Any]) -> NodeJudgement:
    text = evidence["text"]
    blocks = [block for block in evidence["blocks"] if int(block["char_count"]) >= 12]
    block_count = len(blocks)
    image_ratio = float(evidence["images"]["area_ratio"])
    text_ratio = float(text["text_area_ratio"])
    columns = estimate_text_columns(evidence)
    if image_ratio >= 0.35 and block_count <= 6 and text_ratio <= 0.28:
        return _judgement(
            "body.flow.topology",
            "DECIDED",
            "visual_anchored",
            0.86,
            ("IMAGE1", "TEXT1", "IMG1"),
            "大面积固定视觉中只有少量锚定文字流",
        )
    if columns >= 2:
        return _judgement(
            "body.flow.topology",
            "DECIDED",
            "multi",
            0.84,
            ("TEXT1", "IMG1"),
            f"主体文字形成 {columns} 条稳定栏道",
        )
    return _judgement(
        "body.flow.topology",
        "DECIDED",
        "single",
        0.82,
        ("TEXT1", "IMG1"),
        "主体文字只有一条稳定栏道",
    )


def decide_composite_kind(evidence: dict[str, Any]) -> NodeJudgement:
    text = evidence["text"]
    tables = evidence["tables"]
    table_count = int(tables["count"])
    table_ratio = float(tables["area_ratio"])
    outside = int(text["outside_table_chars"])
    text_ratio = float(text["text_area_ratio"])
    if table_count and table_ratio >= 0.08 and outside >= 350 and text_ratio >= 0.45:
        return _judgement(
            "body.composite.kind",
            "DECIDED",
            "flow_text_table",
            0.82,
            ("TABLE1", "TEXT1", "IMG1"),
            "主体表格与表外正文都包含实质内容",
        )
    return _inconclusive(
        "body.composite.kind",
        "规则不能可靠区分卡片、正文、表格、图表与结构图的复合组合",
        ("IMG1", "TEXT1", "TABLE1", "DRAWING1"),
    )


def decide_rule(node_key: str, evidence: dict[str, Any]) -> NodeJudgement:
    if node_key == "page.role":
        return decide_page_role(evidence)
    if node_key == "body.layout_owner":
        return decide_layout_owner(evidence)
    if node_key == "body.flow.topology":
        return decide_flow_topology(evidence)
    if node_key == "body.composite.kind":
        return decide_composite_kind(evidence)
    raise KeyError(node_key)
