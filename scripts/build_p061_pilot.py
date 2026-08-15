"""Build the P0.6.1 pilot blotter from causal capacity panels."""

from pathlib import Path

import pandas as pd

from qrp.execution import CAPACITY_FIELDS

ROOT = Path(__file__).resolve().parents[1]
orders = pd.read_csv(ROOT / "artifacts/pilots/p06_orders_20240102_20240105.csv")
orders["trade_date"] = pd.to_datetime(orders["trade_date"]).dt.normalize()
panels = pd.concat(
    [
        pd.read_parquet(ROOT / "artifacts/pilots/p061_capacity_000001.parquet"),
        pd.read_parquet(ROOT / "artifacts/pilots/p061_capacity_600000.parquet"),
    ],
    ignore_index=True,
)
pilot = orders.drop(columns=["adv_shares_lag1"]).merge(
    panels[["symbol", "trade_date", *CAPACITY_FIELDS]],
    on=["symbol", "trade_date"],
    how="left",
    validate="many_to_one",
)
pilot["adv_shares_lag1"] = pilot["adv20_shares_lag1"]
columns = [
    "order_id",
    "trade_date",
    "instrument_id",
    "symbol",
    "side",
    "quantity",
    "limit_price",
    "adv_shares_lag1",
    *CAPACITY_FIELDS,
]
output = ROOT / "artifacts/pilots/p061_orders_20240102_20240105.parquet"
pilot[columns].to_parquet(output, index=False)
print(output)
