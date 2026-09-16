"""Publish a validated adaptive trial separately from the preserved baseline."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .workflow_publish import publish_completed_run


def _linked_path(link: Path, target: Path) -> None:
    """Create an idempotent local link without replacing unexpected user data."""

    target = target.resolve()
    if not target.exists():
        raise FileNotFoundError(target)
    if link.is_symlink():
        if link.resolve() != target:
            raise ValueError(f"existing link has a different target: {link}")
        return
    if link.exists():
        raise FileExistsError(link)
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(os.path.relpath(target, link.parent))


def publish_adaptive_trial(
    trial_dir: str | Path,
    reference_case_dir: str | Path,
    review_case_dir: str | Path,
    viewer_dir: str | Path,
) -> dict[str, object]:
    trial = Path(trial_dir).resolve()
    reference = Path(reference_case_dir).resolve()
    review = Path(review_case_dir).resolve()
    viewer = Path(viewer_dir).resolve()
    directories = (trial, reference, review, viewer)
    if len(set(directories)) != 4 or any(
        left.is_relative_to(right) or right.is_relative_to(left)
        for index, left in enumerate(directories)
        for right in directories[index + 1 :]
    ):
        raise ValueError("adaptive trial, baseline, review case and viewer must be separate")
    validation_path = trial / "reports/solver/adaptive_trial_validation.json"
    validation = json.loads(validation_path.read_text(encoding="utf-8"))
    if (
        validation.get("status") != "ADAPTIVE_TRIAL_VALIDATION_PASSED"
        or Path(str(validation.get("reference", ""))).resolve() != reference
        or validation.get("stages", {}).get("reference_identity", {}).get("status")
        != "MATCHED_MESH_AND_DYNAMIC_HEAT_SOURCE"
    ):
        raise ValueError("adaptive validation or baseline mesh/heat-source identity failed")
    audit = json.loads(
        (trial / "reports/solver/energy_coupling_audit.json").read_text(encoding="utf-8")
    )
    if audit.get("status") != "ENERGY_COUPLING_HISTORY_EXTRACTED":
        raise ValueError("adaptive energy-coupling audit is unavailable")
    merged_log = trial / "reports/solver/adaptive_solver_1_to_1800.log"
    links = (
        (review / "runs/frozen_flow_actual_1s", trial),
        (review / "runs/steady_cold_flow", reference / "runs/steady_cold_flow"),
        (review / "runs/steady_cht", reference / "runs/steady_cht"),
        (review / "reports/production_mesh", reference / "reports/production_mesh"),
        (review / "production_mesh_manifest.json", reference / "production_mesh_manifest.json"),
        (review / "constant", trial / "constant"),
        (trial / "logs/solver/frozen_flow_actual_1s.log", merged_log),
    )
    for link, target in links:
        if not target.is_file() and not target.is_dir():
            raise FileNotFoundError(target)
        if link.is_symlink() and link.resolve() != target.resolve():
            raise ValueError(f"existing link has a different target: {link}")
        if link.exists() and not link.is_symlink():
            raise FileExistsError(link)
    for link, target in links:
        _linked_path(link, target)
    # vtkOpenFOAMReader uses this marker to locate the validated trial fields.
    (trial / "frozen_flow_actual_1s.foam").touch(exist_ok=True)
    result = publish_completed_run(review, viewer)
    result["execution_variant"] = "adaptive_energy_coupling_trial"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trial-dir", type=Path, required=True)
    parser.add_argument("--reference-case-dir", type=Path, required=True)
    parser.add_argument("--review-case-dir", type=Path, required=True)
    parser.add_argument("--viewer-dir", type=Path, required=True)
    args = parser.parse_args()
    result = publish_adaptive_trial(
        args.trial_dir, args.reference_case_dir, args.review_case_dir, args.viewer_dir
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
