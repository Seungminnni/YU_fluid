# 영남대학교 배터리 냉각 디지털 트윈

23개 원통형 배터리의 액침냉각을 CAD 생성부터 OpenFOAM 계산, 검증, 3D 결과 표시까지 연결하는 로컬 CFD 웹앱이다. 웹 화면은 별도의 근사 모델이 아니라 OpenFOAM의 실제 메시, 솔버 로그, 온도장과 검증 JSON을 읽어 표시한다.

## 현재 상태 — 2026-09-22

- 웹앱 첫 화면, `결과 보기`, `계산하기`, CAD 확인, 운전조건 입력, 700k/800k급 메시 선택, 계산 확인 팝업, 단계별 진행 화면, 계산 중지·초기화와 결과 뷰어가 구현돼 있다.
- 검증된 STEP 3개를 업로드하는 경로는 `CAD 검사 → 메시 → S0 → S1 → S2 → 후처리` 전체 작업을 별도 실행 폴더에서 수행한다.
- 기준 CAD의 탱크 X/Y/Z 또는 외곽 체적과 입·출구 직경을 바꾸는 파라메트릭 경로도 전체 계산에 연결돼 있다. 확인창에서 형상·경계 조건과 저장공간을 승인하면 `STEP/STL 생성·재검증 → 메시 → S0 → S1 → S2 → 후처리`를 별도 실행 폴더에서 수행한다.
- 저장된 849,284셀 결과는 1초 간격, 1,800초, 시간가변 Bernardi 발열과 에너지 잔차 기반 적응형 반복으로 완료된 단일 메시 검토 결과다.
- 대용량 OpenFOAM 원본은 USB로 옮겼고, 웹 뷰어·검증 JSON·요약 로그는 프로젝트에 남아 있다. 복원 방법은 [`docs/CFD_ARCHIVE_USB_20260921.md`](docs/CFD_ARCHIVE_USB_20260921.md)에 기록했다.
- 사용자 확인 자료에 따라 입구는 우측 상단, 출구는 반대편 좌측 하단 코너로 수정해야 한다. [`configs/baseline_23cell.yaml`](configs/baseline_23cell.yaml)은 새 중심을 반영했지만, 현재 배포 뷰어와 849,284셀 결과는 **수정 전 CAD로 계산된 과거 결과**다. 최종 논문값으로 사용하려면 새 CAD로 메시와 S0/S1/S2를 다시 수행해야 한다.

## 사용자가 보는 UI 흐름

```text
시작 화면
  ├─ 결과 보기
  │    └─ 저장된 결과, 그래프, CAD 도면, 3D 결과, 3D 메시
  └─ 계산하기
       ├─ CAD 입력 방식 선택
       │    ├─ 검증된 기준 CAD
       │    ├─ STEP 3개 업로드
       │    └─ 기준 CAD 치수 변경
       ├─ 입구온도·속도·출구압력·냉각수·메시 등급 입력
       ├─ CAD 역할·포트·수치·저장공간 확인 팝업
       ├─ 계산 시작
       └─ CAD → 메시 → S0 → S1 → S2 → 후처리 상태 표시
```

`결과 보기`는 저장된 결과를 읽으므로 Python, Docker와 백엔드 없이도 사용할 수 있다. `계산하기`에서 실제 CAD 검사와 OpenFOAM 실행, 중지·초기화를 사용하려면 로컬 서버와 Docker가 필요하다.

## 전체 계산 파이프라인

```text
입력 확정
  → CAD 생성 또는 STEP 역할 지정
  → 형상 검사와 STL 변환
  → 실제 체적 메시 생성과 품질 gate
  → S0 냉간 정상 유동
  → S1 정상 CHT 검증
  → S2 고정 유동 과도 열해석
  → 잔차·질량·에너지·온도 검증
  → 그래프·3D 결과·3D 메시 게시
```

각 단계는 앞 단계의 검증 JSON이 `PASSED`여야 진행한다. 화면의 진행률은 임의 타이머가 아니라 실제 보고서와 솔버 로그에서 읽는다.

### 1. 입력과 CAD

지원하는 전체 계산용 CAD는 현재 23셀 `5-4-5-4-5` 배열 계열이다. 새 CAD는 다음 세 역할의 STEP을 각각 제공해야 한다.

1. 탱크·포트 외형
2. 냉각수 체적
3. 23개 배터리 고체

파일명만으로 역할을 추측하지 않는다. STEP 구문, 단위, 닫힌 고체, 냉각수 연결성, 배터리 개수, 셀·벽·포트 간섭, 입·출구 원형 면과 체적을 검사한다. STL은 참고·메시 표면 형식이며, STL 하나만으로 임의 형상의 영역과 경계조건을 자동 추측해 계산하지 않는다.

파라메트릭 기준 CAD에서는 다음 값을 바꿀 수 있다.

- 입구 직경
- 출구 직경
- 탱크 X/Y/Z 크기
- 탱크 외곽 목표 체적

체적을 입력하면 기준 X:Y:Z 비율을 유지하는 축척을 계산하고 CadQuery로 STEP/STL을 다시 만든다. 확인 전에는 해석식 간섭 검사와 경계조건·저장공간 검사만 수행한다. 사용자가 확인하면 생성된 STEP 3개를 다시 읽어 닫힌 체적·입출구 면·23개 셀·체적 균형을 검증하고, 그 형상에 대응하는 설정을 `job.json`에 고정한 뒤 메시와 S0/S1/S2를 시작한다.

### 2. 메시 생성

메시는 OpenFOAM의 다음 과정을 사용한다.

```text
blockMesh
  → surfaceFeatureExtract
  → snappyHexMesh
  → 배터리 주변 국부 세분화·prism layer
  → createPatch
  → splitMeshRegions
  → coolant / batterySolid 계면 생성
  → checkMesh와 자체 품질 검증
```

UI의 `700k급`, `800k급`은 각각 목표 셀 수 700,000과 800,000을 뜻한다. 새 CAD에서 정확히 그 셀 수가 보장되는 것은 아니며, 실제 결과가 목표의 ±10% 범위인지와 다음 항목을 생성 후 다시 검사한다.

- 전체 셀 수와 육면체 비율
- 음수 체적, 비직교도, 스큐와 연결 영역
- 냉각수/배터리 체적
- 입·출구 면적
- 23개 배터리 zone과 23쌍 CHT 계면
- 경계층 생성률

과거 기준 CAD에서 측정된 대표 결과는 732,766셀과 849,284셀이다. 이는 새 CAD의 셀 수를 미리 보증하는 값이 아니다.

### 3. S0 — 냉간 정상 유동

발열을 끄고 다음 유동·난류장을 먼저 구한다.

```text
U, p_rgh, k, omega
```

- 알고리즘: 정상 SIMPLE
- 1차 계산: 6,000 외부 반복
- 자동 합격 잔차: 마지막 200회에서 필수 유동 방정식의 OpenFOAM raw initial residual `≤ 1×10⁻⁴`
- 함께 확인: 질량 불균형 `< 0.1%`, 압력강하 안정, 유한한 필드, 배터리 면적의 `y+ < 1` 비율 `≥ 95%`

6,000회 뒤 수렴 관련 gate만 실패하면 기존 장을 버리지 않고 500회씩 이어 계산하며 절대 상한은 9,500회다. 메시 오류, 무한값, y+ 실패, 솔버 치명 오류는 반복 횟수만 늘려 통과시키지 않는다. `1×10⁻⁶`은 엄격 감사값이며 현재 자동 합격 기준과 혼동하지 않는다.

### 4. S1 — 정상 CHT 검증

CHT는 `Conjugate Heat Transfer`, 즉 배터리 고체 내부 열전도와 냉각수 대류·전도를 계면에서 함께 푸는 해석이다.

S1에서는 시간가변 UDF의 `t=0` 발열값을 대표 상수로 고정하고 다음을 모두 계산한다.

```text
U, p_rgh, k, omega, 유체 h, 고체 h
```

`h`는 열전달계수 하나가 아니라 OpenFOAM 에너지 방정식의 엔탈피 변수다.

- 알고리즘: 정상 SIMPLE
- 1차 계산: 6,000 외부 반복
- 자동 합격 잔차: 마지막 200회의 유동과 양쪽 `h` raw initial residual `≤ 1×10⁻⁴`
- 함께 확인: 질량 불균형, 압력강하 안정, 배터리 평균·최고온도 변동 `< 0.001 K`, 평균/최대 에너지 불균형 `< 2% / < 5%`
- 미수렴 시: S0와 같이 500회씩 연장, 최대 9,500회

S1의 반복 번호는 물리 시간이 아니다. 따라서 S1에서 시간가변 UDF를 흘려보내지 않는다.

### 5. S2 — 고정 유동 과도 열해석

현재 물성은 상수이고 중력은 0이므로 온도 변화가 속도·압력으로 되먹임되지 않는다. 그래서 S0에서 수렴한 다음 장만 복사해 고정한다.

```text
U, p, p_rgh, k, omega, nut, alphat, phi
```

S1의 가열된 온도장은 가져오지 않는다. 과도 시작 시 냉각수는 293 K, 배터리는 293.15 K로 다시 초기화한다. 이후 시간가변 Bernardi 발열 `q'''(t)`를 적용해 유체와 고체 에너지만 계산한다.

- 물리 시간간격: 1초
- 계산 구간: 0–1,800초
- 시간단계 수: 1,800개
- PIMPLE outer corrector: 3회
- 시간단계별 에너지 결합 반복 상한: 200회
- 유체·고체 모든 영역의 최신 `h` initial residual 목표: `< 1×10⁻⁵`

온도 의존 밀도·점도, 부력, 시간가변 유량 또는 압력 제어를 추가하면 유동장 고정 가정은 다시 검토해야 하며, 필요하면 매 시간단계 전체 유동을 다시 푸는 full-transient 경로가 필요하다.

## 잔차 기반 적응형 반복

“1초에 200번 계산”은 200회를 무조건 수행한다는 뜻이 아니다. 200은 상한이다.

```text
for t = 1 ... 1800 s:
    q'''(t) 갱신
    PIMPLE outer correction 수행

    for energyCoupling = 1 ... 200:
        냉각수 h 계산
        배터리 고체 h 계산
        모든 영역·모든 MPI rank의 최신 initial residual 확인

        if fluid_h < 1e-5 and solid_h < 1e-5:
            현재 초의 남은 결합 반복 종료
            break

    현재 초의 온도·probe·잔차·에너지 수지 저장
    다음 1초로 진행
```

여기서 “스킵”은 **현재 1초 안에서 남은 내부 반복을 생략**한다는 뜻이다. 물리 시간단계나 발열 갱신을 건너뛰지 않는다. 바깥 에너지 풀이에서 이미 두 영역이 기준 아래면 내부 결합 루프를 시작하지 않을 수도 있다. 반대로 200회까지 기준에 도달하지 않으면 그 사실을 `cap reached`로 기록하고 임의로 PASS 처리하지 않는다.

완료된 849,284셀 적응형 실행의 기록은 다음과 같다.

| 항목 | 측정값 |
| --- | ---: |
| 전체 시간단계 | 1,800 |
| 내부 반복 중 `1×10⁻⁵`를 만족해 조기 종료 | 1,701 |
| 바깥 풀이가 이미 기준 아래라 내부 반복 미진입 | 66 |
| 200회 상한 도달 | 33 |
| 상한에서도 양쪽 `h` 목표를 모두 달성하지 못한 초기 단계 | 32 |
| 마지막 60초 평균 에너지 불균형 | 0.3363% |

따라서 잔차 한 숫자만으로 전체 계산을 판정하지 않는다. S2 종료 뒤에는 온도 유한성, 시간 연속성, 질량수지, 마지막 구간 평균/최대 에너지 불균형과 결과 파일 완전성을 함께 검사한다.

## 후처리와 결과 게시

검증을 통과한 계산만 다음 결과로 변환한다.

- 23개 셀별 평균·최고·최저온도
- 주요 셀 온도 비교
- 입구·출구 온도, 속도, 압력
- 시간별 동적 발열
- S0/S1 다중 방정식 수렴 이력
- S2 시간단계별 에너지 결합 잔차
- 에너지 수지
- XY/XZ 온도 단면
- 3D 온도장과 고정 유동장 유선
- 실제 `polyMesh` 경계·절개·단면·전체 체적 격자

브라우저용 `uint16` 데이터는 원본 OpenFOAM 값을 포장한 것이며, 검증되지 않은 시간이나 공간을 보간해 만들지 않는다. 결과 화면은 한국어/영어 전환과 PNG 저장을 지원한다.

## 로그와 작업 파일

새 계산은 기존 결과를 덮어쓰지 않고 다음 구조의 고유 작업 ID에 저장한다.

```text
build/imported_runs/runs/<job-id>/
  job.json                 # 확인된 CAD·운전조건·메시 목표
  status.json              # 현재 단계와 성공/실패/중지 상태
  worker.log               # Docker·메시·솔버 실행 로그
  cancel.request           # 계산 중지 요청 기록
  cad/                     # 검사·변환된 CAD/STL
  case/                    # 메시, S0/S1/S2, 검증 보고서
  viewer/                  # 완료 후 생성되는 결과 뷰어
```

대표 검증 파일은 다음과 같다.

```text
case/reports/production_mesh/production_mesh_gate.json
case/runs/steady_cold_flow/reports/solver/cold_flow_validation.json
case/runs/steady_cht/reports/solver/steady_cht_validation.json
case/runs/frozen_flow_actual_1s/reports/solver/energy_coupling_audit.json
case/runs/frozen_flow_actual_1s/reports/solver/energy_balance.json
case/runs/frozen_flow_actual_1s/reports/solver/frozen_flow_validation.json
```

계산 중지는 요청 파일을 만든 뒤 실행 중 Docker 컨테이너를 종료한다. 초기화는 작업 기록을 지우기 전에 실행 중 여부를 확인해야 하며, 완료 결과와 원본 CFD를 자동 삭제하지 않는다.

## 실행 방법

### 저장된 결과만 보기

배포 ZIP을 완전히 압축 해제하고 루트의 `index.html`을 연다. `viewer/`와 `data/`를 따로 떼어내면 3D 전체 격자나 그래프를 읽지 못한다.

이 파일 화면은 발표용 데모 흐름을 포함한다. 기준 CAD는 계산 조건 확인 뒤 저장된 결과로 연결되고, 치수 변경·CAD 첨부는 확인창에서 동의하면 CAD → 메시 → S0 → S1 → S2 → 결과 뷰어 단계가 순서대로 진행되어 저장된 3D 결과로 연결된다. 이 진행 표시는 UI 시연이며 실제 OpenFOAM 재계산·형상 검증·파일 생성은 수행하지 않는다. 실제 계산은 아래의 로컬 서버 실행 방식으로만 시작한다.

### 새 계산 기능까지 사용

필요 조건:

- Python 3.12
- Docker Desktop과 Linux 컨테이너
- 첫 설치·이미지 빌드를 위한 인터넷
- 새 800k급 전체 계산 전 최소 55 GiB 이상의 실제 여유공간

배포본에서는 macOS의 `run_mac.command` 또는 Windows의 `run_windows.cmd`를 실행하고 브라우저에서 `http://127.0.0.1:8765/`를 연다. `127.0.0.1`은 실행한 컴퓨터 자신만 가리킨다.

개발 환경에서는 다음으로 상태를 확인한다.

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m cfd_twin doctor
```

## 입력이 바뀌면 어디부터 다시 계산하는가

| 변경 | 다시 수행할 단계 |
| --- | --- |
| 포트 위치·직경, 탱크 크기, 셀 위치, CAD | CAD → 메시 → S0 → S1 → S2 → 후처리 |
| 메시 등급·세분화 | 메시 → S0 → S1 → S2 → 후처리 |
| 냉각수 종류·물성, 입구속도, 출구압력 | S0 → S1 → S2 → 후처리 |
| 입구·초기온도, 배터리 열물성 | S1 → S2 → 후처리. 유동 피드백이 있으면 S0도 재검토 |
| UDF 계수·전류·SOC·발열 모드 | S1 대표 발열 검증 → S2 → 후처리 |
| 그래프 색·언어·카메라·표시 위치 | CFD 재계산 없음 |

## 주요 코드

| 역할 | 파일 |
| --- | --- |
| 기준·파라메트릭 CAD | [`src/cfd_twin/cad.py`](src/cfd_twin/cad.py), [`src/cfd_twin/parametric_cad.py`](src/cfd_twin/parametric_cad.py) |
| 새 STEP 검사 | [`src/cfd_twin/cad_import.py`](src/cfd_twin/cad_import.py) |
| 생산 메시 | [`src/cfd_twin/production_mesh.py`](src/cfd_twin/production_mesh.py) |
| S0/S1/S2 케이스 생성 | [`src/cfd_twin/openfoam_case.py`](src/cfd_twin/openfoam_case.py) |
| S0/S1 연장 판정 | [`src/cfd_twin/steady_convergence.py`](src/cfd_twin/steady_convergence.py) |
| 최신 `h` 잔차 조기 종료 솔버 | [`src/cfd_twin/patch_adaptive_cht_solver.py`](src/cfd_twin/patch_adaptive_cht_solver.py) |
| 통합 작업 실행 | [`src/cfd_twin/workflow_run.py`](src/cfd_twin/workflow_run.py) |
| 로컬 웹 서버/API | [`src/cfd_twin/workflow_app.py`](src/cfd_twin/workflow_app.py) |
| 결과·3D 뷰어 생성 | [`src/cfd_twin/viewer.py`](src/cfd_twin/viewer.py), [`src/cfd_twin/mesh_viewer.py`](src/cfd_twin/mesh_viewer.py) |

## 문서 기준

- 이 `README.md`가 현재 UI와 전체 계산 흐름의 기준 문서다.
- 세부 실행 계약은 [`docs/INTEGRATED_CFD_FLOW.md`](docs/INTEGRATED_CFD_FLOW.md)에 둔다.
- 대용량 원본 위치와 복원은 [`docs/CFD_ARCHIVE_USB_20260921.md`](docs/CFD_ARCHIVE_USB_20260921.md)를 따른다.
- `distribution/README_시작하기.txt`와 배포 폴더 내부의 같은 파일은 다른 컴퓨터에서 독립 실행할 때 필요하므로 중복 문서가 아니라 패키지 구성요소다.
- 생산 메시 폴더 안의 `README.md`는 생성된 OpenFOAM 번들의 실행 계약이므로 유지한다.

현재 저장된 결과는 단일 메시 내부 검토용이며, 수정된 포트 CAD 재계산과 격자 독립성·Fluent 교차검증이 끝나기 전에는 최종 논문 승인값으로 취급하지 않는다.
