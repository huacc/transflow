"""使用跨页重复、几何和对象类型处理窄职责公共页眉页脚。"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from transflow.domain.common import canonical_json_bytes, require_non_empty, require_unique
from transflow.domain.errors import DomainContractError, ErrorCode
from transflow.domain.toolbox import Region

LOGGER = logging.getLogger("transflow.toolboxes.margin")
PACKAGE_ROOT = Path(__file__).resolve().parent.parent
PAGE_NUMBER_PATTERN = re.compile(
    r"^\s*(?:page\s*)?(?:\d+|[ivxlcdm]+)(?:\s*[/\-]\s*\d+)?\s*$",
    re.IGNORECASE,
)
DIGIT_PATTERN = re.compile(r"\d+")
HAND_BACK_HINTS = frozenset({"body", "table_note", "chart_label"})


@dataclass(frozen=True, slots=True)
class MarginPolicy:
    """保存集中配置的边缘比例、重复阈值和跨叶证据下限。"""

    top_ratio: float
    bottom_ratio: float
    minimum_page_fraction: float
    minimum_repeated_pages: int
    minimum_distinct_routes: int

    def __post_init__(self) -> None:
        """校验阈值位于可解释的几何和计数范围。"""

        if not 0 < self.top_ratio < self.bottom_ratio < 1:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "margin 上下边界比例无效")
        if not 0 < self.minimum_page_fraction <= 1:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "重复页比例无效")
        if self.minimum_repeated_pages < 2 or self.minimum_distinct_routes < 2:
            raise DomainContractError(
                ErrorCode.INVALID_CONTRACT,
                "公共下沉至少需要两页和两个叶证据",
            )


@dataclass(frozen=True, slots=True)
class MarginObservation:
    """表示整本文档中一个靠近边缘的机械对象观测。"""

    page_no: int
    object_id: str
    kind: str
    bbox: tuple[float, float, float, float]
    text: str
    page_width: float
    page_height: float
    route: str
    semantic_hint: str = "unknown"

    def __post_init__(self) -> None:
        """校验页面、对象、尺寸、几何、Route 和提示类型。"""

        require_non_empty(self.object_id, "margin.object_id")
        require_non_empty(self.kind, "margin.kind")
        require_non_empty(self.route, "margin.route")
        x0, y0, x1, y1 = self.bbox
        if (
            self.page_no < 1
            or self.page_width <= 0
            or self.page_height <= 0
            or x0 < 0
            or y0 < 0
            or x1 <= x0
            or y1 <= y0
            or x1 > self.page_width
            or y1 > self.page_height
        ):
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "margin 观测几何无效")


@dataclass(frozen=True, slots=True)
class MarginEvidence:
    """记录一个对象被拥有、保护或交回的结构证据及页内顺序。"""

    page_no: int
    object_id: str
    normalized_pattern: str
    position: str
    disposition: str
    ordinal: int


@dataclass(frozen=True, slots=True)
class MarginProcessingResult:
    """聚合共享 Region、保护对象、交回对象、唯一 owner 和证据哈希。"""

    shared_regions: tuple[Region, ...]
    protected_object_ids: tuple[str, ...]
    handback_object_ids: tuple[str, ...]
    owner_by_object: tuple[tuple[str, str], ...]
    evidence: tuple[MarginEvidence, ...]
    evidence_hash: str

    def __post_init__(self) -> None:
        """校验每个对象恰有一个处置且证据哈希可复算。"""

        require_unique(self.protected_object_ids, "protected_object_ids")
        require_unique(self.handback_object_ids, "handback_object_ids")
        owner_ids = tuple(item[0] for item in self.owner_by_object)
        require_unique(owner_ids, "owner_by_object.object_id")
        groups = (set(self.protected_object_ids), set(self.handback_object_ids), set(owner_ids))
        overlaps = any(
            groups[index] & groups[other] for index in range(3) for other in range(index + 1, 3)
        )
        if overlaps:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "margin 对象处置发生重叠")
        expected = hashlib.sha256(canonical_json_bytes(self.evidence)).hexdigest()
        if self.evidence_hash != expected:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "margin evidence_hash 不匹配")


class MarginRegionProcessor:
    """只用确定性机械证据识别公共边缘对象，不调用模型或推断页面类别。"""

    def __init__(self, policy: MarginPolicy) -> None:
        """绑定集中读取的不可变边缘策略。"""

        self._policy = policy

    def process(
        self,
        observations: tuple[MarginObservation, ...],
        document_page_count: int,
    ) -> MarginProcessingResult:
        """在整本范围计算重复模式并为每个输入对象给出唯一处置。"""

        if document_page_count < 1:
            raise ValueError("document_page_count 必须为正整数")
        LOGGER.info(
            "调用公共边缘处理，意图=识别共享页眉页脚 observations=%s pages=%s",
            len(observations),
            document_page_count,
        )
        groups: dict[tuple[str, str, str], list[MarginObservation]] = {}
        positions: dict[str, str] = {}
        for item in observations:
            position = self._edge_position(item)
            positions[item.object_id] = position
            normalized = self._normalize(item.text)
            groups.setdefault((item.kind, normalized, position), []).append(item)

        shared: list[Region] = []
        protected: list[str] = []
        handback: list[str] = []
        owners: list[tuple[str, str]] = []
        evidence: list[MarginEvidence] = []
        ordered_observations = sorted(
            observations,
            key=lambda item: (
                item.page_no,
                item.bbox[1],
                item.bbox[0],
                item.object_id,
            ),
        )
        page_ordinals: dict[int, int] = {}
        for item in ordered_observations:
            ordinal = page_ordinals.get(item.page_no, 0)
            page_ordinals[item.page_no] = ordinal + 1
            position = positions[item.object_id]
            normalized = self._normalize(item.text)
            group = groups[(item.kind, normalized, position)]
            repeated_pages = {candidate.page_no for candidate in group}
            distinct_routes = {candidate.route for candidate in group}
            repeated = (
                len(repeated_pages) >= self._policy.minimum_repeated_pages
                and len(repeated_pages) / document_page_count >= self._policy.minimum_page_fraction
                and len(distinct_routes) >= self._policy.minimum_distinct_routes
            )
            protected_kind = item.kind in {"logo", "image", "drawing", "decoration"}
            if position != "outside" and (
                protected_kind or PAGE_NUMBER_PATTERN.fullmatch(item.text)
            ):
                disposition = "PROTECTED"
                protected.append(item.object_id)
            elif position == "outside" or item.semantic_hint in HAND_BACK_HINTS:
                disposition = "HANDBACK"
                handback.append(item.object_id)
            elif item.kind == "text" and item.text.strip() and repeated:
                owner = f"shared.margin.{position}"
                disposition = "SHARED_OWNER"
                owners.append((item.object_id, owner))
                shared.append(Region(item.object_id, item.page_no, *item.bbox, owner))
            else:
                disposition = "HANDBACK"
                handback.append(item.object_id)
            evidence.append(
                MarginEvidence(
                    item.page_no,
                    item.object_id,
                    normalized,
                    position,
                    disposition,
                    ordinal,
                )
            )
        evidence_tuple = tuple(evidence)
        return MarginProcessingResult(
            tuple(shared),
            tuple(protected),
            tuple(handback),
            tuple(owners),
            evidence_tuple,
            hashlib.sha256(canonical_json_bytes(evidence_tuple)).hexdigest(),
        )

    def _edge_position(self, item: MarginObservation) -> str:
        """按页高归一化坐标判定 top、bottom 或交回正文范围。"""

        if item.bbox[3] / item.page_height <= self._policy.top_ratio:
            return "header"
        if item.bbox[1] / item.page_height >= self._policy.bottom_ratio:
            return "footer"
        return "outside"

    @staticmethod
    def _normalize(text: str) -> str:
        """规范空白并把数字替换成模式占位，适配页码变化而不看文件名。"""

        compact = " ".join(text.casefold().split())
        return DIGIT_PATTERN.sub("#", compact)


def validate_owner_assignments(*assignments: tuple[tuple[str, str], ...]) -> None:
    """拒绝 margin 与具体 Toolbox 对同一对象声明不同 owner。"""

    owners: dict[str, str] = {}
    for collection in assignments:
        for object_id, owner in collection:
            previous = owners.get(object_id)
            if previous is not None and previous != owner:
                raise DomainContractError(ErrorCode.INVALID_CONTRACT, "对象存在重复 owner")
            owners[object_id] = owner


def load_margin_policy(path: Path) -> MarginPolicy:
    """从集中配置指向的静态 JSON 读取冻结边缘阈值。"""

    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != "transflow.margin-policy/v1":
        raise ValueError("margin policy schema_version 不受支持")
    return MarginPolicy(
        top_ratio=float(payload["top_ratio"]),
        bottom_ratio=float(payload["bottom_ratio"]),
        minimum_page_fraction=float(payload["minimum_page_fraction"]),
        minimum_repeated_pages=int(payload["minimum_repeated_pages"]),
        minimum_distinct_routes=int(payload["minimum_distinct_routes"]),
    )


def main() -> int:
    """记录公共边缘处理只消费结构事实和冻结阈值。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    LOGGER.info("MarginRegionProcessor 示例，意图=保护页码与视觉对象并交回不确定语义")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
