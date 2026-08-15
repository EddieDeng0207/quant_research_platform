"""Git-backed code identity and deterministic committed-source archives."""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Optional


class VersionControlError(RuntimeError):
    """Raised when formal research cannot prove a recoverable code version."""


@dataclass(frozen=True)
class GitIdentity:
    repository_root: str
    commit: str
    tree: str
    branch: str
    tag: Optional[str]
    remote: Optional[str]
    working_tree_clean: bool
    dirty_state_sha256: Optional[str]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def inspect_git_repository(
    start: Path,
    *,
    require_clean: bool = False,
) -> GitIdentity:
    """Return the exact recoverable Git identity containing ``start``."""
    candidate = Path(start).resolve()
    if candidate.is_file():
        candidate = candidate.parent
    root_result = _git(candidate, "rev-parse", "--show-toplevel", check=False)
    if root_result.returncode != 0:
        raise VersionControlError(f"not inside a Git repository: {candidate}")
    root = Path(root_result.stdout.strip()).resolve()
    commit = _git(root, "rev-parse", "HEAD").stdout.strip()
    tree = _git(root, "rev-parse", "HEAD^{tree}").stdout.strip()
    branch = _git(root, "branch", "--show-current").stdout.strip() or "DETACHED"
    tag_result = _git(root, "describe", "--tags", "--exact-match", check=False)
    tag = tag_result.stdout.strip() if tag_result.returncode == 0 else None
    remote_result = _git(root, "remote", "get-url", "origin", check=False)
    remote = (
        _normalize_remote(remote_result.stdout.strip())
        if remote_result.returncode == 0
        else None
    )
    status = _git(
        root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    ).stdout
    clean = not status.strip()
    dirty_sha = None if clean else _dirty_state_sha256(root, status)
    identity = GitIdentity(
        repository_root=str(root),
        commit=commit,
        tree=tree,
        branch=branch,
        tag=tag,
        remote=remote,
        working_tree_clean=clean,
        dirty_state_sha256=dirty_sha,
    )
    if require_clean and not clean:
        raise VersionControlError(
            "formal research requires a clean Git worktree; commit or discard changes"
        )
    return identity


def archive_committed_source(
    repository_root: Path,
    destination: Path,
    *,
    commit: str = "HEAD",
) -> Dict[str, Any]:
    """Write a deterministic Git archive and return its content identity."""
    root = Path(repository_root).resolve()
    target = Path(destination).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise VersionControlError(f"source archive already exists: {target}")
    result = subprocess.run(
        ["git", "archive", "--format=tar", f"--output={target}", commit],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise VersionControlError(result.stderr.strip() or "git archive failed")
    return {
        "path": str(target),
        "commit": _git(root, "rev-parse", commit).stdout.strip(),
        "sha256": _sha256(target),
        "bytes": target.stat().st_size,
    }


def environment_lock_identity(repository_root: Path) -> Dict[str, Any]:
    """Require and fingerprint the committed dependency lock."""
    path = Path(repository_root).resolve() / "requirements.lock"
    if not path.exists():
        raise VersionControlError(f"environment lock does not exist: {path}")
    return {
        "path": str(path.relative_to(Path(repository_root).resolve())),
        "sha256": _sha256(path),
        "bytes": path.stat().st_size,
    }


def _dirty_state_sha256(root: Path, status: str) -> str:
    digest = hashlib.sha256(status.encode("utf-8"))
    for args in (("diff", "--binary", "HEAD"), ("diff", "--binary", "--cached")):
        result = _git(root, *args, check=False)
        digest.update(result.stdout.encode("utf-8"))
    for line in sorted(status.splitlines()):
        if not line.startswith("?? "):
            continue
        path = root / line[3:]
        if path.is_file():
            digest.update(line[3:].encode("utf-8"))
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _normalize_remote(remote: str) -> str:
    if remote.startswith("git@github.com:"):
        return "https://github.com/" + remote.split(":", 1)[1]
    if "@" in remote and "://" in remote:
        scheme, remainder = remote.split("://", 1)
        remote = f"{scheme}://{remainder.split('@', 1)[1]}"
    return remote


def _git(
    root: Path,
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise VersionControlError(result.stderr.strip() or "Git command failed")
    return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
