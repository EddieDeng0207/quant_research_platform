"""Content-addressed diagnostics for financial-to-industry PIT identity coverage."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict

import pandas as pd

from qrp.versioning import environment_lock_identity, inspect_git_repository


class IndustryCoverageError(RuntimeError):
    """Raised when PIT coverage inputs cannot be verified."""


def build_industry_coverage_audit(
    fundamental_artifact: Path,
    industry_artifact: Path,
    calendar_path: Path,
    output_root: Path,
    start_date: str,
    end_date: str,
    *,
    minimum_coverage: float = 0.80,
    require_clean_git: bool = False,
) -> Path:
    """Build an immutable daily identity-coverage diagnostic for P0.7 readiness."""
    start = pd.Timestamp(start_date).normalize()
    end = pd.Timestamp(end_date).normalize()
    if start > end:
        raise ValueError("start_date must be on or before end_date")
    if not 0 < minimum_coverage <= 1:
        raise ValueError("minimum_coverage must be in (0, 1]")

    fundamental, fundamental_manifest, fundamental_meta = _load_artifact_output(
        fundamental_artifact, "version_index"
    )
    industry, industry_manifest, industry_meta = _load_artifact_output(
        industry_artifact, "membership"
    )
    _require_columns(fundamental, {"instrument_id", "available_at"}, "fundamental")
    _require_columns(
        industry,
        {"instrument_id", "membership_start", "membership_end"},
        "industry",
    )
    invalid_fundamental_ids = _invalid_namespace_rows(fundamental["instrument_id"])
    invalid_industry_ids = _invalid_namespace_rows(industry["instrument_id"])
    if invalid_fundamental_ids or invalid_industry_ids:
        raise IndustryCoverageError(
            "coverage audit requires a shared CN_EQ instrument namespace; "
            f"fundamental_invalid={invalid_fundamental_ids}, "
            f"industry_invalid={invalid_industry_ids}"
        )

    calendar_file = Path(calendar_path).resolve()
    calendar = pd.read_parquet(calendar_file)
    _require_columns(calendar, {"calendar_date", "is_open"}, "calendar")
    calendar_dates = pd.to_datetime(calendar["calendar_date"], errors="coerce")
    open_dates = pd.DatetimeIndex(
        calendar_dates.loc[
            calendar["is_open"].astype(bool)
            & calendar_dates.dt.normalize().between(start, end)
        ].dt.normalize()
    ).unique().sort_values()
    if open_dates.empty:
        raise IndustryCoverageError("calendar has no open sessions in the audit range")

    first_available = _first_available_dates(fundamental)
    intervals = _industry_intervals(industry, end)
    daily = _daily_coverage(open_dates, first_available, intervals, minimum_coverage)
    fundamental_ids = set(fundamental["instrument_id"].dropna().astype(str))
    industry_ids = set(industry["instrument_id"].dropna().astype(str))
    shared_ids = fundamental_ids & industry_ids

    git = inspect_git_repository(Path(__file__), require_clean=require_clean_git)
    environment = environment_lock_identity(Path(git.repository_root))
    identity = {
        "schema_version": "p08_industry_financial_coverage_v1",
        "diagnostic_scope": "pit_known_financial_universe_vs_effective_industry_intervals",
        "start_date": str(start.date()),
        "end_date": str(end.date()),
        "minimum_coverage": minimum_coverage,
        "fundamental_artifact_id": fundamental_manifest.get("artifact_id"),
        "fundamental_output_sha256": fundamental_meta["sha256"],
        "industry_artifact_id": industry_manifest.get("artifact_id"),
        "industry_output_sha256": industry_meta["sha256"],
        "calendar_sha256": _sha256(calendar_file),
        "implementation_sha256": _sha256(Path(__file__)),
        "git_commit": git.commit,
        "git_tree": git.tree,
        "environment_lock_sha256": environment["sha256"],
    }
    artifact_id = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:20]
    destination = Path(output_root) / f"artifact_id={artifact_id}"
    destination.mkdir(parents=True, exist_ok=True)
    daily_path = destination / "daily_coverage.parquet"
    _write_immutable_parquet(daily, daily_path)

    coverage = daily["fundamental_to_industry_coverage"]
    reverse_coverage = daily["industry_to_fundamental_coverage"]
    summary = {
        "open_sessions": len(daily),
        "static_identity_counts": {
            "fundamental_instruments": len(fundamental_ids),
            "industry_instruments": len(industry_ids),
            "shared_instruments": len(shared_ids),
            "fundamental_only_instruments": len(fundamental_ids - industry_ids),
            "industry_only_instruments": len(industry_ids - fundamental_ids),
            "static_fundamental_to_industry_coverage": _ratio(
                len(shared_ids), len(fundamental_ids)
            ),
        },
        "daily_fundamental_to_industry_coverage": _distribution(coverage),
        "daily_industry_to_fundamental_coverage": _distribution(reverse_coverage),
        "sessions_below_minimum_coverage": int((coverage < minimum_coverage).sum()),
        "conservative_stress_test_passed": int(
            (coverage < minimum_coverage).sum()
        )
        == 0,
        "lowest_coverage_sessions": daily.nsmallest(
            10, "fundamental_to_industry_coverage"
        ).to_dict("records"),
        "formal_p07_factor_panel_coverage": "not_measured",
        "interpretation": (
            "This is a conservative pre-factor identity diagnostic. The actual P0.7 "
            "coverage denominator must be recomputed from each dated eligible factor panel."
        ),
    }
    manifest = {
        "artifact_id": artifact_id,
        "schema_version": identity["schema_version"],
        "identity": identity,
        "inputs": {
            "fundamental_artifact": str(Path(fundamental_artifact).resolve()),
            "industry_artifact": str(Path(industry_artifact).resolve()),
            "calendar": str(calendar_file),
        },
        "outputs": {
            "daily_coverage": {
                "path": daily_path.name,
                "sha256": _sha256(daily_path),
                "rows": len(daily),
            }
        },
        "summary": summary,
        "quality": {
            "promotion_passed": True,
            "hard_failures": {
                "invalid_fundamental_namespace_rows": invalid_fundamental_ids,
                "invalid_industry_namespace_rows": invalid_industry_ids,
            },
        },
        "guardrails": {
            "artifact_hashes_verified": True,
            "shared_cn_equity_namespace_required": True,
            "point_in_time_financial_availability_used": True,
            "effective_industry_intervals_used": True,
            "not_substituted_for_formal_factor_panel_coverage": True,
            "conservative_coverage_is_diagnostic_not_promotion_gate": True,
        },
    }
    _write_immutable_json(manifest, destination / "manifest.json")
    return destination


def _load_artifact_output(
    artifact: Path, output_name: str
) -> tuple[pd.DataFrame, Dict[str, Any], Dict[str, Any]]:
    root = Path(artifact).resolve()
    manifest_path = root / "manifest.json"
    if not manifest_path.exists():
        raise IndustryCoverageError(f"artifact manifest missing: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not manifest.get("quality", {}).get("promotion_passed", False):
        raise IndustryCoverageError(f"artifact is not promoted: {root}")
    metadata = manifest.get("outputs", {}).get(output_name)
    if metadata is None:
        raise IndustryCoverageError(f"artifact output {output_name!r} missing: {root}")
    path = root / metadata["path"]
    if not path.exists() or _sha256(path) != metadata.get("sha256"):
        raise IndustryCoverageError(f"artifact output hash mismatch: {path}")
    frame = pd.read_parquet(path)
    if len(frame) != int(metadata.get("rows", len(frame))):
        raise IndustryCoverageError(f"artifact output row count mismatch: {path}")
    return frame, manifest, metadata


def _first_available_dates(frame: pd.DataFrame) -> pd.Series:
    available = pd.to_datetime(frame["available_at"], utc=True, errors="coerce")
    if available.isna().any():
        raise IndustryCoverageError("fundamental index contains invalid available_at")
    local_dates = available.dt.tz_convert("Asia/Shanghai").dt.tz_localize(None).dt.normalize()
    work = pd.DataFrame(
        {"instrument_id": frame["instrument_id"].astype(str), "available_date": local_dates}
    )
    return work.groupby("instrument_id")["available_date"].min()


def _industry_intervals(frame: pd.DataFrame, audit_end: pd.Timestamp) -> pd.DataFrame:
    intervals = frame[["instrument_id", "membership_start", "membership_end"]].copy()
    intervals["instrument_id"] = intervals["instrument_id"].astype(str)
    intervals["membership_start"] = pd.to_datetime(
        intervals["membership_start"], errors="coerce"
    ).dt.normalize()
    intervals["membership_end"] = pd.to_datetime(
        intervals["membership_end"], errors="coerce"
    ).dt.normalize().fillna(audit_end)
    if intervals[["membership_start", "membership_end"]].isna().any(axis=None):
        raise IndustryCoverageError("industry membership contains invalid intervals")
    return intervals


def _daily_coverage(
    open_dates: pd.DatetimeIndex,
    first_available: pd.Series,
    intervals: pd.DataFrame,
    minimum_coverage: float,
) -> pd.DataFrame:
    records = []
    for date in open_dates:
        financial_ids = set(first_available.index[first_available.le(date)])
        active = intervals.loc[
            intervals["membership_start"].le(date)
            & intervals["membership_end"].ge(date),
            "instrument_id",
        ]
        industry_ids = set(active)
        matched = financial_ids & industry_ids
        forward = _ratio(len(matched), len(financial_ids))
        reverse = _ratio(len(matched), len(industry_ids))
        records.append(
            {
                "trade_date": date,
                "financial_known_instruments": len(financial_ids),
                "industry_active_instruments": len(industry_ids),
                "matched_instruments": len(matched),
                "financial_without_active_industry": len(financial_ids - industry_ids),
                "active_industry_without_financial": len(industry_ids - financial_ids),
                "fundamental_to_industry_coverage": forward,
                "industry_to_fundamental_coverage": reverse,
                "below_minimum_coverage": bool(forward < minimum_coverage),
            }
        )
    return pd.DataFrame(records)


def _distribution(series: pd.Series) -> Dict[str, float]:
    return {
        "minimum": float(series.min()),
        "p01": float(series.quantile(0.01)),
        "p05": float(series.quantile(0.05)),
        "median": float(series.median()),
        "maximum": float(series.max()),
    }


def _invalid_namespace_rows(series: pd.Series) -> int:
    return int((~series.astype("string").str.startswith("CN_EQ:", na=False)).sum())


def _require_columns(frame: pd.DataFrame, required: set[str], name: str) -> None:
    missing = sorted(required - set(frame.columns))
    if missing:
        raise IndustryCoverageError(f"{name} missing columns: {missing}")


def _ratio(numerator: int, denominator: int) -> float:
    return float(numerator / denominator) if denominator else 0.0


def _write_immutable_parquet(frame: pd.DataFrame, path: Path) -> None:
    temporary = path.parent / f".{path.name}.{os.getpid()}.tmp"
    frame.to_parquet(temporary, index=False)
    if path.exists():
        if _sha256(path) != _sha256(temporary):
            temporary.unlink()
            raise IndustryCoverageError(f"refusing to overwrite immutable output: {path}")
        temporary.unlink()
        return
    os.replace(temporary, path)


def _write_immutable_json(payload: Dict[str, Any], path: Path) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n"
    temporary = path.parent / f".{path.name}.{os.getpid()}.tmp"
    temporary.write_text(encoded, encoding="utf-8")
    if path.exists():
        if path.read_text(encoding="utf-8") != encoded:
            temporary.unlink()
            raise IndustryCoverageError(f"refusing to overwrite immutable output: {path}")
        temporary.unlink()
        return
    os.replace(temporary, path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
