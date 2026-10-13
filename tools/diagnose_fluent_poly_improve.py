from __future__ import annotations

import shutil
from pathlib import Path

from ansys.fluent.core import launch_fluent


SOURCE = Path(
    r"C:\Users\user\AppData\Local\YU_Fluent\runs\5bb57aa529bf440e9e20b29fd91f126c\mesh\phi19_1000k_watertight_polyhexcore.msh.h5"
)
DIAG = Path(r"C:\Users\user\AppData\Local\YU_Fluent\diagnostic_poly_improve")
INPUT = DIAG / "input.msh.h5"
OUTPUT = DIAG / "improved.msh.h5"
TRANSCRIPT = DIAG / "improve_transcript.log"

DIAG.mkdir(parents=True, exist_ok=True)
shutil.copy2(SOURCE, INPUT)
session = launch_fluent(
    mode="meshing",
    precision="double",
    processor_count=2,
    product_version="24.1.0",
    cwd=str(DIAG),
    cleanup_on_exit=True,
    start_transcript=False,
)
try:
    session.transcript.start(TRANSCRIPT)
    session.tui.file.read_mesh(str(INPUT))
    session.tui.mesh.poly.quality_method("orthoskew")
    session.tui.mesh.poly.improve()
    session.tui.report.mesh_size()
    session.tui.report.cell_quality_limits()
    session.tui.file.write_mesh(str(OUTPUT))
finally:
    try:
        session.transcript.stop()
    except Exception:
        pass
    session.exit()
