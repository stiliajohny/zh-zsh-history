"""Read Git branch and commit from a work tree without running Git."""

from __future__ import annotations

from pathlib import Path


def read_git_metadata(start: Path) -> tuple[str | None, str | None]:
    """Return the branch name and commit hash for a directory, when Git metadata is present."""
    current = start if start.is_dir() else start.parent
    for directory in [current, *current.parents]:
        git_path = directory / ".git"
        if git_path.is_dir():
            return _read_head(git_path)
        if git_path.is_file():
            git_dir = _gitdir_file(directory, git_path)
            if git_dir is None:
                return None, None
            return _read_head(git_dir)
        if directory.parent == directory:
            break
    return None, None


def _gitdir_file(directory: Path, git_path: Path) -> Path | None:
    text = git_path.read_text(encoding="utf-8", errors="replace").strip()
    if not text.startswith("gitdir:"):
        return None
    raw = Path(text.split(":", 1)[1].strip())
    if not raw.is_absolute():
        raw = directory / raw
    if raw.is_dir():
        return raw
    return None


def _read_head(git_dir: Path) -> tuple[str | None, str | None]:
    head = git_dir / "HEAD"
    if not head.is_file():
        return None, None
    text = head.read_text(encoding="utf-8", errors="replace").strip()
    if text.startswith("ref:"):
        ref = text.split(":", 1)[1].strip()
        branch = ref.removeprefix("refs/heads/")
        return branch, _commit_for_ref(git_dir, ref)
    if text:
        return None, text
    return None, None


def _commit_for_ref(git_dir: Path, ref: str) -> str | None:
    ref_file = git_dir / ref
    if ref_file.is_file():
        commit = ref_file.read_text(encoding="utf-8", errors="replace").strip()
        return commit or None
    packed = git_dir / "packed-refs"
    if not packed.is_file():
        return None
    for line in packed.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line or line.startswith("#") or line.startswith("^"):
            continue
        commit, _, name = line.partition(" ")
        if name.strip() == ref:
            return commit.strip() or None
    return None
