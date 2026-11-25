"""Loading and validating `config/experiment.yaml` and `config/projects.yaml`."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ConfigError(Exception):
    """Raised for invalid or incomplete configuration."""


@dataclass(frozen=True)
class Project:
    name: str
    repo: str
    base_commit: str
    group: str | None = None
    timeout_minutes: float | None = None


@dataclass(frozen=True)
class JunieConfig:
    executable: str
    model: str
    model_display_name: str
    reasoning_effort: str
    extra_args: tuple[str, ...]
    home: Path


@dataclass(frozen=True)
class OpenRewriteConfig:
    plugin: str
    recipe_artifact: str
    recipe: str

    @property
    def plugin_version(self) -> str:
        return self.plugin.rsplit(":", 1)[-1]

    @property
    def recipe_artifact_version(self) -> str:
        return self.recipe_artifact.rsplit(":", 1)[-1]


@dataclass(frozen=True)
class MavenConfig:
    executable: str
    validation_args: tuple[str, ...]
    local_repository: Path | None
    help_plugin: str


@dataclass(frozen=True)
class ExperimentConfig:
    name: str
    repetitions: int
    work_dir: Path
    results_dir: Path
    prompt_file: Path
    source_java: int
    target_java: int
    timeout_minutes: float
    validation_timeout_minutes: float
    baseline_jdk: str | None   # unresolved values are kept as None so dry runs still work
    target_jdk: str | None
    maven: MavenConfig
    junie: JunieConfig
    openrewrite: OpenRewriteConfig
    diff_exclude: tuple[str, ...]
    unresolved_env: tuple[str, ...] = field(default_factory=tuple)

    def migration_timeout_seconds(self, project: Project) -> float:
        minutes = project.timeout_minutes or self.timeout_minutes
        return minutes * 60

    def require_jdks(self) -> tuple[Path, Path]:
        """Return (baseline_jdk, target_jdk) or fail loudly if not configured."""
        baseline, target = self.baseline_jdk, self.target_jdk
        if not baseline or not target:
            missing = [k for k, v in (("jdk.baseline_home", baseline), ("jdk.target_home", target)) if not v]
            raise ConfigError(
                f"{', '.join(missing)} not configured (unset env vars: "
                f"{', '.join(self.unresolved_env) or 'none'}). Set JAVA8_HOME / JAVA17_HOME "
                "or edit config/experiment.yaml."
            )
        paths = (Path(baseline), Path(target))
        for p in paths:
            if not (p / "bin" / "java").exists():
                raise ConfigError(f"JDK not found: {p}/bin/java does not exist")
        return paths


def _expand(value: Any, unresolved: list[str]) -> Any:
    """Expand ${VAR}; record unresolved variables instead of guessing a value."""
    if not isinstance(value, str):
        return value
    missing = [m for m in _ENV_RE.findall(value) if m not in os.environ]
    if missing:
        unresolved.extend(missing)
        return None
    return os.path.expanduser(_ENV_RE.sub(lambda m: os.environ[m.group(1)], value))


def _get(d: dict[str, Any], dotted: str, default: Any = ..., *, where: str) -> Any:
    cur: Any = d
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            if default is ...:
                raise ConfigError(f"{where}: missing required key '{dotted}'")
            return default
        cur = cur[part]
    return cur


def _resolve_path(p: str, root: Path) -> Path:
    path = Path(os.path.expanduser(p))
    return path if path.is_absolute() else (root / path).resolve()


def load_experiment(path: Path | None = None, root: Path = REPO_ROOT) -> ExperimentConfig:
    path = path or root / "config" / "experiment.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    w = str(path)
    unresolved: list[str] = []

    validation_args = _get(raw, "maven.validation_args", ["-B", "-ntp", "clean", "test"], where=w)
    if not isinstance(validation_args, list) or not all(isinstance(a, str) for a in validation_args):
        raise ConfigError(f"{w}: maven.validation_args must be a list of strings")
    local_repo = _get(raw, "maven.local_repository", None, where=w)
    extra_args = _get(raw, "junie.extra_args", [], where=w) or []

    cfg = ExperimentConfig(
        name=str(_get(raw, "experiment.name", "experiment", where=w)),
        repetitions=int(_get(raw, "experiment.repetitions", 1, where=w)),
        work_dir=_resolve_path(_get(raw, "paths.work_dir", "work", where=w), root),
        results_dir=_resolve_path(_get(raw, "paths.results_dir", "results", where=w), root),
        prompt_file=_resolve_path(_get(raw, "paths.prompt_file", where=w), root),
        source_java=int(_get(raw, "migration.source_java", 8, where=w)),
        target_java=int(_get(raw, "migration.target_java", 17, where=w)),
        timeout_minutes=float(_get(raw, "limits.timeout_minutes", 30, where=w)),
        validation_timeout_minutes=float(_get(raw, "limits.validation_timeout_minutes", 30, where=w)),
        baseline_jdk=_expand(_get(raw, "jdk.baseline_home", None, where=w), unresolved),
        target_jdk=_expand(_get(raw, "jdk.target_home", None, where=w), unresolved),
        maven=MavenConfig(
            executable=str(_get(raw, "maven.executable", "mvn", where=w)),
            validation_args=tuple(validation_args),
            local_repository=_resolve_path(_expand(local_repo, unresolved), root) if local_repo else None,
            help_plugin=str(_get(raw, "maven.help_plugin", where=w)),
        ),
        junie=JunieConfig(
            executable=str(_get(raw, "junie.executable", "junie", where=w)),
            model=str(_get(raw, "junie.model", where=w)),
            model_display_name=str(_get(raw, "junie.model_display_name", "", where=w)),
            reasoning_effort=str(_get(raw, "junie.reasoning_effort", where=w)),
            extra_args=tuple(str(a) for a in extra_args),
            home=Path(os.path.expanduser(str(_get(raw, "junie.home", "~/.junie", where=w)))),
        ),
        openrewrite=OpenRewriteConfig(
            plugin=str(_get(raw, "openrewrite.plugin", where=w)),
            recipe_artifact=str(_get(raw, "openrewrite.recipe_artifact", where=w)),
            recipe=str(_get(raw, "openrewrite.recipe", where=w)),
        ),
        diff_exclude=tuple(_get(raw, "evaluation.diff_exclude", ["**/target/**"], where=w)),
        unresolved_env=tuple(sorted(set(unresolved))),
    )
    if cfg.repetitions < 1:
        raise ConfigError(f"{w}: experiment.repetitions must be >= 1")
    if not cfg.prompt_file.is_file():
        raise ConfigError(f"{w}: prompt file not found: {cfg.prompt_file}")
    return cfg


def load_projects(path: Path | None = None, root: Path = REPO_ROOT) -> list[Project]:
    path = path or root / "config" / "projects.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries = raw.get("projects") or []
    projects: list[Project] = []
    seen: set[str] = set()
    for i, e in enumerate(entries):
        where = f"{path}: projects[{i}]"
        for key in ("name", "repo", "base_commit"):
            if not e.get(key):
                raise ConfigError(f"{where}: missing '{key}'")
        name, commit = str(e["name"]), str(e["base_commit"]).strip().lower()
        if not _NAME_RE.match(name):
            raise ConfigError(f"{where}: invalid name '{name}' (use letters, digits, . _ -)")
        if name in seen:
            raise ConfigError(f"{where}: duplicate project name '{name}'")
        if not _SHA_RE.match(commit):
            raise ConfigError(f"{where}: base_commit must be a full 40-character SHA, got '{commit}'")
        group = e.get("group")
        if group is not None and group not in ("small", "medium", "large"):
            raise ConfigError(f"{where}: group must be small|medium|large")
        timeout = e.get("timeout_minutes", e.get("timeout"))
        seen.add(name)
        projects.append(Project(name, str(e["repo"]), commit, group,
                                float(timeout) if timeout is not None else None))
    return projects


def select_projects(projects: list[Project], names: list[str] | None) -> list[Project]:
    if not names or names == ["all"]:
        return projects
    by_name = {p.name: p for p in projects}
    unknown = [n for n in names if n not in by_name]
    if unknown:
        raise ConfigError(f"Unknown project(s): {', '.join(unknown)}. Known: {', '.join(by_name)}")
    return [by_name[n] for n in names]
