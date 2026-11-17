from __future__ import annotations

import argparse
import re
from pathlib import Path


COOLANT_PROBES = (
    "actual_xy04_temperature_1s",
    "actual_xy15_temperature_1s",
    "actual_xy33_temperature_1s",
    "actual_xy_temperature_1s",
    "actual_xy68_temperature_1s",
    "actual_xy85_temperature_1s",
    "actual_xy96_temperature_1s",
    "actual_xz15_temperature_1s",
    "actual_xz33_temperature_1s",
    "actual_xz50_temperature_1s",
    "actual_xz68_temperature_1s",
    "actual_xz85_temperature_1s",
    "actual_yz15_temperature_1s",
    "actual_yz33_temperature_1s",
    "actual_yz50_temperature_1s",
    "actual_yz68_temperature_1s",
    "actual_yz85_temperature_1s",
)

BATTERY_PROBES = ("actual_battery_near_surface_temperature_1s",)


def extract_block(text: str, name: str) -> str:
    match = re.search(rf"(?m)^[ \t]*{re.escape(name)}[ \t]*\r?$", text)
    if not match:
        raise ValueError(f"missing function object: {name}")
    opening = text.find("{", match.end())
    if opening < 0:
        raise ValueError(f"missing opening brace: {name}")
    depth = 0
    for index in range(opening, len(text)):
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                block = text[match.start() : index + 1]
                if re.search(r"(?m)^\s*enabled\s+false\s*;", block):
                    block = re.sub(
                        r"(?m)^(\s*enabled\s+)false(\s*;)",
                        r"\1true\2",
                        block,
                    )
                elif not re.search(r"(?m)^\s*enabled\s+", block):
                    block = block[:-1] + "    enabled true;\n}"
                return block
    raise ValueError(f"unterminated function object: {name}")


def render(text: str, names: tuple[str, ...]) -> str:
    blocks = "\n".join(extract_block(text, name) for name in names)
    return f"""FoamFile
{{
    format ascii;
    class dictionary;
    object controlDict;
}}

application postProcess;
startFrom startTime;
startTime 0;
stopAt endTime;
endTime 1800;
deltaT 1;
writeControl timeStep;
writeInterval 1;

functions
{{
{blocks}
}}
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("control_dict", type=Path)
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()
    text = args.control_dict.read_text(encoding="utf-8")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    coolant = args.output_dir / "postProcessActualCoolantProbes"
    battery = args.output_dir / "postProcessActualBatteryProbes"
    coolant.write_text(render(text, COOLANT_PROBES), encoding="utf-8", newline="\n")
    battery.write_text(render(text, BATTERY_PROBES), encoding="utf-8", newline="\n")
    print(coolant)
    print(battery)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
