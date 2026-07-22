"""复核 P9A current overlay、Gate 索引、真实年报 Artifact 和安全边界。"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from scripts import build_p0_assets, verify_p0

LOGGER = logging.getLogger("transflow.p9a.verify")
REPO_ROOT = Path(__file__).resolve().parent.parent
EVIDENCE_PATH = REPO_ROOT / "resources" / "evidence" / "p9a" / "real_document_manifest.json"
GATE_INDEX_PATH = REPO_ROOT / "resources" / "manifests" / "gate_index.json"


def load_json(path: Path) -> dict[str, Any]:
    """读取仓库内 UTF-8 JSON 权威文件。"""

    return json.loads(path.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    """流式重算实际文件 SHA-256。"""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_governance() -> list[str]:
    """核验 current overlay、字母阶段追溯、排期与四个唯一 Gate 路径。"""

    violations = [
        *build_p0_assets.check_assets(),
        *verify_p0.check_traceability(),
        *verify_p0.check_schedule(),
    ]
    if not GATE_INDEX_PATH.is_file():
        return [*violations, "GATE_INDEX_MISSING"]
    index = load_json(GATE_INDEX_PATH)
    paths = tuple(index.get("gates", {}).values())
    if set(index.get("gates", {})) != {"G9C", "G9A-0", "G9A", "G9B"}:
        violations.append("GATE_INDEX_SET_INVALID")
    if len(paths) != len(set(paths)):
        violations.append("GATE_MANIFEST_PATH_DUPLICATED")
    for relative in paths:
        path = (REPO_ROOT / relative).resolve()
        try:
            path.relative_to(REPO_ROOT.resolve())
        except ValueError:
            violations.append(f"GATE_PATH_OUTSIDE_REPOSITORY:{relative}")
            continue
        if not path.is_file():
            violations.append(f"GATE_MANIFEST_MISSING:{relative}")
    return violations


def check_real_artifacts() -> list[str]:
    """核验至少两份完整 PDF、全页引用、内容哈希和无界内容边界。"""

    if not EVIDENCE_PATH.is_file():
        return ["REAL_EVIDENCE_MANIFEST_MISSING"]
    evidence = load_json(EVIDENCE_PATH)
    documents = evidence.get("documents", [])
    violations: list[str] = []
    if len(documents) < 2:
        violations.append("COMPLETE_REAL_PDF_COUNT_LT_2")
    source_hashes: set[str] = set()
    memory_hashes: set[str] = set()
    for item in documents:
        source = (REPO_ROOT / item["source_path"]).resolve()
        artifact = (REPO_ROOT / item["artifact_path"]).resolve()
        if not source.is_file() or sha256_file(source) != item["source_sha256"]:
            violations.append(f"SOURCE_HASH_INVALID:{item['source_path']}")
        if not artifact.is_file() or sha256_file(artifact) != item["artifact_sha256"]:
            violations.append(f"ARTIFACT_HASH_INVALID:{item['artifact_path']}")
            continue
        if item["artifact_sha256"] != item["memory_hash"]:
            violations.append(f"CONTENT_ADDRESS_MISMATCH:{item['artifact_path']}")
        if item["page_count"] != item["page_ref_count"]:
            violations.append(f"PAGE_COVERAGE_INVALID:{item['source_path']}")
        lowered = artifact.read_text(encoding="utf-8").casefold()
        for token in ("api_key", "provider_response", "raw_text", "translated_text"):
            if token in lowered:
                violations.append(f"FORBIDDEN_MEMORY_CONTENT:{token}:{item['artifact_path']}")
        source_hashes.add(item["source_sha256"])
        memory_hashes.add(item["memory_hash"])
    if len(source_hashes) < 2 or len(memory_hashes) < 2:
        violations.append("REAL_DOCUMENT_DIFFERENTIATION_INVALID")
    return violations


def all_checks() -> dict[str, list[str]]:
    """运行 G9A 治理和真实 Artifact 两组只读核验。"""

    return {"governance": check_governance(), "real_artifacts": check_real_artifacts()}


def main() -> int:
    """输出机器可读真实核验结果并原样返回失败状态。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    results = all_checks()
    print(json.dumps(results, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if any(results.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
