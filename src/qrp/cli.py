"""Command-line entry point for reproducible data ingestion."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Iterable, List

import pandas as pd

from .backtest import BacktestSpec, build_backtest_artifact, generate_backtest_report
from .config import load_env_file
from .data.audit import audit_lake, write_audit_report
from .data.corporate_actions import build_corporate_action_artifact
from .data.curated import build_curated_price_artifact
from .data.fundamental_ingestion import (
    FUNDAMENTAL_STATEMENTS,
    FundamentalBackfillConfig,
    FundamentalIngestionRunner,
)
from .data.fundamentals import build_fundamental_pit_artifact
from .data.industry import build_historical_industry_artifact
from .data.industry_coverage import build_industry_coverage_audit
from .data.industry_ingestion import (
    INDUSTRY_TAXONOMIES,
    IndustryBackfillConfig,
    IndustryIngestionRunner,
)
from .data.ingestion import (
    DEFAULT_REQUESTS_PER_MINUTE,
    P0BackfillConfig,
    P0IngestionRunner,
    RateLimiter,
)
from .data.providers.base import FetchResult, ProviderError
from .data.readiness import (
    DEFAULT_MAXIMUM_AVAILABILITY_STALENESS_DAYS,
    DEFAULT_MAXIMUM_REPORT_PERIOD_STALENESS_DAYS,
    audit_research_readiness,
    write_research_readiness_report,
)
from .data.registry import PROVIDER_CAPABILITIES, create_provider
from .data.reporting import generate_ingestion_report
from .data.storage import ParquetLake
from .data.tradability import build_tradability_artifact
from .execution import (
    ExecutionSpec,
    FeePolicy,
    build_calibration_artifact,
    build_execution_artifact,
    build_lagged_capacity_panel,
    generate_target_weight_orders,
)
from .research import (
    FactorEvaluationSpec,
    build_factor_evaluation_artifact,
    generate_factor_evaluation_report,
)


def _write_results(results: Iterable[FetchResult], data_root: str) -> None:
    lake = ParquetLake(Path(data_root))
    for result in results:
        path = lake.write(result)
        print(f"wrote {len(result.frame):,} rows -> {path}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="qrp-data")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("providers", help="list built-in providers and capabilities")

    bars = subparsers.add_parser("bars", help="ingest canonical daily bars")
    bars.add_argument("--provider", choices=("akshare", "tushare"), required=True)
    bars.add_argument("--symbol", action="append", required=True)
    bars.add_argument("--start", required=True)
    bars.add_argument("--end", required=True)
    bars.add_argument("--adjustment", choices=("raw", "qfq", "hfq"), default="raw")
    bars.add_argument("--data-root", default="data/lake")

    instruments = subparsers.add_parser("instruments", help="ingest security master")
    instruments.add_argument("--provider", choices=("akshare", "tushare"), required=True)
    instruments.add_argument("--data-root", default="data/lake")

    calendar = subparsers.add_parser("calendar", help="ingest an exchange calendar")
    calendar.add_argument("--provider", choices=("tushare",), default="tushare")
    calendar.add_argument("--exchange", default="SSE")
    calendar.add_argument("--start", required=True)
    calendar.add_argument("--end", required=True)
    calendar.add_argument("--data-root", default="data/lake")

    adjustments = subparsers.add_parser("adjustments", help="ingest raw adjustment factors")
    adjustments.add_argument("--provider", choices=("tushare",), default="tushare")
    adjustments.add_argument("--symbol", action="append", required=True)
    adjustments.add_argument("--start", required=True)
    adjustments.add_argument("--end", required=True)
    adjustments.add_argument("--data-root", default="data/lake")

    indicators = subparsers.add_parser("daily-indicators", help="ingest valuation/size fields")
    indicators.add_argument("--provider", choices=("tushare",), default="tushare")
    indicators.add_argument("--symbol", action="append", required=True)
    indicators.add_argument("--start", required=True)
    indicators.add_argument("--end", required=True)
    indicators.add_argument("--data-root", default="data/lake")

    fundamentals = subparsers.add_parser("fundamentals", help="ingest PIT fundamentals")
    fundamentals.add_argument("--provider", choices=("tushare",), default="tushare")
    fundamentals.add_argument(
        "--statement",
        choices=("income", "balance_sheet", "cashflow", "financial_indicators"),
        required=True,
    )
    fundamentals.add_argument("--symbol", action="append", required=True)
    fundamentals.add_argument(
        "--start",
        required=True,
        help="announcement start for statements; report-period start for indicators",
    )
    fundamentals.add_argument(
        "--end",
        required=True,
        help="announcement end for statements; report-period end for indicators",
    )
    fundamentals.add_argument("--period")
    fundamentals.add_argument("--data-root", default="data/lake")

    actions = subparsers.add_parser(
        "corporate-actions", help="ingest versioned Tushare dividend/bonus records"
    )
    actions.add_argument("--provider", choices=("tushare",), default="tushare")
    actions.add_argument("--symbol", action="append", required=True)
    actions.add_argument("--start", required=True, help="announcement start date")
    actions.add_argument("--end", required=True, help="announcement end date")
    actions.add_argument("--data-root", default="data/lake")

    macro = subparsers.add_parser("macro", help="ingest FRED/ALFRED observations")
    macro.add_argument("--provider", choices=("fred",), default="fred")
    macro.add_argument("--series", action="append", required=True)
    macro.add_argument("--start")
    macro.add_argument("--end")
    macro.add_argument("--realtime-start")
    macro.add_argument("--realtime-end")
    macro.add_argument("--data-root", default="data/lake")

    backfill = subparsers.add_parser(
        "backfill-p0", help="run or resume full-market P0 ingestion by trade date"
    )
    backfill.add_argument("--start", required=True)
    backfill.add_argument("--end", required=True)
    backfill.add_argument(
        "--dataset",
        action="append",
        choices=(
            "daily_bars",
            "adjustment_factors",
            "daily_indicators",
            "stock_status",
            "daily_limits",
            "daily_suspensions",
            "historical_instruments",
        ),
        help="repeat to select datasets; defaults to bars, adjustments, and indicators",
    )
    backfill.add_argument("--exchange", default="SSE")
    backfill.add_argument(
        "--requests-per-minute", type=int, default=DEFAULT_REQUESTS_PER_MINUTE
    )
    backfill.add_argument("--max-attempts", type=int, default=3)
    backfill.add_argument("--retry-base-seconds", type=float, default=2.0)
    backfill.add_argument("--workers", type=int, default=1)
    backfill.add_argument("--job-name", default="p0_backfill")
    backfill.add_argument("--skip-instruments", action="store_true")
    backfill.add_argument("--data-root", default="data/lake")
    backfill.add_argument("--artifact-root", default="artifacts")
    backfill.add_argument("--state-root", default="state")

    p05 = subparsers.add_parser(
        "backfill-p05", help="run or resume full-market tradability input ingestion"
    )
    p05.add_argument("--start", required=True)
    p05.add_argument("--end", required=True)
    p05.add_argument("--exchange", default="SSE")
    p05.add_argument(
        "--requests-per-minute", type=int, default=DEFAULT_REQUESTS_PER_MINUTE
    )
    p05.add_argument("--max-attempts", type=int, default=3)
    p05.add_argument("--retry-base-seconds", type=float, default=2.0)
    p05.add_argument("--workers", type=int, default=1)
    p05.add_argument("--job-name", default="p05_tradability_backfill")
    p05.add_argument("--data-root", default="data/lake")
    p05.add_argument("--artifact-root", default="artifacts")
    p05.add_argument("--state-root", default="state")

    fundamental_backfill = subparsers.add_parser(
        "backfill-fundamentals",
        help="run or resume the full-universe P0.8 financial-statement grid",
    )
    fundamental_backfill.add_argument("--start", required=True)
    fundamental_backfill.add_argument("--end", required=True)
    fundamental_backfill.add_argument(
        "--statement",
        action="append",
        choices=FUNDAMENTAL_STATEMENTS,
        help="repeat to select statements; defaults to all four families",
    )
    fundamental_backfill.add_argument(
        "--symbol",
        action="append",
        help="optional explicit pilot universe; defaults to the frozen full security master",
    )
    fundamental_backfill.add_argument(
        "--requests-per-minute", type=int, default=DEFAULT_REQUESTS_PER_MINUTE
    )
    fundamental_backfill.add_argument("--max-attempts", type=int, default=3)
    fundamental_backfill.add_argument("--retry-base-seconds", type=float, default=2.0)
    fundamental_backfill.add_argument("--workers", type=int, default=1)
    fundamental_backfill.add_argument(
        "--by-period-vip",
        action="store_true",
        help="use full-market quarterly VIP endpoints instead of the per-symbol grid",
    )
    fundamental_backfill.add_argument(
        "--job-name", default="p08_fundamentals_backfill"
    )
    fundamental_backfill.add_argument("--data-root", default="data/lake")
    fundamental_backfill.add_argument("--artifact-root", default="artifacts")
    fundamental_backfill.add_argument("--state-root", default="state")

    fundamental_pit = subparsers.add_parser(
        "build-fundamentals-pit",
        help="build an immutable next-session-available P0.8 financial artifact",
    )
    fundamental_pit.add_argument("--run", required=True)
    fundamental_pit.add_argument("--calendar", required=True)
    fundamental_pit.add_argument("--data-root", default="data/lake")
    fundamental_pit.add_argument("--output-root", default="data/curated")
    fundamental_pit.add_argument("--as-of-ingested-at")
    fundamental_pit.add_argument(
        "--aliases", default="configs/instrument_aliases.json"
    )
    fundamental_pit.add_argument("--security-code-mappings")
    fundamental_pit.add_argument("--allow-failed-promotion", action="store_true")
    fundamental_pit.add_argument(
        "--allow-dirty-code",
        action="store_true",
        help="allow exploratory output without a clean committed Git identity",
    )

    industry_backfill = subparsers.add_parser(
        "backfill-industry",
        help="run or resume SW2014/SW2021 L1 historical membership ingestion",
    )
    industry_backfill.add_argument(
        "--taxonomy",
        action="append",
        choices=INDUSTRY_TAXONOMIES,
        help="repeat to select taxonomies; defaults to both historical regimes",
    )
    industry_backfill.add_argument(
        "--requests-per-minute", type=int, default=DEFAULT_REQUESTS_PER_MINUTE
    )
    industry_backfill.add_argument("--max-attempts", type=int, default=5)
    industry_backfill.add_argument("--retry-base-seconds", type=float, default=2.0)
    industry_backfill.add_argument(
        "--job-name", default="p08_industry_membership_backfill"
    )
    industry_backfill.add_argument("--data-root", default="data/lake")
    industry_backfill.add_argument("--artifact-root", default="artifacts")
    industry_backfill.add_argument("--state-root", default="state")

    industry_pit = subparsers.add_parser(
        "build-industry-pit",
        help="build a taxonomy-versioned immutable historical industry artifact",
    )
    industry_pit.add_argument("--run", required=True)
    industry_pit.add_argument("--start", required=True)
    industry_pit.add_argument("--end", required=True)
    industry_pit.add_argument("--data-root", default="data/lake")
    industry_pit.add_argument("--output-root", default="data/curated")
    industry_pit.add_argument(
        "--aliases", default="configs/instrument_aliases.json"
    )
    industry_pit.add_argument("--security-code-mappings")
    industry_pit.add_argument("--allow-failed-promotion", action="store_true")
    industry_pit.add_argument("--allow-dirty-code", action="store_true")

    industry_coverage = subparsers.add_parser(
        "audit-industry-coverage",
        help="build a PIT daily financial-to-industry identity coverage audit",
    )
    industry_coverage.add_argument("--fundamental-artifact", required=True)
    industry_coverage.add_argument("--industry-artifact", required=True)
    industry_coverage.add_argument("--calendar", required=True)
    industry_coverage.add_argument("--start", required=True)
    industry_coverage.add_argument("--end", required=True)
    industry_coverage.add_argument("--minimum-coverage", type=float, default=0.80)
    industry_coverage.add_argument(
        "--output-root", default="artifacts/audits/industry_coverage"
    )
    industry_coverage.add_argument("--allow-dirty-code", action="store_true")

    readiness = subparsers.add_parser(
        "audit-research-readiness",
        help="gate multi-year market, fundamental, and historical-industry coverage",
    )
    readiness.add_argument("--start", required=True)
    readiness.add_argument("--end", required=True)
    readiness.add_argument("--calendar", required=True)
    readiness.add_argument("--fundamental-artifact")
    readiness.add_argument("--industry-membership")
    readiness.add_argument("--minimum-weekly-periods", type=int, default=104)
    readiness.add_argument("--minimum-fundamental-symbols", type=int, default=200)
    readiness.add_argument("--minimum-industry-instruments", type=int, default=200)
    readiness.add_argument(
        "--maximum-report-period-staleness-days",
        type=int,
        default=DEFAULT_MAXIMUM_REPORT_PERIOD_STALENESS_DAYS,
    )
    readiness.add_argument(
        "--maximum-availability-staleness-days",
        type=int,
        default=DEFAULT_MAXIMUM_AVAILABILITY_STALENESS_DAYS,
    )
    readiness.add_argument("--data-root", default="data/lake")
    readiness.add_argument(
        "--output", default="artifacts/audits/latest_research_readiness.json"
    )
    readiness.add_argument("--allow-not-ready", action="store_true")

    limits = subparsers.add_parser("daily-limits", help="ingest full-market daily price limits")
    limits.add_argument("--provider", choices=("tushare",), default="tushare")
    limits.add_argument("--date", required=True)
    limits.add_argument("--data-root", default="data/lake")

    suspensions = subparsers.add_parser(
        "suspensions", help="ingest a full-market daily suspension/resumption event set"
    )
    suspensions.add_argument("--provider", choices=("tushare",), default="tushare")
    suspensions.add_argument("--date", required=True)
    suspensions.add_argument("--data-root", default="data/lake")

    mappings = subparsers.add_parser(
        "bse-mappings", help="ingest the BSE old/new security-code crosswalk"
    )
    mappings.add_argument("--provider", choices=("tushare",), default="tushare")
    mappings.add_argument("--data-root", default="data/lake")

    audit = subparsers.add_parser("audit-lake", help="verify hashes, row counts, and contracts")
    audit.add_argument("--data-root", default="data/lake")
    audit.add_argument("--output", default="artifacts/audits/latest_lake_audit.json")

    report = subparsers.add_parser(
        "report-ingestion", help="generate a human-readable report from run artifacts"
    )
    report.add_argument("--run", required=True)
    report.add_argument("--audit", required=True)
    report.add_argument("--output", required=True)

    build_prices = subparsers.add_parser(
        "build-prices", help="build an immutable causal/adjusted price artifact"
    )
    build_prices.add_argument("--start", required=True)
    build_prices.add_argument("--end", required=True)
    build_prices.add_argument(
        "--mode",
        choices=("causal_returns", "qfq_asof", "hfq", "total_return_index"),
        default="causal_returns",
    )
    build_prices.add_argument("--as-of-date")
    build_prices.add_argument("--base-date")
    build_prices.add_argument("--as-of-ingested-at")
    build_prices.add_argument("--data-root", default="data/lake")
    build_prices.add_argument("--output-root", default="data/curated")

    build_tradability = subparsers.add_parser(
        "build-tradability", help="build a fail-closed daily execution eligibility matrix"
    )
    build_tradability.add_argument("--start", required=True)
    build_tradability.add_argument("--end", required=True)
    build_tradability.add_argument("--as-of-ingested-at")
    build_tradability.add_argument("--data-root", default="data/lake")
    build_tradability.add_argument("--output-root", default="data/curated")
    build_tradability.add_argument(
        "--allow-failed-promotion",
        action="store_true",
        help="retain and return a diagnostic artifact even when hard quality gates fail",
    )

    build_execution = subparsers.add_parser(
        "build-execution", help="execute a frozen order blotter against a promoted P0.5 artifact"
    )
    build_execution.add_argument("--tradability-artifact", required=True)
    build_execution.add_argument("--orders", required=True)
    build_execution.add_argument("--initial-cash", type=float, required=True)
    build_execution.add_argument("--output-root", default="data/curated")
    build_execution.add_argument("--commission-bps", type=float, default=3.0)
    build_execution.add_argument("--minimum-commission", type=float, default=5.0)
    build_execution.add_argument("--max-participation", type=float, default=0.01)
    build_execution.add_argument("--liquidity-haircut", type=float, default=1.0)
    build_execution.add_argument(
        "--max-position-free-float", type=float, default=0.001
    )
    build_execution.add_argument("--base-slippage-bps", type=float, default=5.0)
    build_execution.add_argument("--impact-y", type=float, default=0.50)
    build_execution.add_argument(
        "--max-executable-impact-bps", type=float, default=100.0
    )
    build_execution.add_argument(
        "--allow-nonstandard-universe",
        action="store_true",
        help="allow explicitly ordered ST/nonstandard securities; P0.5 legal constraints still apply",
    )

    capacity = subparsers.add_parser(
        "build-capacity-panel",
        help="build lagged 20/60-day amount, share and free-float capacity inputs",
    )
    capacity.add_argument("--bars", required=True)
    capacity.add_argument("--daily-indicators", required=True)
    capacity.add_argument("--adjustment-factors", required=True)
    capacity.add_argument("--output", required=True)
    capacity.add_argument("--min-periods-20", type=int, default=20)
    capacity.add_argument("--min-periods-60", type=int, default=60)

    target_orders = subparsers.add_parser(
        "build-target-orders", help="convert one frozen target portfolio into net share orders"
    )
    target_orders.add_argument("--targets", required=True)
    target_orders.add_argument("--positions", required=True)
    target_orders.add_argument("--prices", required=True)
    target_orders.add_argument("--capacity-panel")
    target_orders.add_argument("--portfolio-nav", type=float, required=True)
    target_orders.add_argument("--cash-buffer", type=float, default=0.02)
    target_orders.add_argument("--output", required=True)

    calibration = subparsers.add_parser(
        "calibrate-execution", help="freeze and summarize real broker fill calibration data"
    )
    calibration.add_argument("--fills", required=True)
    calibration.add_argument("--output-root", default="data/curated")
    calibration.add_argument("--minimum-group-samples", type=int, default=30)

    build_actions = subparsers.add_parser(
        "build-corporate-actions",
        help="build causal implementation events from explicit raw snapshots",
    )
    build_actions.add_argument("--raw", action="append", required=True)
    build_actions.add_argument("--tradability-artifact", required=True)
    build_actions.add_argument("--output-root", default="data/curated")
    build_actions.add_argument("--allow-failed-promotion", action="store_true")

    backtest = subparsers.add_parser(
        "build-backtest",
        help="run the chronological P0.6.3 portfolio backtest and freeze all ledgers",
    )
    backtest.add_argument("--tradability-artifact", required=True)
    backtest.add_argument("--capacity", required=True)
    backtest.add_argument("--targets", required=True)
    backtest.add_argument("--corporate-actions")
    backtest.add_argument("--initial-positions")
    backtest.add_argument("--initial-cash", type=float, required=True)
    backtest.add_argument("--output-root", default="data/curated")
    backtest.add_argument("--cash-buffer", type=float, default=0.02)
    backtest.add_argument("--max-stale-sessions", type=int, default=20)
    backtest.add_argument("--commission-bps", type=float, default=3.0)
    backtest.add_argument("--minimum-commission", type=float, default=5.0)
    backtest.add_argument("--max-participation", type=float, default=0.01)
    backtest.add_argument("--liquidity-haircut", type=float, default=1.0)
    backtest.add_argument("--max-position-free-float", type=float, default=0.001)
    backtest.add_argument("--normal-exit-participation", type=float, default=0.05)
    backtest.add_argument("--stress-exit-participation", type=float, default=0.02)
    backtest.add_argument("--max-stress-exit-days", type=float, default=3.0)
    backtest.add_argument("--base-slippage-bps", type=float, default=5.0)
    backtest.add_argument("--impact-y", type=float, default=0.50)
    backtest.add_argument(
        "--max-executable-impact-bps", type=float, default=100.0
    )
    backtest.add_argument(
        "--minimum-routine-trade-notional", type=float, default=0.0
    )
    backtest.add_argument(
        "--allow-dirty-code",
        action="store_true",
        help="allow exploratory output without a clean committed Git identity",
    )

    backtest_report = subparsers.add_parser(
        "report-backtest", help="generate a verified Markdown report from a P0.6.3 artifact"
    )
    backtest_report.add_argument("--artifact", required=True)
    backtest_report.add_argument("--output", required=True)

    factor = subparsers.add_parser(
        "build-factor-evaluation",
        help="build a PIT-safe, neutralized P0.7 single-factor evaluation artifact",
    )
    factor.add_argument("--observations", required=True)
    factor.add_argument("--forward-returns", required=True)
    factor.add_argument("--factor-name", required=True)
    factor.add_argument(
        "--factor-family",
        choices=(
            "fundamental",
            "analyst_expectation",
            "alternative",
            "price_volume_close",
        ),
        required=True,
    )
    factor.add_argument("--expected-direction", type=int, choices=(-1, 1), default=1)
    factor.add_argument("--quantiles", type=int, default=5)
    factor.add_argument("--winsor-mad-multiplier", type=float, default=5.0)
    factor.add_argument("--min-cross-section", type=int, default=200)
    factor.add_argument("--min-ic-observations", type=int, default=100)
    factor.add_argument("--min-evaluation-periods", type=int, default=104)
    factor.add_argument("--min-industry-members", type=int, default=5)
    factor.add_argument("--minimum-coverage", type=float, default=0.80)
    factor.add_argument("--minimum-label-match-rate", type=float, default=0.95)
    factor.add_argument(
        "--neutralization-weighting",
        choices=("equal", "sqrt_market_cap"),
        default="sqrt_market_cap",
    )
    factor.add_argument("--target-gross-weight", type=float, default=0.98)
    factor.add_argument("--annualization-periods", type=int, default=52)
    factor.add_argument("--annualization-frequency-tolerance", type=float, default=0.15)
    factor.add_argument("--minimum-newey-west-lags", type=int, default=1)
    factor.add_argument(
        "--return-basis", choices=("raw", "residualized"), default="raw"
    )
    factor.add_argument("--industry-active-weight-floor", type=float, default=0.05)
    factor.add_argument("--log-market-cap-z-floor", type=float, default=0.25)
    factor.add_argument("--exposure-sampling-sigma-multiplier", type=float, default=4.0)
    factor.add_argument("--output-root", default="data/curated")
    factor.add_argument("--allow-failed-promotion", action="store_true")
    factor.add_argument(
        "--allow-dirty-code",
        action="store_true",
        help="allow exploratory output without a clean committed Git identity",
    )

    factor_report = subparsers.add_parser(
        "report-factor", help="generate a verified Markdown report from a P0.7 artifact"
    )
    factor_report.add_argument("--artifact", required=True)
    factor_report.add_argument("--output", required=True)
    return parser


def main(argv: List[str] = None) -> int:
    load_env_file(Path(".env"))
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "providers":
        print(json.dumps(PROVIDER_CAPABILITIES, indent=2, ensure_ascii=False))
        return 0
    if args.command == "audit-lake":
        report = audit_lake(Path(args.data_root))
        write_audit_report(report, Path(args.output))
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
        return 0 if report.passed else 1
    if args.command == "report-ingestion":
        output = generate_ingestion_report(
            Path(args.run), Path(args.audit), Path(args.output)
        )
        print(f"wrote ingestion report -> {output}")
        return 0
    if args.command == "build-prices":
        output = build_curated_price_artifact(
            lake_root=Path(args.data_root),
            output_root=Path(args.output_root),
            start_date=args.start,
            end_date=args.end,
            mode=args.mode,
            as_of_date=args.as_of_date,
            base_date=args.base_date,
            as_of_ingested_at=args.as_of_ingested_at,
        )
        print(f"built curated price artifact -> {output}")
        return 0
    if args.command == "build-tradability":
        output = build_tradability_artifact(
            lake_root=Path(args.data_root),
            output_root=Path(args.output_root),
            start_date=args.start,
            end_date=args.end,
            as_of_ingested_at=args.as_of_ingested_at,
            strict=not args.allow_failed_promotion,
        )
        print(f"built tradability artifact -> {output}")
        return 0
    if args.command == "build-execution":
        output = build_execution_artifact(
            tradability_artifact=Path(args.tradability_artifact),
            orders_path=Path(args.orders),
            output_root=Path(args.output_root),
            initial_cash=args.initial_cash,
            spec=ExecutionSpec(
                max_participation_rate=args.max_participation,
                liquidity_haircut=args.liquidity_haircut,
                max_position_free_float_fraction=args.max_position_free_float,
                base_slippage_bps=args.base_slippage_bps,
                impact_y=args.impact_y,
                max_executable_impact_bps=args.max_executable_impact_bps,
                require_standard_research_eligible=not args.allow_nonstandard_universe,
            ),
            fees=FeePolicy(
                commission_bps=args.commission_bps,
                minimum_commission_cny=args.minimum_commission,
            ),
        )
        print(f"built execution artifact -> {output}")
        return 0
    if args.command == "build-capacity-panel":
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        panel = build_lagged_capacity_panel(
            _read_frame(Path(args.bars)),
            _read_frame(Path(args.daily_indicators)),
            _read_frame(Path(args.adjustment_factors)),
            min_periods_20=args.min_periods_20,
            min_periods_60=args.min_periods_60,
        )
        _write_frame(panel, output)
        print(f"built lagged capacity panel -> {output}")
        return 0
    if args.command == "build-target-orders":
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        capacity_panel = (
            _read_frame(Path(args.capacity_panel)) if args.capacity_panel else None
        )
        orders = generate_target_weight_orders(
            _read_frame(Path(args.targets)),
            _read_frame(Path(args.positions)),
            _read_frame(Path(args.prices)),
            portfolio_nav=args.portfolio_nav,
            cash_buffer_fraction=args.cash_buffer,
            capacity_panel=capacity_panel,
        )
        _write_frame(orders, output)
        print(f"built target-weight orders -> {output}")
        return 0
    if args.command == "calibrate-execution":
        output = build_calibration_artifact(
            Path(args.fills),
            Path(args.output_root),
            minimum_group_samples=args.minimum_group_samples,
        )
        print(f"built execution calibration artifact -> {output}")
        return 0
    if args.command == "build-corporate-actions":
        output = build_corporate_action_artifact(
            [Path(path) for path in args.raw],
            Path(args.tradability_artifact),
            Path(args.output_root),
            strict=not args.allow_failed_promotion,
        )
        print(f"built corporate-action artifact -> {output}")
        return 0
    if args.command == "build-backtest":
        output = build_backtest_artifact(
            tradability_artifact=Path(args.tradability_artifact),
            capacity_path=Path(args.capacity),
            targets_path=Path(args.targets),
            output_root=Path(args.output_root),
            initial_cash=args.initial_cash,
            corporate_actions_path=(
                Path(args.corporate_actions) if args.corporate_actions else None
            ),
            initial_positions_path=(
                Path(args.initial_positions) if args.initial_positions else None
            ),
            backtest_spec=BacktestSpec(
                cash_buffer_fraction=args.cash_buffer,
                max_stale_valuation_sessions=args.max_stale_sessions,
            ),
            execution_spec=ExecutionSpec(
                max_participation_rate=args.max_participation,
                liquidity_haircut=args.liquidity_haircut,
                max_position_free_float_fraction=args.max_position_free_float,
                normal_exit_participation_rate=args.normal_exit_participation,
                stress_exit_participation_rate=args.stress_exit_participation,
                max_stress_exit_days=args.max_stress_exit_days,
                base_slippage_bps=args.base_slippage_bps,
                impact_y=args.impact_y,
                max_executable_impact_bps=args.max_executable_impact_bps,
                minimum_routine_trade_notional_cny=(
                    args.minimum_routine_trade_notional
                ),
            ),
            fees=FeePolicy(
                commission_bps=args.commission_bps,
                minimum_commission_cny=args.minimum_commission,
            ),
            require_clean_git=not args.allow_dirty_code,
        )
        print(f"built P0.6.3 backtest artifact -> {output}")
        return 0
    if args.command == "report-backtest":
        output = generate_backtest_report(Path(args.artifact), Path(args.output))
        print(f"built P0.6.3 report -> {output}")
        return 0
    if args.command == "build-factor-evaluation":
        output = build_factor_evaluation_artifact(
            Path(args.observations),
            Path(args.forward_returns),
            Path(args.output_root),
            spec=FactorEvaluationSpec(
                factor_name=args.factor_name,
                factor_family=args.factor_family,
                expected_direction=args.expected_direction,
                quantiles=args.quantiles,
                winsor_mad_multiplier=args.winsor_mad_multiplier,
                min_cross_section=args.min_cross_section,
                min_ic_observations=args.min_ic_observations,
                min_evaluation_periods=args.min_evaluation_periods,
                min_industry_members=args.min_industry_members,
                minimum_coverage=args.minimum_coverage,
                minimum_label_match_rate=args.minimum_label_match_rate,
                neutralization_weighting=args.neutralization_weighting,
                target_gross_weight=args.target_gross_weight,
                annualization_periods=args.annualization_periods,
                annualization_frequency_tolerance=(
                    args.annualization_frequency_tolerance
                ),
                minimum_newey_west_lags=args.minimum_newey_west_lags,
                return_basis=args.return_basis,
                industry_active_weight_floor=args.industry_active_weight_floor,
                log_market_cap_z_floor=args.log_market_cap_z_floor,
                exposure_sampling_sigma_multiplier=(
                    args.exposure_sampling_sigma_multiplier
                ),
            ),
            require_clean_git=not args.allow_dirty_code,
            strict=not args.allow_failed_promotion,
        )
        print(f"built P0.7 factor evaluation artifact -> {output}")
        return 0
    if args.command == "report-factor":
        output = generate_factor_evaluation_report(
            Path(args.artifact), Path(args.output)
        )
        print(f"built P0.7 factor report -> {output}")
        return 0
    try:
        if args.command == "backfill-industry":
            provider = create_provider("tushare")
            run_path = IndustryIngestionRunner(
                provider=provider,
                lake=ParquetLake(Path(args.data_root)),
                artifact_root=Path(args.artifact_root),
                state_root=Path(args.state_root),
            ).run(
                IndustryBackfillConfig(
                    taxonomies=tuple(args.taxonomy or INDUSTRY_TAXONOMIES),
                    requests_per_minute=args.requests_per_minute,
                    max_attempts=args.max_attempts,
                    retry_base_seconds=args.retry_base_seconds,
                    job_name=args.job_name,
                )
            )
            print(f"completed P0.8 industry ingestion -> {run_path}")
            return 0

        if args.command == "build-industry-pit":
            output = build_historical_industry_artifact(
                run_path=Path(args.run),
                lake_root=Path(args.data_root),
                output_root=Path(args.output_root),
                start_date=args.start,
                end_date=args.end,
                aliases_path=Path(args.aliases) if args.aliases else None,
                security_code_mappings_path=(
                    Path(args.security_code_mappings)
                    if args.security_code_mappings
                    else None
                ),
                require_clean_git=not args.allow_dirty_code,
                strict=not args.allow_failed_promotion,
            )
            print(f"built P0.8 historical industry artifact -> {output}")
            return 0

        if args.command == "audit-industry-coverage":
            output = build_industry_coverage_audit(
                fundamental_artifact=Path(args.fundamental_artifact),
                industry_artifact=Path(args.industry_artifact),
                calendar_path=Path(args.calendar),
                output_root=Path(args.output_root),
                start_date=args.start,
                end_date=args.end,
                minimum_coverage=args.minimum_coverage,
                require_clean_git=not args.allow_dirty_code,
            )
            print(f"built P0.8 industry coverage audit -> {output}")
            return 0

        if args.command == "backfill-fundamentals":
            provider = create_provider("tushare")
            config = FundamentalBackfillConfig(
                start_date=args.start,
                end_date=args.end,
                statements=tuple(args.statement or FUNDAMENTAL_STATEMENTS),
                symbols=tuple(args.symbol or ()),
                requests_per_minute=args.requests_per_minute,
                max_attempts=args.max_attempts,
                retry_base_seconds=args.retry_base_seconds,
                job_name=args.job_name,
                workers=args.workers,
                period_mode=args.by_period_vip,
            )
            run_path = FundamentalIngestionRunner(
                provider=provider,
                lake=ParquetLake(Path(args.data_root)),
                artifact_root=Path(args.artifact_root),
                state_root=Path(args.state_root),
            ).run(config)
            print(f"completed P0.8 fundamental ingestion -> {run_path}")
            return 0

        if args.command == "build-fundamentals-pit":
            output = build_fundamental_pit_artifact(
                run_path=Path(args.run),
                lake_root=Path(args.data_root),
                calendar_path=Path(args.calendar),
                output_root=Path(args.output_root),
                as_of_ingested_at=args.as_of_ingested_at,
                aliases_path=Path(args.aliases) if args.aliases else None,
                security_code_mappings_path=(
                    Path(args.security_code_mappings)
                    if args.security_code_mappings
                    else None
                ),
                require_clean_git=not args.allow_dirty_code,
                strict=not args.allow_failed_promotion,
            )
            print(f"built P0.8 fundamental PIT artifact -> {output}")
            return 0

        if args.command == "audit-research-readiness":
            report = audit_research_readiness(
                lake_root=Path(args.data_root),
                calendar_path=Path(args.calendar),
                start_date=args.start,
                end_date=args.end,
                fundamental_artifact=(
                    Path(args.fundamental_artifact)
                    if args.fundamental_artifact
                    else None
                ),
                industry_membership_path=(
                    Path(args.industry_membership)
                    if args.industry_membership
                    else None
                ),
                minimum_weekly_periods=args.minimum_weekly_periods,
                minimum_fundamental_symbols=args.minimum_fundamental_symbols,
                minimum_industry_instruments=args.minimum_industry_instruments,
                maximum_report_period_staleness_days=(
                    args.maximum_report_period_staleness_days
                ),
                maximum_availability_staleness_days=(
                    args.maximum_availability_staleness_days
                ),
            )
            output = write_research_readiness_report(report, Path(args.output))
            print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
            print(f"wrote research-readiness report -> {output}")
            return 0 if report.passed or args.allow_not_ready else 1

        if args.command in {"backfill-p0", "backfill-p05"}:
            provider = create_provider("tushare")
            datasets = (
                tuple(
                    args.dataset
                    or ("daily_bars", "adjustment_factors", "daily_indicators")
                )
                if args.command == "backfill-p0"
                else (
                    "historical_instruments",
                    "daily_bars",
                    "daily_limits",
                    "daily_suspensions",
                    "stock_status",
                )
            )
            config = P0BackfillConfig(
                start_date=args.start,
                end_date=args.end,
                datasets=datasets,
                exchange=args.exchange,
                include_instruments=not getattr(args, "skip_instruments", False),
                include_security_code_mappings=args.command == "backfill-p05",
                requests_per_minute=args.requests_per_minute,
                max_attempts=args.max_attempts,
                retry_base_seconds=args.retry_base_seconds,
                job_name=args.job_name,
                workers=args.workers,
            )
            run_path = P0IngestionRunner(
                provider=provider,
                lake=ParquetLake(Path(args.data_root)),
                artifact_root=Path(args.artifact_root),
                state_root=Path(args.state_root),
            ).run(config)
            print(f"completed P0 ingestion -> {run_path}")
            return 0

        provider = create_provider(args.provider)
        install_limiter = getattr(provider, "set_request_limiter", None)
        if callable(install_limiter):
            install_limiter(RateLimiter(DEFAULT_REQUESTS_PER_MINUTE).wait)
        if args.command == "bars":
            results = []
            for symbol in args.symbol:
                if args.provider == "akshare":
                    result = provider.fetch_daily_bars(
                        symbol, args.start, args.end, adjustment=args.adjustment
                    )
                else:
                    if args.adjustment != "raw":
                        parser.error(
                            "Tushare bars are intentionally raw; ingest adjustment factors separately"
                        )
                    result = provider.fetch_daily_bars(symbol, args.start, args.end)
                results.append(result)
        elif args.command == "instruments":
            results = [provider.fetch_instruments()]
        elif args.command == "calendar":
            results = [provider.fetch_calendar(args.start, args.end, args.exchange)]
        elif args.command == "adjustments":
            results = [
                provider.fetch_adjustment_factors(symbol, args.start, args.end)
                for symbol in args.symbol
            ]
        elif args.command == "daily-indicators":
            results = [
                provider.fetch_daily_indicators(symbol, args.start, args.end)
                for symbol in args.symbol
            ]
        elif args.command == "fundamentals":
            results = [
                provider.fetch_fundamentals(
                    args.statement, symbol, args.start, args.end, args.period
                )
                for symbol in args.symbol
            ]
        elif args.command == "corporate-actions":
            results = [
                provider.fetch_corporate_actions(symbol, args.start, args.end)
                for symbol in args.symbol
            ]
        elif args.command == "daily-limits":
            results = [provider.fetch_daily_limits_by_date(args.date)]
        elif args.command == "suspensions":
            results = [provider.fetch_daily_suspensions_by_date(args.date)]
        elif args.command == "bse-mappings":
            results = [provider.fetch_security_code_mappings()]
        elif args.command == "macro":
            results = [
                provider.fetch_series(
                    series,
                    observation_start=args.start,
                    observation_end=args.end,
                    realtime_start=args.realtime_start,
                    realtime_end=args.realtime_end,
                )
                for series in args.series
            ]
        else:
            parser.error(f"Unsupported command: {args.command}")
            return 2
        _write_results(results, args.data_root)
        return 0
    except (ProviderError, RuntimeError, ValueError) as exc:
        parser.exit(1, f"provider error: {exc}\n")


def _read_frame(path: Path):
    if path.suffix.lower() in {".parquet", ".pq"}:
        return pd.read_parquet(path)
    if path.suffix.lower() == ".csv":
        return pd.read_csv(path)
    raise ValueError(f"unsupported tabular file: {path}")


def _write_frame(frame, path: Path) -> None:
    if path.suffix.lower() in {".parquet", ".pq"}:
        frame.to_parquet(path, index=False)
        return
    if path.suffix.lower() == ".csv":
        frame.to_csv(path, index=False)
        return
    raise ValueError(f"unsupported tabular output: {path}")


if __name__ == "__main__":
    raise SystemExit(main())
