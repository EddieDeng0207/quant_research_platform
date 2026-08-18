"""Content-addressed inputs for a point-in-time 20-session reversal study."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

import numpy as np
import pandas as pd


class PriceReversalError(RuntimeError):
    """Raised when reversal inputs cannot satisfy their frozen contract."""


@dataclass(frozen=True)
class PriceReversalInputSpec:
    """Frozen formation and outcome-label policies for the pilot factor."""

    window_sessions: int = 20
    skip_sessions: int = 1
    min_observed_sessions: int = 15
    min_listing_sessions: int = 120
    horizons: tuple[int, ...] = (5, 10, 20, 60)
    weekly_rule: str = "W-FRI"
    factor_family: str = "price_volume_close"
    return_convention: str = "close_times_contemporaneous_adj_factor_ratio_v1"
    label_return_convention: str = "open_times_contemporaneous_adj_factor_ratio_v1"
    market_value_unit: str = "CNY"
    version: str = "price_reversal_pit_inputs_v2_cny_market_value"

    def validate(self) -> "PriceReversalInputSpec":
        if self.window_sessions < 2:
            raise ValueError("window_sessions must be at least 2")
        if self.skip_sessions < 0:
            raise ValueError("skip_sessions must be non-negative")
        if not 1 <= self.min_observed_sessions <= self.window_sessions:
            raise ValueError("min_observed_sessions must be within the formation window")
        if self.min_listing_sessions < self.window_sessions + self.skip_sessions:
            raise ValueError("min_listing_sessions is shorter than the formation requirement")
        if not self.horizons or min(self.horizons) < 1:
            raise ValueError("horizons must contain positive session counts")
        if len(set(self.horizons)) != len(self.horizons):
            raise ValueError("horizons cannot contain duplicates")
        if self.weekly_rule != "W-FRI":
            raise ValueError("the reversal pilot freezes weekly decisions to W-FRI")
        if self.market_value_unit != "CNY":
            raise ValueError("price-reversal market values must be normalized to CNY")
        return self

    @property
    def fingerprint(self) -> str:
        return _fingerprint(asdict(self))


def build_price_reversal_input_artifact(
    *,
    tradability_artifacts: Sequence[Path],
    lake_root: Path,
    industry_artifact: Path,
    output_root: Path,
    start_date: str,
    end_date: str,
    research_as_of_at: str,
    spec: Optional[PriceReversalInputSpec] = None,
) -> Path:
    """Build physically separated factor observations and future labels."""
    frozen = (spec or PriceReversalInputSpec()).validate()
    start = pd.Timestamp(start_date).normalize()
    end = pd.Timestamp(end_date).normalize()
    if start > end:
        raise ValueError("start_date must not exceed end_date")
    research_as_of = _utc_timestamp(research_as_of_at)

    lake = Path(lake_root)
    lake_entries = _lake_manifest_entries(lake)
    market, p05_identities = _load_tradability_chain(tradability_artifacts, start, end)
    sessions = pd.DatetimeIndex(market["trade_date"].unique()).sort_values()
    decisions = _decision_schedule(sessions, frozen)
    if decisions.empty:
        raise PriceReversalError("no weekly decisions have complete formation and outcome windows")

    adjustments, adjustment_entries = _load_latest_partitions(
        lake,
        lake_entries,
        dataset="adjustment_factors",
        partition_dates=set(sessions),
        columns=("symbol", "trade_date", "adj_factor", "ingested_at"),
        research_as_of=research_as_of,
    )
    indicator_dates = set(pd.DatetimeIndex(decisions["decision_date"]))
    indicators, indicator_entries = _load_latest_partitions(
        lake,
        lake_entries,
        dataset="daily_indicators",
        partition_dates=indicator_dates,
        columns=("symbol", "trade_date", "total_mv", "ingested_at"),
        research_as_of=research_as_of,
    )
    membership, industry_identity = _load_industry_membership(Path(industry_artifact))

    market = _attach_causal_prices(market, adjustments)
    market = _calculate_reversal_variants(market, frozen)
    observations = _prepare_observations(
        market,
        decisions,
        indicators,
        membership,
        research_as_of,
        frozen,
    )
    labels = _prepare_forward_returns(
        market,
        observations,
        decisions,
        end,
        frozen,
    )
    latest_source_ingestion = _latest_source_ingestion(
        market,
        indicators,
        adjustment_entries,
        indicator_entries,
        p05_identities,
        lake_entries,
    )
    if latest_source_ingestion > research_as_of:
        raise PriceReversalError(
            "research_as_of_at predates a selected platform-ingestion timestamp"
        )
    for frame in observations.values():
        frame["ingested_at"] = latest_source_ingestion
        frame["research_as_of_at"] = research_as_of

    implementation = _implementation_identity()
    identity = {
        "schema_version": frozen.version,
        "start_date": str(start.date()),
        "end_date": str(end.date()),
        "research_as_of_at": research_as_of.isoformat(),
        "spec_sha256": frozen.fingerprint,
        "tradability": p05_identities,
        "industry": industry_identity,
        "adjustment_factors": _partition_identity(adjustment_entries),
        "daily_indicators": _partition_identity(indicator_entries),
        "implementation_sha256": implementation["tree_sha256"],
    }
    artifact_id = _fingerprint(identity)[:20]
    destination = Path(output_root) / "factor_inputs" / f"artifact_id={artifact_id}"
    destination.mkdir(parents=True, exist_ok=True)
    outputs: Dict[str, Dict[str, Any]] = {}
    for name, frame in {
        "observations_rev20": observations["rev20"],
        "observations_rev20_skip1": observations["rev20_skip1"],
        "forward_returns": labels,
    }.items():
        path = destination / f"{name}.parquet"
        sort_columns = (
            ["decision_at", "instrument_id"]
            if name.startswith("observations")
            else ["execution_at", "instrument_id", "horizon_sessions"]
        )
        logical_sha = _frame_fingerprint(frame, sort_columns)
        _write_immutable_parquet(frame, path, sort_columns, logical_sha)
        outputs[name] = {
            "path": path.name,
            "rows": len(frame),
            "logical_sha256": logical_sha,
            "sha256": _sha256(path),
        }
    quality = _quality_summary(observations, labels, frozen)
    manifest = {
        "artifact_id": artifact_id,
        "schema_version": frozen.version,
        "identity": identity,
        "spec": {**asdict(frozen), "sha256": frozen.fingerprint},
        "outputs": outputs,
        "quality": quality,
        "guardrails": {
            "factor_values_use_causal_total_returns": True,
            "qfq_or_raw_price_ratio_not_used_for_formation": True,
            "primary_variant_skips_most_recent_session": True,
            "suspension_days_are_missing_not_zero_returns": True,
            "minimum_observed_sessions_enforced": True,
            "minimum_listing_sessions_enforced": True,
            "research_eligibility_from_decision_date": True,
            "execution_constraints_not_backfilled_into_factor": True,
            "future_labels_physically_separated": True,
            "open_to_open_labels_match_modeled_execution_clock": True,
            "all_requested_horizons_complete": True,
            "outcome_labels_never_read_during_factor_formation": True,
            "investment_conclusion_allowed": False,
        },
        "implementation": implementation,
    }
    _write_immutable_json(manifest, destination / "manifest.json")
    return destination


def _load_tradability_chain(
    artifacts: Sequence[Path], start: pd.Timestamp, end: pd.Timestamp
) -> tuple[pd.DataFrame, list[Dict[str, Any]]]:
    if not artifacts:
        raise ValueError("at least one P0.5 artifact is required")
    frames = []
    identities = []
    columns = [
        "instrument_id",
        "symbol",
        "source_bar_symbol",
        "trade_date",
        "list_date",
        "open",
        "close",
        "has_bar",
        "standard_research_eligible",
        "available_at",
    ]
    for artifact in artifacts:
        root = Path(artifact)
        manifest_path = root / "manifest.json"
        parquet_path = root / "tradability.parquet"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        actual_sha = _sha256(parquet_path)
        if actual_sha != manifest.get("output", {}).get("sha256"):
            raise PriceReversalError(f"P0.5 hash mismatch: {root}")
        if not manifest.get("quality", {}).get("promotion_passed", False):
            raise PriceReversalError(f"P0.5 artifact was not promoted: {root}")
        frame = pd.read_parquet(parquet_path, columns=columns)
        frames.append(frame)
        identities.append(
            {
                "artifact_id": manifest["artifact_id"],
                "start_date": manifest["identity"]["start_date"],
                "end_date": manifest["identity"]["end_date"],
                "manifest_sha256": _sha256(manifest_path),
                "parquet_sha256": actual_sha,
                "raw_input_paths": sorted(
                    {
                        item["path"]
                        for values in manifest.get("inputs", {}).values()
                        if isinstance(values, list)
                        for item in values
                        if isinstance(item, dict) and item.get("path", "").startswith("raw/")
                    }
                ),
            }
        )
    market = pd.concat(frames, ignore_index=True)
    market["trade_date"] = pd.to_datetime(market["trade_date"]).dt.normalize()
    market["list_date"] = pd.to_datetime(market["list_date"], errors="coerce").dt.normalize()
    market = market.loc[market["trade_date"].between(start, end)].copy()
    if market.empty or market.duplicated(["instrument_id", "trade_date"]).any():
        raise PriceReversalError("P0.5 chain is empty or has duplicate instrument dates")
    if not pd.api.types.is_bool_dtype(market["standard_research_eligible"].dtype):
        raise PriceReversalError("P0.5 research eligibility must be boolean")
    if market["list_date"].isna().any():
        raise PriceReversalError("P0.5 chain has missing list_date values")
    return market.sort_values(["instrument_id", "trade_date"]).reset_index(drop=True), identities


def _decision_schedule(sessions: pd.DatetimeIndex, spec: PriceReversalInputSpec) -> pd.DataFrame:
    if len(sessions) <= spec.min_listing_sessions + max(spec.horizons) + 1:
        return pd.DataFrame(columns=["decision_date", "execution_date"])
    position = {date: index for index, date in enumerate(sessions)}
    weekly = (
        pd.Series(sessions, index=sessions)
        .groupby(sessions.to_period(spec.weekly_rule))
        .max()
        .sort_values()
    )
    rows = []
    for decision_date in weekly:
        index = position[pd.Timestamp(decision_date)]
        execution_index = index + 1
        if index + 1 + max(spec.horizons) >= len(sessions):
            continue
        if index + 1 < spec.min_listing_sessions:
            continue
        rows.append(
            {
                "decision_date": pd.Timestamp(decision_date),
                "execution_date": sessions[execution_index],
                **{
                    f"horizon_{horizon}_end_date": sessions[execution_index + horizon]
                    for horizon in spec.horizons
                },
            }
        )
    return pd.DataFrame(rows)


def _attach_causal_prices(market: pd.DataFrame, adjustments: pd.DataFrame) -> pd.DataFrame:
    factors = adjustments.copy()
    factors["trade_date"] = pd.to_datetime(factors["trade_date"]).dt.normalize()
    factors["adj_factor"] = pd.to_numeric(factors["adj_factor"], errors="coerce")
    if factors.duplicated(["symbol", "trade_date"]).any():
        raise PriceReversalError("adjustment factors have duplicate symbol dates")
    if factors["adj_factor"].isna().any() or (factors["adj_factor"] <= 0).any():
        raise PriceReversalError("adjustment factors must be positive and finite")
    work = _resolve_vendor_field(
        market,
        factors,
        value_column="adj_factor",
        output_column="adj_factor",
        source_output_column="adjustment_source_symbol",
        candidate_values_must_match=False,
    )
    missing_factor_on_bar = work["has_bar"] & work["adj_factor"].isna()
    if missing_factor_on_bar.any():
        sample = work.loc[missing_factor_on_bar, ["instrument_id", "symbol", "trade_date"]].head(10)
        raise PriceReversalError(
            f"adjustment factor missing on P0.5 bar rows: {sample.to_dict('records')}"
        )
    work["total_return_close"] = work["close"] * work["adj_factor"]
    work["total_return_open"] = work["open"] * work["adj_factor"]
    work["source_symbol"] = work["symbol"].astype("string")
    observed = work.loc[work["has_bar"]].copy()
    observed = observed.sort_values(["source_symbol", "trade_date"])
    observed["causal_return_1d"] = observed.groupby("source_symbol", sort=False, observed=True)[
        "total_return_close"
    ].pct_change(fill_method=None)
    adjustment_source_changed = observed.groupby("source_symbol", sort=False, observed=True)[
        "adjustment_source_symbol"
    ].transform(lambda values: values.ne(values.shift(1)))
    observed.loc[adjustment_source_changed, "causal_return_1d"] = np.nan
    if (observed["causal_return_1d"].dropna() <= -1).any():
        raise PriceReversalError("causal total return contains a value at or below -100%")
    work["causal_return_1d"] = np.nan
    work.loc[observed.index, "causal_return_1d"] = observed["causal_return_1d"]
    return work.sort_values(["instrument_id", "trade_date"]).reset_index(drop=True)


def _calculate_reversal_variants(
    market: pd.DataFrame, spec: PriceReversalInputSpec
) -> pd.DataFrame:
    work = market.copy()
    work["listing_sessions"] = (
        work.groupby("instrument_id", sort=False, observed=True).cumcount() + 1
    )
    log_return = np.log1p(work["causal_return_1d"])
    group_key = work["instrument_id"]
    for name, skip in (("rev20", 0), ("rev20_skip1", spec.skip_sessions)):
        candidate = log_return.groupby(group_key, sort=False).shift(skip) if skip else log_return
        rolling = candidate.groupby(group_key, sort=False).rolling(
            spec.window_sessions, min_periods=spec.min_observed_sessions
        )
        work[f"{name}_observed_sessions"] = rolling.count().reset_index(level=0, drop=True)
        work[name] = np.expm1(rolling.sum().reset_index(level=0, drop=True))
        valid = (work[f"{name}_observed_sessions"] >= spec.min_observed_sessions) & (
            work["listing_sessions"] >= spec.min_listing_sessions
        )
        work.loc[~valid, name] = np.nan
    return work


def _prepare_observations(
    market: pd.DataFrame,
    decisions: pd.DataFrame,
    indicators: pd.DataFrame,
    membership: pd.DataFrame,
    research_as_of: pd.Timestamp,
    spec: PriceReversalInputSpec,
) -> Dict[str, pd.DataFrame]:
    work = _prepare_market_observation_base(
        market,
        decisions,
        indicators,
        membership,
    )
    common = [
        "instrument_id",
        "industry_code",
        "market_cap",
        "research_eligible",
        "market_close_at",
        "available_at",
        "decision_at",
        "execution_at",
        "listing_sessions",
    ]
    result = {}
    for name in ("rev20", "rev20_skip1"):
        frame = work[common].copy()
        frame["factor_value"] = work[name]
        frame["observed_sessions"] = work[f"{name}_observed_sessions"].astype("Int64")
        frame["factor_variant"] = name
        frame["factor_family"] = spec.factor_family
        frame["formation_return_convention"] = spec.return_convention
        frame["minimum_listing_sessions"] = spec.min_listing_sessions
        frame["minimum_observed_sessions"] = spec.min_observed_sessions
        frame["research_as_of_at"] = research_as_of
        result[name] = frame.sort_values(["decision_at", "instrument_id"]).reset_index(drop=True)
    return result


def _prepare_market_observation_base(
    market: pd.DataFrame,
    decisions: pd.DataFrame,
    indicators: pd.DataFrame,
    membership: pd.DataFrame,
) -> pd.DataFrame:
    """Attach decision clocks, market value and point-in-time industry membership."""
    decision_dates = set(pd.DatetimeIndex(decisions["decision_date"]))
    work = market.loc[market["trade_date"].isin(decision_dates)].copy()
    schedule = decisions.set_index("decision_date")
    work["execution_date"] = work["trade_date"].map(schedule["execution_date"])
    work["market_close_at"] = _local_time(work["trade_date"], 15, 0)
    work["available_at"] = _local_time(work["trade_date"], 16, 0)
    work["decision_at"] = work["available_at"]
    work["execution_at"] = _local_time(work["execution_date"], 9, 30)

    daily = indicators.copy()
    daily["trade_date"] = pd.to_datetime(daily["trade_date"]).dt.normalize()
    daily["total_mv"] = pd.to_numeric(daily["total_mv"], errors="coerce")
    if daily.duplicated(["symbol", "trade_date"]).any():
        raise PriceReversalError("daily indicators have duplicate symbol dates")
    work = _resolve_vendor_field(
        work,
        daily,
        value_column="total_mv",
        output_column="total_mv",
        source_output_column="market_cap_source_symbol",
    )
    # The Tushare adapter normalizes daily_basic values from 10k CNY to CNY
    # before they enter the lake.  Multiplying here would silently apply the
    # vendor unit conversion twice.
    work["market_cap"] = _market_cap_cny(work["total_mv"])
    work = pd.merge_asof(
        work.sort_values(["trade_date", "instrument_id"]),
        membership.sort_values(["membership_start", "instrument_id"]),
        by="instrument_id",
        left_on="trade_date",
        right_on="membership_start",
        direction="backward",
        allow_exact_matches=True,
    )
    outside = work["trade_date"] > work["membership_end"]
    work.loc[outside, "industry_code"] = pd.NA
    work["research_eligible"] = work["standard_research_eligible"]
    return work


def _market_cap_cny(total_mv: pd.Series) -> pd.Series:
    """Preserve lake-normalized CNY market values without a second unit conversion."""
    return pd.to_numeric(total_mv, errors="coerce")


def _prepare_forward_returns(
    market: pd.DataFrame,
    observations: Dict[str, pd.DataFrame],
    decisions: pd.DataFrame,
    outcome_end_date: pd.Timestamp,
    spec: PriceReversalInputSpec,
) -> pd.DataFrame:
    eligible_keys = pd.concat(
        [
            frame.loc[
                np.isfinite(frame["factor_value"]),
                ["instrument_id", "execution_at"],
            ]
            for frame in observations.values()
        ],
        ignore_index=True,
    ).drop_duplicates()
    schedule = decisions.copy()
    schedule["execution_at"] = _local_time(schedule["execution_date"], 9, 30)
    prices = market.loc[
        market["has_bar"],
        ["instrument_id", "trade_date", "source_symbol", "total_return_open"],
    ].copy()
    if prices.duplicated(["instrument_id", "trade_date"]).any():
        raise PriceReversalError("open-price lookup has duplicate instrument dates")
    start_lookup = prices.rename(
        columns={
            "trade_date": "execution_date",
            "source_symbol": "start_source_symbol",
            "total_return_open": "start_open_value",
        }
    )
    end_lookup = prices.rename(
        columns={
            "trade_date": "label_end_date",
            "source_symbol": "end_source_symbol",
            "total_return_open": "end_open_value",
        }
    )
    base = eligible_keys.merge(
        schedule[["execution_at", "execution_date"]],
        on="execution_at",
        how="left",
        validate="many_to_one",
    )
    base = base.merge(
        start_lookup,
        on=["instrument_id", "execution_date"],
        how="left",
        validate="many_to_one",
    )
    rows = []
    for horizon in spec.horizons:
        frame = base.copy()
        end_map = schedule.set_index("execution_at")[f"horizon_{horizon}_end_date"]
        frame["label_end_date"] = frame["execution_at"].map(end_map)
        frame = frame.merge(
            end_lookup,
            on=["instrument_id", "label_end_date"],
            how="left",
            validate="many_to_one",
        )
        same_price_chain = frame["start_source_symbol"].eq(frame["end_source_symbol"])
        frame["forward_return"] = np.where(
            same_price_chain & frame["start_open_value"].gt(0) & frame["end_open_value"].gt(0),
            frame["end_open_value"] / frame["start_open_value"] - 1.0,
            np.nan,
        )
        frame["horizon_sessions"] = horizon
        frame["label_start_at"] = frame["execution_at"]
        frame["label_end_at"] = _local_time(frame["label_end_date"], 9, 30)
        rows.append(frame)
    labels = pd.concat(rows, ignore_index=True)
    labels["outcome_observation_end_at"] = _local_time(
        pd.Series(outcome_end_date, index=labels.index), 16, 0
    )
    labels["label_return_convention"] = spec.label_return_convention
    return (
        labels[
            [
                "instrument_id",
                "execution_at",
                "horizon_sessions",
                "label_start_at",
                "label_end_at",
                "outcome_observation_end_at",
                "forward_return",
                "label_return_convention",
            ]
        ]
        .sort_values(["execution_at", "instrument_id", "horizon_sessions"])
        .reset_index(drop=True)
    )


def _load_industry_membership(
    artifact: Path,
) -> tuple[pd.DataFrame, Dict[str, Any]]:
    manifest_path = artifact / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    metadata = manifest["outputs"]["membership"]
    path = artifact / Path(metadata["path"]).name
    actual_sha = _sha256(path)
    if actual_sha != metadata["sha256"]:
        raise PriceReversalError("industry-membership hash mismatch")
    if not manifest.get("quality", {}).get("promotion_passed", False):
        raise PriceReversalError("industry-membership artifact was not promoted")
    frame = pd.read_parquet(
        path,
        columns=["instrument_id", "industry_code", "membership_start", "membership_end"],
    )
    frame["membership_start"] = pd.to_datetime(frame["membership_start"]).dt.normalize()
    frame["membership_end"] = pd.to_datetime(frame["membership_end"]).dt.normalize()
    return frame, {
        "artifact_id": manifest["artifact_id"],
        "manifest_sha256": _sha256(manifest_path),
        "membership_sha256": actual_sha,
    }


def _resolve_vendor_field(
    frame: pd.DataFrame,
    vendor: pd.DataFrame,
    *,
    value_column: str,
    output_column: str,
    source_output_column: str,
    candidate_values_must_match: bool = True,
) -> pd.DataFrame:
    """Resolve actual-code first, auditing every multi-source alias candidate."""
    work = frame.copy().reset_index(drop=True)
    work["_resolver_row"] = np.arange(len(work), dtype=np.int64)
    values = vendor[["symbol", "trade_date", value_column]].copy()
    values = values.rename(columns={"symbol": "_vendor_symbol", value_column: "_vendor_value"})
    primary = work[["_resolver_row", "symbol", "trade_date"]].merge(
        values,
        left_on=["symbol", "trade_date"],
        right_on=["_vendor_symbol", "trade_date"],
        how="left",
        validate="many_to_one",
    )
    resolved = primary.set_index("_resolver_row")["_vendor_value"]
    source = primary.set_index("_resolver_row")["_vendor_symbol"]

    audit_text = work["source_bar_symbol"].astype("string").fillna("")
    needs_candidates = (
        resolved.isna().reindex(work["_resolver_row"]).to_numpy()
        | audit_text.str.contains(r"\s*\|\s*", regex=True).to_numpy()
    )
    if needs_candidates.any():
        candidates = work.loc[
            needs_candidates, ["_resolver_row", "symbol", "trade_date", "source_bar_symbol"]
        ].copy()
        candidates["_candidate"] = candidates.apply(
            lambda row: sorted(
                {
                    str(row["symbol"]),
                    *[
                        item.strip()
                        for item in (
                            str(row["source_bar_symbol"])
                            if pd.notna(row["source_bar_symbol"])
                            else ""
                        ).split("|")
                        if item.strip() and item.strip() != "<NA>"
                    ],
                }
            ),
            axis=1,
        )
        candidates = candidates.explode("_candidate", ignore_index=True)
        candidates = candidates.merge(
            values,
            left_on=["_candidate", "trade_date"],
            right_on=["_vendor_symbol", "trade_date"],
            how="left",
            validate="many_to_one",
        )
        available = candidates.dropna(subset=["_vendor_value"]).copy()
        if candidate_values_must_match:
            inconsistent = []
            for row_id, group in available.groupby("_resolver_row", observed=True):
                numeric = pd.to_numeric(group["_vendor_value"], errors="coerce")
                if numeric.isna().any():
                    continue
                scale = max(1.0, float(numeric.abs().max()))
                if float(numeric.max() - numeric.min()) > 1e-10 * scale:
                    inconsistent.append(int(row_id))
            if inconsistent:
                sample = work.loc[
                    work["_resolver_row"].isin(inconsistent[:10]),
                    ["instrument_id", "symbol", "source_bar_symbol", "trade_date"],
                ]
                raise PriceReversalError(
                    f"alias candidates disagree for {value_column}: {sample.to_dict('records')}"
                )
        fallback = (
            available.sort_values(["_resolver_row", "_candidate"])
            .drop_duplicates("_resolver_row")
            .set_index("_resolver_row")
        )
        missing_ids = resolved.index[resolved.isna()]
        resolved.loc[missing_ids] = fallback["_vendor_value"].reindex(missing_ids)
        source.loc[missing_ids] = fallback["_vendor_symbol"].reindex(missing_ids)

    work[output_column] = work["_resolver_row"].map(resolved)
    work[source_output_column] = work["_resolver_row"].map(source).astype("string")
    return work.drop(columns="_resolver_row")


def _lake_manifest_entries(lake_root: Path) -> list[Dict[str, Any]]:
    path = lake_root / "manifest.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _load_latest_partitions(
    lake_root: Path,
    entries: Sequence[Dict[str, Any]],
    *,
    dataset: str,
    partition_dates: set[pd.Timestamp],
    columns: Sequence[str],
    research_as_of: pd.Timestamp,
) -> tuple[pd.DataFrame, list[Dict[str, Any]]]:
    normalized_dates = {pd.Timestamp(value).normalize() for value in partition_dates}
    latest: Dict[pd.Timestamp, tuple[pd.Timestamp, Dict[str, Any]]] = {}
    for entry in entries:
        if entry.get("provider") != "tushare" or entry.get("dataset") != dataset:
            continue
        value = entry.get("partition_values", {}).get("trade_date")
        if not value:
            continue
        trade_date = pd.Timestamp(value).normalize()
        written_at = _utc_timestamp(entry["written_at"])
        if trade_date in normalized_dates and written_at <= research_as_of:
            current = latest.get(trade_date)
            if current is None or current[0] < written_at:
                latest[trade_date] = (written_at, entry)
    missing = sorted(normalized_dates - set(latest))
    if missing:
        raise PriceReversalError(f"{dataset} missing partitions: {missing[:10]}")
    selected = [latest[date][1] for date in sorted(latest)]
    frames = []
    for entry in selected:
        path = lake_root / entry["path"]
        if _sha256(path) != entry["sha256"]:
            raise PriceReversalError(f"raw input hash mismatch: {path}")
        frames.append(pd.read_parquet(path, columns=list(columns)))
    nonempty = [frame for frame in frames if not frame.empty]
    if not nonempty:
        raise PriceReversalError(f"all selected {dataset} partitions are empty")
    return pd.concat(nonempty, ignore_index=True), selected


def _partition_identity(entries: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    references = [{"path": item["path"], "sha256": item["sha256"]} for item in entries]
    return {
        "partitions": len(references),
        "fingerprint": _fingerprint(references),
        "inputs": references,
    }


def _latest_source_ingestion(
    market: pd.DataFrame,
    indicators: pd.DataFrame,
    adjustment_entries: Sequence[Dict[str, Any]],
    indicator_entries: Sequence[Dict[str, Any]],
    p05_identities: Sequence[Dict[str, Any]],
    lake_entries: Sequence[Dict[str, Any]],
) -> pd.Timestamp:
    candidates = []
    for frame in (indicators,):
        if "ingested_at" in frame:
            candidates.extend(pd.to_datetime(frame["ingested_at"], utc=True).dropna().tolist())
    candidates.extend(_utc_timestamp(item["written_at"]) for item in adjustment_entries)
    candidates.extend(_utc_timestamp(item["written_at"]) for item in indicator_entries)
    written_by_path = {
        item["path"]: _utc_timestamp(item["written_at"])
        for item in lake_entries
        if item.get("path") and item.get("written_at")
    }
    for identity in p05_identities:
        candidates.extend(
            written_by_path[path] for path in identity["raw_input_paths"] if path in written_by_path
        )
    if "available_at" in market:
        candidates.extend(pd.to_datetime(market["available_at"], utc=True).dropna().tolist())
    if not candidates:
        raise PriceReversalError("cannot establish a source-ingestion timestamp")
    return max(candidates)


def _quality_summary(
    observations: Dict[str, pd.DataFrame],
    labels: pd.DataFrame,
    spec: PriceReversalInputSpec,
) -> Dict[str, Any]:
    variants = {}
    for name, frame in observations.items():
        eligible = frame.loc[frame["research_eligible"]]
        finite = np.isfinite(eligible["factor_value"])
        variants[name] = {
            "rows": len(frame),
            "decision_dates": int(frame["decision_at"].nunique()),
            "eligible_rows": len(eligible),
            "finite_factor_rows": int(finite.sum()),
            "missing_factor_rows": int((~finite).sum()),
            "median_daily_missing_factor_rate": float(
                eligible.assign(missing=~finite)
                .groupby("decision_at", observed=True)["missing"]
                .mean()
                .median()
            ),
        }
    return {
        "variants": variants,
        "forward_return_rows": len(labels),
        "forward_return_missing_rows": int(labels["forward_return"].isna().sum()),
        "forward_return_missing_rate": float(labels["forward_return"].isna().mean()),
        "horizons": list(spec.horizons),
        "label_start_before_execution_rows": int(
            (labels["label_start_at"] < labels["execution_at"]).sum()
        ),
        "labels_beyond_observation_end_rows": int(
            (labels["label_end_at"] > labels["outcome_observation_end_at"]).sum()
        ),
    }


def _local_time(values: pd.Series, hour: int, minute: int) -> pd.Series:
    naive = pd.to_datetime(values, errors="coerce").dt.normalize() + pd.Timedelta(
        hours=hour, minutes=minute
    )
    return naive.dt.tz_localize("Asia/Shanghai").dt.tz_convert("UTC")


def _utc_timestamp(value: Any) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    return timestamp.tz_convert("UTC") if timestamp.tzinfo else timestamp.tz_localize("UTC")


def _implementation_identity() -> Dict[str, Any]:
    here = Path(__file__)
    root = here.resolve().parents[3]
    relative = str(here.resolve().relative_to(root))
    digest = _sha256(here)
    return {
        "tree_sha256": _fingerprint({relative: digest}),
        "files": [{"path": relative, "sha256": digest}],
    }


def _frame_fingerprint(frame: pd.DataFrame, sort_columns: Sequence[str]) -> str:
    normalized = frame.sort_values(list(sort_columns)).reset_index(drop=True)
    return hashlib.sha256(
        pd.util.hash_pandas_object(normalized, index=False).values.tobytes()
    ).hexdigest()


def _write_immutable_parquet(
    frame: pd.DataFrame,
    path: Path,
    sort_columns: Sequence[str],
    logical_sha: str,
) -> None:
    if path.exists():
        if _frame_fingerprint(pd.read_parquet(path), sort_columns) != logical_sha:
            raise PriceReversalError(f"immutable reversal-input conflict: {path}")
        return
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    frame.sort_values(list(sort_columns)).to_parquet(temporary, index=False)
    os.replace(temporary, path)


def _write_immutable_json(payload: Dict[str, Any], path: Path) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") != encoded:
        raise PriceReversalError(f"immutable reversal-input manifest conflict: {path}")
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


def _fingerprint(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()
