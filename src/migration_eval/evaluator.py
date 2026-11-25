"""Common evaluator, applied identically to baseline, Junie and OpenRewrite results.

Steps (all under one explicitly chosen JDK):
  1. verify the JDK really is the expected major version;
  2. run the configured Maven validation command (default `mvn -B -ntp clean test`);
  3. produce the effective POM with a pinned maven-help-plugin;
  4. parse Surefire/Failsafe XML reports written by *this* evaluation;
  5. analyse the Java target (config + bytecode, see java_target.py);
  6. classify a failure from the logs (heuristic; raw logs are kept for manual review).
"""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import java_target
from .config import ExperimentConfig
from .environment import probe_java
from .process import CommandRunner, ProcessResult, java_env


@dataclass
class TestCounts:
    total: int = 0
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    source: str = "none"          # "surefire-xml" | "maven-log" | "none"
    report_files: int = 0
    unparsable_reports: int = 0


@dataclass
class EvaluationResult:
    java: dict
    jdk_ok: bool
    validation: dict | None
    build_success: bool
    tests: TestCounts
    effective_pom: dict | None
    java_target: dict
    target_verified: bool
    failure_category: str | None
    failure_evidence: str | None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- tests

def parse_test_reports(repo: Path, since_epoch: float) -> TestCounts:
    counts = TestCounts()
    reports = [p for pattern in ("**/target/surefire-reports/TEST-*.xml",
                                 "**/target/failsafe-reports/TEST-*.xml")
               for p in repo.glob(pattern)]
    for rp in reports:
        try:
            if rp.stat().st_mtime < since_epoch:
                continue      # stale report from an earlier (e.g. agent) build
            root = ET.parse(rp).getroot()
        except (ET.ParseError, OSError):
            counts.unparsable_reports += 1
            continue
        suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
        for s in suites:
            counts.total += int(float(s.get("tests", 0) or 0))
            counts.failed += int(float(s.get("failures", 0) or 0))
            counts.errors += int(float(s.get("errors", 0) or 0))
            counts.skipped += int(float(s.get("skipped", 0) or 0))
        counts.report_files += 1
    if counts.report_files:
        counts.source = "surefire-xml"
        counts.passed = counts.total - counts.failed - counts.errors - counts.skipped
    return counts


_SUMMARY_RE = re.compile(
    r"^\[(?:INFO|WARNING|ERROR)\]\s+Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+),\s*Skipped:\s*(\d+)\s*$")


def parse_maven_log_tests(log: str) -> TestCounts:
    """Fallback: sum per-module summary lines ('Tests run: X, Failures: ...' without
    'Time elapsed', which only per-class lines contain)."""
    c = TestCounts()
    for line in log.splitlines():
        m = _SUMMARY_RE.match(line.strip())
        if m:
            t, f, e, s = map(int, m.groups())
            c.total += t; c.failed += f; c.errors += e; c.skipped += s
    if c.total or any(_SUMMARY_RE.match(l.strip()) for l in log.splitlines()):
        c.source = "maven-log"
        c.passed = c.total - c.failed - c.errors - c.skipped
    return c


# --------------------------------------------------------------- classification

# Ordered: first match wins. Patterns are deliberately conservative; the evidence line
# is stored so every label can be reviewed manually.
_FAILURE_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("build_configuration", re.compile(
        r"Non-resolvable parent POM|Malformed POM|ProjectBuildingException|"
        r"The build could not read \d+ project|Unknown packaging|Non-parseable POM|"
        r"invalid target release|release version \d+ not supported|"
        r"Source option \d+ is no longer supported|Target option \d+ is no longer supported", re.I)),
    ("dependency", re.compile(
        r"Could not resolve dependencies|Could not find artifact|Failed to collect dependencies|"
        r"Could not transfer artifact|DependencyResolutionException|"
        r"Failed to read artifact descriptor", re.I)),
    ("compilation", re.compile(
        r"COMPILATION ERROR|Compilation failure|maven-compiler-plugin.*(?:failed|Fatal error)", re.I)),
    ("tests", re.compile(
        r"There are test failures|There were test failures|maven-surefire-plugin.*failed|"
        r"maven-failsafe-plugin.*failed|Tests run:.*(?:Failures: [1-9]|Errors: [1-9])", re.I)),
    ("plugin_incompatibility", re.compile(
        r"Unsupported class file major version|PluginResolutionException|"
        r"PluginContainerException|Failed to execute goal [^\s]+:[^\s]+:[^\s]+|"
        r"InaccessibleObjectException|module java\.base does not \"opens", re.I)),
]


def classify_log(log: str) -> tuple[str, str] | None:
    lines = log.splitlines()
    for category, pattern in _FAILURE_PATTERNS:
        for line in lines:
            if pattern.search(line):
                return category, line.strip()[:500]
    return None


# --------------------------------------------------------------------- evaluator

def _read(p: Path | None) -> str:
    if p is None or not p.is_file():
        return ""
    return p.read_text(encoding="utf-8", errors="replace")


def evaluate(
    runner: CommandRunner,
    cfg: ExperimentConfig,
    repo: Path,
    out_dir: Path,
    *,
    java_home: Path,
    expected_jdk_major: int,
    expected_target: int,
    prefix: str = "validation",
) -> EvaluationResult:
    """Evaluate the project at `repo`, writing logs to `out_dir/<prefix>.*`."""
    notes: list[str] = []
    java = probe_java(runner, java_home)
    jdk_ok = java.get("major") == expected_jdk_major
    if not jdk_ok and not runner.dry_run:
        return EvaluationResult(java, False, None, False, TestCounts(), None,
                                {"config_source": "unavailable"}, False, "harness_error",
                                f"JDK at {java_home} is major {java.get('major')}, expected {expected_jdk_major}",
                                ["evaluation aborted: wrong JDK"])

    env, overrides = java_env(java_home, cfg.maven.local_repository)
    since = time.time() - 1  # 1s slack for coarse filesystem timestamps
    stdout_p, stderr_p = out_dir / f"{prefix}.stdout.log", out_dir / f"{prefix}.stderr.log"
    validation: ProcessResult = runner.run(
        [cfg.maven.executable, *cfg.maven.validation_args], repo,
        stdout_path=stdout_p, stderr_path=stderr_p, env=env, env_overrides_for_log=overrides,
        timeout=cfg.validation_timeout_minutes * 60,
    )

    eff_pom_path = out_dir / f"{prefix}.effective-pom.xml"
    eff = runner.run(
        [cfg.maven.executable, "-B", "-ntp", "-q", f"{cfg.maven.help_plugin}:effective-pom",
         f"-Doutput={eff_pom_path}"], repo,
        stdout_path=out_dir / f"{prefix}.effective-pom.log",
        stderr_path=out_dir / f"{prefix}.effective-pom.stderr.log",
        env=env, env_overrides_for_log=overrides, timeout=600,
    )
    if runner.dry_run:
        return EvaluationResult(java, jdk_ok, validation.to_dict(), False, TestCounts(), eff.to_dict(),
                                {"config_source": "dry-run"}, False, None, None, ["dry run"])
    if not eff.ok:
        notes.append("effective POM generation failed; falling back to static pom.xml parsing")

    build_success = validation.ok
    tests = parse_test_reports(repo, since)
    log = _read(stdout_p) + "\n" + _read(stderr_p)
    if tests.source == "none":
        fallback = parse_maven_log_tests(log)
        if fallback.source != "none":
            tests = fallback
            notes.append("no fresh Surefire/Failsafe XML reports; test counts parsed from Maven log")

    report = java_target.analyse(repo, eff_pom_path if eff.ok else None, since, expected_target)
    notes.extend(report.notes)

    category, evidence = None, None
    if validation.timed_out:
        category, evidence = "timeout", f"validation exceeded {cfg.validation_timeout_minutes} min"
    elif validation.error:
        category, evidence = "harness_error", validation.error
    elif not build_success:
        hit = classify_log(log)
        category, evidence = hit if hit else ("unknown", None)
    elif tests.failed or tests.errors:
        # Build "succeeded" with failures, e.g. testFailureIgnore=true
        category, evidence = "tests", f"{tests.failed} failed, {tests.errors} errors"
    elif not report.verified:
        category = "java_target_not_met"
        evidence = (f"declared levels={report.declared_levels}, "
                    f"main class majors={report.main_class_majors}")

    return EvaluationResult(
        java=java, jdk_ok=jdk_ok, validation=validation.to_dict(), build_success=build_success,
        tests=tests, effective_pom=eff.to_dict(), java_target=report.to_dict(),
        target_verified=report.verified, failure_category=category, failure_evidence=evidence,
        notes=notes,
    )
