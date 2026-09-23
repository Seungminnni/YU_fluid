"use strict";

const $ = (id) => document.getElementById(id);
const CAD_ROLES = {
  "enclosure_with_ports.step": "탱크·포트 외형",
  "coolant.step": "냉각수 유체 영역",
  "battery_cells.step": "23개 배터리 고체 영역",
};
const state = { geometry: null, status: null, fourCaseManifest: null, cadFiles: [], importedPreview: null, parametricPreview: null, pendingParametric: null, pendingImport: null, activeRun: null };
const statusNames = { passed: "검증 통과", running: "계산 중", pending: "대기", needs_check: "확인 필요", cancelled: "중단됨" };

async function api(path, options = {}) {
  const response = await fetch(path, { cache: "no-store", ...options });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
  return payload;
}

function showView(name) {
  $("landing-view").hidden = name !== "landing";
  $("results-view").hidden = name !== "results";
  $("calculate-view").hidden = name !== "calculate";
  history.replaceState(null, "", name === "landing" ? location.pathname : `#${name}`);
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function textCell(row, tag, value) {
  const cell = document.createElement(tag);
  cell.textContent = String(value);
  row.appendChild(cell);
}

function renderStatus(status) {
  state.status = status;
  const transient = status.transient;
  const result = status.result_summary || {};
  const postprocess = status.postprocess || {};
  const calculationComplete = Boolean(result.solver_validation_passed);
  const active = status.stages.some((stage) => stage.id === "transient" && stage.status === "running");
  $("live-state").classList.toggle("running", active);
  $("live-state").textContent = active ? "실제 CFD 계산 중" : status.viewer_ready ? "3D 결과 준비 완료" : calculationComplete ? "CFD 계산 완료 · 후처리 중" : "계산 상태 확인 필요";
  $("mesh-count").textContent = status.mesh_cells ? `${Number(status.mesh_cells).toLocaleString()}셀` : "메시 대기";
  $("physical-time").textContent = transient.time_s === null ? "— / 1800 s" : `${transient.time_s.toLocaleString()} / ${transient.end_time_s.toLocaleString()} s`;
  $("progress-label").textContent = `${transient.physical_time_percent.toFixed(1)}%`;
  $("transient-progress").value = transient.physical_time_percent;
  $("coupling-state").textContent = transient.energy_coupling_iteration === null
    ? "에너지 결합 반복 로그 대기 중"
    : `${calculationComplete ? "마지막" : "현재 시간단계의"} 유체·고체 에너지 결합 반복 ${transient.energy_coupling_iteration} / ${transient.energy_coupling_limit}`;
  if (transient.energy_coupling_audit?.time_step_count) {
    $("coupling-state").textContent += ` · 조기 종료 ${transient.energy_coupling_audit.early_exit_count}단계 / 상한 도달 ${transient.energy_coupling_audit.cap_reached_count}단계`;
  }

  const stageList = $("stage-list");
  stageList.replaceChildren();
  status.stages.forEach((stage, index) => {
    const item = document.createElement("div");
    item.className = `stage ${stage.status}`;
    const title = document.createElement("strong");
    title.textContent = `${String(index + 1).padStart(2, "0")} · ${stage.label}`;
    item.append(title, document.createTextNode(statusNames[stage.status] || stage.status));
    stageList.append(item);
  });

  const ready = Boolean(status.viewer_ready);
  $("open-viewer").hidden = !ready;
  $("open-cad").hidden = !status.reference_cad_ready;
  $("result-status").textContent = ready
    ? "원본 CFD의 검토용 3D 형상, 단면, 온도·수렴 그래프가 준비됐습니다. 격자 독립성·교차검증 전에는 논문 최종값으로 사용하지 마세요."
    : calculationComplete
      ? "1,800초 CFD 계산과 S2 검증이 끝났습니다. 3D 형상·단면·그래프는 실제 원본 프레임을 포장·검증한 후 열립니다."
      : "현재 과도 계산 또는 결과 검증이 끝나지 않았습니다. 이전·부분 결과를 최종 3D 결과로 표시하지 않습니다.";
  const fourRuns = state.fourCaseManifest?.runs;
  const hasFourRuns = ready && Array.isArray(fourRuns) && fourRuns.length >= 3;
  const english = new URLSearchParams(location.search).get("lang") === "en";
  $("result-notice").textContent = hasFourRuns
    ? english
      ? `${fourRuns.length} CFD results are available: ${fourRuns.map(run => run.label_en.split(" · ")[0]).join(", ")}.${fourRuns.length === 3 ? " Ø21 will appear after calculation and validation." : ""}`
      : `${fourRuns.map(run => run.label.split(" · ")[0]).join("·")} 계산 결과 ${fourRuns.length}개가 연결됐습니다.${fourRuns.length === 3 ? " Ø21은 계산·검증 후 추가됩니다." : ""}`
    : ready
    ? status.comparison_mode
      ? "Ø19 mm와 Ø23 mm의 독립 CFD 결과, 검증 보고서, 실제 메시가 연결됐습니다."
      : "원본 CFD 결과와 검증 보고서가 연결됐습니다. 단일 메시 검토용입니다."
    : active ? `S2 과도 열해석 ${transient.time_s ?? 0} / 1800 s 진행 중 · 화면은 실제 로그를 10초마다 확인합니다.`
      : calculationComplete
        ? postprocess.stage === "failed"
          ? `후처리 실패: ${postprocess.message}`
          : postprocess.message || `S2 계산·검증 통과${result.energy_balance_passed ? " · 완전 에너지 수지 통과" : ""} · 3D 뷰어 후처리 대기`
        : "결과 상태를 확인 중입니다. 최종 검증 전에는 뷰어를 열지 않습니다.";

  const rows = $("comparison-rows");
  rows.replaceChildren();
  const displaySlots = hasFourRuns
    ? fourRuns.map((run) => ({ id: run.id, name: run.label, name_en: run.label_en, ready: run.data_ready,
      battery_tmax_k: run.battery_tmax_k, outlet_temperature_k: run.outlet_temperature_k }))
    : status.result_slots;
  $("result-count-pill").textContent = english ? `${displaySlots.length} Results` : `총 ${displaySlots.length}개`;
  $("result-table").setAttribute("aria-label", english
    ? `Comparison of ${displaySlots.length} CFD Results` : `CFD 결과 ${displaySlots.length}개 비교`);
  displaySlots.forEach((slot) => {
    const row = document.createElement("tr");
    const nameCell = document.createElement("td");
    nameCell.append(document.createTextNode(english && slot.name_en ? slot.name_en : slot.name));
    if (slot.id) {
      const cadLink = document.createElement("a");
      cadLink.className = "case-cad-link";
      cadLink.href = `/viewer/cad/index.html?lang=${english ? "en" : "ko"}&mesh=${encodeURIComponent(slot.id)}`;
      cadLink.textContent = "CAD ↗";
      nameCell.append(cadLink);
    }
    row.append(nameCell);
    const current = slot.name.startsWith("현재") || slot.name.startsWith("적응형");
    const slotState = slot.ready ? "3D 준비 완료" : current && calculationComplete ? "CFD 완료 · 3D 후처리" : current && active ? "계산 중" : "결과 없음";
    textCell(row, "td", slotState);
    const degrees = (kelvin) => typeof kelvin === "number" && Number.isFinite(kelvin) ? `${(kelvin - 273.15).toFixed(4)} °C` : "—";
    textCell(row, "td", slot.ready ? degrees(slot.battery_tmax_k) : "—");
    textCell(row, "td", slot.ready ? degrees(slot.outlet_temperature_k) : "—");
    rows.appendChild(row);
  });
}

async function refreshPublishedRuns() {
  if (!state.status?.viewer_ready) return;
  try {
    const manifest = await api("/viewer/four_case_manifest.json");
    if (manifest.runs?.length >= 3) {
      state.fourCaseManifest = manifest;
      renderStatus(state.status);
    }
  } catch (_) { /* Keep the last validated result list during a publication. */ }
}

function renderGeometry(geometry) {
  state.geometry = geometry;
  updateCadPresentation();
}

function currentCadMode() {
  const selected = document.querySelector('input[name="cad-mode"]:checked')?.value;
  if (selected === "upload") return "imported_step_upload";
  if (selected === "parametric") return "parametric_template";
  return "baseline";
}

function updateCadPresentation() {
  const geometry = state.geometry;
  if (!geometry) return;
  const mode = currentCadMode();
  const imported = mode === "imported_step_upload" ? state.importedPreview?.geometry : null;
  const parametric = mode === "parametric_template" ? state.parametricPreview?.derived : null;
  $("cad-badge").textContent = mode === "baseline" && geometry.verified ? "기준 CAD 검증 완료"
    : imported ? "첨부 STEP 형상 검증 완료" : parametric ? "변경 형상 사전검사 통과" : mode === "parametric_template" ? "치수 입력 대기" : "새 CAD 형상 검토 필요";
  $("tank-size").textContent = imported ? `${imported.tank_mm.map((x) => Number(x).toFixed(1)).join(" × ")} mm`
    : parametric ? `${parametric.tank_mm.map((x) => Number(x).toFixed(1)).join(" × ")} mm`
    : mode === "baseline" ? `${geometry.tank_mm.join(" × ")} mm` : "검증 전";
  $("cell-count").textContent = imported ? `${imported.battery_count}개 · 5–4–5–4–5`
    : parametric ? `${parametric.cell_count}개 · 5–4–5–4–5`
    : mode === "baseline" ? `${geometry.cell_count}개 · ${geometry.arrangement}` : "검증 전";
  $("port-size").textContent = imported ? `Ø${Number(imported.inlet.diameter_mm).toFixed(2)} / Ø${Number(imported.outlet.diameter_mm).toFixed(2)} mm`
    : parametric ? `Ø${Number(state.parametricPreview.spec.inlet_diameter_mm).toFixed(2)} / Ø${Number(state.parametricPreview.spec.outlet_diameter_mm).toFixed(2)} mm`
    : mode === "baseline" ? `Ø${geometry.inlet_diameter_mm} / Ø${geometry.outlet_diameter_mm} mm` : "검증 전";
  $("cad-source").textContent = imported ? "첨부된 3개 STEP" : parametric ? "기준 CAD 파라메트릭 변형" : mode === "baseline" ? geometry.cad_source : mode === "parametric_template" ? "생성 전" : "첨부 CAD · 미확정";
  $("geometry-note").textContent = imported ? "입·출구 원형 면, 23개 고체, 체적 관계를 확인했습니다. 메시 품질과 CFD 수렴은 계산 단계에서 별도 검증합니다."
    : parametric ? `외곽 최소 간격 ${Number(parametric.minimum_outline_gap_mm).toFixed(2)} mm · 실제 STEP/STL 생성 전 해석식 사전검사를 통과했습니다.`
      : mode === "baseline" ? geometry.note : mode === "parametric_template" ? "탱크 치수와 입·출구 Ø를 입력하면 배터리·포트 간섭을 먼저 검사합니다." : "새 CAD는 STEP 3개를 역할별로 지정한 뒤 형상·조건 확인을 누르면 계산 가능 여부를 검사합니다.";
}

function parametricRequest(request) {
  return {
    template: request.parametric_template,
    size_mode: request.tank_size_mode,
    tank_width_mm: request.tank_width_mm,
    tank_length_mm: request.tank_length_mm,
    tank_height_mm: request.tank_height_mm,
    tank_volume_l: request.tank_volume_l,
    inlet_diameter_mm: request.inlet_diameter_mm,
    outlet_diameter_mm: request.outlet_diameter_mm,
  };
}

function selectedUploadFiles() {
  const selection = {};
  for (const file of state.cadFiles) {
    if (!file.selectedRole) continue;
    if (!file.upload_token) throw new Error(`${file.filename}: 새 계산에는 STEP 파일이 필요합니다.`);
    if (selection[file.selectedRole]) throw new Error(`${CAD_ROLES[file.selectedRole]} 역할을 두 번 지정했습니다.`);
    selection[file.selectedRole] = file.upload_token;
  }
  if (Object.keys(selection).length !== 3) throw new Error("탱크·포트, 냉각수, 배터리 STEP 3개를 각각 지정하세요.");
  return selection;
}

async function inspectUploads() {
  const files = Array.from($("cad-files").files);
  const output = $("cad-file-results");
  state.cadFiles = [];
  state.importedPreview = null;
  output.replaceChildren();
  if (!files.length) { updateCadPresentation(); return; }
  for (const file of files) {
    const row = document.createElement("div");
    row.className = "file-row";
    row.textContent = `${file.name} · 검사 중…`;
    output.appendChild(row);
    try {
      const report = await api("/api/cad/inspect", {
        method: "POST",
        headers: { "Content-Type": "application/octet-stream", "X-CAD-Filename": encodeURIComponent(file.name) },
        body: file,
      });
      report.selectedRole = report.format === "STEP"
        ? report.recognized_baseline_role || (Object.keys(CAD_ROLES).find((role) => file.name.toLowerCase() === role) || "")
        : "";
      state.cadFiles.push(report);
      row.textContent = `${report.filename} · ${report.format} · ${report.verification === "EXACT_BASELINE_MATCH" ? "기준 CAD 일치" : "형상 확인 대기"}`;
      if (report.upload_token) {
        const select = document.createElement("select");
        select.setAttribute("aria-label", `${report.filename}의 CAD 역할`);
        for (const [role, label] of Object.entries({ "": "역할 선택", ...CAD_ROLES })) {
          const option = document.createElement("option");
          option.value = role;
          option.textContent = label;
          select.appendChild(option);
        }
        select.value = report.selectedRole;
        select.addEventListener("change", () => {
          report.selectedRole = select.value;
          state.importedPreview = null;
          updateCadPresentation();
        });
        row.appendChild(select);
      }
    } catch (error) {
      row.textContent = `${file.name} · ${error.message}`;
      row.classList.add("warn");
    }
  }
  updateCadPresentation();
}

function confirmationField(label, value) {
  const item = document.createElement("div");
  const heading = document.createElement("span");
  heading.textContent = label;
  const data = document.createElement("strong");
  data.textContent = String(value);
  item.append(heading, data);
  return item;
}

async function prepare(event) {
  event.preventDefault();
  const form = new FormData($("calculation-form"));
  const request = Object.fromEntries(form.entries());
  request.cad_mode = currentCadMode();
  try {
    if (request.cad_mode === "parametric_template") {
      const parameters = parametricRequest(request);
      const checked = await api("/api/parametric/prepare", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ parameters, inputs: request }),
      });
      state.parametricPreview = checked;
      state.pendingParametric = { parameters, inputs: checked.inputs, confirmation: checked.confirmation };
      state.pendingImport = null;
      updateCadPresentation();
      const derived = checked.derived;
      const fields = [
        ["CAD", "검증된 23셀 기준 CAD · 파라메트릭 변형"],
        ["탱크 X×Y×Z", `${derived.tank_mm.map((x) => Number(x).toFixed(2)).join(" × ")} mm`],
        ["탱크 체적", `${Number(derived.gross_tank_volume_l).toFixed(4)} L`],
        ["입구 / 출구 구경", `Ø${Number(checked.spec.inlet_diameter_mm).toFixed(2)} / Ø${Number(checked.spec.outlet_diameter_mm).toFixed(2)} mm`],
        ["배터리", `${derived.cell_count}개 · Ø19×68 mm · 배열 고정`],
        ["셀–외곽 최소 간격", `${Number(derived.minimum_outline_gap_mm).toFixed(3)} mm`],
        ["입구 / 출구 외곽 여유", `${Number(derived.inlet_outline_clearance_mm).toFixed(3)} / ${Number(derived.outlet_outline_clearance_mm).toFixed(3)} mm`],
        ["냉각수", "탈이온수"],
        ["입구 온도", `${checked.inputs.inlet_temperature_k} K`],
        ["입구 속도", `${checked.inputs.inlet_velocity_m_s} m/s`],
        ["출구 절대압력", `${checked.inputs.outlet_pressure_pa} Pa`],
        ["목표 메시", `${Number(checked.inputs.mesh_target_cells).toLocaleString()} cells`],
        ["열원", "시간가변 2C · 0–1800 s"],
        ["이번 실행 범위", "CAD → 메시 → S0 → S1 → S2 → 결과 뷰어"],
      ];
      $("confirmation-details").replaceChildren(...fields.map(([label, value]) => confirmationField(label, value)));
      $("confirmation-message").textContent = `해석식 간섭 검사를 통과했습니다. 확인 후 STEP/STL 생성·재검증을 거쳐 메시·S0·S1·S2 실제 계산과 결과 뷰어 생성을 시작합니다. 디스크 여유 ${checked.disk_free_gib} GiB / 최소 ${checked.disk_minimum_gib} GiB.${checked.can_start_new_solver ? "" : " 현재 공간으로는 새 계산 시작을 막습니다."}`;
      $("attach-run").hidden = true;
      $("geometry-confirmation").hidden = false;
      $("geometry-confirmed").checked = false;
      $("generate-parametric-cad").hidden = false;
      $("generate-parametric-cad").disabled = true;
      $("generate-parametric-cad").dataset.canStart = checked.can_start_new_solver ? "true" : "false";
      $("start-imported-run").hidden = true;
      $("confirm-dialog").showModal();
      return;
    }
    const uploading = request.cad_mode === "imported_step_upload";
    let selected = uploading ? selectedUploadFiles() : null;
    let checked = await api(uploading ? "/api/import/prepare" : "/api/prepare", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(uploading ? { files: selected, inputs: request } : request),
    });
    const baselineNew = !uploading && !checked.can_view_current_run;
    if (baselineNew) {
      checked = await api("/api/baseline/prepare", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(request),
      });
      selected = checked.files;
    }
    const newRun = uploading || baselineNew;
    state.pendingImport = newRun ? { files: selected, inputs: request, confirmation: checked.confirmation } : null;
    state.importedPreview = uploading ? checked : null;
    updateCadPresentation();
    const geometry = uploading ? checked.geometry : state.geometry;
    const fields = [
      ["CAD", uploading ? "첨부 STEP 3개 · 형상 검증 통과" : geometry.cad_source],
      ["탱크 / 배터리", `${geometry.tank_mm.map((x) => Number(x).toFixed(1)).join("×")} mm · ${uploading ? geometry.battery_count : geometry.cell_count}개`],
      ["입구 / 출구 구경", uploading ? `Ø${Number(geometry.inlet.diameter_mm).toFixed(2)} / Ø${Number(geometry.outlet.diameter_mm).toFixed(2)} mm` : `Ø${geometry.inlet_diameter_mm} / Ø${geometry.outlet_diameter_mm} mm`],
      ...(uploading ? [["입구 / 출구 중심", `${geometry.inlet.center_xy_mm.map((x) => Number(x).toFixed(1)).join(", ")} / ${geometry.outlet.center_xy_mm.map((x) => Number(x).toFixed(1)).join(", ")} mm`]] : []),
      ["냉각수", "탈이온수"],
      ["입구 온도", `${checked.inputs.inlet_temperature_k} K`],
      ["입구 속도", `${checked.inputs.inlet_velocity_m_s} m/s`],
      ["출구 절대압력", `${checked.inputs.outlet_pressure_pa} Pa`],
      ["목표 메시", `${Number(checked.inputs.mesh_target_cells).toLocaleString()} cells`],
      ["열원", "시간가변 2C · 0–1800 s"],
    ];
    $("confirmation-details").replaceChildren(...fields.map(([label, value]) => confirmationField(label, value)));
    $("confirmation-message").textContent = newRun
      ? `${uploading ? "첨부 CAD" : "기준 CAD"}로 별도 메시·S0·S1·S2 계산을 실행합니다. 실제 메시 수와 품질, 수렴, 에너지 수지가 통과해야 3D 결과를 공개합니다. 디스크 여유 ${checked.disk_free_gib} GiB / 최소 ${checked.disk_minimum_gib} GiB.${checked.can_start_new_solver ? "" : " 현재 공간으로는 새 계산 시작을 막습니다."}`
      : checked.message;
    $("attach-run").hidden = newRun;
    $("attach-run").disabled = newRun || !checked.can_view_current_run;
    $("geometry-confirmation").hidden = !newRun;
    $("geometry-confirmed").checked = false;
    $("generate-parametric-cad").hidden = true;
    $("generate-parametric-cad").disabled = true;
    $("start-imported-run").hidden = !newRun;
    $("start-imported-run").disabled = true;
    $("start-imported-run").dataset.canStart = newRun && checked.can_start_new_solver ? "true" : "false";
    $("confirm-dialog").showModal();
  } catch (error) {
    alert(error.message);
  }
}

function renderParametricResult(result) {
  const box = $("parametric-result");
  box.hidden = false;
  box.replaceChildren();
  const title = document.createElement("strong");
  title.textContent = "파라메트릭 CAD 생성·형상검사 완료";
  const message = document.createElement("div");
  message.textContent = `${result.message} 탱크 ${result.derived.tank_mm.map((x) => Number(x).toFixed(2)).join(" × ")} mm · ${Number(result.derived.gross_tank_volume_l).toFixed(4)} L`;
  const links = document.createElement("div");
  links.className = "parametric-downloads";
  const labels = {
    "enclosure_with_ports.step": "탱크·포트 STEP",
    "coolant.step": "냉각수 STEP",
    "battery_cells.step": "배터리 STEP",
    "coolant.stl": "냉각수 STL",
    "battery_cells.stl": "배터리 STL",
    "cad_result.json": "검사 JSON",
  };
  for (const [filename, label] of Object.entries(labels)) {
    const href = result.download_urls?.[filename];
    if (!href) continue;
    const link = document.createElement("a");
    link.href = href;
    link.download = filename;
    link.textContent = label;
    links.appendChild(link);
  }
  box.append(title, message, links);
}

async function startParametricRun() {
  if (!state.pendingParametric || !$("geometry-confirmed").checked) return;
  const button = $("generate-parametric-cad");
  button.disabled = true;
  button.textContent = "CAD 생성 및 CFD 준비 중…";
  try {
    const started = await api("/api/parametric/start", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...state.pendingParametric, geometry_confirmed: true }),
    });
    state.parametricPreview = started.parametric;
    updateCadPresentation();
    renderParametricResult(started.parametric);
    state.activeRun = started.id;
    localStorage.setItem("yuFluidImportedRun", started.id);
    $("confirm-dialog").close();
    button.textContent = "새 CFD 계산 시작";
    showView("results");
    await refreshImportedRun();
  } catch (error) {
    alert(`CAD 생성 또는 CFD 시작 실패: ${error.message}`);
    button.disabled = false;
    button.textContent = "새 CFD 계산 시작";
  }
}

function renderImportedRun(job) {
  const panel = $("imported-run-panel");
  panel.hidden = false;
  $("imported-run-state").textContent = job.status === "complete" ? "검증·후처리 완료"
    : job.status === "failed" ? "계산 실패·확인 필요"
      : job.status === "cancelled" ? "사용자 중지"
        : job.status === "stopping" ? "중지 처리 중"
          : job.status === "queued" ? "대기" : "실제 계산 중";
  $("imported-run-message").textContent = [job.message, job.diagnostic].filter(Boolean).join(" · ") || "단계 상태를 확인 중입니다.";
  const progress = job.progress;
  const stageList = $("imported-run-stages");
  stageList.replaceChildren();
  if (progress?.stages) {
    const stageId = job.stage === "steady"
      ? (progress.stages.some((stage) => stage.id === "cold_flow" && stage.status === "passed") ? "steady_cht" : "cold_flow")
      : job.stage === "validation" && job.message?.startsWith("mesh:") ? "mesh" : job.stage;
    progress.stages.forEach((stage, index) => {
      const item = document.createElement("div");
      const displayStatus = stage.id === stageId && ["queued", "running", "stopping", "failed", "cancelled"].includes(job.status)
        ? job.status === "failed" ? "needs_check" : job.status === "cancelled" ? "cancelled" : "running"
        : stage.status;
      item.className = `stage ${displayStatus}`;
      const title = document.createElement("strong");
      title.textContent = `${String(index + 1).padStart(2, "0")} · ${stage.label}`;
      item.append(title, document.createTextNode(statusNames[displayStatus] || displayStatus));
      stageList.append(item);
    });
  }
  const transient = progress?.transient;
  $("imported-run-progress").textContent = transient?.time_s !== null && transient?.time_s !== undefined
    ? `실제 과도 시간 ${transient.time_s} / 1800 s · 현재 에너지 결합 ${transient.energy_coupling_iteration ?? "—"} / ${transient.energy_coupling_limit ?? 200}회`
    : `현재 단계: ${job.stage || "대기"} · 메시/해석 로그에서 실제 진행 상태를 읽습니다.`;
  if (transient?.energy_coupling_audit?.time_step_count) {
    $("imported-run-progress").textContent += ` · 조기 종료 ${transient.energy_coupling_audit.early_exit_count}단계 / 상한 도달 ${transient.energy_coupling_audit.cap_reached_count}단계`;
  }
  $("open-imported-viewer").hidden = !job.viewer_url;
  if (job.viewer_url) $("open-imported-viewer").href = job.viewer_url;
  const cancel = $("cancel-imported-run");
  cancel.hidden = !["queued", "running", "stopping"].includes(job.status);
  cancel.disabled = job.status === "stopping";
  cancel.textContent = job.status === "stopping" ? "중지 처리 중…" : "계산 중지";
  const reset = $("reset-imported-run");
  reset.disabled = !["complete", "failed", "cancelled"].includes(job.status);
  reset.title = reset.disabled ? "진행 중인 계산은 먼저 중지하세요." : "계산 기록은 보존하고 입력 화면만 초기화합니다.";
}

async function cancelImportedRun() {
  if (!state.activeRun || !window.confirm("이 새 CAD 계산을 중지할까요? 현재 단계의 Docker 작업만 종료하며 부분 파일은 보존합니다.")) return;
  const button = $("cancel-imported-run");
  button.disabled = true;
  try {
    const job = await api(`/api/runs/${state.activeRun}/cancel`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
    });
    renderImportedRun(job);
  } catch (error) {
    alert(`계산 중지 요청 실패: ${error.message}`);
    button.disabled = false;
  }
}

async function refreshImportedRun() {
  if (!state.activeRun) return;
  const runId = state.activeRun;
  try {
    const job = await api(`/api/runs/${runId}`);
    if (state.activeRun === runId) renderImportedRun(job);
  } catch (error) {
    if (state.activeRun === runId) $("imported-run-message").textContent = `새 계산 상태를 읽지 못했습니다: ${error.message}`;
  }
}

async function resetImportedRun() {
  const runId = state.activeRun;
  if (!runId) return;
  try {
    const job = await api(`/api/runs/${runId}`);
    if (state.activeRun !== runId) return;
    if (!["complete", "failed", "cancelled"].includes(job.status)) {
      renderImportedRun(job);
      alert("진행 중인 계산은 먼저 중지하고 종료될 때까지 기다린 뒤 초기화하세요.");
      return;
    }
    if (!window.confirm("새 CAD 입력을 처음부터 다시 설정할까요? 기존 계산 폴더·로그·결과는 삭제하지 않습니다.")) return;
    state.activeRun = null;
    localStorage.removeItem("yuFluidImportedRun");
    state.cadFiles = [];
    state.importedPreview = null;
    state.pendingImport = null;
    $("imported-run-panel").hidden = true;
    $("calculation-form").reset();
    $("cad-files").value = "";
    $("cad-file-results").replaceChildren();
    document.querySelector('input[name="cad-mode"][value="upload"]').checked = true;
    $("upload-area").hidden = false;
    $("geometry-confirmed").checked = false;
    $("confirmation-details").replaceChildren();
    updateCadPresentation();
    showView("calculate");
  } catch (error) {
    alert(`새 CAD 계산 초기화 실패: ${error.message}`);
  }
}

async function startImportedRun() {
  if (!state.pendingImport || !$("geometry-confirmed").checked) return;
  const button = $("start-imported-run");
  button.disabled = true;
  try {
    const started = await api("/api/import/start", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...state.pendingImport, geometry_confirmed: true }),
    });
    state.activeRun = started.id;
    localStorage.setItem("yuFluidImportedRun", started.id);
    $("confirm-dialog").close();
    showView("results");
    await refreshImportedRun();
  } catch (error) {
    alert(error.message);
    button.disabled = false;
  }
}

document.addEventListener("DOMContentLoaded", async () => {
  $("go-start").addEventListener("click", () => showView("calculate"));
  $("go-graphics").addEventListener("click", () => showView("results"));
  document.querySelectorAll(".home").forEach((button) => button.addEventListener("click", () => showView("landing")));
  document.querySelectorAll('input[name="cad-mode"]').forEach((radio) => radio.addEventListener("change", () => {
    const selected = document.querySelector('input[name="cad-mode"]:checked')?.value;
    $("upload-area").hidden = selected !== "upload";
    $("parametric-area").hidden = selected !== "parametric";
    updateCadPresentation();
  }));
  $("tank-size-mode").addEventListener("change", () => {
    const volumeMode = $("tank-size-mode").value === "volume";
    $("tank-dimension-fields").hidden = volumeMode;
    $("tank-volume-field").hidden = !volumeMode;
    state.parametricPreview = null;
    updateCadPresentation();
  });
  $("parametric-area").querySelectorAll("input, select").forEach((control) => control.addEventListener("input", () => {
    state.parametricPreview = null;
    updateCadPresentation();
  }));
  $("cad-files").addEventListener("change", inspectUploads);
  $("calculation-form").addEventListener("submit", prepare);
  $("attach-run").addEventListener("click", () => showView("results"));
  $("geometry-confirmed").addEventListener("change", () => {
    $("start-imported-run").disabled = !$("geometry-confirmed").checked || $("start-imported-run").dataset.canStart !== "true";
    $("generate-parametric-cad").disabled = !$("geometry-confirmed").checked || !state.pendingParametric || $("generate-parametric-cad").dataset.canStart !== "true";
  });
  $("generate-parametric-cad").addEventListener("click", startParametricRun);
  $("start-imported-run").addEventListener("click", startImportedRun);
  $("cancel-imported-run").addEventListener("click", cancelImportedRun);
  $("reset-imported-run").addEventListener("click", resetImportedRun);
  state.activeRun = localStorage.getItem("yuFluidImportedRun");
  showView(["results", "calculate"].includes(location.hash.slice(1)) ? location.hash.slice(1) : "landing");
  try {
    renderGeometry(await api("/api/geometry"));
    renderStatus(await api("/api/status"));
    await refreshPublishedRuns();
    await refreshImportedRun();
  } catch (error) {
    $("live-state").textContent = `상태를 읽지 못했습니다: ${error.message}`;
  }
  setInterval(async () => {
    try {
      renderStatus(await api("/api/status"));
      await refreshPublishedRuns();
    } catch (_) { $("live-state").textContent = "상태 연결 끊김"; }
    await refreshImportedRun();
  }, 10000);
});
