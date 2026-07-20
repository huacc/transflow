"""定义 P6 机械内核使用的稳定、可序列化值对象。"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from transflow.domain.common import require_non_empty, require_sha256, require_unique
from transflow.domain.errors import DomainContractError, ErrorCode

LOGGER = logging.getLogger("transflow.pdf_kernel.models")


@dataclass(frozen=True, slots=True, order=True)
class KernelFinding:
    """记录机械硬约束的稳定代码、级别、说明和规范化证据。"""

    code: str
    severity: str
    message: str
    evidence: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        """拒绝空字段、重复证据键和非确定性证据顺序。"""

        require_non_empty(self.code, "finding.code")
        require_non_empty(self.severity, "finding.severity")
        require_non_empty(self.message, "finding.message")
        evidence_keys = tuple(item[0] for item in self.evidence)
        require_unique(evidence_keys, "finding.evidence")
        if self.evidence != tuple(sorted(self.evidence)):
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "Finding 证据必须按键排序")

    @property
    def blocking(self) -> bool:
        """返回该发现项是否阻断候选提交。"""

        return self.severity == "HARD"


@dataclass(frozen=True, slots=True)
class FontProbe:
    """记录受控字体登记、完整性、加载和字形覆盖结果。"""

    font_id: str
    registered: bool
    integrity_passed: bool
    loadable: bool
    glyph_count: int | None
    missing_codepoints: tuple[str, ...]

    @property
    def covers_text(self) -> bool:
        """判断字体是否来自 manifest 且覆盖全部非空白字符。"""

        return (
            self.registered
            and self.integrity_passed
            and self.loadable
            and not self.missing_codepoints
        )


@dataclass(frozen=True, slots=True)
class PatchManifest:
    """冻结一次声明式 Patch 的解释器、操作顺序和渲染配置指纹。"""

    schema_version: str
    interpreter_id: str
    patch_id: str
    source_hash: str
    page_no: int
    geometry_hash: str
    owner: str
    operation_ids: tuple[str, ...]
    operation_hashes: tuple[str, ...]
    render_config_hash: str
    manifest_hash: str

    def __post_init__(self) -> None:
        """校验 Patch manifest 的稳定身份、顺序和全部哈希。"""

        for field_name in ("schema_version", "interpreter_id", "patch_id", "owner"):
            require_non_empty(getattr(self, field_name), field_name)
        for field_name in (
            "source_hash",
            "geometry_hash",
            "render_config_hash",
            "manifest_hash",
        ):
            require_sha256(getattr(self, field_name), field_name)
        require_unique(self.operation_ids, "patch_manifest.operation_ids")
        if self.page_no < 1 or len(self.operation_ids) != len(self.operation_hashes):
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "Patch manifest 页面或操作无效")
        for value in self.operation_hashes:
            require_sha256(value, "patch_manifest.operation_hash")


@dataclass(frozen=True, slots=True)
class RepairDecision:
    """记录一次机械 Repair 的接受、回滚或预算停止决定。"""

    outcome: str
    accepted: bool
    selected_candidate_ref: str
    round_index: int
    operations_used: int
    no_improvement_count: int
    finding_codes: tuple[str, ...]
    reason: str


def make_finding(
    code: str,
    message: str,
    *,
    severity: str = "HARD",
    **evidence: object,
) -> KernelFinding:
    """把任意简单证据规范化为排序稳定的字符串键值对。"""

    normalized = tuple(sorted((str(key), str(value)) for key, value in evidence.items()))
    return KernelFinding(code, severity, message, normalized)


def main() -> int:
    """展示结构化 Finding 的稳定构造方式。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    finding = make_finding("PAGE_BOUNDS_EXCEEDED", "示例越界", page_no=1)
    LOGGER.info("调用 Finding 示例，意图=展示稳定机械证据 code=%s", finding.code)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
