from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class NodeJudgement:
    node_key: str
    source: str
    status: str
    selected_child: str | None
    confidence: float
    evidence_refs: tuple[str, ...]
    reason_summary: str

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["evidence_refs"] = list(self.evidence_refs)
        return value


@dataclass(frozen=True)
class ProviderResult:
    http_status: int | None
    latency_ms: int
    request_id: str | None
    reported_model: str | None
    finish_reason: str | None
    raw_content: str | None
    parsed_json: dict[str, Any] | None
    raw_response_sha256: str | None
    error_code: str | None


@dataclass(frozen=True)
class NodeResolution:
    node_key: str
    rule: NodeJudgement
    qwen_primary: NodeJudgement
    review: NodeJudgement | None
    resolution: str
    final: NodeJudgement

    def as_dict(self) -> dict[str, Any]:
        return {
            "node_key": self.node_key,
            "rule": self.rule.as_dict(),
            "qwen_primary": self.qwen_primary.as_dict(),
            "review": self.review.as_dict() if self.review else None,
            "resolution": self.resolution,
            "final": self.final.as_dict(),
        }
