"""定义逐叶迁移 runner 使用的显式、静态 Route 驱动注册表。"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol

LOGGER = logging.getLogger("transflow.toolbox_leaf_migration.drivers")


@dataclass(frozen=True, slots=True)
class LeafMigrationRunContext:
    """向单叶驱动提供已经校验的运行身份和私有目录。"""

    stage: str
    route: str
    route_slug: str
    run_id: str
    repository_root: Path
    evidence_root: Path
    output_root: Path
    input_manifest: dict[str, Any]
    baseline_hash: str
    catalog_hash: str


class LeafMigrationDriver(Protocol):
    """约束 Route 私有迁移驱动返回统一、可由公共 verifier 复核的证据。"""

    def execute(self, context: LeafMigrationRunContext) -> dict[str, Any]:
        """执行受控 A/B、完整 PDF 主链并返回结构化运行证据。"""

        ...


LeafMigrationDriverFactory = Callable[[], LeafMigrationDriver]


def _visual_only_factory() -> LeafMigrationDriver:
    """延迟导入 TM1 驱动，避免公共上下文与具体叶形成导入环。"""

    from scripts.toolbox_leaf_migration_visual_only import VisualOnlyMigrationDriver

    return VisualOnlyMigrationDriver()


def _single_factory() -> LeafMigrationDriver:
    """延迟导入 TM2 驱动，保持公共 runner 与 single 私有实现解耦。"""

    from scripts.toolbox_leaf_migration_single import SingleMigrationDriver

    return SingleMigrationDriver()


# 每个 TM 阶段只在这里增加当前叶的显式 factory；公共 runner 不扫描目录，
# 也不允许运行时注册。当前只登记已经进入 TM1/TM2 的两个 Route。
DRIVER_FACTORIES: Mapping[str, LeafMigrationDriverFactory] = MappingProxyType(
    {
        "body.flow_text.single": _single_factory,
        "visual_only": _visual_only_factory,
    }
)


def resolve_route_driver(route: str) -> LeafMigrationDriver | None:
    """从静态映射解析一个 Route 驱动；未注册时诚实返回空。"""

    factory = DRIVER_FACTORIES.get(route)
    if factory is None:
        return None
    LOGGER.info("调用逐叶迁移驱动，意图=选择显式 Route 私有执行器 route=%s", route)
    return factory()


def main() -> int:
    """显示当前阶段已经显式登记的驱动数量。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    print(f"TOOLBOX_LEAF_MIGRATION_DRIVERS registered={len(DRIVER_FACTORIES)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
