"""定义工程闭环、产品验收与晋级资格三个独立结果轴。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from transflow.domain.common import require_non_empty, require_unique
from transflow.domain.completeness import (
    CompletenessStatus,
    TranslationCompletenessDecision,
)
from transflow.domain.delivery import DiagnosticStatus, TranslatedDiagnosticCandidate
from transflow.domain.pages import PageOutcome
from transflow.domain.states import (
    ArtifactIntegrity,
    Fallback,
    Quality,
    TranslationCoverage,
)

LOGGER = logging.getLogger("transflow.domain.result_axes")
PACKAGE_ROOT = Path(__file__).resolve().parent.parent
RESULT_SCHEMA = "transflow.three-axis-result/v1"


class EngineeringClosure(StrEnum):
    """表示技术流程是否安全形成完整可验证交付。"""

    PASS = "PASS"
    FAIL = "FAIL"


class ProductAcceptance(StrEnum):
    """表示产品质量是否通过、失败或尚未按 P14 阈值评估。"""

    PASS = "PASS"
    FAIL = "FAIL"
    NOT_EVALUATED = "NOT_EVALUATED"


class PromotionEligibility(StrEnum):
    """表示叶/阶段是否具备晋级资格。"""

    ELIGIBLE = "ELIGIBLE"
    INELIGIBLE = "INELIGIBLE"
    BLOCKED = "BLOCKED"


class ResultScope(StrEnum):
    """表示三轴结论所属页面、文档或阶段。"""

    PAGE = "PAGE"
    DOCUMENT = "DOCUMENT"
    STAGE = "STAGE"


@dataclass(frozen=True, slots=True)
class ThreeAxisResult:
    """以三个互不蕴含的枚举报告同一作用域结论。"""

    scope_type: ResultScope
    scope_id: str
    engineering_closure: EngineeringClosure
    product_acceptance: ProductAcceptance
    promotion_eligibility: PromotionEligibility
    reasons: tuple[str, ...]
    schema_version: str = RESULT_SCHEMA

    def __post_init__(self) -> None:
        """校验作用域、Schema、原因唯一性和晋级真值约束。"""

        require_non_empty(self.scope_id, "scope_id")
        require_unique(self.reasons, "result.reasons")
        if self.schema_version != RESULT_SCHEMA:
            raise ValueError("三轴结果 Schema 无效")
        if (
            self.promotion_eligibility is PromotionEligibility.ELIGIBLE
            and self.product_acceptance is not ProductAcceptance.PASS
        ):
            raise ValueError("产品未 PASS 时不得标记 ELIGIBLE")

    def to_dict(self) -> dict[str, Any]:
        """序列化为三个字段互相独立的纯 JSON 对象。"""

        return {
            "engineering_closure": self.engineering_closure.value,
            "product_acceptance": self.product_acceptance.value,
            "promotion_eligibility": self.promotion_eligibility.value,
            "reasons": list(self.reasons),
            "schema_version": self.schema_version,
            "scope_id": self.scope_id,
            "scope_type": self.scope_type.value,
        }


def _promotion(
    engineering: EngineeringClosure,
    product: ProductAcceptance,
) -> PromotionEligibility:
    """按工程与产品轴计算晋级轴，不反向改写前两轴。"""

    if engineering is EngineeringClosure.FAIL:
        return PromotionEligibility.BLOCKED
    if product is ProductAcceptance.PASS:
        return PromotionEligibility.ELIGIBLE
    if product is ProductAcceptance.FAIL:
        return PromotionEligibility.INELIGIBLE
    return PromotionEligibility.BLOCKED


def project_page_result(
    scope_id: str,
    outcome: PageOutcome,
    *,
    final_available: bool,
    completeness: TranslationCompletenessDecision | None,
    diagnostic: TranslatedDiagnosticCandidate | None = None,
    p14_evaluated: bool = False,
    p14_threshold_passed: bool | None = None,
) -> ThreeAxisResult:
    """从真实 final、完整性和质量维度投影单页三轴结论。"""

    LOGGER.info("调用页面三轴投影，意图=区分技术闭环与产品成功 scope_id=%s", scope_id)
    engineering = (
        EngineeringClosure.PASS
        if final_available and outcome.integrity is ArtifactIntegrity.PASS
        else EngineeringClosure.FAIL
    )
    reasons: list[str] = []
    hard_product_failure = (
        completeness is None
        or completeness.status is CompletenessStatus.FAIL
        or outcome.translation_coverage is not TranslationCoverage.FULL
        or outcome.quality is Quality.FAIL
        or outcome.fallback is not Fallback.NONE
    )
    if engineering is EngineeringClosure.FAIL:
        reasons.append("ENGINEERING_FINAL_UNAVAILABLE")
    if completeness is None or completeness.status is CompletenessStatus.FAIL:
        reasons.append("TRANSLATION_COMPLETENESS_NOT_PASS")
    if outcome.translation_coverage is not TranslationCoverage.FULL:
        reasons.append("TRANSLATION_COVERAGE_NOT_FULL")
    if outcome.quality is Quality.FAIL:
        reasons.append("QUALITY_FAIL")
    if outcome.fallback is not Fallback.NONE:
        reasons.append("FALLBACK_PRESENT")
    if (
        diagnostic is not None
        and diagnostic.status is not DiagnosticStatus.TRANSLATED_DIAGNOSTIC_READY
    ):
        reasons.append(diagnostic.status.value)
    if hard_product_failure:
        product = ProductAcceptance.FAIL
    elif not p14_evaluated:
        product = ProductAcceptance.NOT_EVALUATED
        reasons.append("P14_THRESHOLDS_NOT_EVALUATED")
    elif p14_threshold_passed is True:
        product = ProductAcceptance.PASS
    else:
        product = ProductAcceptance.FAIL
        reasons.append("P14_THRESHOLDS_FAIL")
    return ThreeAxisResult(
        ResultScope.PAGE,
        scope_id,
        engineering,
        product,
        _promotion(engineering, product),
        tuple(dict.fromkeys(reasons)),
    )


def aggregate_results(
    scope_type: ResultScope,
    scope_id: str,
    children: tuple[ThreeAxisResult, ...],
    *,
    engineering_gate_passed: bool = True,
) -> ThreeAxisResult:
    """按保守真值表聚合页面到文档或阶段，不把 Gate PASS 推导为产品 PASS。"""

    if scope_type is ResultScope.PAGE or not children:
        raise ValueError("聚合只接受非空 DOCUMENT/STAGE 子结果")
    engineering = (
        EngineeringClosure.PASS
        if engineering_gate_passed
        and all(item.engineering_closure is EngineeringClosure.PASS for item in children)
        else EngineeringClosure.FAIL
    )
    if any(item.product_acceptance is ProductAcceptance.FAIL for item in children):
        product = ProductAcceptance.FAIL
    elif any(
        item.product_acceptance is ProductAcceptance.NOT_EVALUATED for item in children
    ):
        product = ProductAcceptance.NOT_EVALUATED
    else:
        product = ProductAcceptance.PASS
    reasons = tuple(
        dict.fromkeys(reason for item in children for reason in item.reasons)
    )
    return ThreeAxisResult(
        scope_type,
        scope_id,
        engineering,
        product,
        _promotion(engineering, product),
        reasons,
    )


def main() -> int:
    """记录三轴结论不能由单一 PASS 布尔值代替。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    LOGGER.info("ThreeAxisResult 示例，意图=独立报告工程、产品与晋级结论")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
