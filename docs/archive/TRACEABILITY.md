# 입력값 추적표

| 구현값 | 값 | 상태 | 근거/처리 |
|---|---:|---|---|
| enclosure | 114×121×82 mm | SOURCE | 논문 Table 1 / PPT |
| cell | 23개, Ø19×68 mm | SOURCE | 논문 Table 1 |
| inlet/outlet | Ø19 / Ø19 mm | SOURCE+DECISION | PPT 측면 치수와 300 dpi 실측; 24 mm 가설 폐기 |
| notch depth | 11.25 mm | SOURCE+USER CONFIRMED | PPT 상·하 노치 깊이 치수 |
| notch mouth/inner | 19/11 mm | SOURCE+MEASURED | PPT 라벨과 300 dpi contour |
| right land | 34 mm | MEASURED+CONSISTENCY | 24 mm 표기는 총폭 104 mm를 만들어 폐기; 명목 합 114 mm |
| default port centres | IN (100.3,107.5), OUT (13.5,13.5) mm | USER CONFIRMED | 2026-09-21 제공 PPT와 완성 모델 사진, 서로 반대 코너 약 4 mm 여유 |
| lattice | 5-4-5-4-5 | SOURCE/MEASURED | 도면 원 중심 검출 |
| x/y/offset pitch | 19.0/22.5/11.25 mm | DECISION | 도면 실측 기본값 |
| coolant | ρ 998.2, Cp 4182, k 0.598, μ 0.00112 SI | SOURCE | 논문 Table 4 |
| inlet | 0.4 m/s, 293 K, TI 5% | SOURCE | 논문 Table 2 |
| outlet | 101325 Pa absolute | SOURCE+DECISION | 내부 gauge reference도 101325 Pa |
| battery | ρ 2000, Cp 900, k 27 SI | SOURCE | 논문 Table 3 |
| qvol | 16,325 W/m³ | SOURCE | 논문 baseline 본문 |
| cell heat | 0.314745 W | DERIVED | `qvol·π(D/2)²L` |
| total heat | 7.239141 W | DERIVED | 23 cells |
| Re | 6,773.5 | DERIVED | Table 4와 Ø19 mm |
| paper Re | 8,782 | SOURCE/DISCREPANCY | 형상 입력으로 사용하지 않음 |
| implied μ | 8.63849×10⁻⁴ Pa·s | DERIVED | 논문 Re 역산, sensitivity 전용 |
| nominal current | 4 A | DERIVED | 2 Ah × 2C |
| equivalent R | 19.6716 mΩ | DERIVED | baseline 열과 4 A의 등가값; 측정 저항 아님 |
| PPT Bernardi Ri | `0.0994-0.145S+0.246S²-0.126S³` Ω | SOURCE | Peneklioglu Figure 4.2, Group 7, 2C·293.15 K |
| PPT Bernardi ΔS | `-68.5+22.2S+449.1S²-568.3S³+138.6S⁴` J/(mol·K) | SOURCE | Peneklioglu Figure 4.6, 293.15 K |
| PPT Bernardi qcell | `I²Ri-ITΔS/F` | SOURCE+DERIVED | 논문 식 (3.1), I=4 A, F=96485.33212 C/mol |
| PPT Bernardi qvol | 78,699.333→125,668.881 W/m³ | DERIVED | 0–1,800 s, 1초 간격; 중간값은 비선형 |

실행 시에는 원본 YAML SHA-256, SI 변환 결과와 geometry SHA-256가
`resolved_config.json`과 `case_manifest.json`에 함께 저장된다.
