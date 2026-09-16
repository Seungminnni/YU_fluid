"use strict";

const $ = (id) => document.getElementById(id);
const state = { geometry: null, status: null, cadFiles: [] };
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
  const recognized = new Set(state.cadFiles.map((file) => file.recognized_baseline_role).filter(Boolean));
  if (state.cadFiles.length === 3 && recognized.size === 3) return "exact_baseline_upload";
  return "unverified_upload";
}

function updateCadPresentation() {
  const geometry = state.geometry;
  if (!geometry) return;
  const mode = currentCadMode();
  const known = mode !== "unverified_upload";
  $("cad-badge").textContent = mode === "exact_baseline_upload"
    ? "첨부 CAD · 기준 형상과 완전 일치"
    : mode === "baseline" && geometry.verified ? "기준 CAD 검증 완료" : "새 CAD 형상 검토 필요";
  $("tank-size").textContent = known ? `${geometry.tank_mm.join(" × ")} mm` : "검증 전";
  $("cell-count").textContent = known ? `${geometry.cell_count}개 · ${geometry.arrangement}` : "검증 전";
  $("port-size").textContent = known ? `Ø${geometry.inlet_diameter_mm} / Ø${geometry.outlet_diameter_mm} mm` : "검증 전";
  $("cad-source").textContent = known ? geometry.cad_source : "첨부 CAD · 미확정";
  $("geometry-note").textContent = known ? geometry.note
    : "새 CAD는 파일 형식만 확인합니다. 단위, 닫힌 체적, 배터리 영역, 입구·출구 면과 구경을 검증하기 전에는 계산에 연결되지 않습니다.";
}

async function inspectUploads() {
  const files = Array.from($("cad-files").files);
  const output = $("cad-file-results");
  state.cadFiles = [];
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
      state.cadFiles.push(report);
      row.textContent = `${report.filename} · ${report.role_label} · ${report.verification === "EXACT_BASELINE_MATCH" ? "기준 CAD 일치" : "형상 검토 필요"}`;
      row.classList.toggle("warn", report.verification !== "EXACT_BASELINE_MATCH");
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
    const checked = await api("/api/prepare", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(request),
    });
    const geometry = state.geometry;
    const fields = [
      ["CAD", geometry.cad_source],
      ["탱크 / 배터리", `${geometry.tank_mm.join("×")} mm · ${geometry.cell_count}개`],
      ["입구 / 출구 구경", `Ø${geometry.inlet_diameter_mm} / Ø${geometry.outlet_diameter_mm} mm`],
      ["냉각수", "탈이온수"],
      ["입구 온도", `${checked.inputs.inlet_temperature_k} K`],
      ["입구 속도", `${checked.inputs.inlet_velocity_m_s} m/s`],
      ["출구 절대압력", `${checked.inputs.outlet_pressure_pa} Pa`],
      ["목표 메시", `${Number(checked.inputs.mesh_target_cells).toLocaleString()} cells`],
      ["열원", "시간가변 2C · 0–1800 s"],
    ];
    $("confirmation-details").replaceChildren(...fields.map(([label, value]) => confirmationField(label, value)));
    $("confirmation-message").textContent = checked.message;
    $("attach-run").disabled = !checked.can_view_current_run;
    $("confirm-dialog").showModal();
  } catch (error) {
    alert(error.message);
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
  showView(["results", "calculate"].includes(location.hash.slice(1)) ? location.hash.slice(1) : "home");
  try {
    renderGeometry(await api("/api/geometry"));
    renderStatus(await api("/api/status"));
  } catch (error) {
    $("live-state").textContent = `상태를 읽지 못했습니다: ${error.message}`;
  }
  setInterval(async () => {
    try { renderStatus(await api("/api/status")); } catch (_) { $("live-state").textContent = "상태 연결 끊김"; }
  }, 10000);
});
