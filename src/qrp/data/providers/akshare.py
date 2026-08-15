"""AKShare adapter used for free bootstrap data and source cross-checks."""

from __future__ import annotations

from typing import Any, Optional

import pandas as pd

from ..contracts import normalize_cn_symbol
from .base import FetchResult, ProviderError


def _utc_now() -> pd.Timestamp:
    return pd.Timestamp.now(tz="UTC")


class AkshareProvider:
    name = "akshare"
    capabilities = ("instruments", "daily_bars")

    def __init__(self, module: Optional[Any] = None) -> None:
        if module is not None:
            self._ak = module
            return
        try:
            import akshare as ak
        except ImportError as exc:
            raise ProviderError(
                "AKShare is not installed. Install the project with the 'china' extra."
            ) from exc
        self._ak = ak

    def fetch_instruments(self) -> FetchResult:
        try:
            raw = self._ak.stock_info_a_code_name()
        except Exception as exc:
            raise ProviderError(f"AKShare instrument request failed: {exc}") from exc
        rename = {"code": "source_symbol", "name": "name", "代码": "source_symbol", "名称": "name"}
        frame = raw.rename(columns=rename).copy()
        if not {"source_symbol", "name"}.issubset(frame.columns):
            raise ProviderError(f"Unexpected AKShare instrument columns: {list(raw.columns)}")
        frame["symbol"] = frame["source_symbol"].map(normalize_cn_symbol)
        frame["exchange"] = frame["symbol"].str.rsplit(".", n=1).str[-1]
        frame["list_status"] = "L"
        frame["list_date"] = pd.NaT
        frame["delist_date"] = pd.NaT
        frame["source"] = self.name
        frame["ingested_at"] = _utc_now()
        columns = [
            "symbol",
            "source_symbol",
            "name",
            "exchange",
            "list_status",
            "list_date",
            "delist_date",
            "source",
            "ingested_at",
        ]
        return FetchResult(
            dataset="instruments",
            provider=self.name,
            frame=frame[columns],
            query={"endpoint": "stock_info_a_code_name"},
            metadata={
                "coverage": "currently listed instruments only",
                "point_in_time_safe": False,
            },
        ).validate()
    def fetch_daily_bars(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
        adjustment: str = "raw",
    ) -> FetchResult:
        canonical = normalize_cn_symbol(symbol)
        adjustment_map = {"raw": "", "qfq": "qfq", "hfq": "hfq"}
        if adjustment not in adjustment_map:
            raise ProviderError("adjustment must be one of: raw, qfq, hfq")
        source_symbol = canonical.split(".")[0]
        try:
            raw = self._ak.stock_zh_a_hist(
                symbol=source_symbol,
                period="daily",
                start_date=start_date.replace("-", ""),
                end_date=end_date.replace("-", ""),
                adjust=adjustment_map[adjustment],
            )
        except Exception as exc:
            raise ProviderError(f"AKShare daily bar request failed for {canonical}: {exc}") from exc
        if raw.empty:
            raise ProviderError(f"AKShare returned no bars for {canonical}")

        rename = {
            "日期": "trade_date",
            "开盘": "open",
            "最高": "high",
            "最低": "low",
            "收盘": "close",
            "成交量": "volume",
            "成交额": "amount",
            "涨跌幅": "pct_change",
            "换手率": "turnover_rate",
        }
        frame = raw.rename(columns=rename).copy()
        required = {"trade_date", "open", "high", "low", "close", "volume", "amount"}
        if not required.issubset(frame.columns):
            raise ProviderError(f"Unexpected AKShare daily bar columns: {list(raw.columns)}")
        frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
        for column in ["open", "high", "low", "close", "volume", "amount"]:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        # Eastmoney's A-share volume is reported in lots; the canonical unit is shares.
        frame["volume"] = frame["volume"] * 100.0
        frame["symbol"] = canonical
        frame["source_symbol"] = source_symbol
        frame["adjustment"] = adjustment
        frame["source"] = self.name
        frame["ingested_at"] = _utc_now()
        optional = [column for column in ["pct_change", "turnover_rate"] if column in frame]
        columns = [
            "symbol",
            "source_symbol",
            "trade_date",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "amount",
            *optional,
            "adjustment",
            "source",
            "ingested_at",
        ]
        return FetchResult(
            dataset="daily_bars",
            provider=self.name,
            frame=frame[columns].sort_values("trade_date").reset_index(drop=True),
            query={
                "endpoint": "stock_zh_a_hist",
                "symbol": canonical,
                "start_date": start_date,
                "end_date": end_date,
                "adjustment": adjustment,
            },
            metadata={
                "canonical_volume_unit": "shares",
                "canonical_amount_unit": "CNY",
                "source_volume_unit": "lots",
                "volume_multiplier": 100,
                "immutable_raw_recommended": adjustment == "raw",
            },
        ).validate()
