"""Orchestration of single runs: baseline validation and migration runs.

Every run:
  1. allocates a new run id and a new results directory (never reused);
  2. creates a fresh, verified checkout of base_commit from the canonical clone;
  3. records the environment (tool versions, JDKs, OS);
  4. (migration only) runs the migration tool;
  5. (migration only) stores the git diff against base_commit *before* evaluation;
  6. runs the common evaluator;
  7. writes metadata.json and result.json.

Failures at any step are recorded in result.json (`harness_error`); nothing is retried.
"""

from __future__ import annotations

import hashlib
import shutil
import traceback
from dataclasses import asdict
from pathlib import Path

from . import METHODS, __version__
from . import environment, evaluator, git, junie, openrewrite
from .config import ExperimentConfig, Project
from .process import CommandRunner, utc_now
from .results import (RESULT_SCHEMA_VERSION, analyse_test_changes, is_full_success, latest_baseline,
                      next_run_id, run_dir, write_json)


class PipelineError(Exception):
    pass


def _allocate(cfg: ExperimentConfig, project: Project, method: str, run_id: int | None,
              dry_run: bool) -> tuple[int, Path, Path, CommandRunner]:
    rid = run_id or next_run_id(cfg.results_dir, cfg.work_dir, project.name, method)
    rdir = run_dir(cfg.results_dir, project.name, method, rid)
    wdir = cfg.work_dir / project.name / method / f"run-{rid:02d}"
    if not dry_run:
        if rdir.exists() or wdir.exists():
            raise PipelineError(f"run {rid} for {project.name}/{method} already exists "
                                f"({rdir if rdir.exists() else wdir}); results are never overwritten")
        rdir.mkdir(parents=True, exist_ok=False)
    runner = CommandRunner(dry_run=dry_run, log_file=None if dry_run else rdir / "commands.log")
    return rid, rdir, wdir, runner


def _snapshot_inputs(cfg: ExperimentConfig, rdir: Path, config_path: Path | None) -> None:
    inputs = rdir / "inputs"
    inputs.mkdir()
    if config_path and config_path.is_file():
        shutil.copy2(config_path, inputs / "experiment.yaml")
    shutil.copy2(cfg.prompt_file, inputs / cfg.prompt_file.name)


def _prompt_info(cfg: ExperimentConfig) -> dict:
    text = cfg.prompt_file.read_text(encoding="utf-8")
    return {"file": str(cfg.prompt_file), "sha256": hashlib.sha256(text.encode()).hexdigest(), "text": text}


# ------------------------------------------------------------------------ baseline

def run_baseline(cfg: ExperimentConfig, project: Project, *, dry_run: bool = False,
                 run_id: int | None = None, config_path: Path | None = None) -> dict:
    """Validate the original project with the baseline (Java 8) JDK."""
    rid, rdir, wdir, runner = _allocate(cfg, project, "baseline", run_id, dry_run)
    print(f"\n=== baseline: {project.name} run-{rid:02d} ({'dry run' if dry_run else rdir}) ===")
    started = utc_now()
    result: dict = {
        "schema_version": RESULT_SCHEMA_VERSION, "harness_version": __version__,
        "project": project.name, "method": "baseline", "run_id": rid, "group": project.group,
        "repo": project.repo, "base_commit": project.base_commit, "started_at": started,
    }
    metadata: dict = {"project": asdict(project), "run_dir": str(rdir), "work_copy": str(wdir)}
    try:
        baseline_jdk, _ = cfg.require_jdks() if not dry_run else (Path(cfg.baseline_jdk or "$JAVA8_HOME"), None)
        if not dry_run:
            _snapshot_inputs(cfg, rdir, config_path)
            metadata["environment"] = environment.collect(runner, cfg, include_junie=False)
        git.ensure_canonical_clone(runner, cfg.work_dir, project)
        metadata["repository"] = git.create_checkout(runner, cfg.work_dir, project, wdir)
        ev = evaluator.evaluate(runner, cfg, wdir, rdir, java_home=baseline_jdk,
                                expected_jdk_major=cfg.source_java, expected_target=cfg.source_java)
        metadata["evaluation"] = ev.to_dict()
        valid = ev.build_success and ev.jdk_ok
        result.update({
            "baseline_valid": valid,
            "build_success": ev.build_success,
            "validation_exit_code": (ev.validation or {}).get("exit_code"),
            "validation_duration_seconds": (ev.validation or {}).get("duration_seconds"),
            "tests_total": ev.tests.total, "tests_passed": ev.tests.passed,
            "tests_failed": ev.tests.failed, "tests_errors": ev.tests.errors,
            "tests_skipped": ev.tests.skipped, "tests_source": ev.tests.source,
            "java_source_level_verified": ev.target_verified,
            "failure_category": None if valid else "baseline_invalid",
            "baseline_failure_reason": None if valid else ev.failure_category,
            "failure_evidence": ev.failure_evidence,
            "stdout_log": str(rdir / "validation.stdout.log"),
            "stderr_log": str(rdir / "validation.stderr.log"),
            "harness_error": None,
        })
    except KeyboardInterrupt:
        result.update({"baseline_valid": False, "failure_category": "interrupted",
                       "harness_error": "interrupted by user (KeyboardInterrupt)"})
        result["finished_at"] = utc_now()
        _finish(rdir, result, metadata, dry_run)
        raise
    except Exception as e:  # recorded, never retried
        result.update({"baseline_valid": False, "failure_category": "harness_error",
                       "harness_error": f"{type(e).__name__}: {e}"})
        metadata["traceback"] = traceback.format_exc()
        print(f"!! harness error: {e}")
    result["finished_at"] = utc_now()
    _finish(rdir, result, metadata, dry_run)
    return result


# ----------------------------------------------------------------------- migration

def run_migration(cfg: ExperimentConfig, project: Project, method: str, *, dry_run: bool = False,
                  run_id: int | None = None, allow_invalid_baseline: bool = False,
                  config_path: Path | None = None) -> dict:
    if method not in METHODS:
        raise PipelineError(f"unknown method '{method}' (supported: {', '.join(METHODS)})")

    baseline = latest_baseline(cfg.results_dir, project.name)
    if not dry_run:
        if baseline is None:
            raise PipelineError(f"no baseline result for {project.name}; run the baseline first "
                                "(run_experiment.py --method baseline)")
        if not baseline.get("baseline_valid") and not allow_invalid_baseline:
            raise PipelineError(f"baseline for {project.name} is invalid "
                                f"({baseline.get('baseline_failure_reason') or baseline.get('failure_category')}, "
                                f"see {baseline.get('_path')}); not migrating. "
                                "Use --allow-invalid-baseline to run anyway (excluded from success rates).")

    rid, rdir, wdir, runner = _allocate(cfg, project, method, run_id, dry_run)
    print(f"\n=== {method}: {project.name} run-{rid:02d} ({'dry run' if dry_run else rdir}) ===")
    is_junie = method == "junie"
    result: dict = {
        "schema_version": RESULT_SCHEMA_VERSION, "harness_version": __version__,
        "project": project.name, "method": method, "run_id": rid, "group": project.group,
        "repo": project.repo, "base_commit": project.base_commit,
        "model": cfg.junie.model if is_junie else None,
        "model_display_name": cfg.junie.model_display_name if is_junie else None,
        "reasoning_effort": cfg.junie.reasoning_effort if is_junie else None,
        "model_verified": None, "effort_verified": None,
        "openrewrite_recipe": None if is_junie else cfg.openrewrite.recipe,
        "openrewrite_version": None if is_junie else cfg.openrewrite.plugin_version,
        "baseline_valid": baseline.get("baseline_valid") if baseline else None,
        "baseline_run": baseline.get("_path") if baseline else None,
        "baseline_tests_total": baseline.get("tests_total") if baseline else None,
        "started_at": utc_now(),
    }
    metadata: dict = {"project": asdict(project), "run_dir": str(rdir), "work_copy": str(wdir)}
    try:
        _, target_jdk = cfg.require_jdks() if not dry_run else (None, Path(cfg.target_jdk or "$JAVA17_HOME"))
        if not dry_run:
            _snapshot_inputs(cfg, rdir, config_path)
            metadata["environment"] = environment.collect(runner, cfg, include_junie=is_junie)
        git.ensure_canonical_clone(runner, cfg.work_dir, project)
        metadata["repository"] = git.create_checkout(runner, cfg.work_dir, project, wdir)

        timeout = cfg.migration_timeout_seconds(project)
        if is_junie:
            metadata["prompt"] = _prompt_info(cfg)
            info = junie.run(runner, cfg, wdir, rdir, timeout)
            sv = info.session_verification
            usage = sv.get("usage_by_model") or {}
            result.update({"model_verified": sv.get("model_verified"),
                           "effort_verified": sv.get("effort_verified"),
                           "configured_model_cost_share": (usage.get(cfg.junie.model) or {}).get("cost_share"),
                           "llm_cost_usd": round(sum(float(u.get("cost") or 0) for u in usage.values()), 4),
                           "agent_reported_completion": info.agent_reported_completion,
                           "junie_version": info.version_before,
                           "junie_version_changed_during_run": info.version_changed_during_run})
        else:
            info = openrewrite.run(runner, cfg, wdir, rdir, timeout)
            result["agent_reported_completion"] = None
        metadata["migration"] = info.to_dict()
        proc = info.process or {}
        result.update({
            "migration_status": info.status,
            "migration_exit_code": proc.get("exit_code"),
            "migration_timed_out": bool(proc.get("timed_out")),
            "migration_timeout_seconds": timeout,
            "migration_duration_seconds": proc.get("duration_seconds"),
        })
        if dry_run:
            evaluator.evaluate(runner, cfg, wdir, rdir, java_home=target_jdk,
                               expected_jdk_major=cfg.target_java, expected_target=cfg.target_java)
            result["dry_run"] = True
            return result

        diff = git.write_diff(runner, wdir, project.base_commit, rdir / "git.diff", cfg.diff_exclude)
        metadata["diff"] = asdict(diff)

        ev = evaluator.evaluate(runner, cfg, wdir, rdir, java_home=target_jdk,
                                expected_jdk_major=cfg.target_java, expected_target=cfg.target_java)
        metadata["evaluation"] = ev.to_dict()

        test_files = [f for f in diff.files if "src/test/" in f["path"]]
        result.update({
            "build_success": ev.build_success,
            "validation_exit_code": (ev.validation or {}).get("exit_code"),
            "validation_duration_seconds": (ev.validation or {}).get("duration_seconds"),
            "tests_total": ev.tests.total, "tests_passed": ev.tests.passed,
            "tests_failed": ev.tests.failed, "tests_errors": ev.tests.errors,
            "tests_skipped": ev.tests.skipped, "tests_source": ev.tests.source,
            # Only meaningful if the build succeeded (a failed build runs no tests).
            "test_count_not_reduced": (ev.tests.total >= result["baseline_tests_total"]
                                       if result["baseline_tests_total"] is not None
                                       and ev.build_success else None),
            "java17_verified": ev.target_verified,
            "java_config_levels": ev.java_target.get("declared_levels"),
            "java_config_source": ev.java_target.get("config_source"),
            "class_major_versions": ev.java_target.get("main_class_majors"),
            "changed_files": diff.changed_files, "lines_added": diff.lines_added,
            "lines_removed": diff.lines_removed, "commits_since_base": diff.commits_since_base,
            "test_files_changed": len(test_files),
            **analyse_test_changes((rdir / "git.diff").read_text(encoding="utf-8", errors="replace")),
            "modified_files": [f["path"] for f in diff.files],
            "evaluation_failure_category": ev.failure_category,
            "failure_evidence": ev.failure_evidence,
            "harness_error": None,
        })
        result["full_success"] = is_full_success(result)
        result["failure_category"] = _final_category(result, info.status, ev.failure_category)
    except KeyboardInterrupt:
        result.update({"failure_category": "interrupted", "full_success": False,
                       "harness_error": "interrupted by user (KeyboardInterrupt)"})
        result["finished_at"] = utc_now()
        _finish(rdir, result, metadata, dry_run)
        raise
    except Exception as e:
        result.update({"failure_category": "harness_error", "full_success": False,
                       "harness_error": f"{type(e).__name__}: {e}"})
        metadata["traceback"] = traceback.format_exc()
        print(f"!! harness error: {e}")
    result["finished_at"] = utc_now()
    _finish(rdir, result, metadata, dry_run)
    return result


def _final_category(result: dict, migration_status: str, eval_category: str | None) -> str | None:
    """Precedence: success -> None; migration-level problems; evaluator classification."""
    if result["full_success"]:
        return None
    if migration_status == "timeout":
        return "timeout"
    if migration_status in ("agent_error", "no_output"):
        return "agent_failure"
    if migration_status in ("tool_failure", "unsupported_build_tool", "harness_error"):
        return migration_status
    if eval_category is None and result.get("test_files_modified"):
        return "tests_modified"
    return eval_category or "unknown"


def _finish(rdir: Path, result: dict, metadata: dict, dry_run: bool) -> None:
    if dry_run:
        print(f"[dry-run] would write {rdir}/result.json and metadata.json")
        return
    metadata["finished_at"] = result["finished_at"]
    write_json(rdir / "metadata.json", metadata)
    write_json(rdir / "result.json", result)
    keys = ("migration_status", "build_success", "tests_total", "tests_failed", "tests_errors",
            "java17_verified", "baseline_valid", "full_success", "failure_category")
    print("    " + ", ".join(f"{k}={result[k]}" for k in keys if k in result))
    print(f"    -> {rdir}/result.json")
