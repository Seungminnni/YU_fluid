# Windows 설치와 새 실험

Python **3.12 이상**을 사용한다. 3.13·3.14를 설치한 PC에 3.12를 추가로 설치할 필요는 없다. 각 Python 버전에 맞는 CadQuery·VTK 등의 패키지 설치는 실제 `pip` 결과로 확인한다. Python 최소 버전과 외부 패키지의 지원 범위는 별개다.

## Windows에서 시작

저장소 폴더에서 최신 코드를 받은 뒤 실행한다.

```powershell
git pull origin main
.\distribution\run_windows.cmd
```

실행기는 저장소 루트를 찾고 Python 3.12 이상의 `.venv`를 준비한다. 기존 `.venv`가 3.11 이하라면 그 폴더를 다른 이름으로 옮기고 다시 실행한다. 설치가 끝나면 `http://127.0.0.1:8765/`에서 웹앱을 연다.

이미 사용할 Python 환경에 의존성이 설치돼 있다면 그 환경에서 다음처럼 실행해도 된다.

```powershell
python -m pip install -e ".[cad,results,media]"
python tools/serve_current_viewer.py
```

새 체크아웃에 `viewer/`, 예전 `batch.json`, 승인 preview가 없어도 웹앱이 열린다. 처음에는 결과가 0개다. CAD 생성/업로드 후 조건과 형상을 확인하고 새 계산을 시작한다. 입력만 바꾸거나 서버를 여는 것으로 계산이 시작되지는 않는다. 첫 계산이 모든 검증을 통과하면 결과 뷰어를 생성하고, 이후 계산 결과는 기존 목록에 추가한다. 기존 PC의 결과도 보려면 `viewer/` 전체를 별도로 복사해야 한다.

## Docker

Docker Desktop을 Linux 컨테이너 모드로 실행하고 다음을 확인한다.

```powershell
docker info
docker compose version
```

OpenFOAM 이미지는 저장소에 포함하지 않는다. 첫 계산에서 Compose의 `--build`가 기본 이미지 다운로드와 로컬 이미지 빌드를 수행하므로 인터넷과 Docker 저장공간이 필요하다. 계산 드라이브에는 최소 55 GiB 여유공간이 필요하다. 이미지 다운로드 실패는 메시 품질 실패로 취급해 A/B 후보를 반복하지 않는다. 원인을 해결한 후 실패 재시도 명령을 사용한다.

## Ø28·31·34 또는 다른 조건의 배치

`configs/experiments/frame_fit_water.yaml`에 기존 물 조건과 세 CAD 시안의 overlay가 들어 있다. 예전 PC의 `build/`를 복사할 필요가 없다.

먼저 CAD를 생성·검증하고 실험 입력을 고정한다. 이 명령은 Docker 계산을 시작하지 않는다.

```powershell
python tools/run_experiment.py --config configs/experiments/frame_fit_water.yaml --output build/experiments/water_trial_01 --ranks 4 --prepare-only
```

`build/experiments/water_trial_01/cad/`의 CAD·형상 보고서와 `batch.json`을 확인한 뒤 계산한다.

```powershell
python tools/run_experiment.py --output build/experiments/water_trial_01 --run
```

`--ranks`는 솔버 MPI 프로세스 수다. 지정하지 않으면 `OPENFOAM_MPI_RANKS`, macOS 코어 감지, 그 외 플랫폼 기본값 4 순서로 결정한다. Windows의 논리 CPU 28개가 곧 최적 MPI 28개를 뜻하지는 않는다. Docker에 할당한 CPU/메모리 내에서 조정해야 하며, 새 출력 폴더를 사용해 비교한다. 메시 생성은 1 rank를 유지한다.

다른 실험은 YAML을 복사해서 `inputs`, `cases`, `overlays` 또는 `overrides`를 수정하고 **새 `--output` 폴더**로 준비한다. overlay 경로는 그 실험 YAML의 위치를 기준으로 해석한다. `cases[].inputs`는 공통 입력을 해당 시안에만 덮어쓴다. 예를 들어:

```yaml
schema_version: 1
name: water_velocity_test
inputs:
  coolant: deionized_water
  inlet_temperature_k: 303.0
  inlet_velocity_m_s: 0.6
  outlet_pressure_pa: 101325.0
  mesh_target_cells: 600000
cold_flow_maximum_iterations: 20000
mesh_profiles:
  - production_frame_fit_8um_10layers_ab_B
cases:
  - name: phi28_warm
    overlays: [../frame_fit_water_phi28_20260929.yaml]
  - name: phi31_faster
    overlays: [../frame_fit_water_phi31_20260929.yaml]
    inputs:
      inlet_velocity_m_s: 0.8
```

`cold_flow_maximum_iterations`는 선택 항목이다. 기본값은 9,500회이며 6,000~50,000
사이의 500회 단위 정수만 허용한다. 이 값이 크면 S0 보고서에서 메시, y+, 유한 필드,
온도·난류 안전성 검사를 통과하고 잔차·질량보존·압력강하 안정성처럼 계속 관찰할 수 있는
항목만 실패했을 때 500회씩 연장한다. 안전성 또는 y+가 실패하면 즉시 멈춘다. 지정한
상한까지 계산해도 모든 기존 합격 기준을 통과하지 못하면 S1·S2와 웹앱 게시는 실행하지
않는다. 즉 이 항목은 잔차 기준을 낮추는 옵션이 아니라, 안전한 정체 구간을 끝까지
측정하기 위한 계산 예산이다.

공통값 대신 특정 `cases[]` 안에 같은 항목을 두면 그 시안에만 적용된다. 준비된
`batch.json`과 각 `job.json`에 값이 고정되므로, 준비 후 값을 바꾸려면 반드시 새
`--output` 폴더와 새 작업 ID를 사용한다.

이 예시는 `configs/experiments/` 아래에 저장한다. 준비가 끝난 배치의 조건과 rank 수는 고정된다. 동일한 출력 폴더에 다른 설정을 덮어쓰지 않는다. 지원 범위는 현재 검증된 **23셀 탱크 계열·탈이온수**이며, 새로운 CAD도 실제 형상/간섭 검사를 통과해야 한다. 형상과 운전조건을 바꿨다고 계산의 수렴이나 메시 품질 통과가 보장되지는 않는다.

## 진행·재시도·결과

- 배치 상태: `build/experiments/<실험명>/status.json`
- 개별 상태/로그: `build/imported_runs/runs/<ID>/status.json`, `worker.log`
- 고정 입력·계획: 배치 `batch.json`, 각 작업의 `job.json`
- 결과: 저장소의 `viewer/`, 동일 내용의 `YU_fluid_데모.zip`

샘플은 각 시안에서 A를 먼저 시험하고 실제 메시/y+ 검증 실패 시 B로 새로 생성한다. 실패한 작업은 진단 자료를 `diagnostic_attempts/<ID>/`에 보존한 뒤 삭제하며, 새 UUID와 빈 폴더로 시작한다. 기존 성공 결과·다른 작업·공유 STEP은 삭제하지 않는다. 일반 오류 또는 후보 소진 시 `needs_attention`으로 멈춘다. 원인을 해결한 후 명시적으로 한 번 재시도할 수 있다.

```powershell
python tools/run_experiment.py --output build/experiments/water_trial_01 --run --retry-failed
```

완료된 작업은 다시 계산하지 않는다. 상태가 `running`인 작업도 자동 재시작하지 않는다. `complete`와 `release_error`가 함께 있으면 계산은 완료됐고 게시만 실패한 것이므로 원본 계산 폴더를 보존하고 게시 오류를 해결해야 한다.

기존 `tools/run_wall_diagnostic_batch.py`와 `tools/run_frame_fit_batch.py`도 `--config`, `--output`, `--prepare-only`, `--run` 옵션으로 이 실행기를 사용한다. 인수 없이 기존 날짜 배치가 있으면 기존 실행 경로를 유지하고, 없으면 새 기본 배치의 준비만 수행한다.

## Windows 호환 처리

잠금은 POSIX `flock` 또는 Windows `msvcrt`를 사용한다. OpenFOAM에 전달되는 스크립트는 LF로 생성한다. 기준 STEP은 `.gitattributes`의 `-text`로 원본 바이트를 보존한다. 예전 Windows 체크아웃에서 CRLF 변환이 이미 일어났다면 LF로 되돌린 내용이 기록된 SHA-256과 **정확히 일치할 때만** `build/reference_cad/`에 검증 복사본을 만든다. 다른 내용 변경은 계속 거부한다.
