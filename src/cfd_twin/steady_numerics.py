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
    "coupled_pressure_relaxation_0p05",
    "coupled_limited_reltol_0p05",
    "coupled_stable_reltol_0p005",
    "coupled_equation_relaxation_0p025",
    "coupled_equation_relaxation_0p005",
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
    if profile in (
        "coupled_relaxation_0p1",
        "coupled_pressure_relaxation_0p05",
        "coupled_limited_reltol_0p05",
        "coupled_stable_reltol_0p005",
        "coupled_equation_relaxation_0p025",
        "coupled_equation_relaxation_0p005",
    ):
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
        pressure_relative_tolerance = (
            0.05
            if profile == "coupled_limited_reltol_0p05"
            else 0.005
            if profile == "coupled_stable_reltol_0p005"
            else 0.0
        )
        pressure_solver_stable = (
            pressure_solver.replace(
                "relTol 0.05;", f"relTol {pressure_relative_tolerance:g};"
            )
            if pressure_relative_tolerance > 0
            else pressure_solver.replace("relTol 0.05;", "relTol 0;")
        )
        pressure_relaxation = (
            0.1
            if profile == "coupled_relaxation_0p1"
            else 0.01
            if profile in (
                "coupled_equation_relaxation_0p005",
                "coupled_stable_reltol_0p005",
            )
            else 0.05
        )
        equation_relaxation = (
            0.005
            if profile in (
                "coupled_equation_relaxation_0p005",
                "coupled_stable_reltol_0p005",
            )
            else 0.025
            if profile == "coupled_equation_relaxation_0p025"
            else 0.05
        )
        relaxation_tokens = {
            "        p_rgh 0.3;": f"        p_rgh {pressure_relaxation:g};",
            "        U 0.15;": f"        U {equation_relaxation:g};",
            "        k 0.15;": f"        k {equation_relaxation:g};",
            "        omega 0.15;": f"        omega {equation_relaxation:g};",
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
            after = after.replace(
                h_tokens[0], f"        h {equation_relaxation:g};"
            )
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
            "pressure_relaxation": pressure_relaxation,
            "equation_relaxation": {
                field: equation_relaxation for field in ("U", "h", "k", "omega")
            },
            "pressure_relTol": pressure_relative_tolerance,
            "pressure_laplacian": "Gauss linear limited 0.5",
            "evidence": (
                "local pressure/velocity/enthalpy runaway after initially stable "
                "SIMPLE iterations; the selected coupled-relaxation follow-up "
                "preserves pressure controls, acceptance limits, and physical inputs. "
                "The relTol=0.05 variant retains the original pressure stopping rule "
                "after measured relTol=0 trials reached 1000 GAMG iterations per solve."
            ),
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
