# Contracts

`AGENTS.md` says what to do. A contract says what is **allowed**, and something
checks it.

That difference is the whole point. An agent generating code at speed will
drift past a suggestion without noticing — not out of malice, but because
nothing pushed back. A contract pushes back:

```bash
jfast contracts check
```

```
modules/invoice/repository.py:1: layer-package: 'storage' must not import 'fastapi'  (Data access. No business rules.)

1 violation(s). Fix them, or waive one inline with
    # contracts: allow <reason>
```

Non-zero exit. In CI, that is a failed build.

---

## The three audiences, one file

`contracts.toml` sits at the root of every generated service.

| Audience | Reads it as |
| --- | --- |
| The build | `jfast contracts check` — fails on a violation |
| An agent | `jfast contracts show --json` — before writing a line |
| A human | `CONTRACTS.md` — generated, for review |

One source, three renderings, so the document and the enforced rule cannot
disagree.

---

## What you declare

### Scope

```toml
[project]
name = "billing"
owns = "Invoices and payments."
does_not_own = "Customers. Ask the catalog service."
```

`does_not_own` is the more useful half, and the one people skip. Most bad code
in a growing system is a service quietly expanding into something another
service already owns — and it never looks wrong from inside that service.

### Layers

```toml
[layers.domain]
description = "Entities and their rules. Framework-free."
paths = ["modules/*/[!_]*.py"]
may_import = []
forbid_packages = ["fastapi", "sqlalchemy", "pydantic"]

[layers.http]
paths = ["modules/*/http.py"]
may_import = ["use_cases", "domain"]
```

`may_import` names **other layers**, not packages. Layers are what a reviewer
argues about; package names are what they forget.

A file is classified by the **most specific** matching pattern — fewest
wildcards, not longest string. That distinction is load-bearing:
`modules/*/[!_]*.py` is longer than `modules/*/http.py`, and ranking by length
would classify every router as domain code and then reject its imports for a
reason nobody could work out.

### Forbidden calls

```toml
[[rules.forbid_call]]
pattern = "os.getenv"
except_in = ["settings.py", "config/*.py", "migrations/env.py"]
why = "Configuration is typed. Add a field to a settings model so a bad value fails at boot."
```

`why` is not decoration. It is what the checker prints, and the difference
between someone fixing the cause and someone deleting the line.

A dotted pattern also catches the bare import, so `from os import getenv` does
not slip through. A bare pattern matches exactly, so forbidding `print` does
not also flag `report.print()`.

### Required structure

```toml
[[rules.require]]
path = "tests"
applies_to = "modules/*"
why = "A module with no tests is a module nobody can change safely."
```

### Event-loop safety

The hardest bug in an async service is the one that never raises. A blocking
call inside `async def` stops every other request on that worker for its
duration, and the symptom arrives as latency on endpoints that have nothing to
do with the cause. Nothing in the traceback, nothing in the log, and a profile
of the slow endpoint points at code that is innocent.

So it is a contract rule, checked on every build:

```toml
[rules.async_safety]
enabled = true
allow_in = ["tests/*", "conftest.py", "scripts/*", "migrations/*"]
follow_local_helpers = true

[rules.async_safety.extra_blocking]
"myapp.legacy.render_pdf" = "await asyncio.to_thread(render_pdf, ...)"
```

```
blocking_demo.py:14: async-blocking: requests.get() blocks the event loop inside async send()
  (every other request on this worker waits. Use httpx.AsyncClient, already a dependency of gateway and auth)
blocking_demo.py:13: async-blocking: warm_cache() is synchronous and calls time.sleep(), which blocks the event loop
  (make the helper a coroutine, or offload it with asyncio.to_thread)
```

Three things it finds that a general-purpose linter does not:

* **Clients on `self`.** `self._s3 = boto3.client("s3")` in `__init__`, then
  `self._s3.put_object(...)` in an async method four screens away. The call is
  a method on an instance, which is invisible to a rule that reads one function
  at a time.
* **One hop of indirection.** The blocking call is rarely in the handler; it is
  in the synchronous helper the handler calls. Within a file, that helper is
  followed into its callers.
* **Your own code.** `extra_blocking` is where the team writes down the
  functions only it knows about, with the replacement to reach for.

It knows `boto3`, `pymongo`, `psycopg2`, the synchronous `redis` client and
`sqlite3` by name, plus the standard-library cases: `time.sleep`,
`subprocess`, `requests`, blocking `pathlib` I/O, `open()`, and `asyncio.run`
or `run_until_complete` inside a coroutine.

**What it will not do**, on the same principle as the rest of the checker:

* A synchronous `def` handler is not reported. FastAPI runs it in a threadpool;
  that is a supported way to write a route, not a bug.
* Work handed to `asyncio.to_thread`, `run_in_executor`,
  `anyio.to_thread.run_sync` or `run_in_threadpool` is correct code and is left
  alone -- including the synchronous closure you pass to it, which is why
  `storage/s3.py` reports nothing.
* A call it cannot resolve through the file's imports is not reported.
  `self._client.ping()` could be anything, and a checker that guessed would
  flag every `ping` in the codebase.
* Nothing crosses a file boundary. Resolving a name to a definition in another
  module is a type checker's job.

If you also run ruff, enable its `ASYNC` ruleset -- it covers the
standard-library cases independently. This framework does, and turning the rule
on found two blocking `Path.is_dir()` calls in its own readiness probe.

Waive one when blocking really is right:

```python
time.sleep(0)  # contracts: allow one-off at startup, not per request
```

### Interfaces

```toml
[[provides]]
name = "invoices-api"
kind = "http"
path = "/invoices"
stability = "stable"      # experimental | stable | deprecated

[[consumes]]
name = "catalog"
via_env = "API_CATALOG_URL"
```

Written down so that changing a `stable` interface is a visible decision
rather than a surprise for whoever depended on it.

### Invariants

```toml
[invariants]
rules = [
  "Money is stored in minor units as an integer. Never a float.",
  "A job handler is idempotent: delivery is at-least-once.",
]
```

The checker cannot verify these. They are here precisely because nothing else
will catch them — this is the list a reviewer, or an agent, checks by hand.

---

## Waivers

```python
from sqlalchemy import text  # contracts: allow one-off reporting query, JF-412
```

The reason is required. A waiver is a decision; `jfast contracts waivers`
lists every one, because decisions nobody revisits are exactly how a contract
stops meaning anything.

---

## What the checker deliberately does not do

It is static, AST-based and conservative. A checker that cries wolf gets an
ignore file within a week, and then the contract is decoration again.

- **Files matching no layer are not layer-checked.** You opt a path *in*.
  Guessing would produce noise on every script, migration and notebook.
- **Only static imports and direct calls are inspected.** `importlib` and
  `getattr` chains are out of scope. This is a design guardrail, not a sandbox.
- **The contract is validated first.** Two layers claiming one path, or a
  `may_import` naming a layer that does not exist, are reported as contract
  errors — because otherwise the findings are confident answers to the wrong
  question.

---

## Using it with an agent

Put this in the agent's instructions, or rely on `AGENTS.md`, which already
does:

> Before writing code here, run `jfast contracts show --json`. Before calling
> the work done, run `jfast contracts check`.

The JSON carries scope, layer boundaries, forbidden calls, interfaces and
invariants. That is enough for an agent to write code that fits the first
time, instead of code that a reviewer has to push back on.

And when it drifts anyway, the check catches it — which is the part that makes
this different from writing the same rules in prose and hoping.

---

## Commands

```bash
jfast contracts init                    # defaults for your layout
jfast contracts init --layout screaming
jfast contracts check                   # non-zero exit on a violation
jfast contracts check --json
jfast contracts show --json             # what an agent reads first
jfast contracts render                  # CONTRACTS.md
jfast contracts waivers                 # every inline exception
```

Add it to the service's CI next to the tests:

```yaml
- run: jfast contracts check
```

## A caveat worth stating

A contract catches structural drift: a layer reaching the wrong way, a
forbidden call, a missing test directory. It does not catch a wrong algorithm,
a bad name, or a rule implemented backwards.

It makes generated code *structurally* clean and it makes the rules explicit.
It does not make the code correct. Review still applies — the contract just
removes the arguments you would otherwise have every time.
