from __future__ import annotations

from dataclasses import replace
import re

import fitz

from page_toolbox_puncture.contracts import PageFacts, PageTranslationBundle
from toolboxes.body.diagram.tools.layout_planner import plan_diagram_layout
from toolboxes.body.diagram.tools.models import DiagramLayoutPlan, DiagramPlacement
from toolboxes.body.flow_text.multi.tools.layout_planner import build_best_multi_plan
from toolboxes.body.flow_text.multi.tools.models import MultiColumnTemplate
from toolboxes.body.flow_text.single.tools.p4_layout_planner import (
    _font_variant,
    _minimum_text_height,
    build_best_p4_plan,
)

from .. import TOOLBOX_KEY
from .models import (
    CompositeFinding,
    CompositeLayoutPlan,
    CompositePageTemplate,
    Rect,
)
from .translation_request import split_translation_bundle


_CJK_FORBIDDEN_LINE_START = frozenset("，。！？；：、）》】」』〉〕］｝％")
_CJK_FORBIDDEN_LINE_END = frozenset("《（【「『〈〔［｛")


def plan_composite_layout(
    *,
    facts: PageFacts,
    template: CompositePageTemplate,
    translations: PageTranslationBundle,
    source_language: str,
    target_language: str,
    font_file: str,
    bold_font_file: str | None,
) -> tuple[CompositeLayoutPlan, tuple[CompositeFinding, ...], dict[str, object]]:
    flow_bundle, diagram_bundle = split_translation_bundle(template, translations)
    flow_findings: list[CompositeFinding] = []
    if isinstance(template.flow_template, MultiColumnTemplate):
        flow_plan, flow_attempts, child_findings = build_best_multi_plan(
            facts=facts,
            template=template.flow_template,
            translations=flow_bundle,
            source_language=source_language,
            target_language=target_language,
            font_file=font_file,
            font_resource="p18flow",
        )
        flow_findings.extend(_flow_finding(item) for item in child_findings)
    else:
        flow_plan, flow_attempts = build_best_p4_plan(
            facts=facts,
            template=template.flow_template,
            translations=flow_bundle,
            source_language=source_language,
            target_language=target_language,
            font_file=font_file,
            font_resource="p18flow",
        )
        if flow_plan is None:
            raise ValueError("P18_FLOW_LAYOUT_PLAN_MISSING")
        if flow_attempts:
            flow_findings.extend(_flow_finding(item) for item in flow_attempts[-1].findings)

    flow_plan = _expand_flow_to_owner_width(facts, template, flow_plan)
    flow_plan = _preserve_structural_flow_bands(facts, template, flow_plan)
    flow_plan = _inline_preserved_prefixes(facts, template, flow_plan)
    flow_plan = _expand_structural_headings(facts, template, flow_plan)
    flow_plan = _preserve_structural_flow_bands(
        facts,
        template,
        flow_plan,
        final_pass=True,
    )
    flow_plan = _compact_flow_before_images(facts, template, flow_plan)
    flow_findings = _resolved_vertical_flow_findings(
        template,
        flow_plan,
        flow_findings,
    )

    diagram_plan, child_diagram_findings = plan_diagram_layout(
        template.diagram_template,
        diagram_bundle,
        font_file=font_file,
        bold_font_file=bold_font_file,
    )
    diagram_plan, repaired_diagram_ids = _repair_diagram_layout(
        template,
        diagram_plan,
        facts,
    )
    child_diagram_findings = tuple(
        item
        for item in child_diagram_findings
        if item.container_id not in repaired_diagram_ids
    )
    findings = [
        *flow_findings,
        *(_diagram_finding(item, template) for item in child_diagram_findings),
    ]

    flow_by_base = {
        item.base_container_id: item for item in template.containers if item.owner != "diagram"
    }
    diagram_by_base = {
        item.base_container_id: item for item in template.containers if item.owner == "diagram"
    }
    combined: dict[str, DiagramPlacement] = {}
    for placement in flow_plan.placements:
        container = flow_by_base[placement.container_id]
        font_path, resource = _font_variant(
            flow_plan.font_file,
            flow_plan.font_resource,
            getattr(placement, "font_weight", "regular"),
        )
        combined[container.composite_id] = DiagramPlacement(
            container_id=container.composite_id,
            owner_kind=container.owner,
            owner_id=container.composite_id,
            node_id=None,
            translated_text=placement.translated_text,
            output_bbox=placement.output_bbox,
            font_file=font_path,
            font_resource=resource,
            font_size=placement.font_size,
            line_height=placement.line_height,
            color_srgb=placement.color_srgb,
            alignment=_flow_alignment(getattr(placement, "horizontal_policy", "")),
            fit_profile=getattr(flow_plan, "profile_id", "multi-column"),
            fit=placement.fit,
            glyph_bbox=None,
        )
    for placement in diagram_plan.placements:
        container = diagram_by_base[placement.container_id]
        combined[container.composite_id] = DiagramPlacement(
            container_id=container.composite_id,
            owner_kind=placement.owner_kind,
            owner_id=container.composite_id,
            node_id=placement.node_id,
            translated_text=placement.translated_text,
            output_bbox=placement.output_bbox,
            font_file=placement.font_file,
            font_resource=placement.font_resource.replace("p14diagram", "p18diagram"),
            font_size=placement.font_size,
            line_height=placement.line_height,
            color_srgb=placement.color_srgb,
            alignment=placement.alignment,
            fit_profile=placement.fit_profile,
            fit=placement.fit,
            glyph_bbox=placement.glyph_bbox,
        )
    combined = _flatten_short_horizontal_node_labels(template, combined)
    combined, word_break_findings = _repair_latin_word_breaks(template, combined)
    findings.extend(word_break_findings)
    combined, line_break_findings = _repair_cjk_line_breaks(combined)
    findings.extend(line_break_findings)
    expected = [item.composite_id for item in template.containers]
    if set(combined) != set(expected):
        raise ValueError("P18_LAYOUT_IDS_DO_NOT_MATCH_TEMPLATE")
    render_plan = DiagramLayoutPlan(
        page_id=template.page_id,
        toolbox_key=TOOLBOX_KEY,
        topology_sha256=template.topology_sha256,
        placements=tuple(combined[item] for item in expected),
    )
    findings.extend(_placement_findings(template, render_plan))
    findings.extend(_flow_image_findings(facts, template, combined))
    plan = CompositeLayoutPlan(
        page_id=template.page_id,
        toolbox_key=TOOLBOX_KEY,
        structure_sha256=template.structure_sha256,
        flow_mode=template.flow_mode,
        flow_plan=flow_plan,
        diagram_plan=diagram_plan,
        render_plan=render_plan,
    )
    return plan, _deduplicate(tuple(findings)), {
        "flow_mode": template.flow_mode,
        "flow_attempts": flow_attempts,
        "flow_child_findings": tuple(flow_findings),
        "diagram_child_findings": child_diagram_findings,
    }


def _repair_latin_word_breaks(
    template: CompositePageTemplate,
    placements: dict[str, DiagramPlacement],
) -> tuple[dict[str, DiagramPlacement], tuple[CompositeFinding, ...]]:
    adjusted = dict(placements)
    findings: list[CompositeFinding] = []
    for container_id, placement in placements.items():
        if placement.alignment == "VERTICAL":
            continue
        words = re.findall(r"[A-Za-z]{4,}", placement.translated_text)
        if not words:
            continue
        available = placement.output_bbox[2] - placement.output_bbox[0]
        if available <= 1.0:
            continue
        font = fitz.Font(fontfile=placement.font_file)
        longest = max(
            font.text_length(word, fontsize=placement.font_size)
            for word in words
        )
        target_width = available * 0.98
        if longest <= target_width + 0.01:
            continue
        font_size = round(
            max(6.0, placement.font_size * target_width / longest),
            4,
        )
        repaired_width = max(
            font.text_length(word, fontsize=font_size)
            for word in words
        )
        candidate_lines = _rendered_lines(placement, font_size)
        glyph_bbox = _measured_glyph_bbox(
            template,
            placement.output_bbox,
            placement.translated_text,
            placement,
            size=font_size,
            line_height=placement.line_height,
            alignment=placement.alignment,
        )
        if (
            repaired_width > target_width + 0.01
            or candidate_lines is None
            or glyph_bbox is None
        ):
            findings.append(
                CompositeFinding(
                    code="P18_LATIN_WORD_FRAGMENTATION",
                    severity="HARD",
                    owner=placement.owner_kind,
                    container_id=container_id,
                    message="A Latin word cannot fit inside its text frame at the readable floor.",
                    evidence={
                        "available_width": round(available, 4),
                        "required_word_width": round(repaired_width, 4),
                        "font_size": font_size,
                    },
                )
            )
            continue
        adjusted[container_id] = replace(
            placement,
            font_size=font_size,
            fit_profile=placement.fit_profile + "+p18-latin-word-fit",
            glyph_bbox=glyph_bbox,
        )
    return adjusted, tuple(findings)


def _flatten_short_horizontal_node_labels(
    template: CompositePageTemplate,
    placements: dict[str, DiagramPlacement],
) -> dict[str, DiagramPlacement]:
    containers = {item.composite_id: item for item in template.containers}
    adjusted = dict(placements)
    for container_id, placement in placements.items():
        container = containers[container_id]
        if (
            container.owner != "diagram"
            or container.role != "node_text"
            or placement.node_id is None
            or placement.alignment == "VERTICAL"
        ):
            continue
        current_lines = _rendered_lines(placement, placement.font_size)
        if current_lines is None or len(current_lines) <= 1:
            continue
        normalized = " ".join(placement.translated_text.split())
        safe_width = container.allowed_bbox[2] - container.allowed_bbox[0]
        if safe_width <= placement.output_bbox[2] - placement.output_bbox[0] + 0.5:
            continue
        minimum_height = _minimum_text_height(
            template.width,
            template.height,
            safe_width,
            normalized,
            placement.font_size,
            placement.line_height,
            placement.font_file,
            placement.font_resource,
            placement.color_srgb,
        )
        safe_height = container.allowed_bbox[3] - container.allowed_bbox[1]
        if minimum_height > safe_height + 0.01:
            continue
        center_y = (placement.output_bbox[1] + placement.output_bbox[3]) / 2.0
        y0 = min(
            max(container.allowed_bbox[1], center_y - minimum_height / 2.0),
            container.allowed_bbox[3] - minimum_height,
        )
        candidate_bbox = (
            container.allowed_bbox[0],
            round(y0, 4),
            container.allowed_bbox[2],
            round(y0 + minimum_height, 4),
        )
        candidate = replace(
            placement,
            translated_text=normalized,
            output_bbox=candidate_bbox,
        )
        candidate_lines = _rendered_lines(candidate, candidate.font_size)
        if candidate_lines is None or len(candidate_lines) != 1:
            continue
        glyph_bbox = _measured_glyph_bbox(
            template,
            candidate_bbox,
            normalized,
            candidate,
            size=candidate.font_size,
            line_height=candidate.line_height,
            alignment=candidate.alignment,
        )
        if glyph_bbox is None:
            continue
        if any(
            other_id != container_id
            and containers[other_id].owner == "diagram"
            and _intersection_area(
                glyph_bbox,
                other.glyph_bbox or other.output_bbox,
            )
            > 0.25
            and _intersection_area(
                container.source_bbox,
                containers[other_id].source_bbox,
            )
            <= 0.25
            for other_id, other in adjusted.items()
        ):
            continue
        adjusted[container_id] = replace(
            candidate,
            fit_profile=candidate.fit_profile + "+p18-horizontal-safe-flatten",
            fit=True,
            glyph_bbox=glyph_bbox,
        )
    return adjusted


def _repair_cjk_line_breaks(
    placements: dict[str, DiagramPlacement],
) -> tuple[dict[str, DiagramPlacement], tuple[CompositeFinding, ...]]:
    adjusted = dict(placements)
    findings: list[CompositeFinding] = []
    for container_id, placement in placements.items():
        if (
            placement.owner_kind not in {"flow", "shared"}
            or placement.alignment == "VERTICAL"
            or not any(
                character in _CJK_FORBIDDEN_LINE_START
                or character in _CJK_FORBIDDEN_LINE_END
                for character in placement.translated_text
            )
        ):
            continue
        lines = _rendered_lines(placement, placement.font_size)
        if lines is None or not _has_unnatural_cjk_break(lines):
            continue
        repaired = None
        for scale in (
            0.9975,
            0.995,
            0.9925,
            0.99,
            0.985,
            0.98,
            0.97,
            0.96,
            0.95,
            0.94,
            0.93,
            0.92,
            0.90,
        ):
            font_size = round(placement.font_size * scale, 4)
            candidate_lines = _rendered_lines(placement, font_size)
            if candidate_lines is not None and not _has_unnatural_cjk_break(candidate_lines):
                repaired = replace(
                    placement,
                    font_size=font_size,
                    fit_profile=placement.fit_profile + "+p18-cjk-line-break",
                )
                break
        if repaired is not None:
            adjusted[container_id] = repaired
            continue
        findings.append(
            CompositeFinding(
                code="P18_UNNATURAL_CJK_LINE_BREAK",
                severity="HARD",
                owner=placement.owner_kind,
                container_id=container_id,
                message="CJK closing punctuation starts a line or opening punctuation ends a line.",
                evidence={"rendered_lines": lines},
            )
        )
    return adjusted, tuple(findings)


def _rendered_lines(
    placement: DiagramPlacement,
    font_size: float,
) -> tuple[str, ...] | None:
    width = placement.output_bbox[2] - placement.output_bbox[0]
    height = placement.output_bbox[3] - placement.output_bbox[1]
    if width <= 1.0 or height <= 1.0:
        return None
    with fitz.open() as document:
        page = document.new_page(width=width + 2.0, height=height + 2.0)
        spare = page.insert_textbox(
            fitz.Rect(0.0, 0.0, width, height),
            placement.translated_text,
            fontname=placement.font_resource,
            fontfile=placement.font_file,
            fontsize=font_size,
            lineheight=placement.line_height,
            align={
                "LEFT": fitz.TEXT_ALIGN_LEFT,
                "CENTER": fitz.TEXT_ALIGN_CENTER,
                "RIGHT": fitz.TEXT_ALIGN_RIGHT,
            }.get(placement.alignment, fitz.TEXT_ALIGN_LEFT),
        )
        if spare < 0:
            return None
        return tuple(
            "".join(span["text"] for span in line["spans"])
            for block in page.get_text("dict")["blocks"]
            for line in block.get("lines", [])
        )


def _has_unnatural_cjk_break(lines: tuple[str, ...]) -> bool:
    for line in lines:
        visible = line.strip()
        if not visible:
            continue
        if (
            visible[0] in _CJK_FORBIDDEN_LINE_START
            or visible[-1] in _CJK_FORBIDDEN_LINE_END
        ):
            return True
    return False


def _repair_diagram_layout(
    template: CompositePageTemplate,
    plan: DiagramLayoutPlan,
    facts: PageFacts | None = None,
) -> tuple[DiagramLayoutPlan, set[str]]:
    containers = {
        item.container_id: item for item in template.diagram_template.containers
    }
    nodes = {item.node_id: item for item in template.diagram_template.nodes}
    adjusted = {item.container_id: item for item in plan.placements}
    repaired: set[str] = set()

    list_nodes = {
        item.node_id
        for item in containers.values()
        if item.node_id and item.role in {"list_heading", "list_item"}
    }
    for node_id in list_nodes:
        members = [
            item
            for item in template.diagram_template.containers
            if item.node_id == node_id and item.role in {"list_heading", "list_item"}
        ]
        packed = _pack_semantic_list(
            template,
            nodes[node_id].safe_text_bbox,
            members,
            adjusted,
        )
        if packed is None:
            continue
        adjusted.update({item.container_id: item for item in packed})
        repaired.update(item.container_id for item in packed)

    independent_lists = {}
    for item in containers.values():
        if item.node_id is not None or item.role not in {"list_heading", "list_item"}:
            continue
        independent_lists.setdefault((item.owner_id, item.allowed_bbox), []).append(item)
    for (_, safe), members in independent_lists.items():
        packed = _pack_semantic_list(
            template,
            safe,
            members,
            adjusted,
        )
        if packed is None:
            continue
        adjusted.update({item.container_id: item for item in packed})
        repaired.update(item.container_id for item in packed)

    stacked_nodes = {}
    for item in containers.values():
        if not item.node_id or item.role != "node_text":
            continue
        stacked_nodes.setdefault(item.node_id, []).append(item)
    protected = (
        [
            item
            for item in facts.text_objects
            if item.object_id in set(template.protected_object_ids)
        ]
        if facts is not None
        else []
    )
    for node_id, members in stacked_nodes.items():
        for group in _stacked_node_text_groups(members):
            safe = nodes[node_id].safe_text_bbox
            source_top = min(item.source_bbox[1] for item in group)
            blockers = [
                item.bbox
                for item in protected
                if item.bbox[1] > source_top
                and item.bbox[1] < safe[3]
                and _horizontal_overlap(item.bbox, safe) > 0.5
            ]
            if blockers:
                safe = (safe[0], safe[1], safe[2], min(safe[3], min(item[1] for item in blockers) - 1.0))
            packed = _pack_stacked_node_labels(template, safe, group, adjusted)
            if packed is None:
                continue
            adjusted.update({item.container_id: item for item in packed})
            repaired.update(item.container_id for item in packed)

    vertical_groups = {}
    for item in containers.values():
        if not item.node_id or item.role != "vertical_node_text":
            continue
        vertical_groups.setdefault((item.node_id, item.allowed_bbox), []).append(item)
    for (node_id, safe), members in vertical_groups.items():
        packed = _pack_vertical_node_labels(
            template,
            nodes[node_id],
            members,
            adjusted,
            safe=safe,
        )
        if packed is None:
            continue
        adjusted.update({item.container_id: item for item in packed})
        repaired.update(item.container_id for item in packed)

    for container_id, placement in tuple(adjusted.items()):
        container = containers[container_id]
        if (
            placement.fit
            or container.node_id is not None
            or container.role not in {"independent_label", "connector_label", "title"}
        ):
            continue
        fitted = _fit_elastic_local_label(template, container, placement)
        if fitted is None:
            continue
        adjusted[container_id] = fitted
        repaired.add(container_id)

    for container_id, placement in tuple(adjusted.items()):
        container = containers[container_id]
        if container.node_id is not None:
            continue
        source_hits = _connector_hit_count(template, container.source_bbox)
        output_hits = _connector_hit_count(
            template,
            placement.glyph_bbox or placement.output_bbox,
        )
        if output_hits <= source_hits:
            continue
        fitted = _fit_elastic_local_label(
            template,
            container,
            placement,
            maximum_connector_hits=source_hits,
            preserve_vertical_anchor=True,
        )
        if fitted is None:
            continue
        adjusted[container_id] = replace(
            fitted,
            fit_profile="p18-connector-safe-" + fitted.fit_profile,
        )
        repaired.add(container_id)

    adjusted, anchored = _anchor_compact_independent_labels(template, adjusted)
    repaired.update(anchored)

    return (
        replace(
            plan,
            placements=tuple(adjusted[item.container_id] for item in plan.placements),
        ),
        repaired,
    )


def _anchor_compact_independent_labels(
    template: CompositePageTemplate,
    placements: dict[str, DiagramPlacement],
) -> tuple[dict[str, DiagramPlacement], set[str]]:
    containers = {
        item.container_id: item for item in template.diagram_template.containers
    }
    adjusted = dict(placements)
    repaired: set[str] = set()
    for container_id, placement in tuple(adjusted.items()):
        container = containers[container_id]
        source_height = container.source_bbox[3] - container.source_bbox[1]
        output_height = placement.output_bbox[3] - placement.output_bbox[1]
        if (
            container.node_id is not None
            or container.role not in {"independent_label", "independent_paragraph", "connector_label"}
            or len(placement.translated_text.strip()) > 80
            or source_height > max(32.0, placement.font_size * 3.2)
            or output_height > source_height + 1.0
        ):
            continue
        source_center = (container.source_bbox[1] + container.source_bbox[3]) / 2.0
        output_center = (placement.output_bbox[1] + placement.output_bbox[3]) / 2.0
        shift = source_center - output_center
        if abs(shift) <= 0.5:
            continue
        candidate_output = (
            placement.output_bbox[0],
            placement.output_bbox[1] + shift,
            placement.output_bbox[2],
            placement.output_bbox[3] + shift,
        )
        if (
            candidate_output[1] < container.allowed_bbox[1] - 0.01
            or candidate_output[3] > container.allowed_bbox[3] + 0.01
        ):
            continue
        glyph = placement.glyph_bbox or placement.output_bbox
        candidate_glyph = (glyph[0], glyph[1] + shift, glyph[2], glyph[3] + shift)
        if _connector_hit_count(template, candidate_glyph) > _connector_hit_count(
            template,
            container.source_bbox,
        ):
            continue
        collision = False
        for other_id, other in adjusted.items():
            if other_id == container_id:
                continue
            other_container = containers[other_id]
            if (
                _intersection_area(container.source_bbox, other_container.source_bbox) <= 0.5
                and _intersection_area(
                    candidate_glyph,
                    other.glyph_bbox or other.output_bbox,
                )
                > 0.5
            ):
                collision = True
                break
        if collision:
            continue
        adjusted[container_id] = replace(
            placement,
            output_bbox=tuple(round(value, 4) for value in candidate_output),
            glyph_bbox=tuple(round(value, 4) for value in candidate_glyph),
            fit_profile=placement.fit_profile + "+p18_source_band_anchor",
        )
        repaired.add(container_id)
    return adjusted, repaired


def _pack_semantic_list(template, safe, members, placements):
    members = sorted(members, key=lambda item: (item.source_bbox[1], item.source_bbox[0]))
    width = safe[2] - safe[0]
    if width <= 4.0 or safe[3] - safe[1] <= 4.0:
        return None
    member_ids = {item.container_id for item in members}
    source_top = max(safe[1], min(item.source_bbox[1] for item in members))
    blocker_bottoms = [
        item.source_bbox[3] + 1.0
        for item in template.diagram_template.containers
        if item.container_id not in member_ids
        and item.source_bbox[1] < source_top
        and item.source_bbox[3] > safe[1]
        and _horizontal_overlap(item.source_bbox, safe) > 0.5
    ]
    earliest_top = max([safe[1], *blocker_bottoms])
    profiles = (1.0, 0.95, 0.90, 0.85, 0.80, 0.75, 0.70, 0.68)
    selected = None
    for top in dict.fromkeys((source_top, earliest_top)):
        available_height = safe[3] - top
        for scale in profiles:
            measured = []
            for container in members:
                placement = placements[container.container_id]
                if container.role == "list_item":
                    placement = replace(
                        placement,
                        translated_text=_bind_list_marker(placement.translated_text),
                    )
                size = max(5.0, container.font_size * scale)
                line_height = 1.08 if container.role == "list_item" else 1.0
                height = _minimum_text_height(
                    template.width,
                    template.height,
                    width,
                    placement.translated_text,
                    size,
                    line_height,
                    placement.font_file,
                    placement.font_resource,
                    placement.color_srgb,
                )
                measured.append((container, placement, size, line_height, height))
            gap = max(1.0, min(item[2] for item in measured) * 0.20)
            required = sum(item[4] for item in measured) + gap * max(0, len(measured) - 1)
            if required <= available_height + 0.01:
                selected = (measured, gap, scale, top)
                break
        if selected is not None:
            break
    if selected is None:
        return None
    measured, gap, scale, cursor = selected
    output = []
    for container, placement, size, line_height, height in measured:
        bbox = (
            round(safe[0], 4),
            round(cursor, 4),
            round(safe[2], 4),
            round(cursor + height, 4),
        )
        output.append(
            replace(
                placement,
                output_bbox=bbox,
                font_size=round(size, 4),
                line_height=line_height,
                alignment="CENTER" if container.role == "list_heading" else "LEFT",
                fit_profile=f"p18-semantic-list-reflow-{scale:.2f}",
                fit=True,
                glyph_bbox=_measured_glyph_bbox(
                    template,
                    bbox,
                    placement.translated_text,
                    placement,
                    size=size,
                    line_height=line_height,
                    alignment="CENTER" if container.role == "list_heading" else "LEFT",
                ),
            )
        )
        cursor += height + gap
    return tuple(output)


def _bind_list_marker(text: str) -> str:
    return re.sub(
        r"^([\u2022\u25cf\u25aa\u25e6\u2023\u00b7\u2192\u27a2\u25ba\u25b8])\s+",
        lambda match: match.group(1) + "\u00a0",
        text,
        count=1,
    )


def _stacked_node_text_groups(members):
    groups = []
    for member in sorted(members, key=lambda item: (item.source_bbox[0], item.source_bbox[1])):
        group = next(
            (
                values
                for values in groups
                if any(
                    _horizontal_overlap(member.source_bbox, other.source_bbox)
                    >= min(
                        member.source_bbox[2] - member.source_bbox[0],
                        other.source_bbox[2] - other.source_bbox[0],
                    )
                    * 0.5
                    for other in values
                )
            ),
            None,
        )
        if group is None:
            groups.append([member])
        else:
            group.append(member)
    output = []
    for group in groups:
        ordered = sorted(group, key=lambda item: (item.source_bbox[1], item.source_bbox[0]))
        if len(ordered) < 2:
            continue
        if any(left.source_bbox[3] > right.source_bbox[1] + 0.5 for left, right in zip(ordered, ordered[1:])):
            continue
        output.append(ordered)
    return output


def _pack_stacked_node_labels(template, safe, members, placements):
    width = safe[2] - safe[0]
    if width <= 4.0 or safe[3] - safe[1] <= 4.0:
        return None
    ordered = sorted(members, key=lambda item: (item.source_bbox[1], item.source_bbox[0]))
    gaps = [
        min(4.0, max(1.0, (right.source_bbox[1] - left.source_bbox[3]) * 0.2))
        for left, right in zip(ordered, ordered[1:])
    ]
    source_top = max(safe[1], ordered[0].source_bbox[1])
    selected = None
    for top in dict.fromkeys((source_top, safe[1])):
        for scale in (1.0, 0.95, 0.90, 0.85, 0.80, 0.75, 0.70, 0.68):
            measured = []
            for container in ordered:
                placement = placements[container.container_id]
                size = max(5.0, container.font_size * scale)
                height = _minimum_text_height(
                    template.width,
                    template.height,
                    width,
                    placement.translated_text,
                    size,
                    1.0,
                    placement.font_file,
                    placement.font_resource,
                    placement.color_srgb,
                )
                measured.append((container, placement, size, height))
            required = sum(item[3] for item in measured) + sum(gaps)
            if required <= safe[3] - top + 0.01:
                selected = (top, scale, measured)
                break
        if selected is not None:
            break
    if selected is None:
        return None
    cursor, scale, measured = selected
    output = []
    for index, (container, placement, size, height) in enumerate(measured):
        bbox = (safe[0], cursor, safe[2], cursor + height)
        output.append(
            replace(
                placement,
                output_bbox=tuple(round(value, 4) for value in bbox),
                font_size=round(size, 4),
                line_height=1.0,
                alignment="CENTER",
                fit_profile=f"p18-stacked-node-reflow-{scale:.2f}",
                fit=True,
                glyph_bbox=_measured_glyph_bbox(
                    template,
                    bbox,
                    placement.translated_text,
                    placement,
                    size=size,
                    line_height=1.0,
                    alignment="CENTER",
                ),
            )
        )
        cursor += height + (gaps[index] if index < len(gaps) else 0.0)
    return tuple(output)


def _pack_vertical_node_labels(template, node, members, placements, *, safe=None):
    safe = safe or node.safe_text_bbox
    non_vertical = [
        item
        for item in template.diagram_template.containers
        if item.node_id == node.node_id and item.role != "vertical_node_text"
        and _horizontal_overlap(item.source_bbox, safe) > 0.5
    ]
    bottom_starts = [
        item.source_bbox[1]
        for item in non_vertical
        if item.source_bbox[1] >= safe[1] + (safe[3] - safe[1]) * 0.55
    ]
    top = safe[1]
    bottom = min([safe[3], *(value - 1.5 for value in bottom_starts)])
    if bottom <= top + 8.0:
        return None
    ordered = sorted(members, key=lambda item: (item.source_bbox[0], item.container_id))
    gap = 0.6
    lane_width = (safe[2] - safe[0] - gap * max(0, len(ordered) - 1)) / len(ordered)
    if lane_width <= 4.0:
        return None
    output = []
    for index, container in enumerate(ordered):
        placement = placements[container.container_id]
        left = safe[0] + index * (lane_width + gap)
        bbox = (left, top, left + lane_width, bottom)
        normalized = " ".join(placement.translated_text.split())
        variants = ((normalized, "one-column"),)
        two_column = _balanced_two_column_text(normalized)
        if two_column != normalized:
            font = fitz.Font(fontfile=placement.font_file)
            source_length = font.text_length(normalized, fontsize=container.font_size)
            if source_length > bottom - top:
                variants = ((two_column, "two-column"), *variants)
            else:
                variants = (*variants, (two_column, "two-column"))
        selected = None
        minimum = max(4.0, container.font_size * 0.50)
        sizes = []
        size = container.font_size
        while size >= minimum - 0.01:
            sizes.append(round(size, 2))
            size -= 0.5
        if not sizes or sizes[-1] > minimum + 0.01:
            sizes.append(round(minimum, 2))
        for size in sizes:
            for text, profile in variants:
                spare = _probe_vertical_text(
                    template.width,
                    template.height,
                    bbox,
                    text,
                    size,
                    placement.font_file,
                    placement.font_resource,
                )
                if spare >= 0:
                    selected = (text, profile, size, spare)
                    break
            if selected is not None:
                break
        if selected is None:
            return None
        text, profile, size, _ = selected
        rounded_bbox = tuple(round(value, 4) for value in bbox)
        output.append(
            replace(
                placement,
                translated_text=text,
                output_bbox=rounded_bbox,
                font_size=round(size, 4),
                line_height=1.0,
                alignment="VERTICAL",
                fit_profile=f"p18-vertical-{profile}",
                fit=True,
                glyph_bbox=_measured_glyph_bbox(
                    template,
                    rounded_bbox,
                    text,
                    placement,
                    size=size,
                    line_height=1.0,
                    alignment="VERTICAL",
                ),
            )
        )
    return tuple(output)


def _balanced_two_column_text(text: str) -> str:
    words = text.split()
    if len(words) >= 2:
        candidates = [
            (abs(len(" ".join(words[:index])) - len(" ".join(words[index:]))), index)
            for index in range(1, len(words))
        ]
        _, index = min(candidates)
        return " ".join(words[:index]) + "\n" + " ".join(words[index:])
    compact = "".join(text.split())
    if len(compact) >= 4:
        midpoint = len(compact) // 2
        return compact[:midpoint] + "\n" + compact[midpoint:]
    return text


def _probe_vertical_text(
    width: float,
    height: float,
    bbox: Rect,
    text: str,
    size: float,
    font_file: str,
    resource: str,
) -> float:
    with fitz.open() as document:
        page = document.new_page(width=width, height=height)
        return float(
            page.insert_textbox(
                fitz.Rect(bbox),
                text,
                fontname=resource,
                fontfile=font_file,
                fontsize=size,
                lineheight=1.0,
                align=fitz.TEXT_ALIGN_CENTER,
                rotate=90,
                overlay=True,
            )
        )


def _measured_glyph_bbox(
    template,
    bbox,
    text,
    placement,
    *,
    size,
    line_height,
    alignment,
):
    with fitz.open() as document:
        page = document.new_page(width=template.width, height=template.height)
        spare = page.insert_textbox(
            fitz.Rect(bbox),
            text,
            fontname=placement.font_resource,
            fontfile=placement.font_file,
            fontsize=size,
            lineheight=line_height,
            align={
                "LEFT": fitz.TEXT_ALIGN_LEFT,
                "CENTER": fitz.TEXT_ALIGN_CENTER,
                "RIGHT": fitz.TEXT_ALIGN_RIGHT,
                "VERTICAL": fitz.TEXT_ALIGN_CENTER,
            }[alignment],
            rotate=90 if alignment == "VERTICAL" else 0,
            overlay=True,
        )
        if spare < 0:
            return None
        rects = [
            fitz.Rect(span["bbox"])
            for block in page.get_text("dict")["blocks"]
            if block.get("type") == 0
            for line in block.get("lines", ())
            for span in line.get("spans", ())
            if span.get("text")
        ]
        if not rects:
            return None
        result = rects[0]
        for rect in rects[1:]:
            result |= rect
        return tuple(float(value) for value in result)


def _fit_elastic_local_label(
    template,
    container,
    placement,
    *,
    maximum_connector_hits: int | None = None,
    preserve_vertical_anchor: bool = False,
):
    fit_bbox = container.allowed_bbox
    if preserve_vertical_anchor:
        fit_bbox = (
            container.allowed_bbox[0],
            max(container.allowed_bbox[1], placement.output_bbox[1]),
            container.allowed_bbox[2],
            container.allowed_bbox[3],
        )
    minimum = max(4.5, container.font_size * 0.50)
    sizes = []
    size = container.font_size
    while size >= minimum - 0.01:
        sizes.append(round(size, 2))
        size -= 0.25
    if not sizes or sizes[-1] > minimum + 0.01:
        sizes.append(round(minimum, 2))
    for size in sizes:
        for line_height in (1.0, 0.95, 0.90):
            spare, glyph_bbox = _probe_horizontal_text(
                template.width,
                template.height,
                fit_bbox,
                placement.translated_text,
                size,
                line_height,
                placement.font_file,
                placement.font_resource,
                container.alignment,
            )
            if spare < 0:
                continue
            if (
                maximum_connector_hits is not None
                and glyph_bbox is not None
                and _connector_hit_count(template, glyph_bbox) > maximum_connector_hits
            ):
                continue
            return replace(
                placement,
                output_bbox=fit_bbox,
                font_size=round(size, 4),
                line_height=line_height,
                alignment=container.alignment,
                fit_profile=f"p18-elastic-local-label-{size:.2f}-{line_height:.2f}",
                fit=True,
                glyph_bbox=glyph_bbox,
            )
    return None


def _connector_hit_count(template, rect: Rect) -> int:
    expanded = fitz.Rect(
        rect[0] - 0.4,
        rect[1] - 0.4,
        rect[2] + 0.4,
        rect[3] + 0.4,
    )
    return sum(
        expanded.intersects(
            fitz.Rect(
                min(edge.start[0], edge.end[0]),
                min(edge.start[1], edge.end[1]),
                max(edge.start[0], edge.end[0]) + 0.01,
                max(edge.start[1], edge.end[1]) + 0.01,
            )
        )
        for edge in template.diagram_template.connectors
    )


def _probe_horizontal_text(
    width: float,
    height: float,
    bbox: Rect,
    text: str,
    size: float,
    line_height: float,
    font_file: str,
    resource: str,
    alignment: str,
) -> tuple[float, Rect | None]:
    with fitz.open() as document:
        page = document.new_page(width=width, height=height)
        spare = page.insert_textbox(
            fitz.Rect(bbox),
            text,
            fontname=resource,
            fontfile=font_file,
            fontsize=size,
            lineheight=line_height,
            align={
                "LEFT": fitz.TEXT_ALIGN_LEFT,
                "CENTER": fitz.TEXT_ALIGN_CENTER,
                "RIGHT": fitz.TEXT_ALIGN_RIGHT,
            }.get(alignment, fitz.TEXT_ALIGN_LEFT),
            overlay=True,
        )
        if spare < 0:
            return float(spare), None
        glyphs = [
            fitz.Rect(span["bbox"])
            for block in page.get_text("dict")["blocks"]
            if block.get("type") == 0
            for line in block.get("lines", ())
            for span in line.get("spans", ())
            if span.get("text")
        ]
        if not glyphs:
            return float(spare), None
        union = glyphs[0]
        for glyph in glyphs[1:]:
            union |= glyph
        return float(spare), tuple(float(value) for value in union)


def _expand_flow_to_owner_width(
    facts: PageFacts,
    template: CompositePageTemplate,
    flow_plan,
):
    containers = {
        item.base_container_id: item
        for item in template.containers
        if item.owner in {"flow", "shared"}
        and item.role not in {"margin", "heading", "title"}
    }
    source_containers = {
        item.container_id: item for item in template.flow_template.containers
    }
    adjusted = []
    for placement in flow_plan.placements:
        container = containers.get(placement.container_id)
        if container is None:
            adjusted.append(placement)
            continue
        x0, y0, x1, y1 = placement.output_bbox
        safe_left, _, safe_right, _ = container.allowed_bbox
        if safe_right - safe_left <= x1 - x0 + 0.5:
            adjusted.append(placement)
            continue
        font_file, font_resource = _font_variant(
            flow_plan.font_file,
            flow_plan.font_resource,
            placement.font_weight,
        )
        translated_text = placement.translated_text
        new_left = safe_left
        new_right = safe_right
        lane_limited = False
        for image in facts.image_objects:
            if _source_text_backdrop(facts, image):
                continue
            if _vertical_overlap(container.source_bbox, image.bbox) <= 0.5:
                continue
            if container.source_bbox[2] <= image.bbox[0] + 1.0:
                candidate_right = min(new_right, image.bbox[0] - 1.5)
                if candidate_right - new_left >= max(48.0, placement.font_size * 8.0):
                    new_right = candidate_right
                    lane_limited = True
            elif container.source_bbox[0] >= image.bbox[2] - 1.0:
                candidate_left = max(new_left, image.bbox[2] + 1.5)
                if new_right - candidate_left >= max(48.0, placement.font_size * 8.0):
                    new_left = candidate_left
                    lane_limited = True
        source = source_containers[container.base_container_id]
        short_post_diagram_label = (
            container.role == "body"
            and container.source_bbox[1] >= template.diagram_region[3] - 2.0
            and container.source_bbox[3] - container.source_bbox[1]
            <= max(2.0, source.font_size * 1.8)
            and len(container.source_text.strip()) <= 60
        )
        if short_post_diagram_label:
            translated_text = " ".join(translated_text.split())
            font = fitz.Font(fontfile=font_file)
            elastic_font_size = max(
                placement.font_size,
                source.font_size * 1.12,
            )
            required_width = max(
                container.source_bbox[2] - container.source_bbox[0],
                font.text_length(translated_text, fontsize=elastic_font_size)
                + elastic_font_size * 0.7,
            )
            if required_width <= safe_right - safe_left + 0.01:
                source_center = (
                    container.source_bbox[0] + container.source_bbox[2]
                ) / 2.0
                new_left = min(
                    max(safe_left, source_center - required_width / 2.0),
                    safe_right - required_width,
                )
                new_right = new_left + required_width
        height = _minimum_text_height(
            template.width,
            template.height,
            new_right - new_left,
            translated_text,
            placement.font_size,
            placement.line_height,
            font_file,
            font_resource,
            placement.color_srgb,
        )
        policy = (
            "p18_short_label_centered_expand"
            if short_post_diagram_label
            else (
                "p18_image_side_lane"
                if lane_limited
                else "p18_owner_safe_width"
            )
        )
        output_height = min(y1 - y0, height)
        if lane_limited:
            available_height = container.allowed_bbox[3] - y0
            if height <= available_height + 0.01:
                output_height = height
            else:
                new_left = safe_left
                new_right = safe_right
                lane_limited = False
                policy = "p18_owner_safe_width"
                height = _minimum_text_height(
                    template.width,
                    template.height,
                    new_right - new_left,
                    translated_text,
                    placement.font_size,
                    placement.line_height,
                    font_file,
                    font_resource,
                    placement.color_srgb,
                )
                output_height = min(y1 - y0, height)
        adjusted.append(
            replace(
                placement,
                translated_text=translated_text,
                output_bbox=(
                    round(new_left, 4),
                    y0,
                    round(new_right, 4),
                    round(y0 + output_height, 4),
                ),
                horizontal_policy=placement.horizontal_policy + "+" + policy,
            )
        )
    return replace(flow_plan, placements=tuple(adjusted))


def _expand_structural_headings(
    facts: PageFacts,
    template: CompositePageTemplate,
    flow_plan,
):
    containers = {
        item.base_container_id: item
        for item in template.containers
        if item.owner in {"flow", "shared"} and item.role in {"heading", "title"}
    }
    adjusted = []
    for placement in flow_plan.placements:
        container = containers.get(placement.container_id)
        if container is None:
            adjusted.append(placement)
            continue
        x0, y0, x1, y1 = placement.output_bbox
        safe_left, _, safe_right, _ = container.allowed_bbox
        for image in facts.image_objects:
            if _source_text_backdrop(facts, image):
                continue
            if _vertical_overlap(container.source_bbox, image.bbox) <= 0.5:
                continue
            if container.source_bbox[2] <= image.bbox[0] + 1.0:
                safe_right = min(safe_right, image.bbox[0] - 1.5)
            elif container.source_bbox[0] >= image.bbox[2] - 1.0:
                safe_left = max(safe_left, image.bbox[2] + 1.5)
        side_structural_heading = (
            _vertical_overlap(container.source_bbox, template.diagram_region) > 0.5
            and _horizontal_overlap(container.source_bbox, template.diagram_region) <= 0.5
        )
        if side_structural_heading:
            if container.source_bbox[0] >= template.diagram_region[2] - 2.0:
                lane_left = max(safe_left, container.source_bbox[0])
                lane_right = safe_right
            else:
                lane_left = safe_left
                lane_right = min(safe_right, container.source_bbox[2])
            font_file, font_resource = _font_variant(
                flow_plan.font_file,
                flow_plan.font_resource,
                placement.font_weight,
            )
            height = _minimum_text_height(
                template.width,
                template.height,
                lane_right - lane_left,
                placement.translated_text,
                placement.font_size,
                placement.line_height,
                font_file,
                font_resource,
                placement.color_srgb,
            )
            anchor_top = container.source_bbox[1]
            for other in flow_plan.placements:
                if (
                    other.container_id != placement.container_id
                    and other.output_bbox[1] < container.source_bbox[1]
                    and _horizontal_overlap(
                        other.output_bbox,
                        (lane_left, anchor_top, lane_right, anchor_top + height),
                    )
                    > 0.5
                ):
                    anchor_top = max(
                        anchor_top,
                        other.output_bbox[3] + max(1.0, placement.font_size * 0.35),
                    )
            if anchor_top + height <= container.allowed_bbox[3] + 0.01:
                adjusted.append(
                    replace(
                        placement,
                        output_bbox=(
                            round(lane_left, 4),
                            round(anchor_top, 4),
                            round(lane_right, 4),
                            round(anchor_top + height, 4),
                        ),
                        horizontal_policy=placement.horizontal_policy
                        + "+p18_side_structure_lane_expand",
                        vertical_policy=placement.vertical_policy + "+p18_side_structure_anchor",
                    )
                )
                continue
        family = container.base_container_id.split("-segment-", 1)[0]
        fragment_siblings = [
            item
            for item in containers.values()
            if item.composite_id != container.composite_id
            and item.base_container_id.split("-segment-", 1)[0] == family
            and _vertical_overlap(item.source_bbox, container.source_bbox) > 0.5
        ]
        if fragment_siblings:
            sibling_ids = {item.base_container_id for item in fragment_siblings}
            row_top = min(
                [y0]
                + [
                    item.output_bbox[1]
                    for item in flow_plan.placements
                    if item.container_id in sibling_ids
                ]
            )
            font_file, font_resource = _font_variant(
                flow_plan.font_file,
                flow_plan.font_resource,
                placement.font_weight,
            )
            translated_text = " ".join(placement.translated_text.split())
            font_size = _fragment_heading_common_font_size(
                flow_plan,
                containers,
                family,
            )
            height = _minimum_text_height(
                template.width,
                template.height,
                safe_right - safe_left,
                translated_text,
                font_size,
                placement.line_height,
                font_file,
                font_resource,
                placement.color_srgb,
            )
            has_right_sibling = any(
                item.source_bbox[0] >= container.source_bbox[2] - 0.01
                for item in fragment_siblings
            )
            policy = (
                "safe_heading_left_whitespace_expand"
                if has_right_sibling
                else "p18_safe_heading_expand"
            )
            adjusted.append(
                replace(
                    placement,
                    translated_text=translated_text,
                    output_bbox=(
                        round(safe_left, 4),
                        round(row_top, 4),
                        round(safe_right, 4),
                        round(row_top + height, 4),
                    ),
                    font_size=font_size,
                    horizontal_policy=placement.horizontal_policy + "+" + policy,
                )
            )
            continue
        allowed_right = safe_right
        left_whitespace = container.source_bbox[0] - safe_left
        right_whitespace = allowed_right - container.source_bbox[2]
        if (
            left_whitespace > max(placement.font_size * 2.0, right_whitespace * 3.0)
            and right_whitespace <= placement.font_size * 1.5
        ):
            font_file, font_resource = _font_variant(
                flow_plan.font_file,
                flow_plan.font_resource,
                placement.font_weight,
            )
            translated_text = " ".join(placement.translated_text.split())
            height = _minimum_text_height(
                template.width,
                template.height,
                allowed_right - safe_left,
                translated_text,
                placement.font_size,
                placement.line_height,
                font_file,
                font_resource,
                placement.color_srgb,
            )
            adjusted.append(
                replace(
                    placement,
                    translated_text=translated_text,
                    output_bbox=(
                        round(safe_left, 4),
                        y0,
                        round(allowed_right, 4),
                        round(y0 + height, 4),
                    ),
                    horizontal_policy=placement.horizontal_policy
                    + "+safe_heading_left_whitespace_expand",
                )
            )
            continue
        for other in flow_plan.placements:
            if other.container_id == placement.container_id:
                continue
            if other.output_bbox[0] < x1 - 0.01:
                continue
            if _vertical_overlap(other.output_bbox, placement.output_bbox) <= 0.5:
                continue
            safe_right = min(safe_right, other.output_bbox[0] - 1.0)
        for other in template.containers:
            if other.composite_id == container.composite_id:
                continue
            if other.source_bbox[0] < container.source_bbox[2] - 0.01:
                continue
            if _vertical_overlap(other.source_bbox, container.source_bbox) <= 0.5:
                continue
            safe_right = min(safe_right, other.source_bbox[0] - 1.0)
        for item in facts.text_objects:
            if item.object_id in container.source_object_ids:
                continue
            if item.bbox[0] < container.source_bbox[2] - 0.01:
                continue
            if _vertical_overlap(item.bbox, container.source_bbox) <= 0.5:
                continue
            safe_right = min(safe_right, item.bbox[0] - 1.0)
        shrinking = safe_right < x1 - 0.1
        if not shrinking and safe_right <= x1 + max(2.0, placement.font_size * 0.5):
            adjusted.append(placement)
            continue
        font_file, font_resource = _font_variant(
            flow_plan.font_file,
            flow_plan.font_resource,
            placement.font_weight,
        )
        height = _minimum_text_height(
            template.width,
            template.height,
            safe_right - x0,
            placement.translated_text,
            placement.font_size,
            placement.line_height,
            font_file,
            font_resource,
            placement.color_srgb,
        )
        if not shrinking and height >= y1 - y0 - 0.1:
            adjusted.append(placement)
            continue
        adjusted.append(
            replace(
                placement,
                output_bbox=(x0, y0, round(safe_right, 4), round(y0 + height, 4)),
                horizontal_policy=placement.horizontal_policy + "+p18_safe_heading_expand",
            )
        )
    return replace(flow_plan, placements=tuple(adjusted))


def _compact_flow_before_images(
    facts: PageFacts,
    template: CompositePageTemplate,
    flow_plan,
):
    containers = {
        item.base_container_id: item
        for item in template.containers
        if item.owner in {"flow", "shared"} and item.role != "margin"
    }
    adjusted = {item.container_id: item for item in flow_plan.placements}
    for image in sorted(facts.image_objects, key=lambda item: item.bbox[1]):
        if _source_text_backdrop(facts, image):
            continue
        image_top = image.bbox[1]
        members = [
            item
            for item in flow_plan.placements
            if item.container_id in containers
            and containers[item.container_id].source_bbox[3] <= image_top - 0.5
            and _horizontal_overlap(
                containers[item.container_id].source_bbox,
                image.bbox,
            )
            > 0.5
            and _horizontal_overlap(item.output_bbox, image.bbox) > 0.5
        ]
        if not members or max(adjusted[item.container_id].output_bbox[3] for item in members) <= image_top - 0.5:
            continue
        cursor = image_top - 1.5
        for item in reversed(sorted(members, key=lambda value: (value.output_bbox[1], value.output_bbox[0]))):
            current = adjusted[item.container_id]
            x0, y0, x1, y1 = current.output_bbox
            height = y1 - y0
            if y1 > cursor:
                y1 = cursor
                y0 = y1 - height
                allowed_top = containers[item.container_id].allowed_bbox[1]
                if y0 < allowed_top - 0.01:
                    continue
                current = replace(
                    current,
                    output_bbox=(x0, round(y0, 4), x1, round(y1, 4)),
                    vertical_policy=current.vertical_policy + "+p18_image_blocker_compact",
                )
                adjusted[item.container_id] = current
            cursor = current.output_bbox[1] - max(2.0, current.font_size * 0.65)
    return replace(
        flow_plan,
        placements=tuple(adjusted[item.container_id] for item in flow_plan.placements),
    )


def _flow_image_findings(
    facts: PageFacts,
    template: CompositePageTemplate,
    placements: dict[str, DiagramPlacement],
) -> tuple[CompositeFinding, ...]:
    containers = {item.composite_id: item for item in template.containers}
    findings = []
    for container_id, placement in placements.items():
        container = containers[container_id]
        if container.owner not in {"flow", "shared"} or container.role == "margin":
            continue
        for image in facts.image_objects:
            if _source_text_backdrop(facts, image):
                continue
            source_overlap = _intersection_area(container.source_bbox, image.bbox)
            candidate_overlap = _intersection_area(
                placement.glyph_bbox or placement.output_bbox,
                image.bbox,
            )
            if source_overlap <= 0.5 and candidate_overlap > 0.5:
                findings.append(
                    CompositeFinding(
                        code="P18_FLOW_IMAGE_COLLISION",
                        severity="HARD",
                        owner="composite_layout_planner",
                        container_id=container_id,
                        message="Translated flow text newly overlapped a native image.",
                        evidence={
                            "image_object_id": image.object_id,
                            "candidate_overlap": round(candidate_overlap, 4),
                            "source_overlap": round(source_overlap, 4),
                        },
                    )
                )
    return tuple(findings)


def _source_text_backdrop(facts: PageFacts, image) -> bool:
    edge_banner = (
        image.bbox[2] - image.bbox[0] >= facts.width * 0.85
        and (
            image.bbox[1] <= max(1.0, facts.height * 0.01)
            or image.bbox[3] >= facts.height - max(1.0, facts.height * 0.01)
        )
    )
    return edge_banner and any(
        _intersection_area(item.bbox, image.bbox) > 0.5
        for item in facts.text_objects
    )


def _fragment_heading_common_font_size(flow_plan, containers, family: str) -> float:
    members = [
        item
        for item in containers.values()
        if item.base_container_id.split("-segment-", 1)[0] == family
    ]
    placements = {
        item.container_id: item
        for item in flow_plan.placements
        if item.container_id in {member.base_container_id for member in members}
    }
    source_sizes = [placements[item.base_container_id].source_font_size for item in members]
    if not source_sizes or max(source_sizes) > min(source_sizes) * 1.05:
        return min(
            placements[item.base_container_id].font_size
            for item in members
        )
    source_size = min(source_sizes)
    for scale in (1.0, 0.96, 0.92, 0.88, 0.84, 0.80):
        size = round(source_size * scale, 4)
        if all(
            fitz.Font(
                fontfile=_font_variant(
                    flow_plan.font_file,
                    flow_plan.font_resource,
                    placements[item.base_container_id].font_weight,
                )[0]
            ).text_length(
                " ".join(placements[item.base_container_id].translated_text.split()),
                fontsize=size,
            )
            + size * 0.7
            <= item.allowed_bbox[2] - item.allowed_bbox[0] + 0.01
            for item in members
        ):
            return size
    return min(
        placements[item.base_container_id].font_size
        for item in members
    )


def _inline_preserved_prefixes(
    facts: PageFacts,
    template: CompositePageTemplate,
    flow_plan,
):
    source_by_id = {item.object_id: item for item in facts.text_objects}
    source_containers = {
        item.container_id: item
        for item in template.flow_template.containers
        if item.preserved_prefix
    }
    composite_containers = {
        item.base_container_id: item
        for item in template.containers
        if item.owner in {"flow", "shared"}
    }
    adjusted = []
    for placement in flow_plan.placements:
        source = source_containers.get(placement.container_id)
        if source is None:
            adjusted.append(placement)
            continue
        prefix = source.preserved_prefix or ""
        marker = next(
            (
                source_by_id[object_id]
                for object_id in source.source_object_ids
                if object_id in source_by_id
                and source_by_id[object_id].text.strip() == prefix
            ),
            None,
        )
        if marker is None:
            adjusted.append(placement)
            continue
        body = placement.translated_text.strip()
        if body.startswith(prefix):
            body = body[len(prefix) :].lstrip()
        translated_text = f"{prefix}  {body}"
        container = composite_containers[placement.container_id]
        x0 = min(marker.bbox[0], placement.output_bbox[0])
        x1 = max(placement.output_bbox[2], min(container.allowed_bbox[2], template.width - 8.0))
        font_file, font_resource = _font_variant(
            flow_plan.font_file,
            flow_plan.font_resource,
            placement.font_weight,
        )
        height = _minimum_text_height(
            template.width,
            template.height,
            x1 - x0,
            translated_text,
            placement.font_size,
            placement.line_height,
            font_file,
            font_resource,
            placement.color_srgb,
        )
        y0 = placement.output_bbox[1]
        adjusted.append(
            replace(
                placement,
                translated_text=translated_text,
                output_bbox=(round(x0, 4), y0, round(x1, 4), round(y0 + height, 4)),
                horizontal_policy=placement.horizontal_policy + "+p18_preserved_prefix_inline",
            )
        )
    return replace(flow_plan, placements=tuple(adjusted))


def _preserve_structural_flow_bands(
    facts: PageFacts,
    template: CompositePageTemplate,
    flow_plan,
    *,
    final_pass: bool = False,
):
    """Repack text inside diagram-bounded zones without changing horizontal structure."""
    flow_plan = _flatten_margin_rows(facts, template, flow_plan)
    containers = {
        item.base_container_id: item
        for item in template.containers
        if item.owner in {"flow", "shared"}
        and item.role != "margin"
    }
    placements = {item.container_id: item for item in flow_plan.placements}
    zones: dict[str, list] = {"above": [], "below": [], "left": [], "right": []}
    for container_id, placement in placements.items():
        if container_id not in containers:
            continue
        container = containers[container_id]
        zone = _structural_zone(container.source_bbox, template.diagram_region)
        if zone in zones:
            zones[zone].append((container, placement))

    adjusted = dict(placements)
    for zone, members in zones.items():
        if not members:
            continue
        safe_top = max(container.allowed_bbox[1] for container, _ in members)
        safe_bottom = min(container.allowed_bbox[3] for container, _ in members)
        zone_left = min(item.output_bbox[0] for _, item in members)
        zone_right = max(item.output_bbox[2] for _, item in members)
        if zone in {"left", "right"}:
            source_top = min(container.source_bbox[1] for container, _ in members)
            safe_top = max(safe_top, source_top)
            image_tops = [
                image.bbox[1]
                for image in facts.image_objects
                if not _source_text_backdrop(facts, image)
                and image.bbox[1] > source_top + 0.5
                and _horizontal_overlap(
                    (zone_left, 0.0, zone_right, template.height),
                    image.bbox,
                )
                > 0.5
            ]
            if image_tops:
                safe_bottom = min(safe_bottom, min(image_tops) - 1.5)
        diagram_bounds = [
            item.allowed_bbox
            for item in template.containers
            if item.owner == "diagram"
            and _horizontal_overlap(
                (zone_left, 0.0, zone_right, template.height),
                item.allowed_bbox,
            )
            > 0.5
        ]
        if zone == "above" and diagram_bounds:
            safe_bottom = min(safe_bottom, min(item[1] for item in diagram_bounds) - 0.75)
        if zone == "below":
            if diagram_bounds:
                safe_top = max(safe_top, max(item[3] for item in diagram_bounds) + 0.75)
            for margin in (
                item
                for item in flow_plan.placements
                if item.container_id not in containers
                and item.output_bbox[1] >= template.height * 0.85
            ):
                if _horizontal_overlap(
                    (zone_left, 0.0, zone_right, template.height),
                    margin.output_bbox,
                ) <= 0.5:
                    continue
                safe_bottom = min(
                    safe_bottom,
                    margin.output_bbox[1] - max(2.0, margin.font_size * 0.35),
                )

        packed = _elastic_zone_placements(
            template,
            flow_plan,
            members,
            safe_top=safe_top,
            safe_bottom=safe_bottom,
            preserve_font_cap=final_pass,
        )
        if packed is None:
            continue
        adjusted.update({item.container_id: item for item in packed})

    flow_plan = replace(
        flow_plan,
        placements=tuple(adjusted[item.container_id] for item in flow_plan.placements),
    )
    return _flatten_edge_headers(template, flow_plan)


def _structural_zone(allowed_bbox: Rect, diagram_region: Rect) -> str | None:
    if allowed_bbox[3] <= diagram_region[1] + 2.0:
        return "above"
    if allowed_bbox[1] >= diagram_region[3] - 2.0:
        return "below"
    if _vertical_overlap(allowed_bbox, diagram_region) > 0.5:
        if allowed_bbox[2] <= diagram_region[0] + 2.0:
            return "left"
        if allowed_bbox[0] >= diagram_region[2] - 2.0:
            return "right"
    return None


def _elastic_zone_placements(
    template: CompositePageTemplate,
    flow_plan,
    members,
    *,
    safe_top: float,
    safe_bottom: float,
    preserve_font_cap: bool,
):
    rows = _split_rows_with_overlapping_outputs(_source_rows(members))
    if not rows or safe_bottom <= safe_top + 1.0:
        return None
    gaps = [
        _row_gap(rows[index - 1], rows[index])
        for index in range(1, len(rows))
    ]
    profiles = (
        (1.12, 1.40),
        (1.08, 1.35),
        (1.04, 1.25),
        (1.00, 1.15),
        (0.96, 1.08),
        (0.92, 1.00),
        (0.88, 0.98),
        (0.84, 0.96),
        (0.80, 0.95),
        (0.75, 0.95),
        (0.72, 0.95),
    )
    selected = None
    for font_scale, line_height in profiles:
        measured = []
        for row in rows:
            row_items = []
            for container, placement in row:
                font_size = max(6.0, placement.source_font_size * font_scale)
                if preserve_font_cap:
                    font_size = min(font_size, placement.font_size)
                font_file, font_resource = _font_variant(
                    flow_plan.font_file,
                    flow_plan.font_resource,
                    placement.font_weight,
                )
                width = placement.output_bbox[2] - placement.output_bbox[0]
                height = _minimum_text_height(
                    template.width,
                    template.height,
                    width,
                    placement.translated_text,
                    font_size,
                    line_height,
                    font_file,
                    font_resource,
                    placement.color_srgb,
                )
                row_items.append((container, placement, font_size, height))
            measured.append(row_items)
        required = sum(max(item[3] for item in row) for row in measured) + sum(gaps)
        if required <= safe_bottom - safe_top + 0.01:
            selected = (line_height, measured)
            break
    if selected is None:
        return None

    line_height, measured = selected
    anchored_tops = []
    cursor = safe_top
    for index, (source_row, row) in enumerate(zip(rows, measured)):
        if index:
            cursor += gaps[index - 1]
        cursor = max(
            cursor,
            min(item.source_bbox[1] for item, _ in source_row),
        )
        anchored_tops.append(cursor)
        cursor += max(item[3] for item in row)
    if cursor > safe_bottom + 0.01:
        anchored_tops = []
        cursor = safe_top
        for index, row in enumerate(measured):
            if index:
                cursor += gaps[index - 1]
            anchored_tops.append(cursor)
            cursor += max(item[3] for item in row)

    packed = []
    for cursor, row in zip(anchored_tops, measured):
        for _, placement, font_size, height in row:
            x0, _, x1, _ = placement.output_bbox
            packed.append(
                replace(
                    placement,
                    output_bbox=(
                        x0,
                        round(cursor, 4),
                        x1,
                        round(cursor + height, 4),
                    ),
                    font_size=round(font_size, 4),
                    line_height=line_height,
                    vertical_policy=placement.vertical_policy
                    + "+p18_structural_elastic_reflow",
                    fit=True,
                )
            )
    return tuple(packed)


def _split_rows_with_overlapping_outputs(rows):
    output = []
    for row in rows:
        ordered = sorted(row, key=lambda item: (item[0].source_bbox[0], item[0].source_bbox[1]))
        overlaps = any(
            _horizontal_overlap(left[1].output_bbox, right[1].output_bbox) > 0.5
            for index, left in enumerate(ordered)
            for right in ordered[index + 1 :]
        )
        if overlaps:
            output.extend([[item] for item in ordered])
        else:
            output.append(ordered)
    return output


def _source_rows(members):
    rows = []
    for member in sorted(
        members,
        key=lambda item: (item[0].source_bbox[1], item[0].source_bbox[0]),
    ):
        container, _ = member
        if rows and _same_source_row(rows[-1], container):
            rows[-1].append(member)
        else:
            rows.append([member])
    return rows


def _same_source_row(row, container) -> bool:
    reference = row[0][0]
    same_fragment_family = (
        reference.base_container_id.split("-segment-", 1)[0]
        == container.base_container_id.split("-segment-", 1)[0]
        and _vertical_overlap(reference.source_bbox, container.source_bbox) > 0.5
    )
    if (
        not same_fragment_family
        and abs(reference.source_bbox[1] - container.source_bbox[1]) > 2.5
    ):
        return False
    return all(
        _horizontal_overlap(item.source_bbox, container.source_bbox) <= 0.5
        for item, _ in row
    )


def _row_gap(previous, current) -> float:
    source_gap = max(
        0.0,
        min(item.source_bbox[1] for item, _ in current)
        - max(item.source_bbox[3] for item, _ in previous),
    )
    return round(min(12.0, max(1.5, source_gap * 0.25)), 4)


def _flatten_margin_rows(
    facts: PageFacts,
    template: CompositePageTemplate,
    flow_plan,
):
    containers = {
        item.base_container_id: item
        for item in template.containers
        if item.owner in {"flow", "shared"}
        and item.role == "margin"
        and (
            item.source_bbox[1] <= template.height * 0.15
            or item.source_bbox[3] >= template.height * 0.85
        )
    }
    members = [
        (containers[item.container_id], item)
        for item in flow_plan.placements
        if item.container_id in containers
    ]
    rows = _source_rows(members)
    adjusted = {item.container_id: item for item in flow_plan.placements}
    for row in rows:
        if len(row) < 2:
            continue
        safe_left = max(8.0, min(item.allowed_bbox[0] for item, _ in row))
        safe_right = min(
            template.width - 8.0,
            max(item.allowed_bbox[2] for item, _ in row),
        )
        gap = max(1.0, min(item[1].font_size for item in row) * 0.45)
        widths = []
        heights = []
        for _, placement in row:
            font_file, font_resource = _font_variant(
                flow_plan.font_file,
                flow_plan.font_resource,
                placement.font_weight,
            )
            font = fitz.Font(fontfile=font_file)
            normalized = " ".join(placement.translated_text.split())
            width = max(
                placement.font_size * 1.5,
                font.text_length(normalized, fontsize=placement.font_size)
                + placement.font_size * 0.7,
            )
            height = _minimum_text_height(
                template.width,
                template.height,
                width,
                normalized,
                placement.font_size,
                placement.line_height,
                font_file,
                font_resource,
                placement.color_srgb,
            )
            widths.append(width)
            heights.append(height)
        required = sum(widths) + gap * (len(row) - 1)
        if required > safe_right - safe_left + 0.01:
            continue
        source_center = (
            min(item.source_bbox[0] for item, _ in row)
            + max(item.source_bbox[2] for item, _ in row)
        ) / 2.0
        row_top = min(item.source_bbox[1] for item, _ in row)
        row_bbox = (safe_left, row_top, safe_right, row_top + max(heights))
        source_object_ids = {
            object_id
            for container, _ in row
            for object_id in container.source_object_ids
        }
        protected_object_ids = set(template.protected_object_ids)
        blockers = [
            item.bbox
            for item in facts.text_objects
            if item.object_id in protected_object_ids
            and item.object_id not in source_object_ids
            and _vertical_overlap(row_bbox, item.bbox) > 0.5
        ]
        segments = _safe_horizontal_segments(
            safe_left,
            safe_right,
            blockers,
            clearance=max(1.0, gap * 0.5),
        )
        viable_segments = [
            segment
            for segment in segments
            if segment[1] - segment[0] >= required - 0.01
        ]
        if not viable_segments:
            continue
        segment_left, segment_right = min(
            viable_segments,
            key=lambda segment: (
                _distance_to_segment(source_center, segment),
                abs(source_center - (segment[0] + segment[1]) / 2.0),
            ),
        )
        cursor = min(
            max(segment_left, source_center - required / 2.0),
            segment_right - required,
        )
        for (_, placement), width, height in zip(row, widths, heights):
            adjusted[placement.container_id] = replace(
                placement,
                translated_text=" ".join(placement.translated_text.split()),
                output_bbox=(
                    round(cursor, 4),
                    round(row_top, 4),
                    round(cursor + width, 4),
                    round(row_top + height, 4),
                ),
                horizontal_policy="p18_margin_row_flatten",
                vertical_policy=placement.vertical_policy + "+p18_margin_row_flatten",
                fit=True,
            )
            cursor += width + gap
    return replace(
        flow_plan,
        placements=tuple(adjusted[item.container_id] for item in flow_plan.placements),
    )


def _safe_horizontal_segments(
    left: float,
    right: float,
    blockers: list[Rect],
    *,
    clearance: float,
) -> list[tuple[float, float]]:
    segments = [(left, right)]
    for blocker in blockers:
        cut_left = blocker[0] - clearance
        cut_right = blocker[2] + clearance
        remaining = []
        for segment_left, segment_right in segments:
            if cut_right <= segment_left or cut_left >= segment_right:
                remaining.append((segment_left, segment_right))
                continue
            if cut_left > segment_left:
                remaining.append((segment_left, min(cut_left, segment_right)))
            if cut_right < segment_right:
                remaining.append((max(cut_right, segment_left), segment_right))
        segments = remaining
    return segments


def _distance_to_segment(value: float, segment: tuple[float, float]) -> float:
    if value < segment[0]:
        return segment[0] - value
    if value > segment[1]:
        return value - segment[1]
    return 0.0


def _flatten_edge_headers(template: CompositePageTemplate, flow_plan):
    containers = {
        item.base_container_id: item
        for item in template.containers
        if item.owner in {"flow", "shared"}
        and item.role in {"heading", "title"}
        and (
            item.source_bbox[1] <= template.height * 0.15
            or item.source_bbox[3] >= template.height * 0.85
        )
    }
    source_containers = {
        item.container_id: item for item in template.flow_template.containers
    }
    adjusted = {item.container_id: item for item in flow_plan.placements}
    for placement in flow_plan.placements:
        container = containers.get(placement.container_id)
        if container is None:
            continue
        font_file, font_resource = _font_variant(
            flow_plan.font_file,
            flow_plan.font_resource,
            placement.font_weight,
        )
        source = source_containers[container.base_container_id]
        normalized = " ".join(placement.translated_text.split())
        if source.preserved_prefix and placement.translated_text.startswith(
            source.preserved_prefix
        ):
            normalized = source.preserved_prefix + "  " + " ".join(
                placement.translated_text[len(source.preserved_prefix) :].split()
            )
        font = fitz.Font(fontfile=font_file)
        safe_left, _, safe_right, _ = container.allowed_bbox
        minimum_size = source.font_size * 0.88
        fitted = None
        maximum_size = source.font_size * 1.08
        font_sizes = [
            max(minimum_size, min(maximum_size, placement.font_size * scale))
            for scale in (1.0, 0.96, 0.92, 0.88)
        ]
        font_sizes.append(minimum_size)
        for font_size in dict.fromkeys(font_sizes):
            width = (
                font.text_length(normalized, fontsize=font_size)
                + font_size * 0.7
            )
            if width <= safe_right - safe_left + 0.01:
                fitted = (font_size, width)
                break
        if fitted is None:
            continue
        font_size, width = fitted
        line_height = min(placement.line_height, 1.15)
        height = _minimum_text_height(
            template.width,
            template.height,
            width,
            normalized,
            font_size,
            line_height,
            font_file,
            font_resource,
            placement.color_srgb,
        )
        current_height = placement.output_bbox[3] - placement.output_bbox[1]
        if height >= current_height - 1.0:
            continue
        source_center = (container.source_bbox[0] + container.source_bbox[2]) / 2.0
        if source_center >= template.width * 0.55:
            x1 = max(
                safe_left + width,
                min(safe_right, max(container.source_bbox[2], placement.output_bbox[2])),
            )
            x0 = x1 - width
        elif source_center <= template.width * 0.45:
            x0 = min(
                safe_right - width,
                max(safe_left, min(container.source_bbox[0], placement.output_bbox[0])),
            )
            x1 = x0 + width
        else:
            x0 = min(
                max(safe_left, source_center - width / 2.0),
                safe_right - width,
            )
            x1 = x0 + width
        y0 = placement.output_bbox[1]
        horizontal_policy = "p18_edge_header_flatten"
        if _flow_alignment(placement.horizontal_policy) == "RIGHT":
            horizontal_policy += "+safe_heading_left_whitespace_expand"
        adjusted[placement.container_id] = replace(
            placement,
            translated_text=normalized,
            font_size=round(font_size, 4),
            line_height=line_height,
            output_bbox=(
                round(x0, 4),
                y0,
                round(x1, 4),
                round(y0 + height, 4),
            ),
            horizontal_policy=horizontal_policy,
            fit=True,
        )
    return replace(
        flow_plan,
        placements=tuple(adjusted[item.container_id] for item in flow_plan.placements),
    )


def _resolved_vertical_flow_findings(template, flow_plan, findings):
    containers = {
        item.base_container_id: item
        for item in template.containers
        if item.owner in {"flow", "shared"}
    }
    placements = {item.container_id: item for item in flow_plan.placements}
    resolvable = {
        "FLOW_P4_VERTICAL_PAGE_ESCAPE",
        "FLOW_P5_COLUMN_VERTICAL_ESCAPE",
    }
    output = []
    for finding in findings:
        container = containers.get(finding.container_id)
        placement = placements.get(finding.container_id)
        if (
            finding.code in resolvable
            and container is not None
            and placement is not None
            and placement.fit
            and _contains(container.allowed_bbox, placement.output_bbox, tolerance=0.35)
        ):
            continue
        output.append(finding)
    return output


def _placement_findings(
    template: CompositePageTemplate,
    plan: DiagramLayoutPlan,
) -> tuple[CompositeFinding, ...]:
    containers = {item.composite_id: item for item in template.containers}
    findings: list[CompositeFinding] = []
    for placement in plan.placements:
        container = containers[placement.container_id]
        if not placement.fit:
            findings.append(
                _finding(
                    "P18_CHILD_LAYOUT_UNFIT",
                    "HARD",
                    container.owner,
                    container.composite_id,
                    "Child leaf could not fit the translated text inside its owned region.",
                    output_bbox=placement.output_bbox,
                )
            )
            continue
        if not _contains(container.allowed_bbox, placement.output_bbox, tolerance=0.35):
            findings.append(
                _finding(
                    "P18_OWNER_ALLOWED_BBOX_CROSSED",
                    "HARD",
                    container.owner,
                    container.composite_id,
                    "Translated text left the owner-specific safe region.",
                    allowed_bbox=container.allowed_bbox,
                    output_bbox=placement.output_bbox,
                )
            )
        if container.owner in {"flow", "shared"}:
            source_overlap = _intersection_area(container.source_bbox, template.diagram_region)
            output_overlap = _intersection_area(placement.output_bbox, template.diagram_region)
            if output_overlap > source_overlap + 0.75:
                findings.append(
                    _finding(
                        "P18_FLOW_ENTERED_DIAGRAM_REGION",
                        "HARD",
                        container.owner,
                        container.composite_id,
                        "Flow/shared text newly entered the structural diagram region.",
                        source_overlap=round(source_overlap, 4),
                        output_overlap=round(output_overlap, 4),
                        diagram_region=template.diagram_region,
                    )
                )

    rows = list(plan.placements)
    for index, left in enumerate(rows):
        left_container = containers[left.container_id]
        for right in rows[index + 1 :]:
            right_container = containers[right.container_id]
            candidate_overlap = _intersection_area(left.output_bbox, right.output_bbox)
            source_overlap = _intersection_area(left_container.source_bbox, right_container.source_bbox)
            if {left_container.owner, right_container.owner} <= {"flow", "shared"}:
                if candidate_overlap > source_overlap + 0.75:
                    findings.append(
                        _finding(
                            "P18_FLOW_TEXT_COLLISION",
                            "HARD",
                            "composite_layout_planner",
                            left.container_id,
                            "Translated flow/shared placements newly collided.",
                            other_container_id=right.container_id,
                            candidate_overlap=round(candidate_overlap, 4),
                            source_overlap=round(source_overlap, 4),
                        )
                    )
                continue
            if left_container.owner == right_container.owner == "diagram":
                diagram_overlap = _intersection_area(
                    left.glyph_bbox or left.output_bbox,
                    right.glyph_bbox or right.output_bbox,
                )
                if diagram_overlap > source_overlap + 0.75:
                    findings.append(
                        _finding(
                            "P18_DIAGRAM_TEXT_COLLISION",
                            "HARD",
                            "composite_layout_planner",
                            left.container_id,
                            "Translated diagram text placements newly collided.",
                            other_container_id=right.container_id,
                            candidate_overlap=round(diagram_overlap, 4),
                            source_overlap=round(source_overlap, 4),
                        )
                    )
                continue
            if left_container.owner == right_container.owner:
                continue
            if candidate_overlap > source_overlap + 0.75:
                findings.append(
                    _finding(
                        "P18_CROSS_OWNER_TEXT_COLLISION",
                        "HARD",
                        "composite_layout_planner",
                        left.container_id,
                        "A translated placement newly collided with a different owner.",
                        other_container_id=right.container_id,
                        candidate_overlap=round(candidate_overlap, 4),
                        source_overlap=round(source_overlap, 4),
                    )
                )
    return tuple(findings)


def _flow_finding(item) -> CompositeFinding:
    return _finding(
        code=f"FLOW_{item.code}",
        severity=item.severity,
        owner="flow_layout_planner",
        container_id=item.container_id,
        message=item.message,
    )


def _diagram_finding(item, template: CompositePageTemplate) -> CompositeFinding:
    by_base = {
        container.base_container_id: container.composite_id
        for container in template.containers
        if container.owner == "diagram"
    }
    return _finding(
        code=item.code,
        severity=item.severity,
        owner=item.owner,
        container_id=by_base.get(item.container_id, item.container_id),
        message=item.message,
        **item.evidence,
    )


def _flow_alignment(horizontal_policy: str) -> str:
    policies = set(horizontal_policy.split("+"))
    if policies & {
        "safe_heading_left_whitespace_expand",
        "safe_margin_left_whitespace_expand",
        "locked_visual_overlay_safe_left_expand",
    }:
        return "RIGHT"
    return "LEFT"


def _finding(
    code: str,
    severity: str,
    owner: str,
    container_id: str | None,
    message: str,
    **evidence,
) -> CompositeFinding:
    return CompositeFinding(code, severity, owner, container_id, message, evidence)


def _contains(outer: Rect, inner: Rect, tolerance: float = 0.0) -> bool:
    return (
        inner[0] >= outer[0] - tolerance
        and inner[1] >= outer[1] - tolerance
        and inner[2] <= outer[2] + tolerance
        and inner[3] <= outer[3] + tolerance
    )


def _intersection_area(left: Rect, right: Rect) -> float:
    return max(0.0, min(left[2], right[2]) - max(left[0], right[0])) * max(
        0.0,
        min(left[3], right[3]) - max(left[1], right[1]),
    )


def _horizontal_overlap(left: Rect, right: Rect) -> float:
    return max(0.0, min(left[2], right[2]) - max(left[0], right[0]))


def _vertical_overlap(left: Rect, right: Rect) -> float:
    return max(0.0, min(left[3], right[3]) - max(left[1], right[1]))


def _deduplicate(findings: tuple[CompositeFinding, ...]) -> tuple[CompositeFinding, ...]:
    output = []
    seen = set()
    for finding in findings:
        key = (finding.code, finding.container_id, finding.message)
        if key in seen:
            continue
        seen.add(key)
        output.append(finding)
    return tuple(output)
