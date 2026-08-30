# Skills: making the framework legible to agents

A skill is a folder with a `SKILL.md`: instructions for one task, written so an
agent can execute it without reconstructing context from the source tree.

## Why this exists

An agent dropped into an unfamiliar codebase greps, infers, and guesses. The
output is plausible and structurally wrong — a service where business rules
live in the router, config is read with `os.getenv`, and the module layout
matches nothing else in the fleet.

Three things fix that, and JFast ships all three:

1. **Predictable structure.** A module is generated in one of four layouts, and
   the one it got is recorded in `jfast.toml` next to its own `README.md`. The
   agent does not invent a shape; it reads which one this module has and fills
   it in.
2. **Machine-readable state.** `jfast describe --json` answers "what is
   configured here" without reading a line of source.
3. **Skills.** Task instructions, loaded selectively.

## Selective loading

The whole point is that an agent does *not* read everything. It reads each
skill's `description` and `when_to_use`, picks one, and loads only that file.

So those two fields are the interface. Write them for routing:

```yaml
description: Scaffold a domain module, choosing its layout (layered, modular,
  screaming or hexagonal), and wire it into the app.
when_to_use: The user asks for a new business entity, resource, CRUD surface,
  or database table.
when_not_to_use: The change belongs inside an existing module, or it is a
  cross-cutting capability — use create-plugin.
```

`when_not_to_use` matters as much as `when_to_use`. Most agent errors are
picking a reasonable-looking wrong tool.

## Anatomy

```markdown
---
name: kebab-case-name
description: What it does, one sentence.
when_to_use: The trigger, in the user's words.
when_not_to_use: The nearest wrong choice, and where to go instead.
---

## Preconditions
What must already be true, and the command that checks it.

## Steps
Numbered. Exact commands, not descriptions of commands.

## Verification
How to know it worked. Non-negotiable.

## Common mistakes
What goes wrong here specifically.
```

## Rules

- **One task per skill.** A skill with an "or, if instead…" branch is two
  skills.
- **Commands, not prose.** `jfast new module order`, not "use the CLI to
  generate a module".
- **Always a verification section.** A skill that cannot check its own output
  produces work nobody validated.
- **Name the limits.** `add-rag` states up front that chunking is naive and
  there is no reranking. A skill that oversells produces confident bad advice.
- **Under 150 lines.** Longer means the task needs decomposing.

## Skills vs AGENTS.md

| | Scope |
| --- | --- |
| `AGENTS.md` | Rules for every task: layer boundaries, hard prohibitions, how to verify anything |
| `SKILL.md` | Steps for one task |

Do not duplicate. A rule that applies everywhere belongs in `AGENTS.md`, and a
skill that restates it will drift out of sync with it.

## DESIGN.md

The same idea applied to visual design: a markdown file stating palette, type
scale, spacing, and component states, which the `design-system` skill consumes.
Place it at the frontend root, or next to a module that owns its own surface.

The value is not the document — it is that tokens get decided once instead of
being invented per component.

## Writing a new skill

1. Do the task manually, recording every command.
2. Write it up. Cut everything that is not a command or a decision.
3. Add `when_not_to_use` by asking: what would an agent reach for this instead
   of, by mistake?
4. Hand it to an agent with no other context and watch. Anything it asks about
   is a gap in the skill.

Step 4 is the one people skip, and it is the one that finds the real gaps.
