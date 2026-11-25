#!/usr/bin/env python3
"""Measure manual effort after a failed automated migration.

    # 1. start: copies the migrated project, starts the clock
    python scripts/manual_fix.py start --run results/apollo/openrewrite/run-01

    # 2. fix the project in the printed directory (IDE, terminal, ...). After each fix:
    python scripts/manual_fix.py note --session results/apollo/openrewrite/run-01/manual-fix-01 \
        "bump maven-checkstyle-plugin 3.1.0 -> 3.3.1"

    # 3. finish: stops the clock, diffs, runs the common evaluator (JDK 17)
    python scripts/manual_fix.py finish --session results/apollo/openrewrite/run-01/manual-fix-01 \
        --active-minutes 25            # time actually spent (omit -> wall clock)
        [--gave-up] [--notes "..."]

    # list runs that still need a manual-fix session
    python scripts/manual_fix.py todo

Rules: same evaluator as the automated phase; do not delete, disable or skip tests;
stop at the timebox (default 60 min) and finish with --gave-up if not fixed.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from _common import ROOT

from migration_eval import manual
from migration_eval.config import ConfigError, load_experiment
from migration_eval.results import is_full_success, iter_results


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, default=ROOT / "config" / "experiment.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start")
    s.add_argument("--run", type=Path, required=True)
    s.add_argument("--fixer", default="author")
    s.add_argument("--timebox-minutes", type=float, default=60)
    s.add_argument("--force", action="store_true", help="allow a session on a successful run")
    n = sub.add_parser("note")
    n.add_argument("--session", type=Path, required=True)
    n.add_argument("text")
    n.add_argument("--files", nargs="*")
    f = sub.add_parser("finish")
    f.add_argument("--session", type=Path, required=True)
    f.add_argument("--active-minutes", type=float)
    f.add_argument("--fixes", type=int, help="override the number of fixes (default: logged notes)")
    f.add_argument("--gave-up", action="store_true")
    f.add_argument("--notes")
    sub.add_parser("todo")
    args = ap.parse_args()

    try:
        cfg = load_experiment(args.config, ROOT)
        if args.cmd == "start":
            sdir = manual.start(cfg, args.run, fixer=args.fixer, timebox_minutes=args.timebox_minutes,
                                force=args.force)
            ses = manual.load_json(sdir / "session.json")
            if not ses["source_matches_recorded_diff"]:
                print("warning: the migrated copy differs from the recorded git.diff "
                      "(recorded in session.json)", file=sys.stderr)
            print(f"\nclock started {ses['started_at']} (timebox {args.timebox_minutes:g} min)\n"
                  f"edit:    {ses['work_copy']}\n"
                  f"note:    python scripts/manual_fix.py note --session {sdir} \"what you fixed\"\n"
                  f"finish:  python scripts/manual_fix.py finish --session {sdir} --active-minutes N")
        elif args.cmd == "note":
            k = manual.note(args.session, args.text, args.files)
            print(f"fix #{k} recorded")
        elif args.cmd == "finish":
            r = manual.finish(cfg, args.session, active_minutes=args.active_minutes,
                              gave_up=args.gave_up, notes=args.notes, fixes_override=args.fixes)
            print(f"success_after_manual={r['success_after_manual']} fixes={r['manual_fixes']} "
                  f"active_minutes={r['active_minutes']} files={r['manual_changed_files']} "
                  f"lines=+{r['manual_lines_added']}/-{r['manual_lines_removed']} "
                  f"tests={r['tests_failed']}/{r['tests_errors']}/{r['tests_total']} "
                  f"java17={r['java17_verified']}\n-> {args.session}/manual-fix.json")
        else:
            for r in iter_results(cfg.results_dir):
                if r.get("method") in ("junie", "openrewrite") and not is_full_success(r) \
                        and not r.get("harness_error") and manual.latest_finished(Path(r["_path"])) is None:
                    print(f"{r['_path']}   ({r.get('failure_category')})")
    except (manual.ManualFixError, ConfigError, OSError) as e:
        sys.exit(f"error: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
