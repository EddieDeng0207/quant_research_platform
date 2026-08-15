import hashlib
import json

import pandas as pd
import pytest

from qrp.data.isolation import (
    IsolationError,
    ResearchDataGate,
    SplitWindow,
    TemporalIsolationPlan,
    apply_temporal_isolation,
    seal_isolated_dataset,
)


def _plan():
    return TemporalIsolationPlan(
        train=SplitWindow("train", "2024-01-02", "2024-01-05"),
        validation=SplitWindow("validation", "2024-01-09", "2024-01-11"),
        holdout=SplitWindow("holdout", "2024-01-15", "2024-01-17"),
        label_horizon_sessions=1,
        embargo_sessions=1,
    )


def test_temporal_isolation_purges_label_horizon_and_enforces_embargo():
    sessions = pd.bdate_range("2024-01-02", "2024-01-17")
    frame = pd.DataFrame(
        {
            "symbol": "000001.SZ",
            "trade_date": sessions,
            "feature": range(len(sessions)),
            "target": range(len(sessions)),
        }
    )
    splits, audit = apply_temporal_isolation(frame, _plan(), sessions)
    assert splits["train"]["trade_date"].max() == pd.Timestamp("2024-01-04")
    assert splits["validation"]["trade_date"].max() == pd.Timestamp("2024-01-10")
    assert splits["holdout"]["trade_date"].max() == pd.Timestamp("2024-01-16")
    assert audit["splits"]["train"]["purged_rows"] == 1

    bad = TemporalIsolationPlan(
        train=SplitWindow("train", "2024-01-02", "2024-01-05"),
        validation=SplitWindow("validation", "2024-01-08", "2024-01-11"),
        holdout=SplitWindow("holdout", "2024-01-15", "2024-01-17"),
        embargo_sessions=1,
    )
    with pytest.raises(IsolationError, match="embargo"):
        bad.validate(sessions)


def test_sealed_holdout_is_denied_in_development_and_requires_matching_grant(tmp_path):
    sessions = pd.bdate_range("2024-01-02", "2024-01-17")
    frame = pd.DataFrame(
        {
            "symbol": "000001.SZ",
            "trade_date": sessions,
            "feature": range(len(sessions)),
            "target": range(len(sessions)),
        }
    )
    splits, audit = apply_temporal_isolation(frame, _plan(), sessions)
    root = seal_isolated_dataset(
        splits,
        tmp_path,
        "synthetic_v1",
        _plan(),
        feature_columns=["feature"],
        label_columns=["target"],
        audit=audit,
    )
    development = ResearchDataGate(root)
    assert len(development.read("train", "features")) == 3
    with pytest.raises(IsolationError, match="denied"):
        development.read("holdout", "features")

    manifest_path = root / "manifest.json"
    grant = {
        "dataset_id": "synthetic_v1",
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "approved_by": "research_owner",
        "approved_at": "2024-01-18T00:00:00Z",
        "purpose": "final_model_v1",
    }
    grant_path = tmp_path / "grant.json"
    grant_path.write_text(json.dumps(grant), encoding="utf-8")
    final = ResearchDataGate(root, mode="holdout_evaluation")
    assert len(final.read("holdout", "features", grant_path, "final_model_v1")) == 2
    records = [
        json.loads(line)
        for line in (root / "access_log.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [record["outcome"] for record in records] == ["granted", "denied", "granted"]
    assert records[0]["previous_entry_sha256"] == "0" * 64
    assert records[2]["previous_entry_sha256"] == records[1]["entry_sha256"]
