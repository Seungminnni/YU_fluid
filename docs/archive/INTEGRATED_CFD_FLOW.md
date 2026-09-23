# 통합 CFD 실행 계약

이 문서는 [`README.md`](../README.md)의 사용자 흐름을 실제 파일·검증 gate·재계산 규칙으로 구체화한다. 화면의 단계나 결과 상태는 이 계약과 생성된 JSON 보고서를 기준으로 판정한다.

## 1. 구현 범위

| 기능 | 현재 상태 |
| --- | --- |
| 저장된 결과·그래프·CAD 도면·3D 결과·3D 메시 보기 | 구현 |
| 한국어/영어 전환, 그래프·3D PNG 저장 | 구현 |
| 기준 STEP 3개 해시 인식 | 구현 |
| 새 STEP 3개 역할 지정과 형상 검사 | 구현 |
| 새 STEP의 메시 → S0 → S1 → S2 → 게시 | 구현. 실제 실행 환경과 저장공간 필요 |
| 기준 CAD의 탱크 크기·체적·입출구 Ø 변경 | 구현 |
| 확인된 파라메트릭 CAD의 전체 CFD 실행 | 구현. STEP 재검증·저장공간 검사 후 실행 |
| 임의 STL 하나의 영역·입출구 자동 추론 | 지원하지 않음 |
| 임의 셀 배열과 임의 포트 형상의 완전 자동 CFD | 지원하지 않음 |

## 2. 입력 확정

전체 계산 작업은 다음 정보를 `job.json`에 고정한 뒤 시작한다.

- 탱크·포트 외형 STEP
- 냉각수 체적 STEP
- 23개 배터리 고체 STEP
- 입구온도와 초기 유체·고체 온도
- 입구속도와 출구 절대압
- 냉각수와 배터리 물성
- 시간가변 발열 모델과 계수
- 700k 또는 800k 목표 메시
- 포트 역할과 방향
- 파라메트릭 실행이면 탱크·셀·포트 위치를 재현하는 형상 override와 생성 ID

확인 팝업 이후 파일 또는 수치가 바뀌면 확인 해시가 무효가 되며 작업을 만들지 않는다. 업로드한 이름만 보고 CAD 역할이나 포트 위치를 추측하지 않는다.

## 3. CAD와 메시

### 3.1 CAD 경로

`cad_import.py`는 STEP을 읽어 닫힌 고체, 단위, 체적, 23개 셀, 냉각수 연결성, 원형 입·출구와 간섭을 검사한다. 통과한 형상만 STL과 OpenFOAM 입력으로 변환한다.

`parametric_cad.py`는 검증된 23셀 템플릿에 대해 탱크 X/Y/Z 또는 목표 외곽 체적과 입구·출구 직경을 바꾼다. 확인 전에는 해석식 사전검사만 수행한다. 사용자가 형상과 경계 조건을 확인하면 `workflow_app.py`가 STEP/STL을 생성하고, 그 STEP 3개를 `cad_import.py`로 다시 검사해 해시와 형상 override를 고정한 뒤 일반 `workflow_run.py` 작업자에 넘긴다. 따라서 파라메트릭 경로도 메시 → S0 → S1 → S2 → 게시를 동일한 gate로 수행한다.

### 3.2 메시 경로

```text
blockMesh
→ surfaceFeatureExtract
→ snappyHexMesh
→ 국부 세분화와 prism layer
→ createPatch
→ splitMeshRegions
→ coolant/batterySolid 영역 및 23쌍 계면 검사
→ checkMesh와 생산 메시 gate
```

실행 파일은 [`production_mesh.py`](../src/cfd_twin/production_mesh.py)가 생성한다. 목표 셀 수는 700,000 또는 800,000이며 실제 셀 수는 목표의 ±10% 안에 들어와야 한다. 셀 수만 맞아도 통과하는 것은 아니다. 체적·면적·영역·계면·품질·경계층 보고서가 모두 필요하다.

대표 보고서:

```text
case/reports/production_mesh/mesh_target_composition.json
case/reports/production_mesh/checkMesh_all_regions.json
case/reports/production_mesh/interfaces_post_split.json
case/reports/production_mesh/layer_coverage.json
case/reports/production_mesh/production_mesh_gate.json
```

## 4. S0·S1 정상 계산

S0와 S1은 6,000회로 시작한다. `steady_convergence.py`는 완료 보고서만 읽어 다음 행동 중 하나를 결정한다.

1. 모든 gate 통과: 다음 단계
2. 수렴 관련 gate만 실패: 같은 장에서 500회 연장
3. 비수렴성 실패 또는 9,500회 상한: 실패 종료

메시 실패, y+ 실패, 필드 무한값, 알 수 없는 검사, 솔버 오류는 자동 연장의 대상이 아니다.

### 4.1 S0 냉간 유동

- 열원: 0
- 계산 변수: `U`, `p_rgh`, `k`, `omega`
- 정상 알고리즘: SIMPLE
- 최근 200회 필수 유동 raw initial residual: `≤ 1×10⁻⁴`
- 최대 질량 불균형: `< 0.1%`
- 압력강하 마지막 구간 변동: `< 0.5%`
- 배터리 벽면 `y+ < 1` 면적비: `≥ 95%`

보고서:

```text
case/runs/steady_cold_flow/reports/solver/cold_flow_validation.json
case/runs/steady_cold_flow/reports/solver/convergence_decision.json
```

### 4.2 S1 정상 CHT

- 열원: 동적 발열식의 `t=0` 값을 상수로 고정
- 계산 변수: `U`, `p_rgh`, `k`, `omega`, 냉각수 `h`, 배터리 `h`
- 정상 알고리즘: SIMPLE
- 최근 200회 유동·에너지 raw initial residual: `≤ 1×10⁻⁴`
- 배터리 평균·최고온도 변동: `< 0.001 K`
- 평균/최대 에너지 불균형: `< 2% / < 5%`
- 질량수지와 압력강하 안정도 함께 검사

S1 반복 번호는 시간이 아니며, S1에는 시간가변 발열 이력을 적용하지 않는다.

보고서:

```text
case/runs/steady_cht/reports/solver/steady_cht_validation.json
case/runs/steady_cht/reports/solver/convergence_decision.json
```

## 5. S2 적응형 과도 열해석

### 5.1 초기장

S0에서 다음 유동·난류장만 가져온다.

```text
U, p, p_rgh, k, omega, nut, alphat, phi
```

S1의 가열된 온도는 복사하지 않는다. 냉각수는 293 K, 배터리는 293.15 K로 다시 설정한다. 현재 모델은 상수 물성과 `g=(0,0,0)`을 사용하므로 S2에서 `frozenFlow yes`로 유동장을 고정한다.

### 5.2 시간과 발열

- `deltaT = 1 s`
- `0 → 1800 s`
- 매 시간단계마다 Bernardi 기반 `q'''(t)` 갱신
- 각 시간단계의 유체·고체 온도와 관측값을 저장

### 5.3 잔차 조기 종료

OpenFOAM v2512 기본 루프는 시간단계의 첫 잔차를 기준으로 판단할 수 있다. 이 프로젝트는 원본 솔버를 보존하고 `chtMultiRegionFoamAdaptive`를 별도 빌드한다. 수정 코드는 [`patch_adaptive_cht_solver.py`](../src/cfd_twin/patch_adaptive_cht_solver.py)에 있다.

각 1초에서:

1. 최대 200회의 에너지 결합 반복을 허용한다.
2. 매 반복 후 냉각수·고체 모든 영역과 MPI rank에서 가장 최근 `h` 선형해석 initial residual을 읽는다.
3. 양쪽 최대값이 모두 `1×10⁻⁵` 미만이면 남은 내부 반복을 끝낸다.
4. 바깥 에너지 풀이가 이미 기준 미만이면 내부 반복을 시작하지 않을 수 있다.
5. 200회에서도 미달하면 상한 도달로 기록한다.
6. 온도·발열·관측값을 저장하고 정확히 다음 1초로 이동한다.

이 로직은 여러 물리 시간단계를 건너뛰지 않는다. 계산량을 줄이는 대상은 현재 시간단계 안에서 이미 필요 없는 **남은 에너지 반복**뿐이다.

### 5.4 S2 검증

잔차 외에도 다음을 확인한다.

- 1–1,800초 시간 누락과 중복 없음
- 모든 필수 온도장이 유한함
- 유체·고체 온도와 계면 열전달의 연속성
- 최종 구간 질량 불균형 `< 0.1%`
- 최종 구간 평균/최대 에너지 불균형 `< 2% / < 5%`
- 조기 종료, 미진입과 200회 상한 도달을 원본 그대로 기록

대표 보고서:

```text
case/runs/frozen_flow_actual_1s/reports/solver/energy_coupling_audit.json
case/runs/frozen_flow_actual_1s/reports/solver/energy_balance.json
case/runs/frozen_flow_actual_1s/reports/solver/frozen_flow_validation.json
```

## 6. 완료된 적응형 실행의 의미

저장된 849,284셀 실행은 1,800개 시간단계를 완료했다.

- 잔차 만족 후 내부 반복 조기 종료: 1,701단계
- 바깥 풀이가 이미 만족해 내부 루프 미진입: 66단계
- 200회 상한 도달: 33단계
- 상한에서도 양쪽 `h`가 모두 `1×10⁻⁵` 미만이 아니었던 초기 단계: 32단계
- 마지막 60초 평균 에너지 불균형: 0.3363%

이는 적응형 알고리즘의 실행 증거다. 그러나 이 결과는 수정 전 포트 CAD로 계산됐고 단일 메시이므로 최종 논문 승인값이 아니다.

대용량 원본은 `/Volumes/승민/YU_fluid_CFD_원본_20260921`에 TAR로 보관하며, 프로젝트에는 배포 뷰어와 검증용 축약 데이터가 남아 있다.

## 7. 후처리와 게시

`workflow_run.py`는 메시·S0·S1·S2의 정확한 PASS 상태를 확인한 뒤 `workflow_publish.py`를 호출한다. 게시 단계는 계산값을 새로 만들지 않고 원본 필드와 probe를 브라우저 데이터로 변환한다.

전체 흐름은 다음 상태 문자열로 추적한다.

```text
cad → mesh → steady → transient → publish → complete
```

오류 시 `failed`, 사용자 중지 시 `cancelled`로 남긴다. `worker.log`는 구체적인 Docker·메시·솔버 오류를 보존하고, `status.json`은 UI가 표시할 현재 상태를 제공한다.

## 8. 입력 변경에 따른 무효화

| 변경 | 다시 실행 |
| --- | --- |
| CAD·포트·셀 위치·메시 목표 | CAD → 메시 → S0 → S1 → S2 |
| 냉각수 물성·입구속도·출구압력 | S0 → S1 → S2 |
| 초기온도·입구온도·배터리 열물성 | S1 → S2. 유동 피드백이 있으면 S0도 포함 |
| 발열식·전류·SOC | S1 대표 발열 → S2 |
| 시각화 스타일 | 후처리만 |

온도 의존 물성, 부력, 시간가변 경계 유동이 도입되면 고정 유동 S2를 자동 재사용하지 않는다.

## 9. 문서와 보존 정책

- 현재 전체 설명: [`README.md`](../README.md)
- USB 원본 복원: [`CFD_ARCHIVE_USB_20260921.md`](CFD_ARCHIVE_USB_20260921.md)
- 가정·수동 판단: [`ENGINEERING_JUDGEMENTS.md`](ENGINEERING_JUDGEMENTS.md)
- 배포 실행 안내: `distribution/README_시작하기.txt`

배포본·뷰어·생산 메시 폴더의 로컬 README는 해당 폴더만 전달됐을 때 실행 방법을 제공하므로 삭제하지 않는다. 프로젝트의 현재 전체 흐름을 설명하는 문서는 최상위 README와 이 문서 두 개로 제한한다.
