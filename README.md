# 23셀 액침냉각 CFD 디지털 트윈 — 계산 메커니즘과 결과 해석

이 저장소가 다루는 것은 단순한 3D 배터리 그림이나 온도 애니메이션이 아니다. 23개의 원통형 셀이 냉각수 속에서 동시에 열을 만들 때, 그 열이 셀 내부에서 전도되고 셀 표면을 통과해 냉각수로 넘어가며, 냉각수가 셀 사이의 좁은 통로를 지나 출구로 운반하는 전 과정을 유한체적법 CFD로 계산한다. 브라우저 UI는 이 계산을 대신하는 별도의 모델이 아니라, OpenFOAM이 만든 속도·압력·온도장과 모니터 값을 읽어 사람이 탐색할 수 있는 형태로 다시 포장한 결과 뷰어다.

이 문서는 프로그램 메뉴를 나열하기보다 다음 질문에 답하는 데 초점을 둔다.

- 배터리 발열량은 어떤 식으로 만들어지는가?
- 셀 내부의 열과 냉각수의 흐름은 어떤 방정식으로 연결되는가?
- 왜 현재 과도해석에서는 속도를 먼저 수렴시킨 뒤 고정할 수 있는가?
- 계산 격자는 어떤 방식으로 만들며 실제 셀 수와 요소 종류는 무엇인가?
- 어떤 검증을 통과한 값만 UI에 들어가는가?
- 화면의 색, 유선, 단면, 그래프는 원본 CFD 데이터와 어떤 관계인가?

현재 날짜 기준으로 표준 216,577셀 결과와 보존된 정밀 1,316,854셀 결과가 모두 0–1,800 s 계산을 완료했으며, 뷰어는 두 메시의 데이터를 서로 섞지 않고 별도로 선택하게 되어 있다.

---

## 1. 전체 메커니즘을 한 번에 보기

계산의 인과관계는 다음과 같다.

```text
전류·저항 또는 지정 체적발열 q'''(t)
                    │
                    ▼
23개 batterySolid 영역 전체에서 열 생성
ρs Cps ∂Ts/∂t = ∇·(ks∇Ts) + q'''
                    │
                    ▼
셀–냉각수 CHT 계면
온도와 법선 열유속을 양쪽에서 연속으로 결합
                    │
                    ▼
coolant 영역의 대류–확산 에너지 수송
ρf Cpf(∂Tf/∂t + U·∇Tf) = ∇·(keff∇Tf)
                    ▲
                    │
연속방정식 + 운동량방정식 + k-ω SST로 구한 U, p, k, ω
                    │
                    ▼
출구 엔탈피, 압력강하, 셀별 온도, 단면 T/U, 잔차 저장
                    │
                    ▼
오차 한계를 전수검증한 uint16 양자화 + gzip 포장
                    │
                    ▼
브라우저에서 3D 셀 색, 유선, XY/XZ/YZ 단면, 1초 결과 재생
```

중요한 점은 UI가 온도를 추정하지 않는다는 것이다. 완료된 현재 결과에서는 OpenFOAM이 매 정수 초에 기록한 직접 셀 값만 사용하며, 없는 공간이나 시간을 임의로 메우지 않는다. 화면의 부드러운 색 연결은 렌더링 표현일 뿐 새로운 CFD 값을 만드는 공간 보간이 아니다.

---

## 2. 해석 대상의 실제 형상

### 2.1 좌표계와 하우징

좌표 원점은 상면 형상의 좌하단이며, `x`는 폭, `y`는 길이, `z`는 셀 축 방향이다.

| 항목 | 기준값 |
| --- | ---: |
| 하우징 외형 크기 | 114 × 121 × 82 mm |
| 하우징 상면 형상 | 22점 반시계 폴리곤 |
| 노치 입구 폭 | 19 mm |
| 노치 안쪽 폭 | 11 mm |
| 노치 깊이 | 11.25 mm |
| 셀 개수 | 23개 |
| 셀 크기 | Ø19 × 68 mm |
| 배열 | 5–4–5–4–5 엇갈림 배열 |
| 입구/출구 | 각각 Ø19 mm |
| 외부 포트 길이 | 각각 32 mm |

하우징은 단순한 114 × 121 mm 직사각형이 아니다. PPT 도면에서 측정한 챔퍼와 상·하 노치를 22개 꼭짓점으로 정의하고 82 mm만큼 압출한다. 도면의 `24 mm` 표기는 전체 폭 114 mm와 모순되므로 형상값이나 포트 직경으로 사용하지 않았고, 측정 형상과 전체 폭에 맞는 오른쪽 34 mm land를 채택했다.

### 2.2 셀 배치와 ID

셀 중심은 다음 규칙으로 생성한다.

$$
x_j = 13.6 + 19.0j\;\mathrm{mm}, \qquad j=0,1,2,3,4
$$

5개가 들어가는 열의 `y` 중심은 15.2, 37.7, 60.2, 82.7, 105.2 mm이고, 4개 열은 여기에 11.25 mm의 엇갈림 오프셋을 적용해 26.45, 48.95, 71.45, 93.95 mm가 된다. 모든 셀 중심의 `z`는 41 mm다.

| 열 | x 중심 [mm] | 셀 ID | y 중심 [mm] |
| ---: | ---: | --- | --- |
| 1 | 13.6 | C01–C05 | 15.2, 37.7, 60.2, 82.7, 105.2 |
| 2 | 32.6 | C06–C09 | 26.45, 48.95, 71.45, 93.95 |
| 3 | 51.6 | C10–C14 | 15.2, 37.7, 60.2, 82.7, 105.2 |
| 4 | 70.6 | C15–C18 | 26.45, 48.95, 71.45, 93.95 |
| 5 | 89.6 | C19–C23 | 15.2, 37.7, 60.2, 82.7, 105.2 |

계산된 최소 대각 셀 표면 간격은 3.0808 mm다. 좌측, 우측, 하측, 상측 직선 경계에 대한 대표 여유는 각각 4.1, 14.9, 5.7, 6.3 mm이며, 셀 길이가 68 mm이므로 위아래 축 방향 여유는 각각 7 mm다. 실제 충돌 판정은 직사각형 치수만 보지 않고 22점 외곽 폴리곤의 각 선분까지의 최소거리로 검사한다.

### 2.3 포트와 남아 있는 A-04 가정

기준안의 입구는 상면 `z=82 mm`에서 바깥쪽으로 32 mm 연장되고 중심은 `(89.6, 105.2) mm`다. 출구는 하면 `z=0 mm`에서 아래쪽으로 32 mm 연장되며 중심은 `(13.6, 15.2) mm`다. 입구 속도벡터는 하우징 안쪽을 향하는 `(0, 0, -0.4) m/s`다.

포트 직경 Ø19 mm와 상·하면 배치는 도면 근거가 있지만, 정확한 `y` 위치와 어느 끝 열을 입구로 둘지는 직접 치수가 아니라 도면과 논문 3D 그림을 결합한 `ASSUMPTION_A04`다. 그래서 기준안 외에도 입출구 반전과 코너 4 mm 여유 배치를 별도 overlay로 보존한다. 현재 계산이 수치적으로 통과했다는 사실과 A-04 형상 가정이 실험적으로 확정됐다는 주장은 서로 다르다.

---

## 3. 재료 물성과 경계조건

### 3.1 기준 물성

냉각수는 탈이온수, 배터리는 내부 세부층을 균질화한 등방성 고체로 모델링한다.

| 물성 | 냉각수 | 배터리 고체 |
| --- | ---: | ---: |
| 밀도 `ρ` | 998.2 kg/m³ | 2,000 kg/m³ |
| 비열 `Cp` | 4,182 J/(kg·K) | 900 J/(kg·K) |
| 열전도도 `k` | 0.598 W/(m·K) | 27 W/(m·K) |
| 동점성이 아닌 동적점도 `μ` | 0.00112 Pa·s | 해당 없음 |
| 물성의 온도 의존성 | 없음 | 없음 |
| 복사 | 사용하지 않음 | 사용하지 않음 |

OpenFOAM 열역학 모델은 냉각수에 `heRhoThermo + rhoConst + hConst + const`, 고체에 `heSolidThermo + rhoConst + hConst + constIso`를 사용한다. 즉 밀도, 비열, 점도, 열전도도가 모두 상수다.

### 3.2 경계조건

| 경계 | 속도 | 압력 | 온도 |
| --- | --- | --- | --- |
| 입구 | `(0,0,-0.4) m/s` 고정 | `fixedFluxPressure` | 293.15 K 고정 |
| 출구 | 역류 안전 `inletOutlet` | 101,325 Pa 절대압 기준 고정 | 역류 안전 `inletOutlet` |
| 하우징 외벽 | no-slip | 법선 압력구배 처리 | 단열 `zeroGradient` |
| 배터리 표면 | no-slip | 법선 압력구배 처리 | 유체–고체 결합 온도 경계 |
| 중력 | `g=(0,0,0)` |  |  |

기본 YAML에는 논문 표의 반올림값인 293 K가 남아 있지만, 현재 표준 및 정밀 1초 결과는 입구·유체 초기·고체 초기 온도를 모두 293.15 K, 즉 정확히 20.000 °C로 재설정해 계산했다. 현재 뷰어는 여기에 임의 오프셋을 더하지 않고 `T[°C]=T[K]-273.15`만 수행한다.

### 3.3 유량, Reynolds 수와 입구 난류량

직경 19 mm인 원형 입구 면적과 체적유량은

$$
A_\mathrm{in}=\frac{\pi D^2}{4}=2.83528737\times10^{-4}\;\mathrm{m^2}
$$

$$
\dot V=A_\mathrm{in}U_\mathrm{in}=1.13411495\times10^{-4}\;\mathrm{m^3/s}
$$

이다. 이는 약 6.805 L/min이며 질량유량은 약 0.113207 kg/s다. 구현 물성으로 계산한 Reynolds 수는

$$
Re=\frac{\rho UD}{\mu}=6{,}773.5
$$

다. 참고 논문에 적힌 8,782와는 22.9% 차이가 있다. 논문의 Reynolds 수를 역산하면 점도는 `8.63849×10⁻⁴ Pa·s`가 되며, 이 값은 기준안을 몰래 바꾸지 않고 `viscosity_re8782_sensitivity.yaml`에서만 사용한다.

입구 난류강도는 5%다. 코드가 만드는 난류 운동에너지와 비소산율은

$$
k=\frac{3}{2}(UI)^2=6.0\times10^{-4}\;\mathrm{m^2/s^2}
$$

$$
\ell=0.07D=1.33\times10^{-3}\;\mathrm{m},\qquad
\omega=\frac{\sqrt{k}}{C_\mu^{1/4}\ell}=33.6251\;\mathrm{s^{-1}}
$$

로 초기화한다. 난류 폐쇄모델은 RANS `k-ω SST`이며, 난류 열확산에는 `Prt=0.85`를 사용한다.

---

## 4. 배터리 발열식

### 4.1 현재 기준 결과가 사용하는 식

기준 계산은 23개 셀 모두에 동일한 상수 체적발열을 적용한다.

$$
q''' = 16{,}325\;\mathrm{W/m^3}
$$

원통 셀 하나의 해석상 체적은

$$
V_\mathrm{cell}=\pi\left(\frac{0.019}{2}\right)^2(0.068)
=1.92799541\times10^{-5}\;\mathrm{m^3}
$$

이므로 이상 CAD 체적 기준 셀 하나의 발열과 23개 총발열은

$$
\dot Q_\mathrm{cell}=q'''V_\mathrm{cell}=0.314745\;\mathrm{W}
$$

$$
\dot Q_\mathrm{total}=23\dot Q_\mathrm{cell}=7.239141\;\mathrm{W}
$$

이다. OpenFOAM에서는 `batterySolid` 전체에 `scalarSemiImplicitSource`, `selectionMode all`, `volumeMode specific`으로 `h` 방정식에 이 열원을 넣는다. 실제 에너지 검증에는 이상 CAD 체적이 아니라 완성된 메시의 고체 체적을 사용한다. 표준 메시의 적분 고체 체적은 `4.40811633×10⁻⁴ m³`이므로 솔버가 실제로 받는 적분 열원은 7.196250 W다. CAD값과 메시값의 작은 차이는 곡면을 유한한 셀로 표현하면서 생긴다.

이 총열량과 기준 질량유량만으로 계산한 이상적인 냉각수 벌크 온도 상승은

$$
\Delta T_\mathrm{bulk,ideal}
=\frac{\dot Q_\mathrm{total}}{\dot m C_p}
=0.015291\;\mathrm{K}
$$

이다. 따라서 결과에서 출구 평균온도가 입구보다 약 0.015 K 높은 것은 발열이 없어서가 아니라, 약 7.2 W의 열을 0.113 kg/s의 물이 운반하기 때문에 예상되는 크기다.

### 4.2 지원하는 세 가지 발열 모드

| 모드 | 식 | 용도 |
| --- | --- | --- |
| `constant_qvol` | `q'''(t)=상수` | 현재 기준 결과 |
| `table_qvol` | CSV의 `time_s, qvol_W_m3`를 구간별 선형 보간 | 사용자가 측정·정의한 시간 발열 |
| `electrical_joule` | `q'''(t)=I(t)²R(t)/Vcell` | 전류·저항 기반 Joule 대체모델 |

시간표 범위 밖의 값은 `outside_policy=hold`이면 끝값을 유지하고, `error`이면 계산 준비를 실패시킨다. OpenFOAM용 `fvOptions`와 Fluent용 `DEFINE_SOURCE` UDF를 같은 소스 정의에서 만들 수 있지만, 이것이 참고 논문의 공개되지 않은 원본 UDF를 복원했다는 뜻은 아니다.

2 Ah 셀의 2C 방전을 단순 계산하면 명목전류는 4 A다. 상수 발열 0.314745 W를 순수 Joule 열로 역산한 등가저항은 19.6716 mΩ이지만 측정 저항이 아니며, 3.7 V × 4 A의 전기입력 14.8 W를 전부 열로 간주하지도 않는다. 현재 `electrical_joule` 모드에는 가역 엔트로피열 `-IT(dUoc/dT)`와 반응·혼합열이 포함되지 않는다.

향후 물리 기반 발열을 넣을 때의 의도된 순서는 다음과 같다.

```text
시간 → 전류와 SOC → Ri(SOC,T), ΔS(SOC,T)
     → 셀당 발열 Q̇cell → 체적발열 q'''=Q̇cell/Vcell
```

이 경로를 기준 결과로 사용하려면 SOC 범위, 저항과 엔트로피 상관식의 출처·부호·단위, 23개 영역의 `∫q'''dV`, 상수 발열 회귀시험을 별도로 통과해야 한다.

---

## 5. 실제로 푸는 CFD 방정식

### 5.1 냉각수 유동

상수 밀도 조건에서 질량보존은 사실상 비압축성 연속방정식이 된다.

$$
\nabla\cdot(\rho_f\mathbf U)=0
$$

운동량은 압력, 점성응력과 난류 유효점성에 의해 결정된다.

$$
\frac{\partial(\rho_f\mathbf U)}{\partial t}
+\nabla\cdot(\rho_f\mathbf U\mathbf U)
=-\nabla p+\nabla\cdot\boldsymbol\tau_\mathrm{eff}
$$

중력과 부력은 0이며, 밀도와 점도가 온도에 따라 변하지 않는다. 배터리 표면과 외벽에는 no-slip을 적용한다. `k-ω SST`가 벽 근처 `k`, `ω`, 난류점성 `νt`를 계산하고 이것이 운동량과 난류 열확산에 들어간다.

### 5.2 냉각수 에너지

냉각수에는 대류와 전도가 동시에 존재한다.

$$
\rho_f C_{p,f}
\left(\frac{\partial T_f}{\partial t}+\mathbf U\cdot\nabla T_f\right)
=\nabla\cdot(k_\mathrm{eff}\nabla T_f)
$$

여기서 `keff`는 분자 열전도와 난류 열확산을 함께 포함한다. 냉각수 자체에는 체적발열을 넣지 않는다. 입구는 293.15 K로 고정되고, 외벽은 단열이므로 배터리에서 나온 열은 저장되거나 냉각수와 함께 출구로 빠져나가야 한다.

### 5.3 배터리 고체 에너지

배터리는 움직이지 않는 균질 고체이므로 대류항 없이 전도와 내부발열만 푼다.

$$
\rho_s C_{p,s}\frac{\partial T_s}{\partial t}
=\nabla\cdot(k_s\nabla T_s)+q'''
$$

현재 모델은 원통 셀을 하나의 등방성 재료로 본다. 젤리롤의 방향별 열전도도, 캔·탭·전극·분리막의 개별 재료, 접촉열저항은 분해하지 않는다. 따라서 이 결과는 “주어진 균질 유효물성을 가진 셀”의 냉각 거동이다.

### 5.4 유체–고체 계면

23개 셀마다 냉각수 쪽 패치와 고체 쪽 패치를 한 쌍으로 만든다. 계면에서는 개념적으로

$$
T_f=T_s
$$

$$
-\mathbf n_f\cdot k_{\mathrm{eff}}\nabla T_f
=\mathbf n_s\cdot k_s\nabla T_s
$$

를 만족하도록 `compressible::turbulentTemperatureRadCoupledMixed` 경계가 양쪽 온도와 열유속을 결합한다. 별도의 경험식 대류계수를 입력하는 방식이 아니다. 벽면 열전달계수는 계산된 경계층 속도장과 온도구배의 결과로 나타난다.

---

## 6. 왜 현재 결과는 frozen-flow 과도 CHT인가

현재 모델에서는 다음 조건을 모두 고정했다.

- 냉각수 `ρ`, `μ`, `Cp`, `k`가 상수다.
- 중력이 0이므로 부력과 자연대류가 없다.
- 열원은 에너지방정식에만 들어가며 운동량방정식에 직접 힘을 주지 않는다.
- 입구 유량과 모든 유동 경계조건이 시간에 따라 변하지 않는다.

따라서 온도가 달라져도 운동량방정식의 계수가 달라지지 않는다. 한 번 충분히 수렴한 `U(x)`, `p(x)`, `k(x)`, `ω(x)`는 이후 열원 이력과 무관하게 동일하며, 매 시간마다 같은 유동장을 다시 풀어도 같은 해를 반복 계산하게 된다. 이 때문에 현재 기준 계산은 다음 순서로 수행한다.

1. 발열을 끈 정상상태 cold-flow에서 `U`, `p`, `p_rgh`, `k`, `ω`를 2,000회까지 수렴시킨다.
2. 수렴한 유동장을 이용해 상수 발열 정상상태 CHT를 풀고 최종 열평형과 온도 한계를 확인한다.
3. 유체·고체 온도만 293.15 K로 다시 초기화한다.
4. 검증된 cold-flow의 `U`, `p`, `p_rgh`, `k`, `ω`, `nut`, `alphat`, `phi`를 과도 케이스의 0초장으로 복사한다.
5. `chtMultiRegionFoam`의 `PIMPLE.frozenFlow yes`로 1초마다 유체와 고체 에너지방정식을 결합해 1,800초까지 전진한다.

즉 “frozen-flow”는 온도를 고정한다는 뜻이 아니라 유동만 고정하고 온도장은 계속 시간 적분한다는 뜻이다. 이는 단방향 열–유동 결합이지만, 유체와 고체의 에너지는 매 시간마다 계면에서 양방향으로 결합되는 CHT다.

### full-transient가 반드시 필요한 경우

다음 항목 중 하나라도 도입하면 온도가 유동에 되먹임되므로 `frozenFlow no`인 전체 과도 PIMPLE 경로가 필요하다.

- 온도 의존 밀도 또는 점도
- Boussinesq 부력, 중력, 자연대류
- 온도나 SOC에 따라 유량·압력 경계가 바뀌는 제어
- 발열 또는 전기화학 상태가 운동량원과 직접 연결되는 모델

열전도도나 비열만 온도에 따라 변하면 에너지방정식의 비선형 반복은 필요하지만, 그 자체만으로 운동량 재계산이 필수는 아니다.

전체 과도 경로는 한 내부 시간 단계마다 최소 3회의 PIMPLE outer correction으로 `U`, `p_rgh`, `k`, `ω`, 유체 `h`, 고체 `h`를 함께 갱신한다. 초기 최대 `Δt`는 0.1 s, `maxCo=5`이며 0.05 s 비교 케이스도 있다. 표준 메시의 실측 파일럿에서는 0.736243 s까지 가는 동안 215 time step과 645 outer correction이 필요했고, 안정 `Δt`가 약 0.003425 s였다. 이 속도를 1,800 s까지 단순 외삽하면 약 5.6일이므로 1 s, 10 s, 60 s 단계 검증과 `Δt/2` 비교가 끝나기 전에는 전체 과도 결과로 채택하지 않는다.

UI의 “1초 프레임”과 전체 과도 솔버의 내부 `Δt`는 같은 개념이 아니다. 솔버는 Courant 수에 맞춰 1초 안에서 수백 개의 작은 단계를 밟을 수 있고, UI용 출력만 정수 초에 저장할 수 있다.

---

## 7. 수치해석 방법

| 구분 | 정상상태 단계 | 현재 과도 열 단계 | 전체 과도 검증 경로 |
| --- | --- | --- | --- |
| OpenFOAM 솔버 | `chtMultiRegionSimpleFoam` | `chtMultiRegionFoam` | `chtMultiRegionFoam` |
| 압력–속도 알고리즘 | SIMPLE | PIMPLE, `frozenFlow yes` | PIMPLE, `frozenFlow no` |
| 시간항 | `steadyState` | 1차 Euler, `Δt=1 s` | 1차 Euler, adaptive `Δt≤0.1 s` |
| 유동 재계산 | 수행 | 수행하지 않음 | 매 내부 단계 수행 |
| 비직교 보정 | 유체 2회, 고체 1회 | 1회 | 1회 |
| 병렬 분할 | Scotch, 기본 10 MPI rank | Scotch, 기본 10 MPI rank | Scotch, 기본 10 MPI rank |

공간 이산화는 유한체적법이다. `grad(U)`는 `cellLimited Gauss linear 1`, 대류항 `div(phi,U)`, `div(phi,h)`, `div(phi,k)`, `div(phi,omega)`는 bounded Gauss upwind를 사용한다. 확산항은 `Gauss linear corrected`, 면 보간은 `linear`, 법선구배는 `corrected`다. 곡면 적합 혼합 메시에서 안정성을 우선한 1차 대류 스킴이므로, 더 높은 차수 스킴을 사용한 결과와 같다고 가정해서는 안 된다.

압력 계열은 GAMG, 고체 엔탈피는 PCG+DIC, 나머지 유체 방정식은 `smoothSolver + symGaussSeidel`을 사용한다. 정상상태 수렴에서는 방정식별 raw OpenFOAM initial residual뿐 아니라 마지막 200회 동안의 압력강하, 질량보존, 온도 변화와 에너지수지를 함께 본다. 잔차 하나만 작다는 이유로 계산을 통과시키지 않는다.

---

## 8. 메시를 만드는 방식

### 8.1 CAD에서 영역을 나누는 과정

CadQuery/OpenCascade로 22점 상면을 82 mm 압출하고, 위·아래 Ø19 mm 포트를 합쳐 하나의 외부 유체 용기를 만든다. 그 체적에서 23개의 Ø19 × 68 mm 원통을 빼면 연결된 냉각수 체적이 된다. 동시에 원통 23개는 고체 배터리 형상으로 별도 보존한다.

CAD 단계에서 다음을 검사한다.

- 외부 하우징+포트가 하나의 solid인가?
- 냉각수 공간이 하나로 연결되어 있는가?
- 배터리 solid가 정확히 23개인가?
- 원통 체적과 표면적이 해석식에 대해 `1×10⁻⁶` 상대오차 이내인가?
- 입·출구 면적이 `πD²/4`와 일치하는가?

STEP은 형상 확인용으로, 미터 단위의 개별 STL 27개는 생산 메시용으로 내보낸다. STL 구성은 `outerDomain`, `inlet`, `outlet`, `outerWall`, `battery_C01`–`battery_C23`이다.

### 8.2 표준 메시 생성 순서

1. `blockMesh`가 형상보다 사방 5 mm 큰 배경 박스를 만든다. 범위는 `(-5,-5,-37)`–`(119,126,119) mm`, 분할은 25 × 27 × 32, 명목 셀 크기는 5.0 mm다.
2. `surfaceFeatureExtract`가 하우징과 각 원통의 특징선을 추출한다.
3. `snappyHexMesh`의 castellated 단계가 STL 안팎을 구분하고, snap 단계가 격자점을 실제 곡면으로 이동시킨다.
4. 외부 하우징과 포트는 refinement level 1/2, 배터리 표면은 0/1, 배터리 내부는 level 1로 세분한다.
5. 23개 배터리의 냉각수 쪽 표면에 경계층 prism을 최대 10층 요청한다. 첫 층 두께는 14 μm, 성장비는 1.2, 최소 허용두께는 3 μm다.
6. 셀 표면을 바꾸지 않으면서 고체 내부 해상도를 높이기 위해 각 배터리 중심의 반경 4 mm 코어를 한 단계 추가 세분한다. 위·아래 끝에서 6 mm는 제외해 CHT 계면이 변하지 않게 한다.
7. `topoSet`이 23개 `battery_Cxx` cellZone을 제외한 나머지 모든 셀을 `coolant` zone으로 지정한다.
8. `createPatch`가 닫힌 외부 표면에서 정확한 원형 입구·출구 캡과 외벽을 분리한다.
9. `splitMeshRegions -cellZonesOnly -addZones -useFaceZones`가 계산영역을 `coolant`와 `batterySolid` 두 region으로 나누고 23쌍의 mapped CHT 계면을 만든다.
10. 두 region의 최종 `checkMesh`, 체적·면적·zone·계면·prism 검증을 모두 통과해야 솔버 실행 스크립트가 허용된다.

메시는 기본적으로 직렬로 만든다. 이 거친 곡면 형상에서는 `snappyHexMesh`의 prism 종료 위치가 MPI 분할에 따라 달라질 수 있기 때문이다. 필요하면 `OPENFOAM_MESH_MPI_RANKS`로 병렬 메시를 명시할 수 있지만, 계산 솔버는 별도로 기본 10 rank를 사용한다.

### 8.3 왜 전부 육면체가 아닌가

이 메시는 “hexahedral-based”이지 “all-hexa”가 아니다. 배경은 육면체지만 원통 곡면으로 점을 snap하고, 잘린 셀을 정리하며, 벽면 prism층과 refinement 전이부를 만들면 prism, wedge, tet-wedge, 일반 polyhedron이 생긴다. 원통 주위까지 100% 육면체로 만들려면 별도의 O-grid 또는 다중 블록 토폴로지가 필요하다.

요소 의미는 다음과 같다.

- `hexahedra`: 6면 육면체. 배경과 내부 대부분을 구성한다.
- `prisms`: 경계층을 따라 늘어난 삼각기둥 계열 셀이다.
- `wedges`: 축이나 곡면 전이에서 생기는 쐐기형 셀이다.
- `tet-wedges`: 사면체와 쐐기의 중간 위상으로 snap·전이부에서 생긴다.
- `polyhedra`: 면 수가 고정되지 않은 절단 다면체로 곡면 적합부와 refinement 전이에 존재한다.

### 8.4 실제 메시 개수와 종류

| 메시 | 상태 | 전체 셀 | 냉각수 셀 | 배터리 고체 셀 | 점 |
| --- | --- | ---: | ---: | ---: | ---: |
| 표준 `single` | 현재 생산 프로파일, 계산 완료 | 216,577 | 159,107 | 57,470 | 252,907 |
| 정밀 `L3` | 과거 정밀 프로파일, 결과 보존 | 1,316,854 | 1,061,438 | 255,416 | 1,403,119 |

| 요소 종류 | 표준 개수 | 표준 비율 | 정밀 개수 | 정밀 비율 |
| --- | ---: | ---: | ---: | ---: |
| 육면체 | 185,013 | 85.426% | 1,245,779 | 94.603% |
| prism | 4,452 | 2.056% | 14,981 | 1.138% |
| wedge | 301 | 0.139% | 790 | 0.060% |
| tet-wedge | 518 | 0.239% | 1,005 | 0.076% |
| polyhedron | 26,293 | 12.140% | 54,299 | 4.123% |
| pyramid / tetrahedron | 0 | 0% | 0 | 0% |

현재 생성기가 받는 프로파일 이름은 `single` 하나뿐이다. 표준 메시의 설계 목표는 전체 207,390셀, 고체 70,840셀, 유체 136,550셀, 점 221,878개였지만, 이 수는 생성 전 라벨이 아니라 허용오차를 둔 목표다. 실제 결과는 216,577셀로 총 셀 목표보다 4.430% 많고, 전체 ±10%, 구성요소 ±20%, 육면체 비율 80% 이상이라는 gate를 통과했다.

정밀 `L3`는 현재 CLI의 선택지가 아니라 과거 생성과 계산의 provenance를 보존한 디렉터리명이다. 당시 2.4 mm 배경, 52 × 55 × 65 배경 분할, 10개 prism층, 14 μm 첫 층, 10 MPI rank를 사용했다. UI에서 “정밀 결과”로 보이지만, 표준 결과가 없는 경우 정밀 필드를 표준 결과로 바꿔 표시하는 대체 동작은 없다.

### 8.5 prism층은 “모든 면에 정확히 10층”이라는 뜻이 아니다

10층은 생성 요청값이다. `snappyHexMesh`는 음의 체적, 과도한 skewness, 국부 곡률이나 인접 전이 문제를 피하기 위해 일부 면에서 층 수를 줄이거나 extrusion을 종료할 수 있다. 따라서 단순히 설정파일에 `nSurfaceLayers 10`이 있다는 사실만으로 경계층 메시를 통과시키지 않는다.

표준 메시의 실제 측정값은 다음과 같다.

| 항목 | 실제값 | gate |
| --- | ---: | ---: |
| 추가 layer 내부면 | 54,666개 | 0보다 커야 함 |
| 23셀 평균 표면 coverage | 97.756% | 각 셀 75% 이상 |
| 최소 표면 coverage | C11, 77.980% | 75% 이상 |
| 23셀 평균 wanted thickness 비율 | 80.035% | 각 셀 45% 이상 |
| 최소 wanted thickness 비율 | C11, 48.6% | 45% 이상 |
| 배터리 표면 `y+<1` 면적비 | 95.3196% | 95% 이상 |
| 면적가중 y+ 평균 / p95 | 0.373565 / 0.952719 | 결과 진단 |

`y+`는 실제로 유동을 푼 뒤에만 알 수 있으므로 메시 생성 로그가 아니라 cold-flow 결과에서 다시 계산한다. 국부 최대 y+는 19.41이지만 전체 면적의 95% 이상을 1 아래로 두는 것이 이 모델의 명시된 합격기준이다. 최대값 하나만 보고 전체 벽면 해상도를 판단하지 않는다.

### 8.6 메시 검증 gate

생성된 메시가 다음 조건을 모두 만족해야 솔버 파일럿이 시작된다.

- 외부 STL과 23개 배터리 STL이 닫혀 있고 정상인가?
- `coolant`와 `battery_C01`–`battery_C23` zone이 정확히 존재하는가?
- 216,577개 셀이 빠짐없이 단 하나의 재료 zone에만 속하는가?
- 메시 적분 배터리·냉각수 체적이 CAD값의 2% 이내인가?
- 각 배터리 체적이 해석 원통값의 5% 이내인가?
- 입·출구 면적이 원형 해석값의 2% 이내인가?
- 23개 계면이 냉각수와 고체 양쪽에 모두 존재하는가?
- 계면 쌍의 면적이 수치 정밀도 `1×10⁻⁹` 이내에서 같은가?
- 각 계면 면적이 원통 해석값의 6% 이내인가?
- 최종 region의 최대 비직교도가 75° 이하, 최대 skewness가 20 이하인가?
- 최소 셀 체적이 양수이고 음의 체적·fatal error가 없는가?
- 육면체 비율, prism coverage와 실제 y+ gate를 통과하는가?

표준 메시의 최종 최대 비직교도는 74.3759°, 최대 skewness는 4.27525, 최소 셀 체적은 `7.44×10⁻¹² m³`다. 정밀 메시의 값은 각각 74.9999°, 9.51634, `1.27×10⁻¹² m³`다. region 분리 전 내부 CHT 면은 얇은 유체층과 큰 고체셀을 직접 이으므로 진단상 불량면이 나타날 수 있다. 그 내부 stencil은 `splitMeshRegions` 후 사라지므로, 권위 있는 품질판정은 분리된 `coolant`와 `batterySolid` 양쪽 최종 메시에서 수행한다.

---

## 9. 계산과 검증의 단계별 의미

### 9.1 1단계 — cold-flow

열원을 끄고 `potentialFoam`으로 속도장을 초기화한 뒤 `chtMultiRegionSimpleFoam`으로 정상 유동을 수렴시킨다. 이 단계에서 확인하는 것은 다음과 같다.

- `U`, `p_rgh`, `k`, `ω`, 유체·고체 `h`의 실제 OpenFOAM 초기잔차
- 마지막 200회 구간에서 입·출구 질량 불균형 0.1% 미만
- 같은 구간의 압력강하 span이 평균의 0.5% 미만
- `U`, `T`, `p`, `k`, `ω`가 유한하며 최종 난류장이 음수가 아님
- 23개 배터리 표면의 실제 y+ gate 통과

질량 불균형은

$$
\varepsilon_m=
\frac{|\dot m_\mathrm{in}+\dot m_\mathrm{out}|}
{0.5(|\dot m_\mathrm{in}|+|\dot m_\mathrm{out}|)}
$$

로 계산한다. 표준 결과는 2,000회에서 통과했고, 마지막 감시구간 평균 압력강하는 260.412879 Pa, 최대 질량 불균형은 `2.804×10⁻⁸`이다.

### 9.2 2단계 — 정상상태 CHT

통과한 cold-flow를 복사하고 배터리 열원을 켜 정상상태의 유동과 에너지를 함께 수렴시킨다. 마지막 200회에서 배터리 평균온도와 최고온도의 변동폭이 각각 0.001 K 미만이어야 하며, 평균 에너지 불균형은 2% 미만, 최대는 5% 미만이어야 한다.

냉각수가 가져간 열은 절대 엔탈피 기준값의 영향을 피하기 위해 공통 관통 질량유량과 입출구 온도차로 계산한다.

$$
\dot Q_\mathrm{out}
=C_{p,f}\frac{|\dot m_\mathrm{in}|+|\dot m_\mathrm{out}|}{2}
\left(T_\mathrm{out}-T_\mathrm{in}\right)
$$

$$
\varepsilon_E=
\frac{|\dot Q_\mathrm{out}-q'''V_{s,mesh}|}
{q'''V_{s,mesh}}
$$

표준 정상 CHT는 2,000회에서 통과했으며 최종 배터리 체적평균 293.256929 K, 메시 최고 293.341415 K, 감시구간 평균 에너지 불균형 0.2363%였다.

### 9.3 3단계 — 0–1,800 s 과도 에너지

온도장을 293.15 K로 되돌리고 검증된 유동장을 고정한 채 1초씩 1,800번 에너지방정식을 푼다. 다음 조건이 모두 필요하다.

- 1–1,800 s의 1초 telemetry가 빠짐없이 존재
- 유체와 고체의 온도장이 모든 요구시각에 원본 또는 완전한 MPI 분할 형태로 존재
- 마지막 60초에 최소 61개 표본 존재
- 평균 에너지 불균형 2% 미만, 최대 5% 미만
- 질량 불균형 0.1% 미만
- 배터리 평균온도가 수치 허용범위에서 단조 증가
- 1,800 s 평균·최고온도가 정상 CHT 값과 각각 0.01 K 이내
- 계산 로그가 fatal error 없이 `End`로 종료

현재 표준 결과의 과도 residual CSV에는 1–1,800 s의 1,800개 에너지 반복이 그대로 들어 있다. 이는 Fluent scaled residual이 아니라 OpenFOAM 선형솔버의 raw initial residual이다. 완전 PIMPLE 계산을 수행하면 x축은 각 방정식의 내부 선형 반복 횟수 합이 아니라 누적 outer-correction 횟수로 정의한다.

---

## 10. 현재 계산 결과

두 결과 모두 각자의 메시에서 직접 계산되었다. 아래 온도 평균은 UI가 사용하는 정의에 맞춰 출구는 질량유량 가중, 배터리 표면은 계면 면적가중, 내부는 cellZone 체적가중이다.

| 1,800 s 항목 | 표준 216,577셀 | 정밀 1,316,854셀 |
| --- | ---: | ---: |
| 출구 질량가중 평균온도 | 20.015261 °C | 20.015266 °C |
| 배터리 내부 체적평균 | 20.106335 °C | 20.076480 °C |
| 배터리 표면 면적평균 | 20.098935 °C | 20.068906 °C |
| 배터리 메시 최고온도 | 20.193303 °C, C05 | 20.139094 °C |
| 고정 유동장 UI 최대속도 | 0.596063 m/s | 0.633058 m/s |
| cold-flow 평균 압력강하 | 260.412879 Pa | 262.362376 Pa |
| 배터리 면적 중 `y+<1` | 95.3196% | 95.6381% |
| 최종창 에너지 불균형 | 0.018936% | 0.040675% |

표준 과도 결과의 배터리 평균과 최고는 정상상태 결과에서 각각 −0.000579 K, +0.001887 K 차이로 0.01 K gate를 통과했다. 정밀 결과는 기준온도 차이를 보정한 정상값에 대해 각각 +0.000095 K, +0.004163 K 차이다.

표준과 정밀 결과의 셀 최고온도 차이는 메시 해상도, 곡면 표현, zone 체적과 국부 열전달 분포가 다르기 때문에 생긴다. 두 데이터셋을 화면에서 선택할 수 있다는 사실만으로 이 둘을 새로운 격자독립성 fine-pair로 선언하지 않는다. 역사적 L2–L3 격자독립성 판단은 별도 보고서에서 수행됐고, 현재 표준 `single`은 실행비용을 낮춘 별도의 생산 프로파일이다.

---

## 11. CFD 결과가 UI가 되는 과정

### 11.1 원본값 수집

과도 계산은 매 정수 초마다 function object로 다음 값을 기록한다.

- 입구·출구 질량유량, 면적평균 압력, `phi` 가중 평균온도
- C01–C23 각각의 내부 체적평균 `T`, 내부 `max(T)`
- C01–C23 각각의 표면 최소·면적평균·최대 `T`
- `U`, `T`, `p`, `k`, `ω`의 field bounds
- 5개 XY 온도 단면과 5개 XZ 온도 단면
- 배터리 표면 바로 안쪽의 직접 온도 표본

XY 단면은 `z=15%, 32.5%, 50%, 67.5%, 85%`에 놓이며 각 평면은 114 × 121 = 13,794개 후보점이다. XZ 단면은 `y=15%, 32.5%, 50%, 67.5%, 85%`에 놓이며 각 평면은 114 × 55 = 6,270개 후보점이다.

세로 방향을 반대로 볼 수 있도록 뷰어 제작 단계에서 `x=15%, 32.5%, 50%, 67.5%, 85%`의 YZ 단면도 추가한다. 이 YZ 냉각수·고체 공간장은 보존된 `1,800 s` 최종 `T` 필드에서 포함 계산셀 값을 직접 추출한다. 중간 시각의 YZ 냉각수 공간장은 원 계산에서 저장되지 않았으므로 새 값을 만들지 않고 비워 둔다. 대신 그 시각에 실제로 저장된 C01–C23 내부 체적평균과 표면 표본을 사용해, 해당 X 위치가 자르는 5개 또는 4개 셀 단면만 색칠한다.

배터리 표면 표본은 셀 하나당 옆면 32방향 × 16높이와 위·아래 캡 각각 32점, 즉 576점이다. 23개 전체는 13,248점이다. 원통의 실제 경계면에서 0.35 mm 안쪽 고체셀 값을 읽어 “표면 근처의 직접 고체값”으로 사용한다.

OpenFOAM probe의 `interpolationScheme cell`은 후보점이 들어 있는 계산셀의 값을 그대로 반환한다. 계산영역 밖이거나 찾지 못한 점은 invalid로 남기고 저장에서 제외하며, 이 위치정보를 `valid_indices`로 보존한다. 이웃값으로 구멍을 채우지 않는다.

### 11.2 1초 프레임 포장

각 데이터셋은 0초 초기장 1개와 1–1,800초 계산장 1,800개, 총 1,801 frame을 가진다. 저장량을 줄이기 위해 한 메시 데이터셋 전체의 최소·최대온도를 공유하는 선형 `uint16 little-endian`으로 양자화한다.

$$
n=\operatorname{round}\left(\frac{T-T_{\min}}{\Delta T_q}\right),
\qquad
T_\mathrm{decoded}=T_{\min}+n\Delta T_q
$$

표준 결과의 최대 양자화 오차는 `1.421736×10⁻⁶ K`, 정밀 결과는 `1.015370×10⁻⁶ K`다. 포장 후에는 모든 값을 다시 읽어 원본 probe와 반 quantum 이내인지 전수 비교한다. 11개 데이터셋 모두 이 검증을 통과했다.

브라우저용 `.u16` 파일은 필요할 때만 읽는다. 로컬 `file://`에서도 동작하도록 동일 바이트를 gzip level 9로 압축하고 base64로 감싼 `.u16.js`도 만든다. 따라서 `index.html` 하나만 복사하면 안 되고 `data`, 해시가 붙은 JS/CSS/model 파일을 포함한 뷰어 디렉터리 전체를 함께 옮겨야 한다.

### 11.3 속도장과 유선

속도와 압력은 cold-flow에서 수렴한 고정 공간장 `U(x), p(x)`다. 각 메시마다 모든 냉각수 cell-centre의 `|U|`를 훑어 하나의 공통 표시범위를 만든다. 표준은 0–0.596063 m/s, 정밀은 0–0.633058 m/s다. 유선, XY/XZ/YZ 속도 단면, 벡터 길이와 포트 색은 모두 이 범위를 공유하므로 서로 다른 화면에서도 같은 색이 같은 속도를 뜻한다. 선택 단면의 국부 최고속도는 별도로 표시하지만 색 범위를 다시 늘리지는 않는다.

유선은 입구 원판에 결정론적으로 seed를 배치하고 VTK Runge–Kutta 4/5 적분으로 고정 `U(x)`를 따라 전진시켜 만든다. 35 mm보다 짧은 경로를 버리고 화면 성능에 맞는 긴 경로를 선택한다. 현재 뷰어는 정밀 메시에서 220개, 표준 메시에서 178개 유선을 표시한다.

유선 위의 이동 헤드는 유체가 시간에 따라 다시 계산됐다는 표현이 아니다. 고정 유동장의 방향을 읽기 쉽게 보여주는 애니메이션이며, 이동속도는 실제 `|U|`에 비례한 뒤 사용자가 선택한 표시배율만 적용한다. 온도만 0–1,800 s에 따라 변한다.

### 11.4 UI에서 볼 수 있는 것

하나의 Canvas 기반 화면에서 다음 모드를 전환할 수 있다.

- 종합 화면: 셀 온도, 고정 유선, 이동 헤드, 하우징을 함께 표시
- 셀 온도: 23개 셀의 직접 표면근처 온도분포와 최고·최저점 표시
- 유체 유선: 고정 `U(x)`의 경로와 속도 공통 색 범위 표시
- 가로 XY 온도/유속: 저장된 15%, 32.5%, 50%, 67.5%, 85% 중 하나를 라디오 버튼으로 선택
- 세로 온도/유속: `YZ(X 고정)` 또는 `XZ(Y 고정)` 방향을 고르고, 저장된 다섯 비율 중 하나를 라디오 버튼으로 선택
- 셀, 유선·벡터, 이동 헤드, 단면, 샘플선, 외곽선의 독립 표시
- 마우스 회전, 휠 확대, ±X/±Y/±Z/등각 시점
- 0–1,800 s 탐색, 1×/5×/10×/30×/60×/180× 재생
- C01–C23 버튼을 눌러 선택 셀을 3D 장면에서 강조

1×에서는 실제 1초가 시뮬레이션 1초이므로 전체 재생에 30분이 걸린다. 재생속도를 높여도 데이터값은 같은 정수초 원본이며, 완료된 현재 뷰어는 시간 선형보간을 사용하지 않는다.

온도 그래프는 다음 다섯 종류를 제공한다.

1. 출구 `phi` 가중 평균온도
2. C01–C23의 내부 mesh `max(T)` 중 최고값
3. 23개 계면의 면적가중 표면 평균온도
4. 23개 cellZone의 체적가중 내부 평균온도
5. 각 셀의 내부 체적평균, 표면 면적평균 또는 mesh 최고값

그래프와 수렴 이력 데이터 및 내보내기 코드는 결과 패키지 안에 보존되어 있다. 현재 발표용 화면은 우측을 단순화해 `현재 결과`와 `운전 조건`만 표시하며, 계산 메시 선택·수렴 모니터·온도 그래프·모델/해석/Figure/셀 ID 보조 카드는 화면에서 숨긴다.

---

## 12. 이 모델이 말할 수 있는 것과 말할 수 없는 것

이 계산으로 말할 수 있는 것은 “지정된 형상, 상수 물성, 균질 셀, 상수 발열, 0중력, 단열 외벽, 0.4 m/s 입구 조건에서의 OpenFOAM CHT 해”다. 다음 항목은 현재 결과에 포함되지 않는다.

- 실제 셀 내부 전기화학과 SOC 분포
- 젤리롤·캔·탭의 비등방성 전도와 접촉열저항
- 가역 엔트로피열과 공개되지 않은 원 논문 UDF
- 온도에 따른 물성 변화, 부력, 자연대류
- 외벽과 외기의 대류·복사 열교환
- 펌프 곡선, 배관 손실, 유량 제어기의 동특성
- 제조 공차, 셀별 발열 편차와 실험 불확도

모든 mesh/solver gate를 통과했다는 것은 선언한 수치모델이 내부 품질기준을 만족했다는 뜻이다. 별도 실험과 일치하거나 타 논문의 Fluent 결과를 그대로 재현했다는 뜻은 아니다. 생성 보고서의 `publication_results_authorized`가 `false`인 이유도 이 구분을 유지하기 위해서다.

---

## 13. 재현 절차

Python 3.12 이상이 필요하다. OpenFOAM 계산은 OpenCFD OpenFOAM v2512를 고정하며, 생성된 실행기는 `opencfd/openfoam-default:2512` 기반 로컬 컨테이너 환경을 사용한다.

### 13.1 Python 환경과 단위시험

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[cad,results,media]'
.venv/bin/cfd-twin doctor
.venv/bin/cfd-twin validate-config configs/baseline_23cell.yaml
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```

### 13.2 CAD 생성

```bash
.venv/bin/cfd-twin build-cad-source \
  configs/baseline_23cell.yaml \
  --output build/case_validated/cad_source

.venv/bin/cfd-twin build-cad \
  configs/baseline_23cell.yaml \
  --output build/case_validated/cad_source
```

### 13.3 표준 생산 메시와 솔버 bundle 생성

현재 완료 결과와 같은 20.000 °C 기준을 명시한다.

```bash
.venv/bin/cfd-twin build-production-mesh-source \
  configs/baseline_23cell.yaml \
  --cad-dir build/case_validated/cad_source \
  --output build/case_validated/production/single \
  --profile single \
  --set 'boundary.inlet_temperature=293.15 K' \
  --set 'boundary.initial_fluid_temperature=293.15 K' \
  --set 'boundary.initial_solid_temperature=293.15 K' \
  --set 'battery.initial_temperature=293.15 K'
```

### 13.4 실행 순서

```bash
build/case_validated/production/single/run-production-mesh.sh
build/case_validated/production/single/run-cold-flow-pilot.sh
build/case_validated/production/single/run-cold-flow-continue.sh   # gate가 요구할 때만
build/case_validated/production/single/run-steady-pilot.sh
build/case_validated/production/single/run-steady-continue.sh     # gate가 요구할 때만
build/case_validated/production/single/run-frozen-flow-actual-1s.sh
```

전체 연성 과도 경로는 별도 검증용이다.

```bash
build/case_validated/production/single/run-full-transient-dt-pilot.sh
build/case_validated/production/single/run-full-transient-dt-half-pilot.sh
build/case_validated/production/single/run-full-transient-actual-1s.sh
```

마지막 명령은 짧은 단계와 시간간격 민감도 gate가 모두 통과한 뒤에만 실행해야 한다. `OPENFOAM_MPI_RANKS`로 솔버 rank 수를, `OPENFOAM_MESH_MPI_RANKS`로 메시 rank 수를 바꿀 수 있다.

### 13.5 뷰어 생성과 실행

```bash
.venv/bin/cfd-twin build-viewer \
  configs/baseline_23cell.yaml \
  --case-dir build/case_validated \
  --output build/reference_viewer

python3 -m http.server 8765 --directory build/reference_viewer
```

브라우저에서 `http://127.0.0.1:8765/`를 연다. 현재 Chrome, Edge, Safari에서는 `build/reference_viewer` 전체 폴더를 복사한 뒤 `index.html`을 직접 열어도 압축 데이터 로더가 동작한다.

---

## 14. 입력 변경과 추적성

기준 YAML을 직접 수정하지 않고 overlay나 점 경로 override를 적용할 수 있다.

```bash
.venv/bin/cfd-twin validate-config \
  configs/baseline_23cell.yaml \
  --set 'boundary.inlet_velocity=0.6 m/s' \
  --set 'fluid.dynamic_viscosity=0.001 Pa*s'
```

시간 발열과 Joule 발열 예시는 각각 다음과 같이 생성한다.

```bash
.venv/bin/cfd-twin generate-heat-source \
  configs/baseline_23cell.yaml \
  --overlay configs/heat_table_example.yaml \
  --output build/heat_table

.venv/bin/cfd-twin generate-heat-source \
  configs/baseline_23cell.yaml \
  --overlay configs/electrical_joule_example.yaml \
  --output build/electrical_heat
```

원본 YAML SHA-256, 적용 overlay, 명령행 override, SI 단위로 해석된 값, 형상 SHA-256는 resolved config와 manifest에 기록된다. 실행된 메시의 셀 수는 목표값으로 덮어쓰지 않으며 `polyMesh`와 `checkMesh`에서 다시 측정한다. 뷰어 데이터에도 메시 ID와 실제 셀 수가 함께 들어간다.

---

## 15. 코드와 결과의 역할

| 위치 | 역할 |
| --- | --- |
| `configs/baseline_23cell.yaml` | 기준 형상·물성·경계·발열 입력 |
| `src/cfd_twin/config.py` | 단위변환, 파생량, 입력·형상 해시 |
| `src/cfd_twin/geometry.py` | 5–4–5–4–5 배치, 충돌·간격 검사 |
| `src/cfd_twin/cad.py` | CadQuery solid, STEP/STL, CAD 체적·면적 검증 |
| `src/cfd_twin/production_mesh.py` | blockMesh/snappyHexMesh/refineMesh/region 분리와 메시 gate |
| `src/cfd_twin/heat_source.py` | 상수·table·Joule 발열과 fvOptions/UDF 생성 |
| `src/cfd_twin/openfoam_case.py` | CHT 물성, 경계조건, 수치스킴, 실행 프로파일 |
| `src/cfd_twin/openfoam_*_validation.py` | mesh, y+, 잔차, 질량·에너지·온도 gate |
| `src/cfd_twin/actual_one_second.py` | 직접 probe를 1초 uint16 데이터로 포장·전수검증 |
| `src/cfd_twin/openfoam_results.py` | VTK field, 표면, 유선 추출 |
| `src/cfd_twin/viewer.py` | 검증 결과를 휴대형 HTML/CSS/JS UI로 구성 |
| `build/case_validated/production/single` | 현재 표준 메시와 원본 계산·검증 기록 |
| `build/case_validated/production/L3` | 보존된 정밀 메시와 계산 provenance |
| `build/reference_viewer` | 두 메시를 포함한 현재 휴대형 결과 뷰어 |

권위 있는 수치는 README의 복사본보다 각 실행 디렉터리 아래 JSON 보고서다. 특히 메시 구성은 `mesh_target_composition.json`, zone은 `cellZones_pre_split.json`, 최종 품질은 `checkMesh_all_regions.json`, y+는 `runs/steady_cold_flow/reports/solver/yplus.json`, 과도 열검증은 `runs/frozen_flow_actual_1s/reports/solver/frozen_flow_validation.json`, UI 포장 정확도는 `reports/viewer/actual_1s_validation.json`에서 확인한다.

이 프로젝트의 핵심 원칙은 간단하다. 계산하지 않은 값은 그럴듯하게 만들지 않고, 한 메시의 결과를 다른 메시의 결과처럼 표시하지 않으며, 형상·열원·수치해석·후처리의 각 단계가 실제 파일과 검증 보고서로 이어지게 한다.
