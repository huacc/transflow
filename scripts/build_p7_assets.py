"""确定性生成 P7 Catalog、Margin 策略、叶证据 Schema、模板和状态清单。"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from transflow.toolboxes.catalog import catalog_entry_fingerprint
from transflow.toolboxes.contracts import TOOLBOX_CONTRACT_VERSION

LOGGER = logging.getLogger("transflow.p7.assets")
REPO_ROOT = Path(__file__).resolve().parent.parent
P2_CATALOG_PATH = REPO_ROOT / "resources" / "catalogs" / "page_toolbox_catalog_v1.json"
LEDGER_PATH = REPO_ROOT / "docs" / "迁移" / "migration_ledger.json"
OUTPUT_PATHS = {
    "catalog": REPO_ROOT / "resources" / "catalogs" / "page_toolbox_catalog_v2.json",
    "margin": REPO_ROOT / "resources" / "manifests" / "p7_margin_policy.json",
    "schema": REPO_ROOT / "resources" / "schemas" / "leaf_migration_evidence_v1.schema.json",
    "template": REPO_ROOT / "docs" / "迁移" / "p7_leaf_migration_template.json",
    "state": REPO_ROOT / "docs" / "迁移" / "p7_leaf_initial_state.json",
    "manifest": REPO_ROOT / "resources" / "manifests" / "p7_resource_fingerprints.json",
}


def render_json(payload: dict[str, Any]) -> bytes:
    """以固定键顺序和缩进生成可审查的 UTF-8 JSON。"""

    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def build_catalog(p2_catalog: dict[str, Any]) -> dict[str, Any]:
    """把 P2 全禁用路由提升为带版本、指纹、证明和 fallback 的 v2 Catalog。"""

    entries = []
    for item in p2_catalog["entries"]:
        route = str(item["route"])
        toolbox_key = str(item["toolbox_id"])
        toolbox_version = "0.0.0-pending"
        entries.append(
            {
                "contract_version": TOOLBOX_CONTRACT_VERSION,
                "disabled_reason": "P7_SKELETON_ONLY_PENDING_LEAF_GATE",
                "enabled": False,
                "evidence_attestation_hash": None,
                "evidence_state": "PASS_DISABLED_WITH_FALLBACK",
                "fallback": "PAGE_PASSTHROUGH",
                "fingerprint": catalog_entry_fingerprint(
                    route,
                    toolbox_key,
                    toolbox_version,
                    TOOLBOX_CONTRACT_VERSION,
                ),
                "route": route,
                "toolbox_key": toolbox_key,
                "toolbox_version": toolbox_version,
            }
        )
    return {"schema_version": "transflow.page-toolbox-catalog/v2", "entries": entries}


def build_margin_policy() -> dict[str, Any]:
    """生成只依赖归一化几何和跨页/跨叶重复证据的冻结策略。"""

    return {
        "schema_version": "transflow.margin-policy/v1",
        "top_ratio": 0.14,
        "bottom_ratio": 0.86,
        "minimum_page_fraction": 0.6,
        "minimum_repeated_pages": 2,
        "minimum_distinct_routes": 2,
    }


def build_leaf_schema() -> dict[str, Any]:
    """生成禁止额外字段的叶迁移证据 JSON Schema。"""

    string_fields = (
        "route",
        "source_path",
        "source_hash",
        "original_state",
        "target_toolbox_key",
        "target_version",
        "fallback",
        "owner",
        "code_hash",
        "schema_hash",
        "catalog_hash",
        "font_hash",
        "threshold_hash",
        "evidence_hash",
    )
    array_fields = (
        "allowed_changes",
        "migration_differences",
        "fixture_refs",
        "gold_refs",
        "threshold_refs",
        "limitations",
    )
    boolean_fields = (
        "contract_passed",
        "equivalence_passed",
        "blind_passed",
        "anti_overfit_passed",
        "failure_passed",
        "document_e2e_passed",
        "fallback_has_page_outcome",
        "fallback_has_complete_pdf",
        "new_evidence",
    )
    properties: dict[str, Any] = {
        "schema_version": {"const": "transflow.leaf-migration-evidence/v1"}
    }
    for field_name in string_fields:
        properties[field_name] = {"minLength": 1, "type": "string"}
    for field_name in array_fields:
        properties[field_name] = {
            "items": {"minLength": 1, "type": "string"},
            "type": "array",
            "uniqueItems": True,
        }
    for field_name in boolean_fields:
        properties[field_name] = {"type": "boolean"}
    required = ["schema_version", *string_fields, *array_fields, *boolean_fields]
    return {
        "$id": "https://transflow.local/schemas/leaf_migration_evidence_v1.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": False,
        "properties": properties,
        "required": required,
        "schema_version": "transflow.leaf-migration-evidence-schema/v1",
        "title": "Transflow Leaf Migration Evidence v1",
        "type": "object",
    }


def build_leaf_template(schema: dict[str, Any]) -> dict[str, Any]:
    """生成 P8 起每个叶复制填写的显式模板和唯一三态集合。"""

    return {
        "schema_version": "transflow.leaf-migration-template/v1",
        "evidence_schema": "resources/schemas/leaf_migration_evidence_v1.schema.json",
        "required_fields": schema["required"],
        "allowed_conclusions": [
            "PASS_ENABLE",
            "PASS_DISABLED_WITH_FALLBACK",
            "FAIL",
        ],
        "publication_rule": (
            "enabled 仅允许匹配 PASS_ENABLE 的版本、evidence hash 和 attestation hash"
        ),
        "invalidation_inputs": [
            "code_hash",
            "schema_hash",
            "catalog_hash",
            "font_hash",
            "threshold_hash",
        ],
    }


def build_initial_state(
    p2_catalog: dict[str, Any],
    ledger: dict[str, Any],
) -> dict[str, Any]:
    """导入每个旧叶真实成熟度，但一律保持禁用并指定完整文档 fallback。"""

    leaf_units = {
        str(item["unit_id"]).removeprefix("toolbox.leaf."): item
        for item in ledger["units"]
        if item.get("category") == "toolbox_leaf"
    }
    leaves = []
    for entry in p2_catalog["entries"]:
        route = str(entry["route"])
        source = leaf_units[route]
        leaves.append(
            {
                "conclusion": "PASS_DISABLED_WITH_FALLBACK",
                "enabled": False,
                "fallback": "PAGE_PASSTHROUGH",
                "original_state": str(entry["evidence_status"]),
                "promotion_manifest_present": bool(entry["promotion_manifest_present"]),
                "route": route,
                "source_hash": str(source["source_hash"]),
                "source_path": str(source["source_path"]),
                "upgrade_performed": False,
            }
        )
    return {"schema_version": "transflow.p7-leaf-initial-state/v1", "leaves": leaves}


def build_outputs() -> dict[Path, bytes]:
    """在内存中生成全部 P7 资源，避免部分写入形成伪基线。"""

    p2_catalog = json.loads(P2_CATALOG_PATH.read_text(encoding="utf-8"))
    ledger = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    schema = build_leaf_schema()
    outputs = {
        OUTPUT_PATHS["catalog"]: render_json(build_catalog(p2_catalog)),
        OUTPUT_PATHS["margin"]: render_json(build_margin_policy()),
        OUTPUT_PATHS["schema"]: render_json(schema),
        OUTPUT_PATHS["template"]: render_json(build_leaf_template(schema)),
        OUTPUT_PATHS["state"]: render_json(build_initial_state(p2_catalog, ledger)),
    }
    resources = [
        {
            "path": path.relative_to(REPO_ROOT).as_posix(),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        for path, content in sorted(outputs.items(), key=lambda item: item[0].as_posix())
    ]
    outputs[OUTPUT_PATHS["manifest"]] = render_json(
        {"schema_version": "transflow.p7-resource-fingerprints/v1", "resources": resources}
    )
    return outputs


def apply_outputs(outputs: dict[Path, bytes], check: bool) -> int:
    """检查或写入全部确定性输出，并打印每个文件的真实状态。"""

    drifted: list[str] = []
    for path, expected in outputs.items():
        relative = path.relative_to(REPO_ROOT).as_posix()
        if check:
            actual = path.read_bytes() if path.is_file() else None
            status = "PASS" if actual == expected else "FAIL"
            print(f"P7_ASSET_CHECK {status} path={relative}")
            if actual != expected:
                drifted.append(relative)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(expected)
        print(f"P7_ASSET_WRITE PASS path={relative}")
    status = "PASS" if not drifted else "FAIL"
    print(f"P7_ASSET_DRIFT {status} count={len(drifted)} checked={len(outputs)}")
    return 0 if not drifted else 1


def parse_args() -> argparse.Namespace:
    """解析只检查模式开关。"""

    parser = argparse.ArgumentParser(description="生成或检查 Transflow P7 确定性资源")
    parser.add_argument("--check", action="store_true", help="只比较，不写文件")
    return parser.parse_args()


def main() -> int:
    """生成或核对 P7 静态资源。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    args = parse_args()
    LOGGER.info("调用 P7 资源构建，意图=冻结 Catalog、Margin 和叶 Gate 资源 check=%s", args.check)
    return apply_outputs(build_outputs(), args.check)


if __name__ == "__main__":
    raise SystemExit(main())
