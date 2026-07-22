"""定义 single 叶从原生文字容器到固定锚点布局的私有模型。"""

from __future__ import annotations

from dataclasses import dataclass

Rect = tuple[float, float, float, float]

MINIMUM_LINE_HEIGHT = 1.25
MAXIMUM_LINE_HEIGHT = 1.35
DEFAULT_LINE_HEIGHT = 1.30


@dataclass(frozen=True, slots=True)
class SingleTextContainer:
    """保存一个阅读顺序稳定、可独立翻译的原生文字容器。"""

    container_id: str
    semantic_object_id: str
    source_object_ids: tuple[str, ...]
    source_rects: tuple[Rect, ...]
    source_text: str
    reading_order: int
    role: str
    source_bbox: Rect
    anchor: tuple[float, float]
    font_size: float
    color_srgb: int
    preferred_line_height: float
    preserved_prefix: str | None = None
    preserved_page_numbers: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SinglePlacement:
    """保存一个译文容器的固定锚点、输出区域和排版参数。"""

    container_id: str
    translated_text: str
    output_bbox: Rect
    font_size: float
    line_height: float
    color_srgb: int
    fit: bool
