from __future__ import annotations

import json

from ansys.fluent.core import launch_fluent
from ansys.fluent.core.services.datamodel_tui import PyMenu


session = launch_fluent(
    mode="meshing",
    precision="double",
    processor_count=1,
    product_version="24.1.0",
    cleanup_on_exit=True,
)
try:
    session.workflow.InitializeWorkflow(WorkflowType="Watertight Geometry")
    volume_command = session.meshing.GenerateTheVolumeMeshWTM
    improve_command = session.meshing.ImproveVolumeMesh
    volume_instance = volume_command.create_instance()
    improve_instance = improve_command.create_instance()
    print(
        json.dumps(
            {
                "meshing_volume_names": [
                    name for name in dir(session.meshing) if "volume" in name.lower()
                ],
                "volume_command_keys": dir(volume_command),
                "volume_static_info": volume_command._get_static_info(),
                "volume_instance_state": volume_instance.get_state(),
                "improve_command_keys": dir(improve_command),
                "improve_static_info": improve_command._get_static_info(),
                "improve_instance_state": improve_instance.get_state(),
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )
    poly = session.tui.mesh.poly
    modify = session.tui.mesh.modify
    prism_improve_menu = session.tui.mesh.prism.improve
    commands = {
        "poly_improve": object.__getattribute__(poly, "improve"),
        "auto_node_move": object.__getattribute__(modify, "auto_node_move"),
        "prism_smooth_improve": object.__getattribute__(
            prism_improve_menu, "smooth_improve_prism_cells"
        ),
        "prism_improve": object.__getattribute__(prism_improve_menu, "improve_prism_cells"),
    }
    result = {}
    for name, command in commands.items():
        menu = PyMenu(command._service, command._version, command._mode, command._path)
        result[name] = menu.get_static_info()
    print(json.dumps(result, ensure_ascii=False, indent=2))
finally:
    session.exit()
