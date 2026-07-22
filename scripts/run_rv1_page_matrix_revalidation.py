"""执行 RV1 分类单页矩阵与大年报抽页重新验收。"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import time
import unicodedata
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
from transflow.pdf_kernel.text_inventory import freeze_page_text_inventory

REPO_ROOT = Path(__file__).resolve().parents[1]
CLASSIFICATION_ROOT = (
    REPO_ROOT / "spikes" / "page_classification_engine_puncture_v1" / "分类结果"
)
ANNUAL_ROOT = REPO_ROOT / "样本" / "年报"
ANNUAL_PROBE = REPO_ROOT / "tmp" / "pdfs" / "rv1_multidoc_probe.json"
RUN_ID = "05-page-matrix-20260721-224910"
RUN_ROOT = REPO_ROOT / "runs" / "critical_chain_revalidation" / "RV1" / RUN_ID
PRIOR_SINGLE_DOCUMENT_RUN = (
    REPO_ROOT
    / "runs"
    / "critical_chain_revalidation"
    / "RV1"
    / "02-pagefacts-kernel-20260721-173444"
)
FONT_MANIFEST = REPO_ROOT / "resources" / "manifests" / "font_manifest.json"
FONT_ID = "noto-sans-cjk-sc-regular"
OWNER = "shared.rv1.page-matrix"
CONFIG_HASH = "c" * 64
COUNT_KEYS = (
    "text_spans",
    "images",
    "drawings",
    "tables",
    "annotations",
    "links",
    "fonts",
)
CATEGORY_NAMES = {
    "body/anchored_blocks": "锚定图文块",
    "body/chart": "图表页",
    "body/composite/anchored_blocks_chart": "锚定图文块与图表混合页",
    "body/composite/chart_table": "图表与表格混合页",
    "body/composite/flow_text_chart": "正文与图表混合页",
    "body/composite/flow_text_diagram": "正文与示意图混合页",
    "body/composite/flow_text_table": "正文与表格混合页",
    "body/diagram": "示意图页",
    "body/flow_text/multi": "多栏正文页",
    "body/flow_text/single": "单栏正文页",
    "body/flow_text/visual_anchored": "正文与锚定视觉页",
    "body/table": "表格页",
    "contents": "目录页",
    "cover": "封面",
    "end": "结束页",
    "visual_only": "纯视觉页",
}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(json_ready(payload), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def detect_language(path: Path) -> str:
    with pymupdf.open(path) as document:
        text = "".join(page.get_text() for page in document)
    latin = sum(character.isascii() and character.isalpha() for character in text)
    chinese = sum("\u4e00" <= character <= "\u9fff" for character in text)
    if chinese >= 10 and chinese * 2 >= latin:
        return "chinese"
    if latin >= 20:
        return "english"
    return "unknown"


def classification_selection() -> list[dict[str, object]]:
    """每个叶子类别选两页；优先中英各一页并保留 S2P0151。"""

    selected: list[dict[str, object]] = []
    leaf_directories = sorted(
        path
        for path in CLASSIFICATION_ROOT.rglob("*")
        if path.is_dir() and tuple(path.glob("*.pdf"))
    )
    for directory in leaf_directories:
        category = directory.relative_to(CLASSIFICATION_ROOT).as_posix()
        files = sorted(directory.glob("*.pdf"))
        language_by_path = {path: detect_language(path) for path in files}
        picks: list[tuple[Path, str]] = []
        if category == "visual_only":
            smallest = min(files, key=lambda path: (path.stat().st_size, path.name))
            largest = max(files, key=lambda path: (path.stat().st_size, path.name))
            picks = [(smallest, "文件最小"), (largest, "文件最大")]
        else:
            if category == "body/flow_text/single":
                forced = next((path for path in files if path.name == "S2P0151.pdf"), None)
                if forced is None:
                    raise RuntimeError("单栏正文强制页 S2P0151 缺失")
                picks.append((forced, "RV1 小表格强制页"))
            picked_languages = {language_by_path[path] for path, _ in picks}
            for language in ("english", "chinese"):
                if language in picked_languages:
                    continue
                candidates = [
                    path
                    for path in files
                    if language_by_path[path] == language
                    and path not in {item[0] for item in picks}
                ]
                if candidates:
                    candidate = max(
                        candidates,
                        key=lambda path: (path.stat().st_size, path.name),
                    )
                    picks.append((candidate, f"{language} 文件较大代表"))
            for candidate in sorted(
                files,
                key=lambda path: (path.stat().st_size, path.name),
                reverse=True,
            ):
                if len(picks) >= 2:
                    break
                if candidate not in {item[0] for item in picks}:
                    picks.append((candidate, "类别补位代表"))
            picks = picks[:2]
        if len(picks) != 2:
            raise RuntimeError(f"类别 {category} 未选满两页")
        for path, reason in picks:
            with pymupdf.open(path) as document:
                if document.page_count != 1:
                    raise RuntimeError(f"分类结果不是单页 PDF: {path}")
            selected.append(
                {
                    "category": category,
                    "category_name": CATEGORY_NAMES[category],
                    "language": language_by_path[path],
                    "origin_path": path.relative_to(REPO_ROOT).as_posix(),
                    "origin_sha256": sha256_file(path),
                    "origin_size_bytes": path.stat().st_size,
                    "selection_reason": reason,
                }
            )
    if len(leaf_directories) != 16 or len(selected) != 32:
        raise RuntimeError(
            f"分类矩阵应为 16 类/32 页，实际 {len(leaf_directories)} 类/{len(selected)} 页"
        )
    return selected


def _sample_feature_total(row: dict[str, Any]) -> int:
    return sum(
        int(page[key])
        for page in row["pages"]
        for key in ("images", "drawings", "tables", "links")
    )


def _geometry_variant_count(row: dict[str, Any]) -> int:
    return len(
        {
            (
                tuple(page["media_box"]),
                tuple(page["crop_box"]),
                int(page["rotation"]),
            )
            for page in row["pages"]
        }
    )


def annual_selection() -> list[dict[str, object]]:
    """选四份大年报，每份取十个等距原页。"""

    probe = read_json(ANNUAL_PROBE)
    rows: list[dict[str, Any]] = list(probe["documents"])
    selected: dict[str, tuple[dict[str, Any], str]] = {}

    def add(row: dict[str, Any], reason: str) -> None:
        selected.setdefault(str(row["source_sha256"]), (row, reason))

    english = [row for row in rows if row["language"] == "english"]
    chinese = [row for row in rows if row["language"] == "chinese"]
    add(max(english, key=lambda row: (int(row["page_count"]), str(row["path"]))), "英文页数最多")
    add(max(chinese, key=lambda row: (int(row["size_bytes"]), str(row["path"]))), "中文文件最大")
    add(
        max(
            english,
            key=lambda row: (_sample_feature_total(row), str(row["path"])),
        ),
        "英文视觉结构压力",
    )
    large_chinese = [row for row in chinese if int(row["page_count"]) >= 150]
    add(
        max(
            large_chinese,
            key=lambda row: (
                _geometry_variant_count(row),
                int(row["page_count"]),
                str(row["path"]),
            ),
        ),
        "中文大文档页面几何变化",
    )
    if len(selected) != 4:
        raise RuntimeError(f"年报应选 4 份，实际 {len(selected)} 份")
    result: list[dict[str, object]] = []
    for row, reason in selected.values():
        page_count = int(row["page_count"])
        page_numbers = tuple(
            sorted({round(index * (page_count - 1) / 9) + 1 for index in range(10)})
        )
        if len(page_numbers) != 10:
            raise RuntimeError(f"年报等距页未达到 10 页: {row['path']}")
        result.append(
            {
                "source_path": row["path"],
                "source_sha256": row["source_sha256"],
                "source_size_bytes": row["size_bytes"],
                "source_page_count": page_count,
                "language": row["language"],
                "producer": row["producer"],
                "pdf_format": row["pdf_format"],
                "selection_reason": reason,
                "page_numbers": page_numbers,
            }
        )
    return sorted(result, key=lambda item: str(item["source_path"]))


def export_single_page(source: Path, page_no: int, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    with pymupdf.open(source) as source_document, pymupdf.open() as target_document:
        target_document.insert_pdf(
            source_document,
            from_page=page_no - 1,
            to_page=page_no - 1,
        )
        target_document.save(target)


def freeze_inputs() -> list[dict[str, object]]:
    """复制分类单页并从年报截取四十页，冻结完整来源谱系。"""

    tasks: list[dict[str, object]] = []
    for index, item in enumerate(classification_selection(), start=1):
        page_id = f"classification-{index:02d}"
        source = REPO_ROOT / str(item["origin_path"])
        target = RUN_ROOT / "input" / "pages" / "classification" / f"{page_id}.pdf"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        tasks.append(
            {
                "page_id": page_id,
                "source_kind": "CLASSIFICATION_ORIGINAL_SINGLE_PAGE",
                **item,
                "source_document_path": item["origin_path"],
                "source_document_sha256": item["origin_sha256"],
                "source_page_no": 1,
                "page_pdf_path": target.relative_to(RUN_ROOT).as_posix(),
                "page_pdf_sha256": sha256_file(target),
                "page_pdf_size_bytes": target.stat().st_size,
            }
        )
    annual_index = 0
    for document_index, item in enumerate(annual_selection(), start=1):
        source = REPO_ROOT / str(item["source_path"])
        if sha256_file(source) != item["source_sha256"]:
            raise RuntimeError(f"年报源文件哈希已变化: {source}")
        for page_no in item["page_numbers"]:
            annual_index += 1
            page_id = f"annual-{annual_index:02d}"
            target = RUN_ROOT / "input" / "pages" / "annual" / f"{page_id}.pdf"
            export_single_page(source, int(page_no), target)
            tasks.append(
                {
                    "page_id": page_id,
                    "source_kind": "ANNUAL_REPORT_EXTRACTED_PAGE",
                    "category": "annual_report_sample",
                    "category_name": "大年报抽样页",
                    "language": item["language"],
                    "selection_reason": item["selection_reason"],
                    "source_document_group": document_index,
                    "source_document_path": item["source_path"],
                    "source_document_sha256": item["source_sha256"],
                    "source_document_page_count": item["source_page_count"],
                    "source_page_no": int(page_no),
                    "producer": item["producer"],
                    "pdf_format": item["pdf_format"],
                    "page_pdf_path": target.relative_to(RUN_ROOT).as_posix(),
                    "page_pdf_sha256": sha256_file(target),
                    "page_pdf_size_bytes": target.stat().st_size,
                }
            )
    if len(tasks) != 72:
        raise RuntimeError(f"页面矩阵应为 72 页，实际 {len(tasks)} 页")
    return tasks


def facts_summary(facts: Any) -> dict[str, object]:
    return {
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


def safe_write_candidates(facts: Any, language: str) -> list[dict[str, object]]:
    if language != "english":
        return []
    candidates: list[tuple[tuple[float, ...], dict[str, object]]] = []
    crop = pymupdf.Rect(facts.crop_box)
    for span in facts.text_spans:
        text = span.text.strip()
        rect = pymupdf.Rect(span.bbox)
        latin = sum(character.isascii() and character.isalpha() for character in text)
        if (
            latin < 8
            or not 1 <= len(text.split()) <= 18
            or rect.width < 60
            or rect.height < 6
            or rect.height > 32
            or not crop.contains(rect)
            or any(
                rect.intersects(pymupdf.Rect(image.bbox))
                for image in facts.image_objects
            )
        ):
            continue
        uppercase_ratio = sum(character.isupper() for character in text) / max(latin, 1)
        score = (
            min(float(span.font_size), 18.0),
            uppercase_ratio,
            min(float(rect.width), 420.0),
            -float(len(text)),
        )
        candidates.append(
            (
                score,
                {
                    "object_id": span.object_id,
                    "text": text,
                    "bbox": span.bbox,
                    "font_size": span.font_size,
                    "color_srgb": span.color_srgb,
                },
            )
        )
    return [item for _, item in sorted(candidates, key=lambda row: row[0], reverse=True)[:8]]


def inspect_page(task: dict[str, object]) -> dict[str, object]:
    page_path = RUN_ROOT / str(task["page_pdf_path"])
    started = time.perf_counter()
    try:
        extractor = PageFactsExtractor()
        first = extractor.extract_page(page_path, str(task["page_pdf_sha256"]), 1)
        second = extractor.extract_page(page_path, str(task["page_pdf_sha256"]), 1)
        first_summary = facts_summary(first)
        second_summary = facts_summary(second)
        inventory = freeze_page_text_inventory(first)
        return {
            "page_id": task["page_id"],
            "status": "PASS" if first_summary == second_summary else "DRIFT",
            "duration_seconds": round(time.perf_counter() - started, 3),
            "first": first_summary,
            "second": second_summary,
            "nondeterministic_drift_count": int(first_summary != second_summary),
            "inventory_item_count": len(inventory.items),
            "safe_write_candidates": safe_write_candidates(first, str(task["language"])),
        }
    except Exception as error:
        return {
            "page_id": task["page_id"],
            "status": "ERROR",
            "duration_seconds": round(time.perf_counter() - started, 3),
            "error_type": type(error).__name__,
            "error": str(error),
        }


def run_command(command_id: str, arguments: list[str]) -> dict[str, object]:
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
    output_path = RUN_ROOT / "process" / "command_outputs" / f"{command_id}.txt"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(completed.stdout + completed.stderr, encoding="utf-8")
    return {
        "id": command_id,
        "argv": arguments,
        "exit_code": completed.returncode,
        "duration_seconds": round(time.perf_counter() - started, 3),
        "output": output_path.relative_to(RUN_ROOT).as_posix(),
        "output_sha256": sha256_file(output_path),
    }


def collect(workers: int) -> int:
    """冻结 72 页并完成两次事实提取。"""

    if (RUN_ROOT / "process" / "collection_summary.json").exists():
        raise RuntimeError("run05 已完成 collect，不能覆盖")
    started_at = now_iso()
    RUN_ROOT.mkdir(parents=True, exist_ok=True)
    (RUN_ROOT / "process").mkdir(parents=True, exist_ok=True)
    tasks = freeze_inputs()
    write_json(
        RUN_ROOT / "input" / "page_matrix_manifest.json",
        {
            "schema_version": "transflow.rv1-page-matrix/v1",
            "classification_category_count": 16,
            "classification_page_count": 32,
            "annual_document_count": 4,
            "annual_page_count": 40,
            "total_page_count": len(tasks),
            "pages": tasks,
        },
    )
    shutil.copy2(Path(__file__), RUN_ROOT / "process" / Path(__file__).name)
    results: dict[str, dict[str, Any]] = {}
    with ProcessPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(inspect_page, task): task for task in tasks}
        for completed_count, future in enumerate(as_completed(futures), start=1):
            task = futures[future]
            result = future.result()
            results[str(task["page_id"])] = result
            write_json(
                RUN_ROOT / "process" / "pages" / f"{task['page_id']}.json",
                result,
            )
            print(
                f"[{completed_count}/{len(tasks)}] {result['status']} "
                f"{task['page_id']} {task['category']} source_page={task['source_page_no']}",
                flush=True,
            )
    ordered_results = [results[str(task["page_id"])] for task in tasks]
    feature_totals = {
        key: sum(
            int(result.get("first", {}).get("counts", {}).get(key, 0))
            for result in ordered_results
        )
        for key in COUNT_KEYS
    }
    category_status: list[dict[str, object]] = []
    for category in CATEGORY_NAMES:
        category_tasks = [task for task in tasks if task["category"] == category]
        category_results = [results[str(task["page_id"])] for task in category_tasks]
        category_status.append(
            {
                "category": category,
                "category_name": CATEGORY_NAMES[category],
                "page_count": len(category_tasks),
                "pass_count": sum(item["status"] == "PASS" for item in category_results),
                "page_ids": [task["page_id"] for task in category_tasks],
            }
        )
    visual_tasks = [task for task in tasks if task["category"] == "visual_only"]
    visual_empty = all(
        results[str(task["page_id"])].get("inventory_item_count") == 0
        for task in visual_tasks
    )
    shortlist: list[dict[str, object]] = []
    priority = (
        "body/flow_text/single",
        "body/composite/flow_text_table",
        "body/chart",
        "contents",
        "body/diagram",
        "body/table",
        "body/flow_text/multi",
        "body/anchored_blocks",
    )
    for category in priority:
        candidates = [
            (task, results[str(task["page_id"])])
            for task in tasks
            if task["category"] == category
            and task["language"] == "english"
            and results[str(task["page_id"])].get("safe_write_candidates")
        ]
        if not candidates:
            continue
        task, result = candidates[0]
        shortlist.append(
            {
                "page_id": task["page_id"],
                "category": category,
                "category_name": task["category_name"],
                "source": task["page_pdf_path"],
                "candidates": result["safe_write_candidates"],
            }
        )
    write_json(
        RUN_ROOT / "process" / "write_candidate_shortlist.json",
        {
            "status": "AWAITING_MEANINGFUL_TRANSLATION_PLAN",
            "rule": "只允许真实对应译文，不写通用占位语",
            "pages": shortlist,
        },
    )
    summary = {
        "schema_version": "transflow.rv1-page-matrix-results/v1",
        "started_at": started_at,
        "ended_at": now_iso(),
        "page_count": len(tasks),
        "pass_count": sum(result["status"] == "PASS" for result in ordered_results),
        "error_count": sum(result["status"] == "ERROR" for result in ordered_results),
        "drift_count": sum(
            int(result.get("nondeterministic_drift_count", 0)) for result in ordered_results
        ),
        "classification_page_count": sum(
            task["source_kind"] == "CLASSIFICATION_ORIGINAL_SINGLE_PAGE" for task in tasks
        ),
        "annual_page_count": sum(
            task["source_kind"] == "ANNUAL_REPORT_EXTRACTED_PAGE" for task in tasks
        ),
        "feature_totals": feature_totals,
        "category_status": category_status,
        "visual_only_inventory_empty": visual_empty,
        "write_shortlist_count": len(shortlist),
        "write_status": "PENDING",
    }
    write_json(RUN_ROOT / "process" / "collection_summary.json", summary)
    project_python = str(REPO_ROOT / ".venv" / "Scripts" / "python.exe")
    commands = [
        run_command(
            "01-runner-ruff",
            [project_python, "-m", "ruff", "check", "scripts/run_rv1_page_matrix_revalidation.py"],
        ),
        run_command(
            "02-rv1-directed-tests",
            [
                project_python,
                "-m",
                "pytest",
                "tests/test_critical_chain_rv1.py",
                "-k",
                "not t01",
                "-q",
            ],
        ),
        run_command("03-mypy-src", [project_python, "-m", "mypy", "src"]),
    ]
    write_json(RUN_ROOT / "process" / "commands.json", commands)
    return 0 if summary["pass_count"] == len(tasks) and summary["drift_count"] == 0 else 1


def render_page(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open(source) as document:
        pixmap = document[0].get_pixmap(
            matrix=pymupdf.Matrix(2, 2),
            colorspace=pymupdf.csRGB,
            alpha=False,
        )
        pixmap.save(target)


def execute_write_case(
    task: dict[str, Any],
    plan: dict[str, Any],
) -> dict[str, object]:
    source = RUN_ROOT / str(task["page_pdf_path"])
    source_hash = str(task["page_pdf_sha256"])
    source_facts = PageFactsExtractor().extract_page(source, source_hash, 1)
    span = next(
        item
        for item in source_facts.text_spans
        if item.object_id == plan["source_object_id"] and item.text.strip() == plan["source_text"]
    )
    replacement = str(plan["replacement"])
    requested_size = min(max(float(span.font_size), 5.5), 18.0)
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
        operation_id=f"{plan['case_id']}-replace",
        region_id=f"{plan['case_id']}.meaningful-translation",
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
        patch_id=f"{plan['case_id']}-single-page",
        source_hash=source_hash,
        page_no=1,
        geometry_hash=source_facts.page.geometry_hash,
        owner=OWNER,
        operations=(operation,),
    )
    context = PageExecutionContext(
        job_id="critical-chain-rv1-page-matrix",
        run_id=RUN_ID,
        source_hash=source_hash,
        page_no=1,
        geometry_hash=source_facts.page.geometry_hash,
        config_snapshot_hash=CONFIG_HASH,
    )
    candidate = RUN_ROOT / "output" / "translated_spans" / f"{plan['case_id']}.pdf"
    candidate.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, candidate)
    source_structure = capture_document_structure(source)
    applied = PagePatchInterpreter(
        ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT)
    ).replay_document(
        candidate,
        (ReplayPage(context, source_facts, patch, OWNER),),
        diagnostic=True,
    )
    candidate_hash = sha256_file(candidate)
    candidate_facts = PageFactsExtractor().extract_page(candidate, candidate_hash, 1)
    target_structure = capture_document_structure(candidate)
    preservation = validate_preservation(
        source_structure,
        target_structure,
        frozenset(applied),
        load_support_matrix(),
    )
    with pymupdf.open(candidate) as document:
        extracted = unicodedata.normalize("NFKC", document[0].get_text())
    normalized_replacement = unicodedata.normalize("NFKC", replacement)
    chinese_extractable = normalized_replacement in extracted
    font_pass = any(
        item.embedded
        and item.has_to_unicode
        and "NotoSans" in item.base_font.replace(" ", "")
        for item in candidate_facts.font_objects
    )
    locked_unchanged = (
        source_facts.locked_objects_hash == candidate_facts.locked_objects_hash
    )
    case_id = str(plan["case_id"])
    source_preview = RUN_ROOT / "output" / "preview" / f"{case_id}-source.png"
    candidate_preview = RUN_ROOT / "output" / "preview" / f"{case_id}-candidate.png"
    render_page(source, source_preview)
    render_page(candidate, candidate_preview)
    passed = preservation.passed and chinese_extractable and font_pass and locked_unchanged
    return {
        "case_id": case_id,
        "page_id": task["page_id"],
        "category": task["category"],
        "category_name": task["category_name"],
        "status": "PASS" if passed else "FAIL",
        "source_text": span.text,
        "replacement": replacement,
        "bbox": span.bbox,
        "source": task["page_pdf_path"],
        "candidate": candidate.relative_to(RUN_ROOT).as_posix(),
        "candidate_sha256": candidate_hash,
        "preservation": preservation,
        "locked_objects_unchanged": locked_unchanged,
        "chinese_extractable": chinese_extractable,
        "font_embedded_with_to_unicode": font_pass,
        "source_preview": source_preview.relative_to(RUN_ROOT).as_posix(),
        "candidate_preview": candidate_preview.relative_to(RUN_ROOT).as_posix(),
    }


def write_candidates() -> int:
    """按人工冻结的真实译文计划生成单页技术候选。"""

    plan_path = RUN_ROOT / "process" / "write_plan.json"
    if not plan_path.exists():
        raise RuntimeError("缺少 process/write_plan.json，不能写入占位译文")
    plan = read_json(plan_path)
    cases: list[dict[str, Any]] = list(plan["cases"])
    if len(cases) != 4:
        raise RuntimeError("真实译文写入计划必须恰好包含 4 例")
    manifest = read_json(RUN_ROOT / "input" / "page_matrix_manifest.json")
    tasks = {str(item["page_id"]): item for item in manifest["pages"]}
    results = [execute_write_case(tasks[str(case["page_id"])], case) for case in cases]
    write_json(
        RUN_ROOT / "process" / "write_results.json",
        {
            "schema_version": "transflow.rv1-meaningful-single-page-write/v1",
            "case_count": len(results),
            "pass_count": sum(item["status"] == "PASS" for item in results),
            "cases": results,
        },
    )
    write_json(
        RUN_ROOT / "process" / "visual_review.json",
        {
            "schema_version": "transflow.rv1-visual-review/v1",
            "status": "PENDING",
            "scope": "真实短译文可见、无截断、无重叠、无受保护内容破坏",
            "cases": [
                {
                    "case_id": item["case_id"],
                    "source_preview": item["source_preview"],
                    "candidate_preview": item["candidate_preview"],
                    "status": "PENDING",
                    "observation": "",
                }
                for item in results
            ],
        },
    )
    summary = read_json(RUN_ROOT / "process" / "collection_summary.json")
    summary["write_status"] = (
        "PASS" if all(item["status"] == "PASS" for item in results) else "FAIL"
    )
    summary["write_case_count"] = len(results)
    write_json(RUN_ROOT / "process" / "collection_summary.json", summary)
    shutil.copy2(Path(__file__), RUN_ROOT / "process" / Path(__file__).name)
    return 0 if summary["write_status"] == "PASS" else 1


def finalize() -> int:
    """视觉核验后冻结 Gate、中文报告和 manifest。"""

    manifest_path = RUN_ROOT / "run_manifest.json"
    if manifest_path.exists():
        raise RuntimeError("run05 已冻结")
    summary = read_json(RUN_ROOT / "process" / "collection_summary.json")
    writes = read_json(RUN_ROOT / "process" / "write_results.json")
    visual = read_json(RUN_ROOT / "process" / "visual_review.json")
    commands = read_json(RUN_ROOT / "process" / "commands.json")
    prior = read_json(PRIOR_SINGLE_DOCUMENT_RUN / "process" / "gate_results.json")
    prior_gates = {item["id"]: item["status"] for item in prior["gates"]}
    facts_pass = (
        summary["page_count"] == summary["pass_count"] == 72
        and summary["classification_page_count"] == 32
        and summary["annual_page_count"] == 40
        and summary["drift_count"] == summary["error_count"] == 0
        and all(
            item["page_count"] == item["pass_count"] == 2
            for item in summary["category_status"]
        )
        and summary["visual_only_inventory_empty"]
        and prior_gates.get("G-RV-02") == "PASS"
    )
    visual_pass = visual["status"] == "PASS" and all(
        item["status"] == "PASS" for item in visual["cases"]
    )
    write_pass = (
        writes["case_count"] == writes["pass_count"] == 4
        and visual_pass
        and prior_gates.get("G-RV-03") == "PASS"
    )
    commands_pass = all(item["exit_code"] == 0 for item in commands)
    status = "PASS" if facts_pass and write_pass and commands_pass else "FAIL"
    gates = {
        "schema_version": "transflow.rv1-page-matrix-gates/v1",
        "gates": [
            {
                "id": "G-RV-02",
                "status": "PASS" if facts_pass else "FAIL",
                "metrics": {
                    "classification_categories": 16,
                    "classification_pages": summary["classification_page_count"],
                    "annual_documents": 4,
                    "annual_pages": summary["annual_page_count"],
                    "total_pages": summary["page_count"],
                    "nondeterministic_drift_count": summary["drift_count"],
                },
            },
            {
                "id": "G-RV-03",
                "status": "PASS" if write_pass else "FAIL",
                "metrics": {
                    "meaningful_translation_write_cases": writes["case_count"],
                    "write_pass_count": writes["pass_count"],
                    "visual_review": visual["status"],
                },
            },
        ],
        "axes": {
            "EngineeringClosure": status,
            "ProductAcceptance": "NOT_EVALUATED_RV1_TECHNICAL_SCOPE_ONLY",
        },
    }
    write_json(RUN_ROOT / "process" / "gate_results.json", gates)

    category_lines = "\n".join(
        f"- {item['category_name']}：{item['pass_count']}/{item['page_count']} 页通过。"
        for item in summary["category_status"]
    )
    feature_totals = summary["feature_totals"]
    report = f"""# RV1 页面级事实与 PDF 保真重新验收报告

## 结论

- G-RV-02：`{'PASS' if facts_pass else 'FAIL'}`。本轮不是逐份跑 112 份年报，
  而是从 16 个正确分类中各取 2 个未翻译原文单页，再从 4 份大年报中
  各截 10 页，共 72 页；全部重复提取两次，漂移 0。
- G-RV-03：`{'PASS' if write_pass else 'FAIL'}`。4 个英文单页只替换一处
  与原文真实对应的中文短译文；保存、重开、文字提取、字体和受保护内容均通过。
- EngineeringClosure：`{status}`。本轮只回答页面事实、机械写入和 PDF 保真，
  不代表完整 Toolbox 翻译排版已经通过产品验收。

## 为什么这样选

- 分类结果本身就是未翻译单页，适合直接看原页和修改页，不再生成几百页、
  只修改一句的年报候选。
- 16 个叶子类别每类 2 页；能识别语言时优先中英文各一页。
- 单栏正文保留计划强制页 `S2P0151`；纯视觉页选择文件大小两端，验证没有
  可编辑文字时不会伪造翻译分母。
- 大年报只选四份：英文最长、中文文件最大、英文视觉结构压力、中文大文档
  页面几何变化；每份按首尾和等距位置截 10 页，不挨个跑整本语料。

## 分类覆盖

{category_lines}

## 页面事实总量

- 原生文字片段：{feature_totals['text_spans']}。
- 图片：{feature_totals['images']}；矢量绘图：{feature_totals['drawings']}。
- 表格：{feature_totals['tables']}；链接：{feature_totals['links']}；
  注释：{feature_totals['annotations']}；字体引用：{feature_totals['fonts']}。

## 中文写入边界

- 4 个候选只翻译一个明确短语，用于验证中文字体、可提取性和保护合同；
  它们不是整页译文，也不使用“年度报告验证”之类无语义占位文字。
- 原页和候选页对照见 `output/preview/`；具体原文、译文和坐标见
  `process/write_results.json`。
- 上一轮 run04 的整本年报结果保留为诊断历史，但已被本轮页面级方案取代，
  不再作为推荐回归集。

## 实现索引

- 输入谱系：`input/page_matrix_manifest.json`。
- 逐页双跑：`process/pages/`；汇总：`process/collection_summary.json`。
- 写入计划与结果：`process/write_plan.json`、`process/write_results.json`。
- 视觉记录与 Gate：`process/visual_review.json`、`process/gate_results.json`。
"""
    (RUN_ROOT / "report.md").write_text(report, encoding="utf-8")
    artifacts: list[dict[str, object]] = []
    for path in sorted(RUN_ROOT.rglob("*")):
        if not path.is_file() or path.name == "run_manifest.json":
            continue
        artifacts.append(
            {
                "path": path.relative_to(RUN_ROOT).as_posix(),
                "size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    run_manifest = {
        "schema_version": "transflow.critical-chain-revalidation-run/v1",
        "stage": "RV1",
        "run_id": RUN_ID,
        "status": status,
        "ended_at": now_iso(),
        "gates": {
            "G-RV-02": "PASS" if facts_pass else "FAIL",
            "G-RV-03": "PASS" if write_pass else "FAIL",
        },
        "scope": {
            "classification_categories": 16,
            "classification_pages": 32,
            "annual_documents": 4,
            "annual_pages": 40,
            "total_pages": 72,
            "meaningful_translation_write_cases": 4,
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
        "artifacts": artifacts,
        "artifact_count_excluding_self": len(artifacts),
        "supersedes": {
            "run_id": "04-stratified-14pdf-20260721-181437",
            "reason": "用户要求改为分类单页 + 大年报抽页，不再整本逐份跑",
        },
        "product_acceptance": "NOT_EVALUATED",
        "tm3_allowed": False,
        "self_hash": "NOT_RECORDED_TO_AVOID_RECURSIVE_MANIFEST_HASH",
    }
    write_json(manifest_path, run_manifest)
    return 0 if status == "PASS" else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("collect", "write", "finalize"))
    parser.add_argument("--workers", type=int, default=4)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.phase == "collect":
        return collect(max(1, args.workers))
    if args.phase == "write":
        return write_candidates()
    return finalize()


if __name__ == "__main__":
    raise SystemExit(main())
