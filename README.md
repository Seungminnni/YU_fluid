# MDPI 23-cell immersion-cooling CFD

The canonical requirements are in
[`document/MDPI_CFD_IMPLEMENTATION_FINAL_DESIGN.md`](document/MDPI_CFD_IMPLEMENTATION_FINAL_DESIGN.md).

The implemented baseline uses two 19 mm ports, Table 1–4 properties, a
constant volumetric heat source of 16,325 W/m³, and explicitly records the
paper's inconsistent Reynolds number as reference metadata.

Geometry source priority is user decision → dimensioned PPT → paper tables →
paper-figure digitization. Figure 8-derived layouts and 293.15 K are provided
as sensitivity overlays; they do not silently replace the PPT baseline.

Quick verification:

```bash
PYTHONPATH=src python3 -m mdpi_cfd doctor
PYTHONPATH=src python3 -m mdpi_cfd validate-config configs/baseline_23cell.yaml
PYTHONPATH=src python3 -m mdpi_cfd build-geometry configs/baseline_23cell.yaml --output build/geometry
PYTHONPATH=src python3 -m mdpi_cfd build-viewer configs/baseline_23cell.yaml --output build/viewer
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Optional local CAD/mesh environment:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[cad,mesh,overlay]'
```

Open `build/viewer/index.html` to inspect the 23-cell preview from all six
directions. It is dependency-free and prominently marks that CFD has not run;
its cell colours are identifiers, not synthetic temperatures. The same page
shows Table 2/4 inputs and the Figure 1~4, 6~14 readiness contract.
Pass `--case-dir build/case_validated` when rebuilding the viewer to attach
real Figure artifacts. With validated results, Figure 1 becomes a shared 3D
animation hub with seven radio modes covering Figures 4, 6--14: transient cell
temperature, velocity-coloured streamlines, and transient XY/XZ temperature
sections with fixed-field velocity direction markers. All modes share the
0--1,800 s timeline and mouse orbit/zoom controls. Independent checkboxes hide
or show cells, flow, moving heads, sections, sampling-grid lines and the
enclosure, while the `flow only` preset exposes the inlet-to-outlet paths.
Figures 2--3 remain static
because mesh structure and mesh-independence are not transient quantities.
Temperature legends use 19 labelled major ticks plus minor ticks, keep the
actual CFD colour scale, and print the corresponding paper range underneath
instead of clipping computed values to the reference endpoints.

Generate the measured top-view overlay from the original PPT asset:

```bash
PYTHONPATH=src python3 -m mdpi_cfd build-overlay \
  configs/baseline_23cell.yaml \
  --pptx 'document/20260826 (1).pptx' \
  --output build/overlay
```

The command detects all 23 drawing circles, overlays the configured CAD
centres, and reports the residual in millimetres. It deliberately leaves G1
unapproved because the measured outline/notches are fixed but port y position
and inlet-column assignment remain A-04 sensitivity choices.

Generate deterministic CadQuery/OpenCascade CAD source:

```bash
PYTHONPATH=src python3 -m mdpi_cfd build-cad-source \
  configs/baseline_23cell.yaml --output build/cad
```

This always writes `cad_model.json`, `build_cad.py`, and a validation manifest.
If CadQuery is installed, `build-cad` executes the Boolean model and exports
the enclosure/ports, coolant and 23-cell STEP/STL artifacts. Without CadQuery
it fails explicitly and never writes fake STEP files.

Generate heat-source artifacts:

```bash
PYTHONPATH=src python3 -m mdpi_cfd generate-heat-source \
  configs/baseline_23cell.yaml --output build/heat_source
```

This writes a source manifest, an OpenFOAM `fvOptions` source dictionary and
a Fluent `DEFINE_SOURCE` C file. The dynamic modes require user-provided CSV
profiles and are never labelled as the unpublished paper UDF.

Build the complete traceable case-input bundle in one command:

```bash
PYTHONPATH=src python3 -m mdpi_cfd prepare-case \
  configs/baseline_23cell.yaml --output build/case
```

The bundle contains geometry previews, deterministic CAD source, heat-source
files, OpenFOAM region metadata, resolved inputs and provenance. It remains
truthfully non-runnable until CAD topology, mesh and OpenFOAM gates pass.
With the `cad` optional dependency installed, add `--execute-cad` to include
validated STEP/STL artifacts in the same bundle.

With both `cad` and `mesh` dependencies installed, the complete local
geometry/mesh checkpoint is:

```bash
.venv/bin/mdpi-cfd prepare-case configs/baseline_23cell.yaml \
  --output build/case_validated --execute-mesh smoke
```

`--execute-mesh smoke` implies CAD execution and writes a Gmsh 2.2 mesh with named
`coolant`, `batterySolid`, `inlet`, `outlet`, `walls` and
`batteryInterfaces` physical groups. OpenFOAM conversion and `checkMesh` stay
as explicit gates. Gmsh is restricted to the pipeline smoke test; production
L0-L3 uses the documented `blockMesh` + `snappyHexMesh` path with prism-layer
and y+ gates. The smoke report integrates inlet/outlet/interface areas and
fluid/battery volumes from the actual mesh elements instead of reusing CAD
arithmetic. The generated `openfoam/Allmesh` script performs the
mandatory millimetre-to-metre conversion and region split in the cloud solver
environment.

The CAD export also writes metre-based production surfaces under
`cad_source/production_surfaces`: a closed `outerDomain.stl`, partitioned
inlet/outlet/wall surfaces and `battery_C01.stl` through `battery_C23.stl`.
Production uses OpenFOAM v2512 `snappyHexMesh` cell/face zones and
`splitMeshRegions -addZones` to retain 23 cell identities inside one
`batterySolid` solver region. Meshing and solver pilots default to 10 MPI ranks
on the current 10-CPU Docker allocation; set `OPENFOAM_MPI_RANKS` to override.

After CAD validation, generate the production L0 source bundle with:

```bash
.venv/bin/mdpi-cfd build-production-mesh-source \
  configs/baseline_23cell.yaml \
  --cad-dir build/case_validated/cad_source \
  --output build/case_validated/production/L0 --level L0
```

The generated `AllmeshProduction` is pinned to OpenCFD OpenFOAM v2512. It
creates `coolant` plus `battery_C01`…`battery_C23` cellZones, verifies exact
non-empty/exclusive/full cell coverage, then maps them to the two solver
regions `coolant` and `batterySolid`. Ten prism layers with a 15 μm initial
estimate are enabled, but the manifest remains `NOT_EXECUTED` until Linux
`surfaceCheck`, `checkMesh`, 23-zone, layer-coverage, zone-volume, port-area and
paired-interface gates pass. `production_mesh_gate.json` aggregates all 30
required reports and no solver launcher will start without its explicit PASS.
Actual y+ remains a cold-flow result gate after meshing.
The separate topoSet cylinder path is an explicit fallback, never a silent
substitute for failed snappy zoning.

For the same solver environment on macOS and Windows, run the generated
`run-production-mesh.sh` or `run-production-mesh.ps1`. Both launch the official
multi-architecture `opencfd/openfoam-default:2512` base image, add only Python 3
for the validation tools, and mount the case at `/case`; no Ubuntu installation
on the host is required. The macOS launcher also finds Docker Desktop directly
when its CLI symlink has not been added to `PATH`.

Once the production mesh gate passes, the generated solver launchers follow
the implementation-design sequence:

```bash
./run-cold-flow-pilot.sh
./run-cold-flow-continue.sh  # only when the cold-flow residual gate needs more iterations
./run-steady-pilot.sh
./run-steady-continue.sh  # only when the steady stability gate needs more iterations
./run-frozen-flow-pilot.sh
./run-full-transient-dt-pilot.sh
./run-full-transient-dt-half-pilot.sh
```

The frozen-flow run uses `deltaT=1 s`, writes compact per-cell telemetry every
1 s, and stores compressed 3D fields every 15 s. Its validator requires all
1,800 telemetry samples, all 121 reconstructed field frames, mass/energy gates,
monotonic heating and agreement with the validated steady CHT temperatures.

After the L0 mesh, cold-flow, steady-CHT and frozen-flow reports all pass,
extract the actual result tables and render the L0 figures with:

```bash
python3 -m pip install -e '.[results]'
PYTHONPATH=src python3 -m mdpi_cfd postprocess-l0 \
  configs/baseline_23cell.yaml --case-dir build/case_validated
PYTHONPATH=src python3 -m mdpi_cfd animate-l0 \
  configs/baseline_23cell.yaml --case-dir build/case_validated
PYTHONPATH=src python3 -m mdpi_cfd build-viewer \
  configs/baseline_23cell.yaml --case-dir build/case_validated \
  --output build/reference_viewer
PYTHONPATH=src python3 -m mdpi_cfd validate-animations \
  --case-dir build/case_validated --viewer-dir build/reference_viewer
PYTHONPATH=src python3 -m mdpi_cfd evaluate-paper-alignment \
  configs/baseline_23cell.yaml --case-dir build/case_validated
```

The postprocessor refuses to run unless all four L0 validation gates pass. It
writes actual CFD `telemetry.csv`, 23-cell temperature histories, residuals,
mesh-volume-normalised energy balance, and Figures 2, 4, 6--14. L0--L3 have now
all passed their production-mesh, cold-flow/y+, and steady-CHT gates. Figure 3
therefore contains actual L0/L3 fields plus four-level convergence trends; the
L2--L3 comparison passes all configured temperature, outlet-temperature, and
pressure-drop mesh-independence tolerances. Figure 3 uses the datum-independent
temperature rise `ΔT = T - Tin` and overlays the current 20 °C L3 1 s rerun at
1,800 s as a direct cross-check against the steady L3 result.

The renderer uses the paper's jet-style palette, comparable camera orientation
and enclosure wireframe. Colour-bar limits remain tied to the actual solver
fields; the paper's published ranges are stored as reference metadata and are
never forced onto a different result. `animate-l0` writes an actual 0--1800 s
temperature-field movie and a separately labelled moving-head visualization
along long inlet-fed streamlines of the fixed frozen-flow velocity field. The
main result scene combines actual cell-surface T(x,t) with those U(x) streamlines and can
be rotated or zoomed with the mouse while it plays. The result viewer provides frame-step,
seek and 1x/5x/10x/30x/60x/180x playback controls. Thermal playback maps video time to
the simulated 0--1,800 s interval; particle playback is explicitly labelled as
motion through a fixed `U(x)` field, not a time-varying velocity solution.
Port-anchored callouts report actual inlet and outlet surface-average
temperature, mass-flow-derived mean velocity, and area-average absolute pressure,
plus the converged pressure drop. Temperature follows the playback time; velocity
and pressure remain the validated frozen `U(x)` and `p(x)` solution.
The viewer's temperature-graph panel exposes five 0--1,800 s outputs at the
native 1 s cadence and can export the current chart as PNG or CSV, or all
metrics as one CSV. Outlet temperature is the `phi`-weighted outlet average;
battery maximum is the maximum battery-solid mesh-cell temperature across the
23 cell zones; surface average is weighted by each interface patch area;
internal temperature is weighted by each battery cell-zone volume. The per-cell
view contains C01--C23 and can switch between internal volume average, surface
area average, and cell-zone mesh maximum.
The current interactive Figure 1 uses the dedicated L3 one-second thermal run.
Its raw initial and inlet temperature is 293.15 K, so 20.000 degC is obtained
only by subtracting 273.15; there is no display-only datum correction. Five XY
and five XZ planes, battery near-surface samples, 23 cell averages, and
inlet/outlet temperatures are stored for every integer second from 0 through
1,800 s.
Section and battery colours use the containing OpenFOAM cell value. Invalid
probe locations remain invalid: no spatial or temporal interpolation and no
arbitrary padding is applied.
The generated `build/reference_viewer` directory is also portable: keep the
whole directory together and double-click `index.html` on macOS or Windows.
For `file://` use, each one-second direct-cell dataset has an on-demand,
gzip-packed JavaScript sidecar in `data/`, so Figure 13 and the other thermal
modes do not depend on a localhost web server. A current Chrome, Edge, or Safari
browser is required; copying only `index.html` omits the model and field data.
Interactive playback uses simulation time: 1x advances one simulation second
per real second, so a complete 1,800 s pass takes 30 minutes. The 5x, 10x, 30x,
60x and 180x choices reduce that to 6 minutes, 3 minutes, 1 minute, 30 seconds
and 10 seconds while retaining the exact integer-second samples used at the
displayed times. Figure 1 exposes a one-second seek increment backed by 1,801
authoritative samples. Heavy full 3D fields are retained at 0 and 1,800 s;
compact direct-cell samples provide the interactive history without duplicating
1,801 full L3 volumes.
The interactive cylinders use direct `batterySolid` containing-cell
temperatures on a 32-angle by 16-height lattice plus cap samples, rather than a
single average colour per cell. Vertical XZ temperature and velocity modes provide five independently
selectable cuts at Y=15%, 32.5%, 50%, 67.5% and 85%. A shared legend keeps
colours comparable when several translucent cuts are displayed together.
The five XY grids at Z=15%, 32.5%, 50%, 67.5% and 85% are each 114x121 at
1 mm spacing; each XZ grid is 114x55 at about 1 x 1.49 mm spacing. Each
coloured raster cell is one direct probe sample, not
a bilinear subdivision. Figure 11 separately draws 54,297 actual L3
cell-centred cut triangles, leaving only the physical battery holes.
`validate-animations` checks codec metadata, duration/frame count, changed
pixels at start/middle/end and the viewer controls, and writes QA contact sheets.
`evaluate-paper-alignment` records the quantitative Figure 3 and Figure 8--14
comparison without forcing paper colour limits onto computed fields. Numerical
mesh independence is complete, but publication authorization remains false
while geometry assumption A-04 and the unpublished time-varying heat-source
inputs remain unresolved; the velocity movie is also truthfully limited to
particle motion through the validated frozen `U(x)` field.

Use the matching `.ps1` files on Windows. Every solver mode runs in its own
`runs/` subdirectory. The S0 profile disables the battery source; S1 and the
thermal profiles use `16,325 W/m³`. T1 copies only the converged cold-flow
`U/p/p_rgh/k/omega/nut/alphat/phi`, resets coolant and solid temperatures to
293 K, and uses the v2512 `PIMPLE { frozenFlow yes; }` control. T2 uses
`frozenFlow no`, `maxCo 5`, `nOuterCorrectors 3`, and paired maximum time steps
of 0.1/0.05 s. These commands remain pilots until convergence, balance, layer,
y+ and mesh-independence gates pass.

Change approved simulation inputs without editing the baseline file:

```bash
PYTHONPATH=src python3 -m mdpi_cfd validate-config \
  configs/baseline_23cell.yaml \
  --set 'boundary.inlet_velocity=0.6 m/s' \
  --set 'fluid.dynamic_viscosity=0.001 Pa*s'
```

Use a reviewed variant or a dynamic heat-source example with an overlay:

```bash
PYTHONPATH=src python3 -m mdpi_cfd generate-heat-source \
  configs/baseline_23cell.yaml \
  --overlay configs/electrical_joule_example.yaml \
  --output build/electrical_heat
```

Every overlay and command-line override is included in the resolved-input
SHA-256 provenance. Final-design dimensions remain enforced: attempts to set
either port to 24 mm are rejected.

OpenFOAM and optional GUI/VTK dependencies are checked by `doctor`. Missing
external solvers never cause synthetic CFD results to be generated.
