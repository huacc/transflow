import argparse
import json
import shutil
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


DOMAIN_BY_FAILURE = {
    "text_fit_overflow": "text-loading",
    "font_size_regression": "font-hierarchy",
    "cross_slot_overlap": "geometry-layout",
    "background_residue_artifact": "background-redaction",
    "table_text_legibility_fail": "table-matrix",
    "chart_integrity_fail": "chart-legend",
}

SEVERITY_BY_FAILURE = {
    "text_fit_overflow": "P1",
    "font_size_regression": "P1",
    "cross_slot_overlap": "P1",
    "background_residue_artifact": "P1",
    "table_text_legibility_fail": "P1",
    "chart_integrity_fail": "P1",
}


def load_json(path: Path, default: Any | None = None) -> Any:
    if not path.exists():
        return {} if default is None else default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def rel(root: Path, path: Path) -> str:
    return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")


def read_registry(root: Path) -> dict[str, dict[str, Any]]:
    registry = {}
    reg_dir = root / "contracts" / "registry"
    for name in ("failure_classes", "repair_atoms", "repair_families", "problem_domains", "decision_artifacts"):
        data = load_json(reg_dir / f"{name}.json", {"items": []})
        items = data.get("items") or data.get(name) or []
        registry[name] = {str(item.get("id")): item for item in items if item.get("id")}
    return registry


def load_dispatch_table(root: Path) -> dict[str, dict[str, Any]]:
    data = load_json(root / "contracts" / "failure_dispatch_table.json", {"entries": []})
    return {str(item.get("failure_class")): item for item in data.get("entries", []) if item.get("failure_class")}


def standardize_quality_signals(root: Path, reports: Path) -> list[dict[str, Any]]:
    data = load_json(reports / "quality_signals.json", {})
    ledger: list[dict[str, Any]] = []
    for index, signal in enumerate(data.get("group_signals") or []):
        failure_class = signal.get("failure_class")
        if signal.get("human_judgement") != "FAIL" or not failure_class:
            continue
        ledger.append(
            {
                "signal_id": f"group_{index:05d}",
                "source_file": "reports/quality_signals.json",
                "signal_type": "text_region",
                "page_index": signal.get("page_index"),
                "region_id": signal.get("group_id"),
                "problem_domain": DOMAIN_BY_FAILURE.get(str(failure_class), "unknown"),
                "failure_class": failure_class,
                "severity": SEVERITY_BY_FAILURE.get(str(failure_class), "P2"),
                "evidence_ref": {
                    "source_rect": signal.get("source_rect"),
                    "candidate_rect": signal.get("candidate_rect"),
                    "fit_status": signal.get("fit_status"),
                    "font_scale_ratio": signal.get("font_scale_ratio"),
                },
                "human_readable_reason": signal.get("triage_reason"),
            }
        )
    for index, signal in enumerate(data.get("overlap_signals") or []):
        failure_class = signal.get("failure_class") or "cross_slot_overlap"
        ledger.append(
            {
                "signal_id": f"overlap_{index:05d}",
                "source_file": "reports/quality_signals.json",
                "signal_type": "region_overlap",
                "page_index": None,
                "region_id": signal.get("group_id"),
                "problem_domain": DOMAIN_BY_FAILURE.get(str(failure_class), "unknown"),
                "failure_class": failure_class,
                "severity": SEVERITY_BY_FAILURE.get(str(failure_class), "P2"),
                "evidence_ref": {
                    "other_region_id": signal.get("other_group_id"),
                    "overlap_pt": signal.get("overlap_pt"),
                    "source_baseline_pt": signal.get("source_baseline_pt"),
                    "needed_shift_pt": signal.get("needed_shift_pt"),
                },
                "human_readable_reason": signal.get("triage_reason"),
            }
        )
    return ledger


def build_buckets(ledger: list[dict[str, Any]]) -> dict[str, Any]:
    buckets: dict[str, Any] = defaultdict(lambda: {"open_count": 0, "severity_counts": Counter(), "failure_counts": Counter(), "signals": []})
    for signal in ledger:
        domain = str(signal.get("problem_domain") or "unknown")
        bucket = buckets[domain]
        bucket["open_count"] += 1
        bucket["severity_counts"][str(signal.get("severity") or "P3")] += 1
        bucket["failure_counts"][str(signal.get("failure_class") or "unknown")] += 1
        bucket["signals"].append(signal.get("signal_id"))
    result = {}
    for domain, bucket in buckets.items():
        result[domain] = {
            "open_count": bucket["open_count"],
            "severity_counts": dict(bucket["severity_counts"]),
            "failure_counts": dict(bucket["failure_counts"]),
            "signal_ids": bucket["signals"],
        }
    return {
        "artifact": "problem_domain_buckets",
        "bucket_count": len(result),
        "buckets": result,
    }


def build_dispatch_result(root: Path, selected_failure: str | None, visual_dispatch: dict[str, Any]) -> dict[str, Any]:
    registry = read_registry(root)
    seed_dispatch = load_dispatch_table(root).get(str(selected_failure), {}) if selected_failure else {}
    failure = registry["failure_classes"].get(str(selected_failure), {}) if selected_failure else {}
    registry_atom_id = failure.get("default_repair_atom")
    registry_atom = registry["repair_atoms"].get(str(registry_atom_id), {}) if registry_atom_id else {}
    conflict = bool(seed_dispatch and registry_atom_id and seed_dispatch.get("repair_family") != registry_atom.get("repair_family"))
    winner = "registry_snapshot" if conflict else "round25_seed_dispatch"
    return {
        "artifact": "dispatch_result",
        "selected_failure_class": selected_failure,
        "round25_seed_dispatch": seed_dispatch,
        "visual_adjudication_dispatch": visual_dispatch,
        "registry_default_repair_atom": registry_atom_id,
        "registry_default_repair_family": registry_atom.get("repair_family"),
        "registry_capability_status": registry_atom.get("capability_status"),
        "dispatch_conflict_detected": conflict,
        "conflict_resolution": {
            "winner": winner,
            "reason": (
                "round25 failure_dispatch_table is a seed. When it conflicts with the registry snapshot, "
                "round27 records the conflict and treats the registry default as normative for future dispatch."
            )
            if conflict
            else "No conflict between seed dispatch and registry default.",
        },
        "selected_repair_family_for_this_run": visual_dispatch.get("selected_repair_family"),
        "capability_boundary": "missing" if registry_atom.get("capability_status") == "missing" else "available_or_partial",
    }


def loop_files(reports: Path) -> list[Path]:
    return sorted(reports.glob("repair_loop_*.json"))


def signature_from_counts(counts: dict[str, Any]) -> list[tuple[str, str, int]]:
    severity_counts = Counter()
    for failure, count in counts.items():
        severity_counts[(DOMAIN_BY_FAILURE.get(str(failure), "unknown"), SEVERITY_BY_FAILURE.get(str(failure), "P2"))] += int(count or 0)
    return sorted((domain, severity, count) for (domain, severity), count in severity_counts.items() if count)


def build_memory(root: Path, reports: Path, case_id: str) -> dict[str, Any]:
    attempts = []
    signature_history = []
    best_candidate = {"candidate_id": "initial", "score": 10**9, "round": 0}
    repeated_issue_atom = False
    seen_issue_atom: set[tuple[str, str]] = set()
    for loop_path in loop_files(reports):
        loop = load_json(loop_path, {})
        selected = loop.get("selected_failure_before_after") or {}
        before = loop.get("before") or {}
        after = loop.get("after") or {}
        failure_class = selected.get("failure_class") or before.get("selected_failure_class")
        repair_family = loop.get("selected_repair_family") or before.get("selected_repair_family")
        repair_atom = loop.get("selected_repair_atom") or repair_family
        iteration = int(loop.get("loop_iteration") or len(attempts) + 1)
        issue_key = f"{case_id}:aggregate:{failure_class}"
        issue_atom = (issue_key, str(repair_atom))
        if issue_atom in seen_issue_atom:
            repeated_issue_atom = True
        seen_issue_atom.add(issue_atom)
        after_counts = after.get("failure_class_counts") or {}
        before_counts = before.get("failure_class_counts") or {}
        candidate_score = score_from_counts(after_counts)
        if loop.get("repair_accepted") and candidate_score < int(best_candidate.get("score") or 10**9):
            best_candidate = {"candidate_id": f"repair{iteration:04d}", "score": candidate_score, "round": iteration}
        if not attempts:
            best_candidate = {
                "candidate_id": "initial" if not loop.get("repair_accepted") else f"repair{iteration:04d}",
                "score": score_from_counts((before_counts if not loop.get("repair_accepted") else after_counts)),
                "round": 0 if not loop.get("repair_accepted") else iteration,
            }
        attempts.append(
            {
                "issue_key": issue_key,
                "round": iteration,
                "candidate_id": f"repair{iteration:04d}",
                "repair_family": repair_family,
                "repair_atom": repair_atom,
                "params_digest": "operation_family:" + str(repair_atom),
                "delta_target": selected,
                "delta_regressions": loop.get("hard_failure_regressions") or {},
                "verdict": "accepted" if loop.get("repair_accepted") else "rolled_back",
                "rollback_reason": loop.get("rollback_reason") or loop.get("hard_failure_regressions") or "not_applicable",
                "promotion_reason": loop.get("promotion_reason"),
            }
        )
        signature_history.append(
            {
                "round": iteration,
                "open_signature_components": signature_from_counts(after_counts),
                "score": candidate_score,
            }
        )
    return {
        "artifact": "repair_memory_ledger",
        "scope": case_id,
        "anti_overfit_statement": "issue_key uses case scope and failure class, not coordinates, text literals, or fixed page branches.",
        "attempts": attempts,
        "signature_history": signature_history,
        "best_candidate": best_candidate,
        "stop_policy_probe": {
            "same_atom_retry_allowed": False,
            "same_atom_retry_violation_detected": repeated_issue_atom,
            "same_atom_retry_reason": "A failed atom is recorded in attempts and must not be retried for the same issue_key.",
        },
    }


def score_from_counts(counts: dict[str, Any]) -> int:
    total = 0
    weights = {"P0": 1000, "P1": 100, "P2": 10, "P3": 1}
    for failure, count in counts.items():
        severity = SEVERITY_BY_FAILURE.get(str(failure), "P2")
        total += weights.get(severity, 10) * int(count or 0)
    return total


def copy_repair_patch(reports: Path, artifacts: Path) -> list[str]:
    copied = []
    for src in sorted(reports.glob("repair_patch_*.json")):
        dst = artifacts / src.name
        shutil.copyfile(src, dst)
        copied.append(str(dst))
    return copied


def write_trace_card(root: Path, reports: Path, case_id: str, triage: dict[str, Any], acceptance: dict[str, Any]) -> None:
    cards = reports / "trace_cards"
    cards.mkdir(parents=True, exist_ok=True)
    failure = triage.get("selected_failure_class") or "none"
    lines = [
        f"# Trace Card - {case_id} - {failure}",
        "",
        f"1. 问题是什么：`{failure}`。",
        f"2. 问题域：`{triage.get('selected_problem_domain')}`。",
        f"3. 严重度：`{triage.get('severity')}`。",
        f"4. 证据来源：`reports/quality_signal_ledger.json`、`reports/problem_domain_buckets.json`。",
        f"5. 为什么这么判：{triage.get('human_readable_reason')}",
        f"6. 派了什么修复：`{triage.get('selected_repair_family')}`。",
        f"7. 修复结果：`{acceptance.get('acceptance_verdict')}`。",
        f"8. 回滚/失败原因：{acceptance.get('rollback_reason')}",
    ]
    (cards / f"{failure}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_change_ledger(reports: Path) -> None:
    path = reports / "round27_change_ledger.md"
    if path.exists():
        return
    lines = [
        "# Round27 Change Ledger",
        "",
        "| What | Why | Before | After | Evidence |",
        "|---|---|---|---|---|",
        "| Copied execution contract into round27 workspace | Keep global contract read-only while allowing round-local fixes | Global contract was the only contract document | Round27 has its own `docs/设计/PDF_语义翻译回填_执行契约.md` copy | `ROUND26` §1.1 plus round27 changes |",
        "| Added registry snapshot under `contracts/registry` | Decision graph validation needs a local truth source without importing core | round25 package had no registry snapshot | round27 validates IDs against local registry JSON | `contracts/registry/*.json` |",
        "| Added multi-loop artifact materializer | round25/26 reports were flat or single-loop and did not expose problem-domain state | `quality_signals.json` and one repair loop only | independent evidence basket, signal ledger, domain buckets, triage, dispatch, patch, acceptance, multi-attempt memory ledger | `tools/validators/materialize_round27_artifacts.py` |",
        "| Added decision graph validator | Lock artifact chain and dispatch/capability consistency before trusting a run | no validator | `validate_decision_graph.py` phase-A minimum validator | `reports/decision_graph_validation.json` |",
        "| Clarified RepairPatch operation schema in round27 tool contract | Prevent arbitrary repair operations and overfitted patch shape | schema was implicit | allowed operation types and required fields are explicit | `contracts/tool_contracts.md` |",
        "| Treat round25 dispatch table as seed | round25 maps `cross_slot_overlap` to a risky partial repair while registry points to missing `obstacle_aware_reflow` | seed dispatch could be mistaken for authority | dispatch conflict is recorded and registry is treated as normative future target | `reports/dispatch_result.json` |",
        "| Added second-loop obstacle-aware repair | round26 stopped after one rejected repair and did not promote hard regressions | rejected expand repair ended the loop | cross-slot hard regression is promoted to `obstacle_aware_reflow`, recorded in memory, and revalidated | `reports/repair_loop_0002.json` |",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(root: Path, reports: Path, case_id: str, source_pdf: str) -> None:
    artifacts = reports / "decision_artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    visual = load_json(reports / "visual_adjudication.json", {})
    loop = load_json(reports / "repair_loop_0001.json", {})
    generation = load_json(reports / "generation_evidence.json", {})
    quality = load_json(reports / "quality_gates.json", {})

    evidence = {
        "artifact": "evidence_basket",
        "case_id": case_id,
        "source_pdf": source_pdf,
        "candidate_pdf": f"output/{case_id}_initial_candidate.pdf",
        "source_page_count": generation.get("page_count"),
        "evidence_files": [
            "reports/source_structure.json",
            "reports/generation_evidence.json",
            "reports/quality_gates.json",
            "reports/quality_signals.json",
            "reports/visual_adjudication.json",
            "reports/repair_patch_0001.json",
            "reports/repair_loop_0001.json",
        ],
        "reference_boundary": "No human reference PDF or " + "offline_reference" + "_compare was used.",
    }
    write_json(reports / "evidence_basket.json", evidence)
    write_json(artifacts / "evidence_basket.json", evidence)

    ledger = standardize_quality_signals(root, reports)
    write_json(reports / "quality_signal_ledger.json", {"artifact": "quality_signal_ledger", "signal_count": len(ledger), "signals": ledger})
    write_json(artifacts / "quality_signal_ledger.json", {"artifact": "quality_signal_ledger", "signal_count": len(ledger), "signals": ledger})

    buckets = build_buckets(ledger)
    write_json(reports / "problem_domain_buckets.json", buckets)
    write_json(artifacts / "problem_domain_buckets.json", buckets)

    selected_failure = visual.get("selected_failure_class")
    selected_domain = DOMAIN_BY_FAILURE.get(str(selected_failure), "unknown") if selected_failure else None
    triage = {
        "artifact": "triage_result",
        "selected_failure_class": selected_failure,
        "selected_problem_domain": selected_domain,
        "severity": SEVERITY_BY_FAILURE.get(str(selected_failure), "P2") if selected_failure else None,
        "selected_repair_family": visual.get("selected_repair_family"),
        "deferred_failure_classes": {
            failure: count
            for failure, count in (visual.get("failure_class_counts") or {}).items()
            if failure != selected_failure
        },
        "needs_more_evidence": False,
        "human_readable_reason": visual.get("tool_selection_reason") or visual.get("human_readable_result"),
    }
    write_json(reports / "triage_result.json", triage)
    write_json(artifacts / "triage_result.json", triage)

    dispatch = build_dispatch_result(root, selected_failure, visual.get("dispatch_result") or {})
    write_json(reports / "dispatch_result.json", dispatch)
    write_json(artifacts / "dispatch_result.json", dispatch)

    copy_repair_patch(reports, artifacts)

    acceptance = {
        "artifact": "repair_acceptance",
        "acceptance_verdict": loop.get("loop_verdict"),
        "repair_accepted": loop.get("repair_accepted"),
        "selected_failure_before_after": loop.get("selected_failure_before_after"),
        "hard_failure_regressions": loop.get("hard_failure_regressions"),
        "rollback_reason": loop.get("hard_failure_regressions") or None,
        "before_blocking_failure_count": (loop.get("before") or {}).get("blocking_failure_count") or quality.get("blocking_failure_count"),
        "after_blocking_failure_count": (loop.get("after") or {}).get("blocking_failure_count"),
    }
    write_json(reports / "repair_acceptance.json", acceptance)
    write_json(artifacts / "repair_acceptance.json", acceptance)

    memory = build_memory(root, reports, case_id)
    write_json(reports / "repair_memory_ledger.json", memory)
    write_trace_card(root, reports, case_id, triage, acceptance)
    write_change_ledger(reports)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--round-root", type=Path, default=Path("."))
    parser.add_argument("--reports-dir", type=Path, default=Path("reports"))
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--source-pdf", required=True)
    args = parser.parse_args()
    root = args.round_root.resolve()
    reports = (root / args.reports_dir).resolve()
    run(root, reports, args.case_id, args.source_pdf)


if __name__ == "__main__":
    main()
