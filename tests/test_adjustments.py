import pandas as pd
import pytest

from qrp.data.adjustments import (
    AdjustmentError,
    AdjustmentSpec,
    build_adjusted_price_view,
    build_causal_return_panel,
)
from qrp.data.temporal import causal_asof_view


def _inputs():
    bars = pd.DataFrame(
        {
            "symbol": ["000001.SZ"] * 3,
            "trade_date": ["2024-01-02", "2024-01-03", "2024-01-04"],
            "open": [99.0, 101.0, 50.0],
            "high": [101.0, 103.0, 52.0],
            "low": [98.0, 100.0, 49.0],
            "close": [100.0, 102.0, 51.0],
            "volume": [10.0, 11.0, 22.0],
            "amount": [1000.0, 1122.0, 1122.0],
        }
    )
    factors = pd.DataFrame(
        {
            "symbol": ["000001.SZ"] * 3,
            "trade_date": ["2024-01-02", "2024-01-03", "2024-01-04"],
            "adj_factor": [1.0, 1.0, 2.0],
        }
    )
    return bars, factors


def test_qfq_anchor_is_explicit_and_cannot_see_future_factor():
    bars, factors = _inputs()
    before_action = build_adjusted_price_view(
        bars, factors, AdjustmentSpec(mode="qfq_asof", as_of_date="2024-01-03")
    )
    assert before_action["adj_close"].tolist() == [100.0, 102.0]

    after_action = build_adjusted_price_view(
        bars, factors, AdjustmentSpec(mode="qfq_asof", as_of_date="2024-01-04")
    )
    assert after_action["adj_close"].tolist() == [50.0, 51.0, 51.0]
    assert after_action["volume"].tolist() == bars["volume"].tolist()
    assert after_action["adjustment_anchor_date"].nunique() == 1
    assert causal_asof_view(
        after_action, pd.Timestamp("2024-01-04 15:59", tz="Asia/Shanghai")
    ).empty
    assert len(
        causal_asof_view(
            after_action, pd.Timestamp("2024-01-04 16:01", tz="Asia/Shanghai")
        )
    ) == 3


def test_hfq_and_causal_total_return_bridge_the_corporate_action():
    bars, factors = _inputs()
    hfq = build_adjusted_price_view(bars, factors, AdjustmentSpec(mode="hfq"))
    assert hfq["adj_close"].tolist() == [100.0, 102.0, 102.0]
    causal = build_causal_return_panel(bars, factors)
    assert causal.loc[2, "total_return_1d"] == pytest.approx(0.0)
    assert causal.loc[2, "factor_change"] == pytest.approx(1.0)


def test_total_return_index_and_missing_factor_fail_closed():
    bars, factors = _inputs()
    indexed = build_adjusted_price_view(
        bars,
        factors,
        AdjustmentSpec(mode="total_return_index", base_date="2024-01-02"),
    )
    assert indexed["adj_close"].tolist() == [100.0, 102.0, 102.0]
    with pytest.raises(AdjustmentError, match="Missing adjustment factor"):
        build_causal_return_panel(bars, factors.iloc[:-1])
