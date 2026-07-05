# Semantic Translation Inputs

This directory stores real translation input files consumed by `product_quality` runs.

Filename contract:

```text
<regression_id>.translations.json
```

Example:

```text
R2_AIA_pages_08_09_24_25.translations.json
```

These files must follow:

```text
pdf_translation_workflow_core\contracts\semantic_translation_contract.md
```

They must be generated from the current run's `source_extraction.json` and D2 translation prompt evidence. Do not place placeholder translations here.
