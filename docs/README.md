# YU fluid — 현행 계산·뷰어 안내

기준일: 2026-09-23. 이 문서가 `docs/`의 현행 안내서다. 세부 판정은 문장의 기억이 아니라 각 계산 폴더의 생성된 JSON 보고서를 따른다.

## 현재 대상

`build/result/`에 보존한 Ø19·21·23·25 mm 네 결과만 현행 비교 대상으로 삼는다. 잘못된 CAD를 사용한 과거 약 80만 셀 계산은 여기의 결과·품질·실행 기준에 포함하지 않는다. 그 원본을 이 문서 정리로 삭제한 것은 아니다.

| 포트 직경 | 실제 메시 셀 수 | 보존 위치 (`build/result/` 아래) | 출처 |
| --- | ---: | --- | --- |
| Ø19 mm | 611,313 | `recalculation_phi19_600k_10um_20260922/case` | 기존 계산장 보존, 뷰어에서는 Y축 강체 반사 표시 |
| Ø21 mm | 614,037 | `recalculation_c05_c19_phi21_600k_20260923` | C05 상부 입구·C19 하부 출구로 신규 계산 |
| Ø23 mm | 621,948 | `recalculation_c05_c19_phi23_600k_20260922` | 동일 배치로 신규 계산 |
| Ø25 mm | 624,788 | `recalculation_c05_c19_phi25_600k_20260922` | 동일 배치로 신규 계산 |

Ø19의 화면 좌표 변환은 솔버 재계산이 아니다. 따라서 Ø21·23·25의 새 CAD 계산과 완전히 같은 방식으로 생성된 독립 해석값이라고 주장하지 않는다. 네 케이스 모두 메시·S0·S1·S2·과도 에너지수지 검증 JSON은 PASS이지만, `result_manifest.json`의 `publication_authorized`는 `false`, `quality_status`는 `SINGLE_MESH_REVIEW_REQUIRED`다. 발표·비교용 결과와 논문 최종 승인값을 구분한다.

전체 경로와 Ø19 CAD/통합 뷰어의 보존 위치는 [결과 폴더 안내](../build/result/README.md)에 적었다.

## 화면과 실제 계산의 구분

- [시작 화면](../viewer/index.html)에서 `Start`는 입력 화면, `Graphics`는 저장된 결과로 이동한다.
- [결과 뷰어](../viewer/results.html)는 네 케이스의 그래프·온도장·유선을, [CAD 도면](../viewer/cad/index.html)은 선택한 케이스의 상면도·측면도를, [3D 메시](../viewer/mesh.html)는 해당 케이스의 메시를 보여준다.
- 압축을 풀어 `index.html`을 직접 여는 배포본은 시연용이다. 버튼·진행 단계가 보이더라도 새 OpenFOAM 계산을 실행한 증거가 아니다.
- 새 STEP 검사, 메시 생성, S0/S1/S2 실행은 Python·Docker가 준비된 로컬 서버에서만 수행한다. 새 작업은 기존 네 결과를 덮어쓰지 않는 별도 폴더에 생성해야 한다.

## 계산 입력과 CAD

공통 입력은 [기준 설정](../configs/baseline_23cell.yaml)과 [Bernardi 열원 설정](../configs/ppt_bernardi_udf.yaml)이다. 기준 설정의 포트 좌표는 **현재 C05/C19 배치 자체가 아니므로**, 최신 Ø21·23·25 계산을 재현할 때는 각각의 포트 설정을 반드시 추가한다.

| 신규 계산 | 추가 설정 |
| --- | --- |
| Ø21 | [`recalculation_600k_c05_inlet_c19_outlet_phi21_20260923.yaml`](../configs/recalculation_600k_c05_inlet_c19_outlet_phi21_20260923.yaml) |
| Ø23 | [`recalculation_600k_c05_inlet_c19_outlet_phi23_20260922.yaml`](../configs/recalculation_600k_c05_inlet_c19_outlet_phi23_20260922.yaml) |
| Ø25 | [`recalculation_600k_c05_inlet_c19_outlet_phi25_20260922.yaml`](../configs/recalculation_600k_c05_inlet_c19_outlet_phi25_20260922.yaml) |

세 신규 CAD는 23개 원통 셀의 5–4–5–4–5 배열, 114 × 121 × 82 mm 외곽을 공유한다. 입구는 C05 위의 탱크 상면, 출구는 C19 아래의 하면이며 중심 XY는 각각 `(13.6, 105.2)`와 `(89.6, 15.2)` mm다. 포트 지름만 해당 케이스의 Ø21/23/25 mm로 바꾼다. 최신 케이스의 CAD 해시와 영역 검사는 각 폴더의 `cad/cad_manifest.json`에서 확인한다.

공통 운전 조건은 탈이온수, 입구 속도 `0.4 m/s`, 입구·유체 초기 온도 `293 K`, 배터리 초기 온도 `293.15 K`, 출구 절대압 `101325 Pa`, 2C Bernardi 기반 시간가변 발열이다. 물성·계수의 정확한 값과 단위는 위 두 YAML 및 케이스의 생성 설정을 기준으로 한다. 제공된 논문 최종 Fluent UDF 원본과 같은 코드라고 주장하지 않는다.

네 계산에 실제 사용된 발열식과 OpenFOAM 적용 방식은 [UDF 적용 기록](../document/UDF_적용_기록.md)에 모았다.

## CAD부터 결과까지

```text
CAD 생성/STEP 역할 확정 → 형상·간섭 검사 → OpenFOAM 메시·품질 검사
→ S0 냉간 정상 유동 → S1 정상 CHT → S2 과도 열해석
→ 잔차·질량·에너지·온도 검증 → 그래프·CAD·3D 뷰어 게시
```

S2는 0–1800초를 1초 물리 시간간격으로 계산한다. 한 시간단계 안의 에너지 결합 반복은 **최대 200회**이며, 냉각수·고체 `h` 잔차가 목표 이하로 수렴하면 남은 내부 반복을 조기 종료한다. 물리 시간단계를 건너뛰는 방식은 아니다. 상수 물성·중력 0의 고정 유동 모델을 사용했으므로, 온도에 따라 유량·밀도·점도 또는 부력이 변하도록 조건을 바꾸면 유동장 재사용 가능성을 다시 판단해야 한다.

대표 검증 파일은 각 `case/` 아래에 있다.

```text
reports/production_mesh/production_mesh_gate.json
reports/production_mesh/mesh_target_composition.json
runs/steady_cold_flow/reports/solver/cold_flow_validation.json
runs/steady_cht/reports/solver/steady_cht_validation.json
runs/frozen_flow_actual_1s/reports/solver/energy_coupling_audit.json
runs/frozen_flow_actual_1s/reports/solver/energy_balance.json
runs/frozen_flow_actual_1s/reports/solver/frozen_flow_validation.json
results/result_manifest.json
```

형상·포트·메시를 바꾸면 CAD부터 S0/S1/S2까지 다시 계산한다. 냉각수 물성·속도·압력을 바꾸면 S0부터, 초기온도·열물성을 바꾸면 최소 S1부터, 발열식을 바꾸면 S1의 대표 발열과 S2를 다시 검증한다. 색상·범례·언어만 바꾸면 CFD 재계산은 필요 없다.

## 파일 역할과 과거 기록

- `configs/`, `src/`, `tests/`, `schemas/`는 입력·구현·회귀검사 원본이다.
- `build/result/`은 네 결과의 계산 케이스와 통합 뷰어를 보존한다. 최상위 `viewer/`는 현재 화면 자산이다.
- `docs/archive/`는 이전 문서 11개와 이전 최상위 README의 원문 보관소다. 삭제된 폴더, 이전 포트 배치, 80만 셀 결과를 현행으로 설명하는 문장이 있으므로 **현재 상태의 근거로 사용하지 않는다**.
- 새 계산 전에는 각 케이스의 CAD·메시·검증 JSON과 실제 저장공간을 확인한다. 과거 실행 기록만으로 새 형상의 통과를 보증하지 않는다.
