"""实现版本化、不可变、无动态发现的显式 ToolboxCatalog。"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from transflow.domain.common import canonical_json_bytes, require_non_empty, require_sha256
from transflow.domain.errors import DomainContractError, ErrorCode
from transflow.domain.pages import PageOutcome
from transflow.domain.toolbox import Finding
from transflow.toolboxes.contracts import (
    TOOLBOX_CONTRACT_VERSION,
    PageToolbox,
    normalized_page_outcome,
)

LOGGER = logging.getLogger("transflow.toolboxes.catalog")
PACKAGE_ROOT = Path(__file__).resolve().parent.parent
ToolboxFactory = Callable[[], PageToolbox]


def catalog_entry_fingerprint(
    route: str,
    toolbox_key: str,
    toolbox_version: str,
    contract_version: str,
) -> str:
    """由 Catalog 生产身份计算可复算指纹，拒绝目录或运行环境参与选择。"""

    payload = {
        "contract_version": contract_version,
        "route": route,
        "toolbox_key": toolbox_key,
        "toolbox_version": toolbox_version,
    }
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


@dataclass(frozen=True, slots=True)
class ToolboxCatalogEntry:
    """表示一条 Route 到版本化 Toolbox 或确定 fallback 的静态映射。"""

    route: str
    toolbox_key: str
    toolbox_version: str
    fingerprint: str
    contract_version: str
    evidence_state: str
    evidence_attestation_hash: str | None
    enabled: bool
    fallback: str
    disabled_reason: str | None = None

    def __post_init__(self) -> None:
        """校验生产身份、指纹、证据状态和 fallback 完整。"""

        for field_name in (
            "route",
            "toolbox_key",
            "toolbox_version",
            "contract_version",
            "evidence_state",
            "fallback",
        ):
            require_non_empty(getattr(self, field_name), field_name)
        require_sha256(self.fingerprint, "fingerprint")
        expected = catalog_entry_fingerprint(
            self.route,
            self.toolbox_key,
            self.toolbox_version,
            self.contract_version,
        )
        if self.fingerprint != expected:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "Catalog entry 指纹不可复算")
        if self.enabled:
            if self.evidence_state != "PASS_ENABLE" or self.evidence_attestation_hash is None:
                raise DomainContractError(
                    ErrorCode.INVALID_CONTRACT,
                    "enabled 叶缺少 PASS_ENABLE 证明",
                )
            require_sha256(self.evidence_attestation_hash, "evidence_attestation_hash")
        elif not self.disabled_reason:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "disabled 叶缺少原因")


@dataclass(frozen=True, slots=True)
class CatalogStartupReport:
    """表示启动校验 readiness 和全部稳定违规代码。"""

    ready: bool
    violations: tuple[str, ...]
    catalog_hash: str


@dataclass(frozen=True, slots=True)
class CatalogResolution:
    """表示唯一启用 Toolbox 或唯一确定 fallback 页面结果。"""

    route: str
    toolbox: PageToolbox | None
    finding: Finding | None
    outcome: PageOutcome | None
    entry: ToolboxCatalogEntry | None = None

    def __post_init__(self) -> None:
        """确保 enabled 与 fallback 两种结果恰好出现一种。"""

        enabled = self.toolbox is not None and self.finding is None and self.outcome is None
        fallback = self.toolbox is None and self.finding is not None and self.outcome is not None
        if not enabled and not fallback:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "Catalog 解析出口不唯一")


class ToolboxCatalog:
    """保存加载时冻结的 Catalog 快照和显式 factory 集合。"""

    def __init__(
        self,
        entries: tuple[ToolboxCatalogEntry, ...],
        catalog_hash: str,
        factories: Mapping[str, ToolboxFactory],
        source_path: Path | None = None,
    ) -> None:
        """复制 factories 并冻结映射，禁止运行时注册或替换。"""

        require_sha256(catalog_hash, "catalog_hash")
        self._entries = tuple(entries)
        self._catalog_hash = catalog_hash
        self._factories: Mapping[str, ToolboxFactory] = MappingProxyType(dict(factories))
        self._source_path = source_path.resolve() if source_path is not None else None

    @property
    def entries(self) -> tuple[ToolboxCatalogEntry, ...]:
        """返回加载时冻结的全部 Catalog 项。"""

        return self._entries

    @property
    def catalog_hash(self) -> str:
        """返回进入 run/checkpoint 兼容指纹的 Catalog 内容哈希。"""

        return self._catalog_hash

    def validate_startup(self) -> CatalogStartupReport:
        """校验 Route 唯一、证据、合同、factory 和初始化，不认目录存在。"""

        LOGGER.info("调用 Catalog 启动校验，意图=阻止悬空或多绑定 Route")
        violations: list[str] = []
        routes = tuple(item.route for item in self._entries)
        for route in sorted(set(routes)):
            if routes.count(route) != 1:
                violations.append(f"ROUTE_BINDING_COUNT:{route}:{routes.count(route)}")
        # 结构不唯一时立即 not_ready，避免构造任何叶或发生后续 claim 副作用。
        if violations:
            ordered = tuple(sorted(violations))
            return CatalogStartupReport(False, ordered, self._catalog_hash)
        for entry in self._entries:
            if not entry.fallback:
                violations.append(f"FALLBACK_MISSING:{entry.route}")
            if not entry.enabled:
                continue
            if entry.contract_version != TOOLBOX_CONTRACT_VERSION:
                violations.append(f"CONTRACT_VERSION_MISMATCH:{entry.route}")
            factory = self._factories.get(entry.toolbox_key)
            if factory is None:
                violations.append(f"FACTORY_MISSING:{entry.route}")
                continue
            try:
                toolbox = factory()
            except Exception as error:
                violations.append(f"INITIALIZATION_FAILED:{entry.route}:{type(error).__name__}")
                continue
            if (
                toolbox.descriptor.route != entry.route
                or toolbox.descriptor.toolbox_id != entry.toolbox_key
                or toolbox.descriptor.contract_version != entry.contract_version
            ):
                violations.append(f"DESCRIPTOR_MISMATCH:{entry.route}")
        ordered = tuple(sorted(violations))
        return CatalogStartupReport(not ordered, ordered, self._catalog_hash)

    def resolve_enabled(
        self,
        route: str,
        page_no: int,
        *,
        expected_version: str | None = None,
        expected_fingerprint: str | None = None,
    ) -> CatalogResolution:
        """解析唯一 enabled Toolbox；所有不可用情况都返回确定 PageOutcome。"""

        LOGGER.info("调用 Catalog 解析，意图=选择显式 Toolbox 或 fallback route=%s", route)
        matched = tuple(item for item in self._entries if item.route == route)
        if len(matched) != 1:
            code = "TOOLBOX_UNREGISTERED" if not matched else "TOOLBOX_ROUTE_AMBIGUOUS"
            return self._fallback(route, page_no, code)
        entry = matched[0]
        if not entry.enabled:
            return self._fallback(route, page_no, "TOOLBOX_DISABLED", entry)
        if expected_version is not None and expected_version != entry.toolbox_version:
            return self._fallback(route, page_no, "TOOLBOX_VERSION_MISMATCH", entry)
        if expected_fingerprint is not None and expected_fingerprint != entry.fingerprint:
            return self._fallback(route, page_no, "TOOLBOX_FINGERPRINT_MISMATCH", entry)
        factory = self._factories.get(entry.toolbox_key)
        if factory is None:
            return self._fallback(route, page_no, "TOOLBOX_INITIALIZATION_FAILED", entry)
        try:
            toolbox = factory()
        except Exception:
            LOGGER.exception("Toolbox 初始化失败，意图=收敛到页级 fallback route=%s", route)
            return self._fallback(route, page_no, "TOOLBOX_INITIALIZATION_FAILED", entry)
        if toolbox.descriptor.route != route:
            return self._fallback(route, page_no, "TOOLBOX_INITIALIZATION_FAILED", entry)
        return CatalogResolution(route, toolbox, None, None, entry)

    def assert_source_unchanged(self) -> None:
        """检测 Catalog 文件运行期修改，同时保持当前对象的旧快照不变。"""

        if self._source_path is None:
            return
        current_hash = hashlib.sha256(self._source_path.read_bytes()).hexdigest()
        if current_hash != self._catalog_hash:
            raise DomainContractError(ErrorCode.CHECKPOINT_INCOMPATIBLE, "运行中 Catalog 文件变化")

    @staticmethod
    def _fallback(
        route: str,
        page_no: int,
        code: str,
        entry: ToolboxCatalogEntry | None = None,
    ) -> CatalogResolution:
        """构造四类及异常 Route 共用的确定性页级透传出口。"""

        finding = Finding(
            finding_id=f"catalog-p{page_no:04d}-{code.lower()}",
            code=code,
            severity="HARD",
            evidence_ids=(route,),
        )
        outcome = normalized_page_outcome(
            page_no,
            accepted=False,
            translated=False,
            finding_codes=(code,),
        )
        return CatalogResolution(route, None, finding, outcome, entry)


def load_toolbox_catalog(
    path: Path,
    factories: Mapping[str, ToolboxFactory] | None = None,
) -> ToolboxCatalog:
    """从显式路径加载 v2-v4 Catalog，禁止目录扫描或运行时注册。"""

    LOGGER.info("调用 Catalog 加载，意图=冻结生产 Route 映射 path=%s", path)
    content = path.read_bytes()
    payload = json.loads(content.decode("utf-8"))
    if payload.get("schema_version") not in {
        "transflow.page-toolbox-catalog/v2",
        "transflow.page-toolbox-catalog/v3",
        "transflow.page-toolbox-catalog/v4",
    }:
        raise ValueError("Toolbox Catalog schema_version 不受支持")
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise ValueError("Toolbox Catalog entries 必须是非空数组")
    entries = tuple(
        ToolboxCatalogEntry(
            route=str(item["route"]),
            toolbox_key=str(item["toolbox_key"]),
            toolbox_version=str(item["toolbox_version"]),
            fingerprint=str(item["fingerprint"]),
            contract_version=str(item["contract_version"]),
            evidence_state=str(item["evidence_state"]),
            evidence_attestation_hash=item.get("evidence_attestation_hash"),
            enabled=bool(item["enabled"]),
            fallback=str(item["fallback"]),
            disabled_reason=item.get("disabled_reason"),
        )
        for item in raw_entries
    )
    return ToolboxCatalog(
        entries,
        hashlib.sha256(content).hexdigest(),
        factories or {},
        path,
    )


def catalog_payload_hash(payload: dict[str, Any]) -> str:
    """计算内存 Catalog payload 的规范哈希，供测试和 checkpoint 构造。"""

    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def main() -> int:
    """记录 Catalog 只从显式静态资源加载且不可运行时修改。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    LOGGER.info("ToolboxCatalog 示例，意图=确保每条 Route 唯一解析或确定 fallback")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
