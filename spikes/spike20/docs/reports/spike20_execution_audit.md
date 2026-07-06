# Spike20 Round13 Source PDF Puncture Execution Audit

- generated_at_local: `2026-07-06 23:43:19`
- workspace_root: `D:\项目\开源项目\MerqFin\spikes\独立测试\spikes\spike20`
- execution_mode: `product_quality`
- final_process_contract_verdict: `PASS`
- final_product_quality_verdict: `FAIL`
- terminal_state: `S_FAIL_QUALITY`
- adaptive_changes_made: `false`

## Scope and Assumptions

- All commands were executed from the spike20 root; evidence paths below are all relative to that root.
- Top-level `input` was treated as the only source-PDF input boundary. No parent `round13` semantic JSON, official bilingual reference, previous candidate PDF, or parent report was used as runtime evidence.
- Frozen framework/docs/request files were not modified. Runtime evidence was written under `docs/input`, `docs/output`, and `docs/reports`.
- Candidate PDFs are evidence artifacts only; because product quality is `FAIL`, they are not accepted final translations.

## Required Files Read

| file | exists | sha256 |
|---|---:|---|
| `run_request.json` | `true` | `3de59c36b4fd0458827605795ce21b01261a40c132d899c7b36ec4b62859e6cc` |
| `SPIKE20_PACKAGE_MANIFEST.md` | `true` | `bf2bc45546b59e400a5810d082df02bf6499c720b7582f110a4b9b834b32a260` |
| `docs/测试提示词/SPIKE20_ROUND13_SOURCE_PUNCTURE_PROMPT.md` | `true` | `ebd6ba8e849d54ce825f5a79982a74206f1683a44823b8236a5dc3912d58b369` |
| `docs/业务流程/PDF_语义翻译回填_标准流程设计.md` | `true` | `6c6445cf62f7ac5c189db9970b44043d36ef562b92debd36c01b29bd86a735fa` |
| `pdf_translation_workflow_core/README.md` | `true` | `df1079b73c514186c3b24885bb9e414dd0dce7164f45cf913a6994938b70264c` |
| `pdf_translation_workflow_core/tools/README.md` | `true` | `6497971091da1a94c77f98182c43010f64040a6d009241dd9354c7cd4247d090` |
| `pdf_translation_workflow_core/contracts/state_machine.md` | `true` | `2aab90a8fd5a5d9f28ae30b3fdd7ed07c560e8ce22b5fd8f36fb9c944414f833` |
| `pdf_translation_workflow_core/contracts/tool_contracts.md` | `true` | `bd861d48844fd6beda66cef2190210f0e058d9c49d82630c9a29da3698901744` |
| `pdf_translation_workflow_core/contracts/decision_contracts.md` | `true` | `a661796a47397a6d9dd7b4efd33431fbce1448b4d2cf41303cd56a2911ea9285` |
| `pdf_translation_workflow_core/contracts/semantic_translation_contract.md` | `true` | `003322c4a225fe41db4427139414bf0b0f1a59387bb289e8c2c0f6193f890340` |
| `pdf_translation_workflow_core/contracts/product_quality_contract.md` | `true` | `4815f7a29342bc5cb89340efa30ed73b258c8cdaf192213f5c11300d87b38067` |
| `pdf_translation_workflow_core/contracts/page_type_repair_matrix.md` | `true` | `4b4e66b7020db5caa0b301ae5118c5c0c8539452394e9be771437898d28f6fe2` |
| `pdf_translation_workflow_core/contracts/change_control_contract.md` | `true` | `175bf490ff144cc462938200b7168cb62c07cf04338605c2a3f9a073a64225f3` |
| `pdf_translation_workflow_core/prompts/model_tool_orchestration_contract.md` | `true` | `7a1ef40f119026238aeec8f1b160953b0c671535396e73a29d24ccab62e824a0` |
| `pdf_translation_workflow_core/prompts/prompt_tool_bindings.json` | `true` | `8ab07bed2e45b91bf5bcf0d5639f3cc4f37f642d096e7bf5f220d952bf52cc60` |
| `pdf_translation_workflow_core/prompts/templates/D2_translation.prompt.json` | `true` | `d0e4006e58cf8d2ca50ed3ccc02f51e0268edcf9f31dd18391f6a1d9aaf3a261` |
| `pdf_translation_workflow_core/prompts/templates/D4_layout_plan.prompt.json` | `true` | `717761715de9e7b9664345d7535274e3c4631588abbe3164a5db7d6f330e8191` |
| `pdf_translation_workflow_core/prompts/templates/D5_D7_quality_gate.prompt.json` | `true` | `582ae0147cf4de0d094014aa29083bdbbac43f15dc739c9ec13786dfa1087a11` |
| `pdf_translation_workflow_core/prompts/templates/D8_repair_selection.prompt.json` | `true` | `0c7276c46ed9503c5c700057d02bb6e89082ce28dbce3c2000a060a74ae164a7` |
| `pdf_translation_workflow_core/prompts/templates/D9_final_acceptance.prompt.json` | `true` | `f14b3f09eb7e3657fe6577697c261ba300fdf7c5ac2ef276343eda87e597eeb7` |

## Input Purity

- input_purity: `PASS`
- Evidence: top-level `input` contains exactly these PDF files:
  - `00005_2025_interim_report_zh.pdf` bytes=5252473 sha256=`7a9dcdcb77be921d41adac3eae2b77ac3f11c8dd86209f668aef48b5ad65c0d3`
  - `00388_2026_annual_report_en.pdf` bytes=71026 sha256=`0642d314ec4c8b3837564187aa751dca6e61ffe94c1cc4f8b61721dc89b820d6`
  - `00992_2023_annual_report_en.pdf` bytes=638927 sha256=`2919b1f547165a9b41db9f5831d45fdb5ca99f3447cd6759063aeed18a3ede65`
  - `建業新生活有限公司_c.pdf` bytes=168896 sha256=`d1b3e97bb552f785bde132029fdb722b56c7c65687ba9c777c9e0de164164c97`
  - `建業新生活有限公司_e.pdf` bytes=86111 sha256=`61cd782456e9cd44055e79aeb403e6bd2d0cc67ae963444edf56566c140ef0a1`

## S1/S2 Preflight

- workspace_boundary_preflight: `PASS` -> `docs/reports/workspace_boundary_preflight.json`
- tool_probe.required_ok: `True` -> `docs/reports/tool_probe.json`
- tool packages: fitz=True, pypdf=True, pdfplumber=True, PIL=True

## Language Detection

Current extraction text was used; filenames and `run_request.json` were not the sole decision source.

| case | metadata | detected | mismatch | cjk | latin_letters | lines |
|---|---:|---:|---:|---:|---:|---:|
| `S20_00005_2025_interim_report_zh` | `zh` | `zh` | `False` | 5686 | 396 | 3884 |
| `S20_00388_2026_annual_report_en` | `en` | `en` | `False` | 10 | 1117 | 138 |
| `S20_00992_2023_annual_report_en` | `en` | `en` | `False` | 0 | 20046 | 562 |
| `S20_建業新生活有限公司_c` | `zh` | `zh` | `False` | 2888 | 45 | 139 |
| `S20_建業新生活有限公司_e` | `en` | `en` | `False` | 9 | 10293 | 227 |

## State Transitions

- Required states executed: `S1_ContractLoad`, `S2_ToolProbe`, `S3_SourceExtract`, `S5_TranslationPlan`, `S6_LayoutPlan`, `S7_GenerateCandidate`, `S8_VerifyProductQuality`, `Lx_RepairLoop`, `S9_VerifyProcessContract`.
- process validator states_seen: `Lx_RepairLoop, S0_Request, S1_ContractLoad, S2_ToolProbe, S3_SourceExtract, S4_PageStrategy, S5_TranslationPlan, S6_LayoutPlan, S7_GenerateCandidate, S8_VerifyProductQuality, S_FAIL_QUALITY`
- process validator decisions_seen: `D1_role_classification, D2_translation, D3_visual_only_text, D4_layout_plan, D5_initial_verification, D6_user_feedback_adjudication, D7_similarity_gate, D8_minimal_repair_selection, D9_final_acceptance`
- Full transition records: `docs/reports/state_trace.json`.

## Command and Tool Invocation Records

- Manual S3/S5/D2 and runner invocation command records: `docs/reports/spike20_operation_log.jsonl` (136 JSONL records). Each record includes `command`, `input_artifacts`, `output_artifacts`, `returncode`, stdout, and stderr.
- Runner S6-S9 atomic command records: `docs/reports/operation_log.jsonl` (64 JSONL records). Each record includes `cmd`, `input_artifacts`, `output_artifacts`, `returncode`, and workspace-boundary evidence.
- Decision records: `docs/reports/decision_log.jsonl`.

## S3/S5 Semantic Translation Materialization

| case | source units | batches | materialized | batch pass/fail | assembly | semantic validation | translation JSON |
|---|---:|---:|---:|---:|---:|---:|---|
| `S20_00005_2025_interim_report_zh` | 857 | 22 | 22 | 22/0 | `PASS` | `PASS` | `docs/input/semantic_translations/S20_00005_2025_interim_report_zh.translations.json` |
| `S20_00388_2026_annual_report_en` | 64 | 2 | 2 | 2/0 | `PASS` | `PASS` | `docs/input/semantic_translations/S20_00388_2026_annual_report_en.translations.json` |
| `S20_00992_2023_annual_report_en` | 493 | 13 | 13 | 13/0 | `PASS` | `PASS` | `docs/input/semantic_translations/S20_00992_2023_annual_report_en.translations.json` |
| `S20_建業新生活有限公司_c` | 132 | 4 | 4 | 4/0 | `PASS` | `PASS` | `docs/input/semantic_translations/S20_建業新生活有限公司_c.translations.json` |
| `S20_建業新生活有限公司_e` | 216 | 6 | 6 | 6/0 | `PASS` | `PASS` | `docs/input/semantic_translations/S20_建業新生活有限公司_e.translations.json` |

### D2 Batch Artifact Index

| case | batch | prompt_instance | model_output | decision_record | validation | workspace_boundary |
|---|---|---|---|---|---|---|
| `S20_00005_2025_interim_report_zh` | `batch_0001` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0001.prompt_instance.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0001.model_output.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0001.decision_record.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0001.validation.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0001.workspace_boundary.json` |
| `S20_00005_2025_interim_report_zh` | `batch_0002` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0002.prompt_instance.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0002.model_output.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0002.decision_record.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0002.validation.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0002.workspace_boundary.json` |
| `S20_00005_2025_interim_report_zh` | `batch_0003` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0003.prompt_instance.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0003.model_output.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0003.decision_record.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0003.validation.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0003.workspace_boundary.json` |
| `S20_00005_2025_interim_report_zh` | `batch_0004` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0004.prompt_instance.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0004.model_output.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0004.decision_record.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0004.validation.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0004.workspace_boundary.json` |
| `S20_00005_2025_interim_report_zh` | `batch_0005` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0005.prompt_instance.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0005.model_output.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0005.decision_record.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0005.validation.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0005.workspace_boundary.json` |
| `S20_00005_2025_interim_report_zh` | `batch_0006` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0006.prompt_instance.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0006.model_output.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0006.decision_record.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0006.validation.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0006.workspace_boundary.json` |
| `S20_00005_2025_interim_report_zh` | `batch_0007` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0007.prompt_instance.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0007.model_output.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0007.decision_record.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0007.validation.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0007.workspace_boundary.json` |
| `S20_00005_2025_interim_report_zh` | `batch_0008` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0008.prompt_instance.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0008.model_output.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0008.decision_record.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0008.validation.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0008.workspace_boundary.json` |
| `S20_00005_2025_interim_report_zh` | `batch_0009` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0009.prompt_instance.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0009.model_output.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0009.decision_record.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0009.validation.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0009.workspace_boundary.json` |
| `S20_00005_2025_interim_report_zh` | `batch_0010` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0010.prompt_instance.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0010.model_output.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0010.decision_record.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0010.validation.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0010.workspace_boundary.json` |
| `S20_00005_2025_interim_report_zh` | `batch_0011` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0011.prompt_instance.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0011.model_output.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0011.decision_record.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0011.validation.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0011.workspace_boundary.json` |
| `S20_00005_2025_interim_report_zh` | `batch_0012` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0012.prompt_instance.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0012.model_output.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0012.decision_record.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0012.validation.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0012.workspace_boundary.json` |
| `S20_00005_2025_interim_report_zh` | `batch_0013` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0013.prompt_instance.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0013.model_output.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0013.decision_record.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0013.validation.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0013.workspace_boundary.json` |
| `S20_00005_2025_interim_report_zh` | `batch_0014` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0014.prompt_instance.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0014.model_output.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0014.decision_record.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0014.validation.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0014.workspace_boundary.json` |
| `S20_00005_2025_interim_report_zh` | `batch_0015` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0015.prompt_instance.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0015.model_output.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0015.decision_record.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0015.validation.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0015.workspace_boundary.json` |
| `S20_00005_2025_interim_report_zh` | `batch_0016` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0016.prompt_instance.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0016.model_output.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0016.decision_record.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0016.validation.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0016.workspace_boundary.json` |
| `S20_00005_2025_interim_report_zh` | `batch_0017` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0017.prompt_instance.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0017.model_output.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0017.decision_record.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0017.validation.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0017.workspace_boundary.json` |
| `S20_00005_2025_interim_report_zh` | `batch_0018` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0018.prompt_instance.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0018.model_output.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0018.decision_record.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0018.validation.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0018.workspace_boundary.json` |
| `S20_00005_2025_interim_report_zh` | `batch_0019` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0019.prompt_instance.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0019.model_output.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0019.decision_record.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0019.validation.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0019.workspace_boundary.json` |
| `S20_00005_2025_interim_report_zh` | `batch_0020` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0020.prompt_instance.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0020.model_output.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0020.decision_record.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0020.validation.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0020.workspace_boundary.json` |
| `S20_00005_2025_interim_report_zh` | `batch_0021` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0021.prompt_instance.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0021.model_output.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0021.decision_record.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0021.validation.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0021.workspace_boundary.json` |
| `S20_00005_2025_interim_report_zh` | `batch_0022` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0022.prompt_instance.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0022.model_output.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0022.decision_record.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0022.validation.json` | `docs/reports/S20_00005_2025_interim_report_zh/translation_batches/batch_0022.workspace_boundary.json` |
| `S20_00388_2026_annual_report_en` | `batch_0001` | `docs/reports/S20_00388_2026_annual_report_en/translation_batches/batch_0001.prompt_instance.json` | `docs/reports/S20_00388_2026_annual_report_en/translation_batches/batch_0001.model_output.json` | `docs/reports/S20_00388_2026_annual_report_en/translation_batches/batch_0001.decision_record.json` | `docs/reports/S20_00388_2026_annual_report_en/translation_batches/batch_0001.validation.json` | `docs/reports/S20_00388_2026_annual_report_en/translation_batches/batch_0001.workspace_boundary.json` |
| `S20_00388_2026_annual_report_en` | `batch_0002` | `docs/reports/S20_00388_2026_annual_report_en/translation_batches/batch_0002.prompt_instance.json` | `docs/reports/S20_00388_2026_annual_report_en/translation_batches/batch_0002.model_output.json` | `docs/reports/S20_00388_2026_annual_report_en/translation_batches/batch_0002.decision_record.json` | `docs/reports/S20_00388_2026_annual_report_en/translation_batches/batch_0002.validation.json` | `docs/reports/S20_00388_2026_annual_report_en/translation_batches/batch_0002.workspace_boundary.json` |
| `S20_00992_2023_annual_report_en` | `batch_0001` | `docs/reports/S20_00992_2023_annual_report_en/translation_batches/batch_0001.prompt_instance.json` | `docs/reports/S20_00992_2023_annual_report_en/translation_batches/batch_0001.model_output.json` | `docs/reports/S20_00992_2023_annual_report_en/translation_batches/batch_0001.decision_record.json` | `docs/reports/S20_00992_2023_annual_report_en/translation_batches/batch_0001.validation.json` | `docs/reports/S20_00992_2023_annual_report_en/translation_batches/batch_0001.workspace_boundary.json` |
| `S20_00992_2023_annual_report_en` | `batch_0002` | `docs/reports/S20_00992_2023_annual_report_en/translation_batches/batch_0002.prompt_instance.json` | `docs/reports/S20_00992_2023_annual_report_en/translation_batches/batch_0002.model_output.json` | `docs/reports/S20_00992_2023_annual_report_en/translation_batches/batch_0002.decision_record.json` | `docs/reports/S20_00992_2023_annual_report_en/translation_batches/batch_0002.validation.json` | `docs/reports/S20_00992_2023_annual_report_en/translation_batches/batch_0002.workspace_boundary.json` |
| `S20_00992_2023_annual_report_en` | `batch_0003` | `docs/reports/S20_00992_2023_annual_report_en/translation_batches/batch_0003.prompt_instance.json` | `docs/reports/S20_00992_2023_annual_report_en/translation_batches/batch_0003.model_output.json` | `docs/reports/S20_00992_2023_annual_report_en/translation_batches/batch_0003.decision_record.json` | `docs/reports/S20_00992_2023_annual_report_en/translation_batches/batch_0003.validation.json` | `docs/reports/S20_00992_2023_annual_report_en/translation_batches/batch_0003.workspace_boundary.json` |
| `S20_00992_2023_annual_report_en` | `batch_0004` | `docs/reports/S20_00992_2023_annual_report_en/translation_batches/batch_0004.prompt_instance.json` | `docs/reports/S20_00992_2023_annual_report_en/translation_batches/batch_0004.model_output.json` | `docs/reports/S20_00992_2023_annual_report_en/translation_batches/batch_0004.decision_record.json` | `docs/reports/S20_00992_2023_annual_report_en/translation_batches/batch_0004.validation.json` | `docs/reports/S20_00992_2023_annual_report_en/translation_batches/batch_0004.workspace_boundary.json` |
| `S20_00992_2023_annual_report_en` | `batch_0005` | `docs/reports/S20_00992_2023_annual_report_en/translation_batches/batch_0005.prompt_instance.json` | `docs/reports/S20_00992_2023_annual_report_en/translation_batches/batch_0005.model_output.json` | `docs/reports/S20_00992_2023_annual_report_en/translation_batches/batch_0005.decision_record.json` | `docs/reports/S20_00992_2023_annual_report_en/translation_batches/batch_0005.validation.json` | `docs/reports/S20_00992_2023_annual_report_en/translation_batches/batch_0005.workspace_boundary.json` |
| `S20_00992_2023_annual_report_en` | `batch_0006` | `docs/reports/S20_00992_2023_annual_report_en/translation_batches/batch_0006.prompt_instance.json` | `docs/reports/S20_00992_2023_annual_report_en/translation_batches/batch_0006.model_output.json` | `docs/reports/S20_00992_2023_annual_report_en/translation_batches/batch_0006.decision_record.json` | `docs/reports/S20_00992_2023_annual_report_en/translation_batches/batch_0006.validation.json` | `docs/reports/S20_00992_2023_annual_report_en/translation_batches/batch_0006.workspace_boundary.json` |
| `S20_00992_2023_annual_report_en` | `batch_0007` | `docs/reports/S20_00992_2023_annual_report_en/translation_batches/batch_0007.prompt_instance.json` | `docs/reports/S20_00992_2023_annual_report_en/translation_batches/batch_0007.model_output.json` | `docs/reports/S20_00992_2023_annual_report_en/translation_batches/batch_0007.decision_record.json` | `docs/reports/S20_00992_2023_annual_report_en/translation_batches/batch_0007.validation.json` | `docs/reports/S20_00992_2023_annual_report_en/translation_batches/batch_0007.workspace_boundary.json` |
| `S20_00992_2023_annual_report_en` | `batch_0008` | `docs/reports/S20_00992_2023_annual_report_en/translation_batches/batch_0008.prompt_instance.json` | `docs/reports/S20_00992_2023_annual_report_en/translation_batches/batch_0008.model_output.json` | `docs/reports/S20_00992_2023_annual_report_en/translation_batches/batch_0008.decision_record.json` | `docs/reports/S20_00992_2023_annual_report_en/translation_batches/batch_0008.validation.json` | `docs/reports/S20_00992_2023_annual_report_en/translation_batches/batch_0008.workspace_boundary.json` |
| `S20_00992_2023_annual_report_en` | `batch_0009` | `docs/reports/S20_00992_2023_annual_report_en/translation_batches/batch_0009.prompt_instance.json` | `docs/reports/S20_00992_2023_annual_report_en/translation_batches/batch_0009.model_output.json` | `docs/reports/S20_00992_2023_annual_report_en/translation_batches/batch_0009.decision_record.json` | `docs/reports/S20_00992_2023_annual_report_en/translation_batches/batch_0009.validation.json` | `docs/reports/S20_00992_2023_annual_report_en/translation_batches/batch_0009.workspace_boundary.json` |
| `S20_00992_2023_annual_report_en` | `batch_0010` | `docs/reports/S20_00992_2023_annual_report_en/translation_batches/batch_0010.prompt_instance.json` | `docs/reports/S20_00992_2023_annual_report_en/translation_batches/batch_0010.model_output.json` | `docs/reports/S20_00992_2023_annual_report_en/translation_batches/batch_0010.decision_record.json` | `docs/reports/S20_00992_2023_annual_report_en/translation_batches/batch_0010.validation.json` | `docs/reports/S20_00992_2023_annual_report_en/translation_batches/batch_0010.workspace_boundary.json` |
| `S20_00992_2023_annual_report_en` | `batch_0011` | `docs/reports/S20_00992_2023_annual_report_en/translation_batches/batch_0011.prompt_instance.json` | `docs/reports/S20_00992_2023_annual_report_en/translation_batches/batch_0011.model_output.json` | `docs/reports/S20_00992_2023_annual_report_en/translation_batches/batch_0011.decision_record.json` | `docs/reports/S20_00992_2023_annual_report_en/translation_batches/batch_0011.validation.json` | `docs/reports/S20_00992_2023_annual_report_en/translation_batches/batch_0011.workspace_boundary.json` |
| `S20_00992_2023_annual_report_en` | `batch_0012` | `docs/reports/S20_00992_2023_annual_report_en/translation_batches/batch_0012.prompt_instance.json` | `docs/reports/S20_00992_2023_annual_report_en/translation_batches/batch_0012.model_output.json` | `docs/reports/S20_00992_2023_annual_report_en/translation_batches/batch_0012.decision_record.json` | `docs/reports/S20_00992_2023_annual_report_en/translation_batches/batch_0012.validation.json` | `docs/reports/S20_00992_2023_annual_report_en/translation_batches/batch_0012.workspace_boundary.json` |
| `S20_00992_2023_annual_report_en` | `batch_0013` | `docs/reports/S20_00992_2023_annual_report_en/translation_batches/batch_0013.prompt_instance.json` | `docs/reports/S20_00992_2023_annual_report_en/translation_batches/batch_0013.model_output.json` | `docs/reports/S20_00992_2023_annual_report_en/translation_batches/batch_0013.decision_record.json` | `docs/reports/S20_00992_2023_annual_report_en/translation_batches/batch_0013.validation.json` | `docs/reports/S20_00992_2023_annual_report_en/translation_batches/batch_0013.workspace_boundary.json` |
| `S20_建業新生活有限公司_c` | `batch_0001` | `docs/reports/S20_建業新生活有限公司_c/translation_batches/batch_0001.prompt_instance.json` | `docs/reports/S20_建業新生活有限公司_c/translation_batches/batch_0001.model_output.json` | `docs/reports/S20_建業新生活有限公司_c/translation_batches/batch_0001.decision_record.json` | `docs/reports/S20_建業新生活有限公司_c/translation_batches/batch_0001.validation.json` | `docs/reports/S20_建業新生活有限公司_c/translation_batches/batch_0001.workspace_boundary.json` |
| `S20_建業新生活有限公司_c` | `batch_0002` | `docs/reports/S20_建業新生活有限公司_c/translation_batches/batch_0002.prompt_instance.json` | `docs/reports/S20_建業新生活有限公司_c/translation_batches/batch_0002.model_output.json` | `docs/reports/S20_建業新生活有限公司_c/translation_batches/batch_0002.decision_record.json` | `docs/reports/S20_建業新生活有限公司_c/translation_batches/batch_0002.validation.json` | `docs/reports/S20_建業新生活有限公司_c/translation_batches/batch_0002.workspace_boundary.json` |
| `S20_建業新生活有限公司_c` | `batch_0003` | `docs/reports/S20_建業新生活有限公司_c/translation_batches/batch_0003.prompt_instance.json` | `docs/reports/S20_建業新生活有限公司_c/translation_batches/batch_0003.model_output.json` | `docs/reports/S20_建業新生活有限公司_c/translation_batches/batch_0003.decision_record.json` | `docs/reports/S20_建業新生活有限公司_c/translation_batches/batch_0003.validation.json` | `docs/reports/S20_建業新生活有限公司_c/translation_batches/batch_0003.workspace_boundary.json` |
| `S20_建業新生活有限公司_c` | `batch_0004` | `docs/reports/S20_建業新生活有限公司_c/translation_batches/batch_0004.prompt_instance.json` | `docs/reports/S20_建業新生活有限公司_c/translation_batches/batch_0004.model_output.json` | `docs/reports/S20_建業新生活有限公司_c/translation_batches/batch_0004.decision_record.json` | `docs/reports/S20_建業新生活有限公司_c/translation_batches/batch_0004.validation.json` | `docs/reports/S20_建業新生活有限公司_c/translation_batches/batch_0004.workspace_boundary.json` |
| `S20_建業新生活有限公司_e` | `batch_0001` | `docs/reports/S20_建業新生活有限公司_e/translation_batches/batch_0001.prompt_instance.json` | `docs/reports/S20_建業新生活有限公司_e/translation_batches/batch_0001.model_output.json` | `docs/reports/S20_建業新生活有限公司_e/translation_batches/batch_0001.decision_record.json` | `docs/reports/S20_建業新生活有限公司_e/translation_batches/batch_0001.validation.json` | `docs/reports/S20_建業新生活有限公司_e/translation_batches/batch_0001.workspace_boundary.json` |
| `S20_建業新生活有限公司_e` | `batch_0002` | `docs/reports/S20_建業新生活有限公司_e/translation_batches/batch_0002.prompt_instance.json` | `docs/reports/S20_建業新生活有限公司_e/translation_batches/batch_0002.model_output.json` | `docs/reports/S20_建業新生活有限公司_e/translation_batches/batch_0002.decision_record.json` | `docs/reports/S20_建業新生活有限公司_e/translation_batches/batch_0002.validation.json` | `docs/reports/S20_建業新生活有限公司_e/translation_batches/batch_0002.workspace_boundary.json` |
| `S20_建業新生活有限公司_e` | `batch_0003` | `docs/reports/S20_建業新生活有限公司_e/translation_batches/batch_0003.prompt_instance.json` | `docs/reports/S20_建業新生活有限公司_e/translation_batches/batch_0003.model_output.json` | `docs/reports/S20_建業新生活有限公司_e/translation_batches/batch_0003.decision_record.json` | `docs/reports/S20_建業新生活有限公司_e/translation_batches/batch_0003.validation.json` | `docs/reports/S20_建業新生活有限公司_e/translation_batches/batch_0003.workspace_boundary.json` |
| `S20_建業新生活有限公司_e` | `batch_0004` | `docs/reports/S20_建業新生活有限公司_e/translation_batches/batch_0004.prompt_instance.json` | `docs/reports/S20_建業新生活有限公司_e/translation_batches/batch_0004.model_output.json` | `docs/reports/S20_建業新生活有限公司_e/translation_batches/batch_0004.decision_record.json` | `docs/reports/S20_建業新生活有限公司_e/translation_batches/batch_0004.validation.json` | `docs/reports/S20_建業新生活有限公司_e/translation_batches/batch_0004.workspace_boundary.json` |
| `S20_建業新生活有限公司_e` | `batch_0005` | `docs/reports/S20_建業新生活有限公司_e/translation_batches/batch_0005.prompt_instance.json` | `docs/reports/S20_建業新生活有限公司_e/translation_batches/batch_0005.model_output.json` | `docs/reports/S20_建業新生活有限公司_e/translation_batches/batch_0005.decision_record.json` | `docs/reports/S20_建業新生活有限公司_e/translation_batches/batch_0005.validation.json` | `docs/reports/S20_建業新生活有限公司_e/translation_batches/batch_0005.workspace_boundary.json` |
| `S20_建業新生活有限公司_e` | `batch_0006` | `docs/reports/S20_建業新生活有限公司_e/translation_batches/batch_0006.prompt_instance.json` | `docs/reports/S20_建業新生活有限公司_e/translation_batches/batch_0006.model_output.json` | `docs/reports/S20_建業新生活有限公司_e/translation_batches/batch_0006.decision_record.json` | `docs/reports/S20_建業新生活有限公司_e/translation_batches/batch_0006.validation.json` | `docs/reports/S20_建業新生活有限公司_e/translation_batches/batch_0006.workspace_boundary.json` |

## S6-S9 Product Quality Execution

| case | candidate | S8 artifacts | previews | product | failed gates | repair loop |
|---|---|---:|---:|---:|---|---|
| `S20_00005_2025_interim_report_zh` | `docs/output/S20_00005_2025_interim_report_zh_candidate.pdf` | `True` | 9 | `FAIL` | text_residue, visual_similarity, sidebar_navigation_legibility, table_text_legibility, background_residue_artifact, matrix_diagram_integrity | docs/reports/S20_00005_2025_interim_report_zh/repair_loop_0001.json |
| `S20_00388_2026_annual_report_en` | `docs/output/S20_00388_2026_annual_report_en_candidate.pdf` | `True` | 1 | `PASS` | - | - |
| `S20_00992_2023_annual_report_en` | `docs/output/S20_00992_2023_annual_report_en_candidate.pdf` | `True` | 11 | `PASS` | - | - |
| `S20_建業新生活有限公司_c` | `docs/output/S20_建業新生活有限公司_c_candidate.pdf` | `True` | 6 | `PASS` | - | - |
| `S20_建業新生活有限公司_e` | `docs/output/S20_建業新生活有限公司_e_candidate.pdf` | `True` | 6 | `PASS` | - | - |

## D7/D8/Lx Result

- `S20_00005_2025_interim_report_zh` has blocking failures: `text_residue`, `visual_similarity`, `sidebar_navigation_legibility`, `table_text_legibility`, `background_residue_artifact`, `matrix_diagram_integrity`.
- D8 was not skipped. Evidence: `docs/reports/S20_00005_2025_interim_report_zh/repair_loop_0001.json`.
- `repair_loop_0001.json` records `execution_status=not_executed_unrepairable` because repair atoms were selected but no generic atom executor is wired for the failure class. This routes to `S_FAIL_QUALITY`, not process failure.

## Prompt Template and Decision Boundary

- D2 materialization used `pdf_translation_workflow_core/prompts/templates/D2_translation.prompt.json` for every batch and persisted prompt/model/decision artifacts before validation.
- Runner decision log records prompt contracts for D4 (`D4_layout_plan.prompt.json`), D5/D7 (`D5_D7_quality_gate.prompt.json`), D8 (`D8_repair_selection.prompt.json`), and D9 (`D9_final_acceptance.prompt.json`).
- Backend model calls were not made inside the runner decisions; deterministic tool outputs and recorded prompt contracts were used. Translation provider evidence is `google_translate_web_gtx` in D2 materialization outputs.

## Framework Immutability and Anti-overfit

- framework_immutability: `PASS`; no adaptive change was made and `git status --short -- pdf_translation_workflow_core docs\业务流程 docs\测试提示词 run_request.json SPIKE20_PACKAGE_MANIFEST.md` returned no modified paths.
- anti_overfit_scan: `PASS` -> `docs/reports/anti_overfit_scan.json`, blocking_hit_count=0
- adaptive_change_refs: `[]`

## Final Verdict

- process_contract_verdict: `PASS`
- product_quality_verdict: `FAIL`
- terminal_state: `S_FAIL_QUALITY`
- Failure class: product quality failure. Process artifacts, workspace boundaries, S5 materialization, S8 artifacts, D8 repair selection, process validation, and anti-overfit scan passed; one candidate remains visually/structurally unacceptable.

## Design Gaps Found

- `missing_generic_repair_atom_executor`: D8 selected repair atoms for sidebar/table/background/matrix failures, but no generic executor is wired, so repair_loop_0001 records not_executed_unrepairable and terminal_state remains S_FAIL_QUALITY. Evidence: `docs/reports/S20_00005_2025_interim_report_zh/repair_loop_0001.json`
- `complex_table_sidebar_quality_not_closed`: The 00005 zh-to-en candidate still has blocking text residue, visual similarity, sidebar navigation, table text, background residue, and matrix diagram integrity failures. Evidence: `docs/reports/S20_00005_2025_interim_report_zh/product_quality_gates.json`

## Evidence Map

- execution_audit: `docs/reports/spike20_execution_audit.md`
- runner_verdict: `docs/reports/S20_final_verdict.json`
- runner_report: `docs/reports/S20_execution_report.md`
- manual_operation_log: `docs/reports/spike20_operation_log.jsonl`
- runner_operation_log: `docs/reports/operation_log.jsonl`
- state_trace: `docs/reports/state_trace.json`
- decision_log: `docs/reports/decision_log.jsonl`
- process_validation: `docs/reports/process_validation.json`
- anti_overfit_scan: `docs/reports/anti_overfit_scan.json`
- language_detection: `docs/reports/language_detection_summary.json`
- final_outputs_boundary: `docs/reports/final_outputs_workspace_boundary.json`
