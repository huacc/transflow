"""从真实 PDF 对象、几何和颜色提取数据绑定与低对比度证据。"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from dataclasses import dataclass
from pathlib import Path

from transflow.domain.common import require_non_empty, require_sha256
from transflow.domain.errors import DomainContractError, ErrorCode
from transflow.pdf_kernel.facts import ExtractedPageFacts, RectTuple

LOGGER = logging.getLogger("transflow.pdf_kernel.relations")
PACKAGE_ROOT = Path(__file__).resolve().parent.parent
NUMERIC_OR_UNIT = re.compile(
    r"(?:[$€£¥]?\d[\d,.]*(?:%|\s*(?:million|billion|kg|t|m|km|kwh))?|%|亿元|万元|吨)",
    re.IGNORECASE,
)


def _center(rect: RectTuple) -> tuple[float, float]:
    """返回 PDF 矩形中心点。"""

    return ((rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2)


def _distance(first: RectTuple, second: RectTuple) -> float:
    """计算两个对象中心点的欧氏距离。"""

    first_x, first_y = _center(first)
    second_x, second_y = _center(second)
    return math.hypot(first_x - second_x, first_y - second_y)


@dataclass(frozen=True, slots=True)
class DataBindingEvidence:
    """记录一个数字/单位/图例文字与最近视觉对象的机械绑定。"""

    text_object_id: str
    visual_object_id: str
    text_hash: str
    distance_points: float
    binding_hash: str

    def __post_init__(self) -> None:
        """校验对象身份、哈希和非负距离。"""

        require_non_empty(self.text_object_id, "text_object_id")
        require_non_empty(self.visual_object_id, "visual_object_id")
        require_sha256(self.text_hash, "text_hash")
        require_sha256(self.binding_hash, "binding_hash")
        if self.distance_points < 0:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "绑定距离不得为负")


@dataclass(frozen=True, slots=True)
class LowContrastFinding:
    """记录源文本对白底的实际对比度和保持源设计的降级策略。"""

    text_object_id: str
    color_srgb: int
    contrast_ratio: float
    action: str = "PRESERVE_SOURCE_AND_DEGRADE"

    def __post_init__(self) -> None:
        """校验颜色、比例及禁止静默改色的固定动作。"""

        require_non_empty(self.text_object_id, "text_object_id")
        if not 0 <= self.color_srgb <= 0xFFFFFF or self.contrast_ratio < 1:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "低对比度证据数值无效")
        if self.action != "PRESERVE_SOURCE_AND_DEGRADE":
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "低对比度默认不得静默改色")


def _relative_luminance(color_srgb: int) -> float:
    """按 sRGB 规范计算颜色相对亮度。"""

    channels = (
        (color_srgb >> 16) & 0xFF,
        (color_srgb >> 8) & 0xFF,
        color_srgb & 0xFF,
    )
    linear = []
    for channel in channels:
        value = channel / 255
        linear.append(value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4)
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def probe_data_bindings(facts: ExtractedPageFacts) -> tuple[DataBindingEvidence, ...]:
    """把数字/单位文字绑定到实际最近图片、绘图或表格对象。"""

    LOGGER.info("调用数据绑定探针，意图=用 PDF 对象和几何验证图例/数字/单位关系")
    visuals = (
        tuple((item.object_id, item.bbox) for item in facts.image_objects)
        + tuple((item.object_id, item.bbox) for item in facts.drawing_objects)
        + tuple((item.object_id, item.bbox) for item in facts.table_objects)
    )
    if not visuals:
        return ()
    bindings: list[DataBindingEvidence] = []
    for text in facts.text_spans:
        if not NUMERIC_OR_UNIT.search(text.text):
            continue
        visual_id, visual_bbox = min(
            visuals,
            key=lambda item: (_distance(text.bbox, item[1]), item[0]),
        )
        distance = round(_distance(text.bbox, visual_bbox), 4)
        text_hash = hashlib.sha256(text.text.encode("utf-8")).hexdigest()
        binding_hash = hashlib.sha256(
            f"{text.object_id}\0{visual_id}\0{distance:.4f}".encode("ascii")
        ).hexdigest()
        bindings.append(
            DataBindingEvidence(
                text.object_id,
                visual_id,
                text_hash,
                distance,
                binding_hash,
            )
        )
    return tuple(bindings)


def validate_data_binding(
    facts: ExtractedPageFacts,
    text_object_id: str,
    visual_object_id: str,
) -> DataBindingEvidence:
    """只接受探针实际重算出的最近对象绑定，错绑立即硬失败。"""

    binding = next(
        (item for item in probe_data_bindings(facts) if item.text_object_id == text_object_id),
        None,
    )
    if binding is None or binding.visual_object_id != visual_object_id:
        raise DomainContractError(
            ErrorCode.DATA_BINDING_INVALID,
            "声明关系与实际 PDF 对象/几何不一致",
        )
    return binding


def probe_low_contrast(
    facts: ExtractedPageFacts,
    *,
    threshold: float = 3.0,
) -> tuple[LowContrastFinding, ...]:
    """探测对白底低对比度文字并固定“保持源设计、诚实降级”。"""

    if threshold <= 1:
        raise ValueError("低对比度阈值必须大于 1")
    findings: list[LowContrastFinding] = []
    for text in facts.text_spans:
        luminance = _relative_luminance(text.color_srgb)
        contrast = round(1.05 / (luminance + 0.05), 4)
        if contrast < threshold:
            findings.append(
                LowContrastFinding(text.object_id, text.color_srgb, contrast)
            )
    return tuple(findings)


def main() -> int:
    """记录关系和低对比度必须来自实际 PDF 事实。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    LOGGER.info("PDF 关系探针示例，意图=拒绝静默错绑与改色")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
