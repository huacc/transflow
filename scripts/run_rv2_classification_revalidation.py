"""冻结 RV2 真值叠加层并审计当前工程分类规则。"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

from transflow.classification.decision_adapter import BoundedDecisionRunner
from transflow.classification.engine import ClassificationEngine
from transflow.classification.evidence import build_evidence
from transflow.classification.rules import decide_rule, uses_direct_table_evidence
from transflow.domain.classification import ModelDecision, ModelDecisionRequest
from transflow.pdf_kernel.facts import PageFactsExtractor

REPO_ROOT = Path(__file__).resolve().parent.parent
GOLD_ROOT = REPO_ROOT / "spikes" / "page_classification_engine_puncture_v1" / "分类结果"
DEEP_AUDIT = (
    REPO_ROOT
    / "spikes"
    / "page_classification_engine_puncture_v1"
    / "reports"
    / "deep_audits"
    / "current_classification_20260711"
    / "page_audit.jsonl"
)
SOURCE_MANIFEST = (
    REPO_ROOT
    / "spikes"
    / "page_classification_engine_puncture_v1"
    / "manifests"
    / "sample2_source_manifest.jsonl"
)
RV0_SOURCE = (
    REPO_ROOT
    / "runs"
    / "critical_chain_revalidation"
    / "RV0"
    / "01-baseline-20260721-164419"
    / "input"
    / "source_document.pdf"
)
P5_BASELINE = REPO_ROOT / "resources" / "manifests" / "p5_anonymous_baseline.json"
P5_ANSWER_KEY = REPO_ROOT / "tests" / "migration" / "classification_answer_key.json"
P5_SAMPLE_ROOT = (
    REPO_ROOT / "spikes" / "page_classification_engine_puncture_v1" / "样本1"
)
DEFAULT_RUN_ID = "01-current-validity-20260721-233029"
RUN_ID = DEFAULT_RUN_ID
RUN_ROOT = REPO_ROOT / "runs" / "critical_chain_revalidation" / "RV2" / RUN_ID
BLIND_PER_ROUTE = 2
HIGH_CONFIDENCE = 0.9
BLIND_SALT = "transflow-rv2-blind-v1"

# 这些页面已在冻结前用于快速探针，不能再冒充盲测。
PRE_FREEZE_INSPECTED = {
    "AB_EN_01_00050_p060",
    "00050_HK FERRY (HOLD)_英文_2025_p011_body_chart",
    "EN_03_03988_p0010",
    "00050_HK FERRY (HOLD)_英文_2025_p011_body_composite_chart_table",
    "EN_01_00050_p0077",
    "EN_01_03988_p0101",
    "S2P0055",
    "00295_江山控股_中文_2025_p074_body_diagram",
    "S2P0168",
    "S2P0043",
    "EN_00468_p0010",
    "00005_2025_interim_report_zh_p001_body_table",
    "S2P0042",
    "S2P0001",
    "S2P0020",
    "S2P0080",
    "S2P0151",
}


def now_iso() -> str:
    """返回带时区的秒级时间。"""

    return datetime.now().astimezone().isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    """流式计算文件哈希。"""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_hash(value: object) -> str:
    """计算稳定 JSON 哈希。"""

    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def write_json(path: Path, value: object) -> None:
    """写入稳定、可人工阅读的 JSON。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


def read_json(path: Path) -> Any:
    """读取 UTF-8 JSON。"""

    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """读取非空 JSONL 记录。"""

    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def dotted_route(value: str) -> str:
    """把目录式 Route 统一为生产点分形式。"""

    return value.replace("\\", ".").replace("/", ".")


def source_document_ids() -> dict[str, str]:
    """读取旧 S2P 页面对应的真实来源文档哈希。"""

    return {
        str(item["sample_id"]): str(item["source_sha256"])
        for item in read_jsonl(SOURCE_MANIFEST)
    }


def inferred_document_id(stem: str, known: dict[str, str]) -> str:
    """只为盲测去重推导来源，不把文件名交给分类器。"""

    if stem in known:
        return f"source-sha256:{known[stem]}"
    stock = re.search(r"(?<!\d)(\d{5})(?!\d)", stem)
    language = "zh" if "中文" in stem or stem.startswith("ZH_") else "en"
    year = re.search(r"20\d{2}", stem)
    if stock:
        return f"named:{stock.group(1)}:{language}:{year.group(0) if year else 'unknown'}"
    return f"named:{re.sub(r'_p\d+.*$', '', stem, flags=re.I)}"


def expected_nodes(route: str) -> dict[str, str]:
    """把一个叶 Route 展开成待独立核验的节点答案。"""

    parts = route.split(".")
    if parts[0] != "body":
        return {"page.role": parts[0]}
    result = {"page.role": "body", "body.layout_owner": parts[1]}
    if parts[1] == "flow_text":
        result["body.flow.topology"] = parts[2]
    elif parts[1] == "composite":
        result["body.composite.kind"] = parts[2]
    return result


def freeze() -> int:
    """冻结目录标签、人工深审修正、冲突排除和盲测划分。"""

    manifest_path = RUN_ROOT / "input" / "gold_manifest.json"
    if manifest_path.exists():
        raise RuntimeError("RV2 真值已经冻结")
    audit_by_stem = {str(item["sample_id"]): item for item in read_jsonl(DEEP_AUDIT)}
    known_documents = source_document_ids()
    raw_cases: list[dict[str, Any]] = []
    for path in sorted(GOLD_ROOT.rglob("*.pdf")):
        folder_route = ".".join(path.parent.relative_to(GOLD_ROOT).parts)
        audit = audit_by_stem.get(path.stem)
        if audit and audit["audit_status"] == "AMBIGUOUS":
            expected_route = None
            provenance = "deep_audit_ambiguous"
        elif audit and audit["audit_status"] == "ERROR":
            expected_route = dotted_route(str(audit["suggested_leaf"]))
            provenance = "deep_audit_corrected"
        elif audit:
            expected_route = dotted_route(str(audit["suggested_leaf"]))
            provenance = "deep_audit_confirmed"
        else:
            expected_route = folder_route
            provenance = "user_confirmed_toolbox_set"
        content_hash = sha256_file(path)
        raw_cases.append(
            {
                "content_sha256": content_hash,
                "expected_route": expected_route,
                "folder_route": folder_route,
                "gold_provenance": provenance,
                "path": path.relative_to(REPO_ROOT).as_posix(),
                "sample_id": path.stem,
                "size_bytes": path.stat().st_size,
                "source_document_id": inferred_document_id(path.stem, known_documents),
            }
        )

    by_hash: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in raw_cases:
        by_hash[str(item["content_sha256"])].append(item)

    exclusions: list[dict[str, Any]] = []
    cases: list[dict[str, Any]] = []
    for content_hash, duplicates in sorted(by_hash.items()):
        expected = {item["expected_route"] for item in duplicates if item["expected_route"]}
        ambiguous = [item for item in duplicates if item["expected_route"] is None]
        if ambiguous:
            exclusions.append(
                {
                    "content_sha256": content_hash,
                    "reason": "DEEP_AUDIT_AMBIGUOUS",
                    "files": duplicates,
                }
            )
            continue
        if len(expected) != 1:
            exclusions.append(
                {
                    "content_sha256": content_hash,
                    "reason": "GOLD_ROUTE_CONFLICT",
                    "candidate_routes": sorted(str(route) for route in expected),
                    "files": duplicates,
                }
            )
            continue
        representative = min(duplicates, key=lambda item: str(item["path"]))
        cases.append(
            {
                **representative,
                "aliases": sorted(str(item["path"]) for item in duplicates),
                "case_id": f"rv2-{content_hash[:16]}",
                "evaluation_role": "calibration",
            }
        )

    selected_blind: set[str] = set()
    blind_shortfalls: list[dict[str, Any]] = []
    by_route: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for case in cases:
        by_route[str(case["expected_route"])].append(case)
    for route, route_cases in sorted(by_route.items()):
        candidates = [
            item for item in route_cases if str(item["sample_id"]) not in PRE_FREEZE_INSPECTED
        ]
        candidates.sort(
            key=lambda item: hashlib.sha256(
                f"{BLIND_SALT}\0{item['content_sha256']}".encode()
            ).hexdigest()
        )
        used_documents: set[str] = set()
        for item in candidates:
            document_id = str(item["source_document_id"])
            if document_id in used_documents:
                continue
            selected_blind.add(str(item["case_id"]))
            used_documents.add(document_id)
            if len(used_documents) == BLIND_PER_ROUTE:
                break
        if len(used_documents) < BLIND_PER_ROUTE:
            blind_shortfalls.append(
                {
                    "route": route,
                    "required": BLIND_PER_ROUTE,
                    "selected_distinct_documents": len(used_documents),
                }
            )
    for case in cases:
        if str(case["case_id"]) in selected_blind:
            case["evaluation_role"] = "blind"

    cases.sort(key=lambda item: (str(item["expected_route"]), str(item["case_id"])))
    route_counts = Counter(str(item["expected_route"]) for item in cases)
    blind_counts = Counter(
        str(item["expected_route"]) for item in cases if item["evaluation_role"] == "blind"
    )
    manifest = {
        "schema_version": "transflow.rv2-gold-overlay/v1",
        "run_id": RUN_ID,
        "frozen_at": now_iso(),
        "source_file_count": len(raw_cases),
        "unique_content_count": len(by_hash),
        "eligible_case_count": len(cases),
        "excluded_content_count": len(exclusions),
        "blind_case_count": len(selected_blind),
        "blind_per_route": BLIND_PER_ROUTE,
        "route_counts": dict(sorted(route_counts.items())),
        "blind_route_counts": dict(sorted(blind_counts.items())),
        "blind_shortfalls": blind_shortfalls,
        "input_lock": "repository_relative_path + size_bytes + sha256",
        "gold_precedence": [
            "人工深度审计明确修正",
            "人工深度审计明确确认",
            "用户确认的新增 Toolbox 分类集",
        ],
        "cases": cases,
    }
    thresholds = {
        "schema_version": "transflow.rv2-threshold-freeze/v1",
        "frozen_at": manifest["frozen_at"],
        "result_count_at_freeze": 0,
        "blind_per_route_min": BLIND_PER_ROUTE,
        "known_counterexample_accuracy_min": 1.0,
        "blind_route_accuracy_min": 1.0,
        "high_confidence_rule_conflict_max": 0,
        "identity_special_case_max": 0,
        "no_route_state_max": 0,
        "perturbation_invariance_min": 1.0,
        "fresh_model_repetitions_per_boundary": 3,
        "unresolved_gold_policy": "EXCLUDE_AND_MARK_EVIDENCE_INSUFFICIENT",
        "threshold_change_policy": "FORBIDDEN_AFTER_RESULTS_WITHOUT_DECISION_RECORD",
    }
    snapshots = {}
    for relative in (
        "src/transflow/classification/evidence.py",
        "src/transflow/classification/rules.py",
        "src/transflow/classification/engine.py",
        "resources/prompts/classification/body_layout_owner/decide.zh-CN.md",
        "resources/prompts/classification/body_composite_kind/decide.zh-CN.md",
        "resources/taxonomy/page_classification_routes_v1.json",
        "resources/catalogs/page_toolbox_catalog_v4.json",
    ):
        path = REPO_ROOT / relative
        snapshots[relative] = {"sha256": sha256_file(path), "size_bytes": path.stat().st_size}
    write_json(manifest_path, manifest)
    write_json(RUN_ROOT / "input" / "thresholds.json", thresholds)
    write_json(
        RUN_ROOT / "input" / "gold_exclusions.json",
        {
            "schema_version": "transflow.rv2-gold-exclusions/v1",
            "count": len(exclusions),
            "reason_counts": dict(Counter(item["reason"] for item in exclusions)),
            "items": exclusions,
        },
    )
    write_json(RUN_ROOT / "input" / "code_snapshot_before_fix.json", snapshots)
    return 0


def audit_case(case: dict[str, Any]) -> dict[str, Any]:
    """只用匿名页面事实审计规则节点，不调用或伪造模型。"""

    path = REPO_ROOT / str(case["path"])
    actual_hash = sha256_file(path)
    if actual_hash != case["content_sha256"]:
        raise RuntimeError(f"冻结输入漂移:{case['case_id']}")
    facts = PageFactsExtractor().extract_page(
        path,
        actual_hash,
        1,
        include_classification=True,
    )
    evidence = build_evidence(facts, 1)
    answers = expected_nodes(str(case["expected_route"]))
    decisions: dict[str, Any] = {}
    for node_key, expected_child in answers.items():
        judgement = decide_rule(node_key, evidence)
        conflict = judgement.status == "DECIDED" and judgement.selected_child != expected_child
        decisions[node_key] = {
            "expected_child": expected_child,
            "status": judgement.status,
            "selected_child": judgement.selected_child,
            "confidence": judgement.confidence,
            "reason_summary": judgement.reason_summary,
            "rule_conflict": conflict,
            "high_confidence_conflict": conflict and judgement.confidence >= HIGH_CONFIDENCE,
            "model_skip_direct_evidence": uses_direct_table_evidence(judgement),
        }
    return {
        "case_id": case["case_id"],
        "expected_route": case["expected_route"],
        "gold_provenance": case["gold_provenance"],
        "page_identity": facts.page_identity,
        "kernel_facts_hash": facts.kernel_facts_hash,
        "evidence_hash": canonical_hash(
            {key: value for key, value in evidence.items() if key != "page_image"}
        ),
        "decisions": decisions,
    }


def historical_cases() -> list[dict[str, Any]]:
    """按内容哈希恢复旧 22 例，答案与运行时输入保持分离。"""

    baseline = read_json(P5_BASELINE)
    answers = {
        str(item["case_key"]): str(item["route"])
        for item in read_json(P5_ANSWER_KEY)["answers"]
    }
    paths_by_hash: dict[str, Path] = {}
    for path in sorted(P5_SAMPLE_ROOT.glob("*.pdf")):
        paths_by_hash.setdefault(sha256_file(path), path)
    cases = []
    for item in baseline["cases"]:
        content_hash = str(item["content_sha256"])
        matched_path = paths_by_hash.get(content_hash)
        if matched_path is None:
            raise RuntimeError(f"历史匿名输入缺失:{content_hash[:12]}")
        case_key = str(item["case_key"])
        cases.append(
            {
                "case_id": case_key,
                "content_sha256": content_hash,
                "expected_route": answers[case_key],
                "gold_provenance": "p5_sealed_answer_key",
                "path": matched_path.relative_to(REPO_ROOT).as_posix(),
            }
        )
    return cases


class HashingDecisionPort:
    """只保存真实模型请求和归一化响应哈希，不落盘正文或秘密。"""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.records: list[dict[str, Any]] = []

    def decide(self, request: ModelDecisionRequest) -> ModelDecision:
        request_hash = canonical_hash(
            {
                "allowed_actions": request.allowed_actions,
                "decision_id": request.decision_id,
                "decision_kind": request.decision_kind,
                "node_spec": request.node_spec,
                "prompt_version": request.prompt_version,
                "schema_version": request.schema_version,
                "typed_evidence": request.typed_evidence,
            }
        )
        decision = self._inner.decide(request)
        response_hash = canonical_hash(
            {
                "confidence": decision.confidence,
                "decision_id": decision.decision_id,
                "decision_kind": decision.decision_kind,
                "evidence_ids": decision.evidence_ids,
                "reason_summary": decision.reason_summary,
                "result_code": decision.result_code,
            }
        )
        self.records.append(
            {
                "node_key": str(request.node_spec["node_key"]),
                "request_hash": request_hash,
                "response_hash": response_hash,
                "stage": str(request.node_spec["stage"]),
            }
        )
        return decision


def live_cases(evaluation_set: str) -> list[dict[str, Any]]:
    """返回真实模型运行所需的冻结案例，不把答案放进模型证据。"""

    if evaluation_set == "historical":
        cases = historical_cases()
    elif evaluation_set == "blind":
        manifest = read_json(RUN_ROOT / "input" / "gold_manifest.json")
        cases = [
            item for item in manifest["cases"] if item["evaluation_role"] == "blind"
        ]
    elif evaluation_set == "p0151":
        cases = [
            {
                "case_id": "krv-p0151",
                "content_sha256": sha256_file(RV0_SOURCE),
                "expected_route": "body.composite.flow_text_table",
                "gold_provenance": "rv2_frozen_known_counterexample",
                "page_count": 187,
                "page_no": 151,
                "path": RV0_SOURCE.relative_to(REPO_ROOT).as_posix(),
            }
        ]
    else:
        raise ValueError(f"真实模型集合不受支持:{evaluation_set}")
    return cases


def live(
    label: str,
    evaluation_set: str,
    repetitions: int,
    evidence_role: str | None = None,
    case_ids: tuple[str, ...] = (),
) -> int:
    """执行真实模型分类，并仅保存路由及请求/响应哈希。"""

    from tests.migration.qwen_adapter import (
        MigrationQwenDecisionAdapter,
        migration_environment_ready,
    )

    if not migration_environment_ready():
        raise RuntimeError("真实迁移模型环境变量未配置；禁止生成伪造的 RV2 live 结果")
    if repetitions < 1:
        raise ValueError("真实模型重复次数必须为正数")
    cases = live_cases(evaluation_set)
    if case_ids:
        requested = set(case_ids)
        cases = [case for case in cases if str(case["case_id"]) in requested]
        missing = requested - {str(case["case_id"]) for case in cases}
        if missing:
            raise ValueError(f"case-id 不属于所选集合:{sorted(missing)}")
    results = []
    actual_model_call_count = 0
    for repetition in range(1, repetitions + 1):
        for case in cases:
            path = REPO_ROOT / str(case["path"])
            content_hash = sha256_file(path)
            if content_hash != case["content_sha256"]:
                raise RuntimeError(f"冻结输入漂移:{case['case_id']}")
            facts = PageFactsExtractor().extract_page(
                path,
                content_hash,
                int(case.get("page_no", 1)),
                include_classification=True,
            )
            adapter = MigrationQwenDecisionAdapter()
            port = HashingDecisionPort(adapter)
            classified = ClassificationEngine(BoundedDecisionRunner(port)).classify_page(
                facts,
                int(case.get("page_count", 1)),
            )
            actual_model_call_count += adapter.call_count
            model_failure_codes = sorted(
                {
                    judgement.reason_summary
                    for resolution in classified.resolutions
                    for judgement in (resolution.primary, resolution.review)
                    if judgement is not None
                    and judgement.reason_summary.startswith("model_failure:")
                }
            )
            results.append(
                {
                    "case_id": case["case_id"],
                    "expected_route": case["expected_route"],
                    "failed_node": classified.route.failed_node,
                    "model_call_attempt_count": adapter.call_count,
                    "model_call_success_count": len(port.records),
                    "model_failure_codes": model_failure_codes,
                    "model_calls": port.records,
                    "predicted_route": classified.route.route,
                    "repetition": repetition,
                    "route_matches": classified.route.route == case["expected_route"],
                }
            )
    write_json(
        RUN_ROOT / "process" / f"{label}.json",
        {
            "schema_version": "transflow.rv2-live-classification/v1",
            "actual_model_call_count": actual_model_call_count,
            "case_run_count": len(results),
            "evaluation_set": evidence_role or evaluation_set,
            "source_evaluation_set": evaluation_set,
            "fake_result_count": 0,
            "repetitions": repetitions,
            "selected_case_ids": list(case_ids),
            "route_accuracy": sum(item["route_matches"] for item in results)
            / max(len(results), 1),
            "results": results,
            "run_id": RUN_ID,
        },
    )
    return 0


def audit(label: str, evaluation_set: str, workers: int) -> int:
    """运行指定冻结分组并输出规则冲突清单。"""

    if workers != 1:
        raise ValueError("PageFacts/PyMuPDF 提取必须单线程；并发只允许发生在事实冻结之后")
    manifest = read_json(RUN_ROOT / "input" / "gold_manifest.json")
    if evaluation_set == "historical":
        cases = historical_cases()
    elif evaluation_set == "all":
        cases = list(manifest["cases"])
    else:
        cases = [
            item
            for item in manifest["cases"]
            if item["evaluation_role"] == evaluation_set
        ]
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        results = list(pool.map(audit_case, cases))
    results.sort(key=lambda item: str(item["case_id"]))
    node_total = 0
    decided = 0
    correct = 0
    conflicts: list[dict[str, Any]] = []
    high_conflicts: list[dict[str, Any]] = []
    unsafe_model_skips: list[dict[str, Any]] = []
    by_route: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for item in results:
        route = str(item["expected_route"])
        by_route[route]["case_count"] += 1
        for node_key, decision in item["decisions"].items():
            node_total += 1
            by_route[route]["node_count"] += 1
            if decision["status"] == "DECIDED":
                decided += 1
                by_route[route]["decided_count"] += 1
                if not decision["rule_conflict"]:
                    correct += 1
                    by_route[route]["correct_decided_count"] += 1
            if decision["rule_conflict"]:
                finding = {
                    "case_id": item["case_id"],
                    "expected_route": route,
                    "node_key": node_key,
                    **decision,
                }
                conflicts.append(finding)
                if decision["high_confidence_conflict"]:
                    high_conflicts.append(finding)
                if decision["model_skip_direct_evidence"]:
                    unsafe_model_skips.append(finding)
    summary = {
        "schema_version": "transflow.rv2-rule-audit/v1",
        "run_id": RUN_ID,
        "label": label,
        "evaluation_set": evaluation_set,
        "case_count": len(results),
        "node_count": node_total,
        "decided_node_count": decided,
        "correct_decided_node_count": correct,
        "rule_conflict_count": len(conflicts),
        "high_confidence_rule_conflict_count": len(high_conflicts),
        "unsafe_model_skip_count": len(unsafe_model_skips),
        "by_route": {route: dict(values) for route, values in sorted(by_route.items())},
        "conflicts": conflicts,
        "high_confidence_conflicts": high_conflicts,
        "unsafe_model_skips": unsafe_model_skips,
        "cases": results,
    }
    write_json(RUN_ROOT / "process" / f"{label}.json", summary)
    return 0


def main() -> int:
    """解析 RV2 冻结或规则审计阶段。"""

    global RUN_ID, RUN_ROOT

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("freeze", "audit", "live"))
    parser.add_argument("--label", default="rule-audit")
    parser.add_argument(
        "--set",
        choices=("calibration", "blind", "historical", "p0151", "all"),
        default="calibration",
    )
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--run-id", default=DEFAULT_RUN_ID)
    parser.add_argument(
        "--evidence-role",
        choices=("current_validity", "exposed_regression"),
    )
    parser.add_argument("--case-id", action="append", default=[])
    args = parser.parse_args()
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", args.run_id) is None:
        raise ValueError("run-id 只能包含字母、数字、点、下划线和连字符")
    RUN_ID = args.run_id
    RUN_ROOT = REPO_ROOT / "runs" / "critical_chain_revalidation" / "RV2" / RUN_ID
    if args.phase == "freeze":
        return freeze()
    if args.phase == "live":
        return live(
            args.label,
            args.set,
            args.repetitions,
            args.evidence_role,
            tuple(args.case_id),
        )
    if args.set == "p0151":
        raise ValueError("规则审计不接受 p0151 集合；请运行 live 或针对性回归")
    return audit(args.label, args.set, args.workers)


if __name__ == "__main__":
    raise SystemExit(main())
