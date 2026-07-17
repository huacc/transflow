# P16 chart-table English-to-Chinese page translation

Translate only the native PDF text units supplied in the request.

- Return exactly one result for every `container_id`; never add, remove, merge, split, or rename IDs.
- Translate all English meaning in `source_text` into concise professional Simplified Chinese suitable for the original chart, table, title, source, or footer position.
- Preserve every `required_literals` string exactly once in the corresponding translation. Do not convert values or units.
- Do not infer or output coordinates, fonts, layout instructions, tool calls, explanations, or text seen only inside images.
- Do not alter chart data, table numbers, axes, ticks, rules, colors, swatches, shapes, or geometry.
- Never emit question-mark placeholders, box characters, mojibake, or untranslated English-only residue.

Return only the JSON object required by the response schema.
