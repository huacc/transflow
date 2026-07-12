from __future__ import annotations

import json
from pathlib import Path

from .models import AcceptanceResult, CadenceError, KernelRegressionResult, SampleSplit, ToolboxMaturity, ToolboxSampleRecord


ALLOWED_MATURITY_TRANSITIONS = {
    ToolboxMaturity.EXPERIMENTAL: {ToolboxMaturity.REGRESSION, ToolboxMaturity.EVIDENCE_INSUFFICIENT},
    ToolboxMaturity.REGRESSION: {ToolboxMaturity.PROMOTED, ToolboxMaturity.EVIDENCE_INSUFFICIENT},
    ToolboxMaturity.EVIDENCE_INSUFFICIENT: {ToolboxMaturity.EXPERIMENTAL},
    ToolboxMaturity.PROMOTED: set(),
}

REQUIRED_ACCEPTANCE_PATHS = (
    "docs/分类边界与不变量.md",
    "docs/工具分类与调用流程.md",
    "docs/裁决与修复规则.md",
    "samples/manifest.jsonl",
    "reports/fixed_translation_regression.json",
    "reports/qwen_page_e2e.json",
    "reports/regression.json",
    "reports/holdout.json",
    "reports/visual_review.json",
    "stage_gate.json",
)

PASS_REPORTS = (
    "reports/fixed_translation_regression.json",
    "reports/qwen_page_e2e.json",
    "reports/regression.json",
    "reports/holdout.json",
    "reports/visual_review.json",
)


def _validate_evidence_refs(
    package_root: Path,
    owner: str,
    evidence_refs: object,
    failed_reports: list[str],
    reasons: list[str],
) -> None:
    if not isinstance(evidence_refs, list) or not evidence_refs:
        failed_reports.append(owner)
        return
    resolved_root = package_root.resolve()
    for evidence_ref in evidence_refs:
        if not isinstance(evidence_ref, str) or not evidence_ref.strip():
            failed_reports.append(owner)
            continue
        evidence_path = (package_root / evidence_ref).resolve()
        try:
            evidence_path.relative_to(resolved_root)
        except ValueError:
            reasons.append(f"evidence_ref_outside_package:{owner}:{evidence_ref}")
            continue
        if not evidence_path.is_file():
            reasons.append(f"evidence_ref_missing:{owner}:{evidence_ref}")


class ToolboxMaturityMachine:
    def __init__(self, initial: ToolboxMaturity = ToolboxMaturity.EXPERIMENTAL) -> None:
        self.current = initial

    def transition(self, target: ToolboxMaturity, *, promotion_ready: bool = False) -> None:
        if target not in ALLOWED_MATURITY_TRANSITIONS[self.current]:
            raise CadenceError(f"illegal_maturity_transition:{self.current.value}->{target.value}")
        if target is ToolboxMaturity.PROMOTED and not promotion_ready:
            raise CadenceError("promotion_requires_complete_acceptance_package")
        self.current = target


class HoldoutAccessGuard:
    def __init__(self) -> None:
        self.workflow_frozen = False

    def freeze_workflow(self) -> None:
        self.workflow_frozen = True

    def require_content_access(self, split: SampleSplit, *, purpose: str) -> None:
        if split is not SampleSplit.HOLDOUT:
            return
        if not self.workflow_frozen:
            raise CadenceError("holdout_content_forbidden_before_workflow_freeze")
        if purpose != "final_validation":
            raise CadenceError("holdout_content_only_allowed_for_final_validation")


class ToolboxWorkLedger:
    def __init__(self) -> None:
        self.active_toolbox_key: str | None = None
        self.completed: list[dict[str, str]] = []

    def start(self, toolbox_key: str) -> None:
        if self.active_toolbox_key is not None:
            raise CadenceError(f"toolbox_already_active:{self.active_toolbox_key}")
        self.active_toolbox_key = toolbox_key

    def record_gate(self, decision: str) -> None:
        if self.active_toolbox_key is None:
            raise CadenceError("no_active_toolbox")
        if decision == "FAIL":
            return
        if decision not in {"PASS", "EVIDENCE_INSUFFICIENT"}:
            raise CadenceError("invalid_stage_gate_decision")
        self.completed.append({"toolbox_key": self.active_toolbox_key, "decision": decision})
        self.active_toolbox_key = None


def validate_kernel_change(promoted_toolboxes: set[str], regression_results: dict[str, str]) -> KernelRegressionResult:
    missing_or_failed = tuple(sorted(key for key in promoted_toolboxes if regression_results.get(key) != "PASS"))
    return KernelRegressionResult(not missing_or_failed, tuple(sorted(promoted_toolboxes)), missing_or_failed)


def read_sample_manifest(path: Path, expected_toolbox_key: str) -> tuple[ToolboxSampleRecord, ...]:
    records: list[ToolboxSampleRecord] = []
    seen: set[str] = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
            record = ToolboxSampleRecord(
                sample_id=value["sample_id"],
                toolbox_key=value["toolbox_key"],
                split=SampleSplit(value["split"]),
                source_ref=value["source_ref"],
                sha256=value["sha256"],
                original_document_id=value["original_document_id"],
                original_page_number=int(value["original_page_number"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CadenceError(f"invalid_sample_manifest_line:{line_number}") from exc
        if record.toolbox_key != expected_toolbox_key:
            raise CadenceError(f"sample_toolbox_key_mismatch:{record.sample_id}")
        if record.sample_id in seen:
            raise CadenceError(f"duplicate_sample_id:{record.sample_id}")
        seen.add(record.sample_id)
        records.append(record)
    if not records:
        raise CadenceError("sample_manifest_is_empty")
    return tuple(records)


def validate_sample_partition(records: tuple[ToolboxSampleRecord, ...]) -> tuple[str, ...]:
    present = {record.split for record in records}
    return tuple(split.value for split in SampleSplit if split not in present)


def validate_acceptance_package(package_root: Path, toolbox_key: str, *, require_promotion: bool = True) -> AcceptanceResult:
    missing = [relative for relative in REQUIRED_ACCEPTANCE_PATHS if not (package_root / relative).is_file()]
    failed_reports: list[str] = []
    reasons: list[str] = []
    records: tuple[ToolboxSampleRecord, ...] = ()
    manifest = package_root / "samples" / "manifest.jsonl"
    if manifest.is_file():
        try:
            records = read_sample_manifest(manifest, toolbox_key)
            missing_splits = validate_sample_partition(records)
            if missing_splits:
                reasons.append("missing_sample_splits:" + ",".join(missing_splits))
        except CadenceError as exc:
            reasons.append(str(exc))

    for relative in PASS_REPORTS:
        path = package_root / relative
        if not path.is_file():
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if value.get("verdict") != "PASS":
                failed_reports.append(relative)
            _validate_evidence_refs(package_root, relative, value.get("evidence_refs"), failed_reports, reasons)
        except (json.JSONDecodeError, AttributeError):
            failed_reports.append(relative)

    gate_path = package_root / "stage_gate.json"
    if gate_path.is_file():
        try:
            gate = json.loads(gate_path.read_text(encoding="utf-8"))
            if gate.get("decision") != "PASS":
                failed_reports.append("stage_gate.json")
            _validate_evidence_refs(package_root, "stage_gate.json", gate.get("evidence_refs"), failed_reports, reasons)
        except (json.JSONDecodeError, AttributeError):
            failed_reports.append("stage_gate.json")

    if require_promotion:
        promotion_path = package_root / "promotion_manifest.json"
        if not promotion_path.is_file():
            missing.append("promotion_manifest.json")
        else:
            try:
                promotion = json.loads(promotion_path.read_text(encoding="utf-8"))
                if promotion.get("toolbox_key") != toolbox_key or promotion.get("status") != "PROMOTED":
                    failed_reports.append("promotion_manifest.json")
                _validate_evidence_refs(package_root, "promotion_manifest.json", promotion.get("evidence_refs"), failed_reports, reasons)
            except (json.JSONDecodeError, AttributeError):
                failed_reports.append("promotion_manifest.json")

    passed = not missing and not failed_reports and not reasons and bool(records)
    return AcceptanceResult(passed, tuple(sorted(set(missing))), tuple(sorted(set(failed_reports))), tuple(reasons))
