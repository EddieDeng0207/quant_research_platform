"""Immutable, taxonomy-versioned historical industry membership artifacts."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd

from qrp.versioning import (
    VersionControlError,
    environment_lock_identity,
    inspect_git_repository,
)

from .catalog import load_latest_snapshot
from .fundamentals import _load_aliases

INDUSTRY_POLICY_VERSION = "p08_shenwan_l1_effective_interval_v3"
MAXIMUM_BRIDGE_GAP_CALENDAR_DAYS = 7
TAXONOMY_WINDOWS = {
    "SW2014": (None, pd.Timestamp("2021-12-12")),
    "SW2021": (pd.Timestamp("2021-12-13"), None),
}


class IndustryPITError(RuntimeError):
    """Raised when historical industry membership cannot prove temporal safety."""


def build_historical_industry_artifact(
    run_path: Path,
    lake_root: Path,
    output_root: Path,
    start_date: str,
    end_date: str,
    *,
    aliases_path: Optional[Path] = None,
    security_code_mappings_path: Optional[Path] = None,
    require_clean_git: bool = False,
    strict: bool = True,
) -> Path:
    """Build an immutable L1 interval table using the taxonomy valid at each date."""
    start = pd.Timestamp(start_date).normalize()
    end = pd.Timestamp(end_date).normalize()
    if start > end:
        raise ValueError("start_date must be on or before end_date")
    run = Path(run_path).resolve()
    lake = Path(lake_root).resolve()
    run_manifest_path = run / "run_manifest.json"
    config_path = run / "config_snapshot.json"
    if not run_manifest_path.exists() or not config_path.exists():
        raise IndustryPITError("industry run is missing its frozen manifest/config")
    run_manifest = json.loads(run_manifest_path.read_text(encoding="utf-8"))
    config = json.loads(config_path.read_text(encoding="utf-8"))
    configured_taxonomies = set(config.get("taxonomies", ()))
    if configured_taxonomies != set(TAXONOMY_WINDOWS):
        raise IndustryPITError(
            "formal historical industry artifact requires both SW2014 and SW2021"
        )
    if run_manifest.get("status") != "completed":
        raise IndustryPITError("only a completed industry ingestion run can be curated")
    checkpoint_value = run_manifest.get("summary", {}).get("checkpoint")
    if not checkpoint_value:
        raise IndustryPITError("industry run does not identify its checkpoint")
    checkpoint_path = Path(checkpoint_value)
    if not checkpoint_path.is_absolute():
        checkpoint_path = (run / checkpoint_path).resolve()
    state = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    categories = list(state.get("categories", []))
    if not categories:
        raise IndustryPITError("industry checkpoint contains no frozen categories")
    expected = {
        f"industry_membership:{item['taxonomy']}:{item['source_index_code']}" for item in categories
    }
    completed = state.get("completed", {})
    missing = sorted(expected - set(completed))
    if missing:
        raise IndustryPITError(
            f"industry checkpoint is incomplete; missing {len(missing)} membership tasks"
        )

    entries = _lake_manifest_by_path(lake)
    raw_inputs = []
    frames = []
    for task_id in sorted(expected):
        raw_path = Path(completed[task_id]).resolve()
        try:
            relative = str(raw_path.relative_to(lake))
        except ValueError as exc:
            raise IndustryPITError(f"industry input is outside the lake: {raw_path}") from exc
        entry = entries.get(relative)
        if entry is None or entry.get("dataset") != "industry_membership":
            raise IndustryPITError(f"industry raw input is absent or mismatched: {relative}")
        digest = _sha256(raw_path)
        if digest != entry.get("sha256"):
            raise IndustryPITError(f"industry raw input hash mismatch: {relative}")
        frame = pd.read_parquet(raw_path)
        if len(frame) != int(entry.get("rows", -1)):
            raise IndustryPITError(f"industry raw row-count mismatch: {relative}")
        frames.append(frame)
        raw_inputs.append(
            {
                "task_id": task_id,
                "path": relative,
                "sha256": digest,
                "rows": len(frame),
                "written_at": entry["written_at"],
            }
        )
    aliases, alias_identity = _load_aliases(aliases_path, security_code_mappings_path)
    instruments_snapshot = load_latest_snapshot(lake, "tushare", "instruments")
    lifecycle = _curate_instrument_lifecycle(instruments_snapshot.frame, aliases)
    membership = _curate_membership(pd.concat(frames, ignore_index=True), start, end, aliases)
    membership, interval_bridge = _bridge_short_membership_gaps(
        membership,
        lifecycle,
        maximum_gap_calendar_days=MAXIMUM_BRIDGE_GAP_CALENDAR_DAYS,
    )
    hard_failures = _quality_failures(
        membership,
        start,
        end,
        lifecycle,
        maximum_gap_calendar_days=MAXIMUM_BRIDGE_GAP_CALENDAR_DAYS,
    )
    quality = {
        "rows": len(membership),
        "instruments": int(membership["instrument_id"].nunique()),
        "industries": int(membership["industry_code"].nunique()),
        "taxonomy_rows": {
            key: int(value) for key, value in membership.groupby("taxonomy").size().items()
        },
        "covered_start": _date_or_none(membership["membership_start"], "min"),
        "covered_end": _date_or_none(membership["membership_end"], "max"),
        "interval_bridge": interval_bridge,
        "hard_failures": hard_failures,
        "promotion_passed": all(value == 0 for value in hard_failures.values()),
    }
    code_identity: Optional[Dict[str, Any]] = None
    environment_lock: Optional[Dict[str, Any]] = None
    try:
        git = inspect_git_repository(Path(__file__), require_clean=require_clean_git)
        code_identity = git.to_dict()
        environment_lock = environment_lock_identity(Path(git.repository_root))
    except VersionControlError:
        if require_clean_git:
            raise
    identity = {
        "schema_version": "p08_historical_industry_v3",
        "policy_version": INDUSTRY_POLICY_VERSION,
        "start_date": str(start.date()),
        "end_date": str(end.date()),
        "taxonomy_windows": {
            key: [
                str(window_start.date()) if window_start is not None else None,
                str(window_end.date()) if window_end is not None else None,
            ]
            for key, (window_start, window_end) in TAXONOMY_WINDOWS.items()
        },
        "run_manifest_sha256": _sha256(run_manifest_path),
        "config_sha256": _sha256(config_path),
        "checkpoint_sha256": _sha256(checkpoint_path),
        "raw_inputs": [{"path": item["path"], "sha256": item["sha256"]} for item in raw_inputs],
        "instrument_lifecycle_snapshot": {
            "fingerprint": instruments_snapshot.fingerprint,
            "inputs": [
                {"path": item["path"], "sha256": item["sha256"]}
                for item in instruments_snapshot.manifest_entries
            ],
        },
        "aliases": alias_identity,
        "implementation_sha256": _implementation_sha256(),
        "git_commit": code_identity["commit"] if code_identity else None,
        "git_tree": code_identity["tree"] if code_identity else None,
        "git_dirty_state_sha256": (code_identity["dirty_state_sha256"] if code_identity else None),
        "environment_lock_sha256": (environment_lock["sha256"] if environment_lock else None),
    }
    artifact_id = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:20]
    destination = Path(output_root) / "industry" / f"artifact_id={artifact_id}"
    destination.mkdir(parents=True, exist_ok=True)
    output = destination / "membership.parquet"
    _write_immutable_parquet(
        membership,
        output,
        ["instrument_id", "membership_start", "taxonomy", "industry_code"],
    )
    lifecycle_output = destination / "instrument_lifecycle.parquet"
    _write_immutable_parquet(
        lifecycle,
        lifecycle_output,
        ["instrument_id"],
    )
    manifest = {
        "artifact_id": artifact_id,
        "schema_version": "p08_historical_industry_v3",
        "identity": identity,
        "inputs": {
            "run": str(run),
            "lake_root": str(lake),
            "raw": raw_inputs,
            "instrument_lifecycle": instruments_snapshot.manifest_entries,
        },
        "outputs": {
            "membership": {
                "path": output.name,
                "sha256": _sha256(output),
                "rows": len(membership),
            },
            "instrument_lifecycle": {
                "path": lifecycle_output.name,
                "sha256": _sha256(lifecycle_output),
                "rows": len(lifecycle),
            },
        },
        "quality": quality,
        "code_identity": code_identity,
        "environment_lock": environment_lock,
        "guardrails": {
            "taxonomy_revision_not_backfilled": True,
            "sw2014_used_through_2021_12_12": True,
            "sw2021_used_from_2021_12_13": True,
            "source_intervals_preserved_unless_audited_short_gap_bridge": True,
            "source_out_date_treated_as_exclusive": True,
            "short_interval_gaps_bridged_only_while_listed": True,
            "maximum_bridge_gap_calendar_days": MAXIMUM_BRIDGE_GAP_CALENDAR_DAYS,
            "overlapping_instrument_intervals_forbidden": True,
            "unresolved_listed_short_interval_gaps_forbidden": True,
            "stable_instrument_identity_mapping": True,
            "cn_equity_namespace_enforced": True,
            "raw_inputs_hash_verified": True,
            "git_commit_bound": code_identity is not None,
            "git_dirty_state_bound": bool(
                code_identity and code_identity.get("dirty_state_sha256")
            ),
        },
    }
    _write_immutable_json(manifest, destination / "manifest.json")
    if strict and not quality["promotion_passed"]:
        raise IndustryPITError(
            f"historical industry artifact failed promotion at {destination}: {hard_failures}"
        )
    return destination


def select_industry_as_of(frame: pd.DataFrame, decision_at: Any) -> pd.DataFrame:
    """Return the single effective industry assignment at a historical decision."""
    required = {"instrument_id", "membership_start", "membership_end", "industry_code"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise IndustryPITError(f"industry frame missing columns: {missing}")
    local_date = (
        (
            pd.Timestamp(decision_at).tz_localize("Asia/Shanghai")
            if pd.Timestamp(decision_at).tzinfo is None
            else pd.Timestamp(decision_at).tz_convert("Asia/Shanghai")
        )
        .normalize()
        .tz_localize(None)
    )
    work = frame.copy()
    starts = pd.to_datetime(work["membership_start"], errors="coerce").dt.normalize()
    ends = pd.to_datetime(work["membership_end"], errors="coerce").dt.normalize()
    selected = work.loc[starts.le(local_date) & ends.ge(local_date)].copy()
    if selected.duplicated("instrument_id").any():
        sample = selected.loc[
            selected.duplicated("instrument_id", keep=False), "instrument_id"
        ].head(5)
        raise IndustryPITError(f"ambiguous industry assignment: {sample.tolist()}")
    selected["decision_at"] = pd.Timestamp(decision_at)
    return selected.sort_values("instrument_id").reset_index(drop=True)


def _curate_membership(
    frame: pd.DataFrame,
    start: pd.Timestamp,
    end: pd.Timestamp,
    aliases: Dict[str, str],
) -> pd.DataFrame:
    required = {
        "taxonomy",
        "industry_code",
        "industry_name",
        "source_index_code",
        "industry_level",
        "symbol",
        "source_membership_start",
        "source_membership_end",
        "source",
        "ingested_at",
    }
    missing = sorted(required - set(frame.columns))
    if missing:
        raise IndustryPITError(f"industry raw rows missing columns: {missing}")
    result = frame.copy()
    result["source_membership_start"] = pd.to_datetime(
        result["source_membership_start"], errors="coerce"
    ).dt.normalize()
    result["source_membership_end"] = pd.to_datetime(
        result["source_membership_end"], errors="coerce"
    ).dt.normalize()
    result["source_ingested_at"] = pd.to_datetime(result["ingested_at"], utc=True, errors="coerce")
    if result[["source_membership_start", "source_ingested_at"]].isna().any(axis=None):
        raise IndustryPITError("industry raw rows contain invalid temporal fields")
    result["instrument_id"] = (
        result["symbol"]
        .astype("string")
        .map(lambda symbol: aliases.get(str(symbol), f"CN_EQ:{symbol}"))
    )
    result["identity_alias_resolved"] = result["symbol"].astype(str).isin(aliases)
    starts = []
    ends = []
    for row in result.itertuples(index=False):
        window_start, window_end = TAXONOMY_WINDOWS.get(row.taxonomy, (None, None))
        membership_start = max(
            value
            for value in (row.source_membership_start, start, window_start)
            if value is not None
        )
        source_end_inclusive = (
            row.source_membership_end - pd.Timedelta(days=1)
            if pd.notna(row.source_membership_end)
            else end
        )
        membership_end = min(
            value for value in (source_end_inclusive, end, window_end) if value is not None
        )
        starts.append(membership_start)
        ends.append(membership_end)
    result["membership_start"] = starts
    result["membership_end"] = ends
    result = result.loc[result["membership_start"].le(result["membership_end"])].copy()
    return _assign_membership_ids(result)


def _assign_membership_ids(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    result["membership_id"] = result.apply(
        lambda row: hashlib.sha256(
            "|".join(
                [
                    str(row["instrument_id"]),
                    str(row["taxonomy"]),
                    str(row["industry_code"]),
                    str(row["membership_start"].date()),
                    str(row["membership_end"].date()),
                ]
            ).encode("utf-8")
        ).hexdigest(),
        axis=1,
    )
    result = result.drop(columns=["ingested_at"], errors="ignore")
    return result.drop_duplicates("membership_id").reset_index(drop=True)


def _curate_instrument_lifecycle(
    instruments: pd.DataFrame,
    aliases: Dict[str, str],
) -> pd.DataFrame:
    required = {"symbol", "list_date", "delist_date"}
    missing = sorted(required - set(instruments.columns))
    if missing:
        raise IndustryPITError(f"instrument lifecycle snapshot missing columns: {missing}")
    work = instruments.copy()
    if "instrument_kind" in work:
        work = work.loc[work["instrument_kind"].eq("stock")].copy()
    work["instrument_id"] = (
        work["symbol"]
        .astype("string")
        .map(lambda symbol: aliases.get(str(symbol), f"CN_EQ:{symbol}"))
    )
    work["listed_from"] = pd.to_datetime(work["list_date"], errors="coerce").dt.normalize()
    work["listed_through"] = pd.to_datetime(work["delist_date"], errors="coerce").dt.normalize()
    if work["listed_from"].isna().any():
        sample = work.loc[work["listed_from"].isna(), "symbol"].head(5).tolist()
        raise IndustryPITError(f"instrument lifecycle has invalid list dates: {sample}")
    records = []
    for instrument_id, group in work.groupby("instrument_id", sort=True):
        listed_through = (
            pd.NaT if group["listed_through"].isna().any() else group["listed_through"].max()
        )
        records.append(
            {
                "instrument_id": instrument_id,
                "listed_from": group["listed_from"].min(),
                "listed_through": listed_through,
                "source_symbols": " | ".join(sorted(group["symbol"].astype(str).unique())),
            }
        )
    return pd.DataFrame(records)


def _bridge_short_membership_gaps(
    frame: pd.DataFrame,
    lifecycle: pd.DataFrame,
    *,
    maximum_gap_calendar_days: int,
) -> tuple[pd.DataFrame, Dict[str, int]]:
    if maximum_gap_calendar_days < 1:
        raise ValueError("maximum_gap_calendar_days must be positive")
    result = frame.sort_values(
        ["instrument_id", "membership_start", "membership_end", "membership_id"]
    ).reset_index(drop=True)
    lifecycle_by_id = lifecycle.set_index("instrument_id")
    adjacent_gap_rows = 0
    short_gap_rows = 0
    unverified_short_gap_rows = 0
    bridged_interval_rows = 0
    bridged_calendar_days = 0
    long_gap_rows = 0
    previous_index: Optional[int] = None
    previous_instrument: Optional[str] = None
    for index, row in result.iterrows():
        instrument_id = str(row["instrument_id"])
        if previous_index is None or previous_instrument != instrument_id:
            previous_index = index
            previous_instrument = instrument_id
            continue
        previous_end = pd.Timestamp(result.at[previous_index, "membership_end"])
        next_start = pd.Timestamp(row["membership_start"])
        gap_days = int((next_start - previous_end).days - 1)
        if gap_days <= 0:
            previous_index = index
            continue
        adjacent_gap_rows += 1
        if gap_days > maximum_gap_calendar_days:
            long_gap_rows += 1
            previous_index = index
            continue
        short_gap_rows += 1
        if instrument_id not in lifecycle_by_id.index:
            unverified_short_gap_rows += 1
            previous_index = index
            continue
        instrument = lifecycle_by_id.loc[instrument_id]
        gap_start = previous_end + pd.Timedelta(days=1)
        gap_end = next_start - pd.Timedelta(days=1)
        listed_from = pd.Timestamp(instrument["listed_from"])
        listed_through = instrument["listed_through"]
        remained_listed = listed_from <= gap_start and (
            pd.isna(listed_through) or pd.Timestamp(listed_through) >= gap_end
        )
        if remained_listed:
            result.at[previous_index, "membership_end"] = gap_end
            bridged_interval_rows += 1
            bridged_calendar_days += gap_days
        previous_index = index
    result = _assign_membership_ids(result.drop(columns="membership_id"))
    remaining = _listed_short_interval_gaps(
        result,
        lifecycle,
        maximum_gap_calendar_days=maximum_gap_calendar_days,
    )
    return result, {
        "maximum_gap_calendar_days": maximum_gap_calendar_days,
        "adjacent_interval_gap_rows_before_bridge": adjacent_gap_rows,
        "short_interval_gap_rows_before_bridge": short_gap_rows,
        "long_interval_gap_rows_not_bridgeable": long_gap_rows,
        "bridged_interval_rows": bridged_interval_rows,
        "bridged_calendar_days": bridged_calendar_days,
        "unverified_short_interval_gap_rows": unverified_short_gap_rows,
        "interval_gap_rows": remaining,
    }


def _listed_short_interval_gaps(
    frame: pd.DataFrame,
    lifecycle: pd.DataFrame,
    *,
    maximum_gap_calendar_days: int,
) -> int:
    ordered = frame.sort_values(["instrument_id", "membership_start", "membership_end"])
    previous_end = ordered.groupby("instrument_id")["membership_end"].shift()
    gap_days = (ordered["membership_start"] - previous_end).dt.days - 1
    short_gap = previous_end.notna() & gap_days.between(1, maximum_gap_calendar_days)
    if not short_gap.any():
        return 0
    candidates = ordered.loc[short_gap, ["instrument_id", "membership_start"]].copy()
    candidates["gap_start"] = previous_end.loc[short_gap] + pd.Timedelta(days=1)
    candidates["gap_end"] = candidates["membership_start"] - pd.Timedelta(days=1)
    candidates = candidates.merge(lifecycle, on="instrument_id", how="left")
    listed = (
        candidates["listed_from"].notna()
        & candidates["listed_from"].le(candidates["gap_start"])
        & (
            candidates["listed_through"].isna()
            | candidates["listed_through"].ge(candidates["gap_end"])
        )
    )
    return int(listed.sum())


def _quality_failures(
    frame: pd.DataFrame,
    start: pd.Timestamp,
    end: pd.Timestamp,
    lifecycle: pd.DataFrame,
    *,
    maximum_gap_calendar_days: int,
) -> Dict[str, int]:
    ordered = frame.sort_values(["instrument_id", "membership_start", "membership_end"])
    previous_end = ordered.groupby("instrument_id")["membership_end"].shift()
    overlaps = previous_end.notna() & ordered["membership_start"].le(previous_end)
    return {
        "duplicate_membership_ids": int(frame.duplicated("membership_id").sum()),
        "invalid_intervals": int(frame["membership_start"].gt(frame["membership_end"]).sum()),
        "overlapping_instrument_intervals": int(overlaps.sum()),
        "interval_gap_rows": _listed_short_interval_gaps(
            frame,
            lifecycle,
            maximum_gap_calendar_days=maximum_gap_calendar_days,
        ),
        "unexpected_taxonomies": int((~frame["taxonomy"].isin(TAXONOMY_WINDOWS)).sum()),
        "non_l1_rows": int((~frame["industry_level"].eq("L1")).sum()),
        "invalid_instrument_namespace_rows": int(
            (~frame["instrument_id"].astype("string").str.startswith("CN_EQ:", na=False)).sum()
        ),
        "coverage_start_after_requested": int(frame["membership_start"].min() > start),
        "coverage_end_before_requested": int(frame["membership_end"].max() < end),
    }


def _lake_manifest_by_path(lake: Path) -> Dict[str, Dict[str, Any]]:
    manifest = lake / "manifest.jsonl"
    if not manifest.exists():
        raise IndustryPITError(f"lake manifest does not exist: {manifest}")
    entries: Dict[str, Dict[str, Any]] = {}
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if line:
            entry = json.loads(line)
            entries[entry["path"]] = entry
    return entries


def _write_immutable_parquet(frame: pd.DataFrame, path: Path, sort_columns: list[str]) -> None:
    ordered = frame.sort_values(sort_columns).reset_index(drop=True)
    temporary = path.parent / f".{path.name}.tmp"
    ordered.to_parquet(temporary, index=False)
    if path.exists():
        if _sha256(path) != _sha256(temporary):
            temporary.unlink()
            raise IndustryPITError(f"refusing to overwrite immutable output: {path}")
        temporary.unlink()
        return
    os.replace(temporary, path)


def _write_immutable_json(payload: Dict[str, Any], path: Path) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n"
    temporary = path.parent / f".{path.name}.tmp"
    temporary.write_text(encoded, encoding="utf-8")
    if path.exists():
        if path.read_text(encoding="utf-8") != encoded:
            temporary.unlink()
            raise IndustryPITError(f"refusing to overwrite immutable output: {path}")
        temporary.unlink()
        return
    os.replace(temporary, path)


def _implementation_sha256() -> str:
    digest = hashlib.sha256()
    for path in sorted(
        [
            Path(__file__),
            Path(__file__).with_name("industry_ingestion.py"),
            Path(__file__).with_name("catalog.py"),
            Path(__file__).with_name("fundamentals.py"),
            Path(__file__).with_name("contracts.py"),
            Path(__file__).parent / "providers" / "tushare.py",
        ]
    ):
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _date_or_none(series: pd.Series, operation: str) -> Optional[str]:
    value = getattr(pd.to_datetime(series, errors="coerce"), operation)()
    return None if pd.isna(value) else str(value.date())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
