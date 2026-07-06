"""Evaluate source-vs-output PDF quality gates.

tool_name: evaluate_pdf_quality
category: validators
input_contract: source PDF path, candidate PDF path
output_contract: JSON with product_quality_verdict and gate evidence
failure_signals: missing PDFs, unreadable PDFs, metric computation exception
fallback: mark S_FAIL_TOOLING or quality unknown
anti_overfit_statement: metrics are structural and do not branch on sample names, fixed text, or page numbers
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import ascii_tokens, median, rel, resolve_workspace_path, write_json  # noqa: E402


FORBIDDEN_TRANSLATION_PROVIDERS = {None, "", "deterministic_placeholder", "placeholder", "manual_placeholder"}
UNIT_ID_RE = re.compile(r"^(p\d+_b\d+)_l(\d+)$")
CJK_RE = re.compile(r"[\u3400-\u9fff]")


def unit_ref(unit_id: Any) -> tuple[str, int] | None:
    match = UNIT_ID_RE.match(str(unit_id))
    if not match:
        return None
    return match.group(1), int(match.group(2))


def source_anchor_order_violations(evidence_data: dict[str, Any]) -> list[dict[str, Any]]:
    violations: list[dict[str, Any]] = []
    for insertion in evidence_data.get("insertions", []):
        if not isinstance(insertion, dict):
            continue
        unit_ids = [str(item) for item in insertion.get("unit_ids", [])]
        refs = [unit_ref(unit_id) for unit_id in unit_ids]
        refs = [ref for ref in refs if ref is not None]
        if len(refs) < 2:
            continue
        for (prev_block, prev_index), (next_block, next_index) in zip(refs, refs[1:]):
            if prev_block == next_block and next_index - prev_index > 1:
                violations.append(
                    {
                        "region_id": insertion.get("region_id") or insertion.get("slot_id"),
                        "unit_ids": unit_ids,
                        "gap": {
                            "block": prev_block,
                            "previous_line_index": prev_index,
                            "next_line_index": next_index,
                            "skipped_line_count": next_index - prev_index - 1,
                        },
                        "reason": "one inserted region crosses source lines that were not translation units; this can move text across visible headings, years, bullets, or separators",
                    }
                )
                break
    return violations


def line_records(page: fitz.Page) -> list[dict[str, Any]]:
    records = []
    for block in page.get_text("dict")["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            text = "".join(span.get("text", "") for span in spans).strip()
            if not text:
                continue
            bbox = [float(v) for v in line.get("bbox", [0, 0, 0, 0])]
            size = float(spans[0].get("size", 0)) if spans else 0
            records.append({"text": text, "bbox": bbox, "font_size": size})
    return records


def metrics(lines: list[dict[str, Any]]) -> dict[str, Any]:
    if not lines:
        return {
            "line_count": 0,
            "text_area": 0.0,
            "y_span": 0.0,
            "median_gap": None,
            "median_font_size": None,
        }
    area = 0.0
    y0s = []
    y1s = []
    sizes = []
    for item in lines:
        x0, y0, x1, y1 = item["bbox"]
        area += max(0.0, x1 - x0) * max(0.0, y1 - y0)
        y0s.append(y0)
        y1s.append(y1)
        sizes.append(item["font_size"])
    ordered = sorted(lines, key=lambda item: (item["bbox"][1], item["bbox"][0]))
    gaps = [ordered[i + 1]["bbox"][1] - ordered[i]["bbox"][3] for i in range(len(ordered) - 1)]
    return {
        "line_count": len(lines),
        "text_area": round(area, 3),
        "y_span": round(max(y1s) - min(y0s), 3),
        "median_gap": None if median(gaps) is None else round(float(median(gaps)), 3),
        "median_font_size": None if median(sizes) is None else round(float(median(sizes)), 3),
    }


def quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * q)))
    return float(ordered[index])


def font_hierarchy(lines: list[dict[str, Any]]) -> dict[str, Any]:
    sizes = [float(item["font_size"]) for item in lines if float(item.get("font_size") or 0) > 0]
    if not sizes:
        return {
            "q10": None,
            "q25": None,
            "q50": None,
            "q75": None,
            "q90": None,
            "small_to_body_ratio": None,
            "large_to_body_ratio": None,
        }
    q25 = quantile(sizes, 0.25)
    q75 = quantile(sizes, 0.75)
    q90 = quantile(sizes, 0.90)
    return {
        "q10": round(float(quantile(sizes, 0.10)), 3),
        "q25": round(float(q25), 3),
        "q50": round(float(quantile(sizes, 0.50)), 3),
        "q75": round(float(q75), 3),
        "q90": round(float(q90), 3),
        "small_to_body_ratio": ratio(q25, q75),
        "large_to_body_ratio": ratio(q90, q75),
    }


def ratio(out_value: float | int | None, source_value: float | int | None) -> float | None:
    if source_value in (None, 0):
        return None
    if out_value is None:
        return None
    return round(float(out_value) / float(source_value), 3)


def visual_dimension_status(visual_data: dict[str, Any] | None, dimension: str) -> str | None:
    if visual_data is None:
        return None
    for item in visual_data.get("dimensions", []):
        if isinstance(item, dict) and item.get("dimension") == dimension:
            return str(item.get("status") or "")
    return None


def evaluate(
    source: Path,
    output: Path,
    generation_evidence: Path | None = None,
    visual_adjudication: Path | None = None,
    visual_region_metrics: Path | None = None,
) -> dict[str, Any]:
    src = fitz.open(source)
    out = fitz.open(output)
    gates: list[dict[str, Any]] = []
    page_count_pass = src.page_count == out.page_count
    gates.append(
        {
            "gate_id": "page_count",
            "status": "pass" if page_count_pass else "fail",
            "blocking": True,
            "evidence": f"source={src.page_count}; output={out.page_count}",
        }
    )
    page_results = []
    output_ascii = []
    output_cjk_chars = []
    evidence_data: dict[str, Any] = {}
    if generation_evidence is not None and generation_evidence.exists():
        evidence_data = json.loads(generation_evidence.read_text(encoding="utf-8"))
    target_language = str(evidence_data.get("target_language") or "zh").lower()
    for page_index in range(min(src.page_count, out.page_count)):
        s_page = src[page_index]
        o_page = out[page_index]
        rect_match = [round(v, 3) for v in s_page.rect] == [round(v, 3) for v in o_page.rect]
        gates.append(
            {
                "gate_id": "page_geometry",
                "scope": f"page_{page_index + 1}",
                "status": "pass" if rect_match else "fail",
                "blocking": True,
                "evidence": f"source={list(s_page.rect)}; output={list(o_page.rect)}",
            }
        )
        tokens = ascii_tokens(o_page.get_text("text"))
        output_ascii.extend(tokens)
        output_cjk_chars.extend(CJK_RE.findall(o_page.get_text("text")))
        s_lines = line_records(s_page)
        o_lines = line_records(o_page)
        s_metrics = metrics(s_lines)
        o_metrics = metrics(o_lines)
        s_font_hierarchy = font_hierarchy(s_lines)
        o_font_hierarchy = font_hierarchy(o_lines)
        page_results.append(
            {
                "page_index": page_index,
                "source_metrics": s_metrics,
                "output_metrics": o_metrics,
                "source_font_hierarchy": s_font_hierarchy,
                "output_font_hierarchy": o_font_hierarchy,
                "ratios": {
                    "text_area_ratio": ratio(o_metrics["text_area"], s_metrics["text_area"]),
                    "line_count_ratio": ratio(o_metrics["line_count"], s_metrics["line_count"]),
                    "y_span_ratio": ratio(o_metrics["y_span"], s_metrics["y_span"]),
                    "font_size_ratio": ratio(o_metrics["median_font_size"], s_metrics["median_font_size"]),
                    "small_to_body_ratio_delta": (
                        None
                        if s_font_hierarchy["small_to_body_ratio"] is None or o_font_hierarchy["small_to_body_ratio"] is None
                        else round(float(o_font_hierarchy["small_to_body_ratio"]) - float(s_font_hierarchy["small_to_body_ratio"]), 3)
                    ),
                },
            }
        )
    ascii_unique = sorted(set(output_ascii))
    cjk_unique = sorted(set(output_cjk_chars))
    residue_fail = bool(ascii_unique) if target_language == "zh" else bool(cjk_unique)
    gates.append(
        {
            "gate_id": "text_residue",
            "status": "pass" if not residue_fail else "fail",
            "blocking": True,
            "evidence": {
                "target_language": target_language,
                "ascii_token_count": len(ascii_unique),
                "cjk_unique_count": len(cjk_unique),
                "sample": ascii_unique[:80] if target_language == "zh" else cjk_unique[:80],
            },
        }
    )
    visual_data: dict[str, Any] | None = None
    if visual_adjudication is not None and visual_adjudication.exists():
        visual_data = json.loads(visual_adjudication.read_text(encoding="utf-8"))
    region_data: dict[str, Any] | None = None
    if visual_region_metrics is not None and visual_region_metrics.exists():
        region_data = json.loads(visual_region_metrics.read_text(encoding="utf-8"))

    if generation_evidence is not None and generation_evidence.exists():
        semantic_coverage = evidence_data.get("semantic_coverage")
        translation_quality = evidence_data.get("translation_quality")
        translation_provider = evidence_data.get("translation_provider")
        semantic_translation_validation = evidence_data.get("semantic_translation_validation")
        visual_quality_adjudication = (
            visual_data.get("verdict")
            if visual_data is not None
            else evidence_data.get("visual_quality_adjudication")
        )
        real_backfill_pdf = bool(evidence_data.get("real_backfill_pdf"))
        redacted_line_count = int(evidence_data.get("redacted_line_count") or 0)
        inserted_line_count = int(evidence_data.get("inserted_line_count") or 0)
        inserted_unit_count = int(evidence_data.get("inserted_unit_count") or inserted_line_count)
        inserted_region_count = int(evidence_data.get("inserted_region_count") or inserted_line_count)
        fit_warning_count = int(evidence_data.get("fit_warning_count") or 0)
        gates.append(
            {
                "gate_id": "backfill_generation",
                "status": "pass" if real_backfill_pdf and redacted_line_count == inserted_unit_count and inserted_unit_count > 0 else "fail",
                "blocking": True,
                "evidence": {
                    "real_backfill_pdf": real_backfill_pdf,
                    "redacted_line_count": redacted_line_count,
                    "inserted_line_count": inserted_line_count,
                    "inserted_unit_count": inserted_unit_count,
                    "inserted_region_count": inserted_region_count,
                    "generation_evidence": rel(generation_evidence),
                },
            }
        )
        gates.append(
            {
                "gate_id": "text_fit",
                "status": "pass" if fit_warning_count == 0 else "fail",
                "blocking": True,
                "evidence": {
                    "fit_warning_count": fit_warning_count,
                    "reason": "fallback insertion or overflow evidence blocks product-quality acceptance",
                    "generation_evidence": rel(generation_evidence),
                },
            }
        )
        anchor_violations = source_anchor_order_violations(evidence_data)
        gates.append(
            {
                "gate_id": "source_anchor_order",
                "status": "pass" if not anchor_violations else "fail",
                "blocking": True,
                "evidence": {
                    "violation_count": len(anchor_violations),
                    "sample": anchor_violations[:12],
                    "reason": "a target-language reflow region must not cross untranslated visible source separators inside the same block",
                    "generation_evidence": rel(generation_evidence),
                },
            }
        )
        gates.append(
            {
                "gate_id": "translation_authenticity",
                "status": "pass" if translation_provider not in FORBIDDEN_TRANSLATION_PROVIDERS else "fail",
                "blocking": True,
                "evidence": {
                    "translation_provider": translation_provider,
                    "reason": "product-quality success requires a real translation provider, not deterministic placeholders",
                    "generation_evidence": rel(generation_evidence),
                },
            }
        )
        gates.append(
            {
                "gate_id": "semantic_translation_preflight",
                "status": "pass" if semantic_translation_validation == "PASS" else "fail",
                "blocking": True,
                "evidence": {
                    "semantic_translation_validation": semantic_translation_validation,
                    "reason": "product-quality success requires validated semantic translations before generation",
                    "generation_evidence": rel(generation_evidence),
                },
            }
        )
        gates.append(
            {
                "gate_id": "semantic_coverage",
                "status": "pass" if semantic_coverage == "full_semantic_translation" else "fail",
                "blocking": True,
                "evidence": {
                    "semantic_coverage": semantic_coverage,
                    "translation_quality": translation_quality,
                    "reason": "placeholder translations are valid generation evidence but not product-quality success",
                    "generation_evidence": rel(generation_evidence),
                },
            }
        )
        gates.append(
            {
                "gate_id": "visual_similarity",
                "status": "pass" if visual_quality_adjudication == "PASS" else "fail",
                "blocking": True,
                "evidence": {
                    "visual_quality_adjudication": visual_quality_adjudication,
                    "visual_adjudication": None if visual_adjudication is None else rel(visual_adjudication),
                    "reason": "automated structural checks are not enough for product-quality acceptance; source-vs-output PNG adjudication must be recorded",
                    "generation_evidence": rel(generation_evidence),
                },
            }
        )
        font_hierarchy_status = visual_dimension_status(visual_data, "font_hierarchy_ratio")
        if font_hierarchy_status is not None:
            gates.append(
                {
                    "gate_id": "font_hierarchy_ratio",
                    "status": "fail" if font_hierarchy_status == "FAIL" else "pass",
                    "blocking": True,
                    "evidence": {
                        "visual_dimension_status": font_hierarchy_status,
                        "visual_adjudication": None if visual_adjudication is None else rel(visual_adjudication),
                        "reason": "translated PDF must preserve source-relative font hierarchy such as note/body/table/title proportions",
                    },
                }
            )
        for dimension in ["sidebar_orientation_group_consistency", "sidebar_glyph_orientation"]:
            dimension_status = visual_dimension_status(visual_data, dimension)
            if dimension_status is not None:
                gates.append(
                    {
                        "gate_id": dimension,
                        "status": "fail" if dimension_status == "FAIL" else "pass",
                        "blocking": True,
                        "evidence": {
                            "visual_dimension_status": dimension_status,
                            "visual_adjudication": None if visual_adjudication is None else rel(visual_adjudication),
                            "reason": "side navigation must preserve group writing mode and glyph orientation relative to the source",
                        },
                    }
                )
        if region_data is not None:
            for role_gate in region_data.get("role_gates", []):
                if not isinstance(role_gate, dict):
                    continue
                role_status = str(role_gate.get("status") or "fail").lower()
                gates.append(
                    {
                        "gate_id": str(role_gate.get("gate_id") or "region_visual_quality"),
                        "status": "fail" if role_status == "fail" else "pass",
                        "blocking": bool(role_gate.get("blocking", role_status == "fail")),
                        "evidence": {
                            "visual_region_metrics": rel(visual_region_metrics),
                            "role_gate_status": role_gate.get("status"),
                            "failure_count": role_gate.get("failure_count", 0),
                            "warning_count": role_gate.get("warning_count", 0),
                            "region_count": role_gate.get("region_count", 0),
                            "sample": role_gate.get("sample", [])[:8],
                            "reason": "region-level visual gates evaluate title/body/table/footnote/sidebar/image/background roles separately",
                        },
                    }
                )
    blocking_failures = [gate for gate in gates if gate.get("blocking") and gate.get("status") == "fail"]
    result = {
        "tool": "evaluate_pdf_quality",
        "source_pdf": rel(source),
        "output_pdf": rel(output),
        "generation_evidence": None if generation_evidence is None else rel(generation_evidence),
        "visual_adjudication": None if visual_adjudication is None else rel(visual_adjudication),
        "visual_region_metrics": None if visual_region_metrics is None else rel(visual_region_metrics),
        "product_quality_verdict": "PASS" if not blocking_failures else "FAIL",
        "gates": gates,
        "page_metrics": page_results,
        "blocking_failure_count": len(blocking_failures),
    }
    src.close()
    out.close()
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--generation-evidence", default=None)
    parser.add_argument("--visual-adjudication", default=None)
    parser.add_argument("--visual-region-metrics", default=None)
    args = parser.parse_args()
    result = evaluate(
        resolve_workspace_path(args.source),
        resolve_workspace_path(args.output),
        resolve_workspace_path(args.generation_evidence) if args.generation_evidence else None,
        resolve_workspace_path(args.visual_adjudication) if args.visual_adjudication else None,
        resolve_workspace_path(args.visual_region_metrics) if args.visual_region_metrics else None,
    )
    write_json(Path(args.out), result)
    print(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
