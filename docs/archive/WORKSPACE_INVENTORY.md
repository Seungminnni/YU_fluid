# 작업공간 보존 목록 — 2026-09-21

이 문서는 현재 프로젝트의 원본, 배포본, 재생성 가능한 산출물을 구분한다. 삭제 전에는 실제 경로와 USB 보관 상태를 다시 확인한다.

## 반드시 유지

| 경로 | 역할 |
| --- | --- |
| `configs/` | CAD·물성·경계조건·발열의 단일 입력 |
| `src/` | CAD, 메시, OpenFOAM, UI와 후처리의 원본 코드 |
| `tests/` | 회귀·계약 검사 |
| `schemas/` | 입력 스키마 |
| `docs/` | 현재 실행 계약, 판단 기록, USB 복원 기록 |
| `document/` | PPT/PDF/Fluent 참고 원본. Git 외부 대용량 자료 |
| `examples/` | 발열·전류 예제 입력 |
| `pyproject.toml` | Python 패키지와 실행 명령 |
| `README.md` | 현재 전체 UI·계산 흐름의 기준 문서 |

## 현재 로컬 결과·배포 자료

| 경로 | 역할 |
| --- | --- |
| `build/reference_viewer/` | 현재 저장 결과를 보는 로컬 뷰어 |
| `build/YU_fluid_통합CFD_적응형결과/` | 전달 가능한 통합 웹앱 폴더 |
| `build/YU_fluid_웹앱_최종_20260920_v2.zip` | 전달용 ZIP |
| `build/validation_evidence/` | 적응형 솔버와 중단 시험의 축약 로그·보고서 |
| `build/ppt_bernardi_cad_source/` | 기존 CAD에서 입·출구 위치만 사용자 확정 좌표로 수정한 기준 CAD. 메시·CFD 미실행 |
| `build/CAD_상면도_측면도_최종/` | 발표용 CAD 도면 |
| `build/imported_runs/` | 웹앱에서 생성·업로드한 CAD와 새 작업 기록 |
| `build/host_preflight_mac_20260917_v2/` | Mac Docker/CAD 사전점검과 700k/800k 생성 템플릿 |

현재 배포 뷰어와 849,284셀 결과는 수정 전 포트 형상의 기록이다. 새 포트 위치로 재계산하기 전까지 비교·UI 시연용으로 유지하되 최종 논문 결과로 표시하지 않는다.

## USB로 이전한 대용량 원본

위치: `/Volumes/승민/YU_fluid_CFD_원본_20260921`

| 파일 | 내용 |
| --- | --- |
| `ppt_bernardi_800k.tar` | 849,284셀 기준 메시, S0/S1과 기존 전체 계산 원본 |
| `energy_coupling_adaptive_cold_pilot.tar` | 1–1,800초 적응형 에너지 결합 원본 |

두 TAR은 내부 파일과 바이트 단위로 검증했다. 복원 명령과 크기는 [`CFD_ARCHIVE_USB_20260921.md`](CFD_ARCHIVE_USB_20260921.md)에 있다. USB 연결 여부를 확인하지 않고 로컬 요약 결과를 원본이 없다고 판단해 삭제하지 않는다.

## 로컬 README 유지 이유

| 파일 | 유지 이유 |
| --- | --- |
| `README.md` | 저장소 전체 기준 문서 |
| `distribution/README_시작하기.txt` | 새 배포본을 만드는 원본 실행 안내 |
| `build/YU_fluid_통합CFD_적응형결과/README_시작하기.txt` | 폴더나 ZIP만 받은 사용자의 독립 실행 안내 |
| `build/reference_viewer/README_VIEWER.txt` | 뷰어 폴더만 전달될 때의 실행 안내 |
| `build/YU_fluid_통합CFD_적응형결과/viewer/README_VIEWER.txt` | 배포본 내부 뷰어 안내 |
| `build/host_preflight_mac_20260917_v2/production_*/README.md` | 생성된 OpenFOAM 메시 번들의 실행 계약 |

이 파일들은 같은 내용을 무의미하게 복사한 과거 README가 아니라, 각 폴더가 단독으로 이동됐을 때 필요한 지역 안내서다. 현재 전체 흐름 설명은 `README.md`와 `docs/INTEGRATED_CFD_FLOW.md`만 사용한다.

## 재생성 가능하지만 자동 삭제하지 않는 항목

- `.venv/`: 의존성을 다시 설치할 수 있으나 오프라인 즉시 실행에 필요할 수 있다.
- `__pycache__/`, `.pytest_cache/`, `.DS_Store`: 재생성 가능한 캐시다.
- `build/imported_runs/staging/`: 업로드 중간 파일이지만 활성 작업이 참조할 수 있다.
- 실패·중단 작업 폴더: 원인 분석용 `worker.log`와 검증 JSON이 있으므로 작업 ID를 확인한 뒤 삭제한다.

## 삭제 전에 확인할 것

1. 실행 중인 작업이나 Docker 컨테이너가 없는가?
2. 대상이 현재 뷰어 또는 배포 ZIP에서 참조되는가?
3. CFD 원본이면 USB TAR과 바이트 검증 기록이 있는가?
4. 실패 작업이면 원인을 문서나 축약 로그로 남겼는가?
5. 삭제 후 새 계산에 필요한 여유공간이 실제 파일시스템 기준으로 확보되는가?
