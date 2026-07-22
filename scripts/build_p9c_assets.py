"""构建 P9C 历史纠偏账本、合同 Schema 与固定资源指纹。"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger("transflow.scripts.build_p9c_assets")
REPO_ROOT = Path(__file__).resolve().parent.parent
ANCHOR_PATH = REPO_ROOT / "resources" / "manifests" / "p9c_historical_anchor.json"
LEDGER_PATH = REPO_ROOT / "resources" / "evidence" / "p9c" / "p9c_corrective_ledger.v1.json"
FINGERPRINT_PATH = REPO_ROOT / "resources" / "manifests" / "p9c_resource_fingerprints.json"
SCHEMA_ROOT = REPO_ROOT / "resources" / "schemas"
HISTORICAL_REPORT_PATTERN = re.compile(r"^P([5-9])阶段_.*\.md$")


def _canonical_bytes(value: object) -> bytes:
    """把资源对象编码为确定性 UTF-8 JSON 字节。"""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_bytes(content: bytes) -> str:
    """计算字节内容的 SHA-256。"""

    return hashlib.sha256(content).hexdigest()


def _sha256_file(path: Path) -> str:
    """流式计算文件 SHA-256，避免一次载入大型 PDF。"""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative(path: Path) -> str:
    """返回仓库相对 POSIX 路径并拒绝越界。"""

    return path.resolve().relative_to(REPO_ROOT.resolve()).as_posix()


def _git_bytes(anchor_commit: str, relative_path: str) -> bytes | None:
    """从冻结提交读取历史文件原始字节；未跟踪来源返回 ``None``。"""

    process = subprocess.run(
        ["git", "show", f"{anchor_commit}:{relative_path}"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
    )
    if process.returncode != 0:
        return None
    return process.stdout


def _anchor_time(anchor_commit: str) -> str:
    """读取冻结提交时间，作为全部历史来源的稳定版本时间。"""

    process = subprocess.run(
        ["git", "show", "-s", "--format=%cI", anchor_commit],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return process.stdout.strip()


def _walk_toolbox_contracts() -> tuple[Path, ...]:
    """枚举 Toolbox 合同文件，并主动跳过历史运行大目录。"""

    root = REPO_ROOT / "spikes" / "page_toolbox_engine_puncture_v1" / "toolboxes"
    discovered: list[Path] = []
    for current, directories, files in os.walk(root):
        directories[:] = [name for name in directories if name not in {"runs", "__pycache__"}]
        current_path = Path(current)
        for filename in files:
            if filename in {"stage_gate.json", "toolbox_manifest.json"}:
                discovered.append(current_path / filename)
    return tuple(sorted(discovered, key=_relative))


def _source_groups() -> dict[str, tuple[Path, ...]]:
    """返回 P9C.1 规定的全部来源组。"""

    report_root = REPO_ROOT / "docs" / "reports"
    reports = tuple(
        sorted(
            (
                path
                for path in report_root.glob("P*阶段_*.md")
                if HISTORICAL_REPORT_PATTERN.fullmatch(path.name)
            ),
            key=_relative,
        )
    )
    experience_root = REPO_ROOT / "spikes" / "page_toolbox_engine_puncture_v1" / "docs" / "经验"
    experiences = tuple(sorted(experience_root.glob("*.md"), key=_relative))
    audit_root = (
        REPO_ROOT
        / "spikes"
        / "page_classification_engine_puncture_v1"
        / "reports"
        / "deep_audits"
        / "current_classification_20260711"
    )
    classification = tuple(
        audit_root / name
        for name in ("page_audit.jsonl", "summary.json", "深度分类审计报告.md")
    )
    p9_chain = tuple(
        REPO_ROOT / relative
        for relative in (
            "scripts/run_p9_real_samples.py",
            "scripts/verify_p9_real_samples.py",
            "tests/migration/test_p9_real_samples.py",
            "output/pdf/P9_real_samples/P9_real_samples_summary.json",
        )
    )
    stage_evidence = tuple(
        REPO_ROOT / relative
        for relative in (
            "resources/manifests/gate_catalog.json",
            "resources/evidence/p8/p8_acceptance_summary.json",
            "resources/evidence/p9/p9_acceptance_summary.json",
            "resources/evidence/p9/real_sample_regression.json",
        )
    )
    return {
        "historical_report": reports,
        "toolbox_experience": experiences,
        "classification_deep_audit": classification,
        "toolbox_stage_contract": _walk_toolbox_contracts(),
        "p9_real_sample_chain": p9_chain,
        "stage_evidence": stage_evidence,
    }


def _inventory(
    anchor_commit: str,
    anchor_time: str,
    stage_start_hashes: dict[str, str],
    stage_start_capture_time: str,
) -> tuple[dict[str, Any], ...]:
    """计算来源路径、大小、当前哈希与冻结提交哈希。"""

    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for evidence_type, paths in _source_groups().items():
        for path in paths:
            relative_path = _relative(path)
            if relative_path in seen:
                raise RuntimeError(f"duplicate_inventory_path:{relative_path}")
            seen.add(relative_path)
            if not path.is_file():
                raise RuntimeError(f"missing_inventory_path:{relative_path}")
            anchor_content = _git_bytes(anchor_commit, relative_path)
            anchor_sha256 = stage_start_hashes.get(relative_path)
            if anchor_sha256 is None:
                raise RuntimeError(f"stage_start_source_not_anchored:{relative_path}")
            if anchor_content is None:
                anchor_mode = "P9C_STAGE_START_SHA256"
                source_version = "p9c-stage-start"
                version_time = stage_start_capture_time
                git_blob_sha256 = None
            else:
                # Git 在 Windows 检出时可能按属性转换换行；历史不变性以阶段开始工作树
                # 字节哈希判断，同时保留冻结提交 blob 哈希供双重追溯。
                anchor_mode = "GIT_COMMIT_AND_STAGE_START_SHA256"
                source_version = anchor_commit
                version_time = stage_start_capture_time
                git_blob_sha256 = _sha256_bytes(anchor_content)
            entries.append(
                {
                    "anchor_mode": anchor_mode,
                    "anchor_sha256": anchor_sha256,
                    "current_sha256": _sha256_file(path),
                    "evidence_type": evidence_type,
                    "git_blob_sha256": git_blob_sha256,
                    "path": relative_path,
                    "size_bytes": path.stat().st_size,
                    "source_version": source_version,
                    "version_time": version_time,
                }
            )
    return tuple(sorted(entries, key=lambda item: str(item["path"])))


def _historical_facts() -> dict[str, Any]:
    """从真实 P5/P9 报告、审计和样本汇总重算关键历史事实。"""

    p5_report = next((REPO_ROOT / "docs" / "reports").glob("P5阶段_*.md"))
    p9_report = next((REPO_ROOT / "docs" / "reports").glob("P9阶段_*.md"))
    p5_text = p5_report.read_text(encoding="utf-8")
    p9_text = p9_report.read_text(encoding="utf-8")
    audit = json.loads(
        (
            REPO_ROOT
            / "spikes"
            / "page_classification_engine_puncture_v1"
            / "reports"
            / "deep_audits"
            / "current_classification_20260711"
            / "summary.json"
        ).read_text(encoding="utf-8")
    )
    p9_summary = json.loads(
        (REPO_ROOT / "output" / "pdf" / "P9_real_samples" / "P9_real_samples_summary.json")
        .read_text(encoding="utf-8")
    )
    candidates = p9_summary["candidate_results"]
    accepted = sum(item["verdict"] == "ACCEPT" for item in candidates)
    fallback = sum(item["verdict"] == "FALLBACK" for item in candidates)
    passthrough_routes = sorted(
        {
            str(item["route"])
            for item in candidates
            if item["production_safe_hash"] == item["source_hash"]
        }
    )
    if not re.search(r"匿名输入\s*22\s*个|22\s*个按内容哈希", p5_text):
        raise RuntimeError("p5_22_page_baseline_not_found")
    if not re.search(r"1\s*份安全接受、11\s*份安全回退", p9_text):
        raise RuntimeError("p9_candidate_fact_not_found")
    return {
        "classification_current_audit": {
            "ambiguous": int(audit["status_counts"]["AMBIGUOUS"]),
            "correct": int(audit["status_counts"]["CORRECT"]),
            "error": int(audit["status_counts"]["ERROR"]),
            "original_scope_total": 709,
            "removed_original_table_pages": 252,
            "scope": str(audit["scope"]),
            "total": int(audit["total"]),
        },
        "p5_migration_baseline": {
            "anonymous_page_count": 22,
            "historical_gate_reexecuted": False,
        },
        "p9_real_qwen": {
            "accepted": accepted,
            "candidate_count": len(candidates),
            "fallback": fallback,
            "historical_gate_reexecuted": False,
            "production_source_passthrough_routes": passthrough_routes,
            "source_passthrough_leaf_count": len(passthrough_routes),
        },
    }


def _contradictions() -> tuple[dict[str, str], ...]:
    """冻结较晚且更严格的 P9C 合同优先级。"""

    return (
        {
            "adopted_contract": "完整译文通过门禁但布局失败时生成隔离诊断候选；硬物化失败只记状态",
            "effective_gate": "G9C",
            "id": "candidate-semantics",
            "older_semantics": "部分经验把硬失败统一解释为不产生任何候选",
            "reason": "让真实译文布局错误可见，同时不放宽发布安全边界",
        },
        {
            "adopted_contract": (
                "SemanticUnitMap 冻结分母且 TranslationCompletenessDecision PASS 才能进入布局"
            ),
            "effective_gate": "G9C",
            "id": "translation-completeness",
            "older_semantics": (
                "TranslationBundle 仅校验 ID 对齐，无法拒绝占位、回显或无理由源文复制"
            ),
            "reason": "身份完整不等于语义完整",
        },
        {
            "adopted_contract": "能力前提不成立时记录 ROUTE_CAPABILITY_MISMATCH 并安全透传",
            "effective_gate": "G9C",
            "id": "classification-route-mismatch",
            "older_semantics": "错误 Route 可能只表现为叶内普通能力失败",
            "reason": "阻止无限 Repair、运行时热改 Route 或跨叶偷用",
        },
        {
            "adopted_contract": "字形、字体、bbox 与数据绑定必须来自实际 PDF 探针",
            "effective_gate": "G9C",
            "id": "glyph-data-binding",
            "older_semantics": "配置名或可打开 PDF 曾被当作足够证据",
            "reason": "可打开不等于无乱码、无错绑",
        },
        {
            "adopted_contract": (
                "EngineeringClosure、ProductAcceptance、PromotionEligibility 独立投影"
            ),
            "effective_gate": "G9C",
            "id": "engineering-vs-product-pass",
            "older_semantics": "技术 Gate PASS 或源 PDF final 容易被误读为产品翻译 PASS",
            "reason": "工程闭环、产品质量与晋级资格没有逻辑蕴含关系",
        },
    )


def _impact_matrix() -> tuple[dict[str, Any], ...]:
    """为五类历史问题指定前向 owner、最低回归与禁止旁路。"""

    return (
        {
            "affected_stages": ["P9C.2", "P9C.3", "P9A", "P9B", "P10", "P14"],
            "category": "CONTRACT_GAP",
            "effective_gate": "G9C",
            "forward_owner": "TranslationCompletenessGate",
            "id": "semantic-denominator-and-candidate-contract",
            "minimum_tests": ["P9C.2-T01", "P9C.2-T03", "P9C.3-T01"],
            "prohibited_bypass": "不得让未 PASS Bundle 进入布局或用源副本冒充诊断候选",
        },
        {
            "affected_stages": ["P9C.3", "P9C.4", "P10", "P14"],
            "category": "QUALITY_GAP",
            "effective_gate": "G9C",
            "forward_owner": "TranslatedDiagnosticMaterializer",
            "id": "actual-glyph-and-data-relation",
            "minimum_tests": ["P9C.3-T04", "P9C.4-T04", "P9C.4-T05"],
            "prohibited_bypass": "不得以配置名、扩展名或 PDF 可打开代替实际字形和绑定证据",
        },
        {
            "affected_stages": ["P9C.4", "P9A", "P9B", "P10"],
            "category": "CLASSIFICATION_GAP",
            "effective_gate": "G9C",
            "forward_owner": "RouteCapabilityGuard",
            "id": "classification-owner-mismatch",
            "minimum_tests": ["P9C.4-T01", "P9C.4-T02"],
            "prohibited_bypass": "不得运行时改 Route、热改 Catalog 或调用其他叶私有工具",
        },
        {
            "affected_stages": ["P9C.1", "P9C.4", "P14"],
            "category": "EVIDENCE_GAP",
            "effective_gate": "G9C",
            "forward_owner": "P9CCorrectiveLedger",
            "id": "historical-summary-boundary",
            "minimum_tests": ["P9C.1-T01", "P9C.1-T03", "P9C.1-T06"],
            "prohibited_bypass": "不得用成功摘要覆盖原始失败事实或伪造历史 Gate 重验",
        },
        {
            "affected_stages": ["P9C.2", "P9C.3", "P9C.4"],
            "category": "IMPLEMENTATION_DEFECT",
            "effective_gate": "G9C",
            "forward_owner": "ToolboxPageCoordinator",
            "id": "bundle-pass-through-and-result-conflation",
            "minimum_tests": ["P9C.2-T05", "P9C.3-T05", "P9C.4-T03"],
            "prohibited_bypass": (
                "不得重复翻译已通过 unit、发布 diagnostic 或把工程 PASS 投影为产品 PASS"
            ),
        },
    )


def _schemas() -> dict[Path, dict[str, Any]]:
    """返回 P9C 四份最小且封闭的 JSON Schema。"""

    sha = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
    unit = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "container_id",
            "disposition",
            "keep_source_reason",
            "object_id",
            "ordinal",
            "owner",
            "required_literals",
            "source_hash",
            "source_text",
            "unit_id",
        ],
        "properties": {
            "container_id": {"type": "string", "minLength": 1},
            "disposition": {"enum": ["TRANSLATE", "KEEP_SOURCE", "UNRESOLVED"]},
            "keep_source_reason": {"type": ["string", "null"]},
            "object_id": {"type": "string", "minLength": 1},
            "ordinal": {"type": "integer", "minimum": 0},
            "owner": {"type": "string", "minLength": 1},
            "required_literals": {"type": "array", "items": {"type": "string"}},
            "source_hash": sha,
            "source_text": {"type": "string", "minLength": 1},
            "unit_id": {"type": "string", "minLength": 1},
        },
    }
    semantic = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "transflow.semantic-unit-map/v1",
        "type": "object",
        "additionalProperties": False,
        "required": ["entries", "map_hash", "map_id", "page_no", "schema_version", "source_hash"],
        "properties": {
            "entries": {"type": "array", "items": unit, "minItems": 1},
            "map_hash": sha,
            "map_id": {"type": "string", "minLength": 1},
            "page_no": {"type": "integer", "minimum": 1},
            "schema_version": {"const": "transflow.semantic-unit-map/v1"},
            "source_hash": sha,
        },
    }
    completeness = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "transflow.translation-completeness/v1",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "bundle_hash",
            "decision_hash",
            "dispositions",
            "errors",
            "map_hash",
            "schema_version",
            "status",
        ],
        "properties": {
            "bundle_hash": {"type": ["string", "null"], "pattern": "^[0-9a-f]{64}$"},
            "decision_hash": sha,
            "dispositions": {"type": "array", "items": {"type": "object"}},
            "errors": {"type": "array", "items": {"type": "object"}},
            "map_hash": sha,
            "schema_version": {"const": "transflow.translation-completeness/v1"},
            "status": {"enum": ["PASS", "FAIL"]},
        },
    }
    diagnostic = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "transflow.translated-diagnostic/v1",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "artifact",
            "bundle_hash",
            "decision_hash",
            "evidence",
            "map_hash",
            "page_no",
            "schema_version",
            "status",
        ],
        "properties": {
            "artifact": {"type": ["object", "null"]},
            "bundle_hash": {"type": ["string", "null"], "pattern": "^[0-9a-f]{64}$"},
            "decision_hash": {"type": ["string", "null"], "pattern": "^[0-9a-f]{64}$"},
            "evidence": {"type": "object"},
            "map_hash": {"type": ["string", "null"], "pattern": "^[0-9a-f]{64}$"},
            "page_no": {"type": ["integer", "null"], "minimum": 1},
            "schema_version": {"const": "transflow.translated-diagnostic/v1"},
            "status": {
                "enum": [
                    "TRANSLATED_DIAGNOSTIC_READY",
                    "DIAGNOSTIC_MATERIALIZATION_FAILED",
                    "NO_TRANSLATED_CANDIDATE",
                ]
            },
        },
    }
    axes = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "transflow.three-axis-result/v1",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "engineering_closure",
            "product_acceptance",
            "promotion_eligibility",
            "reasons",
            "schema_version",
            "scope_id",
            "scope_type",
        ],
        "properties": {
            "engineering_closure": {"enum": ["PASS", "FAIL"]},
            "product_acceptance": {"enum": ["PASS", "FAIL", "NOT_EVALUATED"]},
            "promotion_eligibility": {"enum": ["ELIGIBLE", "INELIGIBLE", "BLOCKED"]},
            "reasons": {"type": "array", "items": {"type": "string"}},
            "schema_version": {"const": "transflow.three-axis-result/v1"},
            "scope_id": {"type": "string", "minLength": 1},
            "scope_type": {"enum": ["PAGE", "DOCUMENT", "STAGE"]},
        },
    }
    return {
        SCHEMA_ROOT / "semantic_unit_map_v1.schema.json": semantic,
        SCHEMA_ROOT / "translation_completeness_v1.schema.json": completeness,
        SCHEMA_ROOT / "translated_diagnostic_v1.schema.json": diagnostic,
        SCHEMA_ROOT / "three_axis_result_v1.schema.json": axes,
    }


def build_assets() -> dict[Path, bytes]:
    """构建全部 P9C 确定性资源，但不写入文件。"""

    anchor = json.loads(ANCHOR_PATH.read_text(encoding="utf-8"))
    if anchor.get("schema_version") != "transflow.p9c-historical-anchor/v1":
        raise RuntimeError("invalid_p9c_historical_anchor")
    anchor_commit = str(anchor["anchor_commit"])
    inventory = _inventory(
        anchor_commit,
        _anchor_time(anchor_commit),
        {
            str(key): str(value)
            for key, value in anchor.get("stage_start_source_hashes", {}).items()
        },
        str(anchor.get("external_capture_time", "")),
    )
    historical_change_count = sum(
        item["anchor_sha256"] != item["current_sha256"] for item in inventory
    )
    ledger_core: dict[str, Any] = {
        "anchor_commit": anchor_commit,
        "contradiction_register": _contradictions(),
        "historical_change_count": historical_change_count,
        "historical_facts": _historical_facts(),
        "historical_gate_reexecution_count": 0,
        "impact_matrix": _impact_matrix(),
        "schema_version": "transflow.p9c-corrective-ledger/v1",
        "source_inventory": inventory,
    }
    ledger = dict(ledger_core)
    ledger["ledger_hash"] = _sha256_bytes(_canonical_bytes(ledger_core))
    outputs = {LEDGER_PATH: _canonical_bytes(ledger) + b"\n"}
    for path, schema in _schemas().items():
        outputs[path] = _canonical_bytes(schema) + b"\n"
    fingerprints = {
        "resources": {
            _relative(path): _sha256_bytes(content) for path, content in sorted(outputs.items())
        },
        "schema_version": "transflow.p9c-resource-fingerprints/v1",
    }
    outputs[FINGERPRINT_PATH] = _canonical_bytes(fingerprints) + b"\n"
    return outputs


def capture_external_anchor() -> int:
    """为全部历史来源记录本阶段开始时工作树哈希。"""

    anchor = json.loads(ANCHOR_PATH.read_text(encoding="utf-8"))
    anchor_commit = str(anchor["anchor_commit"])
    external: dict[str, str] = {}
    stage_start: dict[str, str] = {}
    for paths in _source_groups().values():
        for path in paths:
            relative_path = _relative(path)
            if not path.is_file():
                raise RuntimeError(f"missing_stage_start_source:{relative_path}")
            stage_start[relative_path] = _sha256_file(path)
            if _git_bytes(anchor_commit, relative_path) is None:
                external[relative_path] = _sha256_file(path)
    # 使用宿主机已配置的本地时区，避免 Windows 缺少 IANA tzdata 时基线捕获失败。
    anchor["external_capture_time"] = datetime.now().astimezone().isoformat(
        timespec="seconds"
    )
    anchor["external_source_hashes"] = dict(sorted(external.items()))
    anchor["stage_start_source_hashes"] = dict(sorted(stage_start.items()))
    ANCHOR_PATH.write_text(
        json.dumps(anchor, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        "P9C_EXTERNAL_ANCHOR PASS "
        f"source_count={len(stage_start)} external_source_count={len(external)} "
        f"anchor_commit={anchor_commit}"
    )
    return 0


def write_or_check(*, check: bool) -> int:
    """写入资源或逐字节检查现有资源是否可复算。"""

    outputs = build_assets()
    drift: list[str] = []
    for path, content in outputs.items():
        if check:
            if not path.is_file() or path.read_bytes() != content:
                drift.append(_relative(path))
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    if drift:
        print(f"P9C_ASSETS FAIL drift_count={len(drift)} paths={','.join(drift)}")
        return 1
    ledger = json.loads(outputs[LEDGER_PATH].decode("utf-8"))
    print(
        "P9C_ASSETS PASS "
        f"inventory_count={len(ledger['source_inventory'])} "
        f"historical_change_count={ledger['historical_change_count']} "
        f"ledger_hash={ledger['ledger_hash']} schema_count={len(_schemas())}"
    )
    return 0


def main() -> int:
    """解析命令行并构建或复核 P9C 资源。"""

    parser = argparse.ArgumentParser(description="构建 P9C 纠偏资源")
    parser.add_argument("--check", action="store_true", help="只检查，不写入")
    parser.add_argument(
        "--capture-external-anchor",
        action="store_true",
        help="记录冻结提交未跟踪来源的阶段开始哈希",
    )
    arguments = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    LOGGER.info("调用 P9C 资源构建，意图=冻结历史账本与横切合同 check=%s", arguments.check)
    if arguments.capture_external_anchor:
        return capture_external_anchor()
    return write_or_check(check=arguments.check)


if __name__ == "__main__":
    raise SystemExit(main())
