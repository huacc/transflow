"""定义 P9B 页级有效布局、修复记忆、原子目录和比较合同。"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

from transflow.domain.common import content_sha256, json_ready, require_sha256
from transflow.domain.errors import DomainContractError, ErrorCode

LOGGER = logging.getLogger("transflow.domain.repair_memory")
DOMAIN_ROOT = Path(__file__).resolve().parent.parent
PAGE_MEMORY_SCHEMA = "transflow.page-repair-memory/v1"
ABSOLUTE_PATH = re.compile(r"(?i)(?:^|[^A-Za-z0-9_])[A-Z]:[\\/]")
FORBIDDEN_TEXT = re.compile(r"(?i)(?:api[_-]?key|bearer\s+|provider_response|raw_text)")


class RepairAttemptStatus(StrEnum):
    """列出一个已经实际执行动作允许形成的唯一终态。"""

    ACCEPTED = "ACCEPTED"
    ROLLED_BACK = "ROLLED_BACK"
    REJECTED = "REJECTED"
    MATERIALIZATION_FAILED = "MATERIALIZATION_FAILED"


class RepairStopReason(StrEnum):
    """列出页级修复闭环的确定性停止原因。"""

    PASSED = "PASSED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    NO_IMPROVEMENT = "NO_IMPROVEMENT"
    STATE_CYCLE = "STATE_CYCLE"
    NO_APPLICABLE_ACTION = "NO_APPLICABLE_ACTION"
    HARD_CONSTRAINT_FAILED = "HARD_CONSTRAINT_FAILED"


class MetricDirection(StrEnum):
    """表示叶 comparator 指标的唯一改善方向。"""

    MINIMIZE = "MINIMIZE"
    MAXIMIZE = "MAXIMIZE"


class RepairComparisonOutcome(StrEnum):
    """表示叶 comparator 的改善、退化、相等或硬拒绝结论。"""

    IMPROVED = "IMPROVED"
    REGRESSED = "REGRESSED"
    TIE = "TIE"
    HARD_REJECTED = "HARD_REJECTED"


@dataclass(frozen=True, slots=True)
class PageEffectiveLayout:
    """保存当前页在固定文档记忆下解析出的布局参数，不回写全局记忆。"""

    document_memory_hash: str
    page_no: int
    route: str
    source_facts_hash: str
    translation_bundle_hash: str
    font_scale: float
    line_spacing: float
    paragraph_spacing: float
    wrap_mode: str
    page_adjustments: tuple[tuple[str, float], ...] = ()

    def __post_init__(self) -> None:
        """校验页身份、绑定哈希、布局区间和调整键唯一性。"""

        for field_name in (
            "document_memory_hash",
            "source_facts_hash",
            "translation_bundle_hash",
        ):
            require_sha256(getattr(self, field_name), field_name)
        if self.page_no < 1 or not self.route or not self.wrap_mode:
            raise DomainContractError(ErrorCode.INVALID_IDENTITY, "页级有效布局身份无效")
        if min(self.font_scale, self.line_spacing, self.paragraph_spacing) <= 0:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "页级布局参数必须为正")
        keys = tuple(item[0] for item in self.page_adjustments)
        if (
            len(keys) != len(set(keys))
            or tuple(sorted(self.page_adjustments)) != self.page_adjustments
        ):
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "页级调整必须唯一且稳定排序")

    @property
    def layout_hash(self) -> str:
        """计算页级有效布局规范哈希。"""

        return content_sha256(self)


@dataclass(frozen=True, slots=True)
class RepairMemoryIdentity:
    """绑定同一 run 页记忆恢复所需的全部身份和实现指纹。"""

    run_id: str
    run_token: str
    source_hash: str
    page_no: int
    route: str
    toolbox_id: str
    toolbox_version: str
    config_hash: str
    document_memory_hash: str
    atom_catalog_hash: str
    comparator_hash: str
    translation_bundle_hash: str
    schema_hash: str
    implementation_hash: str
    static_registry_hash: str

    def __post_init__(self) -> None:
        """校验 run/page/route/toolbox 身份和全部恢复指纹。"""

        for value, field_name in (
            (self.run_id, "run_id"),
            (self.run_token, "run_token"),
            (self.route, "route"),
            (self.toolbox_id, "toolbox_id"),
            (self.toolbox_version, "toolbox_version"),
        ):
            if not value:
                raise DomainContractError(ErrorCode.INVALID_IDENTITY, f"{field_name} 不得为空")
        if self.page_no < 1:
            raise DomainContractError(ErrorCode.INVALID_IDENTITY, "page_no 必须从 1 开始")
        for field_name in (
            "source_hash",
            "config_hash",
            "document_memory_hash",
            "atom_catalog_hash",
            "comparator_hash",
            "translation_bundle_hash",
            "schema_hash",
            "implementation_hash",
            "static_registry_hash",
        ):
            require_sha256(getattr(self, field_name), field_name)

    @property
    def identity_hash(self) -> str:
        """计算同 run 页记忆兼容身份哈希。"""

        return content_sha256(self)

    def changed_fields(self, other: RepairMemoryIdentity) -> tuple[str, ...]:
        """列出所有阻止陈旧页记忆复用的变化字段。"""

        return tuple(
            name
            for name in self.__dataclass_fields__
            if getattr(self, name) != getattr(other, name)
        )


@dataclass(frozen=True, slots=True)
class QualityVector:
    """保存叶 comparator 的具名数值指标与硬失败集合，不形成跨叶万能总分。"""

    metrics: tuple[tuple[str, float], ...]
    hard_failure_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """校验指标和硬失败身份唯一且按名称稳定排序。"""

        names = tuple(item[0] for item in self.metrics)
        if (
            not names
            or len(names) != len(set(names))
            or tuple(sorted(self.metrics)) != self.metrics
        ):
            raise DomainContractError(
                ErrorCode.INVALID_CONTRACT,
                "质量向量指标必须非空、唯一、有序",
            )
        if len(self.hard_failure_codes) != len(set(self.hard_failure_codes)):
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "硬失败代码不得重复")

    def value(self, name: str) -> float:
        """读取冻结 comparator 声明的具名指标。"""

        try:
            return dict(self.metrics)[name]
        except KeyError as error:
            raise DomainContractError(
                ErrorCode.INVALID_CONTRACT,
                f"质量指标缺失: {name}",
            ) from error


@dataclass(frozen=True, slots=True)
class ComparatorMetric:
    """声明一个比较指标及其改善方向。"""

    name: str
    direction: MetricDirection


@dataclass(frozen=True, slots=True)
class RepairComparison:
    """记录版本化叶 comparator 的数值精度、epsilon、tie 和硬拒绝语义。"""

    version: str
    metrics: tuple[ComparatorMetric, ...]
    hard_rejection_codes: tuple[str, ...]
    precision: int
    epsilon: float
    tie_policy: str

    def __post_init__(self) -> None:
        """校验版本、指标唯一性、精度和 tie 策略。"""

        names = tuple(item.name for item in self.metrics)
        if not self.version or not names or len(names) != len(set(names)):
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "Comparator 版本或指标无效")
        if self.precision < 0 or self.epsilon < 0 or self.tie_policy not in {
            "KEEP_CURRENT",
            "ROLLBACK",
        }:
            raise DomainContractError(
                ErrorCode.INVALID_CONTRACT,
                "Comparator 精度/epsilon/tie 无效",
            )

    @property
    def comparator_hash(self) -> str:
        """计算 comparator 规范内容指纹。"""

        return content_sha256(self)

    def compare(self, before: QualityVector, after: QualityVector) -> RepairComparisonOutcome:
        """先执行硬拒绝，再按具名指标、方向和 epsilon 给出确定结论。"""

        if set(after.hard_failure_codes) & set(self.hard_rejection_codes):
            return RepairComparisonOutcome.HARD_REJECTED
        improved = False
        regressed = False
        for metric in self.metrics:
            previous = round(before.value(metric.name), self.precision)
            current = round(after.value(metric.name), self.precision)
            delta = current - previous
            if abs(delta) <= self.epsilon:
                continue
            metric_improved = (
                delta < 0 if metric.direction is MetricDirection.MINIMIZE else delta > 0
            )
            improved = improved or metric_improved
            regressed = regressed or not metric_improved
        if regressed:
            return RepairComparisonOutcome.REGRESSED
        if improved:
            return RepairComparisonOutcome.IMPROVED
        return RepairComparisonOutcome.TIE


@dataclass(frozen=True, slots=True)
class BoundedRepairParameter:
    """声明修复原子的单个有界数值参数。"""

    name: str
    minimum: float
    maximum: float
    default: float

    def __post_init__(self) -> None:
        """校验参数非空且默认值位于闭区间。"""

        if not self.name or not self.minimum <= self.default <= self.maximum:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "修复参数边界无效")


@dataclass(frozen=True, slots=True)
class RepairAtom:
    """表示叶显式登记、确定且有界的单个修复动作模板。"""

    atom_id: str
    applicable_finding_codes: tuple[str, ...]
    required_facts: tuple[str, ...]
    excluded_conditions: tuple[str, ...]
    bounded_parameters: tuple[BoundedRepairParameter, ...]
    owner_scope: str
    hard_guards: tuple[str, ...]
    apply_adapter: str
    priority: int

    def __post_init__(self) -> None:
        """校验稳定身份、owner、适用 Finding、Adapter 和参数唯一性。"""

        if (
            not self.atom_id
            or not self.applicable_finding_codes
            or not self.owner_scope
            or not self.apply_adapter
            or self.priority < 0
        ):
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "RepairAtom 字段不完整")
        parameter_names = tuple(item.name for item in self.bounded_parameters)
        if len(parameter_names) != len(set(parameter_names)):
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "RepairAtom 参数重复")

    def action_key(
        self,
        failure_code: str,
        owner: str,
        parameters: tuple[tuple[str, float], ...],
        input_state_hash: str,
    ) -> str:
        """从 failure/owner/atom/参数/输入状态生成稳定 action identity。"""

        require_sha256(input_state_hash, "input_state_hash")
        if owner != self.owner_scope:
            raise DomainContractError(ErrorCode.INVALID_IDENTITY, "RepairAtom owner scope 不匹配")
        allowed = {item.name: item for item in self.bounded_parameters}
        if tuple(sorted(parameters)) != parameters or set(dict(parameters)) != set(allowed):
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "Repair 参数集合或顺序无效")
        for name, value in parameters:
            boundary = allowed[name]
            if not boundary.minimum <= value <= boundary.maximum:
                raise DomainContractError(ErrorCode.INVALID_CONTRACT, "Repair 参数越界")
        return content_sha256(
            {
                "atom_id": self.atom_id,
                "failure_code": failure_code,
                "input_state_hash": input_state_hash,
                "owner": owner,
                "parameters": parameters,
            }
        )


@dataclass(frozen=True, slots=True)
class RepairAtomCatalog:
    """保存一个叶的手工登记 RepairAtom，按 priority/atom_id 稳定枚举。"""

    catalog_version: str
    route: str
    toolbox_id: str
    toolbox_version: str
    comparator_hash: str
    atoms: tuple[RepairAtom, ...]

    def __post_init__(self) -> None:
        """校验叶身份、Comparator 指纹、Atom 唯一性和 owner 边界。"""

        if not all((self.catalog_version, self.route, self.toolbox_id, self.toolbox_version)):
            raise DomainContractError(ErrorCode.INVALID_IDENTITY, "RepairAtomCatalog 身份不完整")
        require_sha256(self.comparator_hash, "comparator_hash")
        atom_ids = tuple(item.atom_id for item in self.atoms)
        if not atom_ids or len(atom_ids) != len(set(atom_ids)):
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "Catalog Atom 必须非空且唯一")
        if any(item.owner_scope != self.route for item in self.atoms):
            raise DomainContractError(ErrorCode.INVALID_IDENTITY, "Catalog 含跨叶 RepairAtom")

    @property
    def catalog_hash(self) -> str:
        """按稳定 Atom 顺序计算 Catalog 内容指纹。"""

        return content_sha256(
            {
                "catalog_version": self.catalog_version,
                "route": self.route,
                "toolbox_id": self.toolbox_id,
                "toolbox_version": self.toolbox_version,
                "comparator_hash": self.comparator_hash,
                "atoms": self.ordered_atoms,
            }
        )

    @property
    def ordered_atoms(self) -> tuple[RepairAtom, ...]:
        """返回不受输入枚举顺序影响的确定性 Atom 序列。"""

        return tuple(sorted(self.atoms, key=lambda item: (item.priority, item.atom_id)))

    def applicable_atoms(
        self,
        finding_codes: tuple[str, ...],
        available_facts: frozenset[str],
        active_conditions: frozenset[str],
        attempted_action_keys: frozenset[str],
        input_state_hash: str,
    ) -> tuple[tuple[RepairAtom, str], ...]:
        """按 Finding、required facts、hard guards 和当前 run 已尝试集合过滤动作。"""

        proposals: list[tuple[RepairAtom, str]] = []
        for atom in self.ordered_atoms:
            if not set(atom.applicable_finding_codes) & set(finding_codes):
                continue
            if not set(atom.required_facts) <= available_facts:
                continue
            if set(atom.excluded_conditions) & active_conditions:
                continue
            if set(atom.hard_guards) & active_conditions:
                continue
            parameters = tuple((item.name, item.default) for item in atom.bounded_parameters)
            failure_code = sorted(
                set(atom.applicable_finding_codes) & set(finding_codes)
            )[0]
            action_key = atom.action_key(
                failure_code,
                self.route,
                parameters,
                input_state_hash,
            )
            if action_key not in attempted_action_keys:
                proposals.append((atom, action_key))
        return tuple(proposals)


@dataclass(frozen=True, slots=True)
class RepairProposal:
    """表示已通过预检、即将实际执行且尚未计入预算的单个动作。"""

    action_key: str
    atom_id: str
    failure_code: str
    owner: str
    parameters: tuple[tuple[str, float], ...]
    input_state_hash: str

    def __post_init__(self) -> None:
        """校验 action/state 哈希和参数稳定顺序。"""

        require_sha256(self.action_key, "action_key")
        require_sha256(self.input_state_hash, "input_state_hash")
        if not self.atom_id or not self.failure_code or not self.owner:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "RepairProposal 身份不完整")
        if tuple(sorted(self.parameters)) != self.parameters:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "RepairProposal 参数未稳定排序")


def repair_state_hash(
    patch_hash: str,
    geometry_hash: str,
    content_hash: str,
    layout_hash: str,
) -> str:
    """从 Patch/geometry/content/effective-layout 生成候选状态哈希。"""

    for value, name in (
        (patch_hash, "patch_hash"),
        (geometry_hash, "geometry_hash"),
        (content_hash, "content_hash"),
        (layout_hash, "layout_hash"),
    ):
        require_sha256(value, name)
    return content_sha256(
        {
            "patch_hash": patch_hash,
            "geometry_hash": geometry_hash,
            "content_hash": content_hash,
            "layout_hash": layout_hash,
        }
    )


@dataclass(frozen=True, slots=True)
class RepairAttempt:
    """记录一个实际执行动作的唯一候选或物化失败终态。"""

    attempt_no: int
    proposal: RepairProposal
    status: RepairAttemptStatus
    layout_before_hash: str
    layout_after: PageEffectiveLayout | None
    quality_before: QualityVector
    quality_after: QualityVector | None
    output_state_hash: str | None
    candidate_artifact_ref: str | None
    patch_hash: str | None
    evidence_hash: str
    error_code: str | None = None

    def __post_init__(self) -> None:
        """校验轮次连续基础、引用哈希和成功/物化失败字段互斥。"""

        if self.attempt_no < 1:
            raise DomainContractError(ErrorCode.INVALID_IDENTITY, "attempt_no 必须从 1 开始")
        require_sha256(self.layout_before_hash, "layout_before_hash")
        require_sha256(self.evidence_hash, "evidence_hash")
        if self.output_state_hash is not None:
            require_sha256(self.output_state_hash, "output_state_hash")
        if self.patch_hash is not None:
            require_sha256(self.patch_hash, "patch_hash")
        if self.status is RepairAttemptStatus.MATERIALIZATION_FAILED:
            if any(
                value is not None
                for value in (
                    self.layout_after,
                    self.quality_after,
                    self.output_state_hash,
                    self.candidate_artifact_ref,
                    self.patch_hash,
                )
            ) or not self.error_code:
                raise DomainContractError(
                    ErrorCode.INVALID_CONTRACT,
                    "MATERIALIZATION_FAILED 不得伪造候选且必须含结构化错误",
                )
        elif any(
            value is None
            for value in (
                self.layout_after,
                self.quality_after,
                self.output_state_hash,
                self.candidate_artifact_ref,
                self.patch_hash,
            )
        ):
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "已物化 Attempt 证据不完整")
        if self.candidate_artifact_ref is not None:
            artifact_path = Path(self.candidate_artifact_ref)
            if artifact_path.is_absolute() or ".." in artifact_path.parts:
                raise DomainContractError(
                    ErrorCode.PATH_OUTSIDE_ALLOWED_ROOT,
                    "候选引用必须是受控相对路径",
                )


@dataclass(frozen=True, slots=True)
class PageRepairMemory:
    """表示单页、单 run、append-only 且可 Checkpoint 恢复的修复账本。"""

    identity: RepairMemoryIdentity
    initial_layout: PageEffectiveLayout
    current_layout: PageEffectiveLayout
    initial_state_hash: str
    attempts: tuple[RepairAttempt, ...]
    max_repair_rounds: int
    max_no_improvement: int
    consecutive_no_improvement: int = 0
    stop_reason: RepairStopReason | None = None
    finalized: bool = False
    schema_version: str = PAGE_MEMORY_SCHEMA

    def __post_init__(self) -> None:
        """校验身份绑定、预算、追加顺序、action/state 去重和终态闭合。"""

        if self.schema_version != PAGE_MEMORY_SCHEMA:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "PageRepairMemory Schema 无效")
        require_sha256(self.initial_state_hash, "initial_state_hash")
        if self.max_repair_rounds < 1 or self.max_no_improvement < 1:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "修复预算必须为正")
        if not 0 <= self.consecutive_no_improvement <= self.max_repair_rounds:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "连续无改善计数无效")
        if self.initial_layout.page_no != self.identity.page_no:
            raise DomainContractError(ErrorCode.INVALID_IDENTITY, "页记忆与有效布局串页")
        for layout in (self.initial_layout, self.current_layout):
            if (
                layout.document_memory_hash != self.identity.document_memory_hash
                or layout.translation_bundle_hash != self.identity.translation_bundle_hash
                or layout.route != self.identity.route
                or layout.page_no != self.identity.page_no
            ):
                raise DomainContractError(ErrorCode.INVALID_IDENTITY, "页布局身份指纹漂移")
        numbers = tuple(item.attempt_no for item in self.attempts)
        if numbers != tuple(range(1, len(self.attempts) + 1)):
            raise DomainContractError(ErrorCode.INVALID_IDENTITY, "Attempt 必须连续追加")
        action_keys = tuple(item.proposal.action_key for item in self.attempts)
        if len(action_keys) != len(set(action_keys)):
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "当前 run 重复 action")
        if len(self.attempts) > self.max_repair_rounds:
            raise DomainContractError(ErrorCode.REPAIR_BUDGET_EXHAUSTED, "Attempt 超出预算")
        if self.finalized != (self.stop_reason is not None):
            raise DomainContractError(
                ErrorCode.INVALID_CONTRACT,
                "finalized 与停止原因必须同时闭合",
            )
        expected_layout = self.initial_layout
        expected_state = self.initial_state_hash
        for attempt in self.attempts:
            if (
                attempt.layout_before_hash != expected_layout.layout_hash
                or attempt.proposal.input_state_hash != expected_state
            ):
                raise DomainContractError(
                    ErrorCode.INVALID_STATE_TRANSITION,
                    "Attempt 未从当前批准状态开始",
                )
            if attempt.status is RepairAttemptStatus.ACCEPTED:
                if attempt.layout_after is None or attempt.output_state_hash is None:
                    raise DomainContractError(
                        ErrorCode.INVALID_CONTRACT,
                        "ACCEPTED Attempt 缺少批准状态",
                    )
                expected_layout = attempt.layout_after
                expected_state = attempt.output_state_hash
        if self.current_layout != expected_layout:
            raise DomainContractError(
                ErrorCode.INVALID_STATE_TRANSITION,
                "当前有效布局未指向最后一个批准 Attempt",
            )

    @property
    def attempted_action_keys(self) -> frozenset[str]:
        """返回本 run 已提交动作集合。"""

        return frozenset(item.proposal.action_key for item in self.attempts)

    @property
    def seen_state_hashes(self) -> frozenset[str]:
        """返回初始和已提交候选状态集合。"""

        return frozenset(
            (
                self.initial_state_hash,
                *(
                    item.output_state_hash
                    for item in self.attempts
                    if item.output_state_hash is not None
                ),
            )
        )

    @property
    def memory_hash(self) -> str:
        """计算不含派生字段的规范页记忆内容哈希。"""

        return content_sha256(self)

    def append(
        self,
        attempt: RepairAttempt,
        *,
        current_layout: PageEffectiveLayout,
        no_improvement: bool,
    ) -> PageRepairMemory:
        """追加唯一 Attempt；已终止、重复 action/state 或预算耗尽时 fail closed。"""

        LOGGER.info(
            "调用页修复记忆追加，意图=提交实际动作终态 page_no=%s attempt=%s",
            self.identity.page_no,
            attempt.attempt_no,
        )
        if self.finalized:
            raise DomainContractError(ErrorCode.INVALID_STATE_TRANSITION, "FINALIZED 后不得追加")
        if attempt.attempt_no != len(self.attempts) + 1:
            raise DomainContractError(ErrorCode.INVALID_IDENTITY, "Attempt 编号不连续")
        return replace(
            self,
            attempts=(*self.attempts, attempt),
            current_layout=current_layout,
            consecutive_no_improvement=(
                self.consecutive_no_improvement + 1 if no_improvement else 0
            ),
        )

    def finalize(self, reason: RepairStopReason) -> PageRepairMemory:
        """以唯一停止原因关闭页记忆，之后禁止追加或篡改。"""

        if self.finalized:
            raise DomainContractError(ErrorCode.INVALID_STATE_TRANSITION, "页记忆已终止")
        return replace(self, stop_reason=reason, finalized=True)

    def to_dict(self) -> dict[str, Any]:
        """输出携带派生 memory hash 的严格 JSON 字典。"""

        payload = json_ready(self)
        _validate_safe(payload)
        return {**payload, "memory_hash": self.memory_hash}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> PageRepairMemory:
        """严格加载页记忆并重新执行全部身份、顺序、安全和 hash 校验。"""

        expected = {
            "identity", "initial_layout", "current_layout", "initial_state_hash", "attempts",
            "max_repair_rounds", "max_no_improvement", "consecutive_no_improvement",
            "stop_reason", "finalized", "schema_version", "memory_hash",
        }
        if set(payload) != expected:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "PageRepairMemory 字段漂移")
        _validate_safe(payload)
        try:
            memory = cls(
                identity=RepairMemoryIdentity(**payload["identity"]),
                initial_layout=_layout_from_dict(payload["initial_layout"]),
                current_layout=_layout_from_dict(payload["current_layout"]),
                initial_state_hash=payload["initial_state_hash"],
                attempts=tuple(_attempt_from_dict(item) for item in payload["attempts"]),
                max_repair_rounds=payload["max_repair_rounds"],
                max_no_improvement=payload["max_no_improvement"],
                consecutive_no_improvement=payload["consecutive_no_improvement"],
                stop_reason=(
                    RepairStopReason(payload["stop_reason"])
                    if payload["stop_reason"] is not None
                    else None
                ),
                finalized=payload["finalized"],
                schema_version=payload["schema_version"],
            )
        except (KeyError, TypeError, ValueError) as error:
            if isinstance(error, DomainContractError):
                raise
            raise DomainContractError(
                ErrorCode.INVALID_CONTRACT,
                "PageRepairMemory 结构无效",
            ) from error
        if memory.memory_hash != payload["memory_hash"]:
            raise DomainContractError(
                ErrorCode.ARTIFACT_INTEGRITY_FAILED,
                "PageRepairMemory hash 错误",
            )
        return memory


@dataclass(frozen=True, slots=True)
class PriorRepairEvidenceRef:
    """只定位旧 run 终态审计 Artifact，不携带或导入旧 action/state/译文。"""

    source_run_id: str
    source_memory_hash: str
    terminal_artifact_ref: str
    terminal_artifact_hash: str
    identity_fingerprint: str

    def __post_init__(self) -> None:
        """校验旧 run、审计路径及全部哈希，不允许绝对路径。"""

        if not self.source_run_id or not self.terminal_artifact_ref:
            raise DomainContractError(ErrorCode.INVALID_IDENTITY, "PriorRef 身份不完整")
        if Path(self.terminal_artifact_ref).is_absolute() or ".." in Path(
            self.terminal_artifact_ref
        ).parts:
            raise DomainContractError(
                ErrorCode.PATH_OUTSIDE_ALLOWED_ROOT,
                "PriorRef 必须是受控相对路径",
            )
        for value, name in (
            (self.source_memory_hash, "source_memory_hash"),
            (self.terminal_artifact_hash, "terminal_artifact_hash"),
            (self.identity_fingerprint, "identity_fingerprint"),
        ):
            require_sha256(value, name)


@dataclass(frozen=True, slots=True)
class RepairRuleRegistry:
    """表示 V1 dormant 静态只读快照；运行时无选择、写入或晋级接口。"""

    version: str
    entries: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """校验静态版本和条目唯一，不解释条目为可执行规则。"""

        if not self.version or len(self.entries) != len(set(self.entries)):
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "静态 Registry 无效")
        _validate_safe(json_ready(self))

    @property
    def registry_hash(self) -> str:
        """计算静态审计快照 hash，不参与动作选择。"""

        return content_sha256(self)

    def with_runtime_entry(self, _entry: str) -> RepairRuleRegistry:
        """明确拒绝运行时新增、修改或晋级 Registry 规则。"""

        raise DomainContractError(ErrorCode.INVALID_STATE_TRANSITION, "V1 Registry 运行时只读")


def _layout_from_dict(payload: dict[str, Any]) -> PageEffectiveLayout:
    """从严格 JSON 字典恢复 PageEffectiveLayout。"""

    return PageEffectiveLayout(
        **{key: value for key, value in payload.items() if key != "page_adjustments"},
        page_adjustments=tuple(tuple(item) for item in payload["page_adjustments"]),
    )


def _quality_from_dict(payload: dict[str, Any]) -> QualityVector:
    """从严格 JSON 字典恢复 QualityVector。"""

    return QualityVector(
        metrics=tuple(tuple(item) for item in payload["metrics"]),
        hard_failure_codes=tuple(payload["hard_failure_codes"]),
    )


def _proposal_from_dict(payload: dict[str, Any]) -> RepairProposal:
    """从严格 JSON 字典恢复 RepairProposal。"""

    return RepairProposal(
        **{key: value for key, value in payload.items() if key != "parameters"},
        parameters=tuple(tuple(item) for item in payload["parameters"]),
    )


def _attempt_from_dict(payload: dict[str, Any]) -> RepairAttempt:
    """从严格 JSON 字典恢复 RepairAttempt 及嵌套合同。"""

    return RepairAttempt(
        attempt_no=payload["attempt_no"],
        proposal=_proposal_from_dict(payload["proposal"]),
        status=RepairAttemptStatus(payload["status"]),
        layout_before_hash=payload["layout_before_hash"],
        layout_after=(
            _layout_from_dict(payload["layout_after"])
            if payload["layout_after"] is not None
            else None
        ),
        quality_before=_quality_from_dict(payload["quality_before"]),
        quality_after=(
            _quality_from_dict(payload["quality_after"])
            if payload["quality_after"] is not None
            else None
        ),
        output_state_hash=payload["output_state_hash"],
        candidate_artifact_ref=payload["candidate_artifact_ref"],
        patch_hash=payload["patch_hash"],
        evidence_hash=payload["evidence_hash"],
        error_code=payload["error_code"],
    )


def _validate_safe(payload: Any) -> None:
    """递归拒绝秘密、无界内容、历史正文和宿主绝对路径。"""

    if isinstance(payload, dict):
        for key, value in payload.items():
            if key.casefold() in {
                "sample_id", "file_name", "company_name", "raw_text",
                "provider_response", "historical_candidate", "translated_text",
            }:
                raise DomainContractError(ErrorCode.INVALID_CONTRACT, f"禁止页记忆字段: {key}")
            _validate_safe(value)
        return
    if isinstance(payload, list | tuple):
        for item in payload:
            _validate_safe(item)
        return
    if isinstance(payload, str) and (
        len(payload) > 2048 or ABSOLUTE_PATH.search(payload) or FORBIDDEN_TEXT.search(payload)
    ):
        raise DomainContractError(ErrorCode.INVALID_CONTRACT, "页记忆含秘密、无界内容或绝对路径")


def canonical_page_memory_bytes(memory: PageRepairMemory) -> bytes:
    """输出包含派生 hash 的稳定 UTF-8 JSON 字节，供 Artifact 持久化。"""

    return json.dumps(
        memory.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def main() -> int:
    """记录页级记忆只接受协调器追加结构化 Attempt。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    LOGGER.info("PageRepairMemory 示例，意图=由 RepairCoordinator 追加确定性轮次")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
