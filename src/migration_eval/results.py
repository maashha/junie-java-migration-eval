"""Result layout, schema helpers and the success definition.

    results/<project>/baseline/run-NN/      validation of the original Java 8 project
    results/<project>/<method>/run-NN/      one migration run (method: junie | openrewrite)

Run directories are created exclusively and files are opened with mode "x": an
existing result is never overwritten.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

RESULT_SCHEMA_VERSION = 1
_RUN_RE = re.compile(r"^run-(\d+)$")


def run_dir(results_dir: Path, project: str, method: str, run_id: int) -> Path:
    return results_dir / project / method / f"run-{run_id:02d}"


def existing_run_ids(*parents: Path) -> set[int]:
    ids: set[int] = set()
    for parent in parents:
        if parent.is_dir():
            for d in parent.iterdir():
                m = _RUN_RE.match(d.name)
                if m:
                    ids.add(int(m.group(1)))
    return ids


def next_run_id(results_dir: Path, work_dir: Path, project: str, method: str) -> int:
    """Next free id, considering both results and work copies (both must be new)."""
    used = existing_run_ids(results_dir / project / method, work_dir / project / method)
    return max(used, default=0) + 1


def write_json(path: Path, data: Any) -> None:
    with path.open("x", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=False, default=str)
        fh.write("\n")


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def iter_results(results_dir: Path) -> Iterable[dict]:
    for p in sorted(results_dir.glob("*/*/run-*/result.json")):
        try:
            data = load_json(p)
        except (json.JSONDecodeError, OSError):
            continue
        data["_path"] = str(p.parent)
        yield data


def latest_baseline(results_dir: Path, project: str) -> dict | None:
    """Most recent completed baseline result for `project` (highest run id)."""
    base = results_dir / project / "baseline"
    for rid in sorted(existing_run_ids(base), reverse=True):
        p = run_dir(results_dir, project, "baseline", rid) / "result.json"
        if p.is_file():
            data = load_json(p)
            data["_path"] = str(p.parent)
            return data
    return None


# ------------------------------------------------------------------ definitions

FULL_SUCCESS_DEFINITION = (
    "full_success := build_success AND tests_failed == 0 AND tests_errors == 0 "
    "AND java17_verified AND test_files_modified == 0  (evaluated with JDK 17 by the common "
    "evaluator; test_files_modified = files under src/test/ that were added, deleted, renamed "
    "or changed in content; changes of whitespace/line endings only and newly added "
    "test classes (*Test/*Tests/*IT/Test*.java) are allowed)"
)
STRICT_SUCCESS_DEFINITION = (
    "strict_success := full_success AND tests_total >= baseline_tests_total "
    "(guards against removing/skipping tests)"
)


TEST_PATH_MARKER = "src/test/"
NEW_TEST_CLASS_RE = re.compile(
    r"src/test/(java|kotlin|groovy)/.*/(Test\w*|\w*(Test|Tests|TestCase|IT))\.(java|kt|groovy)$")


def analyse_test_changes(diff_text: str) -> dict:
    """Classify changed files under src/test/: whitespace-only and new test classes are allowed."""
    files: dict[str, dict] = {}
    cur: dict | None = None
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            path = line.split(" b/", 1)[-1]
            cur = files.setdefault(path, {"path": path, "kind": "M", "minus": [], "plus": []}) \
                if TEST_PATH_MARKER in line else None
            continue
        if cur is None:
            continue
        if line.startswith("new file mode"):
            cur["kind"] = "A"
        elif line.startswith("deleted file mode"):
            cur["kind"] = "D"
        elif line.startswith(("rename from", "copy from")):
            cur["kind"] = "R"
        elif line.startswith("Binary files"):
            cur["kind"] = "B"
        elif line.startswith(("+++", "---")):
            continue
        elif line.startswith("-"):
            cur["minus"].append("".join(line[1:].split()))
        elif line.startswith("+"):
            cur["plus"].append("".join(line[1:].split()))
    modified, whitespace_only, new_tests = [], [], []
    for f in files.values():
        entry = {"path": f["path"], "kind": f["kind"]}
        if f["kind"] == "A" and NEW_TEST_CLASS_RE.search(f["path"]):
            new_tests.append(entry)
        elif f["kind"] == "M" and sorted(x for x in f["minus"] if x) == sorted(x for x in f["plus"] if x):
            whitespace_only.append(entry)
        else:
            modified.append(entry)
    return {"test_files_modified": len(modified),
            "test_files_deleted": sum(1 for f in modified if f["kind"] == "D"),
            "test_files_added": sum(1 for f in modified if f["kind"] == "A"),
            "test_classes_added": len(new_tests),
            "test_files_whitespace_only": len(whitespace_only),
            "modified_test_files": [f["path"] for f in modified],
            "added_test_classes": [f["path"] for f in new_tests]}


def ensure_test_changes(r: dict) -> dict:
    if r.get("test_files_modified") is None and r.get("_path"):
        diff = Path(r["_path"]) / "git.diff"
        if diff.is_file():
            r.update(analyse_test_changes(diff.read_text(encoding="utf-8", errors="replace")))
    return r


def is_full_success(r: dict) -> bool:
    ensure_test_changes(r)
    return bool(r.get("build_success")) and r.get("tests_failed") == 0 \
        and r.get("tests_errors") == 0 and bool(r.get("java17_verified")) \
        and r.get("test_files_modified") == 0


def is_strict_success(r: dict) -> bool:
    base = r.get("baseline_tests_total")
    return is_full_success(r) and base is not None and (r.get("tests_total") or 0) >= base


def tests_pass(r: dict) -> bool:
    return bool(r.get("build_success")) and r.get("tests_failed") == 0 and r.get("tests_errors") == 0
