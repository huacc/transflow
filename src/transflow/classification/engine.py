"""实现单页分类树控制流，不承担文档批量调度或工具箱执行。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from transflow.classification.decision_adapter import BoundedDecisionRunner
from transflow.classification.evidence import build_evidence, compact_evidence
from transflow.classification.resolver import resolve_node
from transflow.classification.rules import decide_rule, uses_direct_table_evidence
from transflow.domain.classification import (
    ClassificationRoute,
    NodeJudgement,
    NodeResolution,
)
from transflow.pdf_kernel.facts import ExtractedPageFacts

LOGGER = logging.getLogger("transflow.classification.engine")


@dataclass(frozen=True, slots=True)
class ClassifiedPage:
    """绑定稳定页面身份、唯一 Route 和完整节点审计。"""

    page_no: int
    page_identity: str
    route: ClassificationRoute
    resolutions: tuple[NodeResolution, ...]


class ClassificationEngine:
    """保持规则、主判、一次复核、Resolver 和确定性 fallback 控制流。"""

    def __init__(self, decision_runner: BoundedDecisionRunner) -> None:
        """绑定有界模型判定执行器，不持有目录、线程池或样本状态。"""

        self._decision_runner = decision_runner

    @property
    def decision_runner(self) -> BoundedDecisionRunner:
        """公开只读执行器，供协调器汇总不含正文的调用审计。"""

        return self._decision_runner

    def _decide_node(
        self,
        node_key: str,
        evidence: dict[str, Any],
        parent_path: tuple[str, ...],
    ) -> NodeResolution:
        """对一个分类节点执行独立规则、至多一次主判和至多一次复核。"""

        rule = decide_rule(node_key, evidence)
        if uses_direct_table_evidence(rule):
            primary = NodeJudgement(
                node_key,
                "MODEL_SKIPPED",
                "INCONCLUSIVE",
                None,
                0.0,
                rule.evidence_refs,
                "直接表格证据达到冻结置信度，跳过模型主判",
            )
        else:
            primary_payload = {
                "confirmed_parent_path": list(parent_path),
                **compact_evidence(evidence),
            }
            primary = self._decision_runner.decide(node_key, "PRIMARY", primary_payload)

        review_calls = 0

        def review_factory() -> NodeJudgement:
            """只在 Resolver 请求时构造一次细粒度复核载荷。"""

            nonlocal review_calls
            review_calls += 1
            if review_calls > 1:
                raise RuntimeError("单个分类节点不得执行第二次复核")
            candidate_labels = sorted(
                {
                    item.selected_child
                    for item in (rule, primary)
                    if item.status == "DECIDED" and item.selected_child is not None
                }
            )
            review_payload = {
                "confirmed_parent_path": list(parent_path),
                **compact_evidence(evidence),
                "disagreement": {
                    "candidate_labels": candidate_labels,
                    "instruction": "不要投票；重新依据当前页面和 typed evidence 裁决当前节点",
                    "primary": primary.as_dict(),
                    "rule": rule.as_dict(),
                },
            }
            return self._decision_runner.decide(node_key, "REVIEW", review_payload)

        return resolve_node(node_key, rule, primary, review_factory)

    def classify_page(
        self,
        facts: ExtractedPageFacts,
        page_count: int,
    ) -> ClassifiedPage:
        """沿冻结分类树判断一页，并保证成功、freeform 或 unclassified 三类出口。"""

        LOGGER.info("调用页面分类，意图=产生唯一 Route page_no=%s", facts.page.page_no)
        evidence = build_evidence(facts, page_count)
        path: list[str] = []
        resolutions: list[NodeResolution] = []
        role = self._decide_node("page.role", evidence, ())
        resolutions.append(role)
        if role.final.status != "DECIDED" or role.final.selected_child is None:
            route = ClassificationRoute(
                route="unclassified",
                confidence=0.0,
                evidence_ids=role.final.evidence_refs,
                complete_to_leaf=False,
                failed_node="page.role",
            )
            return ClassifiedPage(
                facts.page.page_no,
                facts.page_identity,
                route,
                tuple(resolutions),
            )
        path.append(role.final.selected_child)

        if path == ["body"]:
            layout = self._decide_node("body.layout_owner", evidence, tuple(path))
            resolutions.append(layout)
            if layout.final.status != "DECIDED" or layout.final.selected_child is None:
                return self._freeform_page(facts, resolutions, "body.layout_owner")
            path.append(layout.final.selected_child)

        if path == ["body", "flow_text"]:
            topology = self._decide_node("body.flow.topology", evidence, tuple(path))
            resolutions.append(topology)
            if topology.final.status != "DECIDED" or topology.final.selected_child is None:
                return self._freeform_page(facts, resolutions, "body.flow.topology")
            path.append(topology.final.selected_child)

        if path == ["body", "composite"]:
            kind = self._decide_node("body.composite.kind", evidence, tuple(path))
            resolutions.append(kind)
            if kind.final.status != "DECIDED" or kind.final.selected_child is None:
                return self._freeform_page(facts, resolutions, "body.composite.kind")
            path.append(kind.final.selected_child)

        confidence = min(item.final.confidence for item in resolutions)
        evidence_ids = tuple(
            sorted({ref for resolution in resolutions for ref in resolution.final.evidence_refs})
        )
        route = ClassificationRoute(".".join(path), confidence, evidence_ids)
        return ClassifiedPage(facts.page.page_no, facts.page_identity, route, tuple(resolutions))

    @staticmethod
    def _freeform_page(
        facts: ExtractedPageFacts,
        resolutions: list[NodeResolution],
        failed_node: str,
    ) -> ClassifiedPage:
        """把已确认的 body 后续失败确定映射为 body.freeform。"""

        evidence_ids = tuple(
            sorted({ref for resolution in resolutions for ref in resolution.final.evidence_refs})
        )
        route = ClassificationRoute(
            route="body.freeform",
            confidence=0.0,
            evidence_ids=evidence_ids,
            complete_to_leaf=True,
            failed_node=failed_node,
            taxonomy_fallback=True,
        )
        return ClassifiedPage(facts.page.page_no, facts.page_identity, route, tuple(resolutions))


def main() -> int:
    """记录分类引擎只执行单页逻辑，由文档协调器提供并发预算。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    LOGGER.info("ClassificationEngine 示例，意图=说明生产分类不持有固定路由或目录调度")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
