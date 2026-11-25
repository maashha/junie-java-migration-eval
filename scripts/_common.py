"""Shared CLI plumbing for the scripts (makes `src/` importable without installation)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from migration_eval.config import (ConfigError, ExperimentConfig, Project,  # noqa: E402
                                   load_experiment, load_projects, select_projects)


def add_common_args(p: argparse.ArgumentParser, *, project_required: bool = False) -> None:
    p.add_argument("--project", "-p", nargs="+", required=project_required,
                   help="project name(s) from projects.yaml, or 'all'")
    p.add_argument("--config", type=Path, default=ROOT / "config" / "experiment.yaml")
    p.add_argument("--projects-file", type=Path, default=ROOT / "config" / "projects.yaml")
    p.add_argument("--dry-run", action="store_true",
                   help="print every command that would run; create/modify nothing")


def load(args: argparse.Namespace) -> tuple[ExperimentConfig, list[Project]]:
    try:
        cfg = load_experiment(args.config, ROOT)
        projects = select_projects(load_projects(args.projects_file, ROOT), args.project)
    except (ConfigError, OSError) as e:
        sys.exit(f"configuration error: {e}")
    if cfg.unresolved_env:
        print(f"warning: unset environment variables in config: {', '.join(cfg.unresolved_env)}",
              file=sys.stderr)
    return cfg, projects
