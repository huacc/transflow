"""从统一配置加载 P9B 叶级 RepairAtomCatalog 和版本化 comparator。"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from transflow.domain.common import content_sha256
from transflow.domain.errors import DomainContractError, ErrorCode
from transflow.domain.repair_memory import (
    BoundedRepairParameter,
    ComparatorMetric,
    MetricDirection,
    RepairAtom,
    RepairAtomCatalog,
    RepairComparison,
    RepairRuleRegistry,
)

LOGGER = logging.getLogger("transflow.application.repair_catalog")
APPLICATION_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True, slots=True)
class RepairPolicySnapshot:
    """保存一次加载后冻结的 P9B 配置、目录和审计指纹。"""

    config_hash: str
    max_repair_rounds: int
    max_no_improvement: int
    real_sample_pressure_factor: int
    catalogs: tuple[RepairAtomCatalog, ...]
    comparators: tuple[tuple[str, RepairComparison], ...]
    static_registry: RepairRuleRegistry

    def resolve(self, route: str) -> tuple[RepairAtomCatalog, RepairComparison]:
        """按唯一 Route 返回匹配目录与 comparator，不进行目录扫描或猜测。"""

        LOGGER.info("调用修复目录解析，意图=选择叶私有确定性动作 route=%s", route)
        catalogs = tuple(item for item in self.catalogs if item.route == route)
        comparators = tuple(item for key, item in self.comparators if key == route)
        if len(catalogs) != 1 or len(comparators) != 1:
            raise DomainContractError(ErrorCode.INVALID_IDENTITY, "修复 Route 必须唯一登记")
        if catalogs[0].comparator_hash != comparators[0].comparator_hash:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "Catalog/comparator 指纹漂移")
        return catalogs[0], comparators[0]


def load_repair_policy(path: Path) -> RepairPolicySnapshot:
    """从调用方给出的统一配置文件加载 P9B 快照并冻结全部派生指纹。"""

    LOGGER.info("调用 P9B 配置加载，意图=冻结预算、目录和比较合同 path=%s", path)
    content = path.read_bytes()
    payload = json.loads(content.decode("utf-8"))
    if payload.get("schema_version") != "transflow.repair-policy/v1":
        raise DomainContractError(ErrorCode.INVALID_CONTRACT, "P9B 配置版本不受支持")
    atom_defaults = _mapping(payload, "atom_defaults")
    comparator_defaults = _mapping(payload, "comparator_defaults")
    catalogs: list[RepairAtomCatalog] = []
    comparators: list[tuple[str, RepairComparison]] = []
    for item in payload.get("catalogs", []):
        route = str(item["route"])
        comparator = _build_comparator(item, comparator_defaults)
        atoms = tuple(_build_atom(route, atom, atom_defaults) for atom in item["atoms"])
        catalogs.append(
            RepairAtomCatalog(
                catalog_version=str(item["catalog_version"]),
                route=route,
                toolbox_id=str(item["toolbox_id"]),
                toolbox_version=str(item["toolbox_version"]),
                comparator_hash=comparator.comparator_hash,
                atoms=atoms,
            )
        )
        comparators.append((route, comparator))
    if not catalogs or len({item.route for item in catalogs}) != len(catalogs):
        raise DomainContractError(ErrorCode.INVALID_CONTRACT, "P9B Catalog 必须非空且 Route 唯一")
    registry = RepairRuleRegistry(
        version=str(payload["static_registry_version"]),
        entries=tuple(str(item) for item in payload["static_registry_entries"]),
    )
    return RepairPolicySnapshot(
        config_hash=content_sha256(payload),
        max_repair_rounds=int(payload["max_repair_rounds"]),
        max_no_improvement=int(payload["max_no_improvement"]),
        real_sample_pressure_factor=int(payload["real_sample_pressure_factor"]),
        catalogs=tuple(catalogs),
        comparators=tuple(comparators),
        static_registry=registry,
    )


def _mapping(payload: dict[str, Any], key: str) -> dict[str, Any]:
    """读取必需 JSON 对象，避免调用方对缺失配置采用代码默认值。"""

    value = payload.get(key)
    if not isinstance(value, dict):
        raise DomainContractError(ErrorCode.INVALID_CONTRACT, f"P9B 配置缺少对象: {key}")
    return value


def _build_comparator(
    catalog_payload: dict[str, Any],
    defaults: dict[str, Any],
) -> RepairComparison:
    """为每个叶建立独立版本的 comparator，指标定义来自统一配置。"""

    return RepairComparison(
        version=str(catalog_payload["comparator_version"]),
        metrics=tuple(
            ComparatorMetric(str(item["name"]), MetricDirection(str(item["direction"])))
            for item in defaults["metrics"]
        ),
        hard_rejection_codes=tuple(str(item) for item in defaults["hard_rejection_codes"]),
        precision=int(defaults["precision"]),
        epsilon=float(defaults["epsilon"]),
        tie_policy=str(defaults["tie_policy"]),
    )


def _build_atom(
    route: str,
    atom_payload: dict[str, Any],
    defaults: dict[str, Any],
) -> RepairAtom:
    """把一个手工登记项与公共安全约束合成为叶私有 RepairAtom。"""

    return RepairAtom(
        atom_id=str(atom_payload["atom_id"]),
        applicable_finding_codes=tuple(
            str(item) for item in defaults["applicable_finding_codes"]
        ),
        required_facts=tuple(str(item) for item in defaults["required_facts"]),
        excluded_conditions=tuple(str(item) for item in defaults["excluded_conditions"]),
        bounded_parameters=tuple(
            BoundedRepairParameter(
                name=str(item["name"]),
                minimum=float(item["minimum"]),
                maximum=float(item["maximum"]),
                default=float(item["default"]),
            )
            for item in defaults["bounded_parameters"]
        ),
        owner_scope=route,
        hard_guards=tuple(str(item) for item in defaults["hard_guards"]),
        apply_adapter=str(defaults["apply_adapter"]),
        priority=int(atom_payload["priority"]),
    )


def main() -> int:
    """加载仓库内统一 P9B 配置，展示静态目录数量。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    repository_root = APPLICATION_ROOT.parent.parent
    policy_path = repository_root / "resources" / "manifests" / "p9b_repair_policy.json"
    policy = load_repair_policy(policy_path)
    LOGGER.info("P9B 配置示例，意图=验证静态目录数量 count=%s", len(policy.catalogs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
