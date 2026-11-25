#!/usr/bin/env python3
"""Re-run the common evaluator on an existing run's working copy (for inspection).

    python scripts/evaluate.py --run results/tinify-java/junie/run-01
    python scripts/evaluate.py --path /some/checkout --out /tmp/eval-out

Migration runs are evaluated automatically by run_experiment.py; use this script to
re-check a result (e.g. after a tool/JDK change) without touching the original
result. Output goes to a new timestamped directory `<run>/re-evaluation-<UTC>/`.
The working copy's sources are not modified (Maven rebuilds target/ only).
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from _common import ROOT

from migration_eval import evaluator
from migration_eval.config import ConfigError, load_experiment
from migration_eval.process import CommandRunner
from migration_eval.results import load_json, write_json


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--run", type=Path, help="results/<project>/<method>/run-NN directory")
    src.add_argument("--path", type=Path, help="arbitrary Maven project directory")
    ap.add_argument("--out", type=Path, help="output directory (with --path; must not exist)")
    ap.add_argument("--jdk", choices=["target", "baseline"], default="target")
    ap.add_argument("--config", type=Path, default=ROOT / "config" / "experiment.yaml")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    try:
        cfg = load_experiment(args.config, ROOT)
        baseline_jdk, target_jdk = cfg.require_jdks()
    except ConfigError as e:
        sys.exit(f"configuration error: {e}")

    if args.run:
        repo = Path(load_json(args.run / "metadata.json")["work_copy"])
        out = args.run / f"re-evaluation-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    else:
        if not args.out:
            sys.exit("--out is required with --path")
        repo, out = args.path.resolve(), args.out.resolve()
    if not repo.is_dir():
        sys.exit(f"working copy not found: {repo}")
    if not args.dry_run:
        out.mkdir(parents=True, exist_ok=False)

    level = cfg.target_java if args.jdk == "target" else cfg.source_java
    runner = CommandRunner(dry_run=args.dry_run, log_file=None if args.dry_run else out / "commands.log")
    ev = evaluator.evaluate(runner, cfg, repo, out,
                            java_home=target_jdk if args.jdk == "target" else baseline_jdk,
                            expected_jdk_major=level, expected_target=level)
    if not args.dry_run:
        write_json(out / "evaluation.json", ev.to_dict())
        print(f"build_success={ev.build_success} tests={ev.tests.total} failed={ev.tests.failed} "
              f"errors={ev.tests.errors} target_verified={ev.target_verified} "
              f"category={ev.failure_category}\n-> {out}/evaluation.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
