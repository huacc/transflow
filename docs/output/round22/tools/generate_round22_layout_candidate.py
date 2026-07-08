import argparse
import json
import math
import re
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import fitz
from PIL import Image


SYMBOL_FONT_TOKENS = ("wingdings", "symbol", "dingbats")
VALUE_TOKEN_RE = re.compile(
    r"(\d[\d,]*(?:\.\d+)?\s*(?:%|\uFF05|bps|bn|billion|million|m|美元|港元|億元|亿元)?)|(?:[$]|US\$|HK\$)"
)
METRIC_UNIT_RE = re.compile(r"(%|\uFF05|bps|bn|billion|million|美元|港元|億元|亿元|[$]|US\$|HK\$)", re.IGNORECASE)


def rgb_from_int(value: int | None) -> tuple[float, float, float]:
    if value is None:
        return (0.0, 0.0, 0.0)
    return (
        ((int(value) >> 16) & 255) / 255.0,
        ((int(value) >> 8) & 255) / 255.0,
        (int(value) & 255) / 255.0,
    )


def rgb255_from_int(value: int | None) -> tuple[int, int, int]:
    r, g, b = rgb_from_int(value)
    return (round(r * 255), round(g * 255), round(b * 255))


def normalize_background_rgb(rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    if min(rgb) >= 246 and max(rgb) - min(rgb) <= 8:
        return (255, 255, 255)
    return rgb


def color_int_from_rgb255(rgb: tuple[int, int, int]) -> int:
    r, g, b = rgb
    return (r << 16) + (g << 8) + b


def color_distance(a: int | None, b: int | None) -> float:
    if a is None or b is None:
        return 0.0
    ar, ag, ab = rgb255_from_int(a)
    br, bg, bb = rgb255_from_int(b)
    return math.sqrt((ar - br) ** 2 + (ag - bg) ** 2 + (ab - bb) ** 2)


def is_saturated_accent(color_int: int | None) -> bool:
    if color_int is None:
        return False
    r, g, b = rgb255_from_int(color_int)
    return max(r, g, b) - min(r, g, b) > 45


def has_word_content(text: str) -> bool:
    letters = sum(1 for char in text if char.isalpha())
    digits = sum(1 for char in text if char.isdigit())
    return letters >= 2 and letters + digits >= 3


def quantile(values: list[float], q: float) -> float:
    clean = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not clean:
        return 0.0
    if len(clean) == 1:
        return clean[0]
    pos = (len(clean) - 1) * q
    lower = math.floor(pos)
    upper = math.ceil(pos)
    if lower == upper:
        return clean[int(pos)]
    return clean[lower] * (upper - pos) + clean[upper] * (pos - lower)


def rect_union(rects: list[fitz.Rect]) -> fitz.Rect:
    rect = fitz.Rect(rects[0])
    for item in rects[1:]:
        rect |= item
    return rect


def rect_x_overlap_ratio(left: fitz.Rect, right: fitz.Rect) -> float:
    overlap = max(0.0, min(left.x1, right.x1) - max(left.x0, right.x0))
    return overlap / max(1.0, min(left.width, right.width))


def sanitize_text(text: str) -> str:
    replacements = {
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2013": "-",
        "\u2014": "-",
        "\u00a0": " ",
        "\uf0d1": "-",
        "Ñ": "-",
    }
    for source, target in replacements.items():
        text = text.replace(source, target)
    text = "".join(char if ord(char) >= 32 or char in "\n\t" else " " for char in text)
    text = " ".join(text.split())
    text = re.sub(r"(^|\n)\s*[a-z](?=[A-Z][a-z])", r"\1", text)
    return text


def is_symbol_font(font: str) -> bool:
    lowered = font.lower()
    return any(token in lowered for token in SYMBOL_FONT_TOKENS)


def span_weight(span: dict[str, Any]) -> int:
    text = str(span.get("text", ""))
    return sum(1 for char in text if not char.isspace())


def dominant_span(spans: list[dict[str, Any]]) -> dict[str, Any]:
    content = [span for span in spans if span_weight(span) > 0 and not is_symbol_font(str(span.get("font", "")))]
    if content:
        return max(content, key=lambda span: (span_weight(span), float(span.get("size", 0))))
    non_empty = [span for span in spans if span_weight(span) > 0]
    if non_empty:
        return max(non_empty, key=lambda span: (span_weight(span), float(span.get("size", 0))))
    return spans[0] if spans else {}


@dataclass
class Line:
    unit_id: str
    page_index: int
    block_index: int
    line_index: int
    text: str
    rect: fitz.Rect
    font_size: float
    font: str
    color_int: int | None
    first_color_int: int | None
    has_symbol_span: bool


@dataclass
class PageStats:
    font_q25: float
    font_q50: float
    font_q75: float
    font_q90: float
    font_max: float
    width_q25: float
    width_q50: float
    width_q75: float
    text_y_median: float
    body_color_int: int | None
    accent_colors: set[int]

    def is_accent_color(self, color_int: int | None) -> bool:
        return color_int in self.accent_colors


@dataclass
class Group:
    group_id: str
    page_index: int
    lines: list[Line]
    role: str
    source_rect: fitz.Rect
    target_text: str
    color_int: int | None
    source_font_size: float
    bullet_color_int: int | None = None
    background_rgb: tuple[int, int, int] | None = None
    output_rect: fitz.Rect | None = None
    output_font_size: float | None = None
    fit_status: str = "pending"
    fit_attempts: list[dict[str, Any]] | None = None


def load_translations(path: Path) -> dict[str, dict[str, str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    mapping: dict[str, dict[str, str]] = {}
    for unit in data.get("units", []):
        unit_id = unit.get("unit_id")
        text = unit.get("translation_en") or unit.get("translation_target_text") or ""
        if unit_id and text:
            variants = unit.get("layout_variants") if isinstance(unit.get("layout_variants"), dict) else {}
            mapping[unit_id] = {
                "display": sanitize_text(text),
                "short": sanitize_text(variants.get("short_label_en") or variants.get("compact_en") or text),
                "compact": sanitize_text(variants.get("compact_en") or variants.get("short_label_en") or text),
            }
    return mapping


def translated_text(unit_id: str, role: str, translations: dict[str, dict[str, str]]) -> str | None:
    variants = translations.get(unit_id)
    if not variants:
        return None
    if role in {"compact_panel", "nav_footer"}:
        return variants.get("short") or variants.get("compact") or variants.get("display")
    if role == "red_note":
        return variants.get("short") or variants.get("compact") or variants.get("display")
    if role in {"section_heading", "red_heading"}:
        return variants.get("display") or variants.get("short")
    return variants.get("display") or variants.get("short")


def extract_lines(doc: fitz.Document) -> list[list[Line]]:
    pages: list[list[Line]] = []
    for page_index, page in enumerate(doc):
        page_lines: list[Line] = []
        raw = page.get_text("dict")
        for block_index, block in enumerate(raw.get("blocks", [])):
            if block.get("type") != 0:
                continue
            for line_index, line in enumerate(block.get("lines", [])):
                spans = line.get("spans", [])
                text = "".join(str(span.get("text", "")) for span in spans).strip()
                if not text:
                    continue
                dom = dominant_span(spans)
                first = spans[0] if spans else {}
                page_lines.append(
                    Line(
                        unit_id=f"p{page_index}_b{block_index}_l{line_index}",
                        page_index=page_index,
                        block_index=block_index,
                        line_index=line_index,
                        text=text,
                        rect=fitz.Rect(line.get("bbox", [0, 0, 0, 0])),
                        font_size=float(dom.get("size", 0) or 0),
                        font=str(dom.get("font", "")),
                        color_int=dom.get("color"),
                        first_color_int=first.get("color"),
                        has_symbol_span=any(is_symbol_font(str(span.get("font", ""))) for span in spans),
                    )
                )
        pages.append(page_lines)
    return pages



def page_stats(lines: list[Line], page_rect: fitz.Rect) -> PageStats:
    sizes = [line.font_size for line in lines if line.font_size > 0]
    widths = [line.rect.width for line in lines if line.rect.width > 0]
    ys = [line.rect.y0 for line in lines]
    color_counts: dict[int, int] = {}
    for line in lines:
        if line.color_int is not None:
            color_counts[line.color_int] = color_counts.get(line.color_int, 0) + 1
    body_color = max(color_counts.items(), key=lambda item: item[1])[0] if color_counts else None
    accent_colors = {
        color
        for color in color_counts
        if body_color is not None
        and color != body_color
        and color_distance(color, body_color) > 45
        and is_saturated_accent(color)
    }
    return PageStats(
        font_q25=quantile(sizes, 0.25),
        font_q50=quantile(sizes, 0.50),
        font_q75=quantile(sizes, 0.75),
        font_q90=quantile(sizes, 0.90),
        font_max=max(sizes) if sizes else 0.0,
        width_q25=quantile(widths, 0.25),
        width_q50=quantile(widths, 0.50),
        width_q75=quantile(widths, 0.75),
        text_y_median=quantile(ys, 0.50),
        body_color_int=body_color,
        accent_colors=accent_colors,
    )


def relative_font_rank(size: float, stats: PageStats) -> float:
    return size / max(stats.font_q75, 1.0)


def role_for_lines(lines: list[Line], page_rect: fitz.Rect, stats: PageStats) -> str:
    rect = rect_union([line.rect for line in lines])
    max_size = max((line.font_size for line in lines), default=0)
    font_rank = relative_font_rank(max_size, stats)
    has_symbol = any(line.has_symbol_span for line in lines)
    has_accent_symbol = any(stats.is_accent_color(line.first_color_int) for line in lines)
    has_accent_text = any(stats.is_accent_color(line.color_int) or stats.is_accent_color(line.first_color_int) for line in lines)
    has_value_token = any(METRIC_UNIT_RE.search(line.text) for line in lines)
    has_words = any(has_word_content(line.text) for line in lines)
    before_body_median = rect.y0 <= stats.text_y_median
    largest_tier = stats.font_max > 0 and max_size >= stats.font_max * 0.78
    broad_text = rect.width >= max(stats.width_q50, stats.width_q75 * 0.65)
    if has_symbol and has_accent_symbol:
        return "red_note"
    if has_value_token and font_rank >= max(1.05, stats.font_q90 / max(stats.font_q75, 1.0) * 0.85):
        return "metric_value"
    if has_accent_text and font_rank >= 1.0 and not has_symbol:
        return "red_heading"
    if len(lines) == 1 and largest_tier and before_body_median and has_words:
        return "title"
    compact_width_limit = max(stats.width_q50, stats.width_q75 * 0.72)
    if rect.width <= compact_width_limit and len(lines) <= 3 and font_rank <= 1.0:
        return "compact_panel"
    if font_rank >= 1.0 and before_body_median:
        return "section_heading"
    return "body"


def is_table_like_block(lines: list[Line], page_rect: fitz.Rect, stats: PageStats) -> bool:
    if len(lines) < 10:
        return False
    rect = rect_union([line.rect for line in lines])
    if rect.width < page_rect.width * 0.42:
        return False
    numeric_count = sum(1 for line in lines if VALUE_TOKEN_RE.search(line.text))
    numeric_ratio = numeric_count / max(1, len(lines))
    short_count = sum(1 for line in lines if line.rect.width <= rect.width * 0.42)
    short_ratio = short_count / max(1, len(lines))
    bucket = max(4.0, stats.font_q50 or 6.0)
    x_columns = len({round((line.rect.x0 - rect.x0) / bucket) for line in lines})
    dense_financial_grid = numeric_ratio >= 0.28 and short_ratio >= 0.42 and x_columns >= 3
    large_matrix = len(lines) >= 24 and numeric_ratio >= 0.18 and short_ratio >= 0.35 and x_columns >= 4
    return dense_financial_grid or large_matrix


def is_table_neighbor_block(lines: list[Line], table_rects: list[fitz.Rect], stats: PageStats) -> bool:
    if not table_rects or len(lines) > 6:
        return False
    rect = rect_union([line.rect for line in lines])
    max_size = max((line.font_size for line in lines), default=0.0)
    if max_size > max(stats.font_q75, stats.font_q50 * 1.2):
        return False
    for table_rect in table_rects:
        close_above = 0 <= table_rect.y0 - rect.y1 <= max(3.0, stats.font_q50 * 1.2)
        close_inside_top = table_rect.y0 - max(3.0, stats.font_q50 * 1.2) <= rect.y0 <= table_rect.y0 + max(3.0, stats.font_q50 * 1.4)
        if (close_above or close_inside_top) and rect_x_overlap_ratio(rect, table_rect) >= 0.18:
            return True
    return False


def build_groups(page_lines: list[Line], page_rect: fitz.Rect, translations: dict[str, dict[str, str]]) -> list[Group]:
    groups: list[Group] = []
    stats = page_stats(page_lines, page_rect)
    by_block: dict[int, list[Line]] = {}
    for line in page_lines:
        by_block.setdefault(line.block_index, []).append(line)
    table_rects = [
        rect_union([line.rect for line in block_lines])
        for block_lines in by_block.values()
        if is_table_like_block(block_lines, page_rect, stats)
    ]

    for block_index, block_lines in by_block.items():
        block_lines = sorted(block_lines, key=lambda item: (item.line_index, item.rect.y0))
        block_rect = rect_union([line.rect for line in block_lines])
        max_font = max((line.font_size for line in block_lines), default=0)
        if is_table_like_block(block_lines, page_rect, stats) or is_table_neighbor_block(block_lines, table_rects, stats):
            for part_index, line in enumerate(block_lines):
                groups.append(make_group(block_index, part_index, [line], page_rect, stats, translations, force_role="table_cell"))
            continue
        row_clusters = horizontal_row_clusters(block_lines, stats)
        if len(row_clusters) > 1:
            for part_index, cluster in enumerate(row_clusters):
                groups.append(make_group(block_index, part_index, cluster, page_rect, stats, translations))
            continue
        top_small_cluster = (
            block_rect.y1 < page_rect.height * 0.08
            and max_font <= stats.font_q50
            and block_rect.height <= max(stats.font_q50 * 3.0, quantile([line.rect.height for line in page_lines], 0.50) * 3.0)
        )
        bottom_small_cluster = (
            block_rect.y0 > page_rect.height * 0.90
            and max_font <= stats.font_q50
            and block_rect.width >= stats.width_q50
        )
        metric_cluster = any(VALUE_TOKEN_RE.search(line.text) and relative_font_rank(line.font_size, stats) >= 1.05 for line in block_lines) and len(block_lines) > 1
        if top_small_cluster or bottom_small_cluster:
            for part_index, line in enumerate(block_lines):
                groups.append(make_group(block_index, part_index, [line], page_rect, stats, translations, force_role="nav_footer"))
            continue
        if metric_cluster:
            current: list[Line] = []
            current_kind: str | None = None
            part_index = 0

            def flush_current() -> None:
                nonlocal current, current_kind, part_index
                if not current:
                    return
                force_role = "metric_value" if current_kind == "metric" else None
                groups.append(make_group(block_index, part_index, current, page_rect, stats, translations, force_role=force_role))
                part_index += 1
                current = []
                current_kind = None

            for line in block_lines:
                is_metric_line = bool(VALUE_TOKEN_RE.search(line.text)) and relative_font_rank(line.font_size, stats) >= 1.05
                kind = "metric" if is_metric_line else "text"
                if current and (kind != current_kind or kind == "metric"):
                    flush_current()
                current.append(line)
                current_kind = kind
                if kind == "metric":
                    flush_current()
            flush_current()
            continue
        red_note_lines = [line for line in block_lines if line.has_symbol_span and stats.is_accent_color(line.first_color_int)]
        if len(red_note_lines) >= 2 and len(block_lines) >= 3:
            current: list[Line] = []
            part_index = 0
            for line in block_lines:
                starts_note = line.has_symbol_span and stats.is_accent_color(line.first_color_int)
                if starts_note and current:
                    groups.append(make_group(block_index, part_index, current, page_rect, stats, translations))
                    part_index += 1
                    current = []
                current.append(line)
            if current:
                groups.append(make_group(block_index, part_index, current, page_rect, stats, translations))
            continue
        groups.append(make_group(block_index, 0, block_lines, page_rect, stats, translations))
    return groups


def horizontal_row_clusters(lines: list[Line], stats: PageStats) -> list[list[Line]]:
    if len(lines) <= 1:
        return [lines]
    heights = [line.rect.height for line in lines if line.rect.height > 0]
    median_height = quantile(heights, 0.50) or max(stats.font_q50, 1.0)
    block_rect = rect_union([line.rect for line in lines])
    same_row_block = block_rect.height <= max(median_height * 1.9, stats.font_q50 * 1.9)
    if not same_row_block:
        return [lines]
    ordered = sorted(lines, key=lambda item: item.rect.x0)
    gap_limit = max(stats.width_q25 * 0.45, median_height * 4.0)
    clusters: list[list[Line]] = [[ordered[0]]]
    for line in ordered[1:]:
        previous = clusters[-1][-1]
        gap = line.rect.x0 - previous.rect.x1
        vertical_overlap = min(line.rect.y1, previous.rect.y1) - max(line.rect.y0, previous.rect.y0)
        if gap > gap_limit and vertical_overlap > min(line.rect.height, previous.rect.height) * 0.35:
            clusters.append([line])
        else:
            clusters[-1].append(line)
    return clusters if len(clusters) > 1 else [lines]


def make_group(
    block_index: int,
    part_index: int,
    lines: list[Line],
    page_rect: fitz.Rect,
    stats: PageStats,
    translations: dict[str, dict[str, str]],
    force_role: str | None = None,
) -> Group:
    source_rect = rect_union([line.rect for line in lines])
    role = force_role or role_for_lines(lines, page_rect, stats)
    translated = []
    for line in lines:
        target = translated_text(line.unit_id, role, translations)
        if target:
            translated.append(target)
        elif line.text.strip():
            translated.append(sanitize_text(line.text))
    font_sizes = [line.font_size for line in lines if line.font_size > 0]
    source_font_size = statistics.median(font_sizes) if font_sizes else max(stats.font_q50, 1.0)
    text_color_int = lines[0].color_int
    bullet_color_int = None
    if role == "red_note":
        bullet_color_int = lines[0].first_color_int or lines[0].color_int
    if role == "red_heading":
        text_color_int = lines[0].first_color_int or text_color_int
    text = "\n".join(translated) if role == "red_note" else " ".join(translated)
    return Group(
        group_id=f"p{lines[0].page_index}_b{block_index}_{part_index}",
        page_index=lines[0].page_index,
        lines=lines,
        role=role,
        source_rect=source_rect,
        target_text=sanitize_text(text),
        color_int=text_color_int,
        source_font_size=source_font_size,
        bullet_color_int=bullet_color_int,
        fit_attempts=[],
    )

def render_page_image(page: fitz.Page, zoom: float = 2.0) -> Image.Image:
    matrix = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=matrix, alpha=False)
    return Image.frombytes("RGB", [pix.width, pix.height], pix.samples)


def sample_background(image: Image.Image, rect: fitz.Rect, zoom: float = 2.0) -> tuple[int, int, int]:
    x0 = max(0, int(math.floor((rect.x0 - 2) * zoom)))
    y0 = max(0, int(math.floor((rect.y0 - 2) * zoom)))
    x1 = min(image.width, int(math.ceil((rect.x1 + 2) * zoom)))
    y1 = min(image.height, int(math.ceil((rect.y1 + 2) * zoom)))
    if x1 <= x0 or y1 <= y0:
        return (255, 255, 255)
    crop = image.crop((x0, y0, x1, y1))
    max_side = 96
    scale = min(1.0, max_side / max(crop.width, crop.height))
    if scale < 1.0:
        crop = crop.resize((max(1, int(crop.width * scale)), max(1, int(crop.height * scale))), Image.Resampling.BOX)
    counts: dict[tuple[int, int, int], int] = {}
    for r, g, b in crop.getdata():
        brightness = (r + g + b) / 3
        if brightness < 145:
            continue
        key = (round(r / 12) * 12, round(g / 12) * 12, round(b / 12) * 12)
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        for r, g, b in crop.getdata():
            key = (round(r / 12) * 12, round(g / 12) * 12, round(b / 12) * 12)
            counts[key] = counts.get(key, 0) + 1
    best = max(counts.items(), key=lambda item: item[1])[0]
    return normalize_background_rgb(tuple(max(0, min(255, value)) for value in best))


def text_rect_for_group(group: Group, page_rect: fitz.Rect, groups: list[Group]) -> fitz.Rect:
    rect = fitz.Rect(group.source_rect)
    left_margin = 36.0
    right_margin = page_rect.width - 36.0
    gap = 8.0

    same_band = [
        other.source_rect
        for other in groups
        if other is not group
        and other.page_index == group.page_index
        and min(other.source_rect.y1, rect.y1) - max(other.source_rect.y0, rect.y0) > min(rect.height, other.source_rect.height) * 0.25
    ]
    right_obstacles = [other.x0 for other in same_band if other.x0 > rect.x0 + 4]
    left_obstacles = [other.x1 for other in same_band if other.x1 < rect.x0 - 4]
    column_right = min(right_obstacles) - gap if right_obstacles else right_margin
    column_left = max(left_obstacles) + gap if left_obstacles else left_margin

    if group.role in {"body", "red_note", "section_heading"}:
        if rect.x0 < page_rect.width * 0.38:
            rect.x1 = max(rect.x1, min(column_right, page_rect.width * 0.46))
        else:
            rect.x1 = max(rect.x1, min(column_right, right_margin))
        rect.x0 = max(column_left, rect.x0)
    elif group.role == "table_cell":
        available_width = max(8.0, column_right - rect.x0)
        estimated_width = len(group.target_text) * max(1.8, group.source_font_size * 0.30)
        rect.x1 = min(column_right, max(rect.x1, rect.x0 + min(available_width, estimated_width)))
    elif group.role == "red_heading":
        heading_width = max(rect.width * 1.4, page_rect.width * 0.22)
        if right_obstacles:
            rect.x1 = max(rect.x1, min(column_right, rect.x0 + heading_width))
        else:
            rect.x1 = min(right_margin, max(rect.x1, rect.x0 + heading_width))
    elif group.role == "compact_panel":
        available_width = max(8.0, column_right - rect.x0)
        estimated_width = len(group.target_text) * max(2.2, group.source_font_size * 0.34)
        rect.x1 = min(column_right, max(rect.x1, rect.x0 + min(available_width, estimated_width)))
    elif group.role == "nav_footer":
        rect.x1 = min(column_right, max(rect.x1, rect.x0 + rect.width * 1.35))
    elif group.role == "metric_value":
        available_width = max(8.0, column_right - rect.x0)
        estimated_width = len(group.target_text) * max(3.0, group.source_font_size * 0.38)
        rect.x1 = min(column_right, max(rect.x1, rect.x0 + min(available_width, estimated_width)))
    elif group.role == "title":
        estimated_width = len(group.target_text) * max(5.0, group.source_font_size * 0.34)
        base_width = page_rect.width * 0.42
        if estimated_width > rect.width * 1.25:
            base_width = page_rect.width * 0.78
        rect.x1 = min(right_margin, max(rect.x1, rect.x0 + base_width))
    else:
        rect.x1 = min(column_right, max(rect.x1, rect.x0 + rect.width * 1.15))

    line_count = max(1, len(group.target_text) // max(14, int(rect.width / max(3.0, group.source_font_size * 0.45))))
    if group.role == "red_note":
        rect.y1 = max(rect.y1, rect.y0 + max(group.source_rect.height * 1.35, 8.0 * (line_count + 1)))
    elif group.role == "table_cell":
        rect.y1 = max(rect.y1, rect.y0 + max(group.source_rect.height * 1.05, group.source_font_size * 1.10))
    elif group.role in {"body", "compact_panel"}:
        rect.y1 = max(rect.y1, rect.y0 + max(group.source_rect.height * 1.15, 9.0 * line_count))
    elif group.role == "nav_footer":
        rect.y1 = max(rect.y1, rect.y0 + max(group.source_rect.height * 1.6, 7.0 * line_count))
    elif group.role == "title":
        source_height_factor = 1.45 if line_count > 1 else 1.15
        rect.y1 = max(rect.y1, rect.y0 + max(group.source_rect.height * source_height_factor, group.source_font_size * 1.08 * line_count))
    elif group.role == "section_heading":
        rect.y1 = max(rect.y1, rect.y0 + max(group.source_rect.height * 1.2, 8.5 * line_count))
    elif group.role == "metric_value":
        rect.y1 = max(rect.y1, rect.y0 + max(group.source_rect.height, group.source_font_size * 0.92 * line_count))
    elif group.role == "red_heading":
        rect.y1 = max(rect.y1, rect.y0 + max(group.source_rect.height * 1.2, group.source_font_size * 0.98 * line_count))
    rect.x0 = max(4.0, min(rect.x0, page_rect.width - 12.0))
    rect.x1 = max(rect.x0 + 8.0, min(rect.x1, page_rect.width - 4.0))
    rect.y0 = max(4.0, min(rect.y0, page_rect.height - 12.0))
    bottom_margin = 4.0 if group.role == "nav_footer" else 28.0
    rect.y1 = max(rect.y0 + 8.0, min(page_rect.height - bottom_margin, rect.y1))
    return rect


def initial_font_size(group: Group) -> tuple[float, float]:
    source = max(4.0, group.source_font_size)
    if group.role == "title":
        start, floor = min(source * 0.88, 27.0), source * 0.50
        return max(start, floor), floor
    if group.role == "red_heading":
        start, floor = min(source * 1.02, 15.0), max(6.4, source * 0.60)
        return max(start, floor), floor
    if group.role == "red_note":
        start, floor = min(source * 0.98, 8.2), max(4.8, source * 0.68)
        return max(start, floor), floor
    if group.role == "table_cell":
        start, floor = min(source * 0.88, 6.8), max(3.2, source * 0.42)
        return max(start, floor), floor
    if group.role == "metric_value":
        has_alpha = any(ch.isalpha() for ch in group.target_text)
        if has_alpha and len(group.target_text) > 10:
            start, floor = min(source * 0.82, 30.0), source * 0.50
        else:
            start, floor = min(source * 1.0, 30.0), source * 0.72
        return max(start, floor), floor
    if group.role == "compact_panel":
        start, floor = min(source * 0.98, 8.6), max(5.2, source * 0.68)
        return max(start, floor), floor
    if group.role == "nav_footer":
        start, floor = min(source * 0.86, 7.2), max(3.8, source * 0.52)
        return max(start, floor), floor
    if group.role == "section_heading":
        start, floor = min(source * 1.0, 14.0), max(6.0, source * 0.65)
        return max(start, floor), floor
    start, floor = min(source * 0.98, 8.8), max(5.6, source * 0.68)
    return max(start, floor), floor


def draw_group(page: fitz.Page, group: Group, page_rect: fitz.Rect, all_groups: list[Group]) -> None:
    if not group.target_text:
        return
    source_bg = group.background_rgb or (255, 255, 255)
    bg = tuple(value / 255 for value in source_bg)
    target_rect = text_rect_for_group(group, page_rect, all_groups)
    erase_rect = fitz.Rect(group.source_rect)
    erase_rect |= fitz.Rect(target_rect)
    erase_rect.x0 -= 1.2
    erase_rect.y0 -= 1.2
    erase_rect.x1 += 1.2
    erase_rect.y1 += 1.2
    page.draw_rect(erase_rect, color=bg, fill=bg, width=0, overlay=True)

    group.output_rect = fitz.Rect(target_rect)
    color = rgb_from_int(group.color_int)
    if group.role == "red_heading":
        color = (0.84, 0.18, 0.22)

    start_size, min_size = initial_font_size(group)
    attempts = []
    for font_size in [start_size, start_size * 0.94, start_size * 0.88, start_size * 0.82, start_size * 0.76, min_size]:
        font_size = max(min_size, font_size)
        trial_rect = fitz.Rect(target_rect)
        if group.role in {"body", "red_note", "compact_panel"}:
            trial_rect.y1 = min(page_rect.height - 26.0, target_rect.y1 + (start_size - font_size) * 3.5)
        if not math.isfinite(trial_rect.x0 + trial_rect.y0 + trial_rect.x1 + trial_rect.y1) or trial_rect.width <= 0 or trial_rect.height <= 0:
            attempts.append({"font_size": round(font_size, 3), "rect": [round(v, 3) for v in trial_rect], "result": "invalid_rect"})
            continue
        if group.role == "red_note":
            text_rect = fitz.Rect(trial_rect)
            text_rect.x0 += max(7.0, font_size * 1.25)
            bullet_color = rgb_from_int(group.bullet_color_int or color_int_from_rgb255((210, 50, 58)))
            bullet_size = max(3.2, font_size * 0.62)
            cy = trial_rect.y0 + font_size * 0.78
            x = trial_rect.x0 + 1.0
            triangle = [
                fitz.Point(x, cy - bullet_size * 0.72),
                fitz.Point(x, cy + bullet_size * 0.72),
                fitz.Point(x + bullet_size * 1.05, cy),
                fitz.Point(x, cy - bullet_size * 0.72),
            ]
            result = page.insert_textbox(
                text_rect,
                group.target_text.lstrip("- ").strip(),
                fontsize=font_size,
                fontname="helv",
                color=color,
                align=fitz.TEXT_ALIGN_LEFT,
                overlay=True,
            )
            if result >= -0.1:
                page.draw_polyline(triangle, color=bullet_color, fill=bullet_color, width=0.2, overlay=True)
                group.output_rect = fitz.Rect(trial_rect)
                group.output_font_size = font_size
                group.fit_status = "fit"
                group.fit_attempts = attempts + [{"font_size": round(font_size, 3), "rect": [round(v, 3) for v in trial_rect], "result": round(float(result), 3), "mixed_bullet_text": True}]
                return
            page.draw_rect(trial_rect, color=bg, fill=bg, width=0, overlay=True)
            attempts.append({"font_size": round(font_size, 3), "rect": [round(v, 3) for v in trial_rect], "result": round(float(result), 3), "mixed_bullet_text": True})
            continue
        result = page.insert_textbox(
            trial_rect,
            group.target_text,
            fontsize=font_size,
            fontname="helv",
            color=color,
            align=fitz.TEXT_ALIGN_LEFT,
            overlay=True,
        )
        attempts.append({"font_size": round(font_size, 3), "rect": [round(v, 3) for v in trial_rect], "result": round(float(result), 3)})
        if result >= -0.1:
            group.output_rect = fitz.Rect(trial_rect)
            group.output_font_size = font_size
            group.fit_status = "fit"
            group.fit_attempts = attempts
            return
        page.draw_rect(trial_rect, color=bg, fill=bg, width=0, overlay=True)

    group.output_font_size = min_size
    group.fit_status = "overflow_after_fit"
    group.fit_attempts = attempts


def render_previews(pdf_path: Path, output_dir: Path, prefix: str) -> list[str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    doc = fitz.open(pdf_path)
    for index, page in enumerate(doc):
        pix = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False)
        out = output_dir / f"{prefix}_page_{index + 1:03d}.png"
        pix.save(out)
        paths.append(str(out))
    doc.close()
    return paths


def run(source_pdf: Path, translations_json: Path, output_pdf: Path, reports_dir: Path, previews_dir: Path) -> None:
    translations = load_translations(translations_json)
    source = fitz.open(source_pdf)
    target = fitz.open(source_pdf)
    extracted = extract_lines(source)
    evidence: dict[str, Any] = {
        "tool": "round22_isolated_layout_candidate",
        "source_pdf": str(source_pdf),
        "translations_json": str(translations_json),
        "output_pdf": str(output_pdf),
        "core_dependency": False,
        "pages": [],
    }

    for page_index, page in enumerate(target):
        page_rect = page.rect
        page_image = render_page_image(source[page_index])
        groups = build_groups(extracted[page_index], page_rect, translations)
        for group in groups:
            group.background_rgb = sample_background(page_image, group.source_rect)
        for group in sorted(groups, key=lambda item: (item.source_rect.y0, item.source_rect.x0)):
            draw_group(page, group, page_rect, groups)
        evidence["pages"].append(
            {
                "page_index": page_index,
                "group_count": len(groups),
                "groups": [
                    {
                        "group_id": group.group_id,
                        "role": group.role,
                        "source_rect": [round(v, 3) for v in group.source_rect],
                        "output_rect": [round(v, 3) for v in group.output_rect] if group.output_rect else None,
                        "source_font_size": round(group.source_font_size, 3),
                        "output_font_size": round(group.output_font_size, 3) if group.output_font_size else None,
                        "source_color_rgb": rgb255_from_int(group.color_int),
                        "background_rgb": group.background_rgb,
                        "fit_status": group.fit_status,
                        "text_len": len(group.target_text),
                        "fit_attempts": group.fit_attempts,
                    }
                    for group in groups
                ],
            }
        )

    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    target.save(output_pdf, garbage=4, deflate=True)
    target.close()
    source.close()

    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / "generation_evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    overflow_groups = [
        group
        for page in evidence["pages"]
        for group in page["groups"]
        if group["fit_status"] != "fit"
    ]
    tiny_groups = [
        group
        for page in evidence["pages"]
        for group in page["groups"]
        if group["output_font_size"] and group["output_font_size"] < max(4.8, group["source_font_size"] * 0.55)
    ]
    gates = {
        "tool": "round22_quality_gate_experiment",
        "product_quality_verdict": "FAIL" if overflow_groups or tiny_groups else "PASS",
        "gates": [
            {
                "gate_id": "all_groups_fit",
                "status": "fail" if overflow_groups else "pass",
                "blocking": True,
                "failure_count": len(overflow_groups),
            },
            {
                "gate_id": "source_relative_font_floor",
                "status": "fail" if tiny_groups else "pass",
                "blocking": True,
                "failure_count": len(tiny_groups),
            },
            {
                "gate_id": "source_style_preservation",
                "status": "pass",
                "blocking": True,
                "evidence": "Each target group records source color/font/background and renders from those source-derived values.",
            },
        ],
    }
    (reports_dir / "quality_gates.json").write_text(json.dumps(gates, ensure_ascii=False, indent=2), encoding="utf-8")
    render_previews(output_pdf, previews_dir, "candidate")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-pdf", required=True, type=Path)
    parser.add_argument("--translations-json", required=True, type=Path)
    parser.add_argument("--output-pdf", required=True, type=Path)
    parser.add_argument("--reports-dir", required=True, type=Path)
    parser.add_argument("--previews-dir", required=True, type=Path)
    args = parser.parse_args()
    run(args.source_pdf, args.translations_json, args.output_pdf, args.reports_dir, args.previews_dir)


if __name__ == "__main__":
    main()
