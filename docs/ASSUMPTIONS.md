# 구현 가정 및 차단 항목

이 문서는 `MDPI_CFD_IMPLEMENTATION_FINAL_DESIGN.md`의 가정을 실행 코드 관점에서 고정한다.

| ID | 상태 | 현재 처리 | 해제 조건 |
|---|---|---|---|
| A-01 | 구현됨 | 기준 발열은 `16,325 W/m³`; 동적 모드는 사용자 CSV 확장 | 원 저자 UDF와 측정 `I(t)`, `R(t)` 확보 |
| A-04 | **형상 확정/기본안 채택** | 22점 외곽, 19→11×11.25 mm 노치, 포트 Ø19 확정; PPT 우선 end-cell 배치를 baseline으로 실행 허용 | reversed/corner profile과 CFD 비교 후 최종 승인 |
| A-05/A-06 | 구현됨 | 초기온도 293 K, 외벽 adiabatic/no-slip | 별도 실험 조건 제공 시 case profile 추가 |
| A-07 | 차단 아님 | solver Δt는 temporal study에서 선정 | OpenFOAM 실행 환경에서 pilot 완료 |
| A-13 | 구현됨 | Table 4 `Re=6,773.5`; 논문 8,782는 메타데이터 | 원 저자의 점도/온도 계산 근거 확보 |
| A-15 | 구현됨 | x pitch 19.0 mm 기본, 19.48557 mm 대안 기록 | CAD overlay 승인 |

현재 A-04는 non-blocking warning이다. case manifest의
`status=INPUT_BUNDLE_READY_NOT_RUNNABLE`, `runnable=false`는 CadQuery를 실행하지
않은 기본 번들이거나 OpenFOAM/`checkMesh` gate가 남았기 때문이다. 실제 CAD
검사를 통과한 번들은 OpenFOAM과 mesh gate만 남긴다. 어느 경우에도 solver
성공을 주장하거나 합성 CFD field를 만들지 않는다.

## UDF 상태

- `constant_qvol`: 논문 baseline과 직접 대응하는 상수 체적 발열원이다.
- `table_qvol`: 사용자가 제공한 `time_s,qvol_W_m3`를 선형 보간한다.
- `electrical_joule`: 사용자 `I(t)`와 `R(t)`로 `I²R/Vcell`을 계산하는 대체 모델이다.
- 논문의 원본 dynamic UDF, 실험 current profile, resistance profile은 공개 자료에 없어
  동일 코드 재현으로 표시하지 않는다.
- `3.7 V × 4 A`는 전기 입력전력이며 전량을 열로 간주하지 않는다.
