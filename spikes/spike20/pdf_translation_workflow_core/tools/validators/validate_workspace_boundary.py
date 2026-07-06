"""Validate that run artifacts stay inside the declared workspace root.

tool_name: validate_workspace_boundary
category: validators
input_contract: workspace root plus planned or observed artifact paths
output_contract: JSON report with resolved paths and PASS/FAIL boundary verdict
failure_signals: output path outside workspace, artifact path outside workspace, required existing path missing
fallback: S_FAIL_PROCESS_CONTRACT before continuing the state that wanted to read or write the artifact
anti_overfit_statement: validates filesystem containment only and does not inspect sample identity, page numbers, text, colors, or coordinates
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import WORKSPACE_ROOT, write_json  # noqa: E402


PATH_FIELDS = {
    "path",
    "ref",
    "source_pdf",
    "output_pdf",
    "input_pdf",
    "source_extraction",
    "semantic_translations",
    "layout_policy",
    "generation_evidence",
    "candidate_pdf",
    "out",
    "manifest",
    "input_artifacts",
    "output_artifacts",
    "evidence_refs",
}


def resolve_root(root_text: str | None) -> Path:
    if root_text:
        return Path(root_text).expanduser().resolve()
    return WORKSPACE_ROOT.resolve()


def resolve_artifact_path(workspace_root: Path, path_text: str) -> Path:
    path = Path(path_text).expanduser()
    if not path.is_absolute():
        path = workspace_root / path
    return path.resolve(strict=False)


def is_under(path: Path, workspace_root: Path) -> bool:
    root_text = os.path.normcase(str(workspace_root.resolve()))
    path_text = os.path.normcase(str(path.resolve(strict=False)))
    try:
        return os.path.commonpath([root_text, path_text]) == root_text
    except ValueError:
        return False


def looks_like_artifact_path(value: str) -> bool:
    if not value or "\n" in value:
        return False
    if value.startswith(("http://", "https://")):
        return False
    return (
        "/" in value
        or "\\" in value
        or value.endswith((".json", ".jsonl", ".pdf", ".png", ".md", ".tsv", ".txt"))
    )


def collect_paths_from_json(value: Any, parent_key: str = "") -> list[str]:
    paths: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in PATH_FIELDS or key.endswith(("_path", "_ref", "_file", "_dir")):
                paths.extend(collect_paths_from_json(item, key))
            elif isinstance(item, (dict, list)):
                paths.extend(collect_paths_from_json(item, key))
    elif isinstance(value, list):
        for item in value:
            paths.extend(collect_paths_from_json(item, parent_key))
    elif isinstance(value, str) and (parent_key in PATH_FIELDS or looks_like_artifact_path(value)):
        paths.append(value)
    return paths


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records


def load_path_inputs(workspace_root: Path, args: argparse.Namespace) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path_text in args.path or []:
        records.append({"source": "cli", "path": path_text})

    for json_path_text in args.paths_json or []:
        json_path = resolve_artifact_path(workspace_root, json_path_text)
        payload = json.loads(json_path.read_text(encoding="utf-8-sig"))
        for path_text in collect_paths_from_json(payload):
            records.append({"source": str(json_path), "path": path_text})

    for log_path_text in args.operation_log or []:
        log_path = resolve_artifact_path(workspace_root, log_path_text)
        for idx, record in enumerate(read_jsonl(log_path)):
            for field in ("input_artifacts", "output_artifacts", "evidence_refs"):
                for path_text in collect_paths_from_json(record.get(field, []), field):
                    records.append({"source": f"{log_path}:{idx + 1}:{field}", "path": path_text})
    return records


def validate_paths(workspace_root: Path, records: list[dict[str, Any]], allow_missing: bool) -> dict[str, Any]:
    checked: list[dict[str, Any]] = []
    escaping: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for record in records:
        path_text = str(record["path"])
        key = (str(record.get("source", "")), path_text)
        if key in seen:
            continue
        seen.add(key)
        resolved = resolve_artifact_path(workspace_root, path_text)
        inside = is_under(resolved, workspace_root)
        exists = resolved.exists()
        item = {
            "source": record.get("source"),
            "path": path_text,
            "resolved": str(resolved),
            "inside_workspace": inside,
            "exists": exists,
        }
        checked.append(item)
        if not inside:
            escaping.append(item)
        if not allow_missing and not exists:
            missing.append(item)

    return {
        "tool": "validate_workspace_boundary",
        "workspace_root": str(workspace_root),
        "allow_missing": allow_missing,
        "workspace_boundary_verdict": "PASS" if not escaping and not missing else "FAIL",
        "checked_count": len(checked),
        "checked_paths": checked,
        "escaping_paths": escaping,
        "missing_paths": missing,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace-root", default=None)
    parser.add_argument("--path", action="append", default=[])
    parser.add_argument("--paths-json", action="append", default=[])
    parser.add_argument("--operation-log", action="append", default=[])
    parser.add_argument("--allow-missing", action="store_true")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    workspace_root = resolve_root(args.workspace_root)
    out_path = resolve_artifact_path(workspace_root, args.out)
    if not is_under(out_path, workspace_root):
        print(f"output path outside workspace: {out_path}", file=sys.stderr)
        return 2

    path_records = load_path_inputs(workspace_root, args)
    result = validate_paths(workspace_root, path_records, args.allow_missing)
    result["output_report"] = str(out_path)
    write_json(out_path, result)
    print(out_path)
    return 0 if result["workspace_boundary_verdict"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
