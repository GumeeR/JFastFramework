# Rate limiting

Un endpoint de login sin rate limit es un blanco de credential stuffing. Este
plugin es el límite, y vive en la aplicación y no en el edge.

```toml
[plugins]
enabled = ["observability", "cache", "auth", "ratelimit"]

[plugin.ratelimit]
limit = 100
window = 60.0
```

Apagado salvo que lo actives. Un límite es una decisión de política con una
respuesta equivocada para alguien, y un servicio que empieza a rechazar tráfico
porque un default se encendió solo es peor que uno sin límite.

## Este es el limiter de capa de aplicación, no un reemplazo del edge

Un limiter en el edge — Caddy, un ingress, Cloudflare — ve direcciones y rutas.
Este ve lo que el edge no puede:

- **qué tenant** está llamando, porque el plugin de tenancy ya lo resolvió;
- **qué sujeto**, porque el plugin de auth ya verificó el token;
- **cuánto cuesta el endpoint**, porque una búsqueda vectorial no es un health
  check.

Los dos son complementarios y quieres los dos. El edge frena una inundación
volumétrica antes de que llegue a un worker; este evita que un cliente se gaste
la cuota de otro, y es la única capa que puede distinguir entre ambas cosas.
Fíjate en el límite hacia el otro lado: la aplicación del límite es una
dependencia por ruta, así que un request que **no** matchea ninguna ruta — una
inundación de 404, un escaneo — no queda limitado acá. Eso le toca al edge.

## Algoritmo: token bucket, evaluado dentro de Redis

Una ventana fija deja que alguien gaste una cuota completa a las 11:59:59 y otra
a las 12:00:00, así que el techo real es el doble del configurado en cada
frontera. Un token bucket no tiene frontera. Se rellena de forma continua a
`limit / window` tokens por segundo, y su tamaño de ráfaga es una perilla
explícita en lugar de un accidente de dónde cae el reloj. Además cuesta un hash
por identidad, mientras que un log de ventana deslizante cuesta una entrada por
request.

| Ajuste | Significado |
| --- | --- |
| `limit` | Requests por ventana. También la tasa de recarga: `limit / window` por segundo. |
| `window` | Segundos para rellenar por completo un bucket vacío. |
| `burst` | Tamaño del bucket. Por defecto `limit` — una ventana entera puede llegar de golpe. |

### Por qué un script Lua y no `GET` y después `SET`

Dos requests que leen el mismo contador antes de que alguno escriba **pasan los
dos**. Eso no es un entrelazado raro; es exactamente la carga para la que existe
un limiter, y un limiter que se escapa bajo carga es decoración. Redis ejecuta un
script hasta el final sin nada entrelazado, así que la lectura, la decisión y la
escritura son un único paso indivisible:

```lua
local clock = redis.call('TIME')
local now = tonumber(clock[1]) + tonumber(clock[2]) / 1000000

local stored = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(stored[1])
-- refill, then spend, then store -- all before anyone else runs
tokens = math.min(burst, tokens + (now - ts) * rate)
if tokens >= cost then allowed = 1; tokens = tokens - cost end
redis.call('HSET', key, 'tokens', tokens, 'ts', now)
```

Dos detalles que no son incidentales:

- **El reloj es el de Redis** (`TIME`), no el de quien llama. Workers en hosts
  distintos tienen relojes distintos, y un bucket compartido entre ellos no puede
  rellenarse a una tasa que dependa de qué worker preguntó. Eso vuelve al script
  no determinista, lo que exige replicación por efectos — el default desde Redis
  5.
- **Los floats cruzan como strings.** Redis trunca un número de Lua a entero en
  la salida, lo que redondearía cada token fraccionario hasta desaparecer.

El bucket está en Redis y no en el proceso porque un contador en memoria está mal
en el momento en que hay dos workers, y tanto `jfast dev` como el compose
generado levantan más de uno. La conexión viene del plugin `cache` — de ahí
`requires = ["cache"]` — en vez de abrir un segundo pool contra el mismo
servidor.

## Sobre qué se aplica el límite

`key_sources` es un orden de especificidad, y gana la primera fuente que
responde.

| Fuente | Clave | Notas |
| --- | --- | --- |
| `principal` | `sub:<subject>` | Del token verificado. La respuesta correcta cuando existe. |
| `api_key` | `key:<prefijo sha256>` | **Apagada salvo que definas `api_key_header`.** |
| `tenant` | `tenant:<id>` | Tráfico anónimo, limitado por tenant. |
| `ip` | `ip:<dirección>` | El fallback. |

Con la IP como clave, una oficina entera detrás de un NAT es un solo llamante.
Con el sujeto como clave, una cuenta comprometida no puede gastarse la cuota de
sus vecinos. Caer al tenant limita el tráfico anónimo por tenant en vez de
globalmente — lo que también significa que un llamante abusivo puede gastarse la
cuota anónima de ese tenant. Saca `tenant` de `key_sources` donde ese trade sea
el equivocado.

`api_key` está apagada por defecto a propósito. Un header sin verificar lo elige
quien llama, y un llamante que puede elegir su propio bucket no tiene ningún
límite. Define `api_key_header` solo donde algo aguas arriba ya verificó la
clave.

### La IP, detrás de un proxy

Detrás de un balanceador cada request lleva la dirección del balanceador, así que
un limiter con el peer del socket como clave le da a todo internet un único
bucket compartido.

Este plugin **nunca parsea `X-Forwarded-For` por su cuenta**. El header lo
escribe el atacante hasta que algo que conoce la topología de proxies lo recorre
desde la derecha y se detiene en el primer salto no confiable; un limiter que lo
cree en crudo le regala a cada llamante un bucket nuevo por cada salto
falsificado. El contrato es un solo atributo:

```python
request.state.client_ip = "203.0.113.7"   # written by the trusted-proxy middleware
```

Hasta que eso esté puesto se usa el peer del socket, y un request que llega con
headers de forwarding que nadie resolvió escribe un warning una vez, diciendo que
todos los clientes detrás del proxy están compartiendo un bucket.

## Costo por ruta

Un único límite global o queda demasiado apretado para las rutas baratas o
demasiado suelto para las caras. Sobrescríbelo por ruta con una dependencia, con
la misma forma que usa `require_scopes`:

```python
from fastapi import Depends
from jfastframework.plugins.builtin.ratelimit import rate_limit

@router.post("/search", dependencies=[Depends(rate_limit(10, 60))])
async def search(): ...

# One combined budget across a family of expensive endpoints.
@router.post("/embed", dependencies=[Depends(rate_limit(30, 60, scope="vectors"))])
async def embed(): ...

# Or keep the shared budget and charge more for this one.
@router.post("/reindex", dependencies=[Depends(rate_limit(100, 60, cost=25))])
async def reindex(): ...
```

Un override **reemplaza** el default de todo el servicio en esa ruta en vez de
sumarse a él, y tiene su propio bucket: gastar el presupuesto de `/search` no
toca el default. Las rutas que comparten un `scope` comparten presupuesto.

El default se instala en cada ruta de la API al arrancar, que es cuando los
routers terminaron de montarse. Las rutas agregadas después de que la aplicación
arrancó no lo reciben.

## La respuesta

`429`, con un problem document — la misma forma `application/problem+json` que
usa cualquier otro fallo en un servicio JFast:

```http
HTTP/1.1 429 Too Many Requests
Content-Type: application/problem+json
Retry-After: 12
RateLimit-Limit: 100
RateLimit-Remaining: 0
RateLimit-Reset: 47

{
  "type": "about:blank",
  "title": "Too Many Requests",
  "status": 429,
  "detail": "Rate limit of 100 request(s) per 60s exceeded.",
  "instance": "/search",
  "limit": 100,
  "remaining": 0,
  "retry_after": 12,
  "request_id": "01J..."
}
```

`RateLimit-Limit`, `RateLimit-Remaining` y `RateLimit-Reset` también van en las
respuestas exitosas, para que un cliente bien educado pueda bajar el ritmo antes
de que lo rechacen. `Reset` son los segundos hasta que el bucket vuelve a estar
lleno; `Retry-After` son los segundos hasta que se permitiría un request más, y
nunca es `0` — decirle a un cliente rechazado que reintente de inmediato es lo
único que no debe hacer.

## Cuando Redis se cae: fail open

**El limiter deja pasar el request, escribe un log en `WARNING`, y reporta el
servicio `degraded` en `/ready`.**

Fallar cerrado convierte una caída de la caché en una caída total: Redis
parpadea y cada endpoint de la flota empieza a responder 429, incluidos los que
nunca estuvieron cerca de un límite. Fallar abierto cuesta el límite mientras
dure la caída. Se cambia un problema de disponibilidad por una ventana de abuso,
y la ventana de abuso es la más barata de las dos.

Lo que hace defendible ese trade es que no es silencioso, porque "silenciosamente"
es toda la objeción:

```json
{
  "status": "degraded",
  "checks": {
    "ratelimit": {
      "healthy": false,
      "critical": false,
      "detail": "failing open: rate limit backend unreachable, no limit is being enforced",
      "meta": {"enforcing": false}
    }
  }
}
```

`/ready` sigue respondiendo `200` — el check no es crítico, porque sacar a cada
réplica de rotación es justamente la caída que este diseño existe para evitar —
pero dice `degraded` y nombra el motivo. Ponle una alerta.

Define `fail_open = false` en un servicio donde el límite importe más que el
endpoint.

## Ajustes

| Clave | Default | Significado |
| --- | --- | --- |
| `limit` | `100` | Requests por ventana. |
| `window` | `60.0` | Segundos para una recarga completa. |
| `burst` | `limit` | Tamaño del bucket. |
| `key_prefix` | nombre de la app | Namespace de claves en Redis. |
| `key_sources` | `["principal", "api_key", "tenant", "ip"]` | Orden de especificidad. |
| `api_key_header` | `""` | Apagado. Defínelo solo donde la clave se verifica aguas arriba. |
| `fail_open` | `true` | Ver arriba. |
| `exempt_paths` | probes y docs | `/health`, `/ready`, `/info`, `/metrics`, `/docs`, `/redoc`, `/openapi.json` |

Los probes están exentos por defecto y deberían seguir así: un orquestador que
consulta `/ready` cada dos segundos nunca puede quedar throttleado fuera de la
flota.

Los overrides por entorno usan el prefijo `JFAST_RATELIMIT_` —
`JFAST_RATELIMIT_LIMIT=500`.
