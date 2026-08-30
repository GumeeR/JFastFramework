"""What each framework version broke, as data, and how to see it in a project.

The obvious implementation of ``jfast upgrade --check`` parses ``CHANGELOG.md``.
It is the wrong one, for three reasons that are each fatal on their own:

* prose is not a data format. "Breaking" is a heading today; the moment somebody
  writes "Breaking changes" the parser reports that nothing broke, and reports
  it confidently.
* the file does not ship. ``[tool.hatch.build.targets.wheel] packages =
  ["src/jfastframework"]`` puts the package in the wheel and nothing else, so an
  installed framework has no changelog to read.
* it answers the wrong question. A changelog says what changed; the person
  upgrading needs to know what changes **to their project**, and a list of
  twenty release notes with no way to tell which three apply is a list nobody
  reads twice.

So the breaking changes are declared here, in the package, each with a
``detect`` that looks at the project on disk and returns the evidence -- the
tables that need the migration, the layers missing a permission, the settings
about to acquire a default. A change whose ``detect`` returns nothing is not
reported at all. That rule is the whole value of the command: one warning that
does not apply teaches people to skip the output, and the next one is skipped
with it.

``detect`` is ``None`` only where nothing on disk can decide the question. Then
the entry is informational and says so.

Version comparison is PEP 440-aware and **vendored**, in :func:`parse_version`.
``packaging`` is not a dependency of this framework -- neither a direct one nor
a transitive one of fastapi, pydantic, pydantic-settings, typer or jinja2 -- so
importing it would work on a development machine and fail on a plain
``pip install jfastframework``. The vendored key follows ``packaging``'s own
ordering, and ``tests/test_cli_upgrade.py`` pins that agreement against the real
implementation, which *is* installed for development.
"""

from __future__ import annotations

import ast
import json
import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jfastframework.project import SKIP_DIRS, Project

__all__ = [
    "CHANGES",
    "Applicable",
    "Change",
    "Pin",
    "applicable",
    "parse_version",
    "pinned_version",
]

KINDS = ("breaking", "deprecated", "behaviour")


# ---------------------------------------------------------------------------
# Version comparison
# ---------------------------------------------------------------------------

_VERSION = re.compile(
    r"""^\s*v?
    (?P<release>\d+(?:\.\d+)*)
    (?:[-_.]?(?P<pre_l>alpha|beta|preview|pre|a|b|c|rc)[-_.]?(?P<pre_n>\d*))?
    (?:[-_.]?post[-_.]?(?P<post>\d*))?
    (?:[-_.]?dev[-_.]?(?P<dev>\d*))?
    \s*$""",
    re.VERBOSE | re.IGNORECASE,
)

_PRE_RANK = {"a": 0, "alpha": 0, "b": 1, "beta": 1, "c": 2, "rc": 2, "pre": 2, "preview": 2}

# Sorts after every pre-release of the same version, and after every dev.
_FINAL = (3, 0)
# Sorts before every pre-release: `1.0.dev1` precedes `1.0a1`.
_BEFORE_ANY_PRE = (-1, 0)
_NO_DEV = 1 << 62

_SortKey = tuple[tuple[int, ...], tuple[int, int], int, int]


def parse_version(text: str) -> _SortKey:
    """A sortable key for *text*, ordered as PEP 440 orders versions.

    String comparison is what this exists to avoid: it reads ``0.1.0a10`` as
    older than ``0.1.0a9`` and quietly reports that a project is up to date.

    Epochs and local versions are not accepted. This framework has never
    published one, and a key that silently ignores half of what it was given is
    worse than one that refuses it.
    """
    match = _VERSION.match(text)
    if match is None:
        raise ValueError(f"not a PEP 440 version: {text!r}")

    numbers = [int(part) for part in match.group("release").split(".")]
    # 1.0 and 1.0.0 are the same version, so trailing zeros cannot count.
    while len(numbers) > 1 and numbers[-1] == 0:
        numbers.pop()
    release = tuple(numbers)

    pre_letter = match.group("pre_l")
    post = match.group("post")
    dev = match.group("dev")

    if pre_letter is not None:
        pre = (_PRE_RANK[pre_letter.lower()], int(match.group("pre_n") or 0))
    elif post is None and dev is not None:
        pre = _BEFORE_ANY_PRE
    else:
        pre = _FINAL

    return (
        release,
        pre,
        -1 if post is None else int(post or 0),
        _NO_DEV if dev is None else int(dev or 0),
    )


# ---------------------------------------------------------------------------
# What the project pins
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Pin:
    """The framework version a project asks for, and where that was written."""

    version: str
    source: str


STAMP_FILE = ".jfast-template"

_REQUIREMENT = re.compile(
    r"^\s*jfastframework(?:\[[^\]]*\])?\s*(?:==|~=|>=|<=|=)\s*(?P<version>[^\s,;#]+)",
    re.IGNORECASE | re.MULTILINE,
)


def pinned_version(root: Path) -> Pin | None:
    """The version this project runs on, or `None` when nothing says.

    ``requirements.txt`` wins over the scaffold stamp because it is the one pip
    acts on: a project upgraded by editing that line and reinstalling is on the
    new version whatever the stamp still remembers about the day it was
    generated.
    """
    requirements = root / "requirements.txt"
    if requirements.is_file():
        try:
            text = requirements.read_text(encoding="utf-8")
        except OSError:
            text = ""
        match = _REQUIREMENT.search(text)
        if match:
            return Pin(match.group("version"), "requirements.txt")

    stamp = root / STAMP_FILE
    if stamp.is_file():
        versions = _stamped_versions(stamp)
        if versions:
            return Pin(max(versions, key=parse_version), STAMP_FILE)
    return None


def _stamped_versions(stamp: Path) -> list[str]:
    try:
        loaded = json.loads(stamp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(loaded, dict):
        return []
    templates = loaded.get("templates")
    if not isinstance(templates, dict):
        return []
    found = []
    for entry in templates.values():
        if isinstance(entry, dict):
            version = entry.get("framework_version")
            # A stamp written by a future version can carry anything; a
            # template whose version does not parse is skipped, not fatal.
            if isinstance(version, str) and _parses(version):
                found.append(version)
    return found


def _parses(version: str) -> bool:
    try:
        parse_version(version)
    except ValueError:
        return False
    return True


# ---------------------------------------------------------------------------
# Reading the project
# ---------------------------------------------------------------------------


def _config(project: Project) -> dict[str, Any]:
    """``jfast.toml`` as a plain mapping.

    Read here rather than taken from `Project`, which keeps the plugin *names*
    and drops their settings -- and the settings are exactly what decides
    whether two of the changes below apply.
    """
    path = project.root / "jfast.toml"
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as handle:
            loaded: dict[str, Any] = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return loaded


def _table(config: dict[str, Any], *keys: str) -> dict[str, Any]:
    current: Any = config
    for key in keys:
        if not isinstance(current, dict):
            return {}
        current = current.get(key, {})
    return current if isinstance(current, dict) else {}


def _issues_tokens(project: Project) -> bool:
    """Whether this service is the one minting tokens, not just verifying them.

    A service that only validates somebody else's tokens is untouched by every
    change to the issuing endpoints, which is most services with `auth` on.
    """
    if "auth" not in project.plugins:
        return False
    return bool(_table(_config(project), "plugin", "auth").get("issue_tokens", False))


def _python_files(root: Path) -> list[Path]:
    return [
        path
        for path in sorted(root.rglob("*.py"))
        if not any(part in SKIP_DIRS for part in path.parts)
    ]


def _imports_timestamp_mixin(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and any(
            alias.name == "TimestampMixin" for alias in node.names
        ):
            return True
    return False


def _tablenames(tree: ast.Module) -> list[str]:
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not isinstance(node.value, ast.Constant) or not isinstance(node.value.value, str):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "__tablename__":
                found.append(node.value.value)
    return found


def _timestamp_migration(project: Project) -> list[str]:
    """One `ALTER TABLE` per table whose model mixes in `TimestampMixin`."""
    affected: list[str] = []
    for path in _python_files(project.root):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError, ValueError):
            continue
        if not _imports_timestamp_mixin(tree):
            continue
        where = path.relative_to(project.root).as_posix()
        tables = _tablenames(tree)
        if not tables:
            affected.append(f"{where}: imports TimestampMixin, declares no __tablename__")
            continue
        for table in tables:
            affected.append(
                f"{where}  ->  {table}\n"
                f"ALTER TABLE {table}\n"
                f"    ALTER COLUMN created_at TYPE timestamptz "
                f"USING created_at AT TIME ZONE 'UTC',\n"
                f"    ALTER COLUMN updated_at TYPE timestamptz "
                f"USING updated_at AT TIME ZONE 'UTC';"
            )
    return affected


def _layers_without_shared(project: Project) -> list[str]:
    """Layers whose `may_import` never names `shared`.

    Parsed with `tomllib` rather than through `contracts.Contract`: that loader
    reads the whole file including rule tables this question does not need, and
    a project with one malformed rule would get no report at all instead of the
    report it came for.
    """
    path = project.root / "contracts.toml"
    if not path.is_file():
        return []
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return []
    layers = raw.get("layers")
    if not isinstance(layers, dict):
        return []

    offenders = []
    for name, block in layers.items():
        # `shared` is the one layer that must not import a layer: it is a leaf,
        # and the moment it can import upward the graph is a circle.
        if name == "shared" or not isinstance(block, dict):
            continue
        allowed = block.get("may_import", [])
        if not isinstance(allowed, list) or "shared" not in allowed:
            current = ", ".join(f'"{item}"' for item in allowed) if allowed else ""
            offenders.append(f'[layers.{name}]  may_import = [{current}]  ->  add "shared"')
    return offenders


# 2 MiB / 30 s, and the raised pair a service that takes uploads is generated
# with. Kept beside the change so the report quotes the real numbers rather
# than a sentence about them.
_LIMIT_DEFAULTS = {
    "max_body_bytes": (2 * 1024 * 1024, 25 * 1024 * 1024),
    "request_timeout": (30.0, 120.0),
}


def _request_limit_defaults(project: Project) -> list[str]:
    """Only the limits this project has not set for itself."""
    app = _table(_config(project), "app")
    uploads = "storage" in project.plugins
    affected = []
    for field, (plain, with_storage) in _LIMIT_DEFAULTS.items():
        if field in app:
            continue
        value = with_storage if uploads else plain
        note = "  (this service enables storage)" if uploads else ""
        affected.append(f"{field} = {value}{note}")
    return affected


# ---------------------------------------------------------------------------
# The manifest
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Change:
    """One thing a version broke, and how to see it in a project.

    `detect` returns the evidence found in *this* project -- table names, layer
    names, the values a setting is about to acquire. An empty list means the
    project cannot be affected and the change is not reported.
    """

    version: str
    kind: str
    code: str
    summary: str
    detail: str
    detect: Callable[[Project], list[str]] | None
    remedy: str

    def describe(self, affected: list[str]) -> dict[str, Any]:
        return {
            "version": self.version,
            "kind": self.kind,
            "code": self.code,
            "summary": self.summary,
            "detail": self.detail,
            "remedy": self.remedy,
            "affected": affected,
        }


CHANGES: tuple[Change, ...] = (
    Change(
        version="0.1.0a4",
        kind="breaking",
        code="timestamps-timezone-aware",
        summary="TimestampMixin columns are timezone-aware. Existing tables need a migration.",
        detail=(
            "created_at and updated_at mapped to TIMESTAMP WITHOUT TIME ZONE, so a row "
            "serialised as 2026-08-29T20:55:15 with no offset and every JavaScript client "
            "read it as local time. They are timestamptz now, and the columns already in "
            "your database are not."
        ),
        detect=_timestamp_migration,
        remedy=(
            "Write the statements above into an Alembic revision by hand. The USING clause "
            "is load-bearing and autogenerate omits it: without USING, PostgreSQL converts "
            "through the implicit cast, reads every stored value in the server's own "
            "TimeZone, and silently shifts the whole table on any server not set to UTC. "
            "Nothing fails; the timestamps are just wrong afterwards."
        ),
    ),
    Change(
        version="0.1.0a4",
        kind="breaking",
        code="contracts-shared-import",
        summary="Generated contracts let every layer import shared/. Yours predate that.",
        detail=(
            "contracts.toml belongs to the project once generated, so this one was not "
            "rewritten. The placement rule tells you to move a twice-wanted enum into "
            "shared/, and a layer that may not import shared/ has no legal way to follow "
            "the instruction the checker itself prints."
        ),
        detect=_layers_without_shared,
        remedy=(
            'Add "shared" to the may_import list of each layer above, by hand. Do not run '
            "contracts init --force: it writes the corrected defaults and overwrites the "
            "whole file, discarding the project-specific lines that are the part worth "
            "having."
        ),
    ),
    Change(
        version="0.1.0a4",
        kind="breaking",
        code="refresh-tokens-rejected",
        summary="Refresh tokens minted by 0.1.0a3 and earlier are refused with a 401.",
        detail=(
            "Every active session re-authenticates once, at the moment it deploys. Those "
            "sessions were already broken: rotating one returned a token with no scopes, "
            "which authenticated and was authorized for nothing."
        ),
        detect=lambda project: (
            ["[plugin.auth] issue_tokens = true -- this service mints the tokens"]
            if _issues_tokens(project)
            else []
        ),
        remedy=(
            "Nothing to change in code. Deploy at a quiet hour, or expect one sign-in per "
            "active session."
        ),
    ),
    Change(
        version="0.1.0a4",
        kind="breaking",
        code="logout-ends-one-session",
        summary="/auth/logout ends the calling session only. It used to end all of them.",
        detail=(
            "There is no sign-out-everywhere replacement in this release: a subject-level "
            "cursor needs a TokenStore change and ships separately. A UI whose button says "
            "'sign out of all devices' now tells the truth about one device."
        ),
        detect=lambda project: (
            ["[plugin.auth] issue_tokens = true -- this service serves /auth/logout"]
            if _issues_tokens(project)
            else []
        ),
        remedy=(
            "Reword any UI that promised more, and re-check tests that asserted the old behaviour."
        ),
    ),
    Change(
        version="0.1.0a4",
        kind="behaviour",
        code="access-token-fam-claim",
        summary="Access tokens carry a fam claim, so revoking a session kills its access tokens.",
        detail=(
            "Previously an access token outlived the revocation of its session until it "
            "expired on its own. Anything that cached a decoded token payload sees a new "
            "claim it did not before."
        ),
        detect=lambda project: (
            ["[plugin.auth] issue_tokens = true -- this service mints the tokens"]
            if _issues_tokens(project)
            else []
        ),
        remedy="No action unless something of yours asserts on the exact claim set.",
    ),
    Change(
        version="0.1.0a4",
        kind="breaking",
        code="request-limit-defaults",
        summary="max_body_bytes and request_timeout have values now. Both were None.",
        detail=(
            "A request larger than the limit is refused with 413 and one that outlives the "
            "timeout is answered with 504, where both used to be accepted. 0 and None both "
            "still mean unlimited, so an explicit 0 keeps the old behaviour."
        ),
        detect=_request_limit_defaults,
        remedy=(
            "Set both in [app] of jfast.toml if these are wrong for what this service "
            "accepts. A service taking uploads wants the raised pair; 0 turns a limit off."
        ),
    ),
    Change(
        version="0.1.0a4",
        kind="behaviour",
        code="cli-exit-codes",
        summary="contracts check exits 5, doctor exits 2 or 3. Both used to exit 1.",
        detail=(
            "Informational, and stated unconditionally: nothing in a project says whether "
            "its pipeline branches on an exit code, so this cannot be detected and will "
            "not be guessed at. Codes are documented in jfastframework.cli.exits."
        ),
        detect=None,
        remedy=(
            "Grep your CI config for these commands. A step asserting `exit code == 1` now "
            "passes when it should fail; one testing `!= 0` is unaffected."
        ),
    ),
)


Applicable = list[tuple[Change, list[str]]]


def applicable(project: Project, *, current: str, installed: str) -> Applicable:
    """Changes landing in ``(current, installed]`` that this project can feel.

    Ordered as the manifest declares them: oldest version first, and within a
    version by how much work the change costs the reader.
    """
    low = parse_version(current)
    high = parse_version(installed)

    found: Applicable = []
    for change in CHANGES:
        landed = parse_version(change.version)
        if not (low < landed <= high):
            continue
        if change.detect is None:
            found.append((change, []))
            continue
        affected = change.detect(project)
        if affected:
            found.append((change, affected))
    return found
