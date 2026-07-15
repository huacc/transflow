from types import SimpleNamespace

import fitz

from toolboxes.body.composite.flow_text_table.tools.models import TableRegionTransform
from toolboxes.body.composite.flow_text_table.tools.renderer import (
    _has_authorized_table_text_at_bbox,
    _owned_table_drawings,
)


def test_authorized_repaint_at_old_source_bbox_is_not_residue() -> None:
    placement = SimpleNamespace(
        translated_text="译文 –",
        output_bbox=(350.0, 700.0, 400.0, 730.0),
    )

    assert _has_authorized_table_text_at_bbox(
        "–",
        (373.0, 713.0, 377.0, 725.0),
        (placement,),
    )
    assert not _has_authorized_table_text_at_bbox(
        "Revenue",
        (373.0, 713.0, 377.0, 725.0),
        (placement,),
    )
    assert not _has_authorized_table_text_at_bbox(
        "–",
        (410.0, 713.0, 414.0, 725.0),
        (placement,),
    )


def test_only_moved_table_regions_have_their_graphics_repainted() -> None:
    fixed = TableRegionTransform(
        (10.0, 10.0, 90.0, 40.0),
        (10.0, 10.0, 90.0, 40.0),
        None,
        None,
        None,
    )
    moved = TableRegionTransform(
        (110.0, 10.0, 190.0, 40.0),
        (110.0, 50.0, 190.0, 80.0),
        None,
        None,
        None,
    )
    with fitz.open() as document:
        page = document.new_page(width=200.0, height=100.0)
        page.draw_rect(fitz.Rect(fixed.source_bbox), fill=(1.0, 0.8, 0.8))
        page.draw_rect(fitz.Rect(moved.source_bbox), fill=(0.8, 0.8, 1.0))

        rows = _owned_table_drawings(page, (fixed, moved))

    assert rows
    assert {transform for _, transform in rows} == {moved}
