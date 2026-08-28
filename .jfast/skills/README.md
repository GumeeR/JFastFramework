# Skills

A skill is a folder holding one `SKILL.md`: instructions for a task an agent
performs repeatedly in this codebase.

The point is selective loading. An agent should not read the entire manual to
add one module — it reads the front matter of each skill, picks the one that
matches, and loads only that file.

## Layout

```
.jfast/skills/
├── README.md
├── create-module/SKILL.md      scaffold a domain module: layout, UI, wiring
├── create-plugin/SKILL.md      build a plugin that adds a capability
├── build-frontend/SKILL.md     server-rendered UI with Jinja2 and HTMX
├── add-rag/SKILL.md            semantic search over pgvector or Qdrant
└── design-system/SKILL.md      apply a DESIGN.md to generated UI
```

## Format

Front matter, then the steps.

```markdown
---
name: create-module
description: Scaffold a domain module. Use when adding a new business entity
  with its own table and endpoints.
when_to_use: The user asks for a new resource, entity, CRUD surface, or table.
when_not_to_use: The change fits inside an existing module, or adds a
  cross-cutting capability (that is a plugin).
---

## Steps
1. ...
```

`description` and `when_to_use` are the routing signal. Write them so an agent
can decide from those two lines alone whether to open the file.

## Rules

- One task per skill. If a skill has two "or else" branches, it is two skills.
- Give exact commands, not descriptions of commands.
- State the verification step. A skill that does not say how to check its own
  output produces work nobody validated.
- Keep it under 150 lines. Longer means the task is not decomposed.

## Related conventions

- `DESIGN.md` next to a module or frontend describes the visual language:
  palette, type scale, spacing, component patterns. The `design-system` skill
  consumes it.
- `AGENTS.md` at the repository root holds rules that apply to every task.
  Skills hold rules for one task. Do not duplicate between them.
