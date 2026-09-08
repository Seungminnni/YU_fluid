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

## 포트 후보와 남은 가정

기본 후보는 상면 inlet Ø19 중심 `(89.6,105.2)`, 하면 outlet Ø19 중심
`(13.6,15.2)`이다. 이는 측면도의 x 위치와 논문 3D 그림을 결합한
`ASSUMPTION_A04`이며 직접 치수값이 아니다.

비교 profile:

- `configs/port_reversed_end_cell_center.yaml`: 입출구 열 배정 반전
- `configs/port_corner_clearance.yaml`: 4 mm corner-clearance 후보

외곽/노치/셀은 입력 형상으로 승격한다. 포트 y와 입구 열 배정은 두
profile의 실제 CFD 결과에서 Figure 11 고속 영역을 비교하고 승인하기
전까지 가정 태그와 `runnable=false`를 유지한다.

