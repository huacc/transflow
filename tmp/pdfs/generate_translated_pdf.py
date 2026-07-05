from __future__ import annotations

from pathlib import Path
import json
import math
import re
import statistics
from typing import Iterable

import fitz


BASE = Path.cwd()
SOURCE = BASE / "01_source.pdf"
OUTPUT_DIR = BASE / "docs" / "output"
TMP_DIR = BASE / "tmp" / "pdfs"
OUTPUT_PDF = OUTPUT_DIR / "01_source.zh.pdf"
OUTPUT_RENDER = OUTPUT_DIR / "01_source.zh.page_01.png"
EVIDENCE_JSON = TMP_DIR / "translation_backfill_evidence.json"
VISUAL_METRICS_JSON = TMP_DIR / "visual_similarity_metrics.json"
CONTRACT_VISUAL_METRICS_JSON = BASE / "docs" / "pdf_translation_contract_01_source" / "visual_similarity_metrics.json"

FONT_REGULAR = r"C:\Windows\Fonts\msyh.ttc"
FONT_LIGHT = r"C:\Windows\Fonts\msyhl.ttc"
FONT_BOLD = r"C:\Windows\Fonts\msyhbd.ttc"

TEAL = (41 / 255, 81 / 255, 98 / 255)
WHITE = (1, 1, 1)
GRAY = (231 / 255, 225 / 255, 220 / 255)
COLUMN_BANDS = [
    ("col1", 95, 198),
    ("col2", 198, 302),
    ("col3", 302, 405),
    ("col4", 405, 510),
]


def text_width(font: fitz.Font, text: str, size: float) -> float:
    return font.text_length(text, fontsize=size)


def wrap_cjk(text: str, font: fitz.Font, size: float, max_width: float) -> list[str]:
    units = re.findall(r"[A-Za-z0-9]+|[\u4e00-\u9fff]|[^\u4e00-\u9fffA-Za-z0-9]", text)
    lines: list[str] = []
    current = ""
    closing_punctuation = set("，。；：、！？,. ;:!?%)）】》")
    for unit in units:
        if unit in closing_punctuation:
            if current:
                current += unit
            elif lines:
                lines[-1] += unit
            else:
                current = unit
            continue
        candidate = current + unit
        if current and text_width(font, candidate, size) > max_width:
            lines.append(current.rstrip())
            current = unit.lstrip()
        else:
            current = candidate
    if current.strip():
        lines.append(current.rstrip())
    return lines


def draw_wrapped(
    page: fitz.Page,
    rect: fitz.Rect,
    text: str,
    *,
    font: fitz.Font,
    fontname: str,
    fontfile: str,
    size: float,
    color: tuple[float, float, float],
    line_height: float,
    target_lines: int | None = None,
    min_width: float | None = None,
) -> tuple[float, list[str]]:
    draw_width = rect.width
    lines = wrap_cjk(text, font, size, draw_width)
    if target_lines and len(lines) < target_lines:
        lower_bound = min_width if min_width is not None else max(62, math.floor(rect.width * 0.68))
        for candidate_width in range(math.floor(rect.width) - 4, math.floor(lower_bound) - 1, -3):
            candidate = wrap_cjk(text, font, size, candidate_width)
            if len(candidate) >= target_lines:
                draw_width = float(candidate_width)
                lines = candidate
                break
    y = rect.y0
    for line in lines:
        if y + size > rect.y1:
            break
        page.insert_text(
            fitz.Point(rect.x0, y + size),
            line,
            fontname=fontname,
            fontfile=fontfile,
            fontsize=size,
            color=color,
        )
        y += line_height
    return y, lines


def draw_timeline_item(
    page: fitz.Page,
    item: dict,
    *,
    regular_font: fitz.Font,
    bold_font: fitz.Font,
) -> dict:
    x = item["x"]
    y = item["y"]
    width = item["w"]
    body_x = item.get("body_x", x)
    body_width = item.get("body_w", width)
    body = item["body"]
    year = item.get("year")
    body_size = item.get("body_size", 7.05)
    year_size = item.get("year_size", 12.0)
    body_line_height = item.get("body_line_height", 10.4)
    year_line_height = item.get("year_line_height", 13.0)
    inserted: dict = {"id": item["id"], "kind": "timeline_item", "x": x, "y": y, "w": width}
    current_y = y
    if year:
        page.insert_text(
            fitz.Point(x, current_y + year_size),
            year,
            fontname="msyhbd",
            fontfile=FONT_BOLD,
            fontsize=year_size,
            color=TEAL,
        )
        inserted["year"] = year
        current_y += year_line_height
    body_rect = fitz.Rect(body_x, current_y, body_x + body_width, item.get("bottom", 730))
    current_y, lines = draw_wrapped(
        page,
        body_rect,
        body,
        font=regular_font,
        fontname="msyhl",
        fontfile=FONT_LIGHT,
        size=body_size,
        color=TEAL,
        line_height=body_line_height,
        target_lines=item.get("target_lines"),
        min_width=item.get("min_width"),
    )
    inserted["body"] = body
    inserted["body_lines"] = lines
    inserted["bottom"] = round(current_y, 3)
    inserted["fits"] = current_y <= body_rect.y1
    return inserted


def add_text_redactions(page: fitz.Page) -> list[dict]:
    redactions = []
    for block in page.get_text("dict")["blocks"]:
        if block.get("type") != 0:
            continue
        rect = fitz.Rect(block["bbox"])
        rect.x0 -= 1.5
        rect.y0 -= 1.5
        rect.x1 += 1.5
        rect.y1 += 1.5
        page.add_redact_annot(rect)
        redactions.append({"bbox": [round(v, 3) for v in rect]})
    return redactions


def collect_column_metrics(pdf_path: Path) -> dict:
    doc = fitz.open(pdf_path)
    page = doc[0]
    lines = []
    for block in page.get_text("dict")["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            text = "".join(span.get("text", "") for span in line.get("spans", [])).strip()
            if not text:
                continue
            x0, y0, x1, y1 = line["bbox"]
            if y0 < 290:
                continue
            size = max((span.get("size", 0) for span in line.get("spans", [])), default=0)
            cx = (x0 + x1) / 2
            column = next((name for name, left, right in COLUMN_BANDS if left <= cx < right), "other")
            lines.append({"text": text, "bbox": [x0, y0, x1, y1], "column": column, "size": size})

    metrics = {}
    for name, _left, _right in COLUMN_BANDS:
        column_lines = [line for line in lines if line["column"] == name]
        ys = sorted(line["bbox"][1] for line in column_lines)
        gaps = [round(ys[i + 1] - ys[i], 2) for i in range(len(ys) - 1)]
        if column_lines:
            y_min = min(line["bbox"][1] for line in column_lines)
            y_max = max(line["bbox"][3] for line in column_lines)
        else:
            y_min = y_max = 0
        metrics[name] = {
            "line_count": len(column_lines),
            "y_min": round(y_min, 2),
            "y_max": round(y_max, 2),
            "y_span": round(y_max - y_min, 2),
            "line_area_sum": round(
                sum((line["bbox"][2] - line["bbox"][0]) * (line["bbox"][3] - line["bbox"][1]) for line in column_lines),
                2,
            ),
            "median_y_gap": statistics.median(gaps) if gaps else 0,
            "sizes": sorted(set(round(line["size"], 2) for line in column_lines)),
        }
    doc.close()
    return metrics


def write_visual_similarity_metrics() -> dict:
    source_metrics = collect_column_metrics(SOURCE)
    output_metrics = collect_column_metrics(OUTPUT_PDF)
    ratios = {}
    for column, source in source_metrics.items():
        output = output_metrics[column]
        ratios[column] = {
            "line_count_ratio": round(output["line_count"] / source["line_count"], 3) if source["line_count"] else None,
            "y_span_ratio": round(output["y_span"] / source["y_span"], 3) if source["y_span"] else None,
            "area_ratio": round(output["line_area_sum"] / source["line_area_sum"], 3) if source["line_area_sum"] else None,
            "median_gap_delta": round(output["median_y_gap"] - source["median_y_gap"], 2),
        }
    metrics = {
        "source": source_metrics,
        "output": output_metrics,
        "ratios": ratios,
        "notes": ["Metrics compare visual occupancy in column bands and intentionally ignore language semantics."],
    }
    VISUAL_METRICS_JSON.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    if CONTRACT_VISUAL_METRICS_JSON.parent.exists():
        CONTRACT_VISUAL_METRICS_JSON.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    return metrics


def make_output() -> dict:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    TMP_DIR.mkdir(parents=True, exist_ok=True)

    doc = fitz.open(SOURCE)
    page = doc[0]
    evidence: dict = {
        "source": str(SOURCE),
        "output_pdf": str(OUTPUT_PDF),
        "output_render": str(OUTPUT_RENDER),
        "state_transitions": [],
        "redactions": [],
        "insertions": [],
        "visual_overlays": [],
        "fit_warnings": [],
    }

    evidence["state_transitions"].append(
        {
            "from": "S4_vector_backfill_allowed",
            "to": "S5_remove_source_text",
            "reason": "redaction probe preserved images, red/gray backgrounds, and vector art",
        }
    )
    evidence["redactions"] = add_text_redactions(page)
    page.apply_redactions(
        images=fitz.PDF_REDACT_IMAGE_NONE,
        graphics=fitz.PDF_REDACT_LINE_ART_NONE,
        text=fitz.PDF_REDACT_TEXT_REMOVE,
    )

    regular_font = fitz.Font(fontfile=FONT_LIGHT)
    bold_font = fitz.Font(fontfile=FONT_BOLD)

    evidence["state_transitions"].append(
        {
            "from": "S5_remove_source_text",
            "to": "S6_insert_chinese_title",
            "reason": "source text objects were removed and page background remained editable",
        }
    )
    title_lines = ["自1919年以来", "推动亚洲经济", "与社会发展"]
    title_y = 84
    for line in title_lines:
        page.insert_text(
            fitz.Point(36, title_y + 27),
            line,
            fontname="msyh",
            fontfile=FONT_REGULAR,
            fontsize=27,
            color=WHITE,
        )
        evidence["insertions"].append(
            {"id": f"title_{len(evidence['insertions']) + 1}", "kind": "title_line", "text": line, "x": 36, "y": title_y, "size": 27}
        )
        title_y += 35

    items = [
        {"id": "1919", "year": "1919", "x": 102.7, "y": 295.2, "w": 91, "body_x": 115.0, "body_w": 78, "body": "友邦保险在亚洲奠定企业根基：集团创始人康纳利·范德·斯塔尔先生在上海创办保险代理机构。", "bottom": 379, "target_lines": 5, "min_width": 58},
        {"id": "1921", "year": "1921", "x": 115.0, "y": 381.5, "w": 78, "body": "康纳利·范德·斯塔尔先生在上海创办亚洲人寿保险公司，这是他的首家人寿保险企业。", "bottom": 447, "target_lines": 5, "min_width": 54},
        {"id": "1931", "year": "1931", "x": 102.7, "y": 448.2, "w": 91, "body": "康纳利·范德·斯塔尔先生在上海创办国际保险有限公司，简称国际保险公司。", "bottom": 514, "target_lines": 5, "min_width": 56},
        {"id": "intasco_branches", "x": 102.7, "y": 515.7, "w": 91, "body": "国际保险有限公司在香港和新加坡设立分支办事处。", "bottom": 548, "target_lines": 3, "min_width": 60},
        {"id": "1947", "year": "1947", "x": 102.7, "y": 549.5, "w": 91, "body": "菲律宾美国人寿及综合保险公司，即友邦菲律宾，在菲律宾成立。", "bottom": 615, "target_lines": 4, "min_width": 58},
        {"id": "intasco_hq", "x": 102.7, "y": 617.0, "w": 91, "body": "国际保险公司将总部迁至香港。", "bottom": 640},
        {"id": "1948", "year": "1948", "x": 102.7, "y": 640.9, "w": 91, "body": "国际保险有限公司更名为美国国际保险有限公司。", "bottom": 700, "target_lines": 4, "min_width": 60},
        {"id": "1992", "year": "1992", "x": 206.0, "y": 295.2, "w": 91, "body": "我们通过设于上海的分公司重新建立在中国的业务布局，成为国内首家获准经营的人寿保险外资企业。", "bottom": 380, "target_lines": 6, "min_width": 58},
        {"id": "1998", "year": "1998", "x": 206.0, "y": 381.5, "w": 91, "body": "我们庆祝重返位于上海外滩的昔日总部大楼。", "bottom": 438, "target_lines": 4, "min_width": 58},
        {"id": "2009", "year": "2009", "x": 206.0, "y": 438.4, "w": 91, "body": "我们完成由美国国际集团2008年流动性危机推动的重组，由此为公司公开上市完成定位与准备。", "bottom": 526, "target_lines": 6, "min_width": 58},
        {"id": "2010", "year": "2010", "x": 206.0, "y": 524.8, "w": 91, "body": "友邦保险控股有限公司成功在香港联合交易所主板上市，是当时全球第三大的首次公开募股项目。", "bottom": 610, "target_lines": 6, "min_width": 58},
        {"id": "2011", "year": "2011", "x": 309.3, "y": 295.2, "w": 91, "body": "友邦保险控股有限公司成为恒生指数的成份股。", "bottom": 352, "target_lines": 4, "min_width": 58},
        {"id": "adr", "x": 309.3, "y": 352.9, "w": 91, "body": "我们推出一项由公司赞助的一级美国存托凭证计划。", "bottom": 396, "target_lines": 4, "min_width": 58},
        {"id": "2013", "year": "2013", "x": 309.3, "y": 396.4, "w": 91, "body": "友邦保险完成对友邦保险与荷兰国际集团马来西亚业务的全面整合。", "bottom": 454, "target_lines": 4, "min_width": 58},
        {"id": "sri_lanka", "x": 309.3, "y": 454.1, "w": 91, "body": "我们通过收购斯里兰卡英杰华保险业务，在斯里兰卡开展业务。", "bottom": 497, "target_lines": 4, "min_width": 58},
        {"id": "2014", "year": "2014", "x": 309.3, "y": 497.6, "w": 91, "body": "友邦保险与花旗银行建立一项具有里程碑意义、长期且独家的银保合作伙伴关系，覆盖亚太11个市场。", "bottom": 575, "target_lines": 6, "min_width": 58},
        {"id": "tottenham", "x": 309.3, "y": 574.9, "w": 91, "body": "友邦保险成为托特纳姆热刺足球俱乐部官方球衣合作伙伴，推广运动作为健康生活的重要元素。", "bottom": 638, "target_lines": 6, "min_width": 62},
        {"id": "2015", "year": "2015", "x": 412.6, "y": 295.8, "w": 91, "body": "友邦保险成为全球第一的百万圆桌公司。", "bottom": 337},
        {"id": "2016", "year": "2016", "x": 412.6, "y": 396.3, "w": 91, "body": "友邦领导力中心在曼谷启用。", "bottom": 434, "target_lines": 2},
        {"id": "tata", "x": 412.6, "y": 434.4, "w": 91, "body": "我们将友邦保险集团在印度合资企业塔塔友邦人寿保险有限公司的持股比例由26%提高至49%。", "bottom": 498, "target_lines": 6, "min_width": 62},
        {"id": "2017", "year": "2017", "x": 412.6, "y": 497.5, "w": 91, "body": "友邦保险呈献香港摩天轮和友邦健康活力公园。", "bottom": 545, "target_lines": 3},
        {"id": "2018", "year": "2018", "x": 412.6, "y": 544.7, "w": 91, "body": "友邦保险推出全新品牌承诺：更健康、更长久、更美好的人生。", "bottom": 592, "target_lines": 4},
    ]

    evidence["state_transitions"].append(
        {
            "from": "S6_insert_chinese_title",
            "to": "S7_insert_timeline_items",
            "reason": "title Chinese text fit the red panel without changing panel geometry",
        }
    )
    for item in items:
        inserted = draw_timeline_item(page, item, regular_font=regular_font, bold_font=bold_font)
        evidence["insertions"].append(inserted)
        if not inserted["fits"]:
            evidence["fit_warnings"].append(inserted)

    evidence["state_transitions"].append(
        {
            "from": "S7_insert_timeline_items",
            "to": "S8_cover_visual_english_logo",
            "reason": "MDRT logo text remained after text redaction because it is vector art, not extractable text",
        }
    )
    mdrt_cover = fitz.Rect(411, 368, 477, 394)
    page.draw_rect(mdrt_cover, color=GRAY, fill=GRAY, overlay=True)
    page.insert_text(
        fitz.Point(413, 386),
        "百万圆桌®",
        fontname="msyhbd",
        fontfile=FONT_BOLD,
        fontsize=13,
        color=TEAL,
        overlay=True,
    )
    evidence["visual_overlays"].append(
        {
            "id": "mdrt_vector_logo_text",
            "action": "cover_vector_text_and_insert_chinese",
            "cover_bbox": [round(v, 3) for v in mdrt_cover],
            "replacement": "百万圆桌®",
            "reason": "The letters MDRT remained visible but were not returned by PDF text extraction.",
        }
    )

    evidence["state_transitions"].append(
        {
            "from": "S8_cover_visual_english_logo",
            "to": "S9_save_and_render",
            "reason": "all planned extractable text and the visible MDRT vector text had replacement actions",
        }
    )
    if OUTPUT_PDF.exists():
        OUTPUT_PDF.unlink()
    doc.save(OUTPUT_PDF, garbage=4, deflate=True)
    doc.close()

    out_doc = fitz.open(OUTPUT_PDF)
    pix = out_doc[0].get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
    pix.save(OUTPUT_RENDER)
    extracted_text = out_doc[0].get_text("text")
    out_doc.close()
    evidence["post_save_text_extract"] = extracted_text
    evidence["contains_ascii_letters_after_text_extract"] = bool(re.search(r"[A-Za-z]", extracted_text))
    visual_metrics = write_visual_similarity_metrics()
    evidence["visual_similarity_ratios"] = visual_metrics["ratios"]
    evidence["state_transitions"].append(
        {
            "from": "S9_save_and_render",
            "to": "S10_machine_extract_check",
            "reason": "output PDF and rendered PNG were written",
        }
    )
    EVIDENCE_JSON.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    return evidence


if __name__ == "__main__":
    data = make_output()
    print("output_pdf", data["output_pdf"])
    print("output_render", data["output_render"])
    print("evidence_json", str(EVIDENCE_JSON))
    print("visual_metrics_json", str(VISUAL_METRICS_JSON))
    print("fit_warnings", len(data["fit_warnings"]))
    print("contains_ascii_letters_after_text_extract", data["contains_ascii_letters_after_text_extract"])
