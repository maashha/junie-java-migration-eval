"""Aggregate result.json files into side-by-side CSV/Markdown summaries."""

from __future__ import annotations

import csv
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from . import METHODS
from .manual import latest_finished
from .results import is_full_success, is_strict_success, iter_results, latest_baseline, tests_pass

RUN_COLUMNS = [
    "project", "group", "method", "run_id", "base_commit", "model", "reasoning_effort",
    "model_verified", "effort_verified", "baseline_valid", "migration_status",
    "migration_duration_seconds", "agent_reported_completion", "build_success", "tests_total",
    "baseline_tests_total", "tests_passed", "tests_failed", "tests_errors", "tests_skipped",
    "java17_verified", "changed_files", "lines_added", "lines_removed", "test_files_changed",
    "test_files_modified", "test_files_added", "test_files_deleted", "test_files_whitespace_only",
    "test_classes_added", "modified_test_files", "added_test_classes", "full_success", "strict_success", "failure_category",
    "llm_cost_usd", "manual_fix_needed", "time_to_success_seconds", "harness_error", "_path",
]


def attach_manual(r: dict) -> None:
    auto = is_full_success(r)
    rec = None if auto else latest_finished(Path(r["_path"]))
    mig = r.get("migration_duration_seconds") or 0
    r["llm_cost_usd"] = r.get("llm_cost_usd") if r.get("method") == "junie" else 0.0
    r["manual_fix_needed"] = not auto
    r["manual_session_done"] = auto or rec is not None
    r["manual_fixes"] = 0 if auto else (rec or {}).get("manual_fixes")
    r["manual_active_minutes"] = 0 if auto else (rec or {}).get("active_minutes")
    r["manual_changed_lines"] = 0 if auto else ((rec["manual_lines_added"] + rec["manual_lines_removed"])
                                                if rec else None)
    r["manual_gave_up"] = None if auto else (rec or {}).get("gave_up")
    r["success_after_manual"] = True if auto else ((rec or {}).get("success_after_manual") if rec else None)
    r["time_to_success_seconds"] = (round(mig, 1) if auto else
                                    round(mig + rec["active_minutes"] * 60, 1)
                                    if rec and rec.get("success_after_manual") else None)


def _median(xs: list[float]) -> float | None:
    return round(statistics.median(xs), 1) if xs else None


def _mean(xs: list[float]) -> float | None:
    return round(statistics.fmean(xs), 1) if xs else None


def method_stats(runs: list[dict], baselines: dict[str, dict | None],
                 hourly_rate: float | None = None) -> dict:
    projects = sorted({r["project"] for r in runs})
    valid_projects = [p for p in projects if (baselines.get(p) or {}).get("baseline_valid")]
    # Success rates use only runs on projects with a valid baseline and no harness error.
    eligible = [r for r in runs if r["project"] in valid_projects and not r.get("harness_error")]
    durations = [r["migration_duration_seconds"] for r in eligible
                 if r.get("migration_duration_seconds") is not None]
    changed = [r["changed_files"] for r in eligible if r.get("changed_files") is not None]
    cats = Counter(r.get("failure_category") or "none (success)" for r in eligible)
    n = len(eligible)
    needed = [r for r in eligible if r["manual_fix_needed"]]
    done = [r for r in needed if r["manual_session_done"]]
    fixes = [r["manual_fixes"] for r in done if r["manual_fixes"] is not None]
    minutes = [r["manual_active_minutes"] for r in done if r["manual_active_minutes"] is not None]
    lines = [r["manual_changed_lines"] for r in done if r["manual_changed_lines"] is not None]
    tts = [r["time_to_success_seconds"] for r in eligible if r["time_to_success_seconds"] is not None]
    costs = [float(r["llm_cost_usd"]) for r in eligible if r.get("llm_cost_usd") is not None]
    n_full = sum(1 for r in eligible if is_full_success(r))

    def cnt(pred) -> str:
        k = sum(1 for r in eligible if pred(r))
        return f"{k}/{n}" + (f" ({100 * k / n:.0f}%)" if n else "")

    return {
        "projects_attempted": len(projects),
        "valid_baseline_projects": len(valid_projects),
        "runs_total": len(runs),
        "runs_eligible": n,
        "runs_harness_error": sum(1 for r in runs if r.get("harness_error")),
        "runs_invalid_baseline_excluded": sum(1 for r in runs if r["project"] not in valid_projects),
        "full_success": cnt(is_full_success),
        "strict_success": cnt(is_strict_success),
        "build_success": cnt(lambda r: bool(r.get("build_success"))),
        "tests_pass": cnt(tests_pass),
        "java17_verified": cnt(lambda r: bool(r.get("java17_verified"))),
        # recomputed from raw fields; only builds that actually ran the tests count
        "tests_modified": cnt(lambda r: bool(r.get("test_files_modified"))),
        "test_count_reduced": cnt(lambda r: bool(r.get("build_success"))
                                  and r.get("baseline_tests_total") is not None
                                  and (r.get("tests_total") or 0) < r["baseline_tests_total"]),
        "median_migration_seconds": _median(durations),
        "mean_changed_files": _mean(changed),
        "median_changed_files": _median(changed),
        "manual_fix_needed": cnt(lambda r: r["manual_fix_needed"]),
        "manual_fix_sessions_done": f"{sum(1 for r in needed if r['manual_session_done'])}/{len(needed)}",
        "success_after_manual": cnt(lambda r: r["success_after_manual"] is True),
        "manual_gave_up": sum(1 for r in needed if r["manual_gave_up"]),
        "manual_fixes_total": sum(fixes),
        "manual_fixes_median_per_fixed_run": _median(fixes),
        "manual_minutes_total": round(sum(minutes), 1),
        "manual_minutes_median_per_fixed_run": _median(minutes),
        "manual_changed_lines_median": _median(lines),
        "time_to_success_median_seconds": _median(tts),
        "time_to_success_mean_seconds": _mean(tts),
        "llm_cost_usd_total": round(sum(costs), 2),
        "llm_cost_usd_median_per_run": _median(costs) if costs else None,
        "llm_cost_usd_per_full_success": (round(sum(costs) / n_full, 3) if n_full else None),
        **({"total_cost_usd_incl_manual": round(sum(costs) + sum(minutes) / 60 * hourly_rate, 2)}
           if hourly_rate is not None else {}),
        "failure_categories": dict(sorted(cats.items())),
    }


def summarize(results_dir: Path, out_root: Path | None = None,
              hourly_rate: float | None = None) -> Path:
    runs = [r for r in iter_results(results_dir) if r.get("method") in METHODS]
    projects = sorted({r["project"] for r in iter_results(results_dir)})
    baselines = {p: latest_baseline(results_dir, p) for p in projects}

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = (out_root or results_dir / "_summaries") / stamp
    out.mkdir(parents=True, exist_ok=False)

    by_method: dict[str, list[dict]] = defaultdict(list)
    for r in runs:
        r["full_success"] = is_full_success(r)       # recomputed from raw fields
        r["strict_success"] = is_strict_success(r)
        if not r["full_success"] and r.get("failure_category") is None and r.get("test_files_modified"):
            r["failure_category"] = "tests_modified"
        attach_manual(r)
        by_method[r["method"]].append(r)
    stats = {m: method_stats(by_method.get(m, []), baselines, hourly_rate) for m in METHODS}

    with (out / "runs.csv").open("x", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=RUN_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in sorted(runs, key=lambda r: (r["project"], r["method"], r["run_id"])):
            w.writerow(r)

    metric_rows = [k for k in stats[METHODS[0]] if k != "failure_categories"]
    with (out / "summary.csv").open("x", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["metric", *METHODS])
        for k in metric_rows:
            w.writerow([k, *[stats[m][k] for m in METHODS]])
        all_cats = sorted({c for m in METHODS for c in stats[m]["failure_categories"]})
        for c in all_cats:
            w.writerow([f"failure_category:{c}", *[stats[m]["failure_categories"].get(c, 0) for m in METHODS]])

    md = [
          "## Baselines", "", "| project | baseline valid | tests | reason |", "|---|---|---|---|"]
    for p in projects:
        b = baselines[p]
        md.append(f"| {p} | {b.get('baseline_valid') if b else 'not run'} | "
                  f"{b.get('tests_total') if b else ''} | "
                  f"{(b.get('baseline_failure_reason') or b.get('harness_error') or '') if b else ''} |")
    incomplete = sorted(str(d.relative_to(results_dir)) for d in results_dir.glob("*/*/run-*")
                        if d.is_dir() and not (d / "result.json").exists())
    if incomplete:
        md += ["", "## Incomplete runs", "", "| run |", "|---|", *[f"| {d} |" for d in incomplete]]
    md += ["", "## Junie vs OpenRewrite", "", "| metric | " + " | ".join(METHODS) + " |",
           "|---|" + "---|" * len(METHODS)]
    for k in metric_rows:
        md.append(f"| {k} | " + " | ".join(str(stats[m][k]) for m in METHODS) + " |")
    md += ["", "### Failure categories (eligible runs)", "",
           "| category | " + " | ".join(METHODS) + " |", "|---|" + "---|" * len(METHODS)]
    for c in sorted({c for m in METHODS for c in stats[m]["failure_categories"]}):
        md.append(f"| {c} | " + " | ".join(str(stats[m]["failure_categories"].get(c, 0)) for m in METHODS) + " |")
    md += ["", "## Per run", "",
           "| project | method | run | status | build | tests (fail/err/total) | java17 | files | full success | category "
           "| cost $ | time to success s |",
           "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in sorted(runs, key=lambda r: (r["project"], r["method"], r["run_id"])):
        md.append(f"| {r['project']} | {r['method']} | {r['run_id']} | {r.get('migration_status')} | "
                  f"{r.get('build_success')} | {r.get('tests_failed')}/{r.get('tests_errors')}/{r.get('tests_total')} | "
                  f"{r.get('java17_verified')} | {r.get('changed_files')} | {r['full_success']} | "
                  f"{r.get('failure_category')} | {r.get('llm_cost_usd')} | "
                  f"{r['time_to_success_seconds']} |")
    with (out / "summary.md").open("x", encoding="utf-8") as fh:
        fh.write("\n".join(md) + "\n")
    return out
