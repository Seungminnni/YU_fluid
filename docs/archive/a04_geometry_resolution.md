# A-04 형상 해소 기록

출처는 `document/20260826 (1).pptx` slide 2의 상면도·측면도와 300 dpi
contour 측정이다. 좌표계 원점은 상면도 박스 좌하단, x=114 mm 폭,
y=121 mm 길이, z=82 mm 셀 축이다.

## 확정하여 구현한 항목

- 외곽: `configs/baseline_23cell.yaml`의 22점 반시계 폴리곤
- 좌측 챔퍼 측정값: 약 4×11.5 mm
- 상·하 노치: 입구 19 mm, 안쪽 11 mm, 깊이 11.25 mm
- 노치 입구 x: 23–42 mm, 61–80 mm
- 노치 사이 land: 19 mm
- 오른쪽 land: 34 mm
- 셀: x=13.6, 32.6, 51.6, 70.6, 89.6 mm
- 5셀 열 첫 y=15.2 mm, 4셀 열 첫 y=26.45 mm
- pitch/offset: x=19.0, y=22.5, offset=11.25 mm

PPT의 `24 mm` 표기는 오른쪽 land에 놓여 있지만 24 mm를 적용하면 전체
폭이 104 mm가 된다. 명목 구간 합은 `23+19+19+19+34=114 mm`이므로 외곽
폴리곤에는 contour 실측과 Table 1에 일치하는 34 mm를 사용한다. 24 mm는
포트 직경으로도, 외곽 형상값으로도 사용하지 않는다.

## 포트 위치 확정

2026-09-21 사용자 제공 PPT와 완성 모델 사진을 정답 기준으로 다시 대조했다.
상면 inlet Ø19 중심은 `(100.3,107.5)`, 하면 outlet Ø19 중심은
`(13.5,13.5)` mm로 확정했다. 두 포트는 서로 반대 코너에 있고 개구
표면에서 외곽까지의 여유는 inlet x 4.2 mm, 나머지 방향은 4.0 mm다.
기존 end-cell-center 위치는 폐기하며 과거 CAD·메시·CFD 결과와 혼용하지 않는다.

보존하는 과거 비교 profile:

- `configs/port_reversed_end_cell_center.yaml`: 입출구 열 배정 반전
- `configs/port_corner_clearance.yaml`: 현재 기준 위치를 재현하는 과거 sensitivity 파일

외곽/노치/셀과 포트 위치 모두 기준 형상 입력으로 승격한다. 입출구 반전
profile은 오배치 회귀 검사용으로만 남긴다.
