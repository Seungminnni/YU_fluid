# 23-Cell CFD Digital Twin

This repository generates and validates a 23-cell immersion-cooling OpenFOAM
case and packages the calculated fields into a portable interactive viewer.
Calculated fields are never replaced by synthetic data or by values from a
different mesh.

## Current state

Two mesh records are intentionally kept separate:

| Viewer option | State | Total cells | Fluid | Battery solid | Points | Mesh composition |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| Precision result | Calculation and 0–1800 s replay complete | 1,316,854 | 1,061,438 | 255,416 | 1,403,119 | 1,245,779 hexahedra (94.603%), 14,981 prisms, 790 wedges, 1,005 tet-wedges, 54,299 polyhedra |
| Standard calculation | Source prepared; calculation pending | target 207,390 | target 136,550 | target 70,840 | target 221,878 | hexa-dominant, at least 80% hexahedra |

The precision result is preserved under the historical directory
`build/case_validated/production/L3`; that directory name is provenance only
and is not exposed as a user-facing calculation level. The active production
generator accepts one profile only: `single`. It never labels target counts as
measured counts, and it never displays precision-mesh fields as standard-mesh
results. The superseded L0, L1 and L2 production directories were removed after
their validation summaries were consolidated; only the precision result and
the active standard calculation are retained.

Exact provenance for the preserved result:

- Cell and zone counts:
  `build/case_validated/production/L3/reports/production_mesh/cellZones_pre_split.json`
- Mesh topology:
  `build/case_validated/production/L3/logs/production_mesh/checkMesh_pre_split_diagnostic.log`
- Steady solver validation:
  `build/case_validated/production/L3/runs/steady_cht/reports/solver/steady_cht_validation.json`
- One-second replay validation:
  `build/case_validated/production/L3/runs/frozen_flow_actual_1s_xy5/reports/viewer/actual_1s_validation.json`
- New standard profile:
  `build/case_validated/production/single/production_mesh_manifest.json`
- New measured count report, available after meshing:
  `build/case_validated/production/single/reports/production_mesh/mesh_target_composition.json`

## Geometry, mesh and physics

The shared geometry is a 114 × 121 × 82 mm notched enclosure containing 23
cylindrical cells (Ø19 × 68 mm) in a staggered 5–4–5–4–5 arrangement. Both
ports are Ø19 mm with 32 mm external lengths.

The precision mesh used 2.4 mm background cells, battery surface refinement
0/1, battery-interior refinement level 1, enclosure refinement 1/2, ten
coolant-side prism layers, a 14 µm first layer, expansion ratio 1.2 and ten MPI
ranks. The standard profile starts from a 5.0 mm background calibration with
the same refinement layout, ten prism layers and a 15 µm first layer. Its mesh
gate requires:

- total cell count within 10% of 207,390;
- fluid, solid and point counts within 20% of their targets;
- exact, exclusive coverage by the coolant and 23 cell zones;
- at least 80% hexahedra;
- passed topology, port-area, interface, prism-layer and volume checks.

“Hexahedral-based” means hexa-dominant. Snapped cylindrical boundaries and
wall-layer transitions can create prisms and cut polyhedra; a strict all-hexa
mesh would require a separate multi-block/O-grid topology.

Baseline calculation inputs:

| Input | Value |
| --- | ---: |
| Coolant | deionized water |
| Density | 998.2 kg/m³ |
| Specific heat | 4,182 J/(kg·K) |
| Thermal conductivity | 0.598 W/(m·K) |
| Dynamic viscosity | 0.00112 Pa·s |
| Inlet velocity | 0.4 m/s |
| Inlet temperature | 293.15 K (20.000 °C) for the new run |
| Outlet pressure | 101,325 Pa absolute |
| Inlet turbulence intensity | 5% |
| Turbulence model | k-ω SST |
| Battery density / heat capacity / conductivity | 2,000 kg/m³ / 900 J/(kg·K) / 27 W/(m·K) |
| Battery heat generation | 16,325 W/m³, constant |
| Outer wall / gravity | adiabatic / zero |
| Solver | OpenCFD OpenFOAM v2512 |

The archived precision replay uses a converged fixed `U(x), p(x)` field and a
time-varying conjugate temperature field. The new standard calculation uses
full transient PIMPLE, adaptive `deltaT <= 0.1 s`, `maxCo = 5` and three outer
correctors. Direct telemetry and compressed full fields are written at every
integer second from 0 through 1,800 s, producing 1,801 display states without
temporal interpolation. At an unchanged 0.1 s numerical step this corresponds
to 18,000 time steps and 54,000 cumulative PIMPLE outer iterations; the actual
log-derived count can be higher when the Courant limit reduces the time step.

The convergence graph uses raw OpenFOAM initial linear-solver residuals against
the cumulative PIMPLE outer-correction number. This follows the iteration
structure of a transient coupled solve, but the residual normalization is not
renamed or rescaled to another solver's definition. Residual decrease, mass
balance, energy balance, outlet stability, field bounds, prism coverage and y+
remain separate acceptance checks.

## Installation and checks

Python 3.12 or newer is required.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[cad,mesh,results,media]'
.venv/bin/cfd-twin doctor
.venv/bin/cfd-twin validate-config configs/baseline_23cell.yaml
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Optional dependencies are separated so a geometry-only or viewer-only machine
does not need the full solver toolchain. Missing solvers or libraries cause an
explicit failure; they never cause generated placeholder CFD values.

## Generate the standard case

Build and validate the CAD first:

```bash
.venv/bin/cfd-twin build-cad-source \
  configs/baseline_23cell.yaml --output build/case_validated/cad_source
.venv/bin/cfd-twin build-cad \
  configs/baseline_23cell.yaml --output build/case_validated/cad_source
```

Generate the only active production mesh/solver bundle:

```bash
.venv/bin/cfd-twin build-production-mesh-source \
  configs/baseline_23cell.yaml \
  --cad-dir build/case_validated/cad_source \
  --output build/case_validated/production/single \
  --profile single \
  --set 'boundary.inlet_temperature=293.15 K' \
  --set 'boundary.initial_fluid_temperature=293.15 K' \
  --set 'boundary.initial_solid_temperature=293.15 K' \
  --set 'battery.initial_temperature=293.15 K'
```

The generated launchers use the same containerized OpenFOAM v2512 environment
on macOS, Windows and Linux. `OPENFOAM_MPI_RANKS` overrides the default ten MPI
ranks. Execute the generated scripts in this order:

```bash
./run-production-mesh.sh
./run-cold-flow-pilot.sh
./run-cold-flow-continue.sh       # only if the residual/stability gate requests it
./run-steady-pilot.sh
./run-steady-continue.sh          # only if the steady gate requests it
./run-full-transient-actual-1s.sh # canonical 0–1800 s result
./run-full-transient-dt-pilot.sh
./run-full-transient-dt-half-pilot.sh
```

The two shorter transient pilots check time-step sensitivity. The canonical
run writes the cumulative residual history to
`runs/full_transient_actual_1s/reports/solver/transient_residuals.csv` together
with a JSON summary. No solver launcher starts unless the production mesh gate
has passed.

## Build and use the viewer

```bash
.venv/bin/cfd-twin build-viewer \
  configs/baseline_23cell.yaml \
  --case-dir build/case_validated \
  --output build/reference_viewer
python3 -m http.server 8765 --directory build/reference_viewer
```

Open `http://127.0.0.1:8765/`. The viewer includes the combined 3D scene, cell
temperature, streamlines, XY/XZ temperature and velocity sections, cell IDs,
inlet/outlet values, convergence history and exportable temperature graphs.
Mouse drag rotates, the wheel zooms, checkboxes control cells/flow/sections,
and multiple section positions can be selected.

The graph panel exports PNG or CSV for outlet average temperature, battery
mesh maximum, area-weighted battery surface average, volume-weighted battery
internal average and C01–C23 temperatures. At 1×, one real second advances one
simulation second, so the complete timeline takes 30 minutes; faster playback
uses the same authoritative integer-second values.

For another computer, copy the entire `build/reference_viewer` directory and
open `index.html`. The portable bundle contains gzip-packed JavaScript data and
does not require a localhost server. Copying only the HTML file omits the model
and CFD fields. Use a current Chrome, Edge or Safari browser.

## Input overrides and provenance

Approved inputs can be changed without editing the baseline YAML:

```bash
.venv/bin/cfd-twin validate-config configs/baseline_23cell.yaml \
  --set 'boundary.inlet_velocity=0.6 m/s' \
  --set 'fluid.dynamic_viscosity=0.001 Pa*s'
```

Configuration overlays remain supported for controlled sensitivity cases:

```bash
.venv/bin/cfd-twin generate-heat-source \
  configs/baseline_23cell.yaml \
  --overlay configs/electrical_joule_example.yaml \
  --output build/electrical_heat
```

Every YAML overlay and command-line override is recorded in the resolved input
and its SHA-256 provenance. Final-design constraints, including the two 19 mm
ports, remain enforced.

## Preserved outputs

Generated solver fields, validation reports and the current portable viewer
are intentionally excluded from source-control cleanup. The main preserved
directories are:

- `build/case_validated` — CAD, mesh, solver fields, telemetry and validation;
- `build/reference_viewer` — portable current viewer;
- `build/case_validated/production/L3` — archived precision-result provenance;
- `build/case_validated/production/single` — active standard calculation.

Do not delete or relabel those directories when cleaning source code. Git
ignores `build/`, local environments, dependency caches, documents and archive
files so large or private artifacts are not committed accidentally.
