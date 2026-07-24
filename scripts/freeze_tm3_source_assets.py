"""Freeze the exact TM3 body.chart source and historical-evidence inputs."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
TM3_ROOT = REPO_ROOT / "runs" / "toolbox_leaf_migration" / "TM3"
PLAN_PATH = Path("docs/计划/Transflow_Toolbox逐类别核心逻辑迁移与全流程验收计划_v0.1.md")
AUTHORIZATION_PATH = Path(
    "resources/manifests/toolbox_leaf_migration/authorizations/tm3_chart.json"
)
SOURCE_GROUPS: dict[str, tuple[Path, ...]] = {
    "governing_documents": (
        Path("docs/背景/Transflow_关键链路重新验收与TM2_会话接手背景_20260721.md"),
        Path("docs/背景/Transflow_RV0-RV7与TM2最终限定范围重验_会话接手背景_20260723.md"),
        Path("docs/背景/PDF_翻译排版引擎_演进背景_既有资产与踩坑记录.md"),
        PLAN_PATH,
    ),
    "spike_experience": (
        Path("spikes/page_toolbox_engine_puncture_v1/docs/经验"),
    ),
    "chart_spike": (
        Path("spikes/page_toolbox_engine_puncture_v1/toolboxes/body/chart"),
    ),
    "classification_chart_pool": (
        Path("spikes/page_classification_engine_puncture_v1/分类结果/body/chart"),
    ),
    "production_surface": (
        Path("src/transflow"),
        Path("scripts/freeze_tm3_source_assets.py"),
        Path("scripts/run_toolbox_leaf_migration.py"),
        Path("scripts/toolbox_leaf_migration_drivers.py"),
        Path("scripts/toolbox_leaf_migration_single.py"),
        Path("scripts/verify_toolbox_leaf_migration.py"),
        Path("resources/catalogs/page_toolbox_catalog_v4.json"),
        Path("resources/manifests/toolbox_leaf_migration/tm0_baseline.json"),
        Path("resources/manifests/toolbox_leaf_migration/tm2_gate.json"),
        AUTHORIZATION_PATH,
    ),
    "test_surface": (
        Path("tests/test_toolbox_leaf_migration.py"),
        Path("tests/test_toolbox_leaf_migration_tm1.py"),
        Path("tests/test_toolbox_leaf_migration_tm2.py"),
        Path("tests/test_p8.py"),
        Path(
            "spikes/page_toolbox_engine_puncture_v1/toolboxes/body/chart/tests/test_chart.py"
        ),
    ),
}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_hash(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _relative(path: Path) -> str:
    return path.resolve().relative_to(REPO_ROOT.resolve()).as_posix()


def _files(path: Path) -> tuple[Path, ...]:
    target = REPO_ROOT / path
    if target.is_file():
        return (target,)
    if not target.is_dir():
        raise FileNotFoundError(path.as_posix())
    return tuple(
        item
        for item in sorted(target.rglob("*"))
        if item.is_file() and "__pycache__" not in item.parts and item.suffix != ".pyc"
    )


def _inventory() -> tuple[list[dict[str, Any]], dict[str, dict[str, int]]]:
    records: dict[str, dict[str, Any]] = {}
    group_stats: dict[str, dict[str, int]] = {}
    for group, roots in SOURCE_GROUPS.items():
        group_files = {item for root in roots for item in _files(root)}
        group_stats[group] = {
            "file_count": len(group_files),
            "byte_count": sum(item.stat().st_size for item in group_files),
        }
        for item in sorted(group_files):
            relative = _relative(item)
            record = records.setdefault(
                relative,
                {
                    "path": relative,
                    "sha256": _sha256_file(item),
                    "size": item.stat().st_size,
                    "groups": [],
                },
            )
            record["groups"].append(group)
    return [records[key] for key in sorted(records)], group_stats


def _git_snapshot() -> dict[str, Any]:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    status = subprocess.run(
        ["git", "status", "--short", "--untracked-files=all"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    ).stdout.splitlines()
    return {
        "head": head,
        "dirty": bool(status),
        "dirty_entry_count": len(status),
        "working_tree_policy": "PRESERVE_EXISTING_UNRELATED_CHANGES",
    }


def _chart_catalog_state() -> dict[str, Any]:
    catalog_path = REPO_ROOT / "resources/catalogs/page_toolbox_catalog_v4.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    entry = next(item for item in catalog["entries"] if item["route"] == "body.chart")
    stage_gate_path = (
        REPO_ROOT
        / "spikes/page_toolbox_engine_puncture_v1/toolboxes/body/chart/stage_gate.json"
    )
    stage_gate = json.loads(stage_gate_path.read_text(encoding="utf-8"))
    return {
        "default_catalog_entry": entry,
        "default_catalog_sha256": _sha256_file(catalog_path),
        "spike_stage_gate": stage_gate,
        "spike_stage_gate_sha256": _sha256_file(stage_gate_path),
        "production_implementation_before_tm3": (
            "ChartTextToolbox(TextPatchToolbox); no body.chart production driver"
        ),
        "promotion_allowed_during_technical_run": False,
        "catalog_overlay_required": True,
    }


def freeze(run_id: str) -> Path:
    run_root = TM3_ROOT / run_id
    if run_root.exists():
        raise FileExistsError(f"run already exists: {run_id}")
    for name in ("input", "process", "output"):
        (run_root / name).mkdir(parents=True, exist_ok=False)

    authorization = REPO_ROOT / AUTHORIZATION_PATH
    request = {
        "schema_version": "transflow.tm3-source-freeze-request/v1",
        "stage": "TM3",
        "route": "body.chart",
        "run_id": run_id,
        "authorization_ref": AUTHORIZATION_PATH.as_posix(),
        "authorization_sha256": _sha256_file(authorization),
        "artifact_root": "runs/toolbox_leaf_migration/TM3",
        "credential_material_in_scope": False,
    }
    _write_json(run_root / "input/request.json", request)

    records, group_stats = _inventory()
    inventory_payload = {
        "schema_version": "transflow.tm3-source-inventory/v1",
        "stage": "TM3",
        "route": "body.chart",
        "records": records,
        "group_stats": group_stats,
    }
    inventory_hash = _canonical_hash(inventory_payload)
    inventory_payload["inventory_hash"] = inventory_hash
    _write_json(run_root / "process/source_inventory.json", inventory_payload)

    status = {
        "schema_version": "transflow.tm3-source-status/v1",
        "git": _git_snapshot(),
        "python": {
            "executable": _relative(Path(sys.executable)),
            "version": sys.version.split()[0],
        },
        "chart_state": _chart_catalog_state(),
        "interpretation": {
            "spike_pass_non_blind_is_historical_hint_only": True,
            "spike_runtime_import_forbidden": True,
            "lightweight_text_patch_is_not_tm3_core_migration": True,
            "tm3_requires_new_forward_validation": True,
        },
    }
    _write_json(run_root / "process/source_status.json", status)

    frozen = {
        "schema_version": "transflow.tm3-frozen-source-set/v1",
        "state": "SOURCE_FROZEN",
        "inventory_hash": inventory_hash,
        "record_count": len(records),
        "byte_count": sum(item["size"] for item in records),
        "plan_ref": PLAN_PATH.as_posix(),
        "plan_sha256": _sha256_file(REPO_ROOT / PLAN_PATH),
        "source_inventory_ref": _relative(run_root / "process/source_inventory.json"),
        "source_status_ref": _relative(run_root / "process/source_status.json"),
    }
    _write_json(run_root / "output/frozen_source_set.json", frozen)

    trace = {
        "schema_version": "transflow.tm3-source-freeze-trace/v1",
        "run_id": run_id,
        "states": [
            {"state": "AUTHORIZED", "artifact": "input/request.json"},
            {"state": "SOURCE_INVENTORIED", "artifact": "process/source_inventory.json"},
            {"state": "BOUNDARY_RECORDED", "artifact": "process/source_status.json"},
            {"state": "SOURCE_FROZEN", "artifact": "output/frozen_source_set.json"},
        ],
    }
    _write_json(run_root / "trace_index.json", trace)
    manifest = {
        "schema_version": "transflow.tm3-source-freeze-run/v1",
        "stage": "TM3",
        "route": "body.chart",
        "run_id": run_id,
        "status": "PASS",
        "last_successful_state": "SOURCE_FROZEN",
        "inventory_hash": inventory_hash,
        "input_ref": "input/request.json",
        "process_refs": [
            "process/source_inventory.json",
            "process/source_status.json",
        ],
        "output_ref": "output/frozen_source_set.json",
        "candidate_artifacts": {
            "present": False,
            "reason": "SOURCE_FREEZE_ROUND",
        },
        "next_state": "RED_TESTS",
    }
    _write_json(run_root / "run_manifest.json", manifest)
    (run_root / "report.md").write_text(
        "# TM3 body.chart 源资产冻结\n\n"
        f"- 轮次：`{run_id}`\n"
        f"- 状态：`SOURCE_FROZEN`\n"
        f"- 文件数：`{len(records)}`\n"
        f"- 资产字节数：`{sum(item['size'] for item in records)}`\n"
        f"- 清单哈希：`{inventory_hash}`\n\n"
        "本轮只冻结计划、Spike 核心与历史产物、分类 chart 池、生产边界和测试面；"
        "不修改默认 Catalog，不调用模型，不生成候选 PDF。"
        "Spike 的 `PASS_NON_BLIND` 只作为迁移提示，不作为 TM3 晋级证据。\n",
        encoding="utf-8",
    )
    return run_root


def main() -> int:
    parser = argparse.ArgumentParser(description="Freeze TM3 body.chart source assets")
    parser.add_argument(
        "--run-id",
        default=f"01-source-freeze-{datetime.now():%Y%m%d-%H%M%S}",
    )
    arguments = parser.parse_args()
    run_root = freeze(arguments.run_id)
    print(f"TM3_SOURCE_FROZEN run={_relative(run_root)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
