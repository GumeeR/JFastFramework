# Upgrading a project

```bash
jfast upgrade --check          # what breaks, for THIS project
jfast upgrade --check --json   # the same, for an agent or a CI step
```

Compares the framework version your project pins against the one installed, and
reports the changes in between **that this project can actually feel**.

---

## What it is not

It is not the changelog. `CHANGELOG.md` says what changed; the person doing the
upgrade needs to know what changes *for them*, and twenty release notes with no
way to tell which three apply is a list nobody reads twice.

So the command does not parse the changelog — it could not if it wanted to. The
breaking changes are declared as data inside the package, in
`jfastframework/upgrades.py`, each with a `detect` that inspects your project on
disk. Three reasons that file is the source and prose is not:

- **Prose is not a data format.** `### Breaking` is a heading today. Rename it
  to `### Breaking changes` and the parser reports that nothing broke — and
  reports it confidently.
- **The changelog does not ship.** The wheel contains
  `packages = ["src/jfastframework"]` and nothing else, so an installed
  framework has no changelog to read.
- **A release note is not a finding.** "Timestamps are timezone-aware" is a
  sentence. `ALTER TABLE invoices ALTER COLUMN created_at TYPE timestamptz
  USING created_at AT TIME ZONE 'UTC'` is a thing you can run.

---

## The rule that makes it worth reading

**A change this project cannot be affected by is not printed.**

That is not politeness, it is the whole design. One warning that does not apply
teaches the reader that the output is padding, and the next warning — the one
that mattered — is skipped along with it.

So each entry carries a `detect` that returns the evidence found in *your*
tree:

| Change | Reported only when |
| --- | --- |
| `timestamps-timezone-aware` | some model file imports `TimestampMixin` |
| `contracts-shared-import` | a layer in `contracts.toml` omits `"shared"` |
| `refresh-tokens-rejected` | `auth` is enabled **and** `issue_tokens = true` |
| `logout-ends-one-session` | same |
| `access-token-fam-claim` | same |
| `request-limit-defaults` | `jfast.toml` does not set the limit itself |
| `cli-exit-codes` | always — see below |

A service that only *validates* somebody else's tokens is untouched by every
change to the issuing endpoints, which is most services with `auth` on. It is
never told about them.

`cli-exit-codes` is the exception, and it is honest about being one: nothing in
a project says whether its pipeline branches on an exit code, so the entry is
marked informational and states the change unconditionally rather than guessing.

---

## What it reports

```
  0.1.0a3 → 0.1.0a4   (pinned in requirements.txt)

  ✗ timestamps-timezone-aware  [breaking, 0.1.0a4]
      TimestampMixin columns are timezone-aware. Existing tables need a migration.

      created_at and updated_at mapped to TIMESTAMP WITHOUT TIME ZONE,
      so a row serialised as 2026-08-29T20:55:15 with no offset and
      every JavaScript client read it as local time.

      in this project:
        modules/invoice/models.py  ->  invoices
          ALTER TABLE invoices
              ALTER COLUMN created_at TYPE timestamptz USING created_at AT TIME ZONE 'UTC',
              ALTER COLUMN updated_at TYPE timestamptz USING updated_at AT TIME ZONE 'UTC';

      → fix
        Write the statements above into an Alembic revision by hand.
        The USING clause is load-bearing and autogenerate omits it.

  ✗ contracts-shared-import  [breaking, 0.1.0a4]
      ...
      in this project:
        [layers.http]  may_import = ["service", "schemas", "storage"]  ->  add "shared"

  7 apply here, 5 breaking.
```

### The `USING` clause is not decoration

Alembic's autogenerate writes the bare form:

```sql
ALTER TABLE invoices ALTER COLUMN created_at TYPE timestamptz;
```

That does not fail. It converts through the implicit cast, which reads every
stored value in the **server's** `TimeZone`, and silently shifts the whole table
on any server not set to UTC. The migration this command prints is the one that
does not:

```sql
ALTER TABLE invoices
    ALTER COLUMN created_at TYPE timestamptz USING created_at AT TIME ZONE 'UTC',
    ALTER COLUMN updated_at TYPE timestamptz USING updated_at AT TIME ZONE 'UTC';
```

---

## Which version the project is on

Two places say it, and they are checked in this order:

1. `requirements.txt` — the `jfastframework[...]==X` line. This wins, because
   it is what `pip` acts on: a project upgraded by editing that line and
   reinstalling is on the new version whatever else on disk still remembers.
2. `.jfast-template` — the stamp every scaffold leaves, recording the framework
   version that generated the tree. The fallback for a project with no
   requirements file.

Neither present is a `2` (configuration error), not a silent success: the
command refuses to report on a version it had to guess.

Comparison is PEP 440-aware, so `0.1.0a10` is newer than `0.1.0a9` — which is
not what string comparison says. `packaging` is **not** a dependency of this
framework, direct or transitive, so the ordering is vendored in
`upgrades.parse_version` rather than imported; the test suite pins it against
the real `packaging`, which is installed for development.

---

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | nothing between those versions affects this project |
| `2` | no `jfast.toml`, or no pin to compare against |
| `6` | `--apply` was passed |
| `7` | something applies, or the project pins a version newer than the installed one |

`7` is `COMPATIBILITY`: the project and the installed framework do not agree.
Gate a deploy on it.

---

## `--apply` does not exist

Not "not yet in this build" — not planned for this release, on purpose.

Automatic rewriting of somebody's models, contract and settings needs a rollback
story: a clean tree to start from, a diff to review, a way back when the rewrite
is wrong. None of that exists here, and an automatic edit that is wrong costs
more than the manual one it saved. Passing `--apply` says so and exits `6`.

The report names the file and the line for every finding. Make the edits.

---

## Adding a change to the manifest

When a release breaks something, add a `Change` to `CHANGES` in
`jfastframework/upgrades.py`:

```python
Change(
    version="0.1.0a5",
    kind="breaking",              # breaking | deprecated | behaviour
    code="stable-identifier",     # what --json emits; never reuse one
    summary="One line. What broke.",
    detail="Why it broke and what the silent failure looked like.",
    detect=_something_on_disk,    # None only when nothing can decide it
    remedy="What to do, concretely.",
)
```

`detect` takes a `jfastframework.project.Project` and returns the evidence
strings — table names, layer names, the values a setting is about to acquire.
An empty list means the project is unaffected and the entry is not printed.

`Project` is read from the filesystem and never imports project code, so the
report works on a service that is broken, half-migrated, or missing its
dependencies entirely. It carries the plugin names but not their settings; read
`jfast.toml` directly when a change depends on one, as the `auth` entries do.

Write the entry with `detect=None` only when nothing on disk can decide the
question, and say so in `detail`. It is the difference between an honest
informational note and a warning people learn to ignore.
