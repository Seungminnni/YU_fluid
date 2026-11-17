# 영남대학교 배터리 냉각 디지털 트윈

23개 원통형 배터리의 냉각 CAD, OpenFOAM 계산, 검증 보고서와 3D 뷰어를 연결하는 프로젝트다. Git 저장소에는 재현에 필요한 소스·설정·테스트·문서만 보관하며, OpenFOAM 시간장과 메시, 로컬 웹 결과, 가상환경 및 배포 ZIP은 포함하지 않는다. 따라서 새 체크아웃은 계산 결과가 없는 개발 환경이고, 기존 결과를 그대로 보려면 별도로 보관한 생성물을 복사해야 한다.

- [Windows 설치·Python 3.12 이상·새 실험 실행](docs/windows_and_new_experiments.md)
- [전체 계산·뷰어·검증 안내](docs/README.md)
- [Ø28·31·34 시안의 메시 시험과 벽면 진단](docs/frame_fit_trials.md)
- [실패한 메시·경계조건·수치 설정 통합 기록](docs/CFD_실패_경계조건_메시_통합정리.md)
- [보존한 네 결과의 위치](build/result/README.md)
- [시작 화면](viewer/index.html) · [결과 뷰어](viewer/results.html) · [CAD 도면](viewer/cad/index.html) · [3D 메시](viewer/mesh.html)

## 다른 작업자를 위한 인수인계

### 새 체크아웃 준비

Windows PowerShell과 Python 3.12 이상을 기준으로 한다. 계산에는 Docker Desktop의 Linux 컨테이너와 프로젝트에서 검증한 OpenFOAM 이미지가 추가로 필요하다.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[cad,media,results]"
.\.venv\Scripts\python.exe -m unittest discover -s tests
.\.venv\Scripts\cfd-twin.exe doctor
```

이 저장소의 현행 생산 계산은 **OpenFOAM 전용**이다. 과거 호환·진단 목적으로 남아 있는 Fluent 관련 스크립트는 새 작업에서 실행하지 않는다. ROM 캠페인 설정과 설계 문서는 재현성 때문에 보존하지만 캠페인은 현재 중지 상태이며 명시적인 재개 지시 없이 실행하지 않는다.

### 주요 진입점

- `configs/experiments/`: 재현 가능한 실험 입력. 새 조건은 기존 YAML을 덮어쓰지 말고 새 파일로 추가한다.
- `cfd-twin inspect-reference-mesh`: 등록한 기준 메시의 셀·면·영역과 해시를 점검한다.
- `cfd-twin build-sweep-mesh-source`: 기준 메시 규칙을 이용해 구조화 all-hexa sweep 메시 원본을 새 폴더에 만든다.
- `tools/run_prevalidated_sweep_fom.py`: 검증된 sweep 메시로 새 FOM 작업을 준비·실행한다.
- `tools/sync_viewer_release.py`: 검증 완료 결과만 뷰어 배포본에 동기화한다.
- [원뿔대 sweep 메시 재현 규칙](docs/CONICAL_SWEEP_MESH_LESSONS.md)과 [실패·재시도 원칙](AGENTS.md)을 메시 생성 전에 확인한다.

도움말은 설치 후 `cfd-twin --help`와 각 하위 명령의 `--help`로 확인한다. 계산 작업은 항상 새 UUID와 빈 출력 폴더에서 시작하고, 검증을 통과하지 않은 메시·유동장은 게시하거나 다음 작업에 재사용하지 않는다.

### Git에 포함되는 것과 제외되는 것

커밋 대상은 `src/`, `tests/`, `tools/`, `configs/`, `docs/` 및 최상위 설정·안내 파일이다. 다음 항목은 로컬 생성물이므로 `.gitignore`로 제외한다.

- `.venv/`, `.venv_*/`, `venv/`, `env/`: Python 환경
- `build/`: CAD 복사본, OpenFOAM case, 메시, 시간장, 보고서와 ROM 산출물
- `viewer/`, `outputs/`, `output/`, `dist/`: 생성된 HTML·이미지·배포 자산
- `tmp/`, 캐시, 로그, PID와 편집기 임시파일
- `*.zip`, `*.7z`, `*.rar`, `*.tar*`: 대용량 배포·보관 파일
- `.env`, `.env.*`: 로컬 환경값
- `tools/continue_romfom*.ps1`, `tools/pause_romfom*_helper_after_*.ps1`: 특정 로컬 컨테이너·PID에 묶인 일회성 제어 스크립트

로컬 합격 결과나 체크포인트가 Git에 없다고 삭제하면 안 된다. 결과 보존·정리 규칙은 `AGENTS.md`를 따르며, 다른 컴퓨터로 옮길 때는 필요한 `build/`, `viewer/` 및 배포 ZIP을 별도 저장소로 전달한다. 커밋 전에는 아래처럼 생성물 혼입과 테스트 상태를 확인한다.

```powershell
git status --short
git diff --check
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

물 기준 직경 비교 순서는 Ø19·21·23·25·28·31이다. 나머지 네 항목은 Ø25 물 비교와 PAO2, AmpCool AC-110, Shell S3X 물성 결과다. Ø21·23·25는 C05 상부 입구·C19 하부 출구 CAD로 새로 계산했고 Ø19는 기존 해석장을 보존해 표시한다. 검증 보고서를 통과한 결과만 웹앱에 게시하지만 단일 메시 비교 단계 결과를 논문 최종 승인값으로 자동 간주하지 않는다.

## 계산 흐름과 판정

새 작업은 `CAD 생성·검증 → production mesh → S0 냉간 유동 → S1 정상 CHT → S2 1초 간격 열해석 → 검증 → 웹앱 누적` 순서로 진행된다. 각 작업은 고유 UUID와 빈 폴더에서 시작한다. 실패 작업은 진단과 고정 입력을 보존한 뒤 해당 작업 생성물만 삭제하며, 공유 CAD와 완료 결과는 유지한다. 실패 메시나 유동장을 다음 작업의 초기장으로 재사용하지 않는다.

S0는 메시 품질, 배터리 표면 `y+<1` 면적 95%, 최근 200회 필수 유동 잔차 `1×10⁻⁴`, 질량 불균형 0.1%, 압력강하 변동 0.5%, 유한 온도·속도·난류장을 모두 검사한다. 안전한 잔차·질량·압력 정체만 남으면 기본 9,500회까지 500회씩 자동 연장한다. y+ 또는 안전성 검사가 실패하면 반복만 늘리지 않고 새 메시가 필요하다. S0가 실패하면 S1·S2와 게시를 실행하지 않는다.

Ø34의 안정적인 6 µm·11층 시험은 y+와 질량보존을 통과했지만 압력식 초기 잔차가 기준 위에서 정체됐다. 최대 질량 불균형은 약 `2.05×10⁻⁸`이므로 현재 근거상 일반적인 연속성/질량보존 실패라고 부르는 것은 정확하지 않다. 출구 역류는 기여 요인이지만 경계조건 변경만으로 잔차가 해결되지 않았다. 상세 수치와 배제 설정은 위 통합 기록에 정리돼 있다.

안전한 정체를 더 오래 측정해야 하는 새 실험은 YAML에 다음 값을 선택적으로 넣을 수 있다.

```yaml
cold_flow_maximum_iterations: 20000
```

허용 범위는 6,000~50,000회의 500회 단위다. 이 값은 합격 기준을 낮추지 않으며, 지정 상한까지 계산해도 검증에 실패하면 결과를 게시하지 않는다. 새 값은 준비 단계에서 `batch.json`과 `job.json`에 고정된다.

## 빠른 실행

Python 3.12 이상과 Docker Desktop Linux 컨테이너가 필요하다. 계산 드라이브에는 최소 55 GiB 여유공간을 둔다. 웹앱만 실행할 때는 다음 명령을 사용한다.

```sh
PYTHONPATH=src python3 tools/serve_current_viewer.py
```

새 CAD·운전조건은 새 YAML과 새 출력 폴더로 준비한 뒤 실행한다.

```sh
python tools/run_experiment.py --config configs/experiments/frame_fit_water.yaml --output build/experiments/water_trial_01 --ranks 4 --prepare-only
python tools/run_experiment.py --output build/experiments/water_trial_01 --run
```

Windows에서는 `distribution/run_windows.cmd`로 웹앱을 시작한다. 배치 준비·실행·재시도, Docker 확인, 로그와 결과 위치, Windows 잠금 처리와 Python 환경은 [Windows 설치·새 실험 안내](docs/windows_and_new_experiments.md)에 명령 단위로 기록돼 있다.

압축을 풀어 HTML을 직접 여는 형태는 발표용 화면이다. 새 CFD 계산을 실제로 실행하려면 로컬 Python 서버와 Docker/OpenFOAM 환경이 필요하다. 과거 상세 기록은 `docs/archive/`에 원문 그대로 보관했으며 현행 상태의 기준은 [통합 안내](docs/README.md)다.

배포본은 두 가지만 유지한다. `viewer/`가 로컬 서버에서 제공하는 원본이고, `YU_fluid_데모.zip`은 이 폴더의 검증된 오프라인 복사본이다. 별도의 데모 뷰어 폴더를 손으로 수정하지 않는다. 기존 뷰어만 바꾼 경우 `PYTHONPATH=src python3 tools/sync_viewer_release.py`로 ZIP을 다시 만든다. 새 1800초 계산은 메시·수렴·에너지 수지·1초 데이터 검증을 통과한 뒤 실행 흐름에서 두 배포본에 함께 추가된다. 배포에 실패해도 계산 결과는 보존되고 실행 상태의 `release_error`에 원인이 기록된다.

`build/result/`, `viewer/`, `YU_fluid_데모.zip`은 대용량 생성물이라 Git에는 포함하지 않는다. 다른 컴퓨터에서 현재 10개 결과를 그대로 보려면 이 파일들을 별도로 복사해야 한다.

로컬 서버는 `PYTHONPATH=src python3 tools/serve_current_viewer.py`로 실행한다. 이 명령은 최상위 `viewer/`와 별도 보존한 검증 기준 CAD를 사용하며, 과거 원본 케이스가 있는 `build/result/`에 의존하지 않는다. 새 계산 작업 파일은 `build/imported_runs/`에 저장된다. 따라서 `build/result/`을 외장 저장소에 보관하더라도 서버의 기존 결과와 새 실험의 결과 추가 경로는 유지된다. 단, 원본 OpenFOAM 케이스를 다시 분석하거나 재가공하려면 외장 저장소를 다시 연결해야 한다.

Python 요구사항은 **3.12 이상**이다. Windows에서는 `distribution/run_windows.cmd`를 실행한다. 새 체크아웃도 결과 0개 상태로 웹앱을 시작할 수 있다. 새 조건의 배치는 `tools/run_experiment.py --config <실험.yaml> --output <새 폴더> --prepare-only`로 준비한 뒤 같은 출력 폴더에 `--run`으로 실행한다. 상세 명령은 위 Windows·새 실험 안내를 따른다.
