# Round22 Promotion Checklist

Round22 is an experiment snapshot. It can provide ideas for core, but it is not a stable implementation by itself.

## Must Keep

- Geometry-derived table cell splitting.
- Neighbor-aware table header and label binding.
- Obstacle-aware region expansion for target-language growth.
- Source-relative font hierarchy checks.
- Anti-overfit rules that reject page-number, exact text, and exact value branches.

## Must Not Promote

- Any branch keyed by a specific page number.
- Any branch keyed by exact sample text or exact numeric values.
- Any use of the manual reference PDF during runtime generation.
- Any result-only workaround that does not appear in contracts, prompts, tools, and process docs.

## Required Evidence

- Regression case manifest exists.
- Baseline result is marked honestly as product-quality fail when it fails.
- Spike validation can run from a clean package root.
- Final process document maps each gate to its repair action.

## Current Decision

Do not merge the whole round into `pdf_translation_workflow_core`.

Promote only the validated capabilities after they are rewritten as stable core tools and pass regression plus spike validation.
