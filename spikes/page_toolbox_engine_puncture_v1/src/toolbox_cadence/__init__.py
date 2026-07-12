"""Executable lifecycle rules shared by leaf-owned toolboxes."""

CADENCE_VERSION = "toolbox-cadence/v1"

SUPPORTED_TOOLBOX_KEYS = frozenset(
    {
        "cover",
        "contents",
        "end",
        "body.flow_text.single",
        "body.flow_text.multi",
        "body.flow_text.visual_anchored",
        "body.table",
        "body.chart",
        "body.diagram",
        "body.anchored_blocks",
        "body.composite.flow_text_table",
        "body.composite.anchored_blocks_chart",
        "body.composite.chart_table",
        "body.composite.flow_text_chart",
        "body.composite.flow_text_diagram",
    }
)

