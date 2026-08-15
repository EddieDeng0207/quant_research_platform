"""Research governance, factor timing and immutable experiment records."""

from .experiments import (
    ExperimentSpec,
    benjamini_hochberg,
    register_experiment,
    walk_forward_splits,
)
from .factor_artifact import (
    build_factor_evaluation_artifact,
    generate_factor_evaluation_report,
)
from .factor_evaluation import (
    FactorEvaluationError,
    FactorEvaluationResult,
    FactorEvaluationSpec,
    evaluate_single_factor,
)
from .timing import FACTOR_TIMING_CONTRACTS, FactorTimingContract, validate_factor_timing

__all__ = [
    "ExperimentSpec",
    "FACTOR_TIMING_CONTRACTS",
    "FactorTimingContract",
    "FactorEvaluationError",
    "FactorEvaluationResult",
    "FactorEvaluationSpec",
    "benjamini_hochberg",
    "build_factor_evaluation_artifact",
    "evaluate_single_factor",
    "generate_factor_evaluation_report",
    "register_experiment",
    "validate_factor_timing",
    "walk_forward_splits",
]
