#!/usr/bin/env python3
"""Run ONE Junie migration per project (fresh copy of base_commit) and evaluate it.

    python scripts/run_junie.py -p tinify-java
    python scripts/run_junie.py -p tinify-java --dry-run

Model, reasoning effort and prompt come from config/experiment.yaml and are the
same for every project. Requires a valid baseline (see run_experiment.py).
Failed runs are recorded, never retried.
"""

from __future__ import annotations

import argparse
import sys

from _common import add_common_args, load

from migration_eval import pipeline


def main(method: str = "junie") -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_common_args(ap, project_required=True)
    ap.add_argument("--run-id", type=int, help="explicit run id (default: next free); must not exist")
    ap.add_argument("--allow-invalid-baseline", action="store_true")
    args = ap.parse_args()
    cfg, projects = load(args)
    if args.run_id is not None and len(projects) > 1:
        sys.exit("--run-id can only be used with a single project")
    rc = 0
    for project in projects:
        try:
            pipeline.run_migration(cfg, project, method, dry_run=args.dry_run, run_id=args.run_id,
                                   allow_invalid_baseline=args.allow_invalid_baseline,
                                   config_path=args.config)
        except pipeline.PipelineError as e:
            print(f"!! {project.name}: {e}", file=sys.stderr)
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
