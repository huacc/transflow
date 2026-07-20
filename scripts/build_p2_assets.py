"""确定性生成 P2 路由分类、Toolbox Catalog、JSON Schema 和哈希清单。"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger("transflow.p2.assets")
REPO_ROOT = Path(__file__).resolve().parent.parent
LEDGER_PATH = REPO_ROOT / "docs" / "迁移" / "migration_ledger.json"
OUTPUT_PATHS = {
    "taxonomy": REPO_ROOT / "resources" / "taxonomy" / "page_classification_routes_v1.json",
    "catalog": REPO_ROOT / "resources" / "catalogs" / "page_toolbox_catalog_v1.json",
    "translation_schema": REPO_ROOT
    / "resources"
    / "schemas"
    / "translation_bundle_v1.schema.json",
    "decision_schema": REPO_ROOT / "resources" / "schemas" / "model_decision_v1.schema.json",
    "manifest": REPO_ROOT / "resources" / "manifests" / "p2_resource_fingerprints.json",
}


def render_json(payload: dict[str, Any]) -> bytes:
    """以固定键顺序和缩进生成可审查且跨运行稳定的 UTF-8 JSON。"""

    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    return text.encode("utf-8")


def load_migration_ledger() -> dict[str, Any]:
    """从仓库相对路径读取 P0 冻结的迁移台账。"""

    LOGGER.info("读取迁移台账，意图=复用 P0 冻结的路由和证据事实 path=%s", LEDGER_PATH)
    return json.loads(LEDGER_PATH.read_text(encoding="utf-8"))


def build_taxonomy(routes: list[str]) -> dict[str, Any]:
    """生成包含每条设计路由且顺序稳定的版本化分类。"""

    return {
        "schema_version": "transflow.page-route-taxonomy/v1",
        "routes": [{"ordinal": ordinal, "route": route} for ordinal, route in enumerate(routes)],
    }


def build_catalog(ledger: dict[str, Any], routes: list[str]) -> dict[str, Any]:
    """生成初始禁用 Catalog，并继承每个叶子的真实阶段证据。"""

    leaf_units = {
        str(unit["unit_id"]).removeprefix("toolbox.leaf."): unit
        for unit in ledger["units"]
        if unit.get("category") == "toolbox_leaf"
    }
    if set(leaf_units) != set(routes):
        missing = sorted(set(routes) - set(leaf_units))
        extra = sorted(set(leaf_units) - set(routes))
        raise ValueError(f"Toolbox 叶子与设计路由不一致 missing={missing} extra={extra}")
    entries = []
    for route in routes:
        unit = leaf_units[route]
        # P2 只冻结合同；没有生产晋升清单的 spike 证据不得打开生产路由。
        entries.append(
            {
                "disabled_reason": "PENDING_PRODUCTION_PROMOTION",
                "enabled": False,
                "evidence_refs": list(unit["evidence_ref"]),
                "evidence_status": unit["evidence_status"],
                "promotion_manifest_present": bool(unit.get("promotion_manifest_present", False)),
                "route": route,
                "toolbox_id": route,
            }
        )
    return {
        "schema_version": "transflow.page-toolbox-catalog/v1",
        "entries": entries,
    }


def build_translation_schema() -> dict[str, Any]:
    """生成严格禁止额外字段的 TranslationBundle JSON Schema。"""

    return {
        "$id": "https://transflow.local/schemas/translation_bundle_v1.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": False,
        "properties": {
            "batch_id": {"minLength": 1, "type": "string"},
            "requested_unit_ids": {
                "items": {"minLength": 1, "type": "string"},
                "minItems": 1,
                "type": "array",
                "uniqueItems": True,
            },
            "schema_version": {"const": "transflow.translation-bundle/v1"},
            "units": {
                "items": {
                    "additionalProperties": False,
                    "properties": {
                        "translated_text": {"minLength": 1, "type": "string"},
                        "unit_id": {"minLength": 1, "type": "string"},
                    },
                    "required": ["unit_id", "translated_text"],
                    "type": "object",
                },
                "minItems": 1,
                "type": "array",
            },
        },
        "required": ["schema_version", "batch_id", "requested_unit_ids", "units"],
        "title": "Transflow TranslationBundle v1",
        "type": "object",
    }


def build_decision_schema() -> dict[str, Any]:
    """生成非翻译类 ModelDecision 的结构化 JSON Schema。"""

    return {
        "$id": "https://transflow.local/schemas/model_decision_v1.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": False,
        "properties": {
            "confidence": {"maximum": 1, "minimum": 0, "type": "number"},
            "decision_id": {"minLength": 1, "type": "string"},
            "decision_kind": {"minLength": 1, "type": "string"},
            "evidence_ids": {
                "items": {"minLength": 1, "type": "string"},
                "type": "array",
                "uniqueItems": True,
            },
            "result_code": {"minLength": 1, "type": "string"},
            "reason_summary": {"type": "string"},
            "schema_version": {"const": "transflow.model-decision/v1"},
        },
        "required": [
            "schema_version",
            "decision_id",
            "decision_kind",
            "result_code",
            "evidence_ids",
        ],
        "title": "Transflow ModelDecision v1",
        "type": "object",
    }


def build_outputs() -> dict[Path, bytes]:
    """在内存中生成全部 P2 资源，避免部分写入产生漂移。"""

    ledger = load_migration_ledger()
    routes = [str(route) for route in ledger["route_behavior_keys"]]
    if len(routes) != len(set(routes)):
        raise ValueError("迁移台账包含重复路由")
    outputs = {
        OUTPUT_PATHS["taxonomy"]: render_json(build_taxonomy(routes)),
        OUTPUT_PATHS["catalog"]: render_json(build_catalog(ledger, routes)),
        OUTPUT_PATHS["translation_schema"]: render_json(build_translation_schema()),
        OUTPUT_PATHS["decision_schema"]: render_json(build_decision_schema()),
    }
    resources = []
    for path, content in sorted(outputs.items(), key=lambda item: item[0].as_posix()):
        payload = json.loads(content)
        resources.append(
            {
                "path": path.relative_to(REPO_ROOT).as_posix(),
                "schema_version": payload["schema_version"]
                if "schema_version" in payload
                else payload["properties"]["schema_version"]["const"],
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    outputs[OUTPUT_PATHS["manifest"]] = render_json(
        {"resources": resources, "schema_version": "transflow.p2-resource-fingerprints/v1"}
    )
    return outputs


def apply_outputs(outputs: dict[Path, bytes], check: bool) -> int:
    """检查或写入全部确定性输出，并逐项打印真实结果。"""

    drifted: list[str] = []
    for path, expected in outputs.items():
        relative = path.relative_to(REPO_ROOT).as_posix()
        if check:
            actual = path.read_bytes() if path.is_file() else None
            status = "PASS" if actual == expected else "FAIL"
            print(f"P2_ASSET_CHECK {status} path={relative}")
            if actual != expected:
                drifted.append(relative)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(expected)
        print(f"P2_ASSET_WRITE PASS path={relative}")
    if drifted:
        print(f"P2_ASSET_DRIFT FAIL count={len(drifted)} paths={drifted}")
        return 1
    print(f"P2_ASSET_DRIFT PASS count=0 checked={len(outputs)}")
    return 0


def parse_args() -> argparse.Namespace:
    """解析只检查模式开关。"""

    parser = argparse.ArgumentParser(description="生成或检查 Transflow P2 确定性资源")
    parser.add_argument("--check", action="store_true", help="只比较，不写文件")
    return parser.parse_args()


def main() -> int:
    """生成或检查 P2 版本化资源。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    args = parse_args()
    LOGGER.info("调用 P2 资源构建，意图=冻结路由、Catalog、Schema 和资源哈希 check=%s", args.check)
    return apply_outputs(build_outputs(), args.check)


if __name__ == "__main__":
    raise SystemExit(main())
