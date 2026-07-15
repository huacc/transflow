from __future__ import annotations

import re
import statistics
from dataclasses import dataclass

from page_toolbox_puncture.contracts import PageFacts, TextObjectFact
from shared_pdf_kernel.facts import canonical_sha256

from . import TOOLBOX_KEY
from .models import EndTemplate, EndTextRegion, Rect


class EndCapabilityError(RuntimeError):
    pass


_URL_OR_EMAIL = re.compile(
    r"^(?:(?:https?|ftp)://\S+|www\.\S+|[^\s@]+@[^\s@]+\.[^\s@]+)$",
    re.IGNORECASE,
)
_LITERAL = re.compile(
    r"(?:https?://\S+|www\.\S+|[^\s@]+@[^\s@]+\.[^\s@]+|"
    r"(?<![A-Za-z0-9])[A-Z]{2,}(?![A-Za-z0-9])|"
    r"(?<!\d)\d+(?:[.,:/-]\d+)*(?!\d)|[®™©])"
)
_CONTACT = re.compile(
    r"\b(?:tel(?:ephone)?|fax|hotline|address|customer\s+service|phone)\b|"
    r"電話|电话|傳真|传真|客服|投訴|投诉|地址|大街|廣場|广场|道路|路\b|號|号",
    re.IGNORECASE,
)
_DISCLAIMER = re.compile(
    r"certif(?:ied|ication)|controlled\s+sources|well-managed|"
    r"incorporated|registered|註冊成立|注册成立|免責|免责|disclaimer",
    re.IGNORECASE,
)
_COMPANY = re.compile(
    r"\b(?:limited|ltd\.?|holdings?|corporation|company|group|corp\.?)\b|"
    r"股份有限公司|有限公司|集團|集团|銀行|银行|控股",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class _Line:
    object_ids: tuple[str, ...]
    text: str
    bbox: Rect
    font_name: str
    font_size: float
    color_srgb: int
    block_index: int
    line_index: int

    @property
    def cy(self) -> float:
        return (self.bbox[1] + self.bbox[3]) / 2.0

    @property
    def height(self) -> float:
        return self.bbox[3] - self.bbox[1]


@dataclass(frozen=True)
class _ClassifiedLine:
    line: _Line
    role: str
    disposition: str
    protection_reason: str | None


def build_end_template(
    facts: PageFacts,
    source_language: str,
    target_language: str,
) -> EndTemplate:
    lines = _logical_lines(facts.text_objects)
    classified = tuple(_classify_line(line, source_language, target_language) for line in lines)
    translatable_groups = _translation_groups(tuple(item for item in classified if item.disposition == "translate"))
    protected = tuple(item for item in classified if item.disposition == "protect")

    drafts: list[tuple[tuple[_ClassifiedLine, ...], str, str, str | None]] = []
    drafts.extend((group, _group_role(group), "translate", None) for group in translatable_groups)
    drafts.extend(((item,), item.role, "protect", item.protection_reason) for item in protected)
    drafts.sort(key=lambda row: (_union(tuple(item.line.bbox for item in row[0]))[1], _union(tuple(item.line.bbox for item in row[0]))[0]))

    regions: list[EndTextRegion] = []
    for reading_order, (group, role, disposition, protection_reason) in enumerate(drafts):
        group_lines = tuple(item.line for item in group)
        source_bbox = _round_rect(_union(tuple(line.bbox for line in group_lines)))
        font_size = round(statistics.median(line.font_size for line in group_lines), 4)
        style = max(group_lines, key=lambda line: (line.font_size, -line.bbox[1]))
        alignment = _alignment(source_bbox, facts.width)
        allowed_bbox = (
            _allowed_bbox(source_bbox, group_lines, lines, facts.width, facts.height, alignment, font_size)
            if disposition == "translate"
            else source_bbox
        )
        source_text = "\n".join(line.text for line in group_lines)
        regions.append(
            EndTextRegion(
                region_id=f"end-region-{reading_order:03d}",
                source_object_ids=tuple(object_id for line in group_lines for object_id in line.object_ids),
                source_text=source_text,
                source_bbox=source_bbox,
                allowed_bbox=allowed_bbox,
                reading_order=reading_order,
                role=role,
                disposition=disposition,
                protection_reason=protection_reason,
                required_literals=_required_literals(source_text) if disposition == "translate" else (),
                font_name=style.font_name,
                font_size=font_size,
                color_srgb=style.color_srgb,
                alignment=alignment,
            )
        )

    protected_ids = tuple(
        object_id
        for region in regions
        if region.disposition == "protect"
        for object_id in region.source_object_ids
    )
    structure_sha256 = canonical_sha256(
        {
            "toolbox_key": TOOLBOX_KEY,
            "source_language": source_language,
            "target_language": target_language,
            "regions": regions,
            "protected_object_ids": protected_ids,
            "locked_objects_sha256": facts.locked_objects_sha256,
        }
    )
    return EndTemplate(
        page_id=facts.page_id,
        toolbox_key=TOOLBOX_KEY,
        width=facts.width,
        height=facts.height,
        source_language=source_language,
        target_language=target_language,
        regions=tuple(regions),
        protected_object_ids=protected_ids,
        structure_sha256=structure_sha256,
    )


def _classify_line(line: _Line, source_language: str, target_language: str) -> _ClassifiedLine:
    text = line.text.strip()
    if not any(character.isalpha() or _is_han(character) for character in text):
        return _ClassifiedLine(line, "identifier", "protect", "nonsemantic_literal")
    if _URL_OR_EMAIL.fullmatch(text):
        return _ClassifiedLine(line, "contact_link", "protect", "link_or_email")

    role = _semantic_role(text)
    if role == "company_name":
        return _ClassifiedLine(line, role, "protect", "brand_or_legal_identifier")
    if _already_target_language(text, source_language, target_language):
        return _ClassifiedLine(line, role, "protect", "already_target_language")
    if role == "contact" and _has_han(text) and _has_latin(text):
        return _ClassifiedLine(line, role, "protect", "bilingual_contact_identifier")
    return _ClassifiedLine(line, role, "translate", None)


def _semantic_role(text: str) -> str:
    if _DISCLAIMER.search(text) or (len(text) >= 70 and not _CONTACT.search(text)):
        return "disclaimer"
    if _CONTACT.search(text) or _looks_like_address(text):
        return "contact"
    if _COMPANY.search(text) and not re.search(r"[()（）]", text):
        return "company_name"
    return "sparse_text"


def _already_target_language(text: str, source_language: str, target_language: str) -> bool:
    if source_language.casefold().startswith("zh") and target_language.casefold().startswith("en"):
        return _has_latin(text) and not _has_han(text)
    if source_language.casefold().startswith("en") and target_language.casefold().startswith("zh"):
        return _has_han(text) and not _has_latin(text)
    return False


def _looks_like_address(text: str) -> bool:
    digit_count = sum(character.isdigit() for character in text)
    word_count = len(re.findall(r"[A-Za-z]+", text))
    return digit_count > 0 and (word_count >= 5 or any(token in text for token in ("香港", "北京", "中環", "中环")))


def _translation_groups(items: tuple[_ClassifiedLine, ...]) -> tuple[tuple[_ClassifiedLine, ...], ...]:
    groups: list[list[_ClassifiedLine]] = []
    for item in sorted(items, key=lambda value: (value.line.bbox[1], value.line.bbox[0])):
        if groups and _can_group(groups[-1][-1], item):
            groups[-1].append(item)
        else:
            groups.append([item])
    return tuple(tuple(group) for group in groups)


def _can_group(previous: _ClassifiedLine, current: _ClassifiedLine) -> bool:
    vertical_gap = current.line.bbox[1] - previous.line.bbox[3]
    same_row = abs(current.line.cy - previous.line.cy) <= max(previous.line.height, current.line.height) * 0.55
    if same_row:
        return False
    limit = max(8.0, previous.line.font_size * 1.35, current.line.font_size * 1.35)
    if vertical_gap < -1.0 or vertical_gap > limit:
        return False
    compatible = {previous.role, current.role} <= {"contact", "disclaimer"}
    same_block = previous.line.block_index == current.line.block_index
    return compatible or same_block


def _group_role(group: tuple[_ClassifiedLine, ...]) -> str:
    roles = {item.role for item in group}
    if len(group) > 1 and roles <= {"contact", "disclaimer"}:
        return "contact_block"
    return group[0].role


def _allowed_bbox(
    source: Rect,
    group_lines: tuple[_Line, ...],
    all_lines: tuple[_Line, ...],
    page_width: float,
    page_height: float,
    alignment: str,
    font_size: float,
) -> Rect:
    margin = max(6.0, page_width * 0.02)
    source_width = source[2] - source[0]
    desired_width = min(page_width - margin * 2.0, max(source_width * 1.8, page_width * 0.55))
    if alignment == "left":
        x0 = source[0]
        x1 = min(page_width - margin, max(source[2], x0 + desired_width))
    elif alignment == "right":
        x1 = source[2]
        x0 = max(margin, min(source[0], x1 - desired_width))
    else:
        center = (source[0] + source[2]) / 2.0
        x0 = max(margin, center - desired_width / 2.0)
        x1 = min(page_width - margin, center + desired_width / 2.0)
        if x1 - x0 < desired_width:
            if x0 <= margin + 0.01:
                x1 = min(page_width - margin, x0 + desired_width)
            else:
                x0 = max(margin, x1 - desired_width)

    group_ids = {object_id for line in group_lines for object_id in line.object_ids}
    lower_obstacles = [
        line.bbox[1]
        for line in all_lines
        if not group_ids.intersection(line.object_ids)
        and line.bbox[1] >= source[3] - 0.5
        and _horizontal_overlap((x0, source[1], x1, source[3]), line.bbox) > 0.0
    ]
    obstacle_bottom = min(lower_obstacles) - 1.5 if lower_obstacles else page_height - max(6.0, page_height * 0.02)
    source_height = source[3] - source[1]
    adaptive_bottom = source[1] + max(32.0, source_height * 2.5, font_size * 4.0)
    y1 = min(obstacle_bottom, adaptive_bottom)
    y1 = max(y1, source[3] + max(1.0, font_size * 0.18))
    y1 = min(y1, page_height - margin)
    if y1 <= source[1] + 0.5:
        raise EndCapabilityError("END_SAFE_TEXT_BAND_NOT_FOUND")
    return _round_rect((x0, source[1], x1, y1))


def _alignment(bbox: Rect, page_width: float) -> str:
    center = (bbox[0] + bbox[2]) / 2.0
    if center <= page_width * 0.40:
        return "left"
    if center >= page_width * 0.60:
        return "right"
    return "center"


def _required_literals(text: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(match.group(0).rstrip(".,;") for match in _LITERAL.finditer(text)))


def _logical_lines(objects: tuple[TextObjectFact, ...]) -> tuple[_Line, ...]:
    grouped: dict[tuple[int, int], list[TextObjectFact]] = {}
    for item in objects:
        grouped.setdefault((item.block_index, item.line_index), []).append(item)
    lines: list[_Line] = []
    for (block_index, line_index), items in grouped.items():
        items.sort(key=lambda item: (item.bbox[0], item.span_index))
        pieces: list[str] = []
        previous: TextObjectFact | None = None
        for item in items:
            if previous is not None and item.bbox[0] - previous.bbox[2] > max(previous.font_size, item.font_size) * 0.45:
                pieces.append(" ")
            pieces.append(item.text)
            previous = item
        text = "".join(pieces).replace("\ufeff", "").strip()
        if not text:
            continue
        style = max(items, key=lambda item: (item.font_size, item.span_index))
        lines.append(
            _Line(
                object_ids=tuple(item.object_id for item in items),
                text=text,
                bbox=_round_rect(_union(tuple(item.bbox for item in items))),
                font_name=style.font_name,
                font_size=max(item.font_size for item in items),
                color_srgb=style.color_srgb,
                block_index=block_index,
                line_index=line_index,
            )
        )
    return tuple(sorted(lines, key=lambda line: (line.bbox[1], line.bbox[0], line.block_index, line.line_index)))


def _union(rects: tuple[Rect, ...]) -> Rect:
    return (
        min(rect[0] for rect in rects),
        min(rect[1] for rect in rects),
        max(rect[2] for rect in rects),
        max(rect[3] for rect in rects),
    )


def _horizontal_overlap(left: Rect, right: Rect) -> float:
    return max(0.0, min(left[2], right[2]) - max(left[0], right[0]))


def _round_rect(rect: Rect) -> Rect:
    return tuple(round(float(value), 4) for value in rect)  # type: ignore[return-value]


def _has_han(text: str) -> bool:
    return any(_is_han(character) for character in text)


def _has_latin(text: str) -> bool:
    return any("A" <= character.upper() <= "Z" for character in text)


def _is_han(character: str) -> bool:
    return "\u3400" <= character <= "\u9fff"
