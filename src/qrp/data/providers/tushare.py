"""Tushare Pro adapter for A-share point-in-time research inputs."""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Callable, Dict, Optional, Sequence

import pandas as pd

from ..contracts import (
    DataContractError,
    normalize_cn_instrument_symbol,
    normalize_cn_symbol,
)
from .base import FetchResult, ProviderError

TUSHARE_PAGE_SIZES: Dict[str, int] = {
    "stock_basic": 6000,
    "daily": 6000,
    "adj_factor": 6000,
    "daily_basic": 6000,
    "stock_st": 1000,
    "stk_limit": 5800,
    "suspend_d": 5000,
    "bak_basic": 7000,
    "bse_mapping": 1000,
    "dividend": 2000,
    "index_classify": 1000,
    "index_member": 2000,
    "income_vip": 9000,
    "balancesheet_vip": 9000,
    "cashflow_vip": 9000,
    "fina_indicator_vip": 9000,
}
MAX_PAGINATION_PAGES = 100


def _utc_now() -> pd.Timestamp:
    return pd.Timestamp.now(tz="UTC")


def _parse_yyyymmdd(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, format="%Y%m%d", errors="coerce")


class TushareProvider:
    name = "tushare"
    capabilities = (
        "instruments",
        "trading_calendar",
        "daily_bars",
        "adjustment_factors",
        "daily_indicators",
        "stock_status",
        "daily_limits",
        "daily_suspensions",
        "historical_instruments",
        "security_code_mappings",
        "corporate_actions",
        "fundamentals",
        "historical_industry_membership",
    )

    def __init__(self, token: Optional[str] = None, client: Optional[Any] = None) -> None:
        self._before_request: Optional[Callable[[], None]] = None
        self._instrument_master_cache: Optional[pd.DataFrame] = None
        if client is not None:
            self._pro = client
            return
        token = token or os.environ.get("TUSHARE_TOKEN")
        if not token:
            raise ProviderError("TUSHARE_TOKEN is required for the Tushare provider")
        try:
            import tushare as ts
        except ImportError as exc:
            raise ProviderError(
                "Tushare is not installed. Install the project with the 'china' extra."
            ) from exc
        self._pro = ts.pro_api(token)

    def set_request_limiter(self, before_request: Callable[[], None]) -> None:
        """Install a runner-owned limiter that is invoked for every API request."""
        self._before_request = before_request

    def _call_endpoint(self, endpoint: str, **params: Any) -> pd.DataFrame:
        if self._before_request is not None:
            self._before_request()
        return getattr(self._pro, endpoint)(**params)

    def _fetch_paginated(
        self, endpoint: str, params: Dict[str, Any]
    ) -> tuple[pd.DataFrame, Dict[str, int]]:
        """Fetch every page using the endpoint's reviewed maximum page size."""
        if endpoint not in TUSHARE_PAGE_SIZES:
            raise ProviderError(f"No reviewed page-size policy for Tushare {endpoint}")
        page_size = TUSHARE_PAGE_SIZES[endpoint]
        frames = []
        seen_page_hashes = set()
        requests = 0
        for page in range(MAX_PAGINATION_PAGES):
            offset = page * page_size
            part = self._call_endpoint(
                endpoint, **params, limit=page_size, offset=offset
            )
            requests += 1
            if part is None:
                raise ProviderError(f"Tushare {endpoint} returned a null response")
            if not part.empty:
                page_hash = hashlib.sha256(
                    "|".join(map(str, part.columns)).encode("utf-8")
                    + pd.util.hash_pandas_object(part, index=False).values.tobytes()
                ).hexdigest()
                if page_hash in seen_page_hashes:
                    raise ProviderError(
                        f"Tushare {endpoint} repeated a page at offset {offset}; "
                        "refusing a silently duplicated snapshot"
                    )
                seen_page_hashes.add(page_hash)
                frames.append(part)
            if len(part) < page_size:
                frame = _concat_provider_pages(frames) if frames else part
                return frame, {
                    "page_size": page_size,
                    "requests": requests,
                    "pages_with_data": len(frames),
                    "rows_fetched": len(frame),
                }
        raise ProviderError(
            f"Tushare {endpoint} pagination exceeded {MAX_PAGINATION_PAGES} pages"
        )

    def fetch_instruments(
        self, statuses: Sequence[str] = ("L", "D", "P", "G")
    ) -> FetchResult:
        frames = []
        pagination_by_status: Dict[str, Dict[str, int]] = {}
        fields = "ts_code,symbol,name,area,industry,market,exchange,list_status,list_date,delist_date"
        try:
            for status in statuses:
                part, pagination = self._fetch_paginated(
                    "stock_basic",
                    {"exchange": "", "list_status": status, "fields": fields},
                )
                if not part.empty:
                    frames.append(part)
                pagination_by_status[status] = pagination
        except Exception as exc:
            raise ProviderError(f"Tushare instrument request failed: {exc}") from exc
        if not frames:
            raise ProviderError("Tushare returned no instruments")
        frame = pd.concat(frames, ignore_index=True).rename(
            columns={"ts_code": "symbol", "symbol": "source_symbol"}
        )
        frame["symbol"] = frame["symbol"].map(normalize_cn_instrument_symbol)
        frame["instrument_kind"] = frame["symbol"].str.startswith("T").map(
            {True: "legacy_stock", False: "stock"}
        )
        frame["list_date"] = _parse_yyyymmdd(frame["list_date"])
        frame["delist_date"] = _parse_yyyymmdd(frame["delist_date"])
        frame["source"] = self.name
        frame["ingested_at"] = _utc_now()
        result = FetchResult(
            dataset="instruments",
            provider=self.name,
            frame=frame,
            query={
                "endpoint": "stock_basic",
                "statuses": list(statuses),
                "fields": fields,
                "page_size": TUSHARE_PAGE_SIZES["stock_basic"],
            },
            metadata={
                "includes_delisted_when_authorized": "D" in statuses,
                "point_in_time_safe": "historical snapshots require archived ingestions",
                "pagination": pagination_by_status,
            },
        ).validate()
        self._instrument_master_cache = result.frame.copy()
        return result

    def fetch_calendar(
        self, start_date: str, end_date: str, exchange: str = "SSE"
    ) -> FetchResult:
        try:
            raw = self._call_endpoint(
                "trade_cal",
                exchange=exchange,
                start_date=start_date.replace("-", ""),
                end_date=end_date.replace("-", ""),
            )
        except Exception as exc:
            raise ProviderError(f"Tushare calendar request failed: {exc}") from exc
        frame = raw.rename(
            columns={
                "exchange": "exchange",
                "cal_date": "calendar_date",
                "is_open": "is_open",
                "pretrade_date": "previous_trade_date",
            }
        ).copy()
        frame["calendar_date"] = _parse_yyyymmdd(frame["calendar_date"])
        if "previous_trade_date" in frame:
            frame["previous_trade_date"] = _parse_yyyymmdd(frame["previous_trade_date"])
        frame["is_open"] = pd.to_numeric(frame["is_open"], errors="raise").astype(bool)
        frame["source"] = self.name
        frame["ingested_at"] = _utc_now()
        return FetchResult(
            dataset="trading_calendar",
            provider=self.name,
            frame=frame.sort_values("calendar_date").reset_index(drop=True),
            query={
                "endpoint": "trade_cal",
                "exchange": exchange,
                "start_date": start_date,
                "end_date": end_date,
            },
        ).validate()

    def fetch_industry_classification(
        self, taxonomy: str, industry_level: str = "L1"
    ) -> FetchResult:
        """Fetch one frozen Shenwan taxonomy catalogue."""
        if taxonomy not in {"SW2014", "SW2021"}:
            raise ProviderError(f"Unsupported Shenwan taxonomy: {taxonomy}")
        if industry_level != "L1":
            raise ProviderError("The research neutralization contract currently requires L1")
        try:
            raw, pagination = self._fetch_paginated(
                "index_classify", {"level": industry_level, "src": taxonomy}
            )
        except Exception as exc:
            raise ProviderError(
                f"Tushare industry classification request failed for {taxonomy}: {exc}"
            ) from exc
        if raw.empty:
            raise ProviderError(f"Tushare returned no {taxonomy} classifications")
        frame = raw.rename(columns={"index_code": "source_index_code"}).copy()
        frame["taxonomy"] = taxonomy
        frame["industry_level"] = frame["level"].astype("string")
        frame["industry_code"] = frame["industry_code"].astype("string")
        frame["industry_name"] = frame["industry_name"].astype("string")
        frame["source_index_code"] = frame["source_index_code"].astype("string")
        frame["source"] = self.name
        frame["ingested_at"] = _utc_now()
        return FetchResult(
            dataset="industry_classification",
            provider=self.name,
            frame=frame,
            query={
                "endpoint": "index_classify",
                "taxonomy": taxonomy,
                "industry_level": industry_level,
                "page_size": TUSHARE_PAGE_SIZES["index_classify"],
            },
            metadata={"pagination": pagination},
            partition_values={"taxonomy": taxonomy, "industry_level": industry_level},
        ).validate()

    def fetch_industry_members(
        self,
        taxonomy: str,
        source_index_code: str,
        industry_code: str,
        industry_name: str,
        industry_level: str = "L1",
    ) -> FetchResult:
        """Fetch every historical membership interval for one industry index."""
        try:
            raw, pagination = self._fetch_paginated(
                "index_member", {"index_code": source_index_code}
            )
        except Exception as exc:
            raise ProviderError(
                f"Tushare industry member request failed for {source_index_code}: {exc}"
            ) from exc
        if raw.empty:
            raise ProviderError(
                f"Tushare returned no industry members for {source_index_code}"
            )
        frame = raw.rename(
            columns={
                "con_code": "symbol",
                "in_date": "source_membership_start",
                "out_date": "source_membership_end",
            }
        ).copy()
        frame["symbol"] = frame["symbol"].map(normalize_cn_instrument_symbol)
        frame["source_membership_start"] = _parse_yyyymmdd(
            frame["source_membership_start"]
        )
        frame["source_membership_end"] = _parse_yyyymmdd(
            frame["source_membership_end"]
        )
        frame["taxonomy"] = taxonomy
        frame["industry_level"] = industry_level
        frame["industry_code"] = str(industry_code)
        frame["industry_name"] = str(industry_name)
        frame["source_index_code"] = str(source_index_code)
        frame["is_current"] = frame.get(
            "is_new", pd.Series(pd.NA, index=frame.index, dtype="string")
        ).astype("string")
        frame["source"] = self.name
        frame["ingested_at"] = _utc_now()
        return FetchResult(
            dataset="industry_membership",
            provider=self.name,
            frame=frame,
            query={
                "endpoint": "index_member",
                "taxonomy": taxonomy,
                "source_index_code": source_index_code,
                "industry_code": industry_code,
                "industry_level": industry_level,
                "page_size": TUSHARE_PAGE_SIZES["index_member"],
            },
            metadata={
                "pagination": pagination,
                "membership_interval_semantics": "inclusive_source_dates",
            },
            partition_values={
                "taxonomy": taxonomy,
                "industry_level": industry_level,
                "source_index_code": source_index_code,
            },
        ).validate()

    def fetch_daily_bars(self, symbol: str, start_date: str, end_date: str) -> FetchResult:
        canonical = normalize_cn_symbol(symbol)
        try:
            raw = self._call_endpoint(
                "daily",
                ts_code=canonical,
                start_date=start_date.replace("-", ""),
                end_date=end_date.replace("-", ""),
            )
        except Exception as exc:
            raise ProviderError(f"Tushare daily request failed for {canonical}: {exc}") from exc
        if raw.empty:
            raise ProviderError(f"Tushare returned no bars for {canonical}")
        return self._normalize_daily_bars(
            raw,
            query={
                "endpoint": "daily",
                "symbol": canonical,
                "start_date": start_date,
                "end_date": end_date,
            },
        )

    def fetch_daily_bars_by_date(self, trade_date: str) -> FetchResult:
        source_date = trade_date.replace("-", "")
        try:
            raw, pagination = self._fetch_paginated(
                "daily", {"trade_date": source_date}
            )
        except Exception as exc:
            raise ProviderError(f"Tushare daily request failed for {trade_date}: {exc}") from exc
        if raw.empty:
            raise ProviderError(f"Tushare returned no full-market bars for {trade_date}")
        return self._normalize_daily_bars(
            raw,
            query={
                "endpoint": "daily",
                "trade_date": trade_date,
                "page_size": TUSHARE_PAGE_SIZES["daily"],
            },
            partition_values={"trade_date": trade_date},
            pagination=pagination,
        )

    def _normalize_daily_bars(
        self,
        raw: pd.DataFrame,
        query: Dict[str, Any],
        partition_values: Optional[Dict[str, Any]] = None,
        pagination: Optional[Dict[str, int]] = None,
    ) -> FetchResult:
        frame = raw.rename(
            columns={
                "ts_code": "symbol",
                "vol": "volume",
                "pct_chg": "pct_change",
            }
        ).copy()
        frame["symbol"] = frame["symbol"].map(normalize_cn_symbol)
        frame["trade_date"] = _parse_yyyymmdd(frame["trade_date"])
        for column in ["open", "high", "low", "close", "volume", "amount"]:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        # Tushare daily: volume=lots, amount=thousand CNY.
        frame["volume"] = frame["volume"] * 100.0
        frame["amount"] = frame["amount"] * 1000.0
        frame["adjustment"] = "raw"
        frame["source"] = self.name
        frame["ingested_at"] = _utc_now()
        return FetchResult(
            dataset="daily_bars",
            provider=self.name,
            frame=frame.sort_values("trade_date").reset_index(drop=True),
            query=query,
            metadata={
                "canonical_volume_unit": "shares",
                "canonical_amount_unit": "CNY",
                "source_volume_unit": "lots",
                "source_amount_unit": "thousand_CNY",
                "volume_multiplier": 100,
                "amount_multiplier": 1000,
                "adjustment": "raw",
                "pagination": pagination,
            },
            partition_values=partition_values or {},
        ).validate()

    def fetch_adjustment_factors(
        self, symbol: str, start_date: str, end_date: str
    ) -> FetchResult:
        canonical = normalize_cn_symbol(symbol)
        try:
            raw = self._call_endpoint(
                "adj_factor",
                ts_code=canonical,
                start_date=start_date.replace("-", ""),
                end_date=end_date.replace("-", ""),
            )
        except Exception as exc:
            raise ProviderError(
                f"Tushare adjustment factor request failed for {canonical}: {exc}"
            ) from exc
        if raw.empty:
            raise ProviderError(f"Tushare returned no adjustment factors for {canonical}")
        return self._normalize_adjustment_factors(
            raw,
            query={
                "endpoint": "adj_factor",
                "symbol": canonical,
                "start_date": start_date,
                "end_date": end_date,
            },
        )

    def fetch_adjustment_factors_by_date(self, trade_date: str) -> FetchResult:
        source_date = trade_date.replace("-", "")
        try:
            raw, pagination = self._fetch_paginated(
                "adj_factor", {"trade_date": source_date}
            )
        except Exception as exc:
            raise ProviderError(
                f"Tushare adjustment factor request failed for {trade_date}: {exc}"
            ) from exc
        if raw.empty:
            raise ProviderError(f"Tushare returned no adjustment factors for {trade_date}")
        return self._normalize_adjustment_factors(
            raw,
            query={
                "endpoint": "adj_factor",
                "trade_date": trade_date,
                "page_size": TUSHARE_PAGE_SIZES["adj_factor"],
            },
            partition_values={"trade_date": trade_date},
            pagination=pagination,
        )

    def _normalize_adjustment_factors(
        self,
        raw: pd.DataFrame,
        query: Dict[str, Any],
        partition_values: Optional[Dict[str, Any]] = None,
        pagination: Optional[Dict[str, int]] = None,
    ) -> FetchResult:
        frame = raw.rename(columns={"ts_code": "symbol"}).copy()
        frame["symbol"] = frame["symbol"].map(normalize_cn_symbol)
        frame["trade_date"] = _parse_yyyymmdd(frame["trade_date"])
        frame["adj_factor"] = pd.to_numeric(frame["adj_factor"], errors="coerce")
        frame["source"] = self.name
        frame["ingested_at"] = _utc_now()
        return FetchResult(
            dataset="adjustment_factors",
            provider=self.name,
            frame=frame.sort_values("trade_date").reset_index(drop=True),
            query=query,
            metadata={
                "usage": "join to raw prices; derive adjusted series in the curated layer",
                "pagination": pagination,
            },
            partition_values=partition_values or {},
        ).validate()

    def fetch_daily_indicators(
        self, symbol: str, start_date: str, end_date: str
    ) -> FetchResult:
        canonical = normalize_cn_symbol(symbol)
        try:
            raw = self._call_endpoint(
                "daily_basic",
                ts_code=canonical,
                start_date=start_date.replace("-", ""),
                end_date=end_date.replace("-", ""),
            )
        except Exception as exc:
            raise ProviderError(
                f"Tushare daily indicator request failed for {canonical}: {exc}"
            ) from exc
        if raw.empty:
            raise ProviderError(f"Tushare returned no daily indicators for {canonical}")
        return self._normalize_daily_indicators(
            raw,
            query={
                "endpoint": "daily_basic",
                "symbol": canonical,
                "start_date": start_date,
                "end_date": end_date,
            },
        )

    def fetch_daily_indicators_by_date(self, trade_date: str) -> FetchResult:
        source_date = trade_date.replace("-", "")
        try:
            raw, pagination = self._fetch_paginated(
                "daily_basic", {"trade_date": source_date}
            )
        except Exception as exc:
            raise ProviderError(
                f"Tushare daily indicator request failed for {trade_date}: {exc}"
            ) from exc
        if raw.empty:
            raise ProviderError(f"Tushare returned no daily indicators for {trade_date}")
        return self._normalize_daily_indicators(
            raw,
            query={
                "endpoint": "daily_basic",
                "trade_date": trade_date,
                "page_size": TUSHARE_PAGE_SIZES["daily_basic"],
            },
            partition_values={"trade_date": trade_date},
            pagination=pagination,
        )

    def _normalize_daily_indicators(
        self,
        raw: pd.DataFrame,
        query: Dict[str, Any],
        partition_values: Optional[Dict[str, Any]] = None,
        pagination: Optional[Dict[str, int]] = None,
    ) -> FetchResult:
        frame = raw.rename(columns={"ts_code": "symbol"}).copy()
        frame["symbol"] = frame["symbol"].map(normalize_cn_symbol)
        frame["trade_date"] = _parse_yyyymmdd(frame["trade_date"])
        # Tushare daily_basic market values are ten-thousand CNY.
        for column in ["total_mv", "circ_mv"]:
            if column in frame:
                frame[column] = pd.to_numeric(frame[column], errors="coerce") * 10000.0
        frame["source"] = self.name
        frame["ingested_at"] = _utc_now()
        return FetchResult(
            dataset="daily_indicators",
            provider=self.name,
            frame=frame.sort_values("trade_date").reset_index(drop=True),
            query=query,
            metadata={"market_value_unit": "CNY", "pagination": pagination},
            partition_values=partition_values or {},
        ).validate()

    def fetch_corporate_actions(
        self, symbol: str, start_date: str, end_date: str
    ) -> FetchResult:
        """Fetch versioned dividend/bonus proposals and implementation records."""
        canonical = normalize_cn_symbol(symbol)
        fields = (
            "ts_code,end_date,ann_date,div_proc,stk_div,stk_bo_rate,stk_co_rate,"
            "cash_div,cash_div_tax,record_date,ex_date,pay_date,div_listdate,"
            "imp_ann_date,base_date,base_share"
        )
        try:
            raw, pagination = self._fetch_paginated(
                "dividend", {"ts_code": canonical, "fields": fields}
            )
        except Exception as exc:
            raise ProviderError(
                f"Tushare corporate-action request failed for {canonical}: {exc}"
            ) from exc
        rename = {
            "ts_code": "symbol",
            "end_date": "report_period",
            "ann_date": "announcement_date",
            "div_proc": "process_status",
            "stk_div": "bonus_share_ratio",
            "stk_bo_rate": "bonus_issue_ratio",
            "stk_co_rate": "capitalization_ratio",
            "cash_div": "cash_per_share_after_tax",
            "cash_div_tax": "cash_per_share_tax",
            "record_date": "record_date",
            "ex_date": "ex_date",
            "pay_date": "pay_date",
            "div_listdate": "bonus_listing_date",
            "imp_ann_date": "implementation_announcement_date",
            "base_date": "base_date",
            "base_share": "base_shares_10k",
        }
        frame = raw.rename(columns=rename).copy()
        expected = list(rename.values())
        if frame.empty:
            frame = pd.DataFrame(columns=expected)
        else:
            frame["symbol"] = frame["symbol"].map(normalize_cn_symbol)
            date_columns = [
                "report_period",
                "announcement_date",
                "record_date",
                "ex_date",
                "pay_date",
                "bonus_listing_date",
                "implementation_announcement_date",
                "base_date",
            ]
            for column in date_columns:
                frame[column] = _parse_yyyymmdd(frame[column])
            numeric_columns = [
                "bonus_share_ratio",
                "bonus_issue_ratio",
                "capitalization_ratio",
                "cash_per_share_after_tax",
                "cash_per_share_tax",
                "base_shares_10k",
            ]
            frame[numeric_columns] = frame[numeric_columns].apply(
                pd.to_numeric, errors="coerce"
            )
            start = pd.Timestamp(start_date).normalize()
            end = pd.Timestamp(end_date).normalize()
            frame = frame.loc[
                frame["announcement_date"].between(start, end, inclusive="both")
            ].copy()
        identity_columns = [
            "symbol",
            "report_period",
            "announcement_date",
            "process_status",
            "record_date",
            "ex_date",
            "pay_date",
            "cash_per_share_tax",
            "bonus_share_ratio",
        ]
        if frame.empty:
            frame["source_action_id"] = pd.Series(dtype=str)
        else:
            frame["source_action_id"] = frame.apply(
                lambda row: hashlib.sha256(
                    "|".join(
                        str(row.get(column, "")) for column in identity_columns
                    ).encode("utf-8")
                ).hexdigest()[:24],
                axis=1,
            )
        frame["source"] = self.name
        frame["ingested_at"] = _utc_now()
        return FetchResult(
            dataset="corporate_actions",
            provider=self.name,
            frame=frame.sort_values(
                ["announcement_date", "report_period", "process_status"]
            ).reset_index(drop=True),
            query={
                "endpoint": "dividend",
                "symbol": canonical,
                "announcement_start_date": start_date,
                "announcement_end_date": end_date,
                "fields": fields,
                "page_size": TUSHARE_PAGE_SIZES["dividend"],
            },
            metadata={
                "cash_unit": "CNY_per_share",
                "share_ratio_unit": "shares_per_share",
                "contains_proposal_and_implementation_versions": True,
                "pagination": pagination,
            },
            partition_values={"symbol": canonical},
        ).validate()

    def fetch_stock_status_by_date(self, trade_date: str) -> FetchResult:
        source_date = trade_date.replace("-", "")
        try:
            raw, pagination = self._fetch_paginated(
                "stock_st", {"trade_date": source_date}
            )
        except Exception as exc:
            raise ProviderError(f"Tushare ST request failed for {trade_date}: {exc}") from exc
        frame = raw.rename(
            columns={
                "ts_code": "symbol",
                "type": "status_type",
                "type_name": "status_name",
            }
        ).copy()
        if frame.empty:
            frame = pd.DataFrame(
                columns=["symbol", "trade_date", "status_type", "status_name"]
            )
        else:
            frame["symbol"] = frame["symbol"].map(normalize_cn_symbol)
            frame["trade_date"] = _parse_yyyymmdd(frame["trade_date"])
        frame["source"] = self.name
        frame["ingested_at"] = _utc_now()
        return FetchResult(
            dataset="stock_status",
            provider=self.name,
            frame=frame.sort_values(["trade_date", "symbol"]).reset_index(drop=True),
            query={
                "endpoint": "stock_st",
                "trade_date": trade_date,
                "page_size": TUSHARE_PAGE_SIZES["stock_st"],
            },
            metadata={
                "snapshot_semantics": "status known for the requested trade date",
                "pagination": pagination,
            },
            partition_values={"trade_date": trade_date},
        ).validate()

    def fetch_daily_limits_by_date(self, trade_date: str) -> FetchResult:
        source_date = trade_date.replace("-", "")
        fields = "trade_date,ts_code,pre_close,up_limit,down_limit"
        try:
            raw, pagination = self._fetch_paginated(
                "stk_limit", {"trade_date": source_date, "fields": fields}
            )
        except Exception as exc:
            raise ProviderError(
                f"Tushare daily-limit request failed for {trade_date}: {exc}"
            ) from exc
        if raw.empty:
            raise ProviderError(f"Tushare returned no daily limits for {trade_date}")
        frame = raw.rename(columns={"ts_code": "symbol"}).copy()
        frame["symbol"] = frame["symbol"].map(normalize_cn_symbol)
        frame["trade_date"] = _parse_yyyymmdd(frame["trade_date"])
        for column in ["pre_close", "up_limit", "down_limit"]:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        zero_pre_close = frame["pre_close"].eq(0)
        frame.loc[zero_pre_close, "pre_close"] = pd.NA
        frame["price_limit_regime"] = "bounded"
        no_limit_sentinel = (frame["up_limit"] >= 99999.0) & (frame["down_limit"] == 0)
        frame.loc[no_limit_sentinel, "price_limit_regime"] = "none_vendor_sentinel"
        frame["source"] = self.name
        frame["ingested_at"] = _utc_now()
        return FetchResult(
            dataset="daily_limits",
            provider=self.name,
            frame=frame.sort_values(["trade_date", "symbol"]).reset_index(drop=True),
            query={
                "endpoint": "stk_limit",
                "trade_date": trade_date,
                "fields": fields,
                "page_size": TUSHARE_PAGE_SIZES["stk_limit"],
            },
            metadata={
                "snapshot_semantics": "pre-open daily price limits",
                "source_update_time": "approximately 08:40 Asia/Shanghai",
                "zero_pre_close_normalized_to_null_rows": int(zero_pre_close.sum()),
                "pagination": pagination,
            },
            partition_values={"trade_date": trade_date},
        ).validate()

    def fetch_daily_suspensions_by_date(self, trade_date: str) -> FetchResult:
        source_date = trade_date.replace("-", "")
        try:
            raw, pagination = self._fetch_paginated(
                "suspend_d", {"trade_date": source_date}
            )
        except Exception as exc:
            raise ProviderError(
                f"Tushare suspension request failed for {trade_date}: {exc}"
            ) from exc
        frame = raw.rename(columns={"ts_code": "symbol"}).copy()
        required = ["symbol", "trade_date", "suspend_type", "suspend_timing"]
        if frame.empty:
            frame = pd.DataFrame(columns=required)
        else:
            frame["symbol"] = frame["symbol"].map(normalize_cn_symbol)
            frame["trade_date"] = _parse_yyyymmdd(frame["trade_date"])
            frame["suspend_type"] = frame["suspend_type"].astype(str).str.upper()
        frame["source"] = self.name
        frame["ingested_at"] = _utc_now()
        return FetchResult(
            dataset="daily_suspensions",
            provider=self.name,
            frame=frame.sort_values(["trade_date", "symbol"]).reset_index(drop=True),
            query={
                "endpoint": "suspend_d",
                "trade_date": trade_date,
                "page_size": TUSHARE_PAGE_SIZES["suspend_d"],
            },
            metadata={
                "snapshot_semantics": "daily S/R event set; zero rows is a valid snapshot",
                "source_update_time": "irregular",
                "pagination": pagination,
            },
            partition_values={"trade_date": trade_date},
        ).validate()

    def fetch_historical_instruments_by_date(self, trade_date: str) -> FetchResult:
        source_date = trade_date.replace("-", "")
        fields = "trade_date,ts_code,name,industry,area,list_date"
        try:
            raw, pagination = self._fetch_paginated(
                "bak_basic", {"trade_date": source_date, "fields": fields}
            )
        except Exception as exc:
            raise ProviderError(
                f"Tushare historical-instrument request failed for {trade_date}: {exc}"
            ) from exc
        reconstructed = raw.empty
        if reconstructed:
            frame = self._reconstruct_historical_instruments(trade_date)
        else:
            frame = raw.rename(columns={"ts_code": "symbol"}).copy()
            frame["symbol"] = frame["symbol"].map(normalize_cn_symbol)
            frame["trade_date"] = _parse_yyyymmdd(frame["trade_date"])
            frame["list_date"] = _parse_yyyymmdd(frame["list_date"])
            frame["universe_snapshot_method"] = "vendor_bak_basic"
        frame["source"] = self.name
        frame["ingested_at"] = _utc_now()
        return FetchResult(
            dataset="historical_instruments",
            provider=self.name,
            frame=frame.sort_values(["trade_date", "symbol"]).reset_index(drop=True),
            query={
                "endpoint": (
                    "stock_basic_all_status_interval_reconstruction"
                    if reconstructed
                    else "bak_basic"
                ),
                "trade_date": trade_date,
                "fields": fields,
                "page_size": TUSHARE_PAGE_SIZES["bak_basic"],
            },
            metadata={
                "snapshot_semantics": "historical daily stock universe",
                "bak_basic_empty_fallback_used": reconstructed,
                "fallback_semantics": (
                    "all-status security master filtered by effective listing interval; "
                    "event boundaries are not exposed as research features"
                ),
                "research_feature_allowed": False,
                "pagination": pagination,
            },
            partition_values={"trade_date": trade_date},
        ).validate()

    def _reconstruct_historical_instruments(self, trade_date: str) -> pd.DataFrame:
        master = self._instrument_master_cache
        if master is None:
            master = self.fetch_instruments().frame
        date = pd.Timestamp(trade_date).normalize()
        work = master.copy()
        work["list_date"] = pd.to_datetime(
            work["list_date"], errors="coerce"
        ).dt.normalize()
        work["delist_date"] = pd.to_datetime(
            work["delist_date"], errors="coerce"
        ).dt.normalize()
        eligible = work.loc[
            work["instrument_kind"].eq("stock")
            & work["symbol"].astype(str).str.fullmatch(r"\d{6}\.(SH|SZ|BJ)")
            & work["list_date"].notna()
            & work["list_date"].le(date)
            & (work["delist_date"].isna() | work["delist_date"].ge(date))
        ].copy()
        if eligible.empty:
            raise ProviderError(
                f"security-master interval reconstruction is empty for {trade_date}"
            )
        eligible["trade_date"] = date
        eligible["universe_snapshot_method"] = (
            "all_status_security_master_effective_interval"
        )
        columns = [
            "symbol",
            "trade_date",
            "name",
            "industry",
            "area",
            "list_date",
            "universe_snapshot_method",
        ]
        return eligible[columns].sort_values("symbol").reset_index(drop=True)

    def fetch_security_code_mappings(self) -> FetchResult:
        """Fetch the BSE old/new trading-code crosswalk as an immutable snapshot."""
        try:
            raw, pagination = self._fetch_paginated("bse_mapping", {})
        except Exception as exc:
            raise ProviderError(f"Tushare BSE code-mapping request failed: {exc}") from exc
        if raw.empty:
            raise ProviderError("Tushare returned no BSE security-code mappings")
        frame = raw.rename(
            columns={"o_code": "historical_symbol", "n_code": "current_symbol"}
        ).copy()
        frame["historical_symbol"] = frame["historical_symbol"].map(normalize_cn_symbol)
        frame["current_symbol"] = frame["current_symbol"].map(normalize_cn_symbol)
        frame["list_date"] = _parse_yyyymmdd(frame["list_date"])
        frame["source"] = self.name
        frame["ingested_at"] = _utc_now()
        return FetchResult(
            dataset="security_code_mappings",
            provider=self.name,
            frame=frame.sort_values(["current_symbol", "historical_symbol"]).reset_index(
                drop=True
            ),
            query={
                "endpoint": "bse_mapping",
                "page_size": TUSHARE_PAGE_SIZES["bse_mapping"],
            },
            metadata={
                "snapshot_semantics": "BSE old/new security-code crosswalk",
                "effective_dates": "derived downstream from versioned BSE transition policy",
                "pagination": pagination,
            },
        ).validate()

    def fetch_fundamentals(
        self,
        statement: str,
        symbol: str,
        start_date: str,
        end_date: str,
        period: Optional[str] = None,
    ) -> FetchResult:
        endpoints: Dict[str, str] = {
            "income": "income",
            "balance_sheet": "balancesheet",
            "cashflow": "cashflow",
            "financial_indicators": "fina_indicator",
        }
        if statement not in endpoints:
            raise ProviderError(f"Unsupported fundamental statement: {statement}")
        canonical = normalize_cn_symbol(symbol)
        params: Dict[str, Any] = {
            "ts_code": canonical,
            "start_date": start_date.replace("-", ""),
            "end_date": end_date.replace("-", ""),
        }
        if period:
            params["period"] = period.replace("-", "")
        endpoint = endpoints[statement]
        try:
            raw = self._call_endpoint(endpoint, **params)
        except Exception as exc:
            raise ProviderError(
                f"Tushare {statement} request failed for {canonical}: {exc}"
            ) from exc
        if raw is None:
            raise ProviderError(f"Tushare returned a null {statement} response for {canonical}")
        if statement == "financial_indicators" and len(raw) >= 100:
            raise ProviderError(
                "Tushare fina_indicator reached its documented 100-row response limit; "
                "narrow the requested date window to prove completeness"
            )
        if raw.empty:
            raw = pd.DataFrame(
                columns=["ts_code", "ann_date", "f_ann_date", "end_date"]
            )
        return self._normalize_fundamentals(
            raw,
            statement,
            query={"endpoint": endpoint, **params},
            partition_values={"symbol": canonical},
            metadata={
                "date_filter_semantics": (
                    "report_period"
                    if statement == "financial_indicators"
                    else "announcement_date"
                ),
                "documented_response_limit": (
                    100 if statement == "financial_indicators" else None
                ),
                "request_axis": "symbol",
            },
            fallback_symbol=canonical,
        )

    def fetch_fundamentals_by_period(
        self, statement: str, period: str
    ) -> FetchResult:
        """Fetch one full-market report period through the privileged endpoints."""
        endpoints: Dict[str, str] = {
            "income": "income_vip",
            "balance_sheet": "balancesheet_vip",
            "cashflow": "cashflow_vip",
            "financial_indicators": "fina_indicator_vip",
        }
        if statement not in endpoints:
            raise ProviderError(f"Unsupported fundamental statement: {statement}")
        normalized_period = pd.Timestamp(period).strftime("%Y%m%d")
        endpoint = endpoints[statement]
        try:
            raw, pagination = self._fetch_paginated(
                endpoint, {"period": normalized_period}
            )
        except Exception as exc:
            raise ProviderError(
                f"Tushare {endpoint} request failed for {normalized_period}: {exc}"
            ) from exc
        if raw is None:
            raise ProviderError(
                f"Tushare returned a null {endpoint} response for {normalized_period}"
            )
        if raw.empty:
            raw = pd.DataFrame(
                columns=["ts_code", "ann_date", "f_ann_date", "end_date"]
            )
        return self._normalize_fundamentals(
            raw,
            statement,
            query={
                "endpoint": endpoint,
                "period": normalized_period,
                "page_size": TUSHARE_PAGE_SIZES[endpoint],
            },
            partition_values={"report_period": str(pd.Timestamp(period).date())},
            metadata={
                "date_filter_semantics": "report_period",
                "request_axis": "full_market_report_period",
                "pagination": pagination,
            },
        )

    def _normalize_fundamentals(
        self,
        raw: pd.DataFrame,
        statement: str,
        *,
        query: Dict[str, Any],
        partition_values: Dict[str, Any],
        metadata: Dict[str, Any],
        fallback_symbol: Optional[str] = None,
    ) -> FetchResult:
        frame = raw.rename(
            columns={
                "ts_code": "symbol",
                "end_date": "report_period",
                "ann_date": "announcement_date",
                "f_ann_date": "actual_announcement_date",
            }
        ).copy()
        if "symbol" not in frame:
            frame["symbol"] = fallback_symbol
        if not frame.empty:
            frame["source_symbol"] = frame["symbol"].astype("string")
            normalized = frame["source_symbol"].map(_try_normalize_cn_symbol)
            frame["instrument_kind"] = normalized.isna().map(
                {True: "vendor_nonstandard", False: "stock"}
            )
            frame["symbol"] = normalized.fillna(frame["source_symbol"])
        for column in ["report_period", "announcement_date", "actual_announcement_date"]:
            if column not in frame:
                frame[column] = pd.NaT
            frame[column] = _parse_yyyymmdd(frame[column])
        frame["available_date"] = frame["actual_announcement_date"].fillna(
            frame["announcement_date"]
        )
        frame["pit_eligible"] = frame["report_period"].notna() & frame[
            "available_date"
        ].notna()
        frame["pit_exclusion_reason"] = pd.Series(
            pd.NA, index=frame.index, dtype="string"
        )
        frame.loc[
            frame["report_period"].notna() & frame["available_date"].isna(),
            "pit_exclusion_reason",
        ] = "missing_announcement_date"
        frame.loc[
            frame["report_period"].isna(), "pit_exclusion_reason"
        ] = "missing_report_period"
        frame["statement_type"] = statement
        row_hashes = _source_row_hashes(frame)
        frame["source_row_sha256"] = row_hashes
        frame["source_row_occurrence"] = row_hashes.groupby(row_hashes).cumcount()
        frame["source"] = self.name
        frame["ingested_at"] = _utc_now()
        dataset = f"fundamentals_{statement}"
        return FetchResult(
            dataset=dataset,
            provider=self.name,
            frame=frame.sort_values(["available_date", "report_period"]).reset_index(drop=True),
            query=query,
            metadata={
                "point_in_time_field": "available_date",
                "available_date_rule": "actual_announcement_date else announcement_date",
                "raw_revisions_preserved": True,
                "empty_snapshot_is_valid": True,
                **metadata,
            },
            partition_values=partition_values,
        ).validate()


def _source_row_hashes(frame: pd.DataFrame) -> pd.Series:
    """Return deterministic identities before ingestion metadata is attached."""
    if frame.empty:
        return pd.Series(index=frame.index, dtype="string")
    columns = sorted(
        column
        for column in frame.columns
        if column not in {"source_row_sha256", "source_row_occurrence", "source", "ingested_at"}
    )

    def canonical(value: Any) -> Any:
        if pd.isna(value):
            return None
        if isinstance(value, pd.Timestamp):
            return value.isoformat()
        if hasattr(value, "item"):
            value = value.item()
        return value

    hashes = []
    for record in frame[columns].to_dict("records"):
        payload = {key: canonical(value) for key, value in record.items()}
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        hashes.append(hashlib.sha256(encoded).hexdigest())
    return pd.Series(hashes, index=frame.index, dtype="string")


def _try_normalize_cn_symbol(symbol: Any) -> Optional[str]:
    try:
        return normalize_cn_symbol(str(symbol))
    except DataContractError:
        return None


def _concat_provider_pages(frames: Sequence[pd.DataFrame]) -> pd.DataFrame:
    """Preserve the union schema without deprecated all-null dtype inference."""
    columns = list(dict.fromkeys(column for frame in frames for column in frame.columns))
    informative = [frame.dropna(axis="columns", how="all") for frame in frames]
    return pd.concat(informative, ignore_index=True, sort=False).reindex(columns=columns)
