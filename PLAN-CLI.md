# JFastFramework — the CLI as a lifecycle tool

A proposal, originating in a multi-model review of the CLI. It is recorded here
rather than in [PLAN.md](PLAN.md) because nothing in it is accepted yet, and
because [STATUS.md](STATUS.md)'s rule applies to plans too: a command is not
real until something exercises it.

Legend: `[x]` shipped · `[~]` partially shipped · `[ ]` proposed · `[!]` needs a
decision before it can be built

**Status:** the P0 tier below has landed -- `inspect`, `analyze`, `graph`,
standard exit codes and `--json` on all three. See
[docs/inspect.md](docs/inspect.md). Everything else here is still a proposal.

---

## The thesis

The CLI today is a **generator**. It is very good at the first ten minutes of a
project and has almost nothing to say at month six. Every question a maintainer
actually asks — *what is in here, what depends on what, is this still legal, is
this migration safe, can this module be pulled out* — is answered today by
reading files.

The proposal is that the CLI grow into a tool over the whole lifecycle:

```
CREATE -> INSPECT -> ANALYZE -> ADOPT -> MODIFY -> VALIDATE -> MIGRATE -> UPGRADE -> EXTRACT -> OPERATE
```

The framework already knows the answers. It holds the plugin graph, the layer
rules, the module layout, the workspace topology and the resource links. Not
exposing them means an agent — or a person — reaches for `grep` to rediscover
what `jfast.toml` already states.

That is the strongest argument in the review, and it stands.

---

## What the audit changed

The review was checked against the code before being written down. Three
corrections.

### 1. The exit-code finding was wrong as stated — and led to a real bug

The review claims `jfast contracts check` can return `0` with violations
present. The code does not support that:

```python
if violations:
    raise typer.Exit(1)
```

But the *symptom* reproduced. A scaffolded service, a planted violation:

```bash
jfast new service shop && cd shop && jfast new module invoice
echo 'from sqlalchemy import select' >> modules/invoice/router.py
jfast contracts check
#   OK  shop: no violations
#   EXIT=0
```

The checker was right. **The contract was empty.** Every layout's docs promise
that a router cannot import SQLAlchemy — `docs/agents.md` lists it first in the
table of rules that catch generated code — and no generated `contracts.toml`
carried the rule. `layers.http` declared `may_import` (which constrains other
*layers*, not packages) and no `forbid_packages` at all. Same hole in all four
layouts, including `adapters` in hexagonal.

So the advertised rule had never been enforced in any generated project.

Fixed: `forbid_packages = ["sqlalchemy"]` on the outermost layer of all four
contract templates. Verified both directions — the four layouts still generate
and pass their own contract, and the planted import now fails:

```
modules/invoice/router.py:82: layer-package: 'http' must not import 'sqlalchemy'
1 violation(s).
EXIT=1
```

The lesson generalises past this one rule, and is the real reason to keep this
document: **a documented rule that no default config enables is
indistinguishable from a rule that does not exist.** Any future `jfast check`
should test the generated defaults against the documentation, not only the
checker against hand-written fixtures.

### 2. `jfast init` is taken

The review proposes `jfast init` for "adopt an existing directory". That name
already ships, with a different meaning: an interactive installer that scaffolds
a *new* service (kind, frontend, datastores). Reusing it would break every
existing user for a feature they did not ask for.

The adoption flow is `jfast adopt` and nothing else. `init` stays as it is.

### 3. Eleven of the proposed commands already exist

Proposing them again would have produced duplicate surface. Current inventory:

| Proposed as new | Already shipped as |
| --- | --- |
| `inspect` | `describe --json`, `plugins list`, `contracts show --json` |
| `graph` | `workspace graph` (services, not modules) |
| `boundaries` | `contracts show --json` (layers and interfaces) |
| `contracts check/list` | `contracts check`, `contracts show`, `contracts waivers` |
| `module create` | `new module` |
| `service create` | `new service` |
| `service list` | `workspace list` |
| `deploy build` | `deploy compose`, `deploy dockerfile`, `deploy function` |
| `validate` (workspace) | `workspace validate` |
| `agent init` | `new service --agent-docs`, and `init` asks |
| `doctor` | `doctor` |

What is missing is not most of these commands. It is that they are **scattered
across five noun namespaces with no single entry point**, and that only three of
them speak JSON.

---

## Proposed shape of the help

```
Project        new · init · adopt · upgrade
Development    dev · serve · test · build
Architecture   inspect · analyze · graph · module · contracts · boundaries · dependencies
Infrastructure db · cache · queue · storage · deploy · health
Migration      migrate · migration · diff
AI             ai · skills · context
Services       service · extract · link · unlink · workspace
Diagnostics    doctor · check · validate
```

Grouping is not cosmetic. Fifteen flat commands is a list; forty flat commands
is a wall, and the current help is already at the edge of readable.

---

## Command inventory

### Project

| Command | State | Notes |
| --- | --- | --- |
| `new service` / `new module` / `new enum` / `new view` | `[x]` | |
| `init` | `[x]` | Interactive installer. **Keeps its current meaning.** |
| `adopt` | `[ ]` | Analyse an existing FastAPI project: detect stack, count routers/models/services, propose module boundaries, estimate difficulty. Read-only. |
| `adopt plan` | `[ ]` | Phased migration plan. Writes nothing. |
| `adopt preview` | `[ ]` | Would-create / would-modify / would-move / would-not-touch. |
| `adopt apply` | `[!]` | Moves files. Needs a rollback story before it is built — see *Open questions*. |
| `upgrade` | `[ ]` | Diff the project against a newer framework version: breaking changes, deprecations, automatic migrations, manual actions. `--check` / `--dry-run` / `--apply`. |

`adopt` is the highest-leverage item in the whole proposal. It is the only one
that changes who can use the framework: today adoption means starting over.

### Architecture

| Command | State | Notes |
| --- | --- | --- |
| `inspect` | `[x]` | Modules, shape, routes, wiring, plugins, migrations, contract. Static: never imports project code, so it works on a project that does not run. |
| `inspect module <name>` | `[x]` | Files, packages, tables, tests, README. |
| `inspect plugin` / `service` / `config` | `[ ]` | The other nouns. `plugins list` and `describe` already answer two of them; wrapping beats duplicating. |
| `analyze` | `[x]` | Eight checks, severity-bucketed, each with the fix. N+1 and unused dependencies deliberately left out -- see *Where this proposal should be trimmed*. |
| `graph` | `[x]` | Module-level, `--module`, ascii/mermaid/dot/json. `workspace graph` still covers services. |
| `boundaries` | `[~]` | `contracts show --json` has the data; the effective public/allowed/forbidden view is new. |
| `dependencies` | `[ ]` | Direct, transitive, external, plugin and module dependencies. `--unused`. |
| `module list` / `inspect` / `validate` / `graph` | `[ ]` | |
| `module rename` | `[!]` | A real refactor: imports, contracts, skills, tests. Must be `--dry-run` first and must report the blast radius. Wrong here is worse than absent. |
| `module delete` | `[!]` | Same. Deleting a module with inbound references should refuse, not warn. |
| `contracts explain <a> <b>` | `[ ]` | Why a dependency is forbidden. The single best thing that can be handed to an agent that just hit a violation. |
| `contracts diff` | `[ ]` | Architecture change between HEAD and the working tree. Built for PR review. |
| `contracts fix` | `[!]` | Only genuinely safe fixes, `--dry-run` before `--apply`. An autofix that edits architecture to satisfy a checker is the failure mode the checker exists to prevent. |

### Migration

| Command | State | Notes |
| --- | --- | --- |
| `migration check` | `[ ]` | `NOT NULL` on a populated table, destructive drops, missing indexes, problematic defaults. |
| `migration plan` | `[ ]` | Risk, reason, and the safe three-step rewrite (nullable, backfill, constrain). |
| `migration create` / `inspect` / `diff` / `validate` | `[ ]` | Thin over Alembic. Do not reimplement Alembic. |
| `db status/migrate/rollback/shell/inspect/stats` | `[ ]` | Uniform front over what already works. |

`migration check` is the second-highest-leverage item, and the one an agent most
needs: a model writing `server_default` on a 50k-row table produces a migration
that passes review and locks a table in production.

### Infrastructure

| Command | State | Notes |
| --- | --- | --- |
| `cache` / `queue` / `storage` — `status`, `inspect`, `test` | `[ ]` | Must probe the real dependency, not read config. |
| `storage verify` | `[ ]` | Whether uploads survive a rebuild. Catches the ephemeral-container-filesystem mistake. |
| `deploy validate` | `[ ]` | Secrets, volumes, workers, ports, health checks, `.env` baked into an image. |
| `deploy plan/run/status/doctor` | `[!]` | `run` makes the CLI a deployment tool. Out of scope until asked for. |
| `health` | `[~]` | `/health` and `/ready` exist in the app. A CLI probe is new. |

### Diagnostics

| Command | State | Notes |
| --- | --- | --- |
| `doctor` | `[x]` | Environment. "What is wrong with my machine." |
| `validate` | `[ ]` | Project. "Is this project internally consistent." |
| `check` | `[ ]` | The battery: config, plugins, skills, architecture, contracts, migrations, typing, tests, deployment. `--ci` for strict exit codes. |

Three commands with three distinct meanings is defensible; the boundary has to
be documented on the first day or they collapse into synonyms.

### Services

| Command | State | Notes |
| --- | --- | --- |
| `service inspect` / `validate` | `[ ]` | |
| `service boundary <module>` | `[ ]` | Extraction readiness as a percentage with named blockers: direct database access, shared transactions, implicit coupling. |
| `extract <module>` | `[!]` | The most interesting command in the proposal and the most dangerous. `--dry-run` must exist before `--apply`. |
| `extract --language go` | `[ ]` | Only where the service contract fully determines the surface. Not soon. |

### AI

| Command | State | Notes |
| --- | --- | --- |
| `ai context` / `architecture` / `rules` / `commands` `--json` | `[~]` | `describe --json` and `contracts show --json` are two thirds of this. |
| `ai diff` | `[ ]` | Files changed, modules affected, contracts affected, tests required, risk. |
| `context --module <m>` | `[ ]` | Shortcut. |
| `skills list/show/add/remove/create/validate` | `[ ]` | Skills are files today; nothing manages them. |
| `skills diff` / `sync` | `[ ]` | Project skill against framework skill. Conflict detection is the hard half. |
| `ai task "<intent>"` | `[!]` | **Recommend deferring.** See below. |
| `prompt` | `[!]` | **Recommend deferring.** See below. |

---

## Where this proposal should be trimmed

Fifty new commands is not a roadmap, it is a wish. Two specific rejections, with
reasons rather than a shrug:

**`ai task "add notifications"` and `jfast prompt` should not ship in this
cycle.** Both bake prompt engineering into a framework release. A prompt tuned
for today's models is stale in a quarter, but it ships on the framework's
version cadence and every user's project pins it. Worse, neither has a testable
success condition: `jfast contracts check` either finds a violation or does not,
while "was this the right plan" cannot be asserted in CI, so both commands would
sit permanently at `unverified` under STATUS.md's own rule.

The durable half of what they promise is `ai context --json` and
`ai diff --json` — facts, not suggestions. Ship the facts. Let the agent do the
prompting; that is what it is for.

**`deploy run` should stay out.** Generating deployment artefacts is in scope.
Executing deployments makes the CLI responsible for credentials, rollback and
state it does not hold.

---

## Cross-cutting rules

These matter more than any single command, and they are cheap only if adopted
before the surface grows.

**`--json` is not an AI feature.** Every command that reports rather than acts
takes it. The schema carries `schema_version` from the first release, because
the alternative is agents parsing human output and breaking on a reworded line.

**One flag for rehearsal.** `--dry-run` or `--preview`, never both. The review
uses them interchangeably; that inconsistency has to die before it ships.
Recommendation: `--dry-run`, because it is the convention everywhere else.

**Standard exit codes**, documented and tested:

```
0  success
1  validation failure
2  configuration error
3  environment error
4  migration risk or failure
5  contract violation
6  user input error
7  compatibility or version error
```

Distinguishing 1 from 5 is what lets CI say *why* it failed without parsing text.

**Idempotence.** Anything that creates or configures is safe to run twice and
says so:

```
Skill already installed. Nothing changed.
```

**Every command that matters carries all six**: human output, machine-readable
output, an exit code, validation, documentation, and a test. The CLI is a public
API. A command that only ever prints is a command whose behaviour nothing pins.

---

## Order of implementation

The review's own ordering, with the P0 tier cut roughly in half — twelve
commands is not a first milestone, it is four.

**P0 — the facts the framework already holds**

- [x] Standard exit codes, documented and tested (`jfastframework.cli.exits`)
- [x] `--json` on `inspect`, `analyze` and `graph`, with `schema_version`
- [x] `inspect`, and `inspect module <name>`
- [x] `analyze`, with `--fail-on`
- [x] `graph` at module level, ascii/mermaid/dot/json
- [ ] `--json` on the *remaining* reporting commands
- [ ] `check`, `check --ci` -- the battery that runs all of the above at once
- [ ] Generated-default coverage test: every rule the docs promise is enabled in
      the templates that ship. The contract bug above was found by hand; nothing
      yet stops the next one.

**P1 — adoption and change**

- [ ] `adopt`, `adopt plan`, `adopt preview`
- [ ] `migration check`, `migration plan`
- [ ] `contracts explain`, `contracts diff`
- [ ] `module rename --dry-run`
- [ ] `ai context --json`, `ai diff --json`
- [ ] `skills list/show/add/validate`
- [ ] `upgrade --check`

**P2 — extraction and operations**

- [ ] `service boundary`
- [ ] `extract --dry-run`
- [ ] `deploy validate`, `storage verify`
- [ ] `db` / `cache` / `queue` diagnostics
- [ ] `skills sync`
- [ ] `adopt apply`, `upgrade --apply`

**P3 — not yet**

- [ ] `extract --apply`, cross-language extraction
- [ ] `contracts fix --apply`
- [ ] Remote skill registry
- [ ] Automated migration rewriting

---

## Open questions

**Rollback for `adopt apply` and `extract --apply`.** Both move source files
across a whole repository. "Create a git checkpoint first" is advice, not a
mechanism, and the projects most likely to run `adopt` are the ones least likely
to have a clean tree. Either the CLI owns a real undo or these stay
rehearsal-only. Not decided.

**Where `analyze` stops.** N+1 detection by static analysis produces false
positives, and a linter that cries wolf gets muted — after which the true
positives are lost too. A smaller pass that is always right beats a broad one
that is usually right.

**Whether `inspect <kind> <name>` replaces the noun namespaces or wraps them.**
Two ways to ask the same question is a documentation cost forever. Wrapping is
the safe start; replacing needs a deprecation window.

---

## What this document is not

It is not a commitment. Nothing here has been built except the contract-template
fix described above, which is already in `CHANGELOG.md`. The value of writing it
down now is that the CLI's surface is still small enough to reshape, and the
cross-cutting rules — exit codes, `--json`, one rehearsal flag — cost nothing
today and cannot be retrofitted once forty commands depend on them.
