"""Explicit numerical trials for freshly generated steady CFD cases."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

PROFILES = (
    "pressure_limited_0p5",
    "pressure_reltol_zero",
    "pressure_relaxation_0p15",
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
        replacement = original.replace("p_rgh 0.3;", "p_rgh 0.15;")
        settings = {
            "field": "p_rgh",
            "relaxation": 0.15,
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
