"""Explicit numerical trials for freshly generated steady CFD cases."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

PROFILES = (
    "pressure_limited_0p5",
    "pressure_reltol_zero",
    "pressure_relaxation_0p15",
    "pressure_relaxation_0p5",
    "coupled_relaxation_0p1",
)


def validate_profile(profile: str | None) -> None:
    if profile is not None and profile not in PROFILES:
        raise ValueError(f"Unknown steady_numerics_profile: {profile}")


def apply_profile(case: Path, profile: str | None) -> None:
    """Apply one explicit pressure-only trial in S0/S1 before any solver runs."""
    validate_profile(profile)
    if profile is None:
        return
    if (case / "runs").exists() and any((case / "runs").iterdir()):
        raise ValueError("Numerical profiles may only be applied before a case has run")
    if profile == "coupled_relaxation_0p1":
        solution_paths = [case / "system/coolant/fvSolution"] + [
            case / "profiles" / stage / "system/coolant/fvSolution"
            for stage in ("steady_cold_flow", "steady")
        ]
        scheme_paths = [case / "system/coolant/fvSchemes"] + [
            case / "profiles" / stage / "system/coolant/fvSchemes"
            for stage in ("steady_cold_flow", "steady")
        ]
        pressure_solver = ("    p_rgh\n    {\n        solver GAMG;\n        tolerance 1e-8;\n"
                           "        relTol 0.05;\n        smoother GaussSeidel;\n    }")
        pressure_solver_stable = pressure_solver.replace("relTol 0.05;", "relTol 0;")
        relaxation_tokens = {
            "        p_rgh 0.3;": "        p_rgh 0.1;",
            "        U 0.15;": "        U 0.05;",
            "        k 0.15;": "        k 0.05;",
            "        omega 0.15;": "        omega 0.05;",
        }
        laplacian = "laplacianSchemes\n{\n    default Gauss linear corrected;\n}"
        laplacian_stable = (laplacian[:-1]
                            + "    laplacian(rhorAUf,p_rgh) Gauss linear limited 0.5;\n}")
        edits = []
        for path in solution_paths:
            before = path.read_text(encoding="utf-8")
            h_tokens = [token for token in ("        h 0.2;", "        h 0.5;")
                        if before.count(token) == 1]
            if (before.count(pressure_solver) != 1
                    or any(before.count(token) != 1 for token in relaxation_tokens)
                    or len(h_tokens) != 1):
                raise ValueError(f"Unexpected fresh coupled solution settings: {path}")
            after = before.replace(pressure_solver, pressure_solver_stable)
            for original, replacement in relaxation_tokens.items():
                after = after.replace(original, replacement)
            after = after.replace(h_tokens[0], "        h 0.05;")
            edits.append((path, before, after))
        for path in scheme_paths:
            before = path.read_text(encoding="utf-8")
            if before.count(laplacian) != 1:
                raise ValueError(f"Unexpected fresh coupled pressure scheme: {path}")
            edits.append((path, before, before.replace(laplacian, laplacian_stable)))
        changes = []
        for path, before, after in edits:
            path.write_text(after, encoding="utf-8", newline="\n")
            changes.append({"path": str(path.relative_to(case)),
                            "before_sha256": hashlib.sha256(before.encode()).hexdigest(),
                            "after_sha256": hashlib.sha256(after.encode()).hexdigest()})
        report = case / "reports/steady_numerics.json"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps({
            "profile": profile,
            "changes": changes,
            "pressure_relaxation": 0.1,
            "equation_relaxation": {"U": 0.05, "h": 0.05, "k": 0.05, "omega": 0.05},
            "pressure_relTol": 0,
            "pressure_laplacian": "Gauss linear limited 0.5",
            "evidence": "local pressure/velocity runaway after initially stable SIMPLE iterations",
            "acceptance_limits_changed": False,
            "physical_inputs_changed": False,
        }, indent=2) + "\n", encoding="utf-8", newline="\n")
        return
    if profile == "pressure_limited_0p5":
        filename = "fvSchemes"
        original = "laplacianSchemes\n{\n    default Gauss linear corrected;\n}"
        replacement = original[:-1] + "    laplacian(rhorAUf,p_rgh) Gauss linear limited 0.5;\n}"
        settings = {"operator": "laplacian(rhorAUf,p_rgh)", "scheme": "Gauss linear limited 0.5"}
    elif profile == "pressure_reltol_zero":
        filename = "fvSolution"
        original = ("    p_rgh\n    {\n        solver GAMG;\n        tolerance 1e-8;\n"
                    "        relTol 0.05;\n        smoother GaussSeidel;\n    }")
        replacement = original.replace("relTol 0.05;", "relTol 0;")
        settings = {"field": "p_rgh", "tolerance": 1e-8, "relTol": 0,
                    "previous_relTol": 0.05}
    else:
        filename = "fvSolution"
        original = "    fields\n    {\n        p_rgh 0.3;\n    }"
        relaxation = 0.15 if profile == "pressure_relaxation_0p15" else 0.5
        replacement = original.replace("p_rgh 0.3;", f"p_rgh {relaxation:g};")
        settings = {
            "field": "p_rgh",
            "relaxation": relaxation,
            "previous_relaxation": 0.3,
            "evidence": "pressure residual plateau with stable mass balance and pressure drop",
        }
    paths = [case / "system/coolant" / filename] + [
        case / "profiles" / stage / "system/coolant" / filename
        for stage in ("steady_cold_flow", "steady")
    ]
    edits = []
    for path in paths:
        before = path.read_text(encoding="utf-8")
        if before.count(original) != 1:
            raise ValueError(f"Unexpected fresh pressure scheme: {path}")
        edits.append((path, before, before.replace(original, replacement)))
    changes = []
    for path, before, after in edits:
        path.write_text(after, encoding="utf-8", newline="\n")
        changes.append({"path": str(path.relative_to(case)),
                        "before_sha256": hashlib.sha256(before.encode()).hexdigest(),
                        "after_sha256": hashlib.sha256(after.encode()).hexdigest()})
    report = case / "reports/steady_numerics.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps({
        "profile": profile, "changes": changes,
        **settings,
        "acceptance_limits_changed": False,
        "physical_inputs_changed": False,
    }, indent=2) + "\n", encoding="utf-8", newline="\n")
