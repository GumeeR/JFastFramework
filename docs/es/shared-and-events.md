# shared/, enums y channels

Dos features, una misma idea: **acoplamiento fácil de agregar, invisible una
vez agregado, y caro de sacar después de que una segunda persona copió el
patrón.** Por eso los dos se verifican en vez de acordarse.

---

## Dónde va un enum

```bash
jfast new enum InvoiceStatus --module invoice
jfast new enum Currency --shared
```

Si lo corres sin flag te pregunta, porque la ubicación *es* la decisión:

```
  Will more than one module use it?  yes puts it in shared/, no puts it in one module
```

No hace falta que aciertes el primer día. Arranca un enum en el módulo que lo
necesita, y el check te avisa el día en que un segundo lo quiere.

### La regla

**Los módulos no se importan entre sí.** Dos módulos que se meten uno en el
otro son un solo módulo con una carpeta en medio — ninguno se puede extraer a
un servicio después, y un cambio en uno rompe al otro de una forma que ningún
test cubre.

```
modules/payment/service.py:41: cross-module: module 'payment' imports module 'invoice'
  (two modules that need the same thing should share it: move it to shared/enums.py)
```

Moverlo a `shared/` limpia el finding. El mensaje nombra el archivo, así que el
arreglo no necesita una discusión de diseño.

**La dirección es de una sola vía**, y eso también se verifica:

```
shared/enums.py:43: shared-direction: shared/ imports modules.invoice
  (the direction is one-way: modules use shared/, never the reverse,
   or the graph becomes a circle)
```

Sin esa segunda regla `shared/` se vuelve el lugar donde termina todo, que es
el modo de falla de todo paquete `utils` jamás escrito.

### Qué pertenece a shared/

| Pertenece | No pertenece |
| --- | --- |
| Enums que hablan dos módulos | Cualquier cosa que use un solo módulo |
| Value objects y tipos compartidos | Cualquier cosa que toque la base de datos |
| Funciones puras | Cualquier cosa que haga una llamada HTTP |

La línea de la base de datos es la que importa, y el `contracts.toml` generado
la hace cumplir prohibiendo `sqlalchemy` en `shared/`. **Dos módulos
compartiendo un repositorio son dos módulos compartiendo una tabla**, y así es
como un conjunto de servicios se convierte en un monolito distribuido con
latencia de más.

### Por qué `str, Enum`

Los dos templates lo usan, y la razón no es estética. Un `Enum` pelado
serializa como `Status.DRAFT` por algunos caminos y como `"DRAFT"` por otros;
heredar de `str` hace que un miembro sea un string en todos lados — en JSON, en
SQL y en una línea de log.

Y **el valor es el formato de cable**. Se guarda en una columna, se serializa
en una respuesta de API y lo lee un frontend. Renombrar un miembro es gratis;
cambiar su valor es una migración de datos.

Apaga todo esto si no estás de acuerdo:

```toml
[rules.placement]
enabled = false
```

---

## Channels

El patrón que esto reemplaza es un archivo de constantes string importado en
todos lados donde alguien publica:

```python
VINCULACION_COMPLETADA = "eventos:vinculacion_completada"
```

Eso funciona, y falla de tres formas concretas: nada verifica el payload, el
transporte queda soldado al call site, y nadie puede listar los canales que usa
un sistema.

```python
# channels.py
from jfastframework.channels import Channel

VINCULACION_COMPLETADA = Channel(
    "eventos:vinculacion_completada",
    description="A linkage finished; downstream balances may be stale.",
    required=("vinculacion_id", "rfc"),
)

LARAVEL_CHEQUES = Channel("LARAVEL_CHEQUES_EVENTS", backend="redis")
```

```python
await VINCULACION_COMPLETADA.publish({"vinculacion_id": 7, "rfc": "AAA010101AAA"})

@VINCULACION_COMPLETADA.on
async def recalculate(payload):
    ...
```

### El payload se valida donde se construye

```
ChannelError: Payload for 'eventos:vinculacion_completada' is missing rfc.
```

Esa falla pertenece al servicio que construyó el mensaje, no a un worker a tres
servicios de distancia que leyó una clave que ya no está.

`required` es un conjunto de nombres de claves y no un modelo, a propósito.
Estos payloads cruzan fronteras de lenguaje — Laravel publica en algunos de
ellos — así que el contrato que se puede hacer cumplir de los dos lados es
*estas claves están presentes*, no *esto es un modelo de pydantic*.

### Los backends son por canal

Mezclar es el caso normal, no un caso borde:

| Backend | Úsalo cuando | Ten en cuenta que |
| --- | --- | --- |
| `memory` | Por defecto. Dentro de un proceso. | La entrega es solo a este proceso — correcto para un monolito modular, incorrecto en el momento en que hay dos réplicas. |
| `redis` | Algo más, en otro lenguaje, publica o se suscribe. | No se retiene nada. Un suscriptor que no está conectado nunca ve el mensaje. |
| `kafka` | Un consumidor que estuvo caído tiene que ponerse al día. | Retenido y reproducible, y necesita el plugin `events`. |

El default no necesita infraestructura alguna, así que publicar un evento no es
una decisión que tengas que tomar el primer día. Mover un canal a Redis después
es una palabra clave en la declaración.

`redis` es para *esto cambió, refresca*. Para *haz este trabajo*, usa la queue
— reintenta, hace backoff y manda a dead-letter, y pub/sub no hace nada de eso.

### Listarlos

```bash
jfast describe --json | jq '.plugins[] | select(.name=="channels") | .channels'
```

Que es la tercera falla arreglada: los canales que habla un sistema están en un
archivo y un comando, en vez de repartidos por los módulos que resulte que
publican.
