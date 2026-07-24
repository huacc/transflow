"""从不可变 Kernel 页面事实冻结独立 PageTextInventory。"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from statistics import median

from transflow.domain.text_inventory import (
    InventoryDisposition,
    PageTextInventory,
    PageTextInventoryItem,
)
from transflow.pdf_kernel.facts import ExtractedPageFacts, KernelTextFact

LOGGER = logging.getLogger("transflow.pdf_kernel.text_inventory")
KERNEL_ROOT = Path(__file__).resolve().parent.parent
URL_OR_EMAIL = re.compile(
    r"(?:https?://\S+|www\.\S+|\b[^\s@]+@[^\s@]+\.[^\s@]+)",
    re.I,
)
NUMERIC_LITERAL = re.compile(r"[\d\s.,%$€£¥()\-+/=:]+")
CURRENCY_SCALE_LITERAL = re.compile(
    r"(?:[A-Z]{1,3}\s*)?[$€£¥]\s*(?:mn|bn|k|m|b|t)?",
    re.I,
)
PAGE_NUMBER = re.compile(r"(?:page\s*)?(?:[ivxlcdm]+|\d+)(?:\s*/\s*\d+)?", re.I)
CODE_OR_ACRONYM = re.compile(r"(?=.*[A-Z0-9])[A-Z0-9][A-Z0-9._/\-]{1,31}")


@dataclass(frozen=True, slots=True)
class CanonicalTextRecord:
    """表示 Kernel 为当前页面机械选定的一个不重叠原生文字对象。"""

    object_id: str
    text: str
    bbox: tuple[float, float, float, float]


def _mechanical_keep_source_reason(
    text: str,
    *,
    natural_language_style: bool = False,
    target_language: str = "zh-CN",
) -> str | None:
    """只批准无需语义判断的稳定机械原文保留原因。"""

    stripped = text.strip()
    if URL_OR_EMAIL.fullmatch(stripped):
        return "URL_OR_EMAIL"
    if PAGE_NUMBER.fullmatch(stripped):
        return "PAGE_NUMBER"
    if CURRENCY_SCALE_LITERAL.fullmatch(stripped) or NUMERIC_LITERAL.fullmatch(stripped) or (
        stripped and not any(character.isalpha() for character in stripped)
    ):
        return "NUMERIC_OR_SYMBOLIC_LITERAL"
    notation = stripped.strip("()[]")
    if re.fullmatch(r"[A-Z][A-Z0-9+./'’$%-]{1,15}", notation) and (
        notation != stripped
        or bool(re.search(r"[0-9+./'’$%-]", notation))
    ):
        return "CODE_OR_ACRONYM"
    if CODE_OR_ACRONYM.fullmatch(stripped) and not (
        natural_language_style and stripped.isalpha()
    ):
        return "CODE_OR_ACRONYM"
    if _already_target_language(stripped, target_language):
        return "ALREADY_TARGET_LANGUAGE"
    return None


def _already_target_language(text: str, target_language: str) -> bool:
    language = target_language.partition("-")[0].casefold()
    has_cjk = bool(re.search(r"[\u3400-\u9fff]", text))
    has_latin_word = bool(re.search(r"[A-Za-z]{2,}", text))
    if language == "zh":
        return has_cjk and not bool(re.search(r"[A-Za-z]{3,}", text))
    if language == "en":
        return has_latin_word and not has_cjk
    return False


def canonical_text_records(
    facts: ExtractedPageFacts,
) -> tuple[CanonicalTextRecord, ...]:
    """冻结最细的不重叠 span 分母；无 span 时才回退到原生 text block。"""

    spans = tuple(item for item in facts.text_spans if item.text.strip())
    blocks = tuple(
        item
        for item in facts.objects
        if item.kind == "text" and not item.protected and item.text.strip()
    )
    if spans:
        return tuple(CanonicalTextRecord(item.object_id, item.text, item.bbox) for item in spans)
    return tuple(CanonicalTextRecord(item.object_id, item.text, item.bbox) for item in blocks)


def freeze_page_text_inventory(
    facts: ExtractedPageFacts,
    *,
    target_language: str = "zh-CN",
) -> PageTextInventory:
    """按几何顺序冻结一个不重叠文字层级，避免 block/span 同文重复计入分母。"""

    LOGGER.info(
        "调用文字清单冻结，意图=在 Toolbox/Provider 前建立独立分母 page_no=%s",
        facts.page.page_no,
    )
    records = canonical_text_records(facts)
    spans_by_id = {item.object_id: item for item in facts.text_spans}
    numeric_suffix_unit_ids = _numeric_suffix_unit_ids(facts)
    page_font_median = (
        median(item.font_size for item in facts.text_spans) if facts.text_spans else 0
    )
    ordered = sorted(
        records,
        key=lambda item: (round(item.bbox[1], 4), round(item.bbox[0], 4), item.object_id),
    )
    items: list[PageTextInventoryItem] = []
    for record in ordered:
        span = spans_by_id.get(record.object_id)
        natural_language_style = span is not None and (
            span.font_size >= page_font_median * 1.25
            or "bold" in span.font_name.casefold()
        )
        reason = (
            "NUMERIC_OR_SYMBOLIC_LITERAL"
            if record.object_id in numeric_suffix_unit_ids
            else _mechanical_keep_source_reason(
                record.text,
                natural_language_style=natural_language_style,
                target_language=target_language,
            )
        )
        items.append(
            PageTextInventoryItem(
                object_id=record.object_id,
                source_hash=hashlib.sha256(record.text.encode("utf-8")).hexdigest(),
                bbox=record.bbox,
                disposition=(
                    InventoryDisposition.KEEP_SOURCE
                    if reason is not None
                    else InventoryDisposition.TRANSLATE
                ),
                keep_source_reason=reason,
            )
        )
    return PageTextInventory(
        page_no=facts.page.page_no,
        page_identity=facts.page_identity,
        kernel_facts_hash=facts.kernel_facts_hash,
        items=tuple(items),
    )


def _numeric_suffix_unit_ids(facts: ExtractedPageFacts) -> set[str]:
    """Identify short units only when the native line is mechanically numeric."""

    lines: dict[tuple[int, int], list[KernelTextFact]] = {}
    for span in facts.text_spans:
        lines.setdefault((span.block_index, span.line_index), []).append(span)
    result: set[str] = set()
    for line in lines.values():
        ordered = sorted(line, key=lambda item: item.span_index)
        text = "".join(item.text for item in ordered).strip()
        if not re.fullmatch(
            r"[-+]?\d+(?:[.,]\d+)*\s*(?:[A-Za-z]{1,3}|%)",
            text,
        ):
            continue
        result.update(
            item.object_id
            for item in ordered
            if re.fullmatch(r"(?:[A-Za-z]{1,3}|%)", item.text.strip())
        )
    numeric_spans = tuple(
        item
        for item in facts.text_spans
        if re.fullmatch(r"[-+]?\d+(?:[.,]\d+)*", item.text.strip())
    )
    for unit in facts.text_spans:
        if not re.fullmatch(r"(?:[A-Za-z]{1,3}|%)", unit.text.strip()):
            continue
        unit_height = unit.bbox[3] - unit.bbox[1]
        for numeric in numeric_spans:
            numeric_height = numeric.bbox[3] - numeric.bbox[1]
            vertical_overlap = max(
                0.0,
                min(unit.bbox[3], numeric.bbox[3])
                - max(unit.bbox[1], numeric.bbox[1]),
            )
            gap = unit.bbox[0] - numeric.bbox[2]
            font_size = max(unit.font_size, numeric.font_size)
            if (
                vertical_overlap
                >= min(unit_height, numeric_height) * 0.50
                and -font_size * 0.30 <= gap <= font_size * 0.80
            ):
                result.add(unit.object_id)
                break
    return result


def main() -> int:
    """记录该入口需要真实 ExtractedPageFacts，避免直接读取宿主绝对路径。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    LOGGER.info("文字清单示例，意图=由调用方注入真实 Kernel 页面事实")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
