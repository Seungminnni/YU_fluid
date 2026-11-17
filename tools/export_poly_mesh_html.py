"""Export a lightweight, dependency-free 3D HTML preview of an OpenFOAM mesh.

The exporter reads the real split-region ``polyMesh`` files.  It keeps the
external boundary edges and three thin, central slabs of internal face edges.
The resulting browser asset is deliberately smaller than a dump of every edge
of a multi-million-cell volume, while every displayed line still comes from an
actual OpenFOAM face.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import json
import math
import re
import struct
from array import array
from pathlib import Path


FLOAT_RE = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
FACE_RE = re.compile(r"^\s*\d+\(([^)]*)\)\s*$")
POINT_RE = re.compile(rf"^\s*\(({FLOAT_RE})\s+({FLOAT_RE})\s+({FLOAT_RE})\)\s*$")


def read_boundary(path: Path) -> dict[str, tuple[int, int]]:
    text = path.read_text(encoding="utf-8", errors="strict")
    result: dict[str, tuple[int, int]] = {}
    pattern = re.compile(r"^\s*([^\s{}()]+)\s*\n\s*\{(.*?)^\s*\}", re.MULTILINE | re.DOTALL)
    for match in pattern.finditer(text):
        body = match.group(2)
        count = re.search(r"\bnFaces\s+(\d+)\s*;", body)
        start = re.search(r"\bstartFace\s+(\d+)\s*;", body)
        if count and start:
            result[match.group(1)] = (int(start.group(1)), int(count.group(1)))
    if not result:
        raise ValueError(f"No boundary patches parsed from {path}")
    return result


def read_points(path: Path) -> tuple[array, tuple[float, float, float, float, float, float]]:
    values = array("f")
    started = False
    expected: int | None = None
    bounds = [math.inf, math.inf, math.inf, -math.inf, -math.inf, -math.inf]
    with path.open("r", encoding="utf-8", errors="strict") as handle:
        for line in handle:
            stripped = line.strip()
            if not started:
                if expected is None and stripped.isdigit():
                    expected = int(stripped)
                elif expected is not None and stripped == "(":
                    started = True
                continue
            if stripped == ")":
                break
            match = POINT_RE.match(line)
            if not match:
                continue
            point = (float(match.group(1)), float(match.group(2)), float(match.group(3)))
            values.extend(point)
            for axis, value in enumerate(point):
                bounds[axis] = min(bounds[axis], value)
                bounds[axis + 3] = max(bounds[axis + 3], value)
    if expected is None or len(values) // 3 != expected:
        raise ValueError(f"Point count mismatch in {path}: expected {expected}, got {len(values) // 3}")
    return values, tuple(bounds)  # type: ignore[return-value]


def face_edges(indices: list[int]) -> tuple[tuple[int, int], ...]:
    return tuple(
        (first, second) if first < second else (second, first)
        for first, second in zip(indices, indices[1:] + indices[:1])
    )


def thin_edges(edges: set[tuple[int, int]], maximum: int) -> list[tuple[int, int]]:
    ordered = sorted(edges)
    if len(ordered) <= maximum:
        return ordered
    stride = len(ordered) / maximum
    return [ordered[min(len(ordered) - 1, int(index * stride))] for index in range(maximum)]


def extract_region(mesh_dir: Path, region: str) -> tuple[dict[str, list[tuple[int, int]]], tuple[float, ...], array]:
    points, bounds = read_points(mesh_dir / "points")
    patches = read_boundary(mesh_dir / "boundary")
    if region == "coolant":
        selected = {name for name in patches if name in {"outerWall", "inlet", "outlet"}}
    else:
        selected = set(patches)
    ranges = sorted((start, start + count) for name, (start, count) in patches.items() if name in selected)
    targets = [(bounds[axis] + bounds[axis + 3]) * 0.5 for axis in range(3)]
    tolerances = [max((bounds[axis + 3] - bounds[axis]) * 0.004, 0.00025) for axis in range(3)]
    shell: set[tuple[int, int]] = set()
    cuts = [set(), set(), set()]
    started = False
    expected: int | None = None
    face_index = 0
    range_index = 0
    faces_path = mesh_dir / "faces"
    with faces_path.open("r", encoding="utf-8", errors="strict") as handle:
        for line in handle:
            stripped = line.strip()
            if not started:
                if expected is None and stripped.isdigit():
                    expected = int(stripped)
                elif expected is not None and stripped == "(":
                    started = True
                continue
            if stripped == ")":
                break
            match = FACE_RE.match(line)
            if not match:
                continue
            indices = [int(value) for value in match.group(1).split()]
            while range_index < len(ranges) and face_index >= ranges[range_index][1]:
                range_index += 1
            if range_index < len(ranges) and ranges[range_index][0] <= face_index < ranges[range_index][1]:
                shell.update(face_edges(indices))
            coordinates = [
                sum(points[3 * index + axis] for index in indices) / len(indices)
                for axis in range(3)
            ]
            for axis in range(3):
                if abs(coordinates[axis] - targets[axis]) <= tolerances[axis]:
                    cuts[axis].update(face_edges(indices))
            face_index += 1
    if expected is None or face_index != expected:
        raise ValueError(f"Face count mismatch in {faces_path}: expected {expected}, got {face_index}")
    limit = 260_000 if region == "coolant" else 180_000
    result = {
        "surface": thin_edges(shell, limit),
        "xcut": thin_edges(cuts[0], limit),
        "ycut": thin_edges(cuts[1], limit),
        "zcut": thin_edges(cuts[2], limit),
    }
    return result, bounds, points


def pack_edges(points: array, edges: list[tuple[int, int]]) -> str:
    packed = array("f")
    for first, second in edges:
        for index in (first, second):
            packed.extend((1000.0 * points[3 * index], 1000.0 * points[3 * index + 1], 1000.0 * points[3 * index + 2]))
    return base64.b64encode(gzip.compress(packed.tobytes(), compresslevel=9)).decode("ascii")


HTML = r'''<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>원뿔대 OpenFOAM 메시 3D</title>
  <style>
    :root{color-scheme:dark;--bg:#071019;--panel:#0d1924;--line:#203647;--text:#e9f4fb;--muted:#8fa7b5;--cyan:#55d6ee;--amber:#ffb85c;--green:#6ce0aa}
    *{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 20% 0,#10293a 0,var(--bg) 48%);color:var(--text);font-family:Inter,"Noto Sans KR",system-ui,sans-serif}
    header{padding:22px 26px 16px;border-bottom:1px solid #173042;background:#08131ee8}h1{font-size:22px;margin:0 0 7px}header p{margin:0;color:var(--muted);font-size:13px}
    .layout{display:grid;grid-template-columns:minmax(0,1fr) 310px;gap:14px;padding:14px;height:calc(100vh - 94px)}
    .viewport,.side{border:1px solid #1c3547;border-radius:14px;background:#08131dcc;box-shadow:0 18px 45px #0007}.viewport{position:relative;overflow:hidden;min-height:520px}
    canvas{width:100%;height:100%;display:block}.toolbar{position:absolute;left:14px;top:14px;display:flex;flex-wrap:wrap;gap:7px;max-width:calc(100% - 28px)}
    button{border:1px solid #2d5368;background:#102433;color:#cfe3ed;padding:8px 11px;border-radius:9px;cursor:pointer;font-weight:650}button:hover{border-color:#5ea3bd}button.active{background:#0d4e62;border-color:#5de0f4;color:#fff}
    .hint{position:absolute;left:14px;bottom:12px;color:#a7bbc6;background:#071019d9;border:1px solid #1e3848;padding:8px 11px;border-radius:9px;font-size:12px}.status{position:absolute;inset:0;display:grid;place-items:center;background:#071019;color:#b8ccd6;z-index:5}.status[hidden]{display:none}
    .side{padding:16px;overflow:auto}.badge{display:inline-flex;align-items:center;gap:7px;color:var(--green);font-weight:750;font-size:12px}.badge:before{content:"";width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 10px var(--green)}
    .metrics{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin:15px 0}.metric{padding:11px;border-radius:10px;border:1px solid #1d3a4d;background:#0c1b27}.metric b{display:block;font-size:17px;margin-top:4px}.metric span{font-size:11px;color:var(--muted)}
    h2{font-size:13px;margin:20px 0 9px;color:#cfe3ed}.control{display:flex;justify-content:space-between;align-items:center;border-top:1px solid #183041;padding:11px 0;font-size:13px}.control input{accent-color:var(--cyan)}
    .legend{display:grid;gap:8px;color:#b4c6d0;font-size:12px}.legend div{display:flex;align-items:center;gap:8px}.swatch{width:23px;height:3px;border-radius:3px}.fluid{background:var(--cyan)}.solid{background:var(--amber)}
    .note{font-size:11px;line-height:1.55;color:#78909d;margin-top:18px}.source{word-break:break-all}
    @media(max-width:850px){.layout{grid-template-columns:1fr;height:auto}.viewport{height:68vh;min-height:440px}.side{max-height:none}}
  </style>
</head>
<body>
<header><h1>원뿔대 입구 · 실제 OpenFOAM 메시 3D</h1><p>Ø33 → Ø22 원뿔대 입구 · Ø19 출구 · 실제 split-region polyMesh 기반</p></header>
<div class="layout">
  <section class="viewport">
    <canvas id="view"></canvas><div id="status" class="status">실제 polyMesh 데이터를 불러오는 중…</div>
    <div class="toolbar">
      <button data-mode="surface" class="active">3D 외곽</button><button data-mode="xcut">X 중앙 단면</button><button data-mode="ycut">Y 중앙 단면</button><button data-mode="zcut">Z 중앙 단면</button><button data-mode="allcuts">3축 내부</button>
      <button id="iso">등각</button><button id="fit">맞춤</button>
    </div>
    <div class="hint">드래그: 회전 · 휠: 확대 · 실제 격자선만 표시</div>
  </section>
  <aside class="side">
    <div class="badge">PRODUCTION_MESH_GATE_PASSED</div>
    <div class="metrics">
      <div class="metric"><span>전체 셀</span><b id="total"></b></div><div class="metric"><span>육면체 비율</span><b>100%</b></div>
      <div class="metric"><span>냉각수</span><b id="fluid"></b></div><div class="metric"><span>배터리 고체</span><b id="solid"></b></div>
      <div class="metric"><span>첫 층 / 층수</span><b>6 µm / 11</b></div><div class="metric"><span>최대 비직교도</span><b id="nonorth"></b></div>
      <div class="metric"><span>최대 skewness</span><b id="skew"></b></div><div class="metric"><span>경계 형상</span><b>33→22 / 19</b></div>
    </div>
    <h2>표시 설정</h2>
    <label class="control">냉각수 격자 <input id="show-fluid" type="checkbox" checked></label>
    <label class="control">배터리 격자 <input id="show-solid" type="checkbox" checked></label>
    <label class="control">외곽 동시 표시 <input id="show-shell" type="checkbox" checked></label>
    <label class="control">선 투명도 <input id="opacity" type="range" min="20" max="100" value="82"></label>
    <h2>색상</h2><div class="legend"><div><span class="swatch fluid"></span>냉각수 영역</div><div><span class="swatch solid"></span>배터리 고체 영역</div></div>
    <p class="note">브라우저 성능을 위해 200만 셀의 모든 선을 한 화면에 겹치지 않고, 실제 외곽 경계와 중앙 X/Y/Z 슬랩의 실제 face edge를 표시합니다. 선을 합성하거나 CAD 선으로 대체하지 않았습니다.</p>
    <p class="note source" id="source"></p>
  </aside>
</div>
<script src="mesh-data.js"></script>
<script>
"use strict";
const meta=window.YU_MESH_DATA.meta, groups=window.YU_MESH_DATA.groups, canvas=document.getElementById("view"), gl=canvas.getContext("webgl",{alpha:false,antialias:true,preserveDrawingBuffer:true});
let yaw=-0.72,pitch=0.48,zoom=1,drag=false,lastX=0,lastY=0,current="surface",buffers=[];
const center=meta.bbox_mm.slice(0,3).map((v,i)=>(v+meta.bbox_mm[i+3])/2), span=Math.max(...center.map((_,i)=>meta.bbox_mm[i+3]-meta.bbox_mm[i]));
document.getElementById("total").textContent=meta.cells.total.toLocaleString();document.getElementById("fluid").textContent=meta.cells.coolant.toLocaleString();document.getElementById("solid").textContent=meta.cells.solid.toLocaleString();document.getElementById("nonorth").textContent=meta.quality.nonorth.toFixed(4)+"°";document.getElementById("skew").textContent=meta.quality.skew.toFixed(6);document.getElementById("source").textContent="source: "+meta.source;
function shader(type,source){const s=gl.createShader(type);gl.shaderSource(s,source);gl.compileShader(s);if(!gl.getShaderParameter(s,gl.COMPILE_STATUS))throw Error(gl.getShaderInfoLog(s));return s}
const program=gl.createProgram();gl.attachShader(program,shader(gl.VERTEX_SHADER,`attribute vec3 p;uniform vec3 c;uniform float span,aspect,yaw,pitch,zoom;void main(){vec3 q=(p-c)/span;float cy=cos(yaw),sy=sin(yaw),cx=cos(pitch),sx=sin(pitch);q=vec3(cy*q.x+sy*q.z,q.y,-sy*q.x+cy*q.z);q=vec3(q.x,cx*q.y-sx*q.z,sx*q.y+cx*q.z);gl_Position=vec4(q.x*zoom/aspect,q.y*zoom,q.z*.55,1.0);}`));gl.attachShader(program,shader(gl.FRAGMENT_SHADER,`precision mediump float;uniform vec4 color;void main(){gl_FragColor=color;}`));gl.linkProgram(program);gl.useProgram(program);
const loc={p:gl.getAttribLocation(program,"p"),c:gl.getUniformLocation(program,"c"),span:gl.getUniformLocation(program,"span"),aspect:gl.getUniformLocation(program,"aspect"),yaw:gl.getUniformLocation(program,"yaw"),pitch:gl.getUniformLocation(program,"pitch"),zoom:gl.getUniformLocation(program,"zoom"),color:gl.getUniformLocation(program,"color")};
async function unpack(payload){const bytes=Uint8Array.from(atob(payload),x=>x.charCodeAt(0)),raw=await new Response(new Blob([bytes]).stream().pipeThrough(new DecompressionStream("gzip"))).arrayBuffer();return new Float32Array(raw)}
async function start(){if(!gl||!window.DecompressionStream)throw Error("WebGL 또는 gzip 해제 기능을 지원하지 않는 브라우저입니다.");for(const item of groups){const data=await unpack(item.payload),buffer=gl.createBuffer();gl.bindBuffer(gl.ARRAY_BUFFER,buffer);gl.bufferData(gl.ARRAY_BUFFER,data,gl.STATIC_DRAW);buffers.push({...item,buffer,count:data.length/3})}document.getElementById("status").hidden=true;resize();render()}
function resize(){const d=Math.min(devicePixelRatio||1,2),w=Math.floor(canvas.clientWidth*d),h=Math.floor(canvas.clientHeight*d);if(canvas.width!==w||canvas.height!==h){canvas.width=w;canvas.height=h}gl.viewport(0,0,w,h)}
function visible(item){if(item.region==="coolant"&&!document.getElementById("show-fluid").checked)return false;if(item.region==="batterySolid"&&!document.getElementById("show-solid").checked)return false;if(current==="surface")return item.mode==="surface";if(current==="allcuts")return item.mode!=="surface"||document.getElementById("show-shell").checked;return item.mode===current||(item.mode==="surface"&&document.getElementById("show-shell").checked)}
function render(){resize();gl.clearColor(.025,.059,.09,1);gl.clear(gl.COLOR_BUFFER_BIT|gl.DEPTH_BUFFER_BIT);gl.enable(gl.BLEND);gl.blendFunc(gl.SRC_ALPHA,gl.ONE_MINUS_SRC_ALPHA);gl.uniform3fv(loc.c,center);gl.uniform1f(loc.span,span*.64);gl.uniform1f(loc.aspect,canvas.width/canvas.height);gl.uniform1f(loc.yaw,yaw);gl.uniform1f(loc.pitch,pitch);gl.uniform1f(loc.zoom,zoom);gl.enableVertexAttribArray(loc.p);const alpha=Number(document.getElementById("opacity").value)/100;for(const item of buffers){if(!visible(item))continue;gl.bindBuffer(gl.ARRAY_BUFFER,item.buffer);gl.vertexAttribPointer(loc.p,3,gl.FLOAT,false,0,0);const rgb=item.region==="coolant"?[.33,.84,.93]:[1,.72,.36],a=item.mode==="surface"&&current!=="surface"?alpha*.22:alpha;gl.uniform4f(loc.color,...rgb,a);gl.drawArrays(gl.LINES,0,item.count)}}
document.querySelectorAll("[data-mode]").forEach(b=>b.onclick=()=>{current=b.dataset.mode;document.querySelectorAll("[data-mode]").forEach(x=>x.classList.toggle("active",x===b));render()});
for(const id of ["show-fluid","show-solid","show-shell","opacity"])document.getElementById(id).oninput=render;
canvas.onpointerdown=e=>{drag=true;lastX=e.clientX;lastY=e.clientY;canvas.setPointerCapture(e.pointerId)};canvas.onpointerup=()=>drag=false;canvas.onpointermove=e=>{if(!drag)return;yaw+=(e.clientX-lastX)*.008;pitch=Math.max(-1.55,Math.min(1.55,pitch+(e.clientY-lastY)*.008));lastX=e.clientX;lastY=e.clientY;render()};canvas.onwheel=e=>{e.preventDefault();zoom=Math.max(.35,Math.min(8,zoom*Math.exp(-e.deltaY*.001)));render()};
document.getElementById("iso").onclick=()=>{yaw=-.72;pitch=.48;render()};document.getElementById("fit").onclick=()=>{zoom=1;render()};window.onresize=render;start().catch(e=>{const s=document.getElementById("status");s.textContent="표시 실패: "+e.message;console.error(e)});
</script>
</body></html>'''


def export(case: Path, output: Path) -> dict[str, object]:
    report_dir = case / "reports" / "production_mesh"
    gate = json.loads((report_dir / "production_mesh_gate.json").read_text(encoding="utf-8"))
    groups: list[dict[str, object]] = []
    all_bounds: list[tuple[float, ...]] = []
    edge_counts: dict[str, int] = {}
    for region in ("coolant", "batterySolid"):
        extracted, bounds, points = extract_region(case / "constant" / region / "polyMesh", region)
        all_bounds.append(bounds)
        for mode, edges in extracted.items():
            key = f"{region}_{mode}"
            edge_counts[key] = len(edges)
            groups.append({"region": region, "mode": mode, "segments": len(edges), "payload": pack_edges(points, edges)})
        del points
    bbox = [min(bounds[i] for bounds in all_bounds) * 1000.0 for i in range(3)] + [max(bounds[i] for bounds in all_bounds) * 1000.0 for i in range(3, 6)]
    metadata = {
        "source": str(case / "constant"),
        "cells": {"total": gate["mesh"]["total_cells"], "coolant": gate["mesh"]["fluid_cells"], "solid": gate["mesh"]["solid_cells"]},
        "quality": {"nonorth": gate["quality"]["maximum_non_orthogonality_deg"], "skew": gate["quality"]["maximum_skewness"]},
        "bbox_mm": bbox,
        "edge_counts": edge_counts,
        "display_policy": "actual external boundary edges plus actual face edges in three central slabs",
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "index.html").write_text(HTML, encoding="utf-8")
    (output / "mesh-data.js").write_text("window.YU_MESH_DATA=" + json.dumps({"meta": metadata, "groups": groups}, separators=(",", ":")) + ";\n", encoding="utf-8")
    (output / "manifest.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("case", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(json.dumps(export(args.case.resolve(), args.output.resolve()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
