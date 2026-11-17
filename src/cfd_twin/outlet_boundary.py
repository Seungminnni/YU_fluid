"""Explicit pressure-outlet backflow trial and measured outlet diagnostics."""
from __future__ import annotations

import gzip
import hashlib
import json
import math
from pathlib import Path
import re

from .openfoam_mesh_integrals import _boundary
from .openfoam_yplus_validation import _balanced_block, parse_scalar_boundary_field

PROFILES = ("pressure_backflow", "flux_corrected_outflow")


def validate_profile(profile: str | None) -> None:
    if profile is not None and profile not in PROFILES:
        raise ValueError(f"Unknown outlet_velocity_profile: {profile}")


def apply_profile(case: Path, profile: str | None) -> None:
    validate_profile(profile)
    if profile is None:
        return
    if (case / 'runs').exists() and any((case / 'runs').iterdir()):
        raise ValueError('Outlet boundary profiles require a fresh case')
    path = case / '0/coolant/U'
    before = path.read_text(encoding='utf-8')
    old = ('    outlet\n    {\n        type inletOutlet;\n'
           '        inletValue uniform (0 0 0);\n        value uniform (0 0 0);\n    }')
    boundary_type = (
        "fluxCorrectedVelocity"
        if profile == "flux_corrected_outflow"
        else "pressureInletOutletVelocity"
    )
    new = (f'    outlet\n    {{\n        type {boundary_type};\n'
           '        value uniform (0 0 0);\n    }')
    if before.count(old) != 1:
        raise ValueError('Unexpected fresh outlet velocity boundary')
    after = before.replace(old, new)
    path.write_text(after, encoding='utf-8', newline='\n')
    report = case / 'reports/outlet_boundary.json'
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps({
        'profile': profile, 'field': 'U', 'patch': 'outlet',
        'before_type': 'inletOutlet', 'after_type': boundary_type,
        'before_sha256': hashlib.sha256(before.encode()).hexdigest(),
        'after_sha256': hashlib.sha256(after.encode()).hexdigest(),
        'boundary_condition_changed': True, 'operating_inputs_changed': False,
        'acceptance_limits_changed': False,
        'comparison_note': (
            'Outlet velocity is explicitly corrected from phi on outflow.'
            if profile == 'flux_corrected_outflow'
            else 'Outlet reverse-flow treatment differs from the original runs.'
        ),
    }, indent=2) + '\n', encoding='utf-8', newline='\n')


def record_outlet(case: Path, iteration: str) -> dict:
    """Measure backflow from reconstructed fields, without advancing a solver."""
    if not re.fullmatch(r'\d+', iteration):
        raise ValueError('Expected an integer steady iteration')
    run = case / 'runs/steady_cold_flow'
    _, count = _boundary(run / 'constant/coolant/polyMesh/boundary')['outlet']
    field_dir = run / iteration / 'coolant'
    flux = parse_scalar_boundary_field(field_dir / 'phi')['outlet']
    path = field_dir / 'U'
    raw = path.read_text(encoding='utf-8') if path.exists() else gzip.open(path.with_suffix('.gz'), 'rt', encoding='utf-8').read()
    raw = raw[raw.index('boundaryField'):]
    match = re.search(r'\boutlet\s*\{', raw)
    if match is None:
        raise ValueError('Missing outlet velocity patch')
    block = _balanced_block(raw, raw.find('{', match.start()))
    match = re.search(r'value\s+nonuniform\s+List<vector>\s+(\d+)\s*\((.*?)\)\s*;', block, re.S)
    if match:
        velocity = [tuple(map(float, row.split())) for row in re.findall(r'\(([^()]+)\)', match.group(2))]
        if len(velocity) != int(match.group(1)):
            raise ValueError('Outlet velocity count does not match declaration')
    else:
        match = re.search(r'value\s+uniform\s*\((.*?)\)', block)
        if match is None:
            raise ValueError('Missing outlet velocity values')
        velocity = [tuple(map(float, match.group(1).split()))] * count
    if len(flux) == 1:
        flux = flux * count
    if not count or len(flux) != count or len(velocity) != count:
        raise ValueError('Outlet field/mesh face count mismatch')
    if any(len(v) != 3 or not all(math.isfinite(x) for x in v) for v in velocity) or not all(math.isfinite(x) for x in flux):
        raise ValueError('Invalid outlet field values')
    speeds = [math.sqrt(sum(x*x for x in row)) for row in velocity]
    reverse = [i for i, phi in enumerate(flux) if phi < 0]
    net = sum(flux)
    reverse_mass = -sum(flux[i] for i in reverse)
    report = {
        'job_id': case.parent.name, 'iteration': int(iteration), 'diagnostic_only': True,
        'outlet_face_count': count, 'backflow_face_count': len(reverse),
        'backflow_mass_kg_s': reverse_mass, 'net_outlet_mass_kg_s': net,
        'backflow_to_net_mass_ratio': reverse_mass / net if net > 0 else None,
        'zero_speed_backflow_faces': sum(speeds[i] < 1e-12 for i in reverse),
        'maximum_backflow_speed_m_s': max((speeds[i] for i in reverse), default=0),
    }
    destination = case / 'diagnostics/outlet' / f'{iteration}.json'
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8', newline='\n')
    return report
