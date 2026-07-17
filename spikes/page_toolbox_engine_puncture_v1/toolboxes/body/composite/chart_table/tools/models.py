from __future__ import annotations

from dataclasses import dataclass

from toolboxes.body.chart.tools.models import ChartTemplate, ChartTextContainer, ChartVisualRegion, Rect
from toolboxes.body.table.tools.models import TableTemplate


@dataclass(frozen=True)
class ChartTableRegion:
    region_id: str
    owner: str
    bbox: Rect
    object_ids: tuple[str, ...]


@dataclass(frozen=True)
class ChartTableTextContainer:
    container_id: str
    owner: str
    role: str
    association_id: str
    source_object_ids: tuple[str, ...]
    source_text: str
    source_bbox: Rect
    allowed_bbox: Rect
    anchor_object_ids: tuple[str, ...]
    anchor_relation: str
    reading_order: int
    required_literals: tuple[str, ...]
    font_name: str
    font_size: float
    color_srgb: int
    alignment: str
    rotation: int = 0

    @classmethod
    def from_chart(cls, container: ChartTextContainer, owner: str) -> "ChartTableTextContainer":
        return cls(owner=owner, **container.__dict__)

    def as_chart(self) -> ChartTextContainer:
        values = dict(self.__dict__)
        values.pop("owner")
        return ChartTextContainer(**values)


@dataclass(frozen=True)
class ChartTableTemplate:
    page_id: str
    toolbox_key: str
    width: float
    height: float
    chart_regions: tuple[ChartVisualRegion, ...]
    table_regions: tuple[ChartTableRegion, ...]
    table_template: TableTemplate
    containers: tuple[ChartTableTextContainer, ...]
    protected_object_ids: tuple[str, ...]
    locked_objects_sha256: str
    structure_sha256: str

    def as_chart_template(self) -> ChartTemplate:
        return ChartTemplate(
            page_id=self.page_id,
            toolbox_key=self.toolbox_key,
            width=self.width,
            height=self.height,
            visual_regions=self.chart_regions,
            containers=tuple(item.as_chart() for item in self.containers),
            protected_object_ids=self.protected_object_ids,
            locked_objects_sha256=self.locked_objects_sha256,
            structure_sha256=self.structure_sha256,
        )
