"""Explicit numerical trials for freshly generated steady CFD cases."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

PROFILES = ("pressure_limited_0p5",)


def validate_profile(profile: str | None) -> None:
    if profile is not None and profile not in PROFILES:
        raise ValueError(f"Unknown steady_numerics_profile: {profile}")


def apply_profile(case: Path, profile: str | None) -> None:
    """Limit only the pressure Laplacian in S0/S1; keep other equations intact."""
    validate_profile(profile)
    if profile is None:
        return
    if (case / "runs").exists() and any((case / "runs").iterdir()):
        raise ValueError("Numerical profiles may only be applied before a case has run")
    original = "laplacianSchemes\n{\n    default Gauss linear corrected;\n}"
    replacement = original[:-1] + "    laplacian(rhorAUf,p_rgh) Gauss linear limited 0.5;\n}"
    paths = [case / "system/coolant/fvSchemes"] + [
        case / "profiles" / stage / "system/coolant/fvSchemes"
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
        "operator": "laplacian(rhorAUf,p_rgh)",
        "scheme": "Gauss linear limited 0.5",
        "acceptance_limits_changed": False,
        "physical_inputs_changed": False,
    }, indent=2) + "\n", encoding="utf-8", newline="\n")
