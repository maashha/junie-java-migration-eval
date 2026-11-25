"""Git operations: canonical clones, isolated working copies, diffs.

Layout under `work_dir`:

    work/<project>/canonical.git        bare clone, never checked out, never modified
                                        after creation (only `git fetch` if the commit
                                        is missing)
    work/<project>/original/            pristine checkout used for baseline validation
    work/<project>/<method>/run-NN/     one fresh copy per migration run

Run copies are independent `git clone --no-hardlinks` copies (not worktrees), so
nothing an agent does inside a copy (commits, branches, gc, push attempts) can reach
the canonical clone; the `origin` remote is removed from every copy.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .config import Project
from .process import CommandRunner, utc_now


class GitError(Exception):
    pass


def project_dir(work_dir: Path, project: Project) -> Path:
    return work_dir / project.name


def canonical_dir(work_dir: Path, project: Project) -> Path:
    return project_dir(work_dir, project) / "canonical.git"


def _git(runner: CommandRunner, args: list[str], cwd: Path, check: bool = True) -> str:
    code, out = runner.capture(["git", *args], cwd)
    if check and code != 0:
        raise GitError(f"git {' '.join(args)} failed in {cwd} (exit {code}):\n{out.strip()}")
    return out


def ensure_canonical_clone(runner: CommandRunner, work_dir: Path, project: Project) -> Path:
    """Create the bare canonical clone if needed and make sure base_commit exists in it."""
    canon = canonical_dir(work_dir, project)
    if runner.dry_run:
        runner.run(["git", "clone", "--bare", project.repo, str(canon)], work_dir)
        return canon
    if not canon.exists():
        canon.parent.mkdir(parents=True, exist_ok=True)
        res = runner.run(["git", "clone", "--bare", "--quiet", project.repo, str(canon)], canon.parent)
        if not res.ok:
            raise GitError(f"clone of {project.repo} failed (exit {res.exit_code}, {res.error})")
    if not has_commit(runner, canon, project.base_commit):
        # Commit may be unreachable from default refs; fetch it explicitly (GitHub allows by SHA).
        _git(runner, ["fetch", "--quiet", "origin", project.base_commit], canon, check=False)
        if not has_commit(runner, canon, project.base_commit):
            raise GitError(f"base_commit {project.base_commit} not found in {project.repo}")
    return canon


def has_commit(runner: CommandRunner, repo: Path, sha: str) -> bool:
    code, _ = runner.capture(["git", "cat-file", "-e", f"{sha}^{{commit}}"], repo)
    return code == 0


def create_checkout(runner: CommandRunner, work_dir: Path, project: Project, dest: Path) -> dict:
    """Create an isolated checkout of base_commit at `dest` and verify it.

    Refuses to touch an existing `dest` (never deletes or reuses directories).
    Returns repository metadata for metadata.json.
    """
    canon = canonical_dir(work_dir, project)
    if runner.dry_run:
        runner.run(["git", "clone", "--no-hardlinks", "--no-checkout", str(canon), str(dest)], work_dir)
        runner.run(["git", "checkout", "--detach", project.base_commit], dest)
        return {"repo_url": project.repo, "base_commit": project.base_commit, "dry_run": True}
    if dest.exists():
        raise GitError(f"refusing to reuse existing directory {dest}; choose a new run id")
    dest.parent.mkdir(parents=True, exist_ok=True)
    _git(runner, ["clone", "--quiet", "--no-hardlinks", "--no-checkout", str(canon), str(dest)], dest.parent)
    _git(runner, ["checkout", "--quiet", "--detach", project.base_commit], dest)
    _git(runner, ["remote", "remove", "origin"], dest)
    return verify_checkout(runner, project, dest)


def verify_checkout(runner: CommandRunner, project: Project, path: Path) -> dict:
    head = _git(runner, ["rev-parse", "HEAD"], path).strip()
    status = _git(runner, ["status", "--porcelain", "--untracked-files=all"], path)
    if head != project.base_commit:
        raise GitError(f"{path}: HEAD is {head}, expected {project.base_commit}")
    if status.strip():
        raise GitError(f"{path}: working tree not clean after checkout:\n{status}")
    return {
        "repo_url": project.repo,
        "base_commit": project.base_commit,
        "head": head,
        "git_status_porcelain": status,
        "working_tree_clean": True,
        "checkout_path": str(path),
        "checked_out_at": utc_now(),
    }


@dataclass
class DiffStats:
    changed_files: int
    lines_added: int
    lines_removed: int
    files: list[dict]            # {"path", "status", "added", "removed"}; binary -> None counts
    commits_since_base: int      # commits the agent created on top of base_commit
    excluded_patterns: list[str]


def write_diff(runner: CommandRunner, path: Path, base_commit: str, diff_file: Path,
               exclude: tuple[str, ...]) -> DiffStats:
    """Write `git diff base_commit` of the full working tree (incl. untracked files and
    any commits the tool made) to `diff_file` and return statistics.

    A temporary index is used so the working copy's own index is left untouched.
    `.gitignore` is respected; `exclude` patterns (e.g. build output) are filtered.
    """
    pathspec = ["--", ".", *[f":(glob,exclude){p}" for p in exclude]]
    with tempfile.TemporaryDirectory(prefix="mig-eval-idx-") as tmp:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(tmp) / "index"))

        def g(args: list[str]) -> str:
            code, out = runner.capture(["git", *args], path, env=env)
            if code != 0:
                raise GitError(f"git {' '.join(args)} failed (exit {code}): {out.strip()}")
            return out

        g(["add", "--all", *pathspec])
        diff = g(["diff", "--cached", "--no-color", "--no-ext-diff", base_commit, *pathspec])
        numstat = g(["diff", "--cached", "--numstat", base_commit, *pathspec])
        namestatus = g(["diff", "--cached", "--name-status", base_commit, *pathspec])
    with diff_file.open("x", encoding="utf-8") as fh:
        fh.write(diff)

    statuses = {}
    for line in namestatus.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            statuses[parts[-1]] = parts[0]
    files, added, removed = [], 0, 0
    for line in numstat.splitlines():
        a, r, name = line.split("\t", 2)
        name = name.split(" => ")[-1].rstrip("}") if " => " in name else name
        a_i = None if a == "-" else int(a)
        r_i = None if r == "-" else int(r)
        added += a_i or 0
        removed += r_i or 0
        files.append({"path": name, "status": statuses.get(name, "?"), "added": a_i, "removed": r_i})

    code, out = runner.capture(["git", "rev-list", "--count", f"{base_commit}..HEAD"], path)
    commits = int(out.strip()) if code == 0 and out.strip().isdigit() else 0
    return DiffStats(len(files), added, removed, files, commits, list(exclude))
