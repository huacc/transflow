from __future__ import annotations

from collections.abc import Callable

from .models import NodeJudgement, NodeResolution


HIGH_CONFIDENCE_RULE_THRESHOLD = 0.9


def resolve_node(
    node_key: str,
    rule: NodeJudgement,
    qwen_primary: NodeJudgement,
    review_factory: Callable[[], NodeJudgement],
) -> NodeResolution:
    if (
        rule.status == "DECIDED"
        and qwen_primary.status == "DECIDED"
        and rule.selected_child == qwen_primary.selected_child
    ):
        final = NodeJudgement(
            node_key,
            "RESOLVER",
            "DECIDED",
            rule.selected_child,
            min(rule.confidence, qwen_primary.confidence),
            tuple(sorted(set(rule.evidence_refs + qwen_primary.evidence_refs))),
            "工程规则与千问初判一致",
        )
        return NodeResolution(node_key, rule, qwen_primary, None, "DIRECT_AGREEMENT", final)

    if rule.status == "DECIDED" and rule.selected_child and rule.confidence >= HIGH_CONFIDENCE_RULE_THRESHOLD:
        final = NodeJudgement(
            node_key,
            "RESOLVER",
            "DECIDED",
            rule.selected_child,
            rule.confidence,
            rule.evidence_refs,
            "工程规则置信度达到 0.90，直接采用规则裁决",
        )
        return NodeResolution(node_key, rule, qwen_primary, None, "HIGH_CONFIDENCE_RULE", final)

    review = review_factory()
    if review.status == "DECIDED":
        final = NodeJudgement(
            node_key,
            "RESOLVER",
            "DECIDED",
            review.selected_child,
            review.confidence,
            review.evidence_refs,
            "初判不一致后由一次细粒度复核裁决",
        )
        return NodeResolution(node_key, rule, qwen_primary, review, "REVIEW_DECIDED", final)

    final = NodeJudgement(node_key, "RESOLVER", "INCONCLUSIVE", None, 0.0, review.evidence_refs, "一次复核后仍无法稳定判断")
    return NodeResolution(node_key, rule, qwen_primary, review, "UNRESOLVED", final)
