# Ø20 mm 포트 변경 CAD → 메시 검증 기록

2026-09-17의 별도 시험 산출물 `build/cad_port20_test`와
`build/cad_port20_mesh_validation`을 2026-09-18에 정리하면서 남긴 요약이다.
이 시험은 기준 849,284셀 계산과 **다른 형상**이며, 최신 웹앱 결과의 입력이 아니다.
원본 STEP·STL·OpenFOAM 메시·로그는 이 문서에 보관하지 않는다.

## 입력 형상과 판독

- CadQuery/OpenCascade로 탱크·포트, 냉각수 체적, 배터리 23개의 STEP 3개를 생성했다.
- 가져온 STEP의 판독 상태: `SUPPORTED_23CELL_PORT_VARIANT`; CAD 내보내기·형상 검사: `CAD_EXPORTED_GEOMETRY_CHECKS_PASSED`.
- 탱크: 114 × 121 × 82 mm. 입구·출구 포트 길이: 32 mm.
- 입구: 중심 (89.6, 105.2) mm, 지름 20.0 mm.
- 출구: 중심 (13.6, 15.2) mm, 지름 20.0 mm.
- 배터리 23개, 냉각수–배터리 체적 겹침 0, 냉각수의 외형 이탈 0.
- 가져온 STEP 3개의 SHA-256:

  | 역할 | SHA-256 |
  | --- | --- |
  | `enclosure_with_ports.step` | `e7a1b71e5b5b8f1d1e48ed1ab5aacb059c3ef1777e12612166a9dec73454c7c5` |
  | `coolant.step` | `e986d0b7564c1c07f94344417b08363719b87359ad5f9e65a16911c7d2008ed4` |
  | `battery_cells.step` | `de340bd313e56a0e33eca4d117a036162178a762662625b7e0a7a8b4eff7768e` |

## 생성 메시와 품질

`production_800k` 목표로 실제 메시를 생성했고, 31개 필수 보고서가 포함된
`production_mesh_gate.json`의 최종 상태는 `PRODUCTION_MESH_GATE_PASSED`였다.

| 항목 | 측정값 |
| --- | ---: |
| 전체 셀 | 849,266 |
| 냉각수 / 배터리 셀 | 557,391 / 291,875 |
| 육면체 셀 | 766,173 (90.2159%) |
| 점 / 면 | 945,440 / 2,640,612 |
| 입구 패치 면적 | 0.0003137001 m² |
| 출구 패치 면적 | 0.0003137084 m² |
| 지름 20 mm 원의 이론 면적 | 0.0003141593 m² |
| 최대 비직교도 | 69.445° (설정 한계 70°) |
| 최대 skewness | 2.87545 (설정 한계 5) |

`mesh_target_composition`, `mesh_integrals_pre_split`, 두 영역의 `checkMesh`,
배터리 계면 및 경계층 검사도 통과했다. 메시 체적 적분에서 비양수 셀 체적은 0개였다.
이 수치는 당시 검사 보고서의 기록이며, 원본 데이터를 삭제한 후 다시 측정한 값은 아니다.

## 검증 범위와 재현

여기까지 검증한 것은 **변경된 STEP 판독 → STL 생성 → OpenFOAM 메시·품질 검사**다.
이 형상의 S0 유동장, S1 정상 CHT, S2 1,800초 과도 계산은 수행·검증하지 않았다.
따라서 이 시험으로 새 CAD의 온도 결과나 최종 디지털 트윈 동작을 주장할 수 없다.

입력 변형과 CAD→계산 설정 전달은
[`tests/test_cad_import_workflow.py`](../tests/test_cad_import_workflow.py)의
`test_changed_port_cad_propagates_to_real_solver_bundle`에서 다시 검사할 수 있다.
이 테스트는 임시 디렉터리에 STEP을 새로 만들고 가져와 설정에 반영하지만,
전체 OpenFOAM 메시나 S0/S1/S2를 실행하지는 않는다. 메시 수치의 재검증에는
같은 설정으로 새 작업을 생성해 `run-production-mesh.sh`와 품질 게이트를 다시
실행해야 한다. CAD 생성·검사 및 실행 경로는
[`src/cfd_twin/cad.py`](../src/cfd_twin/cad.py),
[`src/cfd_twin/cad_import.py`](../src/cfd_twin/cad_import.py),
[`src/cfd_twin/workflow_run.py`](../src/cfd_twin/workflow_run.py)에 남아 있다.

정리 대상은 위 두 `build/` 폴더만이었다(정리 전 각각 약 2.9 MB, 406 MB).
기준 CAD, 849,284셀 메시와 S0/S1, 최신 적응형 S2, 현재 뷰어는 변경하지 않았다.
