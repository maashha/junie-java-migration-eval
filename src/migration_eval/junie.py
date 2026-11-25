"""Junie CLI runner.

CLI syntax verified against the locally installed CLI (`junie --help`, versions
26.10.27 and 26.11.2) and https://junie.jetbrains.com/docs/parameters.html :

    junie --skip-update-check --project=<dir> --model=<id> --effort=<level>
          --output-format=json --json-output-file=<file> --task=<prompt>

Observed behaviour that the runner accounts for:
  * An invalid --model fails fast with exit 1 and lists valid ids.
  * Missing auth prints {"errors": [...]} but exits 0 -> the exit code alone is NOT a
    success signal; the JSON output is parsed.
  * Junie may apply a staged self-update on start even with --skip-update-check ->
    the version is recorded before and after the run.
  * There is no documented timeout flag -> the harness enforces a wall-clock timeout
    and kills the whole process group.
  * Authentication: JUNIE_API_KEY from the environment (documented); the token is
    never placed on the command line, so it never appears in logs.
  * The JSON output schema is undocumented. Fields used here (sessionId, result,
    errors) were observed in practice; anything missing is recorded as unknown.

After the run the session's events.jsonl in ~/.junie/sessions is inspected to verify
which model and effort Junie actually launched with (ModelForLaunchAttachment /
EffortForLaunchAttachment events) and is archived with the results.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from .config import ExperimentConfig
from .environment import probe_junie
from .process import CommandRunner, ProcessResult, java_env

# JUNIE_* variables that may stay in the environment. Everything else (JUNIE_MODEL,
# JUNIE_EFFORT, JUNIE_SYSTEM_PROMPT, JUNIE_AGENT, ...) is removed so the user's shell
# cannot silently change the experimental condition.
_ALLOWED_JUNIE_ENV = {"JUNIE_API_KEY", "JUNIE_HOME"}


def build_command(cfg: ExperimentConfig, project_dir: Path, json_out: Path, prompt: str) -> list[str]:
    j = cfg.junie
    return [
        j.executable,
        "--skip-update-check",
        f"--project={project_dir}",
        f"--model={j.model}",
        f"--effort={j.reasoning_effort}",
        "--output-format=json",
        f"--json-output-file={json_out}",
        *j.extra_args,
        f"--task={prompt}",
    ]


def build_env(cfg: ExperimentConfig) -> tuple[dict[str, str], dict[str, str], list[str]]:
    """Return (env, logged overrides, removed variable names)."""
    # Real runs have already checked the JDK via cfg.require_jdks(); the placeholder only
    # appears in dry-run output.
    jdk = Path(cfg.target_jdk or "<JAVA17_HOME unset>")
    base = dict(os.environ)
    removed = sorted(k for k in base if k.startswith("JUNIE_") and k not in _ALLOWED_JUNIE_ENV)
    for k in removed:
        del base[k]
    env, overrides = java_env(jdk, cfg.maven.local_repository, base=base)
    return env, overrides, removed


@dataclass
class JunieRunInfo:
    command: list[str]
    model: str
    model_display_name: str
    reasoning_effort: str
    reasoning_effort_passed_as: str
    prompt_file: str
    auth_source: str
    removed_env_vars: list[str]
    version_before: str | None
    version_after: str | None
    version_changed_during_run: bool
    process: dict
    status: str                     # completed | agent_error | timeout | no_output | harness_error | dry_run
    agent_reported_completion: bool
    agent_errors: list[str] = field(default_factory=list)
    session_id: str | None = None
    result_excerpt: str | None = None
    llm_usage: Any = None
    session_verification: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def run(runner: CommandRunner, cfg: ExperimentConfig, project_dir: Path, run_dir: Path,
        timeout_seconds: float) -> JunieRunInfo:
    prompt = cfg.prompt_file.read_text(encoding="utf-8").strip()
    json_out = run_dir / "junie-output.json"
    cmd = build_command(cfg, project_dir, json_out, prompt)
    env, overrides, removed = build_env(cfg)
    auth = "JUNIE_API_KEY environment variable" if env.get("JUNIE_API_KEY") else \
        "stored Junie credentials (JUNIE_API_KEY not set; not verified before run)"

    before = probe_junie(runner, cfg).get("version")
    proc: ProcessResult = runner.run(
        cmd, project_dir,
        stdout_path=run_dir / "migration.stdout.log", stderr_path=run_dir / "migration.stderr.log",
        env=env, env_overrides_for_log=overrides, timeout=timeout_seconds,
    )
    after = None if runner.dry_run else probe_junie(runner, cfg).get("version")

    info = JunieRunInfo(
        command=cmd, model=cfg.junie.model, model_display_name=cfg.junie.model_display_name,
        reasoning_effort=cfg.junie.reasoning_effort, reasoning_effort_passed_as="--effort",
        prompt_file=str(cfg.prompt_file), auth_source=auth, removed_env_vars=removed,
        version_before=before, version_after=after,
        version_changed_during_run=bool(before and after and before != after),
        process=proc.to_dict(), status="dry_run" if runner.dry_run else "unknown",
        agent_reported_completion=False,
    )
    if runner.dry_run:
        return info

    output = _load_json(json_out)
    if output is None:
        # Fallback: stdout may contain the JSON document when no file was written.
        output = _load_json(run_dir / "migration.stdout.log")
    if isinstance(output, dict):
        errs = output.get("errors") or []
        info.agent_errors = [str(e) for e in (errs if isinstance(errs, list) else [errs])]
        info.session_id = output.get("sessionId") or output.get("session_id")
        res = output.get("result")
        info.result_excerpt = (res if isinstance(res, str) else json.dumps(res))[:4000] if res else None
        info.llm_usage = output.get("llmUsage")

    if proc.error:
        info.status = "harness_error"
    elif proc.timed_out:
        info.status = "timeout"
    elif info.agent_errors or (proc.exit_code not in (0, None)):
        info.status = "agent_error"
    elif output is None:
        info.status = "no_output"
    else:
        info.status = "completed"
    # "Reported completion" = Junie exited normally and produced a final result without errors.
    # The CLI has no explicit "task solved" flag; whether the migration *succeeded* is
    # decided only by the common evaluator.
    info.agent_reported_completion = info.status == "completed" and bool(info.result_excerpt)

    info.session_verification = verify_session(cfg, info.session_id, project_dir, run_dir,
                                               proc.started_at)
    _verify_from_usage(cfg, info)
    return info


def _verify_from_usage(cfg: ExperimentConfig, info: JunieRunInfo) -> None:
    """Fallback model check from llmUsage in the JSON output.

    Non-interactive sessions (observed with 26.11.2) contain no launch model/effort
    events. Junie also makes small auxiliary calls with other models (e.g. gpt-6-luna,
    gpt-5.4-nano), so the check is: the configured model is present and accounts for
    the largest share of cost. Effort is not reported anywhere -> stays unverified.
    """
    sv = info.session_verification
    usage = info.llm_usage if isinstance(info.llm_usage, list) else []
    by_model = {u.get("model"): u for u in usage if isinstance(u, dict) and u.get("model")}
    total = sum(float(u.get("cost") or 0) for u in by_model.values())
    sv["usage_by_model"] = {m: {"calls": u.get("calls"), "cost": u.get("cost"),
                                "output_tokens": u.get("outputTokens"),
                                "cost_share": round(float(u.get("cost") or 0) / total, 4) if total else None}
                            for m, u in by_model.items()}
    if sv.get("model_verified") is None and by_model:
        dominant = max(by_model, key=lambda m: float(by_model[m].get("cost") or 0))
        sv["model_verified"] = dominant == cfg.junie.model
        sv["model_verified_by"] = "llmUsage (configured model has the largest cost share)"
    if sv.get("effort_verified") is None:
        sv["effort_note"] = ("effort is not reported by Junie in non-interactive mode; "
                             f"it was passed as --effort={cfg.junie.reasoning_effort}")


def _load_json(p: Path) -> Any:
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None


def _walk(o: Any) -> Iterator[dict]:
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from _walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v)


def _find_session_dir(cfg: ExperimentConfig, session_id: str | None, project_dir: Path,
                      started_at: str) -> Path | None:
    sessions = cfg.junie.home / "sessions"
    if not sessions.is_dir():
        return None
    if session_id:
        sid = session_id.removeprefix("junie://sessions/")
        for cand in (sessions / sid, sessions / f"session-{sid}"):
            if cand.is_dir():
                return cand
        matches = [d for d in sessions.iterdir() if d.is_dir() and sid in d.name]
        if len(matches) == 1:
            return matches[0]
    # Fallback: newest session, modified after this run started, whose events mention
    # this project directory (run directories are unique, so this is unambiguous).
    start_epoch = datetime.fromisoformat(started_at).timestamp()
    candidates = sorted((d for d in sessions.iterdir()
                         if (d / "events.jsonl").is_file()
                         and (d / "events.jsonl").stat().st_mtime >= start_epoch),
                        key=lambda d: d.stat().st_mtime, reverse=True)[:20]
    needle = str(project_dir)
    for d in candidates:
        try:
            with (d / "events.jsonl").open(encoding="utf-8", errors="replace") as fh:
                if any(needle in line for line in fh):
                    return d
        except OSError:
            continue
    return None


def verify_session(cfg: ExperimentConfig, session_id: str | None, project_dir: Path,
                   run_dir: Path, started_at: str) -> dict:
    """Check model/effort Junie actually launched with; archive the session events."""
    result: dict = {"session_dir": None, "launch_model": None, "launch_effort": None,
                    "model_verified": None, "effort_verified": None,
                    "main_agent_models": {}, "auxiliary_models": {}, "note": None}
    d = _find_session_dir(cfg, session_id, project_dir, started_at)
    if d is None:
        result["note"] = "session not found; model/effort could not be verified"
        return result
    result["session_dir"] = str(d)
    launch_models, launch_efforts = set(), set()
    with (d / "events.jsonl").open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            for node in _walk(ev):
                kind = node.get("kind")
                if kind == "ModelForLaunchAttachment" and node.get("modelId"):
                    launch_models.add(node["modelId"])
                elif kind == "EffortForLaunchAttachment" and node.get("effort"):
                    launch_efforts.add(node["effort"])
                elif kind == "LlmResponseMetadataEvent":
                    agent_kind = (node.get("agent") or {}).get("kind")
                    bucket = result["main_agent_models"] if agent_kind == "MainAgent" else result["auxiliary_models"]
                    for usage in node.get("modelUsage") or []:
                        m = usage.get("model")
                        if m:
                            bucket[m] = bucket.get(m, 0) + 1
    result["launch_model"] = sorted(launch_models) or None
    result["launch_effort"] = sorted(launch_efforts) or None
    if launch_models:
        result["model_verified"] = launch_models == {cfg.junie.model}
    if launch_efforts:
        result["effort_verified"] = launch_efforts == {cfg.junie.reasoning_effort}
    if not launch_models or not launch_efforts:
        result["note"] = "launch model/effort events not found in session; recorded as unverified"
    archive = run_dir / "junie-session"
    archive.mkdir(exist_ok=False)
    for name in ("events.jsonl", "transcript.md", "summary.json", "state.json"):
        if (d / name).is_file():
            shutil.copy2(d / name, archive / name)
    return result
