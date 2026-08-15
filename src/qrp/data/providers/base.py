"""Provider interfaces and common result metadata."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping

import pandas as pd

from ..contracts import validate_dataset


class ProviderError(RuntimeError):
    """Raised when a source is unavailable or returns malformed data."""


@dataclass
class FetchResult:
    """Canonical provider output plus the provenance needed for reproduction."""

    dataset: str
    provider: str
    frame: pd.DataFrame
    query: Mapping[str, Any]
    metadata: Dict[str, Any] = field(default_factory=dict)
    partition_values: Mapping[str, Any] = field(default_factory=dict)

    def validate(self) -> "FetchResult":
        validate_dataset(self.dataset, self.frame)
        return self
