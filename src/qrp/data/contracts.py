"""Canonical schemas and validation for ingested datasets.

The contracts intentionally retain both event time and knowledge time.  A report
period is not the date on which a researcher could have used the information.
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, Sequence

import numpy as np
import pandas as pd


class DataContractError(ValueError):
    """Raised when provider output violates a canonical data contract."""


_CN_SYMBOL = re.compile(r"^(?P<code>\d{6})(?:\.(?P<exchange>SH|SZ|BJ))?$", re.I)
_CN_INSTRUMENT_SYMBOL = re.compile(
    r"^(?P<legacy>T?)(?P<code>\d{6})(?:\.(?P<exchange>SH|SZ|BJ))?$", re.I
)


def infer_cn_exchange(code: str) -> str:
    """Infer the exchange suffix for a six-digit mainland China stock code."""
    if code.startswith(("4", "8", "92")):
        return "BJ"
    if code.startswith(("5", "6", "9")):
        return "SH"
    return "SZ"


def normalize_cn_symbol(symbol: str) -> str:
    """Return a canonical A-share symbol such as ``600000.SH``."""
    match = _CN_SYMBOL.fullmatch(str(symbol).strip().upper())
    if not match:
        raise DataContractError(f"Invalid mainland China stock symbol: {symbol!r}")
    code = match.group("code")
    exchange = match.group("exchange") or infer_cn_exchange(code)
    return f"{code}.{exchange}"


def normalize_cn_instrument_symbol(symbol: str) -> str:
    """Normalize active codes and vendor legacy instrument identifiers.

    Tushare uses a ``T`` prefix for a small number of historical instruments
    whose six-digit trading code was later reused.  The prefix is identity, not
    decoration, and must never be silently stripped.
    """
    match = _CN_INSTRUMENT_SYMBOL.fullmatch(str(symbol).strip().upper())
    if not match:
        raise DataContractError(f"Invalid mainland China instrument symbol: {symbol!r}")
    code = match.group("code")
    legacy = match.group("legacy").upper()
    exchange = match.group("exchange") or infer_cn_exchange(code)
    return f"{legacy}{code}.{exchange}"


REQUIRED_COLUMNS: Dict[str, Sequence[str]] = {
    "instruments": (
        "symbol",
        "name",
        "exchange",
        "list_status",
        "source",
        "ingested_at",
    ),
    "trading_calendar": (
        "exchange",
        "calendar_date",
        "is_open",
        "source",
        "ingested_at",
    ),
    "daily_bars": (
        "symbol",
        "trade_date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
        "adjustment",
        "source",
        "ingested_at",
    ),
    "adjustment_factors": (
        "symbol",
        "trade_date",
        "adj_factor",
        "source",
        "ingested_at",
    ),
    "daily_indicators": (
        "symbol",
        "trade_date",
        "source",
        "ingested_at",
    ),
    "stock_status": (
        "symbol",
        "trade_date",
        "status_type",
        "status_name",
        "source",
        "ingested_at",
    ),
    "daily_limits": (
        "symbol",
        "trade_date",
        "pre_close",
        "up_limit",
        "down_limit",
        "source",
        "ingested_at",
    ),
    "daily_suspensions": (
        "symbol",
        "trade_date",
        "suspend_type",
        "suspend_timing",
        "source",
        "ingested_at",
    ),
    "historical_instruments": (
        "symbol",
        "trade_date",
        "name",
        "list_date",
        "source",
        "ingested_at",
    ),
    "security_code_mappings": (
        "historical_symbol",
        "current_symbol",
        "name",
        "list_date",
        "source",
        "ingested_at",
    ),
    "corporate_actions": (
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
    ),
    "macro_observations": (
        "series_id",
        "observation_date",
        "value",
        "realtime_start",
        "realtime_end",
        "source",
        "ingested_at",
    ),
}


UNIQUE_KEYS: Dict[str, Sequence[str]] = {
    "instruments": ("symbol", "list_status"),
    "trading_calendar": ("exchange", "calendar_date"),
    "daily_bars": ("symbol", "trade_date", "adjustment"),
    "adjustment_factors": ("symbol", "trade_date"),
    "daily_indicators": ("symbol", "trade_date"),
    "stock_status": ("symbol", "trade_date", "status_type"),
    "daily_limits": ("symbol", "trade_date"),
    "daily_suspensions": ("symbol", "trade_date", "suspend_type", "suspend_timing"),
    "historical_instruments": ("symbol", "trade_date"),
    "security_code_mappings": ("historical_symbol", "current_symbol"),
    "corporate_actions": ("source_action_id",),
    "macro_observations": (
        "series_id",
        "observation_date",
        "realtime_start",
        "realtime_end",
    ),
}


EMPTY_SNAPSHOT_DATASETS = {"corporate_actions", "daily_suspensions", "stock_status"}


def _require_columns(frame: pd.DataFrame, columns: Iterable[str], dataset: str) -> None:
    missing = sorted(set(columns) - set(frame.columns))
    if missing:
        raise DataContractError(f"{dataset} is missing required columns: {missing}")


def validate_dataset(dataset: str, frame: pd.DataFrame) -> None:
    """Validate provider output before it is committed to the local lake."""
    if not isinstance(frame, pd.DataFrame):
        raise DataContractError(f"{dataset} must be a pandas DataFrame")
    if dataset.startswith("fundamentals_"):
        _require_columns(
            frame,
            ("symbol", "report_period", "available_date", "source", "ingested_at"),
            dataset,
        )
    else:
        required = REQUIRED_COLUMNS.get(dataset)
        if required is None:
            raise DataContractError(f"Unknown dataset contract: {dataset}")
        _require_columns(frame, required, dataset)

    if frame.empty:
        if dataset in EMPTY_SNAPSHOT_DATASETS:
            return
        raise DataContractError(f"{dataset} returned no rows")

    keys = UNIQUE_KEYS.get(dataset)
    if keys and frame.duplicated(list(keys)).any():
        sample = frame.loc[frame.duplicated(list(keys), keep=False), list(keys)].head(5)
        raise DataContractError(f"{dataset} has duplicate keys:\n{sample.to_string(index=False)}")

    if dataset == "daily_bars":
        numeric = ["open", "high", "low", "close", "volume", "amount"]
        if frame[numeric].isna().any().any():
            raise DataContractError("daily_bars contains null OHLCV/amount values")
        if (frame["high"] < frame["low"]).any():
            raise DataContractError("daily_bars contains high < low")
        if (frame[["volume", "amount"]] < 0).any().any():
            raise DataContractError("daily_bars contains negative volume or amount")
        if not frame["adjustment"].isin(["raw", "qfq", "hfq"]).all():
            raise DataContractError("daily_bars contains an unsupported adjustment label")

    if dataset == "adjustment_factors":
        factor = pd.to_numeric(frame["adj_factor"], errors="coerce")
        if factor.isna().any() or (factor <= 0).any():
            raise DataContractError("adjustment_factors must be finite and positive")

    if dataset == "daily_limits":
        numeric = frame[["pre_close", "up_limit", "down_limit"]].apply(
            pd.to_numeric, errors="coerce"
        )
        required_prices = numeric[["up_limit", "down_limit"]]
        no_limit_sentinel = (
            (required_prices["up_limit"] >= 99999.0)
            & (required_prices["down_limit"] == 0)
        )
        if (
            required_prices.isna().any().any()
            or (~np.isfinite(required_prices)).any().any()
            or (
                (
                    (required_prices["up_limit"] <= 0)
                    | (required_prices["down_limit"] <= 0)
                )
                & ~no_limit_sentinel
            ).any()
        ):
            raise DataContractError("daily_limits up/down prices must be finite and positive")
        nonnull_pre_close = numeric["pre_close"].dropna()
        if (
            (~np.isfinite(nonnull_pre_close)).any()
            or (nonnull_pre_close <= 0).any()
        ):
            raise DataContractError("daily_limits non-null pre_close must be finite and positive")
        if (numeric["up_limit"] < numeric["down_limit"]).any():
            raise DataContractError("daily_limits contains up_limit < down_limit")

    if dataset == "daily_suspensions" and not frame["suspend_type"].isin(["S", "R"]).all():
        raise DataContractError("daily_suspensions suspend_type must be S or R")

    if dataset == "corporate_actions":
        dates = frame[["report_period", "announcement_date"]].apply(
            pd.to_datetime, errors="coerce"
        )
        if dates.isna().any().any():
            raise DataContractError("corporate_actions requires report and announcement dates")
        numeric = frame[["cash_per_share_tax", "bonus_share_ratio"]].apply(
            pd.to_numeric, errors="coerce"
        )
        if (numeric.fillna(0.0) < 0).any().any():
            raise DataContractError("corporate action cash/share ratios cannot be negative")

    if dataset.startswith("fundamentals_"):
        invalid = frame["available_date"].isna() | frame["report_period"].isna()
        if invalid.any():
            raise DataContractError(
                f"{dataset} contains rows without report_period or available_date"
            )
