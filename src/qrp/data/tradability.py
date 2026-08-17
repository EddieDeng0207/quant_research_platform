"""Conservative A-share execution eligibility derived from frozen daily snapshots."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

import numpy as np
import pandas as pd

from .catalog import load_latest_snapshot, load_partitioned_snapshot


class TradabilityError(RuntimeError):
    """Raised when the execution eligibility layer is incomplete or inconsistent."""


@dataclass(frozen=True)
class TradabilitySpec:
    price_tolerance: float = 0.0051
    block_any_suspension_event: bool = True
    stock_status_history_start: str = "2016-01-01"
    version: str = "a_share_daily_tradability_v6_reviewed_state_machine"

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_tradability_matrix(
    bars: pd.DataFrame,
    limits: pd.DataFrame,
    suspensions: pd.DataFrame,
    stock_status: pd.DataFrame,
    instruments: pd.DataFrame,
    trading_dates: Sequence[pd.Timestamp],
    historical_instruments: Optional[pd.DataFrame] = None,
    symbol_aliases: Optional[Sequence[Dict[str, Any]]] = None,
    security_code_mappings: Optional[pd.DataFrame] = None,
    reviewed_market_events: Optional[Dict[str, Any]] = None,
    historical_bars_context: Optional[pd.DataFrame] = None,
    historical_suspensions_context: Optional[pd.DataFrame] = None,
    suspension_state_seed: Optional[Dict[str, bool]] = None,
    spec: Optional[TradabilitySpec] = None,
) -> pd.DataFrame:
    """Build an execution-only matrix; uncertain states are never tradable."""
    spec = spec or TradabilitySpec()
    dates = pd.DatetimeIndex(pd.to_datetime(list(trading_dates))).normalize().unique().sort_values()
    if dates.empty:
        raise TradabilityError("Trading date set is empty")
    if dates.min() < pd.Timestamp(spec.stock_status_history_start):
        raise TradabilityError(
            "Tushare stock_st has no authoritative history before "
            f"{spec.stock_status_history_start}; configure a reviewed fallback source"
        )
    reviewed = reviewed_market_events or _empty_reviewed_market_events()
    _validate_reviewed_market_events(reviewed)
    universe, normalized_pre_bse_open_rows = _historical_universe(
        instruments, dates, historical_instruments
    )
    universe, residual_pre_bse_open_rows = _exclude_pre_bse_open_universe(universe)
    pre_bse_open_universe_rows = normalized_pre_bse_open_rows + residual_pre_bse_open_rows
    bse_aliases = _bse_symbol_aliases(security_code_mappings)
    universe = _supplement_reviewed_bse_universe(universe, instruments, dates, bse_aliases)
    aliases = [*list(symbol_aliases or []), *bse_aliases]
    _validate_symbol_aliases(aliases)
    universe = _apply_symbol_aliases(universe, aliases, "source_universe_symbol")
    bars_clean = _apply_symbol_aliases(_prepare_bars(bars), aliases, "source_bar_symbol")
    bars_clean, pre_bse_listing_bar_rows = _exclude_pre_bse_listing_bars(
        bars_clean, instruments, bse_aliases
    )
    universe = _supplement_reviewed_lifecycle_gaps(universe, bars_clean, instruments)
    universe, reviewed_listing_episode_rows = _exclude_reviewed_listing_episodes(
        universe, bars_clean, reviewed
    )
    universe, untraded_delist_effective_rows = _exclude_untraded_delist_effective_dates(
        universe, bars_clean
    )
    limits_clean = _apply_symbol_aliases(_prepare_limits(limits), aliases, "source_limit_symbol")
    limits_clean, reviewed_unbounded_limit_rows = _apply_reviewed_unbounded_limits(
        limits_clean, bars_clean, reviewed
    )
    suspension_flags = _apply_symbol_aliases(
        _prepare_suspensions(suspensions), aliases, "source_suspension_symbol"
    )
    status_flags = _apply_symbol_aliases(
        _prepare_status(stock_status), aliases, "source_status_symbol"
    )
    universe = _attach_instrument_ids(universe, aliases)
    outside_universe = bars_clean.merge(
        universe[["symbol", "trade_date"]],
        on=["symbol", "trade_date"],
        how="left",
        indicator=True,
    )
    outside_universe = outside_universe.loc[outside_universe["_merge"] == "left_only"]
    if not outside_universe.empty:
        sample = outside_universe[["symbol", "trade_date"]].head(10).to_dict("records")
        raise TradabilityError(f"Daily bars fall outside the historical universe: {sample}")

    suspension_seed = (
        dict(suspension_state_seed)
        if suspension_state_seed is not None
        else _build_suspension_state_seed(
            historical_bars_context,
            historical_suspensions_context,
            instruments,
            aliases,
            bse_aliases,
        )
    )
    matrix = (
        universe.merge(bars_clean, on=["symbol", "trade_date"], how="left", validate="one_to_one")
        .merge(limits_clean, on=["symbol", "trade_date"], how="left", validate="one_to_one")
        .merge(suspension_flags, on=["symbol", "trade_date"], how="left", validate="one_to_one")
        .merge(status_flags, on=["symbol", "trade_date"], how="left", validate="one_to_one")
    )
    boolean_defaults = [
        "has_suspension_event",
        "is_suspended",
        "is_resumption",
        "is_st",
    ]
    for column in boolean_defaults:
        matrix[column] = matrix[column].eq(True)
    matrix["has_bar"] = matrix["close"].notna()
    matrix = _apply_suspension_state_machine(matrix, suspension_seed)
    matrix = _apply_reviewed_nontrading_intervals(matrix, reviewed)
    matrix["has_limit_record"] = matrix["up_limit"].notna() & matrix["down_limit"].notna()
    matrix["has_bounded_price_limit"] = matrix["has_limit_record"] & (
        matrix["price_limit_regime"] == "bounded"
    )
    matrix["unexplained_missing_bar"] = ~matrix["has_bar"] & ~matrix["is_suspended"]
    matrix["bar_without_limit"] = matrix["has_bar"] & ~matrix["has_limit_record"]
    matrix["partial_or_intraday_suspension"] = matrix["is_suspended"] & matrix["has_bar"]

    comparable = matrix["has_bar"] & matrix["has_bounded_price_limit"]
    tolerance = spec.price_tolerance
    matrix["price_below_down_limit"] = comparable & (
        matrix["low"] < matrix["down_limit"] - tolerance
    )
    matrix["price_above_up_limit"] = comparable & (matrix["high"] > matrix["up_limit"] + tolerance)
    matrix["pre_close_mismatch"] = (
        comparable
        & matrix["bar_pre_close"].notna()
        & matrix["limit_pre_close"].notna()
        & ((matrix["bar_pre_close"] - matrix["limit_pre_close"]).abs() > tolerance)
    )
    matrix["data_complete"] = (
        ~matrix["unexplained_missing_bar"]
        & ~matrix["bar_without_limit"]
        & ~matrix["price_below_down_limit"]
        & ~matrix["price_above_up_limit"]
    )

    matrix["open_at_up_limit"] = comparable & (matrix["open"] >= matrix["up_limit"] - tolerance)
    matrix["open_at_down_limit"] = comparable & (matrix["open"] <= matrix["down_limit"] + tolerance)
    matrix["one_price_limit_up"] = comparable & (matrix["low"] >= matrix["up_limit"] - tolerance)
    matrix["one_price_limit_down"] = comparable & (
        matrix["high"] <= matrix["down_limit"] + tolerance
    )
    suspended_block = (
        matrix["is_suspended"]
        if spec.block_any_suspension_event
        else pd.Series(False, index=matrix.index)
    )
    common = matrix["data_complete"] & matrix["has_bar"] & ~suspended_block
    matrix["can_buy_at_open"] = common & ~matrix["open_at_up_limit"]
    matrix["can_sell_at_open"] = common & ~matrix["open_at_down_limit"]
    matrix["can_buy_during_day"] = common & ~matrix["one_price_limit_up"]
    matrix["can_sell_during_day"] = common & ~matrix["one_price_limit_down"]
    matrix["can_mark_to_market"] = matrix["has_bar"]
    matrix["valuation_method"] = np.where(
        matrix["has_bar"], "observed_close", "carry_forward_prior_close"
    )
    matrix["standard_research_eligible"] = (
        matrix["data_complete"] & matrix["has_bar"] & ~matrix["is_suspended"] & ~matrix["is_st"]
    )
    matrix["buy_block_reason"] = _block_reason(matrix, "buy")
    matrix["sell_block_reason"] = _block_reason(matrix, "sell")
    local_dates = matrix["trade_date"] + pd.Timedelta(hours=16)
    matrix["available_at"] = local_dates.dt.tz_localize("Asia/Shanghai").dt.tz_convert("UTC")
    opens = matrix["trade_date"] + pd.Timedelta(hours=9, minutes=30)
    matrix["execution_event_at"] = opens.dt.tz_localize("Asia/Shanghai").dt.tz_convert("UTC")
    matrix["availability_policy"] = "tradability_matrix:execution_observation_close_v1"
    matrix["execution_only"] = True
    matrix["research_feature_allowed"] = False
    matrix["tradability_version"] = spec.version
    matrix["tradability_spec_sha256"] = spec.fingerprint
    matrix.attrs["pre_bse_listing_bar_rows_excluded"] = pre_bse_listing_bar_rows
    matrix.attrs["pre_bse_open_universe_rows_excluded"] = pre_bse_open_universe_rows
    matrix.attrs["reviewed_listing_episode_rows_excluded"] = reviewed_listing_episode_rows
    matrix.attrs["untraded_delist_effective_rows_excluded"] = untraded_delist_effective_rows
    matrix.attrs["reviewed_unbounded_limit_rows"] = reviewed_unbounded_limit_rows
    return matrix.sort_values(["trade_date", "symbol"]).reset_index(drop=True)


def tradability_quality_summary(frame: pd.DataFrame) -> Dict[str, Any]:
    hard_failures = {
        "unexplained_missing_bar_rows": int(frame["unexplained_missing_bar"].sum()),
        "bar_without_limit_rows": int(frame["bar_without_limit"].sum()),
        "price_below_down_limit_rows": int(frame["price_below_down_limit"].sum()),
        "price_above_up_limit_rows": int(frame["price_above_up_limit"].sum()),
    }
    return {
        "rows": len(frame),
        "symbols": int(frame["symbol"].nunique()),
        "trading_dates": int(frame["trade_date"].nunique()),
        "start_date": str(frame["trade_date"].min().date()),
        "end_date": str(frame["trade_date"].max().date()),
        "bar_rows": int(frame["has_bar"].sum()),
        "suspended_rows": int(frame["is_suspended"].sum()),
        "st_rows": int(frame["is_st"].sum()),
        "open_limit_up_rows": int(frame["open_at_up_limit"].sum()),
        "open_limit_down_rows": int(frame["open_at_down_limit"].sum()),
        "one_price_limit_up_rows": int(frame["one_price_limit_up"].sum()),
        "one_price_limit_down_rows": int(frame["one_price_limit_down"].sum()),
        "unbounded_price_limit_rows": int(
            (frame["has_limit_record"] & ~frame["has_bounded_price_limit"]).sum()
        ),
        "partial_or_intraday_suspension_rows": int(frame["partial_or_intraday_suspension"].sum()),
        "possible_corporate_action_rows": int(frame["pre_close_mismatch"].sum()),
        "master_record_missing_rows": int((~frame["master_record_present"]).sum()),
        "identity_alias_resolved_rows": int(frame["identity_alias_resolved"].sum()),
        "bse_historical_code_restored_rows": int(
            (
                frame["identity_alias_resolved"]
                & (frame["identity_alias_policy_version"] == "bse_920_transition_v1")
            ).sum()
        ),
        "multi_source_symbol_rows": int(
            frame["source_bar_symbol"].fillna("").str.contains(" | ", regex=False).sum()
        ),
        "pre_bse_listing_bar_rows_excluded": int(
            frame.attrs.get("pre_bse_listing_bar_rows_excluded", 0)
        ),
        "pre_bse_open_universe_rows_excluded": int(
            frame.attrs.get("pre_bse_open_universe_rows_excluded", 0)
        ),
        "reviewed_listing_episode_rows_excluded": int(
            frame.attrs.get("reviewed_listing_episode_rows_excluded", 0)
        ),
        "untraded_delist_effective_rows_excluded": int(
            frame.attrs.get("untraded_delist_effective_rows_excluded", 0)
        ),
        "same_day_vendor_suspension_rows": int(frame["vendor_is_suspended"].sum()),
        "carried_forward_suspension_rows": int(frame["carried_forward_suspension"].sum()),
        "reviewed_nontrading_interval_rows": int(
            frame["reviewed_nontrading_interval"].sum()
        ),
        "reviewed_unbounded_limit_rows": int(
            frame.attrs.get("reviewed_unbounded_limit_rows", 0)
        ),
        "hard_failures": hard_failures,
        "promotion_passed": all(value == 0 for value in hard_failures.values()),
    }


def build_tradability_artifact(
    lake_root: Path,
    output_root: Path,
    start_date: str,
    end_date: str,
    as_of_ingested_at: Optional[str] = None,
    strict: bool = True,
    spec: Optional[TradabilitySpec] = None,
    alias_config_path: Optional[Path] = None,
    reviewed_events_path: Optional[Path] = None,
    prior_tradability_artifact: Optional[Path] = None,
) -> Path:
    """Create an immutable matrix from matching full-market daily snapshots."""
    spec = spec or TradabilitySpec()
    snapshots = {
        dataset: load_partitioned_snapshot(
            lake_root, "tushare", dataset, start_date, end_date, as_of_ingested_at
        )
        for dataset in (
            "historical_instruments",
            "daily_bars",
            "daily_limits",
            "daily_suspensions",
            "stock_status",
        )
    }
    instruments = load_latest_snapshot(lake_root, "tushare", "instruments", as_of_ingested_at)
    security_code_mappings = load_latest_snapshot(
        lake_root, "tushare", "security_code_mappings", as_of_ingested_at
    )
    alias_path = alias_config_path or (
        Path(__file__).resolve().parents[3] / "configs" / "instrument_aliases.json"
    )
    alias_bytes = Path(alias_path).read_bytes()
    alias_payload = json.loads(alias_bytes.decode("utf-8"))
    aliases = alias_payload.get("aliases", [])
    events_path = reviewed_events_path or (
        Path(__file__).resolve().parents[3] / "configs" / "p05_reviewed_market_events.json"
    )
    events_bytes = Path(events_path).read_bytes()
    reviewed_events = json.loads(events_bytes.decode("utf-8"))
    _validate_reviewed_market_events(reviewed_events)
    context_snapshots: Dict[str, Any] = {}
    prior_seed: Optional[Dict[str, bool]] = None
    prior_identity: Optional[Dict[str, Any]] = None
    context_end = pd.Timestamp(start_date).normalize() - pd.Timedelta(days=1)
    if prior_tradability_artifact is not None:
        prior_seed, prior_identity = _load_prior_tradability_seed(
            Path(prior_tradability_artifact), pd.Timestamp(start_date).normalize()
        )
    elif context_end >= pd.Timestamp(spec.stock_status_history_start):
        context_snapshots = {
            "daily_bars_context": load_partitioned_snapshot(
                lake_root,
                "tushare",
                "daily_bars",
                spec.stock_status_history_start,
                str(context_end.date()),
                as_of_ingested_at,
                columns=["symbol", "trade_date"],
            ),
            "daily_suspensions_context": load_partitioned_snapshot(
                lake_root,
                "tushare",
                "daily_suspensions",
                spec.stock_status_history_start,
                str(context_end.date()),
                as_of_ingested_at,
                columns=["symbol", "trade_date", "suspend_type", "suspend_timing"],
            ),
        }
    implementation = _implementation_identity()
    partition_dates = {
        dataset: {entry["partition_values"]["trade_date"] for entry in snapshot.manifest_entries}
        for dataset, snapshot in snapshots.items()
    }
    expected_dates = partition_dates["daily_bars"]
    mismatches = {
        dataset: sorted(expected_dates.symmetric_difference(dates))
        for dataset, dates in partition_dates.items()
        if dates != expected_dates
    }
    if mismatches:
        raise TradabilityError(f"P0.5 partition coverage mismatch: {mismatches}")
    frame = build_tradability_matrix(
        bars=snapshots["daily_bars"].frame,
        limits=snapshots["daily_limits"].frame,
        suspensions=snapshots["daily_suspensions"].frame,
        stock_status=snapshots["stock_status"].frame,
        instruments=instruments.frame,
        trading_dates=sorted(expected_dates),
        historical_instruments=snapshots["historical_instruments"].frame,
        symbol_aliases=aliases,
        security_code_mappings=security_code_mappings.frame,
        reviewed_market_events=reviewed_events,
        historical_bars_context=(
            context_snapshots["daily_bars_context"].frame if context_snapshots else None
        ),
        historical_suspensions_context=(
            context_snapshots["daily_suspensions_context"].frame
            if context_snapshots
            else None
        ),
        suspension_state_seed=prior_seed,
        spec=spec,
    )
    quality = tradability_quality_summary(frame)
    identity = {
        "start_date": start_date,
        "end_date": end_date,
        "as_of_ingested_at": as_of_ingested_at,
        "spec_sha256": spec.fingerprint,
        "instrument_aliases_sha256": hashlib.sha256(alias_bytes).hexdigest(),
        "reviewed_market_events_sha256": hashlib.sha256(events_bytes).hexdigest(),
        "implementation_sha256": implementation["tree_sha256"],
        "input_fingerprints": {
            **{dataset: snapshot.fingerprint for dataset, snapshot in snapshots.items()},
            **{
                dataset: snapshot.fingerprint
                for dataset, snapshot in context_snapshots.items()
            },
            **(
                {"prior_tradability_artifact": prior_identity["parquet_sha256"]}
                if prior_identity is not None
                else {}
            ),
            "instruments": instruments.fingerprint,
            "security_code_mappings": security_code_mappings.fingerprint,
        },
    }
    artifact_id = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:20]
    destination = Path(output_root) / "tradability" / f"artifact_id={artifact_id}"
    destination.mkdir(parents=True, exist_ok=True)
    parquet_path = destination / "tradability.parquet"
    logical_sha = _frame_fingerprint(frame)
    _write_immutable_parquet(frame, parquet_path, logical_sha)
    manifest = {
        "artifact_id": artifact_id,
        "identity": identity,
        "rows": len(frame),
        "columns": list(frame.columns),
        "logical_frame_sha256": logical_sha,
        "output": {"path": str(parquet_path), "sha256": _sha256(parquet_path)},
        "quality": quality,
        "inputs": {
            dataset: _manifest_references(snapshot.manifest_entries)
            for dataset, snapshot in {
                **snapshots,
                **context_snapshots,
                "instruments": instruments,
                "security_code_mappings": security_code_mappings,
            }.items()
        },
        "guardrails": {
            "default_deny_on_unknown": True,
            "execution_only_not_a_feature": True,
            "suspended_valuation": "carry_forward_prior_close",
            "st_automatically_excluded_from_standard_universe": True,
            "security_code_changes_require_reviewed_alias": True,
            "bse_historical_codes_restored_from_versioned_transition_policy": True,
            "bse_a_share_domain_starts_at_market_open": str(_BSE_MARKET_OPEN_DATE.date()),
            "suspension_state_carries_until_resumption_or_observed_bar": True,
            "reviewed_market_events_are_exact_symbol_date_or_interval_rules": True,
        },
        "instrument_aliases": {
            "path": str(alias_path),
            "sha256": hashlib.sha256(alias_bytes).hexdigest(),
            "version": alias_payload.get("version"),
        },
        "reviewed_market_events": {
            "path": str(events_path),
            "sha256": hashlib.sha256(events_bytes).hexdigest(),
            "version": reviewed_events.get("version"),
            "nontrading_intervals": len(reviewed_events.get("nontrading_intervals", [])),
            "listing_episode_exclusions": len(
                reviewed_events.get("listing_episode_exclusions", [])
            ),
            "unbounded_price_limit_events": len(
                reviewed_events.get("unbounded_price_limit_events", [])
            ),
        },
        "security_identity_policy": {
            "bse_policy_version": "bse_920_transition_v1",
            "new_listing_policy_start": str(_BSE_NEW_CODE_POLICY_START.date()),
            "six_security_pilot_effective_date": str(_BSE_PILOT_EFFECTIVE_DATE.date()),
            "remaining_legacy_effective_date": str(_BSE_FULL_EFFECTIVE_DATE.date()),
            "pilot_names": sorted(_BSE_PILOT_NAMES),
            "mapping_rows": len(security_code_mappings.frame),
            "sources": [
                "https://tushare.pro/document/2?doc_id=375",
                "https://www.bse.cn/company/introduce.html",
                "https://www.bse.cn/jygl_list/200021626.html",
                "https://www.bse.cn/important_news/200025603.html",
                "https://www.bse.cn/important_news/200026735.html",
            ],
        },
        "implementation": implementation,
    }
    if prior_identity is not None:
        manifest["inputs"]["prior_tradability_artifact"] = [prior_identity]
    _write_immutable_json(manifest, destination / "manifest.json")
    if strict and not quality["promotion_passed"]:
        raise TradabilityError(
            f"Tradability artifact failed promotion and was retained at {destination}: "
            f"{quality['hard_failures']}"
        )
    return destination


def _empty_reviewed_market_events() -> Dict[str, Any]:
    return {
        "version": 0,
        "nontrading_intervals": [],
        "listing_episode_exclusions": [],
        "unbounded_price_limit_events": [],
    }


def _load_prior_tradability_seed(
    artifact: Path, requested_start: pd.Timestamp
) -> tuple[Dict[str, bool], Dict[str, Any]]:
    manifest_path = artifact / "manifest.json"
    parquet_path = artifact / "tradability.parquet"
    if not manifest_path.exists() or not parquet_path.exists():
        raise TradabilityError(f"Prior P0.5 artifact is incomplete: {artifact}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not manifest.get("quality", {}).get("promotion_passed"):
        raise TradabilityError("Prior P0.5 artifact did not pass promotion")
    parquet_sha = _sha256(parquet_path)
    if parquet_sha != manifest.get("output", {}).get("sha256"):
        raise TradabilityError("Prior P0.5 artifact Parquet hash does not match its manifest")
    prior_end = pd.Timestamp(manifest.get("identity", {}).get("end_date")).normalize()
    if prior_end >= requested_start:
        raise TradabilityError(
            f"Prior P0.5 artifact ends on {prior_end.date()}, not before "
            f"{requested_start.date()}"
        )
    frame = pd.read_parquet(
        parquet_path,
        columns=["symbol", "trade_date", "has_bar", "is_suspended"],
    )
    frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.normalize()
    latest = frame.sort_values(["symbol", "trade_date"]).drop_duplicates("symbol", keep="last")
    seed = {
        str(row["symbol"]): bool(row["is_suspended"] and not row["has_bar"])
        for row in latest.to_dict("records")
    }
    return seed, {
        "artifact_id": manifest.get("artifact_id"),
        "path": str(parquet_path),
        "parquet_sha256": parquet_sha,
        "logical_frame_sha256": manifest.get("logical_frame_sha256"),
        "end_date": str(prior_end.date()),
        "seed_symbols": len(seed),
    }


def _validate_reviewed_market_events(payload: Dict[str, Any]) -> None:
    if not isinstance(payload.get("version"), int):
        raise TradabilityError("Reviewed P0.5 market-event policy requires an integer version")
    interval_fields = {"symbol", "start_date", "end_date", "classification", "evidence"}
    event_fields = {"symbol", "trade_date", "classification", "evidence"}
    interval_keys = ("nontrading_intervals", "listing_episode_exclusions")
    for key in interval_keys:
        records = payload.get(key, [])
        if not isinstance(records, list):
            raise TradabilityError(f"Reviewed P0.5 policy {key} must be a list")
        seen = set()
        for record in records:
            missing = sorted(interval_fields - set(record))
            if missing:
                raise TradabilityError(f"Reviewed P0.5 {key} record missing fields: {missing}")
            start = pd.Timestamp(record["start_date"]).normalize()
            end = pd.Timestamp(record["end_date"]).normalize()
            if start > end:
                raise TradabilityError(f"Reviewed P0.5 {key} interval is reversed: {record}")
            identity = (str(record["symbol"]), start, end)
            if identity in seen:
                raise TradabilityError(f"Duplicate reviewed P0.5 {key} interval: {identity}")
            seen.add(identity)
            if not str(record["evidence"]).startswith("https://"):
                raise TradabilityError(f"Reviewed P0.5 {key} evidence must use HTTPS")
    events = payload.get("unbounded_price_limit_events", [])
    if not isinstance(events, list):
        raise TradabilityError("Reviewed P0.5 unbounded_price_limit_events must be a list")
    seen_events = set()
    for record in events:
        missing = sorted(event_fields - set(record))
        if missing:
            raise TradabilityError(
                f"Reviewed P0.5 unbounded-price-limit event missing fields: {missing}"
            )
        identity = (str(record["symbol"]), pd.Timestamp(record["trade_date"]).normalize())
        if identity in seen_events:
            raise TradabilityError(f"Duplicate reviewed unbounded-price-limit event: {identity}")
        seen_events.add(identity)
        if not str(record["evidence"]).startswith("https://"):
            raise TradabilityError("Reviewed unbounded-price-limit evidence must use HTTPS")


def _exclude_pre_bse_open_universe(universe: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Keep pre-opening NEEQ Select snapshots outside the A-share/BSE domain."""
    mask = universe["symbol"].astype(str).str.endswith(".BJ") & (
        universe["trade_date"] < _BSE_MARKET_OPEN_DATE
    )
    return universe.loc[~mask].copy(), int(mask.sum())


def _exclude_reviewed_listing_episodes(
    universe: pd.DataFrame,
    bars: pd.DataFrame,
    reviewed: Dict[str, Any],
) -> tuple[pd.DataFrame, int]:
    excluded = pd.Series(False, index=universe.index)
    for record in reviewed.get("listing_episode_exclusions", []):
        start = pd.Timestamp(record["start_date"]).normalize()
        end = pd.Timestamp(record["end_date"]).normalize()
        excluded |= (
            universe["symbol"].eq(str(record["symbol"]))
            & universe["trade_date"].between(start, end)
        )
    if excluded.any():
        excluded_keys = universe.loc[excluded, ["symbol", "trade_date"]]
        observed = excluded_keys.merge(
            bars[["symbol", "trade_date"]],
            on=["symbol", "trade_date"],
            how="inner",
        )
        if not observed.empty:
            raise TradabilityError(
                "Reviewed listing-episode exclusion conflicts with observed bars: "
                f"{observed.head(10).to_dict('records')}"
            )
    return universe.loc[~excluded].copy(), int(excluded.sum())


def _exclude_untraded_delist_effective_dates(
    universe: pd.DataFrame, bars: pd.DataFrame
) -> tuple[pd.DataFrame, int]:
    """Exclude a delisting effective date only when no exchange bar traded that day."""
    observed = pd.MultiIndex.from_frame(bars[["symbol", "trade_date"]])
    keys = pd.MultiIndex.from_frame(universe[["symbol", "trade_date"]])
    delist_date = pd.to_datetime(universe["delist_date"], errors="coerce").dt.normalize()
    excluded = delist_date.notna() & (universe["trade_date"] >= delist_date) & ~keys.isin(observed)
    return universe.loc[~excluded].copy(), int(excluded.sum())


def _apply_reviewed_unbounded_limits(
    limits: pd.DataFrame,
    bars: pd.DataFrame,
    reviewed: Dict[str, Any],
) -> tuple[pd.DataFrame, int]:
    result = limits.copy()
    applied = 0
    for record in reviewed.get("unbounded_price_limit_events", []):
        symbol = str(record["symbol"])
        trade_date = pd.Timestamp(record["trade_date"]).normalize()
        bar_match = bars["symbol"].eq(symbol) & bars["trade_date"].eq(trade_date)
        if not bar_match.any():
            continue
        limit_match = result["symbol"].eq(symbol) & result["trade_date"].eq(trade_date)
        if limit_match.any():
            result.loc[limit_match, ["up_limit", "down_limit"]] = [99999.99, 0.0]
            result.loc[limit_match, "price_limit_regime"] = "none_reviewed_market_event"
        else:
            bar = bars.loc[bar_match].iloc[0]
            addition = pd.DataFrame(
                [
                    {
                        "symbol": symbol,
                        "trade_date": trade_date,
                        "limit_pre_close": bar.get("bar_pre_close", np.nan),
                        "up_limit": 99999.99,
                        "down_limit": 0.0,
                        "price_limit_regime": "none_reviewed_market_event",
                        "source_limit_symbol": f"reviewed_market_event:{symbol}",
                    }
                ]
            )
            result = addition if result.empty else pd.concat([result, addition], ignore_index=True)
        applied += 1
    if result.duplicated(["symbol", "trade_date"]).any():
        raise TradabilityError("Reviewed price-limit events created duplicate symbol-date keys")
    return result, applied


def _build_suspension_state_seed(
    bars_context: Optional[pd.DataFrame],
    suspensions_context: Optional[pd.DataFrame],
    instruments: pd.DataFrame,
    aliases: Sequence[Dict[str, Any]],
    bse_aliases: Sequence[Dict[str, Any]],
) -> Dict[str, bool]:
    if bars_context is None or suspensions_context is None:
        return {}
    bar_keys = bars_context[["symbol", "trade_date"]].copy()
    bar_keys["trade_date"] = pd.to_datetime(bar_keys["trade_date"]).dt.normalize()
    if bar_keys.duplicated(["symbol", "trade_date"]).any():
        raise TradabilityError("Historical bar context contains duplicate keys")
    bar_keys = _apply_symbol_aliases(bar_keys, aliases, "source_bar_symbol")
    bar_keys, _ = _exclude_pre_bse_listing_bars(bar_keys, instruments, bse_aliases)
    bar_keys["context_has_bar"] = True
    suspension_flags = _apply_symbol_aliases(
        _prepare_suspensions(suspensions_context), aliases, "source_suspension_symbol"
    )
    events = bar_keys[["symbol", "trade_date", "context_has_bar"]].merge(
        suspension_flags[["symbol", "trade_date", "is_suspended", "is_resumption"]],
        on=["symbol", "trade_date"],
        how="outer",
    )
    events["context_has_bar"] = events["context_has_bar"].eq(True)
    events["is_suspended"] = events["is_suspended"].eq(True)
    events["is_resumption"] = events["is_resumption"].eq(True)
    start = events["is_suspended"] & ~events["context_has_bar"]
    reset = events["context_has_bar"] | (events["is_resumption"] & ~start)
    events = events.loc[reset | start].copy()
    events["state_after_event"] = start.loc[events.index]
    latest = events.sort_values(["symbol", "trade_date"]).drop_duplicates(
        "symbol", keep="last"
    )
    return {
        str(row["symbol"]): bool(row["state_after_event"])
        for row in latest.to_dict("records")
    }


def _apply_suspension_state_machine(
    matrix: pd.DataFrame, initial_state: Dict[str, bool]
) -> pd.DataFrame:
    result = matrix.sort_values(["symbol", "trade_date"]).copy()
    result["vendor_is_suspended"] = result["is_suspended"].eq(True)
    changes = pd.Series(pd.NA, index=result.index, dtype="boolean")
    start = result["vendor_is_suspended"] & ~result["has_bar"]
    reset = result["has_bar"] | (result["is_resumption"] & ~start)
    changes.loc[reset] = False
    changes.loc[start] = True
    state = changes.groupby(result["symbol"], observed=True).ffill()
    seed = result["symbol"].map(initial_state).astype("boolean")
    state = state.fillna(seed).fillna(False).astype(bool)
    result["carried_forward_suspension"] = (
        state
        & ~result["vendor_is_suspended"]
        & ~result["has_bar"]
        & ~result["is_resumption"]
    )
    result["reviewed_nontrading_interval"] = False
    result["is_suspended"] = (
        result["vendor_is_suspended"] | result["carried_forward_suspension"]
    )
    result["suspension_state_source"] = np.select(
        [result["vendor_is_suspended"], result["carried_forward_suspension"]],
        ["same_day_vendor_S", "carried_forward_vendor_S"],
        default="",
    )
    return result.sort_index()


def _apply_reviewed_nontrading_intervals(
    matrix: pd.DataFrame, reviewed: Dict[str, Any]
) -> pd.DataFrame:
    result = matrix.copy()
    for record in reviewed.get("nontrading_intervals", []):
        start = pd.Timestamp(record["start_date"]).normalize()
        end = pd.Timestamp(record["end_date"]).normalize()
        applies = (
            result["symbol"].eq(str(record["symbol"]))
            & result["trade_date"].between(start, end)
            & ~result["has_bar"]
            & ~result["is_suspended"]
        )
        result.loc[applies, "reviewed_nontrading_interval"] = True
        result.loc[applies, "is_suspended"] = True
        result.loc[applies, "suspension_state_source"] = (
            "reviewed_nontrading_interval:" + str(record["classification"])
        )
    return result


def _historical_universe(
    instruments: pd.DataFrame,
    dates: pd.DatetimeIndex,
    historical_instruments: Optional[pd.DataFrame] = None,
) -> tuple[pd.DataFrame, int]:
    required = {"symbol", "name", "exchange", "list_date", "delist_date"}
    missing = sorted(required - set(instruments.columns))
    if missing:
        raise TradabilityError(f"Instrument master missing columns: {missing}")
    master = instruments.copy()
    master = master.loc[master["symbol"].astype(str).str.fullmatch(r"\d{6}\.(SH|SZ|BJ)")]
    if "list_status" in master:
        master = master.loc[master["list_status"] != "G"]
    master["list_date"] = pd.to_datetime(master["list_date"], errors="coerce").dt.normalize()
    master["delist_date"] = pd.to_datetime(master["delist_date"], errors="coerce").dt.normalize()
    bse_master_rows = master["exchange"].astype(str).eq("BSE") | master[
        "symbol"
    ].astype(str).str.endswith(".BJ")
    master.loc[bse_master_rows, "list_date"] = master.loc[
        bse_master_rows, "list_date"
    ].clip(lower=_BSE_MARKET_OPEN_DATE)
    master = master.sort_values(["symbol", "list_date"]).drop_duplicates("symbol", keep="last")
    if historical_instruments is not None:
        history = historical_instruments.copy()
        required_history = {"symbol", "trade_date", "name", "list_date"}
        missing_history = sorted(required_history - set(history.columns))
        if missing_history:
            raise TradabilityError(
                f"Historical instrument snapshots missing columns: {missing_history}"
            )
        history["trade_date"] = pd.to_datetime(history["trade_date"]).dt.normalize()
        history["list_date"] = pd.to_datetime(history["list_date"], errors="coerce").dt.normalize()
        history_bse_rows = history["symbol"].astype(str).str.endswith(".BJ")
        normalized_pre_bse_open_rows = int(
            (history_bse_rows & (history["trade_date"] < _BSE_MARKET_OPEN_DATE)).sum()
        )
        history.loc[history_bse_rows, "list_date"] = history.loc[
            history_bse_rows, "list_date"
        ].clip(lower=_BSE_MARKET_OPEN_DATE)
        history = history.loc[
            history["trade_date"].isin(dates)
            & history["symbol"].astype(str).str.fullmatch(r"\d{6}\.(SH|SZ|BJ)")
            & history["list_date"].notna()
            & (history["list_date"] <= history["trade_date"])
        ].copy()
        if history.duplicated(["symbol", "trade_date"]).any():
            raise TradabilityError("Historical instrument snapshots contain duplicate keys")
        covered_dates = set(history["trade_date"].unique())
        if covered_dates != set(dates):
            raise TradabilityError("Historical instrument snapshots do not cover every date")
        metadata = master[["symbol", "list_date", "delist_date"]].rename(
            columns={"list_date": "current_master_list_date"}
        )
        metadata["master_record_present"] = True
        history = history.merge(metadata, on="symbol", how="left", validate="many_to_one")
        history["master_record_present"] = history["master_record_present"].eq(True)
        history["exchange"] = history["symbol"].str.rsplit(".", n=1).str[-1]
        history["listed"] = True
        history["universe_source"] = "historical_daily_snapshot"
        return history[
            [
                "symbol",
                "name",
                "exchange",
                "list_date",
                "delist_date",
                "trade_date",
                "listed",
                "master_record_present",
                "universe_source",
            ]
        ], normalized_pre_bse_open_rows
    if master["list_date"].isna().any():
        sample = master.loc[master["list_date"].isna(), "symbol"].head(5).tolist()
        raise TradabilityError(f"Tradable instruments missing list_date: {sample}")
    chunks = []
    for trade_date in dates:
        eligible = master.loc[
            (master["list_date"] <= trade_date)
            & (master["delist_date"].isna() | (master["delist_date"] >= trade_date)),
            ["symbol", "name", "exchange", "list_date", "delist_date"],
        ].copy()
        eligible["trade_date"] = trade_date
        eligible["listed"] = True
        eligible["master_record_present"] = True
        eligible["universe_source"] = "current_master_date_filter_fallback"
        chunks.append(eligible)
    if not chunks:
        raise TradabilityError("Historical universe is empty")
    result = pd.concat(chunks, ignore_index=True)
    if result.duplicated(["symbol", "trade_date"]).any():
        raise TradabilityError("Historical universe contains duplicate symbol-date keys")
    return result, 0


def _supplement_reviewed_bse_universe(
    universe: pd.DataFrame,
    instruments: pd.DataFrame,
    dates: pd.DatetimeIndex,
    bse_aliases: Sequence[Dict[str, Any]],
) -> pd.DataFrame:
    """Fill vendor ``bak_basic`` BSE gaps from the all-status security master.

    Tushare's historical ``bak_basic`` snapshots omit BSE securities on some
    dates while the daily endpoint retains their bars. The authoritative
    all-status master is bounded by BSE listing/delisting dates; the frozen
    crosswalk additionally restores securities whose display code changed.
    """
    if not bse_aliases:
        return universe
    required = {"symbol", "name", "exchange", "list_date", "delist_date"}
    missing = sorted(required - set(instruments.columns))
    if missing:
        raise TradabilityError(f"Instrument master missing BSE supplement columns: {missing}")
    alias_by_current = {str(alias["current_symbol"]): alias for alias in bse_aliases}
    master = instruments.copy()
    master = master.loc[
        master["exchange"].astype(str).eq("BSE") | master["symbol"].astype(str).str.endswith(".BJ")
    ].copy()
    if "list_status" in master:
        master = master.loc[master["list_status"] != "G"]
    master["list_date"] = pd.to_datetime(master["list_date"], errors="coerce").dt.normalize()
    master["delist_date"] = pd.to_datetime(master["delist_date"], errors="coerce").dt.normalize()
    master["list_date"] = master["list_date"].clip(lower=_BSE_MARKET_OPEN_DATE)
    if master["list_date"].isna().any():
        raise TradabilityError("reviewed BSE master rows require list_date")
    master = master.sort_values(["symbol", "list_date"]).drop_duplicates("symbol", keep="last")
    existing = set(zip(universe["symbol"].astype(str), universe["trade_date"]))
    additions = []
    for row in master.to_dict("records"):
        alias = alias_by_current.get(str(row["symbol"]))
        for trade_date in dates:
            if trade_date < row["list_date"] or (
                pd.notna(row["delist_date"]) and trade_date > row["delist_date"]
            ):
                continue
            current_key = (str(row["symbol"]), trade_date)
            historical_key = (
                (str(alias["historical_symbol"]), trade_date) if alias is not None else current_key
            )
            if current_key in existing or historical_key in existing:
                continue
            additions.append(
                {
                    "symbol": row["symbol"],
                    "name": row["name"],
                    "exchange": row["exchange"],
                    "list_date": row["list_date"],
                    "delist_date": row["delist_date"],
                    "trade_date": trade_date,
                    "listed": True,
                    "master_record_present": True,
                    "universe_source": "reviewed_bse_mapping_master_supplement",
                }
            )
    if not additions:
        return universe
    return pd.concat([universe, pd.DataFrame(additions)], ignore_index=True)


def _supplement_reviewed_lifecycle_gaps(
    universe: pd.DataFrame,
    bars: pd.DataFrame,
    instruments: pd.DataFrame,
) -> pd.DataFrame:
    """Repair ``bak_basic`` omissions only when a bar proves actual trading.

    The all-status master must independently confirm that the bar falls inside
    the security's listing/delisting interval. Code changes are applied before
    this check, so a vendor-rewritten symbol cannot bypass the lifecycle gate.
    """
    outside = bars[["symbol", "trade_date"]].merge(
        universe[["symbol", "trade_date"]],
        on=["symbol", "trade_date"],
        how="left",
        indicator=True,
    )
    outside = outside.loc[outside["_merge"] == "left_only", ["symbol", "trade_date"]]
    if outside.empty:
        return universe
    required = {"symbol", "name", "exchange", "list_date", "delist_date"}
    missing = sorted(required - set(instruments.columns))
    if missing:
        raise TradabilityError(f"Instrument master missing lifecycle supplement columns: {missing}")
    master = instruments.copy()
    master["list_date"] = pd.to_datetime(master["list_date"], errors="coerce").dt.normalize()
    master["delist_date"] = pd.to_datetime(master["delist_date"], errors="coerce").dt.normalize()
    master = master.sort_values(["symbol", "list_date"]).drop_duplicates("symbol", keep="last")
    candidates = outside.merge(
        master[["symbol", "name", "exchange", "list_date", "delist_date"]],
        on="symbol",
        how="left",
        validate="many_to_one",
    )
    within_lifecycle = (candidates["trade_date"] >= candidates["list_date"]) & (
        candidates["delist_date"].isna() | (candidates["trade_date"] <= candidates["delist_date"])
    )
    candidates["lifecycle_reason"] = np.where(
        within_lifecycle, "current_master_lifecycle_validated_observed_bar", ""
    )
    invalid = candidates.loc[
        candidates["lifecycle_reason"].eq("")
        | candidates[["name", "exchange", "list_date"]].isna().any(axis=1)
    ]
    if not invalid.empty:
        sample = invalid[["symbol", "trade_date", "list_date", "delist_date"]].head(10)
        raise TradabilityError(
            "historical-universe gaps fall outside reviewed lifecycle windows: "
            f"{sample.to_dict('records')}"
        )
    additions = candidates.assign(
        listed=True,
        master_record_present=True,
        universe_source=(
            "reviewed_current_master_lifecycle_supplement:"
            + candidates["lifecycle_reason"].astype(str)
        ),
    )[
        [
            "symbol",
            "name",
            "exchange",
            "list_date",
            "delist_date",
            "trade_date",
            "listed",
            "master_record_present",
            "universe_source",
        ]
    ]
    return pd.concat([universe, additions], ignore_index=True)


def _exclude_pre_bse_listing_bars(
    bars: pd.DataFrame,
    instruments: pd.DataFrame,
    bse_aliases: Sequence[Dict[str, Any]],
) -> tuple[pd.DataFrame, int]:
    """Exclude vendor NEEQ history that predates the security's BSE listing.

    Tushare's daily endpoint can expose pre-BSE NEEQ bars under the later BSE
    symbol namespace.  Those observations are not A-share trading history and
    must not expand the P0.5 research universe backwards in time.
    """
    if bars.empty:
        return bars, 0
    required = {"symbol", "exchange", "list_date"}
    missing = sorted(required - set(instruments.columns))
    if missing:
        raise TradabilityError(f"Instrument master missing BSE listing columns: {missing}")
    listing_dates = (
        instruments[["symbol", "list_date"]]
        .assign(list_date=lambda frame: pd.to_datetime(frame["list_date"], errors="coerce"))
        .dropna(subset=["list_date"])
        .sort_values(["symbol", "list_date"])
        .drop_duplicates("symbol", keep="last")
        .set_index("symbol")["list_date"]
        .to_dict()
    )
    bse_master = instruments.loc[
        instruments["exchange"].astype(str).eq("BSE")
        | instruments["symbol"].astype(str).str.endswith(".BJ")
    ].copy()
    valid_from: Dict[str, pd.Timestamp] = {
        str(row["symbol"]): max(
            pd.Timestamp(row["list_date"]).normalize(), _BSE_MARKET_OPEN_DATE
        )
        for row in bse_master.to_dict("records")
        if pd.notna(row["list_date"])
    }
    for alias in bse_aliases:
        current = str(alias["current_symbol"])
        listed = listing_dates.get(current)
        if listed is None:
            raise TradabilityError(f"reviewed BSE mapping lacks master listing date: {current}")
        effective_listed = max(pd.Timestamp(listed).normalize(), _BSE_MARKET_OPEN_DATE)
        valid_from[str(alias["historical_symbol"])] = effective_listed
        valid_from[current] = effective_listed
    minimum_dates = bars["symbol"].map(valid_from)
    excluded = pd.Series(False, index=bars.index)
    bounded = minimum_dates.notna()
    excluded.loc[bounded] = (
        bars.loc[bounded, "trade_date"]
        < pd.to_datetime(minimum_dates.loc[bounded]).dt.normalize()
    )
    return bars.loc[~excluded].copy(), int(excluded.sum())


def _prepare_bars(frame: pd.DataFrame) -> pd.DataFrame:
    required = {"symbol", "trade_date", "open", "high", "low", "close"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise TradabilityError(f"Daily bars missing columns: {missing}")
    result = frame.copy()
    result["trade_date"] = pd.to_datetime(result["trade_date"]).dt.normalize()
    if result.duplicated(["symbol", "trade_date"]).any():
        raise TradabilityError("Daily bars contain duplicate symbol-date keys")
    rename = {"pre_close": "bar_pre_close"} if "pre_close" in result else {}
    columns = ["symbol", "trade_date", "open", "high", "low", "close", "volume", "amount"]
    if "pre_close" in result:
        columns.append("pre_close")
    prepared = result[columns].rename(columns=rename)
    if "bar_pre_close" not in prepared:
        prepared["bar_pre_close"] = np.nan
    return prepared


def _prepare_limits(frame: pd.DataFrame) -> pd.DataFrame:
    required = {"symbol", "trade_date", "pre_close", "up_limit", "down_limit"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise TradabilityError(f"Daily limits missing columns: {missing}")
    result = frame.copy()
    result["trade_date"] = pd.to_datetime(result["trade_date"]).dt.normalize()
    if result.duplicated(["symbol", "trade_date"]).any():
        raise TradabilityError("Daily limits contain duplicate symbol-date keys")
    inferred = pd.Series(
        np.where(
            (result["up_limit"] >= 99999.0) & (result["down_limit"] == 0),
            "none_vendor_sentinel",
            "bounded",
        ),
        index=result.index,
    )
    if "price_limit_regime" not in result:
        result["price_limit_regime"] = inferred
    else:
        result["price_limit_regime"] = result["price_limit_regime"].fillna(inferred)
    valid_regimes = {"bounded", "none_vendor_sentinel"}
    if not result["price_limit_regime"].isin(valid_regimes).all():
        raise TradabilityError("Daily limits contain an unknown price-limit regime")
    return result[
        [
            "symbol",
            "trade_date",
            "pre_close",
            "up_limit",
            "down_limit",
            "price_limit_regime",
        ]
    ].rename(columns={"pre_close": "limit_pre_close"})


def _prepare_suspensions(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(
            columns=[
                "symbol",
                "trade_date",
                "has_suspension_event",
                "is_suspended",
                "is_resumption",
                "suspend_timing",
            ]
        )
    result = frame.copy()
    result["trade_date"] = pd.to_datetime(result["trade_date"]).dt.normalize()
    grouped = result.groupby(["symbol", "trade_date"], observed=True, as_index=False).agg(
        is_suspended=("suspend_type", lambda values: (values == "S").any()),
        is_resumption=("suspend_type", lambda values: (values == "R").any()),
        suspend_timing=(
            "suspend_timing",
            lambda values: " | ".join(sorted({str(value) for value in values if pd.notna(value)})),
        ),
    )
    grouped["has_suspension_event"] = True
    return grouped


def _prepare_status(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=["symbol", "trade_date", "is_st", "status_name"])
    result = frame.copy()
    result["trade_date"] = pd.to_datetime(result["trade_date"]).dt.normalize()
    grouped = result.groupby(["symbol", "trade_date"], observed=True, as_index=False).agg(
        status_name=(
            "status_name",
            lambda values: " | ".join(sorted({str(value) for value in values if pd.notna(value)})),
        )
    )
    grouped["is_st"] = True
    return grouped


def _apply_symbol_aliases(
    frame: pd.DataFrame,
    aliases: Sequence[Dict[str, Any]],
    source_column: str,
) -> pd.DataFrame:
    result = frame.copy()
    if "symbol" not in result or "trade_date" not in result:
        return result
    result[source_column] = result["symbol"]
    for alias in aliases:
        effective = pd.Timestamp(alias["effective_date"]).normalize()
        mask = (result["symbol"] == alias["current_symbol"]) & (result["trade_date"] < effective)
        result.loc[mask, "symbol"] = alias["historical_symbol"]
    keys = ["symbol", "trade_date"]
    if not result.duplicated(keys).any():
        return result
    collapsed = []
    compare_columns = [column for column in result.columns if column not in keys + [source_column]]
    for _, group in result.groupby(keys, observed=True, sort=False, dropna=False):
        if len(group) == 1:
            collapsed.append(group.iloc[0])
            continue
        conflicting_columns = [
            column for column in compare_columns if group[column].dropna().nunique() > 1
        ]
        if conflicting_columns:
            sources = sorted(group[source_column].astype(str).unique())
            raise TradabilityError(
                f"Aliased source records disagree in {conflicting_columns} for "
                f"{sources} on {group.iloc[0]['trade_date']}"
            )
        preferred = group.assign(
            _historical_code=(group[source_column] == group["symbol"]).astype(int)
        ).sort_values("_historical_code", ascending=False)
        row = preferred.iloc[0].drop(labels="_historical_code").copy()
        for column in compare_columns:
            non_null = group[column].dropna()
            if pd.isna(row[column]) and not non_null.empty:
                row[column] = non_null.iloc[0]
        row[source_column] = " | ".join(sorted(group[source_column].astype(str).unique()))
        collapsed.append(row)
    return pd.DataFrame(collapsed).reset_index(drop=True)


_BSE_MARKET_OPEN_DATE = pd.Timestamp("2021-11-15")
_BSE_NEW_CODE_POLICY_START = pd.Timestamp("2024-04-22")
_BSE_PILOT_EFFECTIVE_DATE = pd.Timestamp("2025-05-06")
_BSE_FULL_EFFECTIVE_DATE = pd.Timestamp("2025-10-09")
_BSE_PILOT_NAMES = {
    "颖泰生物",
    "艾融软件",
    "龙竹科技",
    "佳先股份",
    "同享科技",
    "球冠电缆",
}


def _bse_symbol_aliases(
    mappings: Optional[pd.DataFrame],
) -> list[Dict[str, Any]]:
    """Translate the BSE crosswalk into versioned point-in-time alias rules."""
    if mappings is None:
        return []
    required = {"historical_symbol", "current_symbol", "name", "list_date"}
    missing = sorted(required - set(mappings.columns))
    if missing:
        raise TradabilityError(f"BSE code mappings missing columns: {missing}")
    result = []
    for row in mappings[list(required)].to_dict("records"):
        list_date = pd.Timestamp(row["list_date"]).normalize()
        if pd.isna(list_date):
            raise TradabilityError(f"BSE code mapping lacks list_date: {row['historical_symbol']}")
        if list_date >= _BSE_NEW_CODE_POLICY_START:
            effective_date = list_date
            policy = "new_listing_920_from_list_date"
            evidence = "https://www.bse.cn/jygl_list/200021626.html"
        elif str(row["name"]).strip() in _BSE_PILOT_NAMES:
            effective_date = _BSE_PILOT_EFFECTIVE_DATE
            policy = "six_security_pilot_20250506"
            evidence = "https://www.bse.cn/important_news/200025603.html"
        else:
            effective_date = _BSE_FULL_EFFECTIVE_DATE
            policy = "remaining_legacy_securities_20251009"
            evidence = "https://www.bse.cn/important_news/200026735.html"
        current_symbol = str(row["current_symbol"])
        result.append(
            {
                "current_symbol": current_symbol,
                "historical_symbol": str(row["historical_symbol"]),
                "effective_date": str(effective_date.date()),
                "stable_instrument_id": f"CN_EQ:BSE:{current_symbol}",
                "legal_continuity": True,
                "business_continuity": True,
                "price_chain_policy": "continuous",
                "fundamental_chain_policy": "continuous",
                "evidence": evidence,
                "policy": policy,
                "policy_version": "bse_920_transition_v1",
            }
        )
    return result


def _validate_symbol_aliases(aliases: Sequence[Dict[str, Any]]) -> None:
    required = {
        "current_symbol",
        "historical_symbol",
        "effective_date",
        "stable_instrument_id",
        "legal_continuity",
        "business_continuity",
        "price_chain_policy",
        "fundamental_chain_policy",
        "evidence",
    }
    seen_current: Dict[str, Dict[str, Any]] = {}
    seen_stable: Dict[str, Dict[str, Any]] = {}
    for alias in aliases:
        missing = sorted(required - set(alias))
        if missing:
            raise TradabilityError(f"Symbol alias missing fields: {missing}")
        current = str(alias["current_symbol"])
        stable = str(alias["stable_instrument_id"])
        if alias["legal_continuity"] is not True:
            raise TradabilityError(f"Symbol alias must prove legal continuity: {current}")
        if not isinstance(alias["business_continuity"], bool):
            raise TradabilityError(f"Symbol alias has invalid business continuity: {current}")
        if alias["price_chain_policy"] != "continuous":
            raise TradabilityError(
                f"Legally continuous alias must preserve its price chain: {current}"
            )
        expected_fundamental_policy = (
            "continuous" if alias["business_continuity"] is True else "reset_at_effective_date"
        )
        if alias["fundamental_chain_policy"] != expected_fundamental_policy:
            raise TradabilityError(
                f"Fundamental chain policy conflicts with business continuity for {current}"
            )
        if not str(alias["evidence"]).startswith("https://"):
            raise TradabilityError(f"Symbol alias evidence must use HTTPS: {current}")
        if current in seen_current and seen_current[current] != alias:
            raise TradabilityError(f"Conflicting aliases for current symbol {current}")
        if stable in seen_stable and seen_stable[stable] != alias:
            raise TradabilityError(f"Conflicting aliases for stable identity {stable}")
        seen_current[current] = alias
        seen_stable[stable] = alias


def _attach_instrument_ids(
    universe: pd.DataFrame, aliases: Sequence[Dict[str, Any]]
) -> pd.DataFrame:
    result = universe.copy()
    result["instrument_id"] = "CN_EQ:" + result["symbol"].astype(str)
    result["identity_alias_resolved"] = False
    result["identity_alias_policy"] = ""
    result["identity_alias_policy_version"] = ""
    for alias in aliases:
        effective = pd.Timestamp(alias["effective_date"]).normalize()
        historical = (result["symbol"] == alias["historical_symbol"]) & (
            result["trade_date"] < effective
        )
        current = (result["symbol"] == alias["current_symbol"]) & (
            result["trade_date"] >= effective
        )
        applies = historical | current
        result.loc[applies, "instrument_id"] = alias["stable_instrument_id"]
        result.loc[applies, "identity_alias_policy"] = alias.get("policy", "reviewed_manual_alias")
        result.loc[applies, "identity_alias_policy_version"] = alias.get(
            "policy_version", "instrument_alias_config"
        )
        result.loc[historical, "identity_alias_resolved"] = True
    return result


def _block_reason(frame: pd.DataFrame, side: str) -> pd.Series:
    at_limit = frame["open_at_up_limit"] if side == "buy" else frame["open_at_down_limit"]
    return pd.Series(
        np.select(
            [
                ~frame["data_complete"],
                frame["is_suspended"],
                ~frame["has_bar"],
                at_limit,
            ],
            ["data_quality_failure", "suspended", "no_executable_bar", f"open_limit_{side}"],
            default="",
        ),
        index=frame.index,
    )


def _frame_fingerprint(frame: pd.DataFrame) -> str:
    normalized = frame.sort_values(["symbol", "trade_date"]).reset_index(drop=True)
    return hashlib.sha256(
        pd.util.hash_pandas_object(normalized, index=False).values.tobytes()
    ).hexdigest()


def _write_immutable_parquet(frame: pd.DataFrame, path: Path, expected_sha: str) -> None:
    if path.exists():
        existing = _frame_fingerprint(pd.read_parquet(path))
        if existing != expected_sha:
            raise TradabilityError(f"Existing artifact content mismatch: {path}")
        return
    temporary = path.parent / f".{path.name}.{os.getpid()}.tmp"
    frame.to_parquet(temporary, index=False, engine="pyarrow")
    os.replace(temporary, path)


def _write_immutable_json(payload: Dict[str, Any], path: Path) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") != text:
            raise TradabilityError(f"Existing artifact manifest mismatch: {path}")
        return
    temporary = path.parent / f".{path.name}.{os.getpid()}.tmp"
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _manifest_references(entries: Sequence[Dict[str, Any]]) -> list[Dict[str, Any]]:
    return [{"path": entry["path"], "sha256": entry["sha256"]} for entry in entries]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _implementation_identity() -> Dict[str, Any]:
    paths = [
        Path(__file__),
        Path(__file__).with_name("catalog.py"),
        Path(__file__).with_name("contracts.py"),
    ]
    root = Path(__file__).resolve().parents[3]
    entries = []
    tree = hashlib.sha256()
    for path in sorted(paths):
        relative = str(path.resolve().relative_to(root))
        digest = _sha256(path)
        entries.append({"path": relative, "sha256": digest})
        tree.update(relative.encode("utf-8"))
        tree.update(digest.encode("ascii"))
    return {"tree_sha256": tree.hexdigest(), "files": entries}
