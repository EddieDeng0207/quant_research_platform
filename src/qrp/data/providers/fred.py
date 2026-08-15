"""FRED/ALFRED REST adapter with real-time-period support."""

from __future__ import annotations

import json
import os
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd

from .base import FetchResult, ProviderError


class FredProvider:
    name = "fred"
    capabilities = ("macro_observations",)
    endpoint = "https://api.stlouisfed.org/fred/series/observations"

    def __init__(
        self,
        api_key: Optional[str] = None,
        opener: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.api_key = api_key or os.environ.get("FRED_API_KEY")
        if not self.api_key:
            raise ProviderError("FRED_API_KEY is required for the FRED provider")
        self._opener = opener or urlopen

    def fetch_series(
        self,
        series_id: str,
        observation_start: Optional[str] = None,
        observation_end: Optional[str] = None,
        realtime_start: Optional[str] = None,
        realtime_end: Optional[str] = None,
    ) -> FetchResult:
        params: Dict[str, Any] = {
            "series_id": series_id,
            "api_key": self.api_key,
            "file_type": "json",
            "output_type": 1,
        }
        optional = {
            "observation_start": observation_start,
            "observation_end": observation_end,
            "realtime_start": realtime_start,
            "realtime_end": realtime_end,
        }
        params.update({key: value for key, value in optional.items() if value})
        request = Request(
            f"{self.endpoint}?{urlencode(params)}",
            headers={"User-Agent": "quant-research-platform/0.1"},
        )
        try:
            with self._opener(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise ProviderError(f"FRED request failed for {series_id}: {exc}") from exc
        if "error_message" in payload:
            raise ProviderError(f"FRED error: {payload['error_message']}")
        frame = pd.DataFrame(payload.get("observations", []))
        if frame.empty:
            raise ProviderError(f"FRED returned no observations for {series_id}")
        frame = frame.rename(columns={"date": "observation_date"})
        frame["series_id"] = series_id
        for column in ["observation_date", "realtime_start", "realtime_end"]:
            frame[column] = pd.to_datetime(frame[column], errors="coerce")
        frame["value"] = pd.to_numeric(frame["value"].replace(".", pd.NA), errors="coerce")
        frame["source"] = self.name
        frame["ingested_at"] = pd.Timestamp.now(tz="UTC")
        columns = [
            "series_id",
            "observation_date",
            "value",
            "realtime_start",
            "realtime_end",
            "source",
            "ingested_at",
        ]
        return FetchResult(
            dataset="macro_observations",
            provider=self.name,
            frame=frame[columns].sort_values(
                ["observation_date", "realtime_start"]
            ).reset_index(drop=True),
            query={key: value for key, value in params.items() if key != "api_key"},
            metadata={
                "point_in_time_fields": ["realtime_start", "realtime_end"],
                "api_key_redacted": True,
            },
        ).validate()
