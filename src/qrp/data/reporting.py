"""Human-readable reports derived only from immutable run artifacts."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List


def generate_ingestion_report(run_path: Path, audit_path: Path, output_path: Path) -> Path:
    run_path = Path(run_path)
    audit_path = Path(audit_path)
    output_path = Path(output_path)
    manifest = _read_json(run_path / "run_manifest.json")
    config = _read_json(run_path / "config_snapshot.json")
    summary = _read_json(run_path / "summary.json")
    audit = _read_json(audit_path)
    events = _read_jsonl(run_path / "events.jsonl")

    datasets: Dict[str, Dict[str, int]] = defaultdict(lambda: {"files": 0, "rows": 0})
    for event in events:
        if event.get("event") != "task_completed":
            continue
        dataset = event["dataset"]
        datasets[dataset]["files"] += 1
        datasets[dataset]["rows"] += int(event["rows"])

    lines = [
        "# P0 数据接入验收报告",
        "",
        "## 结论",
        "",
        (
            "本批次通过验收。运行状态为 `completed`，数据湖完整性审计通过，"
            "未发现文件缺失、哈希不一致、行数不一致或数据契约错误。"
            if manifest.get("status") == "completed" and audit.get("passed")
            else "本批次未通过验收，必须检查运行清单和审计错误后再继续。"
        ),
        "",
        "## 运行身份",
        "",
        f"- Run ID：`{manifest.get('run_id')}`",
        f"- 开始时间：`{manifest.get('started_at')}`",
        f"- 结束时间：`{manifest.get('finished_at')}`",
        f"- 源代码树 SHA-256：`{manifest.get('source_tree_sha256')}`",
        f"- 配置 SHA-256：`{manifest.get('config_hash')}`",
        f"- Checkpoint：`{summary.get('checkpoint')}`",
        "",
        "## 冻结配置",
        "",
        "| 配置项 | 值 |",
        "|---|---|",
    ]
    for key, value in sorted(config.items()):
        rendered = json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else value
        lines.append(f"| `{key}` | `{rendered}` |")

    lines.extend(
        [
            "",
            "## 本次产物",
            "",
            "| 数据集 | 文件数 | 行数 |",
            "|---|---:|---:|",
        ]
    )
    for dataset, values in sorted(datasets.items()):
        lines.append(f"| `{dataset}` | {values['files']:,} | {values['rows']:,} |")
    lines.extend(
        [
            f"| **合计** | **{summary.get('files_written', 0):,}** | **{summary.get('rows_written', 0):,}** |",
            "",
            "## 断点续传",
            "",
            f"- 本次完成任务：{summary.get('completed_this_run', 0):,}",
            f"- 从 checkpoint 跳过：{summary.get('skipped_from_checkpoint', 0):,}",
            f"- 覆盖交易日：{summary.get('open_dates', 0):,}",
            f"- 失败任务：{len(summary.get('failed_tasks', [])):,}",
            "",
            "## 数据湖完整性审计",
            "",
            f"- Manifest 条目：{audit.get('manifest_entries', 0):,}",
            f"- 已检查文件：{audit.get('files_checked', 0):,}",
            f"- 已检查行数：{audit.get('rows_checked', 0):,}",
            f"- 错误：{len(audit.get('errors', [])):,}",
            f"- 警告：{len(audit.get('warnings', [])):,}",
            f"- 最终状态：`{'PASS' if audit.get('passed') else 'FAIL'}`",
            "",
            "## 可复现说明",
            "",
            "相同数据范围、数据集、交易所和任务名称共享同一 checkpoint；改变限速或重试参数不会改变数据任务身份。每次运行仍生成独立 Run ID、配置快照和事件日志。密钥值不进入任何运行产物。",
            "",
            "该报告由运行清单、事件日志和审计 JSON 自动生成，不接受手工修改指标。",
            "",
        ]
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(map(str, lines)), encoding="utf-8")
    return output_path


def _read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
