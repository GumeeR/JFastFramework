# Trabajar con agentes de IA

La premisa de este framework es que un agente escribiendo código en tu
repositorio ya es normal, y que lo que lo hace sobrevivible no es un prompt
mejor — son **reglas que el agente no puede romper en silencio.**

Tres superficies, en orden creciente de cuánto ayudan:

| | Qué le da a un agente |
| --- | --- |
| `jfast describe --json` | qué hay en este servicio, sin leer un archivo |
| `contracts.toml` | qué puede y qué no, verificado por CI |
| `AGENTS.md` + `.jfast/skills/` | cómo espera este proyecto que se trabaje |

---

## La superficie de agente generada

```bash
jfast new service billing --agent-docs
jfast init                              # asks
```

Escribe:

```
AGENTS.md                                   the rules, at the root where agents look
.jfast/skills/respect-contracts/SKILL.md    read the contract before writing
.jfast/skills/design-system/SKILL.md        only when there is a frontend
```

**Apagado por defecto.** Un proyecto al que nadie le apunta un agente no debe
archivos de agente, y cada archivo que se envía es un archivo que puede
desviarse del código que describe.

### Por qué `.jfast/skills/` y no un archivo grande

Porque el valor está en *no* cargarlo todo. Una skill declara para qué sirve y
cuándo saltarla:

```yaml
---
name: respect-contracts
description: Read this project's contract before writing code in it, and verify
  the code against it before calling the work done.
when_to_use: Always, before any code change in billing.
when_not_to_use: Answering a question about the project without changing it.
---
```

Un agente lee el front matter de cada una, elige la que corresponde a la tarea,
y carga solo esa. Un archivo monolítico gasta el presupuesto de contexto en
reglas que no aplican al cambio en curso — y duplica `AGENTS.md`, que entonces
se desincroniza.

### Para qué es cada archivo

**`AGENTS.md`** — las reglas que aplican a todo cambio: dónde va el código, las
cinco reglas verificadas, qué no hacer, y los comandos. Corto a propósito.

**Una skill** — el procedimiento para un tipo de tarea: precondiciones, pasos
con los comandos exactos, cómo verificar, y los errores que la gente comete de
verdad.

---

## Las reglas de las que un agente no se puede desviar

No son consejos. `jfast contracts check` rompe el build:

```
modules/payment/service.py:41: cross-module: module 'payment' imports module 'invoice'
  (two modules that need the same thing should share it: move it to shared/enums.py)
```

`archivo:línea`, la regla, qué pasó, y **qué hacer al respecto**. Esa segunda
línea importa más de lo que parece: un agente al que le das una violación sin
remedio tiende a satisfacer al checker en vez de arreglar el diseño — borrando
el import, copiando el código, o apagando la regla. Nombrar el destino elimina
la ambigüedad.

La lista completa está en [Contratos](contracts.md). Las que más seguido
atrapan código generado:

| Regla | Por qué la pisa un agente |
| --- | --- |
| Un router no puede importar `sqlalchemy` | Consultar desde el handler es el camino más corto a un endpoint que anda |
| Los módulos no se importan entre sí | Reusar el modelo del vecino es más fácil que moverlo |
| `shared/` no puede importar un módulo | Arreglar lo anterior importando al revés |
| Nada bloqueante en `async def` | `time.sleep` y `requests` es lo que usan casi todos los ejemplos |

---

## Exime una línea, no borres la regla

```python
from modules.invoice.enums import Status  # contracts: allow migrating to shared
```

La exención va en una línea, con una razón, y `jfast contracts waivers` las
lista todas. Apagar la regla en `contracts.toml` la quita para todo el mundo,
en silencio, y la próxima violación no se reporta — que es cómo un contrato
deja de significar algo.

---

## Todo legible por máquina

```bash
jfast describe --json        # settings, grafo de plugins, rutas, datastores
jfast contracts show --json  # alcance, capas, llamadas prohibidas, interfaces
jfast contracts check --json # violaciones, como datos
jfast add --list             # el catálogo de capacidades
```

`describe --json` es la forma más rápida de que un agente responda "qué hay en
este servicio" sin abrir veinte archivos, y no importa la app para hacerlo.

---

## Lo que esto no resuelve

Un agente que sigue todas las reglas igual puede construir lo equivocado. Los
contratos restringen *estructura*, no intención: nada acá se da cuenta de que
la funcionalidad no era la que pediste, de que el test afirma el bug, o de que
una regla que escribiste en enero está mal en junio.

Lo que sí compras es más angosto y vale igual: el código no se degrada mientras
no estás mirando, y un review puede ser sobre si la funcionalidad está bien en
vez de sobre dónde quedó el archivo.

---

## La superficie de agente del propio framework

Este repositorio lo practica: `AGENTS.md` en la raíz y siete skills bajo
`.jfast/skills/`, cubriendo creación de módulos, autoría de plugins, contratos,
el frontend y el sistema de diseño. [Skills para agentes](skills.md) cubre cómo
escribir una.
