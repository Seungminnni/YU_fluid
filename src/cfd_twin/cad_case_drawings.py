"""Case-specific orthographic CAD diagrams from each viewer geometry payload."""

from __future__ import annotations

from html import escape
from pathlib import Path
from typing import Any


def _svg(body: str, width: int, height: int) -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}"><rect width="100%" height="100%" fill="white"/>'
            f'{body}</svg>\n')


def _top(geometry: dict[str, Any], diameter: float, *, english: bool) -> str:
    outline = geometry["outline_mm"]
    xmin = min(float(point[0]) for point in outline)
    xmax = max(float(point[0]) for point in outline)
    ymin = min(float(point[1]) for point in outline)
    ymax = max(float(point[1]) for point in outline)
    scale = min(6.0, 684.0 / (xmax - xmin), 726.0 / (ymax - ymin))
    x = lambda value: 60 + scale * (float(value) - xmin)
    y = lambda value: 55 + scale * (ymax - float(value))
    points = " ".join(f"{x(px):.2f},{y(py):.2f}" for px, py in geometry["outline_mm"])
    parts = [
        f'<text x="60" y="28" font-size="19" font-weight="700" fill="#17324d">{("CAD TOP" if english else "CAD 상면도")} · Ø{diameter:g} mm</text>',
        f'<polygon points="{points}" fill="#f5fbff" stroke="#17324d" stroke-width="2.5"/>',
    ]
    for cell in geometry["cells"]:
        cx, cy, radius = x(cell["x"]), y(cell["y"]), scale * float(cell["radius"])
        parts.append(f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="{radius:.2f}" fill="#dcecff" stroke="#547b9f" stroke-width="1.3"/>')
        parts.append(f'<text x="{cx:.2f}" y="{cy+4:.2f}" text-anchor="middle" font-size="11" fill="#183a58">{escape(str(cell["id"]))}</text>')
    for port in geometry["ports"]:
        inlet = port["id"] == "IN"
        cx, cy, radius = x(port["x"]), y(port["y"]), scale * float(port["radius"])
        color = "#1264c5" if inlet else "#cc3333"
        parts.append(f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="{radius:.2f}" fill="none" stroke="{color}" stroke-width="4"/>')
        parts.append(f'<text x="{cx:.2f}" y="{cy-radius-8:.2f}" text-anchor="middle" font-size="14" font-weight="700" fill="{color}">{port["id"]} Ø{2*float(port["radius"]):g}</text>')
    parts += [
        f'<line x1="60" y1="805" x2="{x(xmax):.2f}" y2="805" stroke="#526779" stroke-width="1.5"/>',
        f'<text x="{(x(xmin)+x(xmax))/2:.2f}" y="798" text-anchor="middle" font-size="14" fill="#263f52">{xmax-xmin:g} mm</text>',
        f'<line x1="770" y1="55" x2="770" y2="{y(ymin):.2f}" stroke="#526779" stroke-width="1.5"/>',
        f'<text x="781" y="418" font-size="14" fill="#263f52">{ymax-ymin:g} mm</text>',
        '<text x="60" y="832" font-size="12" fill="#526779">'
        + ((f'Blue: inlet · Red: outlet · {len(geometry["cells"])} battery cells Ø{2*float(geometry["cells"][0]["radius"]):g} mm')
           if english else (f'파랑: 입수구 · 빨강: 출수구 · 배터리 {len(geometry["cells"])}셀 Ø{2*float(geometry["cells"][0]["radius"]):g} mm'))
        + '</text>',
    ]
    return _svg("".join(parts), 860, 850)


def _side(geometry: dict[str, Any], diameter: float, *, english: bool) -> str:
    outline = geometry["outline_mm"]
    xmin = min(float(point[0]) for point in outline)
    xmax = max(float(point[0]) for point in outline)
    tank_z = float(geometry.get("enclosure_mm", {}).get("z", 82))
    port_bottom = min(float(port["z"]) - float(port.get("length", 32)) / 2 for port in geometry["ports"])
    port_top = max(float(port["z"]) + float(port.get("length", 32)) / 2 for port in geometry["ports"])
    zmin, zmax = min(0.0, port_bottom), max(tank_z, port_top)
    scale_x = min(6.0, 684.0 / (xmax - xmin))
    scale_z = min(2.4, 350.0 / (zmax - zmin))
    x = lambda value: 70 + scale_x * (float(value) - xmin)
    z = lambda value: 40 + scale_z * (zmax - float(value))
    parts = [
        f'<text x="70" y="30" font-size="19" font-weight="700" fill="#17324d">{("CAD SIDE" if english else "CAD 측면도")} · Ø{diameter:g} mm</text>',
        f'<rect x="{x(xmin):.2f}" y="{z(tank_z):.2f}" width="{x(xmax)-x(xmin):.2f}" height="{z(0)-z(tank_z):.2f}" fill="#f5fbff" stroke="#17324d" stroke-width="2.5"/>',
    ]
    columns = {float(cell["x"]): cell for cell in geometry["cells"]}
    for cx, cell in sorted(columns.items()):
        radius = float(cell["radius"])
        cell_top = float(cell["z"]) + float(cell["length"]) / 2
        cell_bottom = float(cell["z"]) - float(cell["length"]) / 2
        parts.append(f'<rect x="{x(cx-radius):.2f}" y="{z(cell_top):.2f}" width="{2*radius*scale_x:.2f}" height="{z(cell_bottom)-z(cell_top):.2f}" fill="#dcecff" fill-opacity=".65" stroke="#547b9f" stroke-width="1.3"/>')
    for port in geometry["ports"]:
        inlet = port["id"] == "IN"
        color = "#1264c5" if inlet else "#cc3333"
        radius = float(port["radius"])
        top = float(port["z"]) + float(port.get("length", 32)) / 2
        bottom = float(port["z"]) - float(port.get("length", 32)) / 2
        parts.append(f'<rect x="{x(float(port["x"])-radius):.2f}" y="{z(top):.2f}" width="{2*radius*scale_x:.2f}" height="{z(bottom)-z(top):.2f}" fill="{color}" fill-opacity=".28" stroke="{color}" stroke-width="3"/>')
        label_z = float(port["z"])
        parts.append(f'<text x="{x(port["x"]):.2f}" y="{z(label_z):.2f}" text-anchor="middle" font-size="14" font-weight="700" fill="{color}">{port["id"]} Ø{2*radius:g}</text>')
    parts += [
        f'<line x1="70" y1="430" x2="{x(xmax):.2f}" y2="430" stroke="#526779" stroke-width="1.5"/>',
        f'<text x="{(x(xmin)+x(xmax))/2:.2f}" y="422" text-anchor="middle" font-size="14" fill="#263f52">{xmax-xmin:g} mm</text>',
        '<text x="760" y="180" font-size="14" fill="#263f52">' + (f'{tank_z:g} mm tank' if english else f'탱크 {tank_z:g} mm') + '</text>',
        '<text x="70" y="465" font-size="12" fill="#526779">'
        + (f'Blue: inlet · Red: outlet · {float(geometry["cells"][0]["length"]):g} mm battery cells'
           if english else f'파랑: 입수구 · 빨강: 출수구 · 배터리 높이 {float(geometry["cells"][0]["length"]):g} mm')
        + '</text>',
    ]
    return _svg("".join(parts), 870, 490)


def write_case_drawings(model: dict[str, Any], viewer: Path) -> list[str]:
    cad = viewer / "cad"
    cad.mkdir(parents=True, exist_ok=True)
    written = []
    for run in model["mesh_runs"]:
        run_id = str(run["id"])
        geometry = model.get("geometry_by_mesh", {}).get(run_id)
        if not isinstance(geometry, dict):
            raise ValueError(f"missing geometry for {run_id}")
        diameter = float(run["inlet_diameter_mm"])
        for english in (False, True):
            suffix = ".en.svg" if english else ".svg"
            for view, content in (("top", _top(geometry, diameter, english=english)),
                                  ("side", _side(geometry, diameter, english=english))):
                filename = f"cad_{run_id}_{view}{suffix}"
                (cad / filename).write_text(content, encoding="utf-8")
                written.append(filename)
    return written
