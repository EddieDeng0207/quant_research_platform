"""Point-in-time fundamental factor inputs, beginning with TTM sales-to-price."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

import numpy as np
import pandas as pd

from qrp.versioning import (
    VersionControlError,
    environment_lock_identity,
    inspect_git_repository,
)

from .factor_registry import SP_TTM
from .factor_universe import CN_A_FULL, ResearchUniverseSpec, attach_research_universe
from .price_reversal import (
    PriceReversalInputSpec,
    _attach_causal_prices,
    _fingerprint,
    _frame_fingerprint,
    _lake_manifest_entries,
    _load_industry_membership,
    _load_latest_partitions,
    _load_tradability_chain,
    _local_time,
    _partition_identity,
    _prepare_forward_returns,
    _prepare_market_observation_base,
    _sha256,
    _utc_timestamp,
    _write_immutable_json,
    _write_immutable_parquet,
)


class FundamentalFactorError(RuntimeError):
    """Raised when a fundamental factor cannot prove its PIT contract."""


@dataclass(frozen=True)
class SalesToPriceInputSpec:
    """Frozen construction policy for ordinary-company TTM sales-to-price."""

    revenue_column: str = "revenue"
    allowed_company_types: tuple[str, ...] = ("1",)
    max_report_age_days: int = 550
    min_listing_sessions: int = 120
    horizons: tuple[int, ...] = (5, 10, 20, 60)
    weekly_rule: str = "W-FRI"
    market_value_unit: str = "CNY"
    version: str = "sp_ttm_pit_inputs_v2_actual_listing_calendar"

    def validate(self) -> "SalesToPriceInputSpec":
        if self.revenue_column != "revenue":
            raise ValueError("SP v1 freezes operating revenue to the revenue field")
        if not self.allowed_company_types:
            raise ValueError("allowed_company_types cannot be empty")
        if self.max_report_age_days < 365:
            raise ValueError("max_report_age_days must allow a complete annual reporting cycle")
        if self.min_listing_sessions < 20:
            raise ValueError("min_listing_sessions is too short for formal A-share research")
        if not self.horizons or min(self.horizons) < 1:
            raise ValueError("horizons must contain positive session counts")
        if len(set(self.horizons)) != len(self.horizons):
            raise ValueError("horizons cannot contain duplicates")
        if self.weekly_rule != "W-FRI":
            raise ValueError("SP v1 freezes weekly decisions to W-FRI")
        if self.market_value_unit != "CNY":
            raise ValueError("SP market values must be normalized to CNY")
        return self

    @property
    def fingerprint(self) -> str:
        return _fingerprint(asdict(self))


def build_sales_to_price_input_artifact(
    *,
    fundamentals_artifact: Path,
    tradability_artifacts: Sequence[Path],
    lake_root: Path,
    industry_artifact: Path,
    aliases_path: Path,
    output_root: Path,
    start_date: str,
    end_date: str,
    research_as_of_at: str,
    spec: Optional[SalesToPriceInputSpec] = None,
    universe_spec: Optional[ResearchUniverseSpec] = None,
    require_clean_git: bool = False,
) -> Path:
    """Build SP observations and physically separate open-to-open outcome labels."""
    frozen = (spec or SalesToPriceInputSpec()).validate()
    frozen_universe = (universe_spec or CN_A_FULL).validate()
    start = pd.Timestamp(start_date).normalize()
    end = pd.Timestamp(end_date).normalize()
    if start > end:
        raise ValueError("start_date must not exceed end_date")
    research_as_of = _utc_timestamp(research_as_of_at)

    market, p05_identities = _load_tradability_chain(tradability_artifacts, start, end)
    sessions = pd.DatetimeIndex(market["trade_date"].unique()).sort_values()
    schedule_spec = PriceReversalInputSpec(
        min_listing_sessions=frozen.min_listing_sessions,
        horizons=frozen.horizons,
        weekly_rule=frozen.weekly_rule,
    )
    decisions = _fundamental_decision_schedule(
        sessions,
        frozen.horizons,
        frozen.weekly_rule,
    )
    if decisions.empty:
        raise FundamentalFactorError("no weekly decisions have complete outcome windows")

    lake = Path(lake_root)
    lake_entries = _lake_manifest_entries(lake)
    trading_calendar, calendar_identity = _load_research_calendar(
        lake,
        lake_entries,
        start,
        end,
        research_as_of,
    )
    adjustments, adjustment_entries = _load_latest_partitions(
        lake,
        lake_entries,
        dataset="adjustment_factors",
        partition_dates=set(sessions),
        columns=("symbol", "trade_date", "adj_factor", "ingested_at"),
        research_as_of=research_as_of,
    )
    decision_dates = set(pd.DatetimeIndex(decisions["decision_date"]))
    indicators, indicator_entries = _load_latest_partitions(
        lake,
        lake_entries,
        dataset="daily_indicators",
        partition_dates=decision_dates,
        columns=("symbol", "trade_date", "total_mv", "ingested_at"),
        research_as_of=research_as_of,
    )
    membership, industry_identity = _load_industry_membership(Path(industry_artifact))
    income, fundamental_identity = _load_income_artifact(Path(fundamentals_artifact))
    resets, alias_identity = _load_business_resets(Path(aliases_path))

    market = _attach_causal_prices(market, adjustments)
    market["listing_sessions"] = _listing_sessions_since_actual_list_date(
        market,
        trading_calendar,
    )
    base = _prepare_market_observation_base(market, decisions, indicators, membership)
    snapshots = build_pit_ttm_revenue_snapshots(
        income,
        decisions,
        research_as_of,
        resets=resets,
        spec=frozen,
    )
    observations = _attach_sales_to_price(base, snapshots, research_as_of, frozen)
    observations = attach_research_universe(observations, frozen_universe)
    labels = _prepare_forward_returns(
        market,
        {"sp_ttm": observations},
        decisions,
        end,
        schedule_spec,
    )

    implementation = _implementation_identity()
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
        "schema_version": frozen.version,
        "factor_definition_sha256": SP_TTM.fingerprint,
        "research_universe_sha256": frozen_universe.fingerprint,
        "start_date": str(start.date()),
        "end_date": str(end.date()),
        "research_as_of_at": research_as_of.isoformat(),
        "spec_sha256": frozen.fingerprint,
        "fundamentals": fundamental_identity,
        "tradability": p05_identities,
        "industry": industry_identity,
        "aliases": alias_identity,
        "adjustment_factors": _partition_identity(adjustment_entries),
        "daily_indicators": _partition_identity(indicator_entries),
        "trading_calendar": calendar_identity,
        "implementation_sha256": implementation["tree_sha256"],
        "git_commit": code_identity["commit"] if code_identity else None,
        "git_tree": code_identity["tree"] if code_identity else None,
        "git_dirty_state_sha256": (
            code_identity["dirty_state_sha256"] if code_identity else None
        ),
        "environment_lock_sha256": environment_lock["sha256"] if environment_lock else None,
    }
    artifact_id = _fingerprint(identity)[:20]
    destination = Path(output_root) / "factor_inputs" / f"artifact_id={artifact_id}"
    destination.mkdir(parents=True, exist_ok=True)
    outputs: Dict[str, Dict[str, Any]] = {}
    for name, frame, sort_columns in (
        ("observations_sp_ttm", observations, ["decision_at", "instrument_id"]),
        (
            "ttm_revenue_diagnostics",
            snapshots,
            ["decision_at", "instrument_id"],
        ),
        (
            "forward_returns",
            labels,
            ["execution_at", "instrument_id", "horizon_sessions"],
        ),
    ):
        path = destination / f"{name}.parquet"
        logical_sha = _frame_fingerprint(frame, sort_columns)
        _write_immutable_parquet(frame, path, sort_columns, logical_sha)
        outputs[name] = {
            "path": path.name,
            "rows": len(frame),
            "logical_sha256": logical_sha,
            "sha256": _sha256(path),
        }

    quality = _quality_summary(observations, snapshots, labels)
    manifest = {
        "artifact_id": artifact_id,
        "schema_version": frozen.version,
        "identity": identity,
        "factor_definition": {**asdict(SP_TTM), "sha256": SP_TTM.fingerprint},
        "spec": {**asdict(frozen), "sha256": frozen.fingerprint},
        "research_universe": {
            **asdict(frozen_universe),
            "sha256": frozen_universe.fingerprint,
        },
        "outputs": outputs,
        "quality": quality,
        "guardrails": {
            "report_period_is_not_knowledge_time": True,
            "each_ttm_component_selected_point_in_time": True,
            "raw_revisions_preserved_and_latest_known_version_selected": True,
            "quarterly_cumulative_values_not_annualized": True,
            "ttm_formula_current_ytd_plus_prior_fy_minus_prior_ytd": True,
            "missing_components_not_zero_filled": True,
            "ordinary_industrial_company_scope_only": True,
            "business_discontinuities_reset_fundamental_chain": True,
            "decision_date_market_value_in_cny": True,
            "research_eligibility_from_decision_date": True,
            "listing_age_uses_actual_list_date_and_frozen_calendar": True,
            "fundamental_schedule_has_no_price_formation_warmup": True,
            "base_eligibility_preserved_separately_from_factor_scope": True,
            "research_universe_frozen_before_outcome_labels": True,
            "execution_constraints_not_backfilled_into_factor": True,
            "future_labels_physically_separated": True,
            "outcome_labels_never_read_during_factor_formation": True,
            "investment_conclusion_allowed": False,
            "formal_cli_requires_clean_git": True,
            "git_commit_bound": code_identity is not None,
            "environment_lock_bound": environment_lock is not None,
        },
        "implementation": implementation,
        "code_identity": code_identity,
        "environment_lock": environment_lock,
    }
    _write_immutable_json(manifest, destination / "manifest.json")
    return destination


def build_pit_ttm_revenue_snapshots(
    income: pd.DataFrame,
    decisions: pd.DataFrame,
    research_as_of_at: Any,
    *,
    resets: Optional[Mapping[str, pd.Timestamp]] = None,
    spec: Optional[SalesToPriceInputSpec] = None,
) -> pd.DataFrame:
    """Replay statement revisions and calculate TTM revenue at each decision."""
    frozen = (spec or SalesToPriceInputSpec()).validate()
    research_as_of = _utc_timestamp(research_as_of_at)
    decision_frame = decisions[["decision_date", "execution_date"]].copy()
    decision_frame["decision_at"] = _local_time(decision_frame["decision_date"], 16, 0)
    decision_frame = decision_frame.sort_values("decision_at").reset_index(drop=True)
    events = _prepare_income_events(
        income,
        research_as_of,
        decision_frame["decision_at"].max(),
        frozen,
    )
    decision_ns = decision_frame["decision_at"].astype("int64").to_numpy()
    event_ns = events["available_at"].astype("int64").to_numpy()
    events["_activation"] = np.searchsorted(decision_ns, event_ns, side="left")
    events = events.loc[events["_activation"] < len(decision_frame)].copy()
    by_activation = {key: value for key, value in events.groupby("_activation", sort=True)}

    state: Dict[str, Dict[pd.Timestamp, Dict[str, Any]]] = {}
    rows: list[Dict[str, Any]] = []
    reset_map = {key: pd.Timestamp(value).normalize() for key, value in (resets or {}).items()}
    for decision_index, decision in decision_frame.iterrows():
        for _, event in by_activation.get(decision_index, pd.DataFrame()).iterrows():
            instrument = str(event["instrument_id"])
            period = pd.Timestamp(event["report_period"]).normalize()
            state.setdefault(instrument, {})[period] = event.to_dict()
        for instrument, periods in state.items():
            result = _ttm_from_known_periods(
                periods,
                pd.Timestamp(decision["decision_date"]),
                reset_map.get(instrument),
                frozen,
            )
            rows.append(
                {
                    "instrument_id": instrument,
                    "decision_at": decision["decision_at"],
                    **result,
                }
            )
    if not rows:
        raise FundamentalFactorError("no PIT income snapshots were available at decisions")
    return pd.DataFrame(rows).sort_values(["decision_at", "instrument_id"]).reset_index(drop=True)


def _fundamental_decision_schedule(
    sessions: pd.DatetimeIndex,
    horizons: Sequence[int],
    weekly_rule: str,
) -> pd.DataFrame:
    """Weekly decisions need complete outcomes but no price-formation warm-up."""
    if len(sessions) <= max(horizons) + 1:
        return pd.DataFrame(columns=["decision_date", "execution_date"])
    position = {date: index for index, date in enumerate(sessions)}
    weekly = (
        pd.Series(sessions, index=sessions)
        .groupby(sessions.to_period(weekly_rule))
        .max()
        .sort_values()
    )
    rows = []
    for decision_date in weekly:
        index = position[pd.Timestamp(decision_date)]
        execution_index = index + 1
        if execution_index + max(horizons) >= len(sessions):
            continue
        rows.append(
            {
                "decision_date": pd.Timestamp(decision_date),
                "execution_date": sessions[execution_index],
                **{
                    f"horizon_{horizon}_end_date": sessions[execution_index + horizon]
                    for horizon in horizons
                },
            }
        )
    return pd.DataFrame(rows)


def _load_research_calendar(
    lake_root: Path,
    entries: Sequence[Dict[str, Any]],
    start: pd.Timestamp,
    end: pd.Timestamp,
    research_as_of: pd.Timestamp,
) -> tuple[pd.DataFrame, Dict[str, Any]]:
    required_start = start - pd.Timedelta(days=400)
    candidates = sorted(
        (
            entry
            for entry in entries
            if entry.get("provider") == "tushare"
            and entry.get("dataset") == "trading_calendar"
            and _utc_timestamp(entry["written_at"]) <= research_as_of
        ),
        key=lambda entry: _utc_timestamp(entry["written_at"]),
        reverse=True,
    )
    for entry in candidates:
        path = lake_root / entry["path"]
        if _sha256(path) != entry["sha256"]:
            raise FundamentalFactorError(f"trading-calendar hash mismatch: {path}")
        frame = pd.read_parquet(path)
        required = {"exchange", "calendar_date", "is_open"}
        if not required.issubset(frame.columns):
            continue
        frame["calendar_date"] = pd.to_datetime(
            frame["calendar_date"], errors="coerce"
        ).dt.normalize()
        frame = frame.loc[frame["exchange"].eq("SSE")].copy()
        if frame.empty or frame["calendar_date"].isna().any():
            continue
        if frame["calendar_date"].min() > required_start or frame["calendar_date"].max() < end:
            continue
        if frame.duplicated("calendar_date").any():
            raise FundamentalFactorError("trading calendar has duplicate SSE dates")
        return frame.sort_values("calendar_date").reset_index(drop=True), {
            "path": entry["path"],
            "sha256": entry["sha256"],
            "written_at": entry["written_at"],
            "coverage_start": str(frame["calendar_date"].min().date()),
            "coverage_end": str(frame["calendar_date"].max().date()),
        }
    raise FundamentalFactorError(
        "no verified trading calendar covers listing warm-up and the research window"
    )


def _listing_sessions_since_actual_list_date(
    market: pd.DataFrame,
    calendar: pd.DataFrame,
) -> pd.Series:
    list_dates = pd.to_datetime(market["list_date"], errors="coerce").dt.normalize()
    trade_dates = pd.to_datetime(market["trade_date"], errors="coerce").dt.normalize()
    if list_dates.isna().any() or trade_dates.isna().any():
        raise FundamentalFactorError("listing-session calculation has invalid dates")
    if (list_dates > trade_dates).any():
        raise FundamentalFactorError("list_date cannot be after trade_date")
    open_dates = pd.DatetimeIndex(
        calendar.loc[calendar["is_open"].astype(bool), "calendar_date"]
    ).sort_values()
    if open_dates.empty:
        raise FundamentalFactorError("trading calendar has no open sessions")
    if open_dates.min() > trade_dates.min() - pd.Timedelta(days=365):
        raise FundamentalFactorError("trading calendar has insufficient listing warm-up")
    if open_dates.max() < trade_dates.max():
        raise FundamentalFactorError("trading calendar ends before the market panel")
    open_values = open_dates.to_numpy(dtype="datetime64[ns]")
    start_positions = np.searchsorted(
        open_values,
        list_dates.to_numpy(dtype="datetime64[ns]"),
        side="left",
    )
    end_positions = np.searchsorted(
        open_values,
        trade_dates.to_numpy(dtype="datetime64[ns]"),
        side="right",
    )
    counts = end_positions - start_positions
    if (counts < 1).any():
        raise FundamentalFactorError("listed market rows must have at least one trading session")
    return pd.Series(counts, index=market.index, dtype="int64")


def _prepare_income_events(
    frame: pd.DataFrame,
    research_as_of: pd.Timestamp,
    last_decision_at: pd.Timestamp,
    spec: SalesToPriceInputSpec,
) -> pd.DataFrame:
    required = {
        "instrument_id",
        "report_period",
        "report_type",
        "comp_type",
        "available_at",
        "announcement_at",
        "source_ingested_at",
        "source_row_sha256",
        "source_row_occurrence",
        "version_id",
        "update_flag",
        spec.revenue_column,
    }
    missing = sorted(required - set(frame.columns))
    if missing:
        raise FundamentalFactorError(f"income artifact missing columns: {missing}")
    work = frame[list(required)].copy()
    for column in ("available_at", "announcement_at", "source_ingested_at"):
        work[column] = pd.to_datetime(work[column], utc=True, errors="coerce")
    work["report_period"] = pd.to_datetime(work["report_period"], errors="coerce").dt.normalize()
    if work[["available_at", "announcement_at", "source_ingested_at", "report_period"]].isna().any(
        axis=None
    ):
        raise FundamentalFactorError("income artifact contains invalid PIT timestamps")
    work = work.loc[
        work["report_type"].astype("string").eq("1")
        & work["source_ingested_at"].le(research_as_of)
        & work["available_at"].le(last_decision_at)
    ].copy()
    if work.empty:
        raise FundamentalFactorError("no PIT income rows survive the frozen cutoffs")

    keys = ["instrument_id", "report_period", "available_at"]
    work = work.sort_values([*keys, "source_row_sha256", "source_row_occurrence"])
    work = work.drop_duplicates([*keys, "source_row_sha256"], keep="first")
    work["_latest_flag"] = work["update_flag"].astype("string").eq("1")
    has_latest = work.groupby(keys, observed=True)["_latest_flag"].transform("any")
    work = work.loc[~has_latest | work["_latest_flag"]].copy()
    work[spec.revenue_column] = pd.to_numeric(work[spec.revenue_column], errors="coerce")
    work["equivalent_source_versions"] = work.groupby(keys, observed=True)[
        "version_id"
    ].transform("size")
    ambiguous_groups = []
    for key, group in work.groupby(keys, observed=True, sort=False):
        if len(group) == 1:
            continue
        revenue = group[spec.revenue_column]
        same_revenue = (
            revenue.isna().all()
            or (revenue.notna().all() and float(revenue.max() - revenue.min()) == 0.0)
        )
        same_announcement = group["announcement_at"].nunique(dropna=False) == 1
        same_ingestion = group["source_ingested_at"].nunique(dropna=False) == 1
        if not (same_revenue and same_announcement and same_ingestion):
            ambiguous_groups.append(key)
    if ambiguous_groups:
        raise FundamentalFactorError(
            "economically ambiguous income versions at one availability time: "
            f"{ambiguous_groups[:5]}"
        )
    work = work.sort_values([*keys, "source_row_sha256"]).drop_duplicates(keys, keep="first")
    return work.drop(columns="_latest_flag").sort_values("available_at").reset_index(drop=True)


def _ttm_from_known_periods(
    periods: Mapping[pd.Timestamp, Mapping[str, Any]],
    decision_date: pd.Timestamp,
    reset_date: Optional[pd.Timestamp],
    spec: SalesToPriceInputSpec,
) -> Dict[str, Any]:
    known = {
        pd.Timestamp(period).normalize(): row
        for period, row in periods.items()
        if reset_date is None
        or (decision_date < reset_date and pd.Timestamp(period) < reset_date)
        or (decision_date >= reset_date and pd.Timestamp(period) >= reset_date)
    }
    empty = {
        "ttm_revenue": np.nan,
        "ttm_status": "no_statement_in_business_regime",
        "ttm_method": pd.NA,
        "latest_report_period": pd.NaT,
        "latest_company_type": pd.NA,
        "report_age_days": pd.NA,
        "component_report_periods": pd.NA,
        "component_version_ids": pd.NA,
        "component_source_version_count": pd.NA,
        "announcement_at": pd.NaT,
        "available_at": pd.NaT,
        "source_ingested_at": pd.NaT,
        "business_chain_reset_applied": bool(reset_date is not None and decision_date >= reset_date),
    }
    if not known:
        return empty
    latest = max(known)
    age = int((decision_date.normalize() - latest).days)
    base = {
        **empty,
        "latest_report_period": latest,
        "latest_company_type": str(known[latest].get("comp_type")),
        "report_age_days": age,
    }
    if age > spec.max_report_age_days:
        return {**base, "ttm_status": "stale_latest_report"}
    allowed_types = {str(value) for value in spec.allowed_company_types}
    if str(known[latest].get("comp_type")) not in allowed_types:
        return {**base, "ttm_status": "unsupported_company_type"}
    if latest.month not in {3, 6, 9, 12}:
        return {**base, "ttm_status": "unsupported_fiscal_period"}
    if latest.month == 12:
        component_periods = [latest]
        method = "annual_report"
        coefficients = [1.0]
    else:
        prior_annual = pd.Timestamp(year=latest.year - 1, month=12, day=31)
        prior_same = latest - pd.DateOffset(years=1)
        component_periods = [latest, prior_annual, prior_same]
        method = "current_ytd_plus_prior_fy_minus_prior_ytd"
        coefficients = [1.0, 1.0, -1.0]
    if any(period not in known for period in component_periods):
        return {**base, "ttm_status": "missing_ttm_component", "ttm_method": method}
    components = [known[period] for period in component_periods]
    if any(str(row.get("comp_type")) not in allowed_types for row in components):
        return {**base, "ttm_status": "company_type_changed_within_ttm"}
    values = [pd.to_numeric(row[spec.revenue_column], errors="coerce") for row in components]
    if not all(np.isfinite(value) for value in values):
        return {**base, "ttm_status": "nonfinite_ttm_component", "ttm_method": method}
    ttm = float(sum(coefficient * float(value) for coefficient, value in zip(coefficients, values)))
    status = "ok" if ttm > 0 else "nonpositive_ttm_revenue"
    return {
        **base,
        "ttm_revenue": ttm if ttm > 0 else np.nan,
        "ttm_status": status,
        "ttm_method": method,
        "component_report_periods": "|".join(str(period.date()) for period in component_periods),
        "component_version_ids": "|".join(str(row["version_id"]) for row in components),
        "component_source_version_count": int(
            sum(int(row.get("equivalent_source_versions", 1)) for row in components)
        ),
        "announcement_at": max(row["announcement_at"] for row in components),
        "available_at": max(row["available_at"] for row in components),
        "source_ingested_at": max(row["source_ingested_at"] for row in components),
    }


def _attach_sales_to_price(
    base: pd.DataFrame,
    snapshots: pd.DataFrame,
    research_as_of: pd.Timestamp,
    spec: SalesToPriceInputSpec,
) -> pd.DataFrame:
    work = base.merge(
        snapshots,
        on=["instrument_id", "decision_at"],
        how="left",
        validate="many_to_one",
    )
    valid = (
        np.isfinite(pd.to_numeric(work["ttm_revenue"], errors="coerce"))
        & np.isfinite(pd.to_numeric(work["market_cap"], errors="coerce"))
        & work["market_cap"].gt(0)
        & work["listing_sessions"].ge(spec.min_listing_sessions)
    )
    work["factor_value"] = np.where(valid, work["ttm_revenue"] / work["market_cap"], np.nan)
    work["factor_name"] = SP_TTM.name
    work["factor_family"] = SP_TTM.family
    work["factor_definition_version"] = SP_TTM.version
    work["minimum_listing_sessions"] = spec.min_listing_sessions
    work["research_as_of_at"] = research_as_of
    work["ingested_at"] = work["source_ingested_at"]
    columns = [
        "instrument_id",
        "symbol",
        "factor_value",
        "industry_code",
        "market_cap",
        "research_eligible",
        "announcement_at",
        "available_at_y" if "available_at_y" in work else "available_at",
        "ingested_at",
        "research_as_of_at",
        "decision_at",
        "execution_at",
        "market_close_at",
        "listing_sessions",
        "ttm_revenue",
        "ttm_status",
        "ttm_method",
        "latest_report_period",
        "latest_company_type",
        "report_age_days",
        "component_report_periods",
        "component_version_ids",
        "component_source_version_count",
        "business_chain_reset_applied",
        "factor_name",
        "factor_family",
        "factor_definition_version",
        "minimum_listing_sessions",
    ]
    result = work[columns].copy()
    if "available_at_y" in result:
        result = result.rename(columns={"available_at_y": "available_at"})
    return result.sort_values(["decision_at", "instrument_id"]).reset_index(drop=True)


def _load_income_artifact(artifact: Path) -> tuple[pd.DataFrame, Dict[str, Any]]:
    manifest_path = artifact / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != "p08_fundamental_pit_v1":
        raise FundamentalFactorError("fundamental input is not a P0.8 PIT artifact")
    if not manifest.get("quality", {}).get("promotion_passed", False):
        raise FundamentalFactorError("fundamental PIT artifact was not promoted")
    metadata = manifest.get("outputs", {}).get("income")
    if not metadata:
        raise FundamentalFactorError("fundamental PIT artifact has no income output")
    path = artifact / Path(metadata["path"]).name
    actual_sha = _sha256(path)
    if actual_sha != metadata["sha256"]:
        raise FundamentalFactorError("fundamental income hash mismatch")
    return pd.read_parquet(path), {
        "artifact_id": manifest["artifact_id"],
        "manifest_sha256": _sha256(manifest_path),
        "income_sha256": actual_sha,
    }


def _load_business_resets(path: Path) -> tuple[Dict[str, pd.Timestamp], Dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    resets: Dict[str, pd.Timestamp] = {}
    for alias in payload.get("aliases", []):
        if alias.get("business_continuity") is False:
            if alias.get("fundamental_chain_policy") != "reset_at_effective_date":
                raise FundamentalFactorError("business discontinuity lacks reset policy")
            instrument = str(alias["stable_instrument_id"])
            effective = pd.Timestamp(alias["effective_date"]).normalize()
            if instrument in resets and resets[instrument] != effective:
                raise FundamentalFactorError(f"conflicting business reset dates: {instrument}")
            resets[instrument] = effective
    return resets, {
        "path": str(path.resolve()),
        "sha256": _sha256(path),
        "version": payload.get("version"),
        "business_reset_count": len(resets),
    }


def _quality_summary(
    observations: pd.DataFrame,
    snapshots: pd.DataFrame,
    labels: pd.DataFrame,
) -> Dict[str, Any]:
    eligible = observations.loc[observations["research_eligible"]].copy()
    finite = np.isfinite(eligible["factor_value"])
    coverage = eligible.assign(finite_factor=finite).groupby("decision_at", observed=True).agg(
        eligible_rows=("instrument_id", "size"),
        finite_factor_rows=("finite_factor", "sum"),
    )
    coverage["factor_coverage"] = coverage["finite_factor_rows"] / coverage["eligible_rows"]
    scope = observations.loc[observations["research_eligible"]].groupby(
        "decision_at", observed=True
    ).agg(
        base_rows=("instrument_id", "size"),
        universe_rows=("universe_in_scope", "sum"),
        applicable_rows=("evaluation_eligible", "sum"),
    )
    scope["universe_retention"] = scope["universe_rows"] / scope["base_rows"]
    scope["evaluation_retention"] = scope["applicable_rows"] / scope["base_rows"]
    statuses = snapshots["ttm_status"].value_counts(dropna=False).to_dict()
    exclusion_reasons = observations.loc[
        observations["research_eligible"] & ~observations["evaluation_eligible"],
        "scope_exclusion_reason",
    ].value_counts(dropna=False)
    return {
        "decision_dates": int(observations["decision_at"].nunique()),
        "observation_rows": len(observations),
        "eligible_rows": len(eligible),
        "finite_factor_rows": int(finite.sum()),
        "missing_factor_rows": int((~finite).sum()),
        "median_decision_coverage": float(coverage["factor_coverage"].median()),
        "minimum_decision_coverage": float(coverage["factor_coverage"].min()),
        "median_universe_retention": float(scope["universe_retention"].median()),
        "minimum_universe_retention": float(scope["universe_retention"].min()),
        "median_evaluation_retention": float(scope["evaluation_retention"].median()),
        "minimum_evaluation_retention": float(scope["evaluation_retention"].min()),
        "scope_exclusion_reason_counts": {
            str(key): int(value) for key, value in exclusion_reasons.items()
        },
        "ttm_status_counts": {str(key): int(value) for key, value in statuses.items()},
        "business_chain_reset_rows": int(
            snapshots["business_chain_reset_applied"].fillna(False).sum()
        ),
        "forward_return_rows": len(labels),
        "forward_return_missing_rows": int(labels["forward_return"].isna().sum()),
    }


def _implementation_identity() -> Dict[str, Any]:
    root = Path(__file__).resolve().parents[3]
    files = [
        Path(__file__),
        root / "src/qrp/research/factor_registry.py",
        root / "src/qrp/research/factor_universe.py",
        root / "src/qrp/research/price_reversal.py",
    ]
    entries = [
        {"path": str(path.resolve().relative_to(root)), "sha256": _sha256(path)}
        for path in files
    ]
    return {"tree_sha256": _fingerprint(entries), "files": entries}
