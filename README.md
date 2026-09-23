# 영남대학교 배터리 냉각 디지털 트윈

23개 원통형 배터리의 냉각 CAD, OpenFOAM 계산 결과와 3D 뷰어를 연결하는 로컬 프로젝트다. 현재 비교 대상은 `build/result/`에 보존한 Ø19·21·23·25 mm 네 케이스다. 잘못된 CAD 기반의 과거 약 80만 셀 결과는 현행 결과에서 제외한다.

- [전체 계산·뷰어·검증 안내](docs/README.md)
- [보존한 네 결과의 위치](build/result/README.md)
- [시작 화면](viewer/index.html) · [결과 뷰어](viewer/results.html) · [CAD 도면](viewer/cad/index.html) · [3D 메시](viewer/mesh.html)

Ø21·23·25는 C05 상부 입구·C19 하부 출구 CAD로 새로 계산했다. Ø19는 기존 해석장을 보존하고 뷰어에서 좌표를 반사해 표시한다. 네 케이스의 검증 보고서는 통과했지만 단일 메시 비교 단계이며 논문 최종 승인값은 아니다.

압축을 풀어 HTML을 직접 여는 형태는 발표용 화면이다. 새 CFD 계산을 실제로 실행하려면 로컬 Python 서버와 Docker/OpenFOAM 환경이 필요하다. 과거 상세 기록은 `docs/archive/`에 원문 그대로 보관했으며 현행 상태의 기준은 [통합 안내](docs/README.md)다.

배포본은 두 가지만 유지한다. `viewer/`가 로컬 서버에서 제공하는 원본이고, `YU_fluid_데모.zip`은 이 폴더의 검증된 오프라인 복사본이다. 별도의 데모 뷰어 폴더를 손으로 수정하지 않는다. 기존 뷰어만 바꾼 경우 `PYTHONPATH=src python3 tools/sync_viewer_release.py`로 ZIP을 다시 만든다. 새 1800초 계산은 메시·수렴·에너지 수지·1초 데이터 검증을 통과한 뒤 실행 흐름에서 두 배포본에 함께 추가된다. 배포에 실패해도 계산 결과는 보존되고 실행 상태의 `release_error`에 원인이 기록된다.

`build/result/`, `viewer/`, `YU_fluid_데모.zip`은 대용량 생성물이라 Git에는 포함하지 않는다. 다른 컴퓨터에서 현재 네 결과를 그대로 보려면 이 파일들을 별도로 복사해야 한다.

로컬 서버는 `PYTHONPATH=src python3 tools/serve_current_viewer.py`로 실행한다. 이 명령은 최상위 `viewer/`와 별도 보존한 검증 기준 CAD를 사용하며, 과거 원본 케이스가 있는 `build/result/`에 의존하지 않는다. 새 계산 작업 파일은 `build/imported_runs/`에 저장된다. 따라서 `build/result/`을 외장 저장소에 보관하더라도 서버의 기존 결과와 새 실험의 결과 추가 경로는 유지된다. 단, 원본 OpenFOAM 케이스를 다시 분석하거나 재가공하려면 외장 저장소를 다시 연결해야 한다.
