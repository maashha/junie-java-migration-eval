# junie-java-migration-eval

Experiment harness for a Bachelor's thesis on **code migration with AI coding agents**.
It compares a general-purpose coding agent, **JetBrains Junie**, with the rule-based
**OpenRewrite** on **Java 8 → Java 17** migration, using projects from
[MigrationBench](https://huggingface.co/datasets/AmazonScience/migration-bench-java-selected).

| Condition | Tool | Configuration (fixed for all projects) |
|---|---|---|
| Junie only | Junie CLI | model **GPT-6.1 Sol** (`--model=gpt-6.1-sol`), reasoning effort **medium** (`--effort=medium`), one fixed prompt (`prompts/java8-to-java17.txt`) |
| OpenRewrite only | `rewrite-maven-plugin` 6.46.1 | recipe `org.openrewrite.java.migrate.UpgradeToJava17` from `rewrite-migrate-java` 3.42.1 |
| *(planned)* OpenRewrite → Junie | — | not implemented yet; the pipeline gives each method its own fresh checkout, so a hybrid method can be added as a third method in `pipeline.py` |

Both methods start from **the same MigrationBench `base_commit`**, each in a fresh,
isolated copy, and are evaluated by **the same evaluator** under JDK 17.

> **Junie is non-deterministic.** The same prompt, model and commit can produce
> different migrations. `experiment.repetitions` (or `--repetitions`) runs each
> (project, method) several times independently, which can later be used to measure
> stability. Every repetition starts from `base_commit`, never from a previous run.

---

## Requirements

- Linux (primary target; macOS works for development), Python ≥ 3.11, `git`
- **JDK 8** (baseline validation of the original project) and **JDK 17** (migration tools + evaluation)
- **Apache Maven 3.9.x** — pin one version for the whole experiment (see *Known limitations*)
- **Junie CLI** (`curl -fsSL https://junie.jetbrains.com/install.sh | bash`) and a Junie API token
  from <https://junie.jetbrains.com/cli>, exported as `JUNIE_API_KEY`
- Network access (git clone, Maven Central, Junie)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'          # only runtime dependency: PyYAML
pytest -q                        # unit tests (no network / Maven / Junie needed)

export JAVA8_HOME=/usr/lib/jvm/java-8-openjdk-amd64
export JAVA17_HOME=/usr/lib/jvm/java-17-openjdk-amd64
export JUNIE_API_KEY=perm-...    # read by Junie from the environment; never put on the command line
```

## Configuration

- `config/projects.yaml` — projects: `name`, `repo`, `base_commit` (full SHA from
  MigrationBench), optional `group` (small/medium/large) and `timeout_minutes`.
  `python scripts/lookup_migrationbench.py owner/repo` prints a ready-to-paste entry.
- `config/experiment.yaml` — model/effort, Java versions, timeouts, repetitions,
  `work_dir`/`results_dir`, JDK paths, Maven executable and validation command
  (default `mvn -B -ntp clean test`), OpenRewrite versions.
- `prompts/java8-to-java17.txt` — the single Junie prompt, identical for every project.

## Workflow

```text
prepare  →  baseline (original, JDK 8)  →  OpenRewrite run → evaluate (JDK 17)
                                        →  Junie run       → evaluate (JDK 17)  →  summarize
```

| Script | Purpose |
|---|---|
| `scripts/prepare.py` | Bare canonical clone (`work/<p>/canonical.git`), pristine checkout `work/<p>/original`, verification (HEAD == base_commit, clean tree), metadata JSON. `--validate-baseline` also runs the baseline. |
| `scripts/run_experiment.py` | Main entry point. `--method baseline\|openrewrite\|junie\|all`, `--repetitions N`, `--dry-run`. Runs a baseline automatically if none exists; skips projects with an invalid baseline. Strictly sequential. Asks for confirmation before more than one Junie run (`--yes` to skip). |
| `scripts/run_openrewrite.py`, `scripts/run_junie.py` | One migration run of a single method (same pipeline as above). |
| `scripts/evaluate.py` | Re-run the common evaluator on an existing run (`--run results/<p>/<m>/run-NN`) into a new `re-evaluation-<UTC>/` folder. |
| `scripts/summarize.py` | CSV + Markdown summary, Junie vs OpenRewrite side by side, in `results/_summaries/<UTC>/`. |
| `scripts/lookup_migrationbench.py` | Look up `base_commit` and size data for a MigrationBench repo. |

### One-project pilot

```bash
python scripts/run_experiment.py -p tinify-java --method all --dry-run   # inspect every command first
python scripts/prepare.py -p tinify-java
python scripts/run_experiment.py -p tinify-java --method baseline        # must report baseline_valid=True
python scripts/run_experiment.py -p tinify-java --method openrewrite
python scripts/run_experiment.py -p tinify-java --method junie           # costs Junie credits, up to 30 min
python scripts/summarize.py
```

Then inspect `results/tinify-java/{baseline,openrewrite,junie}/run-01/`.

## What each run stores

`results/<project>/<method>/run-NN/` (created exclusively; never overwritten):

| File | Content |
|---|---|
| `result.json` | flat result record (schema below) |
| `metadata.json` | repository metadata (URL, commit, git status, checkout path), environment (OS, Python, git, both JDKs, Maven, Junie version, OpenRewrite versions), full migration and evaluation details, prompt text + SHA-256 |
| `commands.log` | every command, logged *before* execution, with timestamps, exit codes, durations |
| `migration.stdout.log`, `migration.stderr.log` | Junie / OpenRewrite output |
| `git.diff` | full diff vs `base_commit` (incl. new files and commits made by the tool; `**/target/**` excluded), taken *before* evaluation |
| `validation.stdout.log`, `validation.stderr.log` | Maven validation build |
| `validation.effective-pom.xml` | effective POM (Java target signal) |
| `junie-output.json`, `junie-session/` | Junie JSON output and archived session (`events.jsonl`, transcript) |
| `inputs/` | copy of `experiment.yaml` and the prompt used |

Working copies stay in `work/<project>/<method>/run-NN/` for inspection. The harness
never deletes directories; clean up `work/` manually when you no longer need it.

### `result.json` (main fields)

```json
{
  "project": "tinify-java", "method": "junie", "run_id": 1, "base_commit": "d9cc…",
  "model": "gpt-6.1-sol", "model_display_name": "GPT-6.1 Sol", "reasoning_effort": "medium",
  "model_verified": true, "effort_verified": true,
  "baseline_valid": true, "baseline_tests_total": 91,
  "migration_status": "completed", "migration_duration_seconds": 0, "migration_timed_out": false,
  "agent_reported_completion": true,
  "build_success": false, "tests_total": 0, "tests_passed": 0, "tests_failed": 0,
  "tests_errors": 0, "tests_skipped": 0, "test_count_not_reduced": false,
  "java17_verified": false, "java_config_levels": [17], "class_major_versions": {"61": 16},
  "changed_files": 0, "lines_added": 0, "lines_removed": 0, "modified_files": [],
  "test_files_changed": 0, "test_files_deleted": 0,
  "full_success": false, "failure_category": null, "failure_evidence": null
}
```

OpenRewrite results use the same schema; `model`, `reasoning_effort` and the other
Junie-only fields are `null`.

## Definitions

**Baseline valid**: the original project, checked out at `base_commit`, passes the
validation command under JDK 8. Projects without a valid baseline get
`failure_category = baseline_invalid`; migrations are not run for them by default.
When they are run anyway (`--allow-invalid-baseline`), the summary leaves them out of
every success rate and counts them separately.

**Full migration success** (primary metric):

```text
build_success == true AND tests_failed == 0 AND tests_errors == 0 AND java17_verified == true
```

evaluated with JDK 17. **Strict success** also requires
`tests_total >= baseline_tests_total`, which catches tests that were deleted or
skipped.

**`java17_verified`** needs both signals to agree:
1. *Configuration*: the effective POM (parents and properties resolved) declares
   level 17 for at least one module and a different level for none. The effective
   level is compiler-plugin `release` > plugin `source`/`target` >
   `maven.compiler.release` > `maven.compiler.source`/`target`. If `source` and
   `target` disagree, the level does not count as 17.
2. *Bytecode*: every main `.class` file compiled **by this evaluation** has major
   version 61. Stale class files from earlier builds are ignored by timestamp.

Toolchains-plugin JDK versions are recorded but not required.

**Test counts** come from Surefire/Failsafe XML reports written during the
evaluation. Older reports, for example from the agent's own builds, are ignored. If
there are no reports, the counts are parsed from Maven's per-module summary lines.

**`failure_category`** (machine-assisted; the evidence line and raw logs are kept for
manual review). Precedence: success → `null`; then `timeout`; then `agent_failure`
(Junie error or no output); then `tool_failure`, `unsupported_build_tool` or
`harness_error`; then the evaluator's classification from the Maven log, in this
order: `build_configuration`, `dependency`, `compilation`, `tests`,
`plugin_incompatibility`. If the build succeeded but the target is not 17, the
category is `java_target_not_met`. Anything else is `unknown`.
`evaluation_failure_category` always holds the evaluator's own classification.

## Reproducibility measures

- Exact `base_commit`, verified after checkout (HEAD and a clean tree). Each run gets
  an independent `git clone --no-hardlinks` with the `origin` remote removed, so an
  agent cannot alter the canonical clone (worktrees would share refs).
- Model and effort are always passed explicitly. This matters: the local Junie
  defaults were a different model (`modelForLaunch`), and the saved effort for
  `gpt-6.1-sol` was `xhigh`. After each run, the Junie session events are read back
  to check the model and effort that were actually launched (`model_verified`,
  `effort_verified`).
- The pinned model id is `gpt-6.1-sol`, not an alias such as `gpt-sol`, which can
  point to a newer model later.
- All `JUNIE_*` environment variables except `JUNIE_API_KEY`/`JUNIE_HOME` are removed
  from Junie's environment. For example, `JUNIE_SYSTEM_PROMPT` would otherwise change
  the condition without anyone noticing.
- Junie runs with `JAVA_HOME` set to JDK 17, so its own builds use the target JDK.
  OpenRewrite also runs on JDK 17.
- Pinned OpenRewrite versions (no `RELEASE`/`LATEST`, no `-U`) and a pinned
  `maven-help-plugin`.
- No retries. A failure at any step is written to `result.json`.

## Known limitations / manual steps

1. **Junie authentication.** Non-interactive runs need `JUNIE_API_KEY` or stored
   credentials. Without them Junie prints `{"errors":[…]}` **and exits 0**, so the
   harness parses the JSON output and does not rely on the exit code.
2. **Junie self-updates.** A staged update was applied on start even with
   `--skip-update-check` (observed 26.10.27 → 26.11.2). The version is recorded
   before and after each run (`junie_version_changed_during_run`). Keep the Junie
   version fixed for the whole experiment and check it before you start.
3. **Junie JSON output schema is undocumented.** `sessionId`, `result` and `errors` are
   used as observed. "Reported completion" means the run ended normally with a final
   result. It is not a claim of success; only the evaluator decides that.
4. **No Junie timeout flag.** The harness enforces the wall-clock limit and kills the
   process group, so the agent gets no chance to wrap up gracefully.
5. **Isolation flags not verified.** `--mcp-default-locations`,
   `--skill-default-locations` and similar exist, but their accepted value format
   could not be confirmed without an authenticated run. They are off by default.
   User-level MCP servers, skills or agents in `~/.junie` can therefore influence
   Junie. Check `~/.junie`, or add the flags to `junie.extra_args` once verified.
6. **The Maven version changes the outcome.** Maven 3.10.1 rejects the tinify-java
   POM at its base commit (duplicate `maven-dependency-plugin` declaration); Maven
   3.9.16 accepts it. Pin one Maven 3.9.x for baseline and evaluation, and record it.
7. **OpenRewrite compiles the original first.** `rewrite:run` forks the lifecycle to
   `process-test-classes`, so the *original* project must compile under JDK 17. If it
   doesn't, the run is recorded as `tool_failure`.
8. **Maven only.** MigrationBench is Maven-only. Gradle projects are recorded as
   `unsupported_build_tool`.
9. **No deprecated-API metric.** There is no reliable universal measure, so none is
   reported. Migration completeness = Java target (config + bytecode) + compilation
   under JDK 17 + tests.
10. **Shared Maven local repository.** By default `~/.m2` is shared. Set
    `maven.local_repository` for a separate one, but note that cache state affects
    timing.
11. **The summary uses the latest baseline per project.** Each migration result
    still records the baseline it ran against (`baseline_run`).

## Repository layout

```text
config/          experiment.yaml, projects.yaml
prompts/         java8-to-java17.txt (fixed Junie prompt)
scripts/         CLI entry points (see table above)
src/migration_eval/
  config.py        config loading/validation        process.py   logged subprocess runner (no shell, timeouts)
  git.py           canonical clone, isolated copies, diff   environment.py  tool/OS versions
  junie.py         Junie CLI runner + session check  openrewrite.py  OpenRewrite runner
  evaluator.py     common evaluator + failure classes java_target.py  Java 17 verification
  pipeline.py      one run end-to-end               results.py   layout, schema, success definition
  summary.py       aggregation
tests/           unit tests
work/            clones and working copies (git-ignored)
results/         run results (git-ignored)
```

## Time to success and cost

`time_to_success_seconds` is the migration runtime of a run with full success (empty
otherwise). `llm_cost_usd` is the Junie cost from `llmUsage`; OpenRewrite costs 0.

## Unmodified tests are part of success

A run only counts as `full_success` if no file under `src/test/` was added, deleted,
renamed or changed in content (`test_files_modified == 0`, category `tests_modified`
otherwise). Changes of whitespace / line endings only are ignored
(`test_files_whitespace_only`). Newly added test classes (new files named
`*Test`, `*Tests`, `*TestCase`, `*IT`, `Test*` under `src/test/java|kotlin|groovy`) are
allowed and reported as `test_classes_added`; new test methods inside an existing test
file, new test resources or helper/config files still count as modifications. Older results are re-checked from their `git.diff`
when the summary is built. This also rejects test edits that are needed for a
dependency upgrade (e.g. Mockito imports); `modified_test_files` lists them for review.
