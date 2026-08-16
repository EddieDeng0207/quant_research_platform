"""Immutable point-in-time financial-statement artifacts and as-of selection."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence

import pandas as pd

from qrp.versioning import (
    VersionControlError,
    environment_lock_identity,
    inspect_git_repository,
)

from .fundamental_ingestion import FUNDAMENTAL_STATEMENTS
from .temporal import attach_next_session_availability


class FundamentalPITError(RuntimeError):
    """Raised when a financial snapshot cannot prove point-in-time safety."""


PIT_POLICY_VERSION = "p08_fundamental_pit_next_open_v1"
INDEX_COLUMNS = [
    "version_id",
    "instrument_id",
    "symbol",
    "statement_type",
    "report_period",
    "report_type",
    "company_type",
    "period_type",
    "announcement_at",
    "available_at",
    "source_ingested_at",
    "source_row_sha256",
    "source_row_occurrence",
    "update_flag",
]


def build_fundamental_pit_artifact(
    run_path: Path,
    lake_root: Path,
    calendar_path: Path,
    output_root: Path,
    *,
    as_of_ingested_at: Optional[str] = None,
    aliases_path: Optional[Path] = None,
    security_code_mappings_path: Optional[Path] = None,
    require_clean_git: bool = False,
    strict: bool = True,
) -> Path:
    """Convert one completed raw backfill grid into an immutable PIT artifact."""
    run = Path(run_path).resolve()
    lake = Path(lake_root).resolve()
    calendar_file = Path(calendar_path).resolve()
    run_manifest_path = run / "run_manifest.json"
    config_path = run / "config_snapshot.json"
    if not run_manifest_path.exists() or not config_path.exists():
        raise FundamentalPITError("fundamental run is missing its frozen manifest/config")
    run_manifest = json.loads(run_manifest_path.read_text(encoding="utf-8"))
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if run_manifest.get("status") != "completed":
        raise FundamentalPITError("only a completed fundamental ingestion run can be curated")
    checkpoint_value = run_manifest.get("summary", {}).get("checkpoint")
    if not checkpoint_value:
        raise FundamentalPITError("fundamental run does not identify its checkpoint")
    checkpoint_path = Path(checkpoint_value)
    if not checkpoint_path.is_absolute():
        checkpoint_path = (run / checkpoint_path).resolve()
    if not checkpoint_path.exists():
        raise FundamentalPITError(f"fundamental checkpoint does not exist: {checkpoint_path}")
    state = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    statements = tuple(config.get("statements", ()))
    symbols = tuple(state.get("symbols", ()))
    periods = tuple(state.get("periods", ()))
    period_mode = bool(config.get("period_mode", False))
    if not statements or (period_mode and not periods) or (not period_mode and not symbols):
        raise FundamentalPITError("fundamental run has an empty statement task grid")
    unknown = sorted(set(statements) - set(FUNDAMENTAL_STATEMENTS))
    if unknown:
        raise FundamentalPITError(f"fundamental run contains unknown statements: {unknown}")

    cells = periods if period_mode else symbols
    expected_tasks = {
        f"fundamentals_{statement}:{cell}"
        for statement in statements
        for cell in cells
    }
    completed = state.get("completed", {})
    missing_tasks = sorted(expected_tasks - set(completed))
    if missing_tasks:
        raise FundamentalPITError(
            f"fundamental checkpoint is incomplete; missing {len(missing_tasks)} tasks"
        )

    manifest_entries = _lake_manifest_by_path(lake)
    cutoff = _utc_timestamp(as_of_ingested_at) if as_of_ingested_at else None
    raw_inputs: list[Dict[str, Any]] = []
    frames_by_statement: Dict[str, list[pd.DataFrame]] = {
        statement: [] for statement in statements
    }
    zero_row_tasks = 0
    excluded_nonstandard_financial_rows = 0
    written_times = []
    for task_id in sorted(expected_tasks):
        raw_path = Path(completed[task_id])
        if not raw_path.is_absolute():
            raw_path = raw_path.resolve()
        try:
            relative = str(raw_path.relative_to(lake))
        except ValueError as exc:
            raise FundamentalPITError(
                f"checkpoint input is outside the declared lake: {raw_path}"
            ) from exc
        entry = manifest_entries.get(relative)
        if entry is None:
            raise FundamentalPITError(f"raw input is absent from lake manifest: {relative}")
        written_at = _utc_timestamp(entry["written_at"])
        if cutoff is not None and written_at > cutoff:
            raise FundamentalPITError(
                f"raw input {relative} was written after as_of_ingested_at"
            )
        if _sha256(raw_path) != entry.get("sha256"):
            raise FundamentalPITError(f"raw input hash mismatch: {raw_path}")
        statement = task_id.split(":", 1)[0].removeprefix("fundamentals_")
        expected_dataset = f"fundamentals_{statement}"
        if entry.get("dataset") != expected_dataset:
            raise FundamentalPITError(f"checkpoint dataset mismatch for {task_id}")
        frame = pd.read_parquet(raw_path)
        if len(frame) != int(entry.get("rows", -1)):
            raise FundamentalPITError(f"raw input row-count mismatch: {raw_path}")
        if frame.empty:
            zero_row_tasks += 1
        else:
            if "instrument_kind" in frame:
                excluded_nonstandard_financial_rows += int(
                    frame["instrument_kind"].ne("stock").sum()
                )
            frames_by_statement[statement].append(frame)
        written_times.append(written_at)
        raw_inputs.append(
            {
                "task_id": task_id,
                "path": relative,
                "sha256": entry["sha256"],
                "rows": int(entry["rows"]),
                "written_at": entry["written_at"],
            }
        )
    research_as_of_at = cutoff or max(written_times)
    if period_mode:
        eligible_symbols = set()
        for parts in frames_by_statement.values():
            for frame in parts:
                eligible = frame
                if "instrument_kind" in eligible:
                    eligible = eligible.loc[eligible["instrument_kind"].eq("stock")]
                eligible_symbols.update(eligible["symbol"].dropna().astype(str).unique())
        symbols = tuple(sorted(eligible_symbols))
        if not symbols:
            raise FundamentalPITError("period-mode run produced no financial symbols")

    calendar = pd.read_parquet(calendar_file)
    _validate_calendar(calendar)
    aliases, alias_identity = _load_aliases(
        aliases_path, security_code_mappings_path
    )
    curated: Dict[str, pd.DataFrame] = {}
    indexes = []
    hard_failures = {
        "duplicate_version_ids": 0,
        "announcement_after_availability_rows": 0,
        "source_ingested_after_research_as_of_rows": 0,
        "invalid_report_period_rows": 0,
    }
    statement_quality: Dict[str, Any] = {}
    for statement in statements:
        parts = frames_by_statement[statement]
        if parts:
            frame = _concat_statement_parts(parts)
            frame = _curate_statement(
                frame,
                statement,
                calendar,
                research_as_of_at,
                aliases,
            )
        else:
            frame = _empty_curated_statement()
        curated[statement] = frame
        if not frame.empty:
            indexes.append(_version_index(frame))
            hard_failures["duplicate_version_ids"] += int(
                frame.duplicated("version_id").sum()
            )
            hard_failures["announcement_after_availability_rows"] += int(
                (frame["announcement_at"] > frame["available_at"]).sum()
            )
            hard_failures["source_ingested_after_research_as_of_rows"] += int(
                (frame["source_ingested_at"] > research_as_of_at).sum()
            )
            hard_failures["invalid_report_period_rows"] += int(
                frame["report_period"].isna().sum()
            )
        statement_quality[statement] = {
            "rows": len(frame),
            "symbols_with_rows": int(frame["symbol"].nunique()) if not frame.empty else 0,
            "report_period_start": _date_or_none(frame.get("report_period"), "min"),
            "report_period_end": _date_or_none(frame.get("report_period"), "max"),
            "available_at_start": _timestamp_or_none(frame.get("available_at"), "min"),
            "available_at_end": _timestamp_or_none(frame.get("available_at"), "max"),
        }
    version_index = (
        pd.concat(indexes, ignore_index=True)
        if indexes
        else pd.DataFrame(columns=INDEX_COLUMNS)
    )
    if not version_index.empty and version_index.duplicated("version_id").any():
        hard_failures["duplicate_version_ids"] = int(
            version_index.duplicated("version_id").sum()
        )
    quality = {
        "expected_statement_tasks": len(expected_tasks),
        "captured_statement_tasks": len(raw_inputs),
        "zero_row_tasks": zero_row_tasks,
        "symbols": len(symbols),
        "request_axis": "report_period" if period_mode else "symbol",
        "report_periods": len(periods) if period_mode else None,
        "excluded_legacy_instruments": len(
            state.get("excluded_legacy_instruments", [])
        ),
        "excluded_nonstandard_financial_rows": excluded_nonstandard_financial_rows,
        "statements": statement_quality,
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
    implementation = _implementation_identity()
    identity = {
        "schema_version": "p08_fundamental_pit_v1",
        "policy_version": PIT_POLICY_VERSION,
        "run_manifest_sha256": _sha256(run_manifest_path),
        "config_sha256": _sha256(config_path),
        "checkpoint_sha256": _sha256(checkpoint_path),
        "calendar_sha256": _sha256(calendar_file),
        "research_as_of_at": research_as_of_at.isoformat(),
        "raw_inputs": [
            {"path": item["path"], "sha256": item["sha256"]}
            for item in raw_inputs
        ],
        "aliases": alias_identity,
        "implementation_sha256": implementation["tree_sha256"],
        "git_commit": code_identity["commit"] if code_identity else None,
        "git_tree": code_identity["tree"] if code_identity else None,
        "git_dirty_state_sha256": (
            code_identity["dirty_state_sha256"] if code_identity else None
        ),
        "environment_lock_sha256": (
            environment_lock["sha256"] if environment_lock else None
        ),
    }
    artifact_id = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:20]
    destination = Path(output_root) / "fundamentals" / f"artifact_id={artifact_id}"
    destination.mkdir(parents=True, exist_ok=True)
    outputs: Dict[str, Dict[str, Any]] = {}
    for statement, frame in curated.items():
        path = destination / f"{statement}.parquet"
        sort_columns = [
            column
            for column in ("instrument_id", "report_period", "available_at", "version_id")
            if column in frame
        ]
        _write_immutable_parquet(frame, path, sort_columns)
        outputs[statement] = _output_identity(path, frame)
    index_path = destination / "version_index.parquet"
    _write_immutable_parquet(
        version_index,
        index_path,
        ["instrument_id", "statement_type", "report_period", "available_at", "version_id"],
    )
    outputs["version_index"] = _output_identity(index_path, version_index)
    manifest = {
        "artifact_id": artifact_id,
        "schema_version": "p08_fundamental_pit_v1",
        "identity": identity,
        "inputs": {
            "run": str(run),
            "lake_root": str(lake),
            "calendar": str(calendar_file),
            "raw": raw_inputs,
        },
        "outputs": outputs,
        "quality": quality,
        "implementation": implementation,
        "code_identity": code_identity,
        "environment_lock": environment_lock,
        "guardrails": {
            "report_period_is_not_knowledge_time": True,
            "actual_announcement_date_preferred": True,
            "date_only_disclosure_available_next_session_open": True,
            "raw_revisions_preserved": True,
            "source_ingestion_cutoff_frozen": True,
            "empty_snapshots_distinguished_from_failed_requests": True,
            "raw_inputs_hash_verified": True,
            "formal_cli_requires_clean_git": True,
            "git_commit_bound": code_identity is not None,
            "environment_lock_bound": environment_lock is not None,
        },
    }
    _write_immutable_json(manifest, destination / "manifest.json")
    if strict and not quality["promotion_passed"]:
        raise FundamentalPITError(
            f"fundamental PIT artifact failed promotion at {destination}: {hard_failures}"
        )
    return destination


def select_fundamentals_as_of(
    frame: pd.DataFrame,
    decision_at: Any,
    research_as_of_at: Any,
    *,
    report_types: Optional[Sequence[str]] = ("1",),
    latest_report_only: bool = True,
) -> pd.DataFrame:
    """Select the latest unambiguous version known at one historical decision."""
    required = {
        "instrument_id",
        "report_period",
        "available_at",
        "source_ingested_at",
        "source_row_sha256",
        "source_row_occurrence",
        "version_id",
    }
    missing = sorted(required - set(frame.columns))
    if missing:
        raise FundamentalPITError(f"fundamental frame missing columns: {missing}")
    decision = _utc_timestamp(decision_at)
    research_as_of = _utc_timestamp(research_as_of_at)
    if decision > research_as_of:
        raise FundamentalPITError("decision_at cannot be after research_as_of_at")
    work = frame.copy()
    work["available_at"] = pd.to_datetime(work["available_at"], utc=True, errors="coerce")
    work["source_ingested_at"] = pd.to_datetime(
        work["source_ingested_at"], utc=True, errors="coerce"
    )
    work["report_period"] = pd.to_datetime(work["report_period"], errors="coerce").dt.normalize()
    if work[["available_at", "source_ingested_at", "report_period"]].isna().any(axis=None):
        raise FundamentalPITError("fundamental frame contains invalid PIT timestamps")
    work = work.loc[
        (work["available_at"] <= decision)
        & (work["source_ingested_at"] <= research_as_of)
    ].copy()
    if (
        report_types is not None
        and "report_type" in work
        and work["report_type"].notna().any()
    ):
        allowed = {str(value) for value in report_types}
        work = work.loc[work["report_type"].astype("string").isin(allowed)].copy()
    if work.empty:
        return work.assign(decision_at=decision, research_as_of_at=research_as_of)

    keys = ["instrument_id", "report_period"]
    max_available = work.groupby(keys, observed=True)["available_at"].transform("max")
    candidates = work.loc[work["available_at"].eq(max_available)].copy()
    candidates = candidates.sort_values(
        [*keys, "source_row_sha256", "source_row_occurrence"]
    ).drop_duplicates([*keys, "available_at", "source_row_sha256"], keep="first")
    if "update_flag" in candidates:
        candidates["_latest_flag"] = candidates["update_flag"].astype("string").eq("1")
        has_latest = candidates.groupby(keys, observed=True)["_latest_flag"].transform("any")
        candidates = candidates.loc[~has_latest | candidates["_latest_flag"]].copy()
    ambiguous = candidates.groupby(keys, observed=True).size()
    ambiguous = ambiguous.loc[ambiguous > 1]
    if not ambiguous.empty:
        sample = [tuple(value) for value in ambiguous.index[:5]]
        raise FundamentalPITError(
            f"ambiguous financial versions at decision time: {sample}"
        )
    selected = candidates.drop(columns=["_latest_flag"], errors="ignore")
    if latest_report_only:
        latest_period = selected.groupby("instrument_id", observed=True)[
            "report_period"
        ].transform("max")
        selected = selected.loc[selected["report_period"].eq(latest_period)].copy()
    selected["decision_at"] = decision
    selected["research_as_of_at"] = research_as_of
    local_decision = decision.tz_convert("Asia/Shanghai").tz_localize(None).normalize()
    selected["report_age_days"] = (
        local_decision - selected["report_period"]
    ).dt.days
    return selected.sort_values(["instrument_id", "report_period"]).reset_index(drop=True)


def _curate_statement(
    frame: pd.DataFrame,
    statement: str,
    calendar: pd.DataFrame,
    research_as_of_at: pd.Timestamp,
    aliases: Mapping[str, str],
) -> pd.DataFrame:
    required = {
        "symbol",
        "statement_type",
        "report_period",
        "announcement_date",
        "available_date",
        "source_row_sha256",
        "source_row_occurrence",
        "source",
        "ingested_at",
    }
    missing = sorted(required - set(frame.columns))
    if missing:
        raise FundamentalPITError(f"{statement} raw rows missing columns: {missing}")
    result = frame.copy()
    if "instrument_kind" in result:
        result = result.loc[result["instrument_kind"].eq("stock")].copy()
    if not result["statement_type"].eq(statement).all():
        raise FundamentalPITError(f"{statement} raw rows contain mixed statement types")
    result["report_period"] = pd.to_datetime(
        result["report_period"], errors="coerce"
    ).dt.normalize()
    result["announcement_date"] = pd.to_datetime(
        result["announcement_date"], errors="coerce"
    ).dt.normalize()
    result["actual_announcement_date"] = pd.to_datetime(
        result.get("actual_announcement_date"), errors="coerce"
    ).dt.normalize()
    result["available_date"] = result["actual_announcement_date"].fillna(
        result["announcement_date"]
    )
    if result[["report_period", "available_date"]].isna().any(axis=None):
        raise FundamentalPITError(f"{statement} contains rows without causal dates")
    result["source_ingested_at"] = pd.to_datetime(
        result.pop("ingested_at"), utc=True, errors="coerce"
    )
    if result["source_ingested_at"].isna().any():
        raise FundamentalPITError(f"{statement} contains invalid ingestion timestamps")
    if (result["source_ingested_at"] > research_as_of_at).any():
        raise FundamentalPITError(f"{statement} contains rows after the frozen data vintage")
    result = attach_next_session_availability(
        result,
        "available_date",
        calendar,
        f"fundamentals_{statement}",
    )
    local_event = result["available_date"] + pd.Timedelta(
        hours=23, minutes=59, seconds=59
    )
    result["announcement_at"] = local_event.dt.tz_localize(
        "Asia/Shanghai"
    ).dt.tz_convert("UTC")
    result["instrument_id"] = result["symbol"].map(
        lambda symbol: aliases.get(str(symbol), f"CN_EQ:{symbol}")
    )
    result["identity_alias_resolved"] = result["symbol"].astype(str).isin(aliases)
    result["report_type"] = result.get(
        "report_type", pd.Series(pd.NA, index=result.index, dtype="string")
    ).astype("string")
    result["company_type"] = result.get(
        "comp_type", pd.Series(pd.NA, index=result.index, dtype="string")
    ).astype("string")
    result["period_type"] = result.get(
        "end_type", pd.Series(pd.NA, index=result.index, dtype="string")
    ).astype("string")
    result["update_flag"] = result.get(
        "update_flag", pd.Series(pd.NA, index=result.index, dtype="string")
    ).astype("string")
    result["version_id"] = result.apply(_version_id, axis=1)
    result["research_as_of_at"] = research_as_of_at
    result["pit_policy_version"] = PIT_POLICY_VERSION
    if result.duplicated("version_id").any():
        raise FundamentalPITError(f"{statement} contains duplicate version identities")
    return result.sort_values(
        ["instrument_id", "report_period", "available_at", "version_id"]
    ).reset_index(drop=True)


def _concat_statement_parts(parts: Sequence[pd.DataFrame]) -> pd.DataFrame:
    """Preserve the union schema without relying on deprecated dtype inference."""
    columns = list(dict.fromkeys(column for part in parts for column in part.columns))
    informative = [part.dropna(axis="columns", how="all") for part in parts]
    combined = pd.concat(informative, ignore_index=True, sort=False)
    return combined.reindex(columns=columns).copy()


def _version_id(row: pd.Series) -> str:
    payload = {
        "instrument_id": row["instrument_id"],
        "statement_type": row["statement_type"],
        "source_row_sha256": row["source_row_sha256"],
        "source_row_occurrence": int(row["source_row_occurrence"]),
        "available_at": row["available_at"].isoformat(),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _version_index(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.reindex(columns=INDEX_COLUMNS).copy()


def _empty_curated_statement() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            *INDEX_COLUMNS,
            "research_as_of_at",
            "availability_policy",
            "pit_policy_version",
        ]
    )


def _load_aliases(
    aliases_path: Optional[Path],
    security_code_mappings_path: Optional[Path],
) -> tuple[Dict[str, str], Dict[str, Any]]:
    aliases: Dict[str, str] = {}
    identity: Dict[str, Any] = {}
    if aliases_path is not None:
        path = Path(aliases_path).resolve()
        payload = json.loads(path.read_text(encoding="utf-8"))
        for alias in payload.get("aliases", []):
            stable = str(alias["stable_instrument_id"])
            aliases[str(alias["historical_symbol"])] = stable
            aliases[str(alias["current_symbol"])] = stable
        identity["reviewed_aliases"] = {"sha256": _sha256(path)}
    if security_code_mappings_path is not None:
        path = Path(security_code_mappings_path).resolve()
        mappings = pd.read_parquet(path)
        required = {"historical_symbol", "current_symbol"}
        missing = sorted(required - set(mappings.columns))
        if missing:
            raise FundamentalPITError(f"security-code mappings missing columns: {missing}")
        for row in mappings.to_dict("records"):
            current = str(row["current_symbol"])
            stable = f"CN_EQ:BSE:{current}"
            aliases[str(row["historical_symbol"])] = stable
            aliases[current] = stable
        identity["security_code_mappings"] = {
            "sha256": _sha256(path),
        }
    return aliases, identity


def _lake_manifest_by_path(lake_root: Path) -> Dict[str, Dict[str, Any]]:
    path = Path(lake_root) / "manifest.jsonl"
    if not path.exists():
        raise FundamentalPITError(f"lake manifest does not exist: {path}")
    entries: Dict[str, Dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        entry = json.loads(line)
        entries[str(entry.get("path"))] = entry
    return entries


def _validate_calendar(calendar: pd.DataFrame) -> None:
    missing = sorted({"calendar_date", "is_open"} - set(calendar.columns))
    if missing:
        raise FundamentalPITError(f"trading calendar missing columns: {missing}")
    dates = pd.to_datetime(
        calendar.loc[calendar["is_open"].astype(bool), "calendar_date"],
        errors="coerce",
    )
    if dates.isna().any() or dates.empty:
        raise FundamentalPITError("trading calendar has no valid open sessions")


def _write_immutable_parquet(
    frame: pd.DataFrame, path: Path, sort_columns: Iterable[str]
) -> None:
    columns = [column for column in sort_columns if column in frame.columns]
    normalized = frame.sort_values(columns).reset_index(drop=True) if columns else frame
    expected = _frame_fingerprint(normalized)
    if path.exists():
        if _frame_fingerprint(pd.read_parquet(path)) != expected:
            raise FundamentalPITError(f"existing artifact content mismatch: {path}")
        return
    temporary = path.parent / f".{path.name}.{os.getpid()}.tmp"
    normalized.to_parquet(temporary, index=False, engine="pyarrow")
    os.replace(temporary, path)


def _write_immutable_json(payload: Mapping[str, Any], path: Path) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") != text:
            raise FundamentalPITError(f"existing artifact manifest mismatch: {path}")
        return
    temporary = path.parent / f".{path.name}.{os.getpid()}.tmp"
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _output_identity(path: Path, frame: pd.DataFrame) -> Dict[str, Any]:
    return {
        "path": path.name,
        "rows": len(frame),
        "sha256": _sha256(path),
        "logical_sha256": _frame_fingerprint(frame),
    }


def _frame_fingerprint(frame: pd.DataFrame) -> str:
    hashed = pd.util.hash_pandas_object(frame, index=False).values.tobytes()
    return hashlib.sha256(hashed).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _implementation_identity() -> Dict[str, Any]:
    files = [
        Path(__file__),
        Path(__file__).with_name("fundamental_ingestion.py"),
        Path(__file__).with_name("contracts.py"),
        Path(__file__).with_name("temporal.py"),
    ]
    entries = []
    digest = hashlib.sha256()
    for path in files:
        file_hash = _sha256(path)
        entries.append({"path": path.name, "sha256": file_hash})
        digest.update(path.name.encode("utf-8"))
        digest.update(file_hash.encode("ascii"))
    return {"tree_sha256": digest.hexdigest(), "files": entries}


def _utc_timestamp(value: Any) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    return timestamp.tz_convert("UTC") if timestamp.tzinfo else timestamp.tz_localize("UTC")


def _date_or_none(series: Optional[pd.Series], method: str) -> Optional[str]:
    if series is None or len(series) == 0:
        return None
    values = pd.to_datetime(series, errors="coerce").dropna()
    if values.empty:
        return None
    return str(getattr(values, method)().date())


def _timestamp_or_none(series: Optional[pd.Series], method: str) -> Optional[str]:
    if series is None or len(series) == 0:
        return None
    values = pd.to_datetime(series, utc=True, errors="coerce").dropna()
    if values.empty:
        return None
    return getattr(values, method)().isoformat()
