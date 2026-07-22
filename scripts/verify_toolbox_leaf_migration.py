"""复核 TM0 基线或逐叶迁移真实产物，不生成补写的成功数据。"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

import pymupdf

# 与计划规定的直接脚本命令保持一致，不依赖调用方预先设置 PYTHONPATH。
_BOOTSTRAP_ROOT = Path(__file__).resolve().parent.parent
for _bootstrap_path in (_BOOTSTRAP_ROOT, _BOOTSTRAP_ROOT / "src"):
    if str(_bootstrap_path) not in sys.path:
        sys.path.insert(0, str(_bootstrap_path))

from scripts.run_toolbox_leaf_migration import (  # noqa: E402
    ALL_GATE_IDS,
    CATALOG_PATH,
    EVIDENCE_ROOT,
    OUTPUT_ROOT,
    REPO_ROOT,
    ROUTE_STAGES,
    TM0_BASELINE_POINTER,
    TM0_OUTPUT_ROOT,
    canonical_stage,
    route_slug,
)
from transflow.domain.common import content_sha256  # noqa: E402

LOGGER = logging.getLogger("transflow.toolbox_leaf_migration.verify")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
WINDOWS_ABSOLUTE_PATH = re.compile(r"(?i)(?:^|[^A-Za-z0-9_])[A-Z]:[\\/]")
SECRET_TEXT = re.compile(r"(?i)bearer\s+|api[_-]?key\s*[:=]")
FORBIDDEN_SECRET_KEYS = {
    "api_key",
    "authorization_header",
    "provider_response",
    "raw_provider_payload",
    "raw_provider_response",
}


def _sha256_file(path: Path) -> str:
    """流式复算持久化 Artifact 的内容哈希。"""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """读取 JSON 对象并返回机器可读失败，不抛出导致审计中断的异常。"""

    if not path.is_file():
        return None, f"JSON_MISSING:{path.name}"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, f"JSON_INVALID:{path.name}"
    if not isinstance(payload, dict):
        return None, f"JSON_ROOT_INVALID:{path.name}"
    return payload, None


def _payload_safety_violations(value: object, context: str = "payload") -> list[str]:
    """独立扫描绝对路径、授权头和禁止 Provider 原始内容。"""

    violations: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{context}.{key}"
            if str(key).casefold() in FORBIDDEN_SECRET_KEYS:
                violations.append(f"FORBIDDEN_SECRET_KEY:{child}")
            violations.extend(_payload_safety_violations(item, child))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            violations.extend(_payload_safety_violations(item, f"{context}[{index}]"))
    elif isinstance(value, str):
        if WINDOWS_ABSOLUTE_PATH.search(value):
            violations.append(f"WINDOWS_ABSOLUTE_PATH:{context}")
        if SECRET_TEXT.search(value):
            violations.append(f"SECRET_TEXT:{context}")
    return violations


def _repository_file(reference: object, repository_root: Path) -> Path | None:
    """把证据中的相对引用安全解析到仓库内。"""

    if not isinstance(reference, str):
        return None
    candidate = Path(reference)
    if candidate.is_absolute() or ".." in candidate.parts:
        return None
    resolved = (repository_root / candidate).resolve()
    try:
        resolved.relative_to(repository_root.resolve())
    except ValueError:
        return None
    return resolved


def verify_tm0_baseline(
    run_id: str,
    *,
    evidence_root: Path = EVIDENCE_ROOT,
    output_root: Path = TM0_OUTPUT_ROOT,
    baseline_pointer_path: Path = TM0_BASELINE_POINTER,
    repository_root: Path = REPO_ROOT,
) -> tuple[str, ...]:
    """逐项复算 TM0 指针、冻结文件、Catalog、P9B 和无伪候选声明。"""

    violations: list[str] = []
    pointer, error = _load_json(baseline_pointer_path)
    if error or pointer is None:
        return (error or "TM0_POINTER_INVALID",)
    violations.extend(_payload_safety_violations(pointer, "pointer"))
    if pointer.get("run_id") != run_id or pointer.get("stage") != "TM0":
        violations.append("TM0_POINTER_IDENTITY_INVALID")
    baseline_ref = pointer.get("baseline_ref")
    if not isinstance(baseline_ref, str) or Path(baseline_ref).is_absolute() or ".." in Path(
        baseline_ref
    ).parts:
        return tuple((*violations, "TM0_BASELINE_REF_INVALID"))
    baseline_path = (evidence_root / baseline_ref).resolve()
    try:
        baseline_path.relative_to(evidence_root.resolve())
    except ValueError:
        return tuple((*violations, "TM0_BASELINE_REF_OUTSIDE_ROOT"))
    baseline, error = _load_json(baseline_path)
    if error or baseline is None:
        return tuple((*violations, error or "TM0_BASELINE_INVALID"))
    violations.extend(_payload_safety_violations(baseline, "baseline"))
    if (
        baseline.get("run_id") != run_id
        or baseline.get("stage") != "TM0"
        or baseline.get("state") != "BASELINE_FROZEN"
    ):
        violations.append("TM0_BASELINE_IDENTITY_INVALID")
    recorded_hash = baseline.get("baseline_hash")
    body = {key: value for key, value in baseline.items() if key != "baseline_hash"}
    actual_hash = content_sha256(body)
    if recorded_hash != actual_hash or pointer.get("baseline_hash") != actual_hash:
        violations.append("TM0_BASELINE_HASH_INVALID")

    catalog = baseline.get("catalog")
    if not isinstance(catalog, dict):
        violations.append("TM0_CATALOG_EVIDENCE_INVALID")
    else:
        current_hash = _sha256_file(CATALOG_PATH)
        if catalog.get("hash_before") != current_hash or catalog.get("hash_after") != current_hash:
            violations.append("TM0_CATALOG_DRIFT")
        if catalog.get("mutation_count") != 0 or pointer.get("catalog_hash") != current_hash:
            violations.append("TM0_CATALOG_MUTATION_INVALID")
        entries = catalog.get("entries")
        if not isinstance(entries, list):
            violations.append("TM0_CATALOG_ENTRIES_INVALID")
        else:
            routes = {item.get("route") for item in entries if isinstance(item, dict)}
            if routes != set(ROUTE_STAGES):
                violations.append("TM0_ROUTE_STAGE_COVERAGE_INVALID")
            for item in entries:
                route_value = item.get("route") if isinstance(item, dict) else None
                if (
                    not isinstance(route_value, str)
                    or ROUTE_STAGES.get(route_value) != item.get("stage")
                ):
                    violations.append("TM0_ROUTE_STAGE_BINDING_INVALID")
                    break

    source_groups = baseline.get("source_groups")
    if not isinstance(source_groups, dict) or not source_groups:
        violations.append("TM0_SOURCE_GROUPS_INVALID")
    else:
        for group_name, group in source_groups.items():
            if not isinstance(group, dict) or not isinstance(group.get("files"), list):
                violations.append(f"TM0_SOURCE_GROUP_INVALID:{group_name}")
                continue
            records = group["files"]
            if group.get("group_hash") != content_sha256(records):
                violations.append(f"TM0_SOURCE_GROUP_HASH_INVALID:{group_name}")
            for record in records:
                if not isinstance(record, dict):
                    violations.append(f"TM0_SOURCE_RECORD_INVALID:{group_name}")
                    continue
                path = _repository_file(record.get("path"), repository_root)
                if path is None or not path.is_file():
                    violations.append(f"TM0_SOURCE_FILE_MISSING:{group_name}")
                elif record.get("sha256") != _sha256_file(path):
                    violations.append(f"TM0_SOURCE_FILE_DRIFT:{record.get('path')}")

    p9b = baseline.get("p9b_reference")
    if not isinstance(p9b, dict) or p9b.get("gate_status") != "PASS":
        violations.append("TM0_P9B_REFERENCE_INVALID")
    else:
        reference_fields = (
            ("gate_path", "gate_hash"),
            ("real_run_manifest_path", "real_run_manifest_hash"),
            ("comparison_path", "comparison_hash"),
        )
        for path_key, hash_key in reference_fields:
            path = _repository_file(p9b.get(path_key), repository_root)
            if path is None or not path.is_file() or p9b.get(hash_key) != _sha256_file(path):
                violations.append(f"TM0_P9B_REFERENCE_DRIFT:{path_key}")

    dependency_scan = baseline.get("production_dependency_scan")
    if not isinstance(dependency_scan, dict) or dependency_scan.get("forbidden_count") != 0:
        violations.append("TM0_PRODUCTION_DEPENDENCY_SCAN_FAILED")
    drivers = baseline.get("route_drivers")
    if not isinstance(drivers, dict) or drivers.get("registered_count") != 0:
        violations.append("TM0_ROUTE_DRIVER_SCOPE_INVALID")
    scope = baseline.get("scope")
    if (
        not isinstance(scope, dict)
        or scope.get("catalog_enablement_changes") != 0
        or scope.get("candidate_pdf_count") != 0
        or scope.get("product_acceptance") != "NOT_EVALUATED"
    ):
        violations.append("TM0_SCOPE_INVALID")

    output_manifest_path = output_root / "TM0" / run_id / "run_manifest.json"
    output_manifest, error = _load_json(output_manifest_path)
    if error or output_manifest is None:
        violations.append(error or "TM0_OUTPUT_MANIFEST_INVALID")
    else:
        violations.extend(_payload_safety_violations(output_manifest, "output_manifest"))
        if (
            output_manifest.get("state") != "BASELINE_FROZEN"
            or output_manifest.get("baseline_hash") != actual_hash
            or output_manifest.get("false_candidate_count") != 0
        ):
            violations.append("TM0_OUTPUT_SCOPE_INVALID")
        artifacts = output_manifest.get("artifacts")
        if not isinstance(artifacts, dict) or any(
            not isinstance(item, dict)
            or item.get("present") is not False
            or item.get("reason") != "TM0_FACILITY_ONLY"
            for item in artifacts.values()
        ):
            violations.append("TM0_FALSE_ARTIFACT_DECLARATION")
    return tuple(sorted(set(violations)))


def _verify_artifact(
    artifact: object,
    repository_root: Path,
    name: str,
) -> list[str]:
    """复算一个显式存在 Artifact，或要求机器可读的缺失原因。"""

    if not isinstance(artifact, dict):
        return [f"ARTIFACT_RECORD_INVALID:{name}"]
    if artifact.get("present") is False:
        return [] if artifact.get("reason") else [f"ARTIFACT_REASON_MISSING:{name}"]
    if artifact.get("present") is not True:
        return [f"ARTIFACT_PRESENT_INVALID:{name}"]
    path = _repository_file(artifact.get("path"), repository_root)
    expected_hash = artifact.get("sha256")
    if path is None or not path.is_file():
        return [f"ARTIFACT_MISSING:{name}"]
    if not isinstance(expected_hash, str) or SHA256_PATTERN.fullmatch(expected_hash) is None:
        return [f"ARTIFACT_HASH_INVALID:{name}"]
    if _sha256_file(path) != expected_hash:
        return [f"ARTIFACT_HASH_MISMATCH:{name}"]
    if path.suffix.casefold() == ".pdf":
        try:
            with pymupdf.open(path) as document:
                if document.page_count < 1:
                    return [f"ARTIFACT_PDF_EMPTY:{name}"]
                document.load_page(0)
        except Exception:
            return [f"ARTIFACT_PDF_INVALID:{name}"]
    return []


def verify_leaf_run(
    stage: str,
    route: str,
    run_id: str,
    *,
    evidence_root: Path = EVIDENCE_ROOT,
    output_root: Path = OUTPUT_ROOT,
    baseline_pointer_path: Path = TM0_BASELINE_POINTER,
    repository_root: Path = REPO_ROOT,
) -> tuple[str, ...]:
    """按公共 Gate 合同复核一个 Route 驱动写出的真实证据和 Artifact。"""

    violations: list[str] = []
    selected_stage = canonical_stage(stage)
    if ROUTE_STAGES.get(route) != selected_stage:
        return ("STAGE_ROUTE_MISMATCH",)
    manifest_path = evidence_root / route_slug(route) / run_id / "run_manifest.json"
    manifest, error = _load_json(manifest_path)
    if error or manifest is None:
        return (error or "RUN_MANIFEST_INVALID",)
    violations.extend(_payload_safety_violations(manifest, "run_manifest"))
    if (
        manifest.get("stage") != selected_stage
        or manifest.get("route") != route
        or manifest.get("run_id") != run_id
        or manifest.get("status") != "REVIEW_PENDING"
        or manifest.get("state") != "FULL_E2E_PASS"
    ):
        violations.append("RUN_IDENTITY_OR_STATE_INVALID")

    pointer, error = _load_json(baseline_pointer_path)
    if error or pointer is None or manifest.get("baseline_hash") != pointer.get("baseline_hash"):
        violations.append("RUN_BASELINE_BINDING_INVALID")
    current_catalog_hash = _sha256_file(CATALOG_PATH)
    if manifest.get("catalog_hash") != current_catalog_hash:
        violations.append("DEFAULT_CATALOG_DRIFT")

    gates = manifest.get("gate_results")
    if not isinstance(gates, dict) or set(gates) != set(ALL_GATE_IDS):
        violations.append("GATE_SET_INVALID")
    else:
        for gate_id in ALL_GATE_IDS[:-1]:
            item = gates[gate_id]
            if not isinstance(item, dict) or item.get("status") != "PASS" or not item.get(
                "evidence_refs"
            ):
                violations.append(f"GATE_NOT_PROVEN:{gate_id}")
        human = gates["G-TM-14"]
        if not isinstance(human, dict) or human.get("status") != "REVIEW_PENDING":
            violations.append("HUMAN_REVIEW_STATE_INVALID")

    attestation = manifest.get("route_attestation")
    if not isinstance(attestation, dict) or any(
        attestation.get(key) != route
        for key in ("spike_contract_route", "production_route", "target_route")
    ):
        violations.append("ROUTE_ATTESTATION_INVALID")
    elif attestation.get("forced_route_count") != 0:
        violations.append("FORCED_ROUTE_PRESENT")

    translation = manifest.get("translation")
    if not isinstance(translation, dict):
        violations.append("TRANSLATION_EVIDENCE_INVALID")
    elif route == "visual_only":
        if any(
            translation.get(key) != 0
            for key in (
                "ocr_call_count",
                "patch_count",
                "provider_call_count",
                "repair_call_count",
                "semantic_object_modification_count",
                "translation_unit_count",
            )
        ):
            violations.append("VISUAL_ONLY_ZERO_CALL_CONTRACT_FAILED")
    else:
        bundle_hash = translation.get("bundle_hash")
        if (
            not isinstance(bundle_hash, str)
            or SHA256_PATTERN.fullmatch(bundle_hash) is None
            or translation.get("spike_bundle_hash") != bundle_hash
            or translation.get("transflow_bundle_hash") != bundle_hash
        ):
            violations.append("TRANSLATION_BUNDLE_HASH_MISMATCH")
        if translation.get("real_provider_call_count", 0) < 1:
            violations.append("REAL_PROVIDER_CALL_MISSING")
        if translation.get("mock_response_count") != 0:
            violations.append("MOCK_TRANSLATION_PRESENT")
        if translation.get("completeness_decision") != "PASS":
            violations.append("TRANSLATION_COMPLETENESS_FAILED")
        if translation.get("materialized_translated_unit_count", 0) < 1:
            violations.append("REAL_TRANSLATION_NOT_MATERIALIZED")

    artifacts = manifest.get("artifacts")
    required_artifacts = {
        "source_document",
        "target_page",
        "spike_output",
        "transflow_candidate",
        "repair_candidate",
        "final_delivery",
        "comparison",
    }
    if not isinstance(artifacts, dict) or set(artifacts) != required_artifacts:
        violations.append("ARTIFACT_SET_INVALID")
    else:
        for name, artifact in artifacts.items():
            violations.extend(_verify_artifact(artifact, repository_root, name))

    trace = manifest.get("trace")
    if not isinstance(trace, dict):
        violations.append("FULL_CHAIN_TRACE_INVALID")
    else:
        expected = {
            "document_coordinator_used": True,
            "document_finalizer_used": True,
            "target_toolbox_hit": True,
            "all_pages_finalized": True,
            "page_count_preserved": True,
            "page_order_preserved": True,
            "preservation_passed": True,
            "page_candidate_stitch_count": 0,
        }
        for key, value in expected.items():
            if trace.get(key) != value:
                violations.append(f"FULL_CHAIN_TRACE_FAILED:{key}")

    output_manifest_path = output_root / selected_stage / run_id / "run_manifest.json"
    output_manifest, error = _load_json(output_manifest_path)
    if error or output_manifest is None:
        violations.append(error or "OUTPUT_RUN_MANIFEST_INVALID")
    elif content_sha256(output_manifest) != content_sha256(manifest):
        violations.append("OUTPUT_EVIDENCE_MANIFEST_MISMATCH")
    return tuple(sorted(set(violations)))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析 TM0 或逐叶 Artifact verifier 的稳定命令形态。"""

    parser = argparse.ArgumentParser(description="复核 Transflow Toolbox 逐叶迁移证据")
    parser.add_argument("--stage", required=True)
    parser.add_argument("--route", help="TM1～TM17 必填；TM0 不填写")
    parser.add_argument("--run-id", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """输出全部真实违规；任一违规均非零退出。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    arguments = parse_args(argv)
    violations: tuple[str, ...]
    try:
        stage = canonical_stage(arguments.stage)
        if stage == "TM0":
            if arguments.route is not None:
                violations = ("TM0_ROUTE_FORBIDDEN",)
            else:
                violations = verify_tm0_baseline(arguments.run_id)
        elif not arguments.route:
            violations = ("LEAF_ROUTE_MISSING",)
        else:
            violations = verify_leaf_run(stage, arguments.route, arguments.run_id)
    except Exception as error:
        LOGGER.exception("逐叶证据复核异常，意图=异常也必须形成非零退出")
        violations = (f"VERIFIER_EXCEPTION:{type(error).__name__}",)
    print(
        json.dumps(
            {
                "status": "PASS" if not violations else "FAIL",
                "stage": arguments.stage,
                "run_id": arguments.run_id,
                "violations": list(violations),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if not violations else 1


if __name__ == "__main__":
    raise SystemExit(main())
