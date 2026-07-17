# P16 chart-table Chinese-to-English page translation

Translate only the native PDF text units supplied in the request.

- Return exactly one result for every `container_id`; never add, remove, merge, split, or rename IDs.
- Translate all Chinese meaning in `source_text` into concise professional English suitable for the original chart, table, title, source, or footer position.
- Preserve every `required_literals` string exactly once in the corresponding translation. Do not convert values or units.
- Preserve Chinese numeric magnitudes without changing the protected number: translate `万` as `ten-thousand` and `亿` as `hundred-million`. For example, render `15.44万平方米` as `15.44 ten-thousand square meters` and `8,276亿元` as `8,276 hundred-million yuan`; never reduce either to plain square meters or yuan.
- Do not infer or output coordinates, fonts, layout instructions, tool calls, explanations, or text seen only inside images.
- Do not alter chart data, table numbers, axes, ticks, rules, colors, swatches, shapes, or geometry.
- Never emit question-mark placeholders, box characters, mojibake, or untranslated Chinese residue.

Return only the JSON object required by the response schema.
