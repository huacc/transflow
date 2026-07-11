from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import fitz


ROOT = Path(__file__).resolve().parents[1]
ANNUAL_ROOT = ROOT.parents[1] / "样本" / "年报"
SAMPLE_ROOT = ROOT / "样本2"
SOURCE_MANIFEST = ROOT / "manifests" / "sample2_source_manifest.jsonl"
SELECTION_MANIFEST = ROOT / "manifests" / "sample2_selection_manifest.json"
SELECTION_SUMMARY = ROOT / "manifests" / "sample2_selection_summary.md"


@dataclass(frozen=True)
class Candidate:
    path: Path
    page_count: int
    language: str


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def language_from_name(name: str) -> str | None:
    if "_中文_" in name:
        return "zh"
    if "_英文_" in name:
        return "en"
    return None


def candidates() -> list[Candidate]:
    rows: list[Candidate] = []
    for path in sorted(ANNUAL_ROOT.glob("*.pdf")):
        if "中英合刊" in path.name:
            continue
        language = language_from_name(path.name)
        if language is None:
            continue
        with fitz.open(path) as document:
            page_count = document.page_count
        if page_count >= 20:
            rows.append(Candidate(path, page_count, language))
    return rows


def image_area_ratio(page: fitz.Page) -> float:
    page_area = max(1.0, float(page.rect.width * page.rect.height))
    area = 0.0
    for item in page.get_image_info():
        x0, y0, x1, y1 = (float(value) for value in item["bbox"])
        area += max(0.0, x1 - x0) * max(0.0, y1 - y0)
    return min(1.0, area / page_area)


def page_features(page: fitz.Page) -> dict[str, Any]:
    text = page.get_text("text", sort=True)
    blocks = [block for block in page.get_text("blocks", sort=True) if len(block) < 7 or int(block[6]) == 0]
    compact_chars = [char for char in text if not char.isspace()]
    digit_count = sum(char.isdigit() for char in compact_chars)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    numeric_lines = sum(len(re.findall(r"[-+()\d,.%]+", line)) >= 2 for line in lines)
    page_refs = sum(bool(re.search(r"(?:\s|\.{2,})\d{1,4}\s*$", line)) for line in lines)
    has_contents_title = bool(re.search(r"(?im)^\s*(CONTENTS?|目錄|目录)\s*$", text))
    return {
        "char_count": len(compact_chars),
        "line_count": len(lines),
        "block_count": len(blocks),
        "digit_ratio": round(digit_count / max(1, len(compact_chars)), 6),
        "numeric_line_ratio": round(numeric_lines / max(1, len(lines)), 6),
        "image_area_ratio": round(image_area_ratio(page), 6),
        "contents_score": (10 if has_contents_title else 0) + page_refs,
    }


def add_pick(picks: dict[int, set[str]], page_index: int, tag: str) -> None:
    picks.setdefault(page_index, set()).add(tag)


def select_pages(features: list[dict[str, Any]], count: int, rng: random.Random) -> list[tuple[int, list[str]]]:
    page_count = len(features)
    picks: dict[int, set[str]] = {}
    add_pick(picks, 0, "position:first")
    add_pick(picks, page_count - 1, "position:last")

    early = range(1, min(page_count - 1, 15))
    if early:
        toc_page = max(early, key=lambda index: (features[index]["contents_score"], rng.random()))
        add_pick(picks, toc_page, "structure:contents_candidate")

    ranked_dense = sorted(range(page_count), key=lambda index: (features[index]["char_count"], rng.random()), reverse=True)
    ranked_table = sorted(
        range(page_count),
        key=lambda index: (
            features[index]["numeric_line_ratio"] * 2 + features[index]["digit_ratio"],
            features[index]["line_count"],
            rng.random(),
        ),
        reverse=True,
    )
    ranked_image = sorted(range(page_count), key=lambda index: (features[index]["image_area_ratio"], rng.random()), reverse=True)
    ranked_low_text = sorted(range(page_count), key=lambda index: (features[index]["char_count"], rng.random()))
    for index in ranked_dense[:2]:
        add_pick(picks, index, "structure:dense_text")
    for index in ranked_table[:2]:
        add_pick(picks, index, "structure:table_like")
    for index in ranked_image[:2]:
        add_pick(picks, index, "structure:image_heavy")
    for index in ranked_low_text[:1]:
        add_pick(picks, index, "structure:low_text")

    for stratum in range(5):
        start = page_count * stratum // 5
        end = page_count * (stratum + 1) // 5
        pool = list(range(start, max(start + 1, end)))
        add_pick(picks, rng.choice(pool), f"position:stratum_{stratum + 1}")

    remaining = [index for index in range(page_count) if index not in picks]
    rng.shuffle(remaining)
    for index in remaining:
        if len(picks) >= count:
            break
        add_pick(picks, index, "random:fill")
    if len(picks) != count:
        raise RuntimeError(f"page_selection_count_mismatch:{len(picks)}:{count}")
    return [(index, sorted(tags)) for index, tags in sorted(picks.items())]


def write_page(document: fitz.Document, page_index: int, target: Path) -> None:
    output = fitz.open()
    output.insert_pdf(document, from_page=page_index, to_page=page_index, links=True, annots=False, widgets=False)
    output.save(target, garbage=4, deflate=True)
    output.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-count", type=int, default=50)
    parser.add_argument("--pages-per-report", type=int, default=20)
    parser.add_argument("--seed", type=int, default=20260711)
    args = parser.parse_args()
    if args.report_count % 2:
        raise ValueError("report_count_must_be_even_for_language_balance")

    rows = candidates()
    by_language = {language: [row for row in rows if row.language == language] for language in ("zh", "en")}
    per_language = args.report_count // 2
    if any(len(values) < per_language for values in by_language.values()):
        raise RuntimeError("insufficient_language_candidates")

    rng = random.Random(args.seed)
    selected = rng.sample(by_language["zh"], per_language) + rng.sample(by_language["en"], per_language)
    rng.shuffle(selected)

    SAMPLE_ROOT.mkdir(parents=True, exist_ok=True)
    for path in SAMPLE_ROOT.glob("*.pdf"):
        path.unlink()

    source_rows: list[dict[str, Any]] = []
    report_rows: list[dict[str, Any]] = []
    sample_index = 0
    for report_index, candidate in enumerate(selected, start=1):
        report_rng = random.Random(f"{args.seed}:{candidate.path.name}")
        with fitz.open(candidate.path) as document:
            features = [page_features(document.load_page(index)) for index in range(document.page_count)]
            selections = select_pages(features, args.pages_per_report, report_rng)
            report_id = f"R{report_index:03d}"
            report_pages = []
            for page_index, tags in selections:
                sample_index += 1
                sample_id = f"S2P{sample_index:04d}"
                target = SAMPLE_ROOT / f"{sample_id}.pdf"
                write_page(document, page_index, target)
                feature = features[page_index]
                source_rows.append(
                    {
                        "sample_id": sample_id,
                        "report_id": report_id,
                        "source_kind": "annual_report_stratified_random_page",
                        "source_path": str(candidate.path),
                        "source_page_number": page_index + 1,
                        "source_page_count": candidate.page_count,
                        "source_sha256": sha256_file(candidate.path),
                        "sample_sha256": sha256_file(target),
                        "selection_tags": tags,
                        "selection_features": feature,
                    }
                )
                report_pages.append({"sample_id": sample_id, "page_number": page_index + 1, "tags": tags, "features": feature})
            report_rows.append(
                {
                    "report_id": report_id,
                    "source_file": candidate.path.name,
                    "source_path": str(candidate.path),
                    "source_page_count": candidate.page_count,
                    "language": candidate.language,
                    "selected_pages": report_pages,
                }
            )
        print(json.dumps({"report": report_index, "report_id": report_id, "language": candidate.language, "pages": len(selections)}, ensure_ascii=False), flush=True)

    expected = args.report_count * args.pages_per_report
    pdfs = sorted(SAMPLE_ROOT.glob("*.pdf"))
    if len(source_rows) != expected or len(pdfs) != expected:
        raise RuntimeError("sample2_count_mismatch")
    for path in pdfs:
        with fitz.open(path) as document:
            if document.page_count != 1:
                raise RuntimeError(f"sample2_not_single_page:{path.name}")

    SOURCE_MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    SOURCE_MANIFEST.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in source_rows),
        encoding="utf-8",
    )
    selection = {
        "seed": args.seed,
        "report_count": args.report_count,
        "pages_per_report": args.pages_per_report,
        "sample_count": expected,
        "excluded_filename_token": "中英合刊",
        "eligible_report_count": len(rows),
        "eligible_by_language": {key: len(value) for key, value in by_language.items()},
        "selected_by_language": {"zh": per_language, "en": per_language},
        "selection_method": "language-balanced report random sampling plus per-report position/content stratified random pages",
        "reports": report_rows,
    }
    SELECTION_MANIFEST.write_text(json.dumps(selection, ensure_ascii=False, indent=2), encoding="utf-8")
    summary_lines = [
        "# 样本2抽样清单",
        "",
        f"- 随机种子：`{args.seed}`",
        f"- 年报：{args.report_count} 份（中文 {per_language}，英文 {per_language}）",
        f"- 每份抽取：{args.pages_per_report} 页",
        f"- 单页样本：{expected} 个",
        "- 排除条件：源文件名包含 `中英合刊`",
        "",
        "| 报告ID | 语种 | 总页数 | 抽取页码 | 源文件 |",
        "|---|---|---:|---|---|",
    ]
    for row in report_rows:
        pages = ", ".join(str(item["page_number"]) for item in row["selected_pages"])
        summary_lines.append(f"| {row['report_id']} | {row['language']} | {row['source_page_count']} | {pages} | {row['source_file']} |")
    SELECTION_SUMMARY.write_text("\n".join(summary_lines) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "SAMPLE2_READY": True,
                "seed": args.seed,
                "eligible_report_count": len(rows),
                "selected_report_count": len(report_rows),
                "sample_count": len(pdfs),
                "source_manifest": str(SOURCE_MANIFEST),
                "selection_manifest": str(SELECTION_MANIFEST),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
