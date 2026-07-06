# Model Tool Orchestration Contract

## Purpose

The prompt package is not a collection of standalone wording snippets. Its job is to let a new executor connect:

1. tool evidence;
2. model judgement prompts;
3. state transitions;
4. repair selection;
5. final PDF generation and verification.

The binding source of truth is:

```text
pdf_translation_workflow_core\prompts\prompt_tool_bindings.json
```

## Execution Pattern

Each model-backed state follows this sequence:

```text
tool outputs -> slot normalization -> system prompt + user prompt template -> strict JSON -> state transition -> next tool
```

The model never receives an unbounded instruction like "make it look good". It receives bounded slots and must emit schema-shaped JSON.

## Required Prompt Packaging

Every backend model call must persist:

| Artifact | Meaning |
|---|---|
| `workspace_boundary.json` | `validate_workspace_boundary.py` report proving all planned prompt/model/decision output paths resolve inside the execution root |
| `prompt_instance.json` | system prompt, user prompt after slot fill, model name/provider if used |
| `slot_values.json` | exact structured data sent into prompt slots |
| `model_output.json` | raw model JSON output |
| `decision_record.json` | normalized verdict, confidence, next_state, evidence refs |

These artifacts must be referenced from `decision_log.jsonl`.

The executor must write these artifacts with tools or shell/Python commands anchored to the execution root after the boundary report passes. `apply_patch` is not the runtime writer for model-call artifacts. If any artifact path cannot be proven inside the run root, the model-backed state stops with `S_FAIL_PROCESS_CONTRACT`.

## Product-Quality Generation Boundary

The current executable selftest generator is intentionally low fidelity:

```text
tools\generators\generate_backfill_candidate.py
```

It creates a real candidate PDF by redacting extractable source-language lines and inserting deterministic placeholder target-language text. It proves backfill mechanics and product-quality failure handling, but it does not implement full semantic translation or polished layout.

For semantic translation/backfill, `S7_GenerateCandidate` is backed by:

```text
tools\validators\validate_semantic_translations.py
tools\planners\build_layout_policy.py
tools\generators\generate_semantic_backfill.py
```

The semantic generator consumes real:

```text
docs\input\semantic_translations\<case_id>.translations.json
source_extraction.json
layout_policy.json
source_pdf
font_capabilities
redaction_fill_plan
```

It does not call a model, does not invent translations, and does not invent document-specific layout abbreviations. A model-backed or human-reviewed D2 step must create the semantic translations JSON first and persist `workspace_boundary.json`, `prompt_instance.json`, `slot_values.json`, `model_output.json`, and `decision_record.json`. The D2 output must be an actual semantic translation, not a line-category description such as `This line reports...` or `本行说明...`.

The semantic generator is policy-driven: it redacts source text per extracted unit, then executes `layout_policy.json` to reflow paragraph, table note, footnote, and multi-line heading translations into region text boxes; group aligned article paragraphs as `body_flow` only when policy evidence supports it; join same-paragraph source-wrapped lines by y-gap rather than forcing `\n\n` between every region; classify dense table/chart labels as `table_cell` when policy says so; rotate side navigation labels only when `draw_modes.vertical_nav` says so; and preserve compact labels and legends when the policy says so. It may still fail visual similarity, table, chart, font-hierarchy, or footnote rhythm gates. That is a product-quality failure, not permission to fall back to placeholders, pseudo translations, line-by-line source bbox copying, or hardcoded generator constants.

## New Codex Reproduction Rule

A new Codex should not read prompt templates alone. It must read:

```text
pdf_translation_workflow_core\README.md
pdf_translation_workflow_core\contracts\state_machine.md
pdf_translation_workflow_core\contracts\tool_contracts.md
pdf_translation_workflow_core\contracts\product_quality_contract.md
pdf_translation_workflow_core\prompts\prompt_manifest.json
pdf_translation_workflow_core\prompts\prompt_tool_bindings.json
pdf_translation_workflow_core\prompts\templates\*.prompt.json
```

Then it must execute the bound tools and persist the prompt instances and decisions.
