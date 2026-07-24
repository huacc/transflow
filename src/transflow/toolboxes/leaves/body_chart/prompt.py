"""Stable migration prompt for native semantic chart text."""

from __future__ import annotations


def chart_translation_system_prompt() -> str:
    """Return the frozen body.chart semantic-only translation instruction."""

    return (
        "你只翻译请求中已由机械规则确认的 PDF 原生语义文字。"
        "严格按 unit_id 和输入顺序返回，不新增、删除、合并或拆分单元。"
        "只翻译 source_text 语义，不输出坐标、字体、布局、工具调用或解释。"
        "标题、图例、类别标签、轴标题、注释和局部数据列表应简洁专业。"
        "页眉页脚属于 shared.margin 全局语义，不得当作图表数据推断。"
        "数字、代码、网址等必保留字面量必须逐字符保留，不换算数值。"
        "不得臆测图片内部文字，不得改写柱、线、点、扇区、色块或数据。"
        "不得返回问号、方框、乱码、占位符或机器约束。"
        "只返回约定 JSON 对象。"
    )
