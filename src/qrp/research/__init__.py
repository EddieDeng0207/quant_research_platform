"""Research governance, factor timing and immutable experiment records."""

from .experiments import (
    ExperimentSpec,
    benjamini_hochberg,
    register_experiment,
    walk_forward_splits,
)
from .timing import FACTOR_TIMING_CONTRACTS, FactorTimingContract, validate_factor_timing

__all__ = [
    "ExperimentSpec",
    "FACTOR_TIMING_CONTRACTS",
    "FactorTimingContract",
    "benjamini_hochberg",
    "register_experiment",
    "validate_factor_timing",
    "walk_forward_splits",
]
