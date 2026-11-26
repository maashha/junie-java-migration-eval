#!/usr/bin/env python3
"""Summarize all result.json files into CSV + Markdown (Junie vs OpenRewrite side by side).

    python scripts/summarize.py
    python scripts/summarize.py --results-dir results

Writes results/_summaries/<UTC timestamp>/{summary.md, summary.csv, runs.csv};
earlier summaries are never overwritten.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from _common import ROOT

from migration_eval.config import load_experiment
from migration_eval.summary import summarize


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results-dir", type=Path)
    ap.add_argument("--config", type=Path, default=ROOT / "config" / "experiment.yaml")
    args = ap.parse_args()
    results_dir = args.results_dir or load_experiment(args.config, ROOT).results_dir
    if not results_dir.is_dir():
        sys.exit(f"results directory not found: {results_dir}")
    out = summarize(results_dir.resolve())
    print((out / "summary.md").read_text(encoding="utf-8"))
    print(f"-> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
