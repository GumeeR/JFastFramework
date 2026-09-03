"""`jfast check`: every *static* check this framework knows, one screen, one exit code.

The checks already existed. What did not exist was a single thing to run, so
CI ran three of them, an agent ran whichever one it remembered, and the two
nobody wired up never ran at all. This command is the answer to "is this
repository consistent with itself", and it belongs in a pipeline -- next to the
tools below, never instead of them.

Against ruff, mypy and pytest
    It runs none of them, and that is a decision rather than an omission. Every
    check here reads files and answers in milliseconds; `pytest` executes your
    code, for an unbounded time, against whatever a fixture decides to start,
    and `mypy` in a tree whose dependencies are not installed reports missing
    imports that the project's own configuration would have silenced. Either
    one inside this command turns a pre-commit hook into a build, and a checker
    that manufactures findings is muted within a week.

    What that costs is a name that promises more than it delivers, so the
    command pays it back explicitly: :data:`NOT_COVERED` is printed under every
    run and carried in `--json`, because the failure this guards against is a
    team reading "check", deleting its own verification script, and losing lint,
    types and tests without a line of output to say so.

Against `doctor`
    `doctor` asks a question about *your machine*: does the configuration
    resolve, does every enabled plugin import on this interpreter. Its answer
    changes when you change virtualenv and never when you change a line of
    code. `check` asks a question about *the repository*: is what is committed
    here consistent with itself. It includes `doctor`'s two questions because a
    broken install makes every downstream answer a lie -- but `doctor` stays,
    because when you are debugging your own laptop you want the two-second
    answer and not the whole battery.

Against `workspace validate`
    That one reads a single file, the workspace resource graph, and is scoped
    to a workspace rather than to a service. `check` runs it as one of six.

Nothing here starts a container, opens a socket or talks to a database. Every
check is static, so the command runs in a pre-commit hook and gives the same
answer on a laptop with nothing installed as it does in CI.
"""

from __future__ import annotations

import contextlib
import io
import json as jsonlib
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

import typer

from jfastframework import contracts as contracts_api
from jfastframework import project as project_model
from jfastframework.cli import insight, ui
from jfastframework.cli.exits import MEANING, Code
from jfastframework.project import SEVERITY_ORDER, Finding
from jfastframework.settings import DEFAULT_CONFIG_FILE, JFastConfig
from jfastframework.workspace import Workspace

__all__ = [
    "CHECKS",
    "CODE_FOR",
    "NOT_COVERED",
    "PRECEDENCE",
    "CheckResult",
    "build_error",
    "payload",
    "register",
    "render",
    "run",
    "skipped",
    "worst_code",
]

SCHEMA_VERSION = "1"

#: Run order, which is also display order: cause before effect. Everything
#: below `config` is meaningless if `config` did not load, and the name of each
#: one is the name of the command that already ran it -- there is no second
#: vocabulary to learn.
CHECKS: tuple[str, ...] = ("config", "plugins", "analyze", "contracts", "migrations", "deploy")

#: What a failure of each check means to a script. One code per check, so the
#: number is decidable from the name and never from the wording of a message.
CODE_FOR: dict[str, Code] = {
    "config": Code.CONFIG,
    "plugins": Code.ENVIRONMENT,
    "analyze": Code.VALIDATION,
    "contracts": Code.CONTRACT,
    "migrations": Code.MIGRATION,
    "deploy": Code.VALIDATION,
}

#: Which code survives when several checks fail, worst first.
#:
#: Not by severity -- by how much of the report the failure invalidates. A
#: config that does not parse makes every other answer a guess, so it comes
#: first. A plugin that will not import is next for the same reason. Then
#: migrations, because a schema the code does not agree with fails in
#: production at the first query and is the one result you must not ship past;
#: a contract violation, by contrast, is visible in the diff and fails safely.
#: Contract before validation because a contract is a rule somebody wrote down,
#: and `analyze` findings are the broadest and least specific class -- last, so
#: any more informative code wins over it.
PRECEDENCE: tuple[Code, ...] = (
    Code.CONFIG,
    Code.ENVIRONMENT,
    Code.MIGRATION,
    Code.CONTRACT,
    Code.VALIDATION,
)

#: A skipped check under `--ci` reports as this. It is an environment error by
#: definition: the runner was missing something the check needed.
#:
#: It is deliberately outside :data:`PRECEDENCE` and applied last. A run that
#: both violates its contract and skipped a check has a concrete defect to
#: report, and exiting 3 there would tell CI "your runner is misconfigured"
#: about a project that is genuinely broken.
SKIP_CODE = Code.ENVIRONMENT

#: What `cli/migrations.py` has to expose for the migrations check to run.
#:
#: Its *functions*, never its Typer command: a command prints to stdout and
#: raises `typer.Exit`, and calling one from inside `--json` would put a tick
#: mark in front of the payload. That module is written and released
#: separately, so the absence of any of these is a skip with a reason and never
#: an exception.
MIGRATION_API = ("versions_dir", "load_revisions", "analyze_revision", "RowCounts")

FAIL_ON_LEVELS = (*SEVERITY_ORDER, "never")

#: What this command does **not** look at, what that would have caught, and the
#: command that does look. Printed under every run and carried in `--json`.
#:
#: The list is here because of a specific, measured failure: with an unused
#: import, a misformatted file, a `str` assigned to an `int` and a failing test
#: all present at once, `jfast check` produced output byte-identical to the
#: clean project and exited 0 -- `--ci` included. Nothing in the report was
#: false. What was false was the impression the name left, and a team that acts
#: on that impression loses four gates in one commit.
NOT_COVERED: tuple[tuple[str, str, str], ...] = (
    ("lint", "unused imports, undefined names, unreachable code", "ruff check ."),
    ("formatting", "a diff nobody agreed to review", "ruff format --check ."),
    ("types", "a str where an int was declared", "mypy ."),
    ("tests", "whether any of it works", "pytest"),
)

#: The one line that has to appear whether or not anyone reads the rest.
NOT_COVERED_LINE = "not checked here: " + ", ".join(name for name, _, _ in NOT_COVERED)


@dataclass(frozen=True)
class CheckResult:
    """What one check found, or why it could not look.

    ``reason`` is the whole point of the type. A check that did not run has no
    findings, which is indistinguishable from a check that ran and found
    nothing -- unless the reason it did not run is carried alongside.
    """

    name: str
    findings: tuple[Finding, ...] = ()
    reason: str | None = None
    detail: str = ""
    duration_ms: int = 0

    @property
    def ran(self) -> bool:
        return self.reason is None

    @property
    def code(self) -> Code:
        return CODE_FOR[self.name]

    def status(self, threshold: int) -> str:
        """``pass``, ``warn``, ``fail`` or ``skip`` at this failure threshold.

        ``warn`` is findings that exist and are below the threshold: reported,
        not fatal. Collapsing it into ``pass`` would hide them, and into
        ``fail`` would make `--fail-on` meaningless.
        """
        if not self.ran:
            return "skip"
        if not self.findings:
            return "pass"
        if threshold >= 0 and any(
            SEVERITY_ORDER.index(finding.severity) <= threshold for finding in self.findings
        ):
            return "fail"
        return "warn"

    def describe(self, threshold: int) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status(threshold),
            "reason": self.reason,
            "detail": self.detail,
            "exit_code": int(self.code),
            "duration_ms": self.duration_ms,
            "counts": insight.severity_counts(list(self.findings)),
            "findings": [finding.describe() for finding in self.findings],
        }


def skipped(name: str, reason: str, *, duration_ms: int = 0) -> CheckResult:
    """A check that could not run, and said so."""
    return CheckResult(name=name, reason=reason, duration_ms=duration_ms)


def _threshold(fail_on: str) -> int:
    return -1 if fail_on == "never" else SEVERITY_ORDER.index(fail_on)


def worst_code(results: Sequence[CheckResult], *, fail_on: str, strict: bool) -> int:
    """The one number the process exits with.

    ``strict`` makes a skip a failure too; it is what `--ci` turns on. A skip
    only ever decides the code when nothing else failed -- see :data:`SKIP_CODE`.
    """
    threshold = _threshold(fail_on)
    statuses = [result.status(threshold) for result in results]
    codes = {
        result.code for result, status in zip(results, statuses, strict=True) if status == "fail"
    }
    for candidate in PRECEDENCE:
        if candidate in codes:
            return int(candidate)
    if strict and "skip" in statuses:
        return int(SKIP_CODE)
    return int(Code.OK)


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------


@dataclass
class _State:
    """What one check resolved that the next one needs.

    Threaded explicitly rather than recomputed: `registry.discover()` imports
    every installed plugin, and it is by a wide margin the slowest thing this
    command does. Doing it once is the difference between a pre-commit hook and
    a coffee break.
    """

    config: JFastConfig | None = None
    instances: list[Any] | None = None
    known_plugins: frozenset[str] | None = None
    blocked: str | None = None
    """Why the plugin-dependent checks cannot run, if they cannot."""
    findings: dict[str, tuple[Finding, ...]] = field(default_factory=dict)


def _timed(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _config_check(root: Path, config_path: str, state: _State) -> CheckResult:
    started = time.perf_counter()
    source = root / config_path
    try:
        state.config = JFastConfig.load(config_path=source)
    except Exception as exc:  # noqa: BLE001 -- pydantic, tomllib and os all reach here
        state.blocked = f"{config_path} did not load"
        return CheckResult(
            name="config",
            findings=(
                Finding(
                    severity="critical",
                    code="config-unreadable",
                    message=f"{config_path} did not load: {exc}",
                    why=(
                        "Nothing downstream of this is trustworthy: the plugin graph, the "
                        "compose file and the settings all come from here. Fix the file, "
                        "then run this again."
                    ),
                    path=config_path,
                ),
            ),
            duration_ms=_timed(started),
        )
    settings = state.config.settings
    return CheckResult(
        name="config",
        detail=f"{settings.app_name} ({settings.env})",
        duration_ms=_timed(started),
    )


def _plugins_check(root: Path, state: _State) -> CheckResult:
    started = time.perf_counter()
    if state.config is None:
        return skipped("plugins", state.blocked or "the configuration did not load")

    from jfastframework.plugins import registry

    findings: list[Finding] = []
    # `[plugins.paths]`: plugins that live in this project rather than in a
    # wheel. Discovering without them would make `analyze` call a plugin
    # declared three lines above the list that reads it "not installed" --
    # `check` runs both checks together, so the two have to see one set.
    declared: dict[str, str] = state.config.raw.get("plugins", {}).get("paths", {}) or {}
    available = registry.discover(extra_paths=declared, search_path=root)
    broken: dict[str, str] = getattr(registry.discover, "broken", {})
    # `cli/main.py::_known_plugins` to the letter, because `analyze` is handed
    # this set and has to answer here exactly what it answers on its own. A
    # dotted path that does not import is a name nothing provides, however
    # confidently jfast.toml names it; only an installed distribution earns the
    # "broken, not missing" reading.
    state.known_plugins = frozenset(available) | frozenset(
        name for name in broken if name not in declared
    )

    for name in state.config.settings.plugins:
        if name in broken:
            findings.append(
                Finding(
                    severity="critical",
                    code="plugin-unimportable",
                    message=f"plugin {name!r} is enabled but cannot import: {broken[name]}",
                    why=(
                        "The app refuses to start. Install the extra that provides it "
                        "(`jfast add --list`), or drop the name from [plugins].enabled."
                    ),
                    path=DEFAULT_CONFIG_FILE,
                )
            )

    try:
        state.instances = registry.build(state.config)
    except Exception as exc:  # noqa: BLE001 -- a plugin's own import errors land here
        findings.append(
            Finding(
                severity="critical",
                code="plugin-graph-unresolved",
                message=f"the plugin graph does not resolve: {exc}",
                why=(
                    "Every plugin is loaded before the first request, so this is a startup "
                    "failure, not a runtime one. `jfast plugins list --all` shows what is "
                    "installed and what failed to import."
                ),
                path=DEFAULT_CONFIG_FILE,
            )
        )
    else:
        # Resolving the graph builds the plugins; registering them is what
        # reads their settings, and that is where a missing jwks_url or an
        # unusable signing key is found. Without this the suite passes on a
        # service whose very first import raises.
        findings.extend(_registration_findings(state))
        findings.extend(_session_store_findings(state))

    count = len(state.instances or ())
    return CheckResult(
        name="plugins",
        findings=tuple(findings),
        detail=f"{count} enabled, {len(available)} installed",
        duration_ms=_timed(started),
    )


def build_error(config: Any) -> str | None:
    """What ``create_app`` raises on this configuration, or None if it builds.

    Deliberately builds rather than duplicating each plugin's rules here: a
    copy of the validation is a copy that drifts, and the question being asked
    is exactly "would `create_app` have worked". The lifespan is not entered,
    so nothing connects to anything and this still answers offline.

    Building a service inside a CLI process is not free of side effects: the
    observability plugin replaces the root logger's handlers with one writing
    to stdout, which is both a stray log line in the middle of a report and --
    under ``--json`` -- a second document on a stream that promised one. So
    the build happens with logging off and the streams captured, and the root
    logger is put back the way it was found.
    """
    from jfastframework.app import create_app

    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    sink = io.StringIO()
    logging.disable(logging.CRITICAL)
    try:
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            create_app(config=config)
        return None
    except Exception as exc:  # noqa: BLE001 -- a plugin may raise anything
        return f"{type(exc).__name__}: {exc}"
    finally:
        logging.disable(logging.NOTSET)
        root.handlers, root.level = saved_handlers, saved_level


def _registration_findings(state: _State) -> list[Finding]:
    """The build failure as a finding, if there is one."""
    error = build_error(state.config)
    if error is None:
        return []
    return [
        Finding(
            severity="critical",
            code="service-unbuildable",
            message=f"the service cannot be built: {error}",
            why=(
                "This is what `import main` does, so the service does not start at "
                "all -- not on the first request, on the first import. Plugins "
                "validate their settings as they register, which is after the graph "
                "resolves."
            ),
            path=DEFAULT_CONFIG_FILE,
        )
    ]


def _session_store_findings(state: _State) -> list[Finding]:
    """Auth minting tokens with nowhere shared to record them.

    The plugin refuses to register in production, which is where it matters --
    but a service is developed with ``env = "local"``, and the boot that fails
    is then the deployment. Reported here whatever the environment says,
    because the environment in the file is not the environment it ships with.

    Read off the configuration rather than the built plugins: what is wrong is
    a pair of settings, and asking the instances would mean registering them,
    which is the thing that refuses.
    """
    if state.config is None:
        return []
    enabled = set(state.config.settings.plugins)
    if "auth" not in enabled or "cache" in enabled:
        return []
    auth_config = state.config.raw.get("plugin", {}).get("auth", {})
    if not auth_config.get("issue_tokens", False):
        return []
    return [
        Finding(
            severity="high",
            code="session-store-per-process",
            message="auth issues tokens and no shared store records them: enable 'cache'",
            why=(
                "The generated image runs one worker per CPU, and an in-memory store "
                "is per process: a logout applies to the worker that served it, and a "
                "refresh reaching any other worker is refused as revoked -- three "
                "times in four on four cores. The service refuses to start in "
                "production for this reason, so the deployment is where it would be "
                'found. Add "cache" to [plugins].enabled, or set issue_tokens = '
                "false if this service only verifies tokens minted elsewhere."
            ),
            path=DEFAULT_CONFIG_FILE,
        )
    ]


def _analyze_check(root: Path, state: _State) -> CheckResult:
    started = time.perf_counter()
    project = project_model.load(root)
    findings = project_model.analyze(project, known_plugins=state.known_plugins)
    return CheckResult(
        name="analyze",
        findings=tuple(findings),
        detail=f"{_plural(len(project.modules), 'module')}, "
        f"{_plural(len(project.shared_files), 'shared file')}",
        duration_ms=_timed(started),
    )


def _contracts_check(root: Path) -> CheckResult:
    started = time.perf_counter()
    source = root / contracts_api.CONTRACTS_FILE
    # Deliberately not `Contract.find()`. That walks upward, which is right for
    # `jfast contracts check` run from inside a module and wrong here: `check`
    # is anchored to one project root, and silently checking a parent's
    # contract against this project would be a finding nobody could explain.
    if not source.is_file():
        return skipped(
            "contracts",
            f"no {contracts_api.CONTRACTS_FILE} in {root} (jfast contracts init)",
            duration_ms=_timed(started),
        )
    try:
        contract = contracts_api.Contract.load(source)
    except Exception as exc:  # noqa: BLE001 -- tomllib and the model both reach here
        return CheckResult(
            name="contracts",
            findings=(
                Finding(
                    severity="critical",
                    code="contract-unreadable",
                    message=f"{contracts_api.CONTRACTS_FILE} did not load: {exc}",
                    why="The rules cannot be applied until the file that declares them parses.",
                    path=contracts_api.CONTRACTS_FILE,
                ),
            ),
            duration_ms=_timed(started),
        )

    violations = contracts_api.check(contract, root)
    findings = tuple(
        Finding(
            severity="high",
            code=violation.rule,
            message=violation.message,
            why=violation.why or "Fix it, or waive it inline with `# contracts: allow <reason>`.",
            path=violation.path,
            line=violation.line,
        )
        for violation in violations
    )
    return CheckResult(
        name="contracts",
        findings=findings,
        detail=f"{contract.project}: {len(contract.layers)} layers",
        duration_ms=_timed(started),
    )


def _unavailable(root: Path) -> tuple[tuple[Finding, ...], str | None, str]:
    """The migrations check, with the sibling command taken as absent."""
    return (), "jfast migration check is not available in this build", ""


def _migration_findings(root: Path) -> tuple[tuple[Finding, ...], str | None, str]:
    """What `jfast migration check` sees here, or why it could not be asked.

    Imported defensively and called defensively: `cli/migrations.py` is a
    separate module with its own release schedule, and a battery that crashes
    because one of its members is not installed yet is a battery nobody adds
    to CI.

    No database, by the rule the whole command follows. `RowCounts.unknown()`
    is the conservative reading of that -- every table is treated as populated,
    so a rewrite is reported rather than assumed cheap. It also means every
    revision is read, applied or not: which are applied is a fact only the
    database has. `jfast migration check --dsn ...` is the narrower answer.
    """
    try:
        from jfastframework.cli import migrations
    except ImportError as exc:
        return (), f"jfast migration check is not installed here ({exc})", ""

    missing = [name for name in MIGRATION_API if not hasattr(migrations, name)]
    if missing:
        return (), f"cli/migrations.py does not expose {', '.join(missing)}", ""

    try:
        if migrations.versions_dir(root) is None:
            return (), f"no migrations/versions under {root}", ""
        rows = migrations.RowCounts.unknown()
        revisions = migrations.load_revisions(root)
        found = tuple(
            risk.finding
            for revision in revisions
            for risk in migrations.analyze_revision(revision, rows)
        )
    except Exception as exc:  # noqa: BLE001 -- any failure means we learned nothing
        return (), f"jfast migration check could not run: {exc}", ""

    # Coercing a foreign shape into a Finding would invent the `why` field,
    # which is the field an agent acts on. Refusing is the honest answer.
    if any(not isinstance(item, Finding) for item in found):
        return (), "jfast migration check returned something other than project.Finding", ""
    return found, None, f"{_plural(len(revisions), 'revision')} read, no database"


def _migrations_check(root: Path) -> CheckResult:
    started = time.perf_counter()
    found, reason, detail = _migration_findings(root)
    if reason is not None:
        return skipped("migrations", reason, duration_ms=_timed(started))
    return CheckResult(
        name="migrations",
        findings=found,
        detail=detail,
        duration_ms=_timed(started),
    )


def _deploy_check(root: Path, state: _State) -> CheckResult:
    """What can be said about the deployment without Docker.

    Which is everything except whether the images pull: the compose file is
    generated from the plugin graph, so a plugin claiming a port outside its
    block or a workspace binding a resource that does not exist are both
    decidable here, and both produce a file that is wrong in a way nobody
    notices until a container fails to start.
    """
    started = time.perf_counter()
    if state.config is None or state.instances is None:
        return skipped(
            "deploy",
            state.blocked or "the plugin graph did not resolve, so nothing can be generated",
            duration_ms=_timed(started),
        )

    from jfastframework.deploy import build_compose, render_compose

    findings: list[Finding] = []
    try:
        workspace = Workspace.load_or_none(root)
    except Exception as exc:  # noqa: BLE001 -- tomllib, KeyError and ValueError all reach here
        workspace = None
        findings.append(
            Finding(
                severity="high",
                code="workspace-unreadable",
                message=f"the workspace file did not load: {exc}",
                why="Every generated compose file, Caddyfile and .env comes from it.",
            )
        )
    if workspace is not None:
        findings.extend(
            Finding(
                severity="high",
                code="workspace-invalid",
                message=problem,
                why=(
                    "The generated compose file would be wrong in a way nothing reports "
                    "until a container fails to start. `jfast workspace validate` is the "
                    "same check on its own."
                ),
                path=str(workspace.file) if workspace.file else None,
            )
            for problem in workspace.validate()
        )

    services = 0
    try:
        compose = build_compose(state.config, state.instances)
        render_compose(compose)
    except Exception as exc:  # noqa: BLE001 -- a plugin's infra() is arbitrary code
        findings.append(
            Finding(
                severity="high",
                code="compose-unrenderable",
                message=f"docker-compose cannot be generated: {exc}",
                why=(
                    "`jfast deploy compose` fails the same way, and it is usually a plugin "
                    "declaring infrastructure the generator cannot express."
                ),
            )
        )
    else:
        services = len(compose.get("services", {}))

    return CheckResult(
        name="deploy",
        findings=tuple(findings),
        detail=f"compose renders, {services} services",
        duration_ms=_timed(started),
    )


def run(
    root: Path,
    *,
    config_path: str = DEFAULT_CONFIG_FILE,
    only: Sequence[str] = CHECKS,
) -> list[CheckResult]:
    """Every selected check, in :data:`CHECKS` order."""
    selected = {name for name in only}
    state = _State()
    results: list[CheckResult] = []

    # `config` and `plugins` run whenever something selected depends on them,
    # and are reported only when selected. Otherwise `--only deploy` would skip
    # for the want of a config it never asked to see.
    #
    # `analyze` is in the plugin list because `plugin-unknown` is decidable only
    # against the installed set. Dropping discovery to make `--only analyze`
    # faster would silently drop that finding, which is the one thing this
    # command exists not to do.
    needs_config = bool(selected & {"config", "plugins", "analyze", "deploy"})
    needs_plugins = bool(selected & {"plugins", "analyze", "deploy"})

    if needs_config:
        result = _config_check(root, config_path, state)
        if "config" in selected:
            results.append(result)
    if needs_plugins:
        result = _plugins_check(root, state)
        if "plugins" in selected:
            results.append(result)
    if "analyze" in selected:
        results.append(_analyze_check(root, state))
    if "contracts" in selected:
        results.append(_contracts_check(root))
    if "migrations" in selected:
        results.append(_migrations_check(root))
    if "deploy" in selected:
        results.append(_deploy_check(root, state))
    return results


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _by_status(results: Sequence[CheckResult], threshold: int) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {"pass": [], "warn": [], "fail": [], "skip": []}
    for result in results:
        grouped[result.status(threshold)].append(result.name)
    return grouped


def payload(
    results: Sequence[CheckResult],
    *,
    project: str,
    fail_on: str,
    strict: bool,
    duration_ms: int,
) -> dict[str, Any]:
    """Everything the single exit code cannot say.

    ``codes`` is the part that matters: a run can fail four checks and exit
    with one number, and a script that had to infer the other three from it
    would be guessing.

    ``not_covered`` is here for the same reason one step further out. A caller
    reading `ok: true` has every reason to think it asked the whole question,
    and this is the field that says which four gates it did not ask about.
    """
    threshold = _threshold(fail_on)
    grouped = _by_status(results, threshold)
    every = [finding for result in results for finding in result.findings]
    code = worst_code(results, fail_on=fail_on, strict=strict)
    codes = sorted({int(r.code) for r in results if r.status(threshold) == "fail"})
    if strict and grouped["skip"]:
        codes = sorted({*codes, int(SKIP_CODE)})
    return {
        "schema_version": SCHEMA_VERSION,
        "project": project,
        "ok": code == int(Code.OK),
        "complete": not grouped["skip"],
        "strict": strict,
        "fail_on": fail_on,
        "exit_code": code,
        "exit_meaning": MEANING[code],
        "codes": codes,
        "duration_ms": duration_ms,
        "summary": {status: len(names) for status, names in grouped.items()},
        "counts": insight.severity_counts(every),
        "passed": grouped["pass"],
        "warned": grouped["warn"],
        "failed": grouped["fail"],
        "skipped": grouped["skip"],
        "checks": [result.describe(threshold) for result in results],
        "not_covered": [
            {"what": what, "catches": catches, "command": command}
            for what, catches, command in NOT_COVERED
        ],
    }


def render(
    results: Sequence[CheckResult],
    *,
    project: str,
    fail_on: str,
    strict: bool,
    duration_ms: int,
) -> str:
    """One screen: a line per check, then the detail of whatever failed."""
    G = ui.G
    marks = {"pass": G.tick, "warn": G.bullet, "fail": G.cross, "skip": G.bullet}
    threshold = _threshold(fail_on)
    code = worst_code(results, fail_on=fail_on, strict=strict)

    lines = [f"  {project}", ""]
    width = max((len(result.name) for result in results), default=0)
    for result in results:
        status = result.status(threshold)
        if status == "skip":
            note = result.reason or ""
        elif result.findings:
            counts = insight.severity_counts(list(result.findings))
            note = "  ".join(f"{severity} {count}" for severity, count in counts.items())
        else:
            note = result.detail
        lines.append(f"  {marks[status]} {result.name:<{width}}  {status:<5}  {note}")

    for result in results:
        if not result.findings:
            continue
        lines.append("")
        lines.append(f"  {result.name.upper()}  ({MEANING[result.code]}, exit {int(result.code)})")
        lines.append(insight.render_analysis(list(result.findings)))

    grouped = _by_status(results, threshold)
    tally = ", ".join(f"{len(names)} {status}" for status, names in grouped.items() if names)
    lines.append("")
    lines.append(f"  {tally or 'nothing ran'} in {duration_ms / 1000:.2f}s")
    if grouped["skip"]:
        skipped_names = ", ".join(grouped["skip"])
        tail = "counted as a failure by --ci" if strict else "not a pass"
        lines.append(f"  {G.bullet} skipped: {skipped_names} -- {tail}")
    lines.append(f"  exit {code}  ({MEANING[code]})")

    # Unconditional, pass or fail. A green screen is exactly when someone
    # concludes the project was checked, and this is the sentence that stops
    # them deleting the script that runs the other four.
    lines.append("")
    lines.append(f"  {NOT_COVERED_LINE}")
    lines.append(f"  {'  '.join(command for _, _, command in NOT_COVERED)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


def _check(
    path: Annotated[Path, typer.Option("--path", "-p", help="Project root.")] = Path("."),
    config: Annotated[str, typer.Option("--config", "-c")] = DEFAULT_CONFIG_FILE,
    json_out: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
    ci: Annotated[
        bool,
        typer.Option("--ci", help="Strict: any finding fails, and so does any skipped check."),
    ] = False,
    allow_skips: Annotated[
        bool,
        typer.Option("--allow-skips", help="Under --ci, let a check that could not run pass."),
    ] = False,
    fail_on: Annotated[
        str,
        typer.Option(
            "--fail-on",
            help=f"Exit non-zero at this severity or worse: {', '.join(FAIL_ON_LEVELS)}.",
        ),
    ] = "high",
    only: Annotated[
        str | None,
        typer.Option("--only", help=f"Comma-separated subset of: {', '.join(CHECKS)}."),
    ] = None,
) -> None:
    """Every static check this framework has, in one command and one exit code.

    Configuration, the plugin graph, project structure, the contract,
    migrations and the deployment artifacts -- reported together, with one exit
    code taken from whichever failure invalidates the most.

    **It does not run ruff, ruff format, mypy or pytest**, and it never will:
    those execute your code or need your dependencies installed, and this
    command is meant to answer in milliseconds inside a pre-commit hook. So it
    is one line of a pipeline, not the pipeline. Every run prints the four it
    leaves to you, and `--json` carries them under `not_covered`.

        jfast check && ruff check . && ruff format --check . && mypy . && pytest

    Not `jfast doctor`. That one asks whether *this machine* can run the
    project and answers in two seconds; this asks whether *the repository* is
    consistent with itself. Neither replaces the other.

    Nothing here starts a container or opens a connection. A check that cannot
    run reports as skipped, in both output modes, and is never counted as a
    pass -- under `--ci` a skip fails, because in CI a missing prerequisite
    means the pipeline is not checking what you think it is checking.

        jfast check
        jfast check --json
        jfast check --ci
        jfast check --only contracts,analyze
    """
    if fail_on not in FAIL_ON_LEVELS:
        raise typer.BadParameter(
            f"choose from: {', '.join(FAIL_ON_LEVELS)}", param_hint="--fail-on"
        )

    selected: Sequence[str] = CHECKS
    if only is not None:
        names = [part.strip() for part in only.split(",") if part.strip()]
        unknown = [name for name in names if name not in CHECKS]
        if unknown:
            typer.echo(
                f"unknown check(s): {', '.join(unknown)}. Choose from: {', '.join(CHECKS)}",
                err=True,
            )
            raise typer.Exit(Code.USAGE)
        selected = names

    root = path.resolve()
    if not (root / config).is_file():
        typer.echo(
            f"no {config} in {root}\nRun this inside a service, or point at one with --path.",
            err=True,
        )
        raise typer.Exit(Code.CONFIG)

    if ci:
        fail_on = "low"

    started = time.perf_counter()
    results = run(root, config_path=config, only=selected)
    elapsed = int((time.perf_counter() - started) * 1000)

    strict = ci and not allow_skips
    reported = payload(
        results, project=root.name, fail_on=fail_on, strict=strict, duration_ms=elapsed
    )
    if json_out:
        typer.echo(jsonlib.dumps(reported, indent=2, default=str))
    else:
        typer.echo(
            render(results, project=root.name, fail_on=fail_on, strict=strict, duration_ms=elapsed)
        )

    code = reported["exit_code"]
    if code != int(Code.OK):
        raise typer.Exit(code)


def register(app: typer.Typer) -> None:
    """Attach `check` to *app*.

    A function rather than a decorator at import time so that `main.py` owns
    the command table and this module owns nothing but the command.
    """
    app.command("check")(_check)
