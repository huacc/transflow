"""集中读取 P9 普通叶的结构、布局、盲测与回退参数。"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

LOGGER = logging.getLogger("transflow.toolboxes.leaves.ordinary_policy")
PACKAGE_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True, slots=True)
class P9OrdinaryLeafPolicy:
    """保存六个普通叶共同使用且不得散落在代码中的冻结参数。"""

    source_language: str
    target_language: str
    font_id: str
    minimum_font_size: float
    maximum_font_size: float
    font_scale: float
    repair_limit: int
    minimum_real_anonymous_documents: int
    line_alignment_tolerance_ratio: float
    multi_minimum_gutter_ratio: float
    multi_spanning_width_ratio: float
    table_axis_tolerance_ratio: float
    anchor_maximum_distance_ratio: float
    anchor_tie_tolerance_ratio: float

    def __post_init__(self) -> None:
        """拒绝空语言、无效字号、开放修复循环和越界结构阈值。"""

        if not self.source_language or not self.target_language or not self.font_id:
            raise ValueError("P9 普通叶语言和字体配置不得为空")
        if not 0 < self.minimum_font_size <= self.maximum_font_size:
            raise ValueError("P9 普通叶字号范围无效")
        if not 0 < self.font_scale <= 1:
            raise ValueError("P9 普通叶 font_scale 必须位于 (0, 1]")
        if self.repair_limit != 1:
            raise ValueError("P9 普通叶只允许一次有界 Repair")
        if self.minimum_real_anonymous_documents < 1:
            raise ValueError("P9 真实匿名文档阈值必须为正整数")
        ratios = (
            self.line_alignment_tolerance_ratio,
            self.multi_minimum_gutter_ratio,
            self.multi_spanning_width_ratio,
            self.table_axis_tolerance_ratio,
            self.anchor_maximum_distance_ratio,
            self.anchor_tie_tolerance_ratio,
        )
        if any(value <= 0 or value >= 1 for value in ratios):
            raise ValueError("P9 普通叶结构比例必须位于 (0, 1)")


def load_p9_ordinary_leaf_policy(path: Path) -> P9OrdinaryLeafPolicy:
    """从统一配置文件读取 P9 普通叶参数并执行完整校验。"""

    LOGGER.info("调用 P9 普通叶策略读取，意图=集中约束六叶结构参数 path=%s", path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != "transflow.p9-ordinary-leaf-policy/v1":
        raise ValueError("P9 普通叶 policy schema_version 不受支持")
    return P9OrdinaryLeafPolicy(
        source_language=str(payload["source_language"]),
        target_language=str(payload["target_language"]),
        font_id=str(payload["font_id"]),
        minimum_font_size=float(payload["minimum_font_size"]),
        maximum_font_size=float(payload["maximum_font_size"]),
        font_scale=float(payload["font_scale"]),
        repair_limit=int(payload["repair_limit"]),
        minimum_real_anonymous_documents=int(payload["minimum_real_anonymous_documents"]),
        line_alignment_tolerance_ratio=float(payload["line_alignment_tolerance_ratio"]),
        multi_minimum_gutter_ratio=float(payload["multi_minimum_gutter_ratio"]),
        multi_spanning_width_ratio=float(payload["multi_spanning_width_ratio"]),
        table_axis_tolerance_ratio=float(payload["table_axis_tolerance_ratio"]),
        anchor_maximum_distance_ratio=float(payload["anchor_maximum_distance_ratio"]),
        anchor_tie_tolerance_ratio=float(payload["anchor_tie_tolerance_ratio"]),
    )


def main() -> int:
    """记录 P9 普通叶参数只能由统一配置入口注入。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    LOGGER.info("P9OrdinaryLeafPolicy 示例，意图=冻结普通叶结构阈值")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
