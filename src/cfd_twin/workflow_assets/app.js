"use strict";

const $ = (id) => document.getElementById(id);
const CAD_ROLES = {
  "enclosure_with_ports.step": "탱크·포트 외형",
  "coolant.step": "냉각수 유체 영역",
  "battery_cells.step": "23개 배터리 고체 영역",
};
const state = { geometry: null, status: null, cadFiles: [], importedPreview: null, pendingImport: null, activeRun: null };
const statusNames = { passed: "검증 통과", running: "계산 중", pending: "대기", needs_check: "확인 필요" };

async function api(path, options = {}) {
  const response = await fetch(path, { cache: "no-store", ...options });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
  return payload;
}

function showView(name) {
  $("results-view").hidden = name !== "results";
  $("calculate-view").hidden = name !== "calculate";
  document.querySelector(".hero").hidden = name !== "home";
  history.replaceState(null, "", name === "home" ? location.pathname : `#${name}`);
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
  $("mesh-count").textContent = status.mesh_cells ? `${Number(status.mesh_cells).toLocaleString()} cells` : "메시 대기";
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
  $("result-status").textContent = ready
    ? "원본 CFD의 검토용 3D 형상, 단면, 온도·수렴 그래프가 준비됐습니다. 격자 독립성·교차검증 전에는 논문 최종값으로 사용하지 마세요."
    : calculationComplete
      ? "1,800초 CFD 계산과 S2 검증이 끝났습니다. 3D 형상·단면·그래프는 실제 원본 프레임을 포장·검증한 후 열립니다."
      : "현재 과도 계산 또는 결과 검증이 끝나지 않았습니다. 이전·부분 결과를 최종 3D 결과로 표시하지 않습니다.";
  $("result-notice").textContent = ready
    ? "원본 CFD 결과와 검증 보고서가 연결됐습니다. 단일 메시 검토용입니다."
    : active ? `S2 과도 열해석 ${transient.time_s ?? 0} / 1800 s 진행 중 · 화면은 실제 로그를 10초마다 확인합니다.`
      : calculationComplete
        ? postprocess.stage === "failed"
          ? `후처리 실패: ${postprocess.message}`
          : postprocess.message || `S2 계산·검증 통과${result.energy_balance_passed ? " · 완전 에너지 수지 통과" : ""} · 3D 뷰어 후처리 대기`
        : "결과 상태를 확인 중입니다. 최종 검증 전에는 뷰어를 열지 않습니다.";

  const rows = $("comparison-rows");
  rows.replaceChildren();
  status.result_slots.forEach((slot) => {
    const row = document.createElement("tr");
    textCell(row, "td", slot.name);
    const current = slot.name.startsWith("현재");
    const slotState = slot.ready ? "3D 준비 완료" : current && calculationComplete ? "CFD 완료 · 3D 후처리" : current && active ? "계산 중" : "결과 없음";
    textCell(row, "td", slotState);
    const degrees = (kelvin) => typeof kelvin === "number" && Number.isFinite(kelvin) ? `${(kelvin - 273.15).toFixed(4)} °C` : "—";
    textCell(row, "td", current && calculationComplete ? degrees(result.battery_tmax_k) : "—");
    textCell(row, "td", current && calculationComplete ? degrees(result.outlet_temperature_k) : "—");
    rows.appendChild(row);
  });
}

function renderGeometry(geometry) {
  state.geometry = geometry;
  updateCadPresentation();
}

function currentCadMode() {
  const selected = document.querySelector('input[name="cad-mode"]:checked')?.value;
  if (selected === "baseline") return "baseline";
  return "imported_step_upload";
}

function updateCadPresentation() {
  const geometry = state.geometry;
  if (!geometry) return;
  const mode = currentCadMode();
  const imported = mode === "imported_step_upload" ? state.importedPreview?.geometry : null;
  $("cad-badge").textContent = mode === "baseline" && geometry.verified ? "기준 CAD 검증 완료"
    : imported ? "첨부 STEP 형상 검증 완료" : "새 CAD 형상 검토 필요";
  $("tank-size").textContent = imported ? `${imported.tank_mm.map((x) => Number(x).toFixed(1)).join(" × ")} mm`
    : mode === "baseline" ? `${geometry.tank_mm.join(" × ")} mm` : "검증 전";
  $("cell-count").textContent = imported ? `${imported.battery_count}개 · 5–4–5–4–5`
    : mode === "baseline" ? `${geometry.cell_count}개 · ${geometry.arrangement}` : "검증 전";
  $("port-size").textContent = imported ? `Ø${Number(imported.inlet.diameter_mm).toFixed(2)} / Ø${Number(imported.outlet.diameter_mm).toFixed(2)} mm`
    : mode === "baseline" ? `Ø${geometry.inlet_diameter_mm} / Ø${geometry.outlet_diameter_mm} mm` : "검증 전";
  $("cad-source").textContent = imported ? "첨부된 3개 STEP" : mode === "baseline" ? geometry.cad_source : "첨부 CAD · 미확정";
  $("geometry-note").textContent = imported ? "입·출구 원형 면, 23개 고체, 체적 관계를 확인했습니다. 메시 품질과 CFD 수렴은 계산 단계에서 별도 검증합니다."
    : mode === "baseline" ? geometry.note : "새 CAD는 STEP 3개를 역할별로 지정한 뒤 형상·조건 확인을 누르면 계산 가능 여부를 검사합니다.";
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
    $("start-imported-run").hidden = !newRun;
    $("start-imported-run").disabled = true;
    $("start-imported-run").dataset.canStart = newRun && checked.can_start_new_solver ? "true" : "false";
    $("confirm-dialog").showModal();
  } catch (error) {
    alert(error.message);
  }
}

function renderImportedRun(job) {
  const panel = $("imported-run-panel");
  panel.hidden = false;
  $("imported-run-state").textContent = job.status === "complete" ? "검증·후처리 완료"
    : job.status === "failed" ? "계산 실패·확인 필요" : job.status === "queued" ? "대기" : "실제 계산 중";
  $("imported-run-message").textContent = job.message || "단계 상태를 확인 중입니다.";
  const progress = job.progress;
  const stageList = $("imported-run-stages");
  stageList.replaceChildren();
  if (progress?.stages) {
    progress.stages.forEach((stage, index) => {
      const item = document.createElement("div");
      item.className = `stage ${stage.status}`;
      const title = document.createElement("strong");
      title.textContent = `${String(index + 1).padStart(2, "0")} · ${stage.label}`;
      item.append(title, document.createTextNode(statusNames[stage.status] || stage.status));
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
}

async function refreshImportedRun() {
  if (!state.activeRun) return;
  try { renderImportedRun(await api(`/api/runs/${state.activeRun}`)); }
  catch (error) { $("imported-run-message").textContent = `새 계산 상태를 읽지 못했습니다: ${error.message}`; }
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
  $("go-results").addEventListener("click", () => showView("results"));
  $("go-calculate").addEventListener("click", () => showView("calculate"));
  document.querySelectorAll(".home").forEach((button) => button.addEventListener("click", () => showView("home")));
  document.querySelectorAll('input[name="cad-mode"]').forEach((radio) => radio.addEventListener("change", () => {
    $("upload-area").hidden = document.querySelector('input[name="cad-mode"]:checked')?.value !== "upload";
    updateCadPresentation();
  }));
  $("cad-files").addEventListener("change", inspectUploads);
  $("calculation-form").addEventListener("submit", prepare);
  $("attach-run").addEventListener("click", () => showView("results"));
  $("geometry-confirmed").addEventListener("change", () => {
    $("start-imported-run").disabled = !$("geometry-confirmed").checked || $("start-imported-run").dataset.canStart !== "true";
  });
  $("start-imported-run").addEventListener("click", startImportedRun);
  state.activeRun = localStorage.getItem("yuFluidImportedRun");
  showView(["results", "calculate"].includes(location.hash.slice(1)) ? location.hash.slice(1) : "home");
  try {
    renderGeometry(await api("/api/geometry"));
    renderStatus(await api("/api/status"));
    await refreshImportedRun();
  } catch (error) {
    $("live-state").textContent = `상태를 읽지 못했습니다: ${error.message}`;
  }
  setInterval(async () => {
    try { renderStatus(await api("/api/status")); } catch (_) { $("live-state").textContent = "상태 연결 끊김"; }
    await refreshImportedRun();
  }, 10000);
});
