# Prompt Package Contracts

This directory contains backend-model prompt contracts only.

It must not contain prompts written for a human or another Codex session. External validation prompts belong under:

```text
docs\测试提示词
```

## Package Shape

Each prompt package must define:

```json
{
  "prompt_id": "D1_page_strategy",
  "purpose": "...",
  "system_prompt": "...",
  "user_prompt_template": "...",
  "slots": {},
  "required_output_schema": {},
  "failure_policy": "...",
  "anti_overfit_policy": "..."
}
```

## Slot Rule

Slots are structured data produced by tools or upstream workflow states. Do not pass raw prose when a structured artifact exists.

Mandatory common slots:

| Slot | Meaning |
|---|---|
| `run_id` | current run identifier |
| `run_mode` | `process_validation` or `product_quality` |
| `state_id` | current state machine state |
| `source_pdf_ref` | source PDF path/hash/page count |
| `page_context` | current extracted page/region evidence |
| `tool_evidence_refs` | paths to extraction/render/quality JSON |

## Output Rule

Model outputs must be JSON-compatible objects. They must include `verdict`, `confidence`, `evidence_refs`, and `next_state`.

## Tool Binding Rule

Prompt templates are not sufficient by themselves. The state-to-tool-to-prompt binding is defined in:

```text
prompt_tool_bindings.json
model_tool_orchestration_contract.md
```

A run is invalid if it records a model judgement without also recording the tool artifacts that filled that prompt's slots.
