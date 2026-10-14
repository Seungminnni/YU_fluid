# ROM 도입 및 자동 메시 안정화 계획

## 현재 상태

- 현재 실행 경로는 OpenFOAM full-order model(FOM)만 지원한다. ROM 학습·검증·온라인 추론 경로는 아직 없다.
- ROM은 첫 기준 계산을 대체하지 않는다. 동일한 형상/메시 계열에서 충분한 FOM snapshot을 만든 뒤, 검증 범위 안의 반복 질의에 사용한다.
- 자동 메시 실패는 셀 수, 벽면층, 기하 품질, 위상/영역/계면 실패를 서로 다른 원인으로 다뤄야 한다.

## 라이브러리 선택

| 선택지 | 장점 | 이 프로젝트에서의 용도 |
| --- | --- | --- |
| [ITHACA-FV](https://github.com/ITHACA-FV/ITHACA-FV) | OpenFOAM 안에서 POD/POD-Galerkin/DEIM 등을 수행하며 OpenFOAM v2512도 지원 목록에 있음 | 최종 field-level ROM의 1순위 |
| [EZyRB](https://mathlab.github.io/EZyRB/) | 비침습적 POD + RBF/GPR/ANN, solver 출력 형식에 독립적 | 가장 빠른 Python MVP와 설계변수 surrogate |
| [pyMOR](https://docs.pymor.org/latest/) | 외부 PDE backend용 추상 인터페이스, reduced-basis 및 오류추정 도구가 풍부함 | 장기적인 ROM orchestration/검증 계층 |
| [Pressio](https://pressio.github.io/) | 대규모 동역학 projection ROM 및 hyper-reduction | 고급 transient 연구 후보; Python 바인딩 유지 상태를 먼저 확인 |

### 권장안

1. **단기:** EZyRB의 POD+RBF로 `입구속도, 입구온도, 발열량 -> T_max, T_avg, 출구온도, 압력강하`와 온도장 snapshot을 학습한다.
2. **중기:** 현재 OpenFOAM v2512 컨테이너에 ITHACA-FV를 별도 이미지로 고정하고, 유동/온도 field ROM을 검증한다.
3. **장기:** pyMOR를 오류 추정과 FOM fallback을 포함한 다중충실도 실행 관리자에 사용한다.

따라서 하나만 고르면 **ITHACA-FV가 현재 OpenFOAM 기반 프로그램에 가장 직접적인 선택**이다. 다만 구현 위험이 가장 낮은 첫 단계는 EZyRB다. 난류 CHT에서 검증되지 않은 ROM 결과는 release 결과로 게시하지 않는다.

## 예상 시간 단축

- Offline 단계: 훈련용 FOM 계산이 필요하므로 첫 계산 시간은 줄지 않으며 전체 준비 시간은 오히려 늘어난다.
- Online scalar KPI surrogate: 학습 범위 안에서 보통 밀리초~초 단위가 가능하므로, 수시간 FOM 대비 대략 `10^3~10^5`배 후보가 된다.
- Online field reconstruction/POD ROM: snapshot 크기와 모드 수에 따라 초~수분이며, 대략 `10^2~10^4`배 후보가 된다.
- 위 수치는 보장이 아니라 용량 산정치다. 이 프로젝트에서는 hold-out FOM으로 온도, 압력강하, 질량/에너지 수지 오차를 측정한 뒤 실측 speed-up을 보고한다.

## 자동 메시 보강 규칙

1. `mesh_target_composition.json` 실패는 셀 예산/조성 실패로 분류한다. 품질 한계를 완화하지 않는다.
2. `layer_coverage.json` 실패는 실패한 배터리 목록만 국부 보정한다. 전체 영역을 무조건 세분화하지 않는다.
3. `checkMesh_all_regions.json` 실패는 기하 품질 실패로 분류하고 내부/경계 skewness 한계를 분리 기록한다.
4. 영역, 양의 체적, 위상, 계면 실패는 hard failure로 다루며 solver를 시작하지 않는다.
5. 여러 gate가 동시에 실패하면 위상/계면 -> 층/품질 -> 셀 수 순으로 고친다.
6. 모든 실패는 원본 보고서, 로그, 입력, 보정 근거를 보존하고 fresh UUID/빈 폴더에서 다시 생성한다.

`checkMesh`가 출력하는 `Max skewness`는 내부/경계를 합친 최대값이다. 별도 최대값을 제공하지 않는 로그를 내부 최대값으로 표시하지 않는다. 대신 품질 표의 `internal`/`boundary` 적용 한계와 초과 face 수를 함께 저장한다.

## ROM 승인 기준

- 훈련/검증/시험 case를 분리한다.
- 검증 범위를 벗어나면 ROM 대신 FOM으로 자동 fallback한다.
- `T_max`, `T_avg`, 출구온도, 압력강하, 질량/에너지 수지에 개별 오차 한계를 둔다.
- 400k/800k/1000k 격자 수렴 결과로 FOM 자체의 격자 불확실도를 먼저 확정한다.
- ROM 오차와 격자 오차를 한 숫자로 합치지 않는다.
