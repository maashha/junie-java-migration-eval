#!/usr/bin/env python3
"""Clone projects, check out base_commit, verify, and record repository metadata.

    python scripts/prepare.py                       # all projects
    python scripts/prepare.py -p tinify-java --validate-baseline
    python scripts/prepare.py --dry-run

Creates work/<project>/canonical.git (bare, never checked out) and
work/<project>/original (pristine reference checkout, never built or modified).
Per-run copies for junie/openrewrite/baseline are created fresh for every run by
run_experiment.py, so each run starts from exactly base_commit.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from _common import add_common_args, load

from migration_eval import git, pipeline
from migration_eval.process import CommandRunner
from migration_eval.results import write_json


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_common_args(ap)
    ap.add_argument("--validate-baseline", action="store_true",
                    help="also validate the original project (mvn build/tests under the baseline JDK)")
    args = ap.parse_args()
    cfg, projects = load(args)
    runner = CommandRunner(dry_run=args.dry_run)
    failures = 0
    for project in projects:
        print(f"\n=== prepare {project.name} @ {project.base_commit[:12]} ===")
        try:
            git.ensure_canonical_clone(runner, cfg.work_dir, project)
            original = git.project_dir(cfg.work_dir, project) / "original"
            if args.dry_run:
                git.create_checkout(runner, cfg.work_dir, project, original)
                continue
            if original.exists():
                meta = git.verify_checkout(runner, project, original)   # must still be pristine
                print(f"    original checkout exists and is clean: {original}")
            else:
                meta = git.create_checkout(runner, cfg.work_dir, project, original)
                print(f"    created {original}")
            meta.update({"name": project.name, "group": project.group,
                         "canonical_clone": str(git.canonical_dir(cfg.work_dir, project))})
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            out = cfg.results_dir / project.name / "prepare" / f"prepare-{stamp}.json"
            out.parent.mkdir(parents=True, exist_ok=True)
            write_json(out, meta)
            print(f"    metadata -> {out}")
        except Exception as e:  # recorded and reported; continue with other projects
            failures += 1
            print(f"!! {project.name}: {e}", file=sys.stderr)
            continue
        if args.validate_baseline:
            r = pipeline.run_baseline(cfg, project, config_path=args.config)
            failures += 0 if r.get("baseline_valid") else 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
