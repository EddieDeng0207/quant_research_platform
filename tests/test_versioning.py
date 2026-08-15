import subprocess
import tempfile
from pathlib import Path

import pytest

from qrp.versioning import (
    VersionControlError,
    archive_committed_source,
    environment_lock_identity,
    inspect_git_repository,
)


def _repository(root: Path) -> Path:
    repository = root / "repo"
    repository.mkdir()
    (repository / "model.py").write_text("VERSION = 1\n", encoding="utf-8")
    (repository / "requirements.lock").write_text(
        "pandas==2.3.3\n", encoding="utf-8"
    )
    subprocess.run(["git", "init"], cwd=repository, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=repository,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repository,
        check=True,
    )
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(
        ["git", "commit", "-m", "baseline"],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    return repository


def test_git_identity_environment_lock_and_source_archive_are_recoverable():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        repository = _repository(root)
        identity = inspect_git_repository(repository, require_clean=True)
        assert identity.working_tree_clean
        assert len(identity.commit) == 40
        assert len(identity.tree) == 40
        lock = environment_lock_identity(repository)
        assert len(lock["sha256"]) == 64
        archive = archive_committed_source(
            repository,
            root / "source_bundle.tar",
        )
        assert archive["commit"] == identity.commit
        assert archive["bytes"] > 0


def test_formal_identity_rejects_dirty_worktree():
    with tempfile.TemporaryDirectory() as directory:
        repository = _repository(Path(directory))
        (repository / "model.py").write_text("VERSION = 2\n", encoding="utf-8")
        exploratory = inspect_git_repository(repository)
        assert not exploratory.working_tree_clean
        assert exploratory.dirty_state_sha256
        with pytest.raises(VersionControlError):
            inspect_git_repository(repository, require_clean=True)
