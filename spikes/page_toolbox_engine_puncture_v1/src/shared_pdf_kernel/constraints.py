from __future__ import annotations

from pathlib import Path

from page_toolbox_puncture.contracts import ContainerWrite, PageFacts, PagePatch, TextObjectFact

from .fonts import probe_font
from .models import ConstraintFinding


def preflight_patch(facts: PageFacts, patch: PagePatch) -> tuple[ConstraintFinding, ...]:
    findings: list[ConstraintFinding] = []
    if patch.source_pdf_sha256 != facts.source_pdf_sha256:
        findings.append(_finding("SOURCE_HASH_MISMATCH", "补丁源哈希与页面事实不一致"))
    if patch.page_index != facts.page_index or patch.page_id != facts.page_id:
        findings.append(_finding("PAGE_IDENTITY_MISMATCH", "补丁页面标识与页面事实不一致"))
    by_id = {item.object_id: item for item in facts.text_objects}
    targets = {item.container_id for item in patch.writes}
    page_rect = (0.0, 0.0, facts.width, facts.height)

    for write in patch.writes:
        source = by_id.get(write.container_id)
        if source is None:
            findings.append(_finding("UNKNOWN_CONTAINER", "补丁引用了不存在的文字对象", container_id=write.container_id))
            continue
        if not _same_origin(source.bbox, write.output_bbox):
            findings.append(_finding("FIXED_ORIGIN_MOVED", "输出 bbox 左上角发生移动", container_id=write.container_id, source_bbox=source.bbox, output_bbox=write.output_bbox))
        if not _contains(write.allowed_bbox, write.output_bbox):
            findings.append(_finding("RESIZE_BOUNDS_EXCEEDED", "输出 bbox 超出声明的安全范围", container_id=write.container_id))
        if not _contains(page_rect, write.output_bbox):
            findings.append(_finding("PAGE_BOUNDS_EXCEEDED", "输出 bbox 超出页面范围", container_id=write.container_id))
        font = probe_font(Path(write.font_file), write.translated_text)
        if not font.covers_text:
            findings.append(_finding("FONT_GLYPH_MISSING", "字体缺失或不覆盖全部译文字形", container_id=write.container_id, missing_codepoints=font.missing_codepoints))
        _append_new_collision_findings(findings, facts, source, write, targets)

    for index, left in enumerate(patch.writes):
        for right in patch.writes[index + 1:]:
            if _intersection_area(left.output_bbox, right.output_bbox) > 0.01:
                findings.append(_finding("WRITE_OVERLAP", "两个目标写入区域发生重叠", left=left.container_id, right=right.container_id))
    return tuple(findings)


def invariant_findings(source: PageFacts, candidate: PageFacts) -> tuple[ConstraintFinding, ...]:
    findings: list[ConstraintFinding] = []
    if source.geometry_sha256 != candidate.geometry_sha256:
        findings.append(_finding("PAGE_GEOMETRY_CHANGED", "页面尺寸、页框或旋转发生变化"))
    if source.locked_objects_sha256 != candidate.locked_objects_sha256:
        findings.append(
            _finding(
                "LOCKED_OBJECTS_CHANGED",
                "图片、矢量图形或页面几何的锁定对象哈希发生变化",
                source=source.locked_objects_sha256,
                candidate=candidate.locked_objects_sha256,
            )
        )
    return tuple(findings)


def _append_new_collision_findings(findings: list[ConstraintFinding], facts: PageFacts, source: TextObjectFact, write: ContainerWrite, target_ids: set[str]) -> None:
    for other in facts.text_objects:
        if other.object_id == source.object_id or other.object_id in target_ids:
            continue
        if _new_overlap(source.bbox, write.output_bbox, other.bbox):
            findings.append(_finding("NON_TARGET_TEXT_OVERLAP", "写入区域新增了与非目标文字的重叠", container_id=write.container_id, obstacle_id=other.object_id))
    for image in facts.image_objects:
        if _new_overlap(source.bbox, write.output_bbox, image.bbox):
            findings.append(_finding("IMAGE_OVERLAP", "写入区域新增了与图片的重叠", container_id=write.container_id, obstacle_id=image.object_id))


def _new_overlap(source: tuple[float, float, float, float], output: tuple[float, float, float, float], obstacle: tuple[float, float, float, float]) -> bool:
    baseline = _intersection_area(source, obstacle) / max(0.001, _area(source))
    proposed = _intersection_area(output, obstacle) / max(0.001, _area(output))
    return proposed > baseline + 0.01


def _contains(outer: tuple[float, float, float, float], inner: tuple[float, float, float, float], tolerance: float = 0.05) -> bool:
    return inner[0] >= outer[0] - tolerance and inner[1] >= outer[1] - tolerance and inner[2] <= outer[2] + tolerance and inner[3] <= outer[3] + tolerance


def _same_origin(left: tuple[float, float, float, float], right: tuple[float, float, float, float], tolerance: float = 0.05) -> bool:
    return abs(left[0] - right[0]) <= tolerance and abs(left[1] - right[1]) <= tolerance


def _area(rect: tuple[float, float, float, float]) -> float:
    return max(0.0, rect[2] - rect[0]) * max(0.0, rect[3] - rect[1])


def _intersection_area(left: tuple[float, float, float, float], right: tuple[float, float, float, float]) -> float:
    return max(0.0, min(left[2], right[2]) - max(left[0], right[0])) * max(0.0, min(left[3], right[3]) - max(left[1], right[1]))


def _finding(code: str, message: str, **evidence: object) -> ConstraintFinding:
    return ConstraintFinding(code=code, severity="HARD", message=message, evidence=dict(evidence))

