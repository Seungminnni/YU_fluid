# 영남대학교 배터리 냉각 디지털 트윈

23개 원통형 배터리의 냉각 CAD, OpenFOAM 계산, 검증 보고서와 3D 뷰어를 연결하는 프로젝트다. Git 저장소에는 재현에 필요한 소스·설정·테스트·문서만 보관하며, OpenFOAM 시간장과 메시, 로컬 웹 결과, 가상환경 및 배포 ZIP은 포함하지 않는다. 따라서 새 체크아웃은 계산 결과가 없는 개발 환경이고, 기존 결과를 그대로 보려면 별도로 보관한 생성물을 복사해야 한다.

- [Windows 설치·Python 3.12 이상·새 실험 실행](docs/windows_and_new_experiments.md)
- [전체 계산·뷰어·검증 안내](docs/README.md)
- [Ø28·31·34 시안의 메시 시험과 벽면 진단](docs/frame_fit_trials.md)
- [실패한 메시·경계조건·수치 설정 통합 기록](docs/CFD_실패_경계조건_메시_통합정리.md)
- [보존한 네 결과의 위치](build/result/README.md)
- [시작 화면](viewer/index.html) · [결과 뷰어](viewer/results.html) · [CAD 도면](viewer/cad/index.html) · [3D 메시](viewer/mesh.html)

## License

Copyright © 2026 Seungmin Lee and Heonje Woo. All rights reserved.

Approved collaborators may work on YU_fluid internally for noncommercial purposes under [LICENSE.md](LICENSE.md). Both copyright holders must give prior written permission to use the original project materials outside that scope, share them externally, or use them commercially. OpenFOAM and other third-party components are governed by their respective licenses.


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
