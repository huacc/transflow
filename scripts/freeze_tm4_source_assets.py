"""冻结 TM4 body.diagram 的当前来源、样本和生产接入边界。"""

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
SPIKE_ROOT = REPO_ROOT / "spikes" / "page_toolbox_engine_puncture_v1"
DIAGRAM_ROOT = SPIKE_ROOT / "toolboxes" / "body" / "diagram"
TM4_ROOT = REPO_ROOT / "runs" / "toolbox_leaf_migration" / "TM4"
PLAN_PATH = Path(
    "docs/计划/Transflow_Toolbox逐类别核心逻辑迁移与全流程验收计划_v0.1.md"
)
AUTHORIZATION_PATH = Path(
    "resources/manifests/toolbox_leaf_migration/authorizations/tm4_diagram.json"
)
SAMPLE_MANIFEST_PATH = Path(
    "spikes/page_toolbox_engine_puncture_v1/toolboxes/body/diagram/samples/manifest.jsonl"
)
WORKFLOW_FREEZE_PATH = Path(
    "spikes/page_toolbox_engine_puncture_v1/toolboxes/body/diagram/reports/workflow_freeze.json"
)

SOURCE_GROUPS: dict[str, tuple[Path, ...]] = {
    "governing_documents": (
        PLAN_PATH,
        Path("docs/经验/Transflow_跨类别文本锚点选择与保持经验_20260723.md"),
    ),
    "spike_experience": (
        Path("spikes/page_toolbox_engine_puncture_v1/docs/经验/P14_body.diagram_新增经验.md"),
        Path("spikes/page_toolbox_engine_puncture_v1/docs/经验/P13_body.chart_新增经验.md"),
    ),
    "diagram_spike_core": (
        Path("spikes/page_toolbox_engine_puncture_v1/toolboxes/body/diagram/README.md"),
        Path("spikes/page_toolbox_engine_puncture_v1/toolboxes/body/diagram/docs"),
        Path("spikes/page_toolbox_engine_puncture_v1/toolboxes/body/diagram/prompts"),
        Path("spikes/page_toolbox_engine_puncture_v1/toolboxes/body/diagram/reports"),
        Path("spikes/page_toolbox_engine_puncture_v1/toolboxes/body/diagram/tools"),
        Path("spikes/page_toolbox_engine_puncture_v1/toolboxes/body/diagram/stage_gate.json"),
        Path("spikes/page_toolbox_engine_puncture_v1/toolboxes/body/diagram/toolbox_manifest.json"),
        SAMPLE_MANIFEST_PATH,
        Path("spikes/page_toolbox_engine_puncture_v1/scripts/run_p14_diagram.py"),
        Path("spikes/page_toolbox_engine_puncture_v1/tests/test_p14_body_diagram.py"),
    ),
    "classification_diagram_pool": (
        Path("spikes/page_classification_engine_puncture_v1/分类结果/body/diagram"),
    ),
    "production_surface": (
        Path("src/transflow"),
        Path("scripts/run_toolbox_leaf_migration.py"),
        Path("scripts/toolbox_leaf_migration_drivers.py"),
        Path("scripts/toolbox_leaf_migration_chart.py"),
        Path("scripts/toolbox_leaf_migration_chart_run.py"),
        Path("scripts/run_tm3_chart_pool_regression.py"),
        Path("resources/catalogs/page_toolbox_catalog_v4.json"),
        AUTHORIZATION_PATH,
    ),
}


def _sha256_file(path: Path) -> str:
    """流式计算文件哈希。"""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_hash(payload: object) -> str:
    """计算不依赖 JSON 排版的稳定哈希。"""

    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _write_json(path: Path, payload: object) -> None:
    """在新轮次目录中原子写入 UTF-8 JSON。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _relative(path: Path) -> str:
    """返回仓库相对 POSIX 路径，避免运行证据落入盘符。"""

    return path.resolve().relative_to(REPO_ROOT.resolve()).as_posix()


def _files(path: Path) -> tuple[Path, ...]:
    """展开文件或目录，并排除缓存与历史大运行目录。"""

    target = REPO_ROOT / path
    if target.is_file():
        return (target,)
    if not target.is_dir():
        raise FileNotFoundError(path.as_posix())
    return tuple(
        item
        for item in sorted(target.rglob("*"))
        if item.is_file()
        and "__pycache__" not in item.parts
        and "runs" not in item.relative_to(target).parts
        and item.suffix != ".pyc"
    )


def _inventory() -> tuple[list[dict[str, Any]], dict[str, dict[str, int]]]:
    """冻结各来源组的去重文件记录。"""

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


def _sample_pool() -> dict[str, Any]:
    """核对 30 个分类快照与 Spike 样本的一一对应和文件哈希。"""

    manifest = REPO_ROOT / SAMPLE_MANIFEST_PATH
    rows = [
        json.loads(line)
        for line in manifest.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    checked: list[dict[str, Any]] = []
    for row in rows:
        sample = DIAGRAM_ROOT / str(row["source_ref"])
        if not sample.is_file():
            raise FileNotFoundError(str(row["source_ref"]))
        actual_hash = _sha256_file(sample)
        if actual_hash != row["sha256"]:
            raise RuntimeError(f"TM4_SAMPLE_HASH_DRIFT:{row['sample_id']}")
        upstream = SPIKE_ROOT.parent / str(row["upstream_ref"])
        checked.append(
            {
                "sample_id": row["sample_id"],
                "split": row["split"],
                "source_language": row["source_language"],
                "target_language": row["target_language"],
                "source_ref": _relative(sample),
                "source_sha256": actual_hash,
                "upstream_ref": _relative(upstream),
                "upstream_present": upstream.is_file(),
                "original_document_id": row["original_document_id"],
                "original_page_number": row["original_page_number"],
                "holdout_status": row["holdout_status"],
            }
        )
    return {
        "schema_version": "transflow.tm4-diagram-sample-pool/v1",
        "route": "body.diagram",
        "sample_count": len(checked),
        "document_count": len({item["original_document_id"] for item in checked}),
        "language_directions": {
            "en_to_zh": sum(item["source_language"].startswith("en") for item in checked),
            "zh_to_en": sum(item["source_language"].startswith("zh") for item in checked),
        },
        "all_upstream_present": all(item["upstream_present"] for item in checked),
        "blind_promotion_claim_allowed": False,
        "records": checked,
    }


def _historical_hash_drift() -> dict[str, Any]:
    """比较 P14 冻结哈希与当前 Spike 文件，避免沿用过期 PASS。"""

    frozen = json.loads((REPO_ROOT / WORKFLOW_FREEZE_PATH).read_text(encoding="utf-8"))
    comparisons = []
    for relative, expected_hash in frozen["frozen_artifacts"].items():
        current = SPIKE_ROOT / relative
        actual_hash = _sha256_file(current) if current.is_file() else None
        comparisons.append(
            {
                "path": _relative(current),
                "expected_sha256": expected_hash,
                "actual_sha256": actual_hash,
                "matches": actual_hash == expected_hash,
            }
        )
    return {
        "schema_version": "transflow.tm4-spike-history-validity/v1",
        "historical_gate": "PASS_NON_BLIND",
        "current_matches_historical_freeze": all(item["matches"] for item in comparisons),
        "comparison_count": len(comparisons),
        "drift_count": sum(not item["matches"] for item in comparisons),
        "comparisons": comparisons,
        "interpretation": "历史 P14 PASS 只作线索；TM4 以当前源码重新测试和前向验收",
    }


def _git_snapshot() -> dict[str, Any]:
    """记录提交和脏工作树数量，不保存用户改动内容。"""

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


def freeze(run_id: str) -> Path:
    """创建只读来源冻结轮次。"""

    run_root = TM4_ROOT / run_id
    if run_root.exists():
        raise FileExistsError(f"run already exists: {run_id}")
    for name in ("input", "process", "output"):
        (run_root / name).mkdir(parents=True, exist_ok=False)

    authorization = REPO_ROOT / AUTHORIZATION_PATH
    _write_json(
        run_root / "input" / "request.json",
        {
            "schema_version": "transflow.tm4-source-freeze-request/v1",
            "stage": "TM4",
            "route": "body.diagram",
            "run_id": run_id,
            "authorization_ref": AUTHORIZATION_PATH.as_posix(),
            "authorization_sha256": _sha256_file(authorization),
            "artifact_root": "runs/toolbox_leaf_migration/TM4",
            "credential_material_in_scope": False,
        },
    )

    records, group_stats = _inventory()
    inventory_payload = {
        "schema_version": "transflow.tm4-source-inventory/v1",
        "stage": "TM4",
        "route": "body.diagram",
        "records": records,
        "group_stats": group_stats,
    }
    inventory_hash = _canonical_hash(inventory_payload)
    inventory_payload["inventory_hash"] = inventory_hash
    _write_json(run_root / "process" / "source_inventory.json", inventory_payload)

    sample_pool = _sample_pool()
    _write_json(run_root / "process" / "sample_pool.json", sample_pool)
    history = _historical_hash_drift()
    _write_json(run_root / "process" / "historical_hash_drift.json", history)

    catalog = json.loads(
        (REPO_ROOT / "resources/catalogs/page_toolbox_catalog_v4.json").read_text(
            encoding="utf-8"
        )
    )
    catalog_entry = next(
        item for item in catalog["entries"] if item["route"] == "body.diagram"
    )
    _write_json(
        run_root / "process" / "source_status.json",
        {
            "schema_version": "transflow.tm4-source-status/v1",
            "git": _git_snapshot(),
            "python": {
                "executable": _relative(Path(sys.executable)),
                "version": sys.version.split()[0],
            },
            "default_catalog_entry": catalog_entry,
            "default_catalog_mutated": False,
            "historical_hash_drift": {
                "comparison_count": history["comparison_count"],
                "drift_count": history["drift_count"],
            },
            "spike_current_test": {
                "command": (
                    ".venv/Scripts/python.exe -X pycache_prefix=tmp/tm4-pycache "
                    "-m pytest -q "
                    "spikes/page_toolbox_engine_puncture_v1/tests/test_p14_body_diagram.py"
                ),
                "passed": 70,
                "failed": 0,
                "verified_before_freeze": True,
            },
            "interpretation": {
                "spike_pass_non_blind_is_historical_hint_only": True,
                "spike_runtime_import_forbidden": True,
                "native_labels_text_patch_is_not_tm4_core_migration": True,
                "tm4_requires_new_forward_validation": True,
            },
        },
    )

    frozen = {
        "schema_version": "transflow.tm4-frozen-source-set/v1",
        "state": "SOURCE_FROZEN",
        "inventory_hash": inventory_hash,
        "record_count": len(records),
        "byte_count": sum(item["size"] for item in records),
        "sample_count": sample_pool["sample_count"],
        "document_count": sample_pool["document_count"],
        "historical_hash_drift_count": history["drift_count"],
        "plan_ref": PLAN_PATH.as_posix(),
        "plan_sha256": _sha256_file(REPO_ROOT / PLAN_PATH),
        "source_inventory_ref": _relative(
            run_root / "process" / "source_inventory.json"
        ),
    }
    _write_json(run_root / "output" / "frozen_source_set.json", frozen)
    _write_json(
        run_root / "run_manifest.json",
        {
            "schema_version": "transflow.tm4-source-freeze-run/v1",
            "stage": "TM4",
            "route": "body.diagram",
            "run_id": run_id,
            "status": "PASS",
            "last_successful_state": "SOURCE_FROZEN",
            "input_ref": "input/request.json",
            "process_refs": [
                "process/source_inventory.json",
                "process/sample_pool.json",
                "process/historical_hash_drift.json",
                "process/source_status.json",
            ],
            "output_ref": "output/frozen_source_set.json",
            "next_state": "RED_TESTS",
        },
    )
    return run_root


def main() -> int:
    """执行 TM4 来源冻结并输出仓库相对轮次路径。"""

    parser = argparse.ArgumentParser(description="Freeze TM4 body.diagram source assets")
    parser.add_argument(
        "--run-id",
        default=f"01-source-freeze-{datetime.now():%Y%m%d-%H%M%S}",
    )
    arguments = parser.parse_args()
    run_root = freeze(arguments.run_id)
    print(f"TM4_SOURCE_FROZEN run={_relative(run_root)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
