"""OpenRewrite runner (Maven).

Recipe and invocation verified on 2026-11-22:
  * recipe `org.openrewrite.java.migrate.UpgradeToJava17` ("Migrate to Java 17") from
    rewrite-migrate-java; its recipe list starts with Java8toJava11 and
    UpgradeBuildToJava17, so it covers Java 8 sources
    (src/main/resources/META-INF/rewrite/java-version-17.yml upstream);
  * command-line invocation without modifying the POM, as documented in
    https://docs.openrewrite.org/running-recipes/popular-recipe-guides/migrate-to-java-17 :
      mvn org.openrewrite.maven:rewrite-maven-plugin:<v>:run
          -Drewrite.recipeArtifactCoordinates=org.openrewrite.recipe:rewrite-migrate-java:<v>
          -Drewrite.activeRecipes=org.openrewrite.java.migrate.UpgradeToJava17
  * versions pinned (no RELEASE/LATEST, no -U) for reproducibility; both were checked
    to exist on Maven Central.

Only Maven projects are supported (all MigrationBench projects are Maven). A project
without a root pom.xml is recorded as `unsupported_build_tool`, not migrated.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import ExperimentConfig
from .process import CommandRunner, java_env


def build_command(cfg: ExperimentConfig) -> list[str]:
    o = cfg.openrewrite
    return [
        cfg.maven.executable, "-B", "-ntp",
        f"{o.plugin}:run",
        f"-Drewrite.recipeArtifactCoordinates={o.recipe_artifact}",
        f"-Drewrite.activeRecipes={o.recipe}",
    ]


@dataclass
class OpenRewriteRunInfo:
    command: list[str]
    plugin: str
    plugin_version: str
    recipe_artifact: str
    recipe_artifact_version: str
    recipe: str
    build_tool: str
    process: dict | None
    status: str                       # completed | tool_failure | timeout | unsupported_build_tool | harness_error | dry_run
    files_reported_changed: list[str] = field(default_factory=list)
    plugin_version_confirmed_in_log: bool | None = None

    def to_dict(self) -> dict:
        return asdict(self)


_CHANGED_RE = re.compile(r"Changes have been made to (\S+) by:")


def run(runner: CommandRunner, cfg: ExperimentConfig, project_dir: Path, run_dir: Path,
        timeout_seconds: float) -> OpenRewriteRunInfo:
    o = cfg.openrewrite
    cmd = build_command(cfg)
    build_tool = "maven" if (project_dir / "pom.xml").is_file() or runner.dry_run else (
        "gradle" if any((project_dir / f).is_file() for f in ("build.gradle", "build.gradle.kts")) else "unknown")
    info = OpenRewriteRunInfo(cmd, o.plugin, o.plugin_version, o.recipe_artifact,
                              o.recipe_artifact_version, o.recipe, build_tool, None, "unknown")
    if build_tool != "maven":
        info.status = "unsupported_build_tool"
        for name in ("migration.stdout.log", "migration.stderr.log"):
            (run_dir / name).open("x").close()
        return info
    # Real runs have already checked the JDK via cfg.require_jdks().
    env, overrides = java_env(Path(cfg.target_jdk or "<JAVA17_HOME unset>"), cfg.maven.local_repository)
    proc = runner.run(cmd, project_dir, stdout_path=run_dir / "migration.stdout.log",
                      stderr_path=run_dir / "migration.stderr.log", env=env,
                      env_overrides_for_log=overrides, timeout=timeout_seconds)
    info.process = proc.to_dict()
    if runner.dry_run:
        info.status = "dry_run"
        return info
    if proc.error:
        info.status = "harness_error"
    elif proc.timed_out:
        info.status = "timeout"
    elif proc.exit_code != 0:
        info.status = "tool_failure"
    else:
        info.status = "completed"
    log = (run_dir / "migration.stdout.log").read_text(encoding="utf-8", errors="replace")
    info.files_reported_changed = _CHANGED_RE.findall(log)
    # Maven logs the goal with its prefix, e.g. "--- rewrite:6.46.1:run (default-cli) @ x ---"
    info.plugin_version_confirmed_in_log = f"rewrite:{o.plugin_version}:run" in log
    return info
