#!/usr/bin/env python3
"""Look up MigrationBench entries (base_commit etc.) and print projects.yaml snippets.

    python scripts/lookup_migrationbench.py airlift/airline tinify/tinify-java
    python scripts/lookup_migrationbench.py --dataset migration-bench-java-full owner/repo

Uses the public Hugging Face datasets-server API (read-only). Copy the printed
snippet into config/projects.yaml; nothing is written automatically.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request

API = "https://datasets-server.huggingface.co/filter"


def lookup(dataset: str, repo: str) -> list[dict]:
    q = urllib.parse.urlencode({
        "dataset": f"AmazonScience/{dataset}", "config": "default", "split": "test",
        "where": f"\"repo\"='{repo.replace(chr(39), '')}'",
    })
    with urllib.request.urlopen(f"{API}?{q}", timeout=30) as resp:
        return [r["row"] for r in json.load(resp).get("rows", [])]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("repos", nargs="+", help="owner/name as in MigrationBench's `repo` column")
    ap.add_argument("--dataset", default="migration-bench-java-selected",
                    choices=["migration-bench-java-selected", "migration-bench-java-full",
                             "migration-bench-java-utg"])
    args = ap.parse_args()
    rc = 0
    for repo in args.repos:
        try:
            rows = lookup(args.dataset, repo)
        except Exception as e:
            print(f"# {repo}: lookup failed: {e}", file=sys.stderr)
            rc = 1
            continue
        if not rows:
            print(f"# {repo}: not found in {args.dataset}", file=sys.stderr)
            rc = 1
            continue
        r = rows[0]
        loc = r.get("num_loc") or 0
        group = "small" if loc < 3000 else "medium" if loc < 20000 else "large"
        owner, name = repo.split("/", 1)
        short = name if name.startswith(owner) else f"{owner}-{name}"
        print(f"  - name: {short}\n"
              f"    repo: https://github.com/{repo}.git\n"
              f"    base_commit: \"{r['base_commit']}\"\n"
              f"    group: {group}          # {args.dataset}: {r.get('num_java_files')} java files, "
              f"{loc} LOC, {r.get('num_test_cases')} test cases (group is a heuristic, review it)")
    return rc


if __name__ == "__main__":
    sys.exit(main())
