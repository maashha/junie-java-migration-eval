"""Unit tests for the deterministic parts of the harness (no network, no Maven, no Junie)."""

from __future__ import annotations

import struct
import subprocess
import time
from pathlib import Path

import pytest

from migration_eval import evaluator, git, java_target, junie
from migration_eval.config import ConfigError, Project, load_experiment, load_projects
from migration_eval.environment import java_major
from migration_eval.process import CommandRunner
from migration_eval.results import is_full_success, is_strict_success

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- config

def test_repo_configs_load():
    cfg = load_experiment(ROOT / "config" / "experiment.yaml", ROOT)
    assert cfg.junie.model == "gpt-6.1-sol"
    assert cfg.junie.reasoning_effort == "medium"
    assert cfg.target_java == 17
    projects = load_projects(ROOT / "config" / "projects.yaml", ROOT)
    assert {p.name for p in projects} >= {"tinify-java", "airlift-airline"}


def test_projects_reject_short_sha(tmp_path):
    f = tmp_path / "p.yaml"
    f.write_text("projects:\n  - {name: x, repo: r, base_commit: abc123}\n")
    with pytest.raises(ConfigError, match="40-character"):
        load_projects(f, tmp_path)


def test_projects_reject_duplicates(tmp_path):
    sha = "a" * 40
    f = tmp_path / "p.yaml"
    f.write_text(f"projects:\n  - {{name: x, repo: r, base_commit: '{sha}'}}\n"
                 f"  - {{name: x, repo: r, base_commit: '{sha}'}}\n")
    with pytest.raises(ConfigError, match="duplicate"):
        load_projects(f, tmp_path)


def test_unset_env_var_is_recorded_not_guessed(tmp_path, monkeypatch):
    monkeypatch.delenv("JAVA8_HOME", raising=False)
    cfg = load_experiment(ROOT / "config" / "experiment.yaml", ROOT)
    if "JAVA8_HOME" in cfg.unresolved_env:
        assert cfg.baseline_jdk is None
        with pytest.raises(ConfigError):
            cfg.require_jdks()


# ---------------------------------------------------------------- junie command

def test_junie_command_pins_model_and_effort(tmp_path, monkeypatch):
    cfg = load_experiment(ROOT / "config" / "experiment.yaml", ROOT)
    cmd = junie.build_command(cfg, tmp_path, tmp_path / "out.json", "PROMPT")
    assert "--model=gpt-6.1-sol" in cmd
    assert "--effort=medium" in cmd
    assert "--output-format=json" in cmd
    assert cmd[-1] == "--task=PROMPT"
    assert not any(a.startswith("--auth") for a in cmd)   # token never on the command line


def test_junie_env_strips_other_junie_vars(monkeypatch):
    monkeypatch.setenv("JUNIE_MODEL", "something-else")
    monkeypatch.setenv("JUNIE_SYSTEM_PROMPT", "be sloppy")
    monkeypatch.setenv("JUNIE_API_KEY", "perm-secret")
    cfg = load_experiment(ROOT / "config" / "experiment.yaml", ROOT)
    env, overrides, removed = junie.build_env(cfg)
    assert "JUNIE_MODEL" not in env and "JUNIE_SYSTEM_PROMPT" not in env
    assert env["JUNIE_API_KEY"] == "perm-secret"
    assert {"JUNIE_MODEL", "JUNIE_SYSTEM_PROMPT"} <= set(removed)
    assert "JUNIE_API_KEY" not in overrides


# ---------------------------------------------------------------- java levels

@pytest.mark.parametrize("raw,expected", [("1.8", 8), ("8", 8), ("17", 17), ("${x}", None), (None, None)])
def test_normalize_level(raw, expected):
    assert java_target.normalize_level(raw) == expected


@pytest.mark.parametrize("out,major", [
    ('openjdk version "1.8.0_392"', 8), ('openjdk version "17.0.9" 2023-10-17', 17),
    ('openjdk version "21" 2023-09-19', 21)])
def test_java_major(out, major):
    assert java_major(out) == major


EFFECTIVE_POM = """<?xml version="1.0"?>
<projects>
 <project xmlns="http://maven.apache.org/POM/4.0.0">
  <artifactId>parent</artifactId>
  <properties><maven.compiler.source>17</maven.compiler.source><maven.compiler.target>17</maven.compiler.target></properties>
 </project>
 <project xmlns="http://maven.apache.org/POM/4.0.0">
  <artifactId>child</artifactId>
  <properties><maven.compiler.source>1.8</maven.compiler.source><maven.compiler.target>1.8</maven.compiler.target></properties>
  <build><plugins><plugin><artifactId>maven-compiler-plugin</artifactId>
   <configuration><release>17</release></configuration></plugin></plugins></build>
 </project>
</projects>"""


def test_effective_pom_plugin_release_wins_over_properties(tmp_path):
    p = tmp_path / "eff.xml"
    p.write_text(EFFECTIVE_POM)
    levels = java_target.levels_from_effective_pom(p)
    assert [(m.module, m.level, m.source) for m in levels] == [
        ("parent", 17, "properties.source/target"), ("child", 17, "compiler-plugin.release")]


def test_source_target_mismatch_is_not_17(tmp_path):
    p = tmp_path / "eff.xml"
    p.write_text("""<project><artifactId>a</artifactId><properties>
      <maven.compiler.source>17</maven.compiler.source><maven.compiler.target>1.8</maven.compiler.target>
      </properties></project>""")
    assert java_target.levels_from_effective_pom(p)[0].level is None


def test_static_pom_resolves_properties(tmp_path):
    (tmp_path / "pom.xml").write_text("""<project><artifactId>a</artifactId><properties>
      <java.version>1.8</java.version><maven.compiler.source>${java.version}</maven.compiler.source>
      <maven.compiler.target>${java.version}</maven.compiler.target></properties></project>""")
    assert java_target.levels_from_static_poms(tmp_path)[0].level == 8


def _class_file(path: Path, major: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xca\xfe\xba\xbe" + struct.pack(">HH", 0, major))


def test_bytecode_and_verification(tmp_path):
    since = time.time() - 5
    _class_file(tmp_path / "target/classes/a/A.class", 61)
    _class_file(tmp_path / "target/test-classes/a/ATest.class", 61)
    _class_file(tmp_path / "target/classes/META-INF/versions/9/module-info.class", 53)
    eff = tmp_path / "eff.xml"
    eff.write_text("<project><artifactId>a</artifactId><properties>"
                   "<maven.compiler.release>17</maven.compiler.release></properties></project>")
    rep = java_target.analyse(tmp_path, eff, since, 17)
    assert rep.main_class_majors == {"61": 1}
    assert rep.verified


def test_stale_and_wrong_bytecode_not_verified(tmp_path):
    _class_file(tmp_path / "target/classes/A.class", 52)
    eff = tmp_path / "eff.xml"
    eff.write_text("<project><artifactId>a</artifactId><properties>"
                   "<maven.compiler.release>17</maven.compiler.release></properties></project>")
    rep = java_target.analyse(tmp_path, eff, time.time() - 5, 17)
    assert rep.config_targets_expected and not rep.bytecode_targets_expected and not rep.verified
    rep_stale = java_target.analyse(tmp_path, eff, time.time() + 60, 17)
    assert rep_stale.main_class_majors == {} and not rep_stale.verified


# ---------------------------------------------------------------- tests parsing

def test_surefire_reports_parsed_and_stale_ignored(tmp_path):
    d = tmp_path / "mod/target/surefire-reports"
    d.mkdir(parents=True)
    (d / "TEST-a.xml").write_text('<testsuite tests="10" failures="1" errors="2" skipped="3"/>')
    counts = evaluator.parse_test_reports(tmp_path, time.time() - 5)
    assert (counts.total, counts.failed, counts.errors, counts.skipped, counts.passed) == (10, 1, 2, 3, 4)
    assert evaluator.parse_test_reports(tmp_path, time.time() + 60).total == 0


def test_maven_log_fallback_ignores_per_class_lines():
    log = """[INFO] Tests run: 5, Failures: 0, Errors: 0, Skipped: 0, Time elapsed: 0.1 s - in a.ATest
[INFO] Tests run: 5, Failures: 0, Errors: 0, Skipped: 0
[ERROR] Tests run: 3, Failures: 1, Errors: 0, Skipped: 1
"""
    c = evaluator.parse_maven_log_tests(log)
    assert (c.total, c.failed, c.skipped, c.passed) == (8, 1, 1, 6)


@pytest.mark.parametrize("line,category", [
    ("[ERROR] COMPILATION ERROR :", "compilation"),
    ("[ERROR] Failed to execute goal on project x: Could not resolve dependencies for project", "dependency"),
    ("[ERROR] Non-resolvable parent POM for x", "build_configuration"),
    ("[ERROR] There are test failures.", "tests"),
    ("java.lang.IllegalArgumentException: Unsupported class file major version 61", "plugin_incompatibility"),
])
def test_classification(line, category):
    assert evaluator.classify_log("noise\n" + line)[0] == category


# ---------------------------------------------------------------- success

def test_success_definitions():
    ok = {"build_success": True, "tests_failed": 0, "tests_errors": 0, "java17_verified": True,
          "tests_total": 10, "baseline_tests_total": 12, "test_files_modified": 0}
    assert is_full_success(ok) and not is_strict_success(ok)
    assert not is_full_success({**ok, "java17_verified": False})
    assert not is_full_success({**ok, "tests_errors": 1})
    assert is_strict_success({**ok, "tests_total": 12})


# ---------------------------------------------------------------- git isolation + diff

def _sh(*args, cwd):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


def test_checkout_isolation_and_diff(tmp_path):
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _sh("git", "init", "-q", cwd=upstream)
    _sh("git", "config", "user.email", "t@t", cwd=upstream)
    _sh("git", "config", "user.name", "t", cwd=upstream)
    (upstream / "A.java").write_text("class A {}\n")
    _sh("git", "add", ".", cwd=upstream)
    _sh("git", "commit", "-qm", "init", cwd=upstream)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=upstream, capture_output=True,
                         text=True).stdout.strip()

    work = tmp_path / "work"
    project = Project("demo", str(upstream), sha)
    runner = CommandRunner(echo=False)
    git.ensure_canonical_clone(runner, work, project)
    copy = work / "demo" / "junie" / "run-01"
    meta = git.create_checkout(runner, work, project, copy)
    assert meta["head"] == sha and meta["working_tree_clean"]
    with pytest.raises(git.GitError, match="refusing"):
        git.create_checkout(runner, work, project, copy)

    # Simulate a tool: modify, add untracked, add build output, commit.
    (copy / "A.java").write_text("class A { int x; }\n")
    (copy / "B.java").write_text("class B {}\n")
    (copy / "target").mkdir()
    (copy / "target" / "A.class").write_bytes(b"\x00")
    stats = git.write_diff(runner, copy, sha, tmp_path / "git.diff", ("**/target/**",))
    assert sorted(f["path"] for f in stats.files) == ["A.java", "B.java"]
    assert stats.lines_added == 2 and stats.lines_removed == 1
    assert "class B" in (tmp_path / "git.diff").read_text()
    # canonical clone untouched: still only the base commit, no remotes in the copy
    out = subprocess.run(["git", "remote"], cwd=copy, capture_output=True, text=True).stdout
    assert out.strip() == ""
    with pytest.raises(FileExistsError):
        git.write_diff(runner, copy, sha, tmp_path / "git.diff", ())


def test_attach_manual_auto_success_and_unfixed(tmp_path):
    from migration_eval.summary import attach_manual
    ok = {"method": "junie", "_path": str(tmp_path), "test_files_modified": 0, "build_success": True, "tests_failed": 0,
          "tests_errors": 0, "java17_verified": True, "migration_duration_seconds": 100.0,
          "llm_cost_usd": 0.2}
    attach_manual(ok)
    assert ok["manual_fixes"] == 0 and ok["time_to_success_seconds"] == 100.0
    bad = {"method": "openrewrite", "_path": str(tmp_path), "build_success": False,
           "migration_duration_seconds": 10.0}
    attach_manual(bad)
    assert bad["manual_fix_needed"] and bad["manual_fixes"] is None
    assert bad["time_to_success_seconds"] is None and bad["llm_cost_usd"] == 0.0


def test_analyse_test_changes_ignores_line_endings_only():
    from migration_eval.results import analyse_test_changes
    diff = (
        "diff --git a/src/test/A.java b/src/test/A.java\n--- a/src/test/A.java\n+++ b/src/test/A.java\n"
        "@@ -1 +1 @@\n-  assertEquals(1, x);\r\n+  assertEquals(1, x);\n"
        "diff --git a/src/test/B.java b/src/test/B.java\n--- a/src/test/B.java\n+++ b/src/test/B.java\n"
        "@@ -1 +1 @@\n-  assertEquals(1, x);\n+  assertEquals(2, x);\n"
        "diff --git a/src/test/C.java b/src/test/C.java\ndeleted file mode 100644\n"
        "diff --git a/pom.xml b/pom.xml\n@@ -1 +1 @@\n-<v>8</v>\n+<v>17</v>\n"
    )
    r = analyse_test_changes(diff)
    assert r["test_files_modified"] == 2 and r["test_files_deleted"] == 1
    assert r["test_files_whitespace_only"] == 1
    assert r["modified_test_files"] == ["src/test/B.java", "src/test/C.java"]


def test_full_success_requires_unmodified_tests():
    from migration_eval.results import is_full_success
    base = {"build_success": True, "tests_failed": 0, "tests_errors": 0, "java17_verified": True}
    assert is_full_success({**base, "test_files_modified": 0})
    assert not is_full_success({**base, "test_files_modified": 1})


def test_new_test_class_is_allowed_but_new_resource_is_not():
    from migration_eval.results import analyse_test_changes
    diff = (
        "diff --git a/src/test/java/a/NewSecurityTests.java b/src/test/java/a/NewSecurityTests.java\n"
        "new file mode 100644\n@@ -0,0 +1 @@\n+class NewSecurityTests {}\n"
        "diff --git a/src/test/resources/junit-platform.properties b/src/test/resources/junit-platform.properties\n"
        "new file mode 100644\n@@ -0,0 +1 @@\n+x=y\n"
    )
    r = analyse_test_changes(diff)
    assert r["test_classes_added"] == 1 and r["test_files_modified"] == 1
    assert r["modified_test_files"] == ["src/test/resources/junit-platform.properties"]
