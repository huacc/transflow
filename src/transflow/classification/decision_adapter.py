"""通过 ModelDecisionPort 实现分类主判与一次复核的有界执行器。"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from transflow.classification.config import NODE_CHOICES, PROMPT_PATHS
from transflow.domain.classification import ModelDecisionRequest, NodeJudgement
from transflow.domain.errors import DomainContractError, PortCallError
from transflow.ports.model_decision import ModelDecisionPort

LOGGER = logging.getLogger("transflow.classification.decision_adapter")
PACKAGE_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = PACKAGE_ROOT.parent.parent
PROMPT_ROOT = REPO_ROOT / "resources" / "prompts" / "classification"
FORBIDDEN_IDENTITY_KEYS = {
    "expected",
    "expected_route",
    "file_name",
    "filename",
    "gold",
    "label",
    "manual_label",
    "path",
    "sample_id",
    "source_path",
}
HOST_PATH_PATTERN = re.compile(
    r"(?i)(?:(?<![a-z0-9])[a-z]:[\\/]|\.pdf(?:$|[?#\s]))"
)


def _canonical_sha256(value: object) -> str:
    """计算模型业务载荷的确定性 JSON 哈希。"""

    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def find_identity_leaks(value: object, location: str = "$") -> tuple[str, ...]:
    """递归扫描禁止字段和宿主路径，仅返回位置而不回显敏感值。"""

    leaks: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key).lower()
            child_location = f"{location}.{key}"
            if key_text in FORBIDDEN_IDENTITY_KEYS:
                leaks.append(child_location)
            leaks.extend(find_identity_leaks(child, child_location))
    elif isinstance(value, list | tuple):
        for index, child in enumerate(value):
            leaks.extend(find_identity_leaks(child, f"{location}[{index}]"))
    elif isinstance(value, str) and not value.startswith("data:image/"):
        if HOST_PATH_PATTERN.search(value):
            leaks.append(location)
    return tuple(sorted(set(leaks)))


@dataclass(frozen=True, slots=True)
class DecisionAudit:
    """记录一次模型判定的哈希、耗时、来源和失败代码。"""

    decision_id: str
    node_key: str
    stage: str
    prompt_sha256: str
    input_sha256: str
    output_sha256: str | None
    latency_ms: int
    status: str
    error_code: str | None


class BoundedDecisionRunner:
    """强制 allow-list、Schema、一次尝试和匿名载荷的薄包装。"""

    def __init__(self, port: ModelDecisionPort) -> None:
        """绑定公共模型判定 Port，并初始化线程安全审计集合。"""

        self._port = port
        self._lock = threading.Lock()
        self._audits: list[DecisionAudit] = []

    @property
    def audits(self) -> tuple[DecisionAudit, ...]:
        """返回按完成时间记录的不可变审计快照。"""

        with self._lock:
            return tuple(self._audits)

    def decide(
        self,
        node_key: str,
        stage: str,
        typed_evidence: dict[str, Any],
    ) -> NodeJudgement:
        """执行一次受限模型判定，任何失败都转换为确定的不确定裁决。"""

        if node_key not in NODE_CHOICES or stage not in {"PRIMARY", "REVIEW"}:
            raise ValueError("分类节点或判定阶段不受支持")
        leaks = find_identity_leaks(typed_evidence)
        if leaks:
            raise ValueError(f"模型载荷存在身份泄漏位置:{','.join(leaks)}")
        prompt_path = PROMPT_ROOT / PROMPT_PATHS[node_key][stage]
        prompt_text = prompt_path.read_text(encoding="utf-8")
        prompt_hash = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()
        node_spec = {
            "node_key": node_key,
            "prompt": prompt_text,
            "prompt_sha256": prompt_hash,
            "stage": stage,
        }
        input_hash = _canonical_sha256(
            {
                "allowed_actions": NODE_CHOICES[node_key],
                "node_spec": node_spec,
                "typed_evidence": typed_evidence,
            }
        )
        decision_id = hashlib.sha256(f"{node_key}\0{stage}\0{input_hash}".encode()).hexdigest()
        request = ModelDecisionRequest(
            decision_id=decision_id,
            decision_kind=f"classification.{node_key}.{stage.lower()}",
            schema_version="transflow.model-decision-request/v1",
            evidence_ids=tuple(str(item) for item in typed_evidence["evidence_ids"]),
            node_spec=node_spec,
            typed_evidence=typed_evidence,
            allowed_actions=NODE_CHOICES[node_key],
            attempt_budget=1,
            prompt_version=f"sha256:{prompt_hash}",
        )
        LOGGER.info(
            "调用分类模型判定，意图=执行有界节点判断 node=%s stage=%s",
            node_key,
            stage,
        )
        started = time.perf_counter()
        try:
            decision = self._port.decide(request)
            if decision.result_code not in {*NODE_CHOICES[node_key], "INCONCLUSIVE"}:
                raise ValueError("模型结果越出节点 allow-list")
            if not set(decision.evidence_ids).issubset(request.evidence_ids):
                raise ValueError("模型结果引用未知证据")
            status = "INCONCLUSIVE" if decision.result_code == "INCONCLUSIVE" else "DECIDED"
            child = None if status == "INCONCLUSIVE" else decision.result_code
            judgement = NodeJudgement(
                node_key=node_key,
                source=f"MODEL_{stage}",
                status=status,
                selected_child=child,
                confidence=decision.confidence if child else 0.0,
                evidence_refs=decision.evidence_ids,
                reason_summary=decision.reason_summary[:500],
            )
            output_hash = _canonical_sha256(judgement.as_dict())
            self._record_audit(
                decision_id,
                node_key,
                stage,
                prompt_hash,
                input_hash,
                output_hash,
                started,
                status,
                None,
            )
            return judgement
        except (DomainContractError, PortCallError, TimeoutError, ValueError) as error:
            error_code = getattr(getattr(error, "code", None), "value", type(error).__name__)
            self._record_audit(
                decision_id,
                node_key,
                stage,
                prompt_hash,
                input_hash,
                None,
                started,
                "INCONCLUSIVE",
                str(error_code),
            )
            return NodeJudgement(
                node_key,
                f"MODEL_{stage}",
                "INCONCLUSIVE",
                None,
                0.0,
                (),
                f"model_failure:{error_code}",
            )

    def _record_audit(
        self,
        decision_id: str,
        node_key: str,
        stage: str,
        prompt_sha256: str,
        input_sha256: str,
        output_sha256: str | None,
        started: float,
        status: str,
        error_code: str | None,
    ) -> None:
        """线程安全追加不包含原始页面内容的模型调用审计。"""

        audit = DecisionAudit(
            decision_id=decision_id,
            node_key=node_key,
            stage=stage,
            prompt_sha256=prompt_sha256,
            input_sha256=input_sha256,
            output_sha256=output_sha256,
            latency_ms=round((time.perf_counter() - started) * 1000),
            status=status,
            error_code=error_code,
        )
        with self._lock:
            self._audits.append(audit)


def main() -> int:
    """记录有界执行器只经 ModelDecisionPort 调用外部能力。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    LOGGER.info("BoundedDecisionRunner 示例，意图=说明生产代码不直连模型 Provider")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
