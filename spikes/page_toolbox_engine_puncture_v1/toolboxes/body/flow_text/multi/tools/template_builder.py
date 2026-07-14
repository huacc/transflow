from __future__ import annotations

from collections import Counter
from dataclasses import replace
from statistics import median

from page_toolbox_puncture.contracts import PageFacts, TextObjectFact
from toolboxes.body.flow_text.single.tools.models import TextContainer
from toolboxes.body.flow_text.single.tools.template_builder import (
    _canonicalize_bullets,
    _detach_numbered_prefix,
    _is_vertical_decoration,
    _marker_assignments,
    build_page_template,
)

from . import TOOLBOX_KEY
from .models import ColumnAssignment, ColumnBand, MultiColumnTemplate
from .validators.margin_text_translation_rule import classify_margin_text_object


def _rectangle_union_area(
    rectangles: list[tuple[float, float, float, float]],
) -> float:
    if not rectangles:
        return 0.0
    x_edges = sorted({value for rectangle in rectangles for value in (rectangle[0], rectangle[2])})
    total = 0.0
    for left, right in zip(x_edges, x_edges[1:]):
        if right <= left:
            continue
        intervals = sorted(
            (y0, y1)
            for x0, y0, x1, y1 in rectangles
            if x0 < right and x1 > left and y1 > y0
        )
        covered_y = 0.0
        current_start: float | None = None
        current_end = 0.0
        for start, end in intervals:
            if current_start is None:
                current_start, current_end = start, end
            elif start <= current_end:
                current_end = max(current_end, end)
            else:
                covered_y += current_end - current_start
                current_start, current_end = start, end
        if current_start is not None:
            covered_y += current_end - current_start
        total += (right - left) * covered_y
    return total


def _page_background_image_ids(facts: PageFacts) -> frozenset[str]:
    """Identify one page-sized image or a non-overlapping raster tile cover."""

    page_area = max(facts.width * facts.height, 1.0)
    backgrounds = {
        item.object_id
        for item in facts.image_objects
        if ((item.bbox[2] - item.bbox[0]) * (item.bbox[3] - item.bbox[1])) / page_area >= 0.60
    }
    candidates: list[tuple[str, tuple[float, float, float, float], float]] = []
    for item in facts.image_objects:
        clipped = (
            max(0.0, item.bbox[0]),
            max(0.0, item.bbox[1]),
            min(facts.width, item.bbox[2]),
            min(facts.height, item.bbox[3]),
        )
        area = max(clipped[2] - clipped[0], 0.0) * max(clipped[3] - clipped[1], 0.0)
        if area > 0.0:
            candidates.append((item.object_id, clipped, area))
    selected: list[tuple[str, tuple[float, float, float, float]]] = []
    covered = 0.0
    for object_id, bbox, area in sorted(candidates, key=lambda item: item[2], reverse=True):
        next_rectangles = [item[1] for item in selected] + [bbox]
        next_covered = _rectangle_union_area(next_rectangles)
        if next_covered - covered >= area * 0.70:
            selected.append((object_id, bbox))
            covered = next_covered
    if len(selected) >= 2 and covered / page_area >= 0.80:
        envelope = (
            min(item[1][0] for item in selected),
            min(item[1][1] for item in selected),
            max(item[1][2] for item in selected),
            max(item[1][3] for item in selected),
        )
        if (
            envelope[0] <= facts.width * 0.03
            and envelope[1] <= facts.height * 0.03
            and envelope[2] >= facts.width * 0.97
            and envelope[3] >= facts.height * 0.97
        ):
            backgrounds.update(item[0] for item in selected)
    return frozenset(backgrounds)


def build_multi_column_template(facts: PageFacts) -> MultiColumnTemplate:
    """构建模板并执行已登记的确定性模板修复。"""

    template, _ = build_multi_column_template_with_repairs(facts)
    return template


def build_multi_column_template_with_repairs(
    facts: PageFacts,
) -> tuple[MultiColumnTemplate, tuple[dict[str, object], ...]]:
    """一次匹配一个模板病因，修复后立即用同一事实重新裁决。"""

    from .orchestrator.template_repair_loop import apply_deterministic_template_repairs

    initial = _build_initial_multi_column_template(facts)
    repaired, records = apply_deterministic_template_repairs(facts=facts, template=initial)
    return _reconcile_column_vertical_bounds(repaired, facts), records


def _build_initial_multi_column_template(facts: PageFacts) -> MultiColumnTemplate:
    """复用 P4 的语义分块，但重新建立多列归属和列优先阅读顺序。"""

    base = _build_multi_base_template(facts)
    candidates = [item for item in base.containers if item.role in {"body", "list"} and len(item.source_text) >= 20]
    narrow = [item for item in candidates if _width(item) <= facts.width * 0.52]
    evidence = narrow or candidates
    clusters = _cluster_column_starts(evidence, facts.width)
    if len(clusters) not in {2, 3}:
        # 局部多栏常用斜体条目或短标题，未必被标成 body/list；仅在正文证据失败时启用窄容器回退。
        fallback_evidence = [
            item
            for item in base.containers
            if item.role != "margin"
            and len(item.source_text) >= 3
            and _width(item) <= facts.width * 0.52
        ]
        clusters = _cluster_column_starts(fallback_evidence, facts.width)
    if len(clusters) not in {2, 3}:
        raise ValueError(f"p5_column_count_not_supported:{len(clusters)}")
    clusters = _active_column_groups(clusters)

    columns = _build_column_bands(clusters, facts)
    anchor_by_id = {column.column_id: _weighted_anchor(clusters[index]) for index, column in enumerate(columns)}
    assignments: list[ColumnAssignment] = []
    assigned: dict[str, list[TextContainer]] = {column.column_id: [] for column in columns}
    spans: list[TextContainer] = []
    fixed: list[TextContainer] = []
    margins: list[TextContainer] = []
    ambiguous: list[str] = []

    structure_top = min(column.content_top for column in columns)
    background_image_ids = _page_background_image_ids(facts)
    provisional = {
        container.container_id: (
            "margin"
            if container.role == "margin"
            else (
                "fixed"
                if _is_locked_visual_overlay(
                    container,
                    facts,
                    background_image_ids=background_image_ids,
                )
                else _assign_column(container, columns, anchor_by_id, facts.width)
            )
        )
        for container in base.containers
    }
    evidence_top_by_column = {
        column.column_id: column.content_top for column in columns
    }
    column_ids = set(evidence_top_by_column)
    for container in base.containers:
        owner = provisional[container.container_id]
        if owner not in column_ids:
            continue
        if any(
            provisional[peer.container_id] in column_ids - {owner}
            and abs(container.source_bbox[1] - peer.source_bbox[1])
            <= max(container.font_size, peer.font_size) * 0.45
            for peer in base.containers
        ):
            evidence_top_by_column[owner] = min(
                evidence_top_by_column[owner],
                container.source_bbox[1],
            )
    for container in base.containers:
        if provisional[container.container_id] == "margin":
            margins.append(container)
            continue
        if provisional[container.container_id] == "fixed":
            fixed.append(container)
            continue
        provisional_column = provisional[container.container_id]
        # 多栏活跃带之前、又与本栏首个证据距离明显较远的内容属于页级前奏。
        # 栏内标题允许比正文证据更早出现，因此采用更大的相对字号邻近范围。
        if provisional_column not in {"span", "margin"}:
            distance = evidence_top_by_column[provisional_column] - container.source_bbox[3]
            proximity_ratio = 2.5 if container.role == "heading" else 2.0
            is_page_prelude = distance > container.font_size * proximity_ratio
        else:
            is_page_prelude = False
        column_id = "span" if is_page_prelude else provisional_column
        if column_id == "span":
            spans.append(container)
            if (
                container.role not in {"heading"}
                and not _has_direct_spanning_geometry(container, columns, facts.width)
                and container.source_bbox[1] > min(column.content_top for column in columns) + facts.height * 0.08
            ):
                ambiguous.append(container.container_id)
            continue
        assigned[column_id].append(container)

    if any(not assigned[column.column_id] for column in columns):
        raise ValueError("p5_empty_detected_column")
    columns = _expand_columns_to_assigned_source(columns, assigned)

    first_column_top = structure_top
    top_spans = sorted((item for item in spans if item.source_bbox[1] <= first_column_top + facts.height * 0.04), key=_position)
    late_spans = sorted((item for item in spans if item not in top_spans), key=_position)
    if late_spans:
        # 页尾跨栏说明不能被上方各栏侵占；保护距离取当前页字号比例，不绑定具体坐标。
        late_guard = min(item.source_bbox[1] for item in late_spans)
        guard_scale = median(item.font_size for item in late_spans) * 0.20
        columns = tuple(
            replace(column, content_bottom=round(min(column.content_bottom, late_guard - guard_scale), 4))
            for column in columns
        )
    ordered: list[TextContainer] = list(top_spans)
    for column in columns:
        values = sorted(assigned[column.column_id], key=_position)
        for index, container in enumerate(values):
            assignments.append(ColumnAssignment(container.container_id, column.column_id, index))
        ordered.extend(values)
    for index, container in enumerate(top_spans + late_spans):
        assignments.append(ColumnAssignment(container.container_id, "span", index))
    ordered.extend(late_spans)
    for index, container in enumerate(sorted(fixed, key=_position)):
        assignments.append(ColumnAssignment(container.container_id, "fixed", index))
        ordered.append(container)
    for index, container in enumerate(sorted(margins, key=_position)):
        assignments.append(ColumnAssignment(container.container_id, "margin", index))
        ordered.append(container)

    ordered_containers = tuple(replace(container, reading_order=index) for index, container in enumerate(ordered))
    assignment_order = {item.container_id: item for item in assignments}
    ordered_assignments = tuple(assignment_order[item.container_id] for item in ordered_containers)
    return MultiColumnTemplate(
        facts.page_id,
        TOOLBOX_KEY,
        facts.width,
        facts.height,
        columns,
        ordered_containers,
        ordered_assignments,
        tuple(sorted(ambiguous)),
    )


def _expand_columns_to_assigned_source(
    columns: tuple[ColumnBand, ...],
    assigned: dict[str, list[TextContainer]],
) -> tuple[ColumnBand, ...]:
    """栏边界覆盖本栏全部源容器；只接受源页本身不互相侵入的栏。"""

    expanded: list[ColumnBand] = []
    for column in columns:
        values = assigned[column.column_id]
        content_top = min(item.source_bbox[1] for item in values)
        source_bottom = max(item.source_bbox[3] for item in values)
        # 初始模板只建立几何所有权；视觉留白保护在全部模板修复完成后统一裁决。
        elastic_bottom = column.content_bottom
        expanded.append(
            replace(
                column,
                left=round(min(column.left, *(item.source_bbox[0] for item in values)), 4),
                right=round(max(column.right, *(item.source_bbox[2] for item in values)), 4),
                content_top=round(content_top, 4),
                content_bottom=round(min(column.content_bottom, elastic_bottom), 4),
            )
        )
    for previous, current in zip(expanded, expanded[1:]):
        if previous.right >= current.left:
            raise ValueError(f"p5_source_column_gutter_not_clear:{previous.column_id}:{current.column_id}")
    return tuple(expanded)


def _reconcile_column_vertical_bounds(
    template: MultiColumnTemplate,
    facts: PageFacts,
) -> MultiColumnTemplate:
    """所有模板修复完成后，按最终栏归属重新计算上下界，防止旧的临时跨栏证据残留。"""

    assignment = {item.container_id: item.column_id for item in template.assignments}
    spans = [item for item in template.containers if assignment[item.container_id] == "span"]
    updated: list[ColumnBand] = []
    for column in template.columns:
        values = [item for item in template.containers if assignment[item.container_id] == column.column_id]
        if not values:
            raise ValueError(f"p5_empty_detected_column:{column.column_id}")
        content_top = min(item.source_bbox[1] for item in values)
        source_bottom = max(item.source_bbox[3] for item in values)
        external_bottom = _external_content_bottom(
            left=min(item.source_bbox[0] for item in values),
            right=max(item.source_bbox[2] for item in values),
            top=content_top,
            facts=facts,
        )
        content_bottom = _elastic_content_bottom(
            values,
            external_bottom,
            preserve_trailing_visual=_has_lower_composite_visual(facts),
        )
        late_spans = [
            item for item in spans
            if item.source_bbox[1] >= source_bottom - item.font_size * 2.0
        ]
        mid_spans = [
            item for item in spans
            if item.source_bbox[1] > content_top + item.font_size * 0.5
            and item.source_bbox[1] < source_bottom - item.font_size * 2.0
        ]
        if late_spans and not mid_spans:
            late_guard = min(item.source_bbox[1] for item in late_spans)
            guard_scale = median(item.font_size for item in late_spans) * 0.20
            content_bottom = min(content_bottom, late_guard - guard_scale)
        updated.append(
            replace(
                column,
                left=round(min(column.left, *(item.source_bbox[0] for item in values)), 4),
                right=round(max(column.right, *(item.source_bbox[2] for item in values)), 4),
                content_top=round(content_top, 4),
                content_bottom=round(max(source_bottom, content_bottom), 4),
            )
        )
    for previous, current in zip(updated, updated[1:]):
        if previous.right >= current.left:
            raise ValueError(f"p5_source_column_gutter_not_clear:{previous.column_id}:{current.column_id}")
    return replace(template, columns=tuple(updated))


def _elastic_content_bottom(
    values: list[TextContainer],
    external_bottom: float,
    *,
    preserve_trailing_visual: bool,
) -> float:
    content_top = min(item.source_bbox[1] for item in values)
    source_bottom = max(item.source_bbox[3] for item in values)
    source_flow_height = max(source_bottom - content_top, median(item.font_size for item in values))
    trailing_room = max(0.0, external_bottom - source_bottom)
    body_scale = median(item.font_size for item in values)
    # 只有“整页背景 + 正文后方局部锁定视觉对象”同时成立，才把大块下部留白视为设计区。
    if preserve_trailing_visual and trailing_room > max(source_flow_height * 0.50, body_scale * 8.0):
        return source_bottom + max(source_flow_height * 0.20, body_scale * 4.0)
    return external_bottom


def _has_lower_composite_visual(facts: PageFacts) -> bool:
    background_image_ids = _page_background_image_ids(facts)
    full_page_background = bool(background_image_ids)
    local_lower_visual = any(
        item.object_id not in background_image_ids
        and
        item.bbox[1] > facts.height * 0.12
        and 0.005
        <= ((item.bbox[2] - item.bbox[0]) * (item.bbox[3] - item.bbox[1]))
        / max(facts.width * facts.height, 1.0)
        < 0.45
        for item in (*facts.image_objects, *facts.drawing_objects)
    )
    return full_page_background and local_lower_visual


def _build_multi_base_template(facts: PageFacts):
    """沿用 single 的原生分块和列表保护，但不执行单列相邻块合并。"""

    base = build_page_template(facts)
    marker_assignments = _marker_assignments(base, facts)
    body_font_evidence = [round(item.font_size, 1) for item in base.containers if item.role != "margin" and len(item.source_text) > 24]
    body_font_baseline = Counter(body_font_evidence).most_common(1)[0][0] if body_font_evidence else median(item.font_size for item in base.containers)
    normalized: list[TextContainer] = []
    for container in base.containers:
        if _is_vertical_decoration(container, facts.height):
            continue
        # 仅小字号运行页眉和页脚固定回填；靠近页顶的大字号标题仍按页级标题处理。
        fixed_margin = container.role == "margin" and (
            container.source_bbox[1] >= facts.height * 0.90
            or (
                container.source_bbox[1] <= facts.height * 0.08
                and container.font_size <= body_font_baseline * 1.15
            )
        )
        if fixed_margin:
            normalized.extend(_translatable_margin_containers(container, facts))
            continue
        role = "heading" if container.role == "margin" else container.role
        height = container.source_bbox[3] - container.source_bbox[1]
        false_body_heading = len(container.source_text) > 24 and container.font_size <= body_font_baseline * 1.08
        if role == "heading" and (false_body_heading or len(container.source_text) > 180 or height > container.font_size * 2.5):
            role = "body"
        marker = marker_assignments.get(container.container_id)
        source_bbox = container.source_bbox
        source_object_ids = container.source_object_ids
        source_text = container.source_text
        preserved_prefix = container.preserved_prefix
        if marker is not None:
            source_object_ids = (marker.object_id,) + source_object_ids
            preserved_prefix = "•"
        else:
            detached = _detach_numbered_prefix(container, facts)
            if detached is not None:
                preserved_prefix, source_text, source_object_ids, source_bbox = detached
        normalized.append(
            replace(
                container,
                source_object_ids=source_object_ids,
                source_text=_canonicalize_bullets(source_text),
                role=role,
                source_bbox=source_bbox,
                anchor=(source_bbox[0], source_bbox[1]),
                preserved_prefix=preserved_prefix,
            )
        )
    return replace(base, containers=tuple(normalized))


def _translatable_margin_containers(
    container: TextContainer,
    facts: PageFacts,
) -> tuple[TextContainer, ...]:
    """页码等运行标记留在源页；含自然语言的边缘片段进入翻译模板。"""

    source_by_id = {item.object_id: item for item in facts.text_objects}
    objects = [source_by_id[object_id] for object_id in container.source_object_ids if object_id in source_by_id]
    translatable = [
        item
        for item in objects
        if classify_margin_text_object(item)["rule_verdict"] == "TRANSLATE"
    ]
    groups: list[list[TextObjectFact]] = []
    for item in sorted(translatable, key=lambda value: (value.line_index, value.span_index, value.bbox[0])):
        if not groups or not _same_margin_fragment(groups[-1][-1], item):
            groups.append([item])
        else:
            groups[-1].append(item)

    output: list[TextContainer] = []
    for index, group in enumerate(groups):
        bbox = (
            min(item.bbox[0] for item in group),
            min(item.bbox[1] for item in group),
            max(item.bbox[2] for item in group),
            max(item.bbox[3] for item in group),
        )
        output.append(
            TextContainer(
                container_id=f"{container.container_id}--margin-{index:03d}",
                source_object_ids=tuple(item.object_id for item in group),
                source_text="".join(item.text for item in group).strip(),
                reading_order=container.reading_order,
                role="margin",
                source_bbox=tuple(round(value, 4) for value in bbox),
                anchor=(round(bbox[0], 4), round(bbox[1], 4)),
                font_size=round(max(item.font_size for item in group), 4),
                color_srgb=max(group, key=lambda item: (item.font_size, len(item.text))).color_srgb,
                font_weight=_margin_font_weight(group),
                preserved_prefix=None,
            )
        )
    return tuple(output)


def _same_margin_fragment(previous: TextObjectFact, current: TextObjectFact) -> bool:
    if previous.line_index != current.line_index:
        return False
    same_style = (
        previous.font_name == current.font_name
        and abs(previous.font_size - current.font_size) <= max(previous.font_size, current.font_size) * 0.05
        and previous.color_srgb == current.color_srgb
    )
    horizontal_gap = current.bbox[0] - previous.bbox[2]
    return same_style and horizontal_gap <= max(previous.font_size, current.font_size) * 2.0


def _margin_font_weight(objects: list[TextObjectFact]) -> str:
    strong_names = ("bold", "semibold", "demi", "medium")
    total = sum(max(1, len(item.text.strip())) for item in objects)
    strong = sum(
        max(1, len(item.text.strip()))
        for item in objects
        if any(token in item.font_name.casefold() for token in strong_names)
    )
    return "bold" if strong * 2 >= total else "regular"


def _cluster_column_starts(containers: list[TextContainer], page_width: float) -> list[list[TextContainer]]:
    threshold = max(18.0, page_width * 0.075)
    clusters: list[list[TextContainer]] = []
    for container in sorted(containers, key=lambda item: item.source_bbox[0]):
        x0 = container.source_bbox[0]
        target = min(clusters, key=lambda group: abs(x0 - _weighted_anchor(group)), default=None)
        if target is None or abs(x0 - _weighted_anchor(target)) > threshold:
            clusters.append([container])
        else:
            target.append(container)

    total_weight = sum(_cluster_weight(group) for group in clusters)
    # 标签栏的文字量天然远小于右侧内容栏；较低权重门槛避免把真实标签栏当噪声丢弃。
    material = [group for group in clusters if _cluster_weight(group) >= total_weight * 0.03]
    if len(material) > 3:
        material = sorted(material, key=_cluster_weight, reverse=True)[:3]
    return sorted(material, key=_weighted_anchor)


def _active_column_groups(
    clusters: list[list[TextContainer]],
) -> list[list[TextContainer]]:
    """只用至少两栏在同一纵向阶段共同出现后的对象确定局部多栏几何。"""

    aligned_tops: list[float] = []
    for left_index, left_group in enumerate(clusters):
        for right_group in clusters[left_index + 1 :]:
            for left in left_group:
                for right in right_group:
                    vertical_gap = max(
                        0.0,
                        max(left.source_bbox[1], right.source_bbox[1])
                        - min(left.source_bbox[3], right.source_bbox[3]),
                    )
                    if vertical_gap <= max(left.font_size, right.font_size) * 2.0:
                        aligned_tops.append(min(left.source_bbox[1], right.source_bbox[1]))
    if not aligned_tops:
        return clusters
    active_top = min(aligned_tops)
    active = [
        [
            item
            for item in group
            if item.source_bbox[3] >= active_top - item.font_size * 2.0
        ]
        for group in clusters
    ]
    active = [group for group in active if group]
    return active if len(active) in {2, 3} else clusters


def _build_column_bands(clusters: list[list[TextContainer]], facts: PageFacts) -> tuple[ColumnBand, ...]:
    anchors = [_weighted_anchor(group) for group in clusters]
    output: list[ColumnBand] = []
    for index, group in enumerate(clusters):
        left = min(item.source_bbox[0] for item in group)
        right_values = sorted(item.source_bbox[2] for item in group)
        if index + 1 < len(clusters):
            # 伸到下一栏锚点的早期标题是跨栏候选，不能拿来撑大当前栏宽。
            bounded = [value for value in right_values if value < anchors[index + 1]]
            usable = bounded or [(anchors[index] + anchors[index + 1]) / 2.0]
        else:
            usable = right_values
        right = usable[min(len(usable) - 1, round((len(usable) - 1) * 0.90))]
        top = min(item.source_bbox[1] for item in group)
        bottom = _external_content_bottom(left=left, right=right, top=top, facts=facts)
        output.append(
            ColumnBand(
                f"column-{index + 1}",
                index,
                round(max(4.0, left), 4),
                round(min(facts.width - 4.0, right), 4),
                round(top, 4),
                round(bottom, 4),
            )
        )
    return tuple(output)


def _external_content_bottom(
    *,
    left: float,
    right: float,
    top: float,
    facts: PageFacts,
) -> float:
    """根据页脚和局部锁定视觉对象给出栏流的外部底线。"""

    guards = [item.bbox[1] for item in facts.text_objects if item.bbox[1] >= facts.height * 0.90]
    background_image_ids = _page_background_image_ids(facts)
    for locked in (*facts.image_objects, *facts.drawing_objects):
        if getattr(locked, "object_id", None) in background_image_ids:
            continue
        x0, y0, x1, y1 = locked.bbox
        area_ratio = ((x1 - x0) * (y1 - y0)) / max(facts.width * facts.height, 1.0)
        horizontal_overlap = max(0.0, min(right, x1) - max(left, x0))
        if (
            area_ratio < 0.45
            and y0 > top
            and horizontal_overlap >= (right - left) * 0.30
            and not _has_aligned_column_flow_below(
                locked_bbox=locked.bbox,
                left=left,
                right=right,
                facts=facts,
            )
        ):
            guards.append(y0)
    return min(guards) - 4.0 if guards else facts.height - 20.0


def _has_aligned_column_flow_below(
    *,
    locked_bbox: tuple[float, float, float, float],
    left: float,
    right: float,
    facts: PageFacts,
) -> bool:
    column_width = max(right - left, 1.0)
    locked_bottom = locked_bbox[3]
    for item in facts.text_objects:
        x0, y0, x1, _ = item.bbox
        if y0 < locked_bottom - 0.01 or y0 >= facts.height * 0.90:
            continue
        text_width = max(x1 - x0, 1.0)
        horizontal_overlap = max(0.0, min(right, x1) - max(left, x0))
        aligned_to_column = abs(x0 - left) <= max(item.font_size * 1.5, column_width * 0.05)
        occupies_column = text_width >= column_width * 0.60
        if horizontal_overlap >= min(text_width, column_width) * 0.60 and (aligned_to_column or occupies_column):
            return True
    return False


def _assign_column(
    container: TextContainer,
    columns: tuple[ColumnBand, ...],
    anchor_by_id: dict[str, float],
    page_width: float,
) -> str:
    x0, _, x1, _ = container.source_bbox
    width = x1 - x0
    if any(
        x0 < previous.right and x1 > current.left
        for previous, current in zip(columns, columns[1:])
    ):
        return "span"
    overlaps = [
        (column.column_id, max(0.0, min(x1, column.right) - max(x0, column.left)))
        for column in columns
    ]
    material = [item for item in overlaps if item[1] >= max(4.0, width * 0.22)]
    if width >= page_width * 0.60 or len(material) >= 2:
        return "span"
    return min(columns, key=lambda column: abs(x0 - anchor_by_id[column.column_id])).column_id


def _has_direct_spanning_geometry(
    container: TextContainer,
    columns: tuple[ColumnBand, ...],
    page_width: float,
) -> bool:
    x0, _, x1, _ = container.source_bbox
    return (x1 - x0) >= page_width * 0.60 or any(
        x0 < previous.right and x1 > current.left
        for previous, current in zip(columns, columns[1:])
    )


def _is_locked_visual_overlay(
    container: TextContainer,
    facts: PageFacts,
    *,
    background_image_ids: frozenset[str] | None = None,
) -> bool:
    """识别签名、徽标等局部锁定图片附近的说明文字，避免把它串入正文栏流。"""

    x0, y0, x1, _ = container.source_bbox
    width = max(x1 - x0, 1.0)
    background_image_ids = background_image_ids or _page_background_image_ids(facts)
    for locked in (*facts.image_objects, *facts.drawing_objects):
        if getattr(locked, "object_id", None) in background_image_ids:
            continue
        lx0, ly0, lx1, ly1 = locked.bbox
        area_ratio = ((lx1 - lx0) * (ly1 - ly0)) / max(facts.width * facts.height, 1.0)
        # 页眉装饰图与整页背景不属于“正文流之后的局部锁定视觉对象”。
        if area_ratio >= 0.60 or ly0 <= facts.height * 0.12:
            continue
        horizontal_overlap = max(0.0, min(x1, lx1) - max(x0, lx0))
        horizontally_related = (
            horizontal_overlap >= width * 0.25
            or lx0 - container.font_size <= (x0 + x1) / 2.0 <= lx1 + container.font_size
        )
        visual_width = max(lx1 - lx0, 1.0)
        near_visual_bottom = ly1 + max(container.font_size * 1.25, (ly1 - ly0) * 0.08)
        vertical_limit = ly1 + max(container.font_size * 5.0, (ly1 - ly0) * 0.50)
        inset_visual_anchor = x0 >= lx0 + max(container.font_size, visual_width * 0.08)
        if (
            horizontally_related
            and ly0 - container.font_size <= y0
            and (y0 <= near_visual_bottom or (inset_visual_anchor and y0 <= vertical_limit))
        ):
            return True
    return False


def _cluster_weight(group: list[TextContainer]) -> int:
    return sum(max(1, len(item.source_text)) for item in group)


def _weighted_anchor(group: list[TextContainer]) -> float:
    weight = _cluster_weight(group)
    return sum(item.source_bbox[0] * max(1, len(item.source_text)) for item in group) / max(weight, 1)


def _width(container: TextContainer) -> float:
    return container.source_bbox[2] - container.source_bbox[0]


def _position(container: TextContainer) -> tuple[float, float]:
    return container.source_bbox[1], container.source_bbox[0]
