# 통합 CFD 실행 흐름과 재계산 규칙

이 문서는 23셀·849,284격자 **기준 계산 기록**과 새 STEP 형상을 받는 통합 실행기의 계약을 구분한다. 새 CAD의 STEP 검증·입력 확인·별도 CFD 작업 생성은 구현됐지만, 임의 탱크 형상 전체를 무조건 자동 해석한다고 뜻하지 않는다. 화면의 진행률이나 결과 그래프는 솔버의 실제 로그와 검증 보고서에서만 만든다.

## 1. 기준 입력과 증거

- 기준 형상·물성·운전조건의 단일 입력은 [`configs/baseline_23cell.yaml`](../configs/baseline_23cell.yaml)이다. 탱크는 114 × 121 × 82 mm, 원통 셀은 Ø19 × 68 mm의 23개(5–4–5–4–5 배열), 입·출구는 Ø19 mm다. 포트 중심의 A-04 가정은 별도로 추적한다.
- 이번 열원과 초기온도 선택은 [`configs/ppt_bernardi_udf.yaml`](../configs/ppt_bernardi_udf.yaml)이다. 냉각수 입구·초기온도는 293 K, 배터리 초기온도는 293.15 K이며, 23개 고체 영역에 **시간가변 Peneklioglu/Bernardi 발열만** 적용한다. 상수 16,325 W/m³ 발열은 이 과도 계산에 섞지 않는다.
- UDF PPT는 발열식의 근거다. **Fluent의 실제 Time Step Size나 내부 반복 설정을 확인할 수 있는 `.cas/.dat`, journal 또는 설정 화면은 현재 저장소에 없다.** 이전 대화의 “Fluent 0.01 s” 문장은 실행 설정의 독립 증거가 아니다. 두 솔버를 비교할 때는 이 파일들과 보고값 정의를 별도로 확보한다.
- 이전에 완료한 849,284셀 결과는 물리 시간간격 **1 s**, 시간단계별 에너지 결합 반복 상한 **100회**, 수렴 설정 **10⁻⁶**으로 계산됐다. **새로 생성하는** 과도 케이스는 1 s, 에너지 결합 최대 **200회**, 유체·고체 `h` 초기 잔차 **10⁻⁵** 조기 종료 설정이다. 서로 다른 설정의 결과를 한 그래프에 동일 실행으로 섞지 않는다. `0.01 s`는 검증된 생산 기본값이 아니다.

## 2. 실행 의존관계

```text
입력 YAML + 검증된 CAD/포트 지정
  → 형상 무결성 검사
  → blockMesh · 표면 특징 추출 · snappyHexMesh · prism layer · 영역 분리
  → 메시 품질/셀 수/체적/23쌍 CHT 계면 gate
  → S0 냉간 정상 유동: U, p, k, ω 수렴 및 y+ 검사
  → S1 정상 CHT: 유동 + 유체 h + 고체 h, t=0 대표 상수열원
  → S2 동적 발열 과도 CHT: S0 유동장 고정, T_f=293 K/T_s=293.15 K 재초기화
  → 원본 필드·probe·에너지 수지·잔차 검증
  → 검증 통과 데이터만 3D 뷰어·그래프·배포 ZIP으로 포장
```

다음 단계는 앞 단계의 `*_VALIDATION_PASSED` JSON 상태 없이는 시작하지 않는다. 반복을 더 수행했다는 사실만으로 물리 gate를 통과한 것으로 간주하지 않는다.

## 3. 단계별 계산과 합격 기준

| 단계 | 푸는 양과 열원 | 종료·검증 | 대표 증거 |
| --- | --- | --- | --- |
| 메시 | 실제 CAD 경계에 맞춘 유체·고체 체적격자 | 70만급은 700,000 ±10%·육면체 ≥89%, 80만급은 800,000 ±10%·육면체 ≥90%. 공통으로 `checkMesh`, 셀 영역·계면·층·체적 gate | `reports/production_mesh/production_mesh_gate.json`, `mesh_target_composition.json` |
| S0 냉간 유동 | 발열 0, 정상 SIMPLE, `U`, `p_rgh`, `k`, `ω` | 우선 6,000회. 최근 200회의 필수 유동 방정식 raw initial residual ≤1×10⁻⁴, 질량 불균형 <0.1%, 압력강하 안정, 배터리 면적 중 `y+<1` ≥95% 등 | `runs/steady_cold_flow/reports/solver/cold_flow_validation.json` |
| S1 정상 CHT | `t=0`의 발열값을 **상수**로 고정. 정상 SIMPLE에서 유동과 유체·고체 `h`를 함께 풂 | 우선 6,000회. 최근 200회의 유동·양쪽 `h` raw initial residual ≤1×10⁻⁴, 온도 변동폭 <0.001 K, 평균/최대 에너지 불균형 <2%/<5% 등 | `runs/steady_cht/reports/solver/steady_cht_validation.json` |
| S2 과도 CHT | S0의 `U`, `p`, `p_rgh`, `k`, `ω`, `nut`, `alphat`, `phi`를 가져와 `frozenFlow yes`. 매 시간단계 동적 `q'''(t)`로 유체·고체 에너지를 결합 | 새 실행은 1 s × 1,800단계, 내부 `energyCoupling` 최대 200회·전 영역 `h` 10⁻⁵ 조기 종료. 200회에서도 미수렴이면 상한 종료로 기록하며, 온도·완전 에너지 수지도 별도 검증 | `runs/frozen_flow_actual_1s/reports/solver/` 및 `postProcessing/` |

S0·S1의 최초 6,000회에서 **수렴 관련 gate만** 실패하면 [`steady_convergence.py`](../src/cfd_twin/steady_convergence.py)의 결정론적 판정으로 마지막 장을 이어 500회씩, 최대 9,500회까지 계산한다. 메시·y+·필드 무한값·솔버 오류 같은 비수렴성 실패를 반복 연장으로 덮지 않는다. `1×10⁻⁶`은 별도 엄격 감사값이지 현재의 자동 합격 기준 `1×10⁻⁴`와 같지 않다.

새로 생성하는 S0·S1 케이스는 정상해석 장을 100회마다 기록하되 `purgeWrite 2`와 OpenFOAM 압축을 사용해 각 MPI 분할 영역에 최근 재시작 장 두 개만 남긴다. 전체 반복 잔차와 물리량 이력은 별도 솔버 로그·function-object 파일에 유지된다. 이 설정은 기존 완료된 기준 케이스의 파일을 소급 변경하지 않으며, 과도 S2의 1초 원본 장 저장 정책도 바꾸지 않는다.

S1의 `Time`은 물리적 방전시간이 아니라 정상 솔버의 반복 번호다. 그래서 동적 발열 시간표를 S1에 넣지 않는다. S2에는 S1에서 데워진 온도를 복사하지 않는다. 배터리와 유체의 온도는 각각 293.15 K, 293 K로 다시 설정하고, 검증된 유동·난류장만 복사한다.

현재 유체 물성은 상수이고 중력은 0이며 발열은 운동량에 직접 작용하지 않는다. 이 조건에서 S2의 고정 유동장이 성립한다. 향후 온도 의존 밀도·점도, 부력, 시간가변 유량·압력 제어가 들어오면 `frozenFlow yes`를 그대로 재사용하지 않고 완전 연성 과도 경로를 별도로 검증해야 한다.

## 4. 현재 실행 명령과 기록 보존

[`production_mesh.py`](../src/cfd_twin/production_mesh.py)가 메시 실행기와 게이트를, [`openfoam_case.py`](../src/cfd_twin/openfoam_case.py)가 S0/S1/S2의 OpenFOAM 파일·실행기를 생성한다. 현재 실행 디렉터리는 `build/ppt_bernardi_800k`다.

```bash
build/ppt_bernardi_800k/run-production-mesh.sh
build/ppt_bernardi_800k/run-until-converged.sh
TRANSIENT_DELTA_T=1 ENERGY_COUPLING_ITERATIONS=200 \
  build/ppt_bernardi_800k/run-frozen-flow-actual-1s.sh
```

이미 합격한 메시·S0·S1은 입력 해시와 경계조건이 변하지 않았다면 다시 생성하지 않는다. 과도 실행기는 `runs/frozen_flow_actual_1s`가 이미 있으면 덮어쓰지 않고 실패한다. 재실행 전에는 이전 디렉터리의 설정·로그·부분 결과를 **별도 이름으로 보존**한 뒤 새 실행을 시작한다. 2026-09-16에 중단한 `Δt=0.01 s` 부분 실행은 `runs/frozen_flow_dt0p01_partial_20260916`에 보존했으며, 이 결과는 합격 결과나 UI 원본으로 사용하지 않는다.

`Δt=0.01 s`는 180,000개 시간단계를 요구한다. 현재의 1초 생산 경로는 1,800단계다. **UDF 발열을 매 시간단계 평가한다는 조건은 `Δt=1 s`에서도 만족한다.** 1초 결과 저장 때문에 0.01초 계산이 필요한 것은 아니다. 정확도 요구가 생기면 같은 초기장·기간에서 `1 s`와 `0.5 s` 등 적절한 후보를 비교하고 온도·에너지 수지 허용오차를 먼저 명시한다.

## 5. 통합 CFD에서 입력이 바뀔 때

| 변경된 입력 | 무효화하고 다시 수행할 단계 | 이유 |
| --- | --- | --- |
| CAD, 셀 위치·치수, 입·출구 구경·위치, 메시 목표 | 형상 → 메시 → S0 → S1 → S2 | 계산 영역·포트 면적·벽면 격자와 유동장이 바뀜 |
| 냉각수 종류·상수 물성, 입구 속도, 출구 압력, 난류 입력 | S0 → S1 → S2; 물성의 온도 의존성은 해법 선택도 재검토 | 압력·속도·열전달장 변경 |
| 입구·초기 온도, 배터리 열물성 | S1 → S2; 온도가 유동에 피드백되면 S0/해법도 재검토 | 열응답과 정상 CHT 검증 변경 |
| UDF 계수, 전류·SOC, 발열 모드 | S1의 `t=0` 대표열원과 S2 | 정상 검증 열원 및 시간별 발생열 변경 |
| 화면의 시점·색상·재생 속도만 | CFD 재실행 없음 | 원본 수치장에 영향 없음 |

`결과 보기/계산하기`, 운전조건 입력, CAD 역할 지정, 확인 팝업과 실제 단계별 상태는 [`workflow_app.py`](../src/cfd_twin/workflow_app.py)에 있다. 새 형상은 탱크·포트 외형 / 냉각수 체적 / 23개 배터리의 **STEP 3개**를 제출한다. [`cad_import.py`](../src/cfd_twin/cad_import.py)가 닫힌 고체, 치수·체적, 셀 위치, 상·하부 원형 포트를 읽고 지원 범위를 확인한다. 사용자가 검증된 구경·중심·운전조건을 명시적으로 승인해야 [`workflow_run.py`](../src/cfd_twin/workflow_run.py)가 해시 고정된 별도 디렉터리에서 STEP→실제 STL→메시→S0/S1→S2를 실행한다. 메시 수·품질·영역·계면·수렴·에너지 수지 gate를 통과하면 [`workflow_publish.py`](../src/cfd_twin/workflow_publish.py)가 3D 뷰어를 생성한다. 업로드만으로 계산이 시작되거나 이전 결과가 새 CAD 결과로 재표시되지는 않는다. 진행률은 `mesh 생성/게이트`, `S0/S1 반복·잔차`, `S2 물리시간·검증`, `결과 후처리`처럼 **서로 다른 단위**로 표시한다.

운전조건이나 메시 등급을 바꾸되 CAD를 유지하려면 화면의 **검증된 기준 CAD 사용**에서도 동일한 확인 절차로 별도 작업을 만들 수 있다. 자동 실행기는 macOS와 Windows 모두 호스트 Python에서 같은 `docker compose` 서비스를 호출하고, S0/S1의 연장 판정도 동일한 [`steady_convergence.py`](../src/cfd_twin/steady_convergence.py)를 쓴다. PowerShell 실행기 역시 생성되지만 Windows Docker Desktop에서 실제 1800초 완주까지 검증한 상태는 아니다.

현재 새 CAD 자동 실행의 지원 범위는 **같은 23셀 탱크/배터리 계열에서 상·하부 원형 포트의 구경을 바꾼 STEP**이다. 임의 배터리 배열, 사각 포트, 단일 STL만 있는 경우에는 형상·역할을 추측해 실행하지 않고 명시적으로 거부한다. 메시 등급은 `production_700k` 또는 `production_800k` 중에서 고른다. 기준 CAD에서 전자는 실제 732,766셀·육면체 89.3517%, 후자는 849,284셀·육면체 90.223%로 생성됐다. 70만급의 89% 기준은 80만급의 90% 기준과 구별해 공개하며, 두 등급 모두 입·출구 면적·체적·경계층·비직교도·스큐·영역 계면 게이트를 통과했다. **새 CAD에서는 어느 등급도 셀 수나 품질을 미리 보증하지 않으며** 메시 후 다시 판정한다. CPU·디스크가 필요한 실제 Docker/OpenFOAM 전 과정을 다른 형상에서 아직 완료한 것으로 주장하지 않는다.

기존 완료된 84.9만 셀 케이스는 약 41 GiB이므로, 새 실행을 만들기 전 현재 구현은 **55 GiB 이상 빈 공간**을 요구한다. 기존 결과를 자동 삭제하지 않는다. 같은 컴퓨터의 기준 S2가 여전히 실행 중이면 새 작업 기동도 막는다. 두 조건은 확인 팝업과 API에서 다시 판정한다.

기본 OpenFOAM v2512의 [루프 제어](https://api.openfoam.com/2512/classFoam_1_1loopControl.html)는 해당 시간단계에 기록된 *첫* `h` 잔차를 확인한다. 따라서 나중 반복의 양쪽 `h`가 `10⁻⁵` 아래가 돼도 200회 상한까지 계속 계산하는 사례를 실제 복사본 시험에서 확인했다. 새 Docker 이미지에는 원본 `chtMultiRegionFoam`을 보존한 채, [`patch_adaptive_cht_solver.py`](../src/cfd_twin/patch_adaptive_cht_solver.py)로 `chtMultiRegionFoamAdaptive`를 별도 빌드한다. 이 솔버는 매 결합 반복의 **최신 선형해석 초기 잔차**를 냉각수·고체 모든 영역과 MPI 랭크에서 확인해 둘 다 `10⁻⁵` 미만이면 그 초의 다음 반복을 건너뛴다. 복사한 1800→1801초 **소프트웨어 시험**에서는 60회에 종료했고 원본 루프는 200회까지 갔다. 별도 0→1초 시험은 아직 수렴하지 않아 200회 상한까지 계산했다. 이 시험들은 전체 물리 결과나 1800초 새 계산의 합격을 의미하지 않는다. [`openfoam_energy_coupling_audit.py`](../src/cfd_twin/openfoam_energy_coupling_audit.py)는 각 초의 조기 종료·상한 도달 및 잔차를 따로 기록한다. 별도 PIMPLE outer correction의 마지막 잔차와 에너지 수지도 함께 봐야 하므로 `h<10⁻⁵` 한 숫자만으로 전체 열해석 정확도를 보증하지 않는다.

같은 기준 CAD의 **연속 0–65초 격리 시험**에서는 2–32초 모두 200회 상한이었고, 33초는 마지막 200회째에 두 영역의 기준을 처음 충족했다. 이후 34초 192회, 40초 148회, 50초 89회, 60초 66회, 65초 63회로 자동 감소했다. 41–65초 25개 시간단계는 모두 조기 종료했다. 이는 1초당 일정 횟수를 강제하거나 임의 시간 이후 건너뛰는 로직이 아니라 실제 잔차로 분기한다는 실행 증거다. 다만 Fluent의 후반 3회 반복과 **동일한 수렴 거동이라고 주장할 근거는 없으며**, OpenFOAM 이 시험의 후반은 60회 안팎이다. 시험 기록은 `build/energy_coupling_adaptive_cold_pilot/solver_1_to_10.log`, `solver_10_to_40.log`, `solver_40_to_65.log` 및 대응하는 `energy_coupling_audit_*.json`이며 기존 1800초 기준 결과와 분리돼 있다.

이 격리 시험을 65초 이후에도 이어 계산할 때는, 기존 웹앱의 S2와 같이 전체 유동·난류장을 매초 중복 저장하지 않고 주된 `controlDict`의 `writeInterval=1800`을 적용한다. `writeObjects`가 유체·고체 온도장과 function-object 관측값을 계속 1초마다 기록한다. 변경 전의 1–103초 파일은 전체 장이 약 17 MiB/랭크·초였고, 변경 후 104초 이후는 온도 장 중심으로 약 2.4 MiB/랭크·초다. 이 저장 정책 변경은 CFD 방정식이나 동적 열원을 바꾸지 않으며 기존 완료 케이스에도 적용하지 않았다. 분리된 재시작 로그는 [`openfoam_log_merge.py`](../src/cfd_twin/openfoam_log_merge.py)가 시간 중복·누락을 거부하면서 연속 이력으로 결합한다. 1800초 종료 뒤 새 실행의 원본 결과 검증을 수행하기 전에는 이 시험을 완료 결과로 승격하지 않는다.

호스트 사전점검 `python -m cfd_twin doctor`는 시스템 OpenFOAM과 Docker 실행을 분리해서 표시한다. macOS 실기에서는 Docker Engine 29.7.2, CAD·VTK·Pillow 의존성, 실제 격리 OpenFOAM 병렬 계산까지 확인했다. `openfoam_ready=false`는 호스트 PATH에 솔버가 없다는 뜻일 뿐이며 Docker 기반 작업의 실패를 뜻하지 않는다. Windows 경로는 같은 Python `docker compose` 드라이버와 PowerShell 실행기를 사용하도록 구성했지만, **Windows PC에서 Docker·메시·S0/S1/S2·뷰어의 종단 간 실행은 아직 실증되지 않았다**. 그 검증 전까지 Windows 완전 호환을 완료로 표시하지 않는다.

공통 호스트 사전점검은 프로젝트 의존성을 설치한 뒤 `python tools/host_preflight.py --output build/host_preflight_호스트명`으로 실행한다. 기준 CAD를 실제 생성하고 `production_700k`·`production_800k` 소스와 Docker Compose 구문을 모두 확인해 `host_preflight.json`을 남긴다. 이미 존재하는 출력 디렉터리는 덮어쓰지 않는다. macOS 실기 보고서 `build/host_preflight_mac_20260917_v2/host_preflight.json`은 통과했다. 이 명령은 **실제 70만/80만 격자 생성이나 S0/S1/S2 완주를 하지 않으며**, JSON의 `full_mesh_and_cfd_verified=false`가 이를 명시한다. Windows PC에서도 같은 명령의 보고서를 받아야 호스트 준비를 비교할 수 있고, 이어서 실제 CFD와 뷰어까지 별도로 검증해야 한다.

현재 포털 실행 명령은 다음과 같다. `127.0.0.1`에만 바인딩하므로 같은 컴퓨터의 브라우저에서 `http://127.0.0.1:8765/`로 연다.

```bash
.venv/bin/python -m cfd_twin serve-workflow \
  --case-dir build/ppt_bernardi_800k \
  --cad-dir build/ppt_bernardi_cad_source \
  --viewer-dir build/reference_viewer \
  --port 8765
```

3개 기준 STEP의 SHA-256이 검증 보고서와 정확히 같으면 탱크·포트/냉각수/23개 배터리 역할을 자동 인식한다. 새 CAD에서 같은 이름을 붙였다는 이유만으로 역할이나 Ø19 mm 포트를 가정하지 않는다. 결과 비교표는 3개 슬롯을 준비했으나 실제 검증 완료 데이터가 없는 칸은 비워 둔다. 계산이 끝난 뒤에도 S2 검증·결과 manifest·뷰어 생성이 모두 끝나야 `3D 결과·그래프 열기`가 활성화된다.

뷰어 생성은 모든 필수 gate가 통과한 뒤에만 한다. [`viewer.py`](../src/cfd_twin/viewer.py)는 검증된 원본 필드·probe를 UI 데이터로 변환한다. 시뮬레이션 화면은 격자선을 그리지 않는다. 별도 메시 화면은 [`mesh_wireframe.py`](../src/cfd_twin/mesh_wireframe.py)로 실제 `polyMesh`의 3D 경계, 내부 절개와 XY/XZ 단면을 보여주며, 선택 시에만 **냉각수 1,863,552개 + 고체 1,002,584개 = 2,866,136개 고유 체적 에지 전체**를 로딩한다(표본화 없음). 전체 3D 에지는 겹침이 많아 내부 구조 판독에는 절개·단면 보기를 함께 쓴다. 합격 전의 중단된 실행이나 서로 다른 메시·초기조건의 곡선을 한 결과로 섞지 않는다. 현재 자동 생성되는 84.9만 셀 결과는 단일 메시 **내부 검토용**이다. 과도 솔버 gate와 완전 에너지 수지 및 UI 프레임 검증을 통과해도 별도 격자 독립성·Fluent 교차검증 전에는 논문 최종 승인 결과로 승격하지 않는다.
