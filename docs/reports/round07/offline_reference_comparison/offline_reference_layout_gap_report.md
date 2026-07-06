# Round07 Offline Reference Layout Comparison

?????????? PDF ??????????? PDF ???????????????????????????????? core ???

- English reference: `样本\AIA_2020_Annual_Report_en.pdf`
- Chinese reference: `样本\中文\AIA_2020_Annual_Report_zh.pdf`

## zh_to_en - ZH source -> EN candidate compared with official EN pages

### Candidate p01 vs Reference p03
- density: candidate=0.6834 reference=0.677 delta=0.0064
- candidate_content_bbox=(0, 81, 862, 1147)
- reference_content_bbox=(0, 81, 862, 1147)
![comparison](docs/reports/round07/offline_reference_comparison/zh_to_en/zh_to_en_candidate_p01_vs_reference_p03.png)

### Candidate p02 vs Reference p08
- density: candidate=0.116 reference=0.1223 delta=-0.0063
- candidate_content_bbox=(55, 41, 826, 1314)
- reference_content_bbox=(55, 46, 826, 1313)
![comparison](docs/reports/round07/offline_reference_comparison/zh_to_en/zh_to_en_candidate_p02_vs_reference_p08.png)

### Candidate p03 vs Reference p09
- density: candidate=0.1181 reference=0.1296 delta=-0.0115
- candidate_content_bbox=(125, 94, 953, 1317)
- reference_content_bbox=(127, 99, 953, 1313)
![comparison](docs/reports/round07/offline_reference_comparison/zh_to_en/zh_to_en_candidate_p03_vs_reference_p09.png)

### Candidate p04 vs Reference p24
- density: candidate=0.1159 reference=0.1004 delta=0.0155
- candidate_content_bbox=(55, 41, 827, 1314)
- reference_content_bbox=(55, 46, 826, 1313)
![comparison](docs/reports/round07/offline_reference_comparison/zh_to_en/zh_to_en_candidate_p04_vs_reference_p24.png)

### Candidate p05 vs Reference p25
- density: candidate=0.1036 reference=0.0753 delta=0.0283
- candidate_content_bbox=(126, 154, 953, 1317)
- reference_content_bbox=(127, 145, 953, 1313)
![comparison](docs/reports/round07/offline_reference_comparison/zh_to_en/zh_to_en_candidate_p05_vs_reference_p25.png)

## en_to_zh - EN source -> ZH candidate compared with official ZH pages

### Candidate p01 vs Reference p08
- density: candidate=0.1181 reference=0.1274 delta=-0.0093
- candidate_content_bbox=(55, 44, 826, 1312)
- reference_content_bbox=(55, 43, 826, 1313)
![comparison](docs/reports/round07/offline_reference_comparison/en_to_zh/en_to_zh_candidate_p01_vs_reference_p08.png)

### Candidate p02 vs Reference p09
- density: candidate=0.12 reference=0.1242 delta=-0.0042
- candidate_content_bbox=(124, 99, 953, 1312)
- reference_content_bbox=(127, 96, 953, 1313)
![comparison](docs/reports/round07/offline_reference_comparison/en_to_zh/en_to_zh_candidate_p02_vs_reference_p09.png)

### Candidate p03 vs Reference p24
- density: candidate=0.1084 reference=0.105 delta=0.0034
- candidate_content_bbox=(55, 41, 831, 1312)
- reference_content_bbox=(55, 43, 826, 1313)
![comparison](docs/reports/round07/offline_reference_comparison/en_to_zh/en_to_zh_candidate_p03_vs_reference_p24.png)

### Candidate p04 vs Reference p25
- density: candidate=0.0785 reference=0.0647 delta=0.0138
- candidate_content_bbox=(124, 151, 953, 1312)
- reference_content_bbox=(127, 154, 953, 1313)
![comparison](docs/reports/round07/offline_reference_comparison/en_to_zh/en_to_zh_candidate_p04_vs_reference_p25.png)

## Manual Audit Notes

- zh_to_en: pages 24/25-style body copy now uses body_flow with source-gap paragraph breaks; major previous defects of tiny fragmented lines and fallback truncation are repaired.
- zh_to_en: early timeline/chart pages still contain small-label fallback regions; these remain product-quality residual risk, not process-contract failures.
- en_to_zh: page 25-style body copy now preserves paragraph rhythm through profile paragraph_gap_pt=8.0; previous single dense paragraph is repaired.
- Both directions: visual quality is closer to annual-report rhythm, but automated source-vs-output gates still mark FAIL because text density naturally diverges after language conversion and because small labels remain hard cases.