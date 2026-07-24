"""Frozen semantic-only translation instruction for body.diagram."""


def diagram_translation_system_prompt() -> str:
    """Keep translation semantic; deterministic code owns all geometry."""

    return (
        "Translate every body.diagram unit into the requested target language. "
        "Return only aligned unit text. Preserve numbers, dates, percentages, "
        "acronyms, bullets, and other required literals. Do not add layout, "
        "coordinates, font, line-break, node, connector, or drawing instructions."
    )
