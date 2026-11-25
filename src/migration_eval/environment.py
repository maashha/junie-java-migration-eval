"""Snapshot of tool versions and host information, stored with every run."""

from __future__ import annotations

import os
import platform
import re
import sys
from pathlib import Path

from . import __version__
from .config import ExperimentConfig
from .process import CommandRunner, java_env

_JAVA_VERSION_RE = re.compile(r'version "([^"]+)"')
_JUNIE_VERSION_RE = re.compile(r"Junie version:\s*(\S+(?:\s*\([^)]*\))?)")


def java_major(version_output: str) -> int | None:
    """Parse `java -version` output: '1.8.0_392' -> 8, '17.0.9' -> 17."""
    m = _JAVA_VERSION_RE.search(version_output)
    if not m:
        return None
    parts = m.group(1).split(".")
    try:
        return int(parts[1]) if parts[0] == "1" else int(parts[0].split("-")[0].split("+")[0])
    except (ValueError, IndexError):
        return None


def probe_java(runner: CommandRunner, java_home: Path) -> dict:
    code, out = runner.capture([str(java_home / "bin" / "java"), "-version"], Path.cwd())
    return {"java_home": str(java_home), "exit_code": code, "output": out.strip(),
            "major": java_major(out)}


def probe_maven(runner: CommandRunner, cfg: ExperimentConfig, java_home: Path) -> dict:
    env, _ = java_env(java_home, cfg.maven.local_repository)
    code, out = runner.capture([cfg.maven.executable, "-version"], Path.cwd(), env=env)
    m = re.search(r"Apache Maven (\S+)", out)
    return {"executable": cfg.maven.executable, "exit_code": code,
            "version": m.group(1) if m else None, "output": out.strip()}


def probe_junie(runner: CommandRunner, cfg: ExperimentConfig) -> dict:
    # NOTE: any Junie start may apply a previously staged self-update, so the version
    # is probed before *and* after each Junie run (see junie.py).
    code, out = runner.capture([cfg.junie.executable, "--version", "--skip-update-check"], Path.cwd())
    m = _JUNIE_VERSION_RE.search(out)
    return {"executable": cfg.junie.executable, "exit_code": code,
            "version": m.group(1).strip() if m else None, "output": out.strip()[-2000:]}


def _os_release() -> str | None:
    p = Path("/etc/os-release")
    if p.is_file():
        for line in p.read_text(errors="replace").splitlines():
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip('"')
    return None


def _mem_total_kb() -> int | None:
    p = Path("/proc/meminfo")
    if p.is_file():
        m = re.search(r"MemTotal:\s+(\d+)", p.read_text())
        return int(m.group(1)) if m else None
    return None


def collect(runner: CommandRunner, cfg: ExperimentConfig, *, include_junie: bool) -> dict:
    """Collect environment info. Missing tools are recorded, not fatal."""
    code, git_out = runner.capture(["git", "--version"], Path.cwd())
    env: dict = {
        "harness_version": __version__,
        "os": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
            "distribution": _os_release(),
            "cpu_count": os.cpu_count(),
            "mem_total_kb": _mem_total_kb(),
        },
        "python": sys.version,
        "git": git_out.strip(),
        "jdk": {},
        "maven": None,
        "openrewrite": {
            "plugin": cfg.openrewrite.plugin,
            "plugin_version": cfg.openrewrite.plugin_version,
            "recipe_artifact": cfg.openrewrite.recipe_artifact,
            "recipe_artifact_version": cfg.openrewrite.recipe_artifact_version,
            "recipe": cfg.openrewrite.recipe,
        },
        "junie": None,
    }
    for label, home in (("baseline", cfg.baseline_jdk), ("target", cfg.target_jdk)):
        env["jdk"][label] = probe_java(runner, Path(home)) if home else {"error": "not configured"}
    if cfg.target_jdk:
        env["maven"] = probe_maven(runner, cfg, Path(cfg.target_jdk))
    if include_junie:
        env["junie"] = probe_junie(runner, cfg)
    return env
