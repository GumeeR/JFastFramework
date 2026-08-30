"""Event-loop blocking: the bug that looks like a slow server.

A blocking call inside ``async def`` does not raise, does not log, and does not
appear in a profile of the handler that made it. It stops *every* other request
on that worker for its duration. Under load the symptom is latency smeared
across endpoints that have nothing to do with the culprit, which is why this is
one of the hardest classes of defect to find by reading code -- and why it is
worth a check rather than a paragraph in a style guide.

Ruff's ``ASYNC`` ruleset covers the standard-library cases and is enabled in
this repository and in every generated service. This module exists for the
three things a general-purpose linter cannot know:

* **The clients this framework actually ships with.** boto3, pymongo,
  psycopg2, the synchronous redis client, sqlite3. A blocking S3 upload inside
  a request handler is the most common way a JFast service stalls, and it is
  invisible to a linter because the call is a method on an instance.
* **One level of indirection.** The blocking call is rarely in the handler; it
  is in the helper the handler calls, or on a client stored on ``self``. Both
  are followed, within a file.
* **What the team knows about its own code**, declared in ``contracts.toml``.

Conservative on purpose, like the rest of the checker:

* only inside ``async def`` bodies -- a synchronous route handler is run in a
  threadpool by FastAPI and is not a bug;
* nested synchronous ``def`` bodies are skipped, because a synchronous closure
  is exactly the shape you hand to an executor;
* anything inside ``asyncio.to_thread``, ``run_in_executor``,
  ``anyio.to_thread.run_sync`` or ``run_in_threadpool`` is correct code and is
  left alone;
* a call whose target cannot be resolved through the file's imports is not
  reported. ``self._client.ping()`` could be anything, and a checker that
  guessed would flag every ``ping`` in the codebase;
* nothing crosses a file boundary. Resolving a name to a definition in another
  module is a type checker's job, and pretending otherwise is how a checker
  starts being wrong confidently.

Every finding is waivable inline with ``# contracts: allow <reason>``, because
sometimes blocking for two microseconds at startup is the right answer and the
reviewer deserves to see that someone decided so.

This module also carries the ``naive-datetime`` rule, which is not about the
event loop at all and lives here because it is the same shape of problem and
uses the same machinery: a call whose result is silently wrong, with no
exception and no log line. ``datetime.now()`` with no argument returns a naive
value in the machine's zone -- UTC in a container, Europe/Madrid on the laptop
that wrote the test -- and ``datetime.utcnow()`` returns a naive value that
merely *looks* like UTC, which is why it was deprecated in 3.12. Both are where
naive values are born; ``UTCDateTime`` refusing one at write time is the
backstop, and a backstop tells you a row was wrong, not which line wrote it.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

from jfastframework.contracts._scan import (
    Violation,
    call_target,
    import_aliases,
    python_files,
    resolve,
    waived,
)
from jfastframework.contracts.model import Contract, match_path

RULE = "async-blocking"
NAIVE_RULE = "naive-datetime"


@dataclass(frozen=True)
class BlockingCall:
    """A call that stops the event loop, and what to reach for instead."""

    pattern: str
    instead: str


@dataclass(frozen=True)
class NaiveCall:
    """A call that yields a datetime with no zone, and what to write instead.

    ``BLOCKING_CALLS`` cannot express this rule. It is matched by ``_matches``,
    which compares a resolved dotted name and nothing else, so every entry in
    it is a violation on sight; ``datetime.now()`` is one and
    ``datetime.now(UTC)`` is not, and the two are the same name. Rather than
    teach every blocking entry about arguments it does not have, the argument
    test lives on this table alone, in the smallest form that stays honest
    about what the syntax can and cannot decide.
    """

    pattern: str
    instead: str
    # Index of the positional parameter that would make the result aware, or
    # None when nothing can -- `datetime.utcnow()` is naive whatever it is
    # given. A `*args` splat or a `**kwargs` splat is not reported: the call
    # may well be passing the zone and the syntax cannot say.
    tz_at: int | None = None


# The zone is not optional; it is the difference between an instant and a
# string that resembles one.
NAIVE_DATETIME_CALLS: tuple[NaiveCall, ...] = (
    NaiveCall(
        "datetime.datetime.now",
        "datetime.now(UTC), or jfastframework.time.now()",
        tz_at=0,
    ),
    NaiveCall(
        "datetime.datetime.utcnow",
        "jfastframework.time.now(): utcnow() is naive despite the name, and deprecated since 3.12",
    ),
    NaiveCall(
        "datetime.datetime.utcfromtimestamp",
        "datetime.fromtimestamp(value, UTC): utcfromtimestamp() is naive and deprecated since 3.12",
    ),
)


_PILLOW_INSTEAD = "await asyncio.to_thread(...): image codecs are CPU-bound, not I/O"

# Calls that block wherever they appear. A trailing ``.*`` matches everything
# under that name.
BLOCKING_CALLS: tuple[BlockingCall, ...] = (
    BlockingCall("time.sleep", "await asyncio.sleep(...)"),
    BlockingCall("asyncio.run", "await the coroutine; you are already in a loop"),
    BlockingCall("os.system", "await asyncio.create_subprocess_shell(...)"),
    BlockingCall("os.popen", "await asyncio.create_subprocess_shell(...)"),
    BlockingCall("subprocess.run", "await asyncio.create_subprocess_exec(...)"),
    BlockingCall("subprocess.call", "await asyncio.create_subprocess_exec(...)"),
    BlockingCall("subprocess.check_call", "await asyncio.create_subprocess_exec(...)"),
    BlockingCall("subprocess.check_output", "await asyncio.create_subprocess_exec(...)"),
    BlockingCall("subprocess.Popen", "await asyncio.create_subprocess_exec(...)"),
    BlockingCall("requests.*", "httpx.AsyncClient, already a dependency of gateway and auth"),
    BlockingCall("urllib.request.urlopen", "httpx.AsyncClient"),
    BlockingCall("socket.create_connection", "await asyncio.open_connection(...)"),
    BlockingCall("smtplib.*", "await asyncio.to_thread(...), or an async SMTP client"),
    BlockingCall("ftplib.*", "await asyncio.to_thread(...)"),
    BlockingCall("shutil.copy", "await asyncio.to_thread(shutil.copy, ...)"),
    BlockingCall("shutil.copy2", "await asyncio.to_thread(shutil.copy2, ...)"),
    BlockingCall("shutil.copyfile", "await asyncio.to_thread(shutil.copyfile, ...)"),
    BlockingCall("shutil.copytree", "await asyncio.to_thread(shutil.copytree, ...)"),
    BlockingCall("shutil.move", "await asyncio.to_thread(shutil.move, ...)"),
    BlockingCall("shutil.rmtree", "await asyncio.to_thread(shutil.rmtree, ...)"),
    BlockingCall("open", "anyio.open_file(...), or await asyncio.to_thread(...)"),
    BlockingCall("input", "nothing: a coroutine must not wait on a human"),
    # Pillow is CPU, not I/O, which is why it reads as harmless and is not.
    # Decoding a 24-megapixel JPEG is hundreds of milliseconds and re-encoding
    # it is seconds; there is no async Pillow to reach for, so the only right
    # answer is a thread. The storage plugin's optimise-image step is the shape
    # to copy.
    BlockingCall("PIL.Image.open", _PILLOW_INSTEAD),
    BlockingCall("PIL.Image.frombytes", _PILLOW_INSTEAD),
    BlockingCall("PIL.ImageOps.*", _PILLOW_INSTEAD),
    BlockingCall("PIL.ImageFilter.*", _PILLOW_INSTEAD),
)

# ``Path(...)`` methods that touch the filesystem. Enumerated rather than
# matched by prefix because most of the class is pure string manipulation and
# flagging ``with_suffix()`` would teach people to ignore the rule.
_PATH_IO = (
    "read_text",
    "write_text",
    "read_bytes",
    "write_bytes",
    "exists",
    "is_dir",
    "is_file",
    "stat",
    "mkdir",
    "rmdir",
    "unlink",
    "touch",
    "rename",
    "replace",
    "iterdir",
    "glob",
    "rglob",
)

BLOCKING_CALLS += tuple(
    BlockingCall(
        f"pathlib.Path.{name}",
        "await asyncio.to_thread(...), or do the filesystem work at startup",
    )
    for name in _PATH_IO
)

# Objects whose *methods* block. The construction is cheap; every call on the
# result is not, and that call is a method on an instance, which no linter
# working from syntax alone can attribute to a library.
BLOCKING_CONSTRUCTORS: tuple[BlockingCall, ...] = (
    BlockingCall("boto3.client", "await asyncio.to_thread(...), or the storage plugin"),
    BlockingCall("boto3.resource", "await asyncio.to_thread(...), or the storage plugin"),
    BlockingCall("pymongo.MongoClient", "the mongo plugin, which uses Motor"),
    BlockingCall("psycopg2.connect", "the database plugin, which uses asyncpg"),
    BlockingCall("sqlite3.connect", "aiosqlite, or await asyncio.to_thread(...)"),
    BlockingCall("redis.Redis", "the cache plugin, which uses redis.asyncio"),
    BlockingCall("redis.StrictRedis", "the cache plugin, which uses redis.asyncio"),
    BlockingCall("redis.from_url", "the cache plugin, which uses redis.asyncio"),
    BlockingCall("requests.Session", "httpx.AsyncClient"),
    # `Image.open` is lazy -- it reads a header. The cost lands on the object
    # it returns, in `.load()`, `.save()`, `.resize()`, `.convert()`, so the
    # name it was bound to has to be tainted as well as the call itself.
    BlockingCall("PIL.Image.open", _PILLOW_INSTEAD),
    BlockingCall("PIL.Image.new", _PILLOW_INSTEAD),
)

# Correct ways to run blocking code from a coroutine. Matched on the final
# segment: being lenient here can only ever suppress a report, never add one.
OFFLOADERS = frozenset(
    {"to_thread", "run_sync", "run_in_threadpool", "run_in_executor", "run_in_thread"}
)

# Matched on the final segment regardless of imports, because there is exactly
# one thing anyone means by it and it deadlocks a running loop.
BLOCKING_SUFFIXES: Mapping[str, str] = {
    "run_until_complete": "await the coroutine; you are already in a loop",
}

DEFAULT_ALLOW_IN: tuple[str, ...] = (
    "tests/*",
    "*/tests/*",
    "test_*.py",
    "conftest.py",
    "scripts/*",
    "migrations/*",
)


def _matches(name: str, pattern: str) -> bool:
    if pattern.endswith(".*"):
        return name.startswith(pattern[:-1])
    return name == pattern


def _is_offloader(node: ast.Call) -> bool:
    target = call_target(node)
    return target is not None and target.rsplit(".", 1)[-1] in OFFLOADERS


def _executing_nodes(body: list[ast.stmt]) -> Iterator[ast.AST]:
    """Nodes that actually run on the event loop when this body runs.

    Synchronous closures and the arguments of an offloader are deliberately
    not part of that: both are how correct code hands blocking work away.
    """
    stack: list[ast.AST] = list(body)
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            continue
        if isinstance(node, ast.Call) and _is_offloader(node):
            continue
        stack.extend(ast.iter_child_nodes(node))


def _blocking_reason(
    node: ast.Call,
    aliases: Mapping[str, str],
    extra: Mapping[str, str],
) -> tuple[str, str] | None:
    """Why this call blocks, and what to use instead -- or None."""
    target = call_target(node)
    if target is None:
        return None

    tail = target.rsplit(".", 1)[-1]
    if tail in BLOCKING_SUFFIXES:
        return target, BLOCKING_SUFFIXES[tail]

    resolved = resolve(target, aliases)
    if resolved is None:
        # A builtin is the one unresolvable name worth trusting: nothing
        # imported it, and shadowing `open` is rare enough to waive.
        if "." not in target:
            for rule in BLOCKING_CALLS:
                if rule.pattern == target:
                    return target, rule.instead
        return None

    for rule in BLOCKING_CALLS:
        if _matches(resolved, rule.pattern):
            return resolved, rule.instead
    for rule in BLOCKING_CONSTRUCTORS:
        # `boto3.client("s3").put_object(...)` in one expression.
        if resolved.startswith(rule.pattern + "."):
            return resolved, rule.instead
    for pattern, instead in extra.items():
        if _matches(resolved, pattern):
            return resolved, instead
    return None


def _tainted_names(scope: list[ast.stmt], aliases: Mapping[str, str]) -> dict[str, str]:
    """Local names holding an object whose methods block.

    ``s3 = boto3.client("s3")`` makes every later ``s3.<anything>()`` a
    blocking call, and that is provable from this function alone.
    """
    tainted: dict[str, str] = {}
    for node in ast.walk(ast.Module(body=scope, type_ignores=[])):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        target = call_target(node.value)
        resolved = resolve(target, aliases) if target else None
        if resolved is None:
            continue
        for rule in BLOCKING_CONSTRUCTORS:
            if resolved != rule.pattern:
                continue
            for bound in node.targets:
                if isinstance(bound, ast.Name):
                    tainted[bound.id] = rule.instead
                elif (
                    isinstance(bound, ast.Attribute)
                    and isinstance(bound.value, ast.Name)
                    and bound.value.id == "self"
                ):
                    tainted[f"self.{bound.attr}"] = rule.instead
    return tainted


def _blocking_helpers(tree: ast.Module, aliases: Mapping[str, str]) -> dict[str, str]:
    """Synchronous functions in this file that block, by name.

    One level only. A helper that calls a helper that blocks is not followed,
    because past one hop the report stops being obviously true to the person
    reading it, and a finding nobody believes is a finding everybody ignores.
    """
    helpers: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for inner in _executing_nodes(node.body):
            if not isinstance(inner, ast.Call):
                continue
            found = _blocking_reason(inner, aliases, {})
            if found is not None:
                helpers[node.name] = found[0]
                break
    return helpers


def _check_async_function(
    fn: ast.AsyncFunctionDef,
    *,
    relative: str,
    lines: list[str],
    aliases: Mapping[str, str],
    helpers: Mapping[str, str],
    class_tainted: Mapping[str, str],
    extra: Mapping[str, str],
) -> list[Violation]:
    violations: list[Violation] = []
    tainted = {**class_tainted, **_tainted_names(fn.body, aliases)}

    for node in _executing_nodes(fn.body):
        if not isinstance(node, ast.Call):
            continue
        if waived(lines, node.lineno) is not None:
            continue

        found = _blocking_reason(node, aliases, extra)
        if found is not None:
            name, instead = found
            violations.append(
                Violation(
                    relative,
                    node.lineno,
                    RULE,
                    f"{name}() blocks the event loop inside async {fn.name}()",
                    f"every other request on this worker waits. Use {instead}",
                )
            )
            continue

        target = call_target(node)
        if target is None:
            continue

        owner = target.rsplit(".", 1)[0]
        if owner in tainted:
            violations.append(
                Violation(
                    relative,
                    node.lineno,
                    RULE,
                    f"{target}() is a synchronous client call inside async {fn.name}()",
                    f"every other request on this worker waits. Use {tainted[owner]}",
                )
            )
            continue

        if target in helpers:
            violations.append(
                Violation(
                    relative,
                    node.lineno,
                    RULE,
                    f"{target}() is synchronous and calls {helpers[target]}(), "
                    f"which blocks the event loop",
                    "make the helper a coroutine, or offload it with asyncio.to_thread",
                )
            )

    return violations


def _yields_naive(node: ast.Call, rule: NaiveCall) -> bool:
    """Whether this particular call site produces a value with no zone."""
    if rule.tz_at is None:
        return True
    if any(isinstance(argument, ast.Starred) for argument in node.args):
        return False
    if len(node.args) > rule.tz_at:
        return False
    for keyword in node.keywords:
        # `keyword.arg is None` is `**kwargs`, which may well carry the zone.
        if keyword.arg is None or keyword.arg in ("tz", "tzinfo"):
            return False
    return True


def _check_naive_datetimes(
    tree: ast.Module,
    *,
    relative: str,
    lines: list[str],
    aliases: Mapping[str, str],
) -> list[Violation]:
    """Naive datetimes, wherever they are born.

    The whole module, not only ``async def`` bodies: a naive value written by a
    synchronous helper reaches the same column and is wrong by the same offset.
    """
    violations: list[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or waived(lines, node.lineno) is not None:
            continue
        target = call_target(node)
        resolved = resolve(target, aliases) if target else None
        if resolved is None:
            continue
        for rule in NAIVE_DATETIME_CALLS:
            if resolved != rule.pattern or not _yields_naive(node, rule):
                continue
            violations.append(
                Violation(
                    relative,
                    node.lineno,
                    NAIVE_RULE,
                    f"{resolved}() returns a datetime with no time zone",
                    f"it reads as local time to whoever renders it, and "
                    f"UTCDateTime refuses it on write. Use {rule.instead}",
                )
            )
            break
    return violations


def _class_tainted(tree: ast.Module, aliases: Mapping[str, str]) -> dict[str, dict[str, str]]:
    """Per class, the ``self.<attr>`` names holding a blocking client.

    Assigned in ``__init__`` and used in an async method four screens away is
    the shape this exists for -- and the shape a linter reading one function at
    a time cannot see.
    """
    per_class: dict[str, dict[str, str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            per_class[node.name] = _tainted_names(node.body, aliases)
    return per_class


def check_blocking(contract: Contract, root: Path) -> list[Violation]:
    """Report every call that stalls the event loop or drops a time zone.

    Two rules, one switch: ``naive-datetime`` is gated on
    ``[rules.async_safety]`` because both are calls whose damage does not show
    up where they are written. It has its own key there,
    ``naive_datetime``, so silencing one does not silence the other: they
    share a table, not a switch. Turning
    async safety off therefore turns this off too, which is worth knowing
    before anyone does it.
    """
    rules = contract.async_safety
    if not rules.enabled:
        return []

    allow_in = rules.allow_in or list(DEFAULT_ALLOW_IN)
    extra = dict(rules.extra_blocking)
    violations: list[Violation] = []

    for path in python_files(root):
        relative = path.relative_to(root).as_posix()
        if any(match_path(relative, pattern) for pattern in allow_in):
            continue
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=relative)
        except (OSError, SyntaxError):
            continue

        lines = source.splitlines()
        aliases = import_aliases(tree)
        helpers = _blocking_helpers(tree, aliases) if rules.follow_local_helpers else {}
        per_class = _class_tainted(tree, aliases)

        if rules.naive_datetime:
            violations.extend(
                _check_naive_datetimes(tree, relative=relative, lines=lines, aliases=aliases)
            )

        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for member in node.body:
                if isinstance(member, ast.AsyncFunctionDef):
                    violations.extend(
                        _check_async_function(
                            member,
                            relative=relative,
                            lines=lines,
                            aliases=aliases,
                            helpers=helpers,
                            class_tainted=per_class.get(node.name, {}),
                            extra=extra,
                        )
                    )

        methods = {
            id(member)
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef)
            for member in node.body
            if isinstance(member, ast.AsyncFunctionDef)
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and id(node) not in methods:
                violations.extend(
                    _check_async_function(
                        node,
                        relative=relative,
                        lines=lines,
                        aliases=aliases,
                        helpers=helpers,
                        class_tainted={},
                        extra=extra,
                    )
                )

    return violations
