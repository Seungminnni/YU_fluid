# 구현 가정 및 차단 항목

이 문서는 23셀 CFD 디지털 트윈의 가정을 실행 코드 관점에서 고정한다.

| ID | 상태 | 현재 처리 | 해제 조건 |
|---|---|---|---|
| A-01 | 부분 구현 | 기준 발열은 `16,325 W/m³`; 시간표/Joule UDF 생성은 지원하나 SOC·T 의존식은 아직 검증 전 | 채택할 상관식·셀 파라미터·실험 프로파일 확정 및 검증 |
| A-04 | **형상 확정/기본안 채택** | 22점 외곽, 19→11×11.25 mm 노치, 포트 Ø19 확정; PPT 우선 end-cell 배치를 baseline으로 실행 허용 | reversed/corner profile과 CFD 비교 후 최종 승인 |
| A-05/A-06 | 구현됨 | 초기온도 293 K, 외벽 adiabatic/no-slip | 별도 실험 조건 제공 시 case profile 추가 |
| A-07 | 차단 아님 | solver Δt는 temporal study에서 선정 | OpenFOAM 실행 환경에서 pilot 완료 |
| A-13 | 구현됨 | Table 4 `Re=6,773.5`; 논문 8,782는 메타데이터 | 원 저자의 점도/온도 계산 근거 확보 |
| A-15 | 구현됨 | x pitch 19.0 mm 기본, 19.48557 mm 대안 기록 | CAD 치수 검토 승인 |

현재 A-04는 non-blocking warning이다. 표준 216,577-cell 및 정밀
1,316,854-cell 결과는 각자의 실제 OpenFOAM 필드와 검증 보고서를 보유한다.
어느 경우에도 solver 성공을 주장하기 위해 합성 CFD field를 만들지 않는다.

## UDF 상태

- `constant_qvol`: 논문 baseline과 직접 대응하는 상수 체적 발열원이다.
- `table_qvol`: 사용자가 제공한 `time_s,qvol_W_m3`를 선형 보간한다.
- `electrical_joule`: 사용자 `I(t)`와 `R(t)`로 `I²R/Vcell`을 계산하는 대체 모델이다.
- 향후 SOC·온도 의존 발열은 `time → SOC → Ri(SOC,T), ΔS(SOC,T) → q'''`의
  단일 소스 모델로 추가하고, 단위·SOC 범위·부호·체적 적분 열량을 gate로 검사한다.
- 논문의 원본 dynamic UDF, 실험 current profile, resistance profile은 공개 자료에 없어
  동일 코드 재현으로 표시하지 않는다.
- `3.7 V × 4 A`는 전기 입력전력이며 전량을 열로 간주하지 않는다.
- 동적 발열만 있고 밀도·점도가 상수이며 중력이 0이면 유동 방정식으로의 역결합이
  없으므로 frozen-flow가 수학적으로 유효하다. 열전도도·비열만 T에 의존하면
  에너지 결합을 갱신하되 운동량 재계산은 필수가 아니다. 밀도·점도의 T 의존성,
  부력 또는 다른 열-운동량 피드백을 켜면 full-transient PIMPLE을 사용한다.
