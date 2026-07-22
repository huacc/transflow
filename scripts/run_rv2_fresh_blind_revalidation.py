"""冻结、匿名运行并解封计分 RV2 新严格盲测集。"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from scripts import run_rv2_classification_revalidation as rv2
from transflow.classification.decision_adapter import BoundedDecisionRunner
from transflow.classification.engine import ClassificationEngine
from transflow.pdf_kernel.facts import PageFactsExtractor

REPO_ROOT = Path(__file__).resolve().parent.parent
RV2_ROOT = REPO_ROOT / "runs" / "critical_chain_revalidation" / "RV2"
SOURCE_RUN_ID = "02-live-replay-20260722-081513"
SOURCE_MANIFEST = RV2_ROOT / SOURCE_RUN_ID / "input" / "gold_manifest.json"
RV3_ROUTE_AUDIT = (
    REPO_ROOT
    / "runs"
    / "critical_chain_revalidation"
    / "RV3"
    / "02-routing-catalog-20260722-012551"
    / "process"
    / "route_catalog_audit.json"
)
FRESH_SALT = "transflow-rv2-fresh-blind-v2-20260722"
CASES_PER_ROUTE = 2
BATCH_SIZE = 8
SNAPSHOT_PATHS = (
    "resources/catalogs/page_toolbox_catalog_v4.json",
    "resources/prompts/classification/body_composite_kind/decide.zh-CN.md",
    "resources/prompts/classification/body_composite_kind/review.zh-CN.md",
    "resources/prompts/classification/body_flow_topology/decide.zh-CN.md",
    "resources/prompts/classification/body_flow_topology/review.zh-CN.md",
    "resources/prompts/classification/body_layout_owner/decide.zh-CN.md",
    "resources/prompts/classification/body_layout_owner/review.zh-CN.md",
    "resources/prompts/classification/page_role/decide.zh-CN.md",
    "resources/prompts/classification/page_role/review.zh-CN.md",
    "resources/taxonomy/page_classification_routes_v1.json",
    "scripts/run_rv2_classification_revalidation.py",
    "scripts/run_rv2_fresh_blind_revalidation.py",
    "spikes/page_classification_engine_puncture_v1/src/page_classifier/evidence.py",
    "spikes/page_classification_engine_puncture_v1/src/page_classifier/rules.py",
    "src/transflow/classification/decision_adapter.py",
    "src/transflow/classification/engine.py",
    "src/transflow/classification/evidence.py",
    "src/transflow/classification/rules.py",
    "tests/migration/qwen_adapter.py",
    "tests/test_critical_chain_rv2.py",
    "tests/test_p5.py",
    "tests/test_rv2_fresh_blind.py",
)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _rank(salt: str, case: dict[str, Any], purpose: str) -> str:
    payload = f"{salt}\0{purpose}\0{case['content_sha256']}".encode()
    return hashlib.sha256(payload).hexdigest()


def select_stratified_cases(
    candidates: list[dict[str, Any]],
    salt: str,
    cases_per_route: int,
) -> list[dict[str, Any]]:
    """按类别稳定取样，优先让整套集合也来自不同源文档。"""

    if cases_per_route < 1:
        raise ValueError("每类样本数必须为正数")
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for case in candidates:
        grouped[str(case["expected_route"])].append(case)
    selected: list[dict[str, Any]] = []
    globally_used_documents: set[str] = set()
    for route in sorted(grouped):
        ranked = sorted(grouped[route], key=lambda case: _rank(salt, case, "select"))
        route_selected: list[dict[str, Any]] = []
        route_documents: set[str] = set()
        for require_global_distinct in (True, False):
            for case in ranked:
                document = str(case["source_document_id"])
                if document in route_documents:
                    continue
                if require_global_distinct and document in globally_used_documents:
                    continue
                route_selected.append(case)
                route_documents.add(document)
                globally_used_documents.add(document)
                if len(route_selected) == cases_per_route:
                    break
            if len(route_selected) == cases_per_route:
                break
        if len(route_selected) != cases_per_route:
            raise ValueError(f"类别缺少独立文档样本:{route}")
        selected.extend(route_selected)
    return sorted(selected, key=lambda case: _rank(salt, case, "public-order"))


def build_public_cases(selected: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """移除 gold 与源身份，只公开匿名冻结副本。"""

    public = []
    for index, case in enumerate(selected, start=1):
        case_id = f"fresh-{index:03d}"
        public.append(
            {
                "case_id": case_id,
                "content_sha256": case["content_sha256"],
                "page_count": 1,
                "page_no": 1,
                "path": f"input/pages/{case_id}.pdf",
                "size_bytes": case["size_bytes"],
            }
        )
    return public


def build_audit_case(
    run_root: Path,
    public_case: dict[str, Any],
    answer: dict[str, Any],
    repo_root: Path = REPO_ROOT,
) -> dict[str, Any]:
    """把轮次内匿名路径转换为旧审计器要求的仓库相对路径。"""

    absolute_path = (run_root / str(public_case["path"])).resolve()
    relative_path = absolute_path.relative_to(repo_root.resolve()).as_posix()
    return {
        **public_case,
        "path": relative_path,
        "expected_route": answer["expected_route"],
        "gold_provenance": answer["gold_provenance"],
    }


def evaluate_predictions(
    public_cases: list[dict[str, Any]],
    answer_key: dict[str, str],
    predictions: list[dict[str, Any]],
) -> dict[str, Any]:
    """在模型运行完成后解封答案，并拒绝缺失或重复预测。"""

    public_ids = [str(case["case_id"]) for case in public_cases]
    predicted_ids = [str(item["case_id"]) for item in predictions]
    if (
        len(predicted_ids) != len(set(predicted_ids))
        or set(predicted_ids) != set(public_ids)
        or set(answer_key) != set(public_ids)
    ):
        raise ValueError("预测 case 不完整或重复")
    by_id = {str(item["case_id"]): item for item in predictions}
    results = []
    failures = []
    by_route: dict[str, dict[str, int]] = defaultdict(lambda: {"total": 0, "passed": 0})
    for case_id in public_ids:
        predicted = str(by_id[case_id]["predicted_route"])
        expected = answer_key[case_id]
        matches = predicted == expected
        by_route[expected]["total"] += 1
        by_route[expected]["passed"] += int(matches)
        result = {
            "case_id": case_id,
            "expected_route": expected,
            "predicted_route": predicted,
            "route_matches": matches,
        }
        results.append(result)
        if not matches:
            failures.append({key: result[key] for key in result if key != "route_matches"})
    passed = sum(int(item["route_matches"]) for item in results)
    return {
        "case_count": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "route_accuracy": passed / max(len(results), 1),
        "by_route": dict(sorted(by_route.items())),
        "failures": failures,
        "results": results,
    }


def _snapshot() -> dict[str, dict[str, Any]]:
    files = {}
    for relative in SNAPSHOT_PATHS:
        path = REPO_ROOT / relative
        files[relative] = {
            "sha256": rv2.sha256_file(path),
            "size_bytes": path.stat().st_size,
        }
    return files


def _verify_snapshot(run_root: Path) -> None:
    frozen = read_json(run_root / "input" / "code_prompt_snapshot.json")["files"]
    current = _snapshot()
    drift = sorted(relative for relative, value in frozen.items() if current.get(relative) != value)
    if drift:
        raise RuntimeError(f"冻结后代码或 Prompt 漂移:{drift}")


def summarize_rule_audit(results: list[dict[str, Any]]) -> dict[str, int]:
    """沿用 RV2 主审计口径，只把冲突且跳模的直判计为危险。"""

    node_count = 0
    conflict_count = 0
    high_conflict_count = 0
    unsafe_skip_count = 0
    for item in results:
        for decision in item["decisions"].values():
            node_count += 1
            conflict = bool(decision["rule_conflict"])
            conflict_count += int(conflict)
            high_conflict_count += int(decision["high_confidence_conflict"])
            unsafe_skip_count += int(conflict and decision["model_skip_direct_evidence"])
    return {
        "node_count": node_count,
        "rule_conflict_count": conflict_count,
        "high_confidence_rule_conflict_count": high_conflict_count,
        "unsafe_model_skip_count": unsafe_skip_count,
    }


def _excluded_documents(source_cases: list[dict[str, Any]]) -> tuple[set[str], dict[str, int]]:
    old_blind_documents = {
        str(case["source_document_id"])
        for case in source_cases
        if case["evaluation_role"] == "blind"
    }
    inspected_documents = {
        str(case["source_document_id"])
        for case in source_cases
        if str(case["sample_id"]) in rv2.PRE_FREEZE_INSPECTED
    }
    rv3_hashes = {
        str(item["source_sha256"])
        for item in read_json(RV3_ROUTE_AUDIT)["representative_samples"]
    }
    rv3_documents = {
        str(case["source_document_id"])
        for case in source_cases
        if str(case["content_sha256"]) in rv3_hashes
    }
    historical_hashes = {
        str(case["content_sha256"]) for case in rv2.live_cases("historical")
    }
    historical_documents = {
        str(case["source_document_id"])
        for case in source_cases
        if str(case["content_sha256"]) in historical_hashes
    }
    excluded = (
        old_blind_documents | inspected_documents | rv3_documents | historical_documents
    )
    return excluded, {
        "old_blind_document_count": len(old_blind_documents),
        "pre_freeze_inspected_document_count": len(inspected_documents),
        "rv3_representative_document_count": len(rv3_documents),
        "historical_overlap_document_count": len(historical_documents),
        "excluded_document_union_count": len(excluded),
    }


def freeze(run_root: Path) -> int:
    """冻结匿名 PDF 副本、密封答案、阈值和当前实现指纹。"""

    if run_root.exists():
        raise FileExistsError(f"目标轮次已存在:{run_root}")
    source = read_json(SOURCE_MANIFEST)
    source_cases = list(source["cases"])
    excluded_documents, exclusion_counts = _excluded_documents(source_cases)
    candidates = [
        case
        for case in source_cases
        if case["evaluation_role"] == "calibration"
        and str(case["source_document_id"]) not in excluded_documents
    ]
    selected = select_stratified_cases(candidates, FRESH_SALT, CASES_PER_ROUTE)
    public_cases = build_public_cases(selected)
    route_counts = Counter(str(case["expected_route"]) for case in selected)
    expected_routes = sorted(source["route_counts"])
    if set(route_counts) != set(expected_routes) or set(route_counts.values()) != {
        CASES_PER_ROUTE
    }:
        raise RuntimeError("新盲集没有完整覆盖 16 个具体类别")
    for relative in ("input/pages", "process", "output/visual_review"):
        (run_root / relative).mkdir(parents=True, exist_ok=True)
    answer_cases = []
    for source_case, public_case in zip(selected, public_cases, strict=True):
        source_path = REPO_ROOT / str(source_case["path"])
        if rv2.sha256_file(source_path) != source_case["content_sha256"]:
            raise RuntimeError(f"源样本哈希漂移:{source_case['case_id']}")
        destination = run_root / str(public_case["path"])
        shutil.copy2(source_path, destination)
        if rv2.sha256_file(destination) != source_case["content_sha256"]:
            raise RuntimeError(f"匿名副本哈希漂移:{public_case['case_id']}")
        answer_cases.append(
            {
                "case_id": public_case["case_id"],
                "expected_route": source_case["expected_route"],
                "gold_provenance": source_case["gold_provenance"],
                "source_content_sha256": source_case["content_sha256"],
                "source_document_id": source_case["source_document_id"],
                "source_path": source_case["path"],
            }
        )
    answer_path = run_root / "input" / "sealed_answer_key.json"
    rv2.write_json(
        answer_path,
        {
            "schema_version": "transflow.rv2-fresh-blind-answer-key/v1",
            "sealed_at": now_iso(),
            "run_id": run_root.name,
            "cases": answer_cases,
        },
    )
    answer_hash = rv2.sha256_file(answer_path)
    rv2.write_json(
        run_root / "input" / "fresh_blind_manifest.json",
        {
            "schema_version": "transflow.rv2-fresh-blind-public/v1",
            "answer_key_sha256": answer_hash,
            "case_count": len(public_cases),
            "cases_per_route": CASES_PER_ROUTE,
            "frozen_at": now_iso(),
            "gold_mapping_exposed": False,
            "input_lock": "anonymous relative path + size_bytes + sha256",
            "route_count": len(route_counts),
            "run_id": run_root.name,
            "cases": public_cases,
        },
    )
    rv2.write_json(
        run_root / "input" / "selection_audit.json",
        {
            "schema_version": "transflow.rv2-fresh-blind-selection/v1",
            "run_id": run_root.name,
            "source_run_id": SOURCE_RUN_ID,
            "candidate_case_count": len(candidates),
            "selected_case_count": len(selected),
            "selected_document_count": len(
                {str(case["source_document_id"]) for case in selected}
            ),
            "route_counts": dict(sorted(route_counts.items())),
            "selection_salt_sha256": hashlib.sha256(FRESH_SALT.encode()).hexdigest(),
            "exclusions": exclusion_counts,
            "old_blind_case_overlap_count": 0,
            "old_blind_document_overlap_count": 0,
        },
    )
    rv2.write_json(
        run_root / "input" / "thresholds.json",
        {
            "schema_version": "transflow.rv2-fresh-blind-thresholds/v1",
            "frozen_at": now_iso(),
            "overall_route_accuracy_min": 1.0,
            "per_route_accuracy_min": 1.0,
            "model_failure_count_max": 0,
            "unclassified_count_max": 0,
            "threshold_change_policy": "FORBIDDEN_AFTER_RESULTS",
        },
    )
    rv2.write_json(
        run_root / "input" / "code_prompt_snapshot.json",
        {
            "schema_version": "transflow.rv2-code-prompt-snapshot/v1",
            "captured_at": now_iso(),
            "run_id": run_root.name,
            "files": _snapshot(),
        },
    )
    return 0


def preflight(run_root: Path) -> int:
    """只输出聚合规则安全计数，不公开 case 到 gold 的映射。"""

    _verify_snapshot(run_root)
    public = read_json(run_root / "input" / "fresh_blind_manifest.json")["cases"]
    answer_payload = read_json(run_root / "input" / "sealed_answer_key.json")
    answers = {str(item["case_id"]): item for item in answer_payload["cases"]}
    results = []
    for case in public:
        answer = answers[str(case["case_id"])]
        results.append(rv2.audit_case(build_audit_case(run_root, case, answer)))
    audit_summary = summarize_rule_audit(results)
    summary = {
        "schema_version": "transflow.rv2-fresh-blind-preflight/v1",
        "run_id": run_root.name,
        "case_count": len(results),
        "case_gold_mapping_persisted": False,
        **audit_summary,
    }
    rv2.write_json(run_root / "process" / "preflight_rule_summary.json", summary)
    if (
        audit_summary["high_confidence_rule_conflict_count"]
        or audit_summary["unsafe_model_skip_count"]
    ):
        raise RuntimeError("新盲集规则安全预检未通过")
    return 0


def live(run_root: Path, batch_index: int, label: str) -> int:
    """只读取公开匿名清单运行模型，不读取密封答案。"""

    from tests.migration.qwen_adapter import (
        MigrationQwenDecisionAdapter,
        migration_environment_ready,
    )

    _verify_snapshot(run_root)
    preflight_summary = read_json(run_root / "process" / "preflight_rule_summary.json")
    if preflight_summary["high_confidence_rule_conflict_count"] != 0:
        raise RuntimeError("规则安全预检未通过")
    if not migration_environment_ready():
        raise RuntimeError("真实迁移模型环境变量未配置")
    manifest = read_json(run_root / "input" / "fresh_blind_manifest.json")
    cases = list(manifest["cases"])
    batch_count = (len(cases) + BATCH_SIZE - 1) // BATCH_SIZE
    if not 1 <= batch_index <= batch_count:
        raise ValueError(f"batch-index 必须位于 1..{batch_count}")
    selected = cases[(batch_index - 1) * BATCH_SIZE : batch_index * BATCH_SIZE]
    results = []
    actual_model_call_count = 0
    for case in selected:
        path = run_root / str(case["path"])
        content_hash = rv2.sha256_file(path)
        if content_hash != case["content_sha256"]:
            raise RuntimeError(f"匿名输入漂移:{case['case_id']}")
        facts = PageFactsExtractor().extract_page(
            path,
            content_hash,
            int(case["page_no"]),
            include_classification=True,
        )
        adapter = MigrationQwenDecisionAdapter()
        port = rv2.HashingDecisionPort(adapter)
        classified = ClassificationEngine(BoundedDecisionRunner(port)).classify_page(
            facts,
            int(case["page_count"]),
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
                "failed_node": classified.route.failed_node,
                "model_call_attempt_count": adapter.call_count,
                "model_call_success_count": len(port.records),
                "model_failure_codes": model_failure_codes,
                "model_calls": port.records,
                "predicted_route": classified.route.route,
            }
        )
    output = run_root / "process" / f"{label}.json"
    if output.exists():
        raise FileExistsError(f"禁止覆盖既有模型证据:{output}")
    rv2.write_json(
        output,
        {
            "schema_version": "transflow.rv2-fresh-blind-live/v1",
            "run_id": run_root.name,
            "batch_index": batch_index,
            "batch_count": batch_count,
            "case_count": len(results),
            "evaluation_set": "fresh_strict_blind_sealed",
            "gold_mapping_read": False,
            "actual_model_call_count": actual_model_call_count,
            "successful_model_call_count": sum(
                int(item["model_call_success_count"]) for item in results
            ),
            "results": results,
        },
    )
    return 0


def score(run_root: Path, labels: list[str], output_label: str) -> int:
    """合并无答案预测，验证完整性后才解封计分。"""

    _verify_snapshot(run_root)
    public_manifest = read_json(run_root / "input" / "fresh_blind_manifest.json")
    answer_path = run_root / "input" / "sealed_answer_key.json"
    if rv2.sha256_file(answer_path) != public_manifest["answer_key_sha256"]:
        raise RuntimeError("密封答案哈希漂移")
    predictions = []
    attempted_calls = 0
    successful_calls = 0
    for label in labels:
        payload = read_json(run_root / "process" / f"{label}.json")
        if payload.get("gold_mapping_read") is not False:
            raise RuntimeError(f"预测文件不是密封运行产物:{label}")
        predictions.extend(payload["results"])
        attempted_calls += int(payload["actual_model_call_count"])
        successful_calls += int(payload["successful_model_call_count"])
    answer_payload = read_json(answer_path)
    answer_key = {
        str(item["case_id"]): str(item["expected_route"])
        for item in answer_payload["cases"]
    }
    evaluation = evaluate_predictions(public_manifest["cases"], answer_key, predictions)
    model_failure_count = sum(
        int(bool(item.get("model_failure_codes"))) for item in predictions
    )
    unclassified_count = sum(
        int(item["predicted_route"] == "unclassified") for item in predictions
    )
    thresholds = read_json(run_root / "input" / "thresholds.json")
    per_route_pass = all(
        values["passed"] / values["total"] >= thresholds["per_route_accuracy_min"]
        for values in evaluation["by_route"].values()
    )
    passed = (
        evaluation["route_accuracy"] >= thresholds["overall_route_accuracy_min"]
        and per_route_pass
        and model_failure_count <= thresholds["model_failure_count_max"]
        and unclassified_count <= thresholds["unclassified_count_max"]
    )
    output = run_root / "process" / f"{output_label}.json"
    if output.exists():
        raise FileExistsError(f"禁止覆盖既有计分证据:{output}")
    rv2.write_json(
        output,
        {
            "schema_version": "transflow.rv2-fresh-blind-score/v1",
            "run_id": run_root.name,
            "scored_at": now_iso(),
            "answer_key_sha256": public_manifest["answer_key_sha256"],
            "prediction_artifacts": [f"process/{label}.json" for label in labels],
            "attempted_model_call_count": attempted_calls,
            "successful_model_call_count": successful_calls,
            "model_failure_case_count": model_failure_count,
            "unclassified_count": unclassified_count,
            "gate_status": "PASS" if passed else "FAIL",
            **evaluation,
        },
    )
    return 0


def render(run_root: Path, score_label: str) -> int:
    """解封后渲染四张联系表，供人工核对 PDF 视觉类别边界。"""

    import fitz
    from PIL import Image, ImageDraw

    public = read_json(run_root / "input" / "fresh_blind_manifest.json")["cases"]
    score_payload = read_json(run_root / "process" / f"{score_label}.json")
    scored = {str(item["case_id"]): item for item in score_payload["results"]}
    output_root = run_root / "output" / "visual_review"
    artifacts = []
    for batch_index in range(1, (len(public) + BATCH_SIZE - 1) // BATCH_SIZE + 1):
        cases = public[(batch_index - 1) * BATCH_SIZE : batch_index * BATCH_SIZE]
        cards = []
        for case in cases:
            document = fitz.open(run_root / str(case["path"]))
            try:
                page = document[0]
                pixmap = page.get_pixmap(matrix=fitz.Matrix(1.15, 1.15), alpha=False)
                image = Image.frombytes("RGB", [pixmap.width, pixmap.height], pixmap.samples)
            finally:
                document.close()
            image.thumbnail((330, 440))
            card = Image.new("RGB", (350, 520), "white")
            card.paste(image, ((350 - image.width) // 2, 8))
            result = scored[str(case["case_id"])]
            label = (
                f"{case['case_id']} {'PASS' if result['route_matches'] else 'FAIL'}\n"
                f"E:{result['expected_route']}\nP:{result['predicted_route']}"
            )
            ImageDraw.Draw(card).multiline_text((8, 452), label, fill="black", spacing=2)
            cards.append(card)
        sheet = Image.new("RGB", (1400, 1040), "#dddddd")
        for index, card in enumerate(cards):
            sheet.paste(card, ((index % 4) * 350, (index // 4) * 520))
        path = output_root / f"fresh-blind-batch{batch_index:02d}.png"
        sheet.save(path)
        artifacts.append(
            {
                "path": path.relative_to(run_root).as_posix(),
                "sha256": rv2.sha256_file(path),
                "size_bytes": path.stat().st_size,
            }
        )
    rv2.write_json(
        run_root / "process" / "visual_review_manifest.json",
        {
            "schema_version": "transflow.rv2-fresh-blind-visual-review/v1",
            "run_id": run_root.name,
            "rendered_at": now_iso(),
            "case_count": len(public),
            "artifacts": artifacts,
        },
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("freeze", "preflight", "live", "score", "render"))
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--batch-index", type=int)
    parser.add_argument("--label")
    parser.add_argument("--prediction-label", action="append", default=[])
    parser.add_argument("--score-label", default="fresh-blind-score")
    args = parser.parse_args()
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", args.run_id) is None:
        raise ValueError("run-id 只能包含字母、数字、点、下划线和连字符")
    run_root = RV2_ROOT / args.run_id
    if args.phase == "freeze":
        return freeze(run_root)
    if args.phase == "preflight":
        return preflight(run_root)
    if args.phase == "live":
        if args.batch_index is None or not args.label:
            raise ValueError("live 必须提供 batch-index 和 label")
        return live(run_root, args.batch_index, args.label)
    if args.phase == "score":
        if not args.prediction_label:
            raise ValueError("score 必须提供 prediction-label")
        return score(run_root, args.prediction_label, args.score_label)
    return render(run_root, args.score_label)


if __name__ == "__main__":
    raise SystemExit(main())
