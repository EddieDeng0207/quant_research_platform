"""Build the full-window P0.7 joint-coverage preflight artifact."""

from __future__ import annotations

import argparse
from pathlib import Path

from qrp.data.factor_coverage import build_inverse_pb_coverage_audit


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tradability-artifact", action="append", required=True)
    parser.add_argument("--industry-artifact", required=True)
    parser.add_argument("--lake-root", default="data/lake")
    parser.add_argument("--output-root", default="artifacts/audits/p07_joint_coverage_preflight")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--minimum-coverage", type=float, default=0.80)
    args = parser.parse_args()
    output = build_inverse_pb_coverage_audit(
        tradability_artifacts=[Path(value) for value in args.tradability_artifact],
        lake_root=Path(args.lake_root),
        industry_artifact=Path(args.industry_artifact),
        output_root=Path(args.output_root),
        start_date=args.start,
        end_date=args.end,
        minimum_coverage=args.minimum_coverage,
    )
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
