# Ø28·31·34 시안의 메시 시험과 벽면 진단

이 코드는 새 입출구 CAD 시안의 메시와 유동을 검증하는 실험용 흐름이다. 프로필을 생성하거나 계산을 시작했다는 사실은 결과 검증 통과를 뜻하지 않는다. 기존 물 조건과 결과 게시 기준을 유지하며, 검증을 통과한 새 결과만 기존 뷰어에 추가한다.

## 계산 설정

- 메시 생성은 재현성을 위해 1 MPI rank를 사용한다. 솔버 rank 수는 `OPENFOAM_MPI_RANKS`로 지정한다. 현재 M5 Pro 시험 배치는 실측 비교에 따라 12 ranks를 사용한다.
- `parallelism.py`의 macOS 자동 감지는 `Super`와 `Performance`로 표시된 코어 수를 합산한다. 이는 기본 프로세스 수이며 특정 물리 코어에 대한 고정을 보장하지 않는다.
- `production_frame_fit_8um_10layers_ab_A`는 첫 층 8 µm·요청 10층의 기준 시험이다. B 프로필은 `displacementMotionSolver`와 이에 필요한 메시 이동 솔버 설정만 변경한다.
- A/B 프로필은 사용자 지시에 따라 총 600,000~690,000셀을 허용한다. 육면체 비율 88%, 비직교도 70°, skewness 5, 배터리 면적의 95% 이상에서 y+<1인 조건은 유지한다. 다른 프로필의 셀 수 허용 범위는 변경하지 않는다.

## 좌표별 진단

`production_mesh.py`는 A/B 시험에서 영역 분리와 진단 필드 삭제 전에 다음을 보존한다.

- `case/diagnostics/wall/snappy_layer_snapshot.tar.gz`: 진단 필드와 그 필드가 속한 원본 메시, 메시 설정 및 로그
- `layer_faces.json.gz`: 배터리 계면의 면 좌표, 면적, 유체측 `nSurfaceLayers`
- `layer_spatial_summary.json`: 층이 없는 면과 1~3층·4~9층·10층 이상 면의 면적 비율

1,500회 초기 유동 계산 후 같은 디렉터리의 `1500/`에 y+ 원본과 `layer_yplus_faces.json.gz`, `spatial_summary.json`을 기록한다. 메시 분리로 면 번호가 바뀌므로 면의 꼭짓점 좌표를 순서와 무관하게 비교한다. 모든 면이 일대일 대응하지 않으면 근접 좌표로 추측하지 않고 중단한다. 서로 다른 작업의 지표를 하나의 결과로 합치지 않는다.

짧은 계산은 진단용이다. 원래 6,000회 지점까지 이어서 y+, 수렴, 질량수지와 필드 검증을 수행하고, 정상 CHT 및 과도 열 계산은 기존 복합 검증을 통과한 뒤 실행한다. 층수가 늘어도 y+ 통과를 가정하지 않는다. v2512의 내부 계면에서 두께 진단 필드가 0으로 기록되는 경우 이를 실제 국부 두께로 해석하지 않는다.

## 배치 실행과 재시도

Python 3.12 이상과 설치된 프로젝트 환경에서 새 배치를 준비하고 실행한다.

```sh
python tools/run_experiment.py --config configs/experiments/frame_fit_water.yaml --output build/experiments/water_trial_01 --ranks 4 --prepare-only
python tools/run_experiment.py --output build/experiments/water_trial_01 --run
```

새 실행기는 저장소의 YAML에서 CAD와 입력을 생성하므로 예전 `batch.json`, preview, `viewer/`가 필요하지 않다. 각 시안에서 A를 시험한 뒤 메시/y+ 실패 시 B를 새 ID로 생성한다. 실패 진단 자료를 `diagnostic_attempts/<작업 ID>/`에 보존하고 실패한 메시·유동장은 삭제한다. 후보 소진이나 다른 오류는 `needs_attention`으로 종료한다. 전체 사용법은 [새 실험 안내](windows_and_new_experiments.md)를 따른다.

기존 Mac의 날짜 배치를 이어가는 `python tools/run_wall_diagnostic_batch.py` 경로도 유지한다. 인수 없이 기존 `build/frame_fit_water_20260929/batch.json`이 있으면 Ø28에서 성공한 설정을 Ø31·Ø34에 적용하는 기존 전략을 사용한다. Windows와 POSIX 모두 파일 잠금으로 동일 배치의 중복 실행을 막는다.

`build/`, `viewer/`, 데모 압축 파일 및 로컬 연구 문서는 Git 대상에서 제외한다. 코드 패키지에 포함된 소형 기준 CAD 자산은 소스와 함께 관리한다.

## Ø34의 B 시험 이후 보정

Ø34의 8 µm B 시도는 662,674셀·육면체 89.4256%로 메시를 통과했으나,
S0 6,000회에서 y+<1 면적 94.5825%와 압력 잔차 기준 미달로 멈췄다.
같은 좌표의 층/y+ 진단에서 기준 초과 면은 0층 또는 1~3층 구간에 있었다.
`production_frame_fit_6um_10layers_motion`과 `production_frame_fit_4um_10layers_motion`은
B의 shrinker·10층·품질 기준을 유지하고 첫 층 두께만 줄이는 미검증 시험 프로필이다.

기존 날짜 배치의 실패한 Ø34만 새 ID로 재생성할 때는 다음을 사용한다.
완료된 Ø28·Ø31은 건드리지 않으며, 진단 자료 보존 후 실패 생성물을 삭제한다.

```sh
python tools/resume_frame_fit_batch.py --diameter 34 --profiles production_frame_fit_6um_10layers_motion production_frame_fit_4um_10layers_motion
```

6 µm가 통과하면 본 계산으로 이어지고, 메시/y+ 실패에만 다음 4 µm 후보를 쓴다.
잔차만 미달한 경우에는 기존 9,500회 상한 내 수렴 판정을 먼저 따르며,
후보 소진이나 다른 오류를 성공으로 처리하지 않는다. 새 YAML 실험에서도 이 이름을
`mesh_profiles`에 명시할 수 있다. 생성된 실제 보고서가 합격 여부의 기준이다.

### Ø34의 층수 보정 시험

6 µm·10층은 층이 없는 벽면 면적 30.3046%, 4 µm·10층은 90.2265%로
층 생성 검증에 실패했다. 해당 시도들은 유동 계산에 진입하지 않았다.
기존 8 µm·10층의 무층 면적은 1.7819%였다. 이 비교만으로 원인이
완전히 확인된 것은 아니며, 서로 다른 메시의 y+를 결합해 판정하지 않는다.

`production_frame_fit_6um_11layers_motion`과 `production_frame_fit_6um_12layers_motion`은
실패한 6 µm·10층에서 요청 층수만 늘린 새 시험이다. 팽창비 1.2에서 요청 총 두께는
각각 192.9 µm와 237.5 µm로, 기존 8 µm·10층의 207.7 µm에 가까운 범위를 시험한다.
실제 층수·두께·품질·600,000~690,000셀·y+ 기준은 모두 다시 검증한다.

```sh
python tools/resume_frame_fit_batch.py --diameter 34 --profiles production_frame_fit_6um_11layers_motion production_frame_fit_6um_12layers_motion
```
