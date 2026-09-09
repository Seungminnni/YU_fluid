# CFD implementation status

This file records measured implementation evidence.  It does not authorize
publication results by itself; the generated JSON gates remain authoritative.

## 2026-09-06 L0 cold-flow diagnostic (superseded)

- Geometry: both ports 19 mm, 23 cells, PPT-based pitch/notch profile.
- Mesh: 339,080 cells; the 30-report production-mesh aggregate gate passed.
- Solver: OpenCFD OpenFOAM v2512, `chtMultiRegionSimpleFoam`, SST k-omega,
  first-order upwind, 10 MPI ranks.
- The final 200-iteration inlet pressure-drop range was 259.988--260.187 Pa
  (0.076% of the mean).
- The maximum inlet/outlet mass imbalance at iterations 1800, 1900 and 2000
  was 0.000063%, below the 0.1% gate.
- Final reconstructed fields were finite; `k_min=3.04e-15` and
  `omega_min=0.115`, so no negative turbulence field remained after model
  bounding.
- The battery-wall area-weighted y+ result was mean 0.395, p95 1.198, p99
  2.809 and maximum 15.072.  Only 92.954% of battery area had y+ below 1,
  which fails the design requirement of at least 95%.

Decision: this diagnostic run is not a final result and its generated field
directory is removed before the next run.  The production first-layer target
is reduced from 20 micrometres to 14 micrometres.  The final value was selected
after the 15-micrometre standard pilot measured y+ p95 = 1.022 and 94.861% of
the battery area below one.  The new mesh must still pass all geometry,
zone, interface, layer and `checkMesh` gates, followed by an actual y+
measurement; this estimate is not itself a pass.

## Solver corrections retained

- Use calculated absolute `p`, `fixedFluxPressure` for non-outlet `p_rgh`, and
  fixed outlet `p_rgh`, matching the installed v2512 CHT tutorials.
- Initialize the coolant velocity with `potentialFoam` but do not write its
  incompressible `phi`; the CHT solver creates the required mass-flux `phi`.
- Use conservative SIMPLE relaxation (`U/k/omega=0.3`, `p_rgh=0.7`).
- Use reverse-flow-safe `inletOutlet` conditions at the outlet for velocity,
  temperature, k and omega.
- Cold-flow profiles omit 115 battery-temperature monitors while retaining
  mass-flow, pressure, field-bound and y+ diagnostics.
- Docker defaults to 10 MPI ranks.  The measured solver utilization was about
  1007--1011% CPU on the 10-CPU Docker allocation.

## 2026-09-06 clean L0 production mesh and cold-flow result

- The clean 15-micrometre-first-layer mesh contains 341,804 cells and passed
  all 30 production-mesh reports, including both-region `checkMesh`, exact 23
  battery cellZones, complete zone coverage and 23 paired interfaces.
- Mesh-integrated inlet area is `2.8280219508e-4 m2`; mesh-integrated battery
  volume is `4.4133616354e-4 m3`.
- Minimum battery prism-layer coverage is 94.134%; the minimum reported wanted
  thickness fraction is 69.2%.
- The clean cold-flow compound gate passed.  Its final 200-iteration mean
  pressure drop is 259.874 Pa and maximum mass imbalance is 0.000175%.
- Battery-wall y+ is area-weighted mean 0.300, p95 0.896, p99 1.956 and maximum
  15.020.  The area fraction below y+=1 is 96.089%, above the 95% gate.

## 2026-09-06 L0 steady CHT pilot

- The actual mesh-integrated heat input is 7.204813 W (`16325 W/m3` multiplied
  by the mesh battery volume), so the energy gate does not use ideal CAD volume.
- The regular profile completed 2,000 iterations.  Extending it unchanged to
  3,000 iterations left a 0.001287 K peak-temperature oscillation, above the
  strict 0.001 K stability gate; all other checks passed.
- The gate was not weakened.  Continuation used `p_rgh=0.5`, `U/k/omega=0.2`,
  coolant `h=0.3` and solid `h=0.5`, and then ran to iteration 4,000.
- The final compound gate passed: battery-average span 0.000153 K,
  battery-maximum span 0.000758 K, mean energy imbalance 0.450%, maximum mass
  imbalance 0.0000985%, and mean pressure drop 261.689 Pa over the final 200
  iterations.  Final monitored battery average/maximum temperatures are
  293.091244 K and 293.173221 K.
- These are L0 pilot-validation values.  They do not authorize publication
  results; frozen-flow thermal time history and L1--L3 mesh independence remain.

## Transient execution policy

- Frozen-flow thermal production uses 1 s physical time steps from 0 to 1800 s.
- Compact per-cell and boundary telemetry plus both regional temperature fields
  are written every 1 s. The constant-property, zero-gravity model reuses its
  validated static flow field; complete unchanged flow fields are written at
  the final time instead of duplicated 1,800 times.
- Full-transient 0.1 s and 0.05 s cases remain coarse validation-only runs and
  are not publication or GUI figure sources.

## 2026-09-10 standard 216,577-cell production result

- The executed mesh contains 216,577 cells and 252,907 points: 159,107 coolant
  cells and 57,470 battery-solid cells. It is 85.426% hexahedral and passes the
  count, topology, zone, interface, volume and coarse-mesh prism-layer gates.
- The final 14-micrometre cold-flow run passed after 2,000 iterations. Battery
  wall y+ is area-weighted mean 0.373565, p95 0.952719 and 95.3196% of the area
  is below one. Mean pressure drop is 260.412879 Pa and maximum mass imbalance
  is 2.803965e-8.
- The 2,000-iteration steady CHT gate passed: outlet T=293.165240 K, battery
  Tavg=293.256929 K, Tmax=293.341415 K and mean energy imbalance 0.236343%.
- The exact one-way transient CHT run completed physical times 1 through 1,800
  s with the passed fixed flow field. All ten native OpenFOAM processor
  decompositions contain both regional T fields at all 1,801 states including
  time zero; the final complete field is reconstructed for whole-mesh use.
- The transient compound gate passed. At 1,800 s, outlet T=293.165261 K,
  battery Tavg=293.256350 K and mesh Tmax=293.343303 K (C05). Final-window mean
  energy imbalance is 0.018936%; maximum mass imbalance is 1.642817e-8. Tavg
  and Tmax differ from the passed steady solution by -0.000579 K and
  +0.001887 K.
- Eleven browser datasets cover five XY temperature sections, five XZ sections
  and direct battery-surface samples at every integer second. Their independent
  pack/re-read validation passed with no temporal interpolation, spatial
  interpolation or padding; maximum quantization error is 1.421736e-6 K.
- `build/reference_viewer` now keeps the precision and standard datasets under
  separate mesh selectors and never substitutes one mesh's field for the other.

## 2026-09-06 L0 frozen-flow thermal pilot

- The 10-rank `chtMultiRegionFoam` run completed 1,800 one-second physical time
  steps with `PIMPLE.frozenFlow yes`; the log contains energy solves but no
  repeated momentum or pressure solves.
- All 1,800 compact telemetry samples and all 121 required 15-second thermal
  frames were produced and reconstructed for both regions.  The first executed
  pilot also contains y+-only 10-second directories; the source is corrected so
  future frozen-flow runs write y+ only at 1,800 s because the flow is frozen.
- The frozen-flow compound gate passed.  At 1,800 s, battery average/maximum
  temperatures are 293.091611 K and 293.173826 K.  Differences from the
  validated steady CHT values are 0.000368 K and 0.000604 K, respectively.
- Final-window mean/maximum energy imbalance is 0.0309%; maximum mass imbalance
  is 0.0000459%.  Battery-average temperature is monotonic within the numerical
  tolerance, required fields are finite, and final turbulence fields are
  non-negative.
- This authorizes L0 figure-generation development only.  Publication results
  still require L1--L3 mesh independence and the remaining paper-result gates.

## 2026-09-07 actual L0 result extraction and visualization

- A gate-aware postprocessor now refuses input unless production mesh,
  cold-flow, steady CHT and frozen-flow validation reports all have their exact
  PASS statuses.
- It read the reconstructed OpenFOAM fields rather than configuration
  arithmetic and generated Figures 2, 4 and 6--14. Figure 3 remains explicitly
  `AWAITING_MESH_INDEPENDENCE`; Figure 5 remains excluded by user decision.
- Result tables now contain 1,800 telemetry rows, 41,400 C01--C23 surface
  temperature rows, 40,000 steady residual rows and 120 field-frame energy
  balance rows. No synthetic values are inserted. Surface heat flux is empty
  and labelled `HEAT_FLUX_NOT_COMPUTED` because that field was not requested
  by the completed solver run.
- The final volume-weighted coolant speed is 0.0630849 m/s. The 1,800 s energy
  row uses the direct `polyMesh` battery volume and gives 7.204813 W generated,
  7.202587 W removed and 0.03089% imbalance. VTK per-cell volume weights are
  normalised to the authoritative direct-mesh fluid and solid volume totals.
- `build/reference_viewer/index.html` now loads the real PNG artifacts and is
  labelled `L0 CFD PASS · 논문 최종 아님`. It still exposes the interactive
  six-direction geometry view.
- Paper-facing plots now use the paper-like jet palette, camera orientation,
  transparent enclosure/wireframe treatment and actual CFD colour-bar limits.
  The published colour ranges are reference metadata only, so visual similarity
  cannot silently replace numerical agreement.
- Figure 6 includes the actual 0--1800 s temperature-field MP4. Figure 9 includes
  a fixed-velocity-field particle MP4 that is explicitly not labelled as a
  transient velocity solution.
- Publication authorization remains false. The next required calculation is
  L1--L3 mesh independence, followed by final-level figure regeneration.

## 2026-09-07 L1--L3 production calculation and mesh independence

- All four L0--L3 levels now pass the production-mesh, cold-flow/y+, and
  steady-CHT compound gates. Their cell counts are 341,804, 695,327, 1,011,898,
  and 1,316,854, respectively.
- L1 cold flow has a 260.407 Pa final-window mean pressure drop, 0.00000322%
  maximum mass imbalance, and 95.556% of battery area below y+=1. Its steady
  result at iteration 2,000 is Tavg=293.080210 K, Tmax=293.149302 K,
  Tout=293.015234 K, pressure drop 260.413 Pa, and 0.3900% mean energy imbalance.
- L2 cold flow has a 265.728 Pa mean pressure drop, 0.00000233% maximum mass
  imbalance, and 95.670% of battery area below y+=1. The unchanged steady case
  required continuation to iteration 4,000 and then passed with
  Tavg=293.080131 K, Tmax=293.146452 K, Tout=293.015174 K, pressure drop
  265.765 Pa, and 0.7051% mean energy imbalance.
- The first L3 15-micrometre mesh passed mesh quality but measured only 94.919%
  of battery area below y+=1, so it was rejected. A 13-micrometre trial was also
  rejected because maximum non-orthogonality was 75.001411 degrees, just above
  the 75-degree limit. Both failed runs and reports are preserved for audit.
- The adopted L3 14-micrometre mesh has 1,316,854 cells and passes with maximum
  non-orthogonality 74.999901 degrees and maximum skewness 9.516337. Its cold
  flow has a 262.362 Pa mean pressure drop, 0.00000402% maximum mass imbalance,
  and 95.638% of battery area below y+=1 (mean 0.285, p95 0.928, p99 2.347).
  Its steady calculation required continuation to iteration 3,000 and passed
  with Tavg=293.076386 K, Tmax=293.134931 K, Tout=293.015179 K, pressure drop
  262.657 Pa, and 0.7139% mean energy imbalance.
- The final L2--L3 comparison passes every configured independence tolerance:
  absolute Tmax difference 0.011521 K <= 0.02 K, maximum of the 23 cell-average
  temperature differences 0.017029 K <= 0.02 K, Tout difference 0.00000469 K
  <= 0.01 K, and pressure-drop difference 1.183% <= 2%. Classical observed
  order is not reported because temperature is not bracketed and pressure drop
  is non-monotonic; the declared decision rule is the direct fine-pair tolerance.
- Figure 3 was regenerated from actual L0/L3 steady XZ fields using the same
  plane, camera, and colour scale, with all four grid trends shown below them.
  Temperature is now reported as the measured rise `ΔT = T - Tin`, so the
  earlier 293.00 K steady-run datum cannot be mistaken for a sub-20 °C inlet.
  Hollow star markers cross-check the current 293.15 K L3 one-second run at
  1,800 s against the steady L3 temperature-rise result.
  The authoritative report status is `MESH_INDEPENDENCE_PASSED`.

## 2026-09-07 final animation and paper-alignment audit

- The final thermal MP4 is H.264, 850x750, 121 frames at 12 fps and maps actual
  OpenFOAM T(x,t) frames every 15 s onto 0--1,800 s. The long-streamline flow
  MP4 is H.264, 1018x730, 90 frames at 20 fps and is explicitly labelled as
  moving comet heads through fixed U(x), not a transient velocity solution.
- Automated start/middle/end changed-pixel checks pass for both movies. The
  dependency-free viewer provides native controls, seek, previous/next frame,
  0.5x/1x/2x/4x speed selection, thermal simulation-time mapping, and the fixed
  velocity truth label. QA contact sheets were generated and visually reviewed.
- A subsequent visual audit against the source Figure 9/10 found that the first
  streamline renderer had interpreted metre-valued integration steps as cell-
  length fractions, truncating paths to roughly 15--25 mm. The corrected tracer
  uses physical `LENGTH_UNIT`, a deterministic inlet-disk seed, and long forward
  paths through the pack. Static Figures 9/10 now use the paper-oriented camera,
  dense inlet-fed streamlines, and cell/port wireframes. The main interactive
  scene shows actual cell T(t) over 0--1,800 s together with moving heads along
  fixed U(x), and remains mouse-rotatable and zoomable during playback.
- The actual 1,800 s global velocity maximum is 0.5853 m/s versus the digitized
  paper Figure 10 reference 0.565 m/s (+3.6%). XY and XZ section p99.5 speeds
  are 0.1523 and 0.1105 m/s, below the corresponding digitized upper references
  0.218 and 0.166 m/s, so the sectional distributions are not exact matches.
- Solid temperature spans 19.8626--20.0238 degC, while its rise above the
  configured 293 K initial state is 0.0126--0.1738 K. The rise scale is broadly
  comparable with the paper, but the absolute temperature colourbar is about
  0.13--0.15 K lower because this configuration uses 293 K rather than the
  paper figure's approximately 293.15 K datum. Computed Re=6,773.5 is 22.9%
  below the paper's 8,782 under the implemented Table 4 properties.
- Overall, the actual fields reproduce the paper-oriented composition and
  qualitative thermal/flow pattern, with broadly comparable temperature rise
  and global peak speed. Exact paper agreement is not claimed. Publication
  authorization stays false because port/heat assignment assumption A-04,
  unpublished time-varying heat-source inputs, and transient U(x,t) remain
  unavailable; this does not invalidate the completed numerical gate results.

## 2026-09-07 Figure 1 multi-mode extension

- Figure 1 now provides seven radio-selectable modes covering the physical
  content of Figures 4, 6, 7, 8, 9, 10, 11, 12, 13 and 14 in one mouse-orbitable
  3D scene. The modes share one 0--1,800 s playback control.
- Actual OpenFOAM T(x,t) was sampled every 15 s on 37x41 XY and 37x27 XZ
  centre-plane grids. Velocity magnitude and direction markers use the
  validated 1,800 s frozen field. Each section mode receives a useful default
  camera, after which drag, wheel and the six orthographic buttons remain active.
- Figures 2 and 3 stay as static artifacts because mesh topology and the
  L0--L3 independence comparison do not represent a time-varying field.
- Layer checkboxes independently control cells, flow/vectors, moving heads,
  section colours, sampling-grid lines and the enclosure. The ports are coloured
  from actual transient inlet/outlet temperature telemetry in thermal modes and
  labelled with mass-flow-derived mean port velocity in velocity modes.
- Temperature legends in the viewer, static Figures 6/8/9/13/14 and the
  thermal animation now use 19 labelled major ticks with minor subdivisions.
  Paper-reference endpoints are printed below the actual CFD scale and are not
  used to clip or recolour the computed field. Paper-oriented static temperature
  figures place the legend to the left of the domain.
- Figure 1 anchors matching inlet and outlet callouts to the projected port
  locations. Each callout reports actual time-dependent surface temperature,
  mass-flow-derived mean velocity, and area-averaged absolute pressure. The
  converged frozen-flow pressure drop is reported alongside them; only the
  temperature varies on the 0--1,800 s playback axis because U(x) and p(x) are
  the validated fixed fields.
- At the user's direction, the viewer and regenerated temperature artifacts
  apply a disclosed `+0.150 K` display-only temperature datum correction so the
  293 K baseline is presented from exactly 20.000 degC. The raw solver fields
  and validation records remain unchanged. The offset is applied consistently
  to cells, inlet/outlet values, XY/XZ sections, and temperature legend limits.
- Figure 6 includes a 121-frame, 0--1,800 s animation with the actual coolant
  T(x,t) on the left and the validated fixed U(x) magnitude on the right.
  Temperature uses Gouraud point interpolation rather than triangle-face
  averaging, removing the false coarse/faceted grid appearance. Figure 1's
  optional grid layer is explicitly labelled as a section sampling grid and is
  off by default; Figure 2 remains the actual computational-mesh view.
- Figure 1 and Figure 6 now use a simulation clock rather than the encoded MP4
  duration as playback time. At 1x, one real second advances one simulation
  second and the complete 1,800 s history takes 30 minutes. Selectable 5x, 10x,
  30x, 60x and 180x rates shorten a full pass to 6 minutes, 3 minutes, 1 minute,
  30 seconds and 10 seconds respectively. Frame-step remains 15 simulation
  seconds for Figure 6.
- Figure 1's seek control and displayed clock advance in 1 s increments. The
  solver snapshots are still the authoritative 15 s samples; the existing
  time interpolation for cell surface, port and XY/XZ section temperatures is
  evaluated at every displayed second. The UI explicitly labels this as
  `화면 1 s · CFD 원본 15 s 선형보간` so interpolated seconds are not presented
  as newly solved CFD time steps.
- Figure 1 cell colours now come from the actual OpenFOAM `batterySolid`
  surface field instead of a single surface-average value per cylinder. Every
  15 s frame stores a compact 28-angle by 12-height surface lattice plus cap
  samples; the largest display-sample-to-solver-surface-point distance in the
  validated build is 1.4560 mm. The legend remains on the actual computed
  temperature range and the display-only +0.150 K datum correction is disclosed.
- XZ temperature and velocity modes now expose five independently selectable
  cuts at Y=18.2, 39.3, 60.5, 81.7 and 102.8 mm. `중앙만` restores the original
  centre plane and `5단면` shows every cut translucently in an ISO view. All
  selected planes share one union range so the same colour means the same value.
- Section rendering now bilinearly subdivides every 37x41 XY or 37x27 XZ probe
  quad 3x3 for effective 109x121 and 109x79 display lattices. Showing all five
  XZ cuts uses 2x2 subdivision per source quad to preserve mouse interaction.
  This is display interpolation of actual CFD samples, not a finer solver mesh.
  Battery-cylinder side faces use continuous colour gradients between adjacent
  axial surface samples and nearly invisible face edges, removing the previous
  coarse-band appearance in Figure 1 while keeping the sampled values unchanged.

## 2026-09-07 L3 actual one-second rerun (current Figure 1 source)

- A new frozen-flow thermal run was completed on the adopted 1,316,854-cell L3
  mesh from 0 to 1,800 s with `deltaT=1 s`. Its raw initial and inlet
  temperature is 293.15 K (20.000 degC); the earlier +0.150 K display correction
  is not used by the current interactive Figure 1.
- The compound validation status is `FROZEN_FLOW_VALIDATION_PASSED`. All 1,800
  one-second telemetry samples are present, the solver reached a clean end,
  maximum mass imbalance is `1.26224e-8`, and final energy imbalance is
  `0.0406751%`.
- Final battery average and maximum temperatures are 293.226480 K and
  293.289094 K. The datum-shifted differences from the validated L3 steady
  solution are 0.0000947 K and 0.004163 K, both within the 0.01 K gate.
- The run writes direct containing-cell temperature probes every 1 s using
  OpenFOAM `interpolationScheme cell`: five 114x121 XY planes at Z=15%, 32.5%,
  50%, 67.5% and 85%, five 114x55 XZ planes, and a 32-angle by 16-height
  near-surface lattice plus cap samples for all 23 cells. Invalid probe points
  remain invalid and are neither filled nor padded.
- Browser storage uses one shared linear uint16 encoding only to reduce I/O.
  Its quantum is `2.03074e-6 K`; an exhaustive source-to-bundle comparison
  passed for all eleven datasets with maximum error `1.01537e-6 K`. No
  spatial or temporal interpolation is performed when rendering a time step.
- Figure 11 uses 54,297 L3 cut triangles with one cell-centred velocity value
  per triangle. Its interior gaps are the 23 physical batteries rather than
  missing display-grid cells.
- Full 3D L3 fields are retained at 0 and 1,800 s. This avoids writing 1,801
  duplicate multi-region volumes while the direct one-second probes retain the
  requested animation data.
