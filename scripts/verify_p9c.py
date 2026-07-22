"""复验 P9C 历史账本、真实双轨 PDF、发布隔离和三轴证据。"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

import pymupdf

from transflow.adapters.filesystem.common import load_json

LOGGER = logging.getLogger("transflow.scripts.verify_p9c")
REPO_ROOT = Path(__file__).resolve().parent.parent
EVIDENCE_PATH = REPO_ROOT / "resources" / "evidence" / "p9c" / "p9c_real_regression.json"
SUMMARY_PATH = (
    REPO_ROOT / "output" / "pdf" / "P9C_real_samples" / "P9C_real_samples_summary.json"
)
PRODUCTION_ROOT = REPO_ROOT / "output" / "pdf" / "P9C_real_samples" / "production_safe"
DIAGNOSTIC_ROOT = (
    REPO_ROOT / "output" / "pdf" / "P9C_real_samples" / "diagnostic_candidates"
)


def _sha256_file(path: Path) -> str:
    """流式复算一个证据文件或 PDF 的 SHA-256。"""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _repository_path(relative_path: object) -> Path:
    """把证据中的仓库相对路径解析回当前工作树。"""

    path = (REPO_ROOT / str(relative_path)).resolve()
    path.relative_to(REPO_ROOT.resolve())
    return path


def verify() -> dict[str, Any]:
    """逐项验证内容哈希、PDF 提取、manifest、隔离目录和三轴结论。"""

    LOGGER.info("调用 P9C 真实证据复验，意图=拒绝临时路径和伪诊断")
    evidence = load_json(EVIDENCE_PATH)
    summary = load_json(SUMMARY_PATH)
    source = _repository_path(evidence["source_path"])
    run_root = _repository_path(evidence["run_root"])
    artifact_manifest = load_json(_repository_path(evidence["artifact_manifest_path"]))
    final_manifest = load_json(_repository_path(evidence["final_manifest_path"]))
    final = evidence["final"]
    diagnostic = evidence["diagnostic"]
    diagnostic_artifact = diagnostic["artifact"]
    final_authoritative = run_root / str(final["relative_path"])
    diagnostic_authoritative = run_root / str(diagnostic_artifact["relative_path"])
    final_projection = _repository_path(final["projection_path"])
    diagnostic_projection = _repository_path(evidence["diagnostic_projection_path"])
    with pymupdf.open(source) as source_pdf, pymupdf.open(
        final_authoritative
    ) as final_pdf, pymupdf.open(diagnostic_authoritative) as diagnostic_pdf:
        pdf_checks = {
            "diagnostic_extractable_text": bool(diagnostic_pdf[0].get_text().strip()),
            "page_count_preserved": (
                source_pdf.page_count == final_pdf.page_count == diagnostic_pdf.page_count == 1
            ),
        }
    serialized = json.dumps(evidence, ensure_ascii=False, sort_keys=True).casefold()
    checks = {
        "artifact_manifest_has_dual_tracks": (
            len(artifact_manifest.get("entries", {})) == 2
            and {item["label"] for item in artifact_manifest["entries"].values()}
            == {"final", "diagnostic"}
        ),
        "diagnostic_content_addressed": (
            _sha256_file(diagnostic_authoritative) == diagnostic_artifact["content_hash"]
            == _sha256_file(diagnostic_projection)
        ),
        "diagnostic_differs_from_source": (
            diagnostic_artifact["content_hash"] != evidence["source_hash"]
        ),
        "diagnostic_isolated": diagnostic_projection.parent.resolve()
        == DIAGNOSTIC_ROOT.resolve(),
        "diagnostic_ready": diagnostic["status"] == "TRANSLATED_DIAGNOSTIC_READY",
        "final_content_addressed": (
            _sha256_file(final_authoritative) == final["content_hash"]
            == _sha256_file(final_projection)
        ),
        "final_is_source_passthrough": (
            final["source_passthrough"] is True
            and final["content_hash"] == evidence["source_hash"] == _sha256_file(source)
        ),
        "final_pointer_isolated": (
            final_manifest["artifact_id"] == final["artifact_id"]
            and final_manifest["artifact_id"] != diagnostic_artifact["artifact_id"]
        ),
        "history_not_regated": evidence["historical_gate_reexecution_count"] == 0,
        "mock_result_count": evidence["mock_result_count"] == 0,
        "no_secret_material": not any(
            token in serialized for token in ("api_key", "authorization", "bearer ")
        ),
        "production_projection_isolated": final_projection.parent.resolve()
        == PRODUCTION_ROOT.resolve(),
        "qwen_called": int(evidence["qwen_http_calls"]) >= 1,
        "summary_matches_evidence": summary == evidence,
        "three_axes_honest": (
            evidence["axes"]["engineering_closure"] == "PASS"
            and evidence["axes"]["product_acceptance"] == "FAIL"
            and evidence["axes"]["promotion_eligibility"] == "INELIGIBLE"
        ),
        "unit_materialization_complete": (
            diagnostic["evidence"]["expected_unit_count"]
            == diagnostic["evidence"]["materialized_unit_count"]
            and diagnostic["evidence"]["missing_unit_ids"] == []
        ),
        **pdf_checks,
    }
    return {
        "checks": checks,
        "diagnostic_hash": diagnostic_artifact["content_hash"],
        "final_hash": final["content_hash"],
        "qwen_http_calls": evidence["qwen_http_calls"],
        "run_id": evidence["run_id"],
        "status": "PASS" if all(checks.values()) else "FAIL",
    }


def main() -> int:
    """打印机器可读的 G9C 双轨产物复验结果。"""

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")
    result = verify()
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
