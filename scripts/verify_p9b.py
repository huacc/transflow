"""复核 P9B 六叶、完整文档、页记忆、恢复、静态边界与对比产物。"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any

import pymupdf

from transflow.domain.repair_memory import PageRepairMemory, RepairAttemptStatus

LOGGER = logging.getLogger("transflow.p9b.verify")
REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = REPO_ROOT / "resources" / "evidence" / "p9b" / "real_run_manifest.json"
WINDOWS_ABSOLUTE_PATH = re.compile(r"(?i)(?:^|[^A-Za-z0-9_])[A-Z]:[\\/]")
P9_ROUTES = {
    "cover",
    "contents",
    "end",
    "body.flow_text.multi",
    "body.table",
    "body.anchored_blocks",
}


def _load_json(path: Path) -> dict[str, Any]:
    """读取仓库内 UTF-8 JSON 权威证据。"""

    return json.loads(path.read_text(encoding="utf-8"))


def _sha256_file(path: Path) -> str:
    """流式重算实际文件 SHA-256。"""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _repository_path(relative: str) -> Path:
    """把证据中的相对路径收敛到仓库内，拒绝绝对路径和目录逃逸。"""

    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError(f"UNSAFE_RELATIVE_PATH:{relative}")
    path = (REPO_ROOT / candidate).resolve()
    path.relative_to(REPO_ROOT.resolve())
    return path


def _open_pdf(relative: str, expected_pages: int | None = None) -> list[str]:
    """真实重开一个 PDF，并按需核对页数。"""

    try:
        path = _repository_path(relative)
    except ValueError as error:
        return [str(error)]
    if not path.is_file():
        return [f"PDF_MISSING:{relative}"]
    try:
        with pymupdf.open(path) as document:
            if expected_pages is not None and document.page_count != expected_pages:
                return [f"PDF_PAGE_COUNT_INVALID:{relative}:{document.page_count}"]
            if document.page_count < 1:
                return [f"PDF_EMPTY:{relative}"]
            document.load_page(0)
    except Exception as error:
        return [f"PDF_OPEN_FAILED:{relative}:{type(error).__name__}"]
    return []


def check_manifest_safety(manifest: dict[str, Any]) -> list[str]:
    """拒绝宿主绝对路径、密钥值和错误证据中的 Provider 原文。"""

    serialized = json.dumps(manifest, ensure_ascii=False)
    violations: list[str] = []
    if WINDOWS_ABSOLUTE_PATH.search(serialized):
        violations.append("WINDOWS_ABSOLUTE_PATH_PRESENT")
    lowered = serialized.casefold()
    for token in ("bearer ", "provider_response", "raw_provider_payload"):
        if token in lowered:
            violations.append(f"FORBIDDEN_SECRET_OR_PROVIDER_CONTENT:{token.strip()}")
    return violations


def check_leaf_runs(manifest: dict[str, Any]) -> list[str]:
    """核验六个真实分类叶的 candidate-0、页记忆和输入输出对比。"""

    leaves = manifest.get("leaf_runs", [])
    violations: list[str] = []
    if {item.get("route") for item in leaves} != P9_ROUTES:
        violations.append("REAL_LEAF_ROUTE_SET_INVALID")
    for item in leaves:
        route = str(item.get("route"))
        for key in (
            "input_path",
            "candidate_zero_path",
            "translated_repaired_path",
            "safe_output_path",
            "comparison_path",
        ):
            violations.extend(_open_pdf(str(item.get(key, "")), 1))
        png = _repository_path(str(item.get("comparison_png_path", "")))
        if not png.is_file() or png.stat().st_size == 0:
            violations.append(f"COMPARISON_PNG_INVALID:{route}")
        memory_path = _repository_path(str(item.get("memory_path", "")))
        if not memory_path.is_file():
            violations.append(f"PAGE_MEMORY_MISSING:{route}")
            continue
        memory = PageRepairMemory.from_dict(_load_json(memory_path))
        if memory.memory_hash != item.get("memory_hash") or not memory.finalized:
            violations.append(f"PAGE_MEMORY_INVALID:{route}")
        if len(memory.attempts) != item.get("attempt_count"):
            violations.append(f"ATTEMPT_COUNT_INVALID:{route}")
    return violations


def check_document_runs(manifest: dict[str, Any]) -> list[str]:
    """核验两份完整 PDF 的唯一输出、页数、记忆引用和发布硬校验。"""

    documents = manifest.get("document_runs", [])
    violations: list[str] = []
    if len(documents) != 2:
        violations.append("COMPLETE_DOCUMENT_COUNT_INVALID")
    for item in documents:
        page_count = int(item.get("page_count", 0))
        violations.extend(_open_pdf(str(item.get("input_path", "")), page_count))
        violations.extend(_open_pdf(str(item.get("output_path", "")), page_count))
        violations.extend(_open_pdf(str(item.get("comparison_path", "")), 1))
        memory_path = _repository_path(str(item.get("page_memory_path", "")))
        memory = PageRepairMemory.from_dict(_load_json(memory_path))
        if memory.memory_hash != item.get("page_memory_hash"):
            violations.append(f"DOCUMENT_PAGE_MEMORY_INVALID:{item.get('source_hash')}")
        if not all(
            (
                item.get("all_pages_finalized"),
                item.get("output_openable"),
                item.get("preservation_passed"),
            )
        ):
            violations.append(f"DOCUMENT_FINALIZATION_INVALID:{item.get('source_hash')}")
    return violations


def check_boundaries(manifest: dict[str, Any]) -> list[str]:
    """从真实探针重算物化失败、恢复、静态边界、错路由和新 run 隔离。"""

    violations: list[str] = []
    if manifest.get("attempt_terminal_coverage") != 1.0:
        violations.append("ATTEMPT_TERMINAL_COVERAGE_INVALID")
    if manifest.get("materialization_failure_count", 0) < 1:
        violations.append("MATERIALIZATION_FAILURE_NOT_OBSERVED")
    if manifest.get("fake_candidate_ref_count") != 0:
        violations.append("MATERIALIZATION_FAILURE_FAKE_CANDIDATE")
    recovery = manifest.get("recovery", {})
    if not all(
        recovery.get(key) is True
        for key in (
            "before_commit_crash_observed",
            "before_commit_equivalent",
            "after_commit_equivalent",
        )
    ) or recovery.get("duplicate_action_count") != 0:
        violations.append("RECOVERY_PROBE_INVALID")
    static = manifest.get("static_boundary", {})
    if (
        static.get("forbidden_call_count") != 0
        or static.get("forbidden_call_sites") != []
        or static.get("static_registry_unchanged") is not True
    ):
        violations.append("STATIC_BOUNDARY_INVALID")
    boundary = manifest.get("result_boundary", {})
    mismatch = boundary.get("route_mismatch", {})
    violations.extend(_open_pdf(str(boundary.get("diagnostic_source_path", "")), 1))
    violations.extend(_open_pdf(str(boundary.get("diagnostic_projection_path", "")), 1))
    violations.extend(_open_pdf(str(boundary.get("diagnostic_comparison_path", "")), 1))
    diagnostic_png = _repository_path(
        str(boundary.get("diagnostic_comparison_png_path", ""))
    )
    if not diagnostic_png.is_file() or diagnostic_png.stat().st_size == 0:
        violations.append("G9C_DIAGNOSTIC_COMPARISON_PNG_INVALID")
    if (
        boundary.get("diagnostic_isolated") is not True
        or boundary.get("diagnostic_published_count") != 0
        or boundary.get("diagnostic_expected_unit_count")
        != boundary.get("diagnostic_materialized_unit_count")
        or mismatch.get("repair_attempt_count") != 0
        or mismatch.get("translation_call_delta") != 0
    ):
        violations.append("G9C_RESULT_BOUNDARY_INVALID")
    reopened = manifest.get("reopened_runs", {})
    if (
        reopened.get("terminal_run_count") != 2
        or reopened.get("imported_attempt_count") != 0
        or reopened.get("identity_hashes_unique") is not True
    ):
        violations.append("REOPENED_RUN_ISOLATION_INVALID")
    return violations


def check_attempt_artifacts(manifest: dict[str, Any]) -> list[str]:
    """逐个重读 Attempt，验证成功候选存在、物化失败不含伪引用。"""

    violations: list[str] = []
    for item in manifest.get("leaf_runs", []):
        memory_path = _repository_path(str(item["memory_path"]))
        memory = PageRepairMemory.from_dict(_load_json(memory_path))
        run_root = memory_path.parents[4]
        for attempt in memory.attempts:
            if attempt.status is RepairAttemptStatus.MATERIALIZATION_FAILED:
                if attempt.candidate_artifact_ref is not None:
                    violations.append("FAILED_ATTEMPT_HAS_CANDIDATE_REF")
                continue
            if attempt.candidate_artifact_ref is None:
                violations.append("MATERIALIZED_ATTEMPT_MISSING_CANDIDATE_REF")
                continue
            candidate = run_root / attempt.candidate_artifact_ref
            if not candidate.is_file() or _sha256_file(candidate) != attempt.evidence_hash:
                violations.append(f"ATTEMPT_CANDIDATE_HASH_INVALID:{attempt.attempt_no}")
    return violations


def all_checks() -> dict[str, list[str]]:
    """运行 G9B 全部只读复核并按证据类别返回违规项。"""

    if not MANIFEST_PATH.is_file():
        return {"manifest": ["P9B_REAL_MANIFEST_MISSING"]}
    manifest = _load_json(MANIFEST_PATH)
    return {
        "manifest_safety": check_manifest_safety(manifest),
        "leaf_runs": check_leaf_runs(manifest),
        "document_runs": check_document_runs(manifest),
        "boundaries": check_boundaries(manifest),
        "attempt_artifacts": check_attempt_artifacts(manifest),
    }


def main() -> int:
    """打印机器可读核验结果并原样返回 Gate 状态。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    checks = all_checks()
    print(json.dumps(checks, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if any(checks.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
