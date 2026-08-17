"""Independently verify a chained P0.5 historical release."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import pandas as pd

HARD_FAILURES = (
    "unexplained_missing_bar_rows",
    "bar_without_limit_rows",
    "price_above_up_limit_rows",
    "price_below_down_limit_rows",
)
BSE_OPEN_DATE = pd.Timestamp("2021-11-15")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _output_path(artifact: Path, manifest: dict[str, Any]) -> Path:
    declared = Path(manifest["output"]["path"])
    if declared.exists():
        return declared
    candidate = artifact / declared.name
    if candidate.exists():
        return candidate
    raise FileNotFoundError(f"P0.5 Parquet is missing: {declared}")


def audit_release(artifacts: list[Path]) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    for artifact in artifacts:
        manifest_path = artifact / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        parquet_path = _output_path(artifact, manifest)
        actual_sha = _sha256(parquet_path)
        identity = manifest["identity"]
        quality = manifest["quality"]
        hard_failures = quality["hard_failures"]
        records.append(
            {
                "artifact_id": manifest["artifact_id"],
                "start_date": identity["start_date"],
                "end_date": identity["end_date"],
                "implementation_sha256": identity["implementation_sha256"],
                "parquet_path": str(parquet_path),
                "parquet_sha256": actual_sha,
                "physical_hash_matches": actual_sha == manifest["output"]["sha256"],
                "promotion_passed": bool(quality["promotion_passed"]),
                "hard_failures": {key: int(hard_failures[key]) for key in HARD_FAILURES},
                "prior": manifest["inputs"].get("prior_tradability_artifact", []),
                "quality": {
                    "rows": int(quality["rows"]),
                    "symbols": int(quality["symbols"]),
                    "trading_dates": int(quality["trading_dates"]),
                    "carried_forward_suspension_rows": int(
                        quality["carried_forward_suspension_rows"]
                    ),
                    "pre_bse_open_universe_rows_excluded": int(
                        quality["pre_bse_open_universe_rows_excluded"]
                    ),
                    "pre_bse_listing_bar_rows_excluded": int(
                        quality["pre_bse_listing_bar_rows_excluded"]
                    ),
                    "reviewed_unbounded_limit_rows": int(
                        quality["reviewed_unbounded_limit_rows"]
                    ),
                },
            }
        )

    records.sort(key=lambda item: item["start_date"])
    chain_errors: list[str] = []
    for index, record in enumerate(records):
        prior = record.pop("prior")
        if index == 0:
            if prior:
                chain_errors.append(f"{record['artifact_id']} unexpectedly declares a predecessor")
            continue
        expected = records[index - 1]
        if len(prior) != 1:
            chain_errors.append(f"{record['artifact_id']} has {len(prior)} predecessor records")
            continue
        reference = prior[0]
        if reference["artifact_id"] != expected["artifact_id"]:
            chain_errors.append(f"{record['artifact_id']} points to the wrong predecessor")
        if reference["parquet_sha256"] != expected["parquet_sha256"]:
            chain_errors.append(f"{record['artifact_id']} predecessor SHA-256 is inconsistent")
        if reference["end_date"] >= record["start_date"]:
            chain_errors.append(f"{record['artifact_id']} predecessor date overlaps")

    implementation_hashes = sorted({item["implementation_sha256"] for item in records})
    bse_record = next(item for item in records if item["start_date"].startswith("2021"))
    bse = pd.read_parquet(
        bse_record["parquet_path"], columns=["symbol", "trade_date", "list_date"]
    )
    bse["trade_date"] = pd.to_datetime(bse["trade_date"]).dt.normalize()
    bse["list_date"] = pd.to_datetime(bse["list_date"]).dt.normalize()
    bse = bse.loc[bse["symbol"].astype(str).str.endswith(".BJ")]
    bse_probe = {
        "pre_open_rows": int((bse["trade_date"] < BSE_OPEN_DATE).sum()),
        "opening_day_rows": int((bse["trade_date"] == BSE_OPEN_DATE).sum()),
        "post_open_rows": int((bse["trade_date"] > BSE_OPEN_DATE).sum()),
        "list_date_before_market_open_rows": int((bse["list_date"] < BSE_OPEN_DATE).sum()),
    }

    suspension_record = next(item for item in records if item["start_date"].startswith("2025"))
    suspension = pd.read_parquet(
        suspension_record["parquet_path"],
        columns=[
            "symbol",
            "trade_date",
            "has_bar",
            "is_suspended",
            "vendor_is_suspended",
            "carried_forward_suspension",
            "suspension_state_source",
        ],
        filters=[("symbol", "==", "688766.SH")],
    ).sort_values("trade_date")
    suspension["trade_date"] = pd.to_datetime(suspension["trade_date"]).dt.normalize()
    suspension = suspension.loc[
        suspension["trade_date"].between("2025-11-25", "2025-12-09")
    ]
    suspension_probe = suspension.assign(
        trade_date=suspension["trade_date"].dt.strftime("%Y-%m-%d")
    ).to_dict(orient="records")
    suspension_probe_passed = bool(
        len(suspension) == 11
        and suspension.loc[suspension["trade_date"] < pd.Timestamp("2025-12-09"), "is_suspended"].all()
        and suspension.loc[
            suspension["trade_date"] == pd.Timestamp("2025-12-09"), "has_bar"
        ].all()
        and not suspension.loc[
            suspension["trade_date"] == pd.Timestamp("2025-12-09"), "is_suspended"
        ].any()
    )

    all_hard_failures_zero = all(
        all(value == 0 for value in record["hard_failures"].values()) for record in records
    )
    passed = bool(
        len(records) == 11
        and all(record["physical_hash_matches"] for record in records)
        and all(record["promotion_passed"] for record in records)
        and all_hard_failures_zero
        and not chain_errors
        and len(implementation_hashes) == 1
        and bse_probe["pre_open_rows"] == 0
        and bse_probe["opening_day_rows"] > 0
        and bse_probe["list_date_before_market_open_rows"] == 0
        and suspension_probe_passed
    )
    return {
        "schema_version": "p05_historical_release_audit_v1",
        "passed": passed,
        "artifact_count": len(records),
        "all_physical_hashes_match": all(
            record["physical_hash_matches"] for record in records
        ),
        "all_artifacts_promoted": all(record["promotion_passed"] for record in records),
        "all_hard_failures_zero": all_hard_failures_zero,
        "implementation_hashes": implementation_hashes,
        "chain_errors": chain_errors,
        "bse_market_open_probe": bse_probe,
        "suspension_state_probe": {
            "symbol": "688766.SH",
            "passed": suspension_probe_passed,
            "rows": suspension_probe,
        },
        "artifacts": records,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tradability-artifact", action="append", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    report = audit_release([Path(value) for value in args.tradability_artifact])
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "artifacts"}, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
