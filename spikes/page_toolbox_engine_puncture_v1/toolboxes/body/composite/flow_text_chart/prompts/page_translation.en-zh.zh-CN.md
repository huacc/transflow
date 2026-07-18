# P17 flow-text/chart English-to-Chinese page translation

Translate only the native PDF text units supplied in the request.

- Return exactly one result for every `container_id`; never add, remove, merge, split, reorder, or rename IDs.
- Translate all English meaning into complete, professional Simplified Chinese. Preserve the paragraph's meaning and logical relations; do not summarize or omit clauses to make the text shorter.
- Keep chart titles, legends, labels, captions, sources, and shared headings concise enough for their original semantic role without dropping meaning.
- Use all supplied units as page-level context, keep terminology consistent across body text and chart labels, and resolve an obvious extracted glyph anomaly from surrounding semantics instead of assigning an unrelated meaning.
- Preserve every `required_literals` string exactly once in the corresponding translation. Do not convert values, dates, percentages, or units.
- Do not infer or output coordinates, fonts, line breaks, layout instructions, tool calls, explanations, or text seen only inside images.
- Do not alter chart data, axes, ticks, colors, swatches, shapes, geometry, or relationships between labels and series.
- Never emit question-mark placeholders, box characters, mojibake, or untranslated English-only residue.

Return only the JSON object required by the response schema.
