#!/usr/bin/env python3
"""Top-level experiment runner (sequential; never runs Junie migrations concurrently).

    python scripts/run_experiment.py -p tinify-java --method baseline
    python scripts/run_experiment.py -p tinify-java --method openrewrite
    python scripts/run_experiment.py -p tinify-java --method junie
    python scripts/run_experiment.py -p tinify-java --method all          # baseline -> openrewrite -> junie
    python scripts/run_experiment.py -p all --method all --dry-run

For every (project, method) it performs `experiment.repetitions` independent runs
(override with --repetitions), each from a fresh checkout of base_commit.
A baseline is run automatically if the project has none yet. Projects whose
baseline is invalid are skipped (unless --allow-invalid-baseline).
"""

from __future__ import annotations

import argparse
import sys

from _common import add_common_args, load

from migration_eval import pipeline
from migration_eval.results import latest_baseline


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_common_args(ap, project_required=True)
    ap.add_argument("--method", "-m", required=True, choices=["baseline", "junie", "openrewrite", "all"])
    ap.add_argument("--repetitions", "-n", type=int, help="override experiment.repetitions")
    ap.add_argument("--rebaseline", action="store_true", help="run a new baseline even if one exists")
    ap.add_argument("--allow-invalid-baseline", action="store_true")
    ap.add_argument("--yes", "-y", action="store_true",
                    help="do not ask for confirmation before more than one Junie run")
    args = ap.parse_args()
    cfg, projects = load(args)
    reps = args.repetitions or cfg.repetitions
    if reps < 1:
        sys.exit("--repetitions must be >= 1")

    methods = ["openrewrite", "junie"] if args.method == "all" else (
        [] if args.method == "baseline" else [args.method])
    junie_runs = reps * len(projects) if "junie" in methods else 0
    print(f"Plan: {len(projects)} project(s) x {methods or ['baseline only']} x {reps} repetition(s); "
          f"{junie_runs} Junie run(s) with model={cfg.junie.model} effort={cfg.junie.reasoning_effort}"
          + (" [DRY RUN]" if args.dry_run else ""))
    if junie_runs > 1 and not args.yes and not args.dry_run:
        if not sys.stdin.isatty():
            sys.exit("refusing to start several Junie runs non-interactively without --yes")
        if input(f"Start {junie_runs} Junie runs (each may take up to "
                 f"{cfg.timeout_minutes:g} min and consume credits)? [y/N] ").strip().lower() != "y":
            return 1

    rc = 0
    for project in projects:
        baseline = None if args.dry_run else latest_baseline(cfg.results_dir, project.name)
        if args.method == "baseline" or args.rebaseline or baseline is None:
            baseline = pipeline.run_baseline(cfg, project, dry_run=args.dry_run, config_path=args.config)
        if not args.dry_run and not baseline.get("baseline_valid") and not args.allow_invalid_baseline:
            if methods:
                print(f"!! {project.name}: baseline invalid "
                      f"({baseline.get('baseline_failure_reason') or baseline.get('failure_category')}); "
                      f"skipping migrations", file=sys.stderr)
            rc = 1
            continue
        for method in methods:
            for _ in range(reps):
                try:
                    pipeline.run_migration(cfg, project, method, dry_run=args.dry_run,
                                           allow_invalid_baseline=args.allow_invalid_baseline,
                                           config_path=args.config)
                except pipeline.PipelineError as e:
                    print(f"!! {project.name}/{method}: {e}", file=sys.stderr)
                    rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
