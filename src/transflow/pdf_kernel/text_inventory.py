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
from transflow.pdf_kernel.facts import ExtractedPageFacts

LOGGER = logging.getLogger("transflow.pdf_kernel.text_inventory")
KERNEL_ROOT = Path(__file__).resolve().parent.parent
URL_OR_EMAIL = re.compile(
    r"(?:https?://\S+|www\.\S+|\b[^\s@]+@[^\s@]+\.[^\s@]+)",
    re.I,
)
NUMERIC_LITERAL = re.compile(r"[\d\s.,%$€£¥()\-+/=:]+")
CURRENCY_SCALE_LITERAL = re.compile(r"[$€£¥]\s*(?:k|m|mn|b|bn|t)", re.I)
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
    if CODE_OR_ACRONYM.fullmatch(stripped) and not (
        natural_language_style and stripped.isalpha()
    ):
        return "CODE_OR_ACRONYM"
    if re.search(r"[\u4e00-\u9fff]", stripped) and not re.search(r"[A-Za-z]{3,}", stripped):
        return "ALREADY_TARGET_LANGUAGE"
    return None


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


def freeze_page_text_inventory(facts: ExtractedPageFacts) -> PageTextInventory:
    """按几何顺序冻结一个不重叠文字层级，避免 block/span 同文重复计入分母。"""

    LOGGER.info(
        "调用文字清单冻结，意图=在 Toolbox/Provider 前建立独立分母 page_no=%s",
        facts.page.page_no,
    )
    records = canonical_text_records(facts)
    spans_by_id = {item.object_id: item for item in facts.text_spans}
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
        reason = _mechanical_keep_source_reason(
            record.text,
            natural_language_style=natural_language_style,
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


def main() -> int:
    """记录该入口需要真实 ExtractedPageFacts，避免直接读取宿主绝对路径。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    LOGGER.info("文字清单示例，意图=由调用方注入真实 Kernel 页面事实")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
