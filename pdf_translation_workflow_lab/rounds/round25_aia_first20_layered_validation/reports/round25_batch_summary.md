# Round25 Batch Summary

Round25 runs the layered state-machine workflow against AIA zh/en first-20-page PDFs and the round24 00005 regression sample.

| Case | Process | Product | Terminal | Loop | Repair accepted | Candidate |
|---|---|---|---|---|---|---|
| `R25_AIA_ZH_TO_EN_pages_001_020` | `PASS` | `FAIL` | `S_FAIL_QUALITY` | `REJECTED_ROLLBACK` | `False` | `output/R25_AIA_ZH_TO_EN_pages_001_020_initial_candidate.pdf` |
| `R25_AIA_EN_TO_ZH_pages_001_020` | `PASS` | `FAIL` | `S_FAIL_QUALITY` | `REJECTED_ROLLBACK` | `False` | `output/R25_AIA_EN_TO_ZH_pages_001_020_initial_candidate.pdf` |
| `R25_REGRESSION_00005_ZH_TO_EN_pages_001_020` | `PASS` | `FAIL` | `S_FAIL_QUALITY` | `REJECTED_ROLLBACK` | `False` | `output/R25_REGRESSION_00005_ZH_TO_EN_pages_001_020_initial_candidate.pdf` |
