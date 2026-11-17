"""Separate, portable mesh-only viewer sourced from the computed OpenFOAM mesh."""

from __future__ import annotations

import base64
import gzip
import json
import re
import shutil
from pathlib import Path

from .mesh_wireframe import extract_complete_volume_edges, extract_full_mesh_views


HTML = """<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>실제 계산 메시 보기</title>
  <link rel="stylesheet" href="mesh.css">
</head>
<body>
  <header>
    <div class="heading"><a id="mesh-home" href="index.html" class="back">← 처음으로</a>
      <a href="results.html" class="back">결과 화면</a><h1>메시 보기</h1><span id="count"></span></div>
    <div class="modes" role="group" aria-label="메시 표시 방식">
      <div class="language-switch" role="group" aria-label="Language / 언어"><button type="button" data-language="ko" aria-pressed="true">한국어</button><button type="button" data-language="en" aria-pressed="false">English</button></div>
      <button type="button" data-mode="surface" aria-pressed="true">3D 경계</button>
      <button type="button" data-mode="volume" aria-pressed="false">3D 전체 격자</button>
      <button type="button" data-mode="interior" aria-pressed="false">3D 내부 절개</button>
      <label class="cut-controls" id="cut-controls" hidden>절개 위치
        <input id="cut-x" type="range" min="25" max="75" step="25" value="50" aria-label="3D 내부 절개 X 위치">
        <span id="cut-readout">50%</span>
      </label>
      <button type="button" data-mode="xy" aria-pressed="false">가로 XY 단면</button>
      <button type="button" data-mode="xz" aria-pressed="false">세로 XZ 단면</button>
    </div>
  </header>
  <main>
    <div class="viewbar"><span id="plane">실제 OpenFOAM polyMesh · 경계선</span>
      <div class="cameras" role="group" aria-label="카메라 시점">
        <button type="button" data-view="iso">등각</button><button type="button" data-view="top">+Z</button>
        <button type="button" data-view="side">+Y</button><button type="button" data-view="front">+X</button>
        <button type="button" id="fit">맞춤</button>
        <button type="button" id="save-png" aria-label="현재 메시 화면을 PNG로 저장" disabled>PNG 저장 ↓</button>
      </div>
    </div>
    <div class="viewport"><canvas id="mesh" aria-label="회전 및 확대 가능한 실제 CFD 계산 메시"></canvas>
      <div id="loading" role="status">실제 계산 격자를 불러오는 중…</div>
      <div class="legend"><span class="fluid"></span> 냉각수 영역 <span class="solid"></span> 배터리 영역</div>
    </div>
    <div class="foot"><span id="detail">3D 전체 격자는 모든 셀 에지를 유지하며 깊이와 외곽 경계를 구분해 표시합니다.</span>
      <span>드래그: 회전 · 휠: 확대</span></div>
  </main>
  <script src="mesh-data.js"></script>
  <script src="mesh-volume-data.js"></script>
  <script src="i18n.js"></script>
  <script src="mesh.js"></script>
</body>
</html>
"""


def enable_multi_case_mesh_selection(html: str) -> str:
    """Add the result selector and switch to per-run mesh assets.

    The standalone mesh viewer keeps its two default assets.  A combined
    viewer instead loads ``four_case_data.js`` first; ``mesh.js`` then loads
    the boundary and full-volume mesh belonging to the selected result ID.
    """

    if 'id="mesh-case-select"' not in html:
        marker = '<h1>메시 보기</h1><span id="count"></span>'
        if html.count(marker) != 1:
            raise ValueError("mesh page title marker missing")
        html = html.replace(
            marker,
            '<h1>메시 보기</h1><label class="mesh-case-control" for="mesh-case-select">'
            '결과 선택 <select id="mesh-case-select"></select></label><span id="count"></span>',
        )
    static_scripts = (
        '  <script src="mesh-data.js"></script>\n'
        '  <script src="mesh-volume-data.js"></script>'
    )
    if static_scripts in html:
        html = html.replace(
            static_scripts,
            '  <script src="four_case_data.js"></script>',
            1,
        )
    elif 'src="four_case_data.js"' not in html:
        raise ValueError("mesh page has no case-data script marker")
    return html

_home_template = (Path(__file__).with_name("workflow_assets") / "index.html").read_text(encoding="utf-8")
_home_template = re.sub(r'href="/style\.css[^\"]*"', 'href="portal.css?v=button-text-1"', _home_template)
_home_template = re.sub(r'src="/viewer/i18n\.js[^\"]*"', 'src="i18n.js?v=ko-en-7"', _home_template)
_home_template = re.sub(r'src="/app\.js[^\"]*"', 'src="offline-data.js"></script>\n  <script src="offline-app.js?v=demo-workflow-1"', _home_template)
HOME_HTML = (
    _home_template
    .replace('src="/cheoma-logo.png"', 'src="cheoma-logo.png"')
    .replace('href="/viewer/index.html"', 'href="results.html"')
    .replace('href="/viewer/results.html"', 'href="results.html"')
    .replace('href="/viewer/cad/index.html"', 'href="cad/index.html"')
)

OFFLINE_JAVASCRIPT = r'''"use strict";
const $=id=>document.getElementById(id),summary=window.CFD_OFFLINE_SUMMARY||{};
const demo={active:false,status:"idle",step:0,timer:null,lastMode:"baseline",files:[]};
const language=()=>window.CFDTwinI18n?.getLanguage?.()||new URLSearchParams(location.search).get("lang")||"ko";
const tx=(ko,en)=>language()==="en"?en:ko;
const locale=()=>language()==="en"?"en-US":"ko-KR";
const number=value=>Number(value).toLocaleString(locale());
const stageLabels=()=>language()==="en"
  ?["CAD Validation","Mesh Generation and Quality","S0 Cold Flow","S1 Steady CHT","S2 Transient Thermal Analysis","Post-processing and 3D Viewer"]
  :["CAD 확인","메시 생성·품질","S0 냉간 유동","S1 정상 CHT","S2 과도 열해석","결과 후처리·3D 뷰어"];

function show(view){
  $("landing-view").hidden=view!=="landing";
  $("results-view").hidden=view!=="results";
  $("calculate-view").hidden=view!=="calculate";
  const here=new URL(location.href);here.hash=view==="landing"?"":view;
  try{history.replaceState(null,"",here.href);}catch(_error){/* file:// can restrict History API */}
  window.scrollTo(0,0);
}

function currentMode(){return document.querySelector('input[name="cad-mode"]:checked')?.value||"baseline";}
function fieldValue(name,fallback){return document.querySelector(`[name="${name}"]`)?.value||fallback;}
function dimensions(){
  if(currentMode()!=="parametric")return summary.tank_mm||[114,121,82];
  if(fieldValue("tank_size_mode","dimensions")==="volume"){
    const litres=Number(fieldValue("tank_volume_l","1.07"));
    const scale=Math.cbrt(litres/1.07);return [114*scale,121*scale,82*scale];
  }
  return ["tank_width_mm","tank_length_mm","tank_height_mm"].map((name,index)=>Number(fieldValue(name,[114,121,82][index])));
}
function diameters(){
  if(currentMode()!=="parametric")return [Number(summary.port_diameter_mm||19),Number(summary.port_diameter_mm||19)];
  return [Number(fieldValue("inlet_diameter_mm",19)),Number(fieldValue("outlet_diameter_mm",19))];
}
function modeCadName(mode=currentMode()){
  if(mode==="parametric")return tx("검증된 23셀 기준 CAD · 파라메트릭 변형","Verified 23-cell Reference CAD · Parametric Variant");
  if(mode==="upload")return tx("첨부 STEP 3개 · 형상 검증 통과","Three Uploaded STEP Files · Geometry Validation Passed");
  return tx("검증된 23셀 기준 CAD","Verified 23-cell Reference CAD");
}

function renderSavedStages(){
  const stages=$("stage-list");stages.replaceChildren();
  for(const [index,label] of stageLabels().entries()){
    const item=document.createElement("div");item.className="stage passed";
    const strong=document.createElement("strong");strong.textContent=`${String(index+1).padStart(2,"0")} · ${label}`;
    item.append(strong,document.createTextNode(tx("검증 통과","Validation passed")));stages.append(item);
  }
}

function renderSavedResult(){
  const ready=Boolean(summary.actual_solver_data);
  $("live-state").textContent=ready?tx("3D 결과 준비 완료","3D results ready"):tx("계산 상태 확인 필요","Calculation status needs review");
  $("result-notice").textContent=ready
    ?tx("원본 CFD 결과와 검증 보고서가 연결됐습니다. 모든 결과 보기 기능을 사용할 수 있습니다.","The original CFD results and validation report are linked. All result-viewing features are available.")
    :tx("계산 결과 데이터가 없습니다.","No calculation result data are available.");
  $("result-status").textContent=ready
    ?tx("저장된 3D 결과와 온도·수렴 그래프가 준비됐습니다.","The saved 3D results, temperature histories, and convergence plots are ready.")
    :tx("검증된 결과 없음","No validated results");
  $("open-viewer").hidden=!ready;$("open-viewer").href="results.html";
  $("open-cad").hidden=!ready||!summary.reference_cad_ready;$("open-cad").href="cad/index.html";
  $("mesh-count").textContent=summary.mesh_cells?`${number(summary.mesh_cells)} ${tx("셀","cells")}`:tx("메시 대기","Mesh pending");
  $("physical-time").textContent=ready?"1800 / 1800 s":"— / 1800 s";
  $("progress-label").textContent=ready?"100%":"—";$("transient-progress").value=ready?100:0;
  $("coupling-state").hidden=true;
  renderSavedStages();
  const rows=$("comparison-rows");rows.replaceChildren();
  const row=document.createElement("tr");
  const values=[tx("현재 결과","Current Results"),ready?tx("3D 준비 완료","3D Ready"):tx("결과 없음","No results"),
    typeof summary.tmax_c==="number"?`${summary.tmax_c.toFixed(4)} °C`:"—",
    typeof summary.outlet_c==="number"?`${summary.outlet_c.toFixed(4)} °C`:"—"];
  for(const value of values){const cell=document.createElement("td");cell.textContent=value;row.append(cell);}rows.append(row);
}

function renderCad(){
  const mode=currentMode(),dims=dimensions(),ports=diameters();
  $("parametric-area").hidden=mode!=="parametric";
  $("upload-area").hidden=mode!=="upload";
  $("tank-size").textContent=`${dims.map(value=>Number(value).toFixed(mode==="baseline"?0:1)).join(" × ")} mm`;
  $("cell-count").textContent=`${summary.cell_count||23} ${tx("개","cells")} · 5–4–5–4–5`;
  $("port-size").textContent=`Ø${ports[0]} / Ø${ports[1]} mm`;
  $("cad-source").textContent=modeCadName(mode);
  if(mode==="baseline"){
    $("cad-badge").textContent=tx("기준 CAD 검증 완료","Reference CAD validated");
    $("geometry-note").textContent="";
  }else if(mode==="parametric"){
    $("cad-badge").textContent=tx("변경 형상 사전검사 통과","Modified Geometry Precheck Passed");
    $("geometry-note").textContent=tx("실제 STEP/STL 생성 전 해석식 사전검사를 통과했습니다.","The analytical precheck passed before actual STEP/STL generation.");
  }else{
    $("cad-badge").textContent=tx("첨부 STEP 형상 검증 완료","Uploaded STEP geometry validated");
    $("geometry-note").textContent=tx("입·출구 원형 면, 23개 고체, 체적 관계를 확인했습니다. 메시 품질과 CFD 수렴은 계산 단계에서 별도 검증합니다.","Circular inlet/outlet faces, 23 solids, and volume relationships were verified. Mesh quality and CFD convergence are checked separately during calculation.");
  }
}

function renderFiles(){
  const output=$("cad-file-results");output.replaceChildren();
  const files=demo.files.length?demo.files:(currentMode()==="upload"?[
    {name:"enclosure_with_ports.step"},{name:"coolant.step"},{name:"battery_cells.step"}
  ]:[]);
  for(const [index,file] of files.entries()){
    const row=document.createElement("div");row.className="file-row";
    const title=document.createElement("span");title.textContent=`${file.name} · ${file.name.toLowerCase().endsWith(".stl")?"STL":"STEP"} · ${tx("형상 준비 완료","Geometry ready")}`;
    row.append(title);
    if(!file.name.toLowerCase().endsWith(".stl")){
      const select=document.createElement("select");select.setAttribute("aria-label",tx(`${file.name} CAD 역할`,`${file.name} CAD Role`));
      const roles=language()==="en"?["Tank and Ports","Coolant Fluid Region","23 Battery Solid Regions"]:["탱크·포트 외형","냉각수 유체 영역","23개 배터리 고체 영역"];
      for(const [roleIndex,label] of roles.entries()){
        const option=document.createElement("option");option.value=String(roleIndex);option.textContent=label;select.append(option);
      }
      select.value=String(index%3);row.append(select);
    }
    output.append(row);
  }
}

function confirmationField(label,value){
  const item=document.createElement("div"),heading=document.createElement("span"),data=document.createElement("strong");
  heading.textContent=label;data.textContent=String(value);item.append(heading,data);return item;
}
function confirmationFields(){
  const form=new FormData($("calculation-form")),mode=currentMode(),dims=dimensions(),ports=diameters();
  const operating=[
    [tx("냉각수","Coolant"),tx("탈이온수","Deionized Water")],
    [tx("입구 온도","Inlet Temperature"),`${form.get("inlet_temperature_k")} K`],
    [tx("입구 속도","Inlet Velocity"),`${form.get("inlet_velocity_m_s")} m/s`],
    [tx("출구 절대압력","Outlet Absolute Pressure"),`${form.get("outlet_pressure_pa")} Pa`],
    [tx("목표 메시","Target Mesh"),`${number(form.get("mesh_target_cells"))} ${tx("셀","cells")}`],
    [tx("열원","Heat Source"),tx("시간가변 2C · 0–1800 s","Time-varying 2C · 0–1800 s")],
  ];
  if(mode==="baseline")return [
    ["CAD",modeCadName(mode)],
    [tx("탱크 / 배터리","Tank / Battery"),`${dims.map(value=>Number(value).toFixed(1)).join("×")} mm · 23 ${tx("개","cells")}`],
    [tx("입구 / 출구 구경","Inlet / Outlet Diameters"),`Ø${ports[0]} / Ø${ports[1]} mm`],
    ...operating,
  ];
  if(mode==="parametric"){
    const volume=1.0720*(dims[0]/114)*(dims[1]/121)*(dims[2]/82);
    return [
      ["CAD",modeCadName(mode)],
      [tx("탱크 X×Y×Z","Tank X×Y×Z"),`${dims.map(value=>Number(value).toFixed(2)).join(" × ")} mm`],
      [tx("탱크 체적","Tank Volume"),`${volume.toFixed(4)} L`],
      [tx("입구 / 출구 구경","Inlet / Outlet Diameters"),`Ø${ports[0].toFixed(2)} / Ø${ports[1].toFixed(2)} mm`],
      [tx("배터리","Battery"),`23 ${tx("개","cells")} · Ø19×68 mm · ${tx("배열 고정","Fixed Arrangement")}`],
      [tx("셀–외곽 최소 간격","Minimum Cell-to-Outline Clearance"),"4.100 mm"],
      [tx("입구 / 출구 외곽 여유","Inlet / Outlet Outline Clearance"),"4.000 / 3.902 mm"],
      ...operating,
      [tx("이번 실행 범위","Scope of This Run"),tx("CAD → 메시 → S0 → S1 → S2 → 결과 뷰어","CAD → Mesh → S0 → S1 → S2 → Result Viewer")],
    ];
  }
  return [
    ["CAD",modeCadName(mode)],
    [tx("탱크 / 배터리","Tank / Battery"),`${dims.map(value=>Number(value).toFixed(1)).join("×")} mm · 23 ${tx("개","cells")}`],
    [tx("입구 / 출구 구경","Inlet / Outlet Diameters"),`Ø${ports[0].toFixed(2)} / Ø${ports[1].toFixed(2)} mm`],
    [tx("입구 / 출구 중심","Inlet / Outlet Centers"),"100.3, 107.5 / 13.5, 13.5 mm"],
    ...operating,
  ];
}
function renderConfirmation(){
  const mode=demo.lastMode;
  const wasConfirmed=$("geometry-confirmed").checked;
  $("confirmation-details").replaceChildren(...confirmationFields().map(([label,value])=>confirmationField(label,value)));
  $("confirmation-message").textContent=mode==="baseline"
    ?tx("동일한 기준 조건의 검증된 계산 결과를 연결합니다.","The validated calculation result with the same reference conditions will be opened.")
    :mode==="parametric"
      ?tx("해석식 간섭 검사를 통과했습니다. 확인 후 STEP/STL 생성·재검증을 거쳐 메시·S0·S1·S2 계산과 결과 뷰어 생성을 시작합니다.","Analytical interference checks passed. After confirmation, STEP/STL generation and revalidation will be followed by mesh, S0, S1, S2 calculation, and result-viewer generation.")
      :tx("첨부 CAD로 별도 메시·S0·S1·S2 계산을 실행합니다. 메시 품질, 수렴, 에너지 수지가 통과하면 3D 결과를 공개합니다.","A separate mesh and S0/S1/S2 calculation will run for the uploaded CAD. The 3D results become available after mesh quality, convergence, and energy-balance checks pass.");
  const baseline=mode==="baseline";
  $("attach-run").hidden=!baseline;$("attach-run").disabled=false;
  $("geometry-confirmation").hidden=baseline;$("geometry-confirmed").checked=baseline?false:wasConfirmed;
  $("generate-parametric-cad").hidden=mode!=="parametric";$("generate-parametric-cad").disabled=mode==="parametric"&&!wasConfirmed;
  $("start-imported-run").hidden=mode!=="upload";$("start-imported-run").disabled=mode==="upload"&&!wasConfirmed;
  $("generate-parametric-cad").textContent=tx("새 CFD 계산 시작","Start New CFD Calculation");
  $("start-imported-run").textContent=tx("새 CFD 계산 시작","Start New CFD Calculation");
}

function renderDemo(){
  const panel=$("imported-run-panel");panel.hidden=!demo.active;
  if(!demo.active)return;
  const complete=demo.status==="complete",cancelled=demo.status==="cancelled";
  $("imported-run-state").textContent=complete?tx("검증·후처리 완료","Validation and post-processing complete"):cancelled?tx("사용자 중지","Stopped by user"):tx("실제 계산 중","Calculation running");
  $("imported-run-message").textContent=complete
    ?tx("CFD 계산과 검증·후처리가 완료됐습니다. 3D 결과를 열 수 있습니다.","CFD calculation, validation, and post-processing are complete. The 3D results are ready.")
    :cancelled?tx("계산을 중지했습니다.","The calculation was stopped.")
      :tx("메시·해석 로그에서 실제 진행 상태를 확인하고 있습니다.","Reading the current progress from the mesh and solver logs.");
  const list=$("imported-run-stages");list.replaceChildren();
  for(const [index,label] of stageLabels().entries()){
    const passed=complete||index<demo.step,running=!cancelled&&!complete&&index===demo.step;
    const item=document.createElement("div");item.className=`stage ${passed?"passed":running?"running":"pending"}`;
    const strong=document.createElement("strong");strong.textContent=`${String(index+1).padStart(2,"0")} · ${label}`;
    item.append(strong,document.createTextNode(passed?tx("검증 통과","Validation passed"):running?tx("계산 중","Calculating"):tx("대기","Pending")));list.append(item);
  }
  const percent=complete?100:cancelled?Math.round(demo.step/stageLabels().length*100):Math.round((demo.step+.35)/stageLabels().length*100);
  $("imported-run-progress").textContent=`${tx("계산 진행률","Calculation progress")} ${percent}% · ${complete?tx("결과 뷰어 준비 완료","Result viewer ready"):stageLabels()[Math.min(demo.step,stageLabels().length-1)]}`;
  $("open-imported-viewer").hidden=!complete;$("open-imported-viewer").href="results.html";
  $("cancel-imported-run").hidden=complete||cancelled;$("cancel-imported-run").disabled=false;
  $("reset-imported-run").disabled=!complete&&!cancelled;
}

function startDemo(){
  clearInterval(demo.timer);demo.active=true;demo.status="running";demo.step=0;
  $("confirm-dialog").close();show("results");renderDemo();
  demo.timer=setInterval(()=>{
    demo.step+=1;
    if(demo.step>=stageLabels().length){clearInterval(demo.timer);demo.status="complete";}
    renderDemo();
  },420);
}
function cancelDemo(){clearInterval(demo.timer);demo.status="cancelled";renderDemo();}
function resetDemo(){
  clearInterval(demo.timer);demo.active=false;demo.status="idle";demo.step=0;renderDemo();show("calculate");
}
function renderAll(){renderSavedResult();renderCad();renderFiles();renderDemo();if($("confirm-dialog").open)renderConfirmation();}

$("go-start").onclick=()=>show("calculate");
$("go-graphics").onclick=()=>show("results");
for(const button of document.querySelectorAll(".home"))button.onclick=()=>show("landing");
for(const input of document.querySelectorAll('input[name="cad-mode"]'))input.onchange=()=>{renderCad();renderFiles();};
$("tank-size-mode").onchange=()=>{
  const volumeMode=$("tank-size-mode").value==="volume";
  $("tank-dimension-fields").hidden=volumeMode;$("tank-volume-field").hidden=!volumeMode;renderCad();
};
$("parametric-area").querySelectorAll("input,select").forEach(control=>control.addEventListener("input",renderCad));
$("cad-files").onchange=event=>{demo.files=Array.from(event.target.files);renderFiles();renderCad();};
$("calculation-form").onsubmit=event=>{
  event.preventDefault();demo.lastMode=currentMode();renderConfirmation();$("confirm-dialog").showModal();
};
$("attach-run").onclick=()=>show("results");
$("geometry-confirmed").onchange=()=>{
  const disabled=!$("geometry-confirmed").checked;
  $("generate-parametric-cad").disabled=disabled;$("start-imported-run").disabled=disabled;
};
$("generate-parametric-cad").onclick=startDemo;$("start-imported-run").onclick=startDemo;
$("cancel-imported-run").onclick=cancelDemo;$("reset-imported-run").onclick=resetDemo;
window.addEventListener("cfdtwin:languagechange",renderAll);
renderAll();
if(["#results","#calculate"].includes(location.hash))show(location.hash.slice(1));else show("landing");
'''

CSS = """*{box-sizing:border-box}html,body{height:100%;margin:0}body{display:flex;flex-direction:column;
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#eef3f6;color:#1e3543}
header{min-height:70px;display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:12px 16px;padding:10px 22px;
  background:#fff;border-bottom:1px solid #cad7df}.heading{display:flex;flex-wrap:wrap;align-items:center;gap:10px 20px;min-width:0}
.back{color:#176e88;text-decoration:none;font-size:13px;font-weight:650;white-space:nowrap}.back:hover{text-decoration:underline}
h1{font-size:20px;margin:0;white-space:nowrap}#count{font-size:11px;color:#68808d}
.mesh-case-control{display:flex;align-items:center;gap:7px;font-size:12px;font-weight:650;white-space:nowrap}
.mesh-case-control select{min-width:210px;max-width:260px;padding:7px;border:1px solid #b9cbd4;border-radius:5px;
  background:#fff;color:#1e3543;font:600 12px -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}
button{border:1px solid #b9cbd4;background:#fff;color:#294451;padding:7px 12px;border-radius:5px;
  font:600 12px -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;cursor:pointer}
button:hover,button:focus-visible{border-color:#167c9a;color:#095b77}
button[aria-pressed=true]{background:#116d8b;color:#fff;border-color:#116d8b}
.language-switch{display:inline-flex;gap:2px;padding:2px;border:1px solid #b9cbd4;border-radius:6px}
.language-switch button{border:0;padding:6px 8px}
.modes,.cameras{display:flex;align-items:center;gap:5px;flex-wrap:wrap}.modes{margin-left:auto}
.cut-controls{display:flex;align-items:center;gap:7px;font-size:11px;white-space:nowrap;color:#476271}
.cut-controls[hidden]{display:none}.cut-controls input{width:94px;accent-color:#116d8b}
main{display:flex;flex-direction:column;flex:1;min-height:0;padding:12px}
.viewbar,.foot{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:8px 12px;
  background:#fff;border:1px solid #c9d7de;font-size:12px}.viewbar{border-radius:8px 8px 0 0}
#plane{font-weight:650}.viewport{position:relative;flex:1;min-height:200px;background:#0c1721;overflow:hidden}
canvas{width:100%;height:100%;display:block;cursor:grab;touch-action:none}canvas:active{cursor:grabbing}
#loading{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:#d3e7ed;
  font-size:14px;pointer-events:none}#loading[hidden]{display:none}
.legend{position:absolute;left:14px;bottom:14px;padding:8px 12px;background:#08121bcf;border:1px solid #476072;
  color:#e4f1f6;border-radius:4px;font-size:12px;pointer-events:none}
.legend span{display:inline-block;width:16px;height:3px;vertical-align:middle;margin:0 5px 3px 12px}
.legend span:first-child{margin-left:0}.fluid{background:#ffcd4e}.solid{background:#64df94}
.foot{border-radius:0 0 8px 8px;color:#607583}
@media(max-width:850px){header{align-items:flex-start;flex-direction:column;padding:11px}
  .heading{flex-wrap:wrap;gap:9px}.viewbar,.foot{align-items:flex-start;flex-direction:column}
  .viewbar{padding:7px}button{padding:7px 9px}}
"""

JAVASCRIPT = r'''"use strict";
const canvas=document.getElementById("mesh"),loading=document.getElementById("loading");
const gl=canvas.getContext("webgl",{antialias:true,alpha:false,preserveDrawingBuffer:true});
const meshCases=(window.CFD_FOUR_CASE_MANIFEST?.runs||[]).filter(run=>run.data_ready);
const requestedMeshCase=new URLSearchParams(window.location.search).get("mesh");
const selectedMeshCase=meshCases.find(run=>run.id===requestedMeshCase)||meshCases[0]||null;
const selectedMeshId=selectedMeshCase?.id||null;
const selectedMeshAssetId=selectedMeshCase?.asset_source_id||selectedMeshId;
// Only the preserved Ø19 field uses the documented Y reflection. The new
// Ø21/23/25 polyMeshes already have the C05-top/C19-bottom orientation.
const reflectY=!selectedMeshAssetId||selectedMeshAssetId==="precision";
const meshCaseSelect=document.getElementById("mesh-case-select");
function refreshMeshCaseOptions(){
  if(!meshCaseSelect)return;
  const english=window.CFDTwinI18n?.getLanguage?.()==="en";
  meshCaseSelect.replaceChildren(...meshCases.map(run=>{
    const option=document.createElement("option");option.value=run.id;
    option.textContent=english?(run.label_en||run.label):(run.label||run.label_en);
    return option;
  }));
  meshCaseSelect.value=selectedMeshId;
}
if(meshCaseSelect&&selectedMeshId){
  refreshMeshCaseOptions();
  for(const anchor of document.querySelectorAll('a[href="results.html"]')){
    const url=new URL(anchor.getAttribute("href"),window.location.href);
    url.searchParams.set("mesh",selectedMeshId);anchor.href=url.href;
  }
  meshCaseSelect.addEventListener("change",()=>{
    const url=new URL(window.location.href);url.searchParams.set("mesh",meshCaseSelect.value);
    url.searchParams.set("lang",window.CFDTwinI18n?.getLanguage?.()||"ko");
    window.location.assign(url.href);
  });
  window.addEventListener("cfdtwin:languagechange",refreshMeshCaseOptions);
}
function loadMeshAsset(filename){return new Promise((resolve,reject)=>{
  const script=document.createElement("script");script.src=filename;script.async=false;
  script.onload=resolve;script.onerror=()=>reject(Error(`${filename}: 파일을 불러올 수 없습니다.`));
  document.head.appendChild(script);
});}
let mesh=null,mode="surface",yaw=0.55,pitch=0.70,zoom=1,baseScale=1,depthScale=1,dragging=false,lastX=0,lastY=0;
let geometry=[],centre=[0,0,0],bounds=[],program,attribute,uniforms;
function shader(type,source){const item=gl.createShader(type);gl.shaderSource(item,source);gl.compileShader(item);
  if(!gl.getShaderParameter(item,gl.COMPILE_STATUS))throw Error(gl.getShaderInfoLog(item));return item;}
function initGL(){program=gl.createProgram();gl.attachShader(program,shader(gl.VERTEX_SHADER,`
  attribute vec3 aPoint;uniform vec3 uCentre;uniform float uYaw,uPitch,uScale,uAspect,uDepthScale;
  varying float vDepth;
  void main(){vec3 p=aPoint-uCentre;float c=cos(uYaw),s=sin(uYaw);
  float x=c*p.x-s*p.y,y=s*p.x+c*p.y;
  float vertical=cos(uPitch)*y+sin(uPitch)*p.z;
  float depth=-sin(uPitch)*y+cos(uPitch)*p.z;
  vDepth=clamp(.5+.5*depth*uDepthScale,0.0,1.0);
  gl_Position=vec4(x*uScale/uAspect,vertical*uScale,-depth*uDepthScale,1.0);}`));
  gl.attachShader(program,shader(gl.FRAGMENT_SHADER,`
  precision mediump float;uniform vec4 uColour;uniform float uDepthCue;varying float vDepth;
  void main(){float shade=mix(1.0,mix(.30,1.0,smoothstep(0.0,1.0,vDepth)),uDepthCue);
  gl_FragColor=vec4(uColour.rgb*shade,uColour.a);}`));
  gl.linkProgram(program);if(!gl.getProgramParameter(program,gl.LINK_STATUS))throw Error(gl.getProgramInfoLog(program));
  gl.useProgram(program);attribute=gl.getAttribLocation(program,"aPoint");uniforms={
    centre:gl.getUniformLocation(program,"uCentre"),yaw:gl.getUniformLocation(program,"uYaw"),
    pitch:gl.getUniformLocation(program,"uPitch"),scale:gl.getUniformLocation(program,"uScale"),
    aspect:gl.getUniformLocation(program,"uAspect"),depthScale:gl.getUniformLocation(program,"uDepthScale"),
    depthCue:gl.getUniformLocation(program,"uDepthCue"),colour:gl.getUniformLocation(program,"uColour")};
  gl.enableVertexAttribArray(attribute);gl.enable(gl.DEPTH_TEST);gl.depthFunc(gl.LEQUAL);
  gl.enable(gl.BLEND);gl.blendFunc(gl.SRC_ALPHA,gl.ONE_MINUS_SRC_ALPHA);}
function displayMode(which,keepView=false){mode=which;for(const entry of geometry)gl.deleteBuffer(entry.buffer);geometry=[];
  const groups=mesh.modes[mode].groups,used=new Set();
  for(const group of groups)for(const edge of group.edges){used.add(edge[0]);used.add(edge[1]);}
  const low=[Infinity,Infinity,Infinity],high=[-Infinity,-Infinity,-Infinity];
  for(const index of used){const point=mesh.points_mm[index];for(let j=0;j<3;j++){
    low[j]=Math.min(low[j],point[j]);high[j]=Math.max(high[j],point[j]);}}
  centre=low.map((value,j)=>(value+high[j])/2);bounds=[];
  depthScale=.9/Math.max(Math.hypot(...high.map((value,j)=>value-low[j])),.001);
  for(const x of [low[0],high[0]])for(const y of [low[1],high[1]])for(const z of [low[2],high[2]])bounds.push([x,y,z]);
  for(const group of groups){const vertices=new Float32Array(group.edges.length*6);let i=0;
    for(const edge of group.edges)for(const index of edge)for(const coordinate of mesh.points_mm[index])vertices[i++]=coordinate;
    const buffer=gl.createBuffer();gl.bindBuffer(gl.ARRAY_BUFFER,buffer);gl.bufferData(gl.ARRAY_BUFFER,vertices,gl.STATIC_DRAW);
    geometry.push({buffer,count:vertices.length/3,kind:group.kind});}
  const interior=mode.startsWith("interior");
  document.getElementById("plane").textContent=mode==="surface"?"실제 polyMesh · 3D 경계 에지 전체":
    `${mesh.modes[mode].label} · 체적 격자에서 추출한 실제 내부 셀 에지`;
  const edgeCount=groups.reduce((total,group)=>total+group.edge_count,0);
  document.getElementById("detail").textContent=`현재 ${edgeCount.toLocaleString()}개 에지 · 표본화 없음 · ${interior?"3D 경계와 내부 체적 셀 절개면":"3D 경계 또는 실제 체적 셀 단면"}`;
  document.getElementById("cut-controls").hidden=!interior;
  for(const button of document.querySelectorAll("[data-mode]"))button.setAttribute("aria-pressed",button.dataset.mode===(interior?"interior":mode));
  if(keepView)render();else setView(mode==="xy"?"top":mode==="xz"?"side":"iso");}
async function displayCompleteVolume(){
  loading.hidden=false;loading.textContent="실제 유체·고체 전체 체적 격자를 불러오는 중…";
  try{
    if(selectedMeshAssetId&&!window.CFD_TWIN_VOLUME_GZIP)await loadMeshAsset(`mesh-volume-data-${selectedMeshAssetId}.js`);
    if(!window.CFD_TWIN_VOLUME_GZIP)throw Error("mesh-volume-data.js가 없습니다. ZIP 전체를 압축 해제한 뒤 mesh.html을 여세요.");
    if(!window.DecompressionStream)throw Error("이 브라우저는 gzip 압축 해제를 지원하지 않습니다.");
    for(const entry of geometry)gl.deleteBuffer(entry.buffer);geometry=[];
    const low=[Infinity,Infinity,Infinity],high=[-Infinity,-Infinity,-Infinity];let total=0;
    for(const [region,kind] of [["coolant","fluid"],["batterySolid","solid"]]){
      const encoded=window.CFD_TWIN_VOLUME_GZIP[region];
      const zipped=Uint8Array.from(atob(encoded),char=>char.charCodeAt(0));
      const raw=await new Response(new Blob([zipped]).stream().pipeThrough(new DecompressionStream("gzip"))).arrayBuffer();
      const header=new DataView(raw),nPoints=header.getUint32(0,true),nEdges=header.getUint32(4,true);
      const required=12+nPoints*12+nEdges*8;
      if(raw.byteLength!==required)throw Error(`${region}: 전체 격자 길이가 맞지 않습니다.`);
      const points=new Float32Array(raw,12,nPoints*3),edges=new Uint32Array(raw,12+nPoints*12,nEdges*2);
      const vertices=new Float32Array(nEdges*6);
      for(let i=0;i<nPoints;i++)for(let j=0;j<3;j++){
        const rawValue=points[i*3+j],value=j===1&&reflectY?121-rawValue:rawValue;
        low[j]=Math.min(low[j],value);high[j]=Math.max(high[j],value);}
      for(let i=0;i<nEdges*2;i++){
        const index=edges[i];if(index>=nPoints)throw Error(`${region}: 잘못된 격자 연결 인덱스`);
        vertices[i*3]=points[index*3];vertices[i*3+1]=reflectY?121-points[index*3+1]:points[index*3+1];vertices[i*3+2]=points[index*3+2];
      }
      const buffer=gl.createBuffer();gl.bindBuffer(gl.ARRAY_BUFFER,buffer);gl.bufferData(gl.ARRAY_BUFFER,vertices,gl.STATIC_DRAW);
      geometry.push({buffer,count:nEdges*2,kind});total+=nEdges;
    }
    // The volume buffers contain every edge. Add the boundary edges as a
    // separate final pass so the enclosure and battery silhouettes remain
    // legible without discarding or sampling any volume cells.
    for(const group of mesh.modes.surface.groups){const vertices=new Float32Array(group.edges.length*6);let i=0;
      for(const edge of group.edges)for(const index of edge)for(const coordinate of mesh.points_mm[index])vertices[i++]=coordinate;
      const buffer=gl.createBuffer();gl.bindBuffer(gl.ARRAY_BUFFER,buffer);gl.bufferData(gl.ARRAY_BUFFER,vertices,gl.STATIC_DRAW);
      geometry.push({buffer,count:vertices.length/3,kind:`${group.kind}_outline`,outline:true});}
    mode="volume";centre=low.map((value,j)=>(value+high[j])/2);bounds=[];
    depthScale=.9/Math.max(Math.hypot(...high.map((value,j)=>value-low[j])),.001);
    for(const x of [low[0],high[0]])for(const y of [low[1],high[1]])for(const z of [low[2],high[2]])bounds.push([x,y,z]);
    document.getElementById("plane").textContent="실제 polyMesh · 전체 3D 체적 격자";
    document.getElementById("detail").textContent=`유체·고체 전체 ${total.toLocaleString()}개 고유 에지 · 표본화 없음 · 깊이 판정 및 경계 강조`;
    document.getElementById("cut-controls").hidden=true;
    for(const button of document.querySelectorAll("[data-mode]"))button.setAttribute("aria-pressed",button.dataset.mode==="volume");
    setView("iso");loading.hidden=true;
  }catch(error){loading.textContent=`전체 격자 표시 실패: ${error.message}`;console.error(error);}
}
function setView(view){const angles={iso:[0.55,0.70],top:[0,0],side:[0,Math.PI/2],front:[Math.PI/2,Math.PI/2]};
  [yaw,pitch]=angles[view]||angles.iso;fitView();}
function canvasAspect(){return Math.max(1,canvas.clientWidth)/Math.max(1,canvas.clientHeight);}
function fitView(){zoom=1;baseScale=fitScale(canvasAspect());render();}
function fitScale(aspect){let minX=Infinity,maxX=-Infinity,minY=Infinity,maxY=-Infinity;
  const c=Math.cos(yaw),s=Math.sin(yaw),cp=Math.cos(pitch),sp=Math.sin(pitch);
  for(const point of bounds){const x=point[0]-centre[0],y=point[1]-centre[1],z=point[2]-centre[2];
    const h=c*x-s*y,v=cp*(s*x+c*y)+sp*z;
    minX=Math.min(minX,h);maxX=Math.max(maxX,h);minY=Math.min(minY,v);maxY=Math.max(maxY,v);}
  return 1.77/Math.max((maxX-minX)/aspect,maxY-minY,0.001);}
function render(){if(!mesh||!gl)return;const ratio=Math.min(2,window.devicePixelRatio||1);
  const width=Math.max(1,Math.round(canvas.clientWidth*ratio)),height=Math.max(1,Math.round(canvas.clientHeight*ratio));
  if(canvas.width!==width||canvas.height!==height){canvas.width=width;canvas.height=height;}
  gl.viewport(0,0,width,height);gl.clearColor(0.047,0.090,0.129,1);gl.clearDepth(1);gl.clear(gl.COLOR_BUFFER_BIT|gl.DEPTH_BUFFER_BIT);
  const aspect=width/height;gl.useProgram(program);gl.uniform3fv(uniforms.centre,centre);
  gl.uniform1f(uniforms.yaw,yaw);gl.uniform1f(uniforms.pitch,pitch);
  // 드래그할 때마다 현재 투영 외접상자를 다시 맞추면 물체가 갑자기 확대된다.
  // 시점 선택·'맞춤'·창 크기 변경 때만 기준 배율을 갱신한다.
  gl.uniform1f(uniforms.scale,baseScale*zoom);gl.uniform1f(uniforms.aspect,aspect);
  gl.uniform1f(uniforms.depthScale,depthScale);gl.uniform1f(uniforms.depthCue,mode==="volume"?1:.35);
  const palette={fluid:[1,.74,.20,mode==="volume"?1:mode==="surface"?.42:.84],solid:[.25,.92,.50,mode==="volume"?1:.90],port:[1,.56,.28,.95],
    fluid_shell:[1,.79,.27,.22],solid_shell:[.35,.92,.57,.22],port_shell:[1,.56,.28,.35],
    fluid_outline:[1,.82,.31,1],solid_outline:[.35,1,.59,1],port_outline:[1,.60,.30,1]};
  if(mode==="volume"){gl.disable(gl.BLEND);gl.depthMask(true);}else{gl.enable(gl.BLEND);gl.depthMask(false);}
  for(const item of geometry){if(mode==="volume"&&item.outline)continue;gl.bindBuffer(gl.ARRAY_BUFFER,item.buffer);
    gl.vertexAttribPointer(attribute,3,gl.FLOAT,false,0,0);gl.uniform4fv(uniforms.colour,palette[item.kind]||palette.fluid);
    gl.drawArrays(gl.LINES,0,item.count);}
  if(mode==="volume"){gl.depthMask(false);for(const item of geometry){if(!item.outline)continue;gl.bindBuffer(gl.ARRAY_BUFFER,item.buffer);
      gl.vertexAttribPointer(attribute,3,gl.FLOAT,false,0,0);gl.uniform4fv(uniforms.colour,palette[item.kind]||palette.fluid_outline);
      gl.drawArrays(gl.LINES,0,item.count);}gl.depthMask(true);gl.enable(gl.BLEND);}}
canvas.addEventListener("pointerdown",event=>{dragging=true;lastX=event.clientX;lastY=event.clientY;canvas.setPointerCapture(event.pointerId);});
canvas.addEventListener("pointerup",()=>{dragging=false;});canvas.addEventListener("pointercancel",()=>{dragging=false;});
canvas.addEventListener("pointermove",event=>{if(!dragging)return;
  yaw+=(event.clientX-lastX)*.008;pitch=Math.max(-Math.PI,Math.min(Math.PI,pitch+(event.clientY-lastY)*.008));
  lastX=event.clientX;lastY=event.clientY;render();});
canvas.addEventListener("wheel",event=>{event.preventDefault();zoom=Math.max(.35,Math.min(9,zoom*Math.exp(-event.deltaY*.001)));render();},{passive:false});
window.addEventListener("resize",()=>{baseScale=fitScale(canvasAspect());render();});
document.querySelectorAll("[data-mode]").forEach(button=>button.onclick=()=>button.dataset.mode==="volume"
  ?displayCompleteVolume():displayMode(button.dataset.mode==="interior"
    ?`interior${document.getElementById("cut-x").value}`:button.dataset.mode));
document.getElementById("cut-x").oninput=event=>{document.getElementById("cut-readout").textContent=`${event.target.value}%`;
  displayMode(`interior${event.target.value}`,true);};
document.querySelectorAll("[data-view]").forEach(button=>button.onclick=()=>setView(button.dataset.view));
document.getElementById("fit").onclick=fitView;
const savePng=document.getElementById("save-png");
savePng.onclick=()=>{if(!mesh)return;
  render();const selectedMode=mode;
  canvas.toBlob(blob=>{if(!blob){window.alert("PNG 이미지를 만들지 못했습니다.");return;}
    const url=URL.createObjectURL(blob),link=document.createElement("a");
    link.href=url;link.download=`YU_fluid_mesh_${selectedMeshId||"reference"}_${selectedMode}.png`;
    document.body.appendChild(link);link.click();link.remove();
    window.setTimeout(()=>URL.revokeObjectURL(url),1000);
  },"image/png");};
async function start(){try{if(!gl)throw Error("이 브라우저에서 WebGL을 사용할 수 없습니다.");
  if(selectedMeshAssetId&&!window.CFD_TWIN_MESH_GZIP)await loadMeshAsset(`mesh-data-${selectedMeshAssetId}.js`);
  if(!window.CFD_TWIN_MESH_GZIP||!window.DecompressionStream)throw Error("메시 데이터 또는 gzip 지원이 없습니다.");
  const bytes=Uint8Array.from(atob(window.CFD_TWIN_MESH_GZIP),character=>character.charCodeAt(0));
  mesh=await new Response(new Blob([bytes]).stream().pipeThrough(new DecompressionStream("gzip"))).json();
  if(reflectY)for(const point of mesh.points_mm)point[1]=121-point[1];
  if(mesh.sampling!=="none")throw Error("전체 에지 자료가 아닙니다.");
  document.getElementById("count").textContent=`${Number(mesh.cell_counts.total_cells).toLocaleString()}개 계산 셀 · 유체 ${Number(mesh.cell_counts.fluid_cells).toLocaleString()} · 고체 ${Number(mesh.cell_counts.solid_cells).toLocaleString()}`;
  initGL();displayMode("surface");loading.hidden=true;savePng.disabled=false;
}catch(error){loading.textContent=`메시 표시 실패: ${error.message}`;console.error(error);}}start();
'''


def write_mesh_only_viewer(
    case_dir: str | Path,
    output_dir: str | Path,
    *,
    steady_run_dir: str | Path | None = None,
) -> dict[str, object]:
    """Publish actual boundary, sections and every unique 3D volume edge."""

    case = Path(case_dir).resolve()
    output = Path(output_dir).resolve()
    mesh = extract_full_mesh_views(case, steady_run_dir=steady_run_dir)
    report = json.loads(
        (case / "reports/production_mesh/mesh_target_composition.json").read_text(
            encoding="utf-8"
        )
    )
    mesh["cell_counts"] = {
        key: report["actual"][key]
        for key in ("total_cells", "fluid_cells", "solid_cells")
    }
    raw = json.dumps(mesh, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    payload = base64.b64encode(gzip.compress(raw, compresslevel=9)).decode("ascii")
    output.mkdir(parents=True, exist_ok=True)
    # Script assets work both via the local workflow server and directly on file://.
    (output / "mesh.html").write_text(HTML, encoding="utf-8")
    (output / "home.html").write_text(HOME_HTML, encoding="utf-8")
    (output / "portal.css").write_text(
        (Path(__file__).with_name("workflow_assets") / "style.css").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (output / "offline-app.js").write_text(OFFLINE_JAVASCRIPT, encoding="utf-8")
    (output / "i18n.js").write_text(
        (Path(__file__).with_name("workflow_assets") / "i18n.js").read_text(encoding="utf-8"), encoding="utf-8"
    )
    shutil.copy2(
        Path(__file__).with_name("workflow_assets") / "cheoma-logo.png",
        output / "cheoma-logo.png",
    )
    (output / "mesh.css").write_text(CSS, encoding="utf-8")
    (output / "mesh.js").write_text(JAVASCRIPT, encoding="utf-8")
    (output / "mesh-data.js").write_text(
        "window.CFD_TWIN_MESH_GZIP=" + json.dumps(payload) + ";\n",
        encoding="utf-8",
    )
    volume = extract_complete_volume_edges(case)
    (output / "mesh-volume-data.js").write_text(
        "window.CFD_TWIN_VOLUME_GZIP=" + json.dumps(volume["payload"], separators=(",", ":")) + ";\n",
        encoding="utf-8",
    )
    return {
        "point_count": mesh["point_count"],
        "cell_counts": mesh["cell_counts"],
        "edge_counts": {
            name: sum(group["edge_count"] for group in item["groups"])
            for name, item in mesh["modes"].items()
        },
        "complete_volume_edge_counts": volume["counts"],
    }
