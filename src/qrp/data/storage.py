"""Append-only local Parquet lake with per-file provenance manifests."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator

import pandas as pd

from .providers.base import FetchResult


def _json_default(value: Any) -> str:
    if isinstance(value, (pd.Timestamp, Path)):
        return str(value)
    return str(value)


class ParquetLake:
    """Write immutable source snapshots and an auditable JSONL manifest."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def write(self, result: FetchResult) -> Path:
        result.validate()
        now = pd.Timestamp.now(tz="UTC")
        partition = (
            self.root
            / "raw"
            / f"provider={result.provider}"
            / f"dataset={result.dataset}"
            / f"ingest_date={now.date().isoformat()}"
        )
        for key, value in sorted(result.partition_values.items()):
            safe_key = _safe_partition_value(key)
            safe_value = _safe_partition_value(value)
            partition = partition / f"{safe_key}={safe_value}"
        partition.mkdir(parents=True, exist_ok=True)
        filename = f"part-{now.strftime('%H%M%S')}-{uuid.uuid4().hex[:12]}.parquet"
        target = partition / filename
        fd, temporary_name = tempfile.mkstemp(prefix=".part-", suffix=".parquet", dir=partition)
        os.close(fd)
        temporary = Path(temporary_name)
        try:
            result.frame.to_parquet(temporary, index=False, engine="pyarrow")
            os.replace(temporary, target)
        finally:
            if temporary.exists():
                temporary.unlink()

        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        entry: Dict[str, Any] = {
            "written_at": now.isoformat(),
            "provider": result.provider,
            "dataset": result.dataset,
            "path": str(target.relative_to(self.root)),
            "sha256": digest,
            "rows": int(len(result.frame)),
            "columns": list(result.frame.columns),
            "query": dict(result.query),
            "metadata": result.metadata,
            "partition_values": dict(result.partition_values),
        }
        self._append_manifest(entry)
        return target

    def _append_manifest(self, entry: Dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        manifest = self.root / "manifest.jsonl"
        line = json.dumps(entry, ensure_ascii=False, default=_json_default, sort_keys=True) + "\n"
        with _locked_append(manifest) as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())


def _safe_partition_value(value: Any) -> str:
    text = str(value)
    safe = "".join(character if character.isalnum() or character in "-_." else "_" for character in text)
    if not safe or safe in {".", ".."}:
        raise ValueError(f"Unsafe partition value: {value!r}")
    return safe


@contextmanager
def _locked_append(path: Path) -> Iterator[Any]:
    """Append with an advisory lock so future Agent workers cannot interleave lines."""
    with path.open("a", encoding="utf-8") as handle:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except ImportError:
            fcntl = None
        try:
            yield handle
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
