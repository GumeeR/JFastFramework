# Working with AI agents

The premise of this framework is that an agent writing code in your repository
is now normal, and that the thing which makes it survivable is not a better
prompt — it is **rules the agent cannot quietly break.**

Three surfaces, in increasing order of how much they help:

| | What it gives an agent |
| --- | --- |
| `jfast describe --json` | what is in this service, without reading a file |
| `contracts.toml` | what it may and may not do, checked by CI |
| `AGENTS.md` + `.jfast/skills/` | how this project expects work to be done |

---

## The generated agent surface

```bash
jfast new service billing --agent-docs
jfast init                              # asks
```

Writes:

```
AGENTS.md                                   the rules, at the root where agents look
.jfast/skills/respect-contracts/SKILL.md    read the contract before writing
.jfast/skills/design-system/SKILL.md        only when there is a frontend
```

**Off by default.** A project nobody points an agent at owes no agent files,
and every file shipped is a file that can drift from the code it describes.

### Why `.jfast/skills/` and not one big file

Because the value is in *not* loading all of it. A skill declares what it is
for and when to skip it:

```yaml
---
name: respect-contracts
description: Read this project's contract before writing code in it, and verify
  the code against it before calling the work done.
when_to_use: Always, before any code change in billing.
when_not_to_use: Answering a question about the project without changing it.
---
```

An agent reads the front matter of each, picks the one that matches the task,
and loads only that. One monolithic file spends the context budget on rules
that do not apply to the change in hand — and duplicates `AGENTS.md`, which
then drifts out of sync with it.

### What each file is for

**`AGENTS.md`** — the rules that apply to every change: where code goes, the
five checked rules, what not to do, and the commands. Short on purpose.

**A skill** — the procedure for one kind of task: preconditions, steps with the
exact commands, how to verify, and the mistakes people actually make.

---

## The rules an agent cannot drift past

These are not advice. `jfast contracts check` fails the build:

```
modules/payment/service.py:41: cross-module: module 'payment' imports module 'invoice'
  (two modules that need the same thing should share it: move it to shared/enums.py)
```

`file:line`, the rule, what happened, and **what to do about it**. That second
line matters more than it looks: an agent given a violation with no remedy
tends to satisfy the checker rather than fix the design — deleting the import,
inlining a copy, or turning the rule off. Naming the destination removes the
ambiguity.

The full list is in [Contracts](contracts.md). The ones that most often catch
generated code:

| Rule | Why an agent trips it |
| --- | --- |
| A router may not import `sqlalchemy` | Querying from the handler is the shortest path to a working endpoint |
| Modules may not import each other | Reusing the neighbour's model is easier than moving it |
| `shared/` may not import a module | Fixing the above by importing backwards |
| No blocking calls in `async def` | `time.sleep` and `requests` are what most examples use |

---

## Waive a line, do not delete the rule

```python
from modules.invoice.enums import Status  # contracts: allow migrating to shared
```

The waiver is on one line, with a reason, and `jfast contracts waivers` lists
every one. Turning the rule off in `contracts.toml` removes it for everybody,
silently, and the next violation goes unreported — which is how a contract
stops meaning anything.

---

## Machine-readable everything

```bash
jfast describe --json      # settings, plugin graph, routes, datastores
jfast contracts show --json  # scope, layers, forbidden calls, interfaces
jfast contracts check --json # violations, as data
jfast add --list           # the capability catalogue
```

`describe --json` is the fastest way for an agent to answer "what is in this
service" without opening twenty files, and it does not import the app to do it.

---

## What this does not solve

An agent that follows every rule can still build the wrong thing. Contracts
constrain *structure*, not intent: nothing here notices that the feature was
not what you asked for, that the test asserts the bug, or that a rule you wrote
in January is wrong in June.

What it buys is narrower and worth having anyway: the codebase does not decay
while you are not looking, and a review can be about whether the feature is
right rather than about where the file went.

---

## The framework's own agent surface

This repository practises it: `AGENTS.md` at the root and seven skills under
`.jfast/skills/`, covering module creation, plugin authoring, contracts, the
frontend and the design system. [Skills for agents](skills.md) covers writing
one.
