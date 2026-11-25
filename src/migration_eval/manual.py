"""Manual-fix phase: measure the human effort needed after an automated migration.

This is a *separate, secondary* phase. The automated result (result.json) is never
changed; manual work happens on a new copy and is recorded next to the run:

    results/<p>/<method>/run-NN/manual-fix-MM/
        session.json         start record (who, when, timebox, starting commit)
        fixes.jsonl          one line per fix, appended by `manual_fix.py note`
        manual.diff          what the human changed (relative to the migrated state)
        validation.*         common evaluator on the fixed project (JDK 17)
        manual-fix.json      final record (written once, by `finish`)
    work/<p>/manual/<method>-run-NN-fix-MM/   the copy the human edits

The copy starts from the migrated working copy of the run. Its state is committed
once ("migrated state") so the manual diff contains only the human's changes.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from . import evaluator, git
from .config import ExperimentConfig
from .process import CommandRunner, utc_now
from .results import analyse_test_changes, is_full_success, load_json, write_json

_SESSION_RE = re.compile(r"^manual-fix-(\d+)$")
FIX_DEFINITION = ("one fix = one logically distinct change that addresses one failure cause "
                  "(e.g. bumping one plugin version, adapting one API usage even if it spans "
                  "several files)")
_GIT_ID = ["-c", "user.name=migration-eval", "-c", "user.email=migration-eval@localhost",
           "-c", "commit.gpgsign=false"]


class ManualFixError(Exception):
    pass


def sessions(run: Path) -> list[Path]:
    return sorted((d for d in run.iterdir() if d.is_dir() and _SESSION_RE.match(d.name)),
                  key=lambda d: int(_SESSION_RE.match(d.name).group(1)))  # type: ignore[union-attr]


def start(cfg: ExperimentConfig, run: Path, *, fixer: str, timebox_minutes: float,
          force: bool = False) -> Path:
    run = run.resolve()
    result = load_json(run / "result.json")
    result["_path"] = str(run)
    meta = load_json(run / "metadata.json")
    if is_full_success(result) and not force:
        raise ManualFixError(f"{run} is already a full success (manual effort = 0); "
                             "use --force to review it anyway")
    if result.get("harness_error"):
        raise ManualFixError(f"{run} has a harness error; it is not a valid migration result")
    open_sessions = [s for s in sessions(run) if not (s / "manual-fix.json").exists()]
    if open_sessions:
        raise ManualFixError(f"unfinished session exists: {open_sessions[-1]} (finish it first)")

    src = Path(meta["work_copy"])
    if not src.is_dir():
        raise ManualFixError(f"migrated working copy not found: {src}")
    n = len(sessions(run)) + 1
    sdir = run / f"manual-fix-{n:02d}"
    dest = cfg.work_dir / result["project"] / "manual" / f"{result['method']}-{run.name}-fix-{n:02d}"
    if dest.exists():
        raise ManualFixError(f"refusing to reuse existing directory {dest}")
    sdir.mkdir()
    runner = CommandRunner(log_file=sdir / "commands.log")

    with tempfile.TemporaryDirectory(prefix="mig-eval-check-") as tmp:
        git.write_diff(runner, src, result["base_commit"], Path(tmp) / "now.diff", cfg.diff_exclude)
        now = (Path(tmp) / "now.diff").read_text(encoding="utf-8", errors="replace")
    recorded = (run / "git.diff").read_text(encoding="utf-8", errors="replace")
    matches = now == recorded

    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dest, symlinks=True)
    pathspec = ["--", ".", *[f":(glob,exclude){p}" for p in cfg.diff_exclude]]
    for args in (["add", "--all", *pathspec],
                 [*_GIT_ID, "commit", "--quiet", "--no-verify", "--allow-empty",
                  "-m", f"migrated state ({result['method']} {run.name})"]):
        code, out = runner.capture(["git", *args], dest)
        if code != 0:
            raise ManualFixError(f"git {' '.join(args)} failed: {out.strip()}")
    _, head = runner.capture(["git", "rev-parse", "HEAD"], dest)

    write_json(sdir / "session.json", {
        "project": result["project"], "method": result["method"], "run_id": result["run_id"],
        "run_dir": str(run), "work_copy": str(dest), "migrated_commit": head.strip(),
        "source_work_copy": str(src), "source_matches_recorded_diff": matches,
        "fixer": fixer, "timebox_minutes": timebox_minutes, "started_at": utc_now(),
        "fix_definition": FIX_DEFINITION,
    })
    (sdir / "fixes.jsonl").touch(exist_ok=False)
    return sdir


def note(session: Path, text: str, files: list[str] | None = None) -> int:
    if (session / "manual-fix.json").exists():
        raise ManualFixError(f"{session} is already finished")
    with (session / "fixes.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"at": utc_now(), "description": text, "files": files or []}) + "\n")
    return len(_fixes(session))


def _fixes(session: Path) -> list[dict]:
    p = session / "fixes.jsonl"
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()] if p.exists() else []


def finish(cfg: ExperimentConfig, session: Path, *, active_minutes: float | None,
           gave_up: bool, notes: str | None, fixes_override: int | None = None) -> dict:
    session = session.resolve()
    if (session / "manual-fix.json").exists():
        raise ManualFixError(f"{session} is already finished")
    s = load_json(session / "session.json")
    result = load_json(Path(s["run_dir"]) / "result.json")
    repo = Path(s["work_copy"])
    _, target_jdk = cfg.require_jdks()
    runner = CommandRunner(log_file=session / "commands.log")
    ended = utc_now()
    wall = (datetime.fromisoformat(ended) - datetime.fromisoformat(s["started_at"])).total_seconds() / 60

    diff = git.write_diff(runner, repo, s["migrated_commit"], session / "manual.diff", cfg.diff_exclude)
    hunks = sum(1 for l in (session / "manual.diff").read_text(encoding="utf-8", errors="replace").splitlines()
                if l.startswith("@@"))
    ev = evaluator.evaluate(runner, cfg, repo, session, java_home=target_jdk,
                            expected_jdk_major=cfg.target_java, expected_target=cfg.target_java)
    fixes = _fixes(session)
    with tempfile.TemporaryDirectory(prefix="mig-eval-base-") as tmp:
        git.write_diff(runner, repo, result["base_commit"], Path(tmp) / "vs-base.diff", cfg.diff_exclude)
        test_changes = analyse_test_changes((Path(tmp) / "vs-base.diff").read_text(
            encoding="utf-8", errors="replace"))
    r = {
        "build_success": ev.build_success, "tests_failed": ev.tests.failed,
        "tests_errors": ev.tests.errors, "java17_verified": ev.target_verified,
        **test_changes,
    }
    success = is_full_success(r)
    base_tests = result.get("baseline_tests_total")
    active = round(active_minutes if active_minutes is not None else wall, 1)
    test_files = [f for f in diff.files if "src/test/" in f["path"]]
    record = {
        **{k: s[k] for k in ("project", "method", "run_id", "fixer", "timebox_minutes",
                             "started_at", "migrated_commit", "source_matches_recorded_diff",
                             "fix_definition")},
        "finished_at": ended,
        "wall_minutes": round(wall, 1),
        "active_minutes": active,
        "active_minutes_source": "reported by fixer" if active_minutes is not None else "wall clock",
        "timebox_exceeded": active > s["timebox_minutes"],
        "gave_up": gave_up,
        "manual_fixes": fixes_override if fixes_override is not None else len(fixes),
        "manual_fixes_source": "--fixes" if fixes_override is not None else "fixes.jsonl",
        "fixes_logged": fixes,
        "manual_changed_files": diff.changed_files, "manual_lines_added": diff.lines_added,
        "manual_lines_removed": diff.lines_removed, "manual_diff_hunks": hunks,
        "manual_test_files_changed": len(test_files),
        "manual_test_files_deleted": sum(1 for f in test_files if f["status"].startswith("D")),
        "manual_modified_files": [f["path"] for f in diff.files],
        "build_success": ev.build_success, "tests_total": ev.tests.total,
        "tests_failed": ev.tests.failed, "tests_errors": ev.tests.errors,
        "java17_verified": ev.target_verified,
        **{f"vs_base_{k}": v for k, v in test_changes.items()},
        "success_after_manual": success and not gave_up,
        "strict_success_after_manual": success and not gave_up and base_tests is not None
                                        and ev.tests.total >= base_tests,
        "evaluation_failure_category": None if success else ev.failure_category,
        "automated_migration_seconds": result.get("migration_duration_seconds"),
        "notes": notes,
    }
    write_json(session / "evaluation.json", ev.to_dict())
    write_json(session / "manual-diff-stats.json", asdict(diff))
    write_json(session / "manual-fix.json", record)
    return record


def latest_finished(run: Path) -> dict | None:
    if not run.is_dir():
        return None
    for s in reversed(sessions(run)):
        p = s / "manual-fix.json"
        if p.is_file():
            data = load_json(p)
            data["_path"] = str(s)
            return data
    return None
