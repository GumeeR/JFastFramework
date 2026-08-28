---
name: design-system
description: Apply a DESIGN.md to generated UI so screens share one visual
  language instead of looking like default templates.
when_to_use: Building or restyling any user-facing surface — a dashboard, an
  admin panel, a landing page, a Vue 3 frontend for a service.
when_not_to_use: Backend-only work, or a change that touches no rendered
  output.
---

## The contract

A `DESIGN.md` is the design system in a form an agent can read. Place it at the
frontend root, or next to a module that owns its own surface.

```markdown
# DESIGN.md — <product>

## Voice
Two or three adjectives, and one line on what to avoid.

## Color
--bg, --surface, --border, --text, --text-muted, --accent, --danger
Light and dark values for each. Every token defined in both.

## Type
Family, scale (e.g. 12 / 14 / 16 / 20 / 28 / 40), weights in use, line heights.

## Space
One base unit, and the multiples allowed. Nothing off-scale.

## Radius, elevation, motion
Border radii in use. Shadow levels. Transition durations and easing.

## Components
For each: anatomy, states (default / hover / active / disabled / loading /
empty / error), and what it must never do.
```

## Steps

1. **Find the DESIGN.md.** Check the frontend root, then the module, then the
   repository root. If none exists, write one *before* writing components —
   otherwise the tokens get invented per file and never converge.

2. **Emit tokens once**, as CSS custom properties on `:root`, with a dark
   variant. Both themes get every token; never define a color only inside a
   media query.

3. **Consume tokens only.** No literal hex values, no one-off pixel values in
   components. If a needed value is missing from the system, add it to
   `DESIGN.md` first, then use it. That ordering is what keeps the system real.

4. **Cover every state.** Loading, empty and error states are where generated
   UI reveals itself as generated. An empty state with no copy is a bug.

5. **Check contrast.** Body text at 4.5:1 against its background, large text at
   3:1. A palette that fails contrast is not a style preference.

## Vue 3 conventions in this stack

- `<script setup>` and the Composition API. Never Options API.
- One Pinia store per domain: `useOrderStore`, not one global store.
- HTTP calls live in `src/api/<module>.js`. Not in components, not in stores.
- Components `PascalCase.vue`; route-level views end in `View.vue`.

```
src/
├── api/         one file per backend module
├── components/  reusable, presentational
├── views/       one per route
├── stores/      one per domain
└── router/
```

## Verification

- Toggle light and dark. Every surface, border and text token resolves in both.
- Resize to 375px wide. Nothing scrolls horizontally.
- Grep the diff for `#` hex literals and hardcoded `px` outside the token file.
  Any hit is a violation.
- Tab through the page. Focus is visible at every stop.

## Common mistakes

- Copying a reference product's look literally. `DESIGN.md` references are for
  calibrating quality and patterns, not for cloning.
- Tokens defined only for the light theme.
- New values invented mid-component instead of added to the system.
- Shipping the happy path with no empty or error state.
