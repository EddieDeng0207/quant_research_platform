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
from .factor_registry import (
    DEFAULT_FACTOR_REGISTRY,
    REV20_SKIP1,
    SP_TTM,
    FactorDefinition,
    FactorRegistry,
)
from .factor_universe import (
    CN_A_FULL,
    CN_A_SW_L1_CORE,
    RESEARCH_UNIVERSE_PROFILES,
    ResearchUniverseSpec,
    attach_research_universe,
)
from .fundamental_factors import (
    FundamentalFactorError,
    SalesToPriceInputSpec,
    build_pit_ttm_revenue_snapshots,
    build_sales_to_price_input_artifact,
)
from .price_reversal import (
    PriceReversalError,
    PriceReversalInputSpec,
    build_price_reversal_input_artifact,
)
from .reversal_execution import (
    FactorExecutionInputError,
    FactorExecutionInputSpec,
    ReversalExecutionInputError,
    ReversalExecutionInputSpec,
    build_factor_execution_input_artifact,
    build_reversal_execution_input_artifact,
)
from .timing import FACTOR_TIMING_CONTRACTS, FactorTimingContract, validate_factor_timing

__all__ = [
    "ExperimentSpec",
    "FACTOR_TIMING_CONTRACTS",
    "FactorTimingContract",
    "FactorEvaluationError",
    "FactorEvaluationResult",
    "FactorEvaluationSpec",
    "FactorDefinition",
    "FactorExecutionInputError",
    "FactorExecutionInputSpec",
    "FactorRegistry",
    "ResearchUniverseSpec",
    "FundamentalFactorError",
    "PriceReversalError",
    "PriceReversalInputSpec",
    "SalesToPriceInputSpec",
    "DEFAULT_FACTOR_REGISTRY",
    "CN_A_FULL",
    "CN_A_SW_L1_CORE",
    "RESEARCH_UNIVERSE_PROFILES",
    "REV20_SKIP1",
    "SP_TTM",
    "ReversalExecutionInputError",
    "ReversalExecutionInputSpec",
    "benjamini_hochberg",
    "build_factor_evaluation_artifact",
    "build_factor_execution_input_artifact",
    "attach_research_universe",
    "build_price_reversal_input_artifact",
    "build_pit_ttm_revenue_snapshots",
    "build_sales_to_price_input_artifact",
    "build_reversal_execution_input_artifact",
    "evaluate_single_factor",
    "generate_factor_evaluation_report",
    "register_experiment",
    "validate_factor_timing",
    "walk_forward_splits",
]
