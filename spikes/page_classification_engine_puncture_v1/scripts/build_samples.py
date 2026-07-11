from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

import fitz


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parents[1]
OLD_ROOT = WORKSPACE / "spikes" / "page_classification_dual_qwen_puncture"
ANNUAL_ROOT = WORKSPACE / "样本" / "年报"
sys.path.insert(0, str(ROOT / "src"))

from page_classifier.io_utils import read_jsonl, sha256_file


ANNUAL_SPECS: list[dict[str, Any]] = [
    {"file": "01093_CSPC PHARMA_英文_2025.pdf", "page": 1, "role": "cover", "layout": None, "why": "报告主封面", "exemplar": ["page.role"]},
    {"file": "00810_CH CASTSON 81_英文_2025.pdf", "page": 1, "role": "cover", "layout": None, "why": "报告主封面"},
    {"file": "01596_YICHEN IND_英文_2025.pdf", "page": 1, "role": "cover", "layout": None, "why": "报告主封面"},
    {"file": "08210_DLC ASIA_英文_2025.pdf", "page": 1, "role": "cover", "layout": None, "why": "报告主封面"},
    {"file": "01399_銳信控股_中英合刊_2025.pdf", "page": 1, "role": "cover", "layout": None, "why": "报告主封面"},
    {"file": "00235_中策資本控股_中文_2025.pdf", "page": 3, "role": "contents", "layout": None, "why": "目录条目与页码重复对应", "exemplar": ["page.role"]},
    {"file": "02215_德信服務集團_中英合刊_2025.pdf", "page": 2, "role": "contents", "layout": None, "why": "双语目录"},
    {"file": "02017_滄海控股_中英合刊_2025.pdf", "page": 2, "role": "contents", "layout": None, "why": "图文目录"},
    {"file": "03700_映宇宙_中文_2025.pdf", "page": 2, "role": "contents", "layout": None, "why": "中文目录"},
    {"file": "08087_CHINA 33MEDIA_英文_2025.pdf", "page": 3, "role": "contents", "layout": None, "why": "英文目录"},
    {"file": "01967_信懇智能_中文_2025.pdf", "page": 246, "role": "end", "layout": None, "why": "品牌结束页", "exemplar": ["page.role"]},
    {"file": "00482_聖馬丁國際_中英合刊_2026.pdf", "page": 206, "role": "end", "layout": None, "why": "品牌结束视觉页"},
    {"file": "08092_ITE HOLDINGS_中英合刊_2025.pdf", "page": 144, "role": "end", "layout": None, "why": "品牌结束视觉页"},
    {"file": "00050_HK FERRY (HOLD)_英文_2025.pdf", "page": 218, "role": "visual_only", "layout": None, "why": "无文字装饰空白页", "exemplar": ["page.role"]},
    {"file": "01380_中國金石_中英合刊_2025.pdf", "page": 127, "role": "body", "layout": "flow_text", "why": "双语稳定多栏正文", "exemplar": ["page.role", "body.layout_owner"]},
    {"file": "01528_RS MACALLINE_英文_2025.pdf", "page": 3, "role": "body", "layout": "flow_text", "why": "英文单栏公司简介"},
    {"file": "02119_捷榮國際控股_中英合刊_2025.pdf", "page": 49, "role": "body", "layout": "flow_text", "why": "双语左右栏正文"},
    {"file": "03988_中國銀行_中文_2025.pdf", "page": 60, "role": "body", "layout": "flow_text", "why": "中文双栏正文"},
    {"file": "01745_LVJI TECH_英文_2025.pdf", "page": 7, "role": "body", "layout": "flow_text", "why": "主席报告正文，含 contents 单词但不是目录"},
    {"file": "01717_AUSNUTRIA_英文_2025.pdf", "page": 73, "role": "body", "layout": "table", "why": "主体财务网格", "exemplar": ["body.layout_owner"]},
    {"file": "03988_中國銀行_中文_2025.pdf", "page": 11, "role": "body", "layout": "table", "why": "密集财务摘要表"},
    {"file": "08317_FINET GROUP_英文_2025-2026.pdf", "page": 7, "role": "body", "layout": "table", "why": "财务摘要表"},
    {"file": "00995_安徽皖通高速公路_中文_2025.pdf", "page": 244, "role": "body", "layout": "table", "why": "稀疏财务表"},
    {"file": "01596_翼辰實業_中文_2026.pdf", "page": 5, "role": "body", "layout": "table", "why": "财务摘要表，附属图片不抢主体"},
    {"file": "00987_中國再生能源投資_中文_2026.pdf", "page": 2, "role": "body", "layout": "diagram", "why": "项目地图及位置标注", "exemplar": ["body.layout_owner"]},
    {"file": "01244_思路迪醫藥股份_中英合刊_2026.pdf", "page": 227, "role": "body", "layout": "composite", "why": "正文与公司资料表都承担内容", "exemplar": ["body.layout_owner"]},
    {"file": "02571_SAIMO_英文_2025.pdf", "page": 112, "role": "body", "layout": "composite", "why": "正文与员工费用表并存"},
    {"file": "01528_紅星美凱龍_中文_2025.pdf", "page": 6, "role": "body", "layout": "composite", "why": "财务表与经营图并存"},
    {"file": "00305_五菱汽車_中英合刊_2026.pdf", "page": 2, "role": "body", "layout": "composite", "why": "公司简介与集团结构图并存"},
]


def old_route_map() -> dict[str, str]:
    rows = read_jsonl(OLD_ROOT / "分类结果" / "classification_manifest.jsonl")
    return {Path(row["source_path"]).name: str(row["leaf_key"]) for row in rows}


def old_gold(name: str, route: str) -> dict[str, Any]:
    role: str | None
    layout: str | None = None
    role_status = "PROVISIONAL"
    layout_status = "NOT_APPLICABLE"
    flow_topology: str | None = None
    flow_topology_status = "NOT_APPLICABLE"
    composite_kind: str | None = None
    composite_kind_status = "NOT_APPLICABLE"
    rationale = f"继承旧穿刺路由 {route}，未人工确认"
    if "正文混标题" in name:
        role, role_status, rationale = "body", "CONFIRMED", "人工截图复核：股东大会通知正文，不是封面"
    elif "标题" in name:
        role, role_status, rationale = "cover", "CONFIRMED", "文件对应页面已按标题页类别复核"
    elif "目录" in name:
        role, role_status, rationale = "contents", "CONFIRMED", "文件对应页面已按目录页类别复核"
    elif "空白" in name:
        role, role_status, rationale = "visual_only", "CONFIRMED", "截图确认仅含建筑照片或纯视觉，无可翻译内容"
    else:
        role = "body" if route.startswith("body/") or "正文" in name or "简介" in name else None
        if role == "body":
            role_status = "CONFIRMED"

    if role == "body":
        layout_status = "PROVISIONAL"
        if route.startswith("body/text"):
            layout = "flow_text"
        elif route == "body/table":
            layout = "table"
        elif route == "body/chart":
            layout = "chart"
        elif route == "body/freeform":
            layout = "anchored_blocks"
        if "3列正文" in name or "单列正文" in name or "正文页单列" in name or "正文混标题" in name:
            layout, layout_status, rationale = "flow_text", "CONFIRMED", "截图确认存在稳定正文阅读流"
        elif "柱状图" in name:
            layout, layout_status, rationale = "chart", "CONFIRMED", "截图确认柱状图占主体"
        elif "表格正文页" in name:
            layout, layout_status, rationale = "table", "CONFIRMED", "截图确认表格占主体"
        elif "正文+表格" in name:
            layout, rationale = "composite", "PROVISIONAL：文件名和旧样本说明指向正文与表格混合"
        anchored_confirmed = {
            "00005_2025_annual_report_en_03_正文页.pdf",
            "00005_2025_annual_report_zh_03_正文页.pdf",
            "AIA_2020_Annual_Report_en_06_简介页.pdf",
            "00388_2025_annual_report_en_10_正文页.pdf",
            "00388_2025_annual_report_zh_10_正文页.pdf",
        }
        visual_flow_confirmed = {
            "AIA_2020_Annual_Report_en_04_简介页.pdf",
            "AIA_2020_Annual_Report_zh_04_简介页.pdf",
        }
        composite_blocks_chart_confirmed = {
            "00005_2025_annual_report_en_06_正文页.pdf",
            "00005_2025_annual_report_zh_06_正文页.pdf",
        }
        composite_flow_table_confirmed = {
            "00005_2025_interim_report_zh_005_正文+表格（密集型）.pdf",
            "00005_2025_interim_report_zh_006_正文+表格（密集型）.pdf",
            "00005_2025_interim_report_zh_007_正文+表格（密集型）.pdf",
        }
        chart_confirmed = {
            "AIA_2020_Annual_Report_en_08_正文页.pdf",
            "AIA_2020_Annual_Report_en_09_正文页.pdf",
            "AIA_2020_Annual_Report_zh_08_正文页.pdf",
            "AIA_2020_Annual_Report_zh_09_正文页.pdf",
        }
        if name in visual_flow_confirmed:
            layout, layout_status, rationale = "flow_text", "CONFIRMED", "截图确认大面积固定图片拼贴中只有一个局部连续正文区"
        elif name in anchored_confirmed:
            layout, layout_status, rationale = "anchored_blocks", "CONFIRMED", "截图确认独立 KPI/信息块占主体"
        elif name in composite_blocks_chart_confirmed:
            layout, layout_status = "composite", "CONFIRMED"
            composite_kind, composite_kind_status = "anchored_blocks_chart", "CONFIRMED"
            rationale = "人工截图复核：独立卡片块与环形图共同主导页面"
        elif name in composite_flow_table_confirmed:
            layout, layout_status = "composite", "CONFIRMED"
            composite_kind, composite_kind_status = "flow_text_table", "CONFIRMED"
            rationale = "人工截图复核：主体表格与表外正文都不可忽略"
        elif name in chart_confirmed:
            layout, layout_status, rationale = "chart", "CONFIRMED", "截图确认柱状图或饼图占主体"
    if layout == "composite" and composite_kind_status == "NOT_APPLICABLE":
        composite_kind_status = "PROVISIONAL"
    if layout == "flow_text":
        flow_topology_status = "PROVISIONAL"
        flow_topology = "multi" if route.endswith("/multi") else "single"
        if "3列正文" in name:
            flow_topology = "multi"
            flow_topology_status = "CONFIRMED"
            rationale = "人工截图复核：主体形成三条独立栏道"
        elif "正文混标题" in name or "单列正文" in name or "正文页单列" in name:
            flow_topology = "single"
            flow_topology_status = "CONFIRMED"
            rationale = "人工截图复核：主体只有一条稳定栏道"
        elif name in {
            "AIA_2020_Annual_Report_en_10_正文页.pdf",
            "AIA_2020_Annual_Report_zh_10_正文页.pdf",
        }:
            flow_topology = "single"
            flow_topology_status = "CONFIRMED"
            rationale = "人工截图复核：密集单栏长篇正文"
        elif name in {
            "AIA_2020_Annual_Report_en_04_简介页.pdf",
            "AIA_2020_Annual_Report_zh_04_简介页.pdf",
        }:
            flow_topology = "visual_anchored"
            flow_topology_status = "CONFIRMED"
            rationale = "人工截图复核：固定图片拼贴中存在局部锚定正文流"
        elif name in {
            "00005_2025_annual_report_en_04_正文页.pdf",
            "00005_2025_annual_report_en_05_正文页.pdf",
            "00005_2025_annual_report_zh_04_正文页.pdf",
            "00005_2025_annual_report_zh_05_正文页.pdf",
        }:
            flow_topology = "multi"
            flow_topology_status = "CONFIRMED"
            rationale = "人工截图复核：密集正文形成三条独立栏道"
    return {
        "role": role,
        "layout_owner": layout,
        "flow_topology": flow_topology,
        "composite_kind": composite_kind,
        "role_gold_status": role_status,
        "layout_gold_status": layout_status,
        "flow_topology_gold_status": flow_topology_status,
        "composite_kind_gold_status": composite_kind_status,
        "rationale": rationale,
    }


def write_single_page(source: Path, page_number: int, target: Path) -> int:
    with fitz.open(source) as document:
        page_count = document.page_count
        if not 1 <= page_number <= page_count:
            raise ValueError(f"invalid_page:{source.name}:{page_number}/{page_count}")
        output = fitz.open()
        output.insert_pdf(document, from_page=page_number - 1, to_page=page_number - 1)
        output.save(target)
        output.close()
    return page_count


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--legacy-only",
        action="store_true",
        help="只使用 page_classification_dual_qwen_puncture/样本，不追加年报抽样页",
    )
    args = parser.parse_args()
    sample_dir = ROOT / "样本1"
    for path in sample_dir.glob("*.pdf"):
        path.unlink()
    sources: list[dict[str, Any]] = []
    gold: list[dict[str, Any]] = []
    exemplars: list[dict[str, Any]] = []
    old_routes = old_route_map()
    old_samples = sorted((OLD_ROOT / "样本").glob("*.pdf"))
    index = 0
    for old_path in old_samples:
        index += 1
        sample_id = f"P{index:04d}"
        target = sample_dir / f"{sample_id}.pdf"
        shutil.copy2(old_path, target)
        route = old_routes[old_path.name]
        labels = old_gold(old_path.name, route)
        split = "test"
        exemplar_nodes: list[str] = []
        if old_path.name == "00005_2025_annual_report_en_03_正文页.pdf":
            split, exemplar_nodes = "exemplar", ["body.layout_owner"]
        elif old_path.name == "00005_2025_annual_report_en_04_正文页.pdf":
            split, exemplar_nodes = "exemplar", ["body.layout_owner"]
        elif old_path.name == "00005_2025_annual_report_en_06_正文页.pdf":
            split, exemplar_nodes = "exemplar", ["body.composite.kind"]
        elif old_path.name == "00005_2025_interim_report_zh_005_正文+表格（密集型）.pdf":
            split, exemplar_nodes = "exemplar", ["body.composite.kind"]
        elif old_path.name == "00388_2025_annual_report_en_12_柱状图_正文页.pdf":
            split, exemplar_nodes = "exemplar", ["body.layout_owner"]
        source_row = {
            "sample_id": sample_id,
            "source_kind": "legacy_single_page",
            "source_path": str(old_path),
            "source_page_number": None,
            "source_page_count": None,
            "source_sha256": sha256_file(old_path),
            "sample_sha256": sha256_file(target),
        }
        sources.append(source_row)
        gold.append({"sample_id": sample_id, "split": split, **labels})
        for node_key in exemplar_nodes:
            if node_key == "body.layout_owner":
                label = labels["layout_owner"]
            elif node_key == "body.composite.kind":
                label = labels["composite_kind"]
            else:
                label = labels["role"]
            exemplars.append({"node_key": node_key, "label": label, "sample_id": sample_id})

    annual_specs = [] if args.legacy_only else ANNUAL_SPECS
    for spec in annual_specs:
        index += 1
        sample_id = f"P{index:04d}"
        source = ANNUAL_ROOT / spec["file"]
        target = sample_dir / f"{sample_id}.pdf"
        page_count = write_single_page(source, int(spec["page"]), target)
        exemplar_nodes = list(spec.get("exemplar", []))
        split = "exemplar" if exemplar_nodes else "test"
        sources.append(
            {
                "sample_id": sample_id,
                "source_kind": "annual_report_extracted_page",
                "source_path": str(source),
                "source_page_number": int(spec["page"]),
                "source_page_count": page_count,
                "source_sha256": sha256_file(source),
                "sample_sha256": sha256_file(target),
            }
        )
        gold.append(
            {
                "sample_id": sample_id,
                "split": split,
                "role": spec["role"],
                "layout_owner": spec["layout"],
                "composite_kind": spec.get("composite_kind"),
                "role_gold_status": "CONFIRMED",
                "layout_gold_status": "CONFIRMED" if spec["layout"] else "NOT_APPLICABLE",
                "flow_topology": None,
                "flow_topology_gold_status": "PROVISIONAL" if spec["layout"] == "flow_text" else "NOT_APPLICABLE",
                "composite_kind_gold_status": "PROVISIONAL" if spec["layout"] == "composite" else "NOT_APPLICABLE",
                "rationale": spec["why"],
            }
        )
        for node_key in exemplar_nodes:
            if node_key == "body.layout_owner":
                label = spec["layout"]
            elif node_key == "body.composite.kind":
                label = spec.get("composite_kind")
            else:
                label = spec["role"]
            exemplars.append({"node_key": node_key, "label": label, "sample_id": sample_id})

    manifest_dir = ROOT / "manifests"
    (manifest_dir / "source_manifest.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in sources), encoding="utf-8")
    (manifest_dir / "gold_manifest.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in gold), encoding="utf-8")
    (ROOT / "exemplars" / "manifest.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in exemplars), encoding="utf-8")
    pdfs = sorted(sample_dir.glob("*.pdf"))
    page_counts: list[int] = []
    for path in pdfs:
        with fitz.open(path) as document:
            page_counts.append(document.page_count)
    if len(pdfs) != len(sources) or any(page_count != 1 for page_count in page_counts):
        raise RuntimeError("sample_integrity_failed")
    print(json.dumps({"sample_count": len(pdfs), "legacy_count": len(old_samples), "annual_count": len(annual_specs), "exemplar_count": len({row['sample_id'] for row in exemplars})}, ensure_ascii=False))


if __name__ == "__main__":
    main()
