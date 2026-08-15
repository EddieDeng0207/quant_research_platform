"""Lazy construction of configured providers."""

from __future__ import annotations

from typing import Any

from .providers.akshare import AkshareProvider
from .providers.base import ProviderError
from .providers.fred import FredProvider
from .providers.tushare import TushareProvider

PROVIDER_CAPABILITIES = {
    "akshare": AkshareProvider.capabilities,
    "tushare": TushareProvider.capabilities,
    "fred": FredProvider.capabilities,
}


def create_provider(name: str) -> Any:
    if name == "akshare":
        return AkshareProvider()
    if name == "tushare":
        return TushareProvider()
    if name == "fred":
        return FredProvider()
    raise ProviderError(f"Unknown provider: {name}")
