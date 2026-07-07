# Round12 ???? PDF ??????????

## ????
- ????`D:\??\????\MerqFin\spikes\????`
- ?????`docs/input/round12/source_pdfs`
- ?????`docs/output/round12`
- ?????`docs/reports/round12`
- ???????? PDF ? 5 ????? CJK/Latin ??????? `zh->en` ? `en->zh`????????????
- D2 provider?`google_translate_web_gtx`???? `prompt_instance.json`?`model_output.json`?`decision_record.json`??? batch/full semantic validators ???

## ????

| case_id | ?? | ?? | D2 ?? | ???? | ???? | ?? gate | ?? PDF |
|---|---:|---:|---:|---|---|---|---|
| `R12_00005_2025_interim_report_zh` | zh->en | 9 | 857 | PASS | FAIL | text_residue, text_fit, visual_similarity, body_paragraph_readability, footnote_readability, sidebar_navigation_legibility, table_text_legibility, background_residue_artifact, matrix_diagram_integrity | `docs/output/round12/R12_00005_2025_interim_report_zh_candidate.pdf` |
| `R12_00388_2026_annual_report_en` | en->zh | 1 | 64 | PASS | PASS | - | `docs/output/round12/R12_00388_2026_annual_report_en_candidate.pdf` |
| `R12_00992_2023_annual_report_en` | en->zh | 11 | 493 | PASS | FAIL | text_residue, text_fit, visual_similarity, event_card_readability, footnote_readability, short_label_legibility | `docs/output/round12/R12_00992_2023_annual_report_en_candidate.pdf` |
| `R12_jianye_new_life_c` | zh->en | 6 | 132 | PASS | PASS | - | `docs/output/round12/R12_jianye_new_life_c_candidate.pdf` |
| `R12_jianye_new_life_e` | en->zh | 6 | 216 | PASS | PASS | - | `docs/output/round12/R12_jianye_new_life_e_candidate.pdf` |

## ?????????
- added materialize_d2_translation_batches.py runtime D2 materializer
- added target-language-dominant and neutral-identifier source-unit filtering across S5/S7 validators/generator
- added NFKC request normalization and cache target-script validation
- added region rect sanitization before PyMuPDF textbox insertion
- updated zh text_residue quality gate to allow generic neutral identifier/code tokens
- updated standard design document, tool README, and D2 prompt template

## ???????????
- `R12_00005_2025_interim_report_zh`?`text_residue, text_fit, visual_similarity, body_paragraph_readability, footnote_readability, sidebar_navigation_legibility, table_text_legibility, background_residue_artifact, matrix_diagram_integrity`??????????? `S_FAIL_QUALITY`????????????
- `R12_00992_2023_annual_report_en`?`text_residue, text_fit, visual_similarity, event_card_readability, footnote_readability, short_label_legibility`??????????? `S_FAIL_QUALITY`????????????

## ????
- ?? manifest?`docs/reports/round12/round12_input_manifest.json`
- S5 ?????`docs/reports/round12/round12_s5_translation_summary.json`
- ?????`docs/reports/round12/round12_quality_after_neutral_gate_summary.json`
- ???????`docs/reports/round12/anti_overfit_scan.json`
- ?? verdict?`docs/reports/round12/round12_final_verdict.json`
- ???????`docs/reports/round12/process_validation.json`?PASS?
- ?????`docs/reports/round12/round12_operation_log.jsonl`

## ????
??????? 5 ????? PDF ???????????????????? PDF ?????/?????`process_validation.json` ? PASS??????? 3/5 ????? `product_quality_verdict=FAIL`?`00005` ? `00992` ????????/?????/???/??????????????????

## Loop 执行诊断修正

本轮复盘后，`Lx_RepairLoop` 不能被视为完整修复循环。

证据：

- `state_trace.json` 确实出现 `S8_VerifyProductQuality -> Lx_RepairLoop`。
- 但 `docs/reports/round12/**/repair_loop_*.json` 数量为 0。
- 各 case 的 `visual_repair_plan.json` 只能证明生成过修复计划，不能证明 repair atom 已被选择、应用、重新生成并重新门禁。
- `R12_00005_2025_interim_report_zh` 和 `R12_00992_2023_annual_report_en` 仍有阻塞质量失败，但没有后续 repair atom 执行证据。

因此，按修正后的契约重新验证，`process_validation.json` 为 `FAIL`，最终应判：

```json
{
  "process_contract_verdict": "FAIL",
  "product_quality_verdict": "FAIL",
  "repair_loop_execution_verdict": "PARTIAL_NOT_FULL_LOOP",
  "terminal_state": "S_FAIL_PROCESS_CONTRACT"
}
```

Jianye 两个候选 PDF 的 `product_quality_gates.json` 为 PASS，只代表当前机器门禁没有发现 blocking failure；它不等于人工审美验收通过。当前门禁没有把标题层级弱、公告英文不自然、段落节奏和局部异常空白全部建模成 blocking gate。

已补强的通用契约：

- `Lx_RepairLoop` 进入后必须写 `repair_loop_<n>.json`。
- `D8_repair_selection` 必须输出 `repair_loop_record_path` 和 `execution_status`。
- `validate_process_artifacts.py` 会把“进入 Lx 但缺少 repair_loop_<n>.json”判为过程契约失败。
