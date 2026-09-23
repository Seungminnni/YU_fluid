/* Shared Korean/English presentation layer. Solver data and CSV field names stay unchanged. */
(() => {
  "use strict";
  const words = {
    "영남대학교 · 자동차 배터리 디지털 트윈":"Yeungnam University · Digital Twin for Automotive Batteries",
    "CFD 결과 뷰어":"CFD Results Viewer",
    "실제 계산 메시 보기":"Computed Mesh Viewer",
    "계산 상태 확인 중":"Checking calculation status",
    "상태 연결 끊김":"Status Not Connected",
    "저장된 CFD 결과":"Saved CFD Results",
    "저장된 CFD 결과 · 오프라인":"Saved CFD Results · Offline",
    "영남대학교":"Yeungnam University",
    "자동차 배터리 디지털 트윈":"Digital Twin for Automotive Batteries",
    "배터리 냉각 형상과 경계 조건을 확인하고, OpenFOAM 계산부터 3D 결과 분석까지 한 흐름으로 진행합니다.":"Review battery cooling geometry and boundary conditions, then proceed from OpenFOAM calculation to 3D result analysis in one workflow.",
    "시작":"Start", "그래픽":"Graphics",
    "작업을 선택하세요.":"Choose a task.",
    "저장된 CFD 결과를 확인하거나, CAD와 경계 조건을 입력해 새로운 계산을 준비할 수 있습니다.":"Review saved CFD results or enter CAD geometry and boundary conditions to prepare a new calculation.",
    "계산에서 결과까지,":"From calculation to results,",
    "한 화면에서 이어서.":"all in one place.",
    "검증된 CAD와 계산 기록을 기준으로 형상을 확인하고, 경계 조건을 검토하고, 실제 솔버 단계의 진행 상황을 확인합니다.":"Inspect the verified CAD, review boundary conditions, and follow the actual solver stages.",
    "결과 보기":"View Results", "입력":"Input", "← 처음으로":"← Home",
    "결과 상태를 불러오는 중입니다.":"Loading result status.",
    "새 CAD 계산":"New CAD Calculation", "새 CAD 계산 초기화":"Reset New CAD Setup",
    "새 CAD 3D 결과·그래프 열기 ↗":"Open New CAD 3D Results and Charts ↗",
    "3D 결과·그래프 열기 ↗":"Open 3D Results and Charts ↗",
    "기준 CAD 상면도·측면도 ↗":"Reference CAD Top and Side Views ↗",
    "CAD 상면도·측면도 ↗":"CAD Top and Side Views ↗",
    "메시 보기 ↗":"View Mesh ↗", "결과 화면":"Results View",
    "현재 결과":"Current Results", "결과 비교":"Result Comparison", "최대 3개":"Up to 3",
    "검증이 끝난 동일 정의의 결과만 비교합니다. 비어 있는 결과를 추정값으로 채우지 않습니다.":"Only validated, consistently defined results are compared. Missing results are not estimated.",
    "결과":"Result", "상태":"Status", "최고 온도":"Maximum Temperature", "출구 온도":"Outlet Temperature",
    "실제 CFD 계산 단계":"Actual CFD Stages", "과도 열해석 물리시간":"Transient Physical Time",
    "저장된 CFD 결과를 열람할 수 있습니다. 이 파일 화면은 새 계산을 시작하지 않습니다.":"Saved CFD results are available for review. This offline page does not start a new calculation.",
    "계산 결과 데이터가 없습니다.":"No calculation result data are available.",
    "저장된 3D 결과와 온도·수렴 그래프를 볼 수 있습니다.":"View the saved 3D results, temperature histories, and convergence plots.",
    "검증된 결과 없음":"No Validated Results", "3D 준비 완료":"3D Ready", "저장된 결과":"Saved Result",
    "최대 세 가지 CFD 결과 비교":"Comparison of Up to Three CFD Results",
    "로그를 확인 중입니다.":"Checking the solver log.",
    "01 · CAD 형상":"01 · CAD Geometry", "02 · 경계 조건":"02 · Boundary Conditions",
    "검증된 기준 CAD 사용":"Use Verified Reference CAD", "기준 CAD 치수 변경":"Modify Reference CAD Dimensions", "새 CAD 첨부·계산":"Upload and Calculate New CAD",
    "기준 CAD 템플릿":"Reference CAD Template", "검증된 23셀 탱크 · 5–4–5–4–5":"Verified 23-cell Tank · 5–4–5–4–5",
    "탱크 크기 입력 방식":"Tank Sizing Method", "X·Y·Z 치수 직접 입력":"Enter X·Y·Z Dimensions",
    "목표 체적 입력 · 기준 비율 유지":"Enter Target Volume · Preserve Reference Ratios",
    "탱크 X":"Tank X", "탱크 Y":"Tank Y", "탱크 Z":"Tank Z", "목표 탱크 체적":"Target Tank Volume",
    "입구 Ø":"Inlet Ø", "출구 Ø":"Outlet Ø", "기준 템플릿":"Reference Template",
    "Ø·탱크 입력":"Ø and Tank Inputs", "치수 계산":"Dimension Derivation", "STEP/STL 생성":"Generate STEP/STL",
    "형상 검사":"Geometry Validation", "메시 생성":"Mesh Generation", "결과 뷰어":"Result Viewer",
    "변경 형상 사전검사 통과":"Modified Geometry Precheck Passed", "치수 입력 대기":"Waiting for Dimensions",
    "기준 CAD 파라메트릭 변형":"Parametric Reference CAD Variant", "생성 전":"Not Generated",
    "배열 고정":"Fixed Arrangement",
    "파라메트릭 CFD 실행 흐름":"Parametric CFD Workflow",
    "외곽 최소 간격":"Minimum Outline Clearance",
    "실제 STEP/STL 생성 전 해석식 사전검사를 통과했습니다.":"The analytical precheck passed before actual STEP/STL generation.",
    "파라메트릭 CAD 생성·형상검사 완료":"Parametric CAD Generation and Validation Complete",
    "STEP/STL 생성 중…":"Generating STEP/STL…", "탱크·포트 STEP":"Tank and Ports STEP",
    "냉각수 STEP":"Coolant STEP", "배터리 STEP":"Battery STEP", "냉각수 STL":"Coolant STL",
    "배터리 STL":"Battery STL", "검사 JSON":"Validation JSON",
    "탱크 외곽·노치는 입력 크기에 맞춰 변형합니다. Ø19 배터리 23개 배열은 늘이지 않고 중앙으로 이동하며 입·출구 중심은 양끝 셀을 따라갑니다.":"The tank outline and notches follow the entered size. The 23-cell Ø19 battery array remains rigid and shifts centrally; inlet and outlet centers follow the two end cells.",
    "탱크 치수와 입·출구 Ø를 입력하면 배터리·포트 간섭을 먼저 검사합니다.":"Enter tank dimensions and inlet/outlet diameters to check battery and port interference first.",
    "검증된 23셀 기준 CAD · 파라메트릭 변형":"Verified 23-cell Reference CAD · Parametric Variant",
    "검증된 23셀 기준 CAD":"Verified 23-cell Reference CAD",
    "23개 · 5–4–5–4–5":"23 cells · 5–4–5–4–5",
    "탱크 X×Y×Z":"Tank X×Y×Z", "탱크 체적":"Tank Volume", "입구 / 출구 구경":"Inlet / Outlet Diameters",
    "셀–외곽 최소 간격":"Minimum Cell-to-Outline Clearance", "입구 / 출구 외곽 여유":"Inlet / Outlet Outline Clearance",
    "이번 실행 범위":"Scope of This Run", "CAD 생성·형상 검사만":"CAD Generation and Geometry Validation Only",
    "CAD → 메시 → S0 → S1 → S2 → 결과 뷰어":"CAD → Mesh → S0 → S1 → S2 → Result Viewer",
    "해석식 간섭 검사를 통과했습니다. 확인 후 STEP/STL과 메시용 영역별 표면을 생성합니다. 용량 보호를 위해 메시·S0·S1·S2 계산은 시작하지 않습니다.":"Analytical interference checks passed. After confirmation, STEP/STL and region surfaces for meshing will be generated. Mesh and S0/S1/S2 calculations will not start to protect disk capacity.",
    "해석식 간섭 검사를 통과했습니다. 확인 후 STEP/STL 생성·재검증을 거쳐 메시·S0·S1·S2 실제 계산과 결과 뷰어 생성을 시작합니다.":"Analytical interference checks passed. After confirmation, STEP/STL generation and revalidation will be followed by the actual mesh, S0, S1, and S2 calculations and result-viewer generation.",
    "STEP/STL 생성과 형상 검사가 끝났습니다. 메시와 S0/S1/S2 계산은 시작하지 않았습니다.":"STEP/STL generation and geometry validation are complete. Mesh and S0/S1/S2 calculations were not started.",
    "STEP/STL 생성과 형상 검사가 끝났고 실제 CFD 계산을 대기열에 등록했습니다.":"STEP/STL generation and geometry validation are complete, and the actual CFD calculation has been queued.",
    "CAD 생성 및 CFD 준비 중…":"Generating CAD and preparing CFD…",
    "CAD 생성 또는 CFD 시작 실패":"CAD generation or CFD startup failed",
    "STEP / STL 파일 선택":"Select STEP / STL Files",
    "여러 파일 가능 · 파일당 최대 50 MiB":"Multiple files · up to 50 MiB each",
    "STEP 3개를 탱크·포트 / 냉각수 / 배터리 영역에 지정합니다. STL은 참고용으로만 검사합니다.":"Assign three STEP files to the enclosure and ports, coolant, and battery regions. STL is checked for reference only.",
    "탱크":"Tank", "배터리":"Battery", "입구 / 출구":"Inlet / Outlet", "형상":"Geometry",
    "탱크·포트 외형":"Tank and Ports", "냉각수 유체 영역":"Coolant Fluid Region", "23개 배터리 고체 영역":"23 Battery Solid Regions",
    "형상 확인 대기":"Awaiting Geometry Review", "기준 CAD 일치":"Matches Reference CAD", "역할 선택":"Select Role", "CAD 역할":"CAD Role",
    "새 CAD는 STEP 3개를 역할별로 지정한 뒤 형상·조건 확인을 누르면 계산 가능 여부를 검사합니다.":"Assign each of the three STEP files to its role, then select Review Geometry and Conditions to check whether the CAD can be calculated.",
    "입력 검토":"Review Inputs", "냉각수 종류":"Coolant", "탈이온수 · 검증된 상수 물성":"Deionized water · verified constant properties",
    "탱크 / 배터리":"Tank / Battery", "탈이온수":"Deionized Water",
    "목표 메시":"Target Mesh", "열원":"Heat Source", "시간가변 2C · 0–1800 s":"Time-varying 2C · 0–1800 s",
    "첨부 STEP 3개 · 형상 검증 통과":"Three Uploaded STEP Files · Geometry Validation Passed",
    "입구 / 출구 중심":"Inlet / Outlet Centers",
    "첨부 CAD":"Uploaded CAD", "기준 CAD":"Reference CAD",
    "로 별도 메시·S0·S1·S2 계산을 실행합니다. 실제 메시 수와 품질, 수렴, 에너지 수지가 통과해야 3D 결과를 공개합니다. 디스크 여유":" will run a separate mesh and S0/S1/S2 calculation. The 3D results will be released only after the actual mesh count and quality, convergence, and energy-balance checks pass. Available disk space",
    "디스크 여유":"Available disk space", "최소":"minimum", "현재 공간으로는 새 계산 시작을 막습니다.":"A new calculation is blocked because the available disk space is insufficient.",
    "동일한 기준 조건의 기존 계산 또는 검증된 결과를 연결합니다. 새 계산은 시작하지 않습니다.":"The existing calculation or validated result with the same reference conditions will be linked. No new calculation will be started.",
    "기준 조건과 달라 새 CFD 계산이 필요합니다. CAD·운전조건 확인과 저장공간 검사를 이어서 진행합니다.":"The inputs differ from the reference conditions, so a new CFD calculation is required. CAD, boundary conditions, and available storage will be checked next.",
    "입구 온도":"Inlet Temperature", "입구 속도":"Inlet Velocity", "출구 절대압력":"Outlet Absolute Pressure",
    "메시 등급":"Mesh Level", "실제 수 검증":"Actual Count Verified", "검증 셀 수":"Verified Cell Count",
    "80만급 · 기준 형상 849,284셀":"800k-class · 849,284 cells on reference CAD",
    "70만급 · 기준 형상 732,766셀":"700k-class · 732,766 cells on reference CAD",
    "60만급 · 목표 600,000셀":"600k-class · target 600,000 cells",
    "80만급 · 849,284셀":"800k-class · 849,284 cells",
    "70만급 · 732,766셀":"700k-class · 732,766 cells",
    "발열":"Heat Generation", "Peneklioglu / Bernardi 시간가변 열원 · 2C · 0–1800 s":"Peneklioglu / Bernardi time-varying source · 2C · 0–1800 s",
    "형상·조건 확인하기 →":"Review Geometry and Conditions →",
    "계산 조건 확인":"Confirm Calculation Settings", "닫기":"Close", "돌아가기":"Back",
    "표시된 CAD 형상과 경계 조건을 확인했습니다":"I have reviewed the CAD geometry and boundary conditions",
    "현재 계산 진행 보기":"View Current Calculation", "새 CFD 계산 시작":"Start New CFD Calculation",
    "YU Fluid · 기준 CAD / 원본 솔버 로그 / 검증 보고서에 연결된 상태만 표시":"YU Fluid · showing only states linked to reference CAD, original solver logs, and validation reports",
    "CFD 계산 완료 · 후처리 중":"CFD complete · post-processing", "실제 CFD 계산 중":"Actual CFD calculation running",
    "3D 결과 준비 완료":"3D results ready", "계산 상태 확인 필요":"Calculation status needs review",
    "메시 확인 중":"Checking mesh", "메시 대기":"Mesh pending", "검증 통과":"Validation passed",
    "계산 중":"Calculating", "대기":"Pending", "확인 필요":"Needs review", "중단됨":"Stopped",
    "검증 전":"Not validated", "기준 CAD 검증 완료":"Reference CAD validated",
    "첨부 STEP 형상 검증 완료":"Uploaded STEP geometry validated", "새 CAD 형상 검토 필요":"New CAD needs geometry review",
    "첨부된 3개 STEP":"Three uploaded STEP files", "첨부 CAD · 미확정":"Uploaded CAD · unconfirmed",
    "계산 실패·확인 필요":"Calculation failed · review required", "사용자 중지":"Stopped by user",
    "중지 처리 중":"Stopping", "중지 처리 중…":"Stopping…", "계산 중지":"Stop Calculation",
    "검증·후처리 완료":"Validation and post-processing complete", "실제 계산 중":"Calculation running",
    "단계 상태를 확인 중입니다.":"Checking stage status.", "결과 없음":"No results",
    "에너지 결합 반복 로그 대기 중":"Waiting for the energy-coupling iteration log",
    "현재 시간단계의":"Current time-step", "마지막":"Final", "조기 종료":"Early exit", "상한 도달":"Iteration cap reached",
    "원본 CFD의 검토용 3D 형상, 단면, 온도·수렴 그래프가 준비됐습니다. 격자 독립성·교차검증 전에는 논문 최종값으로 사용하지 마세요.":"The review-ready 3D geometry, sections, temperature histories, and convergence plots from the original CFD run are available. Do not use them as final publication values before mesh-independence and cross-validation checks.",
    "1,800초 CFD 계산과 S2 검증이 끝났습니다. 3D 형상·단면·그래프는 실제 원본 프레임을 포장·검증한 후 열립니다.":"The 1,800 s CFD run and S2 validation are complete. The 3D geometry, sections, and plots become available after the original frames are packaged and verified.",
    "현재 과도 계산 또는 결과 검증이 끝나지 않았습니다. 이전·부분 결과를 최종 3D 결과로 표시하지 않습니다.":"The transient calculation or result validation is not complete. Previous or partial data are not presented as final 3D results.",
    "원본 CFD 결과와 검증 보고서가 연결됐습니다. 단일 메시 검토용입니다.":"The original CFD results and validation report are linked. These data are for single-mesh review.",
    "결과 상태를 확인 중입니다. 최종 검증 전에는 뷰어를 열지 않습니다.":"Checking result status. The viewer remains unavailable until final validation is complete.",
    "완전 에너지 수지 통과":"Full energy-balance check passed", "계산·검증 완료":"Calculation and Validation Complete", "후처리 대기":"Awaiting post-processing",
    "후처리 실패":"Post-processing failed", "화면은 실제 로그를 10초마다 확인합니다.":"The page checks the actual solver log every 10 seconds.",
    "실제 데이터 없음":"No actual data", "원본 로그 저장":"Original log saved",
    "복합 검증 통과":"Combined validation passed", "로그 없음":"No log",
    "CAD 확인":"CAD Validation", "메시 생성·품질":"Mesh Generation and Quality",
    "S0 냉간 유동":"S0 Cold Flow", "S1 정상 CHT":"S1 Steady CHT",
    "S2 과도 열해석":"S2 Transient Thermal Analysis", "결과 후처리·3D 뷰어":"Post-processing and 3D Viewer",
    "결과 화면 선택":"Select Result View", "종합 화면":"Combined View", "화면 레이어":"Display Layers", "셀 온도":"Cell Temperature",
    "유체 유선":"Fluid Streamlines", "가로 단면 온도":"Horizontal Section Temperature",
    "가로 단면 유속":"Horizontal Section Velocity", "세로 단면 온도":"Vertical Section Temperature",
    "세로 단면 유속":"Vertical Section Velocity", "시점":"View", "등각":"Isometric", "맞춤":"Fit",
    "레이어":"Layers", "유선·벡터":"Streamlines and Vectors", "이동 헤드":"Moving Tracers",
    "단면":"Section", "샘플선":"Sample Lines", "부드러운 색상":"Smooth Colors",
    "셀 표면과 실제 유효 단면 샘플 사이만 부드럽게 연결하며 무효영역은 채우지 않습니다":"Only smooth between cell-surface and valid section samples; invalid regions are left empty.",
    "3D 단면":"3D Section", "외곽선":"Outline", "온도 범례":"Temperature Legend", "속도 범례":"Velocity Legend", "범례":"Legend", "입·출구 정보":"Inlet/Outlet Info",
    "최대·최소":"Maximum/Minimum", "표시 위치 초기화":"Reset Overlay Positions", "유동만":"Flow Only", "전체":"All",
    "세로 단면":"Vertical Section", "가로 XY 위치":"Horizontal XY Position", "중앙":"Center",
    "직접 단면":"Direct Section", "배속":"Playback Speed", "헤드 속도":"Tracer Speed",
    "❚❚ 정지":"❚❚ Pause", "▶ 재생":"▶ Play", "대기 중":"Waiting",
    "화면 1 s":"Display 1 s", "IN/OUT 면적평균":"IN/OUT Area Average",
    "← 3D 화면":"← 3D View", "PNG 저장":"Save PNG", "PNG 저장 ↓":"Save PNG ↓",
    "PNG 추출":"Export PNG", "CSV 추출":"Export CSV", "현재 CSV":"Current CSV", "전체 CSV":"All CSV",
    "그래프":"Chart", "셀 기준":"Cell Metric", "계산 메시 비교":"Computed Mesh Comparison",
    "1. 출구 평균 온도":"1. Outlet Average Temperature", "2. 배터리 최고 온도":"2. Battery Maximum Temperature",
    "3. 배터리 표면 평균 온도":"3. Battery Surface Average Temperature",
    "4. 배터리 내부 온도":"4. Battery Internal Temperature",
    "5. 각 배터리별 온도":"5. Temperature by Battery Cell", "6. 에너지 수지":"6. Energy Balance",
    "7. 주요 배터리 5셀 온도":"7. Five Key Battery Cells",
    "내부 체적평균":"Internal Volume Average", "표면 면적평균":"Surface Area Average",
    "mesh 최고값":"Mesh Maximum", "CFD 수렴 이력":"CFD Convergence History",
    "계산 단계":"Calculation Stage", "실제 솔버 로그 · 잔차 로그 축":"Actual Solver Log · Log Residual Axis",
    "현재 시점":"Current Time", "23개 셀 · 현재 시점":"23 cells · current time",
    "면적 평균":"Area Average", "입구 − 출구":"Inlet − Outlet", "압력 강하":"Pressure Drop",
    "경계 조건":"Boundary Conditions", "계산 메시 선택":"Select Calculation Mesh",
    "결과 데이터":"Result Data", "CFD 수렴 모니터":"CFD Convergence Monitor",
    "수렴 그래프 열기":"Open Convergence Chart", "온도 그래프":"Temperature Charts",
    "온도 그래프 열기":"Open Temperature Charts", "모델 정보":"Model Information",
    "해석 세부정보":"Analysis Details", "논문 Figure 결과":"Paper Figures", "셀 ID 보기":"View Cell IDs",
    "출구 평균 온도":"Outlet Average Temperature", "배터리 최고 온도":"Battery Maximum Temperature",
    "배터리 표면 평균 온도":"Battery Surface Average Temperature",
    "배터리 내부 온도":"Battery Internal Temperature", "각 배터리별 온도":"Temperature by Battery Cell",
    "주요 배터리 5셀 온도":"Five Key Battery Cell Temperatures", "에너지 수지":"Energy Balance",
    "배터리 발열":"Battery Heat Generation", "출구 제거열":"Heat Removed at Outlet",
    "내부 저장열":"Internal Energy Storage", "불균형률":"Imbalance Rate",
    "에너지 수지 · 실제 CFD 후처리값":"Energy Balance · Actual CFD Post-processed Values",
    "저장된 에너지 수지 데이터가 없습니다.":"No saved energy-balance data.",
    "실제 온도 시계열 데이터가 없습니다.":"No actual temperature time-series data.",
    "저장된 residual 데이터가 없습니다.":"No saved residual data.",
    "시간 [s]":"Time [s]", "온도 [°C]":"Temperature [°C]", "열량률 [W]":"Heat Rate [W]",
    "불균형률 [%]":"Imbalance [%]", "수렴 이력":"Convergence History",
    "누적 PIMPLE 전역 반복 횟수":"Cumulative PIMPLE Global Iterations",
    "1초 과도 에너지 반복 횟수":"Transient Energy Iterations per Second",
    "OpenFOAM 외부 반복 횟수":"OpenFOAM Outer Iterations",
    "Initial residual / |global continuity|":"Initial Residual / |Global Continuity|",
    "압력 p_rgh":"Pressure p_rgh", "질량보존 |global|":"Mass Conservation |global|",
    "유체 energy(h)":"Fluid Energy (h)", "고체 energy(h)":"Solid Energy (h)",
    "정밀 CFD 실제값":"Actual Precision CFD Values", "정밀 메시":"Precision Mesh",
    "표준 메시":"Standard Mesh", "실제 polyMesh":"Actual polyMesh",
    "유동 초기화":"Flow Initialization", "정상 CHT":"Steady CHT",
    "고정 유동 과도 에너지 계산":"Frozen-flow Transient Energy Calculation",
    "누적 PIMPLE 전역 반복":"Cumulative PIMPLE Global Iterations",
    "1초 과도 에너지 반복":"Transient Energy Iterations per Second",
    "정상상태 외부 반복":"Steady-state Outer Iterations",
    "유체·고체 에너지 결합 반복":"Fluid–Solid Energy Coupling Iterations",
    "출구 phi 질량유량 가중 평균":"Outlet φ Mass-flow-weighted Average",
    "C01–C23 cell zone의 mesh max(T) 중 최고":"Maximum Mesh T over C01–C23 Cell Zones",
    "해당 셀의 실제 온도 데이터 없음":"No actual temperature data for this cell",
    "에너지 수지 데이터 없음":"No energy-balance data",
    "23개 셀 interface의 실제 면적가중 평균":"Actual Area-weighted Average over 23 Cell Interfaces",
    "23개 batterySolid cell zone의 실제 체적가중 평균":"Actual Volume-weighted Average over 23 Battery-solid Zones",
    "발열량 = 출구 제거 열량 + 내부 저장 열량 + 수치 불균형":"Generated Heat = Heat Removed at Outlet + Internal Storage + Numerical Imbalance",
    "외곽 4셀과 중앙 1셀의":"Four Outer Cells and One Central Cell —",
    "각각의":"for each of", "좌하단":"lower left", "좌상단":"upper left",
    "우하단":"lower right", "우상단":"upper right", "중앙":"center",
    "제거":"removed", "저장":"stored", "불균형":"imbalance",
    "최고":"maximum", "최저":"minimum", "없음":"none",
    "Figure 2 · 현재 두 계산 메시의 실제 구성":"Figure 2 · Actual Structure of Two Computed Meshes",
    "Figure 3 · 정밀/표준 계산 결과 비교":"Figure 3 · Precision/Standard CFD Result Comparison",
    "전체 셀":"Total Cells", "유체 셀":"Fluid Cells", "고체 셀":"Solid Cells",
    "노드":"Nodes", "육면체":"Hexahedra", "육면체 비율":"Hexahedron Fraction",
    "유체 / 고체 셀 구성":"Fluid / Solid Cell Composition", "정밀 메시 대비 셀 수":"Cells Relative to Precision Mesh",
    "현재 선택 결과":"Selected Result", "비교 결과":"Comparison Result",
    "배터리 최고 온도 상승":"Battery Maximum Temperature Rise",
    "배터리 내부 체적평균 상승":"Battery Internal Volume-average Rise",
    "배터리 표면 면적평균 상승":"Battery Surface Area-average Rise",
    "출구 질량가중 평균 온도 상승":"Outlet Mass-weighted Average Temperature Rise",
    "입구–출구 압력강하":"Inlet–Outlet Pressure Drop", "유체 최대 속도":"Maximum Fluid Velocity",
    "결과 없음":"No Result", "기준 입구 온도":"Reference Inlet Temperature",
    "온도 항목은 T − Tin":"Temperature metrics show T − Tin",
    "실제 OpenFOAM polyMesh · 경계선":"Actual OpenFOAM polyMesh · Surface Edges",
    "3D 경계":"3D Surface", "3D 전체 격자":"3D Full Volume Mesh",
    "3D 내부 절개":"3D Interior Cutaway", "가로 XY 단면":"Horizontal XY Section",
    "세로 XZ 단면":"Vertical XZ Section", "절개 위치":"Cut Position",
    "카메라 시점":"Camera View", "냉각수 영역":"Coolant Region", "배터리 영역":"Battery Region",
    "드래그: 회전 · 휠: 확대":"Drag: Rotate · Wheel: Zoom",
    "실제 계산 격자를 불러오는 중…":"Loading computed mesh…"
    ,"기준 CAD 상면도·측면도":"Reference CAD Top and Side Views",
    "최종 계산 CAD 상면도·측면도":"Final Calculated CAD Top and Side Views",
    "화면 이동":"Navigation", "CFD 결과":"CFD Results", "메시 보기":"Mesh Viewer",
    "최종 계산 형상":"Final Calculated Geometry",
    "새 C05 상부 입수구·C19 하부 출수구 Ø23/Ø25 CFD 계산에 사용한 정확 CAD의 직교 투영과 치수 도면입니다.":"Orthographic projections and dimensioned drawings of the exact CAD used for the new Ø23/Ø25 CFD calculations with the inlet above C05 and outlet below C19.",
    "최종 계산 형상 치수":"Final Calculated Geometry Dimensions",
    "포트 Ø23/Ø25 × 32 mm":"Ports Ø23/Ø25 × 32 mm",
    "검증된 기준 형상":"Verified Reference Geometry", "CAD 상면도 · 측면도":"CAD Top and Side Views",
    "기준 STEP 형상의 직교 투영과 치수 도면입니다. 아래 그림은 형상을 설명하며, CFD 온도 단면이나 체적 메시가 아닙니다.":"Orthographic projections and dimensioned drawings of the reference STEP geometry. These illustrate geometry, not CFD temperature sections or volume meshes.",
    "기준 형상 치수":"Reference Geometry Dimensions", "탱크 114 × 121 × 82 mm":"Tank 114 × 121 × 82 mm",
    "배터리 23개 · Ø19 × 68 mm":"23 Batteries · Ø19 × 68 mm", "배열 5–4–5–4–5":"Arrangement 5–4–5–4–5",
    "입·출구 Ø19 × 32 mm":"Inlet/Outlet Ø19 × 32 mm", "상면 및 측면 도면":"Top and Side Drawings",
    "상면도":"Top View", "측면도":"Side View", "도면 저장 ↧":"Download Drawing ↧",
    "결과 선택":"Select Result", "실제 CAD 투영 이미지":"Actual CAD Projection",
    "CAD 상면도·측면도 ↗":"CAD Top and Side Views ↗",
    "선택한 결과의 CAD 상면도":"CAD top view for the selected result",
    "선택한 결과의 CAD 측면도":"CAD side view for the selected result",
    "실제 CAD 상면 투영":"Actual CAD top projection",
    "실제 CAD 측면 투영":"Actual CAD side projection",
    "실제 STEP 투영 보기":"View Actual STEP Projection", "STEP 투영 PNG 저장 ↧":"Download STEP Projection PNG ↧",
    "계산 CAD 투영 보기":"View Calculated CAD Projection", "CAD 투영 PNG 저장 ↧":"Download CAD Projection PNG ↧",
    "기준 탱크와 23개 원통형 셀의 XY 상면 치수 도면":"Dimensioned XY top view of the reference tank and 23 cylindrical cells",
    "기준 탱크, 배터리 높이와 입출구의 XZ 측면 치수 도면":"Dimensioned XZ side view of the reference tank, battery height, and ports",
    "기준 STEP 파일의 XY 상면 투영":"XY top projection of reference STEP file",
    "기준 STEP 파일의 XZ 측면 투영":"XZ side projection of reference STEP file",
    "최종 계산 탱크와 23개 원통형 셀의 XY 상면 치수 도면":"Dimensioned XY top view of the final calculated tank and 23 cylindrical cells",
    "최종 계산 탱크, 배터리 높이와 입출구의 XZ 측면 치수 도면":"Dimensioned XZ side view of the final calculated tank, battery height, and ports",
    "최종 계산 STEP 파일의 XY 상면 투영":"XY top projection of the final calculated STEP file",
    "최종 계산 STEP 파일의 XZ 측면 투영":"XZ side projection of the final calculated STEP file",
    "최종 계산 CAD의 XY 상면 투영":"XY top projection of the final calculated CAD",
    "최종 계산 CAD의 XZ 측면 투영":"XZ side projection of the final calculated CAD",
    "새 계산 CAD입니다. 상면도 기준 파란 입수구 중심은 C05 바로 위, 빨간 출수구 중심은 C19 바로 아래입니다. Ø23과 Ø25 계산은 동일한 중심·탱크·셀 배치를 사용하고 포트 직경만 다릅니다.":"This is the new calculation CAD. In the top view, the blue inlet center is directly above C05 and the red outlet center is directly below C19. The Ø23 and Ø25 cases use identical centers, tank, and cell layout; only the port diameter differs.",
    "Y축 반사 적용 CFD 결과":"Y-axis Reflected CFD Results",
    "수렴된 원본 해석장을 y′ = 121 − y로 강체 반사했습니다. 메시 수·품질과 스칼라 해석값은 보존됩니다.":"The converged source solution was rigidly reflected using y′ = 121 − y. Mesh count, mesh quality, and scalar solution values are preserved.",
    "기존 수렴 계산의 모든 공간 좌표와 속도 Y 성분을 탱크 Y 중앙면에서 강체 반사한 결과입니다. 셀 수와 메시 품질은 원본과 동일하며 새 계산 결과로 오인하지 않도록 반사 변환 여부를 명시합니다.":"All spatial coordinates and the Y velocity component from the converged calculation were rigidly reflected about the tank Y mid-plane. Cell count and mesh quality are unchanged; this is explicitly identified as a reflected result, not a newly solved case.",
    "CFD 결과 · Y축 반사 적용 · 메시 품질 보존":"CFD Results · Y-axis Reflection · Mesh Quality Preserved",
    "치수 표기는 기준 CAD의 설계 설명용입니다. 새 CAD를 업로드해 계산한 경우, 이 기준 도면을 새 형상 결과로 표시하지 않습니다.":"Dimensions describe the reference CAD only. These drawings are not presented as results for a newly uploaded and calculated CAD model."
    ,"셀":"Cells", "입구 유속":"Inlet Velocity", "냉각수":"Coolant", "물 (Water)":"Water",
    "주요 CFD 결과":"Key CFD Results",
    "실제 유효 샘플 또는 셀 표면 샘플의 경계만 부드럽게 연결하며 무효영역은 채우지 않습니다":"Smoothly connects only valid samples or cell-surface sample boundaries; invalid regions remain empty",
    "가로 단면을 회전·확대 가능한 3D 평면으로 표시합니다":"Displays the horizontal section as a rotatable, zoomable 3D plane",
    "유속 수치는 그대로 유지하고 이동 헤드의 표시속도만 조절합니다":"Changes tracer display speed only; velocity values remain unchanged",
    "YZ · X 고정 (5/4열)":"YZ · Fixed X (5/4 columns)", "XZ · Y 고정":"XZ · Fixed Y",
    "셀 계산 온도":"Computed Cell Temperature", "선택한 세로 위치의 유속 크기와 방향":"Velocity magnitude and direction at the selected vertical position",
    "선택한 세로 위치의 셀 온도":"Cell temperature at the selected vertical position",
    "전체 유동장 공통 U 범위":"Common U Range for the Full Flow Field",
    "실제 샘플 사이 화면만 부드럽게":"Smooth Display Only Between Actual Samples",
    "무효영역 채움 없음":"Invalid Regions Left Empty", "현재 단면 최고":"Current Section Maximum",
    "검증된 23셀 CAD":"Verified 23-cell CAD",
    "기준 형상의 포트 중심을 새 CAD에 자동 적용하지 않습니다. 새 CAD의 포트 위치를 확인해야 합니다.":"Port centers from the reference geometry are not automatically applied to new CAD. Verify port locations in the uploaded CAD.",
    "새 STEP의 입·출구 구경은 형상에서 읽습니다. 현재 자동 계산은 동일한 23셀 탱크 계열 형상에 한하며, 검증을 통과하지 못한 CAD는 실행되지 않습니다.":"Inlet and outlet diameters are read from the new STEP geometry. Automatic calculation currently supports the same 23-cell tank family; CAD that fails validation cannot be run.",
    "해석 영역":"Simulation Domain", "입·출구":"Inlet and Outlet", "각":"Each",
    "셀 배열":"Cell Arrangement", "교차 배열":"Staggered Arrangement", "계산 격자":"Computed Mesh",
    "Reynolds 수":"Reynolds Number", "발열 모델":"Heat-generation Model",
    "셀 발열 (0 → 1800 s)":"Per-cell Heat Generation (0 → 1800 s)",
    "23셀 명목 발열 (0 → 1800 s)":"Nominal Heat Generation for 23 Cells (0 → 1800 s)",
    "체적발열 (0 → 1800 s)":"Volumetric Heat Generation (0 → 1800 s)",
    "냉각수 밀도":"Coolant Density", "비열 Cp":"Specific Heat Cp", "점도 μ":"Viscosity μ",
    "열전도율 k":"Thermal Conductivity k",
    "배터리 온도 변화":"Battery Temperature Evolution", "셀 표면 온도":"Cell Surface Temperature",
    "온도·유동 종합":"Temperature and Flow Combined", "배터리 온도와 유선":"Battery Temperature and Streamlines",
    "셀 온도와 유체 흐름을 시간에 따라 확인":"View Cell Temperature and Fluid Flow Over Time",
    "셀 강조":"Highlight Cell",
    "선택 · 3D 모델에서 잠시 강조됩니다":"selected · briefly highlighted in the 3D model",
    "출구 압력":"Outlet Pressure", "해석 시간":"Analysis Duration", "계산 완료":"Calculation Complete",
    "계산 완료 · 검토용":"Calculation Complete · For Review",
    "유체 / 고체":"Fluid / Solid", "요소":"Element Type", "1초 과도 · 에너지":"1-second Transient · Energy",
    "1초 에너지 반복":"1-second Energy Iterations", "내부 반복 합계":"Total Inner Iterations",
    "출구 평균":"Outlet Average", "표면 평균":"Surface Average", "내부 평균":"Internal Average",
    "1 s 간격":"1 s intervals", "1초 단위":"1-second intervals", "실제 CFD 결과 · 단일 메시 검토용":"Actual CFD Results · Single-mesh Review Only",
    "단일 메시 검토용 CFD 결과":"Single-mesh CFD Results for Review",
    "— 0–1800 s의 원본값을 1 s 간격으로 저장·검증했습니다. 격자 독립성·교차검증 전에는 논문 최종값으로 사용하지 않습니다.":"— Original 0–1800 s data were saved and validated at 1 s intervals. Do not use as final publication results before mesh-independence and cross-validation checks.",
    "적응형 시험":"Adaptive Trial", "단계 잔차 목표 미달":" steps missed the residual target",
    "80만급 생산 결과":"800k-class Production Result",
    "80만급/정밀 결과":"800k-class / Precision Result",
    "유동 초기화 · U/p/k/ω":"Flow Initialization · U/p/k/ω",
    "정상 CHT · 유동+에너지":"Steady CHT · Flow and Energy",
    "80만급":"800k-class", "70만급":"700k-class", "적응형 84.9만셀 결과":"Adaptive 849k-cell Result",
    "원본 CFD 결과":"Original CFD Results", "직접 셀 데이터":"Direct Cell Data",
    "직접 셀값":"Direct Cell Values", "로드 중":"Loading", "실제 1 s 셀값 로드 중":"Loading Actual 1 s Cell Values",
    "0.5× 보기":"0.5× Display", "1× 실제":"1× Actual", "완주":"Full Run",
    "CFD 원본 1 s · 직접 셀값 · 시간보간/패딩 없음 · 헤드 최대 90 FPS":"Original CFD at 1 s · Direct Cell Values · No Time Interpolation/Padding · Tracers up to 90 FPS",
    "T 원본(무보정)":"Original T (Unadjusted)",
    "셀 온도와 실제 U(x)에 비례해 이동하는 유동 헤드":"Cell temperature and flow tracers moving in proportion to actual U(x)",
    "단면과 동일한 U 기준":"Same U Basis as Sections", "U 공통범위":"Common U Range",
    "헤드 0.5× 표시속도":"Tracers at 0.5× Display Speed", "드래그 회전":"Drag to Rotate",
    "휠 확대/축소":"Wheel to Zoom", "T 원본":"Original T", "시간보간/패딩 없음":"No Time Interpolation/Padding",
    "실제 면적평균":"Actual Area Average", "최고 셀 온도":"Maximum Cell Temperature",
    "복합 검증 통과":"Combined Validation Passed", "정상 외부 반복":"Steady Outer Iterations",
    "해당 셀의 실제 온도 데이터 없음":"No Actual Temperature Data for This Cell",
    "누적":"Cumulative", "회":"iterations", "단계":"steps",
    "형상 미리보기":"Geometry Preview",
    "메시 표시 방식":"Mesh Display Mode",
    "현재 메시 화면을 PNG로 저장":"Save Current Mesh View as PNG",
    "회전 및 확대 가능한 실제 CFD 계산 메시":"Interactive Computed CFD Mesh",
    "실제 polyMesh · 3D 경계 에지 전체":"Actual polyMesh · All 3D Boundary Edges",
    "체적 격자에서 추출한 실제 내부 셀 에지":"Actual Interior Cell Edges Extracted from the Volume Mesh",
    "3D 경계와 내부 체적 셀 절개면":"3D Boundary and Interior Volume-cell Cutaway",
    "3D 경계 또는 실제 체적 셀 단면":"3D Boundary or Actual Volume-cell Section",
    "표본화 없음":"No Sampling",
    "실제 polyMesh · 전체 3D 체적 격자":"Actual polyMesh · Complete 3D Volume Mesh",
    "전체 격자 표시 실패":"Full Mesh Display Failed",
    "에지 전체":"All Edges", "유체":"Fluid", "고체":"Solid"
  };
  // Longer phrases take precedence when a dynamic status message contains numbers.
  const phrases = Object.entries(words).filter(([key]) => /[가-힣]/.test(key) && !["셀","회","단계"].includes(key)).sort((a,b) => b[0].length-a[0].length);
  const params = new URLSearchParams(location.search);
  const requested = params.get("lang");
  let stored;
  try { stored = localStorage.getItem("yuFluidLanguage"); } catch (_) { stored = null; }
  let language = requested === "en" || requested === "ko" ? requested : stored === "en" ? "en" : "ko";
  const originals = new WeakMap();
  const attributeOriginals = new WeakMap();
  let queued = false;
  function translate(source) {
    if (language !== "en" || typeof source !== "string") return source;
    if (Object.prototype.hasOwnProperty.call(words, source)) return words[source];
    if (source.trim() === "셀") return source.replace("셀","Cells");
    if (!/[가-힣]/.test(source)) return source;
    let result = source;
    for (const [ko,en] of phrases) {
      if (!result.includes(ko)) continue;
      const escaped = ko.replace(/[.*+?^${}()|[\]\\]/g,"\\$&");
      result = result.replace(new RegExp(`(?<![가-힣])${escaped}(?![가-힣])`,"g"),en);
    }
    result = result.replace(/(\d[\d,]*)개 계산 셀/g,"$1 computed cells")
      .replace(/현재 (\d[\d,]*)개 에지/g,"Current $1 edges")
      .replace(/유체·고체 전체 (\d[\d,]*)개 고유 에지/g,"$1 unique edges across fluid and solid")
      .replace(/(\d[\d,]*)개 셀/g,"$1 cells").replace(/(\d[\d,]*)셀/g,"$1 cells")
      .replace(/(\d[\d,]*)개(?=\s*·|\s*$)/g,"$1 cells")
      .replace(/(\d[\d,]*)회/g,"$1 iterations").replace(/(\d[\d,]*)단계/g,"$1 steps");
    return result;
  }
  function isIgnored(node) {
    const element = node.nodeType === 1 ? node : node.parentElement;
    return !element || Boolean(element.closest("script,style,textarea,code,pre,.language-switch,[data-no-i18n]"));
  }
  function renderText(node) {
    if (isIgnored(node) || !node.nodeValue || !/[가-힣]/.test(node.nodeValue) && !originals.has(node)) return;
    const previous = originals.get(node);
    const source = previous && node.nodeValue === previous.rendered ? previous.source : node.nodeValue;
    const rendered = translate(source);
    originals.set(node,{source,rendered});
    if (node.nodeValue !== rendered) node.nodeValue = rendered;
  }
  function renderAttributes(element) {
    if (isIgnored(element)) return;
    const records = attributeOriginals.get(element) || {};
    for (const attribute of ["title","aria-label","placeholder","alt"]) {
      if (!element.hasAttribute(attribute)) continue;
      const current = element.getAttribute(attribute);
      const previous = records[attribute];
      const source = previous && current === previous.rendered ? previous.source : current;
      const rendered = translate(source);
      records[attribute] = {source,rendered};
      if (current !== rendered) element.setAttribute(attribute,rendered);
    }
    attributeOriginals.set(element,records);
  }
  function localizeLink(href) {
    if (!href || href.startsWith("#") || href.startsWith("blob:")) return href;
    try {
      // The portable archive's top-level index.html declares
      // <base href="viewer/">. Resolve relative links against that base so
      // file:// links keep the viewer/ prefix instead of incorrectly opening
      // sibling paths such as cad/index.html beside the package entry page.
      const target = new URL(href,document.baseURI || location.href);
      if (!(["http:","https:","file:"].includes(target.protocol))) return href;
      if (location.protocol !== "file:" && target.origin !== location.origin) return href;
      if (!/\.html?$|\/$/.test(target.pathname)) return href;
      target.searchParams.set("lang",language);
      return target.href;
    } catch (_) { return href; }
  }
  function refresh() {
    if (!document.body) return;
    document.documentElement.lang = language;
    document.querySelectorAll(".language-switch").forEach(group => {
      const label = language === "en" ? "Language" : "언어";
      if (group.getAttribute("aria-label") !== label) group.setAttribute("aria-label",label);
    });
    document.querySelectorAll("[data-language]").forEach(button => {
      button.setAttribute("aria-pressed",String(button.dataset.language === language));
      const label = button.dataset.language === "ko"
        ? (language === "en" ? "Korean" : "한국어")
        : "English";
      if (button.textContent !== label) button.textContent = label;
    });
    const walker = document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) renderText(node);
    document.querySelectorAll("[title],[aria-label],[placeholder],[alt]").forEach(renderAttributes);
    document.querySelectorAll("a[href]").forEach(anchor => {
      if (anchor.hasAttribute("download")) return;
      const current = anchor.getAttribute("href");
      const localized = localizeLink(current);
      // Comparing a relative attribute (for example dimensioned_top.svg) with
      // anchor.href (always absolute) made the MutationObserver rewrite the
      // same link forever. Compare attribute strings so one refresh settles.
      if (localized !== current) anchor.setAttribute("href",localized);
    });
    if (document.title) {
      const previous = refresh.title;
      const source = previous && document.title === previous.rendered ? previous.source : document.title;
      const rendered = translate(source);
      refresh.title = {source,rendered};
      document.title = rendered;
    }
  }
  function scheduleRefresh() { if (queued) return; queued = true; queueMicrotask(() => { queued = false; refresh(); }); }
  function setLanguage(next) {
    if (next !== "ko" && next !== "en") return;
    language = next;
    try { localStorage.setItem("yuFluidLanguage",language); } catch (_) { /* file:// storage may be unavailable */ }
    try {
      const current = new URL(location.href);
      current.searchParams.set("lang",language);
      history.replaceState(null,"",current.href);
    } catch (_) { /* file:// History API may be restricted */ }
    refresh();
    window.dispatchEvent(new CustomEvent("cfdtwin:languagechange",{detail:{language}}));
  }
  window.CFDTwinI18n = { t:translate, getLanguage:() => language, setLanguage, localizeLink, refresh };
  function start() {
    document.querySelectorAll("[data-language]").forEach(button => button.addEventListener("click",() => setLanguage(button.dataset.language)));
    refresh();
    new MutationObserver(scheduleRefresh).observe(document.body,{subtree:true,childList:true,characterData:true,attributes:true,attributeFilter:["href","title","aria-label","placeholder","alt"]});
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded",start,{once:true});
  else start();
})();
