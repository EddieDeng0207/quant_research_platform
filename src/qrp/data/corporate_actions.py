"""Point-in-time corporate-action events for raw-price portfolio accounting."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

import numpy as np
import pandas as pd


class CorporateActionError(RuntimeError):
    """Raised when corporate actions cannot be made causal and auditable."""


CANONICAL_COLUMNS = [
    "action_id",
    "source_action_id",
    "instrument_id",
    "symbol",
    "action_type",
    "report_period",
    "announcement_at",
    "available_at",
    "record_date",
    "ex_date",
    "pay_date",
    "share_ratio",
    "cash_per_share",
    "withholding_tax_rate",
    "source",
    "source_ingested_at",
    "policy_version",
]


def build_corporate_action_events(
    raw_actions: pd.DataFrame,
    tradability: pd.DataFrame,
) -> tuple[pd.DataFrame, Dict[str, Any]]:
    """Select implemented records and split cash/share legs without future pricing data."""
    required_raw = {
        "source_action_id",
        "symbol",
        "report_period",
        "announcement_date",
        "process_status",
        "record_date",
        "ex_date",
        "pay_date",
        "cash_per_share_tax",
        "bonus_share_ratio",
        "source",
        "ingested_at",
    }
    missing = sorted(required_raw - set(raw_actions.columns))
    if missing:
        raise CorporateActionError(f"raw corporate actions missing columns: {missing}")
    required_market = {"symbol", "instrument_id", "trade_date"}
    missing_market = sorted(required_market - set(tradability.columns))
    if missing_market:
        raise CorporateActionError(
            f"tradability artifact missing columns: {missing_market}"
        )

    market = tradability[list(required_market)].copy()
    market["trade_date"] = pd.to_datetime(market["trade_date"]).dt.normalize()
    if market.duplicated(["symbol", "trade_date"]).any():
        raise CorporateActionError("tradability has duplicate symbol-date identity rows")
    calendar = pd.DatetimeIndex(market["trade_date"].unique()).sort_values()
    if calendar.empty:
        raise CorporateActionError("tradability calendar is empty")

    raw = raw_actions.copy()
    for column in [
        "report_period",
        "announcement_date",
        "implementation_announcement_date",
        "record_date",
        "ex_date",
        "pay_date",
    ]:
        if column not in raw:
            raw[column] = pd.NaT
        raw[column] = pd.to_datetime(raw[column], errors="coerce").dt.normalize()
    raw["ingested_at"] = pd.to_datetime(raw["ingested_at"], utc=True, errors="coerce")
    for column in ["cash_per_share_tax", "bonus_share_ratio"]:
        raw[column] = pd.to_numeric(raw[column], errors="coerce")

    implemented = raw.loc[
        raw["process_status"].fillna("").astype(str).str.contains("实施")
    ].copy()
    implemented["knowledge_date"] = implemented[
        "implementation_announcement_date"
    ].combine_first(implemented["announcement_date"])
    implemented = implemented.sort_values(
        [
            "symbol",
            "report_period",
            "knowledge_date",
            "announcement_date",
            "ingested_at",
            "source_action_id",
        ]
    ).drop_duplicates(["symbol", "report_period"], keep="last")

    event_rows = []
    invalid_effective_rows = 0
    unknown_instrument_rows = 0
    late_knowledge_rows = 0
    outside_window_rows = 0
    for row in implemented.to_dict("records"):
        ex_date = row["ex_date"]
        if pd.isna(ex_date):
            invalid_effective_rows += 1
            continue
        ex_session = _covered_session(ex_date, calendar)
        if ex_session is None:
            outside_window_rows += 1
            continue
        identity = market.loc[
            (market["symbol"] == row["symbol"])
            & (market["trade_date"] == ex_session),
            "instrument_id",
        ]
        if len(identity) != 1:
            unknown_instrument_rows += 1
            continue
        knowledge_date = row["knowledge_date"]
        if pd.isna(knowledge_date):
            late_knowledge_rows += 1
            continue
        if knowledge_date >= ex_session:
            late_knowledge_rows += 1
            continue
        announcement_at = _local_timestamp(knowledge_date, 16, 0)
        available_session = _next_session_after(knowledge_date, calendar)
        available_at = _local_timestamp(available_session, 8, 40)
        ex_open = _local_timestamp(ex_session, 9, 30)
        if available_at > ex_open:
            late_knowledge_rows += 1
            continue
        base = {
            "source_action_id": str(row["source_action_id"]),
            "instrument_id": str(identity.iloc[0]),
            "symbol": str(row["symbol"]),
            "report_period": row["report_period"],
            "announcement_at": announcement_at,
            "available_at": available_at,
            "record_date": row["record_date"],
            "ex_date": row["ex_date"],
            "pay_date": row["pay_date"],
            "source": str(row["source"]),
            "source_ingested_at": row["ingested_at"],
            "policy_version": "tushare_implemented_dividend_pit_v1",
        }
        cash = row["cash_per_share_tax"]
        if pd.notna(cash) and float(cash) > 0:
            if pd.isna(row["record_date"]) or pd.isna(row["pay_date"]):
                invalid_effective_rows += 1
            elif _covered_session(row["record_date"], calendar) is None:
                outside_window_rows += 1
            else:
                event_rows.append(
                    {
                        **base,
                        "action_id": f"{row['source_action_id']}:cash",
                        "action_type": "cash_dividend",
                        "share_ratio": np.nan,
                        "cash_per_share": float(cash),
                        # Gross cash is frozen; investor-specific tax belongs in a
                        # separately versioned tax policy rather than an implicit guess.
                        "withholding_tax_rate": 0.0,
                    }
                )
        bonus = row["bonus_share_ratio"]
        if pd.notna(bonus) and float(bonus) > 0:
            event_rows.append(
                {
                    **base,
                    "action_id": f"{row['source_action_id']}:bonus",
                    "action_type": "bonus",
                    "share_ratio": 1.0 + float(bonus),
                    "cash_per_share": np.nan,
                    "withholding_tax_rate": 0.0,
                }
            )

    events = pd.DataFrame(event_rows, columns=CANONICAL_COLUMNS)
    if not events.empty:
        events = events.sort_values(["ex_date", "symbol", "action_id"]).reset_index(
            drop=True
        )
    duplicate_actions = int(events.duplicated("action_id").sum()) if not events.empty else 0
    hard_failures = {
        "implemented_rows_with_invalid_effective_dates": invalid_effective_rows,
        "implemented_rows_with_unknown_instrument": unknown_instrument_rows,
        "implemented_rows_known_after_ex_open": late_knowledge_rows,
        "duplicate_canonical_action_ids": duplicate_actions,
    }
    quality = {
        "raw_rows": len(raw),
        "implemented_latest_rows": len(implemented),
        "canonical_action_rows": len(events),
        "cash_dividend_rows": int((events["action_type"] == "cash_dividend").sum())
        if not events.empty
        else 0,
        "bonus_rows": int((events["action_type"] == "bonus").sum())
        if not events.empty
        else 0,
        "nonimplementation_rows_excluded": len(raw) - len(raw.loc[
            raw["process_status"].fillna("").astype(str).str.contains("实施")
        ]),
        "outside_backtest_window_rows_excluded": outside_window_rows,
        "hard_failures": hard_failures,
        "promotion_passed": all(value == 0 for value in hard_failures.values()),
    }
    return events, quality


def build_corporate_action_artifact(
    raw_paths: Sequence[Path],
    tradability_artifact: Path,
    output_root: Path,
    *,
    strict: bool = True,
    queried_symbols: Optional[Sequence[str]] = None,
    query_run_identity: Optional[Dict[str, Any]] = None,
) -> Path:
    """Build an immutable canonical action artifact from explicit raw snapshots."""
    paths = [Path(path) for path in raw_paths]
    if not paths:
        raise CorporateActionError("at least one raw corporate-action file is required")
    p05_root = Path(tradability_artifact)
    manifest_path = p05_root / "manifest.json"
    market_path = p05_root / "tradability.parquet"
    if not manifest_path.exists() or not market_path.exists():
        raise CorporateActionError("P0.5 artifact is incomplete")
    p05_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not p05_manifest.get("quality", {}).get("promotion_passed", False):
        raise CorporateActionError("P0.5 artifact did not pass promotion")
    if _sha256(market_path) != p05_manifest.get("output", {}).get("sha256"):
        raise CorporateActionError("P0.5 Parquet hash does not match its manifest")
    if query_run_identity is not None and (
        query_run_identity.get("universe_artifact_id") != p05_manifest.get("artifact_id")
        or query_run_identity.get("universe_manifest_sha256") != _sha256(manifest_path)
    ):
        raise CorporateActionError(
            "corporate-action query universe does not match the requested P0.5 artifact"
        )
    for path in paths:
        if not path.exists():
            raise CorporateActionError(f"raw corporate-action file not found: {path}")
    raw_frames = [pd.read_parquet(path) for path in paths]
    empty_raw_snapshot_files = sum(frame.empty for frame in raw_frames)
    raw = pd.concat(raw_frames, ignore_index=True)
    raw = raw.sort_values(["source_action_id", "ingested_at"]).drop_duplicates(
        "source_action_id", keep="last"
    )
    market = pd.read_parquet(market_path)
    events, quality = build_corporate_action_events(raw, market)
    p05_symbols = set(market["symbol"].dropna().astype(str))
    frozen_queries = set(str(value) for value in (queried_symbols or ()))
    if queried_symbols is None:
        missing_query_symbols: list[str] = []
        query_coverage = None
    else:
        missing_query_symbols = sorted(p05_symbols - frozen_queries)
        query_coverage = (
            len(p05_symbols & frozen_queries) / len(p05_symbols) if p05_symbols else 0.0
        )
        quality["hard_failures"]["unqueried_p05_symbols"] = len(
            missing_query_symbols
        )
        quality["promotion_passed"] = all(
            value == 0 for value in quality["hard_failures"].values()
        )
    quality.update(
        {
            "p05_universe_symbols": len(p05_symbols),
            "queried_symbols": len(frozen_queries) if queried_symbols is not None else None,
            "query_coverage": query_coverage,
            "query_coverage_proven": queried_symbols is not None,
            "missing_query_symbol_sample": missing_query_symbols[:20],
            "raw_snapshot_files": len(paths),
            "empty_raw_snapshot_files": empty_raw_snapshot_files,
        }
    )
    identity = {
        "p05_artifact_id": p05_manifest["artifact_id"],
        "p05_manifest_sha256": _sha256(manifest_path),
        "raw_inputs": [
            {"path": str(path), "sha256": _sha256(path)} for path in sorted(paths)
        ],
        "policy_version": "tushare_implemented_dividend_pit_v1",
        "implementation_sha256": _sha256(Path(__file__)),
        "query_run": query_run_identity,
    }
    artifact_id = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:20]
    destination = Path(output_root) / "corporate_actions" / f"artifact_id={artifact_id}"
    destination.mkdir(parents=True, exist_ok=True)
    output = destination / "corporate_actions.parquet"
    logical_sha = _frame_fingerprint(events)
    _write_immutable_parquet(events, output, logical_sha)
    manifest = {
        "artifact_id": artifact_id,
        "schema_version": "p062_corporate_actions_v1",
        "identity": identity,
        "quality": quality,
        "output": {"path": str(output), "rows": len(events), "sha256": _sha256(output)},
        "guardrails": {
            "implementation_records_only": True,
            "latest_version_per_symbol_period": True,
            "knowledge_time_precedes_ex_open": True,
            "cash_and_share_legs_are_separate": True,
            "gross_dividend_tax_policy_explicit": True,
            "instrument_identity_from_promoted_p05": True,
            "full_universe_query_coverage_proven": queried_symbols is not None,
        },
    }
    _write_immutable_json(manifest, destination / "manifest.json")
    if strict and not quality["promotion_passed"]:
        raise CorporateActionError(
            f"corporate-action artifact failed promotion at {destination}: "
            f"{quality['hard_failures']}"
        )
    return destination


def build_corporate_action_artifact_from_ingestion_run(
    ingestion_run: Path,
    tradability_artifact: Path,
    output_root: Path,
    *,
    strict: bool = True,
) -> Path:
    """Build a canonical artifact only after proving every P0.5 symbol was queried."""
    from .corporate_action_ingestion import load_completed_corporate_action_run

    paths, symbols, identity = load_completed_corporate_action_run(ingestion_run)
    return build_corporate_action_artifact(
        paths,
        tradability_artifact,
        output_root,
        strict=strict,
        queried_symbols=symbols,
        query_run_identity=identity,
    )


def _covered_session(date: Any, calendar: pd.DatetimeIndex) -> Optional[pd.Timestamp]:
    normalized = pd.Timestamp(date).normalize()
    if normalized < calendar.min() or normalized > calendar.max():
        return None
    position = calendar.searchsorted(normalized, side="left")
    return calendar[position] if position < len(calendar) else None


def _next_session_after(date: Any, calendar: pd.DatetimeIndex) -> pd.Timestamp:
    normalized = pd.Timestamp(date).normalize()
    if normalized < calendar.min():
        return calendar.min()
    position = calendar.searchsorted(normalized, side="right")
    if position >= len(calendar):
        raise CorporateActionError(
            f"calendar does not cover availability after {normalized.date()}"
        )
    return calendar[position]


def _local_timestamp(date: Any, hour: int, minute: int) -> pd.Timestamp:
    return (
        (pd.Timestamp(date).normalize() + pd.Timedelta(hours=hour, minutes=minute))
        .tz_localize("Asia/Shanghai")
        .tz_convert("UTC")
    )


def _frame_fingerprint(frame: pd.DataFrame) -> str:
    normalized = frame.sort_values("action_id").reset_index(drop=True) if not frame.empty else frame
    return hashlib.sha256(
        pd.util.hash_pandas_object(normalized, index=False).values.tobytes()
    ).hexdigest()


def _write_immutable_parquet(frame: pd.DataFrame, path: Path, logical_sha: str) -> None:
    if path.exists():
        if _frame_fingerprint(pd.read_parquet(path)) != logical_sha:
            raise CorporateActionError(f"immutable corporate-action conflict: {path}")
        return
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    frame.to_parquet(temporary, index=False)
    os.replace(temporary, path)


def _write_immutable_json(payload: Any, path: Path) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") != encoded:
        raise CorporateActionError(f"immutable corporate-action manifest conflict: {path}")
    if not path.exists():
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(encoded, encoding="utf-8")
        os.replace(temporary, path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
