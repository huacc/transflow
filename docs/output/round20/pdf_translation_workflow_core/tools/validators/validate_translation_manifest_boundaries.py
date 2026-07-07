"""Write workspace-boundary reports for every D2 translation batch in a manifest.

tool_name: validate_translation_manifest_boundaries
category: validators
input_contract: translation_batch_manifest.json with per-batch artifact refs
output_contract: one workspace_boundary_ref JSON per batch plus a summary JSON
failure_signals: any planned D2 read/write artifact resolves outside workspace root
fallback: route to S_FAIL_PROCESS_CONTRACT before D2 materialization
anti_overfit_statement: validates filesystem containment only and never inspects sample filenames, page numbers, text, coordinates, colors, or document identity
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import read_json, rel, resolve_workspace_path, write_json  # noqa: E402


BATCH_REF_FIELDS = [
    "slot_values_ref",
    "prompt_instance_ref",
    "model_output_ref",
    "batch_validation_ref",
    "decision_record_ref",
]


def is_under(path: Path, root: Path) -> bool:
    root_text = os.path.normcase(str(root.resolve()))
    path_text = os.path.normcase(str(path.resolve(strict=False)))
    try:
        return os.path.commonpath([root_text, path_text]) == root_text
    except ValueError:
        return False


def resolve_ref(ref: str, workspace_root: Path) -> Path:
    path = Path(ref)
    if not path.is_absolute():
        path = workspace_root / path
    return path.resolve(strict=False)


def boundary_for_batch(batch: dict[str, Any], workspace_root: Path) -> dict[str, Any]:
    checked = []
    escaping = []
    for field in BATCH_REF_FIELDS:
        ref = str(batch.get(field) or "")
        resolved = resolve_ref(ref, workspace_root)
        item = {
            "field": field,
            "path": ref,
            "resolved": str(resolved),
            "inside_workspace": is_under(resolved, workspace_root),
            "exists": resolved.exists(),
            "allow_missing": field != "slot_values_ref",
        }
        checked.append(item)
        if not item["inside_workspace"]:
            escaping.append(item)
        if field == "slot_values_ref" and not resolved.exists():
            escaping.append({**item, "missing_required_input": True})
    return {
        "tool": "validate_translation_manifest_boundaries",
        "batch_id": batch.get("batch_id"),
        "workspace_root": str(workspace_root),
        "workspace_boundary_verdict": "PASS" if not escaping else "FAIL",
        "checked_paths": checked,
        "escaping_paths": escaping,
    }


def validate_manifest(manifest_path: Path, out_path: Path, workspace_root: Path) -> dict[str, Any]:
    manifest = read_json(manifest_path)
    records = []
    failed = []
    for batch in manifest.get("batches", []):
        boundary_ref = str(batch.get("workspace_boundary_ref") or "")
        if not boundary_ref:
            failed.append({"batch_id": batch.get("batch_id"), "reason": "missing workspace_boundary_ref"})
            continue
        report = boundary_for_batch(batch, workspace_root)
        report_path = resolve_ref(boundary_ref, workspace_root)
        if not is_under(report_path, workspace_root):
            report["workspace_boundary_verdict"] = "FAIL"
            report.setdefault("escaping_paths", []).append(
                {
                    "field": "workspace_boundary_ref",
                    "path": boundary_ref,
                    "resolved": str(report_path),
                    "inside_workspace": False,
                }
            )
        else:
            write_json(report_path, report)
        records.append(
            {
                "batch_id": batch.get("batch_id"),
                "workspace_boundary_ref": rel(report_path),
                "workspace_boundary_verdict": report["workspace_boundary_verdict"],
            }
        )
        if report["workspace_boundary_verdict"] != "PASS":
            failed.append(records[-1])
    result = {
        "tool": "validate_translation_manifest_boundaries",
        "manifest": rel(manifest_path),
        "workspace_root": str(workspace_root),
        "batch_count": len(manifest.get("batches", [])),
        "validated_batch_count": len(records),
        "workspace_boundary_verdict": "PASS" if not failed else "FAIL",
        "failed_batches": failed,
        "records": records,
    }
    write_json(out_path, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--workspace-root", default=".")
    args = parser.parse_args()
    workspace_root = Path(args.workspace_root).resolve()
    result = validate_manifest(resolve_workspace_path(args.manifest), resolve_workspace_path(args.out), workspace_root)
    print(resolve_workspace_path(args.out))
    print(f"workspace_boundary_verdict={result['workspace_boundary_verdict']} batch_count={result['batch_count']}")
    return 0 if result["workspace_boundary_verdict"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
