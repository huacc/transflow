# Change Control Contract

## Purpose

This contract defines how an execution Codex may improve an incomplete round package without hiding the change.

The goal is not to prevent every change. The goal is to make every change attributable, reviewable, and reusable for the next core revision.

## Change Policy

| Policy | Requirement |
|---|---|
| Workspace scope | Changes are allowed only inside the current round workspace |
| Parent scope | Do not edit parent/root workflow files from a round execution session |
| Minimality | Change the smallest set of files needed to address the observed failure |
| Traceability | Every changed file must map to one failure, one hypothesis, and one verification command |
| Evidence | Record before/after file hashes, command outputs, and final verdict |
| Honesty | Do not claim the original package was sufficient if any file was changed |

## Allowed Changes

| Area | Allowed when |
|---|---|
| `docs\reports` | Always, for audit output |
| `docs\output` | Only through the workflow or explicitly recorded manual artifact generation |
| `docs\测试提示词` | When the prompt is ambiguous, contradictory, or missing required reporting fields |
| `docs\业务流程` | When the process document lacks a state, contract, or decision needed for execution |
| `pdf_translation_workflow_core\contracts` | When a contract is ambiguous or missing a required boundary |
| `pdf_translation_workflow_core\prompts` | When a prompt slot/schema/tool binding is insufficient |
| `pdf_translation_workflow_core\tools` | When the tool implementation cannot satisfy its stated contract or cannot produce required evidence |

## Forbidden Changes

- Do not edit input PDFs.
- Do not delete failed evidence.
- Do not overwrite an earlier report without recording the overwrite.
- Do not hardcode sample filenames, exact source text, known page numbers, fixed coordinates, or known output values into generic tools.
- Do not mark a run as fully reproducible if modifications were required.

## Required Change Log

Every adaptive round must write:

```text
docs\reports\round##_change_log.md
docs\reports\round##_change_manifest_before.json
docs\reports\round##_change_manifest_after.json
docs\reports\round##_change_manifest_delta.json
```

The change log must include:

| Field | Meaning |
|---|---|
| `change_id` | Stable ID such as `C01` |
| `trigger_failure` | Failure, ambiguity, or missing capability that caused the change |
| `hypothesis` | Why this change should address that failure |
| `files_changed` | Relative paths |
| `change_type` | `doc_contract`, `prompt`, `tool`, `test_or_validator`, `report_only` |
| `before_evidence` | File/hash/report/command evidence before the change |
| `after_evidence` | File/hash/report/command evidence after the change |
| `verification_command` | Command used to check the change |
| `result` | `pass`, `fail`, or `partial` |
| `core_backport_recommendation` | Whether the change should be copied back into root core |

## Maturity Signal

For a validation round:

```json
{
  "modification_count": 0,
  "core_sufficiency_observed": "PASS"
}
```

means the current package was sufficient for the round objective.

If `modification_count > 0`, the round is still useful, but it is not proof that the root workflow is complete. The root workflow must be updated from the reported changes before the next round.
