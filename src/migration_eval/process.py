"""Safe, logged subprocess execution (no shell, timeouts kill the whole process group)."""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

# Environment variables whose values must never be written to logs.
SECRET_ENV_HINTS = ("TOKEN", "KEY", "SECRET", "PASSWORD", "AUTH")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class ProcessResult:
    command: list[str]
    cwd: str
    exit_code: int | None        # None if the process did not run or was killed on timeout
    timed_out: bool
    started_at: str
    finished_at: str
    duration_seconds: float
    stdout_path: str | None
    stderr_path: str | None
    dry_run: bool = False
    error: str | None = None     # e.g. executable not found

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and self.error is None


class CommandRunner:
    """Executes commands, logging each one *before* it runs.

    `log_file` (optional) receives an append-only record of every command, which is
    stored next to the run results.
    """

    def __init__(self, dry_run: bool = False, log_file: Path | None = None, echo: bool = True):
        self.dry_run = dry_run
        self.log_file = log_file
        self.echo = echo

    def _log(self, line: str) -> None:
        if self.echo:
            print(line, file=sys.stderr, flush=True)
        if self.log_file is not None and not self.dry_run:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            with self.log_file.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    def run(
        self,
        cmd: Sequence[str],
        cwd: Path,
        *,
        stdout_path: Path | None = None,
        stderr_path: Path | None = None,
        env: Mapping[str, str] | None = None,
        env_overrides_for_log: Mapping[str, str] | None = None,
        timeout: float | None = None,
        always_run: bool = False,
    ) -> ProcessResult:
        """Run `cmd`. stdout/stderr go to the given files (created exclusively, so an
        existing log is never overwritten); without a path the stream is discarded.

        `always_run=True` executes even in dry-run mode; only for read-only probes
        such as `--version`.
        """
        cmd = [str(c) for c in cmd]
        overrides = " ".join(f"{k}={_redact(k, v)}" for k, v in (env_overrides_for_log or {}).items())
        prefix = "[dry-run] " if self.dry_run and not always_run else ""
        self._log(f"{prefix}[{utc_now()}] $ (cd {shlex.quote(str(cwd))} && "
                  f"{(overrides + ' ') if overrides else ''}{shlex.join(cmd)})"
                  + (f"  [timeout={int(timeout)}s]" if timeout else ""))

        started = utc_now()
        if self.dry_run and not always_run:
            return ProcessResult(cmd, str(cwd), None, False, started, started, 0.0,
                                 _s(stdout_path), _s(stderr_path), dry_run=True)

        t0 = time.monotonic()
        opened = []
        for p in (stdout_path, stderr_path):
            if p is not None:
                p.parent.mkdir(parents=True, exist_ok=True)
                opened.append(p.open("xb"))
            else:
                opened.append(None)
        out_fh = opened[0] if opened[0] is not None else subprocess.DEVNULL
        err_fh = opened[1] if opened[1] is not None else subprocess.DEVNULL
        timed_out, exit_code, error = False, None, None
        try:
            proc = subprocess.Popen(
                cmd, cwd=cwd, env=dict(env) if env is not None else None,
                stdin=subprocess.DEVNULL, stdout=out_fh, stderr=err_fh,
                start_new_session=True,  # own process group so a timeout kills children too
            )
            try:
                exit_code = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_group(proc)
        except FileNotFoundError as e:
            error = f"executable not found: {e.filename or cmd[0]}"
        except OSError as e:
            error = f"failed to start process: {e}"
        finally:
            for fh in opened:
                if fh is not None:
                    fh.close()
        result = ProcessResult(cmd, str(cwd), exit_code, timed_out, started, utc_now(),
                               round(time.monotonic() - t0, 3), _s(stdout_path), _s(stderr_path),
                               error=error)
        self._log(f"[{result.finished_at}] -> exit={exit_code} timed_out={timed_out} "
                  f"duration={result.duration_seconds}s" + (f" error={error}" if error else ""))
        return result

    def capture(self, cmd: Sequence[str], cwd: Path, env: Mapping[str, str] | None = None,
                timeout: float = 120, check: bool = False) -> tuple[int | None, str]:
        """Run a short read-only command and return (exit_code, combined output).

        Used for probes (versions, git queries). Always executes, even in dry-run mode.
        """
        cmd = [str(c) for c in cmd]
        self._log(f"[{utc_now()}] $ (cd {shlex.quote(str(cwd))} && {shlex.join(cmd)})")
        try:
            cp = subprocess.run(cmd, cwd=cwd, env=dict(env) if env is not None else None,
                                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                timeout=timeout, errors="replace")
        except FileNotFoundError:
            if check:
                raise
            return None, f"executable not found: {cmd[0]}"
        except subprocess.TimeoutExpired:
            if check:
                raise
            return None, f"timed out after {timeout}s"
        output = (cp.stdout or "") + (cp.stderr or "")
        if check and cp.returncode != 0:
            raise subprocess.CalledProcessError(cp.returncode, cmd, output)
        return cp.returncode, output


def _kill_group(proc: subprocess.Popen) -> None:
    for sig, wait in ((signal.SIGTERM, 15), (signal.SIGKILL, 15)):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


def _redact(key: str, value: str) -> str:
    return "<redacted>" if any(h in key.upper() for h in SECRET_ENV_HINTS) else shlex.quote(value)


def _s(p: Path | None) -> str | None:
    return str(p) if p else None


def java_env(java_home: Path, maven_local_repo: Path | None = None,
             base: Mapping[str, str] | None = None) -> tuple[dict[str, str], dict[str, str]]:
    """Environment with JAVA_HOME/PATH pointing at `java_home`.

    Returns (full_env, overrides) where `overrides` is what differs from the base
    environment (logged for reproducibility).
    """
    env = dict(base if base is not None else os.environ)
    overrides = {
        "JAVA_HOME": str(java_home),
        "PATH": f"{java_home / 'bin'}{os.pathsep}{env.get('PATH', '')}",
    }
    if maven_local_repo is not None:
        # MAVEN_OPTS is honoured by mvn for the local repository location.
        opts = env.get("MAVEN_OPTS", "")
        overrides["MAVEN_OPTS"] = f"{opts} -Dmaven.repo.local={maven_local_repo}".strip()
    env.update(overrides)
    log_overrides = {k: v for k, v in overrides.items() if k != "PATH"}
    log_overrides["PATH"] = f"{java_home / 'bin'}:$PATH"
    return env, log_overrides
