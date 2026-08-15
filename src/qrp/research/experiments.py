"""Immutable experiment registration and basic multiple-testing controls."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

import numpy as np
import pandas as pd

from qrp.versioning import (
    archive_committed_source,
    environment_lock_identity,
    inspect_git_repository,
)


@dataclass(frozen=True)
class ExperimentSpec:
    name: str
    hypothesis: str
    train_start: str
    train_end: str
    validation_start: str
    validation_end: str
    test_start: str
    test_end: str
    random_seed: int
    factor_family: str
    universe_version: str
    execution_artifact_id: str

    def validate(self) -> "ExperimentSpec":
        if not self.name.strip() or not self.hypothesis.strip():
            raise ValueError("experiment name and ex-ante hypothesis are required")
        dates = {
            field: pd.Timestamp(getattr(self, field)).normalize()
            for field in (
                "train_start",
                "train_end",
                "validation_start",
                "validation_end",
                "test_start",
                "test_end",
            )
        }
        if not (
            dates["train_start"] <= dates["train_end"]
            < dates["validation_start"] <= dates["validation_end"]
            < dates["test_start"] <= dates["test_end"]
        ):
            raise ValueError("train, validation and locked test periods must be ordered and disjoint")
        return self


def register_experiment(
    root: Path,
    spec: ExperimentSpec,
    *,
    data_artifacts: Mapping[str, str],
    code_files: Sequence[Path],
    parameters: Mapping[str, Any],
    results: Optional[Mapping[str, Any]] = None,
    repository_root: Optional[Path] = None,
    require_clean_git: bool = True,
) -> Path:
    """Write an immutable manifest binding hypothesis, data, code and outputs."""
    spec.validate()
    if not code_files:
        raise ValueError("at least one critical code file is required")
    repository_start = repository_root or Path(code_files[0])
    git = inspect_git_repository(
        repository_start,
        require_clean=require_clean_git,
    )
    repo_root = Path(git.repository_root)
    environment_lock = environment_lock_identity(repo_root)
    code = []
    for path in sorted(map(Path, code_files)):
        resolved = path.resolve()
        try:
            relative = resolved.relative_to(repo_root)
        except ValueError as error:
            raise ValueError(f"code file is outside repository: {resolved}") from error
        code.append({"path": str(relative), "sha256": _sha256(resolved)})
    with tempfile.TemporaryDirectory() as directory:
        temporary_archive = Path(directory) / "source_bundle.tar"
        source_bundle = archive_committed_source(
            repo_root,
            temporary_archive,
            commit=git.commit,
        )
        source_bundle["path"] = "source_bundle.tar"
        payload: Dict[str, Any] = {
            "schema_version": "research_experiment_v2",
            "spec": asdict(spec),
            "data_artifacts": dict(sorted(data_artifacts.items())),
            "code_identity": git.to_dict(),
            "environment_lock": environment_lock,
            "source_bundle": source_bundle,
            "critical_code": code,
            "parameters": parameters,
            "results": results or {},
            "guardrails": {
                "locked_test_period": True,
                "hypothesis_registered": True,
                "random_seed_frozen": True,
                "data_and_code_hashes_frozen": True,
                "git_commit_bound": True,
                "clean_worktree_required": require_clean_git,
                "environment_lock_frozen": True,
                "committed_source_bundle_frozen": True,
            },
        }
        encoded_identity = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
        experiment_id = hashlib.sha256(encoded_identity).hexdigest()[:20]
        payload["experiment_id"] = experiment_id
        destination = Path(root) / f"experiment_id={experiment_id}"
        destination.mkdir(parents=True, exist_ok=True)
        archive_path = destination / "source_bundle.tar"
        if archive_path.exists():
            if _sha256(archive_path) != source_bundle["sha256"]:
                raise RuntimeError(f"immutable source bundle conflict: {archive_path}")
        else:
            archive_temporary = archive_path.with_name(
                f".{archive_path.name}.{os.getpid()}.tmp"
            )
            shutil.copy2(temporary_archive, archive_temporary)
            os.replace(archive_temporary, archive_path)
        path = destination / "manifest.json"
        encoded = (
            json.dumps(
                payload,
                indent=2,
                sort_keys=True,
                ensure_ascii=False,
                default=str,
            )
            + "\n"
        )
        if path.exists() and path.read_text(encoding="utf-8") != encoded:
            raise RuntimeError(f"immutable experiment conflict: {path}")
        if not path.exists():
            temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            temporary.write_text(encoded, encoding="utf-8")
            os.replace(temporary, path)
        return destination


def benjamini_hochberg(p_values: Sequence[float]) -> np.ndarray:
    """Return false-discovery-rate adjusted q-values for a factor batch."""
    values = np.asarray(p_values, dtype=float)
    if values.ndim != 1 or not np.isfinite(values).all() or ((values < 0) | (values > 1)).any():
        raise ValueError("p_values must be a finite one-dimensional vector in [0, 1]")
    count = len(values)
    if count == 0:
        return values
    order = np.argsort(values)
    ranked = values[order]
    adjusted = ranked * count / np.arange(1, count + 1)
    adjusted = np.minimum.accumulate(adjusted[::-1])[::-1]
    result = np.empty(count)
    result[order] = np.minimum(adjusted, 1.0)
    return result


def walk_forward_splits(
    sessions: Sequence[Any], *, train_size: int, validation_size: int, test_size: int
) -> list[Dict[str, pd.Timestamp]]:
    """Create deterministic expanding-window, non-overlapping test splits."""
    if min(train_size, validation_size, test_size) <= 0:
        raise ValueError("walk-forward window sizes must be positive")
    dates = pd.DatetimeIndex(pd.to_datetime(list(sessions))).normalize().unique().sort_values()
    splits = []
    test_start = train_size + validation_size
    while test_start + test_size <= len(dates):
        splits.append(
            {
                "train_start": dates[0],
                "train_end": dates[test_start - validation_size - 1],
                "validation_start": dates[test_start - validation_size],
                "validation_end": dates[test_start - 1],
                "test_start": dates[test_start],
                "test_end": dates[test_start + test_size - 1],
            }
        )
        test_start += test_size
    return splits


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
