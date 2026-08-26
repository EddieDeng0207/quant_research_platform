"""Point-in-time TTM operating-cash-flow-to-price factor inputs."""

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

from .factor_registry import CFP_TTM
from .factor_universe import CN_A_FULL, ResearchUniverseSpec, attach_research_universe
from .fundamental_factors import (
    FundamentalFactorError,
    _fundamental_decision_schedule,
    _listing_sessions_since_actual_list_date,
    _load_business_resets,
    _load_research_calendar,
    _quality_summary,
)
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


@dataclass(frozen=True)
class CashFlowToPriceInputSpec:
    """Frozen construction policy for ordinary-company TTM operating CFP."""

    operating_cashflow_column: str = "n_cashflow_act"
    allowed_company_types: tuple[str, ...] = ("1",)
    max_report_age_days: int = 550
    min_listing_sessions: int = 120
    horizons: tuple[int, ...] = (5, 10, 20, 60)
    weekly_rule: str = "W-FRI"
    market_value_unit: str = "CNY"
    version: str = "cfp_ttm_pit_inputs_v1"

    def validate(self) -> "CashFlowToPriceInputSpec":
        if self.operating_cashflow_column != "n_cashflow_act":
            raise ValueError("CFP v1 freezes operating cash flow to n_cashflow_act")
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
            raise ValueError("CFP v1 freezes weekly decisions to W-FRI")
        if self.market_value_unit != "CNY":
            raise ValueError("CFP market values must be normalized to CNY")
        return self

    @property
    def fingerprint(self) -> str:
        return _fingerprint(asdict(self))


def build_cashflow_to_price_input_artifact(
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
    spec: Optional[CashFlowToPriceInputSpec] = None,
    universe_spec: Optional[ResearchUniverseSpec] = None,
    require_clean_git: bool = False,
) -> Path:
    """Build CFP observations and physically separate open-to-open labels."""
    frozen = (spec or CashFlowToPriceInputSpec()).validate()
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
        sessions, frozen.horizons, frozen.weekly_rule
    )
    if decisions.empty:
        raise FundamentalFactorError("no weekly decisions have complete outcome windows")

    lake = Path(lake_root)
    lake_entries = _lake_manifest_entries(lake)
    trading_calendar, calendar_identity = _load_research_calendar(
        lake, lake_entries, start, end, research_as_of
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
    cashflow, fundamental_identity = _load_cashflow_artifact(Path(fundamentals_artifact))
    resets, alias_identity = _load_business_resets(Path(aliases_path))

    market = _attach_causal_prices(market, adjustments)
    market["listing_sessions"] = _listing_sessions_since_actual_list_date(
        market, trading_calendar
    )
    base = _prepare_market_observation_base(market, decisions, indicators, membership)
    snapshots = build_pit_ttm_operating_cashflow_snapshots(
        cashflow, decisions, research_as_of, resets=resets, spec=frozen
    )
    observations = _attach_cashflow_to_price(base, snapshots, research_as_of, frozen)
    observations = attach_research_universe(observations, frozen_universe)
    labels = _prepare_forward_returns(
        market, {CFP_TTM.name: observations}, decisions, end, schedule_spec
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
        "factor_definition_sha256": CFP_TTM.fingerprint,
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
        ("observations_cfp_ttm", observations, ["decision_at", "instrument_id"]),
        ("ttm_operating_cashflow_diagnostics", snapshots, ["decision_at", "instrument_id"]),
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

    manifest = {
        "artifact_id": artifact_id,
        "schema_version": frozen.version,
        "identity": identity,
        "factor_definition": {**asdict(CFP_TTM), "sha256": CFP_TTM.fingerprint},
        "spec": {**asdict(frozen), "sha256": frozen.fingerprint},
        "research_universe": {
            **asdict(frozen_universe),
            "sha256": frozen_universe.fingerprint,
        },
        "outputs": outputs,
        "quality": _quality_summary(observations, snapshots, labels),
        "guardrails": {
            "report_period_is_not_knowledge_time": True,
            "each_ttm_component_selected_point_in_time": True,
            "raw_revisions_preserved_and_latest_known_version_selected": True,
            "quarterly_cumulative_values_not_annualized": True,
            "ttm_formula_current_ytd_plus_prior_fy_minus_prior_ytd": True,
            "missing_components_not_zero_filled": True,
            "finite_negative_operating_cashflow_retained": True,
            "ordinary_industrial_company_scope_only": True,
            "business_discontinuities_reset_fundamental_chain": True,
            "decision_date_market_value_in_cny": True,
            "research_eligibility_from_decision_date": True,
            "listing_age_uses_actual_list_date_and_frozen_calendar": True,
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


def build_pit_ttm_operating_cashflow_snapshots(
    cashflow: pd.DataFrame,
    decisions: pd.DataFrame,
    research_as_of_at: Any,
    *,
    resets: Optional[Mapping[str, pd.Timestamp]] = None,
    spec: Optional[CashFlowToPriceInputSpec] = None,
) -> pd.DataFrame:
    """Replay cash-flow revisions and calculate TTM operating cash flow."""
    frozen = (spec or CashFlowToPriceInputSpec()).validate()
    research_as_of = _utc_timestamp(research_as_of_at)
    decision_frame = decisions[["decision_date", "execution_date"]].copy()
    decision_frame["decision_at"] = _local_time(decision_frame["decision_date"], 16, 0)
    decision_frame = decision_frame.sort_values("decision_at").reset_index(drop=True)
    events = _prepare_cashflow_events(
        cashflow, research_as_of, decision_frame["decision_at"].max(), frozen
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
            result = _ttm_cashflow_from_known_periods(
                periods,
                pd.Timestamp(decision["decision_date"]),
                reset_map.get(instrument),
                frozen,
            )
            rows.append(
                {"instrument_id": instrument, "decision_at": decision["decision_at"], **result}
            )
    if not rows:
        raise FundamentalFactorError("no PIT cash-flow snapshots were available at decisions")
    return pd.DataFrame(rows).sort_values(["decision_at", "instrument_id"]).reset_index(drop=True)


def _prepare_cashflow_events(
    frame: pd.DataFrame,
    research_as_of: pd.Timestamp,
    last_decision_at: pd.Timestamp,
    spec: CashFlowToPriceInputSpec,
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
        spec.operating_cashflow_column,
    }
    missing = sorted(required - set(frame.columns))
    if missing:
        raise FundamentalFactorError(f"cash-flow artifact missing columns: {missing}")
    work = frame[list(required)].copy()
    for column in ("available_at", "announcement_at", "source_ingested_at"):
        work[column] = pd.to_datetime(work[column], utc=True, errors="coerce")
    work["report_period"] = pd.to_datetime(
        work["report_period"], errors="coerce"
    ).dt.normalize()
    if work[["available_at", "announcement_at", "source_ingested_at", "report_period"]].isna().any(axis=None):
        raise FundamentalFactorError("cash-flow artifact contains invalid PIT timestamps")
    work = work.loc[
        work["report_type"].astype("string").eq("1")
        & work["source_ingested_at"].le(research_as_of)
        & work["available_at"].le(last_decision_at)
    ].copy()
    if work.empty:
        raise FundamentalFactorError("no PIT cash-flow rows survive the frozen cutoffs")

    keys = ["instrument_id", "report_period", "available_at"]
    work = work.sort_values([*keys, "source_row_sha256", "source_row_occurrence"])
    work = work.drop_duplicates([*keys, "source_row_sha256"], keep="first")
    work["_latest_flag"] = work["update_flag"].astype("string").eq("1")
    has_latest = work.groupby(keys, observed=True)["_latest_flag"].transform("any")
    work = work.loc[~has_latest | work["_latest_flag"]].copy()
    value_column = spec.operating_cashflow_column
    work[value_column] = pd.to_numeric(work[value_column], errors="coerce")
    work["equivalent_source_versions"] = work.groupby(keys, observed=True)[
        "version_id"
    ].transform("size")
    work["equivalent_version_ids"] = work.groupby(keys, observed=True)[
        "version_id"
    ].transform(lambda values: ",".join(sorted(str(value) for value in values)))
    work["announcement_at"] = work.groupby(keys, observed=True)["announcement_at"].transform(
        "max"
    )
    work["source_ingested_at"] = work.groupby(keys, observed=True)[
        "source_ingested_at"
    ].transform("max")
    ambiguous_groups = []
    for key, group in work.groupby(keys, observed=True, sort=False):
        if len(group) == 1:
            continue
        values = group[value_column]
        equivalent = values.isna().all() or (
            values.notna().all() and float(values.max() - values.min()) == 0.0
        )
        if not equivalent:
            ambiguous_groups.append(key)
    if ambiguous_groups:
        raise FundamentalFactorError(
            "economically ambiguous cash-flow versions at one availability time: "
            f"{ambiguous_groups[:5]}"
        )
    work = work.sort_values([*keys, "source_row_sha256"]).drop_duplicates(keys, keep="first")
    return work.drop(columns="_latest_flag").sort_values("available_at").reset_index(drop=True)


def _ttm_cashflow_from_known_periods(
    periods: Mapping[pd.Timestamp, Mapping[str, Any]],
    decision_date: pd.Timestamp,
    reset_date: Optional[pd.Timestamp],
    spec: CashFlowToPriceInputSpec,
) -> Dict[str, Any]:
    known = {
        pd.Timestamp(period).normalize(): row
        for period, row in periods.items()
        if reset_date is None
        or (decision_date < reset_date and pd.Timestamp(period) < reset_date)
        or (decision_date >= reset_date and pd.Timestamp(period) >= reset_date)
    }
    empty = {
        "ttm_operating_cashflow": np.nan,
        "ttm_status": "no_statement_in_business_regime",
        "ttm_method": pd.NA,
        "latest_report_period": pd.NaT,
        "latest_company_type": pd.NA,
        "report_age_days": pd.NA,
        "component_report_periods": pd.NA,
        "component_version_ids": pd.NA,
        "component_equivalent_version_ids": pd.NA,
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
    values = [
        pd.to_numeric(row[spec.operating_cashflow_column], errors="coerce")
        for row in components
    ]
    if not all(np.isfinite(value) for value in values):
        return {**base, "ttm_status": "nonfinite_ttm_component", "ttm_method": method}
    ttm = float(
        sum(coefficient * float(value) for coefficient, value in zip(coefficients, values))
    )
    return {
        **base,
        "ttm_operating_cashflow": ttm,
        "ttm_status": "ok",
        "ttm_method": method,
        "component_report_periods": "|".join(str(period.date()) for period in component_periods),
        "component_version_ids": "|".join(str(row["version_id"]) for row in components),
        "component_equivalent_version_ids": "|".join(
            str(row.get("equivalent_version_ids", row["version_id"])) for row in components
        ),
        "component_source_version_count": int(
            sum(int(row.get("equivalent_source_versions", 1)) for row in components)
        ),
        "announcement_at": max(row["announcement_at"] for row in components),
        "available_at": max(row["available_at"] for row in components),
        "source_ingested_at": max(row["source_ingested_at"] for row in components),
    }


def _attach_cashflow_to_price(
    base: pd.DataFrame,
    snapshots: pd.DataFrame,
    research_as_of: pd.Timestamp,
    spec: CashFlowToPriceInputSpec,
) -> pd.DataFrame:
    work = base.merge(
        snapshots, on=["instrument_id", "decision_at"], how="left", validate="many_to_one"
    )
    valid = (
        np.isfinite(pd.to_numeric(work["ttm_operating_cashflow"], errors="coerce"))
        & np.isfinite(pd.to_numeric(work["market_cap"], errors="coerce"))
        & work["market_cap"].gt(0)
        & work["listing_sessions"].ge(spec.min_listing_sessions)
    )
    work["factor_value"] = np.where(
        valid, work["ttm_operating_cashflow"] / work["market_cap"], np.nan
    )
    work["factor_name"] = CFP_TTM.name
    work["factor_family"] = CFP_TTM.family
    work["factor_definition_version"] = CFP_TTM.version
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
        "ttm_operating_cashflow",
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


def _load_cashflow_artifact(artifact: Path) -> tuple[pd.DataFrame, Dict[str, Any]]:
    manifest_path = artifact / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != "p08_fundamental_pit_v1":
        raise FundamentalFactorError("fundamental input is not a P0.8 PIT artifact")
    if not manifest.get("quality", {}).get("promotion_passed", False):
        raise FundamentalFactorError("fundamental PIT artifact was not promoted")
    metadata = manifest.get("outputs", {}).get("cashflow")
    if not metadata:
        raise FundamentalFactorError("fundamental PIT artifact has no cashflow output")
    path = artifact / Path(metadata["path"]).name
    actual_sha = _sha256(path)
    if actual_sha != metadata["sha256"]:
        raise FundamentalFactorError("fundamental cashflow hash mismatch")
    return pd.read_parquet(path), {
        "artifact_id": manifest["artifact_id"],
        "manifest_sha256": _sha256(manifest_path),
        "cashflow_sha256": actual_sha,
    }


def _implementation_identity() -> Dict[str, Any]:
    root = Path(__file__).resolve().parents[3]
    files = [
        Path(__file__),
        root / "src/qrp/research/factor_registry.py",
        root / "src/qrp/research/factor_universe.py",
        root / "src/qrp/research/fundamental_factors.py",
        root / "src/qrp/research/price_reversal.py",
    ]
    entries = [
        {"path": str(path.resolve().relative_to(root)), "sha256": _sha256(path)}
        for path in files
    ]
    return {"tree_sha256": _fingerprint(entries), "files": entries}
