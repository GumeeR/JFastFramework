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
may_import = ["shared"]
forbid_packages = ["fastapi", "sqlalchemy", "pydantic"]

[layers.http]
paths = ["modules/*/http.py"]
may_import = ["use_cases", "domain", "shared"]
```

`may_import` names **other layers**, not packages. Layers are what a reviewer
argues about; package names are what they forget.

A file is classified by the **most specific** matching pattern — fewest
wildcards, not longest string. That distinction is load-bearing:
`modules/*/[!_]*.py` is longer than `modules/*/http.py`, and ranking by length
would classify every router as domain code and then reject its imports for a
reason nobody could work out.

#### `shared` is on every list, and on none of its own

Every generated contract declares a `shared` layer for `shared/*.py`, and every
other layer may import it — the domain included. That is not a loosening: it is
what makes the advice in [shared/, enums, and channels](shared-and-events.md)
legal. `[rules.placement]` tells you to move the twice-wanted enum into
`shared/`, and a layer that could not import `shared/` had no way to obey the
instruction the checker itself printed.

It stays safe because `shared` keeps `may_import = []` and forbids `sqlalchemy`
and `fastapi` on itself. Nothing can reach a database or a router through an
enum, and the direction stays one-way — which `[rules.placement]` checks from
the other side.

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

## Why a rule exists: `contracts explain`

`contracts check` says a rule was broken. It does not say *why the rule is
there*, and an agent handed a violation with no remedy tends to satisfy the
checker rather than fix the design — by deleting the import, copying the code
into the second module, or turning the rule off. `explain` closes that:

```bash
jfast contracts explain billing analytics          # may billing import analytics?
jfast contracts explain http sqlalchemy            # may the http layer import it?
jfast contracts explain --rule shared-direction    # what is that rule, and where
jfast contracts explain --file modules/billing/service.py
jfast contracts explain --json
jfast contracts explain                            # every rule that can fire here
```

```
may module 'invoice' import module 'customer'?  [FORBIDDEN]

  rule     cross-module  -- one module imported another module
  what     modules may not import each other: 'invoice' and 'customer' would become one module
           with a folder between them
  declared contracts.toml:110
           [rules.placement]
  why      Two modules that import each other are one module with a folder between them. Neither
           can be extracted into a service later, and a change to one breaks the other in a way
           no test covers. So when a second module needs the same enum, type or pure function,
           it moves to shared/ -- and the check names the file.
  instead  - move what both modules need into shared/models.py, then import it from both
           - if only 'invoice' needs it, it belongs in 'invoice'
           - waive this one line with # contracts: allow <reason> while the move is in flight

  waiver   # contracts: allow <reason> -- one line, and only the rule that fired on it
           jfast contracts waivers lists every one, so it stays a reviewable decision
           deleting the rule from contracts.toml removes it for every file and for everyone,
           silently, and nothing reports the next violation
```

Four things, and the second is the one nothing else provided:

* **Which rule** forbids it, in the same vocabulary `check` prints.
* **Where it is declared** — `contracts.toml` and a line number, with the line
  itself, so the claim can be checked rather than believed. Each layout
  declares different layers at different lines, and the answer follows the
  contract in front of it.
* **Why the rule is there**, quoted from the comment its author wrote above
  that declaration. The generated contracts carry a rationale above every rule;
  this reads it rather than inventing prose. Where there is no comment, the
  layer's `description` and the rule's `why` are used instead.
* **What to do instead**, naming a destination — `shared/models.py`, not "do
  not do that" — and **what waiving costs**, both halves of it: the inline
  waiver takes one line and stays listed by `jfast contracts waivers`, while
  editing `contracts.toml` removes the rule for everyone, in silence.

### When it cannot answer

An answer it cannot derive is reported as `[UNKNOWN]` with what it does know —
the layers the contract declares, the modules on disk — and the command exits
non-zero, so a script can tell "no" from "I do not know". The same applies to a
file no layer claims: that is not a pass, it is a path nobody opted in.

`--json` carries all of it, plus every layer's `paths`, `may_import`,
`forbid_packages` and `description`, and the live violations for the file being
asked about. It is meant to be complete enough that a model never has to open
`contracts.toml` itself — because a model that opens it is one edit away from
deleting the rule.

`--contract PATH` points at a `contracts.toml` (or the directory holding one)
when the command is not run from inside the service.

---

## What changed structurally: `contracts diff`

```bash
jfast contracts diff
jfast contracts diff --json
```

```
Architecture changes  (billing)

  + invoice -> customer           cross-module  modules/invoice/enums.py:33
                                  modules may not import each other
  - http -> shared                permitted, and no import uses it  declared at contracts.toml:32
  - storage -> schemas            permitted, and no import uses it  declared at contracts.toml:47

Potential breaking change:
  invoice loses access to customer.Customer when that import goes -- move it to shared/models.py
    and import it from both
```

**It is not a git diff, and the limit is worth stating plainly.** Nothing here
reads a previous revision or knows what the code looked like yesterday. It
compares the architecture `contracts.toml` *permits* against the imports the
code *makes*:

* `+` is an edge the code has and the contract does not permit — the same
  finding `contracts check` reports, restated as an architecture change, with
  the symbols that cross the edge.
* `-` is an edge the contract permits that no import uses: a permission that
  could be tightened, not something that was removed.
* **Potential breaking change** lists what enforcing the contract costs: which
  name the importer loses if that edge goes, and where to move it. That is the
  difference between moving the code and deleting the import.

Only static imports count, and only between declared layers and directories
under `modules/`. Reporting only — it always exits zero; the build is failed by
`contracts check`.

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
> the work done, run `jfast contracts check`. When it reports a violation, run
> `jfast contracts explain --rule <rule> --json` before changing anything —
> and never edit `contracts.toml` to make a check pass.

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
jfast contracts explain <a> <b>         # why that import is refused, and what to do
jfast contracts explain --rule layer    # what a reported rule means, and where it lives
jfast contracts diff                    # permitted architecture vs. the built one
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
