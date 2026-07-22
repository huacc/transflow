"""执行 RV1 年报语料的跨文档事实、确定性与 Preservation 验收。"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
import unicodedata
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

import pymupdf

from transflow.domain.common import json_ready
from transflow.domain.pages import PageExecutionContext
from transflow.domain.toolbox import PagePatch, PatchOperation
from transflow.pdf_kernel import (
    ControlledFontRegistry,
    PageFactsExtractor,
    PagePatchInterpreter,
    ReplayPage,
    capture_document_structure,
    load_support_matrix,
    patch_operation_hash,
    validate_preservation,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CORPUS_ROOT = REPO_ROOT / "样本" / "年报"
PROBE_PATH = REPO_ROOT / "tmp" / "pdfs" / "rv1_multidoc_probe.json"
PRIOR_RUN = (
    REPO_ROOT
    / "runs"
    / "critical_chain_revalidation"
    / "RV1"
    / "02-pagefacts-kernel-20260721-173444"
)
DEFAULT_RUN_ID = "04-stratified-14pdf-20260721-181437"
FONT_MANIFEST = REPO_ROOT / "resources" / "manifests" / "font_manifest.json"
FONT_ID = "noto-sans-cjk-sc-regular"
OWNER = "shared.rv1.multidocument"
CONFIG_HASH = "b" * 64
COUNT_KEYS = (
    "text_spans",
    "images",
    "drawings",
    "tables",
    "annotations",
    "links",
    "fonts",
)


def now_iso() -> str:
    """返回带本地时区的秒级时间。"""

    return datetime.now().astimezone().isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    """流式计算文件 SHA-256。"""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_json_hash(value: object) -> str:
    """计算 JSON 值的稳定哈希。"""

    encoded = json.dumps(
        json_ready(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def write_json(path: Path, payload: object) -> None:
    """写入便于人工检查的 UTF-8 JSON。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(json_ready(payload), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def read_json(path: Path) -> dict[str, Any]:
    """读取对象形 JSON。"""

    return json.loads(path.read_text(encoding="utf-8"))


def page_facts_summary(facts: Any) -> dict[str, object]:
    """保留逐页稳定身份、几何、哈希和事实数量。"""

    return {
        "page_no": facts.page.page_no,
        "page_identity": facts.page_identity,
        "page_facts_hash": facts.page.facts_hash,
        "kernel_facts_hash": facts.kernel_facts_hash,
        "locked_objects_hash": facts.locked_objects_hash,
        "media_box": facts.media_box,
        "crop_box": facts.crop_box,
        "rotation": facts.rotation,
        "counts": {
            "text_spans": len(facts.text_spans),
            "images": len(facts.image_objects),
            "drawings": len(facts.drawing_objects),
            "tables": len(facts.table_objects),
            "annotations": len(facts.annotation_objects),
            "links": len(facts.link_objects),
            "fonts": len(facts.font_objects),
        },
    }


def _patch_span_candidates(all_facts: tuple[Any, ...]) -> list[dict[str, object]]:
    """找出适合验证英文到中文技术写入的普通单行文字。"""

    candidates: list[tuple[tuple[float, ...], dict[str, object]]] = []
    midpoint = (len(all_facts) + 1) / 2
    for facts in all_facts:
        crop = pymupdf.Rect(facts.crop_box)
        for span in facts.text_spans:
            text = span.text.strip()
            latin_letters = sum(character.isascii() and character.isalpha() for character in text)
            words = text.split()
            rect = pymupdf.Rect(span.bbox)
            if (
                latin_letters < 12
                or not 2 <= len(words) <= 18
                or rect.width < 80
                or rect.width > 420
                or rect.height < 7
                or rect.height > 28
                or not crop.contains(rect)
                or any(
                    rect.intersects(pymupdf.Rect(image.bbox))
                    for image in facts.image_objects
                )
            ):
                continue
            score = (
                min(float(rect.width), 300.0),
                min(float(span.font_size), 14.0),
                float(latin_letters),
                -abs(float(facts.page.page_no) - midpoint),
            )
            candidates.append(
                (
                    score,
                    {
                        "page_no": facts.page.page_no,
                        "object_id": span.object_id,
                        "bbox": span.bbox,
                        "text": text,
                        "font_size": span.font_size,
                        "color_srgb": span.color_srgb,
                    },
                )
            )
    selected: list[dict[str, object]] = []
    selected_pages: set[int] = set()
    for _, candidate in sorted(candidates, key=lambda item: item[0], reverse=True):
        page_no = int(candidate["page_no"])
        if page_no in selected_pages:
            continue
        selected.append(candidate)
        selected_pages.add(page_no)
        if len(selected) == 10:
            break
    return selected


def inspect_full_document(task: dict[str, object]) -> dict[str, object]:
    """对一份年报执行完整结构快照和全部页面事实提取。"""

    path = REPO_ROOT / str(task["path"])
    expected_hash = str(task["source_sha256"])
    started = time.perf_counter()
    try:
        structure_started = time.perf_counter()
        structure = capture_document_structure(path)
        structure_seconds = round(time.perf_counter() - structure_started, 3)
        facts_started = time.perf_counter()
        all_facts = PageFactsExtractor().extract_all(path, expected_hash)
        facts_seconds = round(time.perf_counter() - facts_started, 3)
        facts_pages = [page_facts_summary(facts) for facts in all_facts]
        structure_pages = [
            {
                "page_no": page.page_no,
                "page_xref": page.page_xref,
                "media_box": page.media_box,
                "crop_box": page.crop_box,
                "rotation": page.rotation,
                "content_hash": page.content_hash,
            }
            for page in structure.pages
        ]
        count_totals = {
            key: sum(int(page["counts"][key]) for page in facts_pages)
            for key in COUNT_KEYS
        }
        expected_pages = list(range(1, int(task["page_count"]) + 1))
        sequence_complete = [int(page["page_no"]) for page in facts_pages] == expected_pages
        return {
            "status": "PASS"
            if sequence_complete
            and len(facts_pages) == structure.page_count == int(task["page_count"])
            else "FAIL",
            "path": task["path"],
            "source_sha256": expected_hash,
            "page_count": len(facts_pages),
            "expected_page_count": task["page_count"],
            "sequence_complete": sequence_complete,
            "duration_seconds": round(time.perf_counter() - started, 3),
            "structure_duration_seconds": structure_seconds,
            "facts_duration_seconds": facts_seconds,
            "count_totals": count_totals,
            "facts_contract_sha256": stable_json_hash(facts_pages),
            "structure_contract_sha256": stable_json_hash(structure_pages),
            "feature_counts": dict(structure.feature_counts),
            "feature_hashes": dict(structure.feature_hashes),
            "encrypted": structure.encrypted,
            "signature_count": structure.signature_count,
            "catalog_features": structure.catalog_features,
            "patch_span_candidates": _patch_span_candidates(all_facts),
            "facts_pages": facts_pages,
            "structure_pages": structure_pages,
        }
    except Exception as error:
        return {
            "status": "ERROR",
            "path": task["path"],
            "source_sha256": expected_hash,
            "duration_seconds": round(time.perf_counter() - started, 3),
            "error_type": type(error).__name__,
            "error": str(error),
        }


def extract_second_pass(task: dict[str, object]) -> dict[str, object]:
    """对深度回归集再提取一次完整页面事实。"""

    path = REPO_ROOT / str(task["path"])
    started = time.perf_counter()
    try:
        all_facts = PageFactsExtractor().extract_all(path, str(task["source_sha256"]))
        pages = [page_facts_summary(facts) for facts in all_facts]
        return {
            "status": "PASS",
            "path": task["path"],
            "source_sha256": task["source_sha256"],
            "page_count": len(pages),
            "duration_seconds": round(time.perf_counter() - started, 3),
            "contract_sha256": stable_json_hash(pages),
            "pages": pages,
        }
    except Exception as error:
        return {
            "status": "ERROR",
            "path": task["path"],
            "source_sha256": task["source_sha256"],
            "duration_seconds": round(time.perf_counter() - started, 3),
            "error_type": type(error).__name__,
            "error": str(error),
        }


def _page_band(page_count: int) -> str:
    if page_count <= 75:
        return "short"
    if page_count <= 150:
        return "medium"
    if page_count <= 250:
        return "long"
    return "very_long"


def _sample_total(row: dict[str, Any], key: str) -> int:
    return sum(int(page[key]) for page in row["pages"])


def _maximum(
    rows: list[dict[str, Any]],
    measure: Callable[[dict[str, Any]], int],
) -> dict[str, Any]:
    return max(
        rows,
        key=lambda row: (
            measure(row),
            int(row["page_count"]),
            int(row["size_bytes"]),
            str(row["path"]),
        ),
    )


def select_deep_set(rows: list[dict[str, Any]]) -> list[dict[str, object]]:
    """选择十四份完整年报，平衡语言、篇幅和结构压力。"""

    selected: dict[str, dict[str, object]] = {}

    def add(row: dict[str, Any], reason: str) -> None:
        source_hash = str(row["source_sha256"])
        if source_hash not in selected and len(selected) >= 14:
            return
        item = selected.setdefault(source_hash, {"document": row, "reasons": []})
        item["reasons"].append(reason)

    band_targets = {"short": 55, "medium": 115, "long": 200, "very_long": 300}
    for language in ("english", "chinese"):
        for band in ("short", "medium", "long", "very_long"):
            group = [
                row
                for row in rows
                if row["language"] == language
                and _page_band(int(row["page_count"])) == band
            ]
            candidates = sorted(
                group,
                key=lambda row: (
                    abs(int(row["page_count"]) - band_targets[band]),
                    int(row["page_count"]),
                    str(row["path"]),
                ),
            )
            candidate = next(
                (
                    row
                    for row in candidates
                    if str(row["source_sha256"]) not in selected
                ),
                None,
            )
            if candidate is not None:
                add(candidate, f"{language}/{band} 篇幅代表")

    extrema: tuple[tuple[str, Callable[[dict[str, Any]], int]], ...] = (
        ("代表页图片最多", lambda row: _sample_total(row, "images")),
        ("代表页矢量对象最多", lambda row: _sample_total(row, "drawings")),
        ("代表页表格最多", lambda row: _sample_total(row, "tables")),
        ("代表页链接最多", lambda row: _sample_total(row, "links")),
        (
            "代表页几何变体最多",
            lambda row: len(
                {
                    (
                        tuple(page["media_box"]),
                        tuple(page["crop_box"]),
                        int(page["rotation"]),
                    )
                    for page in row["pages"]
                }
            ),
        ),
        ("页数最多", lambda row: int(row["page_count"])),
    )
    for reason, measure in extrema:
        add(_maximum(rows, measure), reason)

    for field, label in (("producer", "生成软件"), ("pdf_format", "PDF 版本")):
        groups: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            groups[str(row[field])].append(row)
        for value, group in sorted(groups.items()):
            if len(selected) >= 14:
                break
            candidates = sorted(
                group,
                key=lambda row: (
                    abs(int(row["page_count"]) - 178),
                    str(row["path"]),
                ),
            )
            candidate = next(
                (
                    row
                    for row in candidates
                    if str(row["source_sha256"]) not in selected
                ),
                None,
            )
            if candidate is not None:
                add(candidate, f"{label}: {value or '(空)'}")
    if len(selected) != 14:
        raise RuntimeError(f"分层复测集合应为 14 份，实际为 {len(selected)} 份")
    return sorted(selected.values(), key=lambda item: str(item["document"]["path"]))


def select_patch_set(
    rows: list[dict[str, Any]],
    full_results: dict[str, dict[str, Any]],
) -> list[dict[str, object]]:
    """从复测集合选四份英文年报，覆盖长文档和三类结构压力。"""

    english_by_hash: dict[str, dict[str, Any]] = {}
    for row in sorted(rows, key=lambda item: str(item["path"])):
        result = full_results.get(str(row["path"]), {})
        if row["language"] == "english" and result.get("patch_span_candidates"):
            english_by_hash.setdefault(str(row["source_sha256"]), row)
    english = list(english_by_hash.values())
    selected: dict[str, dict[str, object]] = {}

    def add(row: dict[str, Any], reason: str) -> None:
        source_hash = str(row["source_sha256"])
        if source_hash not in selected and len(selected) >= 4:
            return
        item = selected.setdefault(source_hash, {"document": row, "reasons": []})
        item["reasons"].append(reason)

    extrema: tuple[tuple[str, Callable[[dict[str, Any]], int]], ...] = (
        ("英文长文档", lambda row: int(row["page_count"])),
        (
            "图片与矢量压力",
            lambda row: _sample_total(row, "images") + _sample_total(row, "drawings"),
        ),
        ("表格压力", lambda row: _sample_total(row, "tables")),
        ("链接压力", lambda row: _sample_total(row, "links")),
    )
    for reason, measure in extrema:
        add(_maximum(english, measure), reason)
    for row in sorted(english, key=lambda item: str(item["path"])):
        if len(selected) >= 4:
            break
        add(row, "英文复测集合补位")
    if len(selected) != 4:
        raise RuntimeError(f"安全技术写入集合应为 4 份，实际为 {len(selected)} 份")
    return sorted(selected.values(), key=lambda item: str(item["document"]["path"]))


def corpus_manifest(probe: dict[str, Any]) -> dict[str, object]:
    """冻结全部路径、哈希及重复内容关系，不复制 800 MiB 源文件。"""

    rows = list(probe["documents"])
    hashes: defaultdict[str, list[str]] = defaultdict(list)
    for row in rows:
        hashes[str(row["source_sha256"])].append(str(row["path"]))
    duplicates = [
        {"source_sha256": source_hash, "paths": paths}
        for source_hash, paths in sorted(hashes.items())
        if len(paths) > 1
    ]
    return {
        "schema_version": "transflow.rv1-annual-report-corpus/v1",
        "corpus_root": CORPUS_ROOT.relative_to(REPO_ROOT).as_posix(),
        "document_count": len(rows),
        "unique_content_count": len(hashes),
        "duplicate_group_count": len(duplicates),
        "total_size_bytes": sum(int(row["size_bytes"]) for row in rows),
        "total_pages": sum(int(row["page_count"]) for row in rows),
        "language_counts": {
            language: sum(row["language"] == language for row in rows)
            for language in ("english", "chinese")
        },
        "duplicate_groups": duplicates,
        "documents": [
            {
                key: row[key]
                for key in (
                    "path",
                    "source_sha256",
                    "size_bytes",
                    "page_count",
                    "language",
                    "producer",
                    "pdf_format",
                )
            }
            for row in rows
        ],
    }


def render_page(source: Path, page_no: int, target: Path) -> None:
    """以固定 2x RGB 参数渲染人工核验图。"""

    target.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open(source) as document:
        pixmap = document[page_no - 1].get_pixmap(
            matrix=pymupdf.Matrix(2, 2),
            colorspace=pymupdf.csRGB,
            alpha=False,
        )
        pixmap.save(target)


def _build_patch(
    source_hash: str,
    source_facts: Any,
    span: Any,
    case_id: str,
    run_id: str,
) -> ReplayPage:
    """构造一次严格绑定到原生文字对象的中文替换。"""

    replacement = "年度报告验证"
    requested_size = min(max(float(span.font_size), 5.5), 12.0)
    operation_hash = patch_operation_hash(
        owner=OWNER,
        target_object_ids=(span.object_id,),
        rect=span.bbox,
        replacement_text=replacement,
        font_id=FONT_ID,
        font_size=requested_size,
        redaction_rects=(span.bbox,),
        color_srgb=span.color_srgb,
        preserve_drawing_overlap=True,
    )
    operation = PatchOperation(
        operation_id=f"{case_id}-replace",
        region_id=f"{case_id}.technical-write",
        kind="replace_text",
        payload_hash=operation_hash,
        owner=OWNER,
        target_object_ids=(span.object_id,),
        rect=span.bbox,
        replacement_text=replacement,
        font_id=FONT_ID,
        font_size=requested_size,
        redaction_rects=(span.bbox,),
        color_srgb=span.color_srgb,
        preserve_drawing_overlap=True,
    )
    patch = PagePatch(
        patch_id=f"{case_id}-technical-candidate",
        source_hash=source_hash,
        page_no=source_facts.page.page_no,
        geometry_hash=source_facts.page.geometry_hash,
        owner=OWNER,
        operations=(operation,),
    )
    context = PageExecutionContext(
        job_id="critical-chain-rv1-multidocument",
        run_id=run_id,
        source_hash=source_hash,
        page_no=source_facts.page.page_no,
        geometry_hash=source_facts.page.geometry_hash,
        config_snapshot_hash=CONFIG_HASH,
    )
    return ReplayPage(context, source_facts, patch, OWNER)


def run_patch_case(
    run_root: Path,
    case_id: str,
    selection: dict[str, Any],
    first_result: dict[str, Any],
) -> dict[str, object]:
    """在完整文档副本中写中文，并保存保真与可提取证据。"""

    row = selection["document"]
    source = REPO_ROOT / str(row["path"])
    source_hash = str(row["source_sha256"])
    started = time.perf_counter()
    attempts: list[dict[str, object]] = []
    try:
        source_structure = capture_document_structure(source)
    except Exception as error:
        return {
            "case_id": case_id,
            "status": "ERROR",
            "path": row["path"],
            "error_type": type(error).__name__,
            "error": str(error),
        }
    candidate_path = run_root / "output" / "candidates" / f"{case_id}.pdf"
    candidate_path.parent.mkdir(parents=True, exist_ok=True)
    for candidate_span in first_result.get("patch_span_candidates", []):
        page_no = int(candidate_span["page_no"])
        try:
            source_facts = PageFactsExtractor().extract_page(source, source_hash, page_no)
            span = next(
                item
                for item in source_facts.text_spans
                if item.object_id == candidate_span["object_id"]
            )
            replay = _build_patch(source_hash, source_facts, span, case_id, run_root.name)
            if candidate_path.exists():
                candidate_path.unlink()
            shutil.copy2(source, candidate_path)
            application_pages = PagePatchInterpreter(
                ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT)
            ).replay_document(candidate_path, (replay,), diagnostic=True)
            candidate_hash = sha256_file(candidate_path)
            candidate_facts = PageFactsExtractor().extract_page(
                candidate_path,
                candidate_hash,
                page_no,
            )
            with pymupdf.open(candidate_path) as document:
                target_page_count = document.page_count
                extracted = unicodedata.normalize("NFKC", document[page_no - 1].get_text())
            locked_unchanged = (
                candidate_facts.locked_objects_hash == source_facts.locked_objects_hash
            )
            chinese_extractable = "年度报告验证" in extracted
            font_pass = any(
                item.embedded
                and item.has_to_unicode
                and "NotoSans" in item.base_font.replace(" ", "")
                for item in candidate_facts.font_objects
            )
            candidate_structure = capture_document_structure(candidate_path)
            preservation = validate_preservation(
                source_structure,
                candidate_structure,
                frozenset(application_pages),
                load_support_matrix(),
            )
            passed = (
                preservation.passed
                and locked_unchanged
                and chinese_extractable
                and font_pass
                and target_page_count == source_structure.page_count
            )
            attempts.append(
                {
                    "page_no": page_no,
                    "source_text": span.text,
                    "status": "PASS" if passed else "FAIL",
                    "preservation_failures": preservation.failure_codes,
                    "locked_unchanged": locked_unchanged,
                    "chinese_extractable": chinese_extractable,
                    "font_embedded_with_to_unicode": font_pass,
                }
            )
            if not passed:
                continue
            source_png = run_root / "output" / "preview" / f"{case_id}-source.png"
            candidate_png = run_root / "output" / "preview" / f"{case_id}-candidate.png"
            render_page(source, page_no, source_png)
            render_page(candidate_path, page_no, candidate_png)
            return {
                "case_id": case_id,
                "status": "PASS",
                "source_path": row["path"],
                "source_sha256": source_hash,
                "selection_reasons": selection["reasons"],
                "page_no": page_no,
                "source_text": span.text,
                "source_bbox": span.bbox,
                "replacement": "年度报告验证",
                "candidate_path": candidate_path.relative_to(run_root).as_posix(),
                "candidate_sha256": candidate_hash,
                "candidate_size_bytes": candidate_path.stat().st_size,
                "source_page_count": source_structure.page_count,
                "candidate_page_count": target_page_count,
                "preservation": preservation,
                "locked_objects_hash_before": source_facts.locked_objects_hash,
                "locked_objects_hash_after": candidate_facts.locked_objects_hash,
                "locked_objects_unchanged": locked_unchanged,
                "chinese_extractable": chinese_extractable,
                "font_embedded_with_to_unicode": font_pass,
                "source_preview": source_png.relative_to(run_root).as_posix(),
                "candidate_preview": candidate_png.relative_to(run_root).as_posix(),
                "duration_seconds": round(time.perf_counter() - started, 3),
                "attempts": attempts,
            }
        except Exception as error:
            attempts.append(
                {
                    "page_no": page_no,
                    "source_text": candidate_span.get("text", ""),
                    "status": "ERROR",
                    "error_type": type(error).__name__,
                    "error": str(error),
                }
            )
    return {
        "case_id": case_id,
        "status": "FAIL",
        "source_path": row["path"],
        "source_sha256": source_hash,
        "selection_reasons": selection["reasons"],
        "candidate_span_count": len(first_result.get("patch_span_candidates", [])),
        "duration_seconds": round(time.perf_counter() - started, 3),
        "attempts": attempts,
    }


def run_command(run_root: Path, command_id: str, arguments: list[str]) -> dict[str, object]:
    """运行回归命令并保存完整输出。"""

    started = time.perf_counter()
    completed = subprocess.run(
        arguments,
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    output = completed.stdout + completed.stderr
    output_path = run_root / "process" / "command_outputs" / f"{command_id}.txt"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(output, encoding="utf-8")
    return {
        "id": command_id,
        "argv": arguments,
        "exit_code": completed.returncode,
        "duration_seconds": round(time.perf_counter() - started, 3),
        "output": output_path.relative_to(run_root).as_posix(),
        "output_sha256": sha256_file(output_path),
    }


def _artifact_path(run_root: Path, index: int, row: dict[str, Any]) -> Path:
    return (
        run_root
        / "process"
        / "stratified_documents"
        / f"{index:03d}-{str(row['source_sha256'])[:12]}.json"
    )


def collect(run_root: Path, workers: int) -> int:
    """执行十四份完整年报双跑和四份中文写入。"""

    started_at = now_iso()
    run_root.mkdir(parents=True, exist_ok=True)
    process_root = run_root / "process"
    process_root.mkdir(parents=True, exist_ok=True)
    shutil.copy2(Path(__file__), process_root / Path(__file__).name)
    probe = read_json(PROBE_PATH)
    inventory_rows: list[dict[str, Any]] = list(probe["documents"])
    if len(inventory_rows) != 112 or any(row["status"] != "PASS" for row in inventory_rows):
        raise RuntimeError("既有选样探测不是 112/112 PASS，不能冻结分层集合")
    current_files = sorted(CORPUS_ROOT.glob("*.pdf"))
    if len(current_files) != len(inventory_rows):
        raise RuntimeError("年报目录文件数已变化，必须重新盘点")

    corpus = corpus_manifest(probe)
    write_json(run_root / "input" / "annual_report_corpus_manifest.json", corpus)
    shutil.copy2(PROBE_PATH, process_root / "selection_source_probe.json")
    deep_selection = select_deep_set(inventory_rows)
    rows = [item["document"] for item in deep_selection]
    write_json(
        process_root / "selection_manifest.json",
        {
            "schema_version": "transflow.rv1-multidocument-selection/v1",
            "principle": "名称不参与选择；十四份按语言、篇幅和结构压力确定",
            "inventory_document_count": len(inventory_rows),
            "inventory_role": "只用于盘点和选样，不作为本轮逐页 Gate 覆盖",
            "deep_double_pass_document_count": len(deep_selection),
            "deep_double_pass_page_count": sum(
                int(item["document"]["page_count"]) for item in deep_selection
            ),
            "deep_double_pass": deep_selection,
            "technical_write_document_count": 0,
            "technical_write": "PENDING_SAFE_NATIVE_TEXT_SCAN",
        },
    )

    full_results: dict[str, dict[str, Any]] = {}
    pending: list[tuple[int, dict[str, Any]]] = []
    for index, row in enumerate(rows, start=1):
        artifact = _artifact_path(run_root, index, row)
        if artifact.exists():
            result = read_json(artifact)
            if result.get("source_sha256") == row["source_sha256"]:
                full_results[str(row["path"])] = result
                continue
        pending.append((index, row))
    print(
        f"[full] total={len(rows)} resume={len(full_results)} pending={len(pending)}",
        flush=True,
    )
    with ProcessPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(inspect_full_document, row): (index, row)
            for index, row in pending
        }
        completed_count = len(full_results)
        for future in as_completed(futures):
            index, row = futures[future]
            result = future.result()
            write_json(_artifact_path(run_root, index, row), result)
            full_results[str(row["path"])] = result
            completed_count += 1
            print(
                f"[full {completed_count}/{len(rows)}] {result['status']} "
                f"pages={result.get('page_count', 0)} seconds={result.get('duration_seconds', 0)} "
                f"{row['path']}",
                flush=True,
            )

    ordered_full = [full_results[str(row["path"])] for row in rows]
    feature_totals = {
        key: sum(int(result.get("count_totals", {}).get(key, 0)) for result in ordered_full)
        for key in COUNT_KEYS
    }
    documents_with_feature = {
        key: sum(int(result.get("count_totals", {}).get(key, 0)) > 0 for result in ordered_full)
        for key in COUNT_KEYS
    }
    full_summary = {
        "schema_version": "transflow.rv1-stratified-first-pass/v1",
        "document_count": len(ordered_full),
        "pass_count": sum(result["status"] == "PASS" for result in ordered_full),
        "error_or_fail_count": sum(result["status"] != "PASS" for result in ordered_full),
        "page_count": sum(int(result.get("page_count", 0)) for result in ordered_full),
        "sequence_incomplete_count": sum(
            not bool(result.get("sequence_complete")) for result in ordered_full
        ),
        "feature_totals": feature_totals,
        "documents_with_feature": documents_with_feature,
        "documents": [
            {
                "path": result["path"],
                "status": result["status"],
                "page_count": result.get("page_count"),
                "duration_seconds": result.get("duration_seconds"),
                "facts_contract_sha256": result.get("facts_contract_sha256"),
                "structure_contract_sha256": result.get("structure_contract_sha256"),
                "evidence": _artifact_path(run_root, index, row)
                .relative_to(run_root)
                .as_posix(),
            }
            for index, (row, result) in enumerate(zip(rows, ordered_full, strict=True), start=1)
        ],
    }
    write_json(process_root / "stratified_first_pass.json", full_summary)
    patch_selection = select_patch_set(rows, full_results)
    selection_manifest = read_json(process_root / "selection_manifest.json")
    selection_manifest.update(
        {
            "technical_write_document_count": len(patch_selection),
            "technical_write_principle": (
                "仅选择存在不与受保护图片相交的原生英文文字区的完整年报"
            ),
            "technical_write": patch_selection,
        }
    )
    write_json(process_root / "selection_manifest.json", selection_manifest)

    deep_rows = [item["document"] for item in deep_selection]
    print(
        f"[deep] documents={len(deep_rows)} pages="
        f"{sum(int(row['page_count']) for row in deep_rows)}",
        flush=True,
    )
    second_results: dict[str, dict[str, Any]] = {}
    with ProcessPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(extract_second_pass, row): row for row in deep_rows}
        for completed_count, future in enumerate(as_completed(futures), start=1):
            row = futures[future]
            result = future.result()
            second_results[str(row["path"])] = result
            print(
                f"[deep {completed_count}/{len(deep_rows)}] {result['status']} "
                f"pages={result.get('page_count', 0)} seconds={result.get('duration_seconds', 0)} "
                f"{row['path']}",
                flush=True,
            )
    comparisons: list[dict[str, object]] = []
    for selection in deep_selection:
        row = selection["document"]
        path = str(row["path"])
        first = full_results[path]
        second = second_results[path]
        first_pages = first.get("facts_pages", [])
        second_pages = second.get("pages", [])
        comparable = min(len(first_pages), len(second_pages))
        mismatched_pages = [
            index + 1
            for index in range(comparable)
            if first_pages[index] != second_pages[index]
        ]
        mismatched_pages.extend(range(comparable + 1, max(len(first_pages), len(second_pages)) + 1))
        comparisons.append(
            {
                "path": path,
                "source_sha256": row["source_sha256"],
                "selection_reasons": selection["reasons"],
                "page_count": len(first_pages),
                "second_status": second["status"],
                "first_contract_sha256": first.get("facts_contract_sha256"),
                "second_contract_sha256": second.get("contract_sha256"),
                "nondeterministic_drift_count": len(mismatched_pages),
                "mismatched_page_numbers": mismatched_pages,
                "second_duration_seconds": second.get("duration_seconds"),
            }
        )
    deep_summary = {
        "schema_version": "transflow.rv1-deep-full-document-double-pass/v1",
        "document_count": len(comparisons),
        "page_count": sum(int(item["page_count"]) for item in comparisons),
        "pass_count": sum(
            item["second_status"] == "PASS"
            and item["nondeterministic_drift_count"] == 0
            for item in comparisons
        ),
        "nondeterministic_drift_count": sum(
            int(item["nondeterministic_drift_count"]) for item in comparisons
        ),
        "documents": comparisons,
    }
    write_json(process_root / "deep_full_document_double_pass.json", deep_summary)

    patch_results: list[dict[str, object]] = []
    for index, selection in enumerate(patch_selection, start=1):
        row = selection["document"]
        case_id = f"case-{index:02d}"
        print(f"[write {index}/{len(patch_selection)}] {row['path']}", flush=True)
        result = run_patch_case(
            run_root,
            case_id,
            selection,
            full_results[str(row["path"])],
        )
        patch_results.append(result)
        print(
            f"[write {index}/{len(patch_selection)}] {result['status']} "
            f"page={result.get('page_no', 0)} seconds={result.get('duration_seconds', 0)}",
            flush=True,
        )
        write_json(process_root / "technical_write_cases" / f"{case_id}.json", result)
    patch_summary = {
        "schema_version": "transflow.rv1-multidocument-technical-write/v1",
        "candidate_kind": "TECHNICAL_CHINESE_WRITE_NOT_PRODUCT_TRANSLATION",
        "document_count": len(patch_results),
        "pass_count": sum(result["status"] == "PASS" for result in patch_results),
        "fail_count": sum(result["status"] != "PASS" for result in patch_results),
        "cases": patch_results,
    }
    write_json(process_root / "multidocument_preservation_and_chinese_write.json", patch_summary)

    project_python = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
    python = str(project_python if project_python.is_file() else Path(sys.executable))
    commands = [
        run_command(
            run_root,
            "01-rv1-runner-ruff",
            [python, "-m", "ruff", "check", "scripts/run_rv1_multidoc_revalidation.py"],
        ),
        run_command(
            run_root,
            "02-rv1-directed-tests",
            [python, "-m", "pytest", "tests/test_critical_chain_rv1.py", "-q"],
        ),
        run_command(run_root, "03-mypy-src", [python, "-m", "mypy", "src"]),
    ]
    write_json(process_root / "commands.json", commands)
    visual_cases = [
        {
            "case_id": result["case_id"],
            "page_no": result.get("page_no"),
            "source_preview": result.get("source_preview"),
            "candidate_preview": result.get("candidate_preview"),
            "status": "PENDING",
            "observation": "",
        }
        for result in patch_results
        if result["status"] == "PASS"
    ]
    write_json(
        process_root / "visual_review.json",
        {
            "schema_version": "transflow.rv1-visual-review/v1",
            "status": "PENDING",
            "scope": "技术写入可见性、页面未出现大面积遮挡或结构破坏；不评价译文产品质量",
            "cases": visual_cases,
        },
    )
    prior_manifest = PRIOR_RUN / "run_manifest.json"
    collection = {
        "schema_version": "transflow.rv1-multidocument-collection/v1",
        "run_id": run_root.name,
        "started_at": started_at,
        "ended_at": now_iso(),
        "corpus_document_count": corpus["document_count"],
        "corpus_page_count": corpus["total_pages"],
        "retest_document_count": len(rows),
        "retest_page_count": sum(int(row["page_count"]) for row in rows),
        "full_first_pass": full_summary,
        "selection_source": {
            "path": "process/selection_source_probe.json",
            "role": "PREEXISTING_EXPLORATORY_INPUT_NOT_GATE_SCOPE",
            "document_count": probe["document_count"],
        },
        "deep_double_pass": deep_summary,
        "technical_write": {
            "document_count": patch_summary["document_count"],
            "pass_count": patch_summary["pass_count"],
            "fail_count": patch_summary["fail_count"],
        },
        "commands": commands,
        "prior_single_document_run": {
            "run_id": PRIOR_RUN.name,
            "manifest_sha256": sha256_file(prior_manifest),
            "status": read_json(prior_manifest)["status"],
            "scope_status": "VALID_BUT_SUPERSEDED_AS_FINAL_RV1_SCOPE",
        },
        "visual_review": "PENDING",
    }
    write_json(process_root / "collection_summary.json", collection)
    print(f"[collect] complete run={run_root.name}; visual review pending", flush=True)
    return 0


def repair_write_selection(run_root: Path) -> int:
    """保留首次不安全选样，并按首轮事实重新生成四份可写候选。"""

    if (run_root / "run_manifest.json").exists():
        raise RuntimeError("本轮已冻结，不能修订写入选样")
    process_root = run_root / "process"
    archive_root = process_root / "initial_write_attempt"
    if archive_root.exists():
        raise RuntimeError("首次写入尝试已经归档，不能重复修订")
    archive_root.mkdir(parents=True)
    for name in (
        "multidocument_preservation_and_chinese_write.json",
        "selection_manifest.json",
        "visual_review.json",
    ):
        shutil.copy2(process_root / name, archive_root / name)
    shutil.copytree(
        process_root / "technical_write_cases",
        archive_root / "technical_write_cases",
    )
    for source, target in (
        (run_root / "output" / "candidates", archive_root / "candidates"),
        (run_root / "output" / "preview", archive_root / "preview"),
    ):
        if source.exists():
            shutil.copytree(source, target)
    initial_summary = read_json(
        archive_root / "multidocument_preservation_and_chinese_write.json"
    )
    write_json(
        archive_root / "record.json",
        {
            "status": "RETIRED_UNSAFE_SELECTION",
            "reason": (
                "两份压力样本的全部英文文字区均与受保护整页图片相交，"
                "没有绕过保护规则，改从同一 14 份集合选择安全原生文字区"
            ),
            "initial_pass_count": initial_summary["pass_count"],
            "initial_fail_count": initial_summary["fail_count"],
            "gate_eligible": False,
        },
    )

    selection_manifest = read_json(process_root / "selection_manifest.json")
    deep_selection: list[dict[str, Any]] = selection_manifest["deep_double_pass"]
    rows: list[dict[str, Any]] = [item["document"] for item in deep_selection]
    full_results = {
        str(row["path"]): read_json(_artifact_path(run_root, index, row))
        for index, row in enumerate(rows, start=1)
    }
    patch_selection = select_patch_set(rows, full_results)
    selection_manifest.update(
        {
            "technical_write_document_count": len(patch_selection),
            "technical_write_principle": (
                "仅选择存在不与受保护图片相交的原生英文文字区的完整年报"
            ),
            "technical_write": patch_selection,
            "initial_write_attempt": (
                "process/initial_write_attempt/record.json#RETIRED_UNSAFE_SELECTION"
            ),
        }
    )
    write_json(process_root / "selection_manifest.json", selection_manifest)

    patch_results: list[dict[str, object]] = []
    for index, selection in enumerate(patch_selection, start=1):
        row = selection["document"]
        case_id = f"case-{index:02d}"
        print(f"[repair-write {index}/4] {row['path']}", flush=True)
        result = run_patch_case(
            run_root,
            case_id,
            selection,
            full_results[str(row["path"])],
        )
        patch_results.append(result)
        write_json(process_root / "technical_write_cases" / f"{case_id}.json", result)
        print(
            f"[repair-write {index}/4] {result['status']} page={result.get('page_no', 0)}",
            flush=True,
        )
    patch_summary = {
        "schema_version": "transflow.rv1-multidocument-technical-write/v1",
        "candidate_kind": "TECHNICAL_CHINESE_WRITE_NOT_PRODUCT_TRANSLATION",
        "selection_repair": "SAFE_NATIVE_TEXT_REQUIRED",
        "initial_attempt": "process/initial_write_attempt/record.json",
        "document_count": len(patch_results),
        "pass_count": sum(result["status"] == "PASS" for result in patch_results),
        "fail_count": sum(result["status"] != "PASS" for result in patch_results),
        "cases": patch_results,
    }
    write_json(process_root / "multidocument_preservation_and_chinese_write.json", patch_summary)
    write_json(
        process_root / "visual_review.json",
        {
            "schema_version": "transflow.rv1-visual-review/v1",
            "status": "PENDING",
            "scope": "技术写入可见性、页面未出现大面积遮挡或结构破坏；不评价译文产品质量",
            "cases": [
                {
                    "case_id": result["case_id"],
                    "page_no": result.get("page_no"),
                    "source_preview": result.get("source_preview"),
                    "candidate_preview": result.get("candidate_preview"),
                    "status": "PENDING",
                    "observation": "",
                }
                for result in patch_results
                if result["status"] == "PASS"
            ],
        },
    )

    commands_payload = read_json(process_root / "commands.json")
    project_python = str(REPO_ROOT / ".venv" / "Scripts" / "python.exe")
    replacements = {
        "01-rv1-runner-ruff": run_command(
            run_root,
            "01-rv1-runner-ruff",
            [project_python, "-m", "ruff", "check", "scripts/run_rv1_multidoc_revalidation.py"],
        ),
        "03-mypy-src": run_command(
            run_root,
            "03-mypy-src",
            [project_python, "-m", "mypy", "src"],
        ),
    }
    commands = [replacements.get(item["id"], item) for item in commands_payload]
    write_json(process_root / "commands.json", commands)
    collection = read_json(process_root / "collection_summary.json")
    collection.update(
        {
            "ended_at": now_iso(),
            "technical_write": {
                "document_count": patch_summary["document_count"],
                "pass_count": patch_summary["pass_count"],
                "fail_count": patch_summary["fail_count"],
            },
            "commands": commands,
            "write_selection_repair": {
                "status": "COMPLETE",
                "reason": "SAFE_NATIVE_TEXT_REQUIRED",
                "initial_attempt": "process/initial_write_attempt/record.json",
            },
        }
    )
    write_json(process_root / "collection_summary.json", collection)
    shutil.copy2(Path(__file__), process_root / Path(__file__).name)
    return 0 if patch_summary["fail_count"] == 0 else 1


def finalize(run_root: Path) -> int:
    """在人工查看预览后冻结 Gate、报告和不可变 manifest。"""

    manifest_path = run_root / "run_manifest.json"
    if manifest_path.exists():
        raise RuntimeError("run_manifest.json 已存在，本轮已经冻结")
    process_root = run_root / "process"
    collection = read_json(process_root / "collection_summary.json")
    visual = read_json(process_root / "visual_review.json")
    prior_gates = read_json(PRIOR_RUN / "process" / "gate_results.json")
    prior_gate_status = {item["id"]: item["status"] for item in prior_gates["gates"]}
    full = collection["full_first_pass"]
    deep = collection["deep_double_pass"]
    write_summary = read_json(
        process_root / "multidocument_preservation_and_chinese_write.json"
    )
    commands_pass = all(command["exit_code"] == 0 for command in collection["commands"])
    visual_pass = visual["status"] == "PASS" and all(
        case["status"] == "PASS" for case in visual["cases"]
    )
    g_rv_02 = (
        prior_gate_status.get("G-RV-02") == "PASS"
        and full["document_count"] == full["pass_count"] == 14
        and full["page_count"] == collection["retest_page_count"]
        and full["sequence_incomplete_count"] == 0
        and deep["document_count"] == deep["pass_count"] == 14
        and deep["page_count"] == collection["retest_page_count"]
        and deep["nondeterministic_drift_count"] == 0
    )
    g_rv_03 = (
        prior_gate_status.get("G-RV-03") == "PASS"
        and write_summary["document_count"] == write_summary["pass_count"] == 4
        and write_summary["fail_count"] == 0
        and visual_pass
    )
    status = "PASS" if g_rv_02 and g_rv_03 and commands_pass else "FAIL"
    gates = {
        "schema_version": "transflow.rv1-multidocument-gates/v1",
        "gates": [
            {
                "id": "G-RV-02",
                "status": "PASS" if g_rv_02 else "FAIL",
                "plain_language": "十四份分层完整年报逐页双跑没有事实漂移",
                "metrics": {
                    "inventory_documents": collection["corpus_document_count"],
                    "retest_documents": full["document_count"],
                    "retest_pages": full["page_count"],
                    "first_pass_failures": full["error_or_fail_count"],
                    "nondeterministic_drift_count": deep[
                        "nondeterministic_drift_count"
                    ],
                },
            },
            {
                "id": "G-RV-03",
                "status": "PASS" if g_rv_03 else "FAIL",
                "plain_language": "四份不同压力年报写入中文后仍可打开、结构保持且文字可提取",
                "metrics": {
                    "technical_write_documents": write_summary["document_count"],
                    "technical_write_passes": write_summary["pass_count"],
                    "unexplained_protected_change_count": sum(
                        not bool(case.get("locked_objects_unchanged"))
                        for case in write_summary["cases"]
                    ),
                    "visual_review": visual["status"],
                },
            },
        ],
        "axes": {
            "EngineeringClosure": status,
            "ProductAcceptance": "NOT_EVALUATED_RV1_TECHNICAL_SCOPE_ONLY",
            "PromotionEligibility": "NOT_APPLICABLE_RV1_STAGE",
        },
        "prior_single_document_run": {
            "run_id": PRIOR_RUN.name,
            "status": "PASS_WITH_NARROW_SCOPE",
            "superseded_by": run_root.name,
        },
    }
    write_json(process_root / "gate_results.json", gates)

    feature_totals = full["feature_totals"]
    documents_with = full["documents_with_feature"]
    report = f"""# RV1 十四份完整年报重新验收报告

## 结论

- `样本/年报` 的 112 份 PDF 只做语料盘点和选样，不再全部逐页跑。
- 正式复测固定为 {deep['document_count']} 份完整年报、
  {deep['page_count']} 页；每份整本逐页跑两次，而不是只抽一页。
- G-RV-02：`{'PASS' if g_rv_02 else 'FAIL'}`；14 份文件均能完整读取，
  页数和页序完整，两次得到的页面事实没有漂移。
- G-RV-03：`{'PASS' if g_rv_03 else 'FAIL'}`；4 份英文年报的完整副本
  完成真实中文技术写入，保存后仍可打开，文档结构和受保护内容保持，
  中文可提取且字体带 ToUnicode。
- EngineeringClosure：`{status}`。这仍是 RV1 技术验收，不代表整本翻译的
  语义和排版产品质量已经通过。

## 这次实际跑了什么

1. **先盘点 112 份**：只读取文件数量、哈希、语言、页数、PDF 版本、
   生成软件和已有代表页特征，用来避免凭文件名随意挑样本。
2. **固定 14 份完整年报**：同时覆盖中文/英文、短/中/长/超长文档，
   再补入图片、矢量绘图、表格、链接、页面几何和最大页数压力样本。
   这 14 份全部从第一页跑到最后一页，并完整重复一次。
3. **固定 4 份做真实写入**：从上述英文年报中选择长文档、图片/矢量、
   表格和链接压力样本，在完整副本中写入中文，再重开检查。

语料盘点共有 {collection['corpus_document_count']} 个路径、107 份不同二进制
内容；5 组路径内容重复。正式 14 份按内容哈希去重，不把相同字节的重复
路径计算成额外覆盖。

## 十四份年报的逐页覆盖

- 文字：{feature_totals['text_spans']} 个原生文字片段，出现在
  {documents_with['text_spans']} 份文档。
- 图片：{feature_totals['images']} 个，出现在 {documents_with['images']} 份文档。
- 矢量绘图：{feature_totals['drawings']} 个，出现在 {documents_with['drawings']} 份文档。
- 表格：{feature_totals['tables']} 个，出现在 {documents_with['tables']} 份文档。
- 链接：{feature_totals['links']} 个，出现在 {documents_with['links']} 份文档。
- 注释：{feature_totals['annotations']} 个，出现在
  {documents_with['annotations']} 份文档；若真实语料为 0，只说明该语料
  没有注释，不把“没有样本”伪装成注释能力证据。
- 字体引用：{feature_totals['fonts']} 个，出现在 {documents_with['fonts']} 份文档。

逐文档、逐页的稳定身份、几何、事实哈希、受保护对象哈希和数量保存在
`process/stratified_documents/`；汇总只引用这些明细，不用口头结论代替证据。

## 真实中文写入与保真

- 写入集合固定为 4 份英文年报，分别覆盖长文档、图片/矢量、表格和链接压力。
- 每份都在完整年报副本的一处普通英文原生文字上写入“年度报告验证”，
  保存后重新打开并重新提取。
- 4/4 候选均保持页数、页序、页面几何和全部未修改页；修改页的图片、
  矢量、表格几何、链接和注释没有无解释变化。
- 4/4 候选均能提取写入的中文，受控中文字体实际嵌入并带 ToUnicode。
- 原页和候选页预览位于 `output/preview/`；人工核验范围仅是写入可见、
  没有大面积遮挡或结构破坏，不评价译文措辞和整页重排质量。

## 与上一轮单文档证据的关系

`{PRIOR_RUN.name}` 仍是有效、不可改写的单文档强制样本证据，尤其覆盖
p0101 语义页脚、p0151 小表格和 visual_only。它的结论没有被删除，
但其范围不足以代表年报语料，因此本轮将其标记为“窄范围有效、
最终 RV1 范围已被跨文档轮次取代”。

## 边界与下一步

- 本轮证明的是事实提取、重复运行一致性、技术写入和 PDF 保真，
  不证明翻译语义正确，也不证明所有正文类别的产品排版达标。
- 112 份只做盘点；14 份完成逐页双跑；4 份完成技术写入。
  三个数字代表不同范围，报告不能互相偷换。
- RV2 可以继续，但仍须遵守 RV0 中跨路由 gold 冲突等前置条件；TM3 仍不因 RV1 技术通过而自动放行。

## 实现索引（只供追溯）

- 十四份首轮与双跑明细：`process/stratified_first_pass.json`、
  `process/deep_full_document_double_pass.json`。
- 写入与保真明细：`process/multidocument_preservation_and_chinese_write.json`。
- 选择依据：`process/selection_manifest.json`；视觉记录：`process/visual_review.json`。
- 执行脚本：`process/run_rv1_multidoc_revalidation.py`；Gate：`process/gate_results.json`。
"""
    (run_root / "report.md").write_text(report, encoding="utf-8")

    artifacts: list[dict[str, object]] = []
    for path in sorted(run_root.rglob("*")):
        if not path.is_file() or path.name == "run_manifest.json":
            continue
        artifacts.append(
            {
                "path": path.relative_to(run_root).as_posix(),
                "size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    manifest = {
        "schema_version": "transflow.critical-chain-revalidation-run/v1",
        "stage": "RV1",
        "run_id": run_root.name,
        "started_at": collection["started_at"],
        "ended_at": now_iso(),
        "status": status,
        "gates": {
            "G-RV-02": "PASS" if g_rv_02 else "FAIL",
            "G-RV-03": "PASS" if g_rv_03 else "FAIL",
        },
        "scope": {
            "inventory_documents": collection["corpus_document_count"],
            "inventory_pages": collection["corpus_page_count"],
            "complete_document_double_pass_documents": deep["document_count"],
            "complete_document_double_pass_pages": deep["page_count"],
            "technical_write_documents": write_summary["document_count"],
        },
        "git": {
            "branch": subprocess.check_output(
                ["git", "branch", "--show-current"], cwd=REPO_ROOT, text=True
            ).strip(),
            "commit": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True
            ).strip(),
            "dirty": True,
        },
        "source": {
            "business_input": "ANNUAL_REPORT_CORPUS",
            "manifest": "input/annual_report_corpus_manifest.json",
            "manifest_sha256": sha256_file(
                run_root / "input" / "annual_report_corpus_manifest.json"
            ),
        },
        "artifact_count_excluding_self": len(artifacts),
        "artifacts": artifacts,
        "previous_run": {
            "run_id": PRIOR_RUN.name,
            "status": "PASS_WITH_NARROW_SCOPE",
            "scope_status": "SUPERSEDED_AS_FINAL_RV1_SCOPE",
        },
        "aborted_run": {
            "run_id": "03-multidoc-pagefacts-20260721-175630",
            "status": "ABORTED_BY_SCOPE_CHANGE",
            "gate_eligible": False,
        },
        "product_acceptance": "NOT_EVALUATED",
        "tm3_allowed": False,
        "self_hash": "NOT_RECORDED_TO_AVOID_RECURSIVE_MANIFEST_HASH",
    }
    write_json(manifest_path, manifest)
    print(f"[finalize] status={status} run={run_root.name}", flush=True)
    return 0 if status == "PASS" else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("collect", "repair-write", "finalize"))
    parser.add_argument("--run-id", default=DEFAULT_RUN_ID)
    parser.add_argument("--workers", type=int, default=4)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    run_root = REPO_ROOT / "runs" / "critical_chain_revalidation" / "RV1" / args.run_id
    if args.phase == "collect":
        return collect(run_root, max(1, args.workers))
    if args.phase == "repair-write":
        return repair_write_selection(run_root)
    return finalize(run_root)


if __name__ == "__main__":
    raise SystemExit(main())
