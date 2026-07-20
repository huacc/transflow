import argparse
import json
from pathlib import Path
from typing import Any


REQUIRED_ARTIFACT_CHAIN = [
    "evidence_basket.json",
    "quality_signal_ledger.json",
    "problem_domain_buckets.json",
    "triage_result.json",
    "dispatch_result.json",
    "decision_artifacts/repair_patch_0001.json",
    "decision_artifacts/repair_patch_0002.json",
    "repair_acceptance.json",
    "repair_memory_ledger.json",
]


def load_json(path: Path, default: Any | None = None) -> Any:
    if not path.exists():
        return {} if default is None else default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def registry_items(root: Path, name: str) -> dict[str, dict[str, Any]]:
    data = load_json(root / "contracts" / "registry" / f"{name}.json", {"items": []})
    items = data.get("items") or data.get(name) or []
    return {str(item.get("id")): item for item in items if item.get("id")}


def validate(root: Path, reports: Path) -> dict[str, Any]:
    failures = []
    warnings = []
    failure_classes = registry_items(root, "failure_classes")
    repair_atoms = registry_items(root, "repair_atoms")
    repair_families = registry_items(root, "repair_families")
    problem_domains = registry_items(root, "problem_domains")

    missing_artifacts = [name for name in REQUIRED_ARTIFACT_CHAIN if not (reports / name).exists()]
    if missing_artifacts:
        failures.append({"gate_id": "seven_artifact_dependency", "missing": missing_artifacts})

    triage = load_json(reports / "triage_result.json", {})
    dispatch = load_json(reports / "dispatch_result.json", {})
    selected_failure = triage.get("selected_failure_class")
    selected_domain = triage.get("selected_problem_domain")
    if selected_failure and selected_failure not in failure_classes:
        failures.append({"gate_id": "failure_class_registry", "missing_id": selected_failure})
    if selected_domain and selected_domain not in problem_domains:
        failures.append({"gate_id": "problem_domain_registry", "missing_id": selected_domain})

    registry_atom = dispatch.get("registry_default_repair_atom")
    if registry_atom and registry_atom not in repair_atoms:
        failures.append({"gate_id": "repair_atom_registry", "missing_id": registry_atom})
    selected_family = dispatch.get("registry_default_repair_family") or dispatch.get("selected_repair_family_for_this_run")
    if selected_family and selected_family not in repair_families:
        failures.append({"gate_id": "repair_family_registry", "missing_id": selected_family})

    capability = dispatch.get("registry_capability_status")
    if capability == "missing" and dispatch.get("selected_repair_family_for_this_run") == dispatch.get("registry_default_repair_family"):
        failures.append(
            {
                "gate_id": "missing_atom_not_executable",
                "failure_class": selected_failure,
                "repair_atom": registry_atom,
                "reason": "A missing registry atom cannot be treated as an executable repair.",
            }
        )
    elif capability == "missing":
        warnings.append(
            {
                "gate_id": "missing_atom_boundary",
                "failure_class": selected_failure,
                "repair_atom": registry_atom,
                "reason": "Registry says the correct future repair is missing; current run must either stop honestly or record a seed/partial fallback.",
            }
        )

    chain_positions = {name: index for index, name in enumerate(REQUIRED_ARTIFACT_CHAIN)}
    existing = [name for name in REQUIRED_ARTIFACT_CHAIN if (reports / name).exists()]
    if existing:
        positions = [chain_positions[name] for name in existing]
        if positions != sorted(positions):
            failures.append({"gate_id": "artifact_chain_order", "existing": existing})

    change_ledger = reports / "round28_change_ledger.md"
    if not change_ledger.exists():
        failures.append({"gate_id": "change_ledger_required", "missing": "reports/round28_change_ledger.md"})

    memory = load_json(reports / "repair_memory_ledger.json", {})
    attempts = memory.get("attempts") or []
    if len(attempts) < 2:
        failures.append({"gate_id": "multi_loop_memory_required", "attempt_count": len(attempts)})
    if (memory.get("stop_policy_probe") or {}).get("same_atom_retry_violation_detected"):
        failures.append({"gate_id": "same_issue_atom_retry_violation", "memory": memory.get("stop_policy_probe")})

    result = {
        "tool": "validate_decision_graph",
        "validator_scope": "phase_a_minimum",
        "decision_graph_verdict": "FAIL" if failures else "PASS",
        "failure_count": len(failures),
        "warning_count": len(warnings),
        "failures": failures,
        "warnings": warnings,
        "checked_artifacts": REQUIRED_ARTIFACT_CHAIN,
        "selected_failure_class": selected_failure,
        "selected_problem_domain": selected_domain,
        "registry_default_repair_atom": registry_atom,
        "registry_capability_status": capability,
        "dispatch_conflict_detected": dispatch.get("dispatch_conflict_detected"),
    }
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--round-root", type=Path, default=Path("."))
    parser.add_argument("--reports-dir", type=Path, default=Path("reports"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.round_root.resolve()
    reports = (root / args.reports_dir).resolve()
    result = validate(root, reports)
    output = args.output if args.output.is_absolute() else root / args.output
    write_json(output, result)


if __name__ == "__main__":
    main()
