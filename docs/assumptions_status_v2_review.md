# `assumptions_status_v2.md` 검토 결과

검토일: 2026-09-06

## 결론

프로그램의 canonical CAD는 치수가 명시된 PPT를 우선한다. 논문 Figure 8은
결과 렌더이며 투영·창 크기·후처리 scale 영향을 배제할 수 없으므로 형상
source가 아니라 overlay/민감도 자료로 사용한다.

### Canonical baseline에 유지

- `pptx_measured_v1` 22점 외곽
- 노치 mouth 19 mm, inner 11 mm, depth 11.25 mm
- 5-4-5-4-5, x/y/offset = 19.0/22.5/11.25 mm
- inlet/outlet Ø19 mm
- 293 K initial/inlet temperature
- Table 4 물성과 `Re=6,773.5`
- adiabatic wall은 명시적 assumption
- solver Δt는 temporal-convergence pilot으로 결정

### 민감도 profile로 구현

- `paper_fig8_digitized_4mm.yaml`: Figure 8 절대 실측 20/23 mm 배열
- `regular_triangular_3p5mm.yaml`: Table 1의 3.5 mm gap을 우선한 정삼각 배열
- `temperature_293p15K.yaml`: Figure colorbar의 20.000 °C 해석
- `port_reversed_end_cell_center.yaml`: 입출구 대각 배정 반전
- `port_corner_clearance.yaml`: corner-clearance 포트 후보

## 수정이 필요한 주장

1. A-04는 Figure 8 해석만으로 완전 `RESOLVED`가 아니다. 좌하단 동심원과
   좌→우 온도 구배는 좌측 입구를 강하게 지지하지만, port y와 z-face 배치는
   직접 치수가 아니다.
2. A-05/A-18의 293.15 K는 유력한 sensitivity다. Table 2의 직접값 293 K보다
   높은 source 우선순위로 확정하지 않는다.
3. A-06에서 외기 25 °C를 가정하면서 벽 ΔT=0.015 K를 쓴 계산은 모순이다.
   외기가 유체보다 5 K 높으면 같은 식으로 약 3.75 W가 유입된다. 단열 채택은
   논문에 외부 열경계가 없다는 점에 근거한 assumption으로만 둔다.
4. A-07의 Δt=0.1 s는 pilot 시작값으로는 적절하지만, 체류시간/셀 시간상수만으로
   시간수렴을 보장할 수 없다. Δt와 Δt/2 결과 비교 gate를 유지한다.
5. A-13은 baseline Re 계산만 `RESOLVED`다. 논문 Re가 26 °C 점도 때문이라는
   설명은 원 저자 계산자료가 없어 가설이다.
6. 정삼각 3.5 mm profile의 C01/C23 중심은 `(16.3,15.0)`과
   `(94.24228,105.0)`이다. Figure 8 실측 port 좌표를 섞으면 동심 조건이
   깨지므로 각 profile 안에서는 포트와 end-cell 중심을 일치시킨다.

## Source 우선순위

1. 사용자의 현재 결정
2. PPT의 명시 치수/설계선
3. 논문 Table의 직접 수치
4. 논문 Figure의 정성 유동/온도 pattern
5. 논문 Figure 픽셀 실측값
6. 계산상 assumption

