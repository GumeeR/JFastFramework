# Skills: hacer el framework legible para los agentes

Una skill es una carpeta con un `SKILL.md`: instrucciones para una tarea,
escritas para que un agente pueda ejecutarla sin reconstruir el contexto a
partir del árbol de fuentes.

## Por qué existe esto

Un agente que cae en un codebase desconocido hace grep, infiere y adivina. El
resultado es plausible y estructuralmente incorrecto — un servicio donde las
reglas de negocio viven en el router, la config se lee con `os.getenv`, y la
distribución del módulo no coincide con la de ningún otro de la flota.

Tres cosas arreglan eso, y JFast trae las tres:

1. **Estructura predecible.** Un módulo se genera en uno de cuatro layouts, y
   el que le tocó queda registrado en `jfast.toml`, al lado de su propio
   `README.md`. El agente no inventa una forma; lee cuál tiene este módulo y la
   completa.
2. **Estado legible por máquina.** `jfast describe --json` responde "qué hay
   configurado aquí" sin leer una línea de código fuente.
3. **Skills.** Instrucciones de tarea, cargadas selectivamente.

## Carga selectiva

Todo el punto es que un agente *no* lee todo. Lee el `description` y el
`when_to_use` de cada skill, elige una, y carga solo ese archivo.

Así que esos dos campos son la interfaz. Escríbelos para enrutar:

```yaml
description: Scaffold a domain module, choosing its layout (layered, modular,
  screaming or hexagonal), and wire it into the app.
when_to_use: The user asks for a new business entity, resource, CRUD surface,
  or database table.
when_not_to_use: The change belongs inside an existing module, or it is a
  cross-cutting capability — use create-plugin.
```

`when_not_to_use` importa tanto como `when_to_use`. La mayoría de los errores
de un agente son elegir una herramienta equivocada que se veía razonable.

## Anatomía

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

## Reglas

- **Una tarea por skill.** Una skill con una rama "o, si en cambio…" son dos
  skills.
- **Comandos, no prosa.** `jfast new order`, no "usa el CLI para generar un
  módulo".
- **Siempre una sección de verificación.** Una skill que no puede comprobar su
  propia salida produce trabajo que nadie validó.
- **Nombra los límites.** `add-rag` dice de entrada que el chunking es ingenuo
  y que no hay reranking. Una skill que se sobrevende produce malos consejos
  dichos con confianza.
- **Menos de 150 líneas.** Más largo significa que la tarea hay que
  descomponerla.

## Skills vs AGENTS.md

| | Alcance |
| --- | --- |
| `AGENTS.md` | Reglas para toda tarea: límites de capas, prohibiciones duras, cómo verificar cualquier cosa |
| `SKILL.md` | Pasos para una tarea |

No dupliques. Una regla que aplica en todos lados va en `AGENTS.md`, y una
skill que la repite se va a desincronizar de ella.

## DESIGN.md

La misma idea aplicada al diseño visual: un archivo markdown que declara
paleta, escala tipográfica, espaciado y estados de los componentes, y que la
skill `design-system` consume. Ponlo en la raíz del frontend, o al lado de un
módulo que sea dueño de su propia superficie.

El valor no es el documento — es que los tokens se deciden una vez en vez de
inventarse por componente.

## Escribir una skill nueva

1. Haz la tarea a mano, registrando cada comando.
2. Escríbela. Corta todo lo que no sea un comando o una decisión.
3. Agrega `when_not_to_use` preguntándote: ¿en lugar de qué otra cosa un
   agente agarraría esta por error?
4. Dásela a un agente sin ningún otro contexto y observa. Cualquier cosa que
   pregunte es un hueco en la skill.

El paso 4 es el que la gente se salta, y es el que encuentra los huecos de
verdad.
