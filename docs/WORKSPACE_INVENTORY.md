# Workspace inventory

This file records which top-level files are source inputs, reproducibility
records, current results, or disposable generated output. It is intended to
prevent a future cleanup from deleting the validated CFD results.

## Keep

| Path | Purpose |
|---|---|
| `.venv/` | Local Python runtime and installed project dependencies. Rebuildable, but required for immediate offline execution. |
| `configs/` | Baseline case plus documented sensitivity and heat-source input variants. |
| `docs/` | Current assumptions, traceability, implementation status and full-transient plan. |
| `document/` | Original PDF/PPT reference material used to resolve geometry and paper conditions. Git-ignored because of binary size, but not disposable. |
| `examples/` | Example current, volumetric-heat and resistance time profiles used by configurable heat-source modes. |
| `schemas/` | Machine-readable configuration contract. |
| `src/` | Authoritative Python source code. |
| `tests/` | Regression and validation-contract tests. |
| `.gitignore` | Excludes generated cases, binary documents, environments and caches from Git. |
| `pyproject.toml` | Package metadata, dependencies and CLI entry point. |
| `README.md` | Setup, solver workflow, viewer usage and current-result summary. |
| `build/case_validated/production/L3/runs/steady_cold_flow/` | Passed 1,316,854-cell precision coolant solution. |
| `build/case_validated/production/L3/runs/steady_cht/` | Passed precision steady conjugate-heat-transfer initialization. |
| `build/case_validated/production/L3/runs/frozen_flow_actual_1s_xy5/` | Current precision 0--1,800 s one-second replay with five XY and five XZ cuts. |
| `build/case_validated/production/single/runs/steady_cold_flow/` | Passed 216,577-cell standard coolant solution. |
| `build/case_validated/production/single/runs/steady_cht/` | Passed standard steady CHT initialization. |
| `build/case_validated/production/single/runs/frozen_flow_actual_1s/` | Current standard 0--1,800 s one-second replay. |
| `build/case_validated/production/single/runs/full_transient_actual_1s_courant_aborted/` | Deliberately stopped full-transient pilot retained as performance/Courant evidence, not as a valid result. |
| `build/case_validated/production/*/run-*.sh` and `profiles/` | Reproducible OpenFOAM mesh, steady, frozen-flow and full-transient launch definitions. |
| `build/case_validated/results/` | Current static figure artifacts consumed when rebuilding the viewer. |
| `build/case_validated/cad_source/`, `heat_source/`, manifests and reports | Reproducibility inputs and provenance for the retained production cases. |
| `build/reference_viewer/` | Current portable interactive viewer and its packed one-second datasets. |
| `build/*웹앱*.zip`, `build/*viewer*.zip`, Desktop delivery copies | User-deliverable web-app packages. Never delete during CFD-result cleanup; retain older dated packages when creating a replacement. |

`build/case_validated/` is the authoritative generated case/result root.
`build/reference_viewer/` is the authoritative presentation bundle. Neither is
committed to Git, so they require a separate backup if the workspace is moved.

### Web-app retention rule

Deleting or regenerating CFD meshes and solver results does **not** authorize
deleting a packaged viewer. Web-app directories and ZIP deliveries are a
separate preservation class. A new package must use a dated/condition-labelled
name and must not overwrite or remove the previous package unless the user
explicitly names that package for deletion.

## Removed on 2026-09-16

At the user's request, the old CFD result trees were removed before the clean
Fluent-condition rerun. This included the prior `framework_udf_800k`,
`case_validated`, `ppt_bernardi_800k`, `ppt_bernardi_case` and generated
`reference_viewer` trees. The prior 9 September 16 web-app ZIP was also removed
in that cleanup; this is the reason for the stricter retention rule above.

## Removed on 2026-09-11

| Path | Reason |
|---|---|
| `build/actual_1s_source/` | Temporary generated source staging; duplicated by the authoritative source generator and production case. |
| `build/cad/` | Standalone CAD export duplicated under the validated case and reproducible from source. |
| `build/case/` | Early non-runnable case scaffold. |
| `build/geometry_candidate/` | Early geometry preview candidate. |
| `build/mesh_smoke/` | Obsolete Gmsh smoke mesh; not the production mesh. |
| `build/overlay/` | Temporary drawing-overlay extraction; the resolved inputs and source documents are retained. |
| `build/reference_case/` | Early blocked reference scaffold. |
| `tmp/` | Rendered PDF pages and text extraction scratch data. |

## Disposable or superseded but still present

These paths are Git-ignored and are not required to reproduce the current
results. They remain on disk because their deletion was not included in the
last exact approved scope. Delete them only after separately confirming the
targets.

| Path | Reason |
|---|---|
| `production/L3/runs/frozen_flow_actual_1s/` | Superseded by `frozen_flow_actual_1s_xy5`. |
| failed `steady_cold_flow*failed*` runs | Failed y+ experiments; passed cold-flow results remain. |
| `production/L3/processor0` ... `processor9` | Reconstructible decomposition left by mesh generation; retained solver runs contain their own required meshes. |
| `__pycache__/`, `.DS_Store` | Reconstructible language and operating-system caches. |
