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
| Standard calculation | Calculation and 0–1800 s replay complete | 216,577 | 159,107 | 57,470 | 252,907 | 185,013 hexahedra (85.426%), 4,452 prisms, 301 wedges, 518 tet-wedges, 26,293 polyhedra |

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
- New measured count report:
  `build/case_validated/production/single/reports/production_mesh/mesh_target_composition.json`
- New 1-second thermal validation:
  `build/case_validated/production/single/runs/frozen_flow_actual_1s/reports/solver/frozen_flow_validation.json`

## Geometry, mesh and physics

The shared geometry is a 114 × 121 × 82 mm notched enclosure containing 23
cylindrical cells (Ø19 × 68 mm) in a staggered 5–4–5–4–5 arrangement. Both
ports are Ø19 mm with 32 mm external lengths.

The precision mesh used 2.4 mm background cells, battery surface refinement
0/1, battery-interior refinement level 1, enclosure refinement 1/2, ten
coolant-side prism layers, a 14 µm first layer, expansion ratio 1.2 and ten MPI
ranks. The standard profile uses a 5.0 mm background, battery-interior level
1, ten prism layers and a 14 µm first layer. After body fitting, only the
4 mm-radius battery cores (with 6 mm axial end clearance) are refined once;
this raises solid resolution without modifying the paired CHT interface. The
standard coarse-mesh layer gate records the measured C11 limitation explicitly
and requires at least 75% surface coverage and 45% wanted thickness, followed
by the mandatory solver-result y+ gate. Its mesh
gate requires:

- total cell count within 10% of 207,390;
- fluid, solid and point counts within 20% of their targets;
- each discrete battery interface area within 6% of the analytic cylinder area
  (the paired coolant/solid areas must still match to numerical precision);
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
time-varying conjugate temperature field. The standard calculation does the
same because this case uses constant density, viscosity and thermal properties
with zero gravity: temperature cannot feed back into momentum, so repeatedly
solving velocity would reproduce the same converged field. The coupled fluid
and solid energy equations advance at 1 s physical steps. Direct telemetry and
both regional temperature fields are written at every integer second from 0
through 1,800 s; the validated static flow field is reused at each display
state without temporal interpolation. A fully recomputed PIMPLE path remains
available for future dynamic-thermal coupling. A measured pilot on the standard
mesh showed that the 14 µm prisms reduce the Courant-limited internal step to
about 0.003425 s at `maxCo=5`. Therefore the full path must pass staged 1 s,
10 s and 60 s checks before an 1,800 s run is authorized. The browser output
interval may stay at 1 s even when the numerical solver takes many smaller
steps within that second.

A time-varying source by itself does not change `U` when fluid properties are
constant and gravity is zero; the present frozen-flow result remains a valid
one-way energy calculation under exactly those assumptions. The full PIMPLE
path becomes mandatory when the future UDF introduces temperature/SOC feedback
together with temperature-dependent density or viscosity, buoyancy, or any
other thermal-to-momentum coupling. Temperature-dependent conductivity or heat
capacity alone requires updated nonlinear energy coupling but not a momentum
recalculation. The implementation
and acceptance gates are fixed in [`docs/FULL_TRANSIENT_PLAN.md`](docs/FULL_TRANSIENT_PLAN.md).

The convergence graph uses raw OpenFOAM initial linear-solver residuals. The
standard result plots its 1,800 one-second energy iterations; the optional
fully repeated profile uses cumulative PIMPLE outer corrections. Residuals are
not renamed or rescaled to another solver's definition. Residual decrease,
mass balance, energy balance, outlet stability, field bounds, prism coverage
and y+ remain separate acceptance checks.

Measured standard-result checks and final values:

| Quantity | Result |
| --- | ---: |
| Cold-flow battery-wall y+ below 1 / p95 / mean | 95.320% / 0.9527 / 0.3736 |
| Cold-flow mean pressure drop / maximum mass imbalance | 260.413 Pa / 2.804×10⁻⁸ |
| 1,800 s outlet mass-weighted temperature | 20.015261 °C |
| 1,800 s battery internal volume average | 20.106350 °C |
| 1,800 s battery mesh maximum | 20.193303 °C (C05) |
| 1,800 s battery surface area average | 20.098967 °C |
| Final-window mean energy imbalance | 0.018936% |
| Transient vs steady Tavg / Tmax difference | −0.000579 K / +0.001887 K |

The mesh, cold-flow, steady CHT, 1-second transient storage and browser bundle
all pass their configured gates. These values are OpenFOAM results for the
declared model, not relabelled Fluent output and not proof of agreement with a
separate paper by themselves.

## Installation and checks

Python 3.12 or newer is required.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[cad,results,media]'
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
on macOS, Windows and Linux. Meshing is serial by default for reproducible prism
layers (`OPENFOAM_MESH_MPI_RANKS` can override it); the solvers use ten ranks by
default (`OPENFOAM_MPI_RANKS` can override them). Execute the scripts in this
order:

```bash
./run-production-mesh.sh
./run-cold-flow-pilot.sh
./run-cold-flow-continue.sh       # only if the residual/stability gate requests it
./run-steady-pilot.sh
./run-steady-continue.sh          # only if the steady gate requests it
./run-frozen-flow-actual-1s.sh    # canonical exact one-way CHT, 0–1800 s
./run-full-transient-dt-pilot.sh      # mandatory short pilot before full coupling
./run-full-transient-dt-half-pilot.sh # time-step sensitivity comparison
./run-full-transient-actual-1s.sh     # only after the staged full-coupling gates pass
```

The two shorter transient pilots check time-step sensitivity. The canonical
constant-property, zero-gravity calculation writes one energy solve and both
regional temperature fields per physical second under
`runs/frozen_flow_actual_1s`; its velocity/pressure field comes from the passed
cold-flow solve. No solver launcher starts unless the production mesh gate has
passed.

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

All velocity views use one mesh-specific global range taken from every coolant
cell-centred `|U|` value. Streamline colour, XY/XZ section colour, vector length
and port colour therefore have the same numerical meaning. The viewer also
shows the selected section's local peak so a genuinely slower centre section is
not mistaken for a different scale. Moving heads use the same actual-`|U|`
proportional rule and the same display-rate multiplier in every mode; their
screen travel differs because a section head moves on a short vector glyph
while the combined view head follows a full 3D streamline.

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

## Generated-artifact policy

The authoritative solver fields, their validation/provenance reports and the
current portable viewer are the results that must be retained. Failed attempts,
interrupted MPI partitions, superseded result copies, Gmsh smoke meshes,
candidate CAD renders, temporary PDF renders, Python caches and duplicate ZIP
files are classified as disposable build artifacts. Some large deletion
candidates remain on disk until they receive separate explicit deletion
authorization. The retained directories are:

- `build/case_validated` — CAD, mesh, solver fields, telemetry and validation;
- `build/reference_viewer` — portable current viewer;
- `build/case_validated/production/L3` — archived precision-result provenance;
- `build/case_validated/production/single` — active standard calculation.

Within the precision directory, `runs/frozen_flow_actual_1s_xy5` is the
authoritative one-second replay. The older replay without all five XY cuts is
superseded but still exists as a Git-ignored deletion candidate. Git ignores
`build/`, local environments, dependency caches, documents, temporary files
and archives so large or private artifacts are not committed accidentally. All
generated files must be reproducible from the tracked configuration and source
code; a failed run may be deleted after its small diagnostic facts have been
recorded in documentation and the exact deletion scope has been authorized.
