import json
import tempfile
import unittest
from pathlib import Path

from qrp.data.reporting import generate_ingestion_report


class ReportingTests(unittest.TestCase):
    def test_report_is_derived_from_run_and_audit_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "run"
            run.mkdir()
            (run / "run_manifest.json").write_text(
                json.dumps(
                    {
                        "run_id": "run-1",
                        "status": "completed",
                        "started_at": "start",
                        "finished_at": "finish",
                        "source_tree_sha256": "source-hash",
                        "config_hash": "config-hash",
                    }
                ),
                encoding="utf-8",
            )
            (run / "config_snapshot.json").write_text(
                json.dumps({"start_date": "2024-01-02"}), encoding="utf-8"
            )
            (run / "summary.json").write_text(
                json.dumps(
                    {
                        "checkpoint": "state.json",
                        "files_written": 1,
                        "rows_written": 10,
                        "completed_this_run": 1,
                        "skipped_from_checkpoint": 0,
                        "open_dates": 1,
                        "failed_tasks": [],
                    }
                ),
                encoding="utf-8",
            )
            (run / "events.jsonl").write_text(
                json.dumps(
                    {
                        "event": "task_completed",
                        "dataset": "daily_bars",
                        "rows": 10,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            audit = root / "audit.json"
            audit.write_text(
                json.dumps(
                    {
                        "passed": True,
                        "manifest_entries": 1,
                        "files_checked": 1,
                        "rows_checked": 10,
                        "errors": [],
                        "warnings": [],
                    }
                ),
                encoding="utf-8",
            )
            output = root / "report.md"
            generate_ingestion_report(run, audit, output)
            text = output.read_text(encoding="utf-8")
            self.assertIn("本批次通过验收", text)
            self.assertIn("`daily_bars` | 1 | 10", text)
            self.assertIn("`PASS`", text)
