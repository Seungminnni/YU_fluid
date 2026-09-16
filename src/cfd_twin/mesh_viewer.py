"""Separate, portable mesh-only viewer sourced from the computed OpenFOAM mesh."""

from __future__ import annotations

import base64
import gzip
import json
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
    <div class="heading"><a id="mesh-home" href="../" class="back">← 처음으로</a>
      <a href="index.html" class="back">결과 화면</a><h1>메시 보기</h1><span id="count"></span></div>
    <div class="modes" role="group" aria-label="메시 표시 방식">
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
      <div class="legend"><span class="fluid"></span> 냉각수 영역 <span class="solid"></span> 배터리 경계</div>
    </div>
    <div class="foot"><span id="detail">3D 경계는 외부 표면, 3D 내부 절개와 단면은 실제 체적 셀의 교선을 표시합니다.</span>
      <span>드래그: 회전 · 휠: 확대</span></div>
  </main>
  <script src="mesh-data.js"></script>
  <script src="mesh.js"></script>
</body>
</html>
"""

CSS = """*{box-sizing:border-box}html,body{height:100%;margin:0}body{display:flex;flex-direction:column;
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#eef3f6;color:#1e3543}
header{min-height:70px;display:flex;align-items:center;justify-content:space-between;gap:16px;padding:10px 22px;
  background:#fff;border-bottom:1px solid #cad7df}.heading{display:flex;align-items:center;gap:20px;min-width:0}
.back{color:#176e88;text-decoration:none;font-size:13px;font-weight:650;white-space:nowrap}.back:hover{text-decoration:underline}
h1{font-size:20px;margin:0;white-space:nowrap}#count{font-size:11px;color:#68808d;white-space:nowrap}
button{border:1px solid #b9cbd4;background:#fff;color:#294451;padding:7px 12px;border-radius:5px;
  font:600 12px -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;cursor:pointer}
button:hover,button:focus-visible{border-color:#167c9a;color:#095b77}
button[aria-pressed=true]{background:#116d8b;color:#fff;border-color:#116d8b}
.modes,.cameras{display:flex;align-items:center;gap:5px;flex-wrap:wrap}
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
document.getElementById("mesh-home").hidden=window.location.protocol==="file:";
const gl=canvas.getContext("webgl",{antialias:true,alpha:false,preserveDrawingBuffer:true});
let mesh=null,mode="surface",yaw=0.55,pitch=0.70,zoom=1,baseScale=1,dragging=false,lastX=0,lastY=0;
let geometry=[],centre=[0,0,0],bounds=[],program,attribute,uniforms;
function shader(type,source){const item=gl.createShader(type);gl.shaderSource(item,source);gl.compileShader(item);
  if(!gl.getShaderParameter(item,gl.COMPILE_STATUS))throw Error(gl.getShaderInfoLog(item));return item;}
function initGL(){program=gl.createProgram();gl.attachShader(program,shader(gl.VERTEX_SHADER,`
  attribute vec3 aPoint;uniform vec3 uCentre;uniform float uYaw,uPitch,uScale,uAspect;
  void main(){vec3 p=aPoint-uCentre;float c=cos(uYaw),s=sin(uYaw);
  float x=c*p.x-s*p.y,y=s*p.x+c*p.y,z=cos(uPitch)*y+sin(uPitch)*p.z;
  gl_Position=vec4(x*uScale/uAspect,z*uScale,0.0,1.0);}`));
  gl.attachShader(program,shader(gl.FRAGMENT_SHADER,`
  precision mediump float;uniform vec4 uColour;void main(){gl_FragColor=uColour;}`));
  gl.linkProgram(program);if(!gl.getProgramParameter(program,gl.LINK_STATUS))throw Error(gl.getProgramInfoLog(program));
  gl.useProgram(program);attribute=gl.getAttribLocation(program,"aPoint");uniforms={
    centre:gl.getUniformLocation(program,"uCentre"),yaw:gl.getUniformLocation(program,"uYaw"),
    pitch:gl.getUniformLocation(program,"uPitch"),scale:gl.getUniformLocation(program,"uScale"),
    aspect:gl.getUniformLocation(program,"uAspect"),colour:gl.getUniformLocation(program,"uColour")};
  gl.enableVertexAttribArray(attribute);gl.enable(gl.BLEND);gl.blendFunc(gl.SRC_ALPHA,gl.ONE_MINUS_SRC_ALPHA);}
function displayMode(which,keepView=false){mode=which;for(const entry of geometry)gl.deleteBuffer(entry.buffer);geometry=[];
  const groups=mesh.modes[mode].groups,used=new Set();
  for(const group of groups)for(const edge of group.edges){used.add(edge[0]);used.add(edge[1]);}
  const low=[Infinity,Infinity,Infinity],high=[-Infinity,-Infinity,-Infinity];
  for(const index of used){const point=mesh.points_mm[index];for(let j=0;j<3;j++){
    low[j]=Math.min(low[j],point[j]);high[j]=Math.max(high[j],point[j]);}}
  centre=low.map((value,j)=>(value+high[j])/2);bounds=[];
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
    if(!window.CFD_TWIN_VOLUME_GZIP){await new Promise((resolve,reject)=>{
      const script=document.createElement("script");script.src="mesh-volume-data.js";
      script.onload=resolve;script.onerror=()=>reject(Error("전체 격자 파일을 읽지 못했습니다."));
      document.body.appendChild(script);});}
    if(!window.CFD_TWIN_VOLUME_GZIP||!window.DecompressionStream)throw Error("전체 격자 데이터 또는 gzip 지원이 없습니다.");
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
        const value=points[i*3+j];low[j]=Math.min(low[j],value);high[j]=Math.max(high[j],value);}
      for(let i=0;i<nEdges*2;i++){
        const index=edges[i];if(index>=nPoints)throw Error(`${region}: 잘못된 격자 연결 인덱스`);
        vertices.set(points.subarray(index*3,index*3+3),i*3);
      }
      const buffer=gl.createBuffer();gl.bindBuffer(gl.ARRAY_BUFFER,buffer);gl.bufferData(gl.ARRAY_BUFFER,vertices,gl.STATIC_DRAW);
      geometry.push({buffer,count:nEdges*2,kind});total+=nEdges;
    }
    mode="volume";centre=low.map((value,j)=>(value+high[j])/2);bounds=[];
    for(const x of [low[0],high[0]])for(const y of [low[1],high[1]])for(const z of [low[2],high[2]])bounds.push([x,y,z]);
    document.getElementById("plane").textContent="실제 polyMesh · 전체 3D 체적 격자";
    document.getElementById("detail").textContent=`유체·고체 전체 ${total.toLocaleString()}개 고유 에지 · 표본화 없음`;
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
  gl.viewport(0,0,width,height);gl.clearColor(0.047,0.090,0.129,1);gl.clear(gl.COLOR_BUFFER_BIT);
  const aspect=width/height;gl.useProgram(program);gl.uniform3fv(uniforms.centre,centre);
  gl.uniform1f(uniforms.yaw,yaw);gl.uniform1f(uniforms.pitch,pitch);
  // 드래그할 때마다 현재 투영 외접상자를 다시 맞추면 물체가 갑자기 확대된다.
  // 시점 선택·'맞춤'·창 크기 변경 때만 기준 배율을 갱신한다.
  gl.uniform1f(uniforms.scale,baseScale*zoom);gl.uniform1f(uniforms.aspect,aspect);
  const palette={fluid:[1,.79,.27,mode==="volume"?.12:mode==="surface"?.42:.84],solid:[.35,.92,.57,mode==="volume"?.24:.90],port:[1,.56,.28,.95],
    fluid_shell:[1,.79,.27,.22],solid_shell:[.35,.92,.57,.22],port_shell:[1,.56,.28,.35]};
  for(const item of geometry){gl.bindBuffer(gl.ARRAY_BUFFER,item.buffer);
    gl.vertexAttribPointer(attribute,3,gl.FLOAT,false,0,0);gl.uniform4fv(uniforms.colour,palette[item.kind]||palette.fluid);
    gl.drawArrays(gl.LINES,0,item.count);}}
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
    link.href=url;link.download=`YU_fluid_mesh_${selectedMode}.png`;
    document.body.appendChild(link);link.click();link.remove();
    window.setTimeout(()=>URL.revokeObjectURL(url),1000);
  },"image/png");};
async function start(){try{if(!gl)throw Error("이 브라우저에서 WebGL을 사용할 수 없습니다.");
  if(!window.CFD_TWIN_MESH_GZIP||!window.DecompressionStream)throw Error("메시 데이터 또는 gzip 지원이 없습니다.");
  const bytes=Uint8Array.from(atob(window.CFD_TWIN_MESH_GZIP),character=>character.charCodeAt(0));
  mesh=await new Response(new Blob([bytes]).stream().pipeThrough(new DecompressionStream("gzip"))).json();
  if(mesh.sampling!=="none")throw Error("전체 에지 자료가 아닙니다.");
  document.getElementById("count").textContent=`${Number(mesh.cell_counts.total_cells).toLocaleString()}개 계산 셀 · 유체 ${Number(mesh.cell_counts.fluid_cells).toLocaleString()} · 고체 ${Number(mesh.cell_counts.solid_cells).toLocaleString()}`;
  initGL();displayMode("surface");loading.hidden=true;savePng.disabled=false;
}catch(error){loading.textContent=`메시 표시 실패: ${error.message}`;console.error(error);}}start();
'''


def write_mesh_only_viewer(case_dir: str | Path, output_dir: str | Path) -> dict[str, object]:
    """Publish actual, unsampled boundary and exact centre-plane cell sections."""

    case = Path(case_dir).resolve()
    output = Path(output_dir).resolve()
    mesh = extract_full_mesh_views(case)
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
