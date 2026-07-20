"""按既有分类控制流实现一次复核后的确定性 Resolver。"""

from __future__ import annotations

import logging
from collections.abc import Callable

from transflow.classification.config import HIGH_CONFIDENCE_RULE_THRESHOLD
from transflow.domain.classification import NodeJudgement, NodeResolution

LOGGER = logging.getLogger("transflow.classification.resolver")


def resolve_node(
    node_key: str,
    rule: NodeJudgement,
    primary: NodeJudgement,
    review_factory: Callable[[], NodeJudgement],
) -> NodeResolution:
    """按一致、强规则、一次复核、失败四个固定分支归约节点。"""

    LOGGER.info("调用分类归约，意图=确定节点唯一结果 node=%s", node_key)
    if (
        rule.status == "DECIDED"
        and primary.status == "DECIDED"
        and rule.selected_child == primary.selected_child
    ):
        final = NodeJudgement(
            node_key,
            "RESOLVER",
            "DECIDED",
            rule.selected_child,
            min(rule.confidence, primary.confidence),
            tuple(sorted(set(rule.evidence_refs + primary.evidence_refs))),
            "工程规则与模型主判一致",
        )
        return NodeResolution(node_key, rule, primary, None, "DIRECT_AGREEMENT", final)

    if (
        rule.status == "DECIDED"
        and rule.selected_child
        and rule.confidence >= HIGH_CONFIDENCE_RULE_THRESHOLD
    ):
        final = NodeJudgement(
            node_key,
            "RESOLVER",
            "DECIDED",
            rule.selected_child,
            rule.confidence,
            rule.evidence_refs,
            "工程规则置信度达到冻结阈值，直接采用规则裁决",
        )
        return NodeResolution(node_key, rule, primary, None, "HIGH_CONFIDENCE_RULE", final)

    review = review_factory()
    if review.status == "DECIDED":
        final = NodeJudgement(
            node_key,
            "RESOLVER",
            "DECIDED",
            review.selected_child,
            review.confidence,
            review.evidence_refs,
            "主判不一致后由一次细粒度复核裁决",
        )
        return NodeResolution(node_key, rule, primary, review, "REVIEW_DECIDED", final)

    final = NodeJudgement(
        node_key,
        "RESOLVER",
        "INCONCLUSIVE",
        None,
        0.0,
        review.evidence_refs,
        "一次复核后仍无法稳定判断",
    )
    return NodeResolution(node_key, rule, primary, review, "UNRESOLVED", final)


def main() -> int:
    """记录 Resolver 只允许一次复核且结果确定收敛。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    LOGGER.info("Resolver 示例，意图=说明复核次数上限为一次")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
